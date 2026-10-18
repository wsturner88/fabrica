"""Tests for what the spaghetti detector is shown: the camera picture turned upright the way the dashboard shows it, cut to
the printer's watch area, and a baseline that starts over when that view changes.

    python3 tests/test_detect_orient.py

  * framefix.py   the watch-area rules, the ffmpeg command, the four outcomes (same / done / unavailable / failed);
  * real pixels   a fixture JPEG with ONE white quarter, turned / flipped / cropped by the real ffmpeg and read back
                  (skipped when there is no ffmpeg: set FABRICA_FFMPEG to one to run them);
  * the loop      a fake detector records the bytes it is sent, so the assertion is on what the DETECTOR receives;
  * baseline      reset + the words "camera view changed", persisted, cleared after a learning stretch;
  * the Lite path no ffmpeg: pictures go through as they are, one logged note, the screen says so, no baseline reset;
  * fleet / API   the stored area, PUT /api/printers/{name}/detection-area, the MCP tool, scopes, no probe while printing;
  * the editor    the box editor is on the Edit printer screen on desktop and phone, absent on a kiosk (headless Chrome).

Everything is in memory or loopback: no printer, no camera, no detector, no LAN.
"""
import asyncio
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="orient-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)
os.environ.pop("BAMBU_MCP_ALLOWED_HOSTS", None)

import detection as det  # noqa: E402
import framefix as ff  # noqa: E402
from test_detection import BIG, FakeClient, Rig, run_snap, tmpdir  # noqa: E402

logging.getLogger("bambu.detection").setLevel(logging.CRITICAL)

REAL_FFMPEG = ff.find_ffmpeg()
needs_ffmpeg = unittest.skipUnless(REAL_FFMPEG, "no ffmpeg (set FABRICA_FFMPEG to run the pixel tests)")


# =================================================================== helpers

def jpeg_size(data):
    """(width, height) from the JPEG's SOF marker."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            return (data[i + 7] << 8) | data[i + 8], (data[i + 5] << 8) | data[i + 6]
        i += 2 + ((data[i + 2] << 8) | data[i + 3])
    raise ValueError("no SOF")


def quadrant_jpeg(w=64, h=48):
    """A picture that is black except the TOP-LEFT quarter, which is white (so any turn / flip shows)."""
    r = subprocess.run([REAL_FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}",
                        "-vf", f"drawbox=x=0:y=0:w={w // 2}:h={h // 2}:color=white:t=fill", "-frames:v", "1",
                        "-pix_fmt", "yuvj420p", "-q:v", "2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                       capture_output=True, check=True)
    return r.stdout


def white_quarters(jpeg):
    """Decode and say which quarter(s) are white: a set of 'tl' 'tr' 'bl' 'br'. Also returns the size."""
    w, h = jpeg_size(jpeg)
    r = subprocess.run([REAL_FFMPEG, "-v", "error", "-f", "image2pipe", "-vcodec", "mjpeg", "-i", "pipe:0",
                        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], input=jpeg, capture_output=True, check=True)
    px = r.stdout
    assert len(px) == w * h, (len(px), w, h)
    at = lambda fx, fy: px[int(h * fy) * w + int(w * fx)] > 128           # noqa: E731
    out = {n for n, (fx, fy) in {"tl": (.25, .25), "tr": (.75, .25), "bl": (.25, .75), "br": (.75, .75)}.items() if at(fx, fy)}
    return out, (w, h)


def stub_ffmpeg(test, script_body):
    """An executable stand-in for ffmpeg (a python script) -> its path. It logs its argv to <path>.args."""
    d = tmpdir(test)
    path = os.path.join(d, "ffmpeg")
    with open(path, "w") as f:
        f.write("#!%s\nimport sys, json\nopen(__file__ + '.args', 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n%s\n"
                % (sys.executable, script_body))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class Env:
    """Set FABRICA_FFMPEG for a test and put it back."""

    def __init__(self, test, value):
        self.old = os.environ.get("FABRICA_FFMPEG")
        os.environ["FABRICA_FFMPEG"] = value
        test.addCleanup(self.restore)

    def restore(self):
        if self.old is None:
            os.environ.pop("FABRICA_FFMPEG", None)
        else:
            os.environ["FABRICA_FFMPEG"] = self.old


class Recorder(FakeClient):
    """A fake detector that keeps the pictures it was sent."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.pictures = []

    def score(self, jpeg, printer=""):
        self.pictures.append(jpeg)
        return super().score(jpeg, printer)


# =================================================================== the watch area rules

