"""Tests for printer groups: the labels on printers ("Production", "Workshop"), the REST routes and MCP tools that
manage them, and the history / cost filters and the By-group summary that use them.

    python3 tests/test_groups.py          (needs the app's dependencies: mcp, starlette, httpx, paho-mqtt)

Everything is local: the server module is imported with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointing at a throwaway
temp directory (never a real config or fleet), printers are in-process fakes or plain records in the store, the job
log is SQLite in that temp directory, and nothing contacts a printer, the Internet or the LAN. The browser test loads
the real dashboard.html into headless Chrome / Edge with a stubbed `fetch` (skipped when no browser is found).
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="groups-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)
os.environ.pop("BAMBU_MCP_ALLOWED_HOSTS", None)

import costs  # noqa: E402
import fleet_store  # noqa: E402
import joblog  # noqa: E402
import security  # noqa: E402
import settings_store  # noqa: E402

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
        CLIENT.__enter__()          # MCP's session manager needs the app's lifespan running


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    shutil.rmtree(_TMP, ignore_errors=True)


class FakePrinter:
    kind = "fake"
    has_camera = False
    uses_files_api = False

    def __init__(self, name):
        self.name = name

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": "IDLE", "job": "", "percent": 0,
                "remaining_min": None}

    def refresh(self, wait=0):
        pass

    def ams_trays(self):
        return []


# ====================================================================== validation

class ValidationTests(unittest.TestCase):
    def test_clean_group_tidies_whitespace(self):
        self.assertEqual(fleet_store.clean_group("  Production  "), "Production")
        self.assertEqual(fleet_store.clean_group("Print   farm \t east"), "Print farm east")
        self.assertEqual(fleet_store.clean_group("   "), "")
        self.assertEqual(fleet_store.clean_group("Café 3D"), "Café 3D")         # printable non-ASCII is fine

    def test_clean_group_rejects(self):
        for bad in ("x" * 33, "Line\x00break", "zero​width", "a/b", "a\\b", "a,b", 5, None, ["a"]):
            with self.assertRaises(fleet_store.FleetError, msg=repr(bad)):
                fleet_store.clean_group(bad)
        self.assertEqual(fleet_store.clean_group("x" * 32), "x" * 32)            # exactly the limit is fine

    def test_normalize_trims_dedupes_case_insensitively_and_keeps_order(self):
        self.assertEqual(fleet_store.normalize_groups([" Production", "workshop", "PRODUCTION", "", "  "]),
                         ["Production", "workshop"])
        self.assertEqual(fleet_store.normalize_groups(None), [])
        self.assertEqual(fleet_store.normalize_groups([]), [])

    def test_normalize_accepts_one_string_and_a_comma_list(self):
        self.assertEqual(fleet_store.normalize_groups("Production"), ["Production"])
        self.assertEqual(fleet_store.normalize_groups("Production, Workshop"), ["Production", "Workshop"])

    def test_normalize_limits_and_types(self):
        eight = [f"G{i}" for i in range(8)]
        self.assertEqual(fleet_store.normalize_groups(eight), eight)
        with self.assertRaises(fleet_store.FleetError):
            fleet_store.normalize_groups(eight + ["G8"])
        with self.assertRaises(fleet_store.FleetError):
            fleet_store.normalize_groups({"a": 1})
        with self.assertRaises(fleet_store.FleetError):
            fleet_store.normalize_groups([1, 2])

    def test_normalize_adopts_the_fleet_spelling(self):
        self.assertEqual(fleet_store.normalize_groups(["production"], {"production": "Production"}), ["Production"])

    def test_record_groups_is_lenient_about_a_hand_edited_file(self):
        self.assertEqual(fleet_store.record_groups({"name": "a"}), [])
        self.assertEqual(fleet_store.record_groups({"groups": "oops"}), [])
        self.assertEqual(fleet_store.record_groups({"groups": ["A", 3, None, "a", " B "]}), ["A", "B"])


# ====================================================================== the store

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="groups-store-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.st = fleet_store.FleetStore(self.dir)
        for n in ("Alpha", "Beta", "Gamma", "Delta"):
            self.st.add({"name": n, "type": "octoprint", "url": f"http://{n.lower()}.invalid"}, {"api_key": "k" + n})

    def test_a_printer_has_no_groups_by_default(self):
        self.assertEqual(self.st.groups_by_printer()["Alpha"], [])
        self.assertEqual(self.st.group_index(), [])

    def test_set_and_read_back(self):
        self.assertEqual(self.st.set_groups("alpha", ["Production", "Workshop"]), ["Production", "Workshop"])
        self.assertEqual(self.st.get("Alpha")["groups"], ["Production", "Workshop"])
        self.st.set_groups("Beta", ["workshop", "Prototypes"])          # joins the existing spelling
        idx = {g["name"]: g["printers"] for g in self.st.group_index()}
        self.assertEqual(idx, {"Production": ["Alpha"], "Prototypes": ["Beta"], "Workshop": ["Alpha", "Beta"]})
        self.assertEqual(self.st.get("Beta")["groups"], ["Workshop", "Prototypes"])

    def test_clearing_removes_the_key_and_the_group_disappears(self):
        self.st.set_groups("Alpha", ["Production"])
        self.assertEqual(self.st.set_groups("Alpha", []), [])
        self.assertNotIn("groups", self.st.get("Alpha"))
        self.assertEqual(self.st.group_index(), [])

    def test_unknown_printer_and_bad_input_change_nothing(self):
        with self.assertRaises(fleet_store.NotFound):
            self.st.set_groups("Nope", ["A"])
        self.st.set_groups("Alpha", ["Production"])
        with self.assertRaises(fleet_store.FleetError):
            self.st.set_groups("Alpha", ["fine", "x" * 40])
        self.assertEqual(self.st.get("Alpha")["groups"], ["Production"])

    def test_persistence_and_the_file_holds_no_secret(self):
        self.st.set_groups("Alpha", ["Production", "Workshop"])
        again = fleet_store.FleetStore(self.dir)
        self.assertEqual(again.get("Alpha")["groups"], ["Production", "Workshop"])
        self.assertEqual(again.get("Beta").get("groups", []), [])
        with open(os.path.join(self.dir, "printers.json")) as fh:
            raw = fh.read()
        self.assertIn('"Production"', raw)
        self.assertNotIn("kAlpha", raw)                     # secrets stay in secrets.json
        with open(os.path.join(self.dir, "secrets.json")) as fh:
            self.assertEqual(json.load(fh)["Alpha"], {"api_key": "kAlpha"})      # ... and are untouched by a group edit

    def test_an_old_printers_json_without_the_field_loads(self):
        d = tempfile.mkdtemp(prefix="groups-old-")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "printers.json"), "w") as fh:
            json.dump({"version": 1, "legacy_import": {"checked": True},
                       "printers": [{"name": "Old", "type": "octoprint", "url": "http://old.invalid", "added_at": "2025-01-01"}]}, fh)
        st = fleet_store.FleetStore(d)
        self.assertEqual(st.groups_by_printer(), {"Old": []})
        mgr = fleet_store.FleetManager(st, {}, log=lambda *a: None)
        self.assertEqual(mgr.list_public()[0]["groups"], [])
        self.assertEqual(mgr.list_groups(), [])

    def test_rename_across_printers(self):
        self.st.set_groups("Alpha", ["Production", "Workshop"])
        self.st.set_groups("Beta", ["production"])
        self.st.set_groups("Gamma", ["Workshop"])
        res = self.st.rename_group("PRODUCTION", "Office")
        self.assertEqual((res["group"], res["printers"], res["merged"]), ("Office", ["Alpha", "Beta"], False))
        self.assertEqual(self.st.get("Alpha")["groups"], ["Office", "Workshop"])      # position kept
        self.assertEqual(self.st.get("Beta")["groups"], ["Office"])
        self.assertEqual(self.st.get("Gamma")["groups"], ["Workshop"])
        self.assertEqual(fleet_store.FleetStore(self.dir).get("Beta")["groups"], ["Office"])       # persisted

    def test_rename_onto_an_existing_group_merges_and_never_doubles(self):
        self.st.set_groups("Alpha", ["Production", "Workshop"])
        self.st.set_groups("Beta", ["Workshop"])
        res = self.st.rename_group("Production", "workshop")
        self.assertTrue(res["merged"])
        self.assertEqual(res["group"], "Workshop")                       # the spelling already in use
        self.assertEqual(self.st.get("Alpha")["groups"], ["Workshop"])
        self.assertEqual([g["name"] for g in self.st.group_index()], ["Workshop"])

    def test_rename_only_the_case_and_errors(self):
        self.st.set_groups("Alpha", ["production"])
        self.assertEqual(self.st.rename_group("production", "Production")["group"], "Production")
        self.assertEqual(self.st.get("Alpha")["groups"], ["Production"])
        with self.assertRaises(fleet_store.NotFound):
            self.st.rename_group("Nothing", "X")
        with self.assertRaises(fleet_store.FleetError):
            self.st.rename_group("Production", "  ")
        with self.assertRaises(fleet_store.FleetError):
            self.st.rename_group("Production", "a/b")

    def test_delete_across_printers_leaves_the_printers(self):
        self.st.set_groups("Alpha", ["Production", "Workshop"])
        self.st.set_groups("Beta", ["Production"])
        res = self.st.delete_group("production")
        self.assertEqual((res["deleted"], res["printers"]), ("Production", ["Alpha", "Beta"]))
        self.assertEqual(self.st.get("Alpha")["groups"], ["Workshop"])
        self.assertNotIn("groups", self.st.get("Beta"))
        self.assertEqual(len(self.st.records()), 4)
        with self.assertRaises(fleet_store.NotFound):
            self.st.delete_group("Production")

    def test_a_failed_save_puts_everything_back(self):
        self.st.set_groups("Alpha", ["Production"])
        real = fleet_store._atomic_write

        def boom(*a, **k):
            raise OSError("disk full")
        fleet_store._atomic_write = boom
        try:
            with self.assertRaises(OSError):
                self.st.rename_group("Production", "Office")
            with self.assertRaises(OSError):
                self.st.set_groups("Beta", ["X"])
        finally:
            fleet_store._atomic_write = real
        self.assertEqual(self.st.get("Alpha")["groups"], ["Production"])
        self.assertNotIn("groups", self.st.get("Beta"))

    def test_resolve_filter_is_a_union_and_never_means_everything_by_accident(self):
        self.st.set_groups("Alpha", ["Production"])
        self.st.set_groups("Beta", ["Production", "Workshop"])
        self.st.set_groups("Gamma", ["Workshop"])
        r = self.st.resolve_filter(None, None)
        self.assertFalse(r["active"])
        r = self.st.resolve_filter(["production"], [])
        self.assertEqual((r["active"], r["printers"], r["groups"]), (True, ["Alpha", "Beta"], ["Production"]))
        r = self.st.resolve_filter(["Production"], ["delta", "Gamma"])
        self.assertEqual(r["printers"], ["Alpha", "Beta", "Delta", "Gamma"])
        r = self.st.resolve_filter(["Typo"], [])
        self.assertEqual((r["active"], r["printers"], r["unknown_groups"]), (True, [], ["Typo"]))
        r = self.st.resolve_filter([], ["Removed-Printer"])          # a printer no longer in the fleet still has history
        self.assertEqual(r["printers"], ["Removed-Printer"])


# ====================================================================== the manager (no network: fake family)

class FakeDriver:
    kind = "fake"

    def __init__(self, vals, state="IDLE"):
        self.name, self.url, self.state = vals["name"], vals.get("url", ""), state
        self.connects = self.stops = 0

    def connect(self):
        self.connects += 1

    def disconnect(self):
        self.stops += 1

    def snapshot(self):
        return {"printer": self.name, "state": self.state, "job": "part.3mf"}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="groups-mgr-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.built = []
        self.state = "IDLE"

        def build(v):
            d = FakeDriver(v, self.state)
            self.built.append(d)
            return d
        fam = fleet_store.Family("fake", "Fake", "d",
                                 [{"name": "name", "label": "Name", "required": True},
                                  {"name": "url", "label": "URL", "type": "text", "required": True}],
                                 lambda v, t: {"ok": True, "model": "M"}, build)
        self.printers = {}
        self.mgr = fleet_store.FleetManager(fleet_store.FleetStore(self.dir), self.printers, families={"fake": fam},
                                            log=lambda *a: None)

    def test_add_with_groups_and_public_view(self):
        res = self.mgr.add("fake", {"name": "P1", "url": "http://p1.invalid", "groups": ["Production", "production", "Workshop"]})
        self.assertEqual(res["printer"]["groups"], ["Production", "Workshop"])
        self.assertNotIn("groups", res["printer"]["values"])                 # a label, not a connection setting
        self.assertEqual(self.mgr.list_public()[0]["groups"], ["Production", "Workshop"])

    def test_add_with_bad_groups_adds_nothing(self):
        with self.assertRaises(fleet_store.FleetError):
            self.mgr.add("fake", {"name": "P1", "url": "http://p1.invalid", "groups": ["x" * 40]})
        self.assertEqual(self.mgr.list_public(), [])
        self.assertEqual(self.built, [])

    def test_groups_only_update_never_reconnects_and_works_while_printing(self):
        self.mgr.add("fake", {"name": "P1", "url": "http://p1.invalid"})
        drv = self.printers["p1"]
        drv.state = "RUNNING"
        res = self.mgr.update("P1", {"groups": ["Production"]})
        self.assertEqual(res["changed"], ["groups"])
        self.assertEqual(res["printer"]["groups"], ["Production"])
        self.assertIs(self.printers["p1"], drv)                              # same driver, never stopped
        self.assertEqual((drv.connects, drv.stops), (1, 0))
        res = self.mgr.set_groups("P1", ["Workshop", "Prototypes"])
        self.assertEqual(res["groups"], ["Workshop", "Prototypes"])
        self.assertEqual((drv.connects, drv.stops), (1, 0))

    def test_update_with_groups_and_a_busy_connection_change_still_refuses_and_changes_nothing(self):
        self.mgr.add("fake", {"name": "P1", "url": "http://p1.invalid"})
        self.printers["p1"].state = "RUNNING"
        with self.assertRaises(fleet_store.Busy):
            self.mgr.update("P1", {"url": "http://elsewhere.invalid", "groups": ["Production"]}, skip_test=True)
        self.assertEqual(self.mgr.list_public()[0]["groups"], [])

    def test_update_with_a_bad_group_changes_nothing_else(self):
        self.mgr.add("fake", {"name": "P1", "url": "http://p1.invalid"})
        with self.assertRaises(fleet_store.FleetError):
            self.mgr.update("P1", {"url": "http://other.invalid", "groups": ["a/b"]}, skip_test=True)
        self.assertEqual(self.mgr.store.get("P1")["url"], "http://p1.invalid")

    def test_a_connection_edit_keeps_the_groups(self):
        self.mgr.add("fake", {"name": "P1", "url": "http://p1.invalid", "groups": ["Production"]})
        self.mgr.update("P1", {"url": "http://moved.invalid"}, skip_test=True)
        self.assertEqual(self.mgr.store.get("P1")["groups"], ["Production"])
        res = self.mgr.update("P1", {"url": "http://moved2.invalid", "groups": []}, skip_test=True)
        self.assertEqual(sorted(res["changed"]), ["groups", "url"])
        self.assertEqual(self.mgr.store.get("P1").get("groups", []), [])

    def test_unknown_printer(self):
        with self.assertRaises(fleet_store.NotFound):
            self.mgr.set_groups("Nope", ["A"])
        with self.assertRaises(fleet_store.NotFound):
            self.mgr.update("Nope", {"groups": ["A"]})


# ====================================================================== job log filters

T_END = time.time() - 3600


def add_job(printer, result="finished", grams=10.0, hours=1.0, file="part.gcode.3mf", parts=1, ago=0):
    end = T_END - ago
    server.JOBLOG._insert(printer=printer, kind="fake", file=file, file_key=costs.file_key(file),
                          started_at=end - hours * 3600, ended_at=end, duration_s=hours * 3600, result=result,
                          percent=100 if result == "finished" else 40, grams_used=grams, grams_est=grams,
                          material="PLA", filaments="[]", parts=parts, pauses=0, source="event")


def reset_fleet():
    with server.STORE._lock:
        server.STORE._records[:] = []
        server.STORE._secrets.clear()
        server.STORE._save()
    server.PRINTERS.clear()
    server.JOBLOG._exec("DELETE FROM jobs")


def put_printer(name, groups=None, fake=True):
    server.STORE.add({"name": name, "type": "octoprint", "url": f"http://{name.lower()}.invalid"})
    if groups:
        server.STORE.set_groups(name, groups)
    if fake:
        server.PRINTERS[name.lower()] = FakePrinter(name)


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class Base(unittest.TestCase):
    def setUp(self):
        server.SETTINGS.update({"security.api_auth": "off"})
        reset_fleet()
        self.c = CLIENT

    def tearDown(self):
        reset_fleet()

    def fleet_with_jobs(self):
        """Production = Alpha + Beta, Workshop = Beta + Gamma, Delta is in no group."""
        put_printer("Alpha", ["Production"])
        put_printer("Beta", ["Production", "Workshop"])
        put_printer("Gamma", ["Workshop"])
        put_printer("Delta")
        add_job("Alpha", grams=10, hours=1)
        add_job("Alpha", grams=20, hours=2, ago=100)
        add_job("Beta", "failed", grams=5, hours=1, ago=200)
        add_job("Beta", grams=10, hours=1, ago=300)
        add_job("Gamma", grams=30, hours=3, ago=400)
        add_job("Delta", grams=40, hours=4, ago=500)


class JobLogFilterTests(Base):
    def stats(self, **kw):
        return server.JOBLOG.stats("all", **kw)

    def test_printer_argument_still_takes_one_name_and_a_list(self):
        self.fleet_with_jobs()
        self.assertEqual(self.stats(printer="alpha")["jobs"], 2)                         # legacy: one name, any case
        self.assertEqual(self.stats(printer=["Alpha", "gamma"])["jobs"], 3)              # new: any of these
        self.assertEqual(self.stats(printer="")["jobs"], 6)                              # "" = no filter, as before
        self.assertEqual(self.stats(printer=None)["jobs"], 6)

    def test_an_empty_list_matches_nothing_not_everything(self):
        self.fleet_with_jobs()
        st = self.stats(printer=[])
        self.assertEqual(st["jobs"], 0)
        self.assertEqual(server.JOBLOG.recent_jobs(10, []), [])
        self.assertEqual(server.JOBLOG.file_costs("part.gcode.3mf", None, [])["attempts"], 0)

    def test_totals_and_success_rate_are_over_the_filtered_set(self):
        self.fleet_with_jobs()
        st = self.stats(printer=["Alpha", "Beta"])
        self.assertEqual((st["jobs"], st["finished"], st["failed"]), (4, 3, 1))
        self.assertAlmostEqual(st["success_rate"], 0.75)
        self.assertAlmostEqual(st["hours"], 5.0)
        self.assertEqual(sorted(st["by_printer"]), ["Alpha", "Beta"])
        self.assertEqual(st["printer"], ["Alpha", "Beta"])

    def test_by_group_counts_a_shared_printer_in_both_groups(self):
        self.fleet_with_jobs()
        st = self.server_stats()
        by = {g["group"]: g for g in st["by_group"]}
        self.assertEqual(set(by), {"Production", "Workshop", None})
        self.assertEqual((by["Production"]["jobs"], by["Workshop"]["jobs"], by[None]["jobs"]), (4, 3, 1))
        self.assertGreater(sum(g["jobs"] for g in st["by_group"]), st["jobs"])           # Beta counted twice: said in the footnote
        self.assertEqual(by["Production"]["printers"], ["Alpha", "Beta"])
        self.assertEqual(by[None]["printers"], ["Delta"])
        self.assertAlmostEqual(by["Production"]["hours"], 5.0)
        self.assertEqual(by["Production"]["by_material"], {"PLA": 42.0})          # Alpha 10+20, Beta: 40% of a failed 5 g = 2, plus a finished 10
        # a group's sums equal the printer-set totals computed independently
        for name, members in (("Production", ["Alpha", "Beta"]), ("Workshop", ["Beta", "Gamma"]), (None, ["Delta"])):
            solo = self.stats(printer=members)
            self.assertEqual(by[name]["jobs"], solo["jobs"], name)
            self.assertAlmostEqual(by[name]["cost_total"], solo["cost"]["total"], places=2, msg=name)
            self.assertAlmostEqual(by[name]["grams_used"], solo["grams_used"], places=2, msg=name)
            self.assertEqual(by[name]["success_rate"], solo["success_rate"], name)

    def server_stats(self, **kw):
        return server.JOBLOG.stats("all", kw.get("printer"), None, None, server.STORE.group_index())

    def test_by_group_with_no_groups_is_empty_and_hides_the_no_group_row(self):
        put_printer("Alpha")
        add_job("Alpha")
        self.assertEqual(self.server_stats()["by_group"], [])

    def test_by_group_under_a_filter_lists_only_groups_with_prints(self):
        self.fleet_with_jobs()
        st = self.server_stats(printer=["Gamma"])
        self.assertEqual([g["group"] for g in st["by_group"]], ["Workshop"])
        self.assertEqual(st["by_group"][0]["jobs"], 1)
        st = self.server_stats(printer=[])
        self.assertEqual(st["by_group"], [])

    def test_a_group_with_no_prints_still_shows_when_nothing_is_filtered(self):
        put_printer("Alpha", ["Production"])
        put_printer("Beta", ["Idle-Group"])
        add_job("Alpha")
        by = {g["group"]: g for g in self.server_stats()["by_group"]}
        self.assertEqual(by["Idle-Group"]["jobs"], 0)
        self.assertIsNone(by["Idle-Group"]["success_rate"])

    def test_cost_per_part_filter(self):
        self.fleet_with_jobs()
        add_job("Delta", "failed", grams=40, hours=4, ago=900)
        everywhere = server.JOBLOG.file_costs("part.gcode.3mf", None, None)
        only_ab = server.JOBLOG.file_costs("part.gcode.3mf", None, ["Alpha", "Beta"])
        self.assertEqual(everywhere["attempts"], 7)
        self.assertEqual(only_ab["attempts"], 4)
        self.assertEqual(only_ab["printer"], ["Alpha", "Beta"])

    def test_csv_export_filter(self):
        self.fleet_with_jobs()
        text = server.JOBLOG.export_csv(["Gamma", "Delta"], "all")
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 3)                                 # header + 2
        self.assertTrue(all(("Gamma" in l or "Delta" in l) for l in lines[1:]))


class RestFilterTests(Base):
    def get(self, path, **kw):
        r = self.c.get(path, **kw)
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_stats_group_filter_and_union_semantics(self):
        self.fleet_with_jobs()
        s = self.get("/api/stats?period=all&group=Production").json()
        self.assertEqual((s["jobs"], s["filter"]["groups"], s["filter"]["resolved_printers"]), (4, ["Production"], ["Alpha", "Beta"]))
        s = self.get("/api/stats?period=all&group=production&group=workshop").json()      # two groups, any case
        self.assertEqual(s["jobs"], 5)                                                    # Alpha 2 + Beta 2 + Gamma 1 (Beta once)
        s = self.get("/api/stats?period=all&group=Production&printer=Delta").json()       # group + a picked printer
        self.assertEqual(s["jobs"], 5)
        s = self.get("/api/stats?period=all&printer=Alpha&printer=Gamma").json()          # hand-picked printers only
        self.assertEqual((s["jobs"], sorted(s["by_printer"])), (3, ["Alpha", "Gamma"]))
        s = self.get("/api/stats?period=all&printer=Alpha").json()                        # the old single-printer form
        self.assertEqual((s["jobs"], s["printer"]), (2, "Alpha"))
        s = self.get("/api/stats?period=all").json()
        self.assertEqual(s["jobs"], 6)
        self.assertNotIn("filter", s)

    def test_unknown_group_matches_nothing(self):
        self.fleet_with_jobs()
        s = self.get("/api/stats?period=all&group=Nope").json()
        self.assertEqual((s["jobs"], s["filter"]["unknown_groups"]), (0, ["Nope"]))
        j = self.get("/api/jobs?group=Nope").json()
        self.assertEqual(j["jobs"], [])

    def test_jobs_list_and_csv_follow_the_filter(self):
        self.fleet_with_jobs()
        j = self.get("/api/jobs?limit=50&group=Workshop").json()
        self.assertEqual(sorted({x["printer"] for x in j["jobs"]}), ["Beta", "Gamma"])
        j = self.get("/api/jobs?group=Workshop&printer=Delta").json()
        self.assertEqual(sorted({x["printer"] for x in j["jobs"]}), ["Beta", "Delta", "Gamma"])
        j = self.get("/api/jobs?printer=Alpha").json()
        self.assertEqual({x["printer"] for x in j["jobs"]}, {"Alpha"})
        csv_text = self.get("/api/jobs.csv?period=all&group=Production").text
        self.assertEqual(len(csv_text.strip().splitlines()), 5)            # header + 4
        self.assertNotIn("Delta", csv_text)

    def test_costs_file_follows_the_filter(self):
        self.fleet_with_jobs()
        d = self.get("/api/costs/file?name=part.gcode.3mf&group=Workshop").json()
        self.assertEqual(d["attempts"], 3)

    def test_stats_carries_by_group(self):
        self.fleet_with_jobs()
        s = self.get("/api/stats?period=all").json()
        self.assertEqual({g["group"] for g in s["by_group"]}, {"Production", "Workshop", None})


class RestGroupTests(Base):
    def test_printer_list_and_fleet_show_groups(self):
        put_printer("Alpha", ["Production", "Workshop"])
        put_printer("Beta")
        pl = {p["name"]: p for p in self.c.get("/api/printers").json()["printers"]}
        self.assertEqual(pl["Alpha"]["groups"], ["Production", "Workshop"])
        self.assertEqual(pl["Beta"]["groups"], [])
        self.assertNotIn("groups", pl["Alpha"]["values"])
        fl = {p["printer"]: p for p in self.c.get("/api/fleet").json()}
        self.assertEqual(fl["Alpha"]["groups"], ["Production", "Workshop"])
        self.assertEqual(fl["Beta"]["groups"], [])

    def test_get_groups(self):
        put_printer("Alpha", ["Production"])
        put_printer("Beta", ["production", "Workshop"])
        self.assertEqual(self.c.get("/api/groups").json(),
                         [{"name": "Production", "printers": ["Alpha", "Beta"]}, {"name": "Workshop", "printers": ["Beta"]}])
        reset_fleet()
        self.assertEqual(self.c.get("/api/groups").json(), [])

    def test_put_groups_route_and_the_edit_route(self):
        put_printer("Alpha")
        r = self.c.put("/api/printers/Alpha/groups", json={"groups": ["Production", " Workshop "]})
        self.assertEqual((r.status_code, r.json()["groups"]), (200, ["Production", "Workshop"]))
        r = self.c.put("/api/printers/alpha", json={"values": {"groups": ["Prototypes"]}})        # the existing update route
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["printer"]["groups"], ["Prototypes"])
        self.assertEqual(r.json()["changed"], ["groups"])
        self.assertEqual(self.c.put("/api/printers/Nope/groups", json={"groups": []}).status_code, 404)
        self.assertEqual(self.c.put("/api/printers/Alpha/groups", json={"groups": ["a/b"]}).status_code, 400)
        self.assertEqual(self.c.put("/api/printers/Alpha/groups", json={"groups": 5}).status_code, 400)
        self.assertEqual(self.c.put("/api/printers/Alpha/groups", content="nope", headers={"Content-Type": "application/json"}).status_code, 400)
        self.assertEqual(server.STORE.get("Alpha")["groups"], ["Prototypes"])

    def test_rename_and_delete_routes(self):
        put_printer("Alpha", ["Production"])
        put_printer("Beta", ["Production", "Workshop"])
        r = self.c.post("/api/groups/rename", json={"from": "production", "to": "Office"})
        self.assertEqual((r.status_code, r.json()["group"], r.json()["printers"]), (200, "Office", ["Alpha", "Beta"]))
        self.assertEqual(server.STORE.get("Beta")["groups"], ["Office", "Workshop"])
        self.assertEqual(self.c.post("/api/groups/rename", json={"from": "Nope", "to": "X"}).status_code, 404)
        self.assertEqual(self.c.post("/api/groups/rename", json={"from": "Office"}).status_code, 400)
        self.assertEqual(self.c.post("/api/groups/rename", json={"from": "Office", "to": "bad/name"}).status_code, 400)
        r = self.c.delete("/api/groups/office")
        self.assertEqual((r.status_code, r.json()["deleted"]), (200, "Office"))
        self.assertEqual(server.STORE.get("Beta")["groups"], ["Workshop"])
        self.assertEqual(self.c.delete("/api/groups/Office").status_code, 404)

    def test_a_group_name_with_odd_characters_round_trips_in_the_path(self):
        put_printer("Alpha", ["Café 3D & \"co\" <b>"])
        r = self.c.delete("/api/groups/" + "Caf%C3%A9%203D%20%26%20%22co%22%20%3Cb%3E")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(server.STORE.get("Alpha").get("groups", []), [])


class AuthLevelTests(Base):
    """Group changes are fleet changes: the same `control` level as editing a printer; reading needs only `read`."""

    def setUp(self):
        super().setUp()
        self.auth = server.AUTH
        server.SETTINGS.update({"security.api_auth": "on"}, clear=["security.dashboard_password"])
        for k in self.auth.keys.list_public():
            self.auth.delete_key(k["id"])
        _r, self.read = self.auth.create_key("viewer", "read")
        _r, self.ctl = self.auth.create_key("agent", "control")
        put_printer("Alpha", ["Production"])

    def tearDown(self):
        for k in self.auth.keys.list_public():
            self.auth.delete_key(k["id"])
        server.SETTINGS.update({"security.api_auth": "off"})
        super().tearDown()

    @staticmethod
    def bearer(token):
        return {"Authorization": "Bearer " + token}

    def test_the_scope_rows_match_how_printers_are_edited(self):
        self.assertEqual(security.rest_scope("GET", "/api/groups"), "read")
        self.assertEqual(security.rest_scope("PUT", "/api/printers/x/groups"), "control")
        self.assertEqual(security.rest_scope("POST", "/api/groups/rename"), "control")
        self.assertEqual(security.rest_scope("DELETE", "/api/groups/x"), "control")
        self.assertEqual(security.rest_scope("PUT", "/api/printers/x"), security.rest_scope("PUT", "/api/printers/x/groups"))
        self.assertEqual(security.tool_scope("list_groups"), "read")
        self.assertEqual(security.tool_scope("set_printer_groups"), "control")
        self.assertEqual(security.tool_scope("update_printer"), "control")

    def test_no_key_is_401_a_read_key_may_look_only(self):
        for method, path in (("get", "/api/groups"), ("put", "/api/printers/Alpha/groups"),
                             ("post", "/api/groups/rename"), ("delete", "/api/groups/Production")):
            self.assertEqual(getattr(self.c, method)(path).status_code, 401, path)
        h = self.bearer(self.read)
        self.assertEqual(self.c.get("/api/groups", headers=h).status_code, 200)
        self.assertEqual(self.c.get("/api/stats?group=Production", headers=h).status_code, 200)
        for method, path, body in (("put", "/api/printers/Alpha/groups", {"groups": ["X"]}),
                                   ("post", "/api/groups/rename", {"from": "Production", "to": "X"}),
                                   ("delete", "/api/groups/Production", None)):
            r = getattr(self.c, method)(path, headers=h, **({"json": body} if body else {}))
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(r.json()["required_scope"], "control")
        self.assertEqual(server.STORE.get("Alpha")["groups"], ["Production"])           # nothing changed

    def test_a_control_key_may_change_groups(self):
        h = self.bearer(self.ctl)
        self.assertEqual(self.c.put("/api/printers/Alpha/groups", headers=h, json={"groups": ["Workshop"]}).status_code, 200)
        self.assertEqual(self.c.post("/api/groups/rename", headers=h, json={"from": "Workshop", "to": "Shop"}).status_code, 200)
        self.assertEqual(self.c.delete("/api/groups/Shop", headers=h).status_code, 200)
        self.assertEqual(server.STORE.get("Alpha").get("groups", []), [])

    def test_the_csv_export_is_still_control_even_with_a_group_filter(self):
        add_job("Alpha")
        self.assertEqual(self.c.get("/api/jobs.csv?group=Production", headers=self.bearer(self.read)).status_code, 403)
        self.assertEqual(self.c.get("/api/jobs.csv?group=Production", headers=self.bearer(self.ctl)).status_code, 200)

    def test_mcp_scopes_end_to_end(self):
        from test_auth import MCP_HEADERS, INIT, rpc, sse_json            # the same wire helpers the auth suite uses

        def session(token):
            r = self.c.post("/mcp", headers={**MCP_HEADERS, **self.bearer(token)}, json=INIT)
            self.assertEqual(r.status_code, 200, r.text)
            h = {**MCP_HEADERS, **self.bearer(token), "mcp-session-id": r.headers["mcp-session-id"],
                 "mcp-protocol-version": "2025-03-26"}
            self.c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            return h

        def call(h, tool, args):
            r = self.c.post("/mcp", headers=h, json=rpc("tools/call", {"name": tool, "arguments": args}, 5))
            self.assertEqual(r.status_code, 200, r.text)
            return sse_json(r)["result"]

        hr, hc = session(self.read), session(self.ctl)
        res = call(hr, "list_groups", {})
        self.assertFalse(res.get("isError"))
        self.assertEqual(json.loads(res["content"][0]["text"])[0]["name"], "Production")
        res = call(hr, "set_printer_groups", {"printer": "Alpha", "groups": ["X"]})
        self.assertTrue(res["isError"])
        self.assertIn("'set_printer_groups' needs a 'control' key", res["content"][0]["text"])
        res = call(hr, "job_stats", {"period": "all", "group": "Production"})
        self.assertFalse(res.get("isError"))
        res = call(hc, "set_printer_groups", {"printer": "Alpha", "groups": ["Workshop", "Prototypes"]})
        self.assertFalse(res.get("isError"), res)
        self.assertEqual(server.STORE.get("Alpha")["groups"], ["Workshop", "Prototypes"])


# ====================================================================== MCP tools (called directly)

class ToolTests(Base):
    def run_tool(self, fn, *a, **kw):
        out = fn(*a, **kw)
        return asyncio.run(out) if asyncio.iscoroutine(out) else out

    def test_tools_are_registered_with_scopes(self):
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        self.assertTrue({"list_groups", "set_printer_groups"} <= names)

    def test_list_printers_includes_groups(self):
        put_printer("Alpha", ["Production", "Workshop"])
        put_printer("Beta")
        text = self.run_tool(server.list_printers)
        lines = {l.split(":")[0]: l for l in text.splitlines()}
        self.assertIn("[groups: Production, Workshop]", lines["Alpha"])
        self.assertNotIn("groups", lines["Beta"])

    def test_list_groups_and_set_printer_groups(self):
        put_printer("Alpha")
        put_printer("Beta")
        self.assertEqual(json.loads(self.run_tool(server.list_groups)), [])
        res = json.loads(self.run_tool(server.set_printer_groups, "alpha", ["Production", "production", "Workshop"]))
        self.assertEqual(res["groups"], ["Production", "Workshop"])
        self.run_tool(server.set_printer_groups, "Beta", ["Production"])
        self.assertEqual(json.loads(self.run_tool(server.list_groups)),
                         [{"name": "Production", "printers": ["Alpha", "Beta"]}, {"name": "Workshop", "printers": ["Alpha"]}])
        self.assertTrue(self.run_tool(server.set_printer_groups, "Nope", ["A"]).startswith("REFUSED"))
        self.assertTrue(self.run_tool(server.set_printer_groups, "Alpha", ["x" * 40]).startswith("REFUSED"))
        self.assertEqual(json.loads(self.run_tool(server.set_printer_groups, "Alpha", []))["groups"], [])

    def test_job_tools_filter_by_group_printers_and_both(self):
        self.fleet_with_jobs()
        st = json.loads(self.run_tool(server.job_stats, "all", group="Production"))
        self.assertEqual(st["jobs"], 4)
        self.assertEqual(st["filter"]["groups"], ["Production"])
        self.assertIn("by_group", st)
        st = json.loads(self.run_tool(server.job_stats, "all", printers=["Gamma", "Delta"]))
        self.assertEqual(st["jobs"], 2)
        st = json.loads(self.run_tool(server.job_stats, "all", printer="Alpha", printers=["Gamma"], group="Production"))
        self.assertEqual(st["jobs"], 5)                         # Alpha + Beta (Production) + Gamma
        st = json.loads(self.run_tool(server.job_stats, "all", group="production, workshop"))
        self.assertEqual(st["jobs"], 5)
        everything = json.loads(self.run_tool(server.job_stats, "all"))
        self.assertEqual(everything["jobs"], 6)
        self.assertNotIn("filter", everything)
        jobs = json.loads(self.run_tool(server.recent_jobs, 50, group="Workshop"))
        self.assertEqual(sorted({j["printer"] for j in jobs["jobs"]}), ["Beta", "Gamma"])
        jobs = json.loads(self.run_tool(server.recent_jobs, 50, printers=["Delta"]))
        self.assertEqual({j["printer"] for j in jobs["jobs"]}, {"Delta"})
        d = json.loads(self.run_tool(server.cost_per_part, "part.gcode.3mf", group="Workshop"))
        self.assertEqual(d["attempts"], 3)
        d = json.loads(self.run_tool(server.cost_per_part, "part.gcode.3mf", printers=["Alpha"]))
        self.assertEqual(d["attempts"], 2)

    def test_an_unknown_group_is_refused_with_the_names_in_use(self):
        self.fleet_with_jobs()
        for out in (self.run_tool(server.job_stats, "all", group="Prodution"),
                    self.run_tool(server.recent_jobs, 5, group="Prodution"),
                    self.run_tool(server.cost_per_part, "part.gcode.3mf", group="Prodution")):
            self.assertTrue(out.startswith("REFUSED"), out)
            self.assertIn("Production", out)
            self.assertIn("Workshop", out)

    def test_the_old_calls_still_work(self):
        self.fleet_with_jobs()
        self.assertEqual(json.loads(self.run_tool(server.job_stats, "all", "Alpha"))["jobs"], 2)
        self.assertEqual(len(json.loads(self.run_tool(server.recent_jobs, 3))["jobs"]), 3)
        self.assertEqual(json.loads(self.run_tool(server.cost_per_part, "part.gcode.3mf"))["attempts"], 6)


class SettingsExportTests(Base):
    def test_groups_are_fleet_data_not_settings(self):
        put_printer("Alpha", ["Production"])
        text = json.dumps(server.SETTINGS.export_settings())
        self.assertNotIn("Production", text)
        self.assertNotIn("groups", settings_store.FIELDS)


# ====================================================================== the dashboard (static + headless browser)

class DashboardStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(APP, "dashboard.html"), encoding="utf8") as fh:
            cls.html = fh.read()

    def test_the_fleet_screen_markup_has_no_group_row_except_the_switcher(self):
        # kiosk card geography is a hard rule: the home screen's static markup only gains the (hidden until needed) switcher
        # container, which is filled from JS - see tests/test_group_switcher.py
        import re
        home = re.search(r'<section id="home".*?</section>', self.html, re.S).group(0)
        self.assertIn('<div id="fl-sw" class="fl-sw" hidden></div>', home)
        self.assertNotIn("group", home.replace("Main page: filter by group", "").lower())

    def test_new_actions_are_delegated_and_carry_data_in_attributes(self):
        for act in ("pgAdd", "pgDrop", "pgRename", "pgRenameGo", "pgDelete", "pgDeleteGo", "hfOpen", "hfApply", "hfClear"):
            self.assertIn(f"{act}:", self.html, act)
            self.assertIn(f'data-act="{act}"', self.html, act)
        self.assertIn("...PG_ACTIONS", self.html)

    def test_groups_are_sent_with_a_save_and_the_filter_goes_into_every_history_request(self):
        self.assertIn("values.groups = (PM.groups || []).slice()", self.html)
        self.assertIn("'/api/stats' + q", self.html)
        self.assertIn("hfQuery()", self.html)
        self.assertIn("By group", self.html)
        self.assertIn("counted in each of them", self.html)            # the double-count footnote


HOSTILE_DRIVER = r"""
const P4 = '"><svg onload=alert(1)>';           // a hostile name WITHOUT a slash (the editor and the server refuse slashes)
const alpha = {name: 'Alpha', type: 'octoprint', type_label: 'OctoPrint', address: 'a', model: '', driver_available: true, loaded: true,
               secrets: {}, values: {name: 'Alpha'}, groups: [P1, P2]};
