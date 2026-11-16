"""Tests for the printer-profile API and storage: GET /api/profiles, the `profile` block on GET /api/printers, PUT validation,
per-printer overrides kept in the Hub's own data, temps + sensors on GET /api/fleet, the light reason text in the controls,
and an old printers.json that knows nothing about profiles.

    python3 tests/test_profiles_api.py          (needs the app's dependencies: mcp, starlette, httpx, paho-mqtt)

Everything is local: the server module is imported with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointing at a throwaway temp
directory; printers are in-process fakes or plain records in the store; nothing contacts a printer or the LAN.
"""
import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="profiles-api-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)
os.environ.pop("BAMBU_MCP_ALLOWED_HOSTS", None)

import controls  # noqa: E402
import fleet_store  # noqa: E402
import profiles  # noqa: E402
import security  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover - the app's deps are not installed
    HAVE = False
    _ERR = e

CLIENT = None


def setUpModule():
    global CLIENT
    if HAVE:
        CLIENT = TestClient(server.mcp.streamable_http_app())
        CLIENT.__enter__()


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    shutil.rmtree(_TMP, ignore_errors=True)


class FakeDriver:
    """A printer in-process: kind / model like a real driver, readings set by the test."""
    has_camera = False
    uses_files_api = False

    def __init__(self, name, kind, model="", nozzle=210.0, readings=None, light=None):
        self.name, self.kind, self.model = name, kind, model
        self._nozzle, self._readings, self._light = nozzle, readings or {}, light

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": "IDLE", "job": "", "percent": 0, "remaining_min": None,
                "nozzle_c": self._nozzle, "nozzle_target_c": 0.0, "bed_c": 58.1, "bed_target_c": 58.0, "kind": self.kind}

    def temp_readings(self):
        return self._readings

    def control_caps(self):
        caps = controls.all_unsupported("No controls in this fake.")
        if self._light:
            caps["light"] = self._light
        return caps

    def refresh(self, wait=0):
        pass

    def ams_trays(self):
        return []


def reset_fleet():
    with server.STORE._lock:
        server.STORE._records[:] = []
        server.STORE._secrets.clear()
        server.STORE._save()
    server.PRINTERS.clear()
    server._TEMPS_HELD.clear()


def add(name, type_, model="", fake=None, **rec):
    base = {"name": name, "type": type_, "model": model}
    base["url" if type_ != "bambu" else "ip"] = f"http://192.0.2.{len(name) + 10}" if type_ != "bambu" else f"192.0.2.{len(name) + 10}"
    server.STORE.add({**base, **rec})
    if fake is not None:
        server.PRINTERS[name.lower()] = fake


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class Base(unittest.TestCase):
    def setUp(self):
        server.SETTINGS.update({"security.api_auth": "off"})
        reset_fleet()
        self.c = CLIENT

    def tearDown(self):
        reset_fleet()

    def printers(self):
        return {p["name"]: p for p in self.c.get("/api/printers").json()["printers"]}

    def fleet(self):
        return {p["printer"]: p for p in self.c.get("/api/fleet").json()}


class ProfileList(Base):
    def test_get_profiles_shape_and_order(self):
        r = self.c.get("/api/profiles")
        self.assertEqual(r.status_code, 200)
        rows = r.json()["profiles"]
        self.assertGreaterEqual(len(rows), 16)
        for row in rows:
            for k in ("id", "label", "maker", "types", "has", "verified"):
                self.assertIn(k, row)
            self.assertEqual(set(row["has"]), set(profiles.HAS_KEYS))
        keys = [(x["maker"].casefold(), x["label"].casefold()) for x in rows]
        self.assertEqual(keys, sorted(keys))

    def test_the_route_needs_only_read_access(self):
        self.assertEqual(security.rest_scope("GET", "/api/profiles"), "read")
        self.assertEqual(security.rest_scope("PUT", "/api/printers/x"), "control")