class AreaRules(unittest.TestCase):
    def test_none_empty_and_the_whole_picture_mean_no_area(self):
        for v in (None, "", {}, [], {"x": 0, "y": 0, "w": 100, "h": 100}, "0,0,100,100"):
            self.assertIsNone(ff.clean_area(v), v)

    def test_shapes_accepted(self):
        want = {"x": 10.0, "y": 20.0, "w": 60.0, "h": 50.0}
        self.assertEqual(ff.clean_area({"x": 10, "y": 20, "w": 60, "h": 50}), want)
        self.assertEqual(ff.clean_area([10, 20, 60, 50]), want)
        self.assertEqual(ff.clean_area("10, 20, 60, 50"), want)
        self.assertEqual(ff.clean_area({"x": "10", "y": 20.04, "w": 60, "h": 50}), want)        # rounded to 0.1

    def test_nonsense_is_refused_in_words(self):
        for bad in ({"x": 10}, {"x": "a", "y": 0, "w": 50, "h": 50}, [1, 2, 3], "1,2", 5, True,
                    {"x": -1, "y": 0, "w": 50, "h": 50}, {"x": 0, "y": 0, "w": 5, "h": 50},
                    {"x": 60, "y": 0, "w": 50, "h": 50}, {"x": 0, "y": 70, "w": 50, "h": 40},
                    {"x": float("nan"), "y": 0, "w": 50, "h": 50}, {"x": True, "y": 0, "w": 50, "h": 50}):
            with self.assertRaises(ValueError, msg=repr(bad)) as cm:
                ff.clean_area(bad)
            self.assertTrue(len(str(cm.exception)) > 20)

    def test_a_box_a_hair_over_the_edge_is_trimmed_not_refused(self):
        self.assertEqual(ff.clean_area({"x": 50, "y": 0, "w": 50.04, "h": 50}), {"x": 50.0, "y": 0.0, "w": 50.0, "h": 50.0})

    def test_view_of_reads_driver_flags_and_survives_a_bad_stored_area(self):
        v = ff.View.of({"flip_h": False, "flip_v": True, "rotate90": False}, {"x": 10, "y": 20, "w": 60, "h": 50})
        self.assertEqual((v.flip_h, v.flip_v, v.rotate90, v.area), (False, True, False, (10.0, 20.0, 60.0, 50.0)))
        self.assertFalse(v.identity)
        self.assertTrue(ff.View.of(None, None).identity)
        self.assertTrue(ff.View.of({}, {"x": "junk"}).identity, "a bad stored value must mean 'watch everything', never an error")
        self.assertEqual(ff.View.of(None, None).signature(), ff.IDENTITY_SIGNATURE)

    def test_signatures_differ_for_every_change(self):
        base = ff.View.of({"flip_v": True}, {"x": 10, "y": 20, "w": 60, "h": 50})
        sigs = {base.signature(), ff.View.of({"flip_v": True}).signature(), ff.View.of({"flip_h": True}, base.area and
                {"x": 10, "y": 20, "w": 60, "h": 50}).signature(), ff.View.of({"flip_v": True, "rotate90": True}).signature(),
                ff.View.of({"flip_v": True}, {"x": 10, "y": 20, "w": 60, "h": 51}).signature()}
        self.assertEqual(len(sigs), 5)

    def test_filter_chain_order_rotate_then_flips_then_crop(self):
        v = ff.View(True, True, True, (10.0, 20.0, 50.0, 40.0))
        self.assertEqual(ff.filter_chain(v), "transpose=2,hflip,vflip,crop=w=iw*0.5000:h=ih*0.4000:x=iw*0.1000:y=ih*0.2000")
        self.assertEqual(ff.filter_chain(ff.View(flip_v=True)), "vflip")
        self.assertEqual(ff.filter_chain(ff.IDENTITY), "")


# =================================================================== prepare(): plumbing with a stand-in ffmpeg

class PrepareWithStub(unittest.TestCase):
    JPEG = b"\xff\xd8" + b"x" * 300 + b"\xff\xd9"

    def test_nothing_to_do_never_starts_a_process(self):
        Env(self, "/nonexistent/ffmpeg")
        p = ff.prepare(self.JPEG, ff.IDENTITY)
        self.assertEqual((p.status, p.jpeg), ("same", self.JPEG))

    def test_the_command_has_the_filter_and_the_size_limits(self):
        path = stub_ffmpeg(self, "sys.stdin.buffer.read(); sys.stdout.buffer.write(b'\\xff\\xd8NEW\\xff\\xd9')")
        Env(self, path)
        p = ff.prepare(self.JPEG, ff.View.of({"flip_v": True}, {"x": 0, "y": 50, "w": 50, "h": 50}))
        self.assertEqual((p.status, p.jpeg), ("done", b"\xff\xd8NEW\xff\xd9"))
        args = json.loads(open(path + ".args").read().splitlines()[0])
        self.assertEqual(args[args.index("-vf") + 1], "vflip,crop=w=iw*0.5000:h=ih*0.5000:x=iw*0.0000:y=iw*0.0000".replace(
            "y=iw*0.0000", "y=ih*0.5000"))
        self.assertIn("-nostdin", args)
        self.assertIn("-noautorotate", args)
        self.assertEqual(args[args.index("-fs") + 1], str(ff.MAX_OUT_BYTES))
        self.assertEqual(args[-1], "pipe:1")

    def test_a_failing_ffmpeg_is_failed_not_sent_raw(self):
        Env(self, stub_ffmpeg(self, "sys.stdin.buffer.read(); sys.stderr.write('Invalid argument'); sys.exit(1)"))
        p = ff.prepare(self.JPEG, ff.View(flip_v=True))
        self.assertEqual(p.status, "failed")

    def test_garbage_output_and_an_endless_run_are_failed(self):
        Env(self, stub_ffmpeg(self, "sys.stdin.buffer.read(); sys.stdout.buffer.write(b'not a jpeg')"))
        self.assertEqual(ff.prepare(self.JPEG, ff.View(flip_v=True)).status, "failed")
        Env(self, stub_ffmpeg(self, "import time; time.sleep(30)"))
        self.assertEqual(ff.prepare(self.JPEG, ff.View(flip_v=True), timeout=0.5).status, "failed")

    def test_input_limits(self):
        Env(self, stub_ffmpeg(self, "sys.exit(9)"))
        self.assertEqual(ff.prepare(b"GIF89a....", ff.View(flip_v=True)).status, "failed")
        self.assertEqual(ff.prepare(b"\xff\xd8" + b"0" * ff.MAX_IN_BYTES, ff.View(flip_v=True)).status, "failed")

    def test_lite_build_without_ffmpeg_gives_the_picture_back_and_says_so_once(self):
        Env(self, "/nonexistent/ffmpeg")
        ff._told.clear()
        self.assertFalse(ff.available())
        with self.assertLogs("bambu.framefix", "WARNING") as cm:
            p = ff.prepare(self.JPEG, ff.View(flip_v=True))
        self.assertEqual((p.status, p.jpeg), ("unavailable", self.JPEG))
        self.assertIn("no ffmpeg", cm.output[0])
        with self.assertRaises(AssertionError):                     # the second picture adds no log line
            with self.assertLogs("bambu.framefix", "WARNING"):
                ff.prepare(self.JPEG, ff.View(flip_v=True))
        self.assertIn("no ffmpeg", ff.note())
        v = ff.View(flip_v=True)
        self.assertEqual(ff.effective(v), ff.IDENTITY)              # nothing is turned, so the baseline keeps its view
        self.assertIs(ff.effective(ff.IDENTITY), ff.IDENTITY)


# =================================================================== prepare(): the real pixels

