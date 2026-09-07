import logging
import threading
import unittest
from unittest import mock

import requests

from resy_sniper.status import Status
from resy_sniper.telegram import TelegramBot


def _resp(status, body):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.headers["Content-Type"] = "application/json"
    return r


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.bot = TelegramBot("TOKEN", 424242, logging.getLogger("t"), api_base="https://api.telegram.org")

    @mock.patch("resy_sniper.telegram.requests.post")
    def test_send(self, post):
        post.return_value = _resp(200, '{"ok":true}')
        self.assertTrue(self.bot.send("hello"))
        self.assertEqual(post.call_args[0][0], "https://api.telegram.org/botTOKEN/sendMessage")
        self.assertEqual(post.call_args[1]["json"], {"chat_id": 424242, "text": "hello"})
        post.side_effect = requests.ConnectionError("down")
        self.assertFalse(self.bot.send("hello"))  # never raises

    @mock.patch("resy_sniper.telegram.requests.post")
    def test_commands_only_from_owner(self, post):
        post.return_value = _resp(200, '{"ok":true}')
        status = Status()
        status.set(phase="polling")
        self.bot._handle({"chat": {"id": 1}, "text": "/stop"}, status.text, status.request_stop)
        self.assertFalse(status.stopping)
        post.assert_not_called()
        self.bot._handle({"chat": {"id": 424242}, "text": "/status"}, status.text, status.request_stop)
        self.assertIn("phase: polling", post.call_args[1]["json"]["text"])
        self.bot._handle({"chat": {"id": 424242}, "text": "/stop@resy_bot"}, status.text, status.request_stop)
        self.assertTrue(status.stopping)

    @mock.patch("resy_sniper.telegram.requests.post")
    @mock.patch("resy_sniper.telegram.requests.get")
    def test_listener_advances_offset(self, get, post):
        post.return_value = _resp(200, '{"ok":true}')
        done = threading.Event()

        def fake_get(url, params=None, timeout=None):
            if "offset" not in params:
                return _resp(200, '{"ok":true,"result":[{"update_id":7,"message":{"chat":{"id":424242},"text":"/help"}}]}')
            self.assertEqual(params["offset"], 8)
            done.set()
            self.bot.stop_listener()
            return _resp(200, '{"ok":true,"result":[]}')

        get.side_effect = fake_get
        status = Status()
        self.bot.start_listener(status.text, status.request_stop)
        self.assertTrue(done.wait(5))
        self.assertIn("Commands:", post.call_args[1]["json"]["text"])


if __name__ == "__main__":
    unittest.main()
