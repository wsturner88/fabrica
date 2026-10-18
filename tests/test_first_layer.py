"""Tests for the "First layer done" alert: detection per driver family, once per run, restart and
pause safety, the toggle, and the camera picture. No network, no real printers.

    python3 tests/test_first_layer.py
"""
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
from test_events import Watch, snap  # noqa: E402
from test_notify import FakeHTTP, JPEG  # noqa: E402


def fl(w):
    return [e for e in w.out if e.kind == "first_layer_done"]


class LayerMethod(Watch):
    def start(self):
        self.feed(snap("IDLE"))
        self.feed(snap("RUNNING", "benchy", 1, layer="0/200"))

    def test_fires_at_layer_2_with_picture_and_text(self):
        self.start()
        self.feed(snap("RUNNING", "benchy", 3, layer="1/200"))
        self.assertEqual(fl(self), [])
        ev = self.feed(snap("RUNNING", "benchy", 4, layer="2/200", remaining_min=75,
                            nozzle_c=215, bed_c=60))
        e = [x for x in ev if x.kind == "first_layer_done"][0]
        self.assertEqual(e.title, "First layer done — P1S")
        self.assertEqual(e.level, "info")
        self.assertEqual(e.data["method"], "layer")
        for bit in ("benchy", "4%", "1 h 15 min", "215", "Check the picture: is it sticking?"):
            self.assertIn(bit, e.body)
        self.assertEqual(e.get_image(), JPEG)
        self.assertEqual(self.fetched, ["P1S"])

    def test_once_per_run_and_pause_resume_no_repeat(self):
        self.start()
        self.feed(snap("RUNNING", "benchy", 3, layer="2/200"))
        self.feed(snap("PAUSE", "benchy", 3, layer="2/200"))
        self.feed(snap("RUNNING", "benchy", 3, layer="3/200"))
        self.feed(snap("RUNNING", "benchy", 9, layer="40/200"))
        self.assertEqual(len(fl(self)), 1)

    def test_next_print_fires_again(self):
        self.start()
        self.feed(snap("RUNNING", "benchy", 3, layer="2/200"))
        self.feed(snap("FINISH", "benchy", 100, layer="200/200"))
        self.feed(snap("RUNNING", "again", 1, layer="0/90"))
        self.feed(snap("RUNNING", "again", 3, layer="2/90"))
        self.assertEqual(len(fl(self)), 2)

    def test_stale_layer_from_previous_print_does_not_fire(self):
        self.feed(snap("IDLE"))
        self.feed(snap("RUNNING", "b", 0, layer="200/200"))     # leftover value
        self.feed(snap("RUNNING", "b", 1, layer="200/200"))
        self.assertEqual(fl(self), [])
        self.feed(snap("RUNNING", "b", 2, layer="1/90"))
        self.feed(snap("RUNNING", "b", 3, layer="2/90"))
        self.assertEqual(len(fl(self)), 1)

    def test_restart_mid_print_no_alert(self):
        self.feed(snap("RUNNING", "benchy", 40, layer="60/200"))     # first sight = baseline
        self.feed(snap("RUNNING", "benchy", 41, layer="61/200"))
        self.assertEqual(self.out, [])

    def test_restart_during_layer_1_still_alerts(self):
        self.feed(snap("RUNNING", "benchy", 1, layer="1/200"))
        self.feed(snap("RUNNING", "benchy", 2, layer="2/200"))
        self.assertEqual(len(fl(self)), 1)

    def test_not_while_paused(self):
        self.feed(snap("IDLE"))
        self.feed(snap("RUNNING", "b", 1, layer="0/9"))
        self.feed(snap("PAUSE", "b", 1, layer="3/9"))
        self.assertEqual(fl(self), [])


