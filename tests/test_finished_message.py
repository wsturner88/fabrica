"""Tests for the "print finished" message (events.py _finished_event + notify.py Telegram delivery):
finish time, took-vs-slicer, material used, the "Open in Fabrica" link, the camera picture and its
fallback, and a Hub restart in the middle of a print.

    python3 tests/test_finished_message.py

Fake clock and fake servers on 127.0.0.1; nothing leaves the machine. Names are generic.
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import events  # noqa: E402
import notify  # noqa: E402
import settings_store as ss  # noqa: E402
from test_events import snap  # noqa: E402
from test_notify import TG_TOKEN, FakeHTTP, JPEG, parse_multipart, tg_ok  # noqa: E402

HUB = "http://192.168.1.50:8000"
LINK = HUB + "/dashboard?printer=Printer-A"      # the link opens that printer's screen
TOOK = 2 * 3600 + 41 * 60            # 2h 41m


def utc(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.timezone.utc).timestamp()


# the print ends at 20:42 UTC = 3:42 PM in Chicago (CDT) / 15:42 on a 24-hour clock
END = utc(2026, 10, 17, 20, 42)
START = END - TOOK


class Base(unittest.TestCase):
    PRED = None            # file-facts prediction in seconds (None = no file facts)
    MATS = None            # file-facts filaments

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = ss.SettingsStore(self.dir, env={})
        self.settings.update({"general.timezone": "America/Chicago", "general.datetime_format": "us"})
        self.t = START
        self.out = []
        self.fetched = []
        self.facts_calls = []
        self.w = self.watcher()

    def facts(self, printer, job):
        self.facts_calls.append((printer, job))
        res = {}
        if self.PRED:
            res["seconds"] = self.PRED
        if self.MATS:
            res["filaments"] = self.MATS
        return res

    def watcher(self, **kw):
        kw.setdefault("job_facts", self.facts)
        return events.EventWatcher(self.out.append, settings=self.settings, clock=lambda: self.t,
                                   snapshot_fetcher=lambda n: self.fetched.append(n) or JPEG, confirm_s=0, **kw)

    def feed(self, s, at=None, name="Printer-A"):
        if at is not None:
            self.t = at
        n = len(self.out)
        self.w.update({name: s})
        return self.out[n:]

    def run_print(self, took=TOOK, bed=30, running=None, name="Printer-A", job="part.gcode"):
        """idle -> start -> (running snapshot) -> finish `took` seconds later. Returns the finished event."""
        self.feed(snap("IDLE"), at=START - 10, name=name)
        self.feed(snap("PREPARE", job, 0), at=START, name=name)
        self.feed(running or snap("RUNNING", job, 1), at=START + 20, name=name)
        ev = self.feed(snap("FINISH", job, 100, bed_c=bed), at=START + took, name=name)
        self.assertEqual([e.kind for e in ev], ["print_finished"])
        return ev[0]


class MessageText(Base):
    def test_early(self):
        self.PRED = 10200                                 # slicer said 2h 50m
        ev = self.run_print()
        self.assertEqual(ev.title, "Printer-A finished · part")
        self.assertEqual(ev.body.splitlines()[0], "Done 3:42 PM · took 2h 41m (slicer said 2h 50m: 9 min early)")

    def test_late(self):
        self.PRED = TOOK - 12 * 60
        ev = self.run_print()
        self.assertIn("(slicer said 2h 29m: 12 min late)", ev.body)

    def test_on_time_within_two_minutes_or_two_percent(self):
        for pred in (TOOK + 100, TOOK - 119, TOOK + 120, TOOK + 190):   # 2 % of 2h41 = 193 s
            self.setUp()
            self.PRED = pred
            self.assertIn(": on time)", self.run_print().body, pred)
        self.setUp()
        self.PRED = TOOK + 400                            # 6.7 min out: early
        self.assertIn("7 min early", self.run_print().body)

    def test_hour_scale_difference(self):
        self.PRED = TOOK + 65 * 60
        self.assertIn(": 1h 5m early)", self.run_print().body)

    def test_no_prediction_just_took(self):
        self.PRED = None
        ev = self.run_print()
        self.assertEqual(ev.body.splitlines()[0], "Done 3:42 PM · took 2h 41m")
        self.assertNotIn("slicer", ev.body)
        self.assertNotIn("slicer", ev.html)

    def test_24_hour_clock_and_time_zone(self):
        self.PRED = None
        self.settings.update({"general.datetime_format": "iso", "general.timezone": "Europe/Berlin"})
        self.assertTrue(self.run_print().body.startswith("Done 22:42 "))      # CEST = UTC+2

    def test_printer_reporting_its_own_remaining_time_becomes_the_prediction(self):
        self.PRED = None
        # at the first RUNNING reading (15 s into the print, 1 %) the printer says 170 min left -> total 170 min + 15 s
        ev = self.run_print(running=snap("RUNNING", "part.gcode", 1, remaining_min=170))
        self.assertIn("(slicer said 2h 50m: 9 min early)", ev.body)
        self.assertEqual(ev.data["predicted_from"], "remaining")

    def test_remaining_time_late_in_the_print_is_not_a_prediction(self):
        self.PRED = None
        ev = self.run_print(running=snap("RUNNING", "part.gcode", 55, remaining_min=70))
        self.assertNotIn("slicer", ev.body)
        self.assertIsNone(ev.data["predicted_seconds"])

    def test_file_facts_win_over_the_printer_estimate(self):
        self.PRED = 10200
        ev = self.run_print(running=snap("RUNNING", "part.gcode", 1, remaining_min=100))
        self.assertIn("slicer said 2h 50m", ev.body)
        self.assertEqual(ev.data["predicted_from"], "file")

    def test_file_facts_that_arrive_later_are_picked_up(self):
        self.PRED = None
        self.feed(snap("IDLE"), at=START - 10)
        self.feed(snap("RUNNING", "part.gcode", 1), at=START)
        self.PRED = 10200                                 # cache filled in a minute later
        self.feed(snap("RUNNING", "part.gcode", 2), at=START + 30)       # inside the retry gap: not asked again
        self.feed(snap("RUNNING", "part.gcode", 3), at=START + 100)
        ev = self.feed(snap("FINISH", "part.gcode", 100), at=START + TOOK)
        self.assertIn("slicer said 2h 50m", ev[0].body)

    def test_a_broken_facts_callback_does_not_break_the_message(self):
        def boom(printer, job):
            raise RuntimeError("cache unreadable")
        self.w = self.watcher(job_facts=boom)
        ev = self.run_print()
        self.assertIn("took 2h 41m", ev.body)

    def test_paused_print_gets_no_early_late_verdict(self):
        self.PRED = 10200
        self.feed(snap("IDLE"), at=START - 10)
        self.feed(snap("RUNNING", "part.gcode", 1), at=START)
        self.feed(snap("PAUSE", "part.gcode", 30), at=START + 3000)
        self.feed(snap("RUNNING", "part.gcode", 30), at=START + 3900)       # 15 min pause
        ev = self.feed(snap("FINISH", "part.gcode", 100), at=START + TOOK)
        self.assertIn("(slicer said 2h 50m; print was paused)", ev[0].body)
        self.assertNotIn("early", ev[0].body)

    def test_one_material(self):
        self.MATS = [{"type": "PETG", "used_g": 38.4}]
        self.assertIn("\nUsed PETG 38 g\n", self.run_print().body)

    def test_two_materials(self):
        self.MATS = [{"type": "PETG", "used_g": 38.2, "color": "#112233"},
                           {"type": "PLA", "used_g": 4.0, "color": "#445566"}]
        ev = self.run_print()
        self.assertIn("Used PETG 38 g + PLA 4 g", ev.body)
        self.assertEqual(ev.data["materials"], [{"type": "PETG", "grams": 38.2}, {"type": "PLA", "grams": 4.0}])

    def test_same_material_in_two_slots_is_merged_and_unused_slots_dropped(self):
        self.MATS = [{"type": "PLA", "used_g": 20.0}, {"type": "PLA", "used_g": 15.0},
                           {"type": "TPU", "used_g": 0.0}, {"type": None, "used_g": None}]
        self.assertIn("\nUsed PLA 35 g\n", self.run_print().body)

    def test_materials_unknown_means_no_line(self):
        self.MATS = None
        ev = self.run_print()
        self.assertNotIn("Used", ev.body)
        self.assertNotIn("Used", ev.html)
        self.assertNotIn("$", ev.body)

    def test_materials_come_from_the_start_of_the_print_not_the_end(self):
        """The Hub knows the AMS choice only for a while after it starts a print: what was read at the start stays."""
        self.MATS = [{"type": "PETG", "used_g": 38.0}]
        self.feed(snap("IDLE"), at=START - 10)
        self.feed(snap("RUNNING", "part.gcode", 1), at=START)
        self.MATS = [{"type": "PLA", "used_g": 99.0}]     # later lookups would say something else
        ev = self.feed(snap("FINISH", "part.gcode", 100), at=START + TOOK)
        self.assertIn("Used PETG 38 g", ev[0].body)

    def test_bed_warm_and_cool_lines(self):
        warm = self.run_print(bed=62)
        self.assertIn("Bed still 62 °C: let it cool before clearing it.", warm.body)
        self.assertNotIn("Clear the bed before", warm.body)
        self.setUp()
        cool = self.run_print(bed=30)
        self.assertIn("Clear the bed before the next print.", cool.body)
        self.assertNotIn("Bed still", cool.body)

    def test_unnamed_job_and_extensions(self):
        self.assertEqual(events.friendly_job("Phone Stand.gcode.3mf"), "Phone Stand")
        self.assertEqual(events.friendly_job("a.b.gcode"), "a.b")
        self.assertEqual(events.friendly_job("the current job"), "")
        self.setUp()
        ev = self.run_print(job="")
        self.assertEqual(ev.title, "Printer-A finished")

    def test_plain_text_has_no_emoji_and_data_is_json(self):
        self.PRED = 10200
        self.MATS = [{"type": "PETG", "used_g": 38.0}]
        self.settings.update({"notifications.hub_url": HUB})
        ev = self.run_print(bed=60)
        for text in (ev.title, ev.body):
            self.assertTrue(all(ord(c) < 0x2000 for c in text), text)         # · and ° are the only non-ASCII
        json.dumps(ev.to_dict())
        self.assertIn("finished", ev.title)

    def test_samples_for_the_report(self):
        self.PRED = 10200
        self.MATS = [{"type": "PETG", "used_g": 38.0}, {"type": "PLA", "used_g": 4.0}]
        self.settings.update({"notifications.hub_url": HUB})
        ev = self.run_print(bed=58)
        print("\n--- sample (plain: ntfy / email / Discord / webhook) ---\n" + ev.title + "\n" + ev.body)
        print("--- sample (Telegram html) ---\n" + ev.html)


class Link(Base):
    def test_no_hub_url_means_no_link_line(self):
        ev = self.run_print()
        self.assertNotIn("Fabrica", ev.body)
        self.assertNotIn("<a ", ev.html)
        self.assertNotIn("http", ev.body + ev.html)
        self.assertIsNone(ev.data["link"])

    def test_link_format(self):
        self.settings.update({"notifications.hub_url": HUB + "/"})
        ev = self.run_print()
        self.assertEqual(ev.body.splitlines()[-1], f"Open in Fabrica: {LINK}")
        self.assertEqual(ev.html.splitlines()[-1], f'\U0001F517 <a href="{LINK}">Open in Fabrica</a>')
        self.assertEqual(ev.data["link"], LINK)

    def test_dashboard_is_not_added_twice_and_https_works(self):
        self.settings.update({"notifications.hub_url": "https://hub.example.net/dashboard"})
        self.assertEqual(self.run_print().data["link"], "https://hub.example.net/dashboard?printer=Printer-A")

    def test_the_printer_name_is_url_encoded_in_the_link(self):
        self.settings.update({"notifications.hub_url": HUB})
        for name, want in (("Shop Printer", "Shop%20Printer"), ("A&B #1", "A%26B%20%231"), ("Zäh/Ü", "Z%C3%A4h%2F%C3%9C"), ("x=1?y", "x%3D1%3Fy")):
            self.setUp()
            self.settings.update({"notifications.hub_url": HUB})
            ev = self.run_print(name=name)
            self.assertEqual(ev.data["link"], HUB + "/dashboard?printer=" + want, name)
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(ev.data["link"]).query)["printer"], [name])

    def test_html_is_escaped(self):
        self.settings.update({"notifications.hub_url": "http://192.168.1.50:8000/x?a=1&b=2"})
        ev = self.run_print(job="A&B <part>.gcode")
        self.assertIn("A&amp;B &lt;part&gt;", ev.html)
        self.assertIn("a=1&amp;b=2", ev.html)
        self.assertNotIn("<part>", ev.html)

    def test_setting_exists_default_empty_and_is_validated(self):
        self.assertEqual(ss.SettingsStore(tempfile.mkdtemp(), env={}).get("notifications.hub_url"), "")
        with self.assertRaises(Exception):
            self.settings.update({"notifications.hub_url": "not a url"})
        self.settings.update({"notifications.hub_url": HUB + "/"})
        self.assertEqual(self.settings.get("notifications.hub_url"), HUB)
        self.assertIn("Hub address", ss.EVENT_HELP["print_finished"])


class Restart(Base):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.dir, "print_runs.json")

    def test_state_file_lives_in_the_data_dir_and_is_cleared_when_the_print_ends(self):
        self.feed(snap("IDLE"), at=START - 10)
        self.feed(snap("RUNNING", "part.gcode", 1), at=START)
        self.assertEqual(self.state, self.w.state_path)
        self.assertIn("Printer-A", json.load(open(self.state)))
        self.feed(snap("FINISH", "part.gcode", 100), at=START + TOOK)
        self.assertEqual(json.load(open(self.state)), {})

    def test_restart_mid_print_keeps_start_time_prediction_and_materials(self):
        self.PRED = 10200
        self.MATS = [{"type": "PETG", "used_g": 38.0}]
        self.feed(snap("IDLE"), at=START - 10)
        self.feed(snap("RUNNING", "part.gcode", 1), at=START)
        saved = json.load(open(self.state))["Printer-A"]
        self.assertEqual((saved["predicted_s"], saved["pred_src"]), (10200, "file"))
        # the Hub restarts 1 h in: a brand-new watcher, and the cache cannot answer any more
        self.PRED, self.MATS = None, None
        self.w = self.watcher()
        before = len(self.out)
        self.feed(snap("RUNNING", "part.gcode", 37), at=START + 3600)       # baseline: no event
        self.assertEqual(len(self.out), before)
        ev = self.feed(snap("FINISH", "part.gcode", 100), at=START + TOOK)
        self.assertEqual(ev[0].body.splitlines()[:2],
                         ["Done 3:42 PM · took 2h 41m (slicer said 2h 50m: 9 min early)", "Used PETG 38 g"])
        self.assertEqual(json.load(open(self.state)), {})

    def test_restart_keeps_a_prediction_taken_from_the_printers_estimate(self):
        self.PRED = None
        self.feed(snap("IDLE"), at=START - 10)
        self.feed(snap("RUNNING", "part.gcode", 1, remaining_min=170), at=START)
        self.w = self.watcher()
        self.feed(snap("RUNNING", "part.gcode", 50, remaining_min=80), at=START + 5000)
        ev = self.feed(snap("FINISH", "part.gcode", 100), at=START + TOOK)
        self.assertIn("(slicer said 2h 50m: 9 min early)", ev[0].body)

    def test_restart_keeps_the_pause_time(self):
        self.PRED = 10200
        self.feed(snap("IDLE"), at=START - 10)
        self.feed(snap("RUNNING", "part.gcode", 1), at=START)
        self.feed(snap("PAUSE", "part.gcode", 30), at=START + 3000)
        self.feed(snap("RUNNING", "part.gcode", 30), at=START + 3900)
        self.feed(snap("RUNNING", "part.gcode", 31), at=START + 3960)
        self.w = self.watcher()
        self.feed(snap("RUNNING", "part.gcode", 40), at=START + 4500)
        ev = self.feed(snap("FINISH", "part.gcode", 100), at=START + TOOK)
        self.assertIn("print was paused", ev[0].body)

    def test_restart_with_a_different_job_discards_the_old_run(self):
        self.PRED = 10200
        self.feed(snap("IDLE"), at=START - 10)
        self.feed(snap("RUNNING", "part.gcode", 1), at=START)
        self.PRED = None
        self.w = self.watcher()
        self.feed(snap("RUNNING", "other.gcode", 20), at=START + 4000)      # someone started another job meanwhile
        ev = self.feed(snap("FINISH", "other.gcode", 100), at=START + TOOK)
        self.assertNotIn("took", ev[0].body)
        self.assertNotIn("slicer", ev[0].body)
        self.assertTrue(ev[0].body.startswith("Done 3:42 PM"))

    def test_restart_without_any_saved_run_still_reports_the_finish(self):
        self.w = self.watcher()
        self.feed(snap("RUNNING", "part.gcode", 30), at=START)
        ev = self.feed(snap("FINISH", "part.gcode", 100), at=START + 3600)
        self.assertEqual([e.kind for e in ev], ["print_finished"])
        self.assertNotIn("took", ev[0].body)

    def test_stale_run_from_days_ago_is_dropped(self):
        self.PRED = 10200
        self.feed(snap("IDLE"), at=START - 10)
        self.feed(snap("RUNNING", "part.gcode", 1), at=START)
        self.w = self.watcher()
        self.feed(snap("RUNNING", "part.gcode", 10), at=START + 9 * 86400)
        self.assertEqual(json.load(open(self.state)), {})

    def test_corrupt_state_file_is_ignored(self):
        with open(self.state, "w") as fh:
            fh.write("{not json")
        self.w = self.watcher()
        self.feed(snap("RUNNING", "part.gcode", 30), at=START)
        ev = self.feed(snap("FINISH", "part.gcode", 100), at=START + 3600)
        self.assertEqual([e.kind for e in ev], ["print_finished"])


# ----------------------------------------------------------------- delivery

class Delivery(Base):
    def setUp(self):
        super().setUp()
        self.srv = FakeHTTP()
        self.addCleanup(self.srv.close)
        self.srv.respond = tg_ok
        self.settings.update({"telegram": {"enabled": True, "chat_id": "-100123", "bot_token": TG_TOKEN},
                              "notifications.hub_url": HUB})
        self.n = notify.Notifier(self.settings, clock=lambda: START, timeout=5, retry_delays=(),
                                 channel_options={"telegram": {"api_base": self.srv.url}})
        self.addCleanup(self.n.close)
        self.PRED = 10200
        self.MATS = [{"type": "PETG", "used_g": 38.0}, {"type": "PLA", "used_g": 4.0}]

    def finished(self, **kw):
        return self.run_print(**kw)

    def test_telegram_gets_html_with_the_picture_as_caption_and_a_real_link(self):
        ev = self.finished(bed=62)
        res = self.n.notify(ev)
        self.assertTrue(res["sent"], res)
        self.assertEqual([r["path"].rsplit("/", 1)[1] for r in self.srv.requests], ["sendPhoto"])
        parts = parse_multipart(self.srv.requests[0])
        self.assertEqual(parts["photo"]["data"], JPEG)
        self.assertEqual(parts["parse_mode"]["data"], b"HTML")
        cap = parts["caption"]["data"].decode()
        self.assertEqual(cap, ev.html)
        self.assertTrue(cap.startswith("✅ Printer-A finished · part\n\U0001F550 Done 3:42 PM"))
        self.assertIn("\U0001F9F5 Used PETG 38 g + PLA 4 g", cap)
        self.assertIn("Bed still 62", cap)
        self.assertTrue(cap.endswith(f'\U0001F517 <a href="{LINK}">Open in Fabrica</a>'))

    def test_no_picture_sends_text_with_link_previews_off(self):
        self.w = self.watcher()
        self.w.fetch = lambda n: None
        ev = self.finished()
        self.assertTrue(self.n.notify(ev)["sent"])
        r = self.srv.requests[0]
        self.assertTrue(r["path"].endswith("/sendMessage"))
        j = json.loads(r["body"])
        self.assertEqual(j["parse_mode"], "HTML")
        self.assertEqual(j["link_preview_options"], {"is_disabled": True})
        self.assertEqual(j["text"], ev.html)

    def test_camera_failure_still_sends_the_text(self):
        def broken(name):
            raise OSError("camera not reachable")
        self.w = self.watcher()
        self.w.fetch = broken
        ev = self.finished()
        res = self.n.notify(ev)
        self.assertTrue(res["sent"], res)
        self.assertTrue(self.srv.requests[0]["path"].endswith("/sendMessage"))
        self.assertIn("Open in Fabrica", json.loads(self.srv.requests[0]["body"])["text"])

    def test_picture_switched_off_in_settings_sends_text_only(self):
        self.settings.update({"notifications.include_snapshot": False})
        self.assertTrue(self.n.notify(self.finished())["sent"])
        self.assertTrue(self.srv.requests[0]["path"].endswith("/sendMessage"))
        self.assertEqual(self.fetched, [])

    def test_picture_refused_by_telegram_falls_back_to_text(self):
        def respond(r):
            if r["path"].endswith("/sendPhoto"):
                return 400, b'{"ok":false,"description":"Bad Request: PHOTO_INVALID_DIMENSIONS"}'
            return tg_ok(r)
        self.srv.respond = respond
        self.assertTrue(self.n.notify(self.finished())["sent"])
        self.assertEqual([r["path"].rsplit("/", 1)[1] for r in self.srv.requests], ["sendPhoto", "sendMessage"])

    def test_markup_rejected_falls_back_to_plain_text(self):
        def respond(r):
            if b"parse_mode" in r["body"]:
                return 400, b'{"ok":false,"description":"Bad Request: can\'t parse entities: Unsupported start tag"}'
            return tg_ok(r)
        self.srv.respond = respond
        self.w = self.watcher()
        self.w.fetch = lambda n: None
        ev = self.finished()
        self.assertTrue(self.n.notify(ev)["sent"])
        last = json.loads(self.srv.requests[-1]["body"])
        self.assertNotIn("parse_mode", last)
        self.assertEqual(last["text"], ev.title + "\n" + ev.body)

    def test_very_long_job_name_sends_picture_then_full_text(self):
        ev = self.finished(job="x" * 1200 + ".gcode")
        self.assertTrue(self.n.notify(ev)["sent"])
        self.assertEqual([r["path"].rsplit("/", 1)[1] for r in self.srv.requests], ["sendPhoto", "sendMessage"])
        self.assertNotIn("caption", parse_multipart(self.srv.requests[0]))

    def test_other_channels_get_plain_text_without_emoji(self):
        self.settings.update({"telegram": {"enabled": False},
                              "ntfy": {"enabled": True, "server_url": self.srv.url, "topic": "hub"}})
        self.w = self.watcher()
        self.w.fetch = lambda n: None
        ev = self.finished(bed=62)
        self.assertTrue(self.n.notify(ev)["sent"])
        r = self.srv.requests[0]
        self.assertEqual(r["method"], "POST")
        text = r["body"].decode()
        self.assertEqual(text, ev.body)
        self.assertNotIn("<a ", text)
        self.assertIn(f"Open in Fabrica: {LINK}", text)
        self.assertTrue(all(ord(c) < 0x2000 for c in text))

    def test_retry_text_keeps_the_html_valid(self):
        self.srv.respond = lambda r: (500, b"{}")
        self.n.retry_delays = (0.05,)
        self.w = self.watcher()
        self.w.fetch = lambda n: None
        ev = self.finished()
        self.n.notify(ev)
        self.srv.respond = tg_ok
        for _ in range(200):
            if len(self.srv.requests) >= 2:
                break
            time.sleep(0.05)
        last = json.loads(self.srv.requests[-1]["body"])
        self.assertIn("(Sent late: an earlier delivery attempt failed.)", last["text"])
        self.assertEqual(last["parse_mode"], "HTML")


if __name__ == "__main__":
    unittest.main(verbosity=2)
