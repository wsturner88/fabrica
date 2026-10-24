"""Tests for app/moonraker.py — stdlib only, no printers, no external network.

    python3 tests/test_moonraker.py

Everything runs against throwaway fake Moonraker servers on 127.0.0.1 (random ports).
"""
import http.server
import json
import os
import re
import socket
import sys
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from bambu import PrinterError  # noqa: E402
from moonraker import MoonrakerPrinter  # noqa: E402

KEY = "test-api-key"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-frame\xff\xd9"
BIG = b"; thumbnail begin 16x16 100\n" + b"G1 X1 Y1\n" * 5000


class FakeMoonraker:
    def __init__(self, api_key=None, ignore_range=False, webcams="relative"):
        self.api_key, self.ignore_range, self.webcams = api_key, ignore_range, webcams
        self.klippy = "ready"                  # ready | startup | shutdown | error
        self.ps = {"state": "standby", "filename": "", "print_duration": 0.0, "message": "",
                   "info": {"total_layer": None, "current_layer": None}}
        self.progress = 0.0
        self.printer_objects = ["gcode_move", "toolhead", "extruder", "heater_bed", "print_stats", "virtual_sdcard", "exclude_object"]
        self.objects_fail = False              # /printer/objects/list answers 500
        self.files = {"benchy.gcode": BIG, "voron/bracket v2.gcode": b"G1 X2\n" * 300, "readme.md": b"x"}
        self.meta = {"benchy.gcode": {"estimated_time": 3725.4, "filament_total": 4210.5},
                     "voron/bracket v2.gcode": {"estimated_time": 600, "filament_total": 80.0}}
        self.log = []
        self.headers_seen = []
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, code, body=b"", ctype="application/json"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def err(self, code, msg):
                self.reply(code, {"error": {"code": code, "message": msg}})

            def route(self):
                u = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(u.path)
                q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
                with fake.lock:
                    fake.log.append((self.command, path, u.query))
                    fake.headers_seen.append(self.headers.get("X-Api-Key"))
                f, m = fake, self.command
                if path == "/webcam/" and f.webcams != "none":
                    return self.reply(200, JPEG, "image/jpeg")
                if f.api_key and self.headers.get("X-Api-Key") != f.api_key:
                    return self.err(401, "Unauthorized")
                if path == "/server/info":
                    return self.reply(200, {"result": {"klippy_connected": f.klippy == "ready",
                                                       "klippy_state": f.klippy,
                                                       "moonraker_version": "v0.9.3-test",
                                                       "api_version": [1, 5, 0]}})
                if path == "/printer/info":
                    if f.klippy != "ready":
                        return self.err(503, "Klippy Disconnected")
                    return self.reply(200, {"result": {"state": "ready", "software_version": "v0.12.0-test",
                                                       "hostname": "klipper-test"}})
                if path == "/printer/objects/query":
                    if f.klippy != "ready":
                        return self.err(503, "Klippy Disconnected")
                    return self.reply(200, {"result": {"eventtime": 1.0, "status": {
                        "print_stats": f.ps,
                        "virtual_sdcard": {"progress": f.progress, "is_active": f.ps["state"] == "printing"},
                        "display_status": {"progress": 0.0},
                        "extruder": {"temperature": 210.04, "target": 210.0},
                        "heater_bed": {"temperature": 59.96, "target": 60.0},
                        "webhooks": {"state": "ready", "state_message": "Printer is ready"}}}})
                if path == "/printer/objects/list":
                    if f.klippy != "ready":
                        return self.err(503, "Klippy Disconnected")
                    if f.objects_fail:
                        return self.err(500, "internal error")
                    return self.reply(200, {"result": {"objects": f.printer_objects}})
                if path == "/server/files/list":
                    return self.reply(200, {"result": [
                        {"path": p, "modified": 1700000000.5, "size": len(d), "permissions": "rw"}
                        for p, d in f.files.items()]})
                if path == "/server/files/metadata":
                    nm = q["filename"][0]
                    if nm not in f.files:
                        return self.err(404, "File does not exist")
                    return self.reply(200, {"result": {"filename": nm, **f.meta.get(nm, {})}})
                if path.startswith("/server/files/gcodes/"):
                    p = path[len("/server/files/gcodes/"):]
                    if p not in f.files:
                        return self.err(404, "File not found")
                    if m == "DELETE":
                        del f.files[p]
                        return self.reply(200, {"result": {"item": {"path": p}}})
                    data = f.files[p]
                    rng = self.headers.get("Range")
                    if rng and not f.ignore_range:
                        mm = re.fullmatch(r"bytes=(\d+)-(\d+)", rng)
                        return self.reply(206, data[int(mm.group(1)):int(mm.group(2)) + 1],
                                          "application/octet-stream")
                    return self.reply(200, data, "application/octet-stream")
                if path == "/printer/print/start" and m == "POST":
                    nm = q.get("filename", [""])[0]
                    if f.ps["state"] == "printing":
                        return self.err(400, "Print job already in progress")
                    if nm not in f.files:
                        return self.err(404, "File does not exist")
                    return self.reply(200, {"result": "ok"})
                if path in ("/printer/print/pause", "/printer/print/resume", "/printer/print/cancel",
                            "/printer/firmware_restart") and m == "POST":
                    return self.reply(200, {"result": "ok"})
                if path == "/server/webcams/list":
                    if f.webcams == "legacy":
                        return self.err(404, "Not Found")
                    if f.webcams == "none":
                        return self.reply(200, {"result": {"webcams": []}})
                    snap = "/webcam/?action=snapshot" if f.webcams == "relative" else \
                        "http://localhost/webcam/?action=snapshot"
                    return self.reply(200, {"result": {"webcams": [
                        {"name": "off", "enabled": False, "snapshot_url": "/nope?action=snapshot"},
                        {"name": "default", "enabled": True, "service": "mjpegstreamer",
                         "snapshot_url": snap, "stream_url": snap.replace("snapshot", "stream"),
                         "flip_horizontal": True, "flip_vertical": False, "rotation": 0}]}})
                if path == "/server/database/item" and q.get("namespace") == ["webcams"]:
                    return self.reply(200, {"result": {"namespace": "webcams", "value": {
                        "abc": {"enabled": True, "service": "mjpegstreamer", "flipX": False, "flipY": True,
                                "urlStream": "/webcam/?action=stream",
                                "urlSnapshot": "/webcam/?action=snapshot"}}}})
                self.err(404, "Not Found")

            do_GET = do_POST = do_PUT = do_DELETE = route

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def calls(self, method=None):
        return [(m, p) for m, p, _ in self.log if method is None or m == method]


