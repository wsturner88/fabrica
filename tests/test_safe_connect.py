"""Safe "connect", no flip to NOT CONNECTED on a blip, a clean 503 for the file list, and a polite prewarm.

    python3 tests/test_safe_connect.py          (needs the app requirements)

The incident this guards against: a few seconds of network trouble made the Hub offer "connect" on a healthy
Klipper printer, and the connect was a blind FIRMWARE_RESTART (which aborts a print). Everything runs against
throwaway fake Moonraker / OctoPrint servers on 127.0.0.1; nothing here can reach a real printer.
"""
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="safeconnect-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)

import controls  # noqa: E402
from bambu import (OctoPrintPrinter, PrinterBusy, PrinterError, PrinterUnreachable,  # noqa: E402
                   ConnectNeedsConfirm)
from moonraker import MoonrakerPrinter  # noqa: E402
from linkstate import LinkHold  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover
    HAVE = False
    _ERR = e

with open(os.path.join(APP, "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()


def free_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class Fake:
    """A tiny HTTP server. `routes[(METHOD, path)]` = (status, json) or a callable(handler) -> (status, json).
    Every request is logged as (method, path)."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.log = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def go(self):
                path = self.path.split("?", 1)[0]
                fake.log.append((self.command, path))
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                r = fake.routes.get((self.command, path)) or fake.routes.get(("*", path)) or (404, {"error": "nope"})
                code, body = r(self) if callable(r) else r
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_DELETE = go

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def posts(self):
        return [(m, p) for m, p in self.log if m == "POST"]

    def restarts(self):
        return [p for m, p in self.posts() if "restart" in p]


def moon_routes(klippy="ready", job="standby", nozzle_target=0.0, bed_target=0.0, files=()):
    """A Moonraker as the real one answers: /server/info always works; the printer objects only while Klipper is up."""
    down = klippy != "ready"
    r = {("GET", "/server/info"): (200, {"result": {"klippy_connected": not down, "klippy_state": klippy,
                                                     "moonraker_version": "v0.9.3-test"}}),
         ("GET", "/printer/info"): (503, {"error": {"message": "Klippy Disconnected"}}) if down else
         (200, {"result": {"state": "ready", "software_version": "v0.12.0-test"}}),
         ("POST", "/printer/firmware_restart"): (200, {"result": "ok"}),
         ("POST", "/printer/restart"): (200, {"result": "ok"})}
    status = {"print_stats": {"state": job, "filename": "a.gcode", "print_duration": 10.0, "info": {}},
              "virtual_sdcard": {"progress": 0.2}, "display_status": {"progress": 0.0},
              "extruder": {"temperature": 30.0, "target": nozzle_target},
              "heater_bed": {"temperature": 30.0, "target": bed_target},
              "toolhead": {"position": [0, 0, 5.0, 0]},
              "webhooks": {"state": klippy, "state_message": "x"}}
    r[("GET", "/printer/objects/query")] = (503, {"error": {"message": "Klippy Disconnected"}}) if down else \
        (200, {"result": {"status": status}})
    r[("GET", "/server/files/list")] = (200, {"result": [{"path": f, "modified": 1.0, "size": 10} for f in files]})
    r[("GET", "/server/files/metadata")] = (200, {"result": {}})
    return r


# ====================================================================== Moonraker: connect

class TestMoonrakerConnect(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.addCleanup(self.fake.close)
        self.p = MoonrakerPrinter("T", self.fake.url)

    def set(self, **kw):
        self.fake.routes = moon_routes(**kw)
        self.fake.log.clear()

    def test_connect_while_ready_sends_nothing(self):
        self.set(klippy="ready")
        out = self.p.connect_serial()
        self.assertIn("already connected", out)
        self.assertIn("nothing was sent", out)
        self.assertEqual(self.fake.posts(), [])
        # even a confirmed one: a healthy Klipper is never restarted
        self.assertIn("nothing was sent", self.p.connect_serial(confirm_restart=True))
        self.assertEqual(self.fake.posts(), [])

    def test_connect_while_printing_or_paused_is_refused_always(self):
        for job in ("printing", "paused"):
            self.set(klippy="ready", job=job)
            for confirm in (False, True):
                with self.assertRaises(PrinterBusy, msg=f"{job} {confirm}") as cm:
                    self.p.connect_serial(confirm_restart=confirm)
                self.assertIn("Nothing was sent", str(cm.exception))
            self.assertEqual(self.fake.posts(), [], job)

    def test_connect_while_unreachable_sends_nothing(self):
        p = MoonrakerPrinter("T", free_port_url())
        for confirm in (False, True):
            with self.assertRaises(PrinterUnreachable) as cm:
                p.connect_serial(confirm_restart=confirm)
            self.assertIn("Nothing was sent", str(cm.exception))
            self.assertIn("network", str(cm.exception))

    def test_a_timeout_midway_is_unreachable_too_and_still_sends_nothing(self):
        # Moonraker answers /server/info, then the link dies before the job-state read: still nothing sent
        self.set(klippy="shutdown")

        def drop(h):
            h.connection.shutdown(socket.SHUT_RDWR)
            h.connection.close()
            return 200, {}
        self.fake.routes[("GET", "/printer/objects/query")] = drop
        with self.assertRaises(PrinterError):
            self.p.connect_serial(confirm_restart=True)
        self.assertEqual(self.fake.posts(), [])

    def test_startup_is_left_alone(self):
        self.set(klippy="startup")
        self.assertIn("nothing was sent", self.p.connect_serial(confirm_restart=True))
        self.assertEqual(self.fake.posts(), [])

    def test_restartable_states_ask_first_and_restart_only_after_confirm(self):
        for ks in ("shutdown", "error", "disconnected"):
            self.set(klippy=ks)
            with self.assertRaises(ConnectNeedsConfirm, msg=ks) as cm:
                self.p.connect_serial()
            self.assertIn("This restarts Klipper (FIRMWARE_RESTART). Only do this if the printer shows an error.",
                          str(cm.exception))
            self.assertEqual(self.fake.posts(), [], f"{ks}: nothing may be sent before the confirm")
            self.assertIn("restart requested", self.p.connect_serial(confirm_restart=True))
            self.assertEqual(self.fake.restarts(), ["/printer/firmware_restart"], ks)   # FIRMWARE_RESTART, once, never the plain one

    def test_only_a_literal_true_confirms(self):
        self.set(klippy="shutdown")
        for bad in ("true", 1, "yes", None):
            with self.assertRaises(ConnectNeedsConfirm):
                self.p.connect_serial(confirm_restart=bad)
        self.assertEqual(self.fake.posts(), [])

    def test_a_status_query_that_fails_while_klipper_is_ready_is_not_a_disconnect(self):
        # objects query 503 but /server/info says ready: a hiccup. No DISCONNECTED state, so no connect is ever offered.
        self.set(klippy="ready")
        self.fake.routes[("GET", "/printer/objects/query")] = (503, {"error": {"message": "overloaded"}})
        s = self.p.snapshot()
        self.assertFalse(s["connected"])
        self.assertNotEqual(s["state"], "DISCONNECTED")

    def test_real_klipper_down_still_reports_disconnected(self):
        self.set(klippy="shutdown")
        s = self.p.snapshot()
        self.assertTrue(s["connected"])
        self.assertEqual(s["state"], "DISCONNECTED")


# ====================================================================== OctoPrint: connect

class TestOctoConnect(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.addCleanup(self.fake.close)
        self.p = OctoPrintPrinter("O", self.fake.url, "k")

    def state(self, st):
        self.fake.routes = {("GET", "/api/connection"): (200, {"current": {"state": st, "port": "/dev/ttyACM0"},
                                                                "options": {}}),
                            ("POST", "/api/connection"): (204, {})}
        self.fake.log.clear()

    def test_closed_link_is_connected(self):
        for st in ("Closed", "Offline", "Offline after error", "Error: Lost communication"):
            self.state(st)
            self.assertIn("connect requested", self.p.connect_serial(), st)
            self.assertEqual(self.fake.posts(), [("POST", "/api/connection")], st)

    def test_open_idle_link_sends_nothing(self):
        for st in ("Operational", "Connecting", "Opening serial connection"):
            self.state(st)
            self.assertIn("nothing was sent", self.p.connect_serial(), st)
            self.assertEqual(self.fake.posts(), [], st)

    def test_printing_link_is_refused_never_dropped(self):
        for st in ("Printing", "Paused", "Pausing", "Cancelling", "Finishing", "Printing from SD", "Starting"):
            self.state(st)
            with self.assertRaises(PrinterBusy, msg=st):
                self.p.connect_serial()
            self.assertEqual(self.fake.posts(), [], st)

    def test_unreachable_sends_nothing(self):
        p = OctoPrintPrinter("O", free_port_url(), "k")
        with self.assertRaises(PrinterUnreachable) as cm:
            p.connect_serial()
        self.assertIn("Nothing was sent", str(cm.exception))

    def test_unreadable_state_sends_nothing(self):
        self.fake.routes = {("GET", "/api/connection"): (200, {"weird": 1}), ("POST", "/api/connection"): (204, {})}
        with self.assertRaises(PrinterError):
            self.p.connect_serial()
        self.assertEqual(self.fake.posts(), [])


# ====================================================================== the debounce (pure)

class TestLinkHold(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.h = LinkHold(grace_s=30, min_misses=2, clock=lambda: self.t[0])
        self.up = {"printer": "A", "connected": True, "state": "IDLE", "bed_c": 40.0, "age_seconds": 1}
        self.down = {"printer": "A", "connected": False, "state": "UNKNOWN", "age_seconds": None}

    def test_blip_shorter_than_the_grace_keeps_the_last_state_and_says_reconnecting(self):
        self.h.apply("A", self.up)
        self.t[0] += 5
        out = self.h.apply("A", self.down)
        self.assertEqual((out["connected"], out["state"], out["reconnecting"]), (True, "IDLE", True))
        self.t[0] += 5
        out = self.h.apply("A", self.down)
        self.assertEqual((out["connected"], out["state"], out["reconnecting"]), (True, "IDLE", True))
        self.t[0] += 1
        back = self.h.apply("A", self.up)
        self.assertEqual((back["connected"], back["reconnecting"]), (True, False))

    def test_sustained_outage_goes_through_after_the_grace(self):
        self.h.apply("A", self.up)
        self.h.apply("A", self.down)
        self.t[0] += 29
        self.assertTrue(self.h.apply("A", self.down)["reconnecting"])
        self.t[0] += 2
        out = self.h.apply("A", self.down)
        self.assertEqual((out["connected"], out["state"], out["reconnecting"]), (False, "UNKNOWN", False))

    def test_a_lone_failed_poll_after_a_long_quiet_spell_is_still_only_a_blip(self):
        self.h.apply("A", self.up)
        self.t[0] += 600                       # nobody polled for ten minutes
        self.assertTrue(self.h.apply("A", self.down)["reconnecting"])

    def test_a_recovery_resets_the_clock(self):
        self.h.apply("A", self.up)
        self.h.apply("A", self.down)
        self.t[0] += 25
        self.h.apply("A", self.up)
        self.t[0] += 10
        self.assertTrue(self.h.apply("A", self.down)["reconnecting"])      # a NEW outage, not 35 s of the old one

    def test_nothing_to_hold_goes_straight_through(self):
        out = self.h.apply("B", self.down)
        self.assertEqual((out["connected"], out["reconnecting"]), (False, False))

    def test_a_printer_that_says_it_is_disconnected_is_not_held(self):
        self.h.apply("A", self.up)
        dis = {"printer": "A", "connected": True, "state": "DISCONNECTED"}
        self.assertEqual(self.h.apply("A", dis)["state"], "DISCONNECTED")


# ====================================================================== server: routes, fleet, prewarm

@unittest.skipUnless(HAVE, "needs the app requirements")
class ServerCase(unittest.TestCase):
    NAME = "Klip"

    def setUp(self):
        self.fake = Fake(moon_routes())
        self.addCleanup(self.fake.close)
        self.prn = MoonrakerPrinter(self.NAME, self.fake.url)
        self.prn._cam_probed = time.time()
        server.PRINTERS[self.NAME.lower()] = self.prn
        self.addCleanup(server.PRINTERS.pop, self.NAME.lower(), None)
        server.SETTINGS.update({"security.api_auth": "off"})
        self.client = TestClient(server.mcp.streamable_http_app())
        server.LINKS.forget(self.NAME)
        controls.reset_for_tests()

    def routes(self, **kw):
        self.fake.routes = moon_routes(**kw)
        self.fake.log.clear()
        self.prn.refresh()

    def act(self, **body):
        return self.client.post(f"/api/printer/{self.NAME}/action", json={"action": "connect", **body})


@unittest.skipUnless(HAVE, "needs the app requirements")
class TestConnectRoute(ServerCase):
    def test_ready_sends_nothing_and_says_so(self):
        self.routes(klippy="ready")
        r = self.act()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("nothing was sent", r.json()["result"])
        self.assertEqual(self.fake.posts(), [])

    def test_printing_is_refused_with_409_and_nothing_sent(self):
        self.routes(klippy="ready", job="printing")
        for body in ({}, {"confirm_restart": True}):
            r = self.act(**body)
            self.assertEqual(r.status_code, 409, r.text)
            self.assertIn("printing", r.json()["error"])
        self.assertEqual(self.fake.posts(), [])

    def test_unreachable_is_503_nothing_sent(self):
        self.prn.url = free_port_url()
        r = self.act()
        self.assertEqual(r.status_code, 503, r.text)
        self.assertTrue(r.json()["unreachable"])
        self.assertIn("Nothing was sent", r.json()["error"])

    def test_shutdown_needs_the_second_tap(self):
        self.routes(klippy="shutdown")
        r = self.act()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertTrue(r.json()["needs_confirm"])
        self.assertIn("Only do this if the printer shows an error", r.json()["error"])
        self.assertEqual(self.fake.posts(), [])
        r = self.act(confirm_restart=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.fake.restarts(), ["/printer/firmware_restart"])

    def test_the_mcp_tool_goes_through_the_same_guard(self):
        self.routes(klippy="ready")
        self.assertIn("nothing was sent", server.connect_printer(self.NAME))
        self.assertIn("nothing was sent", server.connect_printer(self.NAME, confirm_restart=True))
        self.routes(klippy="ready", job="paused")
        self.assertTrue(server.connect_printer(self.NAME, confirm_restart=True).startswith("REFUSED"))
        self.routes(klippy="error")
        self.assertTrue(server.connect_printer(self.NAME).startswith("CONFIRM"))
        self.assertEqual(self.fake.posts(), [])
        self.assertIn("restart requested", server.connect_printer(self.NAME, confirm_restart=True))
        self.assertEqual(len(self.fake.restarts()), 1)

    def test_the_hubs_own_busy_view_refuses_before_the_driver_looks(self):
        self.routes(klippy="ready", job="printing")
        self.prn.snapshot()                                  # the Hub has seen the print
        self.fake.log.clear()
        self.assertTrue(server.connect_printer(self.NAME, confirm_restart=True).startswith("REFUSED"))
        self.assertEqual(self.fake.posts(), [])

    def test_the_tool_signature_and_the_dashboard_second_tap(self):
        import inspect
        self.assertFalse(inspect.signature(server.connect_printer).parameters["confirm_restart"].default)
        self.assertIn("confirm_restart: restart === true", HTML)
        self.assertIn("Restart Klipper", HTML)
        self.assertIn("data-act=\"connectRestart\"", HTML)


@unittest.skipUnless(HAVE, "needs the app requirements")
class TestNoFlipOnABlip(ServerCase):
    def setUp(self):
        super().setUp()
        self.t = [5000.0]
        p = mock.patch.object(server.LINKS, "_clock", lambda: self.t[0])
        p.start()
        self.addCleanup(p.stop)

    def fleet(self):
        self.prn.refresh()
        rows = [r for r in self.client.get("/api/fleet").json() if r["printer"] == self.NAME]
        return rows[0]

    def test_fleet_keeps_the_last_state_through_a_blip_then_shows_the_outage(self):
        self.routes(klippy="ready")
        s = self.fleet()
        self.assertEqual((s["connected"], s["state"], s["reconnecting"]), (True, "IDLE", False))
        self.fake.routes = {}                                    # every answer 404s: the same as "not answering" to the poller
        self.fake.close()                                        # truly gone
        self.t[0] += 6
        s = self.fleet()
        self.assertEqual((s["connected"], s["state"], s["reconnecting"]), (True, "IDLE", True))
        self.assertTrue(s["can_connect"])                        # the capability flag stays; the UI also needs state DISCONNECTED
        self.t[0] += 6
        self.assertTrue(self.fleet()["reconnecting"])
        self.t[0] += 30
        s = self.fleet()
        self.assertEqual((s["connected"], s["state"], s["reconnecting"]), (False, "UNKNOWN", False))

    def test_the_detail_view_holds_too(self):
        self.routes(klippy="ready")
        self.prn.refresh()
        self.client.get(f"/api/printer/{self.NAME}")
        self.prn.url = free_port_url()
        self.prn.refresh()
        self.t[0] += 3
        d = self.client.get(f"/api/printer/{self.NAME}").json()
        self.assertEqual((d["snapshot"]["connected"], d["snapshot"]["reconnecting"]), (True, True))

    def test_the_raw_driver_snapshot_stays_honest_for_the_safety_paths(self):
        self.routes(klippy="ready")
        self.fleet()
        self.prn.url = free_port_url()
        self.prn.refresh()
        self.assertFalse(self.prn.fresh_snapshot()["connected"])       # auto-off / controls / the offline notice see the truth

    def test_connect_is_offered_only_when_the_printer_itself_says_disconnected(self):
        self.assertIn("connectable(s, d.can_connect)", HTML)
        # the three old offers that fired for any not-up state are gone
        self.assertNotIn("(d.can_connect && (s.state==='DISCONNECTED' || !s.connected))", HTML)
        self.assertNotIn("if(!up && d.can_connect) lead.push", HTML)
        self.assertNotIn("${pd.can_connect ?", HTML)
        self.assertIn("Reconnecting", HTML)


@unittest.skipUnless(HAVE, "needs the app requirements")
class TestFilesWhenUnreachable(ServerCase):
    def test_moonraker_timeout_is_503_in_words_not_500(self):
        self.prn.url = free_port_url()
        r = self.client.get(f"/api/printer/{self.NAME}/files")
        self.assertEqual(r.status_code, 503, r.text)
        j = r.json()
        self.assertIn("Can't reach", j["error"])
        self.assertTrue(j["unreachable"])
        self.assertNotIn("Traceback", r.text)

    def test_octoprint_unreachable_is_503_too(self):
        o = OctoPrintPrinter("Octo", free_port_url(), "k")
        server.PRINTERS["octo"] = o
        self.addCleanup(server.PRINTERS.pop, "octo", None)
        r = self.client.get("/api/printer/Octo/files")
        self.assertEqual(r.status_code, 503, r.text)

    def test_a_printer_that_answers_with_a_refusal_is_a_502_not_a_500(self):
        self.routes(klippy="ready")
        self.fake.routes[("GET", "/server/files/list")] = (403, {"error": {"message": "no"}})
        r = self.client.get(f"/api/printer/{self.NAME}/files")
        self.assertEqual(r.status_code, 502, r.text)

    def test_a_healthy_printer_still_lists(self):
        self.routes(klippy="ready", files=("a.gcode",))
        with mock.patch.object(server, "_prewarm"):             # the route starts a background prewarm; keep it out of other tests
            r = self.client.get(f"/api/printer/{self.NAME}/files")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([f["name"] for f in r.json()], ["a.gcode"])

    def test_unknown_printer_is_404(self):
        self.assertEqual(self.client.get("/api/printer/nope/files").status_code, 404)


@unittest.skipUnless(HAVE, "needs the app requirements")
class TestPrewarmPoliteness(ServerCase):
    FILES = ("a.gcode", "b.gcode", "c.gcode")

    def setUp(self):
        super().setUp()
        with server._cache_lock:
            server._cache_save({})
        p = mock.patch.object(server.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.metas = []
        p2 = mock.patch.object(server, "_octo_meta_for", lambda prn, n, s: self.metas.append(n))
        p2.start()
        self.addCleanup(p2.stop)

    def file_reads(self):
        return [p for m, p in self.fake.log if p.startswith("/server/files")]

    def test_idle_cold_printer_is_prewarmed(self):
        self.routes(klippy="ready", files=self.FILES)
        server._prewarm(self.NAME)
        self.assertEqual(self.metas, list(self.FILES))

    def test_prewarm_skips_while_the_bed_is_heating(self):
        self.routes(klippy="ready", files=self.FILES, bed_target=80.0)
        server._prewarm(self.NAME)
        self.assertEqual(self.metas, [])
        self.assertEqual(self.file_reads(), [], "not even the file list may be read while it is heating")

    def test_prewarm_skips_while_the_nozzle_is_heating(self):
        self.routes(klippy="ready", files=self.FILES, nozzle_target=210.0)
        server._prewarm(self.NAME)
        self.assertEqual((self.metas, self.file_reads()), ([], []))

    def test_prewarm_skips_for_two_minutes_after_a_control_action(self):
        self.routes(klippy="ready", files=self.FILES)
        controls.note_control(self.prn)
        server._prewarm(self.NAME)
        self.assertEqual((self.metas, self.file_reads()), ([], []))
        real = time.monotonic
        with mock.patch.object(controls.time, "monotonic", lambda: real() + 125):
            server._prewarm(self.NAME)
        self.assertEqual(self.metas, list(self.FILES))

    def test_a_real_control_call_starts_the_quiet_window(self):
        class Stub:
            name = "Stub"
            kind = "stub"

            def snapshot(self):
                return {"connected": True, "state": "IDLE", "start_pending": False}

            def control_caps(self):
                return {"light": {"supported": True}}

            def set_light(self, on):
                return {"confirmed": True}
        stub = Stub()
        self.assertFalse(controls.recent_control(stub))
        controls.perform(stub, "light_on")
        self.assertTrue(controls.recent_control(stub))
        self.assertIn("control action", server._prewarm_pause_reason(stub))

    def test_prewarm_skips_an_unreachable_printer(self):
        self.prn.url = free_port_url()
        self.prn.refresh()
        server._prewarm(self.NAME)
        self.assertEqual(self.metas, [])

    def test_a_pass_stops_the_moment_a_preheat_starts(self):
        self.routes(klippy="ready", files=self.FILES)

        def meta(prn, n, s):
            self.metas.append(n)
            controls.note_control(prn)                       # someone taps Pre-heat during the first file
        with mock.patch.object(server, "_octo_meta_for", meta):
            server._prewarm(self.NAME)
        self.assertEqual(self.metas, ["a.gcode"])

    def test_a_running_print_keeps_the_existing_behaviour(self):
        # heaters are on by design during a print; the small file reads from the host are still allowed (documented)
        self.routes(klippy="ready", files=self.FILES, job="printing", nozzle_target=210.0, bed_target=60.0)
        self.assertEqual(server._prewarm_pause_reason(self.prn), "")

    def test_bambu_and_prusalink_loops_ask_too(self):
        import inspect
        for fn in (server._prewarm_bambu, server._prewarm_octo, server._prewarm_prusalink):
            self.assertIn("_prewarm_pause_reason", inspect.getsource(fn), fn.__name__)


# ====================================================================== the dashboard's connect flow (node)

import re  # noqa: E402
import subprocess  # noqa: E402


def js_function(name):
    m = re.search(r"(?:async )?function " + name + r"\(", HTML)
    assert m, name
    i = HTML.index("{", m.end())
    depth, j = 0, i
    while True:
        c = HTML[j]
        depth += c == "{"
        depth -= c == "}"
        j += 1
        if depth == 0:
            return HTML[m.start():j]


def node(src):
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(subprocess.run(["which", "node"], capture_output=True).returncode == 0, "needs node")
class TestDashboardConnectFlow(unittest.TestCase):
    PRE = r"""
const els = {};
const $ = id => els[id] || (els[id] = {innerHTML: '', classList: {add(){}, contains(){ return false; }}});
const document = {body: {classList: {contains(){ return false; }}}};
let CUR = 'A', cards = [], calls = [];
const papi = (n, p) => '/api/printer/' + n + (p || '');
const esc = x => String(x);
const errorCard = (t, m) => cards.push([t, m]);
const closeModal = () => {}; const pview = () => {}; const fleet = () => {};
const chipFor = st => ['idle', st];
let apiImpl = null;
const api = async (url, opts) => { calls.push(JSON.parse((opts && opts.body) || '{}')); return apiImpl(); };
"""

    def run_js(self, body):
        src = self.PRE + js_function("connectable") + "\n" + js_function("doConnect") + "\n(async () => {" + body + "})();"
        return node(src)

    def test_first_tap_asks_the_question_and_sends_no_restart(self):
        out = self.run_js(r"""
apiImpl = async () => { const e = new Error('Q'); e.body = {error: 'Klip: Klipper is shutdown. This restarts Klipper (FIRMWARE_RESTART). Only do this if the printer shows an error.', needs_confirm: true}; throw e; };
await doConnect('Klip');
console.log(JSON.stringify({calls, body: els.mbody.innerHTML, cards}));""")
        self.assertEqual(out["calls"], [{"action": "connect", "confirm_restart": False}])
        self.assertIn("Restart Klipper?", out["body"])
        self.assertIn("Only do this if the printer shows an error", out["body"])
        self.assertIn('data-act="connectRestart"', out["body"])
        self.assertEqual(out["cards"], [])

    def test_second_tap_sends_the_confirm(self):
        out = self.run_js(r"""
apiImpl = async () => ({result: 'firmware restart requested for Klip (Klipper was shutdown)'});
const t = setTimeout; global.setTimeout = (f) => f();
await doConnect('Klip', true);
console.log(JSON.stringify({calls}));""")
        self.assertEqual(out["calls"][0], {"action": "connect", "confirm_restart": True})

    def test_nothing_sent_answer_is_shown_at_once(self):
        out = self.run_js(r"""
apiImpl = async () => ({result: 'Klip is already connected (Klipper is ready); nothing was sent.'});
await doConnect('Klip');
console.log(JSON.stringify({body: els.mbody.innerHTML}));""")
        self.assertIn("Nothing to do", out["body"])
        self.assertIn("nothing was sent", out["body"])

    def test_unreachable_is_said_in_words(self):
        out = self.run_js(r"""
apiImpl = async () => { const e = new Error("Can't reach Klip over the network (x). Nothing was sent."); e.body = {unreachable: true, error: e.message}; throw e; };
await doConnect('Klip');
console.log(JSON.stringify({cards}));""")
        self.assertEqual(out["cards"][0][0], "Can't reach the printer")

    def test_connect_is_offered_only_for_a_printer_that_reports_disconnected(self):
        out = self.run_js(r"""
const r = {};
r.disconnected = connectable({connected: true, state: 'DISCONNECTED'}, true);
r.held_blip = connectable({connected: true, state: 'IDLE', reconnecting: true}, true);
r.unreachable = connectable({connected: false, state: 'UNKNOWN'}, true);
r.no_driver_support = connectable({connected: true, state: 'DISCONNECTED'}, false);
console.log(JSON.stringify(r));""")
        self.assertEqual(out, {"disconnected": True, "held_blip": False, "unreachable": False, "no_driver_support": False})


if __name__ == "__main__":
    unittest.main(verbosity=1)