class ZMethod(Watch):
    def setUp(self):
        super().setUp()
        self.w.first_layer_hold_s = 20

    def z(self, z, dt=5, state="RUNNING", **kw):
        return self.feed(snap(state, "cube", 5, kind="moonraker", z_mm=z, **kw), dt=dt)

    def start(self):
        self.feed(snap("IDLE", kind="moonraker", z_mm=10))
        self.z(10)
        self.z(0.2)
        self.z(0.2)

    def test_fires_after_rise_held_20s_and_2_polls(self):
        self.start()
        self.z(0.4, dt=5)
        self.z(0.4, dt=5)
        self.z(0.4, dt=5)
        self.assertEqual(fl(self), [])
        self.z(0.4, dt=10)
        self.assertEqual(len(fl(self)), 1)
        self.assertEqual(fl(self)[0].data["method"], "z")
        self.z(0.6)
        self.z(0.6, dt=30)
        self.assertEqual(len(fl(self)), 1)

    def test_zhop_ignored(self):
        self.start()
        for _ in range(6):
            self.z(0.6, dt=5)        # up
            self.z(0.2, dt=5)        # back down: the hop never lasts 20 s
        self.assertEqual(fl(self), [])

    def test_purge_height_ignored(self):
        self.start()
        self.z(5.0)
        self.z(5.0, dt=60)
        self.assertEqual(fl(self), [])

    def test_slow_polls_need_two(self):
        self.start()
        self.z(0.4, dt=60)
        self.assertEqual(fl(self), [])           # one poll only
        self.z(0.4, dt=60)
        self.assertEqual(len(fl(self)), 1)

    def test_pause_resets_hold(self):
        self.start()
        self.z(0.4)
        self.z(0.4, state="PAUSE")
        self.z(0.4, dt=60, state="PAUSE")
        self.assertEqual(fl(self), [])

    def test_restart_mid_print_no_alert(self):
        self.z(8.0)                              # baseline
        for z in (8.0, 8.2, 8.2, 8.2, 8.4, 8.4):
            self.z(z, dt=15)
        self.assertEqual(fl(self), [])

    def test_prusalink_style_axis_z(self):
        self.feed(snap("IDLE", kind="prusalink", z_mm=5))
        for z in (0.2, 0.2, 0.4, 0.4, 0.4, 0.4, 0.4):
            self.feed(snap("RUNNING", "x", 3, kind="prusalink", z_mm=z), dt=6)
        self.assertEqual(len(fl(self)), 1)


class TimerMethod(Watch):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.st = ss.SettingsStore(self.dir, env={})
        self.w.settings = self.st

    def run_for(self, minutes, step=60):
        self.feed(snap("IDLE", kind="octoprint"))
        self.feed(snap("RUNNING", "part", 1, kind="octoprint"))
        for _ in range(int(minutes * 60 / step)):
            self.feed(snap("RUNNING", "part", 5, kind="octoprint"), dt=step)

    def test_default_8_minutes_message_says_so(self):
        self.run_for(7)
        self.assertEqual(fl(self), [])
        self.run_for_more = self.feed(snap("RUNNING", "part", 6, kind="octoprint"), dt=120)
        e = fl(self)[0]
        self.assertEqual(e.data["method"], "timer")
        self.assertIn("~8 min in", e.body)
        self.assertIn("doesn't report layers", e.body)
        self.feed(snap("RUNNING", "part", 9, kind="octoprint"), dt=600)
        self.assertEqual(len(fl(self)), 1)

    def test_setting_changes_minutes_and_zero_is_off(self):
        self.st.update({"notifications.first_layer_fallback_min": 3})
        self.run_for(4)
        self.assertEqual(len(fl(self)), 1)
        self.out.clear()
        self.st.update({"notifications.first_layer_fallback_min": 0})
        self.feed(snap("FINISH", "part", 100, kind="octoprint"))
        self.out.clear()
        self.run_for(30)
        self.assertEqual(fl(self), [])

    def test_paused_time_does_not_count(self):
        self.feed(snap("IDLE", kind="octoprint"))
        self.feed(snap("RUNNING", "p", 1, kind="octoprint"))
        self.feed(snap("RUNNING", "p", 1, kind="octoprint"), dt=60)
        self.feed(snap("PAUSE", "p", 1, kind="octoprint"), dt=60)
        self.feed(snap("PAUSE", "p", 1, kind="octoprint"), dt=3600)
        self.feed(snap("RUNNING", "p", 1, kind="octoprint"), dt=60)
        self.assertEqual(fl(self), [])

    def test_restart_mid_print_no_alert(self):
        self.feed(snap("RUNNING", "p", 40, kind="octoprint"))
        self.feed(snap("RUNNING", "p", 41, kind="octoprint"), dt=3600)
        self.assertEqual(fl(self), [])


