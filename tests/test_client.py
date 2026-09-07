import logging
import unittest
from datetime import date
from unittest import mock

import requests

from resy_sniper.client import ApiError, AuthError, ChallengeError, RateLimitError, ResyClient, SlotGoneError, extract_venue_id


def _resp(status, body, ctype="application/json", headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode() if isinstance(body, str) else body
    r.headers["Content-Type"] = ctype
    for k, v in (headers or {}).items():
        r.headers[k] = v
    r.url = "https://api.resy.com/x"
    return r


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("test")
        self.client = ResyClient("KEY", "TOKEN", self.log, mode="test", base_url="https://api.resy.com")
        self.patcher = mock.patch.object(self.client.session, "request")
        self.req = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_headers(self):
        h = self.client.session.headers
        self.assertEqual(h["Authorization"], 'ResyAPI api_key="KEY"')
        self.assertEqual(h["X-Resy-Auth-Token"], "TOKEN")
        self.assertEqual(h["X-Origin"], "https://resy.com")
        self.assertIn("Chrome/", h["User-Agent"])

    def test_find_params(self):
        self.req.return_value = _resp(200, '{"results":{"venues":[{"slots":[]}]}}')
        self.client.find(123, date(2026, 10, 2), 2)
        _, kwargs = self.req.call_args
        self.assertEqual(kwargs["json"], {"day": "2026-10-02", "lat": 0, "long": 0, "party_size": 2, "venue_id": 123})
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(self.req.call_args[0], ("POST", "https://api.resy.com/4/find"))

    def test_details_and_book_shapes(self):
        self.req.return_value = _resp(200, '{"book_token":{"value":"BT1","date_expires":"x"},"user":{"payment_methods":[{"id":42}]}}')
        tok = self.client.details("rgs://resy/1/2/3", date(2026, 10, 9), 2)
        self.assertEqual(tok, "BT1")
        self.assertEqual(self.req.call_args[0], ("POST", "https://api.resy.com/3/details"))
        self.assertEqual(self.req.call_args[1]["json"], {"commit": 1, "config_id": "rgs://resy/1/2/3", "day": "2026-10-09", "party_size": 2})

        self.req.return_value = _resp(201, '{"resy_token":"RT","reservation_id":9}')
        out = self.client.book("BT1", 42)
        self.assertEqual(out["reservation_id"], 9)
        self.assertEqual(self.req.call_args[0], ("POST", "https://api.resy.com/3/book"))
        self.assertEqual(self.req.call_args[1]["data"], {"book_token": "BT1", "source_id": "resy.com-venue-details", "struct_payment_method": '{"id":42}'})
        self.assertEqual(self.req.call_args[1]["headers"]["Content-Type"], "application/x-www-form-urlencoded")

    def test_auth_errors(self):
        for code in (401, 419):
            self.req.return_value = _resp(code, '{"message":"nope"}')
            with self.assertRaises(AuthError):
                self.client.find(1, date(2026, 10, 2), 2)

    def test_challenge_html(self):
        self.req.return_value = _resp(403, "<!DOCTYPE html><html><title>Just a moment...</title></html>", ctype="text/html")
        with self.assertRaises(ChallengeError):
            self.client.find(1, date(2026, 10, 2), 2)

    def test_non_json(self):
        self.req.return_value = _resp(200, "definitely not json")
        with self.assertRaises(ChallengeError):
            self.client.find(1, date(2026, 10, 2), 2)

    @mock.patch("resy_sniper.client.time.sleep")
    def test_429_backoff_then_success(self, sleep):
        self.req.side_effect = [
            _resp(429, "{}", headers={"Retry-After": "2"}),
            _resp(429, "{}"),
            _resp(200, '{"results":{"venues":[{"slots":[]}]}}'),
        ]
        self.client.find(1, date(2026, 10, 2), 2)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 2.0])  # max(1,RetryAfter=2), then backoff 2

    @mock.patch("resy_sniper.client.time.sleep")
    def test_429_exhausted(self, sleep):
        self.client.max_429_retries = 2
        self.req.return_value = _resp(429, "{}")
        with self.assertRaises(RateLimitError):
            self.client.find(1, date(2026, 10, 2), 2)
        self.assertEqual(sleep.call_count, 2)

    def test_slot_gone(self):
        self.req.return_value = _resp(404, '{"message":"Not Found"}')
        with self.assertRaises(SlotGoneError):
            self.client.book("BT", 1)
        self.req.return_value = _resp(500, '{"message":"boom"}')
        with self.assertRaises(ApiError):
            self.client.book("BT", 1)

    def test_extract_venue_id(self):
        self.assertEqual(extract_venue_id({"id": {"resy": 5, "google": "g"}}), 5)
        self.assertEqual(extract_venue_id({"id": 7}), 7)
        self.assertEqual(extract_venue_id({"venue": {"id": {"resy": 9}}}), 9)
        self.assertIsNone(extract_venue_id({"id": {"google": "g"}}))


if __name__ == "__main__":
    unittest.main()
