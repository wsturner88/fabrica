"""Regression tests for the 2026-10-11 hardening review: physical-safety and driver findings.

    python3 tests/test_fix_safety.py          (needs: paho-mqtt; the Bambu tests also need the openssl CLI)

Covers (review ids in brackets):
  * pre-heat auto-off: only connected + positively idle, retry otherwise, never into a print,
    persisted + re-armed at start-up, cooled before a printer is removed/re-pointed, clamped, and a
    timer that cannot be killed                                                        [P0-4, P0-5, P2]
  * Bambu commands fail loudly while the MQTT link is down / reconnecting              [P0-7]
  * Bambu MQTT thread survives junk payloads, and a watchdog replaces a dead one       [P1-6]
  * HTTP drivers never follow a redirect and never report a refused request as sent    [P1-7]
  * fleet: a failed edit puts back the OLD secrets; a stored secret is never reused for
    a new address; one physical Bambu = one MQTT session                               [P1-8, P0-8, P1-9]

Loopback only - nothing here touches a real printer or any other host.
"""
import http.server
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import bambu  # noqa: E402
import controls  # noqa: E402
import fleet_store  # noqa: E402
from bambu import BambuPrinter, OctoPrintPrinter, PrinterError  # noqa: E402
from moonraker import MoonrakerPrinter  # noqa: E402
from prusalink import PrusaLinkPrinter  # noqa: E402

try:
    import paho.mqtt.client  # noqa: F401
    HAVE_PAHO = True
except ImportError:
    HAVE_PAHO = False