@needs_ffmpeg
class PrepareRealPixels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = quadrant_jpeg()

    def setUp(self):
        Env(self, REAL_FFMPEG)

    def run_view(self, **kw):
        p = ff.prepare(self.src, ff.View.of({k: v for k, v in kw.items() if k != "area"}, kw.get("area")))
        self.assertEqual(p.status, "done", p.error)
        return white_quarters(p.jpeg)

    def test_the_fixture_is_what_we_think(self):
        self.assertEqual(white_quarters(self.src), ({"tl"}, (64, 48)))

    def test_vertical_flip_puts_the_top_down(self):
        self.assertEqual(self.run_view(flip_v=True), ({"bl"}, (64, 48)))

    def test_horizontal_flip(self):
        self.assertEqual(self.run_view(flip_h=True), ({"tr"}, (64, 48)))

    def test_both_flips_is_a_half_turn(self):
        self.assertEqual(self.run_view(flip_h=True, flip_v=True), ({"br"}, (64, 48)))

    def test_rotate90_is_counter_clockwise_and_swaps_the_size(self):
        self.assertEqual(self.run_view(rotate90=True), ({"bl"}, (48, 64)))          # top-left corner goes to the bottom-left

    def test_rotate_then_flip(self):
        self.assertEqual(self.run_view(rotate90=True, flip_h=True), ({"br"}, (48, 64)))

    def test_crop_is_cut_from_the_turned_picture(self):
        # upside-down camera: after turning, the white quarter is at the bottom-left
        top = self.run_view(flip_v=True, area={"x": 0, "y": 0, "w": 50, "h": 50})
        bottom = self.run_view(flip_v=True, area={"x": 0, "y": 50, "w": 50, "h": 50})
        self.assertEqual(top[0], set())
        self.assertEqual(bottom[0], {"tl", "tr", "bl", "br"})                       # the crop is all white
        self.assertEqual((top[1], bottom[1]), ((32, 24), (32, 24)))

    def test_crop_without_a_turn(self):
        out, size = self.run_view(area={"x": 25, "y": 25, "w": 50, "h": 50})
        self.assertEqual(size, (32, 24))
        self.assertEqual(out, {"tl"})                                                # the white quarter's corner is inside

    def test_output_is_a_valid_jpeg_with_an_end_marker(self):
        p = ff.prepare(self.src, ff.View(flip_v=True))
        self.assertEqual((p.jpeg[:2], p.jpeg[-2:]), (b"\xff\xd8", b"\xff\xd9"))
        self.assertLess(len(p.jpeg), ff.MAX_OUT_BYTES)


# =================================================================== the loop: what the detector receives

def view_of(**kw):
    area = kw.pop("area", None)
    return ff.View.of(kw, area)


class LoopRig(Rig):
    def __init__(self, test, view, picture, **kw):
        self.view = view
        self.recorder = Recorder()
        kw.setdefault("blind_bytes", 0)
        super().__init__(test, client=self.recorder, view_fetcher=lambda name: self.view, **kw)
        self.jpeg = picture


@needs_ffmpeg
class LoopSendsPreparedPictures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = quadrant_jpeg()

    def setUp(self):
        Env(self, REAL_FFMPEG)

    def test_an_upside_down_camera_reaches_the_detector_upright(self):
        r = LoopRig(self, view_of(flip_v=True), self.src)
        r.tick()
        self.assertEqual(len(r.recorder.pictures), 1)
        self.assertEqual(white_quarters(r.recorder.pictures[0]), ({"bl"}, (64, 48)))
        self.assertEqual(r.loop.status()["printers"]["P1S"]["picture"]["prep"], "ok")

    def test_the_watch_area_reaches_the_detector_as_a_crop(self):
        r = LoopRig(self, view_of(area={"x": 0, "y": 0, "w": 50, "h": 50}), self.src)
        r.tick()
        out, size = white_quarters(r.recorder.pictures[0])
        self.assertEqual((size, out), ((32, 24), {"tl", "tr", "bl", "br"}))
        st = r.loop.status()["printers"]["P1S"]["picture"]
        self.assertEqual(st["area"], {"x": 0.0, "y": 0.0, "w": 50.0, "h": 50.0})

    def test_flip_and_crop_together(self):
        r = LoopRig(self, view_of(flip_v=True, area={"x": 0, "y": 50, "w": 50, "h": 50}), self.src)
        r.tick()
        self.assertEqual(white_quarters(r.recorder.pictures[0])[0], {"tl", "tr", "bl", "br"})

    def test_a_printer_that_needs_nothing_gets_its_picture_untouched(self):
        r = LoopRig(self, view_of(), self.src)
        r.tick()
        self.assertEqual(r.recorder.pictures, [self.src])
        self.assertEqual(r.loop.status()["printers"]["P1S"]["picture"]["prep"], "off")

    def test_the_alert_picture_is_upright_and_whole_not_cropped(self):
        r = LoopRig(self, view_of(flip_v=True, area={"x": 0, "y": 50, "w": 50, "h": 50}), self.src)
        r.recorder.default = 0.0
        for _ in range(60):
            r.tick()
        r.fleet["P1S"] = run_snap("FINISH", percent=100)
        r.tick()
        r.fleet["P1S"] = run_snap("RUNNING", job="second.3mf", percent=1)
        r.recorder.default = 3.0
        for _ in range(31):
            r.tick()
        self.assertEqual(len(r.alerts), 1)
        shown = r.alerts[0][3]
        self.assertEqual(white_quarters(shown), ({"bl"}, (64, 48)), "whole picture, turned upright")

    def test_score_once_is_prepared_too(self):
        r = LoopRig(self, view_of(flip_v=True), self.src)
        r.loop._has_camera = lambda n: True
        out = r.loop.score_once("P1S")
        self.assertTrue(out["ok"], out)
        self.assertEqual(white_quarters(r.recorder.pictures[-1])[0], {"bl"})


