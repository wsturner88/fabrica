"""Tests for app/joblog.py - part-count parser, job lifecycle from event sequences, restarts and
orphans, stats, CSV and migrations.

    python3 tests/test_joblog.py

SQLite in a throwaway temp dir, a fake clock, and no printer or network anywhere.
"""
import csv
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import costs  # noqa: E402
import events  # noqa: E402
import joblog  # noqa: E402

T0 = 1_760_000_000.0     # an arbitrary fixed "now"


def plate_xml(index, n_objects, weight="36.00", prediction="9000", skipped=0, model="C12", extra=""):
    objs = "".join(f'<object identify_id="{i}" name="part {i}" skipped="{"true" if i < skipped else "false"}" />'
                   for i in range(n_objects))
    return (f'<plate><metadata key="index" value="{index}"/><metadata key="printer_model_id" value="{model}"/>'
            f'<metadata key="prediction" value="{prediction}"/><metadata key="weight" value="{weight}"/>'
            f'{objs}<filament id="1" tray_info_idx="GFA00" type="PLA" color="#FF8000" used_m="12.1" used_g="{weight}" />'
            f'{extra}</plate>')


def slice_xml(*plates):
    return '<?xml version="1.0" encoding="UTF-8"?><config><header><header_item key="X" value="1"/></header>' \
           + "".join(plates) + "</config>"


def make_3mf(xml):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", xml)
    return buf.getvalue()


# ============================================================================ parser

class ParserTests(unittest.TestCase):
    def test_one_plate_three_parts(self):
        info = joblog.parse_slice_info(slice_xml(plate_xml(1, 3)))
        self.assertIsNone(info["error"])
        self.assertEqual(len(info["plates"]), 1)
        p = info["plates"][0]
        self.assertEqual((p["index"], p["parts"], p["weight_g"], p["prediction_s"], p["printer_model_id"]),
                         (1, 3, 36.0, 9000, "C12"))
        self.assertEqual(p["filaments"][0]["type"], "PLA")
        self.assertEqual(p["filaments"][0]["color"], "#FF8000")

    def test_single_part(self):
        self.assertEqual(joblog.parts_for_plate(joblog.parse_slice_info(slice_xml(plate_xml(1, 1)))), 1)

    def test_three_plates_counted_separately(self):
        info = joblog.parse_slice_info(slice_xml(plate_xml(1, 3), plate_xml(2, 2), plate_xml(3, 2)))
        self.assertEqual([p["parts"] for p in info["plates"]], [3, 2, 2])
        self.assertEqual([p["index"] for p in info["plates"]], [1, 2, 3])
        self.assertEqual(joblog.parts_for_plate(info, 2), 2)
        self.assertEqual(joblog.parts_for_plate(info, 3), 2)
        self.assertEqual(joblog.parts_for_plate(info, 9), 3)       # unknown plate -> first plate
        self.assertEqual(joblog.parts_for_plate(info), 3)

    def test_skipped_objects_not_counted(self):
        info = joblog.parse_slice_info(slice_xml(plate_xml(1, 4, skipped=1)))
        self.assertEqual(info["plates"][0]["parts"], 3)

    def test_plate_with_no_objects_is_unknown_not_zero(self):
        info = joblog.parse_slice_info(slice_xml(plate_xml(1, 0)))
        self.assertIsNone(info["plates"][0]["parts"])

    def test_malformed_truncated_still_counts(self):
        xml = slice_xml(plate_xml(1, 3), plate_xml(2, 2))
        cut = xml[: xml.index("</plate>", xml.index("</plate>") + 1) - 12]      # chop inside plate 2
        info = joblog.parse_slice_info(cut)
        self.assertIn("malformed", info["error"])
        self.assertEqual([p["parts"] for p in info["plates"]], [3, 2])

    def test_malformed_attribute_junk(self):
        info = joblog.parse_slice_info("<config><plate><object name=\"a\"><object name=\"b\" <<< garbage")
        self.assertEqual(info["plates"][0]["parts"], 2)

    def test_garbage_and_empty_never_raise(self):
        for bad in ("", "   ", None, b"\xff\xfe\x00", "not xml at all", "<config></config>", 12345):
            info = joblog.parse_slice_info(bad)
            self.assertEqual(info["plates"], [], bad)
            self.assertTrue(info["error"], bad)

    def test_entity_bomb_is_not_expanded(self):
        evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><config>' + plate_xml(1, 2) + '</config>'
        info = joblog.parse_slice_info(evil)
        self.assertEqual(info["plates"][0]["parts"], 2)           # read by the lenient scanner instead

    def test_from_3mf_zip(self):
        info = joblog.slice_info_from_3mf(make_3mf(slice_xml(plate_xml(1, 3))))
        self.assertEqual(info["plates"][0]["parts"], 3)

    def test_from_bad_zip_and_missing_member(self):
        self.assertEqual(joblog.slice_info_from_3mf(b"PK nope")["plates"], [])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Metadata/other.txt", "x")
        self.assertEqual(joblog.slice_info_from_3mf(buf.getvalue())["error"], "no slice_info.config")


