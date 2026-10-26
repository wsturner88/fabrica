"""Tests for the Controls sheet: home, jog, extrude, one-heater-off, change filament (app/controls.py + the four drivers
+ the REST route + the Settings page's nozzle temperatures).

    python3 tests/test_controls_sheet.py

Every printer is a throwaway fake on 127.0.0.1 (random port) or a mocked MQTT client: no real printer or host is
contacted. The exact G-code / HTTP calls each driver sends are asserted. Companion to tests/test_controls.py, whose
fakes it reuses.
"""
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, HERE)

# The server module reads its config / data dir at import: point both at an empty temp dir BEFORE importing it.
_TMP = tempfile.mkdtemp(prefix="controls-sheet-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "metacache.json")
os.environ["BAMBU_MCP_API_AUTH"] = "off"

import controls  # noqa: E402
import settings_store as ss  # noqa: E402
from bambu import OctoPrintPrinter, PrinterError  # noqa: E402
from moonraker import MoonrakerPrinter  # noqa: E402
from prusalink import PrusaLinkPrinter  # noqa: E402
# helpers (importing test_controls sets its own throwaway BAMBU_MCP_* dir, then imports the server)
from test_controls import (CORE, KLIPPER_BASE, Fake, make_bambu, octo_handler, refresh, sent)  # noqa: E402
from test_controls import server, TestClient  # noqa: E402

JOG = "SAVE_GCODE_STATE NAME=fabrica\n{}\nRESTORE_GCODE_STATE NAME=fabrica"
RECOVER = "RESTORE_GCODE_STATE NAME=fabrica\nG90"
FILAMENT_MACROS = ["gcode_macro LOAD_FILAMENT", "gcode_macro UNLOAD_FILAMENT"]
M70X_MACROS = ["gcode_macro M701", "gcode_macro M702"]
NEW_ACTIONS = ("home", "move", "extrude", "heater_off", "unload", "load", "preheat")


_FAKES = []


def tearDownModule():
    """Fake.close() waits for the server's 0.5 s poll tick: close them all at once at the end, not one per test."""
    threads = [threading.Thread(target=f.close) for f in _FAKES]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)


def clean_state(case):
    """Per-test hygiene: no auto-off timer, default filament temperatures, before and after."""
    controls.reset_for_tests()
    controls.configure_filament_temps(None)
    case.addCleanup(controls.reset_for_tests)
    case.addCleanup(controls.configure_filament_temps, None)


# ------------------------------------------------------------------ fakes

def sheet_handler(fake, m, path, q, body):
    """A Klipper (Moonraker) fake whose homed axes, nozzle temperature and min_extrude_temp can be set per test."""
    if path == "/printer/objects/list":
        return 200, {"result": {"objects": fake.objects}}
    if path == "/printer/objects/query":
        names = list(q)
        if names == ["configfile"]:
            if fake.min_extrude == "error":
                return 404, {"error": {"code": 404, "message": "nope"}}
            ex = {} if fake.min_extrude is None else {"min_extrude_temp": fake.min_extrude}
            return 200, {"result": {"status": {"configfile": {"settings": {"extruder": ex}}}}}
        if any(n.split(" ")[0] in ("output_pin", "led", "neopixel", "dotstar") for n in names):
            return 200, {"result": {"status": {}}}
        base = copy.deepcopy(KLIPPER_BASE)
        base["print_stats"]["state"] = fake.pstate
        base["extruder"] = {"temperature": fake.nozzle, "target": fake.nozzle_target}
        if fake.can_extrude is not None:
            base["extruder"]["can_extrude"] = fake.can_extrude
        base["heater_bed"] = {"temperature": 24.0, "target": fake.bed_target}
        if fake.homed_axes is not None:
            base["toolhead"] = {"homed_axes": fake.homed_axes, "position": [0, 0, 0, 0]}
        return 200, {"result": {"status": base}}
    if path == "/printer/gcode/script" and m == "POST":
        if fake.fail_on and fake.fail_on in q["script"][0]:
            return 400, {"error": {"code": 400, "message": "Move out of range"}}
        return 200, {"result": "ok"}
    if path == "/server/webcams/list":
        return 200, {"result": {"webcams": []}}
    return 404, {"error": {"code": 404, "message": "nope"}}


def make_sheet(objects=(), homed="xyz", nozzle=200.0, nozzle_target=0.0, bed_target=0.0, pstate="standby", **ctor):
    fake = Fake(sheet_handler)
    fake.objects = CORE + ["toolhead"] + list(objects)
    fake.homed_axes, fake.nozzle, fake.nozzle_target, fake.bed_target = homed, nozzle, nozzle_target, bed_target
    fake.pstate, fake.min_extrude, fake.can_extrude, fake.fail_on = pstate, None, None, None
    prn = MoonrakerPrinter("Voron", fake.url, **ctor)
    prn._cam_probed = time.time()
    return fake, prn


def settle(fake, prn, **attrs):
    """Change what the Klipper fake reports and make the driver re-read it."""
    for k, v in attrs.items():
        setattr(fake, k, v)
    prn.refresh()


def hot_octo_handler(fake, m, path, q, body):
    if path == "/api/printer":
        f = {"printing": fake.printing, "paused": False, "error": False, "ready": not fake.printing}
        return 200, {"state": {"flags": f},
                     "temperature": {"tool0": {"actual": fake.nozzle, "target": 0.0}, "bed": {"actual": 24.0, "target": 0.0}}}
    return octo_handler(fake, m, path, q, body)


def make_hot_octo(name="MK4", model="Prusa MK4S", nozzle=200.0):
    fake = Fake(hot_octo_handler)
    fake.printing, fake.nozzle = False, nozzle
    prn = OctoPrintPrinter(name, fake.url, "key", model)
    return fake, prn


CMD = "/api/printer/command"


# ------------------------------------------------------------------ 1. Klipper

class KlipperCase(unittest.TestCase):
    def setUp(self):
        clean_state(self)

    def mk(self, objects=(), **kw):
        fake, prn = make_sheet(objects, **kw)
        self.addCleanup(_FAKES.append, fake)
        self.addCleanup(controls.cancel_auto_off, prn)
        return fake, prn


