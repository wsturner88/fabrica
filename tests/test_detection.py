"""Tests for app/detection.py - the Hub's side of spaghetti detection.

    python3 tests/test_detection.py

Covers: FailureTracker maths against hand-computed sequences, baseline persistence, the
DetectorClient against fake servers (and against the real adapter + a fake ml_api, which proves
the Split token flow end to end), and the DetectionLoop with fake fleet / camera / detector.

Everything is loopback or in-memory; no printer, no camera and no real detector is contacted.
detection.py imports nothing from server.py, so no config or cache path needs to be set.
"""
import http.server
import json
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import logging  # noqa: E402

import detection as det  # noqa: E402
from test_adapter import FakeMlApi, make_adapter, TOKEN, JPEG  # noqa: E402

logging.getLogger("bambu.detection").setLevel(logging.CRITICAL)   # expected warnings, keep output clean

BIG = JPEG * 40                     # comfortably above the "blank frame" size heuristic


def tmpdir(test):
    d = tempfile.mkdtemp(prefix="det-test-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


# =================================================================== parameters

class ParamsTests(unittest.TestCase):
    def test_defaults_are_the_documented_ones(self):
        p = det.Params()
        self.assertEqual((p.ewm_span, p.short_window, p.long_window, p.warmup_frames), (12, 310, 7200, 30))
        self.assertEqual((p.threshold_low, p.threshold_high, p.short_factor, p.failure_factor),
                         (0.38, 0.78, 3.8, 1.75))

    def test_sensitivity_knob(self):
        self.assertEqual([det.sensitivity_factor(x) for x in ("low", "medium", "high", "HIGH ", "normal")],
                         [0.5, 1.0, 2.0, 2.0, 1.0])
        self.assertEqual(det.sensitivity_factor("nonsense"), 1.0)
        self.assertEqual(det.sensitivity_factor(0.001), 0.25)
        self.assertEqual(det.sensitivity_factor(99), 4.0)
        self.assertEqual(det.sensitivity_factor(None), 1.0)

    def test_for_interval_scaling(self):
        self.assertEqual(det.Params.for_interval(10), det.Params())
        p30 = det.Params.for_interval(30, "high")
        self.assertEqual((p30.ewm_span, p30.warmup_frames, p30.short_window, p30.sensitivity), (4, 10, 103, 2.0))
        self.assertEqual(det.Params.for_interval(30, scale=False).ewm_span, 12)
        self.assertEqual(det.Params.for_interval(10, warmup_frames=5).warmup_frames, 5)


# =================================================================== tracker maths

def P(**kw):
    base = dict(short_window=100, long_window=400, warmup_frames=0)
    base.update(kw)
    return det.Params(**base)


class TrackerMath(unittest.TestCase):
    def test_ewm_hand_computed(self):
        # alpha = 2/13. Scores 0, 0, 2 -> ewm 0, 0, 4/13.
        t = det.FailureTracker("x", det.Params())
        self.assertEqual(t.observe(0).ewm, 0.0)
        self.assertEqual(t.observe(0).ewm, 0.0)
        self.assertAlmostEqual(t.observe(2).ewm, 4 / 13, places=9)
        # next: 2*(2/13) + (4/13)*(11/13) = 4/13 + 44/169 = 96/169
        self.assertAlmostEqual(t.observe(2).ewm, 96 / 169, places=9)

    def test_first_frame_seeds_the_ewm(self):
        v = det.FailureTracker("x", det.Params()).observe(0.5)
        self.assertEqual((v.ewm, v.short_mean, v.long_mean, v.frame), (0.5, 0.5, 0.5, 1))

    def test_means(self):
        t = det.FailureTracker("x", P(short_window=3, long_window=5))
        vals = [1, 2, 3, 4, 5, 6, 7]
        for x in vals:
            v = t.observe(x)
        self.assertAlmostEqual(v.short_mean, (5 + 6 + 7) / 3)
        self.assertAlmostEqual(v.long_mean, (3 + 4 + 5 + 6 + 7) / 5)

    def test_garbage_scores_are_zero(self):
        t = det.FailureTracker("x", P())
        for bad in (None, "abc", float("nan"), float("inf"), -3):
            self.assertEqual(t.observe(bad).score, 0.0)

    def test_constant_signal_is_baseline_not_failure(self):
        # A printer that ALWAYS scores 2.0 (a bed clip that looks like spaghetti) must settle to OK.
        t = det.FailureTracker("x", P())
        for _ in range(50):
            v = t.observe(2.0)
        self.assertEqual((v.level, v.adjusted), (det.LEVEL_OK, 0.0))

    def test_ok_warning_failure_progression(self):
        # 200 quiet frames, then a spike of 1.0s. Hand-computed per frame k (alpha = 2/13):
        #   ewm_k = 1 - (11/13)^k ; short = k/100 ; long = k/(200+k) ; adjusted = ewm - long
        #   k=3: 0.3942-0.0148 = 0.3794 < 0.38           -> OK (just)
        #   k=4: 0.4874-0.0196 = 0.4678 in band, > (0.04-0.0196)*3.8 = 0.0775 -> WARNING
        #        but 0.4678/1.75 = 0.267 < 0.38 -> not FAILURE
        #   k=7: 0.6894-0.0338 = 0.6556 ; /1.75 = 0.3746 < 0.38 -> still WARNING
        #   k=8: 0.7372-0.0385 = 0.6988 ; /1.75 = 0.3993 in band, > (0.08-0.0385)*3.8 = 0.158 -> FAILURE
        t = det.FailureTracker("x", P())
        for _ in range(200):
            t.observe(0)
        levels = []
        for k in range(1, 10):
            v = t.observe(1.0)
            levels.append(v.level)
            if k == 4:
                self.assertAlmostEqual(v.ewm, 13920 / 28561, places=6)
                self.assertAlmostEqual(v.adjusted, 0.4678, places=3)
                self.assertAlmostEqual(v.short_mean, 0.04, places=9)
                self.assertAlmostEqual(v.long_mean, 4 / 204, places=9)
        O, W, F = det.LEVEL_OK, det.LEVEL_WARNING, det.LEVEL_FAILURE
        # flagged pictures no longer feed the long baseline (review P2), so FAILURE is reached one picture sooner
        self.assertEqual(levels, [O, O, O, W, W, W, F, F, F])

    def test_high_threshold_branch(self):
        # One huge frame after a quiet baseline: adjusted well above 0.78 -> failing outright;
        # /1.75 still above 0.78 -> FAILURE immediately.
        t = det.FailureTracker("x", P())
        for _ in range(200):
            t.observe(0)
        self.assertEqual(t.observe(20.0).level, det.LEVEL_FAILURE)   # ewm 3.08, adj 3.07 -> 1.75 -> 1.75 > 0.78

    def test_between_thresholds_is_ok_when_short_mean_explains_it(self):
        # In the band, failing needs adjusted > (short - long) * 3.8. When the whole recent
        # past is already elevated, short - long is large and the same ewm is NOT failing.
        t = det.FailureTracker("x", P(short_window=20, long_window=400))
        for _ in range(380):
            t.observe(0)
        for _ in range(20):
            v = t.observe(0.6)
        # ewm ~ 0.6*(1-(11/13)^20)=0.58, long ~ 20*0.6/400 = 0.03 -> adjusted 0.55 in the band;
        # short - long = 0.6 - 0.03 = 0.57 -> *3.8 = 2.17 > 0.55 -> not failing.
        self.assertTrue(0.38 < v.adjusted < 0.78)
        self.assertEqual(v.level, det.LEVEL_OK)

    def test_sensitivity_scales_the_decision(self):
        def run(sens):
            t = det.FailureTracker("x", P(sensitivity=det.sensitivity_factor(sens)))
            for _ in range(200):
                t.observe(0)
            return [t.observe(1.0).level for _ in range(4)]
        O, W, F = det.LEVEL_OK, det.LEVEL_WARNING, det.LEVEL_FAILURE
        self.assertEqual(run("medium"), [O, O, O, W])
        self.assertEqual(run("low"), [O, O, O, O])            # k=4: 0.4678*0.5 = 0.234 < 0.38
        # high: k=2: (0.284-0.0099)*2 = 0.548 in band, > 0.0384 -> WARNING (0.548/1.75 = 0.313 < 0.38)
        #       k=3: (0.3942-0.0148)*2 = 0.759 ; /1.75 = 0.434 in band, > 0.058 -> FAILURE
        self.assertEqual(run("high"), [O, W, F, F])

    def test_failure_implies_warning_never_the_reverse(self):
        t = det.FailureTracker("x", P())
        rank = {det.LEVEL_OK: 0, det.LEVEL_WARNING: 1, det.LEVEL_FAILURE: 2}
        import random
        rnd = random.Random(7)
        for _ in range(300):
            t.observe(rnd.choice([0, 0, 0, 0.2, 0.9, 2.5]))
            v = t.last
            if v.level == det.LEVEL_FAILURE:
                self.assertTrue(t._failing(v.ewm, v.short_mean, v.long_mean, 1.0))
            self.assertIn(rank[v.level], (0, 1, 2))

    def test_gauge_bands(self):
        t = det.FailureTracker("x", P())
        for _ in range(200):
            t.observe(0)
        self.assertLess(t.observe(0).gauge, 1 / 3)
        t2 = det.FailureTracker("x", P())
        for _ in range(200):
            t2.observe(0)
        g = [t2.observe(1.0).gauge for _ in range(9)]
        self.assertTrue(all(x < 1 / 3 for x in g[:3]))
        self.assertTrue(all(1 / 3 <= x < 2 / 3 for x in g[3:6]))
        self.assertTrue(all(x > 2 / 3 for x in g[6:]))

    def test_verdict_to_dict_has_text_label(self):
        v = det.FailureTracker("x", det.Params(warmup_frames=3)).observe(1.0)
        d = v.to_dict()
        self.assertEqual(d["label"], "WARMING UP")
        json.dumps(d)


class Warmup(unittest.TestCase):
    def test_warmup_frames_never_alert_then_it_can(self):
        # Previous print: 100 quiet frames. New print: constant 3.0.
        # Frames 1-30 are warm-up -> forced OK even though the signal is huge.
        # Frame 31: ewm 3.0, long = 93/131 = 0.7099, adjusted 2.2901 > 0.78, and /1.75 = 1.3086 > 0.78 -> FAILURE.
        t = det.FailureTracker("x", det.Params())
        for _ in range(100):
            t.observe(0)
        t.start_print()
        for i in range(1, 31):
            v = t.observe(3.0)
            self.assertEqual((v.level, v.warmup, v.frame), (det.LEVEL_OK, True, i))
        v = t.observe(3.0)
        self.assertEqual((v.level, v.warmup, v.frame), (det.LEVEL_FAILURE, False, 31))
        self.assertAlmostEqual(v.long_mean, 93 / 131, places=9)
        self.assertAlmostEqual(v.adjusted, 3 - 93 / 131, places=9)

    def test_warmup_still_feeds_the_baseline(self):
        t = det.FailureTracker("x", det.Params(warmup_frames=5))
        for _ in range(5):
            t.observe(1.0)
        self.assertEqual(t.baseline_frames, 5)


class NewPrintReset(unittest.TestCase):
    def test_reset_clears_print_state_but_keeps_baseline(self):
        t = det.FailureTracker("x", det.Params())
        for _ in range(60):
            t.observe(0.5)
        self.assertEqual(t.frame, 60)
        t.start_print()
        self.assertEqual((t.frame, t.baseline_frames, t.last), (0, 60, None))
        v = t.observe(0.1)
        self.assertEqual((v.frame, v.ewm, v.short_mean, v.warmup), (1, 0.1, 0.1, True))   # ewm re-seeded
        self.assertAlmostEqual(v.long_mean, (60 * 0.5 + 0.1) / 61, places=9)


# =================================================================== persistence

class Persistence(unittest.TestCase):
    def test_round_trip_in_data_detection_layout(self):
        d = tmpdir(self)
        store = os.path.join(d, "detection")                    # i.e. /data/detection
        a = det.FailureTracker("P1S", det.Params(), store)
        for x in (0.25, 0.5, 0.75, 1.0):
            a.observe(x)
        a.flush()
        self.assertTrue(os.path.isfile(os.path.join(store, "P1S.json")))
        self.assertEqual(os.listdir(store), ["P1S.json"])        # no stray temp file
        b = det.FailureTracker("P1S", det.Params(), store)       # a Hub restart
        self.assertEqual(b.baseline_frames, 4)
        v = b.observe(0.0)
        self.assertAlmostEqual(v.long_mean, (0.25 + 0.5 + 0.75 + 1.0 + 0.0) / 5, places=9)
        self.assertEqual(v.frame, 1)                              # per-print state starts fresh

    def test_start_print_saves(self):
        d = tmpdir(self)
        a = det.FailureTracker("A1", det.Params(), d)
        a.observe(0.4)
        a.start_print()
        self.assertEqual(det.FailureTracker("A1", det.Params(), d).baseline_frames, 1)

    def test_throttled_save_with_clock(self):
        d = tmpdir(self)
        now = [1000.0]
        t = det.FailureTracker("X", det.Params(), d, clock=lambda: now[0], save_every_s=60)
        t.observe(1.0)
        self.assertFalse(os.path.exists(t.path))                  # too soon after creation
        now[0] += 61
        t.observe(1.0)
        self.assertTrue(os.path.exists(t.path))
        m1 = os.path.getmtime(t.path)
        os.utime(t.path, (0, 0))
        now[0] += 10
        t.observe(1.0)
        self.assertEqual(os.path.getmtime(t.path), 0)              # not rewritten inside the window
        t.flush()
        self.assertGreater(os.path.getmtime(t.path), 0)

    def test_baseline_capped_at_long_window(self):
        d = tmpdir(self)
        a = det.FailureTracker("X", P(long_window=10), d)
        for i in range(25):
            a.observe(i % 3)
        a.flush()
        with open(a.path) as f:
            self.assertEqual(len(json.load(f)["long"]), 10)
        self.assertEqual(det.FailureTracker("X", P(long_window=4), d).baseline_frames, 4)

    def test_corrupt_or_hostile_file_is_ignored(self):
        d = tmpdir(self)
        for content in ("{not json", '{"long": "abc"}', '{"v":1}', "[]", '{"long":[1,"x",null,-2,NaN,0.5]}'):
            with open(os.path.join(d, "X.json"), "w") as f:
                f.write(content)
            t = det.FailureTracker("X", det.Params(), d)
            self.assertLessEqual(t.baseline_frames, 2, content)
            self.assertEqual(t.observe(0.5).frame, 1)

    def test_unwritable_store_never_raises(self):
        d = tmpdir(self)
        blocker = os.path.join(d, "file")
        open(blocker, "w").close()
        t = det.FailureTracker("X", det.Params(), os.path.join(blocker, "sub"))   # parent is a file
        t.observe(1.0)
        t.flush()                                                  # logs once, no exception

    def test_printer_names_cannot_escape_the_store(self):
        d = tmpdir(self)
        t = det.FailureTracker("../../etc/passwd", det.Params(), d)
        t.observe(1.0)
        t.flush()
        files = os.listdir(d)
        self.assertEqual(len(files), 1)
        self.assertNotIn("/", files[0])
        self.assertTrue(files[0].endswith(".json"))
        # different odd names do not collide
        a, b = det._store_name("P1 S"), det._store_name("P1_S")
        self.assertNotEqual(a, b)
        self.assertEqual(det._store_name("P1S"), "P1S")

    def test_no_store_dir_means_memory_only(self):
        t = det.FailureTracker("X", det.Params())
        t.observe(1.0)
        t.flush()
        self.assertIsNone(t.path)


# =================================================================== client

class FakeDetector:
    """A throwaway detector. `handler(self_http_handler, body) -> (status, obj|bytes, headers)`."""

    def __init__(self, handler):
        outer = self
        self.requests = []
        self.handler = handler

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": self.path, "body": body,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                status, obj, headers = outer.handler(self, body)
                data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


GOOD = {"api": 1, "score": 0.43, "detections": [{"label": "failure", "conf": 0.31, "box": [1, 2, 3, 4]},
                                                 {"label": "failure", "conf": 0.12, "box": [5, 6, 7, 8]}], "ms": 120}


class ClientTests(unittest.TestCase):
    def serve(self, handler):
        s = FakeDetector(handler)
        self.addCleanup(s.stop)
        return s

    def test_success_and_request_shape(self):
        s = self.serve(lambda h, b: (200, GOOD, None))
        r = det.DetectorClient(s.url + "/", "sekret").score(BIG, "My P1S!")
        self.assertTrue(r.ok)
        self.assertEqual((r.score, r.ms, len(r.detections)), (0.43, 120, 2))
        self.assertEqual(r.detections[0], {"conf": 0.31, "box": [1.0, 2.0, 3.0, 4.0]})
        self.assertIsInstance(r.rtt_ms, int)
        q = s.requests[0]
        self.assertEqual((q["method"], q["path"], q["body"]), ("POST", "/v1/score", BIG))
        self.assertEqual(q["headers"]["authorization"], "Bearer sekret")
        self.assertEqual(q["headers"]["content-type"], "image/jpeg")
        self.assertEqual(q["headers"]["x-printer"], "My P1S")        # sanitised, log-only

    def test_no_token_sends_no_auth_header(self):
        s = self.serve(lambda h, b: (200, GOOD, None))
        det.DetectorClient(s.url).score(BIG)
        self.assertNotIn("authorization", s.requests[0]["headers"])

    def test_accepts_the_longer_field_names_too(self):
        body = {"score": 0.2, "detections": [{"label": "failure", "confidence": 0.2, "box": [1, 2, 3, 4]}],
                "inference_ms": 99}
        s = self.serve(lambda h, b: (200, body, None))
        r = det.DetectorClient(s.url).score(BIG)
        self.assertEqual((r.ok, r.ms, r.detections[0]["conf"]), (True, 99, 0.2))

    def test_unauthorized(self):
        s = self.serve(lambda h, b: (401, {"error": "unauthorized", "message": "nope"}, None))
        r = det.DetectorClient(s.url, "bad").score(BIG)
        self.assertEqual((r.ok, r.code, r.soft), (False, "unauthorized", False))

    def test_busy_is_soft(self):
        s = self.serve(lambda h, b: (503, {"error": "busy"}, {"Retry-After": "1"}))
        r = det.DetectorClient(s.url).score(BIG)
        self.assertEqual((r.ok, r.code, r.soft), (False, "busy", True))

    def test_other_errors_carry_the_detectors_code(self):
        s = self.serve(lambda h, b: (502, {"error": "detector_unreachable", "message": "ml_api is not reachable"}, None))
        r = det.DetectorClient(s.url).score(BIG)
        self.assertEqual((r.ok, r.code, r.soft), (False, "detector_unreachable", False))
        self.assertIn("502", r.error)

    def test_unreachable(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()                                               # nothing listens there now
        r = det.DetectorClient(f"http://127.0.0.1:{port}", "tok").score(BIG)
        self.assertEqual((r.ok, r.code), (False, "unreachable"))
        h = det.DetectorClient(f"http://127.0.0.1:{port}").health()
        self.assertEqual((h["ok"], h["status"]), (False, "unreachable"))

    def test_timeout(self):
        s = self.serve(lambda h, b: (time.sleep(1.5), (200, GOOD, None))[1])
        t0 = time.monotonic()
        r = det.DetectorClient(s.url, timeout_s=0.4).score(BIG)
        self.assertEqual((r.ok, r.code), (False, "timeout"))
        self.assertLess(time.monotonic() - t0, 1.4)

    def test_bad_answers(self):
        for body in (b"<html>", b"[]", b'{"score": "x"}', b'{"score": -1}', b'{"detections": []}', b'{"score": NaN}'):
            s = self.serve(lambda h, b, body=body: (200, body, None))
            r = det.DetectorClient(s.url).score(BIG)
            self.assertEqual((r.ok, r.code), (False, "bad_response"), body)

    def test_newer_api_is_refused_politely(self):
        s = self.serve(lambda h, b: (200, dict(GOOD, api=2), None))
        self.assertEqual(det.DetectorClient(s.url).score(BIG).code, "api_mismatch")

    def test_redirects_are_not_followed(self):
        target = self.serve(lambda h, b: (200, GOOD, None))
        s = self.serve(lambda h, b: (302, b"", {"Location": target.url + "/v1/score"}))
        r = det.DetectorClient(s.url, "tok").score(BIG)
        self.assertFalse(r.ok)
        self.assertEqual(target.requests, [], "the bearer token must not follow a redirect")

    def test_misconfiguration_and_empty_input_never_raise(self):
        for url in ("", None, "ftp://x", "not a url", "127.0.0.1:8090"):
            r = det.DetectorClient(url, "t").score(BIG)
            self.assertEqual((r.ok, r.code), (False, "bad_config"), url)
            self.assertEqual(det.DetectorClient(url).health()["status"], "bad_config")
        s = self.serve(lambda h, b: (200, GOOD, None))
        self.assertEqual(det.DetectorClient(s.url).score(b"").code, "empty_frame")
        self.assertEqual(det.DetectorClient(s.url).score(None).code, "empty_frame")

    def test_token_is_scrubbed_from_errors(self):
        s = self.serve(lambda h, b: (500, {"message": "oops sekret-123 leaked"}, None))
        r = det.DetectorClient(s.url, "sekret-123").score(BIG)
        self.assertNotIn("sekret-123", r.error)
        self.assertNotIn("sekret-123", json.dumps(r.to_dict()))

    def test_one_request_in_flight(self):
        gate = threading.Event()
        s = self.serve(lambda h, b: (gate.wait(3), (200, GOOD, None))[1])
        c = det.DetectorClient(s.url, timeout_s=5)
        out = []
        t = threading.Thread(target=lambda: out.append(c.score(BIG)))
        t.start()
        time.sleep(0.3)
        second = c.score(BIG)
        gate.set()
        t.join()
        self.assertEqual((second.code, second.soft), ("busy", True))
        self.assertTrue(out[0].ok)

    def test_health_ok_and_degraded(self):
        s = self.serve(lambda h, b: (200, {"status": "ok", "ml_api": "ok", "arch": "arm64"}, None))
        h = det.DetectorClient(s.url).health()
        self.assertEqual((h["ok"], h["status"], h["info"]["arch"]), (True, "ok", "arm64"))
        s2 = self.serve(lambda h, b: (503, {"status": "degraded", "ml_api": "unreachable"}, None))
        h = det.DetectorClient(s2.url).health()
        self.assertEqual((h["ok"], h["status"]), (False, "degraded"))
        self.assertIn("ml_api unreachable", h["error"])

    def test_check_auth(self):
        # right token: the stub JPEG gets past auth and merely fails decoding -> counts as OK
        s = self.serve(lambda h, b: (502, {"error": "detector_fetch_failed"}, None))
        self.assertTrue(det.DetectorClient(s.url, "t").check_auth().ok)
        s2 = self.serve(lambda h, b: (401, {"error": "unauthorized"}, None))
        self.assertEqual(det.DetectorClient(s2.url, "t").check_auth().code, "unauthorized")
        self.assertEqual(det.DetectorClient("http://127.0.0.1:1", "t", timeout_s=1).check_auth().ok, False)


class SplitTokenFlow(unittest.TestCase):
    """Hub client -> the REAL adapter -> a fake ml_api. Proves the token flow end to end."""

    def setUp(self):
        self.ml = FakeMlApi()
        self.ad = make_adapter(self.ml)
        self.url = f"http://127.0.0.1:{self.ad.port}"

    def tearDown(self):
        self.ad.stop()
        self.ml.stop()

    def test_right_token_scores(self):
        c = det.DetectorClient(self.url, TOKEN)
        r = c.score(JPEG, "P1S")
        self.assertTrue(r.ok, r.error)
        self.assertAlmostEqual(r.score, 0.43, places=6)
        self.assertEqual(len(r.detections), 2)
        self.assertEqual(self.ml.calls[0]["fetches"], [(200, JPEG)])
        self.assertTrue(c.check_auth().ok)
        self.assertEqual(c.health()["info"]["ml_api"], "ok")

    def test_wrong_or_missing_token(self):
        self.assertEqual(det.DetectorClient(self.url, "wrong").score(JPEG).code, "unauthorized")
        self.assertEqual(det.DetectorClient(self.url, "").score(JPEG).code, "unauthorized")
        self.assertEqual(det.DetectorClient(self.url, "wrong").check_auth().code, "unauthorized")
        self.assertEqual(self.ml.calls, [])

    def test_ml_api_down_surfaces_as_detector_error(self):
        self.ml.stop()
        r = det.DetectorClient(self.url, TOKEN).score(JPEG)
        self.assertEqual((r.ok, r.code), (False, "detector_unreachable"))
        h = det.DetectorClient(self.url, TOKEN).health()
        self.assertEqual((h["ok"], h["status"]), (False, "degraded"))
        self.ml = FakeMlApi()


# =================================================================== loop

class FakeClient:
    """Scripted detector. `scores` is consumed one per call; `fail` makes score() fail."""

    def __init__(self, scores=None, default=0.0):
        self.scores = list(scores or [])
        self.default = default
        self.calls = []
        self.fail = None                         # a ScoreResult to return instead
        self.healthy = True
        self.health_calls = 0

    def score(self, jpeg, printer=""):
        self.calls.append(printer)
        if self.fail is not None:
            return self.fail
        s = self.scores.pop(0) if self.scores else self.default
        return det.ScoreResult(True, score=s, detections=[{"conf": s, "box": [1, 2, 3, 4]}] if s else [],
                               ms=100, rtt_ms=120)

    def health(self, timeout_s=5.0):
        self.health_calls += 1
        return {"ok": self.healthy, "status": "ok" if self.healthy else "unreachable", "info": {"arch": "arm64"},
                "error": "" if self.healthy else "the detector is not reachable"}


DOWN = det.ScoreResult(False, code="unreachable", error="the detector is not reachable (refused)")


class Rig:
    """Fleet + camera + detector fakes and a loop wired to them, driven by a fake clock."""

    def __init__(self, test, printers=None, client=None, **loop_kw):
        self.fleet = printers if printers is not None else {"P1S": run_snap()}
        self.client = client or FakeClient()
        self.alerts = []
        self.fetched = []
        self.cams = {n: True for n in self.fleet}
        self.frame_ts = 100.0
        self.fixed_ts = None
        self.jpeg = BIG
        self.now = 1000.0
        self.store = tmpdir(test)
        kw = dict(client=self.client, store_dir=self.store, has_camera=lambda n: self.cams.get(n, False))
        kw.update(loop_kw)
        self.loop = det.DetectionLoop(lambda: self.fleet, self._frame,
                                      lambda *a: self.alerts.append(a), clock=lambda: self.now, **kw)

    def _frame(self, name):
        self.fetched.append(name)
        if self.jpeg is None:
            return None
        if self.fixed_ts is not None:
            return self.jpeg, self.fixed_ts
        self.frame_ts += 1
        return self.jpeg, self.frame_ts

    def tick(self, advance=10.0):
        self.loop.tick(self.now)
        self.now += advance


def run_snap(state="RUNNING", job="benchy.3mf", percent=10, connected=True):
    return {"printer": "P1S", "state": state, "job": job, "percent": percent, "connected": connected}


class LoopPolling(unittest.TestCase):
    def test_polls_only_running_printers_with_a_camera(self):
        fleet = {"A": run_snap(), "B": run_snap(), "C": run_snap("IDLE"), "D": run_snap("PAUSE"),
                 "E": run_snap(connected=False), "F": run_snap("FINISH"), "G": run_snap("PREPARE"),
                 "H": run_snap("UNKNOWN", connected=False)}
        r = Rig(self, fleet)
        r.cams.update({"A": True, "B": False, "C": True, "D": True, "E": True, "F": True, "G": True, "H": True})
        r.tick()
        self.assertEqual(r.client.calls, ["A"])
        self.assertEqual(r.fetched, ["A"], "no camera frame may even be requested for the others")

    def test_interval_is_respected(self):
        r = Rig(self, interval_s=10)
        for t in (0, 3, 6, 9.9):
            r.now = 1000 + t
            r.loop.tick(r.now)
        self.assertEqual(len(r.client.calls), 1)
        r.now = 1010
        r.loop.tick(r.now)
        self.assertEqual(len(r.client.calls), 2)

    def test_status_change_starts_and_stops_polling(self):
        r = Rig(self, {"P1S": run_snap("IDLE")})
        r.tick()
        self.assertEqual(r.client.calls, [])
        r.fleet["P1S"] = run_snap("RUNNING")
        r.tick()
        self.assertEqual(len(r.client.calls), 1)
        r.fleet["P1S"] = run_snap("PAUSE")
        r.tick()
        r.tick()
        self.assertEqual(len(r.client.calls), 1)

    def test_off_when_no_client(self):
        r = Rig(self)
        r.loop.configure(client=None)
        r.tick()
        self.assertEqual((r.fetched, r.loop.enabled, r.loop.status()["mode"]), ([], False, "off"))
        self.assertEqual(r.loop.status()["summary"], "Detection is off.")

    def test_excluded_printer_and_vanished_printer(self):
        r = Rig(self, {"A": run_snap(), "B": run_snap()}, excluded=["b"])
        r.cams = {"A": True, "B": True}
        r.tick()
        self.assertEqual(r.client.calls, ["A"])
        self.assertTrue(r.loop.status()["printers"]["B"]["excluded"])
        del r.fleet["A"]
        r.tick()
        self.assertNotIn("A", r.loop.status()["printers"])

    def test_camera_problems_do_not_reach_the_detector(self):
        r = Rig(self)
        r.jpeg = None                                  # camera has no frame
        r.tick()
        self.assertEqual(r.client.calls, [])
        self.assertEqual(r.loop.status()["printers"]["P1S"]["camera"], "no_frame")
        r.jpeg = BIG
        r.fixed_ts = 500.0                             # camera stuck on one frame
        r.tick()
        r.tick()
        r.tick()
        self.assertEqual(len(r.client.calls), 1, "the same picture must be scored once, not every tick")
        self.assertEqual(r.loop.status()["printers"]["P1S"]["camera"], "stale")
        r.fixed_ts = None
        r.frame_ts = 1000.0                            # the camera moves on
        r.jpeg = b"\xff\xd8" + b"0" * 500               # tiny = black frame (light off)
        n = len(r.client.calls)
        r.tick()
        self.assertEqual(len(r.client.calls), n)
        self.assertEqual(r.loop.status()["printers"]["P1S"]["camera"], "blind")
        self.assertEqual(r.loop._ps["P1S"].tracker.baseline_frames, 1, "blind frames must not enter the baseline")

    def test_bare_bytes_from_the_camera_work_too(self):
        r = Rig(self)
        r.loop._jpeg = lambda n: BIG                     # no timestamp
        r.tick()
        r.tick()
        self.assertEqual(len(r.client.calls), 2)

    def test_busy_detector_retries_soon_without_backoff(self):
        r = Rig(self)
        r.client.fail = det.ScoreResult(False, code="busy", soft=True, error="busy")
        r.tick(advance=0)
        self.assertEqual(r.loop.status()["detector"]["state"], "unknown")
        r.now += 1.5
        r.client.fail = None
        r.tick()
        self.assertEqual(len(r.client.calls), 2)

    def test_exceptions_in_fakes_do_not_escape(self):
        r = Rig(self)
        r.loop._fleet = lambda: 1 / 0
        r.loop.tick(r.now)
        r.loop._fleet = lambda: r.fleet
        r.loop._jpeg = lambda n: 1 / 0
        r.loop.tick(r.now)
        self.assertEqual(r.loop.status()["printers"]["P1S"]["camera"], "no_frame")


class LoopBackoff(unittest.TestCase):
    def test_backs_off_exponentially_and_probes_before_spending_frames(self):
        r = Rig(self)
        r.client.fail = DOWN
        r.now = 1000
        r.loop.tick(r.now)                             # t=0   fail #1 -> wait 10
        self.assertEqual(len(r.client.calls), 1)
        for t in (3, 9.9):                              # inside the back-off: nothing at all
            r.now = 1000 + t
            r.loop.tick(r.now)
        self.assertEqual((len(r.client.calls), r.client.health_calls, len(r.fetched)), (1, 0, 1))
        r.client.healthy = False
        r.now = 1010
        r.loop.tick(r.now)                             # probe fails -> fail #2 -> wait 20, NO new frame
        self.assertEqual((len(r.client.calls), r.client.health_calls, len(r.fetched)), (1, 1, 1))
        r.now = 1029
        r.loop.tick(r.now)
        self.assertEqual(r.client.health_calls, 1)
        r.now = 1030
        r.loop.tick(r.now)                             # fail #3 -> wait 40
        self.assertEqual(r.client.health_calls, 2)
        d = r.loop.status()["detector"]
        self.assertEqual((d["state"], d["failures"], d["retry_in_s"]), ("down", 3, 40))
        r.now = 1070
        r.loop.tick(r.now)                             # fail #4 -> wait 80
        self.assertEqual(r.loop.status()["detector"]["retry_in_s"], 80)

    def test_backoff_is_capped(self):
        r = Rig(self, max_backoff_s=60)
        r.client.fail = DOWN
        r.client.healthy = False
        for _ in range(12):
            r.now = r.loop._down_until if r.loop._down_until > r.now else r.now
            r.loop.tick(r.now)
        self.assertEqual(r.loop.status()["detector"]["retry_in_s"], 60)

    def test_recovery(self):
        r = Rig(self)
        r.client.fail = DOWN
        r.loop.tick(1000.0)
        r.client.fail = None
        r.client.healthy = True
        r.now = 1010
        r.loop.tick(r.now)                             # probe ok -> scores again
        self.assertEqual(r.client.health_calls, 1)
        self.assertEqual(len(r.client.calls), 2)
        d = r.loop.status()["detector"]
        self.assertEqual((d["state"], d["failures"], d["error"]), ("up", 0, ""))
        self.assertEqual(r.loop._fails, 0)

    def test_a_dead_detector_is_never_reported_as_clear(self):
        r = Rig(self)
        r.tick()                                        # a good frame first: verdict ok
        r.client.fail = DOWN
        r.tick()
        st = r.loop.status()
        self.assertEqual(st["detector"]["state"], "down")
        self.assertTrue(st["printers"]["P1S"]["detector_down"])
        self.assertTrue(st["summary"].startswith("DETECTOR UNREACHABLE"))
        self.assertIn("NOT being watched", st["summary"])
        json.dumps(st)

    def test_bad_token_is_a_hard_failure_with_its_own_code(self):
        r = Rig(self)
        r.client.fail = det.ScoreResult(False, code="unauthorized", error="the detector rejected the token")
        r.tick()
        d = r.loop.status()["detector"]
        self.assertEqual((d["state"], d["code"]), ("down", "unauthorized"))


class LoopAlerts(unittest.TestCase):
    def test_new_print_warmup_then_failure_alert_with_cooldown(self):
        # Print 1: 100 quiet frames. Print 2: constant 3.0. Frames 1-30 are warm-up -> no alert.
        # Frame 31 is FAILURE (see Warmup.test_warmup_frames_never_alert_then_it_can).
        r = Rig(self, client=FakeClient(default=0.0))
        for _ in range(100):
            r.tick()
        self.assertEqual(r.alerts, [])
        r.fleet["P1S"] = run_snap("FINISH", percent=100)
        r.tick()
        r.fleet["P1S"] = run_snap("RUNNING", job="second.3mf", percent=1)
        r.client.default = 3.0
        for i in range(1, 31):
            r.tick()
            self.assertEqual(r.alerts, [], f"warm-up frame {i} must not alert")
        r.tick()                                         # frame 31
        self.assertEqual(len(r.alerts), 1)
        name, level, score, jpeg = r.alerts[0]
        self.assertEqual((name, level, score, jpeg), ("P1S", "failure", 1.0, BIG))
        for _ in range(59):                              # 590 s of continued failure: inside the 600 s cooldown
            r.tick()
        self.assertEqual(len(r.alerts), 1)
        for _ in range(2):
            r.tick()
        self.assertEqual(len(r.alerts), 2, "a continuing failure re-alerts once the cooldown has passed")

    def test_warning_then_failure_escalates_immediately(self):
        # 200 quiet frames (print 1), then print 2: 30 quiet (warm-up), 3x 1.0, then 3.0s.
        # Hand/scratch-checked: warning first appears at the 3rd spike frame; with 3.0s,
        # failure first appears at the 3rd (adjusted 1.3705 / 1.75 = 0.7831 > 0.78).
        r = Rig(self, client=FakeClient(default=0.0))
        for _ in range(200):
            r.tick()
        r.fleet["P1S"] = run_snap("FINISH", percent=100)
        r.tick()
        r.fleet["P1S"] = run_snap("RUNNING", job="b.3mf", percent=1)
        for _ in range(30):
            r.tick()
        r.client.default = 1.0
        r.tick(); r.tick()
        self.assertEqual(r.alerts, [])
        r.tick()
        self.assertEqual([a[1] for a in r.alerts], ["warning"])
        self.assertAlmostEqual(r.alerts[0][2], 0.394, places=3)
        r.client.default = 3.0
        r.tick(); r.tick()
        self.assertEqual([a[1] for a in r.alerts], ["warning"], "still warning at spike frames 1-2 of 3.0")
        r.tick()
        self.assertEqual([a[1] for a in r.alerts], ["warning", "failure"], "escalation is not held back by the cooldown")
        r.tick(); r.tick()
        self.assertEqual(len(r.alerts), 2)

    def test_learn_only_scores_and_logs_but_never_alerts(self):
        r = Rig(self, client=FakeClient(default=0.0), learn_only=True)
        for _ in range(60):
            r.tick()
        r.fleet["P1S"] = run_snap("FINISH", percent=100)
        r.tick()
        r.fleet["P1S"] = run_snap("RUNNING", job="b.3mf", percent=1)
        r.client.default = 3.0
        for _ in range(45):
            r.tick()
        self.assertEqual(r.alerts, [])
        st = r.loop.status()
        self.assertEqual(st["mode"], "learn")
        self.assertEqual(st["printers"]["P1S"]["verdict"]["level"], "failure")      # tracked, just not sent
        self.assertIn("Learn-only", st["summary"])
        log = os.path.join(r.store, "scores.jsonl")
        with open(log) as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual(len(lines), 60 + 45)
        self.assertEqual(set(lines[0]), {"t", "printer", "frame", "score", "ewm", "long", "level", "warmup"})
        # switching learn-only off takes effect at once
        r.loop.configure(learn_only=False)
        r.tick()
        self.assertEqual(len(r.alerts), 1)

    def test_hook_exceptions_are_contained(self):
        r = Rig(self, client=FakeClient(default=0.0), cooldown_s=0)
        r.loop._on_alert = lambda *a: 1 / 0
        for _ in range(60):
            r.tick()
        r.fleet["P1S"] = run_snap("FINISH", percent=100)
        r.tick()
        r.fleet["P1S"] = run_snap("RUNNING", job="x", percent=1)
        r.client.default = 3.0
        for _ in range(40):
            r.tick()                                    # must not raise
        self.assertGreater(r.loop.status()["printers"]["P1S"]["frames_scored"], 30)

    def test_never_pauses_or_stops_anything(self):
        st = Rig(self).loop.status()
        self.assertIs(st["auto_pause"], False)
        for name in dir(det.DetectionLoop):
            self.assertNotIn("pause_print", name)
            self.assertNotIn("stop_print", name)


class LoopPrintBoundaries(unittest.TestCase):
    def test_pause_resume_does_not_reset_but_new_job_does(self):
        r = Rig(self)
        for _ in range(5):
            r.tick()
        tr = r.loop._ps["P1S"].tracker
        self.assertEqual(tr.frame, 5)
        r.fleet["P1S"] = run_snap("PAUSE", percent=20)
        r.tick()
        r.fleet["P1S"] = run_snap("RUNNING", percent=21)
        r.tick()
        self.assertEqual(tr.frame, 6)                    # resumed: continues
        r.fleet["P1S"] = run_snap("RUNNING", job="other.3mf", percent=21)
        r.tick()
        self.assertEqual(tr.frame, 1)                    # a different job without any idle in between
        r.fleet["P1S"] = run_snap("RUNNING", job="other.3mf", percent=2)
        r.tick()
        self.assertEqual(tr.frame, 1)                    # percent fell by >10: the same name started again
        # Offline blips mid-print are not a new print.
        r.tick()
        r.fleet["P1S"] = run_snap("UNKNOWN", connected=False)
        r.tick()
        r.fleet["P1S"] = run_snap("RUNNING", job="other.3mf", percent=3)
        r.tick()
        self.assertEqual(tr.frame, 3)

    def test_hub_restart_mid_print_gets_a_fresh_warmup_but_keeps_the_baseline(self):
        r = Rig(self, client=FakeClient(default=0.2))
        for _ in range(40):
            r.tick()
        r.loop.stop()                                    # flushes the baseline
        r2 = Rig(self, client=FakeClient(default=0.2))
        r2.store = r.store
        r2.loop = det.DetectionLoop(lambda: r2.fleet, r2._frame, lambda *a: r2.alerts.append(a),
                                    client=r2.client, store_dir=r.store, clock=lambda: r2.now)
        r2.tick()
        ps = r2.loop._ps["P1S"]
        self.assertEqual((ps.tracker.frame, ps.tracker.baseline_frames), (1, 41))
        self.assertTrue(ps.verdict["warmup"])

    def test_print_end_flushes_baseline_to_disk(self):
        r = Rig(self)
        for _ in range(5):
            r.tick()
        self.assertFalse(os.path.exists(os.path.join(r.store, "P1S.json")))
        r.fleet["P1S"] = run_snap("FINISH", percent=100)
        r.tick()
        with open(os.path.join(r.store, "P1S.json")) as f:
            self.assertEqual(len(json.load(f)["long"]), 5)


class LoopConfigAndStatus(unittest.TestCase):
    def test_configure_applies_to_running_trackers(self):
        r = Rig(self)
        r.tick()
        tr = r.loop._ps["P1S"].tracker
        self.assertEqual((tr.params.sensitivity, tr.params.ewm_span), (1.0, 12))
        r.loop.configure(sensitivity="high", interval_s=30)
        self.assertEqual((tr.params.sensitivity, tr.params.ewm_span, tr.params.warmup_frames), (2.0, 4, 10))
        self.assertEqual(r.loop.interval_s, 30.0)
        r.loop.configure(interval_s="junk", cooldown_s=-5)
        self.assertEqual((r.loop.interval_s, r.loop.cooldown_s), (30.0, 0.0))

    def test_status_is_json_safe_and_plain_worded(self):
        r = Rig(self, client=FakeClient(default=0.1))
        for _ in range(3):
            r.tick()
        st = r.loop.status()
        json.dumps(st)
        p = st["printers"]["P1S"]
        self.assertEqual((st["enabled"], st["mode"], p["watching"], p["frames_scored"]), (True, "alert", True, 3))
        self.assertEqual(p["verdict"]["label"], "WARMING UP")
        self.assertEqual(st["summary"], "Watching 1 running print.")
        r.fleet["P1S"] = run_snap("FINISH", percent=100)
        r.tick()
        self.assertEqual(r.loop.status()["summary"], "No print is running.")

    def test_score_once_leaves_the_tracker_alone(self):
        r = Rig(self, client=FakeClient(scores=[0.31]))
        out = r.loop.score_once("P1S")
        self.assertTrue(out["ok"])
        self.assertEqual((out["score"], out["inference_ms"]), (0.31, 100))
        self.assertEqual(r.loop.status()["printers"], {})        # never observed by the loop
        r.cams["P1S"] = False
        self.assertFalse(r.loop.score_once("P1S")["ok"])
        r.cams["P1S"] = True
        r.jpeg = None
        self.assertIn("no camera picture", r.loop.score_once("P1S")["error"])
        r.jpeg = BIG
        r.client.fail = DOWN
        self.assertEqual(r.loop.score_once("P1S")["code"], "unreachable")
        r.loop.configure(client=None)
        self.assertEqual(r.loop.score_once("P1S")["error"], "detection is off")

    def test_thread_start_and_stop(self):
        r = Rig(self, tick_s=0.01)
        r.loop._clock = time.time
        r.loop.start()
        r.loop.start()                                    # idempotent
        deadline = time.time() + 5
        while not r.client.calls and time.time() < deadline:
            time.sleep(0.02)
        r.loop.stop()
        self.assertTrue(r.client.calls)
        self.assertFalse(r.loop._thread.is_alive())

    def test_full_stack_loop_to_real_adapter(self):
        ml = FakeMlApi()
        ad = make_adapter(ml)
        try:
            client = det.DetectorClient(f"http://127.0.0.1:{ad.port}", TOKEN)
            r = Rig(self, client=client)
            r.jpeg = JPEG * 10
            r.tick()
            v = r.loop.status()["printers"]["P1S"]["verdict"]
            self.assertAlmostEqual(v["score"], 0.43, places=3)
            self.assertEqual(r.loop.status()["detector"]["state"], "up")
            self.assertEqual(ml.calls[0]["fetches"][0][1], JPEG * 10)
        finally:
            ad.stop()
            ml.stop()


if __name__ == "__main__":
    unittest.main()
