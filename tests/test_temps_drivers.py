"""Tests for what each driver reports to the temperature cards: Klipper discovery (fixture = the real object list of a
Voron), Bambu chamber, OctoPrint tools and chamber. No printers, no external network: Klipper is a throwaway fake on
127.0.0.1, the Bambu and OctoPrint drivers are fed state directly.

    python3 tests/test_temps_drivers.py          (needs the app's dependencies: paho-mqtt)
"""
import http.server
import json
import os
import sys
import threading
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import profiles  # noqa: E402
import temps  # noqa: E402
from bambu import BambuPrinter, OctoPrintPrinter  # noqa: E402
from moonraker import MoonrakerPrinter  # noqa: E402


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


VORON_OBJECTS = fixture("klipper_objects_voron.json")["objects"]
VORON_STATUS = fixture("klipper_status_voron.json")


class FakeKlipper:
    """Answers the few Moonraker routes the driver polls; /printer/objects/query returns only the objects asked for."""

    def __init__(self, objects, status):
        self.objects, self.status = list(objects), dict(status)
        self.queries, self.lists = [], 0
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                if u.path == "/printer/objects/list":
                    fake.lists += 1
                    return self.reply(200, {"result": {"objects": fake.objects}})
                if u.path == "/printer/objects/query":
                    fake.queries.append(u.query)
                    asked = [urllib.parse.unquote(x) for x in u.query.split("&") if x]
                    st = {"print_stats": {"state": "standby", "filename": "", "print_duration": 0.0, "info": {}},
                          "virtual_sdcard": {"progress": 0.0}, "display_status": {"progress": 0.0},
                          "webhooks": {"state": "ready", "state_message": "ready"}, "toolhead": {}}
                    st.update(fake.status)
                    return self.reply(200, {"result": {"status": {k: st[k] for k in asked if k in st}}})
                if u.path == "/server/webcams/list":
                    return self.reply(200, {"result": {"webcams": []}})
                self.reply(404, {"error": {"message": "no"}})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class KlipperBase(unittest.TestCase):
    objects, status = VORON_OBJECTS, VORON_STATUS

    def setUp(self):
        self.fake = FakeKlipper(self.objects, self.status)
        self.p = MoonrakerPrinter("K", self.fake.url, None, "Voron 2.4")
        self.addCleanup(self.fake.close)

    def poll(self, times=2):
        for _ in range(times):                   # the first poll learns the object list, the next one reads the sensors
            self.p.refresh()
            snap = self.p.snapshot()
        return snap

    def cards(self, has=None, overrides=None):
        snap = self.poll()
        return temps.build(self.p.temp_readings(), snap, has or {}, overrides or {}, "moonraker")


class KlipperVoron(KlipperBase):
    def test_old_snapshot_keys_are_unchanged_and_carry_no_new_noise(self):
        snap = self.poll()
        self.assertEqual((snap["nozzle_c"], snap["nozzle_target_c"], snap["bed_c"], snap["bed_target_c"]), (24.6, 0.0, 24.6, 0.0))
        self.assertNotIn("temps", snap)

    def test_the_object_list_is_read_once_and_the_sensors_ride_in_the_same_status_query(self):
        self.poll(4)
        self.assertEqual(self.fake.lists, 1)                         # one list for exclude_object AND the temperature sensors
        last = urllib.parse.unquote(self.fake.queries[-1])
        for o in ("temperature_sensor EBB_NTC", "temperature_sensor btt_eddy_mcu", "temperature_probe btt_eddy",
                  "temperature_host Pi", "temperature_sensor Pi"):
            self.assertIn(o, last)
        self.assertNotIn("heater_fan", last)                         # fans have no temperature
        self.assertNotIn("temperature_sensor", urllib.parse.unquote(self.fake.queries[0]))     # first poll: list not known yet

    def test_the_real_voron_cards(self):
        t, s = self.cards()
        self.assertEqual([(x["id"], x["label"], x["kind"]) for x in t], [("nozzle", "Nozzle", "nozzle"), ("bed", "Bed", "bed")])
        by = {x["id"]: x for x in s}
        self.assertEqual(by["temperature_sensor EBB_NTC"], {"id": "temperature_sensor EBB_NTC", "label": "Toolhead board",
                                                            "role": "toolhead", "c": 34.8})
        self.assertEqual(len(s), 5)                                  # both Pi readings kept
        self.assertEqual(len({x["label"] for x in s}), 5)

    def test_a_label_the_owner_chose_wins(self):
        _, s = self.cards(overrides={"sensor_labels": {"temperature_sensor EBB_NTC": "Toolhead air"}})
        self.assertEqual({x["id"]: x["label"] for x in s}["temperature_sensor EBB_NTC"], "Toolhead air")

    def test_the_owner_can_name_one_of_the_sensors_as_the_chamber(self):
        t, s = self.cards(overrides={"chamber_sensor": "temperature_sensor EBB_NTC"})
        self.assertEqual([x["kind"] for x in t], ["nozzle", "bed", "chamber"])
        self.assertEqual(t[-1]["c"], 34.8)
        self.assertNotIn("temperature_sensor EBB_NTC", [x["id"] for x in s])