def wait_for(cond, timeout=6.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


# ================================================================== pre-heat auto-off

class FakePrn:
    """In-process driver double: records every heater command."""
    kind = "fake"

    def __init__(self, name="Shop", state="IDLE", connected=True, ip="10.0.0.5"):
        self.name, self.state, self.connected, self.ip = name, state, connected, ip
        self.sent, self.fail = [], False
        self.log = None

    def snapshot(self):
        return {"printer": self.name, "state": self.state, "connected": self.connected}

    def control_caps(self):
        return {"heat": {"supported": True}, "light": {"supported": False},
                "unload": {"supported": False}, "load": {"supported": False}}

    def set_temps(self, nozzle=None, bed=None):
        if self.fail or not self.connected:
            raise PrinterError(f"{self.name}: not connected")
        self.sent.append((nozzle, bed))
        if self.log is not None:
            self.log.append(("set_temps", self.name, nozzle, bed))
        return {"nozzle": nozzle, "bed": bed}

    def connect(self):
        pass

    def disconnect(self):
        self.connected = False
        if self.log is not None:
            self.log.append(("disconnect", self.name))


class AutoOffBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.saved = {k: getattr(controls, k) for k in
                      ("RETRY_WINDOW_S", "RETRY_STEPS", "OBSERVE_EVERY_S", "_data_dir", "_resolver",
                       "_on_auto_off", "_on_auto_off_failed", "_timeout_min")}
        self.saved_restored = controls._restored[0]
        controls._data_dir = self.dir
        controls.RETRY_WINDOW_S, controls.RETRY_STEPS, controls.OBSERVE_EVERY_S = 1.2, (0.1,), 0.1
        controls._resolver = None
        controls._on_auto_off = controls._on_auto_off_failed = None
        controls._timeout_min = None
        controls._last_auto_off.clear()
        controls._restored[0] = True            # no implicit restore from configure() unless a test asks
        self.addCleanup(self._restore)
        controls.reset_for_tests()
        self.failed, self.cooled = [], []
        controls.set_runtime(on_auto_off_failed=lambda n, why: self.failed.append((n, why)),
                             on_auto_off=lambda n, m: self.cooled.append((n, m)))

    def _restore(self):
        controls.reset_for_tests()
        for k, v in self.saved.items():
            setattr(controls, k, v)
        controls._restored[0] = self.saved_restored


class TestAutoOffSafety(AutoOffBase):
    def test_disconnected_printer_gets_no_heater_command_then_cools_when_it_is_back(self):
        p = FakePrn(connected=False, state="IDLE")            # last report said IDLE, but the link is down
        controls.schedule_auto_off(p, seconds=0.1)
        time.sleep(0.7)
        self.assertEqual(p.sent, [])                          # never sent into the void
        self.assertIsNotNone(controls.auto_off_remaining(p))  # still pending: it will retry
        p.connected = True
        self.assertTrue(wait_for(lambda: p.sent == [(0, 0)]), "never cooled once the printer was back")
        # the "cooled" hook and the result line are written just AFTER the command is sent: wait for them too
        self.assertTrue(wait_for(lambda: len(self.cooled) >= 1 and controls.auto_off_remaining(p) is None))
        self.assertIn("cooled down", controls._last_auto_off["shop"]["result"])
        time.sleep(0.3)
        self.assertEqual(len(self.cooled), 1)                                   # the hook fired exactly once
        self.assertEqual(p.sent, [(0, 0)])                                      # and only one cool-down was sent

    def test_unknown_state_is_never_cooled_and_the_failure_is_announced(self):
        p = FakePrn(state="UNKNOWN", connected=True)
        controls.schedule_auto_off(p, seconds=0.05)
        self.assertTrue(wait_for(lambda: self.failed, timeout=5), "gave up silently")
        self.assertEqual(p.sent, [])
        self.assertIn("gave up", self.failed[0][1])
        self.assertEqual(self.failed[0][0], "Shop")
        self.assertTrue(controls._last_auto_off["shop"]["result"].startswith("failed:"))
        self.assertIsNone(controls.auto_off_remaining(p))

    def test_never_sends_to_a_busy_printer(self):
        for st in ("RUNNING", "PREPARE", "PAUSE"):
            p = FakePrn(name=f"B{st}", state=st)
            controls.schedule_auto_off(p, seconds=0.05)
            self.assertTrue(wait_for(lambda: controls.auto_off_remaining(p) is None))
            self.assertEqual(p.sent, [], st)
            self.assertIn("skipped", controls._last_auto_off[p.name.lower()]["result"])

    def test_idle_finish_failed_are_cooled(self):
        for st in ("IDLE", "FINISH", "FAILED"):
            p = FakePrn(name=f"I{st}", state=st)
            controls.schedule_auto_off(p, seconds=0.05)
            self.assertTrue(wait_for(lambda: p.sent == [(0, 0)]), st)

    def test_observed_print_drops_the_timer_but_a_start_announcement_does_not(self):
        p = FakePrn(state="IDLE")
        controls.schedule_auto_off(p, seconds=60)
        self.assertFalse(controls.note_print_start(p))
        self.assertIsNotNone(controls.auto_off_remaining(p))
        p.state = "PREPARE"                                   # the printer is now actually starting a job
        self.assertTrue(wait_for(lambda: controls.auto_off_remaining(p) is None), "observer never saw it")
        self.assertEqual(p.sent, [])

    def test_failed_start_leaves_the_auto_off_armed(self):
        """The server used to cancel the timer before start_print(); a refused start left heaters on."""
        p = FakePrn(state="IDLE")
        controls.schedule_auto_off(p, seconds=0.3)
        controls.note_print_start(p)                          # what server.start_print does first
        # ... start_print() raised / the printer ignored it: still IDLE
        self.assertTrue(wait_for(lambda: p.sent == [(0, 0)]), "heaters left on after a failed start")

    def test_set_temps_failure_at_fire_time_is_retried(self):
        p = FakePrn(state="IDLE")
        p.fail = True
        controls.schedule_auto_off(p, seconds=0.05)
        time.sleep(0.5)
        self.assertEqual(p.sent, [])
        p.fail = False
        self.assertTrue(wait_for(lambda: p.sent == [(0, 0)]))

    def test_name_reused_for_another_machine_is_not_cooled(self):
        old, new = FakePrn("Shop", ip="10.0.0.5"), FakePrn("Shop", ip="10.0.0.99")
        live = {"shop": new}
        controls._resolver = lambda k: live.get(k)
        controls.schedule_auto_off(old, seconds=3600)
        tok = controls._timers["shop"]["token"]
        controls._fire("shop", tok)
        self.assertEqual(new.sent, [])                         # the new machine is not the pre-heated one
        self.assertEqual(old.sent, [(0, 0)])                   # the pre-heated one still is

    def test_live_driver_of_the_same_machine_is_used_after_an_edit(self):
        old, new = FakePrn("Shop", ip="10.0.0.5"), FakePrn("Shop", ip="10.0.0.5")
        controls._resolver = lambda k: new
        controls.schedule_auto_off(old, seconds=3600)
        controls._fire("shop", controls._timers["shop"]["token"])
        self.assertEqual((old.sent, new.sent), ([], [(0, 0)]))

    def test_timeout_is_clamped_and_cannot_kill_the_timer(self):
        with mock.patch.dict(os.environ, {"PREHEAT_TIMEOUT_MIN": "1e12"}):
            self.assertEqual(controls.timeout_seconds(), 240 * 60)
        for bad in ("inf", "nan", "-5", "0", "x"):
            with mock.patch.dict(os.environ, {"PREHEAT_TIMEOUT_MIN": bad}):
                self.assertEqual(controls.timeout_seconds(), 900, bad)
        controls.set_runtime(timeout_min=1e12)
        self.assertEqual(controls.effective_timeout_seconds(), 240 * 60)
        controls.set_runtime(timeout_min=float("inf"))
        self.assertEqual(controls.effective_timeout_seconds(), 900)
        controls.set_runtime(timeout_min=None)
        p = FakePrn()
        secs = controls.schedule_auto_off(p, seconds=1e12)    # would raise OverflowError in a threading.Timer
        self.assertEqual(secs, 240 * 60)
        self.assertTrue(controls._thread[0].is_alive())
        self.assertGreater(controls.auto_off_remaining(p), 0)
        self.assertEqual(controls.schedule_auto_off(p, seconds=float("nan")), 900)

    def test_supervisor_survives_exceptions(self):
        p = FakePrn()
        boom = {"n": 0}
        real = controls._service

        def flaky(key, e, now):
            if boom["n"] < 3:
                boom["n"] += 1
                raise RuntimeError("boom")
            return real(key, e, now)

        with mock.patch.object(controls, "_service", flaky):
            controls.schedule_auto_off(p, seconds=0.05)
            self.assertTrue(wait_for(lambda: p.sent == [(0, 0)], timeout=8), "an exception killed the timer")
        self.assertTrue(controls._thread[0].is_alive())

    def test_supervisor_restarts_if_its_thread_is_gone(self):
        p = FakePrn()
        controls.schedule_auto_off(p, seconds=60)
        t = controls._thread[0]
        controls._thread[0] = None                             # as if it had never started
        controls.schedule_auto_off(p, seconds=0.05)
        self.assertIsNot(controls._thread[0], t)
        self.assertTrue(wait_for(lambda: p.sent == [(0, 0)]))


class TestAutoOffHttpDriver(AutoOffBase):
    """The review's repro: a REAL OctoPrintPrinter against a fake OctoPrint that is printing."""

    def setUp(self):
        super().setUp()
        self.mode, self.posts = {"printer": "printing"}, []
        mode, posts = self.mode, self.posts

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.startswith("/api/printer"):
                    if mode["printer"] == "fail":
                        return self._send(500, {})
                    flags = {"printing": True} if mode["printer"] == "printing" else {"operational": True}
                    return self._send(200, {"state": {"flags": flags}, "temperature": {}})
                if self.path.startswith("/api/job"):
                    return self._send(200, {"job": {"file": {"name": "part.gcode"}}, "progress": {"completion": 40}})
                self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                posts.append((self.path, self.rfile.read(n).decode()))
                self.send_response(204)
                self.end_headers()

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.prn = OctoPrintPrinter("MK4", f"http://127.0.0.1:{self.srv.server_address[1]}", "key")

    def fire_now(self):
        controls.schedule_auto_off(self.prn, seconds=3600)
        controls._fire("mk4", controls._timers["mk4"]["token"])

    def test_failing_status_poll_mid_print_sends_no_heater_command(self):
        self.mode["printer"] = "fail"                          # the poll fails: reads UNKNOWN / not connected
        self.fire_now()
        self.assertEqual(self.posts, [])
        self.assertIsNotNone(controls.auto_off_remaining("mk4"))     # retrying, not "cooled down"
        self.assertNotIn("cooled", controls._last_auto_off.get("mk4", {}).get("result", ""))

    def test_a_stale_idle_reading_does_not_hide_a_print_that_just_started(self):
        self.mode["printer"] = "idle"
        self.assertEqual(self.prn.snapshot()["state"], "IDLE")  # cached for 3 s
        self.mode["printer"] = "printing"                       # started from OctoPrint's own UI
        self.assertEqual(self.prn.snapshot()["state"], "IDLE")  # (still the cached answer)
        self.fire_now()                                         # the auto-off must look FRESH
        self.assertEqual(self.posts, [])
        self.assertIn("skipped", controls._last_auto_off["mk4"]["result"])

    def test_idle_octoprint_is_cooled(self):
        self.mode["printer"] = "idle"
        self.fire_now()
        self.assertEqual([p for p, _ in self.posts], ["/api/printer/tool", "/api/printer/bed"])

    def test_octoprint_cancelling_flags_are_not_idle(self):
        p = OctoPrintPrinter("o", "http://127.0.0.1:9", "k")
        for flag in ("cancelling", "finishing", "resuming"):
            p._ok, p._last_ts = True, time.time()
            p._last = ({"state": {"flags": {"operational": True, flag: True}}, "temperature": {}},
                       {"progress": {}, "job": {"file": {}}})
            self.assertEqual(p.snapshot()["state"], "PREPARE", flag)


class TestAutoOffPersistence(AutoOffBase):
    def file(self):
        with open(os.path.join(self.dir, "preheat_timers.json")) as fh:
            return json.load(fh)

    def simulate_restart(self, printers):
        with controls._tlock:
            controls._timers.clear()                           # the old process is gone
        controls._restored[0] = False
        controls.configure(None, resolver=lambda k: printers.get(k), data_dir=self.dir)

    def test_deadline_is_persisted_and_rearmed_after_a_restart(self):
        p = FakePrn()
        controls.schedule_auto_off(p, seconds=3000, nozzle=240, bed=80)
        saved = self.file()["timers"]
        self.assertEqual([t["key"] for t in saved], ["shop"])
        self.assertAlmostEqual(saved[0]["deadline"], time.time() + 3000, delta=5)
        self.simulate_restart({"shop": p})
        self.assertAlmostEqual(controls.auto_off_remaining("shop"), 3000, delta=5)
        controls.cancel_auto_off(p)
        self.assertEqual(self.file()["timers"], [])           # cancelling forgets it on disk too

    def test_deadline_that_passed_while_down_cools_once_the_printer_is_idle(self):
        p = FakePrn(connected=False)                           # just booted: not connected yet
        controls.schedule_auto_off(p, seconds=3000)
        doc = self.file()
        doc["timers"][0]["deadline"] = time.time() - 500
        with open(os.path.join(self.dir, "preheat_timers.json"), "w") as fh:
            json.dump(doc, fh)
        self.simulate_restart({"shop": p})
        time.sleep(0.4)
        self.assertEqual(p.sent, [])                           # not confirmed idle yet
        p.connected = True
        self.assertTrue(wait_for(lambda: p.sent == [(0, 0)]))
        self.assertEqual(len(p.sent), 1)                       # exactly once
        self.assertTrue(wait_for(lambda: self.file()["timers"] == []))

    def test_restore_for_a_printer_that_is_gone_is_announced_not_silent(self):
        p = FakePrn()
        controls.schedule_auto_off(p, seconds=3000)
        self.simulate_restart({})
        self.assertIsNone(controls.auto_off_remaining("shop"))
        self.assertTrue(self.failed and "no longer in the fleet" in self.failed[0][1])

    def test_restore_does_not_bind_a_timer_to_a_different_machine(self):
        p = FakePrn(ip="10.0.0.5")
        controls.schedule_auto_off(p, seconds=3000)
        other = FakePrn(ip="10.0.0.77")
        self.simulate_restart({"shop": other})
        self.assertIsNone(controls.auto_off_remaining("shop"))
        self.assertEqual(other.sent, [])
        self.assertTrue(self.failed)

    def test_garbage_state_file_never_stops_startup(self):
        with open(os.path.join(self.dir, "preheat_timers.json"), "w") as fh:
            fh.write("{not json")
        self.simulate_restart({})                              # must not raise
        with open(os.path.join(self.dir, "preheat_timers.json"), "w") as fh:
            json.dump({"timers": [None, {"key": "x"}, {"key": "shop", "deadline": "soon"}]}, fh)
        self.simulate_restart({"shop": FakePrn()})

    def test_no_data_dir_means_memory_only_not_a_crash(self):
        controls._data_dir = "/nonexistent-root-dir/zzz/yyy"
        p = FakePrn()
        controls.schedule_auto_off(p, seconds=60)             # the write fails; the timer still runs
        self.assertIsNotNone(controls.auto_off_remaining(p))


def octo_family():
    """A fleet family whose drivers are FakePrn (name/ip) so FleetManager can add/edit/remove them."""
    fields = [{"name": "name", "type": "text", "required": True},
              {"name": "url", "type": "url", "required": True},
              {"name": "api_key", "type": "password", "required": True}]
    seen = []

    def probe(v, t):
        seen.append(dict(v))
        return {"ok": True, "model": "fake"}

    class Drv(FakePrn):
        def __init__(self, v):
            super().__init__(v["name"], ip=v["url"])
            self.v = dict(v)
            self.connected = True

        def connect(self):
            if self.v.get("api_key") == "BAD":
                raise RuntimeError("connect failed")

        def disconnect(self):
            super().disconnect()

    return fleet_store.Family("octoprint", "Fake", "d", fields, probe, lambda v: Drv(v)), seen


class TestFleetReleasesHeaters(AutoOffBase):
    def setUp(self):
        super().setUp()
        self.fam, self.seen = octo_family()
        self.printers = {}
        self.mgr = fleet_store.FleetManager(fleet_store.FleetStore(tempfile.mkdtemp(dir=self.dir)), self.printers,
                                            families={"octoprint": self.fam}, log=lambda *a: None)
        controls._resolver = lambda k: self.printers.get(k)
        self.mgr.add("octoprint", {"name": "Shop", "url": "http://10.0.0.5", "api_key": "K"}, skip_test=True)
        self.prn = self.printers["shop"]
        self.log = self.prn.log = []

    def test_remove_cools_an_idle_preheated_printer_before_dropping_it(self):
        controls.schedule_auto_off(self.prn, seconds=3600)
        self.mgr.remove("Shop")
        self.assertEqual(self.log, [("set_temps", "Shop", 0, 0), ("disconnect", "Shop")])   # cooled FIRST
        self.assertIsNone(controls.auto_off_remaining("shop"))

    def test_remove_never_touches_the_heaters_of_a_printing_machine(self):
        controls.schedule_auto_off(self.prn, seconds=3600)
        self.prn.state = "RUNNING"
        self.mgr.remove("Shop", force=True)
        self.assertEqual([x for x in self.log if x[0] == "set_temps"], [])
        self.assertIsNone(controls.auto_off_remaining("shop"))

    def test_remove_of_an_unreachable_printer_keeps_the_timer_then_announces(self):
        self.prn.connected = False
        controls.schedule_auto_off(self.prn, seconds=3600)
        self.mgr.remove("Shop", force=True)
        self.assertIsNotNone(controls.auto_off_remaining("shop"))     # kept, bound to that machine
        self.assertTrue(wait_for(lambda: self.failed, timeout=6), "gave up silently")
        self.assertIn("heaters may still be on", self.failed[0][1])
        self.assertEqual([x for x in self.log if x[0] == "set_temps"], [])

    def test_removed_then_same_name_added_for_another_machine_is_left_alone(self):
        self.prn.connected = False
        controls.schedule_auto_off(self.prn, seconds=3600)
        self.mgr.remove("Shop", force=True)
        self.mgr.add("octoprint", {"name": "Shop", "url": "http://10.0.0.99", "api_key": "K"}, skip_test=True)
        new = self.printers["shop"]
        self.assertTrue(wait_for(lambda: self.failed, timeout=6))
        self.assertEqual(new.sent, [])                                # the new machine never got "heaters off"

    def test_edit_to_a_new_address_cools_the_old_machine_first(self):
        controls.schedule_auto_off(self.prn, seconds=3600)
        self.mgr.update("Shop", {"url": "http://10.0.0.6", "api_key": "K"}, skip_test=True)
        self.assertEqual(self.log[0], ("set_temps", "Shop", 0, 0))
        self.assertIsNone(controls.auto_off_remaining("shop"))
        self.assertIsNot(self.printers["shop"], self.prn)

    def test_edit_of_the_same_machine_keeps_the_timer(self):
        controls.schedule_auto_off(self.prn, seconds=3600)
        self.mgr.update("Shop", {"api_key": "K2"}, skip_test=True)
        self.assertIsNotNone(controls.auto_off_remaining("shop"))
        self.assertEqual([x for x in self.log if x[0] == "set_temps"], [])


class TestControlGates(unittest.TestCase):
    def test_cooldown_is_allowed_whenever_connected_and_not_printing(self):
        for st in ("UNKNOWN", "SLICING", "DISCONNECTED", "IDLE", "FINISH", "FAILED"):
            self.assertIsNone(controls._gate({"state": st, "connected": True}, "cooldown"), st)
        for st in ("RUNNING", "PREPARE", "PAUSE"):
            self.assertIsNotNone(controls._gate({"state": st, "connected": True}, "cooldown"), st)
        self.assertIsNotNone(controls._gate({"state": "IDLE", "connected": False}, "cooldown"))

    def test_preheat_and_filament_still_need_a_positively_idle_printer(self):
        for act in ("preheat", "unload", "load"):
            for st in ("UNKNOWN", "SLICING", "RUNNING"):
                self.assertIsNotNone(controls._gate({"state": st, "connected": True}, act), (act, st))
            self.assertIsNotNone(controls._gate({"state": "IDLE", "connected": False}, act))
            self.assertIsNone(controls._gate({"state": "IDLE", "connected": True}, act))

    def test_a_hard_fault_is_not_idle(self):
        snap = {"state": "FAILED", "connected": True, "fault": True}
        self.assertIsNotNone(controls._gate(snap, "preheat"))
        self.assertIsNotNone(controls._gate(snap, "load"))
        self.assertIsNone(controls._gate(snap, "cooldown"))                # heaters OFF is always the safe way


# ================================================================== Bambu MQTT

def make_cert(d):
    cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                        "-out", cert, "-days", "2", "-subj", "/CN=localhost"], capture_output=True)
    return (cert, key) if r.returncode == 0 else None


