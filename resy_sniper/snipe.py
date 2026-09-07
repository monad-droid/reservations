"""MODE 2: wait for the release moment, then book the best matching slot within seconds."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

from .client import (
    ApiError,
    AuthError,
    ChallengeError,
    RateLimitError,
    ResyClient,
    ResyError,
    SlotGoneError,
    TransportError,
)
from .config import Config
from .notify import Notifier
from .slots import Slot, parse_find, rank_slots, summarize
from .state import parse_drop_time, read_state
from .status import Status
from .venue import resolve_venue


class SnipeParams:
    def __init__(self, window_days: int, drop_time: dtime, source: str):
        self.window_days = window_days
        self.drop_time = drop_time
        self.source = source


def load_params(cfg: Config, window_override: Optional[int], drop_override: Optional[str], log: logging.Logger) -> SnipeParams:
    state = read_state(cfg.state_file)
    window = window_override
    drop = parse_drop_time(drop_override) if drop_override else None
    src = []
    if window is None:
        if not state or "window_days" not in state:
            raise ResyError(f"no --window-days given and {cfg.state_file} has no window_days; run discover first")
        window = int(state["window_days"])
        src.append("state")
    else:
        src.append("cli")
    if drop is None:
        if not state or "drop_time_local" not in state:
            raise ResyError(f"no --drop-time given and {cfg.state_file} has no drop_time_local; run discover first")
        drop = parse_drop_time(str(state["drop_time_local"]))
        src.append("state")
    else:
        src.append("cli")
    if state and state.get("timezone") and state["timezone"] != cfg.timezone:
        log.warning("state timezone %s differs from config timezone %s; using config", state["timezone"], cfg.timezone)
    if state and not state.get("confirmed", True) and window_override is None:
        log.warning("state file is provisional (only one drop observed)")
    if window < 0:
        raise ResyError("window_days must be >= 0")
    return SnipeParams(window, drop, "+".join(src))


def next_friday_not_bookable(today: date, window_days: int) -> date:
    """Earliest Friday strictly after the last currently-bookable date (today + window_days)."""
    last_bookable = today + timedelta(days=window_days)
    d = last_bookable + timedelta(days=1)
    while d.weekday() != 4:  # Monday=0 ... Friday=4
        d += timedelta(days=1)
    return d


def release_moment(target: date, params: SnipeParams, cfg: Config) -> datetime:
    release_date = target - timedelta(days=params.window_days)
    return datetime.combine(release_date, params.drop_time, tzinfo=cfg.tz)


def _sleep_until(when: datetime, cfg: Config, log: logging.Logger, status: Status) -> None:
    while not status.stopping:
        remaining = (when - datetime.now(cfg.tz)).total_seconds()
        if remaining <= 0:
            return
        if remaining > 600:
            log.info("sleeping; %.1f hours until polling starts at %s", remaining / 3600, when.isoformat(timespec="seconds"))
            status.stop_event.wait(min(remaining - 600, 1800))
        elif remaining > 60:
            log.info("polling starts in %.0fs", remaining)
            status.stop_event.wait(min(remaining - 60, 60))
        else:
            status.stop_event.wait(min(remaining, 1.0))


def run_snipe(
    cfg: Config,
    client: ResyClient,
    notifier: Notifier,
    log: logging.Logger,
    *,
    window_override: Optional[int] = None,
    drop_override: Optional[str] = None,
    target_override: Optional[str] = None,
    dry_run: bool = False,
    status: Optional[Status] = None,
) -> int:
    status = status or Status()
    if not cfg.creds.auth_token:
        raise ResyError("RESY_AUTH_TOKEN is not set; /3/details and /3/book need it")
    if not dry_run and cfg.creds.payment_method_id is None:
        raise ResyError("RESY_PAYMENT_METHOD_ID is not set; required to POST /3/book (or use --dry-run)")
    if dry_run and cfg.creds.payment_method_id is None:
        log.warning("dry run without RESY_PAYMENT_METHOD_ID; a real run would refuse to start")

    params = load_params(cfg, window_override, drop_override, log)
    today = datetime.now(cfg.tz).date()

    if target_override:
        target = date.fromisoformat(target_override)
    elif cfg.target_mode == "next_friday":
        target = next_friday_not_bookable(today, params.window_days)
    else:
        target = cfg.target_date  # validated non-None in config
    assert target is not None

    venue = resolve_venue(client, cfg.venue_url_slug, cfg.venue_location, cfg.venue_id, log)
    log.info("venue: %s", venue)
    if venue.lead_time_in_days is not None and venue.lead_time_in_days != params.window_days:
        log.warning("Resy says lead_time_in_days=%s but using window_days=%s (%s)", venue.lead_time_in_days, params.window_days, params.source)

    release = release_moment(target, params, cfg)
    start_at = release - timedelta(seconds=cfg.snipe_lead_seconds)
    stop_at = release + timedelta(minutes=cfg.snipe_max_minutes)
    now = datetime.now(cfg.tz)
    log.info(
        "target=%s (%s) party=%d prefs=%s types=%s strict=%s | window_days=%d drop_time=%s (%s) -> release %s; poll from %s to %s every %.2fs%s",
        target,
        target.strftime("%A"),
        cfg.party_size,
        cfg.time_preferences,
        cfg.table_types,
        cfg.table_types_strict,
        params.window_days,
        params.drop_time.strftime("%H:%M:%S"),
        params.source,
        release.isoformat(timespec="seconds"),
        start_at.isoformat(timespec="seconds"),
        stop_at.isoformat(timespec="seconds"),
        cfg.snipe_poll_interval_s,
        " [DRY RUN: /3/book will NOT be sent]" if dry_run else "",
    )
    status.set(
        venue=f"{venue.name or ''} (id {venue.venue_id})",
        target=f"{target} ({target.strftime('%A')}) party {cfg.party_size}",
        release=release.isoformat(timespec="seconds"),
        polling_window=f"{start_at.strftime('%H:%M:%S')} - {stop_at.strftime('%H:%M:%S')}",
        phase="waiting for release",
    )
    if now > stop_at:
        log.warning("release moment %s is already more than %g minutes in the past; polling once anyway for %g minutes", release, cfg.snipe_max_minutes, cfg.snipe_max_minutes)
        stop_at = now + timedelta(minutes=cfg.snipe_max_minutes)
    elif now > release:
        log.warning("release moment %s already passed; polling immediately", release)

    _sleep_until(start_at, cfg, log, status)
    if status.stopping:
        log.warning("snipe stopped by request before polling started")
        notifier.send("Resy snipe: stopped", "Stopped on request before the release; nothing booked.")
        return 1
    log.info("polling started for %s", target)
    status.set(phase="polling")

    seen: dict[str, set[str]] = {}
    attempts: list[str] = []
    polls = 0
    stop_mono = time.monotonic() + max(0.0, (stop_at - datetime.now(cfg.tz)).total_seconds())

    while time.monotonic() < stop_mono and not status.stopping:
        t0 = time.monotonic()
        polls += 1
        try:
            slots = parse_find(client.find(venue.venue_id, target, cfg.party_size))
        except (AuthError, ChallengeError) as e:
            notifier.send("Resy snipe: stopped", str(e), priority="high")
            raise
        except (TransportError, RateLimitError, ApiError) as e:
            log.warning("poll #%d error (continuing): %s", polls, e)
            slots = []
        if slots:
            for s in slots:
                seen.setdefault(s.hhmm, set()).add(s.table_type)
            ranked = rank_slots(slots, cfg.time_preferences, cfg.table_types, cfg.table_types_strict)
            log.info("poll #%d: slots=%s | candidates in priority order: %s", polls, summarize(slots), summarize(ranked) if ranked else "none match preferences")
            status.set(polls=polls, last_poll=summarize(slots, limit=8))
            for cand in ranked:
                outcome = _try_book(client, cfg, venue.venue_id, target, cand, dry_run, log, notifier, status)
                attempts.append(f"{cand.label()}: {outcome}")
                if outcome.startswith("BOOKED") or outcome.startswith("DRY-RUN"):
                    return 0
        else:
            log.info("poll #%d: no slots", polls)
            status.set(polls=polls, last_poll="no slots")
        elapsed = time.monotonic() - t0
        status.stop_event.wait(max(0.0, cfg.snipe_poll_interval_s - elapsed))

    if status.stopping:
        log.warning("snipe stopped by request after %d polls; nothing booked", polls)
        notifier.send("Resy snipe: stopped", f"Stopped on request after {polls} polls; nothing booked.")
        return 1

    seen_desc = "; ".join(f"{t} [{', '.join(sorted(v))}]" for t, v in sorted(seen.items())) or "nothing"
    msg = (
        f"{venue.name or venue.venue_id} {target}: nothing booked after {polls} polls "
        f"(release {release.strftime('%H:%M:%S')}, window {params.window_days}d). Slots seen: {seen_desc}. "
        f"Attempts: {attempts or 'none'}"
    )
    log.error(msg)
    notifier.send("Resy snipe: missed", msg, priority="high")
    return 1


def _try_book(client: ResyClient, cfg: Config, venue_id: int, target: date, slot: Slot, dry_run: bool, log: logging.Logger, notifier: Notifier, status: Status) -> str:
    log.info("attempting %s (config_id=%s)", slot.label(), slot.config_token)
    try:
        token = client.details(slot.config_token, target, cfg.party_size)
    except SlotGoneError as e:
        log.warning("details failed for %s: %s", slot.label(), e)
        return f"details rejected ({e.status})"
    except (TransportError, RateLimitError, ApiError) as e:
        log.warning("details error for %s: %s", slot.label(), e)
        return f"details error ({e})"
    log.info("book_token obtained for %s (len=%d)", slot.label(), len(token))
    if dry_run:
        form_preview = {
            "book_token": token[:12] + "…",
            "struct_payment_method": '{"id":%s}' % cfg.creds.payment_method_id,
            "source_id": "resy.com-venue-details",
        }
        log.info("DRY RUN: would POST /3/book %s for %s %s party=%d", form_preview, target, slot.label(), cfg.party_size)
        notifier.send("Resy snipe: dry run OK", f"Would have booked {target} {slot.label()} for {cfg.party_size}")
        return "DRY-RUN (no /3/book sent)"
    try:
        resp = client.book(token, cfg.creds.payment_method_id)
    except SlotGoneError as e:
        log.warning("book rejected for %s: %s", slot.label(), e)
        return f"book rejected ({e.status})"
    except (TransportError, RateLimitError, ApiError) as e:
        log.warning("book error for %s: %s", slot.label(), e)
        return f"book error ({e})"
    resy_token = resp.get("resy_token")
    res_id = resp.get("reservation_id")
    log.info("*** BOOKED %s %s party=%d reservation_id=%s resy_token=%s ***", target, slot.label(), cfg.party_size, res_id, resy_token)
    status.set(phase="booked", booked=f"{target} {slot.label()} reservation_id={res_id}")
    log.info("book response: %s", resp)
    notifier.send(
        "Resy: BOOKED",
        f"{target} {slot.label()} for {cfg.party_size}. reservation_id={res_id}",
        priority="high",
    )
    return f"BOOKED reservation_id={res_id}"