class TestKlipperHome(KlipperCase):
    def test_home_without_confirm_is_invalid_and_sends_nothing(self):
        fake, prn = self.mk()
        for confirm in (None, False, "true", 1, "yes"):
            with self.assertRaises(controls.ControlInvalid, msg=repr(confirm)):
                controls.perform(prn, "home", confirm=confirm)
        self.assertEqual(fake.scripts(), [])

    def test_home_with_confirm_sends_g28(self):
        fake, prn = self.mk()
        r = controls.perform(prn, "home", confirm=True)
        self.assertEqual(fake.scripts(), ["G28"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "home")

    def test_home_is_allowed_on_an_unhomed_printer(self):
        fake, prn = self.mk(homed="")
        controls.perform(prn, "home", confirm=True)
        self.assertEqual(fake.scripts(), ["G28"])


class TestKlipperJog(KlipperCase):
    def test_jog_scripts_for_each_axis_and_direction(self):
        fake, prn = self.mk()
        cases = [("X", 10, "G91\nG1 X10 F6000"), ("X", -10, "G91\nG1 X-10 F6000"),
                 ("Y", 5, "G91\nG1 Y5 F6000"), ("Y", -0.25, "G91\nG1 Y-0.25 F6000"),
                 ("Z", 1, "G91\nG1 Z1 F600"), ("Z", -10, "G91\nG1 Z-10 F600")]
        for axis, mm, body in cases:
            fake.log.clear()
            r = controls.perform(prn, "move", axis=axis, mm=mm)
            self.assertEqual(fake.scripts(), [JOG.format(body)], f"{axis}{mm}")
            self.assertEqual((r["axis"], r["mm"]), (axis, float(mm)))

    def test_lowercase_axis_is_accepted(self):
        fake, prn = self.mk()
        controls.perform(prn, "move", axis="y", mm=2)
        self.assertEqual(fake.scripts(), [JOG.format("G91\nG1 Y2 F6000")])

    def test_extrude_and_retract(self):
        fake, prn = self.mk()
        controls.perform(prn, "extrude", mm=10)
        controls.perform(prn, "extrude", mm=-10)
        self.assertEqual(fake.scripts(), [JOG.format("M83\nG1 E10 F300"), JOG.format("M83\nG1 E-10 F300")])

    def test_extrude_does_not_need_homing(self):
        fake, prn = self.mk(homed="")
        controls.perform(prn, "extrude", mm=5)
        self.assertEqual(fake.scripts(), [JOG.format("M83\nG1 E5 F300")])

    def test_a_failing_jog_restores_the_gcode_state_and_raises(self):
        fake, prn = self.mk()
        fake.fail_on = "G1"
        with self.assertRaises(PrinterError) as cm:
            controls.perform(prn, "move", axis="X", mm=10)
        self.assertIn("Move out of range", str(cm.exception))
        self.assertEqual(fake.scripts(), [JOG.format("G91\nG1 X10 F6000"), RECOVER])

    def test_a_failing_extrude_restores_the_gcode_state_and_raises(self):
        fake, prn = self.mk()
        fake.fail_on = "G1"
        with self.assertRaises(PrinterError):
            controls.perform(prn, "extrude", mm=-10)
        self.assertEqual(fake.scripts(), [JOG.format("M83\nG1 E-10 F300"), RECOVER])


class TestKlipperGuards(KlipperCase):
    def test_moves_refused_unless_every_axis_is_homed(self):
        fake, prn = self.mk(homed="")
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(prn, "move", axis="X", mm=1)
        self.assertIn("Home first", str(cm.exception))
        for partial in ("xy", "z", "xz"):
            settle(fake, prn, homed_axes=partial)
            with self.assertRaises(controls.ControlRefused, msg=partial):
                controls.perform(prn, "move", axis="X", mm=1)
        self.assertEqual(fake.scripts(), [])
        settle(fake, prn, homed_axes="xyz")
        controls.perform(prn, "move", axis="X", mm=1)
        self.assertEqual(len(fake.scripts()), 1)

    def test_a_klipper_that_reports_no_toolhead_falls_back_to_the_hubs_own_home(self):
        fake, prn = self.mk(homed=None)
        with self.assertRaises(controls.ControlRefused):
            controls.perform(prn, "move", axis="X", mm=1)
        controls.perform(prn, "home", confirm=True)
        fake.log.clear()
        controls.perform(prn, "move", axis="X", mm=1)
        self.assertEqual(fake.scripts(), [JOG.format("G91\nG1 X1 F6000")])
        self.assertEqual(controls.describe(prn)["motion"]["homed_how"], "hub")

    def test_reported_homed_state_wins_over_a_hub_home(self):
        fake, prn = self.mk(homed="")
        controls.perform(prn, "home", confirm=True)         # Klipper still says not homed (motors off again): believe Klipper
        with self.assertRaises(controls.ControlRefused):
            controls.perform(prn, "move", axis="X", mm=1)

    def test_cold_nozzle_refuses_extrude_at_170_c(self):
        fake, prn = self.mk(nozzle=25.0)
        for temp, ok in ((25.0, False), (169.9, False), (170.0, True), (200.0, True)):
            settle(fake, prn, nozzle=temp)
            fake.log.clear()
            if ok:
                controls.perform(prn, "extrude", mm=5)
                self.assertEqual(len(fake.scripts()), 1, temp)
            else:
                with self.assertRaises(controls.ControlRefused, msg=temp) as cm:
                    controls.perform(prn, "extrude", mm=5)
                self.assertIn("Heat the nozzle first", str(cm.exception))
                self.assertEqual(fake.scripts(), [], temp)

    def test_extrude_min_comes_from_the_printers_own_config(self):
        fake, prn = self.mk(nozzle=180.0)
        fake.min_extrude = 190
        self.assertEqual(prn.extrude_min_c(), 190)
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(prn, "extrude", mm=5)
        self.assertIn("190", str(cm.exception))
        self.assertEqual(fake.scripts(), [])
        self.assertEqual(controls.describe(prn)["motion"]["min_extrude_c"], 190)
        settle(fake, prn, nozzle=200.0)
        controls.perform(prn, "extrude", mm=5)
        self.assertEqual(len(fake.scripts()), 1)
        self.assertTrue(any(p == "/printer/objects/query" and "configfile" in q for _m, p, q, _b in fake.log))

    def test_unreadable_or_missing_config_falls_back_to_170(self):
        fake, prn = self.mk(nozzle=175.0)
        fake.min_extrude = "error"
        self.assertIsNone(prn.extrude_min_c())
        controls.perform(prn, "extrude", mm=5)
        self.assertEqual(controls.describe(prn)["motion"]["min_extrude_c"], 170)
        fake2, prn2 = self.mk(nozzle=175.0)         # config readable but no min_extrude_temp key
        self.assertIsNone(prn2.extrude_min_c())

    def test_klipper_can_extrude_false_refuses_even_when_hot(self):
        fake, prn = self.mk(nozzle=200.0)
        fake.can_extrude = False
        prn.refresh()
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(prn, "extrude", mm=5)
        self.assertIn("too cold", str(cm.exception))
        self.assertEqual(fake.scripts(), [])

    def test_moves_and_extrusion_refused_while_printing(self):
        fake, prn = self.mk(pstate="printing")
        for act, kw in (("home", {"confirm": True}), ("move", {"axis": "X", "mm": 1}), ("extrude", {"mm": 1}),
                        ("heater_off", {"heater": "both"}), ("unload", {}), ("load", {})):
            with self.assertRaises(controls.ControlRefused, msg=act):
                controls.perform(prn, act, **kw)
        self.assertEqual(fake.scripts(), [])


class TestKlipperHeaters(KlipperCase):
    def test_nozzle_button_sets_the_nozzle_only_and_arms_the_auto_off(self):
        fake, prn = self.mk()
        r = controls.perform(prn, "preheat", material="PETG")
        self.assertEqual(fake.scripts(), ["M104 S235"])              # no M140: the bed is untouched
        self.assertEqual((r["nozzle"], r["bed"]), (235, None))
        self.assertIsNotNone(controls.auto_off_remaining(prn))
        self.assertGreater(r["auto_off_in_s"], 0)

    def test_nozzle_material_defaults_and_case(self):
        fake, prn = self.mk()
        for material, want in (("PLA", "M104 S210"), ("abs", "M104 S245"), (" petg ", "M104 S235")):
            fake.log.clear()
            controls.perform(prn, "preheat", material=material)
            self.assertEqual(fake.scripts(), [want], material)

    def test_material_plus_an_explicit_bed_sets_both(self):
        fake, prn = self.mk()
        controls.perform(prn, "preheat", material="PLA", bed=60)
        self.assertEqual(fake.scripts(), ["M104 S210\nM140 S60"])

    def test_explicit_nozzle_beats_the_material(self):
        fake, prn = self.mk()
        r = controls.perform(prn, "preheat", material="PLA", nozzle=222)
        self.assertEqual(fake.scripts(), ["M104 S222"])
        self.assertEqual(r["nozzle"], 222)

    def test_heater_off_nozzle_bed_both(self):
        fake, prn = self.mk()
        for heater, want in (("nozzle", "M104 S0"), ("bed", "M140 S0"), ("both", "M104 S0\nM140 S0"), ("BED", "M140 S0")):
            fake.log.clear()
            controls.perform(prn, "heater_off", heater=heater)
            self.assertEqual(fake.scripts(), [want], heater)

    def test_heater_off_rejects_a_bad_heater_and_sends_nothing(self):
        fake, prn = self.mk()
        for heater in (None, "", "chamber", 5, "nozzle,bed"):
            with self.assertRaises(controls.ControlInvalid, msg=repr(heater)):
                controls.perform(prn, "heater_off", heater=heater)
        self.assertEqual(fake.scripts(), [])

    def test_heater_off_keeps_the_auto_off_while_the_other_heater_is_on(self):
        fake, prn = self.mk(bed_target=60.0)
        controls.perform(prn, "preheat", preset="PLA")               # bed 60 + auto-off armed
        self.assertIsNotNone(controls.auto_off_remaining(prn))
        prn.refresh()
        r = controls.perform(prn, "heater_off", heater="nozzle")
        self.assertIsNotNone(controls.auto_off_remaining(prn))       # the bed is still on: the timer stays
        self.assertIn("other heater", r["message"])
        controls.perform(prn, "heater_off", heater="both")
        self.assertIsNone(controls.auto_off_remaining(prn))

    def test_heater_off_cancels_the_auto_off_when_nothing_else_is_on(self):
        fake, prn = self.mk()
        controls.perform(prn, "preheat", material="PLA")
        self.assertIsNotNone(controls.auto_off_remaining(prn))
        controls.perform(prn, "heater_off", heater="nozzle")
        self.assertIsNone(controls.auto_off_remaining(prn))


# ------------------------------------------------------------------ 2. Klipper change filament

class TestKlipperFilament(KlipperCase):
    def test_unload_macro_heats_then_runs_the_macro_with_temp(self):
        fake, prn = self.mk(FILAMENT_MACROS)
        r = controls.perform(prn, "unload", material="PETG")
        self.assertEqual(fake.scripts(), ["M109 S235\nUNLOAD_FILAMENT TEMP=235"])
        self.assertEqual(r["nozzle"], 235)
        self.assertIsNotNone(controls.auto_off_remaining(prn))       # the heated nozzle gets the pre-heat auto-off

    def test_load_macro(self):
        fake, prn = self.mk(FILAMENT_MACROS)
        controls.perform(prn, "load", material="PLA")
        self.assertEqual(fake.scripts(), ["M109 S210\nLOAD_FILAMENT TEMP=210"])

    def test_macro_on_an_unhomed_printer_homes_first(self):
        fake, prn = self.mk(FILAMENT_MACROS, homed="")
        controls.perform(prn, "load", material="PETG")
        self.assertEqual(fake.scripts(), ["G28\nM109 S235\nLOAD_FILAMENT TEMP=235"])

    def test_macro_does_not_home_when_klipper_does_not_report(self):
        fake, prn = self.mk(FILAMENT_MACROS, homed=None)
        controls.perform(prn, "load", material="PETG")
        self.assertEqual(fake.scripts(), ["M109 S235\nLOAD_FILAMENT TEMP=235"])

    def test_m701_m702_macros_are_detected_and_used(self):
        fake, prn = self.mk(M70X_MACROS)
        controls.perform(prn, "unload", material="PETG")
        controls.perform(prn, "load", material="PETG")
        self.assertEqual(fake.scripts(), ["M109 S235\nM702 TEMP=235", "M109 S235\nM701 TEMP=235"])

    def test_load_filament_macro_is_preferred_over_m701(self):
        fake, prn = self.mk(FILAMENT_MACROS + M70X_MACROS)
        controls.perform(prn, "load", material="PLA")
        controls.perform(prn, "unload", material="PLA")
        self.assertEqual(fake.scripts(), ["M109 S210\nLOAD_FILAMENT TEMP=210", "M109 S210\nUNLOAD_FILAMENT TEMP=210"])

    def test_no_macros_unload_is_a_plain_gcode_sequence(self):
        fake, prn = self.mk()
        controls.perform(prn, "unload", material="PETG")
        self.assertEqual(fake.scripts(), ["M109 S235\nSAVE_GCODE_STATE NAME=fabrica\nM83\nG1 E10 F300\nG1 E-100 F1500\n"
                                          "RESTORE_GCODE_STATE NAME=fabrica"])

    def test_no_macros_load_is_a_plain_gcode_sequence(self):
        fake, prn = self.mk()
        controls.perform(prn, "load", material="PETG")
        self.assertEqual(fake.scripts(), ["M109 S235\nSAVE_GCODE_STATE NAME=fabrica\nM83\nG1 E90 F300\n"
                                          "RESTORE_GCODE_STATE NAME=fabrica"])

    def test_no_macros_unhomed_homes_before_heating(self):
        fake, prn = self.mk(homed="")
        controls.perform(prn, "load", material="PLA")
        self.assertEqual(fake.scripts(), ["G28\nM109 S210\nSAVE_GCODE_STATE NAME=fabrica\nM83\nG1 E90 F300\n"
                                          "RESTORE_GCODE_STATE NAME=fabrica"])

    def test_per_printer_lengths_are_used(self):
        fake, prn = self.mk(unload_mm=150, load_mm=120)
        controls.perform(prn, "unload", material="PLA")
        controls.perform(prn, "load", material="PLA")
        self.assertEqual(fake.scripts(), [
            "M109 S210\nSAVE_GCODE_STATE NAME=fabrica\nM83\nG1 E10 F300\nG1 E-150 F1500\nRESTORE_GCODE_STATE NAME=fabrica",
            "M109 S210\nSAVE_GCODE_STATE NAME=fabrica\nM83\nG1 E120 F300\nRESTORE_GCODE_STATE NAME=fabrica"])
        f = controls.describe(prn)["filament"]
        self.assertEqual((f["unload_mm"], f["load_mm"], f["needs_nozzle_temp"]), (150, 120, True))

    def test_bad_lengths_fall_back_to_the_defaults(self):
        for bad in (0, -5, 1501, "abc", float("nan"), None, ""):
            prn = MoonrakerPrinter("V", "http://127.0.0.1:9", unload_mm=bad, load_mm=bad)
            self.assertEqual((prn.unload_mm, prn.load_mm), (100.0, 90.0), repr(bad))
        prn = MoonrakerPrinter("V", "http://127.0.0.1:9", unload_mm="1500", load_mm=0.5)
        self.assertEqual((prn.unload_mm, prn.load_mm), (1500.0, 0.5))

    def test_lengths_come_from_the_config_entry(self):
        prn = MoonrakerPrinter.from_config({"name": "V", "url": "http://127.0.0.1:9", "unload_mm": 150, "load_mm": 120})
        self.assertEqual((prn.unload_mm, prn.load_mm), (150.0, 120.0))
        names = [f["name"] for f in MoonrakerPrinter.CONFIG_FIELDS]
        self.assertIn("unload_mm", names)
        self.assertIn("load_mm", names)

    def test_explicit_nozzle_below_170_is_invalid(self):
        fake, prn = self.mk(FILAMENT_MACROS)
        for act in ("unload", "load"):
            for n in (150, 169):
                with self.assertRaises(controls.ControlInvalid, msg=f"{act} {n}"):
                    controls.perform(prn, act, nozzle=n)
        self.assertEqual(fake.scripts(), [])
        controls.perform(prn, "unload", nozzle=170)
        self.assertEqual(fake.scripts(), ["M109 S170\nUNLOAD_FILAMENT TEMP=170"])

    def test_explicit_nozzle_beats_the_material(self):
        fake, prn = self.mk(FILAMENT_MACROS)
        controls.perform(prn, "load", nozzle=222, material="PETG")
        self.assertEqual(fake.scripts(), ["M109 S222\nLOAD_FILAMENT TEMP=222"])

    def test_unknown_material_is_invalid(self):
        fake, prn = self.mk(FILAMENT_MACROS)
        with self.assertRaises(controls.ControlInvalid):
            controls.perform(prn, "unload", material="NYLON")
        self.assertEqual(fake.scripts(), [])

    def test_default_with_an_idle_nozzle_uses_the_first_material_pla(self):
        fake, prn = self.mk(FILAMENT_MACROS, nozzle_target=0.0)
        controls.perform(prn, "load")
        self.assertEqual(fake.scripts(), ["M109 S210\nLOAD_FILAMENT TEMP=210"])

    def test_default_reuses_a_hot_nozzle_target(self):
        fake, prn = self.mk(FILAMENT_MACROS, nozzle_target=240.0)
        controls.perform(prn, "load")
        self.assertEqual(fake.scripts(), ["M109 S240\nLOAD_FILAMENT TEMP=240"])

    def test_default_ignores_a_target_below_170(self):
        fake, prn = self.mk(FILAMENT_MACROS, nozzle_target=150.0)
        controls.perform(prn, "load")
        self.assertEqual(fake.scripts(), ["M109 S210\nLOAD_FILAMENT TEMP=210"])

    def test_failing_generic_sequence_restores_the_gcode_state(self):
        fake, prn = self.mk()
        fake.fail_on = "G1"
        with self.assertRaises(PrinterError):
            controls.perform(prn, "unload", material="PLA")
        self.assertEqual(len(fake.scripts()), 2)
        self.assertEqual(fake.scripts()[1], RECOVER)

    def test_failing_macro_raises_without_a_recovery_script(self):
        fake, prn = self.mk(FILAMENT_MACROS)
        fake.fail_on = "UNLOAD_FILAMENT"
        with self.assertRaises(PrinterError):
            controls.perform(prn, "unload", material="PLA")
        self.assertEqual(len(fake.scripts()), 1)

    def test_describe_entries(self):
        fake, prn = self.mk()
        d = controls.describe(prn)
        for k in ("home", "move", "extrude", "heater_off", "change_filament", "unload", "load"):
            self.assertTrue(d[k]["supported"], k)
        self.assertTrue(d["change_filament"]["allowed_now"])
        self.assertIn("no unload macro", d["unload"]["note"])
        self.assertEqual(d["filament"]["temps"], {"PLA": 210, "PETG": 235, "ABS": 245})
        d2 = controls.describe(self.mk(FILAMENT_MACROS)[1])
        self.assertIn("UNLOAD_FILAMENT", d2["unload"]["note"])
        self.assertIn("LOAD_FILAMENT", d2["load"]["note"])

    def test_no_extruder_means_no_filament_extrude_or_heat(self):
        fake, prn = self.mk()
        fake.objects = [o for o in fake.objects if o != "extruder"]
        prn._caps_cache = None
        d = controls.describe(prn)
        for k in ("extrude", "unload", "load", "change_filament"):
            self.assertFalse(d[k]["supported"], k)
            self.assertTrue(d[k]["reason"], k)
        self.assertTrue(d["home"]["supported"])


# ------------------------------------------------------------------ 3. OctoPrint

class OctoCase(unittest.TestCase):
    def setUp(self):
        clean_state(self)

    def mk(self, **kw):
        fake, prn = make_hot_octo(**kw)
        self.addCleanup(_FAKES.append, fake)
        self.addCleanup(controls.cancel_auto_off, prn)
        return fake, prn


class TestOctoPrintMotion(OctoCase):
    def test_home_needs_confirm(self):
        fake, prn = self.mk()
        with self.assertRaises(controls.ControlInvalid):
            controls.perform(prn, "home")
        self.assertEqual(fake.posts(), [])
        controls.perform(prn, "home", confirm=True)
        self.assertEqual(fake.posts(), [(CMD, {"commands": ["G28"]})])

    def test_jogs_after_a_hub_home(self):
        fake, prn = self.mk()
        controls.perform(prn, "home", confirm=True)
        fake.log.clear()
        controls.perform(prn, "move", axis="X", mm=10)
        controls.perform(prn, "move", axis="Y", mm=-10)
        controls.perform(prn, "move", axis="Z", mm=-5)
        controls.perform(prn, "move", axis="Z", mm=0.5)
        self.assertEqual(fake.posts(), [(CMD, {"commands": ["G91", "G1 X10 F3000", "G90"]}),
                                        (CMD, {"commands": ["G91", "G1 Y-10 F3000", "G90"]}),
                                        (CMD, {"commands": ["G91", "G1 Z-5 F600", "G90"]}),
                                        (CMD, {"commands": ["G91", "G1 Z0.5 F600", "G90"]})])

    def test_extrude_and_retract(self):
        fake, prn = self.mk()
        controls.perform(prn, "extrude", mm=10)
        controls.perform(prn, "extrude", mm=-10)
        self.assertEqual(fake.posts(), [(CMD, {"commands": ["M83", "G1 E10 F300", "M82"]}),
                                        (CMD, {"commands": ["M83", "G1 E-10 F300", "M82"]})])

    def test_cold_nozzle_refuses_extrude(self):
        fake, prn = self.mk(nozzle=25.0)
        with self.assertRaises(controls.ControlRefused):
            controls.perform(prn, "extrude", mm=5)
        self.assertEqual(fake.posts(), [])

    def test_moves_refused_until_the_hub_homed_the_printer(self):
        fake, prn = self.mk()
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(prn, "move", axis="X", mm=1)
        self.assertIn("Home first", str(cm.exception))
        self.assertEqual(fake.posts(), [])
        d = controls.describe(prn)
        self.assertFalse(d["move"]["allowed_now"])
        self.assertEqual((d["motion"]["homed"], d["motion"]["homed_how"]), (False, "hub"))
        controls.perform(prn, "home", confirm=True)
        controls.perform(prn, "move", axis="X", mm=1)
        self.assertEqual(len(fake.posts()), 2)
        self.assertTrue(controls.describe(prn)["move"]["allowed_now"])

    def test_the_hubs_home_expires_after_the_ttl(self):
        fake, prn = self.mk()
        controls.perform(prn, "home", confirm=True)
        with mock.patch.object(controls, "HUB_HOMED_TTL_S", 0.05):
            time.sleep(0.15)
            with self.assertRaises(controls.ControlRefused):
                controls.perform(prn, "move", axis="X", mm=1)
        self.assertEqual(len(fake.posts()), 1)               # only the home
        controls.perform(prn, "home", confirm=True)          # homing again makes moves legal again
        controls.perform(prn, "move", axis="X", mm=1)
        self.assertEqual(len(fake.posts()), 3)

    def test_an_old_hub_home_is_refused_and_a_recent_one_is_extended_by_a_move(self):
        fake, prn = self.mk()
        controls.perform(prn, "home", confirm=True)
        prn._hub_homed_at = time.monotonic() - controls.HUB_HOMED_TTL_S - 5
        with self.assertRaises(controls.ControlRefused):
            controls.perform(prn, "move", axis="X", mm=1)
        self.assertIsNone(prn._hub_homed_at)
        controls.perform(prn, "home", confirm=True)
        old = time.monotonic() - controls.HUB_HOMED_TTL_S + 30
        prn._hub_homed_at = old
        controls.perform(prn, "move", axis="X", mm=1)
        self.assertGreater(prn._hub_homed_at, old + 20)      # a jog keeps the Hub's 'homed' fresh

    def test_a_print_seen_after_the_home_forgets_it(self):
        fake, prn = self.mk()
        controls.perform(prn, "home", confirm=True)
        fake.printing = True
        refresh(prn)
        with self.assertRaises(controls.ControlRefused):
            controls.perform(prn, "move", axis="X", mm=1)    # busy: refused
        controls.describe(prn)                               # a status read that sees the print drops the Hub's home
        self.assertIsNone(prn._hub_homed_at)
        fake.printing = False
        refresh(prn)
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(prn, "move", axis="X", mm=1)
        self.assertIn("Home first", str(cm.exception))

    def test_everything_refused_while_printing(self):
        fake, prn = self.mk()
        fake.printing = True
        refresh(prn)
        for act, kw in (("home", {"confirm": True}), ("move", {"axis": "X", "mm": 1}), ("extrude", {"mm": 1}),
                        ("heater_off", {"heater": "nozzle"}), ("unload", {}), ("load", {})):
            with self.assertRaises(controls.ControlRefused, msg=act):
                controls.perform(prn, act, **kw)
        self.assertEqual(fake.posts(), [])


class TestOctoPrintFilament(OctoCase):
    def test_load_and_unload_heat_first_in_one_command_list(self):
        fake, prn = self.mk()
        controls.perform(prn, "unload")
        controls.perform(prn, "load")
        self.assertEqual(fake.posts(), [(CMD, {"commands": ["M109 S210", "M702"]}),
                                        (CMD, {"commands": ["M109 S210", "M701"]})])

    def test_material_and_explicit_nozzle(self):
        fake, prn = self.mk()
        controls.perform(prn, "load", material="PETG")
        controls.perform(prn, "unload", nozzle=250)
        controls.perform(prn, "load", nozzle=222, material="ABS")
        self.assertEqual(fake.posts(), [(CMD, {"commands": ["M109 S235", "M701"]}),
                                        (CMD, {"commands": ["M109 S250", "M702"]}),
                                        (CMD, {"commands": ["M109 S222", "M701"]})])

    def test_nozzle_below_170_is_invalid(self):
        fake, prn = self.mk()
        with self.assertRaises(controls.ControlInvalid):
            controls.perform(prn, "load", nozzle=160)
        self.assertEqual(fake.posts(), [])

    def test_filament_arms_the_auto_off(self):
        fake, prn = self.mk()
        controls.perform(prn, "load", material="PLA")
        self.assertIsNotNone(controls.auto_off_remaining(prn))

    def test_describe_for_a_plain_printer(self):
        fake, prn = self.mk()
        d = controls.describe(prn)
        self.assertTrue(d["change_filament"]["supported"])
        self.assertIs(d["change_filament"].get("verified"), False)
        self.assertTrue(d["filament"]["needs_nozzle_temp"])
        for k in ("home", "move", "extrude"):
            self.assertIs(d[k]["verified"], False, k)
        self.assertFalse(prn.is_mmu)


class TestOctoPrintMMU(OctoCase):
    def mmu(self):
        fake = Fake(octo_handler)
        fake.printing = False
        self.addCleanup(_FAKES.append, fake)
        prn = OctoPrintPrinter("MK3-MMU", fake.url, "key", "Prusa MK3S+MMU3")
        self.addCleanup(controls.cancel_auto_off, prn)
        return fake, prn

    def test_is_mmu_detection(self):
        for name, model, want in (("MK3-MMU", "", True), ("Shop1", "Prusa MK3S+MMU3", True), ("MMU2S", "", True),
                                  ("MK4", "Prusa MK4S", False), ("Immune", "", False), ("Gemmule", "", False)):
            self.assertEqual(OctoPrintPrinter(name, "http://127.0.0.1:9", "k", model).is_mmu, want, f"{name}/{model}")

    def test_load_unload_are_unsupported_with_the_reason(self):
        fake, prn = self.mmu()
        for act in ("unload", "load"):
            with self.assertRaises(controls.ControlUnsupported) as cm:
                controls.perform(prn, act)
            self.assertIn("MMU printer", str(cm.exception))
        self.assertEqual(fake.posts(), [])

    def test_the_driver_itself_also_refuses(self):
        fake, prn = self.mmu()
        for fn in (prn.unload_filament, prn.load_filament):
            with self.assertRaises(controls.ControlUnsupported):
                fn(nozzle=210)
        self.assertEqual(fake.posts(), [])

    def test_describe_marks_change_filament_unsupported_but_the_rest_supported(self):
        fake, prn = self.mmu()
        d = controls.describe(prn)
        self.assertFalse(d["change_filament"]["supported"])
        self.assertIn("MMU printer", d["change_filament"]["reason"])
        for k in ("unload", "load"):
            self.assertFalse(d[k]["supported"], k)
            self.assertIn("MMU printer", d[k]["reason"], k)
        for k in ("home", "move", "extrude", "preheat", "cooldown", "heater_off"):
            self.assertTrue(d[k]["supported"], k)
        self.assertNotIn("unload", d["actions"])
        self.assertNotIn("load", d["actions"])
        self.assertIn("home", d["actions"])

    def test_home_and_heat_still_work(self):
        fake, prn = self.mmu()
        controls.perform(prn, "home", confirm=True)
        controls.perform(prn, "heater_off", heater="both")
        self.assertEqual(fake.posts(), [(CMD, {"commands": ["G28"]}),
                                        ("/api/printer/tool", {"command": "target", "targets": {"tool0": 0}}),
                                        ("/api/printer/bed", {"command": "target", "target": 0})])


# ------------------------------------------------------------------ 4. PrusaLink

class TestPrusaLinkSheet(unittest.TestCase):
    def setUp(self):
        clean_state(self)
        self.fake = Fake(lambda f, m, p, q, b: (200, {"printer": {"state": "IDLE"}}) if p == "/api/v1/status" else (404, {}))
        self.addCleanup(_FAKES.append, self.fake)
        self.prn = PrusaLinkPrinter("MK4", self.fake.url, {"api_key": "k"})
        self.prn._cam_probed = time.time()

    def test_every_new_action_is_unsupported_with_the_existing_reason(self):
        calls = (("home", {"confirm": True}), ("move", {"axis": "X", "mm": 1}), ("extrude", {"mm": 1}),
                 ("heater_off", {"heater": "both"}), ("preheat", {"material": "PLA"}), ("unload", {}), ("load", {}))
        for act, kw in calls:
            with self.assertRaises(controls.ControlUnsupported, msg=act) as cm:
                controls.perform(self.prn, act, **kw)
            self.assertIn("no light, temperature, G-code or filament endpoint", str(cm.exception), act)
        self.assertEqual([x for x in self.fake.log if x[0] != "GET"], [])

    def test_invalid_arguments_do_not_mask_unsupported(self):
        with self.assertRaises(controls.ControlUnsupported):
            controls.perform(self.prn, "move", axis="W", mm=999)
        with self.assertRaises(controls.ControlUnsupported):
            controls.perform(self.prn, "home")           # no confirm: still "unsupported" first

    def test_the_driver_primitives_raise_too(self):
        for fn, args in ((self.prn.home_all, ()), (self.prn.move_relative, ("X", 1)), (self.prn.extrude, (1,)),
                         (self.prn.unload_filament, ()), (self.prn.load_filament, ())):
            with self.assertRaises(controls.ControlUnsupported):
                fn(*args)

    def test_describe_lists_each_new_control_unsupported_with_a_reason(self):
        d = controls.describe(self.prn)
        for k in ("home", "move", "extrude", "heater_off", "change_filament", "unload", "load", "preheat"):
            self.assertIn(k, d, k)
            self.assertFalse(d[k]["supported"], k)
            self.assertTrue(d[k].get("reason"), k)
            self.assertNotIn("allowed_now", d[k], k)
        self.assertEqual(d["actions"], [])
        self.assertEqual(controls.summary(self.prn)["allowed_now"], [])


# ------------------------------------------------------------------ 5. Bambu

def bambu_homed(prn):
    controls.perform(prn, "home", confirm=True)
    prn._client.publish.reset_mock()


class TestBambuSheet(unittest.TestCase):
    def setUp(self):
        clean_state(self)

    def test_home_sends_g28(self):
        prn = make_bambu()
        with self.assertRaises(controls.ControlInvalid):
            controls.perform(prn, "home")
        self.assertEqual(prn._client.publish.call_count, 0)
        controls.perform(prn, "home", confirm=True)
        m = sent(prn)[0]["print"]
        self.assertEqual((m["command"], m["param"]), ("gcode_line", "G28\n"))

    def test_jogs_after_a_hub_home(self):
        prn = make_bambu()
        bambu_homed(prn)
        controls.perform(prn, "move", axis="Z", mm=-10)
        controls.perform(prn, "move", axis="X", mm=10)
        controls.perform(prn, "move", axis="Y", mm=-2.5)
        self.assertEqual([m["print"]["param"] for m in sent(prn)],
                         ["G91\nG1 Z-10 F600\nG90\n", "G91\nG1 X10 F3000\nG90\n", "G91\nG1 Y-2.5 F3000\nG90\n"])
        self.assertTrue(all(m["print"]["command"] == "gcode_line" for m in sent(prn)))

    def test_moves_refused_until_the_hub_homed_it(self):
        prn = make_bambu()
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(prn, "move", axis="X", mm=1)
        self.assertIn("Home first", str(cm.exception))
        self.assertEqual(prn._client.publish.call_count, 0)
        self.assertFalse(controls.describe(prn)["move"]["allowed_now"])

    def test_the_hub_home_is_forgotten_when_the_link_drops_or_it_expires(self):
        prn = make_bambu()
        bambu_homed(prn)
        self.assertTrue(controls.describe(prn)["move"]["allowed_now"])
        prn._connected = False
        self.assertFalse(controls.describe(prn)["motion"]["homed"])
        prn._connected = True
        with self.assertRaises(controls.ControlRefused):
            controls.perform(prn, "move", axis="X", mm=1)
        bambu_homed(prn)
        prn._hub_homed_at = time.monotonic() - controls.HUB_HOMED_TTL_S - 1
        with self.assertRaises(controls.ControlRefused):
            controls.perform(prn, "move", axis="X", mm=1)
        self.assertEqual(prn._client.publish.call_count, 0)

    def test_extrude_needs_a_hot_nozzle(self):
        prn = make_bambu()
        with self.assertRaises(controls.ControlRefused):         # the mock nozzle reads 31.5 C
            controls.perform(prn, "extrude", mm=5)
        self.assertEqual(prn._client.publish.call_count, 0)
        prn.state["print"]["nozzle_temper"] = 200
        controls.perform(prn, "extrude", mm=10)
        controls.perform(prn, "extrude", mm=-10)
        self.assertEqual([m["print"]["param"] for m in sent(prn)], ["M83\nG1 E10 F300\nM82\n", "M83\nG1 E-10 F300\nM82\n"])

    def test_the_new_caps_are_marked_beta_and_unverified(self):
        caps = make_bambu().control_caps()
        for k in ("home", "move", "extrude"):
            self.assertIs(caps[k]["supported"], True, k)
            self.assertIs(caps[k]["verified"], False, k)
            self.assertIn("Beta", caps[k]["note"], k)
        self.assertTrue(caps["move"]["note"].startswith("Beta"))
        self.assertTrue(caps["extrude"]["note"].startswith("Beta"))
        d = controls.describe(make_bambu())
        for k in ("home", "move", "extrude"):
            self.assertIs(d[k]["verified"], False, k)
            self.assertIn("Beta", d[k]["note"], k)

    def test_busy_printer_refuses_every_motion_control(self):
        for st in ("RUNNING", "PREPARE", "PAUSE"):
            prn = make_bambu(st)
            for act, kw in (("home", {"confirm": True}), ("move", {"axis": "X", "mm": 1}), ("extrude", {"mm": 1})):
                with self.assertRaises(controls.ControlRefused, msg=f"{st} {act}"):
                    controls.perform(prn, act, **kw)
            self.assertEqual(prn._client.publish.call_count, 0)

    def test_load_and_unload_still_go_through_the_ams(self):
        prn = make_bambu()
        controls.perform(prn, "unload")
        controls.perform(prn, "load", tray=2)
        a, b = (m["print"] for m in sent(prn))
        self.assertEqual((a["command"], a["target"]), ("ams_change_filament", 255))
        self.assertEqual((b["command"], b["target"]), ("ams_change_filament", 1))

    def test_the_nozzle_and_material_arguments_are_ignored(self):
        prn = make_bambu()
        r = controls.perform(prn, "load", tray=2, material="PETG")
        controls.perform(prn, "unload", nozzle=250, material="PLA")
        self.assertEqual(len(sent(prn)), 2)
        for m in sent(prn):
            self.assertEqual(m["print"]["command"], "ams_change_filament")
        self.assertEqual(sent(prn)[0]["print"]["tar_temp"], 230)       # the AMS filament's own minimum, not the Hub's
        self.assertEqual(sent(prn)[1]["print"]["tar_temp"], 220)
        self.assertNotIn("M109", json.dumps(sent(prn)))
        self.assertNotIn("nozzle", r)
        self.assertFalse(controls.describe(prn)["filament"]["needs_nozzle_temp"])


# ------------------------------------------------------------------ 6. server refusals through the REST route

ROUTE_CALLS = {
    "home": {"confirm": True}, "move": {"axis": "X", "mm": 5}, "extrude": {"mm": 5}, "heater_off": {"heater": "both"},
    "unload": {}, "load": {}, "preheat": {"material": "PLA"},
}


@unittest.skipIf(server is None, "server deps not installed")
class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.mcp.streamable_http_app())
        cls.ofake, cls.octo = make_hot_octo(name="ZOcto")
        cls.octo._cam_probed = time.time()
        cls.kfake, cls.klip = make_sheet(FILAMENT_MACROS)
        cls.klip.name = "ZVoron"
        cls.plfake = Fake(lambda f, m, p, q, b: (200, {"printer": {"state": "IDLE"}}))
        cls.pl = PrusaLinkPrinter("ZPrusa", cls.plfake.url, {"api_key": "k"})
        cls.pl._cam_probed = time.time()
        cls.printers = (cls.octo, cls.klip, cls.pl)
        for p in cls.printers:
            server.PRINTERS[p.name.lower()] = p

    @classmethod
    def tearDownClass(cls):
        for p in cls.printers:
            controls.cancel_auto_off(p)
            server.PRINTERS.pop(p.name.lower(), None)
        _FAKES.extend((cls.ofake, cls.kfake, cls.plfake))

    def setUp(self):
        clean_state(self)
        for p in (self.octo, self.klip):
            self.addCleanup(controls.clear_start_pending, p)
            controls.clear_start_pending(p)
            self.addCleanup(setattr, p, "_hub_homed_at", None)
            p._hub_homed_at = None
        self.ofake.printing, self.ofake.nozzle = False, 200.0
        self.kfake.pstate, self.kfake.homed_axes, self.kfake.nozzle = "standby", "xyz", 200.0
        self.kfake.fail_on = None
        self.sync()
        self.ofake.log.clear()
        self.kfake.log.clear()

    def sync(self):
        refresh(self.octo)
        self.klip.refresh()

    def post(self, name, **body):
        return self.client.post(f"/api/printer/{name}/control", json=body)

    def raw(self, name, text):
        return self.client.post(f"/api/printer/{name}/control", content=text.encode(),
                                headers={"Content-Type": "application/json"})

    def mutations(self):
        return self.ofake.posts(), self.kfake.scripts()

    def assertNothingSent(self):
        self.assertEqual(self.mutations(), ([], []))