const beta = {name: 'Beta', type: 'octoprint', type_label: 'OctoPrint', address: 'b', model: '', driver_available: true, loaded: true,
              secrets: {}, values: {name: 'Beta'}, groups: [P4, 'Plain']};
const idx = [{name: P1, printers: ['Alpha']}, {name: P2, printers: ['Alpha']}, {name: P4, printers: ['Beta']}, {name: 'Plain', printers: ['Beta']}];
const row = (g, jobs) => ({group: g, printers: [P3], jobs, finished: jobs, failed: 0, cancelled: 0, running: 0, unknown: 0, success_rate: 1, hours: 2,
                           grams_used: 20, by_material: {[P2]: 20}, cost_total: 3, good_parts: 1, true_cost_per_part: 3, clean_cost_per_part: 3});
window.__stubs.push((u, m, init) => {
  if(u === '/api/fleet') return J([]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: []});
  if(u === '/api/printers') return J({printers: [alpha, beta]});
  if(u.startsWith('/api/printer-types')) return J({types: [{type: 'octoprint', label: 'OctoPrint', description: 'd', fields: [{name: 'name', label: 'Name', type: 'text', required: true}]}]});
  if(u === '/api/groups') return J(idx);
  if(/^\/api\/printers\/[^/]+$/.test(u) && m === 'PUT') return J({printer: {name: 'Alpha'}, changed: ['groups']});
  if(u === '/api/groups/rename' && m === 'POST') return J({group: 'Renamed', merged: false, printers: ['Alpha']});
  if(u.startsWith('/api/groups/') && m === 'DELETE') return J({deleted: 'x', printers: ['Alpha']});
  if(u.startsWith('/api/stats')) return J({jobs: 3, finished: 3, failed: 0, cancelled: 0, unknown: 0, running: 0, success_rate: 1, grams_used: 5, by_material: {},
    by_printer: {Alpha: {jobs: 3, finished: 3, failed: 0, cancelled: 0, success_rate: 1, hours: 1, cost_total: 1}}, by_file: [],
    by_group: [row(P1, 2), row(null, 1)], cost: {total: 1.5, wasted_on_failures: 0}, currency: '$', since: null});
  if(u.startsWith('/api/jobs')) return J({jobs: []});
  return null;
});
const step = k => { R[k] = {injected: injected(), alerts: window.__alerts.length}; };
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };

