"""Keep running through the two failures that used to stop the bot.

* AuthError (401/419): the token expired. Tell the owner once, then re-read .env every minute and
  resume as soon as RESY_AUTH_TOKEN changes. No restart needed.
* ChallengeError (403 / CAPTCHA / HTML): pause with backoff (15, 30, 60 min ...) and try again.
  Nothing is bypassed; the bot simply waits for the block to lift.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from dotenv import dotenv_values

from .client import ResyClient
from .notify import Notifier
from .status import Status

import os as _os

AUTH_RECHECK_S = float(_os.environ.get("RESY_AUTH_RECHECK_S", "60"))  # env override is for tests only
CHALLENGE_BACKOFF_MIN = tuple(float(x) for x in _os.environ.get("RESY_CHALLENGE_BACKOFF_MIN", "15,30,60").split(","))
HEALTHY_RUN_S = 30 * 60  # a run that lasted this long resets the challenge backoff


class Recovery:
    def __init__(self, env_path: Optional[str], client: ResyClient, notifier: Notifier, status: Status, log: logging.Logger):
        self.env_path = env_path
        self.client = client
        self.notifier = notifier
        self.status = status
        self.log = log
        self.challenge_strikes = 0
        self.run_started = time.monotonic()

    def mark_run_start(self) -> None:
        self.run_started = time.monotonic()

    def _current_token(self) -> Optional[str]:
        if not self.env_path:
            return None
        try:
            return (dotenv_values(self.env_path).get("RESY_AUTH_TOKEN") or "").strip() or None
        except OSError as e:
            self.log.warning("could not read %s: %s", self.env_path, e)
            return None

    def wait_for_new_token(self, error: Exception) -> bool:
        """Block until RESY_AUTH_TOKEN in .env changes. Returns False on shutdown."""
        old = self._current_token()
        self.log.error("AUTH FAILURE: %s", error)
        self.status.set(phase="PAUSED: Resy token expired. Update RESY_AUTH_TOKEN in .env; the bot picks it up within a minute.")
        self.notifier.send(
            "Resy: token expired (bot paused, not stopped)",
            f"{error}\n\nUpdate RESY_AUTH_TOKEN in {self.env_path or '.env'} on the server. "
            "No restart needed: the bot re-reads the file every minute and resumes on its own.",
            priority="high",
            essential=True,
        )
        while not self.status.shutting_down:
            self.status.shutdown_event.wait(AUTH_RECHECK_S)
            if self.status.shutting_down:
                return False
            new = self._current_token()
            if new and new != old and new.isascii():
                self.client.set_auth_token(new)
                self.log.info("new RESY_AUTH_TOKEN picked up from %s; resuming", self.env_path)
                self.notifier.send("Resy: new token picked up", "Resuming.", essential=True)
                self.status.set(phase="resuming after token refresh")
                return True
        return False

    def wait_out_challenge(self, error: Exception) -> bool:
        """Back off after a challenge/blocked response. Returns False on shutdown."""
        if time.monotonic() - self.run_started > HEALTHY_RUN_S:
            self.challenge_strikes = 0
        wait_min = CHALLENGE_BACKOFF_MIN[min(self.challenge_strikes, len(CHALLENGE_BACKOFF_MIN) - 1)]
        self.challenge_strikes += 1
        self.log.error("CHALLENGE / BLOCKED: %s. Pausing %g min (strike %d), then retrying.", error, wait_min, self.challenge_strikes)
        self.status.set(phase=f"PAUSED {wait_min:g} min after a challenge/blocked response (strike {self.challenge_strikes}); will retry")
        if self.challenge_strikes == 1:
            self.notifier.send(
                "Resy: blocked/challenge page (bot paused, will retry)",
                f"{error}\nRetrying in {wait_min} min; backing off up to 60 min if it keeps happening. Nothing to do unless this persists.",
                priority="high",
                essential=True,
            )
        self.status.shutdown_event.wait(wait_min * 60)
        return not self.status.shutting_down
