"""Shared run status: what the modes report, what the Telegram listener reads, and the stop flag."""

from __future__ import annotations

import threading
from datetime import datetime


class Status:
    def __init__(self):
        self._lock = threading.Lock()
        self._fields: dict[str, str] = {}
        self.stop_event = threading.Event()
        self.started = datetime.now().astimezone()

    def set(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                self._fields[k] = str(v)

    def request_stop(self, reason: str) -> None:
        self.set(stop_reason=reason)
        self.stop_event.set()

    @property
    def stopping(self) -> bool:
        return self.stop_event.is_set()

    def text(self) -> str:
        with self._lock:
            items = dict(self._fields)
        now = datetime.now().astimezone()
        lines = [f"resy-sniper up since {self.started.strftime('%Y-%m-%d %H:%M:%S %Z')} (now {now.strftime('%H:%M:%S %Z')})"]
        for k, v in items.items():
            lines.append(f"{k}: {v}")
        return "\n".join(lines)
