"""Regression tests for the reliability / data findings of the 2026-10-11 hardening review.

    python3 tests/test_fix_reliability.py

Each test names the review finding it pins (P1-10, P1-11, P1-12, P1-13, and the P2/P3 items in
joblog / costs / notify / events / backup / discovery / detection / the detector adapter).
Loopback only; no real printer, host or service is ever contacted.
"""
import datetime
import http.server
import io
import json
import os
import queue
import shutil
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
for _k in ("no_proxy", "NO_PROXY"):
    os.environ[_k] = "127.0.0.1,localhost"

import backup  # noqa: E402
import costs  # noqa: E402
import detection as det  # noqa: E402
import discovery  # noqa: E402
import events  # noqa: E402
import joblog  # noqa: E402
import notify  # noqa: E402
import settings_store as ss  # noqa: E402
from test_notify import FakeHTTP, Ev  # noqa: E402


def put(path, data):
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)


def slurp(path):
    with open(path, "rb") as f:
        return f.read()


def wait_for(cond, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


# =============================================================================== job log (P1-10)

class CorruptJobDbTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "jobs.sqlite")

    def seeded(self):
        jl = joblog.JobLog(self.path)
        jl._insert(printer="P1S", result="finished", started_at=1.0, ended_at=2.0)
        return jl

    def moved(self):
        return sorted(f for f in os.listdir(self.dir) if ".corrupt-" in f)

    def test_garbage_header_does_not_crash_and_is_set_aside(self):
        self.seeded()
        data = slurp(self.path)
        put(self.path, b"\0" * 100 + data[100:])
        jl = joblog.JobLog(self.path)                      # used to raise sqlite3.DatabaseError at import
        self.assertEqual(jl.recent_jobs(), [])
        self.assertTrue(any(f.startswith("jobs.sqlite.corrupt-") for f in self.moved()), self.moved())
        self.assertFalse(jl.storage_status()["ok"])
        self.assertIsNone(jl.degraded)                     # a new file could be made
        jid = jl._insert(printer="P1S", result="finished", started_at=1.0, ended_at=2.0)   # and it works
        self.assertEqual(jl.job(jid)["printer"], "P1S")

    def test_truncated_file_and_leftover_wal(self):
        self.seeded()
        data = slurp(self.path)
        put(self.path, data[:4096] + b"x" * 17)
        put(self.path + "-wal", b"junk" * 100)
        put(self.path + "-shm", b"junk" * 100)
        jl = joblog.JobLog(self.path)
        self.assertEqual(jl.recent_jobs(), [])
        names = self.moved()
        self.assertEqual(len([f for f in names if not f.endswith(("-wal", "-shm"))]), 1)
        for ext in ("-wal", "-shm"):                        # no junk sidecar is left next to the new database
            if os.path.exists(self.path + ext):
                self.assertFalse(slurp(self.path + ext).startswith(b"junk"))

    def test_not_a_database_text_file(self):
        put(self.path, "this is not sqlite at all\n" * 50)
        jl = joblog.JobLog(self.path)
        self.assertEqual(jl.recent_jobs(), [])
        self.assertEqual(len(self.moved()), 1)

    def test_unusable_location_falls_back_to_memory_and_keeps_working(self):
        blocker = os.path.join(self.dir, "file")
        put(blocker, "x")
        jl = joblog.JobLog(os.path.join(blocker, "jobs.sqlite"))      # parent is a FILE: cannot create it
        self.addCleanup(jl.close)
        self.assertTrue(jl.degraded)
        t0 = 1000.0
        jid = jl.on_event({"kind": "print_started", "printer": "P1S", "ts": t0, "data": {"job": "a.3mf"}})
        jl.on_event({"kind": "print_finished", "printer": "P1S", "ts": t0 + 60, "data": {"job": "a.3mf"}})
        self.assertEqual(jl.job(jid)["result"], "finished")
        self.assertFalse(jl.storage_status()["ok"])

    def test_corruption_found_later_is_recovered_without_raising(self):
        jl = self.seeded()
        data = slurp(self.path)
        put(self.path, b"\0" * 100 + data[100:])
        for ext in ("-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except OSError:
                pass
        self.assertEqual(jl.recent_jobs(), [])             # recovered in place, no exception
        self.assertEqual(len(self.moved()), 1)
        jid = jl.on_event({"kind": "print_started", "printer": "P1S", "ts": 5.0, "data": {"job": "b.3mf"}})
        self.assertIsNotNone(jid)

    def test_repeated_corruption_degrades_instead_of_looping(self):
        jl = self.seeded()
        self.addCleanup(jl.close)
        for _ in range(2):
            put(self.path, b"\0" * 5000)
            for ext in ("-wal", "-shm"):
                try:
                    os.remove(self.path + ext)
                except OSError:
                    pass
            jl.recent_jobs()
        self.assertTrue(jl.degraded)
        self.assertEqual(jl.recent_jobs(), [])

    def test_event_hooks_never_raise_even_when_the_table_is_gone(self):
        jl = self.seeded()
        c = sqlite3.connect(self.path)
        c.execute("DROP TABLE jobs")
        c.commit()
        c.close()
        self.assertIsNone(jl.on_event({"kind": "print_started", "printer": "P", "ts": 1.0, "data": {"job": "x"}}))
        self.assertEqual(jl.observe({"P": {"connected": True, "state": "IDLE"}}), [])

    def test_quarantined_files_are_bounded(self):
        for i in range(8):
            put(self.path, "garbage %d" % i * 100)
            joblog.JobLog(self.path)
            time.sleep(0.01)
            # each run leaves a fresh valid db; corrupt it again next loop
        main = [f for f in self.moved() if not f.endswith(("-wal", "-shm"))]
        self.assertLessEqual(len(main), joblog.JobLog.KEEP_QUARANTINED)

    def test_no_connection_leak_on_a_damaged_file(self):
        put(self.path, "garbage" * 1000)
        jl = joblog.JobLog(self.path)
        for _ in range(200):
            jl.recent_jobs()
        # nothing to assert beyond "did not run out of file handles / raise"
        self.assertIsNone(jl.degraded)


# =============================================================================== job log: P2s

class JobLogTruthTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "jobs.sqlite")

    def test_print_that_finished_while_the_hub_was_down_is_finished_not_unknown(self):
        """review P2 'A print that finishes while the Hub is down is logged as unknown' (rel/r1b)."""
        t = [1000.0]
        jl = joblog.JobLog(self.path, clock=lambda: t[0], orphan_grace_s=120)
        jid = jl.on_event({"kind": "print_started", "printer": "P1S", "ts": t[0], "data": {"job": "vase.gcode.3mf"}})
        jl._update(jid, est_seconds=1800.0, grams_est=20.0, last_seen_at=t[0] + 600)
        t[0] += 3600                                       # hub down for an hour; printer finished meanwhile
        fin = {"connected": True, "state": "FINISH", "job": "vase.gcode.3mf", "percent": 100}
        jl.observe({"P1S": fin}, now=t[0])
        t[0] += 130
        self.assertEqual(jl.observe({"P1S": fin}, now=t[0]), [jid])
        row = jl.job(jid)
        self.assertEqual(row["result"], "finished")
        self.assertEqual(row["percent"], 100.0)
        self.assertEqual(row["grams_used"], 20.0)
        self.assertIsNotNone(row["cost"])                  # priced, unlike 'unknown'

    def test_failed_state_while_away_is_failed_and_other_job_stays_unknown(self):
        t = [1000.0]
        jl = joblog.JobLog(self.path, clock=lambda: t[0], orphan_grace_s=10)
        jid = jl.on_event({"kind": "print_started", "printer": "P1S", "ts": t[0], "data": {"job": "a.3mf"}})
        jl.observe({"P1S": {"connected": True, "state": "FAILED", "job": "a.3mf", "percent": 40}}, now=t[0] + 100)
        jl.observe({"P1S": {"connected": True, "state": "FAILED", "job": "a.3mf", "percent": 40}}, now=t[0] + 200)
        self.assertEqual(jl.job(jid)["result"], "failed")
        jid2 = jl.on_event({"kind": "print_started", "printer": "A1", "ts": t[0], "data": {"job": "b.3mf"}})
        jl.observe({"A1": {"connected": True, "state": "FINISH", "job": "OTHER.3mf", "percent": 100}}, now=t[0] + 100)
        jl.observe({"A1": {"connected": True, "state": "FINISH", "job": "OTHER.3mf", "percent": 100}}, now=t[0] + 200)
        self.assertEqual(jl.job(jid2)["result"], "unknown")

    def test_wall_clock_step_does_not_inflate_duration_or_cost(self):
        """review P2 'Clock jumps corrupt durations and costs' (rel/r5)."""
        wall, mono = [1_700_000_000.0], [50.0]
        jl = joblog.JobLog(self.path, clock=lambda: wall[0], mono=lambda: mono[0])
        jid = jl.on_event({"kind": "print_started", "printer": "P1S", "ts": wall[0], "data": {"job": "box.3mf"}})
        wall[0] += 10
        mono[0] += 10
        jl.observe({"P1S": {"connected": True, "state": "RUNNING", "job": "box.3mf", "percent": 5}})
        wall[0] += 3 * 86400 + 600                          # NTP steps the clock 3 days forward ...
        mono[0] += 600                                      # ... but only 10 real minutes passed
        jl.observe({"P1S": {"connected": True, "state": "RUNNING", "job": "box.3mf", "percent": 50}})
        jl.on_event({"kind": "print_finished", "printer": "P1S", "ts": wall[0], "data": {"job": "box.3mf"}})
        row = jl.job(jid)
        self.assertEqual(row["result"], "finished")
        self.assertAlmostEqual(row["duration_s"], 610, delta=5)

    def test_costs_ignore_an_impossible_duration(self):
        job = {"result": "finished", "percent": 100, "duration_s": 72 * 3600, "est_seconds": 7200,
               "grams_est": 10.0, "material": "PLA", "printer": "P1S", "ended_at": 1}
        self.assertAlmostEqual(costs.machine_hours(job), 2.0)          # estimate, not 72 h
        self.assertFalse(costs.duration_plausible(15 * 86400))
        self.assertTrue(costs.duration_plausible(5 * 3600, 7200))
        job2 = dict(job, est_seconds=None)
        self.assertEqual(costs.machine_hours(dict(job2, duration_s=20 * 86400)), None)
        self.assertAlmostEqual(costs.machine_hours(dict(job2, duration_s=7200)), 2.0)

    def test_unknown_timezone_is_loud_not_silent(self):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            tz = joblog.JobLog._tz("Not/AZone")
        self.assertIs(tz, datetime.timezone.utc)
        self.assertIn("not available", buf.getvalue())