class ResolverHelperTests(unittest.TestCase):
    def test_resolved_from_3mf(self):
        r = joblog.resolved_from_3mf(make_3mf(slice_xml(plate_xml(1, 3), plate_xml(2, 2, weight="10"))), plate=2)
        self.assertEqual((r["parts"], r["grams"], r["seconds"], r["material"], r["colour"], r["plate"]),
                         (2, 10.0, 9000.0, "PLA", "#FF8000", 2))
        self.assertEqual(r["parts_source"], "slice_info")

    def test_resolved_from_cached_meta(self):
        meta = {"printer_model_id": "C12", "print_seconds": 3600, "weight_g": 20.0,
                "filaments": [{"id": 1, "type": "PETG", "color": "#00FF00", "used_g": 20.0}]}
        r = joblog.resolved_from_cached_meta(meta, parts=4)
        self.assertEqual((r["parts"], r["grams"], r["material"], r["model"]), (4, 20.0, "PETG", "C12"))
        self.assertIsNone(joblog.resolved_from_cached_meta(meta)["parts"])        # no count -> unknown
        self.assertEqual(joblog.resolved_from_cached_meta({"error": "no slice_info.config"}), {})

    def test_resolved_from_file_entry(self):
        r = joblog.resolved_from_file_entry({"name": "x.gcode", "print_seconds": 1800, "filament_mm": 10000},
                                            material="PETG", kind="octoprint")
        self.assertEqual((r["parts"], r["parts_source"], r["material"], r["kind"]), (1, "assumed", "PETG", "octoprint"))
        self.assertAlmostEqual(r["grams"], 30.55, delta=0.02)
        self.assertEqual(joblog.resolved_from_file_entry("junk"), {})


# ============================================================================ lifecycle