class TestServerRefusals(ServerCase):
    def test_busy_printer_refuses_every_new_action_with_409(self):
        self.ofake.printing = True
        self.kfake.pstate = "printing"
        self.sync()
        for name in ("zocto", "zvoron"):
            for act, kw in ROUTE_CALLS.items():
                r = self.post(name, action=act, **kw)
                self.assertEqual(r.status_code, 409, f"{name} {act} {r.text}")
                self.assertTrue(r.json()["error"].startswith("REFUSED"), f"{name} {act}")
                self.assertEqual(r.json()["code"], "refused")
        self.assertNothingSent()

    def test_start_pending_counts_as_busy(self):
        for p in (self.octo, self.klip):
            controls.mark_start_sent(p)
        self.sync()
        for name in ("zocto", "zvoron"):
            for act, kw in ROUTE_CALLS.items():
                r = self.post(name, action=act, **kw)
                self.assertEqual(r.status_code, 409, f"{name} {act} {r.text}")
                self.assertIn("start was just sent", r.json()["reason"], f"{name} {act}")
        self.assertNothingSent()

    def test_not_homed_refuses_move_with_409(self):
        self.kfake.homed_axes = ""
        self.sync()
        for name in ("zocto", "zvoron"):               # the octo was never homed by the Hub either
            r = self.post(name, action="move", axis="X", mm=5)
            self.assertEqual(r.status_code, 409, name)
            self.assertIn("Home first", r.json()["reason"])
        self.assertNothingSent()

    def test_cold_nozzle_refuses_extrude_with_409(self):
        self.ofake.nozzle = self.kfake.nozzle = 25.0
        self.sync()
        for name in ("zocto", "zvoron"):
            r = self.post(name, action="extrude", mm=5)
            self.assertEqual(r.status_code, 409, name)
            self.assertIn("Heat the nozzle first", r.json()["reason"])
        self.assertNothingSent()

    def test_distances_out_of_range_or_not_numbers_are_400(self):
        for act in ("move", "extrude"):
            extra = {"axis": "X"} if act == "move" else {}
            for bad in (11, -11, 10.5, 10.0001, 0, -0.0, "abc", True, False, None, [], {}):
                r = self.post("zvoron", action=act, mm=bad, **extra)
                self.assertEqual(r.status_code, 400, f"{act} {bad!r} {r.text}")
                self.assertEqual(r.json()["code"], "invalid")
            for token in ("NaN", "Infinity", "-Infinity"):
                r = self.raw("zvoron", '{"action": "%s", "mm": %s%s}' % (act, token, ', "axis": "X"' if extra else ""))
                self.assertEqual(r.status_code, 400, f"{act} {token}")
            r = self.post("zvoron", action=act, **extra)           # mm missing
            self.assertEqual(r.status_code, 400, act)
        self.assertNothingSent()

    def test_limits_are_inclusive(self):
        self.assertEqual(self.post("zvoron", action="move", axis="X", mm=10).status_code, 200)
        self.assertEqual(self.post("zvoron", action="move", axis="X", mm=-10).status_code, 200)
        self.assertEqual(self.post("zvoron", action="extrude", mm=10).status_code, 200)
        self.assertEqual(self.post("zvoron", action="extrude", mm=-10).status_code, 200)
        self.assertEqual(len(self.kfake.scripts()), 4)

    def test_bad_axis_is_400(self):
        for axis in ("W", "", "XY", "E", 5, None, ["X"], True):
            r = self.post("zvoron", action="move", axis=axis, mm=1)
            self.assertEqual(r.status_code, 400, repr(axis))
        r = self.post("zvoron", action="move", mm=1)               # missing
        self.assertEqual(r.status_code, 400)
        self.assertNothingSent()

    def test_unknown_material_is_400(self):
        for name in ("zvoron", "zocto"):
            for act in ("preheat", "unload", "load"):
                for material in ("NYLON", "pla2", 5):
                    r = self.post(name, action=act, material=material)
                    self.assertEqual(r.status_code, 400, f"{name} {act} {material!r} {r.text}")
        self.assertNothingSent()

    def test_bad_heater_is_400(self):
        for heater in (None, "", "chamber", 5, "all", ["bed"]):
            r = self.post("zvoron", action="heater_off", heater=heater)
            self.assertEqual(r.status_code, 400, repr(heater))
        r = self.post("zvoron", action="heater_off")
        self.assertEqual(r.status_code, 400)
        self.assertNothingSent()

    def test_home_without_confirm_is_400_and_only_true_confirms(self):
        for confirm in (None, False, "true", 1, "yes", [True]):
            kw = {} if confirm is None else {"confirm": confirm}
            r = self.post("zvoron", action="home", **kw)
            self.assertEqual(r.status_code, 400, repr(confirm))
            self.assertIn("confirm", r.json()["reason"])
        self.assertNothingSent()
        r = self.post("zvoron", action="home", confirm=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.kfake.scripts(), ["G28"])

    def test_unsupported_printer_gets_422_for_every_new_action(self):
        for act, kw in ROUTE_CALLS.items():
            r = self.post("zprusa", action=act, **kw)
            self.assertEqual(r.status_code, 422, f"{act} {r.text}")
            self.assertTrue(r.json()["error"].startswith("UNSUPPORTED"), act)
            self.assertTrue(r.json()["reason"], act)
        r = self.post("zprusa", action="move", axis="W", mm=99)         # unsupported is decided before the arguments
        self.assertEqual(r.status_code, 422)
        self.assertEqual([x for x in self.plfake.log if x[0] != "GET"], [])

    def test_unknown_printer_is_404_for_every_new_action(self):
        for act, kw in ROUTE_CALLS.items():
            self.assertEqual(self.post("ghost", action=act, **kw).status_code, 404, act)

    def test_success_through_the_route_reaches_the_printer(self):
        r = self.post("zvoron", action="move", axis="y", mm=-3)
        self.assertEqual((r.status_code, r.json()["axis"], r.json()["mm"]), (200, "Y", -3.0))
        r = self.post("zvoron", action="preheat", material="PETG")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["nozzle"], r.json()["bed"]), (235, None))
        self.assertGreater(r.json()["auto_off_in_s"], 0)
        r = self.post("zvoron", action="heater_off", heater="nozzle")
        self.assertEqual(r.status_code, 200)
        r = self.post("zvoron", action="unload", material="PETG")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.kfake.scripts(), [JOG.format("G91\nG1 Y-3 F6000"), "M104 S235", "M104 S0",
                                                "M109 S235\nUNLOAD_FILAMENT TEMP=235"])
        r = self.post("zocto", action="home", confirm=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.post("zocto", action="move", axis="X", mm=2).status_code, 200)
        self.assertEqual(self.ofake.posts(), [(CMD, {"commands": ["G28"]}), (CMD, {"commands": ["G91", "G1 X2 F3000", "G90"]})])

    def test_a_printer_failure_is_502(self):
        self.kfake.fail_on = "G1"
        r = self.post("zvoron", action="move", axis="X", mm=1)
        self.assertEqual(r.status_code, 502, r.text)
        self.assertTrue(r.json()["error"].startswith("FAILED"))
        self.assertEqual(self.kfake.scripts()[-1], RECOVER)


