"""Tests for the spaghetti MODES (OFF / WATCH / ACTIVE), the confirmation count that ends in a PAUSE, and the flagged-picture store
with the owner's Real / False alarm labels.

    python3 tests/test_spag_modes.py

Part 1 (engine): app/detection.py with the fakes of tests/test_detection.py (fake fleet, camera, detector, clock) and app/flagstore.py.
Part 2 (server): app/server.py in-process (tests/test_wiring2.py's fakes: a printer stand-in that records every command, a fake detector and
a fake ntfy on 127.0.0.1) - migration from the old "excluded" list, mode changes and their auth scope, the pause through the SAME code path a
human's Pause uses, the alert texts, the job log's pause reason, the flags routes, and Cancel with "It was spaghetti".
Part 3 (page): the real dashboard in headless Chrome with a stubbed `fetch` (skipped without Chrome): the 3-way control, the badges, Settings
rows, the pop-up's verdict buttons, the Cancel chip and the gallery on 5-inch, 10-inch and phone sizes.

Everything is loopback or in-memory; no printer, camera or detector is contacted. Printer names are generic.
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import test_wiring2 as w2  # noqa: E402  (sets the throwaway config / cache paths and imports the server)
from test_detection import BIG, FakeClient, Rig, run_snap, tmpdir  # noqa: E402

import detection as det  # noqa: E402
import flagstore  # noqa: E402

logging.getLogger("bambu.detection").setLevel(logging.CRITICAL)
server = w2.server


def setUpModule():
    w2.setUpModule()            # the in-process server client (the MCP session manager needs the app's lifespan running)


def tearDownModule():
    w2.tearDownModule()


def seeded(r, name="P1S", n=400):
    """A learned baseline for the printer (ACTIVE needs LEARN_FRAMES of it)."""
    r.loop._ps_for(name).tracker._long.extend([0.0] * n)


class Pauser:
    """The on_pause hook: records the call, answers `ok`."""

    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, name, info):
        self.calls.append((name, info))
        return self.ok


def spag_rig(test, mode="active", ok=True, **kw):
    p = Pauser(ok)
    kw.setdefault("flags", flagstore.FlagStore(os.path.join(tmpdir(test), "flags")))
    r = Rig(test, client=FakeClient(default=0.0), modes={"P1S": mode}, on_pause=p, **kw)
    r.pauser = p
    return r


def quiet_print(r, n=31):
    for _ in range(n):
        r.tick()


# =================================================================== modes, migration

class ModeRules(unittest.TestCase):
    def test_off_watch_active_and_the_old_excluded_list(self):
        loop = det.DetectionLoop(lambda: {}, lambda n: None, lambda *a: None, excluded=["Old-Off"],
                                 modes={"A": "active", "B": "WATCH", "C": "off", "Old-Off-2": "bogus"})
        self.assertEqual([loop.mode_of(n) for n in ("A", "B", "C")], ["active", "watch", "off"])
        self.assertEqual(loop.mode_of("old-off"), "off")                 # migration: excluded -> OFF
        self.assertEqual(loop.mode_of("Brand-New"), "watch")             # a new printer is WATCH
        self.assertEqual(loop.mode_of("Old-Off-2"), "watch")             # an unknown word is not a mode
        loop.configure(modes={"Old-Off": "active"})                      # an explicit entry beats the old list
        self.assertEqual(loop.mode_of("Old-Off"), "active")

    def test_confirm_choices(self):
        self.assertEqual([det.normalize_confirm(x) for x in (2, "3", 4, 6, 5, None, "x")], [2, 3, 4, 6, 3, 3, 3])
        loop = det.DetectionLoop(lambda: {}, lambda n: None, lambda *a: None, confirm_pictures=4)
        self.assertEqual(loop.confirm, 4)
        loop.configure(confirm_pictures=99)
        self.assertEqual(loop.confirm, 4)                                # an unknown choice is ignored
        loop.configure(confirm_pictures="6")
        self.assertEqual(loop.confirm, 6)

    def test_an_off_printer_is_not_scored_and_a_watch_printer_never_pauses(self):
        r = spag_rig(self, "off")
        seeded(r)
        r.client.default = 3.0
        for _ in range(8):
            r.tick()
        self.assertEqual((r.client.calls, r.fetched), ([], []))          # not even a camera picture is asked for
        self.assertTrue(r.loop.status()["printers"]["P1S"]["excluded"])
        r = spag_rig(self, "watch")
        seeded(r)
        quiet_print(r)
        r.client.default = 3.0
        for _ in range(12):
            r.tick()
        self.assertTrue(r.alerts)                                        # WATCH: the warning alert still goes out
        self.assertEqual(r.pauser.calls, [])                             # ... and never a pause
        self.assertFalse(r.loop.status()["auto_pause"])


# =================================================================== the confirmation count

class Confirmation(unittest.TestCase):
    def test_n_in_a_row_pauses_and_the_count_is_shown(self):
        r = spag_rig(self)
        seeded(r)
        quiet_print(r)
        r.client.default = 3.0
        seen = []
        for _ in range(3):
            r.tick()
            seen.append(r.loop.status()["printers"]["P1S"]["confirm"])
        self.assertEqual([c["count"] for c in seen], [1, 2, 3])
        self.assertEqual({c["needed"] for c in seen}, {3})
        self.assertEqual(len(r.pauser.calls), 1)
        name, info = r.pauser.calls[0]
        self.assertEqual((name, info["count"], info["jpeg"], info["job"]), ("P1S", 3, BIG, "benchy.3mf"))
        self.assertTrue(r.loop.status()["auto_pause"])                  # some printer is ACTIVE

    def test_the_owner_can_choose_2_4_or_6(self):
        for n in (2, 4, 6):
            r = spag_rig(self, confirm_pictures=n)
            seeded(r)
            quiet_print(r)
            r.client.default = 3.0
            for i in range(1, 9):
                r.tick()
                if r.pauser.calls:
                    break
            self.assertEqual(i, n, f"confirm={n}")

    def test_one_ok_picture_starts_the_count_again(self):
        # with this smoothing a single spike reads WARNING for two pictures, then OK (see the per-picture levels below)
        r = spag_rig(self)
        seeded(r)
        quiet_print(r)
        ps = r.loop._ps["P1S"]
        seen = []
        for score in (3, 0, 0, 3, 0, 0, 3, 0, 0, 3, 0, 0):
            r.client.default = float(score)
            r.tick()
            seen.append((ps.verdict["level"], ps.consec))
        self.assertEqual(seen[:3], [("warning", 1), ("warning", 2), ("ok", 0)])      # two in a row, then an OK picture: back to 0
        self.assertEqual(max(c for _lvl, c in seen), 2)
        self.assertEqual(r.pauser.calls, [], "never three in a row, so never a pause")
        r.client.default = 3.0                                                         # now three in a row for real
        for _ in range(3):
            r.tick()
        self.assertEqual(len(r.pauser.calls), 1)

    def test_warm_up_never_pauses(self):
        r = spag_rig(self)
        seeded(r)
        r.client.default = 3.0                                           # the first 30 pictures of a print are the warm-up
        for i in range(1, 31):
            r.tick()
            self.assertEqual(r.pauser.calls, [], f"warm-up picture {i}")
        self.assertEqual(r.loop._ps["P1S"].consec, 0)
        for _ in range(3):
            r.tick()
        self.assertEqual(len(r.pauser.calls), 1)                         # warm-up over: three in a row

    def test_a_printer_still_learning_acts_like_watch(self):
        r = spag_rig(self)
        quiet_print(r)                                                   # no seeded baseline: 31 pictures of 360
        r.client.default = 3.0
        for _ in range(8):
            r.tick()
        self.assertTrue(r.alerts)                                        # the WATCH alert is sent
        self.assertEqual(r.pauser.calls, [])
        p = r.loop.status()["printers"]["P1S"]
        self.assertEqual((p["gate"], p["mode"], p["confirm"]["count"]), ("learning", "active", 0))
        self.assertLess(p["baseline_frames"], p["learn_frames"])

    def test_a_relearning_baseline_also_counts_as_learning(self):
        r = spag_rig(self)
        seeded(r)
        quiet_print(r)
        ps = r.loop._ps["P1S"]
        self.assertEqual(r.loop.active_gate("P1S", ps), "")
        ps.tracker.learning = "camera view changed"
        self.assertEqual(r.loop.active_gate("P1S", ps), "learning")

    def test_learn_only_detector_down_camera_down_and_not_running_never_pause(self):
        for what in ("learn_only", "down", "camera", "paused"):
            r = spag_rig(self, learn_only=(what == "learn_only"))
            seeded(r)
            quiet_print(r)
            r.client.default = 3.0
            if what == "down":
                r.client.fail = w2.det.ScoreResult(False, code="unreachable", error="refused")
            if what == "camera":
                r.jpeg = None
            if what == "paused":
                r.fleet["P1S"] = run_snap("PAUSE", percent=20)
            for _ in range(10):
                r.tick()
            self.assertEqual(r.pauser.calls, [], what)
        r = spag_rig(self)                                               # the gate function says why, in codes
        seeded(r)
        quiet_print(r)
        ps = r.loop._ps["P1S"]
        self.assertEqual(r.loop.active_gate("P1S", ps), "")
        r.loop._det["state"] = "down"
        self.assertEqual(r.loop.active_gate("P1S", ps), "detector_down")
        r.loop._det["state"] = "up"
        ps.camera = "no_frame"
        self.assertEqual(r.loop.active_gate("P1S", ps), "camera")
        ps.camera, ps.state = "ok", "PAUSE"
        self.assertEqual(r.loop.active_gate("P1S", ps), "not_running")

    def test_no_pause_hook_means_active_cannot_act(self):
        r = Rig(self, client=FakeClient(default=0.0), modes={"P1S": "active"})
        seeded(r)
        quiet_print(r)
        r.client.default = 3.0
        for _ in range(6):
            r.tick()
        self.assertEqual(r.loop.status()["printers"]["P1S"]["gate"], "no_pause")
        self.assertFalse(r.loop.status()["auto_pause"])


class OnePausePerEpisode(unittest.TestCase):
    def test_exactly_one_pause_however_long_the_warning_lasts(self):
        r = spag_rig(self)
        seeded(r)
        quiet_print(r)
        r.client.default = 3.0
        for _ in range(25):
            r.tick()
        self.assertEqual(len(r.pauser.calls), 1)

    def test_a_failed_pause_is_tried_once_per_episode_and_recorded(self):
        r = spag_rig(self, ok=False)
        seeded(r)
        quiet_print(r)
        r.client.default = 3.0
        for _ in range(12):
            r.tick()
        self.assertEqual(len(r.pauser.calls), 1)
        ps = r.loop._ps["P1S"]
        self.assertFalse(ps.auto_paused)
        self.assertEqual([a["level"] for a in ps.alerts].count("pause_failed"), 1)
        rec = r.loop.flags.list()[0]
        self.assertEqual((rec["paused"], rec["pause_reason"]), (False, "pause failed"))

    def test_a_human_resume_drops_to_watch_for_the_rest_of_that_print_only(self):
        r = spag_rig(self)
        seeded(r)
        quiet_print(r)
        r.client.default = 3.0
        for _ in range(4):
            r.tick()
        self.assertEqual(len(r.pauser.calls), 1)
        ps = r.loop._ps["P1S"]
        self.assertTrue(ps.auto_paused)
        r.fleet["P1S"] = run_snap("PAUSE", percent=40)                   # the printer is paused (by the hook)
        r.tick()
        st = r.loop.status()["printers"]["P1S"]
        self.assertTrue(st["auto_paused"])                               # the fleet card says PAUSED - spaghetti
        r.fleet["P1S"] = run_snap("RUNNING", percent=41)                 # a human pressed Resume
        r.tick()
        st = r.loop.status()["printers"]["P1S"]
        self.assertEqual((st["resumed"], st["auto_paused"], st["gate"]), (True, False, "resumed"))
        for _ in range(15):                                              # the false alarm keeps looking like spaghetti
            r.tick()
        self.assertEqual(len(r.pauser.calls), 1, "no pause loop on a false alarm")
        self.assertTrue(len(r.alerts) >= 1)                              # it still WATCHES and alerts
        r.fleet["P1S"] = run_snap("FINISH", percent=100)                 # next print: ACTIVE again
        r.tick()
        r.fleet["P1S"] = run_snap("RUNNING", job="next.3mf", percent=1)
        r.client.default = 0.0
        quiet_print(r, 31)
        r.client.default = 3.0
        for _ in range(4):
            r.tick()
        self.assertEqual(len(r.pauser.calls), 2)

    def test_a_paused_print_that_somebody_else_resumed_after_a_runout_is_not_a_human_resume(self):
        r = spag_rig(self)
        seeded(r)
        quiet_print(r)
        r.fleet["P1S"] = run_snap("PAUSE", percent=20)                   # a filament runout, not our pause
        r.tick()
        r.fleet["P1S"] = run_snap("RUNNING", percent=21)
        r.tick()
        self.assertFalse(r.loop._ps["P1S"].resumed)


# =================================================================== flagged pictures

class FlaggedPictures(unittest.TestCase):
    def flagged_rig(self, mode="watch", **kw):
        r = spag_rig(self, mode, **kw)
        seeded(r)
        quiet_print(r)
        r.client.default = 3.0
        return r

    def test_a_warning_saves_the_pictures_and_a_small_record(self):
        r = self.flagged_rig("active")
        for _ in range(10):
            r.tick()
        recs = r.loop.flags.list()
        self.assertEqual(len(recs), 1)                                   # one record per episode
        rec = recs[0]
        self.assertEqual((rec["printer"], rec["mode"], rec["kind"], rec["label"], rec["job"]), ("P1S", "active", "flag", "", "benchy.3mf"))
        self.assertEqual(len(rec["pictures"]), det.EPISODE_PICTURES)   # at most ~6 per episode
        self.assertEqual(len(rec["scores"]), 6)
        self.assertTrue(rec["paused"])
        self.assertEqual(rec["pause_reason"], "spaghetti (auto)")
        self.assertEqual(r.loop.flags.image(rec["id"], 1), BIG)
        self.assertEqual(r.loop.flags.image(rec["id"]), BIG)             # the last one
        self.assertIsNone(r.loop.flags.image(rec["id"], 7))
        d = r.loop.flags.dir
        self.assertTrue(os.path.exists(os.path.join(d, rec["id"] + ".json")))
        self.assertEqual(sorted(f for f in os.listdir(d) if f.endswith(".jpg")), [p["file"] for p in rec["pictures"]])

    def test_watch_flags_too_and_the_next_episode_is_a_new_record(self):
        r = self.flagged_rig("watch")
        for _ in range(3):
            r.tick()
        ps = r.loop._ps["P1S"]
        r.client.default = 0.0
        for _ in range(60):
            r.tick()
            if ps.verdict["level"] == "ok":
                break
        self.assertEqual(ps.verdict["level"], "ok")
        r.client.default = 3.0
        for _ in range(3):
            r.tick()
        recs = r.loop.flags.list()
        self.assertEqual(len(recs), 2)
        self.assertEqual({x["mode"] for x in recs}, {"watch"})
        self.assertFalse(any(x["paused"] for x in recs))

    def test_learn_only_and_off_save_nothing(self):
        r = spag_rig(self, "watch", learn_only=True)
        seeded(r)
        quiet_print(r)
        r.client.default = 3.0
        for _ in range(8):
            r.tick()
        self.assertEqual(r.loop.flags.list(), [])
        r = spag_rig(self, "off")
        for _ in range(8):
            r.tick()
        self.assertEqual(r.loop.flags.list(), [])

    def test_the_cap_keeps_the_newest_pictures(self):
        d = tmpdir(self)
        t = [1000.0]
        fs = flagstore.FlagStore(d, max_pictures=7, clock=lambda: t[0])
        ids = []
        for i in range(5):
            t[0] += 100
            rec = fs.new_record("P%d" % i, ts=t[0])
            for _ in range(3):
                fs.add_picture(rec, b"x" * 50, 0.5, t[0])
            ids.append(rec["id"])
        left = [x["id"] for x in fs.list(limit=50)]
        self.assertEqual(left, ids[::-1][:2])                            # 3 + 3 pictures fit in 7: the newest two records
        self.assertEqual(len([f for f in os.listdir(d) if f.endswith(".jpg")]), 6)
        fs2 = flagstore.FlagStore(tmpdir(self), max_pictures=1000, max_bytes=250)       # ... and the size cap
        for i in range(4):
            t[0] += 100
            rec = fs2.new_record("P", ts=t[0])
            fs2.add_picture(rec, b"y" * 100, 0.1, t[0])
        self.assertEqual(len(fs2.list()), 2)
        self.assertLessEqual(sum(os.path.getsize(os.path.join(fs2.dir, f)) for f in os.listdir(fs2.dir) if f.endswith(".jpg")), 250)

    def test_defaults_are_about_300_pictures_and_300_mb(self):
        fs = flagstore.FlagStore(tmpdir(self))
        self.assertEqual((fs.max_pictures, fs.max_bytes), (300, 300 * 1024 * 1024))

    def test_labels_real_false_missed_none_and_filters(self):
        r = self.flagged_rig("watch")
        for _ in range(3):
            r.tick()
        fs = r.loop.flags
        rid = fs.list()[0]["id"]
        rec = r.loop.mark_flag(rid, "real")
        self.assertEqual((rec["label"], rec["baseline"]), ("real", False))
        self.assertIsNotNone(rec["labelled_at"])
        self.assertEqual([x["id"] for x in fs.list(label="real")], [rid])
        self.assertEqual(fs.list(label="none"), [])
        self.assertEqual(fs.counts()["real"], 1)
        r.loop.mark_flag(rid, "none")
        self.assertEqual(fs.get(rid)["label"], "")
        self.assertEqual(len(fs.list(label="none")), 1)
        self.assertEqual(fs.list(printer="nobody"), [])
        self.assertIsNone(r.loop.mark_flag("no-such", "real"))
        with self.assertRaises(ValueError):
            fs.set_label(rid, "banana")
        self.assertIsNone(fs.get("../../etc/passwd"))                    # ids are never paths
        self.assertIsNone(fs.image("../x", 1))

    def test_false_alarm_clears_the_warning_and_the_picture_joins_the_baseline(self):
        r = self.flagged_rig("watch")
        for _ in range(4):
            r.tick()
        ps = r.loop._ps["P1S"]
        self.assertEqual(ps.verdict["level"], "failure")
        n_alerts = len(r.alerts)
        base = ps.tracker.baseline_frames
        rid = r.loop.flags.list()[0]["id"]
        rec = r.loop.mark_flag(rid, "false")
        self.assertEqual((rec["label"], rec["baseline"]), ("false", True))
        self.assertGreater(ps.tracker.baseline_frames, base)             # the labelled-false pictures went in
        self.assertEqual(ps.verdict["level"], "ok")                      # this print's warning is cleared
        for _ in range(15):                                              # the same view keeps scoring 3.0 ...
            r.tick()
        self.assertEqual(ps.verdict["level"], "ok")                      # ... and no longer alerts
        self.assertEqual(len(r.alerts), n_alerts)
        self.assertEqual(len(r.loop.flags.list()), 1)                    # nor is it flagged again
        # the lesson survives a restart: the per-printer baseline file carries it
        ps.tracker.flush()
        tr2 = det.FailureTracker("P1S", ps.tracker.params, r.store)
        self.assertGreater(tr2._floor(), 1.0)

    def test_false_alarm_does_not_blind_the_printer_to_real_spaghetti(self):
        r = self.flagged_rig("watch")
        for _ in range(4):
            r.tick()
        r.loop.mark_flag(r.loop.flags.list()[0]["id"], "false")          # the view looks like 3.0, and that is normal
        r.client.default = 9.0                                           # a real mess is far worse than "normal"
        for _ in range(6):
            r.tick()
        self.assertNotEqual(r.loop._ps["P1S"].verdict["level"], "ok")

    def test_real_label_changes_nothing_about_the_baseline(self):
        r = self.flagged_rig("watch")
        for _ in range(4):
            r.tick()
        ps = r.loop._ps["P1S"]
        base = ps.tracker.baseline_frames
        r.loop.mark_flag(r.loop.flags.list()[0]["id"], "real")
        self.assertEqual(ps.tracker.baseline_frames, base)
        self.assertEqual(ps.verdict["level"], "failure")

    def test_flagged_pictures_are_still_kept_out_of_the_baseline_until_labelled(self):
        r = self.flagged_rig("watch")
        base = r.loop._ps["P1S"].tracker.baseline_frames
        for _ in range(9):
            r.tick()
        grown = r.loop._ps["P1S"].tracker.baseline_frames - base
        self.assertLessEqual(grown, 1)                                   # (one in ADMIT_FLAGGED_EVERY still goes in - the old rule)

    def test_cancel_with_it_was_spaghetti_keeps_a_missed_example(self):
        r = self.flagged_rig("watch")
        r.tick()
        ps = r.loop._ps["P1S"]
        base = ps.tracker.baseline_frames
        rec = r.loop.save_missed("P1S")
        self.assertEqual((rec["kind"], rec["label"], rec["printer"]), ("missed", "missed", "P1S"))
        self.assertEqual(r.loop.flags.image(rec["id"]), BIG)
        self.assertEqual(ps.tracker.baseline_frames, base)               # a missed picture is an example, never baseline
        self.assertEqual(len(r.loop.flags.list(label="missed")), 1)
        r.cams["P1S"] = False
        self.assertIsNone(r.loop.save_missed("P1S"))                     # no camera: nothing, and no error

    def test_storage_problems_never_reach_the_loop(self):
        d = tmpdir(self)
        open(os.path.join(d, "flags"), "w").close()                      # a FILE where the folder should be
        r = spag_rig(self, "watch", flags=flagstore.FlagStore(os.path.join(d, "flags")))
        seeded(r)
        quiet_print(r)
        r.client.default = 3.0
        for _ in range(5):
            r.tick()                                                     # no exception
        self.assertTrue(r.alerts)


# =================================================================== the server

class CancelablePrinter(w2.CamPrinter):
    """A camera printer whose Pause really pauses (state PAUSE) and whose failure can be switched on."""
    fail_pause = False

    def pause(self):
        if self.fail_pause:
            raise server.PrinterError("the printer did not answer")
        self.commands.append("pause")
        self.state = "PAUSE"

    def stop(self):
        self.commands.append("stop")
        self.state = "FAILED"


@unittest.skipUnless(w2.HAVE, "mcp + starlette + httpx needed to import the server")
class ServerModes(w2.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ntfy = w2.FakeHTTP()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.close()

    def setUp(self):
        super().setUp()
        self.ntfy.requests.clear()
        self.scores = []
        self.fake = w2.FakeDetector(w2.detector_handler(self.scores))
        self.addCleanup(self.fake.stop)
        shutil.rmtree(server.DETECTION_DIR, ignore_errors=True)
        server.DETECTION._ps.clear()
        server.WATCHER._pause_reasons.clear()

    def enable(self, **extra):
        vals = {"detection.mode": "remote", "detection.url": self.fake.url, "detection.token": w2.TOKEN, "detection.learn_only": False}
        vals.update(extra)
        r = self.c.put("/api/settings", json={"values": vals})
        self.assertEqual(r.status_code, 200, r.text)

    def ntfy_on(self):
        server.SETTINGS.update({"ntfy.enabled": True, "ntfy.server_url": self.ntfy.url, "ntfy.topic": "spagmodes"})

    def frames(self, n, t):
        for _ in range(n):
            server.DETECTION.tick(t)
            t += 10
        return t

    def said(self):
        """Every ntfy title + message sent so far, decoded (non-ASCII header text arrives RFC 2047 encoded)."""
        import email.header
        out = []
        for req in self.ntfy.requests:
            for k, v in req["headers"].items():
                if k.lower() in ("title", "message"):
                    out.append(str(email.header.make_header(email.header.decode_header(v))))
        return "\n".join(out)

    def learned(self, name="Fake"):
        server.DETECTION._ps_for(name).tracker._long.extend([0.0] * 400)

    def active_print(self, prn):
        """ACTIVE, learned, 35 quiet pictures, then spaghetti scores. -> the clock after the quiet part."""
        self.enable(**{"detection.printer_modes": {prn.name: "active"}})
        prn.set("IDLE")
        server.WATCHER.update(server._fleet_snapshots(), self.t[0])
        prn.set("RUNNING", "benchy", 5)
        server.WATCHER.update(server._fleet_snapshots(), self.t[0] + 1)
        self.learned(prn.name)
        self.scores[:] = [0.0] * 35 + [2.5] * 12
        return self.frames(35, 5000.0)

    # ---- migration + mode changes

    def test_the_old_excluded_list_becomes_off_and_everyone_else_watch(self):
        a, b = w2.CamPrinter("Alpha"), w2.CamPrinter("Beta")
        server.PRINTERS.update({"alpha": a, "beta": b})
        self.enable(**{"detection.excluded_printers": ["Beta"]})
        st = self.c.get("/api/detection").json()
        self.assertEqual(st["setup"]["printer_modes"], {"Alpha": "watch", "Beta": "off"})
        self.assertEqual(server.DETECTION.mode_of("beta"), "off")
        self.assertEqual(self.c.get("/api/printer/Beta").json()["detection"]["mode"], "off")
        self.assertEqual(self.c.get("/api/printer/Alpha").json()["detection"]["mode"], "watch")
        self.assertFalse(st["auto_pause"])
        server.PRINTERS["gamma"] = w2.CamPrinter("Gamma")                    # a printer added later
        self.assertEqual(self.c.get("/api/detection").json()["setup"]["printer_modes"]["Gamma"], "watch")

    def test_a_mode_change_is_a_settings_write_and_the_loop_follows(self):
        prn = w2.CamPrinter()
        server.PRINTERS["fake"] = prn
        self.enable()
        r = self.c.put("/api/settings", json={"values": {"detection.printer_modes": {"Fake": "active"}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(server.DETECTION.mode_of("Fake"), "active")
        st = self.c.get("/api/detection").json()
        self.assertEqual((st["setup"]["printer_modes"]["Fake"], st["auto_pause"], st["confirm_pictures"]), ("active", True, 3))
        server.DETECTION._ps_for("Fake")
        self.assertEqual(self.c.get("/api/detection").json()["printers"]["Fake"]["mode"], "active")
        self.assertEqual(self.c.get("/api/printer/Fake").json()["detection"]["mode"], "active")
        r = self.c.put("/api/settings", json={"values": {"detection.printer_modes": {"Fake": "off"}, "detection.excluded_printers": ["Fake"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get("/api/printer/Fake").json()["detection"]["state"], "excluded")
        self.assertFalse(self.c.get("/api/detection").json()["auto_pause"])
        r = self.c.put("/api/settings", json={"values": {"detection.printer_modes": {"Fake": "paranoid"}}})
        self.assertEqual(r.status_code, 400)                                 # not a mode: nothing saved
        self.assertEqual(server.DETECTION.mode_of("Fake"), "off")

    def test_confirm_pictures_is_a_setting_with_four_choices(self):
        self.enable()
        sec = next(s for s in self.c.get("/api/settings").json()["sections"] if s["id"] == "detection")
        f = next(f for f in sec["fields"] if f["name"] == "confirm_pictures")
        self.assertEqual([o["value"] for o in f["options"]], ["2", "3", "4", "6"])
        self.assertEqual(f["value"], "3")
        self.assertIn("printer_modes", [x["name"] for x in sec["fields"]])
        self.c.put("/api/settings", json={"values": {"detection.confirm_pictures": "6"}})
        self.assertEqual(server.DETECTION.confirm, 6)
        self.assertEqual(self.c.put("/api/settings", json={"values": {"detection.confirm_pictures": "5"}}).status_code, 400)

    def test_changing_a_mode_needs_the_control_scope(self):
        server.SETTINGS.update({"security.api_auth": "on"})
        self.addCleanup(server.SETTINGS.update, {"security.api_auth": "off"})
        read, ctl = server.AUTH.create_key("reader", "read")[1], server.AUTH.create_key("controller", "control")[1]
        body = {"values": {"detection.printer_modes": {"Fake": "active"}}}
        self.assertEqual(self.c.put("/api/settings", json=body, headers={"Authorization": "Bearer " + read}).status_code, 403)
        self.assertEqual(self.c.get("/api/detection", headers={"Authorization": "Bearer " + read}).status_code, 200)
        self.assertEqual(self.c.put("/api/settings", json=body, headers={"Authorization": "Bearer " + ctl}).status_code, 200)

    def test_route_scopes(self):
        import security
        self.assertEqual(security.rest_scope("GET", "/api/detection/flags"), "read")
        self.assertEqual(security.rest_scope("GET", "/api/detection/flags/abc/image"), "read")
        self.assertEqual(security.rest_scope("POST", "/api/detection/flags/abc/label"), "control")
        self.assertEqual(security.rest_scope("PUT", "/api/settings"), "control")

    # ---- the pause

    def test_active_pauses_through_the_same_path_as_a_human_pause_and_tells_the_owner(self):
        prn = CancelablePrinter()
        server.PRINTERS["fake"] = prn
        self.ntfy_on()
        t = self.active_print(prn)
        t = self.frames(12, t)
        self.assertEqual(prn.commands, ["pause"], "exactly one pause, and never a cancel")
        self.assertEqual(prn.state, "PAUSE")
        self.assertTrue(self.ntfy.wait_for("PAUSED"), self.ntfy.titles())
        said = self.said()
        self.assertIn("\u23f8 Paused Fake \u2014 spaghetti seen in 3 pictures in a row. Check it, then Resume or Cancel in Fabrica.", said)
        for _ in range(40):                                                 # (the history is written just after the send)
            sent = [n for n in server.NOTIFIER.recent() if n["event"] == "spaghetti_paused"]
            if sent:
                break
            time.sleep(0.1)
        self.assertEqual(len(sent), 1)
        st = self.c.get("/api/detection").json()["printers"]["Fake"]
        self.assertEqual((st["auto_paused"], st["mode"]), (True, "active"))
        self.assertEqual(self.c.get("/api/printer/Fake").json()["detection"]["pause_reason"], "spaghetti (auto)")

    def test_the_pause_and_its_reason_are_in_the_job_log_and_not_announced_twice(self):
        prn = CancelablePrinter()
        server.PRINTERS["fake"] = prn
        self.ntfy_on()
        t = self.active_print(prn)
        self.frames(6, t)
        self.assertEqual(prn.state, "PAUSE")
        server.WATCHER.update(server._fleet_snapshots(), self.t[0] + 60)
        rows = server.JOBLOG._q("SELECT pauses, pause_reason FROM jobs WHERE printer='Fake'")
        self.assertEqual([(r["pauses"], r["pause_reason"]) for r in rows], [(1, "spaghetti (auto)")])
        time.sleep(0.4)
        self.assertEqual([x for x in self.ntfy.titles() if x.endswith("print PAUSED")], [], "one message, not a second generic one")

    def test_a_failed_pause_says_so(self):
        prn = CancelablePrinter()
        prn.fail_pause = True
        server.PRINTERS["fake"] = prn
        self.ntfy_on()
        t = self.active_print(prn)
        self.frames(8, t)
        self.assertEqual(prn.commands, [])
        self.assertTrue(self.ntfy.wait_for("could NOT pause"), self.ntfy.titles())
        self.assertIn("\u26a0\ufe0f Could not pause Fake \u2014 check it now.", self.said())
        self.assertEqual(server.WATCHER._pause_reasons, {})                 # the reason of a pause that never happened is dropped

    def test_watch_never_touches_the_print(self):
        prn = CancelablePrinter()
        server.PRINTERS["fake"] = prn
        self.enable()
        prn.set("RUNNING", "benchy", 5)
        self.learned()
        self.scores[:] = [0.0] * 35 + [2.5] * 12
        self.frames(47, 5000.0)
        self.assertEqual(prn.commands, [])

    # ---- flagged pictures API

    def flagged(self):
        prn = CancelablePrinter()
        server.PRINTERS["fake"] = prn
        self.enable()
        prn.set("RUNNING", "benchy", 5)
        self.learned()
        self.scores[:] = [0.0] * 35 + [2.5] * 12
        self.frames(47, 5000.0)
        r = self.c.get("/api/detection/flags")
        self.assertEqual(r.status_code, 200, r.text)
        return prn, r.json()["flags"]

    def test_flags_list_image_and_label(self):
        prn, flags = self.flagged()
        self.assertEqual(len(flags), 1)
        f = flags[0]
        self.assertEqual((f["printer"], f["mode"], f["label"], f["pictures"]), ("Fake", "watch", "", 6))
        img = self.c.get(f"/api/detection/flags/{f['id']}/image?n=2")
        self.assertEqual((img.status_code, img.headers["content-type"], img.content), (200, "image/jpeg", BIG))
        self.assertEqual(self.c.get(f"/api/detection/flags/{f['id']}/image?n=99").status_code, 404)
        self.assertEqual(self.c.get("/api/detection/flags/../../x/image").status_code, 404)
        self.assertEqual(self.c.get("/api/detection/flags?label=real").json()["flags"], [])
        self.assertEqual(self.c.get("/api/detection/flags?label=bogus").status_code, 400)
        self.assertEqual(self.c.get("/api/detection/flags?printer=fake").json()["counts"]["all"], 1)
        r = self.c.post(f"/api/detection/flags/{f['id']}/label", json={"label": "false"})
        self.assertEqual((r.status_code, r.json()["flag"]["label"], r.json()["flag"]["baseline"]), (200, "false", True))
        self.assertEqual(self.c.get("/api/detection/flags?label=false").json()["counts"]["false"], 1)
        self.assertEqual(self.c.post(f"/api/detection/flags/{f['id']}/label", json={"label": "real"}).json()["flag"]["label"], "real")
        self.assertEqual(self.c.post(f"/api/detection/flags/{f['id']}/label", json={"label": "none"}).json()["flag"]["label"], "")
        self.assertEqual(self.c.post(f"/api/detection/flags/{f['id']}/label", json={"label": "maybe"}).status_code, 400)
        self.assertEqual(self.c.post("/api/detection/flags/nope/label", json={"label": "real"}).status_code, 404)
        self.assertEqual(prn.commands, [])

    def test_labelling_needs_the_control_scope_and_listing_only_read(self):
        server.SETTINGS.update({"security.api_auth": "on"})
        self.addCleanup(server.SETTINGS.update, {"security.api_auth": "off"})
        read, ctl = server.AUTH.create_key("reader", "read")[1], server.AUTH.create_key("controller", "control")[1]
        rh, ch = {"Authorization": "Bearer " + read}, {"Authorization": "Bearer " + ctl}
        self.assertEqual(self.c.get("/api/detection/flags", headers=rh).status_code, 200)
        self.assertEqual(self.c.post("/api/detection/flags/x/label", json={"label": "real"}, headers=rh).status_code, 403)
        self.assertEqual(self.c.post("/api/detection/flags/x/label", json={"label": "real"}, headers=ch).status_code, 404)

    # ---- cancel with "It was spaghetti"

    def test_cancel_with_it_was_spaghetti_keeps_the_picture_and_still_cancels_once(self):
        prn = CancelablePrinter()
        server.PRINTERS["fake"] = prn
        prn.set("RUNNING", "benchy", 50)
        r = self.c.post("/api/printer/Fake/action", json={"action": "cancel", "was_spaghetti": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(prn.commands, ["stop"])
        recs = self.c.get("/api/detection/flags").json()["flags"]
        self.assertEqual([(x["kind"], x["label"], x["printer"]) for x in recs], [("missed", "missed", "Fake")])
        self.assertEqual(self.c.get(f"/api/detection/flags/{recs[0]['id']}/image").content, BIG)

    def test_cancel_without_the_tick_saves_nothing_and_a_storage_error_cannot_block_it(self):
        prn = CancelablePrinter()
        server.PRINTERS["fake"] = prn
        prn.set("RUNNING", "benchy", 50)
        self.assertEqual(self.c.post("/api/printer/Fake/action", json={"action": "cancel"}).status_code, 200)
        self.assertEqual(self.c.get("/api/detection/flags").json()["flags"], [])
        prn.set("RUNNING", "benchy", 50)
        real = server.DETECTION.save_missed
        server.DETECTION.save_missed = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            r = self.c.post("/api/printer/Fake/action", json={"action": "cancel", "was_spaghetti": True})
        finally:
            server.DETECTION.save_missed = real
        self.assertEqual((r.status_code, prn.commands), (200, ["stop", "stop"]))


# =================================================================== the page

HAVE_BROWSER = False
try:
    from test_layout_5in import run_q
    import test_fix_dashboard as _tfd
    from test_nav_popups import PRE
    HAVE_BROWSER = bool(_tfd.CHROME)
except Exception:      # pragma: no cover
    PRE = ""

def settings_doc():
    """The real Settings document (schema + values) as JSON, so the page renders the real Spaghetti tab."""
    import settings_store
    d = tempfile.mkdtemp(prefix="spagmodes-settings-")
    st = settings_store.SettingsStore(d)
    st.update({"detection.mode": "local", "detection.learn_only": False, "detection.url": "http://detector:8080"})
    shutil.rmtree(d, ignore_errors=True)
    return json.dumps(st.describe())


PAGE = r"""
const run = (n, pct, job) => mk(n, 'RUNNING', {percent: pct, job, layer: '40/200', remaining_min: 60, nozzle_c: 220, bed_c: 58});
FLEET.length = 0;
FLEET.push(run('Printer-A', 32, 'bracket_v3.gcode'), run('Printer-B', 41, 'Frame bezel.gcode'), mk('Printer-C', 'PAUSE', {percent: 57, job: 'clip_t_slot.gcode', remaining_min: 40, layer: '60/150'}),
           run('Printer-D', 12, 'spacer_plate.gcode'), run('Printer-E', 77, 'stand_wedge.gcode'));
