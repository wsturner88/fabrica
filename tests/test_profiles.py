"""Tests for printer profiles (app/profiles.py + app/profiles/*.json): load, validate, resolve, match order,
per-printer overrides. Pure Python, no printers, no network.

    python3 tests/test_profiles.py
"""
import copy
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

import profiles  # noqa: E402


def quiet(*a, **k):
    pass


def reg():
    return profiles.Registry(log=quiet)


class ShippedFiles(unittest.TestCase):
    def test_every_shipped_file_parses_and_validates(self):
        files = sorted(glob.glob(os.path.join(APP, "profiles", "*.json")))
        self.assertGreaterEqual(len(files), 16)
        for f in files:
            with open(f, encoding="utf-8") as fh:
                prof = profiles.clean_profile(json.load(fh))
            self.assertEqual(prof["id"] + ".json", os.path.basename(f), f)

    def test_the_contract_list_is_there_and_nothing_failed_to_load(self):
        r = reg()
        self.assertEqual(r.errors, [])
        want = {"bambu-p1s", "bambu-a1", "bambu-a1-mini", "bambu-x1c", "bambu-h2d", "prusa-mk4s", "prusa-mk4",
                "prusa-core-one", "prusa-mk3s-mmu3", "prusa-xl", "prusa-mini", "voron-2.4",
                "generic-bambu", "generic-prusalink", "generic-octoprint", "generic-klipper"}
        self.assertEqual(want - set(r.profiles), set())

    def test_every_type_has_a_generic_with_unknowns(self):
        r = reg()
        for t in ("bambu", "prusalink", "octoprint", "klipper"):
            g = r.profiles[f"generic-{t}"]
            self.assertEqual(g["types"], [t])
            self.assertEqual(g["has"]["nozzles"], 1)
            self.assertIsNone(g["has"]["light"])
            self.assertIsNone(g["has"]["chamber"])

    def test_known_facts(self):
        r = reg()
        self.assertIs(r.profiles["prusa-core-one"]["has"]["light"], True)
        self.assertEqual(r.profiles["bambu-p1s"]["has"]["chamber"], "none")
        self.assertEqual(r.profiles["bambu-h2d"]["has"]["nozzles"], 2)
        self.assertEqual(r.profiles["bambu-h2d"]["has"]["chamber"], "heater")
        self.assertEqual(r.profiles["bambu-x1c"]["has"]["chamber"], "sensor")

    def test_dockerfile_copies_the_app_folder_and_dockerignore_keeps_profiles(self):
        root = os.path.join(HERE, "..")
        with open(os.path.join(root, "Dockerfile")) as f:
            self.assertIn("COPY app/ /app/", f.read())            # app/profiles/ rides along
        with open(os.path.join(root, ".dockerignore")) as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        for ln in lines:
            self.assertNotIn("profiles", ln)
            self.assertFalse(ln.lstrip("!").startswith("app") and "json" in ln, ln)
        self.assertNotIn("*.json", lines)


