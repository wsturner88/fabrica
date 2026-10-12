"""Regression tests for the 2026-10-12 second review: physical-safety and driver findings (round 2).

    python3 tests/test_fix_safety2.py          (needs: paho-mqtt; the broker tests also need the openssl CLI)

Covers (review ids in brackets):
  * a start that was accepted but has not shown up yet is "busy": the auto-off and every control wait   [N-8]
  * editing / removing a Bambu during a pre-heat cools the OLD machine first, or keeps it supervised    [N-9]
  * Bambu hard faults (print_error / HMS) are modelled and block start / pre-heat / filament            [N-10]
  * auto-off failures from before the hook exists are replayed when it is installed                     [N-11]
  * FTPS list / download / upload / delete re-check "positively idle" at the moment of the operation,
    backup too; a busy printer gets the cached listing or a "printer is busy" error                      [N-12, N-14]
  * Bambu command acks are read: "sent but not confirmed" is reported, the auto-off retries              [N-13]
  * Test connection never opens a second MQTT session to a live Bambu                                   [N-16, P1-9]
  * user:pass@ never survives in an address, error text or snapshot                                      [N-7]
  * multi-AMS tray ids are unique (unit*4 + slot)                                                        [P2]

Loopback only - nothing here touches a real printer or any other host.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, HERE)

import backup  # noqa: E402
import bambu  # noqa: E402
import controls  # noqa: E402
import fleet_store  # noqa: E402
from bambu import BambuPrinter, OctoPrintPrinter, PrinterBusy, PrinterError  # noqa: E402
from moonraker import MoonrakerPrinter  # noqa: E402
from prusalink import PrusaLinkPrinter  # noqa: E402
from test_fix_safety import (SERIAL, TOPIC, AutoOffBase, BambuCase, FakePrn, octo_family,  # noqa: E402
                             wait_for)

S2 = "01P00A000000002"
TOPIC2 = f"device/{S2}/report"


# ------------------------------------------------------------------ helpers

def _read(path):
    with open(path) as fh:
        return fh.read()


def make_bambu(state="IDLE", echo=True, **print_fields):
    """A BambuPrinter over a mock MQTT client. echo=True: a gcode_line command updates the reported heater
    targets (a printer that obeys); False: the printer ignores every command."""
    prn = BambuPrinter("Shop", "127.0.0.1", SERIAL, "12345678", "Bambu Lab P1S")
    prn.CONFIRM_WAIT_S = 0.3
    p = {"gcode_state": state, "nozzle_temper": 31.5, "nozzle_target_temper": 0, "bed_temper": 30,
         "bed_target_temper": 0, "lights_report": [{"node": "chamber_light", "mode": "off"}]}
    p.update(print_fields)
    prn.state = {"print": p}
    prn.last_msg = time.time()
    prn._connected = True
    prn._client = mock.Mock()

    def publish(topic, payload):
        try:
            doc = json.loads(payload)
        except ValueError:
            return mock.Mock(rc=0, is_published=mock.Mock(return_value=True))
        if echo:
            g = (doc.get("print") or {})
            if g.get("command") == "gcode_line":
                for line in g["param"].split("\n"):
                    if line.startswith("M104 S"):
                        prn.state["print"]["nozzle_target_temper"] = int(line[6:])
                    if line.startswith("M140 S"):
                        prn.state["print"]["bed_target_temper"] = int(line[6:])
                prn.last_msg = time.time()
            s = doc.get("system") or {}
            if s.get("command") == "ledctrl":
                prn.state["print"]["lights_report"] = [{"node": s["led_node"], "mode": s["led_mode"]}]
        return mock.Mock(rc=0, is_published=mock.Mock(return_value=True))

    prn._client.publish.side_effect = publish
    return prn


def sent(prn):
    out = []
    for c in prn._client.publish.call_args_list:
        try:
            out.append(json.loads(c[0][1]))
        except Exception:
            pass
    return out


def gcodes(prn):
    return [d["print"]["param"] for d in sent(prn) if (d.get("print") or {}).get("command") == "gcode_line"]


# ================================================================== N-8 start pending

class TestStartPending(AutoOffBase):
    def test_bambu_start_is_pending_until_the_print_is_seen(self):
        prn = make_bambu("IDLE")
        self.assertFalse(prn.snapshot()["start_pending"])
        prn.start_print("a.gcode.3mf", [0])
        self.assertTrue(prn.snapshot()["start_pending"])
        prn.state["print"]["gcode_state"] = "PREPARE"
        s = prn.snapshot()
        self.assertEqual((s["state"], s["start_pending"]), ("PREPARE", False))
        prn.state["print"]["gcode_state"] = "IDLE"
        self.assertFalse(prn.snapshot()["start_pending"], "cleared for good once the print was seen")

    def test_pending_window_expires_for_a_start_the_printer_ignored(self):
        prn = make_bambu("IDLE")
        prn.start_print("a.gcode.3mf", [0])
        with mock.patch.object(controls, "START_PENDING_S", 0.2):
            self.assertTrue(prn.snapshot()["start_pending"])
            time.sleep(0.3)
            self.assertFalse(prn.snapshot()["start_pending"])

    def test_a_new_failure_ends_it_but_a_failed_that_was_already_showing_does_not(self):
        prn = make_bambu("IDLE")
        prn.start_print("a.gcode.3mf", [0])
        prn.state["print"]["gcode_state"] = "FAILED"
        self.assertFalse(prn.snapshot()["start_pending"])
        old = make_bambu("FAILED")                     # the previous job's FAILED is still on screen
        old.start_print("a.gcode.3mf", [0])
        self.assertTrue(old.snapshot()["start_pending"])

    def test_cancelling_ends_the_pending_window(self):
        prn = make_bambu("IDLE")
        prn.start_print("a.gcode.3mf", [0])
        prn.stop()
        self.assertFalse(prn.snapshot()["start_pending"])

    def test_a_refused_start_leaves_no_pending_window(self):
        prn = make_bambu("IDLE")
        prn._connected = False
        with self.assertRaises(PrinterError):
            prn.start_print("a.gcode.3mf", [0])
        prn._connected = True
        self.assertFalse(prn.snapshot()["start_pending"])

    def test_auto_off_waits_while_a_start_is_settling_then_cools(self):
        prn = make_bambu("IDLE")
        controls._resolver = lambda k: prn if k == "shop" else None
        controls.schedule_auto_off(prn, seconds=0.2)
        prn.start_print("a.gcode.3mf", [0])
        time.sleep(1.0)                                # deadline passed ~0.8 s ago, several retries ran
        self.assertEqual(gcodes(prn), [], "the heaters were switched off into a print that is starting")
        self.assertIsNotNone(controls.auto_off_remaining("shop"), "the timer must still be armed")
        controls.clear_start_pending(prn)              # the printer ignored the start: nothing is starting
        self.assertTrue(wait_for(lambda: gcodes(prn) == ["M104 S0\nM140 S0\n"], timeout=3))
        self.assertTrue(wait_for(lambda: self.cooled, timeout=3))

    def test_auto_off_drops_the_timer_when_the_started_print_shows_up(self):
        prn = make_bambu("IDLE")
        controls._resolver = lambda k: prn if k == "shop" else None
        controls.schedule_auto_off(prn, seconds=0.2)
        prn.start_print("a.gcode.3mf", [0])
        prn.state["print"]["gcode_state"] = "PREPARE"
        self.assertTrue(wait_for(lambda: controls.auto_off_remaining("shop") is None, timeout=3))
        self.assertEqual(gcodes(prn), [])

    def test_controls_are_gated_while_a_start_is_pending(self):
        prn = make_bambu("IDLE")
        prn.start_print("a.gcode.3mf", [0])
        snap = prn.snapshot()
        for action in ("preheat", "cooldown", "unload", "load"):
            self.assertIn("start was just sent", controls._gate(snap, action) or "", action)
        self.assertIsNone(controls._gate(snap, "light_on"))
        with self.assertRaises(controls.ControlRefused):
            controls.perform(prn, "cooldown")
        self.assertEqual(gcodes(prn), [])

    def test_release_printer_does_not_cool_into_a_starting_print(self):
        prn = make_bambu("IDLE")
        controls.schedule_auto_off(prn, seconds=3600)
        prn.start_print("a.gcode.3mf", [0])
        note = controls.release_printer(prn)
        self.assertIn("kept", note)
        self.assertEqual(gcodes(prn), [])

    def test_http_drivers_mark_a_start_pending_too(self):
        idle = {"state": "IDLE", "raw": "IDLE", "job": "", "percent": None, "remaining_min": None,
                "nozzle_c": 25, "nozzle_target_c": 0, "bed_c": 25, "bed_target_c": 0, "print_error": 0,
                "layer": "", "message": None}
        octo = OctoPrintPrinter("o", "http://127.0.0.1:1", "K")
        octo._post = lambda *a, **k: 204
        octo._last = ({"state": {"flags": {"operational": True}}, "temperature": {}}, {})
        octo._ok, octo._last_ts = True, time.time()
        octo.start_print("a.gcode")
        self.assertTrue(octo.snapshot()["start_pending"])
        octo._last = ({"state": {"flags": {"printing": True}}, "temperature": {}}, {})
        self.assertFalse(octo.snapshot()["start_pending"])
        for cls, url in ((PrusaLinkPrinter, "http://127.0.0.1:1"), (MoonrakerPrinter, "http://127.0.0.1:1")):
            p = cls("x", url, {"password": "pw"} if cls is PrusaLinkPrinter else "")
            p._call = lambda *a, **k: (200, None) if cls is PrusaLinkPrinter else None
            p.refresh = lambda *a, **k: None
            p._ok, p._last, p._last_ts = True, dict(idle), time.time()
            p.start_print("a.gcode")
            self.assertTrue(p.snapshot()["start_pending"], cls.__name__)
            p._last = {**idle, "state": "RUNNING"}
            self.assertFalse(p.snapshot()["start_pending"], cls.__name__)


# ================================================================== N-10 Bambu hard faults

class TestBambuFaults(unittest.TestCase):
    def test_no_error_and_a_user_cancel_are_not_faults(self):
        self.assertIsNone(bambu.bambu_fault({"print_error": 0, "hms": []}, "IDLE"))
        self.assertIsNone(bambu.bambu_fault({"print_error": 0x0300400C}, "FAILED"))     # normal after a cancel
        self.assertIsNone(bambu.bambu_fault({}, "UNKNOWN"))

    def test_a_print_error_on_a_parked_printer_is_a_fault(self):
        for st in ("IDLE", "FINISH", "FAILED", "PAUSE"):
            f = bambu.bambu_fault({"print_error": 0x05008011}, st)
            self.assertEqual((f["source"], f["code"]), ("print_error", "0500_8011"), st)
            self.assertIn("0500_8011", f["message"])

    def test_a_running_print_owns_its_own_errors(self):
        self.assertIsNone(bambu.bambu_fault({"print_error": 0x05008011}, "RUNNING"))

    def test_hms_severity_decides(self):
        fatal = {"hms": [{"attr": 0x03000100, "code": 0x00010007}]}
        serious = {"hms": [{"attr": 0x07000100, "code": 0x00020001}]}
        common = {"hms": [{"attr": 0x07000100, "code": 0x00030001}]}
        info = {"hms": [{"attr": 0x07000100, "code": 0x00040001}]}
        self.assertEqual(bambu.bambu_fault(fatal, "IDLE")["severity"], "fatal")
        self.assertEqual(bambu.bambu_fault(fatal, "IDLE")["code"], "HMS_0300_0100_0001_0007")
        self.assertEqual(bambu.bambu_fault(serious, "IDLE")["severity"], "serious")
        self.assertIsNone(bambu.bambu_fault(common, "IDLE"))
        self.assertIsNone(bambu.bambu_fault(info, "IDLE"))

    def test_junk_never_raises(self):
        for junk in ({"hms": "x"}, {"hms": [None, 5, {}, {"attr": "zz", "code": []}]}, {"print_error": "abc"},
                     {"print_error": None}, {"print_error": True}):
            self.assertIsNone(bambu.bambu_fault(junk, "IDLE"), junk)

    def test_the_owner_can_tune_the_lists(self):
        with mock.patch.object(bambu, "FAULT_IGNORE_PRINT_ERRORS", frozenset({0x0300400C, 0x05008011})):
            self.assertIsNone(bambu.bambu_fault({"print_error": 0x05008011}, "IDLE"))
        with mock.patch.object(bambu, "FAULT_BLOCKING_HMS_SEVERITIES", ("fatal", "serious", "common")):
            self.assertIsNotNone(bambu.bambu_fault({"hms": [{"attr": 1, "code": 0x00030001}]}, "IDLE"))
        with mock.patch.object(bambu, "FAULT_IGNORE_HMS", frozenset({"0300_0100_0001_0007"})):
            self.assertIsNone(bambu.bambu_fault({"hms": [{"attr": 0x03000100, "code": 0x00010007}]}, "IDLE"))
        with mock.patch.object(bambu, "FAULT_BLOCK_ON_PRINT_ERROR", False):
            self.assertIsNone(bambu.bambu_fault({"print_error": 0x05008011}, "IDLE"))

    def test_snapshot_carries_the_fault_and_the_gates_use_it(self):
        prn = make_bambu("IDLE", print_error=0x05008011)
        s = prn.snapshot()
        self.assertTrue(s["fault"])
        self.assertEqual(s["fault"]["code"], "0500_8011")
        for action in ("preheat", "unload", "load"):
            self.assertIn("fault", controls._gate(s, action))
        self.assertIsNone(controls._gate(s, "cooldown"))             # switching heaters OFF is always allowed
        self.assertIsNone(controls._gate(s, "light_on"))
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(prn, "preheat", preset="PLA")
        self.assertIn("0500_8011", str(cm.exception))
        self.assertEqual(gcodes(prn), [])
        # the start gate (server.py) reads s.get("fault"): a dict is truthy, a healthy printer gives False
        self.assertFalse(make_bambu("IDLE").snapshot()["fault"])
        self.assertTrue(make_bambu("FAILED", print_error=0x0700A001).snapshot()["fault"])
        self.assertFalse(make_bambu("FAILED", print_error=0x0300400C).snapshot()["fault"])

    def test_hms_delivered_over_mqtt_is_replaced_not_accumulated(self):
        prn = make_bambu("IDLE")
        prn._on_message(None, None, mock.Mock(payload=json.dumps(
            {"print": {"hms": [{"attr": 0x03000100, "code": 0x00010007}]}}).encode()))
        self.assertTrue(prn.snapshot()["fault"])
        prn._on_message(None, None, mock.Mock(payload=json.dumps({"print": {"hms": []}}).encode()))
        self.assertFalse(prn.snapshot()["fault"], "a cleared HMS list must clear the fault")


# ================================================================== N-11 boot-time announcements

class TestBootRestoreFailuresAreAnnounced(AutoOffBase):
    def test_failures_before_the_hook_exists_are_replayed_when_it_is_installed(self):
        ghost = FakePrn("Ghost")
        controls.schedule_auto_off(ghost, seconds=3600)
        with controls._tlock:
            controls._timers.clear()                   # "the Hub restarted": only the file remembers
        path = controls._state_path()
        self.assertIn("ghost", _read(path))
        controls._on_auto_off_failed = None            # the server has not installed its hook yet
        controls._restored[0] = False
        controls.configure(resolver=lambda k: None, data_dir=self.dir)          # boot-time restore
        self.assertEqual(self.failed, [], "nothing can be announced yet")
        self.assertEqual(len(controls._unannounced), 1)
        got = []
        controls.set_runtime(on_auto_off_failed=lambda n, why: got.append((n, why)))       # the hook arrives
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "Ghost")
        self.assertIn("heaters may still be on", got[0][1])
        self.assertEqual(controls._unannounced, [])
        controls.set_runtime(on_auto_off_failed=lambda n, why: got.append((n, why)))       # never twice
        self.assertEqual(len(got), 1)

    def test_a_dead_entry_is_dropped_from_the_file_so_it_is_not_re_announced_every_boot(self):
        ghost = FakePrn("Ghost")
        controls.schedule_auto_off(ghost, seconds=3600)
        with controls._tlock:
            controls._timers.clear()
        controls._restored[0] = False
        controls.configure(resolver=lambda k: None, data_dir=self.dir)
        self.assertNotIn("ghost", _read(controls._state_path()))
        self.assertEqual(len(self.failed), 1)          # announced live (the hook was already installed)
        controls._restored[0] = False
        controls.configure(resolver=lambda k: None, data_dir=self.dir)
        self.assertEqual(len(self.failed), 1, "a second boot must not announce it again")


# ================================================================== N-12 / N-14 FTPS and delete guards

class FakeFTP:
    made = []

    def __init__(self):
        FakeFTP.made.append(self)
        self.ops = []

    def connect(self, *a, **k): pass
    def login(self, *a, **k): pass
    def prot_p(self): pass
    def set_pasv(self, *a): pass
    def quit(self): pass
    def close(self): pass

    def retrlines(self, cmd, cb):
        self.ops.append(cmd)
        cb("-rw-r--r--   1 root  root   1000 Jan 01 12:00 part.gcode.3mf")

    def retrbinary(self, cmd, cb):
        self.ops.append(cmd)
        cb(b"DATA")

    def storbinary(self, cmd, fh): self.ops.append(cmd)
    def delete(self, name): self.ops.append("DELE " + name)
    def cwd(self, d): self.ops.append("CWD " + d)


class TestFtpsGuards(unittest.TestCase):
    def setUp(self):
        FakeFTP.made = []
        p = mock.patch.object(bambu, "ImplicitFTPTLS", FakeFTP)
        p.start()
        self.addCleanup(p.stop)
        d = tempfile.mkdtemp(prefix="sdlist-")              # each test starts with no saved file listing
        q = mock.patch.object(bambu.BambuPrinter, "SD_LISTING_DIR", d)
        q.start()
        self.addCleanup(q.stop)
        self.addCleanup(shutil.rmtree, d, True)

    def ops(self, prn):
        return {"list": prn.sd_list, "list_dir": lambda: prn.sd_list_dir("cache"),
                "download": lambda: prn.sd_download("part.gcode.3mf"),
                "upload": lambda: prn.sd_upload("x.3mf", b"1"), "delete": lambda: prn.sd_delete("part.gcode.3mf")}

    def test_no_ftps_session_unless_positively_idle(self):
        for state in ("RUNNING", "PREPARE", "PAUSE", "UNKNOWN"):
            prn = make_bambu(state)
            for label, fn in self.ops(prn).items():
                with self.assertRaises(PrinterBusy, msg=f"{state}.{label}") as cm:
                    fn()
                ok = ("The file list will appear when",) if label == "list" else ("printer is busy",)
                self.assertTrue(str(cm.exception).startswith(ok), str(cm.exception))
        prn = make_bambu("IDLE")
        prn._connected = False
        for label, fn in self.ops(prn).items():
            with self.assertRaises(PrinterBusy, msg=f"disconnected.{label}"):
                fn()
        prn = make_bambu("IDLE", gcode_state="garbage")          # an unmapped state fails closed
        with self.assertRaises(PrinterBusy):
            prn.sd_delete("x")
        self.assertEqual(FakeFTP.made, [], "an FTPS session was opened against a printer that is not idle")

    def test_idle_finish_failed_work(self):
        for state in ("IDLE", "FINISH", "FAILED"):
            prn = make_bambu(state)
            self.assertEqual([r["name"] for r in prn.sd_list()], ["part.gcode.3mf"])
            self.assertEqual(prn.sd_download("part.gcode.3mf"), b"DATA")
            prn.sd_delete("part.gcode.3mf")

    def test_a_start_that_is_settling_blocks_ftps(self):
        prn = make_bambu("IDLE")
        prn.start_print("a.gcode.3mf", [0])
        with self.assertRaises(PrinterBusy):
            prn.sd_delete("part.gcode.3mf")
        self.assertEqual(FakeFTP.made, [])

    def test_busy_printer_lists_from_the_cache_or_says_busy(self):
        prn = make_bambu("IDLE")
        with self.assertRaises(PrinterBusy):
            prn.state["print"]["gcode_state"] = "RUNNING"
            prn.sd_list()                                        # nothing cached yet
        prn.state["print"]["gcode_state"] = "IDLE"
        fresh = prn.sd_list()
        n = len(FakeFTP.made)
        prn.state["print"]["gcode_state"] = "RUNNING"
        self.assertEqual(prn.sd_list(), fresh)                   # the idle-time listing, no FTPS session
        self.assertEqual(len(FakeFTP.made), n)
        prn._sd_cache = (time.time() - 30 * 86400, prn._sd_cache[1])
        self.assertEqual(prn.sd_list(), fresh)                   # however old: the card can't change mid-print
        self.assertEqual(len(FakeFTP.made), n)

    def test_delete_that_waited_behind_a_prewarm_is_refused_when_a_print_started_meanwhile(self):
        """The review's B1: the idle check passed, then the delete waited for the FTPS lock."""
        prn = make_bambu("IDLE")
        lock = threading.Lock()
        out = {}

        def prewarm():
            with lock:
                time.sleep(0.4)

        def delete():
            assert prn.snapshot()["state"] == "IDLE"             # the server's check: passes
            with lock:                                           # ...then waits here
                try:
                    prn.sd_delete("part.gcode.3mf")
                    out["r"] = "deleted"
                except PrinterBusy as e:
                    out["r"] = str(e)

        t1 = threading.Thread(target=prewarm)
        t1.start()
        time.sleep(0.05)
        t2 = threading.Thread(target=delete)
        t2.start()
        time.sleep(0.1)
        prn.state["print"]["gcode_state"] = "RUNNING"            # a print starts while the delete waits
        t1.join()
        t2.join()
        self.assertTrue(out["r"].startswith("printer is busy"), out)
        self.assertEqual(FakeFTP.made, [])

    def test_prusalink_delete_and_download_recheck_the_printer(self):
        p = PrusaLinkPrinter("mk4", "http://127.0.0.1:1", {"password": "pw"})
        p._call = mock.Mock(return_value=(204, None))
        p._open = mock.Mock(side_effect=AssertionError("downloaded from a printing printer"))
        base = {"connected": True, "start_pending": False}
        p.fresh_snapshot = lambda: {**base, "state": "RUNNING"}
        p.snapshot = lambda: {**base, "state": "RUNNING"}
        with self.assertRaises(PrinterBusy):
            p.sd_delete("x.gcode")
        with self.assertRaises(PrinterBusy):
            p.open_file("x.gcode")
        p._call.assert_not_called()
        p.fresh_snapshot = lambda: {**base, "state": "UNKNOWN", "connected": False, "error": "gone"}
        with self.assertRaises(PrinterBusy):                     # delete needs POSITIVE idle
            p.sd_delete("x.gcode")
        p.fresh_snapshot = lambda: {**base, "state": "IDLE"}
        p.sd_delete("x.gcode")
        p._call.assert_called_once()

    def test_moonraker_delete_rechecks_the_printer(self):
        m = MoonrakerPrinter("v", "http://127.0.0.1:1", "")
        m._call = mock.Mock(return_value=None)
        m.fresh_snapshot = lambda: {"connected": True, "state": "PAUSE"}
        with self.assertRaises(PrinterBusy):
            m.sd_delete("x.gcode")
        m._call.assert_not_called()


