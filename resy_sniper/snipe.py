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
from .state import parse_drop_time, read_state, write_state
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


def _wait(seconds: float, status: Status, log: logging.Logger, label: str) -> None:
    """Sleep up to `seconds`, waking early on /stop; logs progress for long waits."""
    deadline = time.monotonic() + max(0.0, seconds)
    while not status.stopping:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if remaining > 600:
            log.info("%s in %.1f hours", label, remaining / 3600)
            status.stop_event.wait(min(remaining - 600, 1800))
        elif remaining > 60:
            log.info("%s in %.0fs", label, remaining)
            status.stop_event.wait(min(remaining - 60, 60))
        else:
            status.stop_event.wait(min(remaining, 1.0))


def _latest_pref_minutes(time_prefs: list[str]) -> int:
    """Latest minute-of-day among the preferences ('HH:MM' or 'HH:MM-HH:MM' -> its end)."""
    mins = []
    for p in time_prefs:
        end = p.split("-", 1)[-1]
        h, m = end.split(":")
        mins.append(int(h) * 60 + int(m))
    return max(mins) if mins else 0


def _cutoff(d: date, cfg: Config) -> datetime:
    """Give up on `d` at this moment: stop_hours_before hours before the latest preferred time (rolling margin)."""
    m = _latest_pref_minutes(cfg.time_preferences)
    latest = datetime.combine(d, dtime(m // 60, m % 60), tzinfo=cfg.tz)
    return latest - timedelta(hours=cfg.snipe_stop_hours_before)


def _not_too_soon(slots: list[Slot], cfg: Config, log: logging.Logger) -> list[Slot]:
    """Drop slots that start less than stop_hours_before hours from now (venue-local wall clock)."""
    earliest_ok = datetime.now(cfg.tz).replace(tzinfo=None) + timedelta(hours=cfg.snipe_stop_hours_before)
    kept = [s for s in slots if s.start >= earliest_ok]
    dropped = len(slots) - len(kept)
    if dropped:
        log.info("ignoring %d slot(s) starting before %s (less than %g h away)", dropped, earliest_ok.strftime("%Y-%m-%d %H:%M"), cfg.snipe_stop_hours_before)
    return kept


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
    """Go after one or more target dates; the first booking wins.

    Per date: fast polling (every poll_interval_s) from lead_seconds before its release moment until
    max_minutes after; afterwards (or if it was already open) a slow check every watch_interval_min
    until the day itself. All open dates are checked in the same round, in priority order.
    """
    status = status or Status()
    if not cfg.creds.auth_token:
        raise ResyError("RESY_AUTH_TOKEN is not set; /3/details and /3/book need it")
    if cfg.creds.payment_method_id is None:
        log.warning("RESY_PAYMENT_METHOD_ID is not set; /3/book will be sent without a payment method. "
                    "Works only if the venue does not require a card on file (Resy answers 402 otherwise).")

    params = load_params(cfg, window_override, drop_override, log)
    today = datetime.now(cfg.tz).date()

    if target_override:
        targets = [date.fromisoformat(target_override)]
    elif cfg.target_mode == "next_friday":
        targets = [next_friday_not_bookable(today, params.window_days)]
    else:
        targets = list(cfg.target_dates)
    if not targets:
        raise ResyError("no target dates")

    prior = _prior_booking(cfg, targets, today)
    if prior:
        log.info("this target set (%s) is done: %s. Not searching; send /target to set a new one.",
                 ", ".join(d.isoformat() for d in targets), prior)
        status.set(phase=f"done for this target set ({prior}). Send /target for a new one.")
        return 0

    venue = resolve_venue(client, cfg.venue_url_slug, cfg.venue_location, cfg.venue_id, log)
    log.info("venue: %s", venue)
    if venue.lead_time_in_days is not None and venue.lead_time_in_days != params.window_days:
        log.warning("Resy says lead_time_in_days=%s but using window_days=%s (%s)", venue.lead_time_in_days, params.window_days, params.source)

    lead = timedelta(seconds=cfg.snipe_lead_seconds)
    fast_len = timedelta(minutes=cfg.snipe_max_minutes)
    watch_s = cfg.snipe_watch_interval_min * 60
    release = {d: release_moment(d, params, cfg) for d in targets}
    now = datetime.now(cfg.tz)
    log.info(
        "targets=%s party=%d prefs=%s types=%s strict=%s | window_days=%d drop_time=%s (%s) fast poll %.2fs, watch every %g min%s",
        ", ".join(d.isoformat() for d in targets), cfg.party_size, cfg.time_preferences, cfg.table_types, cfg.table_types_strict,
        params.window_days, params.drop_time.strftime("%H:%M:%S"), params.source, cfg.snipe_poll_interval_s, cfg.snipe_watch_interval_min,
        " [DRY RUN: /3/book will NOT be sent]" if dry_run else "",
    )
    for d in targets:
        r = release[d]
        state = "already open" if now > r + fast_len else ("releasing now" if now >= r - lead else f"opens {r.isoformat(timespec='seconds')}")
        log.info("  %s (%s): %s; giving up at %s", d, d.strftime("%A"), state, _cutoff(d, cfg).strftime("%Y-%m-%d %H:%M"))
    status.set(
        venue=f"{venue.name or ''} (id {venue.venue_id})",
        target=", ".join(f"{d} ({d.strftime('%a')})" for d in targets) + f" at {'/'.join(cfg.time_preferences)} party {cfg.party_size}",
        releases="; ".join(f"{d}: {release[d].strftime('%Y-%m-%d %H:%M')}" for d in targets),
    )

    remaining = list(targets)
    seen: dict[date, dict[str, set[str]]] = {d: {} for d in targets}
    attempts: list[str] = []
    polls = 0
    checks = 0
    missed_notified: set[date] = set()

    def attempt(d: date, slots: list[Slot]) -> bool:
        for s in slots:
            seen[d].setdefault(s.hhmm, set()).add(s.table_type)
        ranked = rank_slots(_not_too_soon(slots, cfg, log), cfg.time_preferences, cfg.table_types, cfg.table_types_strict)
        if slots:
            log.info("%s: slots=%s | candidates in priority order: %s", d, summarize(slots), summarize(ranked) if ranked else "none match preferences")
        for cand in ranked:
            outcome = _try_book(client, cfg, venue.venue_id, d, cand, dry_run, log, notifier, status)
            attempts.append(f"{d} {cand.label()}: {outcome}")
            if outcome.startswith("BOOKED") or outcome.startswith("DRY-RUN"):
                _record_booking(cfg, targets, d, cand, outcome, dry_run, log)
                return True
        return False

    def fetch(d: date) -> Optional[list[Slot]]:
        try:
            return parse_find(client.find(venue.venue_id, d, cfg.party_size))
        except (AuthError, ChallengeError) as e:
            notifier.send("Resy snipe: stopped", str(e), priority="high")
            raise
        except (TransportError, RateLimitError, ApiError) as e:
            log.warning("%s: request error (continuing): %s", d, e)
            return None

    while remaining and not status.stopping:
        now = datetime.now(cfg.tz)
        expired = [d for d in remaining if now > _cutoff(d, cfg)]
        for d in expired:
            log.warning("%s: cutoff %s reached without a booking; dropping it", d, _cutoff(d, cfg).strftime("%Y-%m-%d %H:%M"))
        remaining = [d for d in remaining if d not in expired]
        if not remaining:
            break

        fast = [d for d in remaining if release[d] - lead <= now <= release[d] + fast_len]
        opened = [d for d in remaining if now > release[d] + fast_len]
        future = [d for d in remaining if now < release[d] - lead]

        if fast:
            t0 = time.monotonic()
            polls += 1
            status.set(phase=f"fast polling {', '.join(d.isoformat() for d in fast)}", polls=polls)
            for d in fast:
                slots = fetch(d)
                if slots is None:
                    continue
                if not slots:
                    log.info("poll #%d %s: no slots", polls, d)
                    status.set(last_poll=f"{d}: no slots")
                else:
                    status.set(last_poll=f"{d}: {summarize(slots, limit=8)}")
                    if attempt(d, slots):
                        return 0
            status.stop_event.wait(max(0.0, cfg.snipe_poll_interval_s - (time.monotonic() - t0)))
            continue

        # Dates whose fast window just ended: say so once (and drop them if watching is disabled).
        for d in opened:
            if d not in missed_notified and now <= release[d] + fast_len + timedelta(minutes=1):
                missed_notified.add(d)
                seen_desc = "; ".join(f"{t} [{', '.join(sorted(v))}]" for t, v in sorted(seen[d].items())) or "nothing"
                msg = f"{venue.name or venue.venue_id} {d}: nothing booked in the release window. Slots seen: {seen_desc}. Attempts: {attempts or 'none'}"
                log.error(msg)
                if watch_s > 0:
                    msg += f"\nNow checking every {cfg.snipe_watch_interval_min:g} min for a cancellation until the day itself."
                notifier.send("Resy snipe: missed the release", msg, priority="high")
        if watch_s <= 0 and opened:
            remaining = [d for d in remaining if d not in opened]
            if not remaining:
                break
            opened = []

        if opened:
            checks += 1
            status.set(phase=f"watching {', '.join(d.isoformat() for d in opened)} every {cfg.snipe_watch_interval_min:g} min", watch_checks=checks)
            for d in opened:
                slots = fetch(d)
                if slots is None:
                    continue
                ranked = rank_slots(_not_too_soon(slots, cfg, log), cfg.time_preferences, cfg.table_types, cfg.table_types_strict)
                log.info("watch check #%d %s: slots=%s | matching: %s", checks, d, summarize(slots, limit=12), summarize(ranked) if ranked else "none")
                status.set(last_poll=f"{d}: {summarize(slots, limit=8)}")
                if ranked and attempt(d, slots):
                    return 0

        waits: list[tuple[float, str]] = []
        if future:
            nxt = min(future, key=lambda d: release[d])
            waits.append(((release[nxt] - lead - datetime.now(cfg.tz)).total_seconds(), f"fast polling for {nxt} starts"))
        if opened:
            waits.append((watch_s, "next cancellation check"))
        if not waits:
            break
        soonest_cutoff = min((_cutoff(d, cfg) - datetime.now(cfg.tz)).total_seconds() for d in remaining)
        waits.append((max(0.0, soonest_cutoff) + 1, "cutoff"))
        wait_s, label = min(waits, key=lambda w: w[0])
        if not opened:
            status.set(phase=f"waiting for release of {nxt}")
        _wait(wait_s, status, log, label)

    if status.stopping:
        log.warning("snipe stopped by request; nothing booked (polls=%d, watch checks=%d)", polls, checks)
        _record_stopped(cfg, targets, log)
        notifier.send("Resy snipe: stopped", f"Stopped on request; nothing booked ({polls} polls, {checks} checks).")
        return 1
    msg = f"{venue.name or venue.venue_id}: none of {', '.join(d.isoformat() for d in targets)} could be booked. Attempts: {attempts or 'none'}"
    log.error(msg)
    notifier.send("Resy snipe: nothing booked", msg, priority="high")
    return 1


def _record_booking(cfg: Config, targets: list[date], d: date, slot: Slot, outcome: str, dry_run: bool, log: logging.Logger) -> None:
    """Remember the booking in the state file so a restart does not go after the other dates of this target set."""
    try:
        state = read_state(cfg.state_file) or {}
        state["last_booking"] = {
            "date": d.isoformat(),
            "slot": slot.label(),
            "outcome": outcome,
            "dry_run": dry_run,
            "targets": [t.isoformat() for t in targets],
            "time_preferences": list(cfg.time_preferences),
            "booked_at": datetime.now(cfg.tz).isoformat(timespec="seconds"),
        }
        write_state(cfg.state_file, state)
    except OSError as e:
        log.warning("could not record booking in %s: %s", cfg.state_file, e)


def _record_stopped(cfg: Config, targets: list[date], log: logging.Logger) -> None:
    """Remember that this target set was cancelled with /stop, so a restart does not resume it."""
    try:
        state = read_state(cfg.state_file) or {}
        state["stopped_targets"] = sorted(t.isoformat() for t in targets)
        write_state(cfg.state_file, state)
    except OSError as e:
        log.warning("could not record stop in %s: %s", cfg.state_file, e)


def _prior_booking(cfg: Config, targets: list[date], today: date) -> Optional[str]:
    """A recorded booking (or /stop) for this exact target set that is still relevant, or None."""
    state = read_state(cfg.state_file) or {}
    if sorted(state.get("stopped_targets") or []) == sorted(t.isoformat() for t in targets):
        return "stopped with /stop"
    lb = state.get("last_booking")
    if not isinstance(lb, dict):
        return None
    try:
        booked = date.fromisoformat(str(lb.get("date")))
    except ValueError:
        return None
    if booked < today:
        return None
    if sorted(lb.get("targets") or []) != sorted(t.isoformat() for t in targets):
        return None
    return f"{booked} {lb.get('slot')} ({'dry run' if lb.get('dry_run') else lb.get('outcome')})"


def clear_last_booking(state_file: str) -> None:
    """Forget the last booking and any /stop for the previous target set (a new /target is a new intent)."""
    state = read_state(state_file) or {}
    changed = state.pop("last_booking", None) is not None
    changed = (state.pop("stopped_targets", None) is not None) or changed
    if changed:
        write_state(state_file, state)


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
            "struct_payment_method": ('{"id":%s}' % cfg.creds.payment_method_id) if cfg.creds.payment_method_id is not None else "(omitted)",
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
        essential=True,
    )
    return f"BOOKED reservation_id={res_id}"