const dp = (mode, o) => Object.assign({watching: true, excluded: mode === 'off', mode, gate: '', confirm: {count: 0, needed: 3}, auto_paused: false, resumed: false, flag_id: null,
  baseline_frames: 360, learn_frames: 360, learning: '', state: 'RUNNING', camera: 'ok', alerts: [], verdict: {level: 'ok', score: 0.08, warmup: false, at: Date.now() / 1000}}, o || {});
const warn = {level: 'warning', score: 0.74, warmup: false, at: Date.now() / 1000};
DET = {enabled: true, mode: 'alert', interval_s: 10, confirm_pictures: 3, auto_pause: true, detector: {state: 'up'},
  setup: {mode: 'local', excluded_printers: ['Printer-E'], confirm_pictures: 3, confirm_choices: [2, 3, 4, 6],
          printer_modes: {'Printer-A': 'watch', 'Printer-B': 'active', 'Printer-C': 'active', 'Printer-D': 'active', 'Printer-E': 'off'}},
  printers: {'Printer-A': dp('watch'), 'Printer-B': dp('active', {verdict: warn, confirm: {count: 2, needed: 3}, flag_id: 'flag-b1'}),
    'Printer-C': dp('active', {watching: false, state: 'PAUSE', auto_paused: true, verdict: warn, flag_id: 'flag-c1'}),
    'Printer-D': dp('active', {baseline_frames: 204, gate: 'learning'}), 'Printer-E': dp('off', {watching: false})}};