await openPrinters(); await wait(() => document.querySelector('.pmrow')); step('printers');
R.rowGroups = [...document.querySelectorAll('.pmrow .bkinfo')].map(e => e.textContent).filter(t => t.startsWith('Groups:'));
R.sectionData = [...document.querySelectorAll('[data-act=pgRename]')].map(b => b.dataset.g);
R.sectionText = [...document.querySelectorAll('.pmrow .bkhead b')].map(e => e.textContent);

await pmEdit('Alpha'); await wait(() => document.querySelector('.pg-chip')); step('form');
R.chipData = [...document.querySelectorAll('.pg-chip [data-act=pgDrop]')].map(b => b.dataset.g);
R.chipText = [...document.querySelectorAll('.pg-chip > span')].map(e => e.textContent);
R.sugData = [...document.querySelectorAll('#pg-sug [data-act=pgAdd]')].map(b => b.dataset.g);
$('pg-in').value = 'Prototypes, plain , ' + 'x'.repeat(40); pgAdd('');
R.afterAdd = PM.groups.slice(); R.addMsg = $('pg-msg').textContent;           // a name over 32 characters is refused on the spot
$('pg-in').value = 'a/b'; pgAdd(''); R.slashMsg = $('pg-msg').textContent;
document.querySelector('.pg-chip [data-act=pgDrop]').click(); await sleep(50);
R.afterDrop = PM.groups.slice();
[...document.querySelectorAll('#pg-sug [data-act=pgAdd]')].find(b => b.dataset.g === P4).click(); await sleep(50);
R.afterSuggest = PM.groups.slice();
R.saveLabel = $('pm-save').textContent; R.saveDisabled = $('pm-save').disabled;      // groups changed: the ONE save button is live
$('pm-save').click(); await wait(() => window.__calls.some(c => c.startsWith('PUT /api/printers/Alpha')));
R.saveMsg = 'Groups saved.';
R.putCall = window.__calls.filter(c => c.startsWith('PUT /api/printers/Alpha')).pop();
step('formAfter');

