"""Tests for app/costs.py - the price model, with hand-checked worked examples.

    python3 tests/test_costs.py

Pure arithmetic: no database, no printers, no network.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import costs  # noqa: E402

R = costs.Rates(per_kg={"PLA": 20}, kwh_price=0.15, wear_per_hour=0.30)   # P1S default 120 W


def ok_job(**kw):
    j = {"printer": "P1S", "model": "Bambu Lab P1S", "file": "stand", "result": "finished",
         "started_at": 1000.0, "ended_at": 10000.0, "duration_s": 9000.0, "percent": 100.0,
         "grams_est": 36.0, "material": "PLA", "filaments": [], "parts": 3}
    j.update(kw)
    return j


def bad_job(**kw):
    base = {"result": "failed", "percent": 40.0, "duration_s": 3600.0, "ended_at": 5000.0}
    base.update(kw)
    return ok_job(**base)


class RatesTests(unittest.TestCase):
    def test_defaults(self):
        r = costs.Rates()
        self.assertEqual([r.per_kg[k] for k in ("PLA", "PETG", "ABS", "TPU")], [20, 22, 22, 35])
        self.assertEqual(r.watts["P1S"], 120)
        self.assertEqual(r.watts["A1"], 90)
        self.assertEqual(r.watts["MK4"], 100)
        self.assertEqual(r.default_watts, 120)
        self.assertEqual(r.wear_per_hour, 0.30)

    def test_parse_pairs_and_override(self):
        self.assertEqual(costs.parse_pairs(["pla=18", "bad", "X=oops", "petg = 23.5"]), {"PLA": 18.0, "PETG": 23.5})
        self.assertEqual(costs.parse_pairs("PLA=1, ABS=2"), {"PLA": 1.0, "ABS": 2.0})
        r = costs.Rates(per_kg=["PLA=18"], watts=["Garage=200"])
        self.assertEqual(r.per_kg["PLA"], 18)
        self.assertEqual(r.per_kg["PETG"], 22)            # untouched defaults survive

    def test_from_settings(self):
        vals = {"costs.kwh_price": "0.30", "costs.filament_prices": ["PLA=15"], "costs.currency": "EUR"}
        r = costs.Rates.from_settings(lambda k: vals.get(k))
        self.assertEqual((r.kwh_price, r.per_kg["PLA"], r.currency), (0.30, 15, "EUR"))
        self.assertEqual(r.wear_per_hour, 0.30)           # missing keys -> defaults

    def test_price_lookup(self):
        r = costs.Rates()
        self.assertEqual(costs.price_per_kg(r, "pla"), 20)
        self.assertEqual(costs.price_per_kg(r, "PLA+"), 20)
        self.assertEqual(costs.price_per_kg(r, "PETG-CF"), 22)
        self.assertEqual(costs.price_per_kg(r, "Nylon"), r.default_per_kg)
        self.assertEqual(costs.price_per_kg(r, None), r.default_per_kg)

    def test_watts_lookup(self):
        r = costs.Rates(watts=["Shop-A1=77"])
        self.assertEqual(costs.watts_for(r, "Shop-A1", "Bambu Lab A1"), 77)      # exact printer name
        self.assertEqual(costs.watts_for(r, "bench", "Bambu Lab P1S"), 120)      # model substring
        self.assertEqual(costs.watts_for(r, "Garage MK4", None), 100)            # name substring
        self.assertEqual(costs.watts_for(r, "Mystery", "Voron"), 120)            # generic
        r2 = costs.Rates(watts=["A1 MINI=70"])
        self.assertEqual(costs.watts_for(r2, "x", "Bambu Lab A1 mini"), 70)      # longest key wins

    def test_grams_from_mm(self):
        # 1000 mm of 1.75 mm PLA = pi*(0.0875 cm)^2 * 100 cm * 1.24 g/cm3 = 2.98 g
        self.assertAlmostEqual(costs.grams_from_mm(1000, "PLA"), 2.98, places=2)
        self.assertIsNone(costs.grams_from_mm(None))
        self.assertIsNone(costs.grams_from_mm(0))


class JobCostTests(unittest.TestCase):
    def test_finished_job_worked_example(self):
        # 36 g PLA @ 20/kg = 0.72 ; 2.5 h * 0.120 kW * 0.15 = 0.045 ; wear 2.5 * 0.30 = 0.75
        c = costs.job_cost(ok_job(), R)
        self.assertEqual((c["material"], c["electricity"], c["wear"], c["total"]), (0.72, 0.045, 0.75, 1.515))
        self.assertEqual(c["per_part"], 0.505)
        self.assertFalse(c["partial"])

    def test_failed_job_scales_by_percent(self):
        # 40 % of 36 g = 14.4 g -> 0.288 ; 1 h -> 0.018 elec + 0.30 wear
        c = costs.job_cost(bad_job(), R)
        self.assertEqual(c["grams_used"], 14.4)
        self.assertEqual((c["material"], c["electricity"], c["wear"], c["total"]), (0.288, 0.018, 0.3, 0.606))
        self.assertIsNone(c["per_part"])                  # a failure yields no good parts

    def test_failed_without_percent_is_not_guessed(self):
        c = costs.job_cost(bad_job(percent=None), R)
        self.assertIsNone(c["grams_used"])
        self.assertTrue(c["partial"])
        self.assertEqual(c["material"], 0)
        self.assertEqual(c["total"], 0.318)               # time still counts

    def test_per_filament_prices(self):
        j = ok_job(grams_est=30, filaments=[{"type": "PLA", "used_g": 20}, {"type": "TPU", "used_g": 10}])
        c = costs.job_cost(j, R)
        self.assertEqual(c["material"], round(0.020 * 20 + 0.010 * 35, 4))       # 0.75
        self.assertEqual(costs.material_grams(j), {"PLA": 20.0, "TPU": 10.0})

    def test_filament_list_rescaled_to_plate_weight(self):
        j = ok_job(grams_est=30, filaments=[{"type": "PLA", "used_g": 10}, {"type": "TPU", "used_g": 10}])
        mg = costs.material_grams(j)
        self.assertAlmostEqual(sum(mg.values()), 30.0, places=2)

    def test_duration_falls_back_to_estimate(self):
        j = ok_job(duration_s=None, est_seconds=7200)
        self.assertEqual(costs.machine_hours(j), 2.0)


class AggregateTests(unittest.TestCase):
    def jobs(self):
        return [ok_job(ended_at=10000.0), ok_job(ended_at=20000.0), ok_job(ended_at=30000.0), bad_job()]

    def test_true_cost_per_part_worked_example(self):
        # 3 good prints * 1.515 + 1 failure * 0.606 = 5.151 over 9 good parts = 0.5723 per part
        a = costs.aggregate(self.jobs(), R)
        self.assertEqual((a["finished"], a["failed"], a["good_parts"]), (3, 1, 9))
        self.assertEqual(a["success_rate"], 0.75)
        self.assertAlmostEqual(a["cost"]["total"], 5.151, delta=0.006)
        self.assertAlmostEqual(a["cost"]["wasted_on_failures"], 0.606, delta=0.006)
        self.assertAlmostEqual(a["true_cost_per_part"], 0.5723, places=4)
        self.assertAlmostEqual(a["clean_cost_per_part"], 0.505, places=4)
        self.assertAlmostEqual(a["waste_per_part"], 0.0673, places=4)
        self.assertAlmostEqual(a["true_cost_per_part"], a["clean_cost_per_part"] + a["waste_per_part"], places=4)

    def test_no_success_has_no_per_part(self):
        a = costs.aggregate([bad_job()], R)
        self.assertIsNone(a["true_cost_per_part"])
        self.assertAlmostEqual(a["cost"]["wasted_on_failures"], 0.606, delta=0.006)
        self.assertEqual(a["success_rate"], 0.0)

    def test_running_and_unknown_not_costed(self):
        run = ok_job(result="running", ended_at=None)
        unk = ok_job(result="unknown")
        a = costs.aggregate([run, unk, ok_job()], R)
        self.assertEqual((a["running"], a["unknown"], a["finished"]), (1, 1, 1))
        self.assertAlmostEqual(a["cost"]["total"], 1.515, delta=0.006)   # only the finished job
        self.assertEqual(a["success_rate"], 1.0)          # unknown does not drag the rate down

    def test_cancelled_counts_as_failure(self):
        a = costs.aggregate([ok_job(), bad_job(result="cancelled")], R)
        self.assertEqual(a["success_rate"], 0.5)
        self.assertEqual(a["cancelled"], 1)

    def test_empty(self):
        a = costs.aggregate([], R)
        self.assertEqual(a["jobs"], 0)
        self.assertIsNone(a["success_rate"])
        self.assertIsNone(a["true_cost_per_part"])

    def test_file_costs_window(self):
        # newest 2 attempts only: ended 30000 (ok) and 20000 (ok) -> no failure inside the window
        a = costs.file_costs(self.jobs(), R, last_n=2)
        self.assertEqual(a["attempts"], 2)
        self.assertEqual(a["failed"], 0)
        self.assertAlmostEqual(a["true_cost_per_part"], 0.505, places=4)
        self.assertEqual(a["avg_parts_per_print"], 3.0)
        self.assertAlmostEqual(a["grams_per_part"], 12.0, places=3)
        self.assertAlmostEqual(a["hours_per_part"], 2.5 / 3, places=3)
        full = costs.file_costs(self.jobs(), R, last_n=20)
        self.assertEqual(full["attempts"], 4)
        self.assertAlmostEqual(full["true_cost_per_part"], 0.5723, places=4)

    def test_file_key(self):
        self.assertEqual(costs.file_key("Benchy.gcode.3mf"), "benchy")
        self.assertEqual(costs.file_key("BENCHY.3MF"), "benchy")
        self.assertEqual(costs.file_key("a/b.gcode"), "a/b")
        self.assertEqual(costs.file_key(None), "")


class SeriesTests(unittest.TestCase):
    def test_buckets(self):
        def ts(y, m, d, h=12):
            return datetime.datetime(y, m, d, h, tzinfo=datetime.timezone.utc).timestamp()
        jobs = [ok_job(ended_at=ts(2026, 10, 5), grams_est=10),               # Monday
                ok_job(ended_at=ts(2026, 10, 7), grams_est=20, material="PETG"),
                ok_job(ended_at=ts(2026, 11, 2), grams_est=5),
                ok_job(result="unknown", ended_at=ts(2026, 10, 6), grams_est=999)]
        day = costs.material_series(jobs, "day")
        self.assertEqual([b["bucket"] for b in day], ["2026-10-05", "2026-10-07", "2026-11-02"])
        wk = costs.material_series(jobs, "week")
        self.assertEqual([b["bucket"] for b in wk], ["2026-10-05", "2026-11-02"])
        self.assertEqual(wk[0]["grams"], 30.0)
        self.assertEqual(wk[0]["by_material"], {"PLA": 10.0, "PETG": 20.0})
        mo = costs.material_series(jobs, "month")
        self.assertEqual([(b["bucket"], b["grams"]) for b in mo], [("2026-10", 30.0), ("2026-11", 5.0)])

    def test_timezone_moves_the_day(self):
        import zoneinfo
        t = datetime.datetime(2026, 10, 5, 2, 0, tzinfo=datetime.timezone.utc).timestamp()   # 22:00 on the 4th in NY
        s = costs.material_series([ok_job(ended_at=t)], "day", zoneinfo.ZoneInfo("America/New_York"))
        self.assertEqual(s[0]["bucket"], "2026-10-04")


if __name__ == "__main__":
    unittest.main()