def free_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class Base(unittest.TestCase):
    key = None

    def setUp(self):
        self.fake = FakeMoonraker(api_key=self.key)
        self.p = MoonrakerPrinter("T", self.fake.url, self.key, "Voron 2.4")

    def tearDown(self):
        self.fake.close()


class TestProbe(unittest.TestCase):
    def test_ok(self):
        f = FakeMoonraker()
        try:
            r = MoonrakerPrinter.probe(f.url, None, timeout=3)
            self.assertTrue(r["ok"])
            self.assertIn("v0.12.0-test", r["firmware"])
            self.assertIn("v0.9.3-test", r["firmware"])
            self.assertTrue(r["has_camera"])
            self.assertIsNone(r["error"])
            self.assertIsNone(r["model"])        # Klipper has no model concept
        finally:
            f.close()

    def test_ok_no_camera(self):
        f = FakeMoonraker(webcams="none")
        try:
            r = MoonrakerPrinter.probe(f.url, None, timeout=3)
            self.assertTrue(r["ok"])
            self.assertFalse(r["has_camera"])
        finally:
            f.close()

    def test_ok_with_key(self):
        f = FakeMoonraker(api_key=KEY)
        try:
            self.assertTrue(MoonrakerPrinter.probe(f.url, KEY, timeout=3)["ok"])
            self.assertTrue(MoonrakerPrinter.probe(f.url, {"api_key": KEY}, timeout=3)["ok"])
        finally:
            f.close()

    def test_key_required_or_wrong(self):
        f = FakeMoonraker(api_key=KEY)
        try:
            for creds in (None, "wrong"):
                r = MoonrakerPrinter.probe(f.url, creds, timeout=3)
                self.assertFalse(r["ok"])
                self.assertIn("API key", r["error"])
        finally:
            f.close()

    def test_unreachable(self):
        r = MoonrakerPrinter.probe(free_port_url(), None, timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("Could not connect", r["error"])

    def test_not_moonraker(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                b = b"<html>hello</html>"
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
                self.wfile.write(b)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        try:
            r = MoonrakerPrinter.probe(f"http://127.0.0.1:{srv.server_address[1]}", None, timeout=3)
            self.assertFalse(r["ok"])
            self.assertIn("does not look like Moonraker", r["error"])
        finally:
            srv.shutdown(); srv.server_close()

    def test_klipper_down_is_ok_with_warning(self):
        f = FakeMoonraker()
        f.klippy = "shutdown"
        try:
            r = MoonrakerPrinter.probe(f.url, None, timeout=3)
            self.assertTrue(r["ok"])
            self.assertIn("shutdown", r["warning"])
        finally:
            f.close()

    def test_garbage_url_never_raises(self):
        r = MoonrakerPrinter.probe("not a url", None, timeout=1)
        self.assertFalse(r["ok"])
        self.assertIsInstance(r["error"], str)


class TestSnapshot(Base):
    def snap(self):
        self.p.refresh()
        return self.p.snapshot()

    def test_keys_match_octoprint_shape(self):
        s = self.snap()
        octo_keys = {"printer", "connected", "state", "job", "percent", "remaining_min", "layer",
                     "nozzle_c", "nozzle_target_c", "bed_c", "bed_target_c", "print_error",
                     "sdcard", "age_seconds", "kind"}
        self.assertTrue(octo_keys <= set(s), octo_keys - set(s))
        self.assertEqual(s["kind"], "moonraker")
        self.assertEqual((s["nozzle_c"], s["nozzle_target_c"], s["bed_c"], s["bed_target_c"]),
                         (210.0, 210.0, 60.0, 60.0))

    def test_state_mapping_every_state(self):
        expect = {"standby": "IDLE", "printing": "RUNNING", "paused": "PAUSE", "complete": "FINISH",
                  "cancelled": "FAILED", "error": "FAILED", "brand_new": "UNKNOWN"}
        for raw, want in expect.items():
            self.fake.ps["state"] = raw
            s = self.snap()
            self.assertEqual(s["state"], want, raw)
            self.assertTrue(s["connected"])
            self.assertEqual(s["raw_state"], raw)
        self.fake.ps["state"] = "error"
        self.fake.ps["message"] = "Heater extruder not heating at expected rate"
        s = self.snap()
        self.assertEqual(s["print_error"], 1)
        self.assertIn("Heater", s["error"])

    def test_klippy_not_ready_states(self):
        for k in ("startup", "shutdown", "error"):
            self.fake.klippy = k
            s = self.snap()
            self.assertEqual((s["state"], s["connected"]), ("DISCONNECTED", True), k)
            self.assertEqual(s["print_error"], 1 if k != "startup" else 0, k)
        self.fake.klippy = "ready"
        self.assertEqual(self.snap()["state"], "IDLE")

    def test_printing_fields(self):
        self.fake.ps.update(state="printing", filename="voron/bracket v2.gcode", print_duration=600.0,
                            info={"total_layer": 120, "current_layer": 30})
        self.fake.progress = 0.25
        s = self.snap()
        self.assertEqual((s["job"], s["percent"], s["layer"]), ("voron/bracket v2", 25, "30/120"))
        self.assertEqual(s["remaining_min"], 30)        # 600s for 25% -> 1800s left

    def test_idle_has_no_job_and_finish_is_100(self):
        self.fake.ps.update(state="standby", filename="benchy.gcode")
        self.assertEqual(self.snap()["job"], "")
        self.fake.ps.update(state="complete", filename="benchy.gcode")
        s = self.snap()
        self.assertEqual((s["state"], s["job"], s["percent"]), ("FINISH", "benchy", 100))

    def test_unreachable_is_unknown_not_connected(self):
        p = MoonrakerPrinter("T", free_port_url())
        s = p.snapshot()
        self.assertFalse(s["connected"])
        self.assertEqual(s["state"], "UNKNOWN")
        self.assertIn("Could not reach", s["error"])

    def test_auth_failure_reported(self):
        f = FakeMoonraker(api_key=KEY)
        try:
            s = MoonrakerPrinter("T", f.url, "wrong").snapshot()
            self.assertFalse(s["connected"])
            self.assertIn("API key", s["error"])
        finally:
            f.close()

    def test_cache_within_ttl(self):
        self.snap()
        n = len(self.fake.calls("GET"))
        self.p.snapshot(); self.p.snapshot()
        self.assertEqual(len(self.fake.calls("GET")), n)

    def test_single_status_request(self):
        self.snap()
        qs = [q for m, p, q in self.fake.log if p == "/printer/objects/query"]
        self.assertEqual(len(qs), 1)
        for obj in ("print_stats", "virtual_sdcard", "display_status", "extruder", "heater_bed", "webhooks"):
            self.assertIn(obj, qs[0])


class TestExcludeObject(Base):
    """Is [exclude_object] loaded in printer.cfg? One cached read-only GET /printer/objects/list per connect."""

    def snap(self):
        self.p.refresh()
        return self.p.snapshot()

    def lists(self):
        return [(m, p) for m, p in self.fake.calls() if p == "/printer/objects/list"]

    def test_loaded_is_asked_once_and_reported_without_a_warning(self):
        for _ in range(4):
            s = self.snap()
        self.assertIs(s["exclude_object"], True)
        self.assertIsNone(s["exclude_object_note"])
        self.assertEqual(self.lists(), [("GET", "/printer/objects/list")])            # one question, GET only, cached

    def test_not_set_up_is_flagged_in_words(self):
        self.fake.printer_objects = ["gcode_move", "toolhead", "print_stats"]
        s = self.snap()
        self.assertIs(s["exclude_object"], False)
        self.assertEqual(s["exclude_object_note"], "Exclude objects not set up in printer.cfg")
        self.snap()
        self.assertEqual(len(self.lists()), 1)                                         # not asked again at every poll

    def test_a_failed_look_is_unknown_not_a_warning_and_is_retried_later(self):
        self.fake.objects_fail = True
        s = self.snap()
        self.assertIsNone(s["exclude_object"])
        self.assertIsNone(s["exclude_object_note"])
        self.snap()
        self.assertEqual(len(self.lists()), 1)                                         # not hammered
        self.fake.objects_fail = False
        self.p.EXCLUDE_RETRY_S = 0
        self.assertIs(self.snap()["exclude_object"], True)

    def test_a_garbled_answer_is_unknown(self):
        self.fake.printer_objects = "nonsense"
        self.assertIsNone(self.snap()["exclude_object"])

    def test_a_not_set_up_printer_is_looked_at_again_later(self):
        self.fake.printer_objects = ["toolhead"]
        self.assertIs(self.snap()["exclude_object"], False)
        self.fake.printer_objects = ["toolhead", "exclude_object"]                    # printer.cfg was fixed and Klipper restarted
        self.assertIs(self.snap()["exclude_object"], False)                           # (within the recheck time: not asked yet)
        self.p.EXCLUDE_RECHECK_S = 0
        s = self.snap()
        self.assertIs(s["exclude_object"], True)
        self.assertIsNone(s["exclude_object_note"])

    def test_a_klipper_restart_forgets_the_answer(self):
        self.fake.printer_objects = ["toolhead"]
        self.assertIs(self.snap()["exclude_object"], False)
        self.fake.klippy = "shutdown"
        self.snap()
        self.fake.klippy = "ready"
        self.fake.printer_objects = ["toolhead", "exclude_object"]
        self.assertIs(self.snap()["exclude_object"], True)                             # asked again as soon as Klipper is back
        self.assertEqual(len(self.lists()), 2)

    def test_it_is_not_asked_while_klipper_is_not_ready(self):
        self.fake.klippy = "error"
        s = self.snap()
        self.assertEqual(s["state"], "DISCONNECTED")
        self.assertEqual(self.lists(), [])

    def test_it_is_asked_during_a_print_too_because_it_is_a_read_only_list(self):
        self.fake.ps["state"] = "printing"
        self.fake.printer_objects = ["toolhead"]
        self.assertIs(self.snap()["exclude_object"], False)
        self.assertEqual([m for m, _p in self.lists()], ["GET"])

    def test_the_existing_snapshot_keys_are_all_still_there(self):
        s = self.snap()
        for k in ("printer", "connected", "state", "job", "percent", "remaining_min", "layer", "nozzle_c", "bed_c", "raw_state", "error"):
            self.assertIn(k, s)


class TestFiles(Base):
    def test_listing_shape(self):
        rows = {r["name"]: r for r in self.p.files()}
        self.assertEqual(set(rows), {"benchy.gcode", "voron/bracket v2.gcode"})     # readme.md dropped
        r = rows["benchy.gcode"]
        for k in ("name", "size", "modified", "print_seconds", "print_time", "filament_mm", "origin"):
            self.assertIn(k, r)
        self.assertEqual((r["size"], r["origin"], r["print_seconds"], r["print_time"], r["filament_mm"]),
                         (len(BIG), "local", 3725, "1h02m", 4210.5))
        self.assertAlmostEqual(r["modified"], 1700000000.5)

    def test_metadata_cached_between_listings(self):
        self.p.files()
        n = len([1 for m, p in self.fake.calls() if p == "/server/files/metadata"])
        self.p.files()
        self.assertEqual(len([1 for m, p in self.fake.calls() if p == "/server/files/metadata"]), n)
        self.assertEqual(n, 2)

    def test_files_without_meta(self):
        rows = self.p.files(with_meta=False)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["print_seconds"])
        self.assertFalse([1 for m, p in self.fake.calls() if p == "/server/files/metadata"])

    def test_meta_lookup_budget(self):
        self.p.META_LOOKUPS_PER_LISTING = 1
        self.assertEqual(sum(1 for r in self.p.files() if r["print_seconds"]), 1)
        self.assertEqual(sum(1 for r in self.p.files() if r["print_seconds"]), 2)    # fills in next call

    def test_download_head_with_range(self):
        self.assertEqual(self.p.download_head("benchy.gcode", 64), BIG[:64])

    def test_download_head_when_range_ignored(self):
        f = FakeMoonraker(ignore_range=True)
        try:
            self.assertEqual(MoonrakerPrinter("T", f.url).download_head("benchy.gcode", 64), BIG[:64])
        finally:
            f.close()

    def test_open_file_full_with_spaces(self):
        with self.p.open_file("voron/bracket v2.gcode") as r:
            self.assertEqual(r.read(), b"G1 X2\n" * 300)
        self.assertIn(("GET", "/server/files/gcodes/voron/bracket v2.gcode"), self.fake.calls())

    def test_download_missing_is_printererror(self):
        with self.assertRaises(PrinterError) as cm:
            self.p.open_file("nope.gcode")
        self.assertIn("File not found", str(cm.exception))

    def test_api_key_sent_on_every_request(self):
        f = FakeMoonraker(api_key=KEY)
        try:
            p = MoonrakerPrinter("T", f.url, KEY)
            p.files(); p.download_head("benchy.gcode", 8); p.snapshot()
            self.assertTrue(f.headers_seen and all(h == KEY for h in f.headers_seen))
        finally:
            f.close()


class TestJobControl(Base):
    def test_start(self):
        self.p.start_print("voron/bracket v2.gcode", [])
        m, path, q = next(c for c in self.fake.log if c[1] == "/printer/print/start")
        self.assertEqual(m, "POST")
        self.assertEqual(urllib.parse.parse_qs(q), {"filename": ["voron/bracket v2.gcode"]})

    def test_start_while_printing_surfaces_moonraker_message(self):
        self.fake.ps["state"] = "printing"
        with self.assertRaises(PrinterError) as cm:
            self.p.start_print("benchy.gcode")
        self.assertIn("already in progress", str(cm.exception))

    def test_pause_resume_cancel(self):
        self.p.pause(); self.p.resume(); self.p.stop()
        for verb in ("pause", "resume", "cancel"):
            self.assertIn(("POST", f"/printer/print/{verb}"), self.fake.calls())

    def test_delete(self):
        self.p.sd_delete("benchy.gcode")
        self.assertIn(("DELETE", "/server/files/gcodes/benchy.gcode"), self.fake.calls())
        self.assertNotIn("benchy.gcode", self.fake.files)
        with self.assertRaises(PrinterError):
            self.p.sd_delete("benchy.gcode")

    def test_connect_serial_never_restarts_a_ready_klipper(self):
        # was a blind FIRMWARE_RESTART; see tests/test_safe_connect.py for the full guard
        self.assertIn("nothing was sent", self.p.connect_serial())
        self.assertEqual(self.fake.calls("POST"), [])

    def test_connect_serial_restarts_a_shut_down_klipper_only_after_confirm(self):
        self.fake.klippy = "shutdown"
        from bambu import ConnectNeedsConfirm
        with self.assertRaises(ConnectNeedsConfirm):
            self.p.connect_serial()
        self.assertEqual(self.fake.calls("POST"), [])
        self.assertIn("restart", self.p.connect_serial(confirm_restart=True))
        self.assertIn(("POST", "/printer/firmware_restart"), self.fake.calls())

    def test_unreachable_is_printererror(self):
        p = MoonrakerPrinter("T", free_port_url())
        for fn in (p.stop, p.pause, p.resume, lambda: p.start_print("x.gcode"),
                   lambda: p.sd_delete("x.gcode"), p.files, lambda: p.download_head("x.gcode")):
            with self.assertRaises(PrinterError):
                fn()


class TestCamera(unittest.TestCase):
    def test_relative_webcam_url_resolved_and_streamed(self):
        f = FakeMoonraker(webcams="relative")
        try:
            p = MoonrakerPrinter("T", f.url)
            self.assertTrue(p.has_camera)
            # served by the same fake (port kept because it is not 7125)
            self.assertEqual(p._cam_info["snapshot"], f.url + "/webcam/?action=snapshot")
            self.assertEqual(p._cam_info["stream"], f.url + "/webcam/?action=stream")
            self.assertEqual(p.camera_info(), {"flip_h": True, "flip_v": False, "rotate90": False})
            cam = p.camera()
            got = cam.get_frame(timeout=5)
            self.assertIsNotNone(got)
            self.assertEqual(got[0], JPEG)
            self.assertIs(p.camera(), cam)
            cam.stop()
        finally:
            f.close()

    def test_disconnect_stops_the_poller_and_is_idempotent(self):
        f = FakeMoonraker(webcams="relative")
        try:
            p = MoonrakerPrinter("T", f.url)
            p.disconnect()                                # never connected, no poller yet: harmless
            p.disconnect()
            self.assertTrue(p.has_camera)
            cam = p.camera()
            self.assertIsNotNone(cam.get_frame(timeout=5))
            thread = cam._thread
            self.assertTrue(thread.is_alive())
            p.disconnect()
            self.assertTrue(cam._stopped)
            thread.join(5)
            self.assertFalse(thread.is_alive())           # the reader thread really exits
            p.disconnect()                                # second call: still fine
            self.assertIsNot(p.camera(), cam)             # a later camera() starts a fresh poller
        finally:
            f.close()

    def test_disabled_webcam_skipped_and_none(self):
        f = FakeMoonraker(webcams="none")
        try:
            p = MoonrakerPrinter("T", f.url)
            self.assertFalse(p.has_camera)
            self.assertIsNone(p.camera_info())
        finally:
            f.close()

    def test_localhost_in_webcam_config_points_at_printer_host(self):
        f = FakeMoonraker(webcams="absolute-localhost")
        try:
            p = MoonrakerPrinter("T", f.url)       # printer host is itself local: URL left alone
            self.assertEqual(p._discover_camera()["snapshot"], "http://localhost/webcam/?action=snapshot")
            # remote printer: "localhost" must become the printer's own hostname
            q = MoonrakerPrinter("T", "http://printer.example:7125")
            self.assertEqual(q._abs_url("http://localhost/webcam/?action=snapshot"),
                             "http://printer.example/webcam/?action=snapshot")
        finally:
            f.close()

    def test_legacy_database_webcams_fallback(self):
        f = FakeMoonraker(webcams="legacy")
        try:
            p = MoonrakerPrinter("T", f.url)
            self.assertTrue(p.has_camera)
            self.assertEqual(p.camera_info()["flip_v"], True)
        finally:
            f.close()

    def test_camera_base_url_override(self):
        cams = FakeMoonraker(webcams="relative")      # stands in for the nginx web server
        mrk = FakeMoonraker(webcams="relative")
        try:
            p = MoonrakerPrinter("T", mrk.url, camera_base_url=cams.url)
            self.assertTrue(p.has_camera)
            self.assertTrue(p._cam_info["snapshot"].startswith(cams.url))
            self.assertIn(("GET", "/webcam/"), cams.calls())
            self.assertNotIn(("GET", "/webcam/"), mrk.calls())
        finally:
            cams.close(); mrk.close()

    def test_url_resolution_rules(self):
        r = lambda url, rel, **kw: MoonrakerPrinter("T", url, **kw)._abs_url(rel)
        self.assertEqual(r("http://pi.example:7125", "/webcam/?action=snapshot"),
                         "http://pi.example/webcam/?action=snapshot")       # drop Moonraker's port
        self.assertEqual(r("http://pi.example", "/webcam/?action=snapshot"),
                         "http://pi.example/webcam/?action=snapshot")
        self.assertEqual(r("http://pi.example:8080", "/webcam/?action=snapshot"),
                         "http://pi.example:8080/webcam/?action=snapshot")   # reverse proxy keeps its port
        self.assertEqual(r("http://pi.example:7125", "webcam2/?action=snapshot"),
                         "http://pi.example/webcam2/?action=snapshot")
        self.assertEqual(r("http://pi.example:7125", "http://cam.example:8081/snap"),
                         "http://cam.example:8081/snap")                    # absolute stays as-is
        self.assertEqual(r("http://pi.example:7125", "/w?x=1", camera_base_url="http://pi.example:81"),
                         "http://pi.example:81/w?x=1")
        self.assertEqual(r("http://[fd00::1]:7125", "/webcam/?action=snapshot"),
                         "http://[fd00::1]/webcam/?action=snapshot")

    def test_orientation_mapping(self):
        o = MoonrakerPrinter._orientation
        self.assertEqual(o(False, False, 0), {"flip_h": False, "flip_v": False, "rotate90": False})
        self.assertEqual(o(False, False, 180), {"flip_h": True, "flip_v": True, "rotate90": False})
        self.assertEqual(o(False, False, 270), {"flip_h": False, "flip_v": False, "rotate90": True})
        self.assertEqual(o(False, False, 90), {"flip_h": True, "flip_v": True, "rotate90": True})
        self.assertEqual(o(None, None, "junk"), {"flip_h": False, "flip_v": False, "rotate90": False})


class TestConfig(unittest.TestCase):
    def test_config_fields_shape(self):
        names = [f["name"] for f in MoonrakerPrinter.CONFIG_FIELDS]
        for need in ("name", "url", "api_key"):
            self.assertIn(need, names)
        for f in MoonrakerPrinter.CONFIG_FIELDS:
            self.assertTrue({"name", "label", "type", "required", "help"} <= set(f))
            self.assertIn(f["type"], ("text", "password", "url"))
        req = [f["name"] for f in MoonrakerPrinter.CONFIG_FIELDS if f["required"]]
        self.assertEqual(sorted(req), ["name", "url"])

    def test_from_config(self):
        p = MoonrakerPrinter.from_config(
            {"name": "V", "type": "moonraker", "url": "http://pi.example:7125/", "api_key_env": "V_KEY",
             "model": "Voron 2.4", "camera_base_url": "http://pi.example/"}, env={"V_KEY": "k"})
        self.assertEqual((p.name, p.url, p.api_key, p.model, p.camera_base_url),
                         ("V", "http://pi.example:7125", "k", "Voron 2.4", "http://pi.example"))
        with self.assertRaises(PrinterError):
            MoonrakerPrinter.from_config({"name": "x"}, env={})

    def test_contract_surface(self):
        for attr in ("connect", "refresh", "snapshot", "ams_trays", "files", "download_head",
                     "open_file", "start_print", "stop", "pause", "resume", "sd_delete",
                     "camera_info", "camera", "probe", "from_config", "connect_serial"):
            self.assertTrue(callable(getattr(MoonrakerPrinter, attr)), attr)
        self.assertEqual(MoonrakerPrinter("t", "http://x").ams_trays(), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
