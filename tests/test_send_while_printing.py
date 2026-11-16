"""Sending (uploading) a file to a printer that is printing or paused (2026-11-16).

    python3 tests/test_send_while_printing.py          (needs: paho-mqtt)

Everything is faked: no printer, no FTPS server, no network. What is pinned here:
  * a Bambu upload is done by hand and NEVER calls unwrap() on the data connection (a printing Bambu never answers the TLS
    goodbye), and it reads the printer's final reply with a long timeout;
  * uploads (and the name check before one) may run on a printing / paused Bambu; delete, download and every other FTPS
    call still need a positively idle printer;
  * PrusaLink / Klipper / OctoPrint uploads need only "connected, no print start pending"; a 409 from the printer is a clean refusal;
  * the switch `library.send_while_printing` off = the old idle-only rule everywhere.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

_TMP = tempfile.mkdtemp(prefix="sendwp-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "config.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")

import bambu  # noqa: E402
import moonraker  # noqa: E402
import prusalink  # noqa: E402
from bambu import BambuPrinter, PrinterBusy, PrinterError  # noqa: E402


class FakeConn:
    def __init__(self):
        self.sent, self.closed, self.timeout, self.unwrapped = [], False, None, False

    def settimeout(self, t): self.timeout = t
    def sendall(self, b): self.sent.append(bytes(b))
    def close(self): self.closed = True
    def unwrap(self): self.unwrapped = True; raise AssertionError("unwrap() must never be called on the data connection")


class FakeSock:
    def __init__(self): self.timeouts = []
    def settimeout(self, t): self.timeouts.append(t)


class FakeFTP:
    made = []

    def __init__(self):
        self.cmds, self.conn, self.sock, self.quit_called = [], FakeConn(), FakeSock(), False
        self.reply_read = False
        self.names, self.sizes = [], {}
        FakeFTP.made.append(self)

    def connect(self, *a, **k): pass
    def login(self, *a, **k): pass
    def prot_p(self): pass
    def set_pasv(self, *a): pass
    def close(self): pass

    def quit(self): self.quit_called = True

    def voidcmd(self, c): self.cmds.append(c); return "200 ok"

    def transfercmd(self, c):
        self.cmds.append(c)
        return self.conn

    def storbinary(self, *a, **k): raise AssertionError("storbinary calls unwrap(): the Hub must not use it")

    def voidresp(self):
        assert self.conn.closed, "the data connection must be closed before the final reply is read"
        self.reply_read = True
        return "226 done"

    def nlst(self): return list(self.names)
    def size(self, n): return self.sizes[n]

    def retrlines(self, cmd, cb):
        for n in self.names:
            cb(f"-rw-r--r--   1 root  root   {self.sizes.get(n, 1)} Jan 01 12:00 {n}")


class BambuUpload(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sendwp-case-")
        os.environ["BAMBU_MCP_CACHE"] = os.path.join(self.dir, "metacache.json")
        FakeFTP.made = []
        p = mock.patch.object(bambu, "ImplicitFTPTLS", FakeFTP)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(bambu.set_send_while_busy, True)
        bambu.set_send_while_busy(True)
        self.snap = {"connected": True, "state": "RUNNING", "start_pending": False}

    def make(self):
        prn = BambuPrinter("Shop A1", "127.0.0.1", "01P00A000000001", "12345678", "Bambu Lab A1")
        prn.snapshot = lambda: dict(self.snap)
        return prn

    def test_upload_never_unwraps_and_reads_the_final_reply(self):
        prn = self.make()
        data = os.urandom(200 * 1024 + 5)
        prn.sd_upload("part.gcode.3mf", data)
        f = FakeFTP.made[0]
        self.assertEqual(f.cmds, ["TYPE I", "STOR part.gcode.3mf"])
        self.assertFalse(f.conn.unwrapped)
        self.assertTrue(f.conn.closed)
        self.assertTrue(f.reply_read)                                   # voidresp ran, after the close
        self.assertEqual(b"".join(f.conn.sent), data)
        self.assertTrue(all(len(b) <= 64 * 1024 for b in f.conn.sent))
        self.assertGreaterEqual(f.conn.timeout, 120)
        self.assertGreaterEqual(max(f.sock.timeouts), 120)               # the wait for the final 226
        self.assertTrue(f.quit_called)

    def test_upload_is_allowed_while_printing_and_paused(self):
        for state in ("RUNNING", "PAUSE", "PREPARE", "IDLE"):
            FakeFTP.made = []
            self.snap["state"] = state
            self.make().sd_upload("a.3mf", b"x")
            self.assertEqual(len(FakeFTP.made), 1, state)

    def test_upload_refused_when_offline_or_a_start_was_just_sent(self):
        prn = self.make()
        for snap in ({"connected": False, "state": "UNKNOWN"}, {"connected": True, "state": "UNKNOWN"},
                     {"connected": True, "state": "RUNNING", "start_pending": True},
                     {"connected": True, "state": "IDLE", "start_pending": True}):
            self.snap = snap
            with self.assertRaises(PrinterBusy):
                prn.sd_upload("a.3mf", b"x")
        self.assertEqual(FakeFTP.made, [])                               # no FTPS session was opened

    def test_with_the_switch_off_upload_is_idle_only_again(self):
        prn = self.make()
        bambu.set_send_while_busy(False)
        with self.assertRaises(PrinterBusy):
            prn.sd_upload("a.3mf", b"x")
        self.assertEqual(FakeFTP.made, [])
        self.snap["state"] = "IDLE"
        prn.sd_upload("a.3mf", b"x")
        self.assertEqual(len(FakeFTP.made), 1)

    def test_everything_else_stays_idle_only_while_printing(self):
        prn = self.make()
        for call in (lambda: prn.sd_delete("a.3mf"), lambda: prn.sd_download("a.3mf"), lambda: prn.sd_list_dir("cache")):
            with self.assertRaises(PrinterBusy):
                call()
        self.assertEqual(FakeFTP.made, [])
        self.snap["state"] = "PAUSE"
        with self.assertRaises(PrinterBusy):
            prn.sd_delete("a.3mf")
        self.assertEqual(FakeFTP.made, [])

    def test_name_check_while_printing_is_live_and_light(self):
        prn = self.make()
        orig = FakeFTP.__init__

        def init(s):
            orig(s)
            s.names, s.sizes = ["one.3mf", "notes.txt", "two.3mf"], {"one.3mf": 11, "two.3mf": 22}
        with mock.patch.object(FakeFTP, "__init__", init):
            rows = prn.sd_list_for_send()
        self.assertEqual([(r["name"], r["size"]) for r in rows], [("one.3mf", 11), ("two.3mf", 22)])
        self.assertTrue(FakeFTP.made[0].quit_called)
        # offline: refused, never the saved copy
        self.snap = {"connected": False, "state": "UNKNOWN"}
        with self.assertRaises(PrinterBusy):
            prn.sd_list_for_send()

    def test_name_check_when_idle_uses_the_normal_listing(self):
        prn = self.make()
        self.snap["state"] = "IDLE"
        orig = FakeFTP.__init__

        def init(s):
            orig(s)
            s.names, s.sizes = ["one.3mf"], {"one.3mf": 11}
        with mock.patch.object(FakeFTP, "__init__", init):
            rows = prn.sd_list_for_send()
        self.assertEqual([(r["name"], r["size"]) for r in rows], [("one.3mf", 11)])


class _FakeResp:
    def __init__(self, data=b"{}", status=201):
        self._b, self.status, self.headers = io.BytesIO(data), status, {}

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=-1): return self._b.read(n)


class _Opener:
    def __init__(self, error=None):
        self.reqs, self.error = [], error

    def open(self, req, timeout=None):
        self.reqs.append(req.full_url)
        if self.error:
            raise self.error
        return _FakeResp()


PRINTING = lambda: {"connected": True, "state": "RUNNING"}                       # noqa: E731
PAUSED = lambda: {"connected": True, "state": "PAUSE"}                           # noqa: E731
OFFLINE = lambda: {"connected": False, "state": "UNKNOWN"}                       # noqa: E731
PENDING = lambda: {"connected": True, "state": "IDLE", "start_pending": True}    # noqa: E731
IDLE = lambda: {"connected": True, "state": "IDLE"}                              # noqa: E731


def _409(url):
    return urllib.error.HTTPError(url, 409, "Conflict", {}, io.BytesIO(b"{}"))


class HttpUploads(unittest.TestCase):
    DATA = b"G28\n" * 20

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(bambu.set_send_while_busy, True)
        bambu.set_send_while_busy(True)

    def drivers(self, op):
        mock.patch.object(bambu, "_OPENER", op).start()
        mock.patch.object(prusalink, "_OPENER", op).start()
        mock.patch.object(moonraker, "_OPENER", op).start()
        return [("octoprint", bambu.OctoPrintPrinter("Octo", "http://octo.invalid", "K")),
                ("prusalink", prusalink.PrusaLinkPrinter("Link", "http://link.invalid", {"api_key": "K"})),
                ("moonraker", moonraker.MoonrakerPrinter("Klip", "http://klip.invalid"))]

    def test_upload_goes_through_while_printing_or_paused(self):
        for snap in (PRINTING, PAUSED, IDLE):
            op = _Opener()
            for kind, p in self.drivers(op):
                p.fresh_snapshot = snap
                p.upload("a.gcode", self.DATA)
            self.assertEqual(len(op.reqs), 3, snap)

    def test_offline_and_start_pending_are_refused_before_any_request(self):
        for snap in (OFFLINE, PENDING):
            op = _Opener()
            for kind, p in self.drivers(op):
                p.fresh_snapshot = snap
                with self.assertRaises(PrinterBusy, msg=kind):
                    p.upload("a.gcode", self.DATA)
            self.assertEqual(op.reqs, [])

    def test_a_409_from_the_printer_is_a_clean_refusal(self):
        for kind, p in self.drivers(_Opener()):
            op = _Opener(error=_409("http://x.invalid/"))
            mock.patch.object({"octoprint": bambu, "prusalink": prusalink, "moonraker": moonraker}[kind], "_OPENER", op).start()
            p.fresh_snapshot = PRINTING
            with self.assertRaises(PrinterBusy, msg=kind) as cm:
                p.upload("a.gcode", self.DATA)
            self.assertIn("busy printing", str(cm.exception), kind)
            self.assertEqual(len(op.reqs), 1, kind)

    def test_other_errors_stay_ordinary_errors(self):
        for kind, p in self.drivers(_Opener()):
            err = urllib.error.HTTPError("http://x.invalid/", 500, "Boom", {}, io.BytesIO(b"{}"))
            mock.patch.object({"octoprint": bambu, "prusalink": prusalink, "moonraker": moonraker}[kind], "_OPENER", _Opener(error=err)).start()
            p.fresh_snapshot = PRINTING
            with self.assertRaises(PrinterError, msg=kind) as cm:
                p.upload("a.gcode", self.DATA)
            self.assertNotIsInstance(cm.exception, PrinterBusy, kind)

    def test_with_the_switch_off_a_printing_printer_is_refused_again(self):
        bambu.set_send_while_busy(False)
        op = _Opener()
        for kind, p in self.drivers(op):
            p.fresh_snapshot = PRINTING
            with self.assertRaises(PrinterBusy, msg=kind):
                p.upload("a.gcode", self.DATA)
            p.fresh_snapshot = PAUSED
            with self.assertRaises(PrinterBusy, msg=kind):
                p.upload("a.gcode", self.DATA)
        self.assertEqual(op.reqs, [])

    def test_no_upload_path_still_has_the_idle_only_guard(self):
        import inspect
        for cls in (bambu.OctoPrintPrinter, prusalink.PrusaLinkPrinter, moonraker.MoonrakerPrinter):
            self.assertNotIn("_require_idle_for_delete", inspect.getsource(cls.upload), cls.__name__)
            self.assertIn("_require_can_receive", inspect.getsource(cls.upload), cls.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=1)