class Validation(unittest.TestCase):
    def good(self, **k):
        d = {"id": "x-one", "label": "X One", "types": ["prusalink"], "matches": ["xone"], "has": {"light": True}}
        d.update(k)
        return d

    def test_minimal_profile_fills_unknowns(self):
        p = profiles.clean_profile(self.good())
        self.assertEqual(set(p["has"]), set(profiles.HAS_KEYS))
        self.assertIsNone(p["has"]["chamber"])
        self.assertIs(p["has"]["light"], True)
        self.assertFalse(p["verified"])

    def test_bad_profiles_are_refused(self):
        bad = [None, [], {}, self.good(id="Bad Id"), self.good(id=""), self.good(label=""), self.good(types=[]),
               self.good(types="prusalink"), self.good(matches=[1]), self.good(has={"light": "yes"}),
               self.good(has={"chamber": "warm"}), self.good(has={"nozzles": 0}), self.good(has={"nozzles": True}),
               self.good(has={"bed_mm": [250]}), self.good(has={"bed_mm": ["a", "b"]}),
               self.good(has={"filament": {"system": "magic", "slots": 1}}), self.good(has={"filament": {"slots": 0}}),
               self.good(has={"spaceship": True}), self.good(has="nope")]
        for b in bad:
            with self.assertRaises(profiles.ProfileError, msg=repr(b)):
                profiles.clean_profile(b)

    def test_overrides_strict(self):
        ok = profiles.clean_overrides({"light": False, "chamber": "sensor", "bed_mm": [300, 300], "nozzles": 2,
                                       "sensor_labels": {"temperature_sensor A": " Hotend air "},
                                       "chamber_sensor": "temperature_sensor C"})
        self.assertEqual(ok["sensor_labels"], {"temperature_sensor A": "Hotend air"})
        self.assertEqual(ok["chamber_sensor"], "temperature_sensor C")
        self.assertIs(ok["light"], False)
        self.assertEqual(profiles.clean_overrides({"chamber_sensor": ""}), {"chamber_sensor": ""})   # "" = none, kept
        self.assertEqual(profiles.clean_overrides({}), {})
        self.assertEqual(profiles.clean_overrides(None), {})
        self.assertEqual(profiles.clean_overrides({"light": None, "sensor_labels": {"a": ""}}), {})  # None / empty = no override

    def test_overrides_reject_unknown_keys_and_bad_values(self):
        for bad in ({"colour": "red"}, {"light": "on"}, {"nozzles": 99}, {"chamber": "x"}, {"sensor_labels": []},
                    {"sensor_labels": {"a": "x" * 41}}, {"sensor_labels": {"a": 5}}, {"chamber_sensor": 3},
                    {"filament": {"system": "ams", "slots": "four"}}, "light", [1]):
            with self.assertRaises(profiles.ProfileError, msg=repr(bad)):
                profiles.clean_overrides(bad)

    def test_overrides_lenient_when_read_from_a_stored_record(self):
        got = profiles.clean_overrides({"light": "oops", "chamber": "heater", "bogus": 1, "sensor_labels": 7}, strict=False)
        self.assertEqual(got, {"chamber": "heater"})
        self.assertEqual(profiles.clean_overrides("junk", strict=False), {})


class LoadingNeverCrashes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="profiles-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_bad_files_are_logged_and_skipped(self):
        logged = []
        self.write("good.json", json.dumps({"id": "good", "label": "Good", "types": ["bambu"]}))
        self.write("broken.json", "{ not json")
        self.write("wrong.json", json.dumps({"id": "Wrong Id", "label": "x", "types": ["bambu"]}))
        self.write("a-first.json", json.dumps({"id": "good", "label": "First", "types": ["bambu"]}))
        self.write("notes.txt", "ignored")
        r = profiles.Registry(self.dir, log=logged.append)
        self.assertEqual(list(r.profiles), ["good"])
        self.assertEqual(r.profiles["good"]["label"], "First")
        self.assertEqual(sorted(n for n, _ in r.errors), ["broken.json", "good.json", "wrong.json"])
        self.assertEqual(len(logged), 3)

    def test_missing_folder_and_empty_folder_still_resolve_to_a_generic(self):
        for d in (os.path.join(self.dir, "nope"), self.dir):
            r = profiles.Registry(d, log=quiet)
            p = r.resolve({"type": "prusalink", "name": "Any", "model": "Core One"})
            self.assertEqual(p["id"], "generic-prusalink")
            self.assertEqual(p["has"]["nozzles"], 1)
            self.assertTrue(p["auto"])