# =============================================================================== notify (P1-11)

class NotifyRetryBase(unittest.TestCase):
    DELAYS = (0.05, 0.05, 0.05)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.srv = FakeHTTP()
        self.addCleanup(self.srv.close)
        self.settings = ss.SettingsStore(self.dir, env={})
        self.settings.update({"ntfy": {"enabled": True, "server_url": self.srv.url, "topic": "hub"},
                              "notifications": {"include_snapshot": False}})
        self.now = [1_800_000_000.0]
        self.n = notify.Notifier(self.settings, clock=lambda: self.now[0], timeout=5)
        self.n.retry_delays = self.DELAYS
        self.addCleanup(self.n.close)
        self.down = [True]
        self.srv.respond = lambda r: (503, b"down") if self.down[0] else (200, b"{}")

    def history_for(self, title):
        return [h for h in self.n.recent(50) if h["title"] == title]


class AlertDeliveryTests(NotifyRetryBase):
    def test_failed_alert_does_not_record_dedupe_so_the_resend_goes_out(self):
        self.n.retry_delays = ()                           # isolate the dedupe rule from retrying
        r1 = self.n.notify(Ev("print_failed", dedupe_key="a.3mf"))
        self.assertFalse(r1["sent"])
        self.assertIn("503", r1["channels"]["ntfy"]["error"])
        self.down[0] = False
        self.now[0] += 30
        r2 = self.n.notify(Ev("print_failed", dedupe_key="a.3mf"))       # used to be skipped as 'duplicate'
        self.assertTrue(r2["sent"])
        self.assertIsNone(r2["skipped"])
        r3 = self.n.notify(Ev("print_failed", dedupe_key="a.3mf"))       # NOW the window applies
        self.assertEqual(r3["skipped"], "duplicate")

    def test_failed_delivery_is_retried_until_it_succeeds_and_each_failure_is_logged(self):
        ev = Ev("print_failed", dedupe_key="a.3mf", title="P1S: print FAILED")
        r = self.n.notify(ev)
        self.assertFalse(r["sent"])
        self.assertEqual(r["attempt"], 1)
        self.assertIn("retry_in_s", r)
        self.assertEqual(self.n.pending_retries(), 1)
        time.sleep(0.12)                                   # let a retry or two fail
        self.down[0] = False
        self.assertTrue(wait_for(lambda: any(h["sent"] for h in self.history_for("P1S: print FAILED"))))
        hist = self.history_for("P1S: print FAILED")
        self.assertGreaterEqual(len([h for h in hist if not h["sent"]]), 2, "one log entry per failed attempt")
        self.assertEqual(sorted({h["attempt"] for h in hist})[0], 1)
        late = [r for r in self.srv.requests if r["method"] == "POST" and b"Sent late" in r["body"]]
        self.assertTrue(late, "the delayed message says it is late")
        # delivered -> the duplicate window now applies
        self.assertEqual(self.n.notify(Ev("print_failed", dedupe_key="a.3mf"))["skipped"], "duplicate")
        self.assertTrue(wait_for(lambda: self.n.pending_retries() == 0))

    def test_retries_are_bounded(self):
        self.n.notify(Ev("print_failed", dedupe_key="x", title="P1S: always down"))
        self.assertTrue(wait_for(lambda: self.n.pending_retries() == 0 and len(
            self.history_for("P1S: always down")) >= 4, timeout=5))
        time.sleep(0.3)
        hist = self.history_for("P1S: always down")
        self.assertEqual(len(hist), 1 + len(self.DELAYS))                 # first try + 3 retries, then it gives up
        self.assertTrue(all(not h["sent"] for h in hist))
        self.assertEqual(self.n.pending_retries(), 0)

    def test_retry_queue_has_a_ceiling(self):
        n = notify.Notifier(self.settings, timeout=5, retry_delays=(30,), max_pending_retries=3)
        self.addCleanup(n.close)
        for i in range(8):
            n.notify(Ev("print_failed", dedupe_key=f"job{i}", title=f"t{i}"))
        self.assertLessEqual(n.pending_retries(), 3)

    def test_a_fresh_identical_alert_supersedes_a_waiting_retry(self):
        n = notify.Notifier(self.settings, timeout=5, retry_delays=(30,))
        self.addCleanup(n.close)
        n.notify(Ev("print_failed", dedupe_key="a"))
        self.assertEqual(n.pending_retries(), 1)
        self.down[0] = False
        self.assertTrue(n.notify(Ev("print_failed", dedupe_key="a"))["sent"])
        self.assertEqual(n.pending_retries(), 0)

    def test_retry_respects_settings_changed_meanwhile(self):
        n = notify.Notifier(self.settings, timeout=5, retry_delays=(0.15,))
        self.addCleanup(n.close)
        n.notify(Ev("print_failed", dedupe_key="a", title="muted-later"))
        self.settings.update({"notifications.print_failed": False})
        self.down[0] = False
        self.assertTrue(wait_for(lambda: n.pending_retries() == 0))
        time.sleep(0.3)
        self.assertFalse([r for r in self.srv.requests if r["method"] == "POST" and 200 == 0])
        self.assertTrue(any(h.get("skipped") == "event_off" for h in n.recent(10)))

    def test_partial_success_retries_only_the_failed_channel(self):
        srv2 = FakeHTTP()
        self.addCleanup(srv2.close)
        self.settings.update({"webhook": {"enabled": True, "url": srv2.url + "/hook"}})
        srv2.respond = lambda r: (500, b"no")
        self.down[0] = False                                # ntfy fine, webhook down
        r = self.n.notify(Ev("print_failed", dedupe_key="p"))
        self.assertTrue(r["sent"])
        self.assertEqual(self.n.notify(Ev("print_failed", dedupe_key="p"))["skipped"], "duplicate")
        n_ntfy = len(self.srv.requests)
        time.sleep(0.3)
        self.assertEqual(len(self.srv.requests), n_ntfy, "the channel that worked is not messaged again")
        self.assertGreaterEqual(len(srv2.requests), 2)

    def test_queue_full_is_logged_and_recorded_not_silent(self):
        self.srv.respond = lambda r: (time.sleep(0.4), (200, b"{}"))[1]
        n = notify.Notifier(self.settings, timeout=5)
        n._q = queue.Queue(maxsize=1)
        self.addCleanup(n.close)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            results = [n.submit(Ev("print_failed", dedupe_key=str(i), title=f"q{i}")) for i in range(6)]
        self.assertIn(False, results)
        self.assertIn("queue full", buf.getvalue())
        self.assertTrue(any(h.get("skipped") == "queue_full" for h in n.recent(20)))
        n.drain(10)

    def test_shutdown_delivers_what_is_queued(self):
        self.down[0] = False
        for i in range(5):
            self.n.submit(Ev("print_failed", dedupe_key=str(i)))
        self.assertTrue(self.n.shutdown(10))
        self.assertEqual(len([r for r in self.srv.requests if r["method"] == "POST"]), 5)

    def test_worker_thread_survives_a_bad_event(self):
        class Bad:
            kind = "print_failed"
            printer = "P"
            title = "x"
            body = "b"
            level = "failure"
            ts = None
            dedupe_key = ""

            def get_image(self):
                raise RuntimeError("camera exploded")

        self.down[0] = False
        self.n.submit(Bad())
        self.n.submit(Ev("print_failed", dedupe_key="after", title="after-bad"))
        self.assertTrue(self.n.drain(5))
        self.assertTrue(self.n._worker.is_alive())
        self.assertTrue(self.history_for("after-bad"))


