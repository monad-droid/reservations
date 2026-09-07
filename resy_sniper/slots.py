"""Parse /4/find responses and rank slots against the user's preferences."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

_START_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class Slot:
    start: datetime  # naive, venue-local (Resy returns local wall time)
    config_token: str  # config.token -> config_id for /3/details
    table_type: str  # config.type, e.g. "Dining Room"
    config_id: Optional[int] = None
    quantity: Optional[int] = None

    @property
    def hhmm(self) -> str:
        return self.start.strftime("%H:%M")

    def label(self) -> str:
        return f"{self.hhmm} {self.table_type or '?'}"


def _parse_start(raw: str) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    for fmt in _START_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def parse_find(payload: dict) -> list[Slot]:
    """results.venues[0].slots[] -> [Slot]. Unparseable entries are skipped."""
    if not isinstance(payload, dict):
        return []
    venues = (payload.get("results") or {}).get("venues") or []
    if not venues or not isinstance(venues, list):
        return []
    raw_slots = (venues[0] or {}).get("slots") or []
    out: list[Slot] = []
    for s in raw_slots:
        if not isinstance(s, dict):
            continue
        cfg = s.get("config") or {}
        dt = s.get("date") or {}
        start = _parse_start(dt.get("start"))
        token = cfg.get("token")
        if start is None or not token:
            continue
        cid = cfg.get("id")
        qty = s.get("quantity")
        out.append(
            Slot(
                start=start,
                config_token=str(token),
                table_type=str(cfg.get("type") or ""),
                config_id=cid if isinstance(cid, int) and not isinstance(cid, bool) else None,
                quantity=qty if isinstance(qty, int) and not isinstance(qty, bool) else None,
            )
        )
    out.sort(key=lambda x: x.start)
    return out


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("_", " ").split())


def _type_rank(table_type: str, prefs: list[str]) -> Optional[int]:
    """Index of the first preferred type matching, or None."""
    t = _norm(table_type)
    for i, p in enumerate(prefs):
        pn = _norm(p)
        if t == pn or (pn and pn in t):
            return i
    return None


def rank_slots(slots: list[Slot], time_prefs: list[str], table_prefs: list[str], strict: bool) -> list[Slot]:
    """Order candidates: by time priority, then by table-type priority within that time.

    Slots whose start time is not in `time_prefs` are excluded. If `strict`, slots whose
    type matches none of `table_prefs` are excluded; otherwise they come after matching ones.
    """
    ranked: list[tuple[int, int, int, Slot]] = []
    for idx, s in enumerate(slots):
        try:
            t_rank = time_prefs.index(s.hhmm)
        except ValueError:
            continue
        ty_rank = _type_rank(s.table_type, table_prefs)
        if ty_rank is None:
            if strict:
                continue
            ty_rank = len(table_prefs)  # after all preferred types
        ranked.append((t_rank, ty_rank, idx, s))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in ranked]


def summarize(slots: list[Slot], limit: int = 40) -> str:
    if not slots:
        return "none"
    parts = [s.label() for s in slots[:limit]]
    more = f" (+{len(slots) - limit} more)" if len(slots) > limit else ""
    return f"{len(slots)}: " + ", ".join(parts) + more