class TestServerReadOnlyCaller(ServerCase):
    def test_a_read_only_caller_is_refused_with_403_and_never_reaches_the_printer(self):
        with mock.patch.object(server.security, "caller_read_only", return_value=True):
            for name in ("zocto", "zvoron"):
                for act, kw in ROUTE_CALLS.items():
                    r = self.post(name, action=act, **kw)
                    self.assertEqual(r.status_code, 403, f"{name} {act} {r.text}")
                    self.assertTrue(r.json()["error"].startswith("REFUSED"))
                    self.assertEqual(r.json()["code"], "refused")
        self.assertEqual(self.ofake.log, [])
        self.assertEqual(self.kfake.log, [])
        self.assertIsNone(controls.auto_off_remaining(self.klip))

    def test_a_read_only_caller_can_still_read(self):
        with mock.patch.object(server.security, "caller_read_only", return_value=True):
            self.assertEqual(self.client.get("/api/printer/zvoron/controls").status_code, 200)

    def test_the_key_table_refuses_a_read_key_and_lets_a_control_key_through(self):
        a = server.AUTH
        old_env = a.env
        a.env = {k: v for k, v in os.environ.items() if k != "BAMBU_MCP_API_AUTH"}
        server.SETTINGS.update({"security.api_auth": "on"})
        rec_r, read_token = a.create_key("sheet-read", "read")
        rec_c, ctl_token = a.create_key("sheet-control", "control")

        def restore():
            server.SETTINGS.update({"security.api_auth": "off"})
            a.env = old_env
            for rec in (rec_r, rec_c):
                a.delete_key(rec["id"])
        self.addCleanup(restore)
        read_h, ctl_h = {"Authorization": "Bearer " + read_token}, {"Authorization": "Bearer " + ctl_token}
        body = {"action": "move", "axis": "X", "mm": 5}
        url = "/api/printer/zvoron/control"
        self.assertEqual(self.client.post(url, json=body).status_code, 401)          # no key at all
        r = self.client.post(url, json=body, headers=read_h)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["required_scope"], "control")
        self.assertEqual(self.kfake.scripts(), [])
        self.assertEqual([x for x in self.kfake.log if x[0] != "GET"], [])
        r = self.client.post(url, json=body, headers=ctl_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.kfake.scripts(), [JOG.format("G91\nG1 X5 F6000")])
        r = self.client.get("/api/printer/zvoron", headers=read_h)                    # reading stays open to a read key
        self.assertEqual(r.status_code, 200)


