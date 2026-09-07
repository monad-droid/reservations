"""Thin HTTP client for the api.resy.com endpoints this bot uses.

Request shapes follow github.com/Alkaar/resy-booking-bot (ResyApi.scala) and
github.com/jeffknaide/resy-bot (api_access.py) for /4/find, /3/details, /3/book,
and github.com/daylamtayari/cierge (resy/venue.go) for /3/venue, /2/config and
/4/venue/calendar. Nothing here tries to bypass rate limits or challenges.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from typing import Any, Optional

import requests

DEFAULT_BASE_URL = "https://api.resy.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Substrings that indicate a bot challenge / WAF page instead of an API response.
CHALLENGE_MARKERS = (
    "<html",
    "<!doctype html",
    "captcha",
    "cf-chl",
    "cf_chl",
    "just a moment",
    "challenge-platform",
    "px-captcha",
    "_pxhd",
    "perimeterx",
    "access denied",
    "request blocked",
)

RAW_LOG_LIMIT = 20_000  # chars of a raw challenge body to put in the log


class ResyError(Exception):
    """Base class; `status` and `body` are set when an HTTP response exists."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class AuthError(ResyError):
    """401 / 419: the auth token or api key is invalid or expired."""


class ChallengeError(ResyError):
    """Non-JSON / CAPTCHA / WAF response. Never retried, never bypassed."""


class RateLimitError(ResyError):
    """429 that persisted through the backoff budget."""


class TransportError(ResyError):
    """Network-level failure (DNS, connect, timeout, reset)."""


class SlotGoneError(ResyError):
    """/3/details or /3/book rejected the slot (taken, expired token, etc.)."""


class ApiError(ResyError):
    """Any other non-2xx response."""


def _preview(text: str, n: int = 300) -> str:
    t = text.replace("\n", " ").strip()
    return t if len(t) <= n else t[:n] + "…"