def _rl(n):
    out = b""
    while True:
        b, n = n % 128, n // 128
        out += bytes([b | (128 if n else 0)])
        if not n:
            return out


class Broker:
    """Minimal TLS MQTT 3.1.1 broker on loopback: accepts any login, records PUBLISHes, can push a
    payload to every client or drop every connection."""

    def __init__(self, cert, port=0):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(*cert)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.conns, self.received, self.connects, self.stop = [], [], 0, False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop:
            try:
                raw, _ = self.srv.accept()
            except OSError:
                return
            try:
                s = self.ctx.wrap_socket(raw, server_side=True)
            except Exception:
                continue
            self.conns.append(s)
            threading.Thread(target=self._serve, args=(s,), daemon=True).start()

    @staticmethod
    def _read(s):
        h = s.recv(1)
        if not h:
            return None, None
        mult, n = 1, 0
        while True:
            b = s.recv(1)[0]
            n += (b & 127) * mult
            mult *= 128
            if not b & 128:
                break
        data = b""
        while len(data) < n:
            c = s.recv(n - len(data))
            if not c:
                return None, None
            data += c
        return h[0], data

    def _serve(self, s):
        try:
            while True:
                t, d = self._read(s)
                if t is None:
                    return
                kind = t >> 4
                if kind == 1:
                    self.connects += 1
                    s.sendall(bytes([0x20, 2, 0, 0]))
                elif kind == 8:
                    s.sendall(b"\x90\x03" + d[:2] + b"\x00")
                elif kind == 3:
                    tl = struct.unpack("!H", d[:2])[0]
                    self.received.append((d[2:2 + tl].decode(), d[2 + tl:]))
                elif kind == 12:
                    s.sendall(b"\xd0\x00")
                elif kind == 14:
                    return
        except Exception:
            return

    def send(self, topic, payload):
        tb = topic.encode()
        body = struct.pack("!H", len(tb)) + tb + (payload if isinstance(payload, bytes) else payload.encode())
        for s in list(self.conns):
            try:
                s.sendall(b"\x30" + _rl(len(body)) + body)
            except Exception:
                pass

    def drop_all(self):
        for s in list(self.conns):
            for fn in (lambda: s.shutdown(socket.SHUT_RDWR), s.close):
                try:
                    fn()
                except Exception:
                    pass
        self.conns = []

    def close(self):
        self.stop = True
        self.drop_all()
        try:
            self.srv.close()
        except OSError:
            pass