class LoopPrepareFailsOrIsMissing(unittest.TestCase):
    PIC = b"\xff\xd8" + b"y" * 3000 + b"\xff\xd9"

    def test_a_picture_that_cannot_be_prepared_is_not_scored_raw(self):
        Env(self, stub_ffmpeg(self, "sys.stdin.buffer.read(); sys.exit(1)"))
        r = LoopRig(self, view_of(flip_v=True), self.PIC)
        r.tick()
        r.tick()
        self.assertEqual(r.recorder.pictures, [], "the baseline must not be fed a picture of the wrong view")
        p = r.loop.status()["printers"]["P1S"]
        self.assertEqual(p["camera"], "unprepared")
        self.assertEqual(p["frames_scored"], 0)

    def test_lite_build_scores_the_picture_as_it_is_and_says_so_in_words(self):
        Env(self, "/nonexistent/ffmpeg")
        ff._told.clear()
        r = LoopRig(self, view_of(flip_v=True, area={"x": 10, "y": 10, "w": 50, "h": 50}), self.PIC)
        r.tick()
        self.assertEqual(r.recorder.pictures, [self.PIC])
        p = r.loop.status()["printers"]["P1S"]
        self.assertIn("no ffmpeg", p["picture"]["note"])
        self.assertEqual(p["picture"]["prep"], "off")
        self.assertEqual(p["learning"], "", "nothing was changed in what the detector sees, so nothing is re-learned")
        out = r.loop.score_once("P1S")
        self.assertIn("no ffmpeg", out["picture_note"])

    def test_lite_build_keeps_its_baseline_when_the_camera_is_flipped(self):
        Env(self, "/nonexistent/ffmpeg")
        r = LoopRig(self, view_of(), self.PIC)
        for _ in range(5):
            r.tick()
        self.assertEqual(r.loop._ps["P1S"].tracker.baseline_frames, 5)
        r.view = view_of(flip_v=True)                       # the Hub learns the camera is upside down...
        for _ in range(3):
            r.tick()
        self.assertEqual(r.loop._ps["P1S"].tracker.baseline_frames, 8, "...but no transform happens here, so keep the baseline")
        self.assertEqual(r.loop.status()["printers"]["P1S"]["learning"], "")


# =================================================================== baseline hygiene

class BaselineReset(unittest.TestCase):
    def tracker(self, d, **kw):
        return det.FailureTracker("P1S", det.Params(warmup_frames=0), d, **kw)

    def feed(self, t, n, score=0.3):
        for _ in range(n):
            t.observe(score)

    def test_a_new_printer_just_remembers_its_view(self):
        d = tmpdir(self)
        t = self.tracker(d)
        self.assertFalse(t.set_view(view_of(flip_v=True).signature()))
        self.assertEqual(t.learning, "")

    def test_a_changed_view_throws_the_baseline_away_and_says_why(self):
        d = tmpdir(self)
        t = self.tracker(d)
        t.set_view("h0v0r0|full")
        self.feed(t, 50)
        self.assertEqual(t.baseline_frames, 50)
        self.assertFalse(t.set_view("h0v0r0|full"))                      # same view: nothing happens
        self.assertEqual(t.baseline_frames, 50)
        self.assertTrue(t.set_view("h0v1r0|full"))
        self.assertEqual((t.baseline_frames, t.learning, t.frame), (0, "camera view changed", 0))
        with open(t.path) as f:                                         # written straight away
            doc = json.load(f)
        self.assertEqual((doc["long"], doc["view"], doc["learning"]), ([], "h0v1r0|full", "camera view changed"))

    def test_the_new_baseline_gets_the_warm_up_again(self):
        d = tmpdir(self)
        t = det.FailureTracker("P1S", det.Params(warmup_frames=5), d)
        t.set_view("a")
        self.feed(t, 20, 0.1)
        t.set_view("b")
        self.assertTrue(t.observe(0.9).warmup)

    def test_it_survives_a_restart_and_clears_after_a_learning_stretch(self):
        d = tmpdir(self)
        t = self.tracker(d)
        t.set_view("a")
        self.feed(t, 10)
        t.set_view("b")
        t2 = self.tracker(d)                                           # the Hub restarted
        self.assertEqual((t2.learning, t2.baseline_frames), ("camera view changed", 0))
        self.assertFalse(t2.set_view("b"))
        self.feed(t2, det.FailureTracker.LEARN_FRAMES - 1)
        self.assertEqual(t2.learning, "camera view changed")
        self.feed(t2, 1)
        self.assertEqual(t2.learning, "")
        t2.flush()
        self.assertEqual(self.tracker(d).learning, "")

    def test_a_baseline_file_from_before_means_the_untouched_picture(self):
        d = tmpdir(self)
        with open(os.path.join(d, "P1S.json"), "w") as f:
            json.dump({"v": 1, "printer": "P1S", "saved": 1, "long": [0.3] * 40}, f)
        t = self.tracker(d)
        self.assertFalse(t.set_view(ff.IDENTITY_SIGNATURE), "same picture as always: keep what was learned")
        self.assertEqual(t.baseline_frames, 40)
        t = self.tracker(d)
        self.assertTrue(t.set_view(view_of(flip_v=True).signature()), "an upside-down camera: the old baseline is of the wrong picture")
        self.assertEqual(t.baseline_frames, 0)

    def test_the_loop_resets_when_the_orientation_or_the_area_changes_and_the_status_says_so(self):
        Env(self, "/nonexistent/ffmpeg")                                # (the loop part does not need real pixels)
        r = LoopRig(self, view_of(), BIG)
        # pretend ffmpeg is there so the views count: patch availability
        orig = ff.available
        ff.available = lambda: True
        self.addCleanup(setattr, ff, "available", orig)
        for _ in range(6):
            r.tick()
        st = r.loop.status()["printers"]["P1S"]
        self.assertEqual((st["baseline_frames"], st["learning"]), (6, ""))
        r.fleet["P1S"] = run_snap("FINISH", percent=100)                # the print ended; the view is still checked
        r.tick()
        r.view = view_of(flip_v=True)
        r.tick()                                                        # a tick is enough, no picture needed
        st = r.loop.status()["printers"]["P1S"]
        self.assertEqual((st["baseline_frames"], st["learning"], st["frames_scored"]), (0, "camera view changed", 0))
        self.assertIsNone(st["verdict"])
        # moving the watch area does it again
        r.view = view_of(flip_v=True, area={"x": 10, "y": 10, "w": 50, "h": 50})
        r.tick()
        self.assertEqual(r.loop.status()["printers"]["P1S"]["learning"], "camera view changed")
        # the same view again: nothing more happens
        n = r.loop._ps["P1S"].tracker.baseline_frames
        r.tick()
        self.assertEqual(r.loop._ps["P1S"].tracker.baseline_frames, n)

    def test_a_printer_with_no_camera_is_left_alone(self):
        r = LoopRig(self, None, BIG)
        r.view = None
        r.tick()
        self.assertEqual(r.loop.status()["printers"]["P1S"]["learning"], "")


