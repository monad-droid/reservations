"""Notifications via ntfy (https://ntfy.sh) or Telegram. Failures are logged, never raised."""

from __future__ import annotations

import logging
from typing import Optional

import requests

from .telegram import TelegramBot


class Notifier:
    def __init__(self, provider: str, server: str, topic: str, logger: logging.Logger, telegram: Optional[TelegramBot] = None):
        self.provider = provider
        self.telegram = telegram
        self.log = logger
        self.ntfy_url = ""
        if provider == "ntfy":
            if topic:
                self.ntfy_url = f"{server.rstrip('/')}/{topic}"
            else:
                logger.warning("notify.provider is ntfy but notify.ntfy.topic is empty; notifications disabled")
        elif provider == "telegram" and telegram is None:
            logger.warning("notify.provider is telegram but no bot configured; notifications disabled")

    def send(self, title: str, message: str, priority: str = "default") -> None:
        self.log.info("NOTIFY [%s] %s — %s", priority, title, message.replace("\n", " | "))
        if self.provider == "telegram" and self.telegram:
            self.telegram.send(f"{title}\n{message}")
        elif self.provider == "ntfy" and self.ntfy_url:
            try:
                r = requests.post(
                    self.ntfy_url,
                    data=message.encode("utf-8"),
                    headers={"Title": title, "Priority": priority, "Tags": "fork_and_knife"},
                    timeout=10,
                )
                if r.status_code >= 300:
                    self.log.warning("ntfy returned %s: %s", r.status_code, r.text[:200])
            except requests.RequestException as e:
                self.log.warning("ntfy send failed: %s", e)