SERIAL = "01P00A000000001"
TOPIC = f"device/{SERIAL}/report"


class BambuCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certdir = tempfile.mkdtemp()
        cls.cert = make_cert(cls.certdir) if shutil.which("openssl") else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.certdir, True)

    def setUp(self):
        if not HAVE_PAHO:
            self.skipTest("paho-mqtt not installed")
        if not self.cert:
            self.skipTest("openssl CLI not available to make a test certificate")
        self.broker = Broker(self.cert)
        self.addCleanup(self.broker.close)
        old = BambuPrinter.MQTT_PORT
        BambuPrinter.MQTT_PORT = self.broker.port
        self.addCleanup(setattr, BambuPrinter, "MQTT_PORT", old)

    def printer(self, **kw):
        p = BambuPrinter("t", "127.0.0.1", SERIAL, "12345678", "Bambu Lab P1S")
        p.connect()
        self.addCleanup(p.disconnect)
        self.assertTrue(wait_for(lambda: p._connected), "never connected")
        self.assertTrue(wait_for(lambda: len(self.broker.received) >= 1), "no pushall")
        return p

    def report(self, payload):
        self.broker.send(TOPIC, payload if isinstance(payload, (str, bytes)) else json.dumps(payload))


class TestBambuCommandsFailLoudly(BambuCase):
    def test_commands_are_delivered_while_connected(self):
        p = self.printer()
        n = len(self.broker.received)
        p.pause()
        self.assertTrue(wait_for(lambda: len(self.broker.received) > n))
        self.assertEqual(json.loads(self.broker.received[-1][1])["print"]["command"], "pause")

    def test_every_command_raises_while_the_link_is_down_or_reconnecting(self):
        p = self.printer()
        self.report({"print": {"gcode_state": "RUNNING", "subtask_name": "bracket"}})
        self.assertTrue(wait_for(lambda: p.snapshot()["state"] == "RUNNING"))
        self.broker.close()                                    # printer / broker gone: paho is reconnecting
        self.assertTrue(wait_for(lambda: not p._connected, timeout=8))
        sent_before = len(self.broker.received)
        for name, call in (("stop", p.stop), ("pause", p.pause), ("resume", p.resume),
                           ("start", lambda: p.start_print("x.gcode.3mf", [0])),
                           ("preheat", lambda: p.set_temps(200, 60)), ("cooldown", lambda: p.set_temps(0, 0)),
                           ("light", lambda: p.light(True))):
            with self.assertRaises(PrinterError, msg=name) as cm:
                call()
            self.assertIn("NOT sent", str(cm.exception))
        self.assertEqual(len(self.broker.received), sent_before)

    def test_unconnected_client_raises_instead_of_dropping_silently(self):
        """The review's r3: client object exists, socket never opened -> paho publish rc=4."""
        import paho.mqtt.client as mqtt
        p = BambuPrinter("t", "192.0.2.1", SERIAL, "abcd1234")
        p._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="x", protocol=mqtt.MQTTv311)
        p._connected = True                                     # even if the flag lies, paho's rc is checked
        with self.assertRaises(PrinterError) as cm:
            p.stop()
        self.assertIn("NOT sent", str(cm.exception))

    def test_no_client_at_all_raises_printer_error(self):
        p = BambuPrinter("t", "192.0.2.1", SERIAL, "abcd1234")
        with self.assertRaises(PrinterError):
            p.pause()

    def test_unconfirmed_publish_raises(self):
        p = BambuPrinter("t", "192.0.2.1", SERIAL, "abcd1234")
        info = mock.Mock(rc=0)
        info.is_published.return_value = False
        p._client, p._connected = mock.Mock(), True
        p._client.publish.return_value = info
        p.PUBLISH_WAIT_S = 0.05
        with self.assertRaises(PrinterError) as cm:
            p.stop()
        self.assertIn("not confirmed", str(cm.exception))

    def test_refresh_stays_best_effort(self):
        p = BambuPrinter("t", "192.0.2.1", SERIAL, "abcd1234")
        p.refresh(wait=5)                                       # not connected: returns at once, no raise

    def test_control_surface_reports_the_failure(self):
        """cancel / pre-heat through the shared controls layer: a dead link is an error, never 'sent'."""
        p = BambuPrinter("t", "192.0.2.1", SERIAL, "abcd1234", "Bambu Lab P1S")
        p.state = {"print": {"gcode_state": "IDLE"}}
        p._connected = True                                     # the gate passes; the publish fails
        p._client = mock.Mock()
        p._client.publish.return_value = mock.Mock(rc=4)
        with self.assertRaises(PrinterError):
            controls.perform(p, "light_on")
        self.addCleanup(controls.cancel_auto_off, p)
        with self.assertRaises(PrinterError):
            controls.perform(p, "preheat", preset="PLA")


