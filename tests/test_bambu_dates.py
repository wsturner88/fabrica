"""Bambu file dates: the FTPS `LIST` gives text without a year ("Jun 20 03:25"); the Hub turns it into epoch seconds like the other
printer drivers do, so the Date sort can order a Bambu's files.

    python3 tests/test_bambu_dates.py

No printer is contacted: the FTPS session is a fake and the clock is passed in. Names are generic.
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
_TMP = tempfile.mkdtemp(prefix="bambudates-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "metacache.json")

import bambu  # noqa: E402
from bambu import BambuPrinter, ftp_list_epoch  # noqa: E402


def ts(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi).timestamp()


NOW = ts(2026, 10, 18, 9, 30)


class ListDateTests(unittest.TestCase):
    def test_a_time_means_this_year(self):
        self.assertEqual(ftp_list_epoch("Jun 20 03:25", NOW), ts(2026, 6, 20, 3, 25))
        self.assertEqual(ftp_list_epoch("Aug 14 23:32", NOW), ts(2026, 8, 14, 23, 32))
        self.assertEqual(ftp_list_epoch("Oct 18 09:00", NOW), ts(2026, 10, 18, 9, 0))

    def test_a_year_means_an_older_file_at_midnight(self):
        self.assertEqual(ftp_list_epoch("Oct 17  2025", NOW), ts(2025, 10, 17))
        self.assertEqual(ftp_list_epoch("Jan 01 2020", NOW), ts(2020, 1, 1))

    def test_a_time_that_would_be_in_the_future_belongs_to_last_year(self):
        self.assertEqual(ftp_list_epoch("Dec 28 10:00", ts(2026, 1, 5, 8)), ts(2025, 12, 28, 10))       # year boundary: read in early January
        self.assertEqual(ftp_list_epoch("Jan 02 10:00", ts(2026, 1, 5, 8)), ts(2026, 1, 2, 10))
        self.assertEqual(ftp_list_epoch("Nov 01 10:00", NOW), ts(2025, 11, 1, 10))
        self.assertEqual(ftp_list_epoch("Oct 19 08:00", NOW), ts(2026, 10, 19, 8))                      # a day of clock slack: tomorrow morning is still "now"
        self.assertEqual(ftp_list_epoch("Dec 31 23:59", ts(2027, 1, 1, 0, 5)), ts(2026, 12, 31, 23, 59))

    def test_29_february(self):
        self.assertEqual(ftp_list_epoch("Feb 29 12:00", ts(2028, 3, 10)), ts(2028, 2, 29, 12))
        self.assertEqual(ftp_list_epoch("Feb 29 12:00", ts(2029, 3, 1)), ts(2028, 2, 29, 12))            # not a leap year now: the earlier one
        self.assertIsNone(ftp_list_epoch("Feb 29 12:00", ts(2027, 3, 1)))

    def test_what_is_not_a_date_gives_none(self):
        for bad in ("", None, "nonsense", "Foo 20 03:25", "Jun 99 03:25", "Jun 20 25:99", "Jun 20 03:25 extra"):
            self.assertIsNone(ftp_list_epoch(bad, NOW), bad)

    def test_the_ordering_is_right_across_the_year_end(self):
        now = ts(2026, 1, 10, 12)
        rows = ["Dec 20 10:00", "Jan 03 10:00", "Nov 02 10:00", "Dec 31 23:00"]
        got = sorted(rows, key=lambda r: -ftp_list_epoch(r, now))
        self.assertEqual(got, ["Jan 03 10:00", "Dec 31 23:00", "Dec 20 10:00", "Nov 02 10:00"])


LISTING = ["-rw-r--r--   1 root  root   1000 Jun 20 03:25 part.gcode.3mf",
           "-rw-r--r--   1 root  root   2048 Aug 14 23:32 other.3mf",
           "-rw-r--r--   1 root  root   3000 Oct 17  2025 old.gcode.3mf",
           "drwxr-xr-x   2 root  root   4096 Aug 14 23:32 cache",
           "-rw-r--r--   1 root  root     10 Feb 02 08:30 notes.txt"]


class FakeFTP:
    def __init__(self): pass
    def connect(self, *a, **k): pass
    def login(self, *a, **k): pass
    def prot_p(self): pass
    def set_pasv(self, *a): pass
    def quit(self): pass
    def close(self): pass
    def cwd(self, d): pass

    def retrlines(self, cmd, cb):
        for r in LISTING:
            cb(r)


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bambudates-")
        os.environ["BAMBU_MCP_CACHE"] = os.path.join(self.dir, "metacache.json")
        p = mock.patch.object(bambu, "ImplicitFTPTLS", FakeFTP)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make(self):
        prn = BambuPrinter("Shop A1", "127.0.0.1", "01P00A000000001", "12345678", "Bambu Lab A1")
        prn.snapshot = lambda: {"connected": True, "state": "IDLE", "start_pending": False}
        return prn

    def test_the_listing_carries_epoch_seconds_not_text(self):
        rows = self.make().sd_list()
        self.assertEqual([r["name"] for r in rows], ["part.gcode.3mf", "other.3mf", "old.gcode.3mf"])
        for r in rows:
            self.assertIsInstance(r["modified"], int, r)
        y = datetime.datetime.fromtimestamp(rows[0]["modified"]).year
        self.assertGreaterEqual(y, 2025)
        self.assertEqual(datetime.datetime.fromtimestamp(rows[2]["modified"]).date(), datetime.date(2025, 10, 17))
        newest_first = sorted(rows, key=lambda r: -r["modified"])
        self.assertEqual(newest_first[-1]["name"], "old.gcode.3mf")        # the one with a year is the oldest, whatever month it names

    def test_a_folder_listing_too(self):
        rows = self.make().sd_list_dir("cache")
        self.assertTrue(rows and all(isinstance(r["modified"], int) for r in rows))

    def test_a_listing_kept_on_disk_by_an_older_hub_is_read_against_the_day_it_was_taken(self):
        prn = self.make()
        taken = ts(2026, 1, 10, 12)
        prn._sd_persist(taken, [{"name": "a.3mf", "size": 5, "modified": "Dec 20 10:00"}, {"name": "b.3mf", "size": 5, "modified": "Jan 03 10:00"},
                                {"name": "c.3mf", "size": 5, "modified": 1700000000}, {"name": "d.3mf", "size": 5, "modified": ""}])
        prn2 = self.make()
        got = {r["name"]: r["modified"] for r in prn2._sd_load_persisted()[1]}
        self.assertEqual(got["a.3mf"], int(ts(2025, 12, 20, 10)))
        self.assertEqual(got["b.3mf"], int(ts(2026, 1, 3, 10)))
        self.assertEqual(got["c.3mf"], 1700000000)
        self.assertEqual(got["d.3mf"], "")

    def test_the_text_date_of_an_unknown_shape_is_kept_as_it_was(self):
        self.assertEqual(bambu._list_modified("sometime"), "sometime")
        self.assertEqual(bambu._list_modified(""), "")


if __name__ == "__main__":
    unittest.main()