class Delivery(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.srv = FakeHTTP()
        self.addCleanup(self.srv.close)
        self.settings = ss.SettingsStore(self.dir, env={})
        self.n = notify.Notifier(self.settings, timeout=5)
        self.addCleanup(self.n.close)
        self.settings.update({"ntfy": {"enabled": True, "server_url": self.srv.url, "topic": "hub"}})
        self.t = 1000.0
        self.w = events.EventWatcher(self.n.notify,
                                     settings=self.settings, snapshot_fetcher=lambda n: JPEG,
                                     clock=lambda: self.t, confirm_s=0)

    def run_print(self):
        for s in (snap("IDLE"), snap("RUNNING", "b", 1, layer="0/9"), snap("RUNNING", "b", 2, layer="2/9")):
            self.t += 5
            self.w.update({"P1S": s})

    def test_kind_registered(self):
        self.assertEqual(ss.EVENT_KINDS["first_layer_done"][1:], ("info", True, False))
        self.assertTrue(self.settings.get("notifications.first_layer_done"))
        self.assertEqual(self.settings.get("notifications.first_layer_fallback_min"), 8)

    def test_sent_with_picture(self):
        self.run_print()
        puts = [r for r in self.srv.requests if r["method"] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"], JPEG)

    def test_toggle_off_sends_nothing(self):
        self.settings.update({"notifications.first_layer_done": False})
        self.run_print()
        self.assertEqual(self.srv.requests, [])

    def test_picture_off_text_only(self):
        self.settings.update({"notifications.include_snapshot": False})
        self.run_print()
        self.assertEqual([r["method"] for r in self.srv.requests], ["POST"])


class DriverFields(unittest.TestCase):
    def test_moonraker_reports_z_and_layer(self):
        from moonraker import MoonrakerPrinter
        p = MoonrakerPrinter("K", "http://127.0.0.1:1")
        seen = {}

        def fake(method, path, *a, **k):
            seen["path"] = path
            return {"status": {
                "print_stats": {"state": "printing", "filename": "a.gcode", "print_duration": 10,
                                "info": {"current_layer": 2, "total_layer": 50}},
                "virtual_sdcard": {"progress": 0.1}, "display_status": {}, "extruder": {"temperature": 200, "target": 200},
                "heater_bed": {"temperature": 60, "target": 60}, "toolhead": {"position": [1, 2, 0.4, 0]},
                "webhooks": {"state": "ready"}}}
        p._call = fake
        s = p.fresh_snapshot()
        self.assertIn("toolhead", seen["path"])
        self.assertEqual(s["z_mm"], 0.4)
        self.assertEqual(s["layer"], "2/50")
        self.assertEqual(events.EventWatcher.method_for(s), "layer")

    def test_moonraker_without_layer_info_uses_z(self):
        s = {"layer": "?/50", "z_mm": 0.4}
        self.assertEqual(events.EventWatcher.method_for(s), "z")

    def test_prusalink_reports_axis_z(self):
        from prusalink import PrusaLinkPrinter
        p = PrusaLinkPrinter("M", "http://127.0.0.1:1", "pw")
        p._get_json = lambda path, **k: {"printer": {"state": "PRINTING", "axis_z": 0.45,
                                                     "temp_nozzle": 215, "target_nozzle": 215, "temp_bed": 60, "target_bed": 60},
                                         "job": {"id": 1, "progress": 5, "time_remaining": 600}}
        p._job_name = lambda jid: "cube"
        s = p.fresh_snapshot()
        self.assertEqual(s["z_mm"], 0.45)
        self.assertEqual(events.EventWatcher.method_for(s), "z")

    def test_bambu_and_octoprint_methods(self):
        self.assertEqual(events.EventWatcher.method_for({"layer": "12/300"}), "layer")
        self.assertEqual(events.EventWatcher.method_for({"layer": ""}), "timer")


if __name__ == "__main__":
    unittest.main(verbosity=1)