def ev(kind, printer="P1S", ts=T0, **data):
    return {"kind": kind, "printer": printer, "ts": ts, "data": data}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="joblog-test-")
        self.path = os.path.join(self.tmp, "jobs.sqlite")
        self.now = T0
        self.calls = []
        self.files = {"stand": {"grams": 36.0, "seconds": 9000, "parts": 3, "material": "PLA", "colour": "#FF8000",
                                "model": "Bambu Lab P1S", "kind": "bambu", "parts_source": "slice_info"}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def resolver(self, printer, job):
        self.calls.append((printer, job))
        return self.files.get(costs.file_key(job))

    def make(self, **kw):
        kw.setdefault("resolver", self.resolver)
        kw.setdefault("rates", costs.Rates(per_kg={"PLA": 20}, kwh_price=0.15, wear_per_hour=0.30))
        return joblog.JobLog(self.path, clock=lambda: self.now, **kw)


class LifecycleTests(Base):
    def test_start_then_finish(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        row = jl.job(jid)
        self.assertEqual((row["result"], row["open"], row["parts"], row["grams_est"], row["material"]),
                         ("running", True, 3, 36.0, "PLA"))
        self.assertIsNone(row["cost"])
        jl.on_event(ev("print_paused", ts=T0 + 600, job="stand", percent=10))
        self.assertEqual(jl.job(jid)["pauses"], 1)
        self.assertEqual(jl.on_event(ev("print_finished", ts=T0 + 9000, job="stand", seconds=9000)), jid)
        row = jl.job(jid)
        self.assertEqual((row["result"], row["percent"], row["duration_s"], row["grams_used"], row["open"]),
                         ("finished", 100.0, 9000.0, 36.0, False))
        self.assertEqual(row["cost"]["total"], 1.515)
        self.assertEqual(row["cost"]["per_part"], 0.505)
        self.assertEqual(row["time_vs_estimate_pct"], 0.0)
        self.assertEqual(len(jl.recent_jobs()), 1)

    def test_start_then_fail_at_40_percent_scales_grams(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        jl.on_event(ev("print_failed", ts=T0 + 3600, job="stand", percent=40.0, cancelled=False,
                       error="printer error code 0x0300400C"))
        row = jl.job(jid)
        self.assertEqual((row["result"], row["percent"], row["grams_used"], row["duration_s"]),
                         ("failed", 40.0, 14.4, 3600.0))
        self.assertEqual(row["error"], "printer error code 0x0300400C")
        self.assertEqual(row["cost"]["total"], 0.606)
        self.assertIsNone(row["cost"]["per_part"])

    def test_cancelled_is_its_own_result(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", job="stand"))
        jl.on_event(ev("print_failed", ts=T0 + 100, job="stand", percent=5, cancelled=True, error=""))
        self.assertEqual(jl.job(jid)["result"], "cancelled")

    def test_failure_without_percent_uses_last_observed(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", job="stand"))
        jl.observe({"P1S": {"connected": True, "state": "RUNNING", "job": "stand", "percent": 25}}, now=T0 + 500)
        jl.on_event(ev("print_failed", ts=T0 + 900, job="stand", percent=None, cancelled=True))
        row = jl.job(jid)
        self.assertEqual((row["percent"], row["grams_used"]), (25.0, 9.0))

    def test_failure_with_no_progress_known_leaves_grams_unknown(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", job="stand"))
        self.assertEqual(jl.job(jid)["percent"], 0.0)       # starts at 0, so a cancel is "0 g used"
        jid2 = jl.on_event(ev("print_failed", printer="A1", ts=T0 + 50, job="stand", percent=None))
        self.assertIsNone(jl.job(jid2)["grams_used"])        # late row, no progress at all -> not guessed

    def test_duplicate_start_event_is_idempotent(self):
        jl = self.make()
        a = jl.on_event(ev("print_started", ts=T0, job="stand"))
        b = jl.on_event(ev("print_started", ts=T0 + 5, job="Stand.gcode.3mf"))
        self.assertEqual(a, b)
        self.assertEqual(len(jl.recent_jobs()), 1)

    def test_new_start_supersedes_unclosed_job(self):
        jl = self.make()
        a = jl.on_event(ev("print_started", ts=T0, job="stand"))
        b = jl.on_event(ev("print_started", ts=T0 + 7200, job="other"))
        self.assertNotEqual(a, b)
        old = jl.job(a)
        self.assertEqual((old["result"], old["open"]), ("unknown", False))
        self.assertIn("superseded", old["error"])
        self.assertTrue(jl.job(b)["open"])

    def test_two_printers_do_not_mix(self):
        jl = self.make()
        a = jl.on_event(ev("print_started", printer="P1S", job="stand"))
        b = jl.on_event(ev("print_started", printer="A1", job="stand"))
        jl.on_event(ev("print_finished", printer="A1", ts=T0 + 100, job="stand"))
        self.assertTrue(jl.job(a)["open"])
        self.assertEqual(jl.job(b)["result"], "finished")

    def test_unrelated_events_ignored(self):
        jl = self.make()
        for kind in ("printer_offline", "backup_done", "spaghetti_suspected", "nonsense"):
            self.assertIsNone(jl.on_event(ev(kind, job="stand")))
        self.assertIsNone(jl.on_event({"kind": "print_started"}))        # no printer
        self.assertIsNone(jl.on_event(object()))                          # garbage: logged, not raised
        self.assertEqual(jl.recent_jobs(), [])

    def test_accepts_real_event_objects(self):
        jl = self.make()
        e = events.Event("print_started", "P1S", "t", "b", ts=T0, data={"job": "stand"})
        self.assertIsNotNone(jl.on_event(e))

    def test_unnamed_job_label_placeholder(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", job="the current job"))
        self.assertIsNone(jl.job(jid)["file"])

    def test_resolver_failure_never_breaks_logging(self):
        def boom(printer, job):
            raise RuntimeError("cache exploded")
        jl = self.make(resolver=boom)
        jid = jl.on_event(ev("print_started", job="stand"))
        self.assertIsNotNone(jid)
        self.assertIsNone(jl.job(jid)["grams_est"])

    def test_late_resolve_at_finish(self):
        self.files.clear()
        jl = self.make()
        jid = jl.on_event(ev("print_started", job="stand"))
        self.assertIsNone(jl.job(jid)["grams_est"])
        self.files["stand"] = {"grams": 36.0, "seconds": 9000, "parts": 3, "material": "PLA"}   # cache warmed meanwhile
        jl.on_event(ev("print_finished", ts=T0 + 9000, job="stand", seconds=9000))
        row = jl.job(jid)
        self.assertEqual((row["grams_est"], row["grams_used"], row["parts"]), (36.0, 36.0, 3))


class RestartTests(Base):
    def test_restart_mid_print_does_not_duplicate(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        jl2 = self.make()                           # Hub restarted: new object, same database
        # the new EventWatcher baselines the running print silently: no start event, then the end arrives
        jl2.on_event(ev("print_finished", ts=T0 + 9000, job="stand", seconds=None))
        rows = jl2.recent_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["result"], rows[0]["duration_s"]), (jid, "finished", 9000.0))

    def test_restart_replaying_the_start_is_ignored(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        jl2 = self.make()
        self.assertEqual(jl2.on_event(ev("print_started", ts=T0 + 3000, job="stand")), jid)
        self.assertEqual(len(jl2.recent_jobs()), 1)

    def test_end_without_any_start_logs_a_late_row(self):
        jl = self.make()
        jid = jl.on_event(ev("print_finished", ts=T0 + 9000, job="stand", seconds=9000))
        row = jl.job(jid)
        self.assertEqual((row["source"], row["result"], row["started_at"]), ("late", "finished", T0))
        self.assertEqual(row["cost"]["total"], 1.515)

    def test_late_row_without_duration_has_unknown_start(self):
        jl = self.make()
        row = jl.job(jl.on_event(ev("print_finished", ts=T0, job="stand", seconds=None)))
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["duration_s"])
        self.assertEqual(row["cost"]["hours"], 2.5)     # no duration: priced at the estimate x progress

    def test_duplicate_end_event_not_double_counted(self):
        jl = self.make()
        jl.on_event(ev("print_started", ts=T0, job="stand"))
        a = jl.on_event(ev("print_finished", ts=T0 + 9000, job="stand", seconds=9000))
        b = jl.on_event(ev("print_finished", ts=T0 + 9010, job="stand", seconds=9010))
        self.assertEqual(a, b)
        self.assertEqual(len(jl.recent_jobs()), 1)

    def test_end_of_a_different_job_does_not_close_the_open_one(self):
        jl = self.make()
        a = jl.on_event(ev("print_started", ts=T0, job="stand"))
        b = jl.on_event(ev("print_finished", ts=T0 + 100, job="other", seconds=100))
        self.assertNotEqual(a, b)
        self.assertTrue(jl.job(a)["open"])             # the stale row is left for observe() to sweep
        self.assertEqual(jl.job(b)["source"], "late")


class OrphanTests(Base):
    def snap(self, state="IDLE", job="", percent=None, connected=True):
        return {"P1S": {"connected": connected, "state": state, "job": job, "percent": percent}}

    def test_orphan_closed_when_printer_idle(self):
        jl = self.make(orphan_grace_s=120)
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        jl.observe(self.snap("RUNNING", "stand", 60), now=T0 + 5000)         # last seen active
        self.assertEqual(jl.observe(self.snap("IDLE"), now=T0 + 6000), [])    # idle, within grace
        self.assertEqual(jl.observe(self.snap("IDLE"), now=T0 + 6130), [jid])
        row = jl.job(jid)
        self.assertEqual((row["result"], row["open"], row["percent"]), ("unknown", False, 60.0))
        self.assertEqual(row["ended_at"], T0 + 5000)                           # ended when last seen, not "now"
        self.assertEqual(row["duration_s"], 5000)
        self.assertIn("seen idle", row["error"])

    def test_active_printer_keeps_job_open_and_resets_grace(self):
        jl = self.make(orphan_grace_s=120)
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        jl.observe(self.snap("IDLE"), now=T0 + 100)                            # a blip
        jl.observe(self.snap("RUNNING", "stand", 10), now=T0 + 150)
        self.assertEqual(jl.observe(self.snap("IDLE"), now=T0 + 200), [])      # grace restarts
        self.assertTrue(jl.job(jid)["open"])

    def test_late_end_event_upgrades_a_swept_orphan(self):
        jl = self.make(orphan_grace_s=0)
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        jl.observe(self.snap("IDLE"), now=T0 + 8990)
        jl.observe(self.snap("IDLE"), now=T0 + 9000)
        self.assertEqual(jl.job(jid)["result"], "unknown")
        self.assertEqual(jl.on_event(ev("print_finished", ts=T0 + 9005, job="stand", seconds=9005)), jid)
        row = jl.job(jid)
        self.assertEqual((row["result"], row["percent"], row["grams_used"]), ("finished", 100.0, 36.0))
        self.assertEqual(len(jl.recent_jobs()), 1)

    def test_unreachable_printer_is_left_alone_until_stale(self):
        jl = self.make(stale_after_s=3600)
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        self.assertEqual(jl.observe(self.snap("UNKNOWN", connected=False), now=T0 + 1800), [])
        self.assertTrue(jl.job(jid)["open"])
        self.assertEqual(jl.observe({}, now=T0 + 4000), [jid])                 # printer removed from the fleet
        self.assertIn("unreachable", jl.job(jid)["error"])

    def test_printer_moves_to_another_job(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        self.assertEqual(jl.observe(self.snap("RUNNING", "other", 3), now=T0 + 60), [jid])
        self.assertIn("different job", jl.job(jid)["error"])

    def test_progress_updates_are_throttled(self):
        jl = self.make(touch_every_s=60)
        jid = jl.on_event(ev("print_started", ts=T0, job="stand"))
        jl.observe(self.snap("RUNNING", "stand", 10), now=T0 + 70)
        jl.observe(self.snap("RUNNING", "stand", 11), now=T0 + 80)             # < 60 s later: not written
        self.assertEqual(jl.job(jid)["percent"], 10.0)
        jl.observe(self.snap("RUNNING", "stand", 12), now=T0 + 140)
        self.assertEqual(jl.job(jid)["percent"], 12.0)

    def test_observe_never_raises(self):
        jl = self.make()
        jl.on_event(ev("print_started", job="stand"))
        self.assertEqual(jl.observe({"P1S": "garbage"}, now=T0 + 10), [])
        self.assertEqual(jl.observe(None, now=T0 + 10), [])


class EndToEndTests(Base):
    """EventWatcher -> JobLog, the way the Hub will wire it."""

    def watcher(self, jl):
        return events.EventWatcher(jl.on_event, clock=lambda: self.now, confirm_s=0)

    @staticmethod
    def fleet(state, job="stand", percent=None, **kw):
        s = {"printer": "P1S", "connected": True, "state": state, "job": job, "percent": percent,
             "remaining_min": None, "print_error": 0, "error": None, "nozzle_c": 25, "bed_c": 24}
        s.update(kw)
        return {"P1S": s}

    def test_watcher_drives_the_log(self):
        jl = self.make()
        w = self.watcher(jl)
        w.update(self.fleet("IDLE", job=""), now=T0)
        w.update(self.fleet("RUNNING", percent=1), now=T0 + 10)
        w.update(self.fleet("RUNNING", percent=50), now=T0 + 4000)
        w.update(self.fleet("FINISH", percent=100), now=T0 + 9010)
        rows = jl.recent_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["result"], rows[0]["parts"]), ("finished", 3))
        self.assertAlmostEqual(rows[0]["duration_s"], 9000, delta=1)

    def test_watcher_failure_path(self):
        jl = self.make()
        w = self.watcher(jl)
        w.update(self.fleet("IDLE", job=""), now=T0)
        w.update(self.fleet("RUNNING", percent=1), now=T0 + 10)
        w.update(self.fleet("RUNNING", percent=40), now=T0 + 3600)
        w.update(self.fleet("FAILED", percent=40, print_error=0x0300400C), now=T0 + 3700)
        row = jl.recent_jobs()[0]
        self.assertEqual((row["result"], row["percent"]), ("failed", 40.0))
        self.assertIn("0x0300400C", row["error"])

    def test_watcher_restart_mid_print(self):
        jl = self.make()
        w = self.watcher(jl)
        w.update(self.fleet("IDLE", job=""), now=T0)
        w.update(self.fleet("RUNNING", percent=1), now=T0 + 10)
        w2 = self.watcher(jl)                         # Hub restart: fresh watcher baselines the running print
        w2.update(self.fleet("RUNNING", percent=30), now=T0 + 3000)
        w2.update(self.fleet("FINISH", percent=100), now=T0 + 9000)
        self.assertEqual(len(jl.recent_jobs()), 1)
        self.assertEqual(jl.recent_jobs()[0]["result"], "finished")


# ============================================================================ queries

class QueryTests(Base):
    def seed(self, jl):
        """P1S: 3 good prints of `stand` + 1 failure at 40 %; A1: 1 good print of `vase`, 1 cancelled."""
        self.files["vase"] = {"grams": 20.0, "seconds": 3600, "parts": 1, "material": "PETG", "kind": "bambu"}
        t = T0
        for i in range(3):
            jl.on_event(ev("print_started", ts=t, job="stand"))
            jl.on_event(ev("print_finished", ts=t + 9000, job="stand", seconds=9000))
            t += 10000
        jl.on_event(ev("print_started", ts=t, job="stand"))
        jl.on_event(ev("print_failed", ts=t + 3600, job="stand", percent=40, cancelled=False, error="jam"))
        t += 4000
        jl.on_event(ev("print_started", printer="A1", ts=t, job="vase"))
        jl.on_event(ev("print_finished", printer="A1", ts=t + 3600, job="vase", seconds=3600))
        t += 4000
        jl.on_event(ev("print_started", printer="A1", ts=t, job="vase"))
        jl.on_event(ev("print_failed", printer="A1", ts=t + 600, job="vase", percent=10, cancelled=True))
        self.end = t + 600

    def test_recent_jobs_filters_and_order(self):
        jl = self.make()
        self.seed(jl)
        rows = jl.recent_jobs(limit=100)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["result"], "cancelled")                      # newest first
        self.assertEqual({r["printer"] for r in jl.recent_jobs(printer="a1")}, {"A1"})
        self.assertEqual(len(jl.recent_jobs(limit=2)), 2)
        self.assertEqual(len(jl.recent_jobs(result="failed")), 1)
        with self.assertRaises(ValueError):
            jl.recent_jobs(result="exploded")

    def test_job_lookup(self):
        jl = self.make()
        self.seed(jl)
        self.assertEqual(jl.job(1)["file"], "stand")
        self.assertIsNone(jl.job(999))

    def test_stats_all(self):
        jl = self.make()
        self.seed(jl)
        s = jl.stats("all", now=self.end + 10)
        self.assertEqual((s["jobs"], s["finished"], s["failed"], s["cancelled"]), (6, 4, 1, 1))
        self.assertAlmostEqual(s["success_rate"], 4 / 6, places=4)
        self.assertEqual(set(s["by_printer"]), {"A1", "P1S"})
        self.assertEqual(s["by_printer"]["P1S"]["success_rate"], 0.75)
        self.assertEqual(s["by_printer"]["A1"]["success_rate"], 0.5)
        self.assertEqual(s["by_printer"]["P1S"]["good_parts"], 9)
        self.assertAlmostEqual(s["by_printer"]["P1S"]["true_cost_per_part"], 0.5723, places=4)
        self.assertEqual(s["by_material"]["PLA"], round(36 * 3 + 14.4, 2))
        self.assertAlmostEqual(s["by_material"]["PETG"], 20 + 2.0, places=2)
        files = {f["file"]: f for f in s["by_file"]}
        self.assertEqual(set(files), {"stand", "vase"})
        self.assertEqual(files["stand"]["good_parts"], 9)
        self.assertIn("material_series", s)

    def test_stats_per_printer_and_period(self):
        jl = self.make()
        self.seed(jl)
        s = jl.stats("all", printer="P1S", now=self.end + 10)
        self.assertEqual(s["jobs"], 4)
        self.assertEqual(set(s["by_printer"]), {"P1S"})
        recent = jl.stats("1d", now=self.end + 10)                              # whole seed is < 1 day old
        self.assertEqual(recent["jobs"], 6)
        old = jl.stats("day", now=self.end + 3 * 86400)
        self.assertEqual(old["jobs"], 0)
        self.assertIsNone(old["success_rate"])
        with self.assertRaises(ValueError):
            jl.stats("fortnight")

    def test_file_costs(self):
        jl = self.make()
        self.seed(jl)
        f = jl.file_costs("Stand.gcode.3mf")
        self.assertEqual((f["attempts"], f["finished"], f["failed"], f["good_parts"]), (4, 3, 1, 9))
        self.assertAlmostEqual(f["true_cost_per_part"], 0.5723, places=4)
        self.assertAlmostEqual(f["clean_cost_per_part"], 0.505, places=4)
        self.assertEqual(jl.file_costs("stand", last_n=2)["attempts"], 2)
        self.assertEqual(jl.file_costs("nothing")["attempts"], 0)
        self.assertEqual(jl.file_costs("stand", printer="A1")["attempts"], 0)
        with self.assertRaises(ValueError):
            jl.file_costs("")

    def test_repricing_applies_to_history(self):
        jl = self.make()
        self.seed(jl)
        before = jl.file_costs("stand")["true_cost_per_part"]
        jl._rates = costs.Rates(per_kg={"PLA": 40}, kwh_price=0.15, wear_per_hour=0.30)
        self.assertGreater(jl.file_costs("stand")["true_cost_per_part"], before)

    def test_material_usage_buckets(self):
        jl = self.make()
        self.seed(jl)
        days = jl.material_usage("day", "all", now=self.end + 10)
        self.assertEqual(sum(d["grams"] for d in days), round(36 * 3 + 14.4 + 20 + 2, 2))
        self.assertEqual(len(jl.material_usage("month", "all", now=self.end + 10)), 1)
        with self.assertRaises(ValueError):
            jl.material_usage("hour")

    def test_export_csv(self):
        jl = self.make()
        self.seed(jl)
        jl.on_event(ev("print_started", printer="A1", ts=self.end + 100, job="=HYPERLINK(\"x\")"))
        text = jl.export_csv(now=self.end + 200)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 7)
        self.assertEqual(list(rows[0]), joblog.JobLog.CSV_COLUMNS)
        first = rows[0]
        self.assertEqual((first["printer"], first["file"], first["result"], first["parts"]), ("P1S", "stand", "finished", "3"))
        self.assertEqual(first["total_cost"], "1.515")
        self.assertEqual(first["cost_per_part"], "0.505")
        failed = [r for r in rows if r["result"] == "failed"][0]
        self.assertEqual((failed["grams_used"], failed["cost_per_part"], failed["error"]), ("14.4", "", "jam"))
        injected = [r for r in rows if r["result"] == "running"][0]
        self.assertTrue(injected["file"].startswith("'="))                      # formula neutralised
        self.assertEqual(injected["total_cost"], "")                            # open job: no cost yet
        only = jl.export_csv(printer="A1", now=self.end + 200)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(only)))), 3)
        buf = io.StringIO()
        jl.export_csv(fp=buf, now=self.end + 200)
        self.assertEqual(buf.getvalue(), text)

    def test_export_csv_when_a_job_has_filaments(self):
        """Regression: a row that carries a filament list (what the Hub's resolver returns) must export."""
        self.files["bracket"] = {"grams": 10.0, "seconds": 3600, "parts": 2, "material": "PETG", "colour": "#0A2989",
                                 "filaments": [{"type": "PETG", "color": "#0A2989", "used_g": 10.0}], "kind": "bambu"}
        jl = self.make()
        jl.on_event(ev("print_started", ts=T0, job="bracket"))
        jl.on_event(ev("print_finished", ts=T0 + 3600, job="bracket", seconds=3600))
        rows = list(csv.DictReader(io.StringIO(jl.export_csv(now=T0 + 4000))))
        self.assertEqual((len(rows), rows[0]["material"], rows[0]["parts"], rows[0]["result"]),
                         (1, "PETG", "2", "finished"))
        self.assertEqual(jl.recent_jobs()[0]["filaments"][0]["type"], "PETG")


