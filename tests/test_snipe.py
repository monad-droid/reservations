import logging
import os
import tempfile
import unittest
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from resy_sniper.config import Config, Credentials
from resy_sniper.snipe import SnipeParams, next_friday_not_bookable, release_moment, load_params
from resy_sniper.state import parse_drop_time, write_state


def _cfg(tmp, **over):
    base = dict(
        venue_url_slug="gin-gins", venue_location="grand-rapids-mi", venue_id=None, party_size=2,
        target_mode="date", target_dates=[date(2026, 10, 9)], timezone="America/Detroit",
        time_preferences=["19:00"], table_types=["Dining Room"], table_types_strict=False,
        discover_poll_interval_s=60, discover_max_hours=48, discover_required_drops=2,
        snipe_lead_seconds=120, snipe_poll_interval_s=1.0, snipe_max_minutes=10, snipe_watch_interval_min=10, snipe_stop_hours_before=2,
        notify_provider="none", ntfy_server="https://ntfy.sh", ntfy_topic="", telegram_chat_id=None, notify_only_when_booked=False,
        state_file=os.path.join(tmp, "state.json"), log_file=os.path.join(tmp, "log.log"),
    )
    base.update(over)
    c = Config(**base)
    c.creds = Credentials("k", "t", 1)
    return c


class CutoffTests(unittest.TestCase):
    def test_cutoff_uses_earliest_pref(self):
        from resy_sniper.snipe import _cutoff
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, time_preferences=["19:30", "18:30-20:00"])
            self.assertEqual(_cutoff(date(2026, 9, 11), cfg), datetime(2026, 9, 11, 16, 30, tzinfo=ZoneInfo("America/Detroit")))
            cfg = _cfg(tmp, time_preferences=["19:00-20:00"], snipe_stop_hours_before=2)
            self.assertEqual(_cutoff(date(2026, 9, 11), cfg).strftime("%H:%M"), "17:00")


class TimingTests(unittest.TestCase):
    def test_next_friday(self):
        # 2026-09-07 is a Monday; window 30 -> last bookable 2026-10-07 (Wed) -> next Friday 2026-10-09
        self.assertEqual(next_friday_not_bookable(date(2026, 9, 7), 30), date(2026, 10, 9))
        # if last bookable is a Friday, the following Friday is the answer
        self.assertEqual(next_friday_not_bookable(date(2026, 9, 9), 30), date(2026, 10, 16))
        self.assertEqual(next_friday_not_bookable(date(2026, 9, 10), 0), date(2026, 9, 11))

    def test_release_moment_dst(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            p = SnipeParams(30, dtime(10, 0, 0), "cli")
            r = release_moment(date(2026, 10, 9), p, cfg)
            self.assertEqual(r, datetime(2026, 9, 9, 10, 0, tzinfo=ZoneInfo("America/Detroit")))
            self.assertEqual(r.utcoffset().total_seconds(), -4 * 3600)  # EDT
            # a target after the DST change still resolves to 10:00 local
            r2 = release_moment(date(2026, 12, 4), p, cfg)
            self.assertEqual(r2.utcoffset().total_seconds(), -5 * 3600)  # EST

    def test_parse_drop_time(self):
        self.assertEqual(parse_drop_time("10:00"), dtime(10, 0))
        self.assertEqual(parse_drop_time("00:00:30"), dtime(0, 0, 30))
        with self.assertRaises(ValueError):
            parse_drop_time("25:00")

    def test_load_params_state_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            log = logging.getLogger("t")
            with self.assertRaises(Exception):
                load_params(cfg, None, None, log)
            write_state(cfg.state_file, {"window_days": 30, "drop_time_local": "10:00:37", "timezone": "America/Detroit", "confirmed": True})
            p = load_params(cfg, None, None, log)
            self.assertEqual((p.window_days, p.drop_time, p.source), (30, dtime(10, 0, 37), "state+state"))
            p = load_params(cfg, 21, "12:00", log)
            self.assertEqual((p.window_days, p.drop_time, p.source), (21, dtime(12, 0), "cli+cli"))


if __name__ == "__main__":
    unittest.main()