class TestServerControlsSummary(ServerCase):
    def detail(self, name):
        return self.client.get(f"/api/printer/{name}").json()["controls"]

    def test_detail_carries_the_sheet_entries(self):
        c = self.detail("zvoron")
        d = c["detail"]
        for k in ("home", "move", "extrude", "heater_off", "change_filament", "motion", "filament"):
            self.assertIn(k, d, k)
        for k in ("home", "move", "extrude", "heater_off", "change_filament"):
            self.assertTrue(d[k]["supported"], k)
        self.assertEqual(d["motion"]["max_move_mm"], 10.0)
        self.assertEqual(d["motion"]["max_extrude_mm"], 10.0)
        self.assertEqual(d["motion"]["axes"], ["X", "Y", "Z"])
        self.assertEqual((d["motion"]["homed"], d["motion"]["homed_how"]), (True, "reported"))
        self.assertEqual(d["filament"]["temps"], {"PLA": 210, "PETG": 235, "ABS": 245})
        self.assertTrue(d["filament"]["needs_nozzle_temp"])
        for k in ("home", "move", "extrude"):
            self.assertIn(k, c["actions"], k)

    def test_allowed_now_when_idle_homed_and_hot(self):
        self.assertEqual(set(self.detail("zvoron")["allowed_now"]),
                         {"preheat", "cooldown", "heater_off", "unload", "load", "home", "move", "extrude", "change_filament"})

    def test_allowed_now_when_unhomed(self):
        self.kfake.homed_axes = ""
        self.sync()
        c = self.detail("zvoron")
        self.assertNotIn("move", c["allowed_now"])
        self.assertIn("home", c["allowed_now"])
        self.assertIn("extrude", c["allowed_now"])
        self.assertIn("Home first", c["detail"]["move"]["blocked"])
        self.assertFalse(c["detail"]["motion"]["homed"])

    def test_allowed_now_when_cold(self):
        self.kfake.nozzle = 25.0
        self.sync()
        c = self.detail("zvoron")
        self.assertNotIn("extrude", c["allowed_now"])
        self.assertIn("move", c["allowed_now"])
        self.assertIn("Heat the nozzle first", c["detail"]["extrude"]["blocked"])

    def test_allowed_now_when_busy(self):
        self.kfake.pstate = "printing"
        self.ofake.printing = True
        self.sync()
        self.assertEqual(self.detail("zvoron")["allowed_now"], [])
        self.assertEqual(self.detail("zocto")["allowed_now"], [])
        self.assertIn("RUNNING", self.detail("zvoron")["detail"]["home"]["blocked"])

    def test_the_hub_homed_octoprint_unlocks_move_in_the_summary(self):
        before = self.detail("zocto")
        self.assertNotIn("move", before["allowed_now"])
        self.assertIn("home", before["allowed_now"])
        self.assertEqual(before["detail"]["motion"]["homed_how"], "hub")
        self.assertEqual(self.post("zocto", action="home", confirm=True).status_code, 200)
        self.assertIn("move", self.detail("zocto")["allowed_now"])

    def test_unsupported_printer_summary(self):
        c = self.detail("zprusa")
        self.assertEqual((c["actions"], c["allowed_now"]), ([], []))
        for k in ("home", "move", "extrude", "heater_off", "change_filament"):
            self.assertFalse(c["detail"][k]["supported"], k)
            self.assertTrue(c["detail"][k]["reason"], k)


