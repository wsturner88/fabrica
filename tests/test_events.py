"""Tests for app/events.py - the EventWatcher transition matrix, debounce, baseline and hooks,
plus one end-to-end run (watcher -> Notifier -> a fake ntfy server on 127.0.0.1).

    python3 tests/test_events.py

Time is a fake clock; nothing sleeps and nothing leaves the machine.
"""
import email.header
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import events  # noqa: E402
import notify  # noqa: E402
import settings_store as ss  # noqa: E402
from test_notify import FakeHTTP, JPEG  # noqa: E402


def snap(state="IDLE", job="", percent=None, connected=True, **kw):
    s = {"printer": "P1S", "connected": connected, "state": state, "job": job, "percent": percent,
         "remaining_min": None, "layer": "", "nozzle_c": 25, "nozzle_target_c": 0, "bed_c": 24,
         "bed_target_c": 0, "print_error": 0, "kind": "bambu", "error": None}
    s.update(kw)
    return s


def offline(error=None):
    return snap("UNKNOWN", connected=False, error=error)


class Watch(unittest.TestCase):
    CONFIRM = 0

    def setUp(self):
        self.t = 1000.0
        self.out = []
        self.fetched = []
        self.w = events.EventWatcher(self.out.append, snapshot_fetcher=lambda n: self.fetched.append(n) or JPEG,
                                     clock=lambda: self.t, offline_grace_s=90, confirm_s=self.CONFIRM)

    def feed(self, s, dt=5, name="P1S", **kw):
        """Advance the clock by dt and feed one snapshot for one printer. Returns new events."""
        self.t += dt
        n = len(self.out)
        self.w.update({name: s} if not isinstance(s, dict) or "state" in s else s, **kw)
        return self.out[n:]

    def kinds(self):
        return [e.kind for e in self.out]


class BaselineTests(Watch):
    def test_first_snapshot_is_baseline_whatever_it_shows(self):
        for st in ("RUNNING", "PAUSE", "FINISH", "FAILED", "PREPARE", "IDLE"):
            self.w = events.EventWatcher(self.out.append, clock=lambda: self.t, confirm_s=0)
            self.assertEqual(self.feed(snap(st, "x.3mf", 50)), [])
            self.assertEqual(self.feed(snap(st, "x.3mf", 51)), [])
        self.assertEqual(self.out, [])

    def test_restart_mid_print_then_finish_is_reported(self):
        self.feed(snap("RUNNING", "benchy", 40))
        self.assertEqual(self.kinds(), [])
        ev = self.feed(snap("FINISH", "benchy", 100))
        self.assertEqual([e.kind for e in ev], ["print_finished"])
        self.assertIn("benchy", ev[0].title)

    def test_restart_with_finished_job_does_not_spam(self):
        self.feed(snap("FINISH", "benchy", 100))
        self.feed(snap("FINISH", "benchy", 100))
        self.feed(snap("IDLE"))
        self.assertEqual(self.out, [])

    def test_restart_with_printer_off_then_it_appears_is_silent(self):
        self.feed(offline())
        self.feed(offline(), dt=300)                     # offline far beyond the grace period: still no notice
        self.feed(snap("IDLE"))
        self.feed(snap("IDLE"), dt=30)
        self.assertEqual(self.out, [])
        # ...but once it was up and drops, it is reported
        self.feed(offline(), dt=10)
        self.feed(offline(), dt=100)
        self.assertEqual(self.kinds(), ["printer_offline"])

    def test_restart_with_printer_off_then_running_does_not_announce_start(self):
        self.feed(offline())
        self.feed(snap("RUNNING", "benchy", 30))
        self.feed(snap("RUNNING", "benchy", 31))
        self.assertEqual(self.out, [])

    def test_printers_are_independent_and_new_printer_baselines(self):
        self.feed({"A": snap("RUNNING", "a", 10), "B": snap("IDLE")})
        self.feed({"A": snap("RUNNING", "a", 20), "B": snap("IDLE")})
        self.assertEqual(self.out, [])
        self.feed({"A": snap("FINISH", "a", 100), "B": snap("RUNNING", "b", 1), "C": snap("RUNNING", "c", 5)})
        self.assertEqual(sorted((e.printer, e.kind) for e in self.out),
                         [("A", "print_finished"), ("B", "print_started")])     # C only baselined

    def test_removed_and_readded_printer_baselines_again(self):
        self.feed({"A": snap("RUNNING", "a", 10)})
        self.feed({})
        self.feed({"A": snap("RUNNING", "a", 12)})
        self.assertEqual(self.out, [])