const FLAGS = [{id: 'flag-b1', kind: 'flag', printer: 'Printer-B', ts: 1.7e9, mode: 'active', job: 'x.gcode', level: 'warning', paused: false, label: '', score: 0.74, pictures: 3},
  {id: 'flag-c1', kind: 'flag', printer: 'Printer-C', ts: 1.69e9, mode: 'active', job: 'y.gcode', level: 'failure', paused: true, label: 'real', score: 0.93, pictures: 4},
  {id: 'miss-d1', kind: 'missed', printer: 'Printer-D', ts: 1.68e9, mode: 'watch', job: 'z.gcode', level: '', paused: false, label: 'missed', score: 0.1, pictures: 1},
  {id: 'flag-a1', kind: 'flag', printer: 'Printer-A', ts: 1.67e9, mode: 'watch', job: 'w.gcode', level: 'warning', paused: false, label: 'false', score: 0.6, pictures: 2}];
window.__puts = []; window.__labels = []; window.__flagUrls = []; window.__actions = [];
window.__stubs.unshift((u, m, init) => {
  if(u.indexOf('/api/detection/flags/') === 0 && /\/label$/.test(u)){ window.__labels.push(u + ' ' + init.body); return J({flag: Object.assign({}, FLAGS[0], {label: JSON.parse(init.body).label})}); }
  if(u.indexOf('/api/detection/flags') === 0){ window.__flagUrls.push(u); const q = new URLSearchParams(u.split('?')[1] || ''), lab = q.get('label');
    const list = FLAGS.filter(f => !lab || (lab === 'none' ? !f.label : f.label === lab));
    return J({flags: list, counts: {all: 4, real: 1, false: 1, missed: 1, none: 1, printers: ['Printer-A', 'Printer-B', 'Printer-C', 'Printer-D']}}); }
  if(u === '/api/settings' && m === 'PUT'){ window.__puts.push(init.body); return J({ok: true}); }
  if(u === '/api/settings') return J(SETTINGS_DOC);
  if(/\/action$/.test(u) && m === 'POST'){ window.__actions.push(init.body); }
  if(u === '/api/printers') return J({printers: FLEET.map(p => ({name: p.printer, type: 'moonraker', type_label: 'Klipper', address: 'x', model: '', driver_available: true, loaded: true, secrets: {}, values: {name: p.printer}, groups: ['Home']}))});
  if(u === '/api/printer/Printer-A') return J(Object.assign(snapA({state: 'RUNNING', job: 'bracket_v3.gcode', percent: 32, remaining_min: 60, nozzle_c: 220, bed_c: 58}),
    {detection: {state: 'ok', text: 'Watching', mode: 'watch', gate: '', confirm: {count: 0, needed: 3}, baseline_frames: 360, learn_frames: 360, learning: '', score: 0.08}}));
  return null; });