# =================================================================== the Hub: store, API, MCP, words

try:
    import fleet_store  # noqa: E402
    import security  # noqa: E402
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover - the app's deps are not installed
    HAVE = False

CLIENT = None


def setUpModule():
    global CLIENT
    if HAVE:
        CLIENT = TestClient(server.mcp.streamable_http_app())
        CLIENT.__enter__()


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    shutil.rmtree(_TMP, ignore_errors=True)


class CamDriver:
    kind = "octoprint"
    uses_files_api = False
    has_camera = True

    def __init__(self, name, orientation=None, state="IDLE"):
        self.name, self._ori, self.state = name, orientation, state
        self.frames = 0

    def camera_info(self):
        return self._ori

    def camera(self):
        return self

    def get_frame(self, wait=0):
        self.frames += 1
        return BIG, 100.0 + self.frames

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": self.state, "job": "x.gcode", "percent": 10,
                "remaining_min": None}

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


def put_cam(name, orientation=None, state="IDLE"):
    server.STORE.add({"name": name, "type": "octoprint", "url": f"http://{name.lower()}.invalid"})
    server.PRINTERS[name.lower()] = CamDriver(name, orientation, state)
    return server.PRINTERS[name.lower()]


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class HubTests(unittest.TestCase):
    def setUp(self):
        server.SETTINGS.update({"security.api_auth": "off"})
        reset_fleet()
        self.c = CLIENT
        self.addCleanup(reset_fleet)

    def run_tool(self, fn, *a, **kw):
        out = fn(*a, **kw)
        return asyncio.run(out) if asyncio.iscoroutine(out) else out

    # ---- store + fleet

    def test_the_area_is_stored_with_the_printer_and_shown_back(self):
        put_cam("Cam")
        res = server.FLEET.set_detect_area("cam", {"x": 10, "y": 20, "w": 60, "h": 50})
        self.assertEqual(res["detect_area"], {"x": 10.0, "y": 20.0, "w": 60.0, "h": 50.0})
        self.assertTrue(res["changed"])
        self.assertEqual(server.STORE.get("Cam")["detect_area"], {"x": 10.0, "y": 20.0, "w": 60.0, "h": 50.0})
        self.assertEqual(self.c.get("/api/printers").json()["printers"][0]["detect_area"], {"x": 10.0, "y": 20.0, "w": 60.0, "h": 50.0})
        self.assertNotIn("detect_area", self.c.get("/api/printers").json()["printers"][0]["values"])
        again = server.FLEET.set_detect_area("Cam", {"x": 10, "y": 20, "w": 60, "h": 50})
        self.assertFalse(again["changed"])
        cleared = server.FLEET.set_detect_area("Cam", None)
        self.assertEqual((cleared["detect_area"], cleared["changed"]), (None, True))
        self.assertNotIn("detect_area", server.STORE.get("Cam"))

    def test_bad_areas_and_unknown_printers_are_refused(self):
        put_cam("Cam")
        with self.assertRaises(fleet_store.FleetError):
            server.FLEET.set_detect_area("Cam", {"x": 10, "y": 20, "w": 2, "h": 50})
        with self.assertRaises(fleet_store.NotFound):
            server.FLEET.set_detect_area("Nope", None)
        self.assertNotIn("detect_area", server.STORE.get("Cam"))

    def test_it_works_while_the_printer_is_printing_and_never_probes_or_reconnects(self):
        drv = put_cam("Cam", state="RUNNING")
        before = server.PRINTERS["cam"]
        res = server.FLEET.update("Cam", {"detect_area": {"x": 0, "y": 0, "w": 50, "h": 50}})
        self.assertEqual(res["changed"], ["detect_area"])
        self.assertIs(server.PRINTERS["cam"], before, "the driver must not be rebuilt")
        self.assertIs(before, drv)

    def test_update_with_the_whole_form_and_a_bad_area_changes_nothing(self):
        put_cam("Cam")
        server.FLEET.set_detect_area("Cam", {"x": 0, "y": 0, "w": 50, "h": 50})
        with self.assertRaises(fleet_store.FleetError):
            server.FLEET.update("Cam", {"detect_area": "garbage", "groups": ["A"]})
        self.assertEqual(server.STORE.get("Cam").get("groups"), None)
        self.assertEqual(server.STORE.get("Cam")["detect_area"]["w"], 50.0)

    def test_the_rest_route(self):
        put_cam("Cam")
        r = self.c.put("/api/printers/Cam/detection-area", json={"area": {"x": 5, "y": 5, "w": 40, "h": 40}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["detect_area"], {"x": 5.0, "y": 5.0, "w": 40.0, "h": 40.0})
        self.assertEqual(self.c.put("/api/printers/Cam/detection-area", json={"area": {"x": 5}}).status_code, 400)
        self.assertEqual(self.c.put("/api/printers/Nope/detection-area", json={"area": None}).status_code, 404)
        r = self.c.put("/api/printers/Cam/detection-area", json={"area": None})
        self.assertEqual(r.json()["detect_area"], None)
        # and through the normal edit route, like the other camera fields
        r = self.c.put("/api/printers/Cam", json={"values": {"detect_area": {"x": 1, "y": 2, "w": 30, "h": 30}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(server.STORE.get("Cam")["detect_area"]["x"], 1.0)

    def test_the_mcp_tool(self):
        put_cam("Cam")
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        self.assertIn("set_detection_area", names)
        res = json.loads(self.run_tool(server.set_detection_area, "cam", {"x": 30, "y": 20, "w": 60, "h": 70}))
        self.assertEqual(res["detect_area"], {"x": 30.0, "y": 20.0, "w": 60.0, "h": 70.0})
        self.assertTrue(self.run_tool(server.set_detection_area, "cam", {"x": 30, "y": 20, "w": 1, "h": 70}).startswith("REFUSED"))
        self.assertTrue(self.run_tool(server.set_detection_area, "nope", None).startswith("REFUSED"))
        self.assertIsNone(json.loads(self.run_tool(server.set_detection_area, "cam", None))["detect_area"])

    def test_scopes_a_read_only_key_cannot_set_it(self):
        self.assertEqual(security.tool_scope("set_detection_area"), "control")
        self.assertEqual(security.rest_scope("PUT", "/api/printers/Cam/detection-area"), "control")
        self.assertTrue(security.rest_rule_known("PUT", "/api/printers/Cam/detection-area"))

    def test_list_printers_json_carries_the_orientation_and_the_area(self):
        put_cam("Cam", {"flip_h": False, "flip_v": True, "rotate90": False})
        server.FLEET.set_detect_area("Cam", {"x": 10, "y": 20, "w": 60, "h": 50})
        p = self.c.get("/api/printers").json()["printers"][0]
        self.assertEqual(p["camera_orientation"], {"flip_h": False, "flip_v": True, "rotate90": False})
        f = server._camera_fields(server.PRINTERS["cam"])
        self.assertEqual(f["detect_area"], {"x": 10.0, "y": 20.0, "w": 60.0, "h": 50.0})
        self.assertEqual(f["camera_orientation"]["flip_v"], True)

    # ---- what the loop is told

    def test_the_view_the_hub_hands_the_loop(self):
        put_cam("Cam", {"flip_h": False, "flip_v": True, "rotate90": False})
        server.FLEET.set_detect_area("Cam", {"x": 10, "y": 20, "w": 60, "h": 50})
        v = server._detection_view("cam")
        self.assertEqual((v.flip_h, v.flip_v, v.rotate90, v.area), (False, True, False, (10.0, 20.0, 60.0, 50.0)))
        put_cam("Bam", None)                                           # a driver with no orientation (a Bambu): shown as it comes
        self.assertTrue(server._detection_view("bam").identity)
        server.PRINTERS["bam"].has_camera = False
        self.assertIsNone(server._detection_view("bam"))
        self.assertIsNone(server._detection_view("nobody"))

    def test_the_real_loop_sends_the_detector_an_upright_cropped_picture(self):
        if not REAL_FFMPEG:
            self.skipTest("no ffmpeg")
        Env(self, REAL_FFMPEG)
        put_cam("Cam", {"flip_h": False, "flip_v": True, "rotate90": False}, "RUNNING")
        server.FLEET.set_detect_area("Cam", {"x": 0, "y": 50, "w": 50, "h": 50})
        src = quadrant_jpeg()
        server.PRINTERS["cam"].get_frame = lambda wait=0: (src, 500.0 + server.PRINTERS["cam"].frames)
        rec = Recorder()
        old_client, old_blind = server.DETECTION._client, server.DETECTION.blind_bytes
        server.DETECTION.configure(client=rec)
        server.DETECTION.blind_bytes = 0
        self.addCleanup(lambda: (server.DETECTION.configure(client=old_client), setattr(server.DETECTION, "blind_bytes", old_blind),
                                 server.DETECTION._ps.clear()))
        server.DETECTION._ps.clear()
        server.DETECTION.tick(10_000.0)
        self.assertEqual(len(rec.pictures), 1)
        out, size = white_quarters(rec.pictures[0])
        self.assertEqual((size, out), ((32, 24), {"tl", "tr", "bl", "br"}))
        st = server.DETECTION.status()["printers"]["Cam"]
        self.assertEqual(st["picture"]["flip_v"], True)

    # ---- the words on the printer screen

    def line(self, name, **printer_status):
        ps = {"watching": False, "excluded": False, "state": "IDLE", "camera": "ok", "frames_scored": 0, "baseline_frames": 0,
              "learning": "", "picture": {"prep": "off", "note": ""}, "verdict": None, "alerts": [], "detector_down": False}
        ps.update(printer_status)
        st = {"enabled": True, "mode": "alert", "detector": {"state": "up"}, "printers": {name: ps}}
        old = server.DETECTION.status
        server.DETECTION.status = lambda: st
        self.addCleanup(setattr, server.DETECTION, "status", old)
        return server._detection_line(name)

    def test_learning_after_a_view_change_is_said_in_words(self):
        put_cam("Cam")
        line = self.line("Cam", learning="camera view changed")
        self.assertEqual(line["state"], "learning")
        self.assertTrue(line["text"].startswith("LEARNING - camera view changed"), line)
        line = self.line("Cam", learning="camera view changed", watching=True, state="RUNNING",
                         verdict={"score": 0.02, "level": "ok", "warmup": False})
        self.assertEqual(line["text"], "LEARNING - camera view changed \u00b7 Watching \u00b7 score 0.02")
        self.assertEqual(self.line("Cam", watching=True, state="RUNNING",
                                   verdict={"score": 0.02, "level": "ok", "warmup": False})["text"], "Watching \u00b7 score 0.02")

    def test_an_alert_is_never_hidden_by_learning(self):
        put_cam("Cam")
        line = self.line("Cam", learning="camera view changed", watching=True, state="RUNNING",
                         verdict={"score": 0.9, "level": "failure", "warmup": False})
        self.assertEqual(line["state"], "failure")
        self.assertTrue(line["text"].startswith("FAILURE - likely spaghetti"), line)
        self.assertIn("LEARNING", line["text"])

    def test_a_lite_build_says_the_picture_is_not_turned_or_cut(self):
        put_cam("Cam")
        line = self.line("Cam", picture={"prep": "off", "note": ff.note()})
        self.assertIn("no ffmpeg", line["text"])
        line = self.line("Cam", watching=True, state="RUNNING", camera="unprepared")
        self.assertIn("could not be prepared", line["text"])

    def test_the_printer_line_reaches_the_5in_panel_fields(self):
        put_cam("Cam")
        self.line("Cam", learning="camera view changed")
        self.assertEqual(server._detection_line_ex("Cam")["learning"], "camera view changed")


# =================================================================== the editor (headless Chrome)

def _run_page(testcase, driver, query="", size="1024,600", budget_ms=60000, frame_width=None):
    """frame_width: show the page inside an iframe that wide (headless Chrome will not make a window narrower than ~500 px)."""
    import test_fix_dashboard as tfd
    if not tfd.CHROME:
        testcase.skipTest("no Chrome / Edge found for the browser tests")
    d = tempfile.mkdtemp(prefix="orient-chrome-")
    testcase.addCleanup(shutil.rmtree, d, True)
    page = os.path.join(d, "page.html")
    with open(page, "w", encoding="utf8") as fh:
        if frame_width:
            import html as _html
            fh.write('<!doctype html><body style="margin:0"><iframe id="f" style="width:%dpx;height:800px;border:0" srcdoc="%s"></iframe>'
                     '<script>setInterval(function(){try{var v=f.contentDocument.documentElement.getAttribute("data-result");'
                     'if(v)document.documentElement.setAttribute("data-result",v);}catch(e){}},200)</script></body>'
                     % (frame_width, _html.escape(tfd.build_page(driver), quote=True)))
        else:
            fh.write(tfd.build_page(driver))
    cmd = [tfd.CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
           "--user-data-dir=" + os.path.join(d, "profile"), f"--window-size={size}", f"--virtual-time-budget={budget_ms}",
           "--dump-dom", "file://" + page + query]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    import re
    m = re.search(r"data-result='(.*?)'", r.stdout, re.S) or re.search(r'data-result="(.*?)"', r.stdout, re.S)
    testcase.assertIsNotNone(m, "the driver did not finish: " + r.stderr[-500:] + r.stdout[-300:])
    txt = m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    return json.loads(txt)


EDITOR_DRIVER = r"""
const SCHEMA = %s;
const prn = {name: 'Core One', type: 'prusalink', type_label: 'PrusaLink', address: 'http://10.0.0.5', model: '', driver_available: true, loaded: true,
  secrets: {password: 'set'}, values: {name: 'Core One', url: '10.0.0.5', username: 'maker', storage: '', model: ''}, groups: [], camera_url: '',
  detect_area: null, camera_orientation: {flip_h: false, flip_v: true, rotate90: false}};
// a picture made by the browser itself: white, with a black TOP-LEFT quarter (the camera is upside down, so it must be drawn at the bottom-left)
const src = document.createElement('canvas'); src.width = 80; src.height = 60;
const sx = src.getContext('2d'); sx.fillStyle = '#fff'; sx.fillRect(0, 0, 80, 60); sx.fillStyle = '#000'; sx.fillRect(0, 0, 40, 30);
const blob = await new Promise(r => src.toBlob(r, 'image/jpeg', 0.95));
window.__stubs.push((u, m, init) => {
  if(u === '/api/fleet') return J([]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: []});
  if(u === '/api/groups') return J([]);
  if(u === '/api/printers') return J({printers: [prn]});
  if(u.startsWith('/api/printer-types')) return J(SCHEMA);
  if(/\/snapshot$/.test(u)) return new Response(blob, {status: 200, headers: {'Content-Type': 'image/jpeg'}});
  if(/^\/api\/printers\/[^/]+$/.test(u) && m === 'PUT') return J({printer: prn, changed: ['detect_area']});
  return null;
});
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
const ptr = (el, type, fx, fy) => { const r = el.getBoundingClientRect();
  el.dispatchEvent(new PointerEvent(type, {bubbles: true, pointerId: 1, button: 0, clientX: r.left + r.width * fx, clientY: r.top + r.height * fy})); };
R.kiosk = KIOSK;
await openPrinters(); await wait(() => document.querySelector('.pmrow'));
await pmEdit('Core One'); await wait(() => $('pm-save'));
R.present = !!$('dw-field'); R.panelCount = document.querySelectorAll('#dw-panel, #dw-stage, #dw-cv').length;
R.insideMore = !!(document.querySelector('#pm-more-body #dw-field'));
if(R.present){
  R.state0 = $('dw-state').textContent; R.resetDisabled0 = $('dw-reset').disabled; R.save0 = $('pm-save').textContent;
  R.panelHidden0 = $('dw-panel').hidden;
  if($('pm-more-body').hidden) pmMoreToggle();
  $('dw-open').click(); await wait(() => DW.loaded); R.loaded = DW.loaded; R.panelHidden1 = $('dw-panel').hidden;
  // the picture is drawn upright: the black quarter of an upside-down camera is at the BOTTOM-left
  const cv = $('dw-cv'), g = cv.getContext('2d');
  const lum = (fx, fy) => { const d = g.getImageData(Math.floor(cv.width * fx), Math.floor(cv.height * fy), 1, 1).data; return (d[0] + d[1] + d[2]) / 3; };
  R.canvasTopLeftDark = lum(.2, .2) < 100; R.canvasBottomLeftDark = lum(.2, .8) < 100;
  // targets are at least 44 px
  const h = id => Math.round($(id).getBoundingClientRect().height), w = id => Math.round($(id).getBoundingClientRect().width);
  R.targets = {open: [w('dw-open'), h('dw-open')], reset: [w('dw-reset'), h('dw-reset')], x: h('dw-x'), y: h('dw-y'), w: h('dw-w'), h: h('dw-h')};
  R.viewport = window.innerWidth; R.noSideScroll = document.documentElement.scrollWidth <= window.innerWidth + 1 && $('dw-field').scrollWidth <= $('dw-field').clientWidth + 1;
  // drag a box from (25%%, 25%%) to (75%%, 75%%)
  const st = $('dw-stage'); ptr(st, 'pointerdown', .25, .25); ptr(st, 'pointermove', .5, .5); ptr(st, 'pointermove', .75, .75); ptr(st, 'pointerup', .75, .75);
  R.area = DW.area; R.state1 = $('dw-state').textContent; R.boxHidden = $('dw-box').hidden; R.boxLabel = $('dw-box').textContent;
  R.nums = ['x', 'y', 'w', 'h'].map(k => $('dw-' + k).value);
  R.save1 = {label: $('pm-save').textContent, disabled: $('pm-save').disabled};
  // a box that is too small is refused in words and the old one stays
  ptr(st, 'pointerdown', .1, .1); ptr(st, 'pointermove', .12, .12); ptr(st, 'pointerup', .12, .12);
  R.tooSmall = {msg: $('dw-msg').textContent, area: DW.area};
  // type a number that runs past the edge
  $('dw-w').value = '90'; $('dw-w').dispatchEvent(new Event('change')); R.pastEdge = {msg: $('dw-msg').textContent, w: DW.area.w};
  $('dw-w').value = '40'; $('dw-w').dispatchEvent(new Event('change')); R.typed = DW.area;
  // save: the area goes with the other fields, no printer test needed
  $('pm-save').click(); await wait(() => window.__calls.some(c => c.startsWith('PUT /api/printers/')));
  R.put = JSON.parse(window.__calls.filter(c => c.startsWith('PUT /api/printers/')).pop().split(' ').slice(2).join(' ')).values.detect_area;
  // back in the editor: Reset clears it
  await wait(() => PM.view === 'list'); await sleep(200);
  prn.detect_area = {x: 25, y: 25, w: 50, h: 50};
  await openPrinters(); await wait(() => document.querySelector('.pmrow'));
  await pmEdit('Core One'); await wait(() => $('pm-save'));
  R.moreOpenWithArea = !$('pm-more-body').hidden; R.state2 = $('dw-state').textContent;
  $('dw-reset').click(); R.afterReset = {state: $('dw-state').textContent, save: $('pm-save').textContent, area: DW.area};
  $('dw-reset').click();
}
"""


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed")
class EditorTests(unittest.TestCase):
    def driver(self):
        import test_ui_round2 as t2
        return EDITOR_DRIVER % t2._schema()

    def test_static_guards(self):
        html = open(os.path.join(APP, "dashboard.html"), encoding="utf8").read()
        self.assertIn("if(KIOSK || !existing) return '';", html)         # not on a wall screen, not while adding
        self.assertIn("...DW_ACTIONS", html)
        self.assertIn("values.detect_area = DW.area", html)
        self.assertIn("all.push('detect_area')", html)
        self.assertIn("const PM_NONCONN = ['name', 'model', 'camera_url']", html)       # a watch-area change never needs a printer test
        self.assertIn("WATCHED", html)                                    # the box says so in words, not only by its look
        self.assertNotRegex(html, r"https?://[^'\"\s]*snapshot")

    def test_desktop_has_the_box_editor_and_it_works(self):
        R = _run_page(self, self.driver())
        self.assertNotIn("error", R, R.get("error"))
        self.assertFalse(R["kiosk"])
        self.assertTrue(R["present"])
        self.assertTrue(R["insideMore"], "it lives under More options")
        self.assertEqual(R["state0"], "Watching the whole picture")
        self.assertTrue(R["resetDisabled0"])
        self.assertEqual(R["save0"], "No changes")
        self.assertTrue(R["panelHidden0"])
        self.assertTrue(R["loaded"])
        self.assertFalse(R["panelHidden1"])
        self.assertFalse(R["canvasTopLeftDark"])
        self.assertTrue(R["canvasBottomLeftDark"], "drawn upright: an upside-down camera's top-left is at the bottom-left")
        for k in ("open", "reset"):
            self.assertGreaterEqual(R["targets"][k][0], 44, k)
            self.assertGreaterEqual(R["targets"][k][1], 44, k)
        for k in ("x", "y", "w", "h"):
            self.assertGreaterEqual(R["targets"][k], 44, k)
        self.assertTrue(R["noSideScroll"])
        a = R["area"]
        for k, want in (("x", 25), ("y", 25), ("w", 50), ("h", 50)):
            self.assertAlmostEqual(a[k], want, delta=1.5, msg=k)
        self.assertIn("Watching", R["state1"])
        self.assertIn("wide", R["state1"])
        self.assertFalse(R["boxHidden"])
        self.assertEqual(R["boxLabel"], "WATCHED")
        self.assertEqual(R["save1"], {"label": "Save changes", "disabled": False})
        self.assertIn("too small", R["tooSmall"]["msg"])
        self.assertAlmostEqual(R["tooSmall"]["area"]["w"], a["w"], delta=0.01)
        self.assertIn("past the edge", R["pastEdge"]["msg"])
        self.assertAlmostEqual(R["typed"]["w"], 40, delta=0.01)
        self.assertAlmostEqual(R["put"]["w"], 40, delta=0.01)
        self.assertTrue(R["moreOpenWithArea"], "More options opens by itself when an area is set")
        self.assertIn("Watching 50% wide and 50% high", R["state2"])
        self.assertEqual(R["afterReset"], {"state": "Watching the whole picture", "save": "Save changes", "area": None})

    def test_phone_width_has_it_too_and_fits(self):
        R = _run_page(self, self.driver(), size="500,860", frame_width=390)
        self.assertNotIn("error", R, R.get("error"))
        self.assertTrue(R["present"])
        self.assertEqual(R["viewport"], 390)
        self.assertTrue(R["noSideScroll"], "no sideways scroll at 390 px")
        self.assertGreaterEqual(R["targets"]["open"][1], 44)
        self.assertGreaterEqual(R["targets"]["w"], 44)

    def test_kiosk_has_no_box_editor(self):
        R = _run_page(self, self.driver(), query="?kiosk=1")
        self.assertNotIn("error", R, R.get("error"))
        self.assertTrue(R["kiosk"])
        self.assertFalse(R["present"])
        self.assertEqual(R["panelCount"], 0)
        self.assertFalse(R["insideMore"])

    def test_the_5in_layout_is_a_kiosk_too(self):
        R = _run_page(self, self.driver(), query="?layout=5in")
        self.assertNotIn("error", R, R.get("error"))
        self.assertTrue(R["kiosk"])
        self.assertFalse(R["present"])


if __name__ == "__main__":
    unittest.main()