class FlippingLock:
    """A lock that makes the printer busy the moment it is acquired (a print starts while we waited)."""

    def __init__(self, on_acquire):
        self._l, self._on = threading.Lock(), on_acquire

    def __enter__(self):
        self._l.acquire()
        self._on()
        return self

    def __exit__(self, *a):
        self._l.release()


class TestBackupRechecksUnderTheLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_backup(self, prn, ftps_lock):
        m = backup.BackupManager({prn.name.lower(): prn}, root=os.path.join(self.tmp, "b"),
                                 ftps_lock=ftps_lock, pause_between_files=0)
        m.start(prn.name)
        end = time.time() + 15
        while time.time() < end and m.status()["running"]:
            time.sleep(0.05)
        return m.status()["recent"][0]

    def fake(self):
        class FB:
            kind = "bambu"
            name, model, state = "BX1", "x", "IDLE"
            downloaded = []

            def snapshot(s):
                return {"state": s.state, "connected": True, "job": "j"}

            def sd_list(s):
                return [{"name": "a.gcode.3mf", "size": 4, "modified": "Jan 01 12:00"}]

            def sd_download(s, n):
                s.downloaded.append(n)
                return b"DATA"
        return FB()

    def test_listing_is_refused_if_a_print_started_while_waiting_for_the_lock(self):
        fb = self.fake()
        job = self.run_backup(fb, lambda name: FlippingLock(lambda: setattr(fb, "state", "RUNNING")))
        self.assertEqual(job["status"], "failed")
        self.assertIn("RUNNING", job["error"])
        self.assertEqual(fb.downloaded, [])

    def test_download_is_refused_if_a_print_started_while_waiting_for_the_lock(self):
        fb = self.fake()
        calls = {"n": 0}

        def flip():
            calls["n"] += 1
            if calls["n"] >= 2:                         # the first hold is the listing; the second the download
                fb.state = "PREPARE"

        job = self.run_backup(fb, lambda name: FlippingLock(flip))
        self.assertEqual(job["status"], "failed")
        self.assertIn("Stopped", job["error"])
        self.assertEqual(fb.downloaded, [])
        self.assertEqual([f for f in os.listdir(os.path.join(self.tmp, "b", "BX1")) if f.endswith(".zip")], [],
                         "no partial zip may be left behind")

    def test_a_driver_that_says_busy_stops_the_backup_cleanly(self):
        fb = self.fake()

        def busy(n):
            raise PrinterBusy("printer is busy (BX1: state RUNNING)")
        fb.sd_download = busy
        job = self.run_backup(fb, lambda name: threading.Lock())
        self.assertEqual(job["status"], "failed")
        self.assertIn("printer is busy", job["error"])


