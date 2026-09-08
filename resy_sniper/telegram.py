"""Telegram Bot API over plain `requests`: outbound notifications plus a tiny command listener.

Commands (only honoured from the configured chat_id): /status, /stop, /help.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

import requests

DEFAULT_API_BASE = "https://api.telegram.org"
HELP = (
    "Commands:\n"
    "/status - what the bot is doing right now\n"
    "/target DATE [DATE...] [times...] - set the date(s) to go after (first to book wins) and optional times (HH:MM or HH:MM-HH:MM); the bot restarts with it\n"
    "/stop - stop the current run (nothing is booked)\n"
    "/help - this text"
)


class TelegramBot:
    def __init__(self, token: str, chat_id: int, logger: logging.Logger, api_base: Optional[str] = None):
        base = (api_base or os.environ.get("TELEGRAM_API_BASE") or DEFAULT_API_BASE).rstrip("/")
        self.url = f"{base}/bot{token}"
        self.chat_id = int(chat_id)
        self.log = logger
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._target_fn: Optional[Callable[[str], str]] = None

    # ----------------------------------------------------------------- send

    def send(self, text: str) -> bool:
        try:
            r = requests.post(self.url + "/sendMessage", json={"chat_id": self.chat_id, "text": text[:4000]}, timeout=10)
            if r.status_code >= 300:
                self.log.warning("telegram sendMessage -> %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as e:
            self.log.warning("telegram sendMessage failed: %s", e)
            return False

    # ------------------------------------------------------------- listener

    def start_listener(
        self,
        status_fn: Callable[[], str],
        stop_fn: Callable[[str], None],
        target_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        """target_fn receives the text after /target and returns a reply (it may restart the process)."""
        if self._thread:
            return
        self._target_fn = target_fn
        self._thread = threading.Thread(target=self._loop, args=(status_fn, stop_fn), name="telegram-listener", daemon=True)
        self._thread.start()
        self.log.info("telegram listener started (chat_id=%s); send /status or /stop", self.chat_id)

    def stop_listener(self) -> None:
        self._stop.set()

    def _loop(self, status_fn, stop_fn) -> None:
        offset: Optional[int] = None
        backoff = 1.0
        while not self._stop.is_set():
            try:
                params = {"timeout": 25, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                r = requests.get(self.url + "/getUpdates", params=params, timeout=35)
                if r.status_code != 200:
                    self.log.warning("telegram getUpdates -> %s: %s", r.status_code, r.text[:200])
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 1.0
                for upd in (r.json().get("result") or []):
                    offset = int(upd["update_id"]) + 1
                    self._handle(upd.get("message") or {}, status_fn, stop_fn, offset)
            except (requests.RequestException, ValueError, KeyError, TypeError) as e:
                self.log.warning("telegram listener error: %s", e)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def _ack(self, offset: Optional[int]) -> None:
        """Confirm updates up to `offset` so a restarted process does not see the same command again."""
        if offset is None:
            return
        try:
            requests.get(self.url + "/getUpdates", params={"offset": offset, "timeout": 0}, timeout=10)
        except requests.RequestException as e:
            self.log.warning("telegram ack failed: %s", e)

    def _handle(self, msg: dict, status_fn, stop_fn, offset: Optional[int] = None) -> None:
        chat = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if chat != self.chat_id:
            self.log.warning("telegram: ignoring message from unknown chat_id=%s (text=%r). Put this id in notify.telegram.chat_id if it is you.", chat, text[:60])
            return
        cmd = text.split()[0].lower().split("@")[0]
        self.log.info("telegram command from owner: %s", text[:80])
        if cmd == "/status":
            self.send(status_fn())
        elif cmd == "/stop":
            self.send("Stopping. Nothing will be booked by this run.")
            stop_fn("telegram /stop")
        elif cmd == "/target":
            args = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if self._target_fn is None:
                self.send("/target is not available in this mode.")
            else:
                self._ack(offset)  # the handler may restart the process; make sure this command is not replayed
                self.send(self._target_fn(args))
        elif cmd in ("/help", "/start"):
            self.send(HELP)
        else:
            self.send(f"Unknown command {cmd!r}.\n{HELP}")
