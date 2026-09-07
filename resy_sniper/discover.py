"""MODE 1: learn the venue's release window (days ahead) and drop time by observation."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from .client import ChallengeError, ResyClient, ResyError, TransportError, AuthError, RateLimitError
from .config import Config
from .notify import Notifier
from .slots import parse_find, summarize
from .state import write_state
from .status import Status
from .venue import VenueInfo, resolve_venue

SCAN_MAX_DAYS = 60
SCAN_STOP_AFTER_EMPTY = 3  # stop scanning after this many consecutive empty days past the last day with slots


def _now(cfg: Config) -> datetime:
    return datetime.now(cfg.tz)


def log_calendar(client: ResyClient, cfg: Config, venue_id: int, today: date, log: logging.Logger) -> None:
    """Log /4/venue/calendar for the next 60 days. Informational only."""
    try:
        sched = client.get_calendar(venue_id, cfg.party_size, today, today + timedelta(days=SCAN_MAX_DAYS))
    except ResyError as e:
        log.warning("calendar endpoint failed (non-fatal): %s", e)
        return
    if not sched:
        log.info("calendar: empty 'scheduled' list")
        return
    statuses: dict[str, int] = {}
    last_open = None
    lines = []
    for entry in sched:
        d = str(entry.get("date"))
        inv = entry.get("inventory") or {}
        r = str(inv.get("reservation"))
        statuses[r] = statuses.get(r, 0) + 1
        if r in ("available", "sold-out"):
            last_open = d
        lines.append(f"{d}:{r}")
    log.info("calendar (%d days): reservation status counts=%s; last date marked available/sold-out=%s", len(sched), statuses, last_open)
    log.info("calendar detail: %s", " ".join(lines))
    if last_open:
        try:
            log.info("calendar-implied window: %d days ahead", (date.fromisoformat(last_open) - today).days)
        except ValueError:
            pass


def scan_window(client: ResyClient, cfg: Config, venue_id: int, today: date, log: logging.Logger, max_days: int) -> tuple[int | None, int | None]:
    """Poll /4/find day by day from today.

    Returns (first_empty_offset, last_with_slots_offset). Sold-out and closed days also return no
    slots, so the *last day with slots* is the better estimate of the window; both are logged.
    """
    first_empty = None
    last_with = None
    empties_since_last = 0
    for offset in range(0, max_days + 1):
        d = today + timedelta(days=offset)
        try:
            slots = parse_find(client.find(venue_id, d, cfg.party_size))
        except (AuthError, ChallengeError):
            raise
        except ResyError as e:
            log.warning("scan: %s (+%d) error: %s", d, offset, e)
            if first_empty is None:
                first_empty = offset
            empties_since_last += 1
            if last_with is not None and empties_since_last >= SCAN_STOP_AFTER_EMPTY:
                break
            continue
        log.info("scan: %s (+%d) slots=%s", d, offset, summarize(slots, limit=12))
        if slots:
            last_with = offset
            empties_since_last = 0
        else:
            if first_empty is None:
                first_empty = offset
            empties_since_last += 1
            if last_with is not None and empties_since_last >= SCAN_STOP_AFTER_EMPTY:
                break
    return first_empty, last_with


def run_discover(cfg: Config, client: ResyClient, notifier: Notifier, log: logging.Logger, status: Status | None = None) -> int:
    status = status or Status()
    venue: VenueInfo = resolve_venue(client, cfg.venue_url_slug, cfg.venue_location, cfg.venue_id, log)
    log.info("venue: %s", venue)
    status.set(venue=f"{venue.name or ''} (id {venue.venue_id})", phase="scanning current window")
    today = _now(cfg).date()
    log_calendar(client, cfg, venue.venue_id, today, log)

    lead = venue.lead_time_in_days
    max_scan = min(SCAN_MAX_DAYS, (lead + 3) if lead else 45)
    first_empty, last_with = scan_window(client, cfg, venue.venue_id, today, log, max_scan)
    log.info("scan result: first empty day offset=%s, last day with slots offset=%s, resy lead_time_in_days=%s", first_empty, last_with, lead)

    if lead is not None and lead > 0:
        window = lead
        if last_with is not None and last_with != lead:
            log.warning("scan disagrees with lead_time_in_days (%s vs %s); trusting lead_time_in_days and verifying by observation", last_with, lead)
    elif last_with is not None:
        window = last_with
    elif first_empty is not None and first_empty > 0:
        window = first_empty - 1
    else:
        msg = "could not establish the current booking window: no day returned slots and Resy gave no lead_time_in_days"
        log.error(msg)
        notifier.send("Resy discover: failed", msg, priority="high")
        return 1
    log.info("starting window estimate N=%d days; watching for the first slots on today+N+1 (and its neighbours)", window)
    status.set(phase="polling for a drop", window_estimate_days=window, resy_lead_time_in_days=lead, drops_observed=0)

    observed: dict[date, bool] = {}
    ever_had: set[date] = set()  # a date that had slots before and shows them again is a cancellation, not a drop
    drops: list[dict] = []
    started = time.monotonic()
    deadline = started + cfg.discover_max_hours * 3600
    poll_n = 0

    while time.monotonic() < deadline and not status.stopping:
        poll_n += 1
        now = _now(cfg)
        today = now.date()
        watch = today + timedelta(days=window + 1)
        status.set(polls=poll_n, watching=f"{watch - timedelta(days=1)} .. {watch + timedelta(days=1)}", last_poll=now.isoformat(timespec="seconds"))
        for d in (watch - timedelta(days=1), watch, watch + timedelta(days=1)):
            try:
                slots = parse_find(client.find(venue.venue_id, d, cfg.party_size))
            except (AuthError, ChallengeError) as e:
                notifier.send("Resy discover: stopped", str(e), priority="high")
                raise
            except (TransportError, RateLimitError, ResyError) as e:
                log.warning("discover poll %s error (continuing): %s", d, e)
                continue
            has = bool(slots)
            prev = observed.get(d)
            offset = (d - today).days
            if has:
                log.info("discover poll #%d %s (+%d): slots=%s", poll_n, d, offset, summarize(slots, limit=12))
            else:
                log.info("discover poll #%d %s (+%d): no slots", poll_n, d, offset)

            if prev is False and has and d not in ever_had:
                seen_at = _now(cfg)
                drop = {
                    "date": d.isoformat(),
                    "observed_at": seen_at.isoformat(timespec="seconds"),
                    "drop_time_local": seen_at.strftime("%H:%M:%S"),
                    "window_days": offset,
                    "first_slots": summarize(slots, limit=12),
                }
                drops.append(drop)
                log.info("*** DROP OBSERVED: %s became bookable at %s (%s), window_days=%d ***", d, drop["observed_at"], cfg.timezone, offset)
                if offset != window:
                    log.info("adjusting window estimate %d -> %d", window, offset)
                    window = offset
                status.set(drops_observed=len(drops), window_estimate_days=window, last_drop=f"{d} at {drop['drop_time_local']}")
                _persist(cfg, venue, drops, window, confirmed=len(drops) >= cfg.discover_required_drops, log=log)
                notifier.send(
                    "Resy discover: drop observed",
                    f"{venue.name or venue.venue_id}: {d} opened at {drop['drop_time_local']} {cfg.timezone} "
                    f"(window {offset} days). {len(drops)}/{cfg.discover_required_drops} observed.",
                )
            elif prev is None and has and offset > window:
                log.info("%s (+%d) is already bookable on first poll; window estimate %d -> %d", d, offset, window, offset)
                window = offset
            elif prev is False and has:
                log.info("%s (+%d) shows slots again after being empty (cancellation?); not counted as a drop", d, offset)
            observed[d] = has
            if has:
                ever_had.add(d)

        if len(drops) >= cfg.discover_required_drops:
            windows = {x["window_days"] for x in drops}
            if len(windows) != 1:
                log.warning("observed drops disagree on window_days: %s; using the latest", windows)
            notifier.send(
                "Resy discover: done",
                f"window_days={window}, drop_time_local={_earliest(drops)} {cfg.timezone}. State written to {cfg.state_file}.",
            )
            log.info("discover complete after %d drops; state written to %s", len(drops), cfg.state_file)
            return 0

        # sleep the remainder of the interval
        elapsed = time.monotonic() - started
        next_tick = ((elapsed // cfg.discover_poll_interval_s) + 1) * cfg.discover_poll_interval_s
        status.stop_event.wait(max(0.0, min(next_tick - elapsed, deadline - time.monotonic())))

    if status.stopping:
        log.warning("discover stopped by request; drops seen=%d, window estimate=%d", len(drops), window)
        notifier.send("Resy discover: stopped", f"Stopped on request after {len(drops)} drop(s); window estimate {window}.")
        return 1

    msg = (
        f"no drop observed within {cfg.discover_max_hours:g}h; drops seen={len(drops)}, "
        f"window estimate={window}, dates observed={ {k.isoformat(): v for k, v in observed.items()} }"
    )
    log.error(msg)
    notifier.send("Resy discover: timed out", msg, priority="high")
    return 1


def _earliest(drops: list[dict]) -> str:
    return min(x["drop_time_local"] for x in drops)


def _persist(cfg: Config, venue: VenueInfo, drops: list[dict], window: int, confirmed: bool, log: logging.Logger) -> None:
    state = {
        "venue_id": venue.venue_id,
        "venue_name": venue.name,
        "window_days": window,
        "drop_time_local": _earliest(drops),
        "timezone": cfg.timezone,
        "resy_lead_time_in_days": venue.lead_time_in_days,
        "observations": drops,
        "confirmed": confirmed,
    }
    write_state(cfg.state_file, state)
    log.info("state written (%s): %s", "confirmed" if confirmed else "provisional", {k: v for k, v in state.items() if k != "observations"})