class ResyClient:
    def __init__(
        self,
        api_key: str,
        auth_token: Optional[str],
        logger: logging.Logger,
        mode: str = "cli",
        base_url: Optional[str] = None,
        max_429_retries: int = 6,
    ):
        self.log = logger
        self.mode = mode
        self.base_url = (base_url or os.environ.get("RESY_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
        self.max_429_retries = max_429_retries
        self.session = requests.Session()
        headers = {
            "Authorization": f'ResyAPI api_key="{api_key}"',
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://resy.com",
            "X-Origin": "https://resy.com",
            "Referer": "https://resy.com/",
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
        }
        if auth_token:
            headers["X-Resy-Auth-Token"] = auth_token
            headers["X-Resy-Universal-Auth"] = auth_token
        self.session.headers.update(headers)

    # ------------------------------------------------------------------ core

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: float = 5.0,
        summary: str = "",
    ) -> Any:
        url = self.base_url + path
        backoff = 1.0
        attempt = 0
        while True:
            attempt += 1
            t0 = time.monotonic()
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json_body,
                    headers=headers,
                    timeout=timeout,
                )
            except requests.RequestException as e:
                ms = (time.monotonic() - t0) * 1000
                self.log.warning("[%s] %s %s %s -> transport error after %.0fms: %s", self.mode, method, path, summary, ms, e)
                raise TransportError(f"{method} {path}: {e}") from e
            ms = (time.monotonic() - t0) * 1000
            text = resp.text or ""
            ctype = (resp.headers.get("Content-Type") or "").lower()

            self.log.info("[%s] %s %s %s -> %s %.0fms", self.mode, method, path, summary, resp.status_code, ms)

            if resp.status_code == 429:
                if attempt > self.max_429_retries:
                    raise RateLimitError(f"{path}: still 429 after {attempt - 1} retries", 429, text)
                retry_after = resp.headers.get("Retry-After")
                wait = backoff
                if retry_after and retry_after.strip().isdigit():
                    wait = max(wait, float(retry_after.strip()))
                wait = min(wait, 60.0)
                self.log.warning("[%s] 429 rate limited on %s; backing off %.1fs (attempt %d)", self.mode, path, wait, attempt)
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
                continue

            if resp.status_code in (401, 419):
                raise AuthError(
                    f"{path} returned {resp.status_code}: auth token / api key rejected or expired. "
                    "Re-extract RESY_AUTH_TOKEN (and RESY_API_KEY) from the browser and update .env.",
                    resp.status_code,
                    text,
                )

            lowered = text[:4000].lower()
            looks_html = "text/html" in ctype or lowered.lstrip().startswith("<")
            if looks_html or any(m in lowered for m in CHALLENGE_MARKERS):
                self.log.error(
                    "[%s] challenge/unexpected non-API response from %s (status %s, content-type %r). RAW BODY FOLLOWS:\n%s",
                    self.mode, path, resp.status_code, ctype, text[:RAW_LOG_LIMIT],
                )
                raise ChallengeError(
                    f"{path}: got a CAPTCHA/challenge or HTML page (status {resp.status_code}). Not bypassing.",
                    resp.status_code,
                    text,
                )

            try:
                payload = resp.json() if text.strip() else {}
            except ValueError:
                self.log.error("[%s] non-JSON response from %s (status %s). RAW BODY FOLLOWS:\n%s", self.mode, path, resp.status_code, text[:RAW_LOG_LIMIT])
                raise ChallengeError(f"{path}: non-JSON body (status {resp.status_code})", resp.status_code, text)

            if 200 <= resp.status_code < 300:
                return payload
            raise ApiError(f"{method} {path} -> {resp.status_code}: {_preview(text)}", resp.status_code, text)

    # ------------------------------------------------------------- endpoints

    def get_venue_by_slug(self, url_slug: str, location: str) -> dict:
        """GET /3/venue?url_slug=&location= -> venue object (id.resy, name, lead_time_in_days, locale.time_zone)."""
        return self._request(
            "GET", "/3/venue", params={"url_slug": url_slug, "location": location}, summary=f"slug={url_slug} loc={location}"
        )

    def search_venues(self, query: str, per_page: int = 10) -> list[dict]:
        """POST /3/venuesearch/search {"query","per_page"} -> search.hits[]  (fallback for venue_id resolution)."""
        payload = self._request(
            "POST",
            "/3/venuesearch/search",
            json_body={"query": query, "per_page": per_page},
            headers={"Content-Type": "application/json"},
            summary=f"query={query!r}",
        )
        hits = (payload or {}).get("search", {}).get("hits", [])
        return hits if isinstance(hits, list) else []

    def get_venue_config(self, venue_id: int) -> dict:
        """GET /2/config?venue_id= -> includes lead_time_in_days (how many days ahead reservations open)."""
        return self._request("GET", "/2/config", params={"venue_id": venue_id}, summary=f"venue_id={venue_id}")

    def get_calendar(self, venue_id: int, num_seats: int, start: date, end: date) -> list[dict]:
        """GET /4/venue/calendar -> scheduled[] of {date, inventory:{reservation,event,walk-in}}."""
        payload = self._request(
            "GET",
            "/4/venue/calendar",
            params={
                "venue_id": venue_id,
                "num_seats": num_seats,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
            summary=f"venue_id={venue_id} seats={num_seats} {start}..{end}",
        )
        sched = (payload or {}).get("scheduled", [])
        return sched if isinstance(sched, list) else []

    def find(self, venue_id: int, day: date, party_size: int) -> dict:
        """GET /4/find?lat=0&long=0&day=YYYY-MM-DD&party_size=&venue_id= -> raw payload."""
        return self._request(
            "GET",
            "/4/find",
            params={"lat": 0, "long": 0, "day": day.isoformat(), "party_size": party_size, "venue_id": venue_id},
            summary=f"day={day.isoformat()} party={party_size}",
            timeout=5.0,
        )

    def details(self, config_id: str, day: date, party_size: int) -> str:
        """GET /3/details?config_id=&day=&party_size= -> book_token.value"""
        try:
            payload = self._request(
                "GET",
                "/3/details",
                params={"config_id": config_id, "day": day.isoformat(), "party_size": party_size},
                summary=f"config_id={config_id[:24]}… day={day.isoformat()}",
                timeout=5.0,
            )
        except ApiError as e:
            if e.status in (400, 404, 409, 410, 412):
                raise SlotGoneError(f"/3/details rejected slot ({e.status}): {_preview(e.body)}", e.status, e.body) from e
            raise
        token = ((payload or {}).get("book_token") or {}).get("value")
        if not token:
            raise SlotGoneError(f"/3/details returned no book_token: {_preview(json.dumps(payload))}", 200, json.dumps(payload))
        return token

    def book(self, book_token: str, payment_method_id: Optional[int]) -> dict:
        """POST /3/book (form-encoded) book_token=&struct_payment_method={"id":N}&source_id=resy.com-venue-details"""
        form = {"book_token": book_token, "source_id": "resy.com-venue-details"}
        if payment_method_id is not None:
            form["struct_payment_method"] = json.dumps({"id": payment_method_id}, separators=(",", ":"))
        try:
            return self._request(
                "POST",
                "/3/book",
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                summary="book_token=…",
                timeout=10.0,
            )
        except ApiError as e:
            if e.status in (400, 404, 409, 410, 412):
                raise SlotGoneError(f"/3/book rejected ({e.status}): {_preview(e.body)}", e.status, e.body) from e
            if e.status == 402:
                raise ApiError(
                    f"/3/book -> 402 Payment Required: the venue needs a card on file; check RESY_PAYMENT_METHOD_ID. {_preview(e.body)}",
                    402,
                    e.body,
                ) from e
            raise


def extract_venue_id(venue: dict) -> Optional[int]:
    """Pull the numeric Resy venue id out of a /3/venue or search-hit object."""
    if not isinstance(venue, dict):
        return None
    vid = venue.get("id")
    if isinstance(vid, dict):
        vid = vid.get("resy")
    if vid is None and isinstance(venue.get("venue"), dict):
        return extract_venue_id(venue["venue"])
    if isinstance(vid, bool):
        return None
    if isinstance(vid, int):
        return vid
    if isinstance(vid, str) and vid.isdigit():
        return int(vid)
    return None