class Resolution(unittest.TestCase):
    def setUp(self):
        self.r = reg()

    @staticmethod
    def raw(path):
        with open(path, "rb") as fh:
            return fh.read()

    def pid(self, **rec):
        return self.r.resolve(rec)["id"]

    def test_match_by_model_or_name_lowercased(self):
        self.assertEqual(self.pid(type="prusalink", model="Original Prusa CORE ONE", name="x"), "prusa-core-one")
        self.assertEqual(self.pid(type="prusalink", model="", name="Shop CoreOne"), "prusa-core-one")
        self.assertEqual(self.pid(type="prusalink", model="COREONE", name="Left"), "prusa-core-one")
        self.assertEqual(self.pid(type="bambu", model="Bambu Lab P1S", name="Bench"), "bambu-p1s")
        self.assertEqual(self.pid(type="bambu", model="Bambu Lab H2D", name="Big"), "bambu-h2d")
        self.assertEqual(self.pid(type="moonraker", model="", name="Voron"), "voron-2.4")

    def test_longest_match_wins_so_the_specific_model_beats_the_short_one(self):
        self.assertEqual(self.pid(type="bambu", model="Bambu Lab A1 mini", name="A"), "bambu-a1-mini")
        self.assertEqual(self.pid(type="bambu", model="Bambu Lab A1", name="A"), "bambu-a1")
        self.assertEqual(self.pid(type="prusalink", model="MK4S", name="a"), "prusa-mk4s")
        self.assertEqual(self.pid(type="prusalink", model="MK4", name="a"), "prusa-mk4")
        self.assertEqual(self.pid(type="bambu", model="Bambu Lab X1 Carbon", name="a"), "bambu-x1c")

    def test_a_profile_only_matches_its_own_type(self):
        self.assertEqual(self.pid(type="bambu", model="Core One", name="a"), "generic-bambu")
        self.assertEqual(self.pid(type="octoprint", model="Core One", name="a"), "prusa-core-one")
        self.assertEqual(self.pid(type="moonraker", model="Core One", name="a"), "generic-klipper")

    def test_unknown_falls_back_to_the_generic_of_its_type(self):
        for t, g in (("bambu", "generic-bambu"), ("prusalink", "generic-prusalink"),
                     ("octoprint", "generic-octoprint"), ("moonraker", "generic-klipper")):
            res = self.r.resolve({"type": t, "model": "Mystery", "name": "Zed"})
            self.assertEqual((res["id"], res["auto"]), (g, True))
        self.assertEqual(self.pid(), "generic-")            # garbage in: still an answer, no crash
        self.assertEqual(self.r.resolve(None)["overridden"], [])

    def test_a_saved_profile_wins_over_matching(self):
        res = self.r.resolve({"type": "prusalink", "model": "Core One", "name": "a", "profile_id": "prusa-mk4s"})
        self.assertEqual((res["id"], res["auto"]), ("prusa-mk4s", False))
        self.assertIs(res["has"]["light"], False)

    def test_a_saved_profile_that_is_gone_or_for_another_type_is_ignored(self):
        for pid in ("deleted-one", "bambu-p1s", 5, ""):
            res = self.r.resolve({"type": "prusalink", "model": "Core One", "name": "a", "profile_id": pid})
            self.assertEqual((res["id"], res["auto"]), ("prusa-core-one", True), pid)

    def test_overrides_are_laid_on_top_and_listed(self):
        res = self.r.resolve({"type": "prusalink", "model": "Core One", "name": "a",
                              "profile_overrides": {"light": False, "bed_max_c": 120, "sensor_labels": {"a": "b"}}})
        self.assertIs(res["has"]["light"], False)
        self.assertEqual(res["overridden"], ["light"])          # bed_max_c equals the profile: not a change
        self.assertEqual(res["has"]["bed_max_c"], 120)
        self.assertFalse(res["verified"])

    def test_overrides_never_leak_into_another_printer_or_the_profile(self):
        before = copy.deepcopy(self.r.profiles)
        files_before = {f: self.raw(f) for f in glob.glob(os.path.join(APP, "profiles", "*.json"))}
        a = {"type": "prusalink", "model": "Core One", "name": "A", "profile_overrides": {"light": False, "chamber": "none"}}
        b = {"type": "prusalink", "model": "Core One", "name": "B"}
        ra = self.r.resolve(a)
        ra["has"]["bed_mm"][0] = 1                                # mutating a result must not reach the registry either
        ra["has"]["filament"]["slots"] = 9
        rb = self.r.resolve(b)
        self.assertIs(ra["has"]["light"], False)
        self.assertIs(rb["has"]["light"], True)
        self.assertEqual(rb["has"]["chamber"], "sensor")
        self.assertEqual(rb["overridden"], [])
        self.assertEqual(rb["has"]["bed_mm"], [250, 220])
        self.assertEqual(self.r.profiles, before)
        for f, data in files_before.items():
            self.assertEqual(self.raw(f), data)

    def test_public_list_shape_and_order(self):
        rows = self.r.public_list()
        self.assertEqual([r["maker"].casefold() for r in rows], sorted(r["maker"].casefold() for r in rows))
        for row in rows:
            self.assertEqual(set(row), {"id", "label", "maker", "types", "printer_types", "has", "verified", "notes"})
        klip = next(r for r in rows if r["id"] == "generic-klipper")
        self.assertEqual((klip["types"], klip["printer_types"]), (["klipper"], ["moonraker"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