# ============================================================================ migrations

class MigrationTests(Base):
    def versions(self):
        c = sqlite3.connect(self.path)
        try:
            return [r[0] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version")]
        finally:
            c.close()

    def test_idempotent(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", job="stand"))
        for _ in range(3):
            jl.migrate()
            self.make()                                    # re-opening the file re-runs migrate()
        self.assertEqual(self.versions(), [1])
        self.assertEqual(jl.schema_version(), 1)
        self.assertEqual(jl.job(jid)["file"], "stand")      # data survived

    def test_new_migration_applies_once_and_keeps_data(self):
        jl = self.make()
        jid = jl.on_event(ev("print_started", job="stand"))

        class Next(joblog.JobLog):
            MIGRATIONS = list(joblog.MIGRATIONS) + [(2, "ALTER TABLE jobs ADD COLUMN note TEXT")]

        n = Next(self.path, clock=lambda: self.now)
        Next(self.path, clock=lambda: self.now)            # second open: version 2 not re-applied
        self.assertEqual(self.versions(), [1, 2])
        self.assertEqual(n.schema_version(), 2)
        c = sqlite3.connect(self.path)
        try:
            self.assertEqual(c.execute("SELECT file FROM jobs WHERE id=?", (jid,)).fetchone()[0], "stand")
            c.execute("SELECT note FROM jobs")
        finally:
            c.close()

    def test_failed_migration_rolls_back(self):
        jl = self.make()

        class Bad(joblog.JobLog):
            MIGRATIONS = list(joblog.MIGRATIONS) + [(2, "CREATE TABLE half (a INTEGER);\nTHIS IS NOT SQL")]

        bad = Bad(self.path, clock=lambda: self.now)       # never raises into the Hub: runs from memory
        self.assertIn("syntax error", bad.degraded)
        self.assertEqual(bad.recent_jobs(), [])             # still usable (version 1 tables exist in memory)
        self.assertTrue(os.path.exists(self.path))          # a broken migration is not the file's fault: untouched
        self.assertEqual(self.versions(), [1])
        c = sqlite3.connect(self.path)
        try:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
        finally:
            c.close()
        self.assertNotIn("half", names)
        self.assertEqual(jl.schema_version(), 1)

    def test_wal_mode(self):
        self.make()
        c = sqlite3.connect(self.path)
        try:
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            c.close()

    def test_creates_missing_directory(self):
        path = os.path.join(self.tmp, "nested", "dir", "jobs.sqlite")
        joblog.JobLog(path)
        self.assertTrue(os.path.isfile(path))


if __name__ == "__main__":
    unittest.main()