class TestBambuMqttSurvivesJunk(BambuCase):
    def test_non_object_payloads_do_not_kill_the_loop(self):
        p = self.printer()
        for bad in ("null", "[1,2]", "42", '"x"', "{not json", '{"print": null}', '{"print": 5}', b"\xff\xfe"):
            self.report(bad)
            time.sleep(0.25)
            th = p._client._thread
            self.assertTrue(th is not None and th.is_alive(), f"loop thread died on {bad!r}")
            self.assertTrue(p._connected)
        self.report({"print": {"mc_percent": 99, "gcode_state": "RUNNING"}})
        self.assertTrue(wait_for(lambda: p.snapshot()["percent"] == 99), "stopped updating after junk")
        self.assertEqual(p.snapshot()["state"], "RUNNING")

    def test_junk_never_wipes_known_state(self):
        p = self.printer()
        self.report({"print": {"gcode_state": "RUNNING", "mc_percent": 10}})
        self.assertTrue(wait_for(lambda: p.snapshot()["percent"] == 10))
        self.report('{"print": null}')
        time.sleep(0.3)
        self.assertEqual(p.snapshot()["percent"], 10)

    def test_a_dead_loop_thread_is_noticed_and_replaced(self):
        p = BambuPrinter("t", "127.0.0.1", SERIAL, "12345678", "Bambu Lab P1S")
        p.WATCHDOG_S = 0.2
        p.connect()
        self.addCleanup(p.disconnect)
        self.assertTrue(wait_for(lambda: p._connected))
        first = p._client
        real = p._on_message
        p._on_message = lambda *a: (_ for _ in ()).throw(SystemExit("kill the network thread"))
        first.on_message = p._on_message
        with mock.patch.object(threading, "excepthook", lambda a: None):
            self.report({"print": {"mc_percent": 1}})
            self.assertTrue(wait_for(lambda: first._thread is None or not first._thread.is_alive(), timeout=3))
            self.assertTrue(wait_for(lambda: p._client is not first and p._client is not None, timeout=6),
                            "watchdog never replaced the dead client")
        self.assertTrue(wait_for(lambda: p._connected, timeout=10), "never reconnected")
        self.assertEqual(self.broker.connects, 2)
        p._on_message = real
        p._client.on_message = real
        self.report({"print": {"mc_percent": 77}})
        self.assertTrue(wait_for(lambda: p.snapshot()["percent"] == 77))

    def test_connect_twice_never_leaves_two_sessions(self):
        p = self.printer()
        first = p._client
        p.connect()
        self.assertIsNot(p._client, first)
        self.assertIsNone(first._thread)                       # loop_stop() joined and cleared it
        self.assertTrue(wait_for(lambda: p._connected))

    def test_disconnect_stops_the_watchdog_and_leaves_no_client(self):
        p = self.printer()
        p.disconnect()
        self.assertIsNone(p._client)
        self.assertTrue(p._wd_stop.is_set())
        n = self.broker.connects
        time.sleep(0.6)
        self.assertEqual(self.broker.connects, n)