class PrinterProfileBlock(Base):
    def test_auto_profile_block(self):
        add("Shop", "prusalink", "Original Prusa Core One")
        add("Left", "bambu", "Bambu Lab P1S")
        add("Vor", "moonraker", "")
        add("Mystery", "octoprint", "Thing")
        p = self.printers()
        self.assertEqual(p["Shop"]["profile"], {"id": "prusa-core-one", "label": "Prusa Core One", "auto": True,
                                                "has": p["Shop"]["profile"]["has"], "overridden": [], "verified": False, "overrides": {}})
        self.assertIs(p["Shop"]["profile"]["has"]["light"], True)
        self.assertEqual(p["Left"]["profile"]["id"], "bambu-p1s")
        self.assertEqual(p["Mystery"]["profile"]["id"], "generic-octoprint")
        self.assertNotIn("profile_id", p["Shop"]["values"])
        self.assertNotIn("profile_overrides", p["Shop"]["values"])

    def test_klipper_records_use_the_klipper_profiles(self):
        add("Voron", "moonraker", "Voron 2.4")
        add("Other", "moonraker", "")
        p = self.printers()
        self.assertEqual(p["Voron"]["profile"]["id"], "voron-2.4")
        self.assertEqual(p["Other"]["profile"]["id"], "generic-klipper")