# ------------------------------------------------------------------ 7. settings

class TestFilamentTemperatureSettings(unittest.TestCase):
    KEYS = ("printers.filament_temp_pla", "printers.filament_temp_petg", "printers.filament_temp_abs")

    def setUp(self):
        clean_state(self)
        self.dir = tempfile.mkdtemp()
        self.store = ss.SettingsStore(self.dir, env={})

    def bad(self, key, value, fragment):
        with self.assertRaises(ss.SettingsError) as cm:
            self.store.update({key: value})
        self.assertIn(key, cm.exception.errors)
        self.assertIn(fragment, cm.exception.errors[key])

    def test_fields_exist_with_defaults_and_range(self):
        for key, default in zip(self.KEYS, (210, 235, 245)):
            self.assertIn(key, ss.FIELDS)
            self.assertEqual(self.store.get(key), default)
            f = ss.FIELDS[key]
            self.assertEqual((f["min"], f["max"]), (170, 300), key)
        self.assertEqual(ss.FILAMENT_TEMP_DEFAULTS, {"PLA": 210, "PETG": 235, "ABS": 245})

    def test_170_and_300_are_accepted(self):
        for key in self.KEYS:
            self.store.update({key: 170})
            self.assertEqual(self.store.get(key), 170)
            self.store.update({key: 300})
            self.assertEqual(self.store.get(key), 300)

    def test_169_and_301_are_rejected(self):
        for key in self.KEYS:
            self.bad(key, 169, "at least")
            self.bad(key, 301, "at most")
            self.assertIn(self.store.get(key), (210, 235, 245))

    def test_junk_is_rejected(self):
        self.bad(self.KEYS[0], "hot", "number")
        self.bad(self.KEYS[0], True, "number")
        self.bad(self.KEYS[1], 210.5, "whole")

    @unittest.skipIf(server is None, "server deps not installed")
    def test_saving_a_value_changes_what_the_controls_use(self):
        def restore():
            server.SETTINGS.update({k: v for k, v in zip(self.KEYS, (210, 235, 245))})
            server._apply_control_settings()
        self.addCleanup(restore)
        server.SETTINGS.update({self.KEYS[0]: 222, self.KEYS[1]: 240, self.KEYS[2]: 260})
        server._apply_control_settings()
        self.assertEqual(controls.filament_temps(), {"PLA": 222, "PETG": 240, "ABS": 260})
        self.assertEqual(controls.material_nozzle("PLA"), 222)
        fake, prn = make_sheet(FILAMENT_MACROS)
        self.addCleanup(_FAKES.append, fake)
        self.addCleanup(controls.cancel_auto_off, prn)
        controls.perform(prn, "preheat", material="PLA")
        controls.perform(prn, "load", material="ABS")
        self.assertEqual(fake.scripts(), ["M104 S222", "M109 S260\nLOAD_FILAMENT TEMP=260"])
        self.assertEqual(controls.describe(prn)["filament"]["temps"]["PETG"], 240)
        restore()
        self.assertEqual(controls.filament_temps(), {"PLA": 210, "PETG": 235, "ABS": 245})

    def test_configure_filament_temps_skips_bad_entries_and_keeps_defaults(self):
        logs = []
        controls.configure_filament_temps({"pla": 215, "petg": "hot", "abs": 301, "nylon": 270, "tpu": 0, "x": True},
                                          log=logs.append)
        self.assertEqual(controls.filament_temps(), {"PLA": 215, "PETG": 235, "ABS": 245, "NYLON": 270})
        self.assertEqual(len(logs), 4)
        controls.configure_filament_temps(None)
        self.assertEqual(controls.filament_temps(), {"PLA": 210, "PETG": 235, "ABS": 245})
        controls.configure_filament_temps({"pla": 215})
        controls.configure_filament_temps({})
        self.assertEqual(controls.material_nozzle("PLA"), 210)

    def test_material_nozzle(self):
        self.assertEqual(controls.material_nozzle(" petg "), 235)
        for bad in ("NYLON", "", None, 5):
            with self.assertRaises(controls.ControlInvalid, msg=repr(bad)):
                controls.material_nozzle(bad)

    def test_filament_temps_returns_a_copy(self):
        controls.filament_temps()["PLA"] = 1
        self.assertEqual(controls.material_nozzle("PLA"), 210)