class TestBambuState(unittest.TestCase):
    def make(self, state, **extra):
        p = BambuPrinter("t", "1.1.1.1", "S", "c", "X1C")
        p.state = {"print": {"gcode_state": state, **extra}}
        p._connected = True
        return p

    def test_known_states_pass_through(self):
        for st in ("IDLE", "PREPARE", "RUNNING", "PAUSE", "FINISH", "FAILED"):
            self.assertEqual(self.make(st).snapshot()["state"], st)

    def test_unknown_states_fail_closed(self):
        s = self.make("WEIRD").snapshot()
        self.assertEqual((s["state"], s["state_raw"]), ("UNKNOWN", "WEIRD"))
        self.assertEqual(self.make("INIT").snapshot()["state"], "UNKNOWN")
        self.assertEqual(self.make("SLICING").snapshot()["state"], "PREPARE")     # busy, never idle
        self.assertNotIn("state_raw", self.make("IDLE").snapshot())
        self.assertEqual(BambuPrinter("t", "1", "S", "c").snapshot()["state"], "UNKNOWN")

    def test_partial_ams_delta_keeps_the_other_trays(self):
        p = self.make("IDLE", ams={"tray_now": "0", "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF"},
            {"id": "1", "tray_type": "PETG", "tray_color": "00FF00FF"}]}]})
        bambu._deep_merge(p.state, {"print": {"ams": {"ams": [{"id": "0", "tray": [{"id": "1", "remain": 50}]}]}}})
        self.assertEqual([(t["tray"], t["type"]) for t in p.ams_trays()], [(0, "PLA"), (1, "PETG")])
        self.assertEqual(p.tray_raw(1)["remain"], 50)
        # a FULL report replaces (a tray that went away does not linger)
        bambu._deep_merge(p.state, {"print": {"ams": {"ams": [{"id": "0", "tray": [
            {"id": "0", "tray_type": "ABS", "tray_color": "000000FF"}, {"id": "1"}]}]}}})
        self.assertEqual([(t["tray"], t["type"]) for t in p.ams_trays()], [(0, "ABS"), (1, None)])


# ================================================================== HTTP drivers

class Hosts:
    """A (the printer) answers with `reply_a`; B (an unrelated host a redirect points at) records hits."""

    def __init__(self, reply_a):
        self.hits_b, self.hits_a = [], []
        hits_b, hits_a = self.hits_b, self.hits_a

        def server(handler_fn):
            class H(http.server.BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def _do(self):
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n) if n else b""
                    handler_fn(self, body)

                do_GET = do_POST = do_PUT = do_DELETE = _do

            s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=s.serve_forever, daemon=True).start()
            return s

        def b(h, body):
            hits_b.append((h.command, h.path, {k.lower(): v for k, v in h.headers.items()}))
            h.send_response(200)
            h.send_header("Content-Length", "2")
            h.end_headers()
            h.wfile.write(b"{}")

        self.B = server(b)
        bport = self.B.server_address[1]

        def a(h, body):
            hits_a.append((h.command, h.path))
            reply_a(h, body, bport)

        self.A = server(a)
        self.url = f"http://127.0.0.1:{self.A.server_address[1]}"

    def close(self):
        self.A.shutdown()
        self.B.shutdown()


def redirecting(h, body, bport):
    h.send_response(302)
    h.send_header("Location", f"http://localhost:{bport}/stolen")
    h.send_header("Content-Length", "0")
    h.end_headers()


def failing(code):
    def fn(h, body, bport):
        h.send_response(code)
        h.send_header("Content-Length", "0")
        h.end_headers()
    return fn


def _octo_delete_past_idle_guard(d):
    """The driver's idle guard (review 3 F6) refuses first on a host that cannot answer; skip it to reach the redirect."""
    with mock.patch.object(bambu, "_require_idle_for_delete"):
        d.sd_delete("job.gcode")


