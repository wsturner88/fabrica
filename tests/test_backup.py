"""Tests for app/backup.py — stdlib only, no printers, no external network.

    python3 tests/test_backup.py

OctoPrint, PrusaLink and Moonraker are exercised against throwaway HTTP fakes on
127.0.0.1 (the PrusaLink / Moonraker fakes come from their own test files); Bambu FTPS
is mocked at the method boundary (sd_list / sd_download / snapshot), and the Bambu
`cache/` folder is exercised through a fake FTP connection under the real driver.
"""
import ftplib
import hashlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import backup  # noqa: E402
import test_moonraker as tmr  # noqa: E402
import test_prusalink as tpl  # noqa: E402
from bambu import BambuPrinter, OctoPrintPrinter, PrinterError  # noqa: E402
from moonraker import MoonrakerPrinter  # noqa: E402
from prusalink import PrusaLinkPrinter  # noqa: E402

FILES = {
    "benchy.gcode": b"G1 X1 Y1\n" * 5000,
    "parts/bracket v2.gcode": b"G1 X2 Y2\n" * 3000,   # folder + space in the name
    "parts/lid.bgcode": b"\x00\x01binary" * 400,
    "gone.gcode": b"vanishes between listing and download\n",
}
KEY = "test-key"


class FakeOcto(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("X-Api-Key") != KEY:
            self.send_response(403); self.end_headers(); return
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/files":
            kids = {}
            for path, data in FILES.items():
                folder, _, name = path.rpartition("/")
                item = {"name": name, "path": path, "type": "machinecode",
                        "size": len(data), "date": 1700000000, "origin": "local"}
                kids.setdefault(folder, []).append(item)
            files = kids.pop("", [])
            for folder, items in kids.items():
                files.append({"name": folder, "path": folder, "type": "folder", "children": items})
            body = json.dumps({"files": files}).encode()
        elif u.path.startswith("/downloads/files/local/"):
            rel = urllib.parse.unquote(u.path[len("/downloads/files/local/"):])
            if rel == "gone.gcode" or rel not in FILES:
                self.send_response(404); self.end_headers(); return
            body = FILES[rel]
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeBambu:
    kind = "bambu"

    def __init__(self, name="BX1", state="IDLE", files=None):
        self.name, self.model, self.state = name, "Test Model", state
        self.files = files if files is not None else {"a.gcode.3mf": b"A" * 1000, "b.gcode.3mf": b"B" * 2000}
        self.gate = None          # optional Event: sd_download blocks until set
        self.downloaded = []
        self.flip_after = None    # switch to RUNNING after this many downloads

    def snapshot(self):
        return {"printer": self.name, "state": self.state, "connected": self.state != "UNKNOWN",
                "job": "some job"}

    def sd_list(self):
        return [{"name": n, "size": len(d), "modified": "Jan 01 12:00"} for n, d in self.files.items()]

    def sd_download(self, name):
        if self.gate:
            self.gate.wait(10)
        self.downloaded.append(name)
        if self.flip_after is not None and len(self.downloaded) >= self.flip_after:
            self.state = "RUNNING"
        return self.files[name]


def wait_done(mgr, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if not mgr.status()["running"]:
            return mgr.status()
        time.sleep(0.05)
    raise AssertionError("backup did not finish")


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOcto)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "backups")
        os.environ.pop("BACKUP_KEEP", None)

    def tearDown(self):
        self.tmp.cleanup()

    def mgr(self, *printers):
        return backup.BackupManager({p.name.lower(): p for p in printers}, root=self.root,
                                    pause_between_files=0)

    def octo(self, name="OC1"):
        return OctoPrintPrinter(name, self.url, KEY, "Test Octo")

    def test_octoprint_end_to_end(self):
        m = self.mgr(self.octo())
        res = m.start("oc1")
        self.assertIn(res["job"]["status"], ("queued", "running"))
        st = wait_done(m)
        job = st["recent"][0]
        self.assertEqual(job["status"], "partial")           # gone.gcode 404s
        self.assertEqual([f["name"] for f in job["failed_files"]], ["gone.gcode"])
        self.assertEqual(job["files_done"], 4)
        rows = st["backups"]["OC1"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_count"], 3)
        path = m.resolve_download("OC1", rows[0]["file"])
        with zipfile.ZipFile(path) as z:
            self.assertIsNone(z.testzip())
            names = set(z.namelist())
            self.assertEqual(names, {"benchy.gcode", "parts/bracket v2.gcode",
                                     "parts/lid.bgcode", "manifest.json"})
            man = json.loads(z.read("manifest.json"))
            self.assertEqual(man["printer"], "OC1")
            self.assertEqual(man["model"], "Test Octo")
            self.assertFalse(man["complete"])
            self.assertEqual(man["totals"]["failed"], 1)
            self.assertEqual(len(man["files"]), 3)
            for f in man["files"]:
                data = z.read(f["name"])
                self.assertEqual(data, FILES[f["name"]])
                self.assertEqual(hashlib.sha256(data).hexdigest(), f["sha256"])
                self.assertEqual(f["size"], len(data))
        leftovers = [n for n in os.listdir(os.path.join(self.root, "OC1")) if not n.endswith(".zip")]
        self.assertEqual(leftovers, [])                       # no .part / temp files

    def test_retention_prunes_to_n(self):
        os.environ["BACKUP_KEEP"] = "2"
        m = self.mgr(FakeBambu())
        for _ in range(4):
            m.start("bx1")
            wait_done(m)
        zips = sorted(os.listdir(os.path.join(self.root, "BX1")))
        self.assertEqual(len(zips), 2)
        self.assertEqual(zips, sorted(zips))
        self.assertEqual(len(m.status()["backups"]["BX1"]), 2)

    def test_busy_bambu_refused(self):
        for state in ("RUNNING", "PREPARE", "PAUSE", "PAUSED", "UNKNOWN"):
            m = self.mgr(FakeBambu(state=state))
            with self.assertRaises(backup.BackupBusy, msg=state) as cx:
                m.start("BX1")
            self.assertEqual(cx.exception.status, 423)
        self.assertFalse(os.path.exists(os.path.join(self.root, "BX1")))

    def test_print_starting_mid_backup_aborts(self):
        p = FakeBambu(files={f"f{i}.gcode.3mf": b"x" * 100 for i in range(5)})
        p.flip_after = 2
        m = self.mgr(p)
        m.start("BX1")
        job = wait_done(m)["recent"][0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("RUNNING", job["error"])
        self.assertEqual(len(p.downloaded), 2)                # stopped, did not pull the rest
        self.assertEqual(os.listdir(os.path.join(self.root, "BX1")), [])

    def test_second_start_while_running_is_409(self):
        p = FakeBambu()
        p.gate = threading.Event()
        m = self.mgr(p)
        m.start("BX1")
        with self.assertRaises(backup.BackupRunning) as cx:
            m.start("BX1")
        self.assertEqual(cx.exception.status, 409)
        with self.assertRaises(backup.BackupRunning):
            m.start("all")                                    # nothing else to queue
        self.assertEqual(len(m.status()["running"]), 1)
        p.gate.set()
        self.assertEqual(wait_done(m)["recent"][0]["status"], "done")

    def test_all_runs_each_printer_and_skips_busy(self):
        a, b, c = FakeBambu("BA"), FakeBambu("BB"), FakeBambu("BC", state="RUNNING")
        m = self.mgr(a, b, c)
        res = m.start("all")
        self.assertEqual(sorted(j["printer"] for j in res["jobs"]), ["BA", "BB"])
        self.assertEqual([s["printer"] for s in res["skipped"]], ["BC"])
        st = wait_done(m)
        self.assertEqual(sorted(st["backups"]), ["BA", "BB"])

    def test_unknown_printer(self):
        with self.assertRaises(backup.UnknownPrinter):
            self.mgr(FakeBambu()).start("nope")

    def test_empty_listing_writes_nothing(self):
        m = self.mgr(FakeBambu(files={}))
        m.start("BX1")
        job = wait_done(m)["recent"][0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(m.status()["backups"], {})

    def test_download_path_validation(self):
        m = self.mgr(FakeBambu())
        m.start("BX1")
        good = wait_done(m)["backups"]["BX1"][0]["file"]
        self.assertTrue(m.resolve_download("BX1", good))
        outside = os.path.join(self.tmp.name, "BX1-20240101-000000.zip")
        with open(outside, "wb") as fh:
            fh.write(b"x")
        os.makedirs(os.path.join(self.root, "evil"))
        for printer, fn in [
            ("..", good), (".", good), ("BX1", "../" + good), ("BX1/..", good),
            ("BX1", "..%2f" + good), ("BX1", "/etc/passwd"), ("BX1", "manifest.json"),
            ("BX1", good[:-4]), ("BX1", good + ".part"), ("", good), ("BX1", ""),
            ("evil", good), ("BX1", "BX1-20240101-000000.zip"),
            ("BX1", "BX1-20240101-000000.zip\x00.zip"), ("BX1\\..", good),
        ]:
            self.assertIsNone(m.resolve_download(printer, fn), (printer, fn))
        # a symlink inside the printer dir pointing outside the root must not be served
        link = os.path.join(self.root, "BX1", "BX1-20240101-000000.zip")
        os.symlink(outside, link)
        self.assertIsNone(m.resolve_download("BX1", "BX1-20240101-000000.zip"))


# ------------------------------------------------------------ open_file drivers

class FakeHttpDriver:
    """Minimal open_file() driver; traits and state are set per test."""
    kind = "fakehttp"

    def __init__(self, name="HX1", state="IDLE", **traits):
        self.name, self.model, self.state = name, "Fake", state
        self.__dict__.update(traits)
        self.data = {"one.gcode": b"G1 X1\n" * 400, "sub/two.gcode": b"G1 X2\n" * 300,
                     "bad.gcode": b"nope"}

    def snapshot(self):
        return {"printer": self.name, "state": self.state, "connected": self.state != "UNKNOWN",
                "job": "a job"}

    def files(self):
        return [{"name": n, "size": len(d), "modified": 1700000000.5, "origin": "local"}
                for n, d in self.data.items()]

    def open_file(self, filename, timeout=30, headers=None):
        if filename == "bad.gcode":
            raise PrinterError("The printer said no (HTTP 500).")
        return io.BytesIO(self.data[filename])      # BytesIO is a context manager with read(n)


class OpenFileBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "backups")
        self.addCleanup(self.tmp.cleanup)

    def mgr(self, *printers):
        return backup.BackupManager({p.name.lower(): p for p in printers}, root=self.root,
                                    pause_between_files=0)

    def check_zip(self, m, name, expected):
        rows = m.status()["backups"][name]
        with zipfile.ZipFile(m.resolve_download(name, rows[0]["file"])) as z:
            self.assertIsNone(z.testzip())
            man = json.loads(z.read("manifest.json"))
            self.assertEqual(set(z.namelist()) - {"manifest.json"}, set(expected))
            for f in man["files"]:
                data = z.read(f["name"])
                self.assertEqual(data, expected[f["name"]])
                self.assertEqual(hashlib.sha256(data).hexdigest(), f["sha256"])
                self.assertEqual(f["size"], len(data))
        return man

    def test_prusalink_files_stream_into_zip(self):
        fake = tpl.FakePrusaLink()
        self.addCleanup(fake.close)
        prn = PrusaLinkPrinter("PL1", fake.url, tpl.CREDS, "Test")
        m = self.mgr(prn)
        m.start("pl1")
        job = wait_done(m)["recent"][0]
        self.assertEqual(job["status"], "done", job)
        man = self.check_zip(m, "PL1", {"benchy.gcode": tpl.BIG,
                                        "sub dir/bracket v2.bgcode": b"BGCODE" * 100})
        self.assertEqual(man["kind"], "prusalink")
        self.assertTrue(man["complete"])
        self.assertEqual(os.listdir(os.path.join(self.root, "PL1"))[0][:4], "PL1-")
        self.assertEqual([n for n in os.listdir(os.path.join(self.root, "PL1"))
                          if not n.endswith(".zip")], [])      # no .dl-/.part leftovers

    def test_moonraker_files_stream_into_zip_even_mid_print(self):
        fake = tmr.FakeMoonraker()
        self.addCleanup(fake.close)
        fake.ps["state"] = "printing"
        fake.ps["filename"] = "benchy.gcode"
        fake.progress = 0.3
        prn = MoonrakerPrinter("MR1", fake.url, None, "Test")
        self.assertEqual(prn.snapshot()["state"], "RUNNING")
        m = self.mgr(prn)
        m.start("mr1")                                          # backup_mid_print_ok -> allowed
        job = wait_done(m)["recent"][0]
        self.assertEqual(job["status"], "done", job)
        man = self.check_zip(m, "MR1", {"benchy.gcode": tmr.BIG,
                                        "voron/bracket v2.gcode": b"G1 X2\n" * 300})
        self.assertEqual(man["kind"], "moonraker")
        # the backup must not have triggered per-file metadata lookups
        self.assertEqual([p for _, p in fake.calls() if p == "/server/files/metadata"], [])

    def test_prusalink_refused_mid_print_and_when_unknown(self):
        fake = tpl.FakePrusaLink()
        self.addCleanup(fake.close)
        fake.state = "PRINTING"
        fake.job = {"id": 7, "name": "benchy", "progress": 40, "time_remaining": 600}
        prn = PrusaLinkPrinter("PL1", fake.url, tpl.CREDS, "Test")
        self.assertEqual(prn.snapshot()["state"], "RUNNING")
        with self.assertRaises(backup.BackupBusy) as cx:
            self.mgr(prn).start("PL1")
        self.assertEqual(cx.exception.status, 423)
        self.assertIn("RUNNING", str(cx.exception))
        self.assertNotIn("FTPS", str(cx.exception))             # not an FTPS printer
        gone = PrusaLinkPrinter("PL2", "http://127.0.0.1:9", tpl.CREDS, "Test")
        with self.assertRaises(backup.BackupBusy) as cx:
            self.mgr(gone).start("PL2")
        self.assertIn("cannot be confirmed idle", str(cx.exception))
        self.assertNotIn("MQTT", str(cx.exception))

    def test_mid_print_follows_the_trait(self):
        for traits, allowed in (({"backup_mid_print_ok": True}, True),
                                ({"backup_mid_print_ok": False}, False),
                                ({}, False)):                    # unknown driver: refuse while busy
            for state in ("RUNNING", "PREPARE", "PAUSE", "UNKNOWN"):
                p = FakeHttpDriver(state=state, **traits)
                m = self.mgr(p)
                if allowed:
                    m.start("HX1")
                    self.assertEqual(wait_done(m)["recent"][0]["status"], "partial", (traits, state))
                    shutil.rmtree(self.root, True)
                else:
                    with self.assertRaises(backup.BackupBusy, msg=(traits, state)):
                        m.start("HX1")
            idle = self.mgr(FakeHttpDriver(state="IDLE", **traits))
            idle.start("HX1")                                    # idle is always fine
            self.assertEqual(wait_done(idle)["recent"][0]["status"], "partial")
            shutil.rmtree(self.root, True)

    def test_print_starting_mid_backup_aborts_when_trait_false(self):
        p = FakeHttpDriver(backup_mid_print_ok=False)
        real = p.open_file
        n = []

        def flip(name, **kw):
            n.append(name)
            if len(n) == 1:
                p.state = "RUNNING"
            return real(name, **kw)
        p.open_file = flip
        m = self.mgr(p)
        m.start("HX1")
        job = wait_done(m)["recent"][0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("RUNNING", job["error"])
        self.assertEqual(len(n), 1)

    def test_open_file_failure_is_reported_per_file(self):
        p = FakeHttpDriver(backup_mid_print_ok=True)
        m = self.mgr(p)
        m.start("HX1")
        job = wait_done(m)["recent"][0]
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["failed_files"], [{"name": "bad.gcode",
                                                "error": "The printer said no (HTTP 500)."}])
        self.check_zip(m, "HX1", {"one.gcode": p.data["one.gcode"],
                                  "sub/two.gcode": p.data["sub/two.gcode"]})


# ------------------------------------------------------------ Bambu /cache folder

def _dir(name):
    return f"drwxr-xr-x    1 root     root            0 Jan 01 12:00 {name}"


def _file(name, size):
    return f"-rw-r--r--    1 root     root         {size:5d} Jan 01 12:00 {name}"


class FakeFTP:
    """Stands in for ImplicitFTPTLS: a tiny SD card with folders. Records every cwd/RETR."""

    def __init__(self, tree, log, cwd_error=None):
        self.tree, self.log, self.cwd_error = tree, log, cwd_error
        self.where = ""

    def cwd(self, d):
        self.log.append(("CWD", d))
        if self.cwd_error:
            raise self.cwd_error
        if d not in self.tree or d == "":
            raise ftplib.error_perm("550 Failed to change directory.")
        self.where = d

    def retrlines(self, cmd, cb):
        assert cmd == "LIST"
        self.log.append(("LIST", self.where))
        for line in self.tree[self.where]["list"]:
            cb(line)

    def retrbinary(self, cmd, cb):
        assert cmd.startswith("RETR ")
        path = cmd[5:]
        self.log.append(("RETR", path))
        folder, _, name = path.rpartition("/")
        cb(self.tree[folder]["data"][name])

    def quit(self):
        pass

    def close(self):
        pass


def make_card(**extra):
    """Root with .3mf files plus every kind of folder a real card has, each holding .3mf
    files that must NOT be backed up (except `cache`)."""
    tree = {"": {"list": [], "data": {}}}
    def add(folder, name, data):
        tree[folder]["data"][name] = data
        tree[folder]["list"].append(_file(name, len(data)))
    tree[""]["list"] += [_dir(d) for d in ("cache", "ipcam", "logger", "recorder",
                                              "timelapse", "image", ".system", "System Volume Information")]
    for d in ("cache", "ipcam", "logger", "recorder", "timelapse", "image", ".system"):
        tree[d] = {"list": [], "data": {}}
    add("", "root-job.gcode.3mf", b"R" * 500)
    add("", "notes.txt", b"not a job")
    add("cache", "sent-from-slicer.gcode.3mf", b"C" * 700)
    add("cache", "second job.3mf", b"D" * 300)
    add("cache", "readme.txt", b"skip non-3mf")
    add("cache", ".hidden.3mf", b"hidden file")
    tree["cache"]["list"].append(_dir("nested.3mf"))              # a folder that looks like a file
    for d in ("ipcam", "logger", "recorder", "timelapse", "image", ".system"):
        add(d, "should-not-be-backed-up.3mf", b"X" * 50)
    tree.update(extra)
    return tree


class BambuCacheBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "backups")
        self.addCleanup(self.tmp.cleanup)
        self.log = []

    def printer(self, tree=None, cwd_error=None):
        prn = BambuPrinter("BC1", "127.0.0.1", "SERIAL", "code", "Test Model")
        tree = tree or make_card()
        prn._ftps = lambda: FakeFTP(tree, self.log, cwd_error)     # the FTPS boundary
        prn.snapshot = lambda: {"printer": "BC1", "state": "IDLE", "connected": True, "job": ""}
        return prn

    def mgr(self, prn):
        return backup.BackupManager({prn.name.lower(): prn}, root=self.root, pause_between_files=0)

    def test_sd_list_default_is_unchanged(self):
        rows = self.printer().sd_list()
        self.assertEqual([r["name"] for r in rows], ["root-job.gcode.3mf"])
        self.assertEqual(rows[0]["size"], 500)
        self.assertEqual(self.log, [("LIST", "")])                 # root only, no folder entered

    def test_sd_list_dir_lists_one_folder(self):
        rows = self.printer().sd_list_dir("cache")
        self.assertEqual([(r["name"], r["size"]) for r in rows],
                         [("cache/sent-from-slicer.gcode.3mf", 700), ("cache/second job.3mf", 300)])
        self.assertEqual(self.log, [("CWD", "cache"), ("LIST", "cache")])
        self.assertEqual(self.printer().sd_list_dir("nope"), [])    # missing folder -> []

    def test_sd_list_dir_rejects_anything_but_a_plain_folder(self):
        p = self.printer()
        for bad in ("", "..", "../x", "a/b", "/cache/x", ".system", "a\\b", "cache/.."):
            with self.assertRaises(ValueError, msg=bad):
                p.sd_list_dir(bad)
        self.assertEqual(self.log, [])                              # never even connected

    def test_backup_includes_cache_and_skips_other_folders(self):
        prn = self.printer()
        m = self.mgr(prn)
        m.start("BC1")
        st = wait_done(m)
        job = st["recent"][0]
        self.assertEqual(job["status"], "done", job)
        with zipfile.ZipFile(m.resolve_download("BC1", st["backups"]["BC1"][0]["file"])) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(set(z.namelist()),
                             {"root-job.gcode.3mf", "cache/sent-from-slicer.gcode.3mf",
                              "cache/second job.3mf", "manifest.json"})
            self.assertEqual(z.read("cache/sent-from-slicer.gcode.3mf"), b"C" * 700)
            man = json.loads(z.read("manifest.json"))
        self.assertEqual({f["name"] for f in man["files"]},
                         {"root-job.gcode.3mf", "cache/sent-from-slicer.gcode.3mf", "cache/second job.3mf"})
        self.assertTrue(man["complete"])
        self.assertEqual(man["totals"]["files"], 3)
        entered = [a for op, a in self.log if op in ("CWD",)]
        self.assertEqual(set(entered), {"cache"})                   # no other folder was opened
        retr = [a for op, a in self.log if op == "RETR"]
        self.assertEqual(sorted(retr), sorted(["root-job.gcode.3mf", "cache/sent-from-slicer.gcode.3mf",
                                               "cache/second job.3mf"]))
        for forbidden in ("ipcam", "logger", "recorder", "timelapse", "image", ".system"):
            self.assertFalse([a for a in retr if forbidden in a], forbidden)

    def test_card_without_cache_folder_still_backs_up_root(self):
        tree = make_card()
        del tree["cache"]
        m = self.mgr(self.printer(tree))
        m.start("BC1")
        st = wait_done(m)
        self.assertEqual(st["recent"][0]["status"], "done")
        self.assertEqual(st["backups"]["BC1"][0]["file_count"], 1)

    def test_cache_listing_error_gives_partial_backup_not_silence(self):
        m = self.mgr(self.printer(cwd_error=ftplib.error_perm("530 Not logged in")))
        m.start("BC1")
        st = wait_done(m)
        job = st["recent"][0]
        self.assertEqual(job["status"], "partial")
        self.assertEqual([f["name"] for f in job["failed_files"]], ["cache/"])
        self.assertEqual(st["backups"]["BC1"][0]["file_count"], 1)   # the root file is safe
        self.assertEqual(st["backups"]["BC1"][0]["failed_count"], 1)

    def test_driver_without_sd_list_dir_still_works(self):
        p = FakeBambu()                      # the old mock surface: sd_list/sd_download only
        m = backup.BackupManager({"bx1": p}, root=self.root, pause_between_files=0)
        m.start("BX1")
        self.assertEqual(wait_done(m)["recent"][0]["status"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
