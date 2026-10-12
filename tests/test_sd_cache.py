"""Regression tests for the busy-printer file list (2026-10-12): a printer that is printing never gets an FTPS
session, and its job list comes from the LAST listing taken while it was idle - however old, and even after a
Hub restart (the listing is saved next to the metadata cache).

    python3 tests/test_sd_cache.py          (needs: paho-mqtt)

Everything is faked: no printer, no FTPS server, no network. Files go to a temp directory.
"""
import glob
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

_TMP = tempfile.mkdtemp(prefix="sdcache-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "config.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")

import bambu  # noqa: E402
from bambu import BambuPrinter, PrinterBusy  # noqa: E402

LISTING = ["-rw-r--r--   1 root  root   1000 Jan 01 12:00 part.gcode.3mf",
           "-rw-r--r--   1 root  root   2048 Feb 02 08:30 other.3mf",
           "-rw-r--r--   1 root  root     10 Feb 02 08:30 notes.txt"]


class FakeFTP:
    made = []

    def __init__(self):
        FakeFTP.made.append(self)

    def connect(self, *a, **k): pass
    def login(self, *a, **k): pass
    def prot_p(self): pass
    def set_pasv(self, *a): pass
    def quit(self): pass
    def close(self): pass

    def retrlines(self, cmd, cb):
        for r in LISTING:
            cb(r)


def _slurp(path):
    with open(path) as fh:
        return fh.read()


class SdCacheCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sdcache-case-")
        os.environ["BAMBU_MCP_CACHE"] = os.path.join(self.dir, "metacache.json")
        FakeFTP.made = []
        p = mock.patch.object(bambu, "ImplicitFTPTLS", FakeFTP)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = {"state": "IDLE"}

    def make(self, name="Shop P1S"):
        prn = BambuPrinter(name, "127.0.0.1", "01P00A000000001", "12345678", "Bambu Lab P1S")
        prn.snapshot = lambda: {"connected": True, "state": self.state["state"], "start_pending": False}
        return prn

    def path(self, name="Shop P1S"):
        return self.make(name)._sd_listing_path()

    NAMES = ["part.gcode.3mf", "other.3mf"]

    def test_busy_with_fresh_cache(self):
        prn = self.make()
        first = prn.sd_list()
        self.assertEqual([r["name"] for r in first], self.NAMES)
        n = len(FakeFTP.made)
        self.state["state"] = "RUNNING"
        self.assertEqual(prn.sd_list(), first)
        self.assertEqual(len(FakeFTP.made), n, "FTPS opened while printing")

    def test_busy_with_old_cache_is_still_served(self):
        prn = self.make()
        first = prn.sd_list()
        t, rows = prn._sd_cache
        prn._sd_cache = (t - 30 * 86400, rows)             # a month-long print later
        self.state["state"] = "RUNNING"
        n = len(FakeFTP.made)
        self.assertEqual(prn.sd_list(), first)
        self.assertEqual(len(FakeFTP.made), n)
        self.assertEqual(prn.sd_listing_cached_at, t - 30 * 86400)

    def test_busy_with_no_listing_says_so_kindly(self):
        prn = self.make()
        self.state["state"] = "RUNNING"
        with self.assertRaises(PrinterBusy) as cm:
            prn.sd_list()
        msg = str(cm.exception)
        self.assertIn("The file list will appear when Shop P1S finishes printing", msg)
        self.assertIn("mid-print", msg)
        self.assertEqual(FakeFTP.made, [])
        self.assertFalse(os.path.exists(prn._sd_listing_path()))

    def test_restart_loads_the_saved_listing(self):
        first = self.make().sd_list()
        self.assertTrue(os.path.exists(self.path()))
        n = len(FakeFTP.made)
        new = self.make()                                  # the Hub restarted: empty memory
        self.assertIsNone(new._sd_cache)
        self.state["state"] = "RUNNING"
        self.assertEqual(new.sd_list(), first)
        self.assertEqual(len(FakeFTP.made), n, "FTPS opened after a restart while printing")
        self.assertIsNotNone(new.sd_listing_cached_at)

    def test_idle_uses_ftps_and_persists(self):
        prn = self.make()
        out = prn.sd_list()
        self.assertEqual(len(FakeFTP.made), 1)
        with open(prn._sd_listing_path()) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["files"], out)
        self.assertEqual(doc["printer"], "Shop P1S")
        self.assertTrue(os.path.basename(prn._sd_listing_path()).startswith("sd_listing_"))
        self.assertEqual(os.path.dirname(prn._sd_listing_path()), self.dir)
        prn.sd_list()
        self.assertEqual(len(FakeFTP.made), 2, "an idle printer is listed live every time")

    def test_file_mode_is_0600(self):
        prn = self.make()
        prn.sd_list()
        self.assertEqual(stat.S_IMODE(os.stat(prn._sd_listing_path()).st_mode), 0o600)

    def test_write_is_atomic(self):
        prn = self.make()
        first = prn.sd_list()
        path = prn._sd_listing_path()
        before = _slurp(path)
        with mock.patch.object(bambu.os, "replace", side_effect=OSError("disk gone")):
            prn._sd_persist(1.0, [{"name": "x.3mf", "size": 1, "modified": "Jan 01 00:00"}])   # must not raise
        self.assertEqual(_slurp(path), before, "a failed write damaged the saved listing")
        self.assertEqual(glob.glob(os.path.join(self.dir, "*.tmp")), [], "temp file left behind")
        self.assertEqual(self.make()._sd_load_persisted()[1], first)
        with mock.patch.object(bambu.os, "fsync", side_effect=OSError("no sync")) as fs:
            prn._sd_persist(2.0, [])
            self.assertTrue(fs.called, "the temp file is fsynced before the replace")
        self.assertEqual(_slurp(path), before)

    def test_damaged_or_foreign_file_is_ignored(self):
        prn = self.make()
        prn.sd_list()
        path = prn._sd_listing_path()
        self.state["state"] = "RUNNING"
        for junk in ("{not json", "[]", json.dumps({"version": 1, "printer": "Other", "serial": "x",
                                                     "taken_at": 1, "files": []})):
            with open(path, "w") as fh:
                fh.write(junk)
            with self.assertRaises(PrinterBusy):
                self.make().sd_list()

    def test_delete_and_upload_keep_the_saved_listing_honest(self):
        prn = self.make()
        prn.sd_list()
        FakeFTP.delete = lambda self, n: None
        FakeFTP.storbinary = lambda self, c, fh: None
        self.addCleanup(lambda: [delattr(FakeFTP, a) for a in ("delete", "storbinary")])
        prn.sd_delete("other.3mf")
        self.state["state"] = "RUNNING"
        self.assertEqual([r["name"] for r in self.make().sd_list()], ["part.gcode.3mf"])
        self.state["state"] = "IDLE"
        prn.sd_upload("new.3mf", b"x")
        self.assertFalse(os.path.exists(prn._sd_listing_path()), "a stale listing survived an upload")
        self.state["state"] = "RUNNING"
        with self.assertRaises(PrinterBusy):
            self.make().sd_list()

    def test_odd_printer_names_get_distinct_safe_files(self):
        a, b = self.path("A/B"), self.path("A_B")
        self.assertNotEqual(a, b)
        for p in (a, b, self.path("../../etc")):
            self.assertEqual(os.path.dirname(p), self.dir)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
