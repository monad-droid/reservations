"""Tiny stand-in for api.resy.com used by the tests and for local rehearsal.

Response shapes mirror the reference bots' fixtures (Alkaar/resy-booking-bot test resources,
jeffknaide/resy-bot models, daylamtayari/cierge structs). Not affiliated with Resy.

    python -m tests.mock_resy [port] [--release-in SECONDS]

--release-in makes the target day (RELEASE_DAY env or 2026-10-09) return no slots until that many
seconds after start-up, so the snipe path can be rehearsed end to end.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VENUE_ID = 74321
WINDOW_DAYS = 30  # pre-drop, today+0 .. today+29 are bookable; RELEASE_DAY (today+30 in a consistent rehearsal) opens at RELEASE_AT
RELEASE_DAY = os.environ.get("RELEASE_DAY", "2026-10-09")
RELEASE_AT = [0.0]  # monotonic timestamp after which RELEASE_DAY has slots
BOOKED = []  # config tokens already booked (first booking of a taken slot -> 404)
TAKEN_ON_BOOK = set(os.environ.get("TAKEN_ON_BOOK", "").split(",")) - {""}
COUNTER = {"find": 0}
CONFIRMED = [0]  # highest getUpdates offset seen (fake Telegram)
STARTED = time.monotonic()


def _slots_for(day: str) -> list[dict]:
    base = [
        ("17:30:00", "Dining Room", 101),
        ("18:30:00", "Bar", 102),
        ("18:45:00", "Dining Room", 103),
        ("19:00:00", "Patio", 104),
        ("19:00:00", "Dining Room", 105),
        ("19:15:00", "Dining Room", 106),
        ("20:00:00", "Dining Room", 107),
        ("21:00:00", "Dining Room", 108),
    ]
    out = []
    for hhmmss, ttype, cid in base:
        tok = f"rgs://resy/{VENUE_ID}/{cid}/2/{day}/{day}/{hhmmss[:5]}/2/{ttype.replace(' ', '%20')}"
        out.append(
            {
                "config": {"id": cid, "type": ttype, "token": tok, "is_visible": True},
                "date": {"start": f"{day} {hhmmss}", "end": f"{day} {hhmmss}"},
                "quantity": 1,
                "size": {"min": 2, "max": 2},
                "payment": {"is_paid": False},
            }
        )
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, status: int, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization", "").startswith('ResyAPI api_key="')

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self._auth_ok() and not u.path.startswith("/bot"):
            return self._send(401, {"message": "unauthorized"})
        if u.path == "/3/venue":
            if q.get("url_slug") == "gin-gins" and q.get("location") == "grand-rapids-mi":
                return self._send(
                    200,
                    {
                        "id": {"resy": VENUE_ID, "google": "x"},
                        "name": "Gin Gin's",
                        "url_slug": "gin-gins",
                        "lead_time_in_days": WINDOW_DAYS,
                        "locale": {"currency": "USD", "time_zone": "America/Detroit"},
                        "location": {"id": 999, "url_slug": "grand-rapids-mi", "time_zone": "America/Detroit"},
                    },
                )
            return self._send(404, {"message": "not found"})
        if u.path == "/2/config":
            return self._send(200, {"lead_time_in_days": WINDOW_DAYS, "venue": {"name": "Gin Gin's", "min_party_size": 1, "max_party_size": 8}})
        if u.path == "/4/venue/calendar":
            start = date.fromisoformat(q["start_date"])
            end = date.fromisoformat(q["end_date"])
            sched = []
            d = start
            today = date.today()
            while d <= end:
                off = (d - today).days
                st = "closed" if d.weekday() == 0 else ("sold-out" if off <= WINDOW_DAYS - 3 else ("available" if off < WINDOW_DAYS else "not available"))
                sched.append({"date": d.isoformat(), "inventory": {"reservation": st, "event": "not available", "walk-in": "not available"}})
                d += timedelta(days=1)
            return self._send(200, {"scheduled": sched})
        if u.path == "/4/find":
            COUNTER["find"] += 1
            day = q.get("day", "")
            off = (date.fromisoformat(day) - date.today()).days
            if day == RELEASE_DAY:
                slots = _slots_for(day) if time.monotonic() >= RELEASE_AT[0] else []
            elif 0 <= off < WINDOW_DAYS and date.fromisoformat(day).weekday() != 0:
                slots = _slots_for(day) if off > WINDOW_DAYS - 3 else []  # earlier days sold out
            else:
                slots = []
            return self._send(200, {"results": {"venues": [{"venue": {"id": {"resy": VENUE_ID}, "name": "Gin Gin's"}, "slots": slots}]}, "query": {"day": day}})
        if u.path.startswith("/bot") and u.path.endswith("/getUpdates"):
            # fake Telegram: hand out one queued /status command (TELEGRAM_CMD env), then nothing
            q_off = int(q.get("offset", "0") or 0)
            CONFIRMED[0] = max(CONFIRMED[0], q_off)  # Telegram forgets updates older than the last offset seen
            cmd = os.environ.get("TELEGRAM_CMD", "/status")
            ready = time.monotonic() >= STARTED + float(os.environ.get("TELEGRAM_CMD_DELAY", "0"))
            upd = [] if CONFIRMED[0] > 1 or not cmd or not ready else [{"update_id": 1, "message": {"chat": {"id": 424242}, "text": cmd}}]
            if not upd:
                time.sleep(min(float(q.get("timeout", "1")), 2))
            return self._send(200, {"ok": True, "result": upd})
        if u.path == "/3/details":
            if not self.headers.get("X-Resy-Auth-Token"):
                return self._send(401, {"message": "no auth"})
            cid = q.get("config_id", "")
            if "/" not in cid:
                return self._send(400, {"message": "invalid config_id"})
            return self._send(200, {"book_token": {"value": f"BT|{cid}", "date_expires": "2099-01-01T00:00:00Z"}, "user": {"payment_methods": [{"id": 5555}]}})
        return self._send(404, {"message": "unknown endpoint"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode()
        if not self._auth_ok() and not u.path.startswith("/bot"):
            return self._send(401, {"message": "unauthorized"})
        if u.path in ("/4/find", "/3/details"):
            body = json.loads(raw or "{}")
            self.path = u.path + "?" + "&".join(f"{k}={v}" for k, v in body.items())
            return self.do_GET()
        if u.path == "/3/book":
            if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                return self._send(400, {"message": "expected form body"})
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            tok = form.get("book_token", "")
            spm = form.get("struct_payment_method", "")
            if not tok.startswith("BT|"):
                return self._send(400, {"message": "bad book_token"})
            try:
                pm = json.loads(spm)
                assert isinstance(pm.get("id"), int)
            except Exception:
                return self._send(400, {"message": "Invalid data received", "struct_payment_method": "invalid"})
            cid = tok[3:]
            if cid in BOOKED or any(t in cid for t in TAKEN_ON_BOOK):
                return self._send(404, {"message": "Not Found"})
            BOOKED.append(cid)
            return self._send(201, {"resy_token": "rgs://resy/RES/123", "reservation_id": 987654321, "venue_opt_in": False})
        if u.path.startswith("/bot") and u.path.endswith("/sendMessage"):
            body = json.loads(raw or "{}")
            print(f"[fake telegram] to chat {body.get('chat_id')}: {body.get('text')!r}", flush=True)
            return self._send(200, {"ok": True, "result": {"message_id": 1}})
        if u.path == "/3/venuesearch/search":
            return self._send(200, {"search": {"hits": [{"id": {"resy": VENUE_ID}, "name": "Gin Gin's", "url_slug": "gin-gins"}]}})
        return self._send(404, {"message": "unknown endpoint"})


def serve(port: int = 0, release_in: float = 0.0) -> ThreadingHTTPServer:
    RELEASE_AT[0] = time.monotonic() + release_in
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8765
    rel = 0.0
    if "--release-in" in sys.argv:
        rel = float(sys.argv[sys.argv.index("--release-in") + 1])
    srv = serve(port, rel)
    print(f"mock resy on http://127.0.0.1:{srv.server_port} (release day {RELEASE_DAY} opens in {rel:g}s)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