class TransitionTests(Watch):
    def test_started_from_idle_via_prepare_exactly_once(self):
        self.feed(snap("IDLE"))
        self.feed(snap("PREPARE", "benchy", 0))
        self.assertEqual(self.kinds(), [])
        ev = self.feed(snap("RUNNING", "benchy", 1, remaining_min=75, nozzle_target_c=215, bed_target_c=60))
        self.assertEqual([e.kind for e in ev], ["print_started"])
        self.assertIn("benchy", ev[0].body)
        self.assertIn("1 h 15 min", ev[0].body)
        self.assertIn("215", ev[0].body)
        self.feed(snap("RUNNING", "benchy", 5))
        self.assertEqual(self.kinds(), ["print_started"])

    def test_started_from_finish_and_failed(self):
        for prev in ("FINISH", "FAILED", "IDLE"):
            self.out.clear()
            self.w = events.EventWatcher(self.out.append, clock=lambda: self.t, confirm_s=0)
            self.feed(snap(prev, "old", 100))
            self.feed(snap("RUNNING", "new", 1))
            self.assertEqual(self.kinds(), ["print_started"], prev)

    def test_resume_after_pause_is_not_a_new_start(self):
        self.feed(snap("IDLE"))
        self.feed(snap("RUNNING", "j", 1))
        self.feed(snap("PAUSE", "j", 30))
        self.feed(snap("PREPARE", "j", 30))            # re-heating on resume
        self.feed(snap("RUNNING", "j", 30))
        self.assertEqual(self.kinds(), ["print_started", "print_paused"])

    def test_finished_with_duration_temperature_and_lazy_image(self):
        self.feed(snap("IDLE"))
        self.feed(snap("RUNNING", "benchy", 1))
        ev = self.feed(snap("FINISH", "benchy", 100, bed_c=62), dt=2 * 3600 + 14 * 60)
        self.assertEqual([e.kind for e in ev], ["print_finished"])
        e = ev[0]
        self.assertIn("took 2h 14m", e.body)
        self.assertIn("62", e.body)
        self.assertIn("cool", e.body)
        self.assertEqual(self.fetched, [])               # nothing fetched until the Notifier asks
        self.assertEqual(e.get_image(), JPEG)
        self.assertEqual(self.fetched, ["P1S"])
        e.get_image()
        self.assertEqual(self.fetched, ["P1S"])          # once
        self.assertEqual(e.title, "P1S finished \u00b7 benchy")
        self.assertEqual(e.level, "info")

    def test_failed_with_error_text(self):
        self.feed(snap("RUNNING", "benchy", 40))
        ev = self.feed(snap("FAILED", "benchy", 41, error="Nozzle clogged", print_error=1))
        self.assertEqual([e.kind for e in ev], ["print_failed"])
        self.assertIn("FAILED", ev[0].title)
        self.assertIn("Nozzle clogged", ev[0].body)
        self.assertIn("41%", ev[0].body)
        self.assertEqual(ev[0].level, "failure")

    def test_failed_without_error_reads_as_cancelled(self):
        self.feed(snap("RUNNING", "benchy", 40))
        ev = self.feed(snap("FAILED", "benchy", 40))
        self.assertEqual(ev[0].kind, "print_failed")
        self.assertIn("CANCELLED", ev[0].title)
        self.assertIn("cancelled", ev[0].body)
        self.assertTrue(ev[0].data["cancelled"])

    def test_bambu_error_code_formatted(self):
        self.feed(snap("RUNNING", "benchy", 40))
        ev = self.feed(snap("PAUSE", "benchy", 40, print_error=0x07008011))
        self.assertIn("0x07008011", ev[0].body)

    def test_idle_after_active_job_short_of_done_is_cancelled(self):
        self.feed(snap("RUNNING", "benchy", 50))
        ev = self.feed(snap("IDLE"))
        self.assertEqual([e.kind for e in ev], ["print_failed"])
        self.assertIn("50%", ev[0].body)

    def test_idle_after_job_at_100_percent_is_finished(self):
        self.feed(snap("RUNNING", "benchy", 99))
        ev = self.feed(snap("IDLE"))
        self.assertEqual([e.kind for e in ev], ["print_finished"])

    def test_stale_failed_or_finish_after_idle_ignored(self):
        self.feed(snap("IDLE"))
        self.feed(snap("FAILED", "old", 10))
        self.feed(snap("FINISH", "old", 100))
        self.feed(snap("IDLE"))
        self.assertEqual(self.out, [])

    def test_no_double_report_finish_then_idle_then_failed(self):
        self.feed(snap("RUNNING", "j", 50))
        self.feed(snap("FINISH", "j", 100))
        self.feed(snap("IDLE"))
        self.feed(snap("FAILED", "j", 100))
        self.assertEqual(self.kinds(), ["print_finished"])

    def test_paused_includes_error_text_and_temps(self):
        self.feed(snap("RUNNING", "benchy", 30))
        ev = self.feed(snap("PAUSE", "benchy", 31, error="Filament runout", nozzle_c=215, bed_c=60, remaining_min=45))
        self.assertEqual([e.kind for e in ev], ["print_paused"])
        e = ev[0]
        self.assertEqual(e.level, "warning")
        self.assertIn("PAUSED", e.title)
        self.assertIn("Filament runout", e.body)
        self.assertIn("31%", e.body)
        self.assertIn("215", e.body)
        self.assertIn("45 min", e.body)

    def test_paused_without_error_says_so(self):
        self.feed(snap("RUNNING", "benchy", 30))
        ev = self.feed(snap("PAUSE", "benchy", 31))
        self.assertIn("No error reported", ev[0].body)

    def test_pause_state_spelled_paused_is_normalised(self):
        self.feed(snap("RUNNING", "j", 30))
        self.assertEqual(self.kinds(), [])
        self.feed(snap("PAUSED", "j", 31))
        self.assertEqual(self.kinds(), ["print_paused"])

    def test_pause_from_idle_ignored(self):
        self.feed(snap("IDLE"))
        self.feed(snap("PAUSE", "j", 10))
        self.assertEqual(self.out, [])

    def test_moonraker_style_error_failure(self):
        self.feed(snap("RUNNING", "part.gcode", 70, kind="moonraker"))
        ev = self.feed(snap("FAILED", "part.gcode", 70, kind="moonraker", print_error=1,
                            error="Heater extruder not heating at expected rate"))
        self.assertIn("Heater extruder", ev[0].body)

    def test_temperature_unit_follows_settings(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        st = ss.SettingsStore(d, env={})
        st.update({"general.temp_unit": "F"})
        w = events.EventWatcher(self.out.append, settings=st, clock=lambda: self.t, confirm_s=0)
        w.update({"P1S": snap("RUNNING", "j", 30)})
        self.t += 5
        w.update({"P1S": snap("PAUSE", "j", 31, nozzle_c=200, bed_c=60)})
        self.assertIn("392 °F", self.out[0].body)
        self.assertIn("140 °F", self.out[0].body)
        self.assertNotIn("°C", self.out[0].body)

    def test_messages_use_words_not_only_symbols(self):
        self.feed(snap("RUNNING", "j", 30))
        self.feed(snap("PAUSE", "j", 31))
        self.feed(snap("RUNNING", "j", 32))
        self.feed(snap("FAILED", "j", 33))
        for e in self.out:
            self.assertTrue(e.title.isascii(), e.title)             # no emoji-only meaning
            self.assertRegex(e.title, r"[A-Z]{5,}")             # PAUSED / FAILED / CANCELLED in capitals


class DebounceTests(Watch):
    CONFIRM = 5

    def test_state_change_needs_to_hold_for_confirm_seconds(self):
        self.feed(snap("IDLE"))
        self.assertEqual(self.feed(snap("RUNNING", "j", 1), dt=1), [])          # first sighting: pending
        self.assertEqual(self.feed(snap("RUNNING", "j", 1), dt=2), [])          # 2 s: still pending
        ev = self.feed(snap("RUNNING", "j", 2), dt=3)                           # 5 s: confirmed
        self.assertEqual([e.kind for e in ev], ["print_started"])

    def test_flapping_state_never_reports(self):
        self.feed(snap("RUNNING", "j", 30), dt=1)
        for i in range(10):
            self.feed(snap("PAUSE" if i % 2 == 0 else "RUNNING", "j", 31), dt=3)
        self.assertEqual(self.out, [])

    def test_a_blip_that_reverts_is_ignored(self):
        self.feed(snap("RUNNING", "j", 30), dt=1)
        self.feed(snap("FAILED", "j", 30), dt=2)
        self.feed(snap("RUNNING", "j", 31), dt=2)
        self.feed(snap("RUNNING", "j", 32), dt=10)
        self.assertEqual(self.out, [])

    def test_confirmed_change_is_reported_once(self):
        self.feed(snap("RUNNING", "j", 30), dt=1)
        for _ in range(6):
            self.feed(snap("FINISH", "j", 100), dt=3)
        self.assertEqual(self.kinds(), ["print_finished"])


class OfflineTests(Watch):
    CONFIRM = 5

    def up(self, dt=5, **kw):
        return self.feed(snap(**kw), dt=dt)

    def test_blip_shorter_than_grace_is_silent(self):
        self.up(state="IDLE")
        self.feed(offline(), dt=5)
        self.feed(offline(), dt=60)                      # 60 s < 90 s grace
        self.up(dt=5, state="IDLE")
        self.up(dt=10, state="IDLE")
        self.assertEqual(self.out, [])

    def test_offline_after_grace_once_then_online_once(self):
        self.up(state="RUNNING", job="benchy", percent=42)
        self.feed(offline("connection refused"), dt=5)
        self.assertEqual(self.kinds(), [])
        ev = self.feed(offline("connection refused"), dt=90)
        self.assertEqual([e.kind for e in ev], ["printer_offline"])
        self.assertEqual(ev[0].level, "warning")
        self.assertIn("OFFLINE", ev[0].title)
        self.assertIn("RUNNING at 42%", ev[0].body)
        self.assertIn("connection refused", ev[0].body)
        for _ in range(5):
            self.feed(offline(), dt=60)
        self.assertEqual(self.kinds(), ["printer_offline"])         # not repeated
        self.up(state="RUNNING", job="benchy", percent=45, dt=10)   # first reachable snapshot: confirm pending
        self.assertEqual(self.kinds(), ["printer_offline"])
        ev = self.up(state="RUNNING", job="benchy", percent=46, dt=6)
        self.assertEqual([e.kind for e in ev], ["printer_online"])
        self.assertIn("back ONLINE", ev[0].title)
        self.assertIn("RUNNING", ev[0].body)
        self.up(state="RUNNING", job="benchy", percent=47, dt=5)
        self.assertEqual(self.kinds(), ["printer_offline", "printer_online"])      # no spurious 'started'

    def test_flapping_connection_inside_grace_never_reports(self):
        self.up(state="IDLE")
        for _ in range(8):
            self.feed(offline(), dt=20)
            self.up(dt=5, state="IDLE")
            self.up(dt=5, state="IDLE")
        self.assertEqual(self.out, [])

    def test_disconnected_state_counts_as_offline(self):
        self.up(state="IDLE")
        self.feed(snap("DISCONNECTED", error="Klipper shutdown"), dt=5)
        ev = self.feed(snap("DISCONNECTED", error="Klipper shutdown"), dt=95)
        self.assertEqual([e.kind for e in ev], ["printer_offline"])
        self.assertIn("Klipper shutdown", ev[0].body)

    def test_print_finishing_while_offline_is_reported_after_return(self):
        self.up(state="RUNNING", job="benchy", percent=90)
        self.feed(offline(), dt=5)
        self.feed(offline(), dt=100)
        self.up(state="FINISH", job="benchy", percent=100, dt=5)
        self.up(state="FINISH", job="benchy", percent=100, dt=6)
        self.up(state="FINISH", job="benchy", percent=100, dt=6)
        self.up(state="FINISH", job="benchy", percent=100, dt=6)
        self.assertEqual(self.kinds(), ["printer_offline", "printer_online", "print_finished"])

    def test_back_online_not_sent_if_offline_never_was(self):
        self.up(state="IDLE")
        self.feed(offline(), dt=10)
        self.up(dt=5, state="IDLE")
        self.up(dt=10, state="IDLE")
        self.assertEqual(self.kinds(), [])

    def test_grace_period_comes_from_settings(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        st = ss.SettingsStore(d, env={})
        st.update({"notifications.offline_grace_s": 20})
        w = events.EventWatcher(self.out.append, settings=st, clock=lambda: self.t, confirm_s=0)
        for s, dt in ((snap("IDLE"), 0), (offline(), 5), (offline(), 25)):
            self.t += dt
            w.update({"P1S": s})
        self.assertEqual(self.kinds(), ["printer_offline"])


class HookTests(Watch):
    def test_backup_hooks(self):
        e = self.w.backup_done("P1S", files=12, size_bytes=5 * 1048576, name="P1S-20260331.zip")
        self.assertEqual((e.kind, e.level), ("backup_done", "info"))
        self.assertIn("12 files", e.body)
        self.assertIn("5.0 MB", e.body)
        f = self.w.backup_failed("P1S", "printer unreachable")
        self.assertEqual((f.kind, f.level), ("backup_failed", "failure"))
        self.assertIn("FAILED", f.title)
        self.assertIn("printer unreachable", f.body)
        self.assertEqual(self.kinds(), ["backup_done", "backup_failed"])

    def test_spaghetti_with_image_score_forms(self):
        e = self.w.spaghetti_suspected("P1S", 0.87, JPEG)
        self.assertEqual((e.kind, e.level), ("spaghetti_suspected", "failure"))
        self.assertIn("87%", e.body)
        self.assertEqual(e.get_image(), JPEG)
        self.assertEqual(self.fetched, [])
        self.assertIn("92%", self.w.spaghetti_suspected("P1S", 92).body)
        self.assertIn("unknown", self.w.spaghetti_suspected("P1S", None).body)
        e = self.w.spaghetti_suspected("P1S", 0.5)           # no image given: fetched lazily
        self.assertEqual(e.get_image(), JPEG)
        self.assertEqual(self.fetched, ["P1S"])

    def test_spaghetti_names_the_current_job(self):
        self.feed(snap("RUNNING", "benchy", 30))
        self.assertIn("benchy", self.w.spaghetti_suspected("P1S", 0.9).body)

    def test_preheat_auto_off(self):
        e = self.w.preheat_auto_off("P1S", 15)
        self.assertEqual(e.kind, "preheat_auto_off")
        self.assertIn("15 min", e.body)
        self.assertIn("OFF", e.title)

    def test_every_hook_kind_is_a_known_settings_event(self):
        for e in (self.w.backup_done("a"), self.w.backup_failed("a"), self.w.spaghetti_suspected("a", 1),
                  self.w.preheat_auto_off("a")):
            self.assertIn(e.kind, ss.EVENT_KINDS)
            self.assertIn(f"notifications.{e.kind}", ss.FIELDS)

    def test_a_broken_sink_does_not_break_the_poll_loop(self):
        def boom(ev):
            raise RuntimeError("sink down")
        w = events.EventWatcher(boom, clock=lambda: self.t, confirm_s=0)
        w.update({"P1S": snap("RUNNING", "j", 1)})
        self.t += 5
        ev = w.update({"P1S": snap("FINISH", "j", 100)})
        self.assertEqual([e.kind for e in ev], ["print_finished"])
        w.backup_failed("P1S", "x")

    def test_garbage_snapshots_do_not_raise(self):
        self.w.update({"A": None, "B": {}, "C": {"state": None, "connected": "yes"}, "D": "text"})
        self.w.update({"A": None, "B": {"state": 5}, "C": {"state": "RUNNING", "percent": "n/a"}})


class EndToEndTests(unittest.TestCase):
    def test_watcher_to_notifier_to_fake_ntfy(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        srv = FakeHTTP()
        self.addCleanup(srv.close)
        settings = ss.SettingsStore(d, env={})
        settings.update({"ntfy": {"enabled": True, "server_url": srv.url, "topic": "hub"}})
        n = notify.Notifier(settings, timeout=5)
        self.addCleanup(n.close)
        t = [0.0]
        w = events.EventWatcher(n.submit, settings=settings, snapshot_fetcher=lambda name: JPEG,
                                clock=lambda: t[0], confirm_s=0)
        w.update({"P1S": snap("IDLE")})
        t[0] += 5
        w.update({"P1S": snap("RUNNING", "benchy", 1)})          # 'started' is off by default
        t[0] += 3600
        w.update({"P1S": snap("FINISH", "benchy", 100)})
        t[0] += 5
        w.update({"P1S": snap("RUNNING", "next", 1)})
        t[0] += 60
        w.update({"P1S": snap("FAILED", "next", 3, error="Spaghetti")})
        self.assertTrue(n.drain(10))
        self.assertEqual(len(srv.requests), 2)
        fin, fail = srv.requests
        self.assertEqual(fin["method"], "PUT")                   # picture attached
        self.assertEqual(fin["body"], JPEG)
        h = {k.lower(): v for k, v in fin["headers"].items()}
        self.assertEqual(str(email.header.make_header(email.header.decode_header(h["title"]))),
                         "P1S finished \u00b7 benchy")                # non-ASCII titles travel RFC 2047 encoded
        self.assertIn("took 1h", str(email.header.make_header(email.header.decode_header(h["message"]))))
        h = {k.lower(): v for k, v in fail["headers"].items()}
        self.assertEqual(h["priority"], "5")
        self.assertEqual(h["title"], "P1S: print FAILED")


if __name__ == "__main__":
    unittest.main(verbosity=1)