# ================================================================== N-13 command acks

class TestCommandsAreConfirmed(AutoOffBase):
    def test_obeyed_commands_are_confirmed(self):
        prn = make_bambu("IDLE", echo=True)
        r = prn.set_temps(240, 80)
        self.assertEqual((r["nozzle"], r["bed"], r["confirmed"]), (240, 80, True))
        self.assertTrue(prn.set_temps(0, 0)["confirmed"])
        self.assertTrue(prn.light(True)["confirmed"])

    def test_ignored_commands_are_reported_as_sent_but_not_confirmed(self):
        prn = make_bambu("IDLE", echo=False, nozzle_target_temper=240, bed_target_temper=80)
        r = prn.set_temps(0, 0)
        self.assertFalse(r["confirmed"])
        self.assertIn("240", r["unconfirmed"])
        self.assertFalse(prn.light(True)["confirmed"])

    def test_manual_cooldown_says_so_and_keeps_the_auto_off(self):
        prn = make_bambu("IDLE", echo=False, nozzle_target_temper=240, bed_target_temper=80)
        controls._resolver = lambda k: prn
        controls.schedule_auto_off(prn, seconds=3600)
        res = controls.perform(prn, "cooldown")
        self.assertFalse(res["confirmed"])
        self.assertIn("NOT confirmed", res["message"])
        self.assertIsNotNone(controls.auto_off_remaining(prn), "the auto-off must keep trying")

    def test_manual_cooldown_that_worked_cancels_the_auto_off(self):
        prn = make_bambu("IDLE", echo=True, nozzle_target_temper=240, bed_target_temper=80)
        controls.schedule_auto_off(prn, seconds=3600)
        res = controls.perform(prn, "cooldown")
        self.assertNotIn("NOT", res["message"])
        self.assertIsNone(controls.auto_off_remaining(prn))

    def test_preheat_and_light_messages_are_honest(self):
        prn = make_bambu("IDLE", echo=False)
        self.addCleanup(controls.cancel_auto_off, prn)
        res = controls.perform(prn, "preheat", preset="PETG")
        self.assertFalse(res["confirmed"])
        self.assertIn("NOT confirmed", res["message"])
        self.assertIsNotNone(controls.auto_off_remaining(prn), "armed even though unconfirmed")
        res = controls.perform(prn, "light_on")
        self.assertIn("NOT confirmed", res["message"])

    def test_an_ignored_cooldown_by_the_auto_off_is_retried_and_finally_announced(self):
        prn = make_bambu("IDLE", echo=False, nozzle_target_temper=240, bed_target_temper=80)
        controls._resolver = lambda k: prn
        controls.schedule_auto_off(prn, seconds=0.1)
        self.assertTrue(wait_for(lambda: self.failed, timeout=6), "gave up silently")
        self.assertEqual(self.cooled, [], "'cooled down' was announced for a printer that ignored the command")
        self.assertIn("heaters may still be on", self.failed[0][1])
        self.assertIn("240", self.failed[0][1])
        self.assertGreater(len(gcodes(prn)), 1, "it should have retried")

    def test_an_obeyed_cooldown_by_the_auto_off_is_announced(self):
        prn = make_bambu("IDLE", echo=True, nozzle_target_temper=240, bed_target_temper=80)
        controls._resolver = lambda k: prn
        controls.schedule_auto_off(prn, seconds=0.1)
        self.assertTrue(wait_for(lambda: self.cooled, timeout=6))
        self.assertEqual(self.failed, [])

    def test_a_printer_that_never_reported_is_not_waited_for(self):
        prn = make_bambu("IDLE", echo=False, nozzle_target_temper=240)
        prn.last_msg = 0.0
        prn.CONFIRM_WAIT_S = 30
        t = time.time()
        self.assertFalse(prn.set_temps(0, 0)["confirmed"])
        self.assertLess(time.time() - t, 2)

    def test_http_drivers_without_an_ack_are_taken_at_their_word(self):
        self.assertEqual(controls._unconfirmed({"nozzle": 0, "bed": 0}), "")
        self.assertEqual(controls._unconfirmed(None), "")


