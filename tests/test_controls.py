"""Tests for printer controls (app/controls.py + the four drivers + the server routes / MCP tools).

    python3 tests/test_controls.py

Stdlib + the app's own dependencies. Every printer is a throwaway fake on 127.0.0.1 (random port)
or a mocked MQTT client: no real printer or host is contacted.
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "app"))

# The server module reads its config / data dir at import: point both at an empty temp dir BEFORE importing it.
_TMP = tempfile.mkdtemp(prefix="controls-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "metacache.json")
# These tests call the routes with no API key: key check off here (tests/test_auth.py covers the check).
os.environ["BAMBU_MCP_API_AUTH"] = "off"

import controls  # noqa: E402
from bambu import BambuPrinter, OctoPrintPrinter, PrinterError  # noqa: E402
from moonraker import MoonrakerPrinter  # noqa: E402
from prusalink import PrusaLinkPrinter  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
except Exception as e:      # pragma: no cover - the app's deps are not installed
    server = None
    _SERVER_ERR = e


# ------------------------------------------------------------------ fake HTTP printers

class Fake:
    """Tiny recording HTTP server. handler(fake, method, path, query_dict, body) -> (code, obj|None)."""

    def __init__(self, handler):
        self.log = []                     # (method, path, query_dict, body)
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def serve(self):
                u = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(u.path)
                q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                fake.log.append((self.command, path, q, body))
                code, obj = handler(fake, self.command, path, q, body)
                data = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = do_PUT = serve

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def posts(self):
        return [(p, b) for m, p, q, b in self.log if m == "POST"]

    def scripts(self):
        return [q["script"][0] for m, p, q, b in self.log if p == "/printer/gcode/script"]


def octo_handler(fake, m, path, q, body):
    if path == "/api/printer":
        f = {"printing": fake.printing, "paused": False, "error": False, "ready": not fake.printing}
        return 200, {"state": {"flags": f},
                     "temperature": {"tool0": {"actual": 25.0, "target": 0.0}, "bed": {"actual": 24.0, "target": 0.0}}}
    if path == "/api/job":
        return 200, {"job": {"file": {"name": "x.gcode"}}, "progress": {"completion": 5.0, "printTimeLeft": 600}}
    if path == "/api/settings":
        return 200, {}
    if path == "/api/files":                 # the start gate re-reads the file list (suggest_mapping -> start_print)
        return 200, {"files": [{"name": "x.gcode", "path": "x.gcode", "size": 10}]}
    if m == "POST":
        return 204, None
    return 404, {"error": "nope"}


def make_octo():
    fake = Fake(octo_handler)
    fake.printing = False
    prn = OctoPrintPrinter("MK4", fake.url, "key", "Prusa MK4S")
    return fake, prn


def refresh(prn):
    prn._last_ts = 0.0
    prn._cam_probed = time.time()          # don't let the webcam probe add noise


KLIPPER_BASE = {
    "print_stats": {"state": "standby", "filename": "", "print_duration": 0.0, "message": "",
                    "info": {"total_layer": None, "current_layer": None}},
    "virtual_sdcard": {"progress": 0.0}, "display_status": {"progress": 0.0},
    "extruder": {"temperature": 25.0, "target": 0.0}, "heater_bed": {"temperature": 24.0, "target": 0.0},
    "webhooks": {"state": "ready", "state_message": "Printer is ready"},
}


def moon_handler(fake, m, path, q, body):
    if path == "/printer/objects/list":
        return 200, {"result": {"objects": fake.objects}}
    if path == "/printer/objects/query":
        names = list(q)
        if any(n.split(" ")[0] in ("output_pin", "led", "neopixel", "dotstar") for n in names):
            return 200, {"result": {"status": {n: fake.light_status[n] for n in names if n in fake.light_status}}}
        base = json.loads(json.dumps(KLIPPER_BASE))
        base["print_stats"]["state"] = fake.pstate
        return 200, {"result": {"status": base}}
    if path == "/printer/gcode/script" and m == "POST":
        if fake.macro_timeout and q["script"][0].startswith("UNLOAD"):
            time.sleep(1.5)
        return 200, {"result": "ok"}
    if path == "/server/webcams/list":
        return 200, {"result": {"webcams": []}}
    return 404, {"error": {"code": 404, "message": "nope"}}


def make_moon(objects, light_status=None, pstate="standby"):
    fake = Fake(moon_handler)
    fake.objects, fake.light_status, fake.pstate, fake.macro_timeout = objects, light_status or {}, pstate, False
    prn = MoonrakerPrinter("Voron", fake.url)
    prn._cam_probed = time.time()
    return fake, prn


def make_bambu(gcode_state="IDLE", ams=True, lights=None):
    prn = BambuPrinter("P1S", "127.0.0.1", "SER123", "code", "Bambu Lab P1S")
    prn.CONFIRM_WAIT_S = 0.2          # the mock printer never echoes a change; do not wait 10 s for it
    p = {"gcode_state": gcode_state, "nozzle_temper": 31.5, "nozzle_target_temper": 0, "bed_temper": 30,
         "bed_target_temper": 0}
    if ams:
        p["ams"] = {"tray_now": "255", "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF", "nozzle_temp_min": "190"},
            {"id": "1", "tray_type": "PETG", "tray_color": "00FF00FF", "nozzle_temp_min": "230"},
            {"id": "2"}, {"id": "3"}]}]}
    p["lights_report"] = lights if lights is not None else [{"node": "chamber_light", "mode": "off"}]
    prn.state = {"print": p}
    prn.last_msg = time.time()
    prn._connected = True
    prn._client = mock.Mock()
    # a publish that really left (request() now checks paho's return code and the "sent" flag)
    prn._client.publish.return_value = mock.Mock(rc=0, is_published=mock.Mock(return_value=True))
    return prn


def sent(prn):
    """Decoded MQTT payloads published by a Bambu driver, in order."""
    out = []
    for c in prn._client.publish.call_args_list:
        assert c.args[0] == "device/SER123/request"
        out.append(json.loads(c.args[1]))
    return out


# ------------------------------------------------------------------ controls module

class TestLimitsAndPresets(unittest.TestCase):
    def setUp(self):
        controls.configure(None)

    def test_defaults(self):
        # pre-heat by preset heats the BED only: no preset carries a nozzle temperature
        self.assertEqual(controls.presets(), {"PLA": {"nozzle": None, "bed": 60}, "PETG": {"nozzle": None, "bed": 80},
                                              "ABS": {"nozzle": None, "bed": 100}})

    def test_resolve(self):
        self.assertEqual(controls.resolve_temps("petg"), (None, 80, "PETG"))
        self.assertEqual(controls.resolve_temps("PLA", nozzle=200), (200, 60, "PLA"))      # an explicit nozzle (API / agents) still works
        self.assertEqual(controls.resolve_temps(None, nozzle=180), (180, None, None))
        self.assertEqual(controls.resolve_temps(None, bed=55.4), (None, 55, None))

    def test_rejects(self):
        bad = [dict(), dict(preset="NYLON"), dict(nozzle=301), dict(bed=121), dict(nozzle=-1),
               dict(nozzle="hot"), dict(bed=True), dict(nozzle=float("nan")), dict(nozzle=0, bed=0),
               dict(preset="PLA", nozzle=400)]
        for kw in bad:
            with self.assertRaises(controls.ControlInvalid, msg=str(kw)):
                controls.resolve_temps(**kw)
        self.assertEqual(controls.resolve_temps(nozzle=300, bed=120), (300, 120, None))   # limits are inclusive

    def test_config_presets(self):
        logs = []
        controls.configure({"nylon": {"nozzle": 270, "bed": 90}, "pla": [205, 55],
                            "hot": {"bed": 999}, "junk": 5}, log=logs.append)
        p = controls.presets()
        self.assertEqual(p["NYLON"], {"nozzle": None, "bed": 90})     # an older config's nozzle value is read and dropped: bed only
        self.assertEqual(p["PLA"], {"nozzle": None, "bed": 55})       # overrides the default
        self.assertNotIn("HOT", p)                                    # out of range -> skipped, logged
        self.assertNotIn("JUNK", p)
        self.assertEqual(len(logs), 2)
        controls.configure(None)

    def test_a_preset_pre_heat_never_sends_a_nozzle_command_on_any_driver_and_prusalink_sends_nothing(self):
        """Pre-heat by preset is bed only: Bambu, OctoPrint and Klipper are covered by their own tests above; PrusaLink has no heater control at all."""
        class NoHeat:
            name = "pl"
            def __init__(self): self.sent = []
            def snapshot(self): return {"connected": True, "state": "IDLE"}
            def control_caps(self): return controls.all_unsupported("no heater endpoint")
            def set_temps(self, nozzle=None, bed=None): self.sent.append((nozzle, bed))
        p = NoHeat()
        for preset in ("PLA", "PETG", "ABS"):
            with self.assertRaises(controls.ControlUnsupported):
                controls.perform(p, "preheat", preset=preset)
        self.assertEqual(p.sent, [])
        for preset in ("PLA", "PETG", "ABS"):                                   # and every preset resolves to a bed temperature only
            n, b, name = controls.resolve_temps(preset)
            self.assertIsNone(n, preset)
            self.assertGreater(b, 0, preset)

    def test_timeout_env(self):
        for val, want in (("", 900), ("5", 300), ("0", 900), ("-3", 900), ("abc", 900), ("0.5", 30)):
            with mock.patch.dict(os.environ, {"PREHEAT_TIMEOUT_MIN": val}):
                self.assertEqual(controls.timeout_seconds(), want, val)


# ------------------------------------------------------------------ Bambu

class TestBambu(unittest.TestCase):
    def test_light_payload_and_state(self):
        prn = make_bambu(lights=[{"node": "chamber_light", "mode": "on"}])
        self.assertEqual(prn.control_caps()["light"]["state"], "on")
        controls.perform(prn, "light_off")
        m = sent(prn)[0]["system"]
        self.assertEqual((m["command"], m["led_node"], m["led_mode"]), ("ledctrl", "chamber_light", "off"))

    def test_work_light_fallback(self):
        prn = make_bambu(lights=[{"node": "work_light", "mode": "off"}])
        controls.perform(prn, "light_on")
        self.assertEqual(sent(prn)[0]["system"]["led_node"], "work_light")
        self.assertEqual(prn.control_caps()["light"]["state"], "off")

    def test_light_allowed_mid_print(self):
        prn = make_bambu("RUNNING")
        controls.perform(prn, "light_on")
        self.assertEqual(len(sent(prn)), 1)

    def test_preheat_and_cooldown_gcode(self):
        prn = make_bambu()
        r = controls.perform(prn, "preheat", preset="PETG")
        self.addCleanup(controls.cancel_auto_off, prn)
        m = sent(prn)[0]["print"]
        self.assertEqual(m["command"], "gcode_line")
        self.assertEqual(m["param"], "M140 S80\n")                    # the bed only: no M104
        self.assertEqual((r["nozzle"], r["bed"]), (None, 80))
        controls.perform(prn, "cooldown")
        self.assertEqual(sent(prn)[1]["print"]["param"], "M104 S0\nM140 S0\n")
        self.assertIsNone(controls.auto_off_remaining(prn))

    def test_preheat_bed_only(self):
        prn = make_bambu()
        controls.perform(prn, "preheat", bed=70)
        self.addCleanup(controls.cancel_auto_off, prn)
        self.assertEqual(sent(prn)[0]["print"]["param"], "M140 S70\n")

    def test_unload_ams(self):
        prn = make_bambu()
        controls.perform(prn, "unload")
        m = sent(prn)[0]["print"]
        self.assertEqual((m["command"], m["target"]), ("ams_change_filament", 255))
        self.assertEqual((m["curr_temp"], m["tar_temp"]), (31, 220))

    def test_load_tray(self):
        prn = make_bambu()
        with self.assertRaises(controls.ControlInvalid):         # two slots hold filament: must choose
            controls.perform(prn, "load")
        controls.perform(prn, "load", tray=2)
        m = sent(prn)[0]["print"]
        self.assertEqual((m["command"], m["target"], m["tar_temp"]), ("ams_change_filament", 1, 230))
        with self.assertRaises(controls.ControlInvalid):         # slot 3 is empty
            controls.perform(prn, "load", tray=3)

    def test_unload_without_ams_unsupported(self):
        prn = make_bambu(ams=False)
        for act in ("unload", "load"):
            with self.assertRaises(controls.ControlUnsupported) as cm:
                controls.perform(prn, act)
            self.assertIn("AMS", str(cm.exception))
        self.assertEqual(prn._client.publish.call_count, 0)
        d = controls.describe(prn)
        self.assertFalse(d["unload"]["supported"])
        self.assertIn("reason", d["unload"])

    def test_busy_refused_sends_nothing(self):
        for st in ("RUNNING", "PREPARE", "PAUSE"):
            prn = make_bambu(st)
            for act, kw in (("preheat", {"preset": "PLA"}), ("cooldown", {}), ("unload", {}), ("load", {"tray": 1})):
                with self.assertRaises(controls.ControlRefused, msg=f"{st} {act}"):
                    controls.perform(prn, act, **kw)
            self.assertEqual(prn._client.publish.call_count, 0)
            self.assertFalse(controls.describe(prn)["preheat"]["allowed_now"])

    def test_idle_finish_failed_allowed(self):
        for st in ("IDLE", "FINISH", "FAILED"):
            prn = make_bambu(st)
            controls.perform(prn, "cooldown")
            self.assertEqual(prn._client.publish.call_count, 1)

    def test_disconnected_refused(self):
        prn = make_bambu()
        prn._connected = False
        with self.assertRaises(controls.ControlRefused):
            controls.perform(prn, "cooldown")

    def test_controls_method(self):
        d = make_bambu().controls()
        self.assertEqual(d["actions"], ["light_on", "light_off", "preheat", "cooldown", "heater_off", "unload", "load", "home", "move", "extrude"])
        self.assertEqual(d["preheat"]["presets"]["PLA"], {"nozzle": None, "bed": 60})
        self.assertFalse(d["preheat"]["verified"])


# ------------------------------------------------------------------ OctoPrint

class TestOctoPrint(unittest.TestCase):
    def setUp(self):
        self.fake, self.prn = make_octo()
        self.addCleanup(self.fake.close)
        self.addCleanup(controls.cancel_auto_off, self.prn)

    def test_preheat_endpoints(self):
        r = controls.perform(self.prn, "preheat", preset="PETG")
        self.assertEqual(self.fake.posts(), [("/api/printer/bed", {"command": "target", "target": 80})])     # no tool0 command
        self.assertEqual((r["nozzle"], r["bed"], r["preset"]), (None, 80, "PETG"))

    def test_cooldown(self):
        controls.perform(self.prn, "cooldown")
        self.assertEqual(self.fake.posts(), [
            ("/api/printer/tool", {"command": "target", "targets": {"tool0": 0}}),
            ("/api/printer/bed", {"command": "target", "target": 0})])

    def test_unload_load_gcode(self):
        controls.perform(self.prn, "unload")
        controls.perform(self.prn, "load")
        # the Hub says how hot (PLA 210 by default) and heats first, in one queued command list
        self.assertEqual(self.fake.posts(), [("/api/printer/command", {"commands": ["M109 S210", "M702"]}),
                                             ("/api/printer/command", {"commands": ["M109 S210", "M701"]})])

    def test_light_unsupported(self):
        with self.assertRaises(controls.ControlUnsupported) as cm:
            controls.perform(self.prn, "light_on")
        self.assertIn("no standard light API", str(cm.exception))
        self.assertEqual(self.fake.posts(), [])
        d = controls.describe(self.prn)
        self.assertFalse(d["light"]["supported"])
        self.assertNotIn("light_on", d["actions"])

    def test_busy_refused(self):
        self.fake.printing = True
        refresh(self.prn)
        for act, kw in (("preheat", {"preset": "PLA"}), ("cooldown", {}), ("unload", {}), ("load", {})):
            with self.assertRaises(controls.ControlRefused):
                controls.perform(self.prn, act, **kw)
        self.assertEqual(self.fake.posts(), [])

    def test_invalid_temps_send_nothing(self):
        with self.assertRaises(controls.ControlInvalid):
            controls.perform(self.prn, "preheat", nozzle=350)
        self.assertEqual(self.fake.posts(), [])

    def test_printer_error(self):
        def refuse(fake, m, path, q, body):
            return (409, {"error": "Printer is not operational"}) if m == "POST" else octo_handler(fake, m, path, q, body)
        fake = Fake(refuse)
        self.addCleanup(fake.close)
        fake.printing = False
        prn = OctoPrintPrinter("MK4", fake.url, "key")
        with self.assertRaises(PrinterError):
            controls.perform(prn, "cooldown")


# ------------------------------------------------------------------ Moonraker

CORE = ["extruder", "heater_bed", "print_stats", "virtual_sdcard", "webhooks", "display_status"]


class TestMoonraker(unittest.TestCase):
    def mk(self, objects, light_status=None, **kw):
        fake, prn = make_moon(CORE + objects, light_status, **kw)
        self.addCleanup(fake.close)
        self.addCleanup(controls.cancel_auto_off, prn)
        return fake, prn

    def test_output_pin_light(self):
        fake, prn = self.mk(["output_pin caselight", "output_pin beeper", "fan_generic exhaust"],
                            {"output_pin caselight": {"value": 1.0}})
        caps = prn.control_caps()
        self.assertTrue(caps["light"]["supported"])
        self.assertEqual(caps["light"]["state"], "on")
        self.assertIn("output_pin caselight", caps["light"]["note"])
        self.assertNotIn("beeper", caps["light"]["note"])
        controls.perform(prn, "light_off")
        controls.perform(prn, "light_on")
        self.assertEqual(fake.scripts(), ["SET_PIN PIN=caselight VALUE=0", "SET_PIN PIN=caselight VALUE=1"])

    def test_led_light(self):
        fake, prn = self.mk(["neopixel chamber_led", "led status_led_x"] + ["gcode_macro CLEAN"],
                            {"neopixel chamber_led": {"color_data": [[0, 0, 0, 0]]},
                             "led status_led_x": {"color_data": [[0, 0, 0, 0]]}})
        self.assertEqual(prn.control_caps()["light"]["state"], "off")
        controls.perform(prn, "light_on")
        self.assertEqual(fake.scripts(), [
            "SET_LED LED=chamber_led RED=1 GREEN=1 BLUE=1 WHITE=1\nSET_LED LED=status_led_x RED=1 GREEN=1 BLUE=1 WHITE=1"])
        fake.light_status["neopixel chamber_led"] = {"color_data": [[1, 1, 1, 1]]}
        self.assertEqual(prn.control_caps()["light"]["state"], "on")

    def test_no_light(self):
        fake, prn = self.mk(["output_pin beeper", "fan_generic exhaust", "neopixel status"])
        d = controls.describe(prn)
        self.assertFalse(d["light"]["supported"])
        self.assertIn("No light found", d["light"]["reason"])
        with self.assertRaises(controls.ControlUnsupported):
            controls.perform(prn, "light_on")
        self.assertEqual(fake.scripts(), [])

    def test_lamp_name_match_case_insensitive(self):
        fake, prn = self.mk(["output_pin Work_LAMP"], {"output_pin Work_LAMP": {"value": 0.0}})
        controls.perform(prn, "light_on")
        self.assertEqual(fake.scripts(), ["SET_PIN PIN=Work_LAMP VALUE=1"])

    def test_heat_scripts(self):
        fake, prn = self.mk([])
        controls.perform(prn, "preheat", preset="ABS")
        controls.perform(prn, "cooldown")
        self.assertEqual(fake.scripts(), ["M140 S100", "M104 S0\nM140 S0"])

    def test_heat_without_bed_skips_bed(self):
        fake, prn = make_moon(["extruder", "print_stats", "virtual_sdcard", "webhooks", "display_status"])
        self.addCleanup(fake.close)
        self.addCleanup(controls.cancel_auto_off, prn)
        r = controls.perform(prn, "preheat", preset="PLA")
        self.assertEqual(fake.scripts(), [])                          # bed-only pre-heat and no bed to heat: nothing is sent
        self.assertIsNone(r["bed"])

    def test_macro_gating(self):
        fake, prn = self.mk([])
        d = controls.describe(prn)
        self.assertTrue(d["unload"]["supported"])                    # no macro: the plain G-code sequence (lengths from Edit printer)
        self.assertIn("no unload macro", d["unload"]["note"])
        self.assertTrue(d["load"]["supported"])
        controls.perform(prn, "unload")
        self.assertEqual(fake.scripts(), ["M109 S210\nSAVE_GCODE_STATE NAME=fabrica\nM83\nG1 E10 F300\nG1 E-100 F1500\nRESTORE_GCODE_STATE NAME=fabrica"])

        fake2, prn2 = self.mk(["gcode_macro UNLOAD_FILAMENT"])
        d = controls.describe(prn2)
        self.assertTrue(d["unload"]["supported"])
        self.assertIn("UNLOAD_FILAMENT macro", d["unload"]["note"])
        self.assertIn("no load macro", d["load"]["note"])  # only UNLOAD exists: load falls back to plain G-code
        controls.perform(prn2, "unload")
        self.assertEqual(fake2.scripts(), ["M109 S210\nUNLOAD_FILAMENT TEMP=210"])

        fake3, prn3 = self.mk(["gcode_macro load_filament"])    # case-insensitive match
        controls.perform(prn3, "load")
        self.assertEqual(fake3.scripts(), ["M109 S210\nload_filament TEMP=210"])

    def test_long_macro_timeout_counts_as_started(self):
        fake, prn = self.mk(["gcode_macro UNLOAD_FILAMENT"])
        fake.macro_timeout = True
        orig = prn._script

        def short(script, what, timeout=15):
            return orig(script, what, timeout=0.3)
        prn._script = short
        controls.perform(prn, "unload")          # the request times out client-side but the macro runs

    def test_busy_refused(self):
        fake, prn = self.mk(["output_pin caselight"], {"output_pin caselight": {"value": 0}}, pstate="printing")
        for act, kw in (("preheat", {"preset": "PLA"}), ("cooldown", {}), ("unload", {})):
            with self.assertRaises((controls.ControlRefused, controls.ControlUnsupported)):
                controls.perform(prn, act, **kw)
        self.assertEqual(fake.scripts(), [])
        controls.perform(prn, "light_on")        # lights are never gated
        self.assertEqual(len(fake.scripts()), 1)

    def test_unreachable_all_unsupported(self):
        prn = MoonrakerPrinter("Gone", "http://127.0.0.1:9")
        d = controls.describe(prn)
        self.assertEqual(d["actions"], [])
        self.assertIn("not reachable", d["light"]["reason"])


# ------------------------------------------------------------------ PrusaLink

class TestPrusaLink(unittest.TestCase):
    def test_all_unsupported_and_nothing_sent(self):
        def h(fake, m, path, q, body):
            if path == "/api/v1/status":
                return 200, {"printer": {"state": "IDLE"}}
            return 404, {}
        fake = Fake(h)
        self.addCleanup(fake.close)
        prn = PrusaLinkPrinter("MK4", fake.url, {"api_key": "k"})
        prn._cam_probed = time.time()
        d = controls.describe(prn)
        self.assertEqual(d["actions"], [])
        for k in ("light", "preheat", "cooldown", "unload", "load"):
            self.assertFalse(d[k]["supported"], k)
            self.assertIn("no light, temperature, G-code or filament endpoint", d[k]["reason"])
        for act, kw in (("light_on", {}), ("preheat", {"preset": "PLA"}), ("cooldown", {}),
                        ("unload", {}), ("load", {})):
            with self.assertRaises(controls.ControlUnsupported):
                controls.perform(prn, act, **kw)
        self.assertEqual([x for x in fake.log if x[0] != "GET"], [])     # no mutating request ever left


# ------------------------------------------------------------------ pre-heat auto-off

class TestAutoOff(unittest.TestCase):
    def setUp(self):
        self.fake, self.prn = make_octo()
        self.addCleanup(self.fake.close)
        self.addCleanup(controls.cancel_auto_off, self.prn)
        p = mock.patch.dict(os.environ, {"PREHEAT_TIMEOUT_MIN": "0.025"})      # 1.5 s
        p.start()
        self.addCleanup(p.stop)

    def targets(self):
        return [b for p, b in self.fake.posts() if p.startswith("/api/printer/") and p != "/api/printer/command"]

    def wait_for(self, n, secs=5):
        end = time.time() + secs
        while time.time() < end and len(self.targets()) < n:
            time.sleep(0.05)

    def test_fires_cooldown(self):
        r = controls.perform(self.prn, "preheat", preset="PLA")
        self.assertEqual(r["auto_off_in_s"], 2)                       # 1.5 s rounds to 2
        self.assertIsNotNone(controls.describe(self.prn)["preheat"]["auto_off_in_s"])
        self.wait_for(3)
        self.assertEqual(self.targets(), [{"command": "target", "target": 60},                       # pre-heat: the bed only
                                          {"command": "target", "targets": {"tool0": 0}},            # auto-off: both heaters off
                                          {"command": "target", "target": 0}])
        self.assertIsNone(controls.auto_off_remaining(self.prn))
        self.assertIn("cooled down", controls.describe(self.prn)["preheat"]["last_auto_off"]["result"])

    def test_print_start_announcement_does_not_cancel(self):
        """note_print_start() used to drop the timer BEFORE the start was confirmed; a refused or silently
        ignored start then left the heaters on for good. It is now a no-op: only an OBSERVED print cancels."""
        controls.perform(self.prn, "preheat", preset="PLA")
        self.assertFalse(controls.note_print_start(self.prn))
        self.assertIsNotNone(controls.auto_off_remaining(self.prn))
        self.wait_for(3)                                              # the printer never started: it cools
        self.assertEqual(self.targets()[1:], [{"command": "target", "targets": {"tool0": 0}},
                                              {"command": "target", "target": 0}])

    def test_cancelled_when_a_print_is_observed(self):
        controls.OBSERVE_EVERY_S, old = 0.2, controls.OBSERVE_EVERY_S
        self.addCleanup(setattr, controls, "OBSERVE_EVERY_S", old)
        controls.perform(self.prn, "preheat", preset="PLA", )
        controls.cancel_auto_off(self.prn)
        controls.schedule_auto_off(self.prn, seconds=30)
        self.fake.printing = True
        self.prn._last_ts = 0
        end = time.time() + 3
        while time.time() < end and controls.auto_off_remaining(self.prn) is not None:
            time.sleep(0.05)
        self.assertIsNone(controls.auto_off_remaining(self.prn))      # seen RUNNING -> the print owns the heaters
        self.assertEqual(len(self.targets()), 1)

    def test_cancelled_by_cooldown(self):
        controls.perform(self.prn, "preheat", preset="PLA")
        controls.perform(self.prn, "cooldown")
        n = len(self.targets())
        time.sleep(2.2)
        self.assertEqual(len(self.targets()), n)                      # no second cool-down

    def test_repreheat_restarts_timer(self):
        controls.perform(self.prn, "preheat", preset="PLA")
        time.sleep(1.0)
        controls.perform(self.prn, "preheat", preset="PETG")
        time.sleep(1.0)                                               # 2 s after the first, 1 s after the second
        self.assertEqual(len(self.targets()), 2)                      # two bed commands, nothing else yet
        self.wait_for(4)
        self.assertEqual(len(self.targets()), 4)

    def test_skips_if_print_started_elsewhere(self):
        controls.perform(self.prn, "preheat", preset="PLA")
        self.fake.printing = True                                     # started from the slicer / printer screen
        refresh(self.prn)
        time.sleep(2.2)
        self.assertEqual(len(self.targets()), 1)
        self.assertIn("skipped", controls._last_auto_off["mk4"]["result"])

    def test_describe_drops_timer_when_printing(self):
        controls.perform(self.prn, "preheat", preset="PLA")
        self.fake.printing = True
        refresh(self.prn)
        d = controls.describe(self.prn)
        self.assertIsNone(d["preheat"]["auto_off_in_s"])
        self.assertIsNone(controls.auto_off_remaining(self.prn))

    def test_resolver_uses_live_driver(self):
        """An edited printer is a new driver object: the cool-down must go through the live one."""
        fake2, prn2 = make_octo()
        self.addCleanup(fake2.close)
        controls.configure(None, resolver=lambda key: prn2 if key == "mk4" else None)
        self.addCleanup(controls.configure, None, lambda key: None)
        # the edit kept the same machine (only e.g. the API key changed): same identity, new driver object
        patcher = mock.patch.object(controls, "printer_identity", lambda p: "the-mk4")
        patcher.start()
        self.addCleanup(patcher.stop)
        controls.perform(self.prn, "preheat", preset="PLA")
        time.sleep(2.2)
        self.assertEqual(len(self.targets()), 1)                      # old driver untouched: only its own bed pre-heat
        self.assertEqual(len([1 for p, b in fake2.posts() if p.startswith("/api/printer/")]), 2)   # the live driver got the cool-down (tool + bed)


# ------------------------------------------------------------------ server: routes + MCP tools

@unittest.skipIf(server is None, "server deps not installed")
class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.mcp.streamable_http_app())
        cls.fake, cls.octo = make_octo()
        cls.octo.name = "MK4"
        cls.mfake, cls.moon = make_moon(CORE + ["output_pin caselight", "gcode_macro UNLOAD_FILAMENT"],
                                        {"output_pin caselight": {"value": 0.0}})
        cls.bam = make_bambu()
        cls.pl_fake = Fake(lambda f, m, p, q, b: (200, {"printer": {"state": "IDLE"}}))
        cls.pl = PrusaLinkPrinter("Prusa", cls.pl_fake.url, {"api_key": "k"})
        cls.pl._cam_probed = time.time()
        cls.octo._cam_probed = time.time()
        for p in (cls.octo, cls.moon, cls.bam, cls.pl):
            server.PRINTERS[p.name.lower()] = p

    @classmethod
    def tearDownClass(cls):
        for p in (cls.octo, cls.moon, cls.bam, cls.pl):
            controls.cancel_auto_off(p)
            server.PRINTERS.pop(p.name.lower(), None)
        for f in (cls.fake, cls.mfake, cls.pl_fake):
            f.close()

    def setUp(self):
        self.fake.printing = False
        refresh(self.octo)
        self.fake.log.clear()

    def post(self, name, **body):
        return self.client.post(f"/api/printer/{name}/control", json=body)

    def test_tools_registered(self):
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        for n in ("printer_controls", "set_light", "chamber_light", "preheat", "cooldown",
                  "unload_filament", "load_filament"):
            self.assertIn(n, names)
        for t in server.mcp._tool_manager.list_tools():
            if t.name in ("printer_controls", "set_light", "preheat", "cooldown", "unload_filament", "load_filament"):
                self.assertGreater(len(t.description), 60, t.name)

    def test_controls_route(self):
        r = self.client.get("/api/printer/mk4/controls")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["printer"], "MK4")
        self.assertFalse(j["light"]["supported"])
        self.assertTrue(j["preheat"]["supported"])
        self.assertIn("PETG", j["preheat"]["presets"])
        self.assertEqual(self.client.get("/api/printer/nope/controls").status_code, 404)

    def test_control_route_status_codes(self):
        r = self.post("mk4", action="preheat", preset="PLA")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertGreater(r.json()["auto_off_in_s"], 0)
        self.assertEqual(self.post("mk4", action="cooldown").status_code, 200)
        # unsupported -> 422 + reason
        r = self.post("mk4", action="light_on")
        self.assertEqual(r.status_code, 422)
        self.assertIn("no standard light API", r.json()["reason"])
        self.assertTrue(r.json()["error"].startswith("UNSUPPORTED"))
        r = self.post("prusa", action="preheat", preset="PLA")
        self.assertEqual(r.status_code, 422)
        # invalid -> 400
        self.assertEqual(self.post("mk4", action="preheat", nozzle=999).status_code, 400)
        self.assertEqual(self.post("mk4", action="preheat").status_code, 400)
        self.assertEqual(self.post("mk4", action="explode").status_code, 400)
        self.assertEqual(self.client.post("/api/printer/mk4/control", content=b"[1]", headers={"Content-Type": "application/json"}).status_code, 400)
        # unknown printer -> 404
        self.assertEqual(self.post("ghost", action="cooldown").status_code, 404)
        # busy -> 409
        self.fake.printing = True
        refresh(self.octo)
        r = self.post("mk4", action="preheat", preset="PLA")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["error"].startswith("REFUSED"))
        self.assertEqual(self.post("mk4", action="unload").status_code, 409)

    def test_moonraker_and_bambu_through_route(self):
        self.mfake.log.clear()
        self.assertEqual(self.post("voron", action="light_on").status_code, 200)
        self.assertEqual(self.mfake.scripts(), ["SET_PIN PIN=caselight VALUE=1"])
        self.assertEqual(self.post("voron", action="load").status_code, 200)     # no LOAD_FILAMENT macro: the plain sequence
        self.assertEqual(self.post("voron", action="unload").status_code, 200)
        self.bam._client.publish.reset_mock()
        self.assertEqual(self.post("p1s", action="light_on").status_code, 200)
        self.assertEqual(self.bam._client.publish.call_count, 1)

    def test_printer_detail_carries_controls_summary(self):
        j = self.client.get("/api/printer/voron").json()
        c = j["controls"]
        self.assertIn("light_on", c["actions"])
        self.assertIn("preheat", c["allowed_now"])
        self.assertIn("unload", c["actions"])
        self.assertIn("load", c["actions"])
        self.assertEqual(c["light"], "off")
        self.assertIn("PLA", c["presets"])
        j = self.client.get("/api/printer/prusa").json()
        self.assertEqual(j["controls"]["actions"], [])
        j = self.client.get("/api/printer/mk4").json()
        self.assertEqual(j["controls"]["actions"], ["preheat", "cooldown", "heater_off", "unload", "load", "home", "move", "extrude"])
        self.fake.printing = True
        refresh(self.octo)
        j = self.client.get("/api/printer/mk4").json()
        self.assertEqual(j["controls"]["allowed_now"], [])            # busy: nothing but lights, and it has none

    def test_action_route_light_still_works(self):
        r = self.client.post("/api/printer/p1s/action", json={"action": "light_off"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/printer/mk4/action", json={"action": "light_on"})
        self.assertEqual(r.status_code, 422)

    def test_mcp_tool_functions(self):
        self.assertIn("pre-heat sent", server.preheat("mk4", preset="ABS"))
        posts = {p: b for p, b in self.fake.posts()}
        self.assertEqual(posts["/api/printer/bed"]["target"], 100)
        self.assertNotIn("/api/printer/tool", posts)                  # a pre-heat preset never touches the nozzle
        self.assertIn("cool-down sent", server.cooldown("mk4"))
        self.assertTrue(server.set_light("mk4", True).startswith("UNSUPPORTED"))
        self.assertTrue(server.chamber_light("mk4", True).startswith("UNSUPPORTED"))
        self.assertIn("light on sent", server.chamber_light("p1s", True))
        self.assertTrue(server.preheat("mk4", preset="NYLON").startswith("INVALID"))
        self.assertIn("sent", server.unload_filament("mk4"))
        self.fake.printing = True
        refresh(self.octo)
        self.assertTrue(server.cooldown("mk4").startswith("REFUSED"))
        self.assertTrue(server.load_filament("mk4").startswith("REFUSED"))
        with self.assertRaises(ValueError):
            server.cooldown("ghost")
        j = json.loads(server.printer_controls("voron"))
        self.assertTrue(j["light"]["supported"])

    def test_start_print_keeps_auto_off_until_print_observed(self):
        self.assertEqual(self.post("mk4", action="preheat", preset="PLA").status_code, 200)
        self.assertIsNotNone(controls.auto_off_remaining("mk4"))
        confirm = json.loads(server.suggest_mapping("mk4", "x.gcode"))["confirm_string"]     # single-use ticket
        with mock.patch("server.time.sleep"):
            out = server.start_print("mk4", "x.gcode", [], confirm, bed_clear_confirmed_by_human=True)
        self.assertNotIn("REFUSED", out)
        # a start only COMMANDS a print: the auto-off stays until the printer is seen printing
        self.assertIsNotNone(controls.auto_off_remaining("mk4"))
        self.fake.printing = True
        refresh(self.octo)
        controls.describe(self.octo)                      # a status read that sees RUNNING drops it
        self.assertIsNone(controls.auto_off_remaining("mk4"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