FL.sig = null; await fleet(); await sleep(250);
const txt = e => e ? e.textContent.replace(/\s+/g, ' ').trim() : null;
const badges = () => qa('#fleet .pcard').map(c => ({name: txt(q('.pname', c)), badge: txt(q('.spb', c)), aria: (q('.spb', c) || {}).getAttribute && q('.spb', c).getAttribute('aria-label'), big: txt(q('.spag.alert, .spg.alert', c))}));
R.badges = badges();
R.banner = txt(q('#sp-alert'));
// the 3-way control on a printer screen
await openPrinter('Printer-A'); await sleep(900);
const seg = () => qa('#sp-mode [role="radio"]').map(b => ({w: txt(q('.w', b)), on: b.getAttribute('aria-checked'), r: r(b)}));
R.seg = seg(); R.segBox = r(q('#sp-mode')); R.pspag = txt(q('#p-spag .t'));
q('#sp-mode [data-mode="active"]').click(); await sleep(700);
R.put = window.__puts.slice(); window.__puts.length = 0;
// the pop-up of the warned printer
show('home'); await sleep(200); FL.sig = null; await fleet(); await sleep(200);
spagOpen('Printer-B'); await sleep(900);
R.popup = {text: mtext(), buttons: qa('#mbody button').map(b => txt(b)), vd: qa('.sgd-vd button').map(b => ({w: txt(b), h: r(b).h}))};
q('.sgd-vd [data-label="false"]').click(); await sleep(600);
R.labels = window.__labels.slice();
spagOpen('Printer-C'); await sleep(700);
R.popupPaused = {head: txt(q('#mbody .cs-mh')), mode: txt(q('.sgd-mode')), resume: !!q('[data-act="spagResume"]')};
closeModal();
// the Cancel confirm
askCancel('Printer-B'); await sleep(300);
R.cancel = {chip: txt(q('#cn-spag')), role: q('#cn-spag').getAttribute('role'), h: r(q('#cn-spag')).h};
q('#mbody .danger').click(); await sleep(700);
R.cancelPlain = window.__actions.slice(); window.__actions.length = 0;
askCancel('Printer-B'); await sleep(300); q('#cn-spag').click(); R.chipOn = q('#cn-spag').getAttribute('aria-checked'); q('#mbody .danger').click(); await sleep(700);
R.cancelSpag = window.__actions.slice();
// Settings > Spaghetti
await openSettings(); await sleep(400); stSelect('detection'); await sleep(400); await detLoad(); await sleep(200);
R.rows = qa('#st-sp-rows .sprow').map(e => ({t: txt(q('.spn b', e)), sub: txt(q('.sps', e)), h: r(e).h, seg: qa('[role="radio"]', e).map(b => ({w: txt(q('.w', b) || b), on: b.getAttribute('aria-checked'), h: r(b).h}))}));
R.confirm = txt(q('#sp-cf-sub'));
q('[data-act="spagAll"][data-mode="watch"]').click(); await sleep(700);
R.putAll = window.__puts.slice(); window.__puts.length = 0;
q('[data-act="spagConfirm"][data-n="6"]').click(); await sleep(500);
R.putConfirm = window.__puts.slice(); window.__puts.length = 0;
// the gallery
q('[data-act="galOpen"]').click(); await sleep(700);
R.gal = {tiles: qa('.gtile').map(b => txt(b)), chips: qa('.gchip').map(b => txt(b)), count: txt(q('.gcount')), h: qa('.gchip').map(b => r(b).h)};
q('.gchip[data-v="real"]').click(); await sleep(500);
R.galFiltered = {tiles: qa('.gtile').length, url: window.__flagUrls[window.__flagUrls.length - 1]};
q('.gchip[data-v=""]').click(); await sleep(500);
q('.gtile').click(); await sleep(300);
R.galDetail = {text: txt(q('.gdet')), btns: qa('.gvd button').map(b => txt(b))};
q('.gvd [data-label="false"]').click(); await sleep(500);
R.galLabels = window.__labels.slice(1);
"""


@unittest.skipUnless(HAVE_BROWSER, "no Chrome / Edge found for the browser tests")
class PageTests(unittest.TestCase):
    RES = {}

    @classmethod
    def setUpClass(cls):
        pass

    def page(self, key, query, size):
        if key not in self.RES:
            self.RES[key] = run_q(self, "const SETTINGS_DOC = " + settings_doc() + ";\n" + PRE + "\n" + PAGE, query, size, 120000)
        R = self.RES[key]
        self.assertNotIn("error", R, R.get("error"))
        return R

    SIZES = {"5in": ("?group=Home&layout=5in", (800, 480)), "10in": ("?kiosk=1", (1024, 600)), "phone": ("", (390, 844))}

    def each(self):
        for k, (q, s) in self.SIZES.items():
            yield k, self.page(k, q, s)

    def test_the_fleet_badges_carry_the_mode_in_words(self):
        for k, R in self.each():
            by = {b["name"]: b for b in R["badges"]}
            self.assertEqual(by["Printer-A"]["badge"], "WATCH", k)
            self.assertIn("WARN 2/3", (by["Printer-B"]["badge"] or "") + " " + (by["Printer-B"]["big"] or "").replace("WARNING", "WARN"), k)
            self.assertIn("PAUSED — spaghetti", (by["Printer-C"]["badge"] or "") + " " + (by["Printer-C"]["big"] or ""), k)
            self.assertEqual(by["Printer-D"]["badge"], "ACTIVE", k)
            self.assertEqual(by["Printer-E"]["badge"], "OFF", k)
            self.assertIn("(ACTIVE)", by["Printer-D"]["aria"])
            self.assertIn("Active once learned", by["Printer-D"]["aria"])
            self.assertIn("WARNING 2/3", str(by["Printer-B"].get("aria")) + " " + str(by["Printer-B"]["big"]))

            self.assertIn("WARNING: Printer-B", R["banner"])
            self.assertIn("PAUSED — spaghetti: Printer-C", R["banner"])

    def test_the_three_way_control_has_words_one_choice_and_big_targets(self):
        for k, R in self.each():
            self.assertEqual([s["w"].split()[-1] for s in R["seg"]], ["OFF", "WATCH", "ACTIVE"], k)
            self.assertEqual([s["on"] for s in R["seg"]], ["false", "true", "false"], k)
            for s in R["seg"]:
                self.assertGreaterEqual(s["r"]["h"], 56 if k == "5in" else 44, (k, s))     # 56 px on the wall screen; 44 on the roomier layouts (the band keeps its 16:9 camera)
                self.assertGreaterEqual(s["r"]["w"], 52, (k, s))
            self.assertLessEqual(R["segBox"]["r"], {"5in": 800, "10in": 1024, "phone": 390}[k], k)       # inside the screen
            self.assertIn("WATCHING", R["pspag"])
            self.assertEqual(len(R["put"]), 1, k)
            vals = json.loads(R["put"][0])["values"]
            self.assertEqual(vals["detection.printer_modes"]["Printer-A"], "active")
            self.assertEqual(vals["detection.printer_modes"]["Printer-E"], "off")
            self.assertEqual(vals["detection.excluded_printers"], ["Printer-E"])                       # the older list is kept in step

    def test_the_popup_shows_mode_progress_and_the_two_verdict_buttons(self):
        for k, R in self.each():
            p = R["popup"]
            self.assertIn("ACTIVE", p["text"], k)
            self.assertIn("Confirming: 2 of 3 pictures", p["text"], k)
            self.assertEqual([b["w"] for b in p["vd"]], ["✓ Real spaghetti", "✗ False alarm"], k)
            for b in p["vd"]:
                self.assertGreaterEqual(b["h"], 56, k)
            self.assertEqual(len(R["labels"]), 1, k)
            self.assertTrue(R["labels"][0].endswith('"label":"false"}'), R["labels"])
            self.assertIn("/api/detection/flags/flag-b1/label", R["labels"][0])
            self.assertIn("PAUSED", R["popupPaused"]["head"], k)
            self.assertTrue(R["popupPaused"]["resume"], k)

    def test_cancel_keeps_its_one_confirm_and_offers_it_was_spaghetti(self):
        for k, R in self.each():
            self.assertIn("It was spaghetti", R["cancel"]["chip"], k)
            self.assertEqual(R["cancel"]["role"], "checkbox")
            self.assertGreaterEqual(R["cancel"]["h"], 56, k)
            self.assertEqual([json.loads(x) for x in R["cancelPlain"]], [{"action": "cancel"}], k)
            self.assertEqual(R["chipOn"], "true")
            self.assertEqual([json.loads(x) for x in R["cancelSpag"]], [{"action": "cancel", "was_spaghetti": True}], k)

    def test_settings_rows_set_all_confirm_and_the_gallery_button(self):
        for k, R in self.each():
            rows = R["rows"]
            names = [r_["t"] for r_ in rows]
            self.assertEqual(names[:5], ["Printer-A", "Printer-B", "Printer-C", "Printer-D", "Printer-E"], k)
            self.assertEqual(rows[3]["sub"], "learning 204/360")
            self.assertEqual(rows[0]["sub"], "learned")
            for r_ in rows[:5]:
                self.assertEqual([s["w"].split()[-1] for s in r_["seg"]], ["OFF", "WATCH", "ACTIVE"])
                self.assertEqual(sum(1 for s in r_["seg"] if s["on"] == "true"), 1)
                self.assertTrue(all(s["h"] >= 56 for s in r_["seg"]), k)
            self.assertTrue(any(r_["t"] == "Set all" for r_ in rows), k)
            self.assertTrue(any(r_["t"] == "Confirm before pausing" for r_ in rows), k)
            self.assertTrue(any(r_["t"] == "Flagged pictures" for r_ in rows), k)
            self.assertEqual(R["confirm"], "3 pictures ≈ 30 s · ACTIVE only")
            conf = next(r_ for r_ in rows if r_["t"] == "Confirm before pausing")
            self.assertEqual([s["w"] for s in conf["seg"]], ["2", "3", "4", "6"])
            vals = json.loads(R["putAll"][0])["values"]
            self.assertEqual(set(vals["detection.printer_modes"].values()), {"watch"}, k)
            self.assertEqual(vals["detection.excluded_printers"], [])
            self.assertEqual(json.loads(R["putConfirm"][0])["values"], {"detection.confirm_pictures": "6"})

    def test_the_gallery_filters_labels_and_relabels(self):
        for k, R in self.each():
            g = R["gal"]
            self.assertEqual(len(g["tiles"]), 4, k)
            self.assertTrue(any("✓ REAL" in t for t in g["tiles"]) and any("✗ FALSE" in t for t in g["tiles"]), k)
            self.assertTrue(any("MISSED" in t for t in g["tiles"]) and any("? NEW" in t for t in g["tiles"]), k)
            self.assertTrue(any("Printer-B" in t for t in g["tiles"]), k)
            self.assertEqual([c.split()[-1] for c in g["chips"]], ["ALL", "REAL", "FALSE", "MISSED", "NEW"], k)
            self.assertTrue(all(h >= 56 for h in g["h"]), k)
            self.assertEqual(R["galFiltered"]["tiles"], 1, k)
            self.assertIn("label=real", R["galFiltered"]["url"])
            self.assertIn("Real spaghetti", R["galDetail"]["btns"][0])
            self.assertEqual(len(R["galLabels"]), 1, k)
            self.assertTrue(R["galLabels"][0].endswith('"label":"false"}'))


if __name__ == "__main__":
    unittest.main()