# ================================================================== N-9 edit / remove during a pre-heat

def report(prn_serial, **fields):
    return json.dumps({"print": {"gcode_state": "IDLE", "nozzle_target_temper": 0, "bed_target_temper": 0,
                                 "lights_report": [{"node": "chamber_light", "mode": "off"}], **fields}})


class Echo(threading.Thread):
    """Plays the printer: every gcode_line the broker receives is answered with a report of the new targets."""

    def __init__(self, broker, serial):
        super().__init__(daemon=True)
        self.broker, self.serial, self.seen, self.stop_flag = broker, serial, 0, False
        self.start()

    def run(self):
        n, noz, bed = 0, 0, 0
        while not self.stop_flag:
            recv = list(self.broker.received)
            for topic, payload in recv[n:]:
                try:
                    g = json.loads(payload).get("print") or {}
                except ValueError:
                    continue
                if topic == f"device/{self.serial}/request" and g.get("command") == "gcode_line":
                    for line in g["param"].split("\n"):
                        if line.startswith("M104 S"):
                            noz = int(line[6:])
                        if line.startswith("M140 S"):
                            bed = int(line[6:])
                    self.broker.send(f"device/{self.serial}/report",
                                     report(self.serial, nozzle_target_temper=noz, bed_target_temper=bed))
            n = len(recv)
            time.sleep(0.05)