class PutValidation(Base):
    def put(self, name, **values):
        return self.c.put(f"/api/printers/{name}", json={"values": values})

    def test_save_a_profile_then_back_to_automatic(self):
        add("Shop", "prusalink", "Core One")
        r = self.put("Shop", profile_id="prusa-mk4s")
        self.assertEqual(r.status_code, 200, r.text)
        blk = r.json()["printer"]["profile"]
        self.assertEqual((blk["id"], blk["auto"]), ("prusa-mk4s", False))
        self.assertEqual(r.json()["changed"], ["profile_id"])
        self.assertEqual(server.STORE.get("Shop")["profile_id"], "prusa-mk4s")
        r = self.put("Shop", profile_id=None)
        self.assertEqual((r.status_code, r.json()["printer"]["profile"]["id"], r.json()["printer"]["profile"]["auto"]),
                         (200, "prusa-core-one", True))
        self.assertNotIn("profile_id", server.STORE.get("Shop"))             # automatic = nothing stored

    def test_the_two_fields_may_also_sit_next_to_values(self):
        add("Shop", "prusalink", "Core One")
        r = self.c.put("/api/printers/Shop", json={"profile_id": "prusa-mk4", "profile_overrides": {"light": True}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(server.STORE.get("Shop")["profile_overrides"], {"light": True})

    def test_overrides_replace_and_empty_clears(self):
        add("Shop", "prusalink", "Core One")
        r = self.put("Shop", profile_overrides={"light": False, "sensor_labels": {"temperature_sensor A": "Air"}, "chamber_sensor": "temperature_sensor A"})
        self.assertEqual(r.status_code, 200, r.text)
        blk = r.json()["printer"]["profile"]
        self.assertEqual(blk["overridden"], ["light"])
        self.assertIs(blk["has"]["light"], False)
        self.assertEqual(blk["overrides"]["sensor_labels"], {"temperature_sensor A": "Air"})
        self.put("Shop", profile_overrides={"chamber": "none"})                # replaces the whole set
        self.assertEqual(server.STORE.get("Shop")["profile_overrides"], {"chamber": "none"})
        r = self.put("Shop", profile_overrides={})
        self.assertEqual(r.json()["printer"]["profile"]["overridden"], [])
        self.assertNotIn("profile_overrides", server.STORE.get("Shop"))

    def test_bad_input_is_a_400_in_plain_english_and_changes_nothing(self):
        add("Shop", "prusalink", "Core One")
        self.put("Shop", profile_overrides={"light": False})
        before = dict(server.STORE.get("Shop"))
        bad = [{"profile_id": "no-such-profile"}, {"profile_id": "bambu-p1s"}, {"profile_id": 5}, {"profile_id": ["a"]},
               {"profile_overrides": {"colour": "red"}}, {"profile_overrides": {"light": "yes"}},
               {"profile_overrides": {"chamber": "warm"}}, {"profile_overrides": {"nozzles": 99}},
               {"profile_overrides": {"sensor_labels": {"a": "x" * 50}}}, {"profile_overrides": [1, 2]},
               {"profile_overrides": "light"}, {"profile_id": "prusa-mk4", "profile_overrides": {"bed_mm": [1]}}]
        for values in bad:
            r = self.put("Shop", **values)
            self.assertEqual(r.status_code, 400, values)
            self.assertIn("error", r.json())
            self.assertTrue(r.json()["error"].startswith("Printer profile:"), r.json())
        after = server.STORE.get("Shop")
        after.pop("updated_at", None)
        before.pop("updated_at", None)
        self.assertEqual(after, before)

    def test_unknown_printer_is_404(self):
        self.assertEqual(self.put("Nope", profile_id="prusa-mk4").status_code, 404)

    def test_overriding_one_printer_changes_no_other_printer_and_no_file(self):
        files = {f: open(f, "rb").read() for f in glob.glob(os.path.join(APP, "profiles", "*.json"))}
        add("A", "prusalink", "Core One")
        add("B", "prusalink", "Core One")
        self.put("A", profile_overrides={"light": False, "nozzle_max_c": 300})
        p = self.printers()
        self.assertIs(p["A"]["profile"]["has"]["light"], False)
        self.assertIs(p["B"]["profile"]["has"]["light"], True)
        self.assertEqual(p["B"]["profile"]["overridden"], [])
        self.assertEqual(p["B"]["profile"]["has"]["nozzle_max_c"], 290)
        self.assertNotIn("profile_overrides", server.STORE.get("B"))
        for f, data in files.items():
            with open(f, "rb") as fh:
                self.assertEqual(fh.read(), data, f)
        # a restart (a new store on the same folder) still has it
        again = fleet_store.FleetStore(server.DATA_DIR)
        self.assertEqual(again.get("A")["profile_overrides"], {"light": False, "nozzle_max_c": 300})

    def test_an_edit_of_the_connection_keeps_the_profile_settings(self):
        add("Shop", "prusalink", "Core One")
        self.put("Shop", profile_id="prusa-mk4s", profile_overrides={"light": True})
        server.STORE.update("Shop", {"model": "Core One v2"})
        rec = server.STORE.get("Shop")
        self.assertEqual((rec["profile_id"], rec["profile_overrides"]), ("prusa-mk4s", {"light": True}))


class OldFiles(unittest.TestCase):
    def test_a_printers_json_without_profile_fields_loads_unchanged(self):
        d = tempfile.mkdtemp(prefix="profiles-old-")
        self.addCleanup(shutil.rmtree, d, True)
        old = {"printers": [
            {"name": "Old1", "type": "prusalink", "url": "http://192.0.2.5", "model": "MK4S", "groups": ["Shop"], "jog_flip_y": True},
            {"name": "Old2", "type": "bambu", "ip": "192.0.2.6", "serial": "01P00A000000001", "model": "Bambu Lab P1S"},
            {"name": "Old3", "type": "moonraker", "url": "http://192.0.2.7"}]}
        path = os.path.join(d, "printers.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(old, f)
        store = fleet_store.FleetStore(d)
        mgr = fleet_store.FleetManager(store, {}, profile_registry=profiles.Registry(log=lambda *a: None))
        rows = {r["name"]: r for r in mgr.list_public()}
        self.assertEqual(rows["Old1"]["profile"]["id"], "prusa-mk4s")
        self.assertEqual(rows["Old2"]["profile"]["id"], "bambu-p1s")
        self.assertEqual(rows["Old3"]["profile"]["id"], "generic-klipper")
        self.assertTrue(all(r["profile"]["auto"] and r["profile"]["overridden"] == [] for r in rows.values()))
        for rec in store.records():
            self.assertNotIn("profile_id", rec)
            self.assertNotIn("profile_overrides", rec)
        self.assertEqual(rows["Old1"]["groups"], ["Shop"])
        self.assertTrue(rows["Old1"]["jog_flip_y"])

    def test_a_hand_edited_record_with_junk_profile_fields_never_breaks_a_page(self):
        d = tempfile.mkdtemp(prefix="profiles-junk-")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "printers.json"), "w", encoding="utf-8") as f:
            json.dump({"printers": [{"name": "J", "type": "prusalink", "url": "http://192.0.2.5", "model": "MK4S",
                                     "profile_id": 7, "profile_overrides": ["x", {"light": "maybe"}]}]}, f)
        mgr = fleet_store.FleetManager(fleet_store.FleetStore(d), {}, profile_registry=profiles.Registry(log=lambda *a: None))
        blk = mgr.list_public()[0]["profile"]
        self.assertEqual((blk["id"], blk["auto"], blk["overridden"]), ("prusa-mk4s", True, []))


class FleetTemps(Base):
    def test_every_entry_has_temps_and_sensors_and_the_old_keys_stay(self):
        add("Plain", "octoprint", "Thing", fake=FakeDriver("Plain", "octoprint", "Thing"))
        e = self.fleet()["Plain"]
        self.assertEqual((e["nozzle_c"], e["nozzle_target_c"], e["bed_c"], e["bed_target_c"]), (210.0, 0.0, 58.1, 58.0))
        self.assertEqual([(t["id"], t["label"], t["kind"], t["c"], t["target_c"]) for t in e["temps"]],
                         [("nozzle", "Nozzle", "nozzle", 210.0, 0.0), ("bed", "Bed", "bed", 58.1, 58.0)])
        self.assertEqual(e["sensors"], [])

    def test_a_printer_with_nothing_to_show_gets_empty_lists(self):
        class Dead(FakeDriver):
            def snapshot(self):
                return {"printer": self.name, "connected": False, "state": "UNKNOWN", "nozzle_c": None, "bed_c": None}
        server.PRINTERS["dead"] = Dead("Dead", "octoprint")
        e = self.fleet()["Dead"]
        self.assertEqual((e["temps"], e["sensors"]), ([], []))

    def test_a_driver_without_temp_readings_or_one_that_raises_is_fine(self):
        class NoReadings(FakeDriver):
            temp_readings = None
        class Boom(FakeDriver):
            def temp_readings(self):
                raise RuntimeError("boom")
        server.PRINTERS["nr"] = NoReadings("NR", "prusalink")
        server.PRINTERS["bm"] = Boom("BM", "prusalink")
        f = self.fleet()
        self.assertEqual([t["kind"] for t in f["NR"]["temps"]], ["nozzle", "bed"])
        self.assertEqual([t["kind"] for t in f["BM"]["temps"]], ["nozzle", "bed"])        # a failing driver read: snapshot values, no 500

    def test_klipper_chamber_sensor_and_second_nozzle_end_to_end(self):
        readings = {"nozzles": [{"c": 215.2, "target": 215.0}, {"c": 24.0, "target": 0.0}],
                    "sensors": [{"id": "temperature_sensor chamber", "kind": "temperature_sensor", "name": "chamber", "c": 31.0, "target": None},
                                {"id": "temperature_sensor EBB_NTC", "kind": "temperature_sensor", "name": "EBB_NTC", "c": 34.7, "target": None}]}
        add("Vor", "moonraker", "Voron 2.4", fake=FakeDriver("Vor", "moonraker", "Voron 2.4", readings=readings))
        e = self.fleet()["Vor"]
        self.assertEqual([(t["id"], t["label"]) for t in e["temps"]],
                         [("nozzle", "Nozzle 1"), ("nozzle2", "Nozzle 2"), ("bed", "Bed"), ("chamber", "Chamber")])
        self.assertEqual(e["sensors"], [{"id": "temperature_sensor EBB_NTC", "label": "Toolhead board", "role": "toolhead", "c": 34.7}])

    def test_the_owners_label_flows_through_a_put(self):
        readings = {"sensors": [{"id": "temperature_sensor EBB_NTC", "kind": "temperature_sensor", "name": "EBB_NTC", "c": 34.7}]}
        add("Vor", "moonraker", "Voron 2.4", fake=FakeDriver("Vor", "moonraker", "Voron 2.4", readings=readings))
        r = self.c.put("/api/printers/Vor", json={"values": {"profile_overrides": {"sensor_labels": {"temperature_sensor EBB_NTC": "Toolhead air"}}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.fleet()["Vor"]["sensors"][0]["label"], "Toolhead air")

    def test_bambu_chamber_follows_the_profile(self):
        reading = {"chamber": {"c": 33.0, "target": None}}
        add("P1", "bambu", "Bambu Lab P1S", fake=FakeDriver("P1", "bambu", "Bambu Lab P1S", readings=reading))
        add("X1", "bambu", "Bambu Lab X1 Carbon", fake=FakeDriver("X1", "bambu", "Bambu Lab X1 Carbon", readings=reading))
        f = self.fleet()
        self.assertNotIn("chamber", [t["kind"] for t in f["P1"]["temps"]])
        self.assertIn("chamber", [t["kind"] for t in f["X1"]["temps"]])
        self.c.put("/api/printers/P1", json={"values": {"profile_overrides": {"chamber": "sensor"}}})     # a P1S with a sensor added
        self.assertIn("chamber", [t["kind"] for t in self.fleet()["P1"]["temps"]])

    def test_a_blip_keeps_the_cards_that_were_showing(self):
        readings = {"nozzles": [{"c": 215.0, "target": 215.0}, {"c": 24.0, "target": 0.0}]}
        drv = FakeDriver("Vor", "moonraker", "Voron 2.4", readings=readings)
        add("Vor", "moonraker", "Voron 2.4", fake=drv)
        self.assertEqual(len(self.fleet()["Vor"]["temps"]), 3)
        drv._readings = {}                                                     # the driver lost its poll...
        drv.snapshot = lambda: {"printer": "Vor", "connected": False, "state": "UNKNOWN", "nozzle_c": None, "bed_c": None}
        e = self.fleet()["Vor"]                                                # ...the Hub still shows the last good screen
        self.assertTrue(e.get("reconnecting"))
        self.assertEqual(len(e["temps"]), 3)

    def test_single_printer_route_carries_the_same_cards(self):
        add("Plain", "octoprint", "Thing", fake=FakeDriver("Plain", "octoprint", "Thing"))
        snap = self.c.get("/api/printer/Plain").json()["snapshot"]
        self.assertEqual([t["kind"] for t in snap["temps"]], ["nozzle", "bed"])
        self.assertEqual(snap["sensors"], [])


class LightReason(Base):
    NOLIGHT = {"supported": False, "state": None, "reason": "PrusaLink's v1 API has no light"}

    def light(self, drv):
        return controls.describe(drv)["light"]

    def test_profile_with_a_light_driver_cannot_switch_it(self):
        add("Shop", "prusalink", "Core One")
        drv = FakeDriver("Shop", "prusalink", "Core One", light=self.NOLIGHT)
        server.PRINTERS["shop"] = drv
        got = self.light(drv)
        self.assertEqual(got, {"supported": False, "state": None,
                               "reason": "This printer has a light, but PrusaLink can't switch it - use the printer's own screen."})

    def test_profile_with_no_light(self):
        add("Mk", "prusalink", "MK4S")
        drv = FakeDriver("Mk", "prusalink", "MK4S", light=self.NOLIGHT)
        server.PRINTERS["mk"] = drv
        self.assertEqual(self.light(drv), {"supported": False, "state": None, "reason": "This printer has no light."})

    def test_connection_names_in_the_sentence(self):
        for kind, word in (("octoprint", "OctoPrint"), ("moonraker", "Klipper")):
            add(f"Lt-{kind}", kind if kind != "moonraker" else "moonraker", "", profile_overrides={"light": True})
            drv = FakeDriver(f"Lt-{kind}", kind, "", light=self.NOLIGHT)
            server.PRINTERS[drv.name.lower()] = drv
            self.assertIn(f"but {word} can't switch it", self.light(drv)["reason"])

    def test_bambu_with_light_false_is_unsupported_and_the_action_is_refused(self):
        add("P1", "bambu", "Bambu Lab P1S", profile_overrides={"light": False})
        drv = FakeDriver("P1", "bambu", "Bambu Lab P1S", light={"supported": True, "state": "off"})
        server.PRINTERS["p1"] = drv
        self.assertEqual(self.light(drv), {"supported": False, "state": None, "reason": "This printer has no light."})
        status, payload = server._control("P1", "light_on")
        self.assertEqual(status, 422, payload)

    def test_bambu_that_has_a_light_stays_switchable_and_unknown_changes_nothing(self):
        add("P1", "bambu", "Bambu Lab P1S")
        drv = FakeDriver("P1", "bambu", "Bambu Lab P1S", light={"supported": True, "state": "off"})
        server.PRINTERS["p1"] = drv
        self.assertTrue(self.light(drv)["supported"])
        add("A1", "bambu", "Bambu Lab A1")                                       # light unknown in the profile
        drv2 = FakeDriver("A1", "bambu", "Bambu Lab A1", light={"supported": True, "state": "off"})
        server.PRINTERS["a1"] = drv2
        self.assertTrue(self.light(drv2)["supported"])
        add("G", "octoprint", "Mystery")
        drv3 = FakeDriver("G", "octoprint", "Mystery", light={"supported": False, "state": None, "reason": "OctoPrint has no standard light API"})
        server.PRINTERS["g"] = drv3
        self.assertEqual(self.light(drv3)["reason"], "OctoPrint has no standard light API")      # unknown: the driver's words stay

    def test_a_klipper_that_cannot_be_reached_keeps_its_own_reason(self):
        add("Vor", "moonraker", "Voron 2.4")
        drv = FakeDriver("Vor", "moonraker", "Voron 2.4", light={"supported": False, "state": None, "reason": "Vor is not reachable.", "transient": True})
        server.PRINTERS["vor"] = drv
        self.assertEqual(self.light(drv)["reason"], "Vor is not reachable.")

    def test_without_the_profile_hook_the_drivers_words_are_used(self):
        saved, controls.profile_has = controls.profile_has, None
        try:
            drv = FakeDriver("Shop", "prusalink", "Core One", light=self.NOLIGHT)
            self.assertEqual(self.light(drv)["reason"], self.NOLIGHT["reason"])
        finally:
            controls.profile_has = saved


if __name__ == "__main__":
    unittest.main(verbosity=1)