class TestHttpDriversRefuseRedirects(unittest.TestCase):
    def hosts(self, reply):
        h = Hosts(reply)
        self.addCleanup(h.close)
        return h

    def drivers(self, url):
        return {
            "octoprint": OctoPrintPrinter("o", url, "OCTO-SECRET"),
            "prusalink": PrusaLinkPrinter("p", url, {"username": "maker", "password": "pw", "api_key": "PL-SECRET"}),
            "moonraker": MoonrakerPrinter("m", url, "MOON-SECRET"),
        }

    def test_a_redirect_is_an_error_and_nothing_follows_it(self):
        h = self.hosts(redirecting)
        calls = {
            "octoprint": [("stop", lambda d: d.stop()), ("start", lambda d: d.start_print("job.gcode")),
                          ("pause", lambda d: d.pause()), ("temps", lambda d: d.set_temps(200, 60)),
                          ("delete", lambda d: _octo_delete_past_idle_guard(d)), ("connect", lambda d: d.connect_serial())],
            "prusalink": [("cancel", lambda d: d._call("DELETE", "/api/v1/job/7", what="cancel the job")),
                          ("start", lambda d: d.start_print("job.gcode")),
                          ("delete", lambda d: d.sd_delete("job.gcode"))],
            "moonraker": [("stop", lambda d: d.stop()), ("start", lambda d: d.start_print("job.gcode")),
                          ("pause", lambda d: d.pause())],
        }
        for kind, items in calls.items():
            for label, fn in items:
                d = self.drivers(h.url)[kind]
                with self.assertRaises(PrinterError, msg=f"{kind}.{label}") as cm:
                    fn(d)
                self.assertIn("redirected", str(cm.exception).lower(), f"{kind}.{label}")
        self.assertEqual(h.hits_b, [], "a redirect was followed to another host")

    def test_credentials_never_reach_the_redirect_target(self):
        h = self.hosts(redirecting)
        for kind, d in self.drivers(h.url).items():
            if kind == "prusalink":
                d._digest._basic = True
            try:
                if kind == "octoprint":
                    d._get("/api/version")
                elif kind == "prusalink":
                    d._call("GET", "/api/v1/status")
                else:
                    d._call("GET", "/server/info")
            except PrinterError:
                pass
        self.assertEqual(h.hits_b, [])

    def test_a_refused_request_is_a_failure_not_a_success(self):
        for code in (404, 409, 500, 503):
            h = self.hosts(failing(code))
            for label, fn in (("octoprint.stop", lambda u: OctoPrintPrinter("o", u, "k").stop()),
                              ("octoprint.start", lambda u: OctoPrintPrinter("o", u, "k").start_print("a.gcode")),
                              ("prusalink.start", lambda u: PrusaLinkPrinter("p", u, "k").start_print("a.gcode")),
                              ("moonraker.start", lambda u: MoonrakerPrinter("m", u).start_print("a.gcode")),
                              ("moonraker.stop", lambda u: MoonrakerPrinter("m", u).stop())):
                with self.assertRaises(PrinterError, msg=f"{label} {code}"):
                    fn(h.url)

    def test_connection_dropped_without_a_response_is_a_printer_error(self):
        def dropper(h, body, bport):
            h.connection.shutdown(socket.SHUT_RDWR)
            h.connection.close()
        h = self.hosts(dropper)
        for label, fn in (("octoprint.stop", lambda u: OctoPrintPrinter("o", u, "k").stop()),
                          ("octoprint.start", lambda u: OctoPrintPrinter("o", u, "k").start_print("a.gcode")),
                          ("octoprint.temps", lambda u: OctoPrintPrinter("o", u, "k").set_temps(nozzle=200)),
                          ("octoprint.connect", lambda u: OctoPrintPrinter("o", u, "k").connect_serial()),
                          ("prusalink.stop", lambda u: PrusaLinkPrinter("p", u, "k").start_print("a.gcode")),
                          ("moonraker.stop", lambda u: MoonrakerPrinter("m", u).stop())):
            with self.assertRaises(PrinterError, msg=label):
                fn(h.url)

    def test_probe_octoprint_does_not_forward_the_key(self):
        h = self.hosts(redirecting)
        r = fleet_store.probe_octoprint({"url": h.url, "api_key": "OCTO-SECRET"}, timeout=3)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "redirect")
        self.assertEqual(h.hits_b, [])

    def test_a_good_printer_still_works(self):
        def ok(h, body, bport):
            h.send_response(204)
            h.end_headers()
        h = self.hosts(ok)
        OctoPrintPrinter("o", h.url, "k").stop()
        PrusaLinkPrinter("p", h.url, "k").start_print("a.gcode")


# ================================================================== fleet: secrets + sessions