await openPrinters(); await wait(() => document.querySelector('[data-act=pgRename]'));
[...document.querySelectorAll('[data-act=pgRename]')].find(b => b.dataset.g === P1).click(); await sleep(100); step('rename');
R.renameValue = $('pg-new').value; R.renameGoData = $('pg-go').dataset.g;
$('pg-new').value = 'Renamed'; $('pg-go').click(); await sleep(300);
R.renameCall = window.__calls.filter(c => c.startsWith('POST /api/groups/rename')).pop();
await openPrinters(); await wait(() => document.querySelector('[data-act=pgDelete]'));
[...document.querySelectorAll('[data-act=pgDelete]')].find(b => b.dataset.g === P1).click(); await sleep(100); step('delete');
R.deleteGoData = document.querySelector('[data-act=pgDeleteGo]').dataset.g;
document.querySelector('[data-act=pgDeleteGo]').click(); await sleep(300);
R.deleteCall = window.__calls.filter(c => c.startsWith('DELETE /api/groups/')).pop();

await openHistory(); await wait(() => document.querySelector('#hi-body .ht')); step('history');
R.filterButton = document.querySelector('[data-act=hfOpen]').textContent;
document.querySelector('[data-act=hfOpen]').click(); await sleep(100); step('filterDialog');
R.boxes = [...document.querySelectorAll('#mbody input[data-hf]')].map(i => i.dataset.hf + ':' + i.dataset.v);
document.querySelector('#mbody input[data-hf=group]').click();
document.querySelector('#mbody input[data-hf=printer]').click();
document.querySelector('[data-act=hfApply]').click(); await sleep(500); step('filtered');
R.statsCall = window.__calls.filter(c => c.startsWith('GET /api/stats')).pop();
R.jobsCall = window.__calls.filter(c => c.startsWith('GET /api/jobs?')).pop();
hiMore(); R.csvHref = document.querySelector('#mbody a.bkdl').getAttribute('href'); closeModal();
R.filterButton2 = document.querySelector('[data-act=hfOpen]').textContent;
const heads = [...document.querySelectorAll('#hi-body h2')].map(h => h.textContent);
R.hasByGroup = heads.some(h => h.startsWith('By group'));
const tbl = [...document.querySelectorAll('#hi-body .ht')].find(t => t.querySelector('.tr.h') && t.querySelector('.tr.h').textContent.startsWith('Group'));
R.groupRows = tbl ? [...tbl.querySelectorAll('.tr:not(.h) .first b')].map(e => e.textContent) : null;
R.footnote = [...document.querySelectorAll('#hi-body .note')].some(n => n.textContent.includes('counted in each of them'));
document.querySelector('[data-act=hfOpen]').click(); await sleep(100);
document.querySelector('[data-act=hfClear]').click(); await sleep(500);
R.afterClear = window.__calls.filter(c => c.startsWith('GET /api/stats')).pop();
"""


P4 = '"><svg onload=alert(1)>'


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class DashboardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_fix_dashboard as tfd
        cls.tfd = tfd
        cls.R = None

    def result(self):
        if DashboardBrowserTests.R is None:
            DashboardBrowserTests.R = self.tfd.run_page(self, HOSTILE_DRIVER, 60000)
        return DashboardBrowserTests.R

    def test_driver_ran_and_nothing_executed_or_was_injected(self):
        R = self.result()
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        steps = [k for k, v in R.items() if isinstance(v, dict) and "injected" in v]
        for want in ("printers", "form", "formAfter", "rename", "delete", "history", "filterDialog", "filtered"):
            self.assertIn(want, steps)
        for k in steps:
            self.assertEqual(R[k], {"injected": 0, "alerts": 0}, k)

    def test_printer_form_chips_round_trip_hostile_names_as_text(self):
        R, P1, P2, P3 = self.result(), self.tfd.P1, self.tfd.P2, P4
        self.assertEqual(R["rowGroups"], [f"Groups: {P1}, {P2}", f"Groups: {P3}, Plain"])
        self.assertEqual(sorted(R["sectionData"]), sorted([P1, P2, P3, "Plain"]))      # A-Z, whatever the browser's collation
        self.assertEqual(R["chipData"], [P1, P2])
        self.assertEqual(R["chipText"], [P1, P2])
        self.assertEqual(sorted(R["sugData"]), sorted([P3, "Plain"]))

    def test_chip_editor_adds_validates_drops_and_saves(self):
        R, P1, P2, P3 = self.result(), self.tfd.P1, self.tfd.P2, P4
        self.assertEqual(R["afterAdd"], [P1, P2, "Prototypes", "Plain"])              # trimmed; the long one was refused
        self.assertIn("at most 32", R["addMsg"])
        self.assertIn("slash", R["slashMsg"])
        self.assertEqual(R["afterDrop"], [P2, "Prototypes", "Plain"])
        self.assertEqual(R["afterSuggest"], [P2, "Prototypes", "Plain", P3])
        self.assertEqual((R["saveLabel"], R["saveDisabled"]), ("Save changes", False))
        self.assertEqual(json.loads(R["putCall"].split(" ", 2)[2])["values"]["groups"], [P2, "Prototypes", "Plain", P3])

    def test_rename_and_delete_carry_the_exact_name(self):
        R, P1 = self.result(), self.tfd.P1
        self.assertEqual((R["renameValue"], R["renameGoData"], R["deleteGoData"]), (P1, P1, P1))
        self.assertEqual(json.loads(R["renameCall"].split(" ", 2)[2]), {"from": P1, "to": "Renamed"})
        self.assertIn("/api/groups/" + __import__("urllib.parse").parse.quote(P1, safe="~()*!.'-_"), R["deleteCall"].replace("%2F", "%2F"))

    def test_history_filter_is_sent_on_every_request_and_the_by_group_table_is_shown(self):
        R, P1, P3 = self.result(), self.tfd.P1, self.tfd.P3
        self.assertEqual(R["filterButton"].strip(), "All printers ▾")
        self.assertIn("group:" + P1, R["boxes"])
        self.assertIn("printer:Alpha", R["boxes"])
        enc = __import__("urllib.parse").parse.quote
        self.assertIn("&group=" + enc(P1, safe="~()*!.'-_"), R["statsCall"])
        self.assertIn("&printer=Alpha", R["statsCall"])
        self.assertIn("&group=" + enc(P1, safe="~()*!.'-_"), R["jobsCall"])
        self.assertIn("&group=", R["csvHref"])
        self.assertIn("Group: ", R["filterButton2"])
        self.assertTrue(R["hasByGroup"])
        self.assertEqual(R["groupRows"], [P1, "No group"])
        self.assertTrue(R["footnote"])
        self.assertNotIn("group=", R["afterClear"])                                    # "All printers" clears the filter


if __name__ == "__main__":
    unittest.main(verbosity=1)