class KlipperChamberSensor(KlipperBase):
    objects = ["extruder", "heater_bed", "temperature_sensor chamber", "temperature_sensor Pi", "exclude_object", "toolhead"]
    status = {"extruder": {"temperature": 200.0, "target": 200.0}, "heater_bed": {"temperature": 60.0, "target": 60.0},
              "temperature_sensor chamber": {"temperature": 38.26}, "temperature_sensor Pi": {"temperature": 50.0}}

    def test_a_sensor_called_chamber_gives_a_chamber_card_with_no_setup(self):
        t, s = self.cards()                                           # generic profile: chamber unknown
        self.assertEqual(t[-1], {"id": "chamber", "label": "Chamber", "kind": "chamber", "c": 38.3, "target_c": None})
        self.assertEqual([x["id"] for x in s], ["temperature_sensor Pi"])
        self.assertEqual(s[0]["label"], "Pi")

    def test_a_profile_that_says_no_chamber_hides_it(self):
        t, _ = self.cards(has={"chamber": "none"})
        self.assertNotIn("chamber", [x["kind"] for x in t])


class KlipperChamberHeater(KlipperBase):
    objects = ["extruder", "heater_bed", "heater_generic chamber", "exclude_object", "toolhead"]
    status = {"extruder": {"temperature": 25.0, "target": 0.0}, "heater_bed": {"temperature": 25.0, "target": 0.0},
              "heater_generic chamber": {"temperature": 40.0, "target": 55.0, "power": 0.3}}

    def test_a_chamber_heater_carries_its_target(self):
        t, _ = self.cards()
        self.assertEqual(t[-1]["target_c"], 55.0)


class KlipperTwoNozzles(KlipperBase):
    objects = ["extruder", "extruder1", "heater_bed", "exclude_object", "toolhead"]
    status = {"extruder": {"temperature": 215.2, "target": 215.0}, "extruder1": {"temperature": 24.0, "target": 0.0},
              "heater_bed": {"temperature": 58.1, "target": 58.0}}

    def test_extruder1_makes_nozzle_1_and_nozzle_2(self):
        t, _ = self.cards()
        self.assertEqual([(x["id"], x["label"]) for x in t], [("nozzle", "Nozzle 1"), ("nozzle2", "Nozzle 2"), ("bed", "Bed")])
        self.assertEqual(t[1]["c"], 24.0)
        self.assertIn("extruder1", urllib.parse.unquote(self.fake.queries[-1]))

    def test_the_old_keys_still_describe_the_first_nozzle(self):
        snap = self.poll()
        self.assertEqual((snap["nozzle_c"], snap["nozzle_target_c"]), (215.2, 215.0))


class KlipperRestart(unittest.TestCase):
    def test_classify_orders_nozzles_numerically_and_caps_the_sensor_list(self):
        objs = ["extruder10", "extruder2", "extruder", "extruder1", "temperature_sensor " + "x"] \
            + [f"temperature_sensor s{i}" for i in range(100)] + ["temperature_sensor ", "heater_fan f", "tmc2209 extruder"]
        got = MoonrakerPrinter.classify_temp_objects(objs)
        self.assertEqual(got["nozzles"], ["extruder", "extruder1", "extruder2", "extruder10"])
        self.assertEqual(len(got["sensors"]), 32)
        self.assertTrue(all(" " in s and s.split(" ", 1)[1] for s in got["sensors"]))

    def test_a_klipper_that_is_down_has_no_extra_readings_and_no_crash(self):
        p = MoonrakerPrinter("K", "http://127.0.0.1:9", None)
        self.assertEqual(p.temp_readings(), {})
        self.assertEqual(temps.build(p.temp_readings(), p.snapshot()), ([], []))