class TestFleetSecrets(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fam, self.seen = octo_family()
        self.fields = self.fam.fields + [{"name": "snapshot_url", "type": "url"}]
        self.fam.fields = self.fields
        self.printers = {}
        self.store = fleet_store.FleetStore(self.dir)
        self.mgr = fleet_store.FleetManager(self.store, self.printers, families={"octoprint": self.fam},
                                            log=lambda *a: None)
        self.mgr.add("octoprint", {"name": "P1", "url": "http://10.0.0.5", "api_key": "GOOD"}, skip_test=True)

    def test_failed_edit_restores_the_old_secret_and_fields(self):
        with self.assertRaises(RuntimeError):
            self.mgr.update("P1", {"url": "http://10.0.0.5", "api_key": "BAD", "snapshot_url": "http://10.0.0.9/s"},
                            skip_test=True)
        self.assertEqual(self.store.secret("P1", "api_key"), "GOOD")          # the rejected secret is NOT stored
        self.assertIsNone(self.store.get("P1").get("snapshot_url"))
        with open(os.path.join(self.dir, "secrets.json")) as fh:              # ...and not on disk either
            self.assertNotIn("BAD", fh.read())
        self.assertEqual(self.printers["p1"].v["api_key"], "GOOD")
        reloaded = fleet_store.FleetStore(self.dir)                           # a restart loads the old one
        self.assertEqual(reloaded.secret("P1", "api_key"), "GOOD")

    def test_failed_edit_leaves_exactly_one_live_driver(self):
        old = self.printers["p1"]
        with self.assertRaises(RuntimeError):
            self.mgr.update("P1", {"api_key": "BAD"}, skip_test=True)
        self.assertIs(self.printers["p1"], old)

    def test_test_connection_reuses_the_secret_only_for_the_same_address(self):
        r = self.mgr.test("octoprint", {"name": "P1", "url": "http://10.0.0.5"}, existing_name="P1")
        self.assertTrue(r["ok"])
        self.assertEqual(self.seen[-1]["api_key"], "GOOD")                     # unchanged address: reused
        self.seen.clear()
        for new in ("http://evil.example", "http://10.0.0.6", "https://10.0.0.5", "http://10.0.0.5:8080"):
            with self.assertRaises(fleet_store.FleetError, msg=new) as cm:
                self.mgr.test("octoprint", {"name": "P1", "url": new}, existing_name="P1")
            self.assertIn("Enter it again", str(cm.exception))
        self.assertEqual(self.seen, [], "the stored secret was sent to a new address")

    def test_test_connection_with_a_new_address_and_a_fresh_secret_is_fine(self):
        r = self.mgr.test("octoprint", {"name": "P1", "url": "http://10.0.0.6", "api_key": "NEW"}, existing_name="P1")
        self.assertTrue(r["ok"])
        self.assertEqual(self.seen[-1], {"name": "P1", "url": "http://10.0.0.6", "api_key": "NEW", "snapshot_url": ""})

    def test_equivalent_spellings_of_the_same_address_keep_the_secret(self):
        for same in ("http://10.0.0.5/", "HTTP://10.0.0.5", "http://10.0.0.5:80", "10.0.0.5"):
            self.seen.clear()
            self.assertTrue(self.mgr.test("octoprint", {"name": "P1", "url": same}, existing_name="P1")["ok"], same)
            self.assertEqual(self.seen[-1]["api_key"], "GOOD", same)

    def test_edit_to_a_new_address_needs_the_secret_again(self):
        with self.assertRaises(fleet_store.FleetError):
            self.mgr.update("P1", {"url": "http://evil.example"})
        self.assertEqual(self.seen, [])                                        # not even probed
        self.assertEqual(self.store.get("P1")["url"], "http://10.0.0.5")
        res = self.mgr.update("P1", {"url": "http://10.0.0.6", "api_key": "NEW"})
        self.assertIn("url", res["changed"])
        self.assertEqual(self.store.secret("P1", "api_key"), "NEW")

    def test_failed_edit_of_a_moved_printer_restores_address_and_secret(self):
        with self.assertRaises(RuntimeError):
            self.mgr.update("P1", {"url": "http://10.0.0.6", "api_key": "BAD"}, skip_test=True)
        self.assertEqual(self.store.get("P1")["url"], "http://10.0.0.5")
        self.assertEqual(self.store.secret("P1", "api_key"), "GOOD")


class TestOneSessionPerBambu(BambuCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.printers = {}
        self.mgr = fleet_store.FleetManager(fleet_store.FleetStore(self.dir), self.printers, log=lambda *a: None)
        self.addCleanup(lambda: [fleet_store.stop_driver(p) for p in list(self.printers.values())])

    def vals(self, name="P1S", **kw):
        return {"name": name, "ip": "127.0.0.1", "serial": SERIAL, "access_code": "12345678", **kw}

    def test_the_same_serial_or_ip_cannot_be_added_twice(self):
        self.mgr.add("bambu", self.vals(), skip_test=True)
        with self.assertRaises(fleet_store.Conflict):
            self.mgr.add("bambu", self.vals("Garage"), skip_test=True)
        with self.assertRaises(fleet_store.Conflict):                          # same IP, other serial
            self.mgr.add("bambu", self.vals("Garage", serial="01P00A000000002"), skip_test=True)
        with self.assertRaises(fleet_store.Conflict):                          # same serial, other IP
            self.mgr.add("bambu", self.vals("Garage", ip="127.0.0.2"), skip_test=True)
        self.assertEqual(list(self.printers), ["p1s"])
        self.mgr.add("bambu", self.vals("Other", ip="127.0.0.2", serial="01P00A000000002"), skip_test=True)

    def test_an_edit_cannot_collide_with_another_printer(self):
        self.mgr.add("bambu", self.vals(), skip_test=True)
        self.mgr.add("bambu", self.vals("Other", ip="127.0.0.2", serial="01P00A000000002"), skip_test=True)
        with self.assertRaises(fleet_store.Conflict):
            self.mgr.update("Other", {"ip": "127.0.0.1", "access_code": "12345678"}, skip_test=True)

    def test_testing_an_unchanged_connected_printer_opens_no_second_session(self):
        self.mgr.add("bambu", self.vals(), skip_test=True)
        self.assertTrue(wait_for(lambda: self.printers["p1s"]._connected))
        n = self.broker.connects
        with mock.patch.object(fleet_store, "probe_bambu", side_effect=AssertionError("opened a 2nd session")):
            r = self.mgr.test("bambu", {"name": "P1S"}, existing_name="P1S")
        self.assertTrue(r["ok"])
        self.assertEqual(self.broker.connects, n)

    def test_edit_probe_runs_with_the_old_session_stopped_and_restores_it_on_failure(self):
        self.mgr.add("bambu", self.vals(), skip_test=True)
        old = self.printers["p1s"]
        self.assertTrue(wait_for(lambda: old._connected))
        seen = []

        def fake_probe(v, timeout=5, port=None):
            seen.append(old._client)                           # the live session must already be gone
            return fleet_store._result(ok=False, error="nope", code="bad_code")

        with mock.patch.object(fleet_store, "probe_bambu", fake_probe):
            self.mgr._probe_cache.clear()
            with self.assertRaises(fleet_store.ProbeFailed):
                self.mgr.update("P1S", {"access_code": "87654321"})
        self.assertEqual(seen, [None])
        self.assertIs(self.printers["p1s"], old)
        self.assertTrue(wait_for(lambda: old._connected, timeout=8), "old session was not brought back")
        self.assertEqual(self.mgr.store.secret("P1S", "access_code"), "12345678")


if __name__ == "__main__":
    unittest.main(verbosity=1)