class RedirectTests(unittest.TestCase):
    """review P1-7 (notify half): a redirect must not carry the bearer to another host."""

    def test_redirect_is_an_error_and_the_target_is_never_contacted(self):
        target = FakeHTTP()
        self.addCleanup(target.close)

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(307)
                self.send_header("Location", target.url + "/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        url = f"http://127.0.0.1:{httpd.server_address[1]}/hook"
        status, _ = notify.http_request("POST", url, {"Authorization": "Bearer SECRET"}, b"{}")
        self.assertEqual(status, 307)
        self.assertEqual(target.requests, [])
        ch = notify.WebhookChannel({"url": url, "bearer": "SECRET"})
        ok, err = ch.send("t", "b")
        self.assertFalse(ok)
        self.assertIn("307", err)
        self.assertNotIn("SECRET", err)


# =============================================================================== events

def esnap(state="IDLE", job="", percent=None, connected=True):
    return {"connected": connected, "state": state, "job": job, "percent": percent, "remaining_min": 10,
            "nozzle_c": 25, "bed_c": 24, "print_error": 0, "error": None}


class EventTruthTests(unittest.TestCase):
    def test_two_prints_of_the_same_file_get_different_dedupe_keys(self):
        """review P2 'The 5-minute dedupe window eats a second finished' (rel/r2 B)."""
        t = [1000.0]
        out = []
        w = events.EventWatcher(out.append, clock=lambda: t[0], confirm_s=0)

        def feed(st, pct=50):
            w.update({"P": esnap(st, "coin.3mf", pct)}, now=t[0])
            t[0] += 60
        for _ in range(2):
            feed("IDLE", 0)
            feed("RUNNING", 5)
            feed("FINISH", 100)
        fin = [e for e in out if e.kind == "print_finished"]
        self.assertEqual(len(fin), 2)
        self.assertNotEqual(fin[0].dedupe_key, fin[1].dedupe_key)

    def test_second_batch_is_delivered_through_the_notifier(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        srv = FakeHTTP()
        self.addCleanup(srv.close)
        st = ss.SettingsStore(d, env={})
        st.update({"ntfy": {"enabled": True, "server_url": srv.url, "topic": "hub"}})
        t = [1000.0]
        n = notify.Notifier(st, clock=lambda: t[0], timeout=5)
        self.addCleanup(n.close)
        w = events.EventWatcher(lambda e: n.notify(e), clock=lambda: t[0], confirm_s=0)
        for _ in range(2):
            for state, pct in (("IDLE", 0), ("RUNNING", 5), ("FINISH", 100)):
                w.update({"P": esnap(state, "coin.3mf", pct)}, now=t[0])
                t[0] += 60
        finished = [r for r in srv.requests if b"finished" in r["body"].lower() and r["method"] == "POST"]
        self.assertEqual(len(finished), 2)

    def test_wall_clock_step_does_not_change_print_duration_or_offline_timing(self):
        wall, mono = [1_700_000_000.0], [100.0]
        out = []
        w = events.EventWatcher(out.append, clock=lambda: wall[0], mono=lambda: mono[0], confirm_s=0)

        def step(snap, dt=5):
            w.update({"P": snap})
            wall[0] += dt
            mono[0] += dt
        step(esnap("IDLE"))
        step(esnap("RUNNING", "a.3mf", 1))
        step(esnap("RUNNING", "a.3mf", 40), dt=600)
        wall[0] += 3 * 86400                                # NTP step, no real time passes
        step(esnap("FINISH", "a.3mf", 100))
        fin = [e for e in out if e.kind == "print_finished"][0]
        self.assertAlmostEqual(fin.data["seconds"], 610, delta=15)
        self.assertNotIn("72", fin.body)
        # and a clock step is not an outage
        out.clear()
        step(esnap("FINISH", "a.3mf", 100))
        wall[0] += 5 * 3600
        step(esnap("UNKNOWN", connected=False))
        self.assertNotIn("printer_offline", [e.kind for e in out])

    def test_printer_already_off_when_the_hub_started_is_eventually_announced_once(self):
        """review P2 'A printer that is offline at boot never gets an OFFLINE alert' (rel/r9 A)."""
        out = []
        w = events.EventWatcher(out.append, offline_grace_s=90, baseline_offline_grace_s=600, confirm_s=0)
        t = 0.0
        down = esnap("UNKNOWN", connected=False)
        for _ in range(100):                                # 500 s: still within the longer boot grace
            w.update({"P": down}, now=t)
            t += 5
        self.assertEqual(out, [])
        for _ in range(40):                                 # past 600 s
            w.update({"P": down}, now=t)
            t += 5
        self.assertEqual([e.kind for e in out], ["printer_offline"])
        self.assertIn("since the Hub started", out[0].body)
        w.update({"P": esnap("IDLE")}, now=t)
        w.update({"P": esnap("IDLE")}, now=t + 5)
        self.assertEqual([e.kind for e in out], ["printer_offline", "printer_online"])

    def test_flapping_link_does_not_alert_every_cycle(self):
        """review P3 'Flapping has no hysteresis' (rel/r9 B)."""
        out = []
        w = events.EventWatcher(out.append, offline_grace_s=90, confirm_s=5)
        t = 0.0

        def run(snap, seconds):
            nonlocal t
            for _ in range(int(seconds / 5)):
                w.update({"P": snap}, now=t)
                t += 5
        run(esnap("IDLE"), 60)
        run(esnap("UNKNOWN", connected=False), 120)         # first outage: announced
        run(esnap("IDLE"), 60)
        for _ in range(4):                                  # then 100 s down / 60 s up, repeatedly
            run(esnap("UNKNOWN", connected=False), 100)
            run(esnap("IDLE"), 60)
        kinds = [e.kind for e in out]
        self.assertEqual(kinds.count("printer_offline"), 1, kinds)
        # a real, long outage after the flapping is still announced
        run(esnap("UNKNOWN", connected=False), 400)
        self.assertEqual([e.kind for e in out].count("printer_offline"), 2)

    def test_detector_events_have_a_hub_level_shape(self):
        out = []
        w = events.EventWatcher(out.append)
        w.detector_down("connection refused", failures=3)
        w.detector_up()
        self.assertEqual([e.kind for e in out], ["detector_down", "detector_up"])
        self.assertEqual(out[0].printer, "")
        self.assertIn("NOT being watched", out[0].body)


# =============================================================================== backup (P1-12)

class Resp(io.BytesIO):
    pass


class FakeDrv:
    kind = "moonraker"
    backup_mid_print_ok = True
    model = "x"

    def __init__(self, name="Shop", n=10):
        self.name = name
        self.n = n
        self.bad = set()

    def files(self, with_meta=False):
        return [{"name": f"part{i}.gcode", "size": 10, "modified": 1.0} for i in range(self.n)]

    def open_file(self, name, timeout=30):
        if name in self.bad:
            raise backup.PrinterError("HTTP 404")
        return Resp(b"x" * 10)


class BackupRetentionTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.d = FakeDrv()
        self.m = backup.BackupManager({"shop": self.d}, self.root, keep=lambda: 2, pause_between_files=0)
        self.counter = [0]
        real = time.strftime

        def fake(fmt, *a):
            if fmt == "%Y%m%d-%H%M%S" and not a:
                self.counter[0] += 1
                return f"20260101-{self.counter[0]:06d}"
            return real(fmt, *a)
        p = mock.patch.object(backup.time, "strftime", fake)
        p.start()
        self.addCleanup(p.stop)

    def run_one(self):
        j = self.m._reserve(self.d)
        self.m._run(j, self.d)
        del self.m._active[self.d.name]
        return j

    def on_disk(self):
        out = {}
        pdir = os.path.join(self.root, "Shop")
        for fn in sorted(os.listdir(pdir)):
            z = zipfile.ZipFile(os.path.join(pdir, fn))
            out[fn] = json.loads(z.read("manifest.json"))["complete"]
        return out

    def test_partial_backups_never_push_complete_ones_out(self):
        for _ in range(2):
            self.assertEqual(self.run_one().status, "done")
        self.d.bad = {f"part{i}.gcode" for i in range(1, 10)}
        for _ in range(2):
            self.assertEqual(self.run_one().status, "partial")
        disk = self.on_disk()
        self.assertEqual(sorted(disk.values()), [False, False, True, True], disk)   # used to leave 2 partial only

    def test_partial_pool_is_capped_at_two_and_keeps_the_newest(self):
        self.run_one()
        self.d.bad = {f"part{i}.gcode" for i in range(1, 10)}
        for _ in range(5):
            last = self.run_one()
        disk = self.on_disk()
        self.assertEqual(list(disk.values()).count(True), 1)
        self.assertEqual(list(disk.values()).count(False), backup.KEEP_PARTIAL)
        partial_names = sorted(f for f, c in disk.items() if not c)
        self.assertEqual(partial_names[-1], last.file)

    def test_complete_pool_still_honours_keep(self):
        for _ in range(4):
            last = self.run_one()
        self.assertEqual(len(self.on_disk()), 2)
        self.assertIn(last.file, self.on_disk())

    def test_newest_complete_survives_even_when_older_than_all_partials(self):
        self.run_one()
        self.d.bad = {f"part{i}.gcode" for i in range(1, 10)}
        for _ in range(3):
            self.run_one()
        self.assertTrue(any(self.on_disk().values()))

    def _fake_zip(self, name, created_iso, complete):
        pdir = os.path.join(self.root, "Shop")
        os.makedirs(pdir, exist_ok=True)
        with zipfile.ZipFile(os.path.join(pdir, name), "w") as z:
            z.writestr("manifest.json", json.dumps({"created": created_iso, "complete": complete,
                                                    "files": [{"name": "a"}], "failed": [] if complete else [{}]}))

    def test_order_follows_created_time_not_the_file_name_across_dst(self):
        """review P3 'Backup names use local time, which breaks sort order across DST'."""
        # fall-back night: the NEWER backup carries the SMALLER local stamp
        self._fake_zip("Shop-20261101-013000.zip", "2026-11-01T01:30:00-05:00", True)    # 06:30 UTC (newest)
        self._fake_zip("Shop-20261101-014500.zip", "2026-11-01T01:45:00-04:00", True)    # 05:45 UTC
        self._fake_zip("Shop-20261101-010000.zip", "2026-11-01T01:00:00-04:00", True)    # 05:00 UTC (oldest)
        self.m._prune(os.path.join(self.root, "Shop"), "Shop")                          # keep = 2
        left = sorted(os.listdir(os.path.join(self.root, "Shop")))
        self.assertEqual(left, ["Shop-20261101-013000.zip", "Shop-20261101-014500.zip"])

    def test_unreadable_zip_counts_as_partial_not_complete(self):
        self._fake_zip("Shop-20260101-000001.zip", "2026-01-01T00:00:01+00:00", True)
        self._fake_zip("Shop-20260101-000002.zip", "2026-01-01T00:00:02+00:00", True)
        pdir = os.path.join(self.root, "Shop")
        put(os.path.join(pdir, "Shop-20260101-000009.zip"), b"not a zip")
        self.m._prune(pdir, "Shop")
        self.assertEqual(len(os.listdir(pdir)), 3)          # both good ones kept; junk is in the partial pool

    def test_new_archive_is_verified_before_it_replaces_anything(self):
        self.run_one()
        with mock.patch.object(backup, "_verify_zip", side_effect=ValueError("bad archive")):
            j = self.run_one()
        self.assertEqual(j.status, "failed")
        self.assertIn("bad archive", j.error)
        self.assertEqual(len(self.on_disk()), 1, "the earlier backup is untouched and no .part is left")
        self.assertFalse([f for f in os.listdir(os.path.join(self.root, "Shop")) if f.endswith(".part")])

    def test_verify_zip_rejects_a_missing_manifest(self):
        p = os.path.join(self.root, "v.zip")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("a", "x")
        with self.assertRaises(ValueError):
            backup._verify_zip(p, 1)

    def test_listing_exposes_complete_flag(self):
        self.run_one()
        rows = self.m.list_backups()["Shop"]
        self.assertTrue(rows[0]["complete"])

    def test_reserve_does_not_hold_the_lock_while_asking_the_printer(self):
        gate = threading.Event()

        class Slow(FakeDrv):
            backup_mid_print_ok = False
            kind = "other"

            def snapshot(self):
                gate.wait(5)
                return {"state": "IDLE", "connected": True}

        slow = Slow("Slow")
        m = backup.BackupManager({"slow": slow}, self.root, pause_between_files=0)
        t = threading.Thread(target=lambda: m._reserve(slow), daemon=True)
        t.start()
        time.sleep(0.2)
        t0 = time.time()
        m.status()                                          # would block for the whole snapshot() before the fix
        self.assertLess(time.time() - t0, 1.0)
        gate.set()
        t.join(5)


# =============================================================================== discovery (P1-13)

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Tarpit:
    """Accepts connections and answers with a slow drip that never ends. mode: 'http' | 'tls'."""

    def __init__(self, mode="http"):
        self.mode = mode
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(50)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        self.accepted = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._drip, args=(c,), daemon=True).start()

    def _drip(self, c):
        try:
            c.settimeout(2)
            try:
                c.recv(4096)
            except OSError:
                pass
            if self.mode == "http":
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000000\r\n\r\n")
            else:
                c.sendall(b"\x16\x03\x03\x40\x00")          # a TLS record header promising 16 KB ...
            while not self.stop:
                c.sendall(b" ")                             # ... delivered one byte at a time
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.stop = True
        self.sock.close()


class DiscoveryDeadlineTests(unittest.TestCase):
    def mgr(self, tar, **kw):
        base = dict(ports={"bambu_mqtt": free_port(), "moonraker": tar.port, "http": (free_port(),),
                           "bambu_ftps": free_port(), "bambu_camera": free_port()},
                    ssdp=False, zeroconf=False, cooldown=0, connect_timeout=0.4, http_timeout=0.8,
                    host_timeout=3.0, scan_timeout=20.0, cancel_grace=0.5)
        base.update(kw)
        return discovery.DiscoveryManager(**base)

    def test_slow_drip_http_response_is_cut_off_at_the_wall_clock_limit(self):
        tar = Tarpit("http")
        self.addCleanup(tar.close)
        m = self.mgr(tar)
        t0 = time.time()
        self.assertIsNone(m._http_get("127.0.0.1", tar.port, "/server/info"))
        self.assertLess(time.time() - t0, 2.5, "per-recv timeouts alone would never end this")

    def test_slow_drip_tls_handshake_is_cut_off(self):
        tar = Tarpit("tls")
        self.addCleanup(tar.close)
        m = self.mgr(tar)
        t0 = time.time()
        self.assertIsNone(m._tls_cert("127.0.0.1", tar.port))
        self.assertLess(time.time() - t0, 3.0)

    def test_one_tarpit_host_ends_in_bounded_time_and_the_next_scan_starts(self):
        tar = Tarpit("http")
        self.addCleanup(tar.close)
        m = self.mgr(tar, host_timeout=2.0)
        sid = m.start(["127.0.0.1/32"])["scan_id"]
        t0 = time.time()
        while not m.status(sid)["finished"] and time.time() - t0 < 15:
            time.sleep(0.1)
        self.assertTrue(m.status(sid)["finished"])
        self.assertLess(time.time() - t0, 9.0)
        m.start(["127.0.0.1/32"])                           # NOT 'Busy'

    def test_cancel_releases_the_scanner_even_while_a_host_is_stuck(self):
        tar = Tarpit("http")
        self.addCleanup(tar.close)
        m = self.mgr(tar, host_timeout=30.0, http_timeout=30.0)      # per-host limit deliberately huge
        sid = m.start(["127.0.0.1/32"])["scan_id"]
        time.sleep(0.6)
        m.cancel(sid)
        t0 = time.time()
        while not m.status(sid)["finished"] and time.time() - t0 < 8:
            time.sleep(0.1)
        st = m.status(sid)
        self.assertTrue(st["finished"])
        self.assertEqual(st["state"], "cancelled")
        self.assertLess(time.time() - t0, 4.0)
        m.start(["127.0.0.1/32"])                           # accepted: no 'Busy' lock-out until restart

    def test_scan_time_limit_stops_a_scan_that_does_not_finish(self):
        tar = Tarpit("http")
        self.addCleanup(tar.close)
        block = threading.Event()
        self.addCleanup(block.set)
        m = self.mgr(tar, scan_timeout=0.6, cancel_grace=0.4)
        m._host_task = lambda scan, ip: block.wait(30)      # a worker that never comes back
        t0 = time.time()
        sid = m.start(["127.0.0.1/32"])["scan_id"]
        while not m.status(sid)["finished"] and time.time() - t0 < 10:
            time.sleep(0.1)
        st = m.status(sid)
        self.assertTrue(st["finished"], st)
        self.assertEqual(st["state"], "done")
        self.assertIn("time limit", st["note"])
        self.assertLess(time.time() - t0, 5.0)
        m.start(["127.0.0.1/32"])

    def test_watchdog_releases_a_wedged_scan_thread(self):
        tar = Tarpit("http")
        self.addCleanup(tar.close)
        m = self.mgr(tar)
        gate = threading.Event()
        self.addCleanup(gate.set)
        m._run = lambda scan, nets: gate.wait(60)           # the scan thread itself is stuck
        sid = m.start(["127.0.0.1/32"])["scan_id"]
        with self.assertRaises(discovery.Busy):
            m.start(["127.0.0.1/32"])
        m._scans[sid].hard_deadline = time.monotonic() - 1  # past its hard limit
        m.start(["127.0.0.1/32"])                           # the watchdog frees the slot
        self.assertEqual(m.status(sid)["state"], "error")

    def test_host_deadline_stops_further_probes(self):
        tar = Tarpit("http")
        self.addCleanup(tar.close)
        m = self.mgr(tar, host_timeout=0.3)
        calls = []
        m._tcp_probe = lambda ip, port: (time.sleep(0.2), calls.append(port), "open")[2]
        m._fingerprint_http = lambda ip, port: None
        m._fingerprint_bambu = lambda *a: None
        m._scan_host("127.0.0.1")
        self.assertLessEqual(len(calls), 3, calls)          # stops once the host's budget is gone

    def test_watchdog_class_wakes_a_blocked_recv(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        a = socket.create_connection(("127.0.0.1", srv.getsockname()[1]))
        b, _ = srv.accept()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.settimeout(30)
        t0 = time.time()
        with discovery._Watchdog(a, 0.3) as wd:
            try:
                a.recv(10)
            except OSError:
                pass
        self.assertTrue(wd.fired)
        self.assertLess(time.time() - t0, 2.0)


# =============================================================================== detection

class DetectionTruthTests(unittest.TestCase):
    P = dict(ewm_span=12, short_window=60, long_window=800, warmup_frames=5)

    def params(self, **kw):
        return det.Params(**{**self.P, **kw})

    def test_a_failed_print_does_not_poison_the_baseline_for_the_next_one(self):
        """review P2 'Failing frames poison the detection baseline' (rel/r8)."""
        t = det.FailureTracker("P", self.params())
        for _ in range(600):
            t.observe(0.05)                                 # healthy history
        clean_mean = sum(t._long) / len(t._long)
        for _ in range(500):
            t.observe(0.95)                                 # a print that failed and was left running
        poisoned = sum(t._long) / len(t._long)
        self.assertLess(poisoned, clean_mean + 0.12, "failing pictures must not teach the baseline")
        t.start_print()                                     # next print is also bad from picture one
        levels = [t.observe(0.95).level for _ in range(60)]
        self.assertNotIn(det.LEVEL_OK, levels[5:], "the next bad print is flagged right after its warm-up")

    def test_a_camera_that_always_looks_bad_is_still_learned_eventually(self):
        t = det.FailureTracker("P", self.params(long_window=400))
        for _ in range(100):
            t.observe(0.05)
        for _ in range(4000):
            t.observe(0.9)
        self.assertGreater(sum(t._long) / len(t._long), 0.5)

    def test_baseline_file_is_small_and_written_rarely(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        now = [0.0]
        t = det.FailureTracker("P", det.Params(), store_dir=d, clock=lambda: now[0])
        writes = []
        real = os.replace
        with mock.patch("os.replace", side_effect=lambda a, b: (writes.append(b), real(a, b))[1]):
            for i in range(7200):
                t.observe(0.1234567 + (i % 7) * 0.01)
                now[0] += 10                                # one picture / 10 s = 20 h
        self.assertLessEqual(len(writes), 7200 * 10 // 300 + 2)        # one write per 5 min (was one per minute)
        self.assertLess(os.path.getsize(t.path), 48_000)               # was ~57 KB per write

    def _loop(self, **kw):
        class Client:
            up = False

            def score(s, jpeg, printer=""):
                return det.ScoreResult(True, score=0.05) if s.up else det.ScoreResult(False, code="unreachable", error="down")

            def health(s, timeout_s=5):
                return {"ok": s.up, "status": "ok" if s.up else "unreachable", "info": {}, "error": "down"}

        cl = Client()
        t = [10000.0]
        fleet = {"P1S": {"state": "RUNNING", "connected": True, "job": "x", "percent": 5}}
        calls = []
        loop = det.DetectionLoop(lambda: fleet, lambda n: (b"\xff\xd8" + b"x" * 5000, t[0]),
                                 lambda *a: None, client=cl, clock=lambda: t[0], interval_s=10,
                                 on_detector=lambda state, info: calls.append((state, info)), **kw)
        return loop, cl, t, calls

    def test_detector_outage_is_reported_once_and_recovery_once(self):
        """review P2 'A detector outage raises no event' (rel/r7)."""
        loop, cl, t, calls = self._loop()
        for _ in range(900):
            loop.tick(now=t[0])
            t[0] += 2
        self.assertEqual([c[0] for c in calls], ["down"], calls)
        self.assertGreaterEqual(calls[0][1]["failures"], 3)
        cl.up = True
        for _ in range(600):
            loop.tick(now=t[0])
            t[0] += 2
        self.assertEqual([c[0] for c in calls], ["down", "up"], calls)

    def test_one_blip_is_not_an_outage(self):
        loop, cl, t, calls = self._loop()
        loop.tick(now=t[0])                                 # one failure ...
        cl.up = True
        t[0] += 60
        for _ in range(30):
            loop.tick(now=t[0])
            t[0] += 2
        self.assertEqual(calls, [])

    def test_a_broken_hook_does_not_stop_scoring(self):
        loop, cl, t, calls = self._loop()
        loop._on_detector = mock.Mock(side_effect=RuntimeError("boom"))
        for _ in range(900):
            loop.tick(now=t[0])
            t[0] += 2
        self.assertTrue(loop._on_detector.called)


# =============================================================================== detector adapter

class AdapterHardeningTests(unittest.TestCase):
    def setUp(self):
        import test_adapter as ta
        self.ta = ta
        self.ml = ta.FakeMlApi()
        self.addCleanup(self.ml.stop)

    def test_slow_drip_headers_are_cut_off_by_the_wall_clock_deadline(self):
        ad = self.ta.make_adapter(self.ml)
        self.addCleanup(ad.stop)
        with mock.patch.object(self.ta.adapter_app, "REQUEST_READ_DEADLINE", 1.0):
            s = socket.create_connection(("127.0.0.1", ad.port), timeout=10)
            self.addCleanup(s.close)
            t0 = time.time()
            closed = False
            try:
                s.sendall(b"POST /v1/score HTTP/1.1\r\nHost: x\r\n")
                while time.time() - t0 < 6:
                    s.sendall(b"X-Pad: a\r\n")              # keeps every recv() alive
                    time.sleep(0.3)
            except OSError:
                closed = True
            self.assertTrue(closed, "the server must hang up on a client that never finishes its headers")
            self.assertLess(time.time() - t0, 4.0)

    def test_slow_drip_body_is_cut_off(self):
        ad = self.ta.make_adapter(self.ml)
        self.addCleanup(ad.stop)
        with mock.patch.object(self.ta.adapter_app, "REQUEST_READ_DEADLINE", 1.0):
            s = socket.create_connection(("127.0.0.1", ad.port), timeout=10)
            self.addCleanup(s.close)
            s.sendall(("POST /v1/score HTTP/1.1\r\nHost: x\r\nContent-Length: 100000\r\n"
                       f"Authorization: Bearer {self.ta.TOKEN}\r\nContent-Type: image/jpeg\r\n\r\n").encode())
            t0 = time.time()
            closed = False
            try:
                s.sendall(b"\xff\xd8")
                while time.time() - t0 < 6:
                    s.sendall(b"a")
                    time.sleep(0.3)
            except OSError:
                closed = True
            self.assertTrue(closed)
            self.assertLess(time.time() - t0, 4.0)

    def test_connection_count_is_capped(self):
        with mock.patch.object(self.ta.adapter_app, "MAX_CONNECTIONS", 3):
            ad = self.ta.make_adapter(self.ml)
        self.addCleanup(ad.stop)
        idle = [socket.create_connection(("127.0.0.1", ad.port), timeout=5) for _ in range(3)]
        for c in idle:
            self.addCleanup(c.close)
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", ad.port), timeout=5)
        self.addCleanup(extra.close)
        extra.settimeout(3)
        try:
            data = extra.recv(10)
        except OSError:
            data = b""
        self.assertEqual(data, b"", "the 4th connection is refused (closed), not queued")
        for c in idle:
            c.close()
        time.sleep(0.3)
        st, body, _ = self.ta.post(ad)                       # slots come back
        self.assertEqual(st, 200)

    def test_normal_scoring_still_works(self):
        ad = self.ta.make_adapter(self.ml)
        self.addCleanup(ad.stop)
        st, body, _ = self.ta.post(ad)
        self.assertEqual(st, 200)
        self.assertGreater(body["score"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