class BambuCards(unittest.TestCase):
    REG = profiles.Registry(log=lambda *a: None)

    def printer(self, model, chamber=31.0):
        p = BambuPrinter("B", "192.0.2.10", "SERIAL000000001", "code", model)
        pr = {"nozzle_temper": 210.0, "nozzle_target_temper": 215.0, "bed_temper": 60.0, "bed_target_temper": 60.0}
        if chamber is not None:
            pr["chamber_temper"] = chamber
        p.state = {"print": pr}
        return p

    def cards(self, model, name="B", chamber=31.0, overrides=None):
        p = self.printer(model, chamber)
        res = self.REG.resolve({"type": "bambu", "model": model, "name": name, "profile_overrides": overrides})
        snap = {"nozzle_c": 210.0, "nozzle_target_c": 215.0, "bed_c": 60.0, "bed_target_c": 60.0}
        return res, temps.build(p.temp_readings(), snap, res["has"], res["overrides"], "bambu")

    def test_p1s_has_no_chamber_card_even_though_it_reports_a_number(self):
        res, (t, _) = self.cards("Bambu Lab P1S")
        self.assertEqual(res["id"], "bambu-p1s")
        self.assertEqual([x["kind"] for x in t], ["nozzle", "bed"])

    def test_a1_and_unknown_models_have_none_either(self):
        for model in ("Bambu Lab A1", "Bambu Lab A1 mini", "Something new"):
            _, (t, _) = self.cards(model)
            self.assertNotIn("chamber", [x["kind"] for x in t], model)

    def test_x1c_and_h2d_show_the_chamber(self):
        for model in ("Bambu Lab X1 Carbon", "Bambu Lab H2D"):
            _, (t, _) = self.cards(model, chamber=44.4)
            self.assertEqual(t[-1], {"id": "chamber", "label": "Chamber", "kind": "chamber", "c": 44.4, "target_c": None}, model)

    def test_x1c_without_a_reading_has_no_card_and_no_error(self):
        _, (t, _) = self.cards("Bambu Lab X1 Carbon", chamber=None)
        self.assertEqual([x["kind"] for x in t], ["nozzle", "bed"])

    def test_the_owner_can_switch_the_chamber_on_for_a_p1s_with_a_mod(self):
        _, (t, _) = self.cards("Bambu Lab P1S", overrides={"chamber": "sensor"})
        self.assertEqual(t[-1]["kind"], "chamber")

    def test_the_bambu_snapshot_keys_are_not_touched(self):
        snap = self.printer("Bambu Lab X1 Carbon").snapshot()
        self.assertNotIn("temps", snap)
        self.assertNotIn("chamber_c", snap)


class OctoPrintCards(unittest.TestCase):
    def printer(self, temperature):
        p = OctoPrintPrinter("O", "http://192.0.2.20", "key", "Prusa MK4S")
        p._ok = True
        p._last = ({"state": {"flags": {}}, "temperature": temperature}, {})
        return p

    def test_tools_bed_and_chamber_are_read_when_present(self):
        p = self.printer({"tool0": {"actual": 200.0, "target": 210.0}, "tool1": {"actual": 25.0, "target": 0.0},
                          "bed": {"actual": 60.0, "target": 60.0}, "chamber": {"actual": 35.5, "target": None}})
        t, _ = temps.build(p.temp_readings(), {}, {}, {}, "octoprint")
        self.assertEqual([(x["id"], x["label"], x["c"]) for x in t],
                         [("nozzle", "Nozzle 1", 200.0), ("nozzle2", "Nozzle 2", 25.0), ("chamber", "Chamber", 35.5)])
        # the bed comes from the snapshot, exactly as before
        t, _ = temps.build(p.temp_readings(), {"bed_c": 60.0, "bed_target_c": 60.0}, {}, {}, "octoprint")
        self.assertEqual([x["kind"] for x in t], ["nozzle", "nozzle", "bed", "chamber"])

    def test_a_plain_printer_has_just_nozzle_and_bed(self):
        p = self.printer({"tool0": {"actual": 200.0, "target": 210.0}, "bed": {"actual": 60.0, "target": 60.0}})
        snap = {"nozzle_c": 200.0, "nozzle_target_c": 210.0, "bed_c": 60.0, "bed_target_c": 60.0}
        t, s = temps.build(p.temp_readings(), snap, {}, {}, "octoprint")
        self.assertEqual([(x["id"], x["label"]) for x in t], [("nozzle", "Nozzle"), ("bed", "Bed")])
        self.assertEqual(s, [])

    def test_no_reading_yet_is_empty_not_an_error(self):
        p = OctoPrintPrinter("O", "http://192.0.2.20", "key")
        self.assertEqual(p.temp_readings(), {})
        p._ok, p._last = True, "DISCONNECTED"
        self.assertEqual(p.temp_readings(), {})


if __name__ == "__main__":
    unittest.main(verbosity=1)
