"""CLI: python -m resy_sniper {venue|find|discover|snipe} [options]"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from . import __version__
from .client import AuthError, ChallengeError, ResyClient, ResyError
from .config import ConfigError, load_config
from .discover import run_discover
from .logsetup import setup_logging
from .notify import Notifier
from .slots import parse_find, rank_slots, summarize
from .snipe import run_snipe
from .state import read_state
from .status import Status
from .telegram import TelegramBot
from .venue import resolve_venue

EXIT_OK = 0
EXIT_NOTHING_BOOKED = 1
EXIT_AUTH = 2
EXIT_CHALLENGE = 3
EXIT_CONFIG = 4
EXIT_ERROR = 5


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="resy_sniper", description="Personal Resy release-drop sniper")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    p.add_argument("--env", default=None, help="path to .env (default: ./.env if present)")
    p.add_argument("--dry-run", action="store_true", help="snipe: do everything except the final POST /3/book")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="mode", required=True)

    sub.add_parser("venue", help="resolve and print the numeric venue_id (plus lead_time_in_days if Resy reports it)")

    f = sub.add_parser("find", help="one GET /4/find for a day; prints parsed slots and how they rank")
    f.add_argument("--day", required=True, help="YYYY-MM-DD")

    sub.add_parser("discover", help="observe the release window and drop time; writes the state file")

    a = sub.add_parser("auto", help="discover (unless the state file is already confirmed), then snipe")
    a.add_argument("--target-date", default=None, help="override target date (YYYY-MM-DD)")
    a.add_argument("--dry-run", action="store_true", dest="dry_run_sub", help="same as global --dry-run")

    s = sub.add_parser("snipe", help="wait for the release moment and book")
    s.add_argument("--window-days", type=int, default=None, help="override window_days from the state file")
    s.add_argument("--drop-time", default=None, help="override drop time (HH:MM or HH:MM:SS, venue-local)")
    s.add_argument("--target-date", default=None, help="override target date (YYYY-MM-DD)")
    s.add_argument("--dry-run", action="store_true", dest="dry_run_sub", help="same as global --dry-run")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    dry_run = bool(args.dry_run or getattr(args, "dry_run_sub", False))

    try:
        cfg = load_config(args.config, args.env)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    log = setup_logging(cfg.log_file)
    log.info("resy_sniper %s mode=%s dry_run=%s config=%s", __version__, args.mode, dry_run, args.config)
    status = Status()
    status.set(mode=args.mode, dry_run=dry_run)
    telegram = None
    if cfg.notify_provider == "telegram" and cfg.creds.telegram_bot_token and cfg.telegram_chat_id is not None:
        telegram = TelegramBot(cfg.creds.telegram_bot_token, cfg.telegram_chat_id, log)
        if args.mode in ("discover", "snipe", "auto"):
            telegram.start_listener(status.text, status.request_stop)
    notifier = Notifier(cfg.notify_provider, cfg.ntfy_server, cfg.ntfy_topic, log, telegram=telegram)
    client = ResyClient(cfg.creds.api_key, cfg.creds.auth_token, log, mode=args.mode)

    try:
        if args.mode == "venue":
            info = resolve_venue(client, cfg.venue_url_slug, cfg.venue_location, cfg.venue_id, log)
            print(f"venue_id={info.venue_id}")
            print(f"name={info.name!r}")
            print(f"lead_time_in_days={info.lead_time_in_days}")
            print(f"time_zone={info.time_zone or 'unknown'}")
            return EXIT_OK

        if args.mode == "find":
            try:
                day = date.fromisoformat(args.day)
            except ValueError:
                print("--day must be YYYY-MM-DD", file=sys.stderr)
                return EXIT_CONFIG
            info = resolve_venue(client, cfg.venue_url_slug, cfg.venue_location, cfg.venue_id, log)
            slots = parse_find(client.find(info.venue_id, day, cfg.party_size))
            ranked = rank_slots(slots, cfg.time_preferences, cfg.table_types, cfg.table_types_strict)
            print(f"{day} party={cfg.party_size} venue_id={info.venue_id}")
            print(f"slots parsed: {summarize(slots, limit=200)}")
            print(f"ranked by preferences: {summarize(ranked, limit=200)}")
            return EXIT_OK

        if args.mode == "discover":
            return run_discover(cfg, client, notifier, log, status)

        if args.mode == "auto":
            state = read_state(cfg.state_file)
            if state and state.get("confirmed") and "drop_time_local" in state:
                log.info("state file %s is already confirmed (drop %s, window %s); skipping discover", cfg.state_file, state.get("drop_time_local"), state.get("window_days"))
            else:
                rc = run_discover(cfg, client, notifier, log, status)
                if rc != 0:
                    log.error("discover did not finish; not starting snipe")
                    return rc
                status.set(mode="auto", phase="discover done; starting snipe")
            return run_snipe(cfg, client, notifier, log, target_override=args.target_date, dry_run=dry_run, status=status)

        if args.mode == "snipe":
            if args.target_date:
                try:
                    date.fromisoformat(args.target_date)
                except ValueError:
                    print("--target-date must be YYYY-MM-DD", file=sys.stderr)
                    return EXIT_CONFIG
            return run_snipe(
                cfg,
                client,
                notifier,
                log,
                window_override=args.window_days,
                drop_override=args.drop_time,
                target_override=args.target_date,
                dry_run=dry_run,
                status=status,
            )
        return EXIT_CONFIG
    except AuthError as e:
        log.error("AUTH FAILURE: %s", e)
        notifier.send("Resy: auth failure", str(e), priority="high")
        return EXIT_AUTH
    except ChallengeError as e:
        log.error("CHALLENGE / UNEXPECTED RESPONSE: %s (raw body is in the log above)", e)
        notifier.send("Resy: challenge response", str(e), priority="high")
        return EXIT_CHALLENGE
    except ResyError as e:
        log.error("%s", e)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.warning("interrupted at %s", datetime.now().astimezone().isoformat(timespec="seconds"))
        return EXIT_ERROR
    finally:
        if telegram:
            telegram.stop_listener()


if __name__ == "__main__":
    sys.exit(main())
