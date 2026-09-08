import json
import unittest
from datetime import datetime

from resy_sniper.slots import parse_find, rank_slots, summarize

# Shape copied from Alkaar/resy-booking-bot src/test/resources/getReservations.json, plus the
# config.id / quantity fields documented in daylamtayari/cierge resy/slot.go.
FIND_FIXTURE = json.loads(
    """
{"results": {"venues": [{"venue": {"id": {"resy": 1}, "name": "X"}, "slots": [
  {"config": {"id": 1, "type": "Bar",         "token": "rgs://resy/1/1/2/2026-10-09/2026-10-09/19:00/2/Bar"},         "date": {"start": "2026-10-09 19:00:00", "end": "2026-10-09 20:30:00"}, "quantity": 1},
  {"config": {"id": 2, "type": "Dining Room", "token": "rgs://resy/1/2/2/2026-10-09/2026-10-09/19:00/2/Dining%20Room"}, "date": {"start": "2026-10-09 19:00:00", "end": "2026-10-09 20:30:00"}, "quantity": 1},
  {"config": {"id": 3, "type": "Dining Room", "token": "rgs://resy/1/3/2/2026-10-09/2026-10-09/17:30/2/Dining%20Room"}, "date": {"start": "2026-10-09 17:30:00", "end": "2026-10-09 19:00:00"}, "quantity": 1},
  {"config": {"id": 4, "type": "Patio",       "token": "rgs://resy/1/4/2/2026-10-09/2026-10-09/19:15/2/Patio"},       "date": {"start": "2026-10-09 19:15:00", "end": "2026-10-09 20:45:00"}, "quantity": 1},
  {"config": {"id": 5, "type": "Dining Room", "token": "rgs://resy/1/5/2/2026-10-09/2026-10-09/20:00/2/Dining%20Room"}, "date": {"start": "2026-10-09 20:00:00", "end": "2026-10-09 21:30:00"}, "quantity": 1},
  {"config": {"type": "Broken", "token": "x"}, "date": {"start": "not a date"}}
]}]}}
"""
)
PREFS = ["19:00", "19:15", "18:45", "19:30", "18:30", "19:45", "20:00"]


class ParseTests(unittest.TestCase):
    def test_parse_find(self):
        slots = parse_find(FIND_FIXTURE)
        self.assertEqual(len(slots), 5)  # the broken entry is skipped
        self.assertEqual(slots[0].start, datetime(2026, 10, 9, 17, 30))  # sorted by start
        self.assertEqual(slots[0].table_type, "Dining Room")
        self.assertEqual(slots[0].config_id, 3)
        self.assertEqual(slots[0].quantity, 1)
        self.assertTrue(slots[0].config_token.startswith("rgs://resy/1/3/"))

    def test_parse_empty_and_garbage(self):
        self.assertEqual(parse_find({"results": {"venues": [{"slots": []}]}}), [])
        self.assertEqual(parse_find({"results": {"venues": []}}), [])
        self.assertEqual(parse_find({}), [])
        self.assertEqual(parse_find(None), [])

    def test_summarize(self):
        self.assertEqual(summarize([]), "none")
        self.assertTrue(summarize(parse_find(FIND_FIXTURE)).startswith("5: 17:30 Dining Room, 19:00 Bar"))


class RankTests(unittest.TestCase):
    def test_prefers_time_then_table_type(self):
        ranked = rank_slots(parse_find(FIND_FIXTURE), PREFS, ["Dining Room"], strict=False)
        self.assertEqual([s.label() for s in ranked], ["19:00 Dining Room", "19:00 Bar", "19:15 Patio", "20:00 Dining Room"])

    def test_strict_table_type(self):
        ranked = rank_slots(parse_find(FIND_FIXTURE), PREFS, ["dining room"], strict=True)
        self.assertEqual([s.label() for s in ranked], ["19:00 Dining Room", "20:00 Dining Room"])

    def test_no_table_preference(self):
        ranked = rank_slots(parse_find(FIND_FIXTURE), ["19:00"], [], strict=False)
        self.assertEqual([s.label() for s in ranked], ["19:00 Bar", "19:00 Dining Room"])  # original order kept

    def test_ranges(self):
        slots = parse_find(FIND_FIXTURE)  # 17:30, 19:00 x2, 19:15, 20:00
        # exact first, then a range: 19:30 absent -> range 18:30-20:00, middle 19:15
        ranked = rank_slots(slots, ["19:30", "18:30-20:00"], ["Dining Room"], strict=False)
        self.assertEqual([s.label() for s in ranked], ["19:15 Patio", "19:00 Dining Room", "19:00 Bar", "20:00 Dining Room"])
        # range only, middle 18:45 -> 19:00 (15 min) before 17:30 (75) and 20:00 (75)
        ranked = rank_slots(slots, ["17:30-20:00"], [], strict=False)
        self.assertEqual([s.hhmm for s in ranked], ["19:00", "19:00", "19:15", "17:30", "20:00"])

    def test_unlisted_times_excluded(self):
        ranked = rank_slots(parse_find(FIND_FIXTURE), ["17:30"], ["Dining Room"], strict=False)
        self.assertEqual([s.label() for s in ranked], ["17:30 Dining Room"])
        self.assertEqual(rank_slots(parse_find(FIND_FIXTURE), ["12:00"], [], False), [])


if __name__ == "__main__":
    unittest.main()
