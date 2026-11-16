"""Tests for the temperature cards (app/temps.py): nozzles, bed, chamber, sensors, roles and labels.
Pure Python, no printers, no network.

    python3 tests/test_temps.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import temps  # noqa: E402

SNAP = {"connected": True, "nozzle_c": 215.24, "nozzle_target_c": 215.0, "bed_c": 58.1, "bed_target_c": 58.0}


def S(kind, name, c, target=None):
    return {"id": f"{kind} {name}", "kind": kind, "name": name, "c": c, "target": target}


class Roles(unittest.TestCase):
    def test_the_real_voron_names(self):
        want = {("temperature_sensor", "EBB_NTC"): "toolhead", ("temperature_sensor", "btt_eddy_mcu"): "probe",
                ("temperature_probe", "btt_eddy"): "probe", ("temperature_host", "Pi"): "host",
                ("temperature_sensor", "Pi"): "host", ("temperature_sensor", "chamber"): "chamber",
                ("temperature_sensor", "Enclosure"): "chamber", ("temperature_sensor", "octopus_mcu"): "board",
                ("temperature_sensor", "SB2209_temp"): "toolhead", ("temperature_sensor", "water_temp"): "other", ("temperature_sensor", "sb22"): "toolhead",
                ("temperature_sensor", "NHK_board"): "toolhead", ("temperature_sensor", "Manta"): "board",
                ("temperature_sensor", "rpi"): "host", ("temperature_sensor", "cpu_temp"): "host",
                ("temperature_sensor", "beacon_coil"): "probe", ("temperature_sensor", "scanner"): "other",
                ("temperature_sensor", "Spider"): "board", ("temperature_sensor", "drybox"): "other",
                ("temperature_host", "weird"): "host", ("temperature_probe", "x"): "probe",
                ("temperature_sensor", "EBBCan"): "toolhead", ("temperature_sensor", "can0"): "toolhead"}
        for (kind, name), role in want.items():
            self.assertEqual(temps.classify(kind, name), role, f"{kind} {name}")

    def test_case_is_ignored(self):
        self.assertEqual(temps.classify("temperature_sensor", "CHAMBER"), "chamber")
        self.assertEqual(temps.classify("temperature_sensor", "toolHead"), "toolhead")


class Basics(unittest.TestCase):
    def test_single_nozzle_and_bed_come_from_the_snapshot(self):
        t, s = temps.build(None, SNAP)
        self.assertEqual(t, [{"id": "nozzle", "label": "Nozzle", "kind": "nozzle", "c": 215.2, "target_c": 215.0},
                             {"id": "bed", "label": "Bed", "kind": "bed", "c": 58.1, "target_c": 58.0}])
        self.assertEqual(s, [])

    def test_missing_data_is_an_empty_list_never_an_error(self):
        for snap in (None, {}, {"connected": False, "nozzle_c": None, "bed_c": None}, "garbage"):
            self.assertEqual(temps.build(None, snap), ([], []))
        self.assertEqual(temps.build({"sensors": "x", "nozzles": "y"}, {}), ([], []))
        self.assertEqual(temps.build(object(), SNAP, {"chamber": object()}, {"sensor_labels": 5}), ([{"id": "nozzle", "label": "Nozzle", "kind": "nozzle", "c": 215.2, "target_c": 215.0}, {"id": "bed", "label": "Bed", "kind": "bed", "c": 58.1, "target_c": 58.0}], []))

    def test_two_nozzles_are_numbered_in_order(self):
        raw = {"nozzles": [{"c": 215.2, "target": 215}, {"c": 24.0, "target": 0}]}
        t, _ = temps.build(raw, SNAP)
        self.assertEqual([(x["id"], x["label"]) for x in t], [("nozzle", "Nozzle 1"), ("nozzle2", "Nozzle 2"), ("bed", "Bed")])
        self.assertEqual(t[1]["target_c"], 0.0)

    def test_chamber_comes_last(self):
        raw = {"sensors": [S("temperature_sensor", "chamber", 31.04)]}
        t, s = temps.build(raw, SNAP)
        self.assertEqual([x["kind"] for x in t], ["nozzle", "bed", "chamber"])
        self.assertEqual(t[-1], {"id": "chamber", "label": "Chamber", "kind": "chamber", "c": 31.0, "target_c": None})
        self.assertEqual(s, [])                                        # not repeated

    def test_a_heater_generic_chamber_keeps_its_target(self):
        raw = {"sensors": [S("temperature_sensor", "chamber", 30.0), S("heater_generic", "chamber", 45.5, 60)]}
        t, s = temps.build(raw, SNAP)
        self.assertEqual(t[-1]["target_c"], 60.0)
        self.assertEqual(t[-1]["c"], 45.5)
        self.assertEqual([x["id"] for x in s], ["temperature_sensor chamber"])
        self.assertEqual(s[0]["role"], "other")                        # the spare "chamber"-named reading is an ordinary one

    def test_a_non_chamber_heater_generic_is_just_a_reading(self):
        t, s = temps.build({"sensors": [S("heater_generic", "dryer", 40, 50)]}, SNAP)
        self.assertEqual([x["kind"] for x in t], ["nozzle", "bed"])
        self.assertEqual(s, [{"id": "heater_generic dryer", "label": "dryer", "role": "other", "c": 40.0}])


class RealVoron(unittest.TestCase):
    SENSORS = [S("temperature_sensor", "EBB_NTC", 34.76), S("temperature_sensor", "btt_eddy_mcu", 35.88),
               S("temperature_probe", "btt_eddy", 37.6), S("temperature_host", "Pi", 54.53), S("temperature_sensor", "Pi", 54.53)]

    def test_default_labels_and_roles(self):
        _, s = temps.build({"sensors": self.SENSORS}, SNAP)
        by = {x["id"]: x for x in s}
        self.assertEqual(by["temperature_sensor EBB_NTC"]["label"], "Toolhead board")
        self.assertEqual(by["temperature_sensor EBB_NTC"]["role"], "toolhead")
        self.assertEqual(by["temperature_sensor EBB_NTC"]["c"], 34.8)
        self.assertEqual(set(by["temperature_sensor EBB_NTC"]), {"id", "label", "role", "c"})
        self.assertEqual(len(s), 5)

    def test_duplicate_labels_are_kept_apart_and_nothing_crashes(self):
        _, s = temps.build({"sensors": self.SENSORS}, SNAP)
        labels = [x["label"] for x in s]
        self.assertEqual(len(set(labels)), len(labels), labels)
        pis = [x for x in s if x["role"] == "host"]
        self.assertEqual(len(pis), 2)
        self.assertTrue(all(x["label"].startswith("Pi (") for x in pis))
        probes = [x["label"] for x in s if x["role"] == "probe"]
        self.assertEqual(sorted(probes), ["Probe (btt_eddy)", "Probe (btt_eddy_mcu)"])

    def test_the_owners_label_wins_exactly_as_typed(self):
        ov = {"sensor_labels": {"temperature_sensor EBB_NTC": "Toolhead air", "temperature_sensor Pi": "Pi", "temperature_host Pi": "Pi"}}
        _, s = temps.build({"sensors": self.SENSORS}, SNAP, {}, ov)
        by = {x["id"]: x["label"] for x in s}
        self.assertEqual(by["temperature_sensor EBB_NTC"], "Toolhead air")
        self.assertEqual(by["temperature_sensor Pi"], "Pi")
        self.assertEqual(by["temperature_host Pi"], "Pi")             # both chosen by the owner: not touched

    def test_no_chamber_card_on_the_real_voron_without_a_chamber_sensor(self):
        t, _ = temps.build({"sensors": self.SENSORS}, SNAP, {"chamber": "sensor"})
        self.assertNotIn("chamber", [x["kind"] for x in t])


class ChamberChoice(unittest.TestCase):
    SENS = [S("temperature_sensor", "chamber", 31.0), S("temperature_sensor", "EBB_NTC", 34.0)]

    def test_override_picks_another_sensor_as_the_chamber(self):
        t, s = temps.build({"sensors": self.SENS}, SNAP, {}, {"chamber_sensor": "temperature_sensor EBB_NTC"})
        self.assertEqual(t[-1]["c"], 34.0)
        self.assertEqual([x["id"] for x in s], ["temperature_sensor chamber"])

    def test_empty_override_means_no_chamber(self):
        t, s = temps.build({"sensors": self.SENS}, SNAP, {}, {"chamber_sensor": ""})
        self.assertNotIn("chamber", [x["kind"] for x in t])
        self.assertEqual(len(s), 2)

    def test_override_naming_a_sensor_that_is_not_there_shows_no_chamber(self):
        t, _ = temps.build({"sensors": self.SENS}, SNAP, {}, {"chamber_sensor": "temperature_sensor gone"})
        self.assertNotIn("chamber", [x["kind"] for x in t])

    def test_profile_saying_none_hides_the_automatic_chamber_but_a_chosen_sensor_still_shows(self):
        t, s = temps.build({"sensors": self.SENS}, SNAP, {"chamber": "none"})
        self.assertNotIn("chamber", [x["kind"] for x in t])
        self.assertEqual(len(s), 2)
        t, _ = temps.build({"sensors": self.SENS}, SNAP, {"chamber": "none"}, {"chamber_sensor": "temperature_sensor chamber"})
        self.assertEqual(t[-1]["kind"], "chamber")

    def test_the_chamber_card_can_carry_the_owners_label(self):
        t, _ = temps.build({"sensors": self.SENS}, SNAP, {}, {"sensor_labels": {"temperature_sensor chamber": "Enclosure"}})
        self.assertEqual(t[-1]["label"], "Enclosure")


class DriverReportedChamber(unittest.TestCase):
    RAW = {"chamber": {"c": 31.0, "target": None}}

    def test_bambu_chamber_only_when_the_profile_says_there_is_one(self):
        for ch, shown in (("none", False), (None, False), ("sensor", True), ("heater", True)):
            t, _ = temps.build(self.RAW, SNAP, {"chamber": ch}, {}, "bambu")
            self.assertEqual("chamber" in [x["kind"] for x in t], shown, ch)

    def test_octoprint_chamber_shows_unless_the_profile_says_none(self):
        for ch, shown in (("none", False), (None, True), ("sensor", True)):
            t, _ = temps.build(self.RAW, SNAP, {"chamber": ch}, {}, "octoprint")
            self.assertEqual("chamber" in [x["kind"] for x in t], shown, ch)

    def test_a_bambu_chamber_target_is_never_invented(self):
        t, _ = temps.build(self.RAW, SNAP, {"chamber": "heater"}, {}, "bambu")
        self.assertIsNone(t[-1]["target_c"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
