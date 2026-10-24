"""Tests for the Analytics view: app/analytics.py (the summary), app/uptime.py (the reachability log), the /api/analytics route
and the dashboard page that shows them.

    python3 tests/test_analytics.py

The summary and the uptime log are tested on their own against SQLite / files in throw-away temp folders with a fixed "now" -
no printer, no network. The route tests import the server with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointed at a temp directory
(printers are in-process fakes). The page tests read dashboard.html as text and, when Chrome / Edge is installed, load it
with a stubbed `fetch` (the same harness as test_fix_dashboard.py).
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

_TMP = tempfile.mkdtemp(prefix="analytics-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)
os.environ.pop("BAMBU_MCP_ALLOWED_HOSTS", None)

import analytics  # noqa: E402
import costs  # noqa: E402
import joblog  # noqa: E402
import uptime  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover - the app's deps are not installed
    HAVE = False
    _ERR = e

CLIENT = None
H = 3600.0
D = 86400.0
NOW = datetime.datetime(2026, 10, 18, 15, 0, tzinfo=datetime.timezone.utc).timestamp()      # a fixed "now"


def setUpModule():
    global CLIENT
    if HAVE:
        CLIENT = TestClient(server.mcp.streamable_http_app())
        CLIENT.__enter__()


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    shutil.rmtree(_TMP, ignore_errors=True)


def put(jl, printer="A", result="finished", end=NOW - H, hours=1.0, est=None, grams=50.0, percent="auto", material="PLA",
        file="part.gcode.3mf", parts=1, error=None, filaments=None, started=True, ended=True, duration=None):
    """One job row, written straight into the table (no events)."""
    dur = hours * H if duration is None else duration
    pct = (100.0 if result == "finished" else 50.0) if percent == "auto" else percent       # percent=None = progress unknown
    return jl._insert(
        printer=printer, kind="fake", file=file, file_key=costs.file_key(file),
        started_at=(end - dur) if started else None, ended_at=end if ended else None, duration_s=dur if ended else None,
        result=result, percent=pct, est_seconds=(hours * H if est is None else est), grams_est=grams,
        grams_used=None, material=material, filaments=json.dumps(filaments or []), parts=parts, pauses=0, error=error, source="event")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="analytics-jl-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.jl = joblog.JobLog(os.path.join(self.dir, "jobs.sqlite"), tz="UTC", clock=lambda: NOW)
        self.addCleanup(self.jl.close)

    def S(self, period="30d", printer=None, **kw):
        return analytics.summary(self.jl, kw.pop("up", None), period, printer, NOW, **kw)

    def by_printer(self, s):
        return {p["printer"]: p for p in s["by_printer"]}


# ====================================================================== totals and the numbers

class TotalsTests(Base):
    def test_an_empty_log_is_an_empty_answer_not_an_error(self):
        for period in analytics.PERIODS:
            s = self.S(period)
            self.assertTrue(s["empty"], period)
            t = s["totals"]
            self.assertEqual((t["prints"], t["hours"], t["filament_cost"]), (0, 0, 0))
            self.assertIsNone(t["success_rate"])
            self.assertIsNone(t["cost_per_good_part"])
            self.assertEqual((s["by_printer"], s["by_material"], s["top_files"]), ([], [], []))
            self.assertEqual(s["failures"]["by_reason"], [])
            self.assertFalse(s["uptime"]["available"])

    def test_the_numbers_on_a_hand_made_set(self):
        put(self.jl, end=NOW - 10 * H, hours=1, grams=50, parts=2)                                    # finished, 2 parts, 1 h
        put(self.jl, end=NOW - 5 * H, hours=2, grams=100, parts=None)                                 # finished, parts unknown (priced as 1)
        put(self.jl, result="failed", end=NOW - 4 * H, hours=0.5, est=3600, grams=80, percent=50, material="PETG",
            error="Nozzle clog")                                                                      # failed at 50 %: used 40 g
        put(self.jl, result="cancelled", end=NOW - 3 * H, duration=600, est=None, grams=30, percent=None)     # no progress known: no grams
        put(self.jl, result="unknown", end=NOW - 2 * H, hours=9, grams=999)                           # left out of everything but the count
        put(self.jl, result="running", end=NOW - H, ended=False, grams=999)                           # not ended yet
        t = self.S("30d")["totals"]
        self.assertEqual((t["prints"], t["finished"], t["failed"], t["cancelled"], t["unknown"], t["running"]), (6, 2, 1, 1, 1, 1))
        self.assertEqual(t["decided"], 4)
        self.assertEqual(t["success_rate"], 0.5)                                                      # finished / (finished + failed + cancelled)
        self.assertAlmostEqual(t["hours"], 1 + 2 + 0.5 + 600 / H, places=2)
        self.assertEqual(t["grams"], 190.0)                                                           # 50 + 100 + 40 + 0
        self.assertAlmostEqual(t["filament_cost"], 150 * 0.020 + 40 * 0.022, places=2)                # PLA 20 / kg, PETG 22 / kg
        self.assertAlmostEqual(t["wasted_cost"], 40 * 0.022, places=2)
        self.assertEqual(t["good_parts"], 3)                                                          # 2 + 1 (unknown counts as 1, as History does)
        self.assertEqual(t["parts_unknown_prints"], 1)
        self.assertAlmostEqual(t["cost_per_good_part"], (150 * 0.020 + 40 * 0.022) / 3, places=3)

    def test_the_prices_are_the_costs_settings(self):
        put(self.jl, grams=100, material="PLA")
        self.jl._rates = costs.Rates(per_kg={"PLA": 50.0}, currency="EUR ")
        s = self.S()
        self.assertAlmostEqual(s["totals"]["filament_cost"], 5.0)
        self.assertEqual(s["currency"], "EUR ")

    def test_it_agrees_with_history_to_the_cent(self):
        """The same rows through JobLog.stats (History) and analytics.summary give the same figures, over many shapes of row."""
        import random
        rnd = random.Random(5)
        for i in range(120):
            result = rnd.choice(["finished"] * 6 + ["failed", "cancelled", "unknown"])
            mat = rnd.choice(["PLA", "PETG", "ASA", None])
            fil = rnd.choice([None, [{"type": "PLA", "used_g": 30.0}, {"type": "TPU", "used_g": 10.0}], [{"type": "PETG", "used_g": None}]])
            put(self.jl, printer=rnd.choice("ABC"), result=result, end=NOW - rnd.uniform(0.2, 100) * D, hours=rnd.uniform(0.2, 9),
                est=rnd.choice([None, rnd.uniform(1, 9) * H]), grams=rnd.choice([None, rnd.uniform(5, 200)]),
                percent=rnd.choice([None, rnd.uniform(1, 99)]) if result != "finished" else None, material=mat,
                parts=rnd.choice([None, 1, 3, 12]), filaments=fil, file=f"f{rnd.randint(1, 9)}.gcode.3mf")
        for ap, hp in (("7d", "week"), ("30d", "month"), ("90d", "90d"), ("1y", "year"), ("all", "all")):
            a, h = self.S(ap), self.jl.stats(hp, now=NOW)
            t = a["totals"]
            self.assertEqual(t["prints"], h["jobs"], ap)
            self.assertEqual(t["success_rate"], h["success_rate"], ap)
            self.assertAlmostEqual(t["hours"], h["hours"], delta=0.01, msg=ap)
            self.assertAlmostEqual(t["grams"], h["grams_used"], delta=0.1, msg=ap)
            self.assertAlmostEqual(t["filament_cost"], h["cost"]["total"], delta=0.02, msg=ap)
            self.assertAlmostEqual(t["wasted_cost"], h["cost"]["wasted_on_failures"], delta=0.02, msg=ap)
            self.assertEqual(t["good_parts"], h["good_parts"], ap)
            if h["true_cost_per_part"] is not None:
                self.assertAlmostEqual(t["cost_per_good_part"], h["true_cost_per_part"], delta=0.001, msg=ap)
            for p in a["by_printer"]:
                hp_ = h["by_printer"][p["printer"]]
                self.assertEqual((p["prints"], p["success_rate"]), (hp_["jobs"], hp_["success_rate"]), (ap, p["printer"]))
                self.assertAlmostEqual(p["cost"], hp_["cost_total"], delta=0.02, msg=(ap, p["printer"]))
                self.assertAlmostEqual(p["hours"], hp_["hours"], delta=0.01, msg=(ap, p["printer"]))
            for m in a["by_material"]:
                if m["grams"]:
                    self.assertAlmostEqual(m["grams"], h["by_material"].get(m["material"], 0), delta=0.1, msg=(ap, m["material"]))

    def test_a_clock_step_in_a_duration_falls_back_to_the_estimate(self):
        put(self.jl, hours=2, est=2 * H, duration=30 * D)                 # 30 days: cannot be right (costs.machine_hours)
        self.assertAlmostEqual(self.S()["totals"]["hours"], 2.0, places=2)


# ====================================================================== periods and time zones

class PeriodTests(Base):
    def test_the_window_edge_is_inclusive_like_history(self):
        put(self.jl, end=NOW - 7 * D, file="edge.gcode.3mf")
        put(self.jl, end=NOW - 7 * D - 1, file="just-outside.gcode.3mf")
        self.assertEqual(self.S("7d")["totals"]["prints"], 1)
        self.assertEqual(self.jl.stats("week", now=NOW)["jobs"], 1)
        self.assertEqual(self.S("30d")["totals"]["prints"], 2)

    def test_every_period(self):
        for days in (3, 20, 60, 200, 400):
            put(self.jl, end=NOW - days * D, file=f"d{days}.gcode.3mf")
        got = {p: self.S(p)["totals"]["prints"] for p in ("7d", "30d", "90d", "1y", "all")}
        self.assertEqual(got, {"7d": 1, "30d": 2, "90d": 3, "1y": 4, "all": 5})
        self.assertEqual(self.S("year")["totals"]["prints"], 4)                  # joblog's names still work
        self.assertEqual(self.S("14d")["totals"]["prints"], 1)
        self.assertEqual(self.S("1Y")["totals"]["prints"], 4)

    def test_a_bad_period_is_a_value_error(self):
        for bad in ("fortnight", "9999999d", "-3d"):
            with self.assertRaises(ValueError, msg=bad):
                self.S(bad)

    def test_a_print_that_has_not_ended_is_in_the_window_by_its_start(self):
        put(self.jl, result="running", ended=False, end=NOW - 2 * H, hours=1)
        s = self.S("7d")
        self.assertEqual((s["totals"]["prints"], s["totals"]["running"]), (1, 1))

    def test_the_trend_follows_the_hubs_time_zone(self):
        end = datetime.datetime(2026, 10, 17, 3, 30, tzinfo=datetime.timezone.utc).timestamp()      # 23:30 on the 16th in New York
        put(self.jl, end=end)
        utc = {b["start"]: b["finished"] for b in self.S("7d")["trend"]["buckets"]}
        self.assertEqual(utc["2026-10-17"], 1)
        self.jl.tz = joblog.JobLog._tz("America/New_York")
        ny = {b["start"]: b["finished"] for b in self.S("7d")["trend"]["buckets"]}
        self.assertEqual((ny["2026-10-16"], ny["2026-10-17"]), (1, 0))

    def test_days_stay_whole_across_a_daylight_saving_change(self):
        ny = zoneinfo.ZoneInfo("America/New_York")
        self.jl.tz = joblog.JobLog._tz("America/New_York")
        now = datetime.datetime(2026, 3, 11, 12, 0, tzinfo=ny).timestamp()
        for h in ((2026, 3, 8, 1, 30), (2026, 3, 8, 23, 30), (2026, 3, 9, 0, 30)):         # DST began on the 8th at 02:00
            put(self.jl, end=datetime.datetime(*h, tzinfo=ny).timestamp(), file=f"{h}.gcode.3mf")
        tr = analytics.summary(self.jl, None, "7d", None, now)["trend"]
        days = [b["start"] for b in tr["buckets"]]
        self.assertEqual(days, sorted(set(days)))                                          # no repeated or missing day
        self.assertEqual(days, [(datetime.date(2026, 3, 4) + datetime.timedelta(days=i)).isoformat() for i in range(len(days))])
        counts = {b["start"]: b["finished"] for b in tr["buckets"]}
        self.assertEqual((counts["2026-03-08"], counts["2026-03-09"]), (2, 1))

    def test_buckets_day_week_month_and_the_partial_first_one(self):
        put(self.jl, end=NOW - 1 * D)
        t30 = self.S("30d")["trend"]
        self.assertEqual(t30["bucket"], "day")
        self.assertEqual(len(t30["buckets"]), 31)                      # 30 days back spans 31 calendar days
        self.assertTrue(t30["partial_first"])
        t90 = self.S("90d")["trend"]
        self.assertEqual(t90["bucket"], "week")
        starts = [datetime.date.fromisoformat(b["start"]) for b in t90["buckets"]]
        self.assertTrue(all(d.weekday() == 0 for d in starts))         # weeks begin on Monday, like History's material series
        self.assertTrue(all((b - a).days == 7 for a, b in zip(starts, starts[1:])))
        self.assertEqual(self.S("1y")["trend"]["bucket"], "week")
        put(self.jl, end=NOW - 1500 * D, file="old.gcode.3mf")
        ta = self.S("all")["trend"]
        self.assertEqual(ta["bucket"], "month")
        self.assertFalse(ta["partial_first"])
        self.assertEqual(sum(b["finished"] for b in ta["buckets"]), 2)

    def test_the_bars_add_up_to_the_totals_and_failures_are_their_own_count(self):
        put(self.jl, end=NOW - 2 * D)
        put(self.jl, end=NOW - 2 * D, result="failed")
        put(self.jl, end=NOW - 2 * D, result="cancelled")
        put(self.jl, end=NOW - 3 * D, result="unknown")
        tr = self.S("7d")["trend"]["buckets"]
        self.assertEqual((sum(b["finished"] for b in tr), sum(b["failed"] for b in tr), sum(b["other"] for b in tr)), (1, 2, 1))


# ====================================================================== per printer, material, files, failures

class BreakdownTests(Base):
    def test_utilization_is_printing_hours_over_the_hours_the_printer_has_existed(self):
        put(self.jl, "A", end=NOW - 10 * D + 24 * H, hours=24, est=24 * H)                           # A's first print began 10 days ago
        put(self.jl, "B", end=NOW - 50 * D + 1 * H, hours=1, est=1 * H, file="b0.gcode.3mf")          # B has existed 50 days ...
        put(self.jl, "B", end=NOW - 2 * D, hours=5, est=5 * H, file="b1.gcode.3mf")                  # ... and printed 5 h of the last 30 days
        b = self.by_printer(self.S("30d"))
        self.assertAlmostEqual(b["A"]["utilization"], 24 / 240, places=3)                           # 24 h of 10 days, not of 30
        self.assertEqual(b["B"]["utilization"], round(5 / (30 * 24), 4))                           # 5 h of the whole 30 days
        self.assertAlmostEqual(self.by_printer(self.S("all"))["B"]["utilization"], 6 / (50 * 24), places=3)

    def test_utilization_is_capped_and_absent_without_enough_time(self):
        put(self.jl, "A", end=NOW - 60, hours=0.3, est=0.3 * H)                                      # brand new printer: 20 minutes old
        self.assertIsNone(self.by_printer(self.S("30d"))["A"]["utilization"])
        put(self.jl, "C", end=NOW - 2 * D + 60 * H, hours=60, est=60 * H)                            # more printing than the printer's lifetime
        self.assertLessEqual(self.by_printer(self.S("30d"))["C"]["utilization"], 1.0)

    def test_per_printer_rows_and_the_filter(self):
        put(self.jl, "Alpha", file="a.gcode.3mf")
        put(self.jl, "Alpha", result="failed", file="a.gcode.3mf")
        put(self.jl, "Beta", file="b.gcode.3mf")
        s = self.S()
        a = self.by_printer(s)["Alpha"]
        self.assertEqual((a["prints"], a["finished"], a["failed"], a["success_rate"]), (2, 1, 1, 0.5))
        self.assertEqual(self.S(printer=["alpha"])["totals"]["prints"], 2)                          # any case
        self.assertEqual(self.S(printer="Beta")["totals"]["prints"], 1)
        self.assertEqual(self.S(printer=["Alpha", "Beta"])["totals"]["prints"], 3)
        none = self.S(printer=[])                                                                    # a group nobody is in matches nothing
        self.assertEqual((none["totals"]["prints"], none["empty"], none["by_printer"]), (0, True, []))
        self.assertEqual([p["printer"] for p in self.S(printer=["Beta"])["by_printer"]], ["Beta"])

    def test_a_known_printer_with_no_prints_still_has_a_row(self):
        put(self.jl, "Alpha")
        s = self.S(known_printers=["Alpha", "Idle-One"])
        idle = self.by_printer(s)["Idle-One"]
        self.assertEqual((idle["prints"], idle["success_rate"], idle["utilization"]), (0, None, 0.0))
        self.assertEqual([p["printer"] for p in self.S(printer=["Idle-One"], known_printers=["Alpha", "Idle-One"])["by_printer"]], ["Idle-One"])

    def test_material_split_follows_the_filament_list_and_is_scaled_to_the_plate_weight(self):
        fil = [{"type": "PLA", "used_g": 30.0}, {"type": "petg", "used_g": 10.0}]
        put(self.jl, grams=80, material="PLA", filaments=fil)                          # slicer lists 40 g, plate weight 80 g: scaled x2
        put(self.jl, grams=20, material="ABS", filaments=[{"type": "ABS", "used_g": None}])    # incomplete list: all on the main material
        put(self.jl, grams=10, material="")                                            # no material at all
        m = {x["material"]: x for x in self.S()["by_material"]}
        self.assertAlmostEqual(m["PLA"]["grams"], 60.0, places=1)
        self.assertAlmostEqual(m["PETG"]["grams"], 20.0, places=1)
        self.assertAlmostEqual(m["ABS"]["grams"], 20.0, places=1)
        self.assertAlmostEqual(m["?"]["grams"], 10.0, places=1)
        self.assertAlmostEqual(m["PLA"]["cost"], 60 * 0.02, places=2)
        self.assertEqual(m["PLA"]["prints"], 1)                                        # a print counts once, under its main material
        self.assertEqual(self.S()["by_material"][0]["material"], "PLA")                # most grams first

    def test_without_the_json_functions_every_gram_goes_to_the_main_material(self):
        put(self.jl, grams=80, material="PLA", filaments=[{"type": "PLA", "used_g": 30.0}, {"type": "PETG", "used_g": 10.0}])
        old = analytics._HAS_JSON
        analytics._HAS_JSON = False
        try:
            s = self.S()
        finally:
            analytics._HAS_JSON = old
        self.assertEqual({m["material"]: m["grams"] for m in s["by_material"]}, {"PLA": 80.0})
        self.assertEqual(s["totals"]["grams"], 80.0)

    def test_a_filament_list_that_is_not_json_is_ignored_not_fatal(self):
        put(self.jl, grams=40)
        self.jl._exec("UPDATE jobs SET filaments='not json {'")
        self.assertEqual(self.S()["totals"]["grams"], 40.0)
        self.jl._exec("UPDATE jobs SET filaments='{\"a\": 1}'")
        self.assertEqual(self.S()["totals"]["grams"], 40.0)

    def test_failures_by_outcome_reason_and_file(self):
        put(self.jl, result="failed", error="Nozzle clog", file="bad.gcode.3mf")
        put(self.jl, result="failed", error="Nozzle clog", file="bad.gcode.3mf")
        put(self.jl, result="failed", error="  Filament   ran out ", file="other.gcode.3mf")
        put(self.jl, result="cancelled", file="bad.gcode.3mf")
        put(self.jl, result="unknown", error="closed: printer unreachable, end never reported")
        put(self.jl, file="bad.gcode.3mf")
        f = self.S()["failures"]
        out = {o["outcome"]: o["count"] for o in f["by_outcome"]}
        self.assertEqual(out, {"failed": 3, "cancelled": 1, "unknown": 1})
        reasons = [(r["reason"], r["count"]) for r in f["by_reason"]]
        self.assertEqual(reasons[0], ("Nozzle clog", 2))
        self.assertIn(("Filament ran out", 1), reasons)
        self.assertIn((analytics.NO_REASON, 1), reasons)                                   # the cancelled one
        self.assertNotIn("closed: printer unreachable, end never reported", [r[0] for r in reasons])    # unknown outcomes are not failures
        top = f["top_files"][0]
        self.assertEqual((top["file"], top["failed"], top["prints"], top["finished"]), ("bad", 3, 4, 1))
        self.assertEqual(f["failed_or_cancelled"], 4)

    def test_many_reasons_are_cut_to_a_short_list_with_the_rest_counted(self):
        for i in range(14):
            put(self.jl, result="failed", error=f"reason number {i}")
        f = self.S()["failures"]
        self.assertEqual(len(f["by_reason"]), analytics.TOP_REASONS)
        self.assertEqual(f["reasons_other"], 14 - analytics.TOP_REASONS)

    def test_a_very_long_reason_is_cut(self):
        put(self.jl, result="failed", error="x" * 500)
        self.assertLessEqual(len(self.S()["failures"]["by_reason"][0]["reason"]), 90)

    def test_top_files_count_known_parts_and_say_when_parts_are_unknown(self):
        for _ in range(3):
            put(self.jl, file="many.gcode.3mf", parts=6)
        for _ in range(2):
            put(self.jl, file="mystery.gcode.3mf", parts=None)
        put(self.jl, file="mixed.gcode.3mf", parts=4)
        put(self.jl, file="mixed.gcode.3mf", parts=None)
        put(self.jl, file="zero.gcode.3mf", parts=0)
        by = {f["file"]: f for f in self.S()["top_files"]}
        self.assertEqual((by["many"]["prints"], by["many"]["parts"], by["many"]["parts_unknown_prints"]), (3, 18, 0))
        self.assertEqual((by["mystery"]["prints"], by["mystery"]["parts"], by["mystery"]["parts_unknown_prints"]), (2, 0, 2))
        self.assertEqual((by["mixed"]["parts"], by["mixed"]["parts_unknown_prints"]), (4, 1))
        self.assertEqual(by["zero"]["parts_unknown_prints"], 1)                                # 0 parts is not a count
        self.assertEqual(self.S()["top_files"][0]["file"], "many")                             # most printed first

    def test_top_files_is_a_short_list(self):
        for i in range(analytics.TOP_FILES + 5):
            put(self.jl, file=f"f{i}.gcode.3mf")
        self.assertEqual(len(self.S()["top_files"]), analytics.TOP_FILES)

    def test_a_renamed_file_is_one_file_under_its_newest_name(self):
        put(self.jl, "A", file="old name.gcode.3mf")
        put(self.jl, "A", file="new name.gcode.3mf", end=NOW - 2 * H)
        self.jl.record_rename("printer", "A", "old name.gcode.3mf", "new name.gcode.3mf")
        files = self.S()["top_files"]
        self.assertEqual([(f["file"], f["prints"]) for f in files], [("new name", 2)])


# ====================================================================== the uptime log

class UptimeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uptime-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.t = 1_760_000_000.0

    def log(self, **kw):
        kw.setdefault("clock", lambda: self.t)
        return uptime.UptimeLog(self.dir, **kw)

    def lines(self):
        with open(os.path.join(self.dir, uptime.FILE_NAME), encoding="utf8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def run_for(self, u, seconds, state, step=5):
        end = self.t + seconds
        while self.t < end:
            u.observe(state)
            self.t += step

    def test_is_online_is_the_notifiers_definition(self):
        self.assertTrue(uptime.is_online({"connected": True, "state": "IDLE"}))
        self.assertTrue(uptime.is_online({"connected": True, "state": "RUNNING"}))
        self.assertFalse(uptime.is_online({"connected": False, "state": "IDLE"}))
        self.assertFalse(uptime.is_online({"connected": True, "state": "UNKNOWN"}))
        self.assertFalse(uptime.is_online({"connected": True, "state": "DISCONNECTED"}))
        self.assertFalse(uptime.is_online({"connected": True}))
        self.assertFalse(uptime.is_online(None))
        self.assertFalse(uptime.is_online("x"))

    def test_nothing_is_written_until_something_is_seen(self):
        self.log()
        self.assertFalse(os.path.exists(os.path.join(self.dir, uptime.FILE_NAME)))

    def test_a_steady_printer_writes_one_start_and_one_state_line_however_long_it_runs(self):
        u = self.log()
        self.run_for(u, 3 * D, {"A": True, "B": True}, step=60)                    # 4,320 polls
        recs = self.lines()
        self.assertEqual([r["k"] for r in recs], ["start", "p", "p"])
        self.assertEqual(u.writes, 3)

    def test_the_first_state_is_dated_from_when_it_was_first_seen(self):
        u = self.log()
        t0 = self.t
        self.run_for(u, 120, {"A": True})
        p = [r for r in self.lines() if r["k"] == "p"][0]
        self.assertEqual((p["n"], p["up"]), ("A", 1))
        self.assertAlmostEqual(p["t"], t0, delta=1)

    def test_a_blip_shorter_than_the_settle_time_is_never_written(self):
        u = self.log(settle_s=45)
        self.run_for(u, 120, {"A": True})
        before = u.writes
        self.run_for(u, 30, {"A": False})                                           # 30 s: a Wi-Fi blip
        self.run_for(u, 120, {"A": True})
        self.assertEqual(u.writes, before)

    def test_an_outage_is_written_once_when_it_has_lasted_and_dated_from_its_start(self):
        u = self.log(settle_s=45)
        self.run_for(u, 120, {"A": True})
        down_at = self.t
        self.run_for(u, 200, {"A": False})
        self.run_for(u, 120, {"A": True})
        ps = [r for r in self.lines() if r["k"] == "p"]
        self.assertEqual([r["up"] for r in ps], [1, 0, 1])
        self.assertAlmostEqual(ps[1]["t"], down_at, delta=1)

    def test_a_printer_that_connects_a_few_seconds_after_the_hub_starts_is_not_logged_as_down(self):
        u = self.log(settle_s=45)
        self.run_for(u, 15, {"A": False})
        self.run_for(u, 100, {"A": True})
        self.assertEqual([r["up"] for r in self.lines() if r["k"] == "p"], [1])

    def test_percent_online_over_a_known_history(self):
        u = self.log(settle_s=45, alive_every_s=10 ** 9)
        t0 = self.t
        self.run_for(u, 1000, {"A": True}, step=5)
        self.run_for(u, 600, {"A": False}, step=5)
        self.run_for(u, 400, {"A": True}, step=5)
        r = u.report(None, t0 + 2000)["printers"]["a"]
        self.assertAlmostEqual(r["online_s"], 1400, delta=10)
        self.assertAlmostEqual(r["observed_s"], 2000, delta=10)
        self.assertEqual(r["name"], "A")
        clipped = u.report(t0 + 1000, t0 + 2000)["printers"]["a"]                    # only the last 1000 s: 400 up, 600 down
        self.assertAlmostEqual(clipped["online_s"], 400, delta=10)
        self.assertAlmostEqual(clipped["observed_s"], 1000, delta=10)
        self.assertEqual(u.report(None, t0 + 2000, {"b"})["printers"], {})           # name filter

    def test_the_time_the_hub_was_not_running_is_not_counted_as_online(self):
        u1 = self.log(settle_s=45, alive_every_s=60)
        t0 = self.t
        self.run_for(u1, 1000, {"A": True}, step=5)
        u1.stop()
        stopped = self.t
        self.t += 5000                                                              # the Hub was off for 5,000 s
        u2 = self.log(settle_s=45, alive_every_s=60)
        self.run_for(u2, 1000, {"A": True}, step=5)
        r = u2.report(None, self.t)["printers"]["a"]
        self.assertAlmostEqual(r["observed_s"], (stopped - t0) + 1000, delta=15)    # not the 7,000 s on the calendar
        self.assertAlmostEqual(r["online_s"], r["observed_s"], delta=1)

    def test_a_crash_without_a_final_heartbeat_loses_at_most_one_heartbeat_interval(self):
        u1 = self.log(settle_s=45, alive_every_s=600)
        t0 = self.t
        self.run_for(u1, 3000, {"A": True}, step=5)                                  # no stop(): power cut
        self.t += 9000
        u2 = self.log(settle_s=45, alive_every_s=600)
        self.run_for(u2, 100, {"A": False}, step=5)
        r = u2.report(None, self.t)["printers"]["a"]
        self.assertGreater(r["observed_s"], 2400)
        self.assertLess(r["observed_s"], 3100 + 100)

    def test_heartbeat_writes_are_rare(self):
        u = self.log(alive_every_s=600)
        calls = []
        orig = u._write_alive
        u._write_alive = lambda now: (calls.append(now), orig(now))
        self.run_for(u, 2 * H, {"A": True}, step=5)                                  # 1,440 polls
        self.assertLessEqual(len(calls), 14)

    def test_a_removed_printer_stops_being_observed(self):
        u = self.log(settle_s=45)
        self.run_for(u, 200, {"A": True, "B": True})
        self.run_for(u, 200, {"A": True})                                            # B was deleted
        recs = [r for r in self.lines() if r["k"] == "p" and r["n"] == "B"]
        self.assertEqual([r["up"] for r in recs], [1, None])
        r = u.report(None, self.t)["printers"]
        self.assertLess(r["b"]["observed_s"], 250)
        self.assertGreater(r["a"]["observed_s"], 380)

    def test_the_file_is_bounded_and_the_percentages_survive_trimming(self):
        u = self.log(settle_s=1, alive_every_s=10 ** 9, max_bytes=3000)
        t0 = self.t
        for i in range(600):                                                         # a printer that flaps all day
            u.observe({"A": i % 2 == 0, "B": True}, now=self.t)
            u.observe({"A": i % 2 == 0, "B": True}, now=self.t + 2)
            self.t += 60
        size = os.path.getsize(os.path.join(self.dir, uptime.FILE_NAME))
        self.assertLessEqual(size, 3000 + 400)
        recs = self.lines()
        self.assertEqual(recs[0]["k"], "start")                                      # a trimmed log still begins with a session start
        r = u.report(None, self.t)
        self.assertIsNotNone(r["first_t"])
        self.assertGreater(r["first_t"], t0)                                         # the oldest part was dropped
        self.assertAlmostEqual(r["printers"]["b"]["online_s"] / r["printers"]["b"]["observed_s"], 1.0, places=2)
        a = r["printers"]["a"]
        self.assertTrue(0.3 < a["online_s"] / a["observed_s"] < 0.7)

    def test_a_torn_or_odd_line_is_skipped(self):
        u = self.log(settle_s=45)
        self.run_for(u, 200, {"A": True})
        with open(os.path.join(self.dir, uptime.FILE_NAME), "a", encoding="utf8") as fh:
            fh.write('{"t": 17600\nnot json\n[1,2]\n{"t":"x","k":"p"}\n')
        r = u.report(None, self.t)["printers"]["a"]
        self.assertAlmostEqual(r["online_s"], 200, delta=10)

    def test_a_file_that_cannot_be_written_never_raises(self):
        os.makedirs(os.path.join(self.dir, uptime.FILE_NAME))                        # a directory where the file should be
        u = self.log(settle_s=1)
        self.run_for(u, 100, {"A": True})
        self.assertEqual(u.report(None, self.t), {"first_t": None, "printers": {}})

    def test_an_empty_report(self):
        self.assertEqual(self.log().report(None, self.t), {"first_t": None, "printers": {}})


class SummaryUptimeTests(Base):
    def test_percent_online_and_collecting_since_reach_the_summary(self):
        put(self.jl, "A", end=NOW - 3 * D)
        put(self.jl, "B", end=NOW - 3 * D)
        ud = tempfile.mkdtemp(prefix="uptime-s-")
        self.addCleanup(shutil.rmtree, ud, True)
        t = {"now": NOW - 10 * D}
        u = uptime.UptimeLog(ud, clock=lambda: t["now"], settle_s=45, alive_every_s=10 ** 9)
        for _ in range(int(10 * D / 600)):                                           # 10 days, a poll every 10 minutes
            up = not (NOW - 5 * D <= t["now"] < NOW - 4 * D)                         # B is down for one day
            u.observe({"A": True, "B": up}, t["now"])
            t["now"] += 600
        s = analytics.summary(self.jl, u, "30d", None, NOW)
        a, b = self.by_printer(s)["A"], self.by_printer(s)["B"]
        self.assertEqual(a["online_pct"], 100.0)
        self.assertAlmostEqual(b["online_pct"], 90.0, delta=0.5)
        self.assertTrue(s["uptime"]["available"])
        self.assertTrue(s["uptime"]["partial"])                                      # the log is 10 days old; the period is 30
        self.assertAlmostEqual(s["uptime"]["since"], NOW - 10 * D, delta=60)
        s7 = analytics.summary(self.jl, u, "7d", None, NOW)
        self.assertFalse(s7["uptime"]["partial"])                                    # a full week is covered
        self.assertAlmostEqual(self.by_printer(s7)["B"]["online_pct"], 100 * 6 / 7, delta=1.0)
        only_a = analytics.summary(self.jl, u, "30d", ["A"], NOW)
        self.assertEqual([p["printer"] for p in only_a["by_printer"]], ["A"])

    def test_no_uptime_history_yet_is_reported_as_collecting(self):
        put(self.jl, "A")
        ud = tempfile.mkdtemp(prefix="uptime-e-")
        self.addCleanup(shutil.rmtree, ud, True)
        s = analytics.summary(self.jl, uptime.UptimeLog(ud), "30d", None, NOW)
        self.assertFalse(s["uptime"]["available"])
        self.assertTrue(s["uptime"]["partial"])
        self.assertIsNone(self.by_printer(s)["A"]["online_pct"])

    def test_too_little_observed_time_is_not_turned_into_a_percentage(self):
        put(self.jl, "A")
        ud = tempfile.mkdtemp(prefix="uptime-f-")
        self.addCleanup(shutil.rmtree, ud, True)
        u = uptime.UptimeLog(ud, clock=lambda: NOW - 100, settle_s=1)
        u.observe({"A": True}, NOW - 100)
        u.observe({"A": True}, NOW - 90)
        s = analytics.summary(self.jl, u, "30d", None, NOW)
        self.assertIsNone(self.by_printer(s)["A"]["online_pct"])


# ====================================================================== the demo seeder

class SeedTests(unittest.TestCase):
    def test_the_demo_data_has_what_the_view_needs_and_only_fills_an_empty_folder(self):
        import seed_demo_jobs
        d = tempfile.mkdtemp(prefix="analytics-seed-")
        self.addCleanup(shutil.rmtree, d, True)
        n = seed_demo_jobs.seed(d, days=60, seed_value=7, printers=False, now=NOW)
        self.assertGreater(n, 150)
        jl = joblog.JobLog(os.path.join(d, "jobs.sqlite"), tz="UTC", clock=lambda: NOW)
        self.addCleanup(jl.close)
        s = analytics.summary(jl, uptime.UptimeLog(d), "90d", None, NOW)
        self.assertEqual(len(s["by_printer"]), 4)
        self.assertGreater(s["totals"]["failed"], 3)
        self.assertEqual(s["totals"]["running"], 1)
        self.assertTrue(s["uptime"]["available"])
        self.assertGreaterEqual(len(s["by_material"]), 3)
        self.assertGreaterEqual(len(s["failures"]["by_reason"]), 3)
        self.assertTrue(any(f["parts_unknown_prints"] for f in s["top_files"] + s["failures"]["top_files"] + analytics.summary(jl, None, "all", None, NOW)["top_files"]))
        again = seed_demo_jobs.seed(tempfile.mkdtemp(prefix="analytics-seed2-"), days=60, seed_value=7, printers=False, now=NOW)
        self.assertEqual(n, again)                                                  # same seed, same data
        with self.assertRaises(SystemExit):
            seed_demo_jobs.seed(d, printers=False, now=NOW)                         # never overwrites an existing log


# ====================================================================== the route and its scope

class FakePrinter:
    kind = "fake"
    has_camera = False
    uses_files_api = False

    def __init__(self, name):
        self.name = name

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": "IDLE", "job": "", "percent": 0, "remaining_min": None}

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
    server.JOBLOG._exec("DELETE FROM jobs")


def put_printer(name, groups=None):
    server.STORE.add({"name": name, "type": "octoprint", "url": f"http://{name.lower()}.invalid"})
    if groups:
        server.STORE.set_groups(name, groups)
    server.PRINTERS[name.lower()] = FakePrinter(name)


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class RouteTests(unittest.TestCase):
    def setUp(self):
        server.SETTINGS.update({"security.api_auth": "off"})
        reset_fleet()
        self.c = CLIENT
        put_printer("Alpha", ["Work"])
        put_printer("Beta", ["Work", "Home"])
        put_printer("Gamma", ["Home"])
        end = time.time() - 3600
        for name in ("Alpha", "Alpha", "Beta", "Gamma"):
            put(server.JOBLOG, name, end=end)
            end -= 7200

    def tearDown(self):
        server.SETTINGS.update({"security.api_auth": "off"})
        reset_fleet()

    def get(self, path, **kw):
        r = self.c.get(path, **kw)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_the_scope_row_is_read_like_stats(self):
        import security
        self.assertEqual(security.rest_scope("GET", "/api/analytics"), "read")
        self.assertEqual(security.rest_scope("GET", "/api/analytics"), security.rest_scope("GET", "/api/stats"))
        self.assertEqual(security.rest_scope("POST", "/api/analytics"), "control")           # only the GET is listed

    def test_the_answer(self):
        a = self.get("/api/analytics?period=30d")
        self.assertEqual((a["period"], a["totals"]["prints"], a["empty"]), ("30d", 4, False))
        self.assertEqual(sorted(p["printer"] for p in a["by_printer"]), ["Alpha", "Beta", "Gamma"])
        for key in ("totals", "by_printer", "by_material", "failures", "top_files", "trend", "uptime", "currency", "since", "until"):
            self.assertIn(key, a)
        self.assertNotIn("filter", a)
        self.assertEqual(self.get("/api/analytics")["period"], "30d")                       # the default
        self.assertEqual(self.get("/api/analytics?period=1y")["period"], "1y")
        self.assertEqual(self.get("/api/analytics?period=all")["totals"]["prints"], 4)

    def test_a_bad_period_is_400_json(self):
        r = self.c.get("/api/analytics?period=soon")
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())

    def test_the_group_filter_is_the_history_filter(self):
        a = self.get("/api/analytics?period=all&group=Work")
        self.assertEqual((a["totals"]["prints"], a["filter"]["groups"], a["filter"]["resolved_printers"]), (3, ["Work"], ["Alpha", "Beta"]))
        self.assertEqual(sorted(p["printer"] for p in a["by_printer"]), ["Alpha", "Beta"])
        a = self.get("/api/analytics?period=all&group=work&group=home")
        self.assertEqual(a["totals"]["prints"], 4)
        a = self.get("/api/analytics?period=all&printer=Gamma")
        self.assertEqual(a["totals"]["prints"], 1)
        a = self.get("/api/analytics?period=all&group=Nope")
        self.assertEqual((a["totals"]["prints"], a["filter"]["unknown_groups"], a["empty"]), (0, ["Nope"], True))

    def test_a_printer_with_no_prints_is_listed(self):
        put_printer("Idle-One", ["Home"])
        names = [p["printer"] for p in self.get("/api/analytics?period=all")["by_printer"]]
        self.assertIn("Idle-One", names)
        self.assertEqual([p["printer"] for p in self.get("/api/analytics?period=all&group=Home")["by_printer"]], ["Beta", "Gamma", "Idle-One"])

    def test_keys_a_read_key_may_look_no_key_may_not(self):
        server.SETTINGS.update({"security.api_auth": "on"}, clear=["security.dashboard_password"])
        for k in server.AUTH.keys.list_public():
            server.AUTH.delete_key(k["id"])
        _r, read = server.AUTH.create_key("viewer", "read")
        _r, ctl = server.AUTH.create_key("agent", "control")
        self.addCleanup(lambda: [server.AUTH.delete_key(k["id"]) for k in server.AUTH.keys.list_public()])
        self.assertEqual(self.c.get("/api/analytics").status_code, 401)
        self.assertEqual(self.c.get("/api/analytics", headers={"Authorization": "Bearer " + read}).status_code, 200)
        self.assertEqual(self.c.get("/api/analytics?group=Work", headers={"Authorization": "Bearer " + ctl}).status_code, 200)
        self.assertEqual(self.c.get("/api/analytics", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_the_tick_feeds_the_uptime_log_and_only_writes_on_change(self):
        ud = tempfile.mkdtemp(prefix="uptime-r-")
        self.addCleanup(shutil.rmtree, ud, True)
        t = {"now": NOW}
        old = server.UPTIME
        server.UPTIME = uptime.UptimeLog(ud, clock=lambda: t["now"], settle_s=45)
        self.addCleanup(setattr, server, "UPTIME", old)
        for _ in range(200):
            server.UPTIME.observe_fleet(server._fleet_snapshots(), t["now"])
            t["now"] += 5
        self.assertEqual(server.UPTIME.writes, 1 + 3)                                  # start + one line per printer
        a = self.get("/api/analytics?period=all")
        self.assertTrue(a["uptime"]["available"])
        self.assertTrue(all(p["observed_h"] >= 0 for p in a["by_printer"]))
        with open(os.path.join(APP, "server.py"), encoding="utf8") as fh:
            src = fh.read()
        self.assertIn("UPTIME.observe_fleet(fleet)", src[src.index("def _event_tick"):src.index("def _event_loop")])

    def test_it_is_read_only_and_not_a_job_list(self):
        a = self.get("/api/analytics?period=all")
        self.assertNotIn("jobs", a)
        self.assertEqual(self.c.post("/api/analytics").status_code in (401, 403, 404, 405), True)
        self.assertEqual(len(server.JOBLOG.recent_jobs(10)), 4)                          # nothing changed


if __name__ == "__main__":
    unittest.main()
