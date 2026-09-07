"""Learned release schedule, persisted as JSON."""

from __future__ import annotations

import json
import os
from datetime import datetime, time
from typing import Optional


def read_state(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_state(path: str, state: dict) -> None:
    state = dict(state)
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def parse_drop_time(s: str) -> time:
    """'HH:MM' or 'HH:MM:SS' -> time"""
    parts = s.strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(f"drop time must be HH:MM or HH:MM:SS, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    sec = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60):
        raise ValueError(f"drop time out of range: {s!r}")
    return time(h, m, sec)
