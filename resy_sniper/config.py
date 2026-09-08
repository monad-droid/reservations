"""Load config.yaml + .env into a validated Config object."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass
class Credentials:
    api_key: str
    auth_token: Optional[str]
    payment_method_id: Optional[int]
    telegram_bot_token: Optional[str] = None


@dataclass
class Config:
    venue_url_slug: str
    venue_location: str
    venue_id: Optional[int]
    party_size: int
    target_mode: str  # "date" | "next_friday"
    target_date: Optional[date]
    timezone: str
    time_preferences: list[str]
    table_types: list[str]
    table_types_strict: bool
    discover_poll_interval_s: float
    discover_max_hours: float
    discover_required_drops: int
    snipe_lead_seconds: float
    snipe_poll_interval_s: float
    snipe_max_minutes: float
    notify_provider: str
    ntfy_server: str
    ntfy_topic: str
    telegram_chat_id: Optional[int]
    state_file: str
    log_file: str
    creds: Credentials = field(repr=False, default=None)  # type: ignore[assignment]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _get(d: dict, path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _require(d: dict, path: str):
    val = _get(d, path, None)
    if val is None or (isinstance(val, str) and not val.strip()):
        raise ConfigError(f"config.yaml: missing required value '{path}'")
    return val


def load_credentials(env_path: Optional[str]) -> Credentials:
    if env_path:
        if not os.path.exists(env_path):
            raise ConfigError(f"env file not found: {env_path}")
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)  # searches for .env in cwd / parents
    api_key = (os.environ.get("RESY_API_KEY") or "").strip()
    if not api_key:
        raise ConfigError("RESY_API_KEY is not set (put it in .env; see README)")
    auth_token = (os.environ.get("RESY_AUTH_TOKEN") or "").strip() or None
    for name, val in (("RESY_API_KEY", api_key), ("RESY_AUTH_TOKEN", auth_token or "")):
        if not val.isascii() or any(c.isspace() for c in val):
            raise ConfigError(
                f"{name} in .env contains non-ASCII or whitespace characters (a masked copy like 'eyJ0eXAi•••' "
                "or a line break?). Re-copy the value directly from the browser's DevTools headers panel."
            )
    pm_raw = (os.environ.get("RESY_PAYMENT_METHOD_ID") or "").strip()
    payment_method_id: Optional[int] = None
    if pm_raw:
        if not pm_raw.isdigit():
            raise ConfigError("RESY_PAYMENT_METHOD_ID must be numeric")
        payment_method_id = int(pm_raw)
    tg = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None
    return Credentials(api_key=api_key, auth_token=auth_token, payment_method_id=payment_method_id, telegram_bot_token=tg)


def load_config(path: str, env_path: Optional[str] = None) -> Config:
    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a mapping")

    venue_id = _get(raw, "venue.venue_id")
    if venue_id is not None:
        if isinstance(venue_id, bool) or not isinstance(venue_id, int) or venue_id <= 0:
            raise ConfigError("venue.venue_id must be a positive integer or null")

    party_size = _require(raw, "party_size")
    if isinstance(party_size, bool) or not isinstance(party_size, int) or party_size < 1:
        raise ConfigError("party_size must be a positive integer")

    target_mode = str(_get(raw, "target.mode", "date")).strip().lower()
    if target_mode not in ("date", "next_friday"):
        raise ConfigError("target.mode must be 'date' or 'next_friday'")
    target_date: Optional[date] = None
    td_raw = _get(raw, "target.date")
    if td_raw not in (None, ""):
        if isinstance(td_raw, date):
            target_date = td_raw
        else:
            try:
                target_date = date.fromisoformat(str(td_raw))
            except ValueError as e:
                raise ConfigError(f"target.date must be YYYY-MM-DD: {e}") from e
    if target_mode == "date" and target_date is None:
        raise ConfigError("target.mode is 'date' but target.date is not set")

    tz_name = str(_get(raw, "timezone", "America/Detroit"))
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise ConfigError(f"unknown timezone '{tz_name}' (is tzdata installed?)") from e

    prefs_raw = _require(raw, "time_preferences")
    if not isinstance(prefs_raw, list) or not prefs_raw:
        raise ConfigError("time_preferences must be a non-empty list of HH:MM strings")
    prefs: list[str] = []
    for p in prefs_raw:
        s = str(p).strip()
        if not _TIME_RE.match(s):
            raise ConfigError(f"time_preferences entry '{p}' is not HH:MM (24h)")
        if s not in prefs:
            prefs.append(s)

    tt_raw = _get(raw, "table_types", []) or []
    if not isinstance(tt_raw, list):
        raise ConfigError("table_types must be a list")
    table_types = [str(t).strip() for t in tt_raw if str(t).strip()]
    strict = bool(_get(raw, "table_types_strict", False))
    if strict and not table_types:
        raise ConfigError("table_types_strict is true but table_types is empty")

    def _num(path, default, lo=None):
        v = _get(raw, path, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"{path} must be a number")
        if lo is not None and v < lo:
            raise ConfigError(f"{path} must be >= {lo}")
        return v

    snipe_poll = float(_num("snipe.poll_interval_s", 1.0))
    if snipe_poll < 0.5:
        raise ConfigError("snipe.poll_interval_s floor is 0.5 seconds")

    provider = str(_get(raw, "notify.provider", "none")).strip().lower()
    if provider not in ("ntfy", "telegram", "none"):
        raise ConfigError("notify.provider must be 'ntfy', 'telegram' or 'none'")
    chat_id_raw = _get(raw, "notify.telegram.chat_id")
    telegram_chat_id: Optional[int] = None
    if chat_id_raw not in (None, ""):
        if isinstance(chat_id_raw, bool) or not isinstance(chat_id_raw, int):
            if not (isinstance(chat_id_raw, str) and chat_id_raw.lstrip("-").isdigit()):
                raise ConfigError("notify.telegram.chat_id must be an integer")
            chat_id_raw = int(chat_id_raw)
        telegram_chat_id = int(chat_id_raw)
    if provider == "telegram" and telegram_chat_id is None:
        raise ConfigError("notify.provider is telegram but notify.telegram.chat_id is not set (see README)")

    cfg = Config(
        venue_url_slug=str(_require(raw, "venue.url_slug")).strip(),
        venue_location=str(_require(raw, "venue.location")).strip(),
        venue_id=venue_id,
        party_size=party_size,
        target_mode=target_mode,
        target_date=target_date,
        timezone=tz_name,
        time_preferences=prefs,
        table_types=table_types,
        table_types_strict=strict,
        discover_poll_interval_s=float(_num("discover.poll_interval_s", 60, lo=5)),
        discover_max_hours=float(_num("discover.max_hours", 48, lo=0.01)),
        discover_required_drops=int(_num("discover.required_drops", 2, lo=1)),
        snipe_lead_seconds=float(_num("snipe.lead_seconds", 120, lo=0)),
        snipe_poll_interval_s=snipe_poll,
        snipe_max_minutes=float(_num("snipe.max_minutes", 10, lo=0.1)),
        notify_provider=provider,
        ntfy_server=str(_get(raw, "notify.ntfy.server", "https://ntfy.sh")).rstrip("/"),
        ntfy_topic=str(_get(raw, "notify.ntfy.topic", "") or "").strip(),
        telegram_chat_id=telegram_chat_id,
        state_file=str(_get(raw, "state_file", "state.json")),
        log_file=str(_get(raw, "log_file", "logs/resy-sniper.log")),
    )
    cfg.creds = load_credentials(env_path)
    if cfg.notify_provider == "telegram" and not cfg.creds.telegram_bot_token:
        raise ConfigError("notify.provider is telegram but TELEGRAM_BOT_TOKEN is not set in .env")
    return cfg


def set_target_in_file(path: str, target: date, time_hhmm: Optional[str]) -> None:
    """Rewrite target.mode/target.date (and time_preferences if given) in config.yaml, keeping comments."""
    with open(path, "r", encoding="utf-8") as f:
        s = f.read()
    s, n = re.subn(r'(^\s*date:\s*)"?\d{4}-\d\d-\d\d"?', lambda m: f'{m.group(1)}"{target.isoformat()}"', s, count=1, flags=re.M)
    if n == 0:
        raise ConfigError("could not find target.date in config.yaml")
    s = re.sub(r"(^\s*mode:\s*)\w+", lambda m: f"{m.group(1)}date", s, count=1, flags=re.M)
    if time_hhmm:
        if not _TIME_RE.match(time_hhmm):
            raise ConfigError(f"time must be HH:MM, got {time_hhmm!r}")
        s, n = re.subn(r'(^time_preferences:\n)(?:\s*-\s*"?\d\d:\d\d"?\n)+', lambda m: f'{m.group(1)}  - "{time_hhmm}"\n', s, count=1, flags=re.M)
        if n == 0:
            raise ConfigError("could not find time_preferences in config.yaml")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(s)
    os.replace(tmp, path)