# ------------------------------------------------------------------ 8. perform ordering / robustness

class Dummy:
    """A minimal driver whose snapshot the test controls; records what perform() sent."""
    kind = "dummy"

    def __init__(self, **snap):
        self.name = "dummy"
        self.snap = {"connected": True, "state": "IDLE", "nozzle_c": 200.0, "nozzle_target_c": 0.0, "homed": True}
        self.snap.update(snap)
        self.calls = []

    def snapshot(self):
        return dict(self.snap)

    def control_caps(self):
        return {k: {"supported": True} for k in ("light", "heat", "unload", "load", "home", "move", "extrude")}

    def set_temps(self, nozzle=None, bed=None):
        self.calls.append(("temps", nozzle, bed))

    def home_all(self):
        self.calls.append(("home",))

    def move_relative(self, axis, mm):
        self.calls.append(("move", axis, mm))

    def extrude(self, mm):
        self.calls.append(("extrude", mm))


class TestPerformRobustness(unittest.TestCase):
    def setUp(self):
        clean_state(self)

    def test_move_limit_is_exact(self):
        d = Dummy()
        for bad in (10.0001, -10.0001, 10.01, 11, 1e9):
            with self.assertRaises(controls.ControlInvalid, msg=bad):
                controls.perform(d, "move", axis="X", mm=bad)
        self.assertEqual(d.calls, [])
        controls.perform(d, "move", axis="X", mm=10)
        controls.perform(d, "move", axis="X", mm=-10)
        controls.perform(d, "move", axis="Z", mm=10.0)
        self.assertEqual(d.calls, [("move", "X", 10.0), ("move", "X", -10.0), ("move", "Z", 10.0)])

    def test_extrude_limit_is_exact(self):
        d = Dummy()
        with self.assertRaises(controls.ControlInvalid):
            controls.perform(d, "extrude", mm=10.0001)
        controls.perform(d, "extrude", mm=-10)
        self.assertEqual(d.calls, [("extrude", -10.0)])

    def test_clean_mm(self):
        self.assertEqual(controls.clean_mm(10, 10, "t"), 10.0)
        self.assertEqual(controls.clean_mm(-10, 10, "t"), -10.0)
        self.assertEqual(controls.clean_mm(0.25, 10, "t"), 0.25)
        self.assertEqual(controls.clean_mm(1.239, 10, "t"), 1.24)
        self.assertIsInstance(controls.clean_mm(3, 10, "t"), float)
        for bad in (0, 0.0, -0.0, 10.001, -10.001, True, False, None, "abc", "", [], {}, float("nan"),
                    float("inf"), float("-inf")):
            with self.assertRaises(controls.ControlInvalid, msg=repr(bad)):
                controls.clean_mm(bad, 10, "t")

    def test_clean_axis(self):
        for given, want in (("x", "X"), ("Y", "Y"), (" z ", "Z")):
            self.assertEqual(controls.clean_axis(given), want)
        for bad in ("W", "", "XY", "E", None, 5, True, ["X"], b"X"):
            with self.assertRaises(controls.ControlInvalid, msg=repr(bad)):
                controls.clean_axis(bad)

    def test_call_driver_passes_only_supported_keywords(self):
        def plain(a, b=None):
            return a, b

        def with_nozzle(a, nozzle=None):
            return a, nozzle

        def anything(a, **kw):
            return a, kw

        def nothing():
            return "ok"
        self.assertEqual(controls._call_driver(plain, 1, b=2, nozzle=250), (1, 2))
        self.assertEqual(controls._call_driver(with_nozzle, 1, nozzle=250, tray=3), (1, 250))
        self.assertEqual(controls._call_driver(anything, 1, nozzle=250, tray=3), (1, {"nozzle": 250, "tray": 3}))
        self.assertEqual(controls._call_driver(nothing, nozzle=250), "ok")

    def test_an_older_driver_without_the_nozzle_argument_still_works(self):
        class Old(Dummy):
            FILAMENT_NOZZLE_TEMP = True

            def unload_filament(self):
                self.calls.append(("unload",))

            def load_filament(self, tray=None):
                self.calls.append(("load", tray))
        d = Old()
        controls.perform(d, "unload", material="PETG")
        controls.perform(d, "load", tray=2, nozzle=230)
        self.assertEqual(d.calls, [("unload",), ("load", 2)])

    def test_the_driver_gets_the_nozzle_it_needs(self):
        class NeedsTemp(Dummy):
            FILAMENT_NOZZLE_TEMP = True

            def unload_filament(self, nozzle=None):
                self.calls.append(("unload", nozzle))

            def load_filament(self, tray=None, nozzle=None):
                self.calls.append(("load", tray, nozzle))
        d = NeedsTemp()
        controls.perform(d, "unload", material="ABS")
        controls.perform(d, "load")
        d.snap["nozzle_target_c"] = 230.0
        controls.perform(d, "load")
        self.assertEqual(d.calls, [("unload", 245), ("load", None, 210), ("load", None, 230)])

    def test_check_order_unsupported_then_invalid_then_busy_then_homed_or_cold(self):
        d = Dummy(state="RUNNING", homed=False, nozzle_c=25.0)
        with self.assertRaises(controls.ControlInvalid):           # invalid input beats a busy printer
            controls.perform(d, "move", axis="W", mm=1)
        with self.assertRaises(controls.ControlInvalid):
            controls.perform(d, "home")
        with self.assertRaises(controls.ControlRefused) as cm:     # busy beats not-homed / too-cold
            controls.perform(d, "move", axis="X", mm=1)
        self.assertIn("RUNNING", str(cm.exception))
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(d, "extrude", mm=1)
        self.assertIn("RUNNING", str(cm.exception))
        self.assertEqual(d.calls, [])
        d.caps_off = True
        d.control_caps = lambda: {k: {"supported": False, "reason": "nope"} for k in ("light", "heat", "unload", "load", "home", "move", "extrude")}
        with self.assertRaises(controls.ControlUnsupported):       # unsupported beats invalid
            controls.perform(d, "move", axis="W", mm=99)

    def test_unknown_action_is_invalid(self):
        with self.assertRaises(controls.ControlInvalid):
            controls.perform(Dummy(), "explode")

    def test_reported_homed_flag_is_believed(self):
        d = Dummy(homed=False)
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(d, "move", axis="X", mm=1)
        self.assertIn("not homed", str(cm.exception))
        d.snap["homed"] = True
        controls.perform(d, "move", axis="X", mm=1)
        self.assertEqual(d.calls, [("move", "X", 1.0)])

    def test_unknown_homed_state_needs_a_hub_home_and_perform_home_provides_it(self):
        d = Dummy()
        del d.snap["homed"]
        with self.assertRaises(controls.ControlRefused):
            controls.perform(d, "move", axis="X", mm=1)
        controls.perform(d, "home", confirm=True)
        controls.perform(d, "move", axis="X", mm=1)
        self.assertEqual(d.calls, [("home",), ("move", "X", 1.0)])

    def test_extrude_min_from_the_driver_is_honoured_when_sane(self):
        d = Dummy(nozzle_c=200.0)
        d.extrude_min_c = lambda: 220
        with self.assertRaises(controls.ControlRefused) as cm:
            controls.perform(d, "extrude", mm=1)
        self.assertIn("220", str(cm.exception))
        for junk in (None, True, 0, -5, 999, "190"):
            d.extrude_min_c = lambda junk=junk: junk
            controls.perform(d, "extrude", mm=1)                    # junk -> the 170 default, 200 C is enough
        d.extrude_min_c = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        controls.perform(d, "extrude", mm=1)
        self.assertEqual(len(d.calls), 7)

    def test_unknown_nozzle_temperature_refuses_extrude(self):
        for cur in (None, True, "hot"):
            d = Dummy(nozzle_c=cur)
            with self.assertRaises(controls.ControlRefused, msg=repr(cur)) as cm:
                controls.perform(d, "extrude", mm=1)
            self.assertIn("unknown", str(cm.exception))
            self.assertEqual(d.calls, [])

    def test_distances_are_json_numbers_and_a_rounded_zero_is_still_zero(self):
        for bad in ("5", "10", "abc", True, None, 0, 0.0, 0.004, -0.004, float("nan"), float("inf")):
            with self.assertRaises(controls.ControlInvalid, msg=repr(bad)):
                controls.clean_mm(bad, 10.0, "move")
        self.assertEqual(controls.clean_mm(0.01, 10.0, "move"), 0.01)
        self.assertEqual(controls.clean_mm(-10, 10.0, "move"), -10.0)

    def test_an_unknown_material_is_refused_on_every_printer(self):
        d = Dummy()                                   # no FILAMENT_NOZZLE_TEMP trait, like a Bambu with an AMS
        for action in ("load", "unload"):
            with self.assertRaises(controls.ControlInvalid, msg=action):
                controls.perform(d, action, material="NYLON")
        self.assertEqual(d.calls, [])

    def test_a_configured_material_below_the_min_extrude_temperature_keeps_the_built_in_number(self):
        self.addCleanup(controls.configure_filament_temps, None)
        logged = []
        controls.configure_filament_temps({"PLA": 150, "TPU": 225}, log=logged.append)
        self.assertEqual(controls.material_nozzle("PLA"), 210)
        self.assertEqual(controls.material_nozzle("TPU"), 225)
        self.assertEqual(len(logged), 1)

    def test_describe_motion_block(self):
        m = controls.describe(Dummy())["motion"]
        self.assertEqual((m["max_move_mm"], m["max_extrude_mm"], m["min_extrude_c"]), (10.0, 10.0, 170))
        self.assertEqual((m["homed"], m["homed_how"], m["nozzle_c"]), (True, "reported", 200.0))

    def test_heater_off_passes_the_right_targets(self):
        d = Dummy()
        for heater, want in (("nozzle", (0, None)), ("bed", (None, 0)), ("both", (0, 0))):
            d.calls.clear()
            controls.perform(d, "heater_off", heater=heater)
            self.assertEqual(d.calls, [("temps",) + want], heater)

    def test_cooling_is_allowed_on_an_unready_but_connected_printer(self):
        d = Dummy(state="UNKNOWN")
        controls.perform(d, "heater_off", heater="both")             # switching OFF is the safe direction
        with self.assertRaises(controls.ControlRefused):
            controls.perform(d, "extrude", mm=1)
        d2 = Dummy(connected=False, state="DISCONNECTED")
        with self.assertRaises(controls.ControlRefused):
            controls.perform(d2, "heater_off", heater="both")


if __name__ == "__main__":
    unittest.main(verbosity=1)