class TestEditDuringPreheat(AutoOffBase, BambuCase):
    def setUp(self):
        AutoOffBase.setUp(self)
        BambuCase.setUp(self)
        self.printers = {}
        self.mgr = fleet_store.FleetManager(fleet_store.FleetStore(tempfile.mkdtemp(dir=self.dir)), self.printers,
                                            log=lambda *a: None)
        self.addCleanup(lambda: [fleet_store.stop_driver(p) for p in list(self.printers.values())])
        controls._resolver = lambda k: self.printers.get(k)

    def start_shop(self, echo):
        self.mgr.add("bambu", {"name": "Shop", "ip": "127.0.0.1", "serial": SERIAL, "access_code": "12345678"},
                     skip_test=True)
        prn = self.printers["shop"]
        prn.CONFIRM_WAIT_S = 0.5
        self.assertTrue(wait_for(lambda: prn._connected))
        # a printer that ignores commands still shows the pre-heat targets (240/80) it was heated to
        hot = 0 if echo else 240
        self.report({"print": {"gcode_state": "IDLE", "nozzle_target_temper": hot, "bed_target_temper": hot // 3}})
        self.assertTrue(wait_for(lambda: prn.snapshot()["state"] == "IDLE"))
        self.assertTrue(wait_for(lambda: prn.state["print"].get("nozzle_target_temper") == hot))
        if echo:
            e = Echo(self.broker, SERIAL)
            self.addCleanup(setattr, e, "stop_flag", True)
        controls.perform(prn, "preheat", preset="PETG")
        return prn

    def cools(self):
        return [p for t, p in self.broker.received if b"M104 S0" in p]

    def test_changing_the_serial_cools_the_old_machine_before_its_driver_is_stopped(self):
        prn = self.start_shop(echo=True)
        seen = {}

        def fake_probe(v, timeout=5, port=None):
            seen["cools_before_probe"] = len(self.cools())          # the old driver is already stopped here
            seen["old_client"] = prn._client
            return fleet_store._result(ok=True, model="Bambu Lab P1S")

        with mock.patch.object(fleet_store, "probe_bambu", fake_probe):
            self.mgr._probe_cache.clear()
            self.mgr.update("Shop", {"serial": S2, "access_code": "12345678"})
        self.assertEqual(seen["cools_before_probe"], 1, "the OLD machine was not cooled before it was let go")
        self.assertIsNone(seen["old_client"])
        self.assertIsNone(controls.auto_off_remaining("shop"))
        self.assertEqual(self.failed, [])
        self.assertEqual(self.printers["shop"].serial, S2)

    def test_when_the_old_machine_cannot_be_cooled_it_stays_supervised_on_a_detached_connection(self):
        prn = self.start_shop(echo=False)                            # ignores gcode: "cooled" cannot be confirmed
        probe = lambda v, timeout=5, port=None: fleet_store._result(ok=True, model="Bambu Lab P1S")  # noqa: E731
        with mock.patch.object(fleet_store, "probe_bambu", probe):
            self.mgr._probe_cache.clear()
            self.mgr.update("Shop", {"serial": S2, "access_code": "12345678"})
        with controls._tlock:
            detached = {k: e for k, e in controls._timers.items() if "#detached" in k}
        self.assertEqual(len(detached), 1, "the heated old machine fell out of supervision")
        entry = next(iter(detached.values()))
        clone = entry["prn"]
        self.assertIsNot(clone, prn)
        self.assertEqual((clone.serial, clone.ip), (SERIAL, "127.0.0.1"))      # the OLD machine, not the new one
        self.assertIsNone(controls.auto_off_remaining("shop"), "the new Shop must not inherit it")
        self.assertTrue(wait_for(lambda: clone._connected, timeout=6))
        self.assertTrue(wait_for(lambda: self.failed, timeout=8), "an unreachable heated machine must be announced")
        self.assertIn("heaters may still be on", self.failed[0][1])
        self.assertTrue(wait_for(lambda: clone._client is None, timeout=4), "detached connection not cleaned up")
        self.assertFalse([k for k in controls._timers if "#detached" in k])

    def test_removing_a_printer_that_cannot_be_cooled_keeps_it_supervised(self):
        prn = self.start_shop(echo=False)
        self.mgr.remove("Shop", force=True)
        with controls._tlock:
            detached = [e for k, e in controls._timers.items() if "#detached" in k]
        self.assertEqual(len(detached), 1)
        self.assertTrue(wait_for(lambda: self.failed, timeout=8))

    def test_a_cooled_removal_leaves_nothing_behind(self):
        prn = self.start_shop(echo=True)
        self.mgr.remove("Shop", force=True)
        self.assertEqual(len(self.cools()), 1)
        self.assertEqual([k for k in controls._timers], [])


# ================================================================== N-16 test connection

class TestTestConnectionNeverOpensASecondSession(BambuCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.printers = {}
        self.mgr = fleet_store.FleetManager(fleet_store.FleetStore(self.dir), self.printers, log=lambda *a: None)
        self.addCleanup(lambda: [fleet_store.stop_driver(p) for p in list(self.printers.values())])
        self.mgr.add("bambu", self.vals("P1S"), skip_test=True)
        self.assertTrue(wait_for(lambda: self.printers["p1s"]._connected))
        self.n = self.broker.connects
        p = mock.patch.object(fleet_store, "probe_bambu", side_effect=AssertionError("opened a 2nd MQTT session"))
        p.start()
        self.addCleanup(p.stop)

    def vals(self, name, **kw):
        return {"name": name, "ip": "127.0.0.1", "serial": SERIAL, "access_code": "12345678", **kw}

    def test_same_address_other_access_code_is_refused_not_probed(self):
        with self.assertRaises(fleet_store.Conflict) as cm:
            self.mgr.test("bambu", {"name": "P1S", "access_code": "87654321"}, existing_name="P1S")
        self.assertIn("already connected", str(cm.exception))
        self.assertEqual(self.broker.connects, self.n)

    def test_a_new_name_for_the_same_printer_is_refused(self):
        for kw in ({}, {"ip": "localhost"}, {"ip": "LOCALHOST"}, {"ip": "127.1"}, {"ip": "::1"},
                   {"serial": "01P00A000000009"}):          # same serial / same address in another spelling
            with self.assertRaises(fleet_store.Conflict, msg=str(kw)):
                self.mgr.test("bambu", self.vals("Copy", **kw))
        self.assertEqual(self.broker.connects, self.n)

    def test_add_of_a_duplicate_in_another_spelling_is_refused(self):
        with self.assertRaises(fleet_store.Conflict):
            self.mgr.add("bambu", self.vals("Copy", ip="localhost", serial="01P00A000000009"))
        with self.assertRaises(fleet_store.Conflict):
            self.mgr.add("bambu", self.vals("Copy", ip="127.1", serial="01P00A000000009"), skip_test=True)
        self.assertEqual(list(self.printers), ["p1s"])
        self.assertEqual(self.broker.connects, self.n)

    def test_unchanged_connected_printer_is_answered_from_the_live_session(self):
        r = self.mgr.test("bambu", {"name": "P1S"}, existing_name="P1S")
        self.assertTrue(r["ok"])
        self.assertEqual(self.broker.connects, self.n)

    def test_an_unconnected_driver_may_be_probed_to_find_out_why(self):
        prn = self.printers["p1s"]
        fleet_store.stop_driver(prn)
        with mock.patch.object(fleet_store, "probe_bambu",
                               return_value=fleet_store._result(ok=True, model="Bambu Lab P1S")) as pb:
            r = self.mgr.test("bambu", {"name": "P1S", "access_code": "87654321"}, existing_name="P1S")
        self.assertTrue(r["ok"])
        pb.assert_called_once()

    def test_address_spellings_compare_by_what_they_mean(self):
        n = fleet_store.norm_host
        self.assertEqual({n(x) for x in ("127.0.0.1", "127.1", "localhost", "LOCALHOST", "::1",
                                         "0:0:0:0:0:0:0:1", "[::1]")}, {"127.0.0.1"})
        self.assertEqual(n("192.168.1.221"), n(" 192.168.1.221 "))
        self.assertNotEqual(n("192.168.1.221"), n("192.168.1.222"))
        self.assertEqual(n("Printer.Local"), "printer.local")
        self.assertNotEqual(n("127.0.0.2"), n("127.0.0.1"))        # only the real loopback aliases collapse
        self.assertEqual(n("2001:DB8:0:0:0:0:0:1"), n("2001:db8::1"))


# ================================================================== N-7 userinfo

class TestUrlPasswordsNeverLeak(unittest.TestCase):
    URL = "http://maker:hunter2@127.0.0.1:1"

    def test_helpers(self):
        self.assertEqual(bambu.strip_userinfo("http://u:p@h:80/x?a=b@c"), "http://h:80/x?a=b@c")
        self.assertEqual(bambu.strip_userinfo("https://u:p@ss@h"), "https://h")
        self.assertEqual(bambu.strip_userinfo("http://h/a@b"), "http://h/a@b")
        self.assertEqual(bambu.scrub_text(OSError("bad http://u:pw@h/ thing")), "bad http://h/ thing")
        self.assertIsNone(bambu.strip_userinfo(None))

    def test_drivers_never_show_it(self):
        drivers = {"octoprint": OctoPrintPrinter("o", self.URL, "K"),
                   "prusalink": PrusaLinkPrinter("p", self.URL, {"password": "x"}),
                   "moonraker": MoonrakerPrinter("m", self.URL, "")}
        for kind, d in drivers.items():
            self.assertNotIn("hunter2", d.url, kind)
            if kind == "octoprint":
                with self.assertRaises(PrinterError) as cm:
                    d.connect_serial()
                msg = str(cm.exception)
            else:
                s = d.snapshot()
                msg = json.dumps(s)
            self.assertNotIn("hunter2", msg, kind)
            self.assertNotIn("maker:", msg, kind)
            self.assertIn("127.0.0.1", msg, kind)


class TestFleetFileModes(unittest.TestCase):
    def test_printers_json_and_secrets_json_are_owner_only(self):
        import stat
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        st = fleet_store.FleetStore(d)
        st.add({"name": "P", "type": "octoprint", "url": "http://10.0.0.5"}, {"api_key": "K"})
        for f in ("printers.json", "secrets.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode), 0o600, f)
        st.remove("P")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "printers.json")).st_mode), 0o600)


# ================================================================== multi-AMS

class TestMultiAms(unittest.TestCase):
    def ams(self, units):
        return {"ams": {"tray_now": "255", "ams": units}}

    def unit(self, uid, *types):
        return {"id": str(uid), "tray": [{"id": str(i), **({"tray_type": t, "tray_color": "FF0000FF",
                                                            "nozzle_temp_min": "190"} if t else {})}
                                         for i, t in enumerate(types)]}

    def test_second_unit_trays_get_their_own_ids(self):
        prn = make_bambu(**self.ams([self.unit(0, "PLA", None, None, None), self.unit(1, "PETG", "ABS", None, None)]))
        trays = {t["tray"]: t for t in prn.ams_trays()}
        self.assertEqual(sorted(trays), [0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual((trays[4]["type"], trays[5]["type"], trays[4]["slot_1based"]), ("PETG", "ABS", 5))
        self.assertEqual(prn.tray_raw(4)["tray_type"], "PETG")
        self.assertEqual(prn.tray_raw(0)["tray_type"], "PLA")

    def test_a_single_ams_is_exactly_as_before(self):
        prn = make_bambu(**self.ams([self.unit(0, "PLA", "PETG", None, None)]))
        self.assertEqual([(t["tray"], t["slot_1based"]) for t in prn.ams_trays()], [(0, 1), (1, 2), (2, 3), (3, 4)])
        prn.load_filament(2)
        m = sent(prn)[-1]["print"]
        self.assertEqual((m["ams_id"], m["target"], m["slot_id"]), (0, 1, 1))

    def test_load_and_set_tray_address_the_right_unit(self):
        prn = make_bambu(**self.ams([self.unit(0, "PLA", None, None, None), self.unit(1, "PETG", "ABS", None, None)]))
        prn.load_filament(6)                                           # slot 6 (1-based) = unit 1, tray 1
        m = sent(prn)[-1]["print"]
        self.assertEqual((m["ams_id"], m["target"], m["slot_id"]), (1, 5, 1))
        prn.set_tray(5, "PETG", "#FF0000")
        m = sent(prn)[-1]["print"]
        self.assertEqual((m["command"], m["ams_id"], m["tray_id"]), ("ams_filament_setting", 1, 1))
        prn.set_tray(2, "PLA", "#00FF00")
        m = sent(prn)[-1]["print"]
        self.assertEqual((m["ams_id"], m["tray_id"]), (0, 2))


if __name__ == "__main__":
    unittest.main(verbosity=1)
