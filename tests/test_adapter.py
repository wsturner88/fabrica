"""Tests for detector/adapter/app.py - the MIT adapter that sits in front of Obico's ml_api.

    python3 tests/test_adapter.py

Everything is loopback. A FAKE ml_api (127.0.0.1, ephemeral port) plays the part of the real
container: it implements GET /hc/ and GET /p/?img=<url>, and - like the real one - fetches the
picture from the URL it is given. The real Obico image is never started.
"""
import http.client
import http.server
import importlib.util
import json
import os
import socketserver
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "detector_adapter_app", os.path.join(HERE, "..", "detector", "adapter", "app.py"))
adapter_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adapter_app)

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes" * 40 + b"\xff\xd9"
TOKEN = "t0ken-for-tests-only-0123456789"
INTERNAL = "internal-ml-token"
CANNED = {"detections": [["failure", 0.31, [100, 120, 50, 40]], ["failure", 0.12, [10, 20, 5, 5]]]}
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakeMlApi:
    """Records what the adapter asked for. `mode` switches behaviour per test."""

    def __init__(self):
        self.mode = "ok"
        self.healthy = True
        self.calls = []              # one dict per /p/ request
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _reply(self, status, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                if u.path == "/hc/":
                    return self._reply(200 if outer.healthy else 500, b"ok", "text/plain")
                if u.path != "/p/":
                    return self._reply(404, {})
                call = {"auth": self.headers.get("Authorization"), "fetches": []}
                outer.calls.append(call)
                img = urllib.parse.parse_qs(u.query).get("img", [""])[0]
                call["url"] = img
                if outer.mode == "reject_auth" or (INTERNAL and call["auth"] != "Bearer " + INTERNAL):
                    return self._reply(401, {"detail": "no"})
                if outer.mode == "400_once" and len(outer.calls) == 1:
                    return self._reply(400, {"detections": [], "message": "fetch failed"})
                if outer.mode == "slow":
                    time.sleep(2.0)
                if outer.mode == "garbage":
                    return self._reply(200, b"<html>not json</html>", "text/html")
                if outer.mode == "boom":
                    return self._reply(500, {})
                for _ in range(2 if outer.mode == "fetch_twice" else 1):
                    try:
                        with NOPROXY.open(img, timeout=3) as r:
                            call["fetches"].append((r.status, r.read()))
                    except urllib.error.HTTPError as e:
                        e.close()
                        call["fetches"].append((e.code, b""))
                    except OSError as e:          # adapter already gone (timeout test)
                        call["fetches"].append((0, b""))
                return self._reply(200, CANNED)

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                                       daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def make_adapter(ml, **env):
    base = {"DETECTOR_TOKEN": TOKEN, "ML_API_URL": ml.url, "ML_API_TOKEN": INTERNAL,
            "BIND": "127.0.0.1", "PORT": "0", "IMAGE_PORT": "0", "IMAGE_HOST": "127.0.0.1"}
    base.update(env)
    cfg = adapter_app.Config(base)
    return adapter_app.Adapter(cfg).start()


def post(ad, body=JPEG, token=TOKEN, ctype="image/jpeg", path="/v1/score", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", ad.port, timeout=10)
    h = {"Content-Type": ctype} if ctype else {}
    if token is not None:
        h["Authorization"] = token if token.startswith(("Bearer ", "Basic ")) else "Bearer " + token
    h.update(headers or {})
    try:
        conn.request("POST", path, body=body, headers=h)
        r = conn.getresponse()
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None), dict(r.getheaders())
    finally:
        conn.close()


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw
    finally:
        conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.ml = FakeMlApi()
        self.ad = make_adapter(self.ml)

    def tearDown(self):
        self.ad.stop()
        self.ml.stop()


class Auth(Base):
    def test_token_required(self):
        self.assertEqual(post(self.ad, token=None)[0], 401)
        st, body, hdr = post(self.ad, token="wrong-token")
        self.assertEqual((st, body["error"]), (401, "unauthorized"))
        self.assertEqual(hdr.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(post(self.ad, token="Basic " + TOKEN)[0], 401)
        self.assertEqual(post(self.ad, token="Bearer " + TOKEN[:-1])[0], 401)
        self.assertEqual(self.ml.calls, [], "an unauthorised request must never reach ml_api")
        self.assertEqual(post(self.ad)[0], 200)

    def test_auth_checked_before_body_is_read(self):
        # A 200 MB claim with a bad token is refused as 401, not 413 (no information for strangers).
        conn = http.client.HTTPConnection("127.0.0.1", self.ad.port, timeout=10)
        conn.putrequest("POST", "/v1/score")
        conn.putheader("Content-Length", "209715200")
        conn.putheader("Authorization", "Bearer nope")
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual(r.status, 401)
        conn.close()

    def test_refuses_to_start_without_token(self):
        cfg = adapter_app.Config({"ML_API_URL": self.ml.url})
        self.assertTrue(cfg.problems())
        self.assertEqual(adapter_app.Config({"DETECTOR_TOKEN": ""}).problems() != [], True)
        with mock.patch.dict(os.environ, {"DETECTOR_TOKEN": "", "DETECTOR_ALLOW_NO_TOKEN": ""}), \
                mock.patch("sys.stderr"):
            self.assertEqual(adapter_app.main(), 2)

    def test_opt_out_of_token(self):
        cfg = adapter_app.Config({"DETECTOR_ALLOW_NO_TOKEN": "1"})
        self.assertEqual(cfg.problems(), [])
        ad = make_adapter(self.ml, DETECTOR_TOKEN="", DETECTOR_ALLOW_NO_TOKEN="1")
        try:
            self.assertEqual(post(ad, token=None)[0], 200)
            self.assertEqual(get(ad.port, "/healthz")[1]["auth"], "none")
        finally:
            ad.stop()


class Limits(Base):
    def test_too_large(self):
        ad = make_adapter(self.ml, MAX_BYTES="2048")
        try:
            big = b"\xff\xd8" + b"a" * 4000
            st, body, _ = post(ad, body=big)
            self.assertEqual((st, body["error"]), (413, "too_large"))
            ok = post(ad, body=b"\xff\xd8" + b"a" * 1000)
            self.assertEqual(ok[0], 200)
        finally:
            ad.stop()

    def test_exactly_at_limit_is_accepted(self):
        ad = make_adapter(self.ml, MAX_BYTES="2048")
        try:
            self.assertEqual(post(ad, body=b"\xff\xd8" + b"a" * 2046)[0], 200)
            self.assertEqual(post(ad, body=b"\xff\xd8" + b"a" * 2047)[0], 413)
        finally:
            ad.stop()

    def test_must_be_jpeg(self):
        st, body, _ = post(self.ad, body=b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        self.assertEqual((st, body["error"]), (415, "not_jpeg"))
        self.assertEqual(post(self.ad, body=b"<html></html>")[1]["error"], "not_jpeg")
        self.assertEqual(self.ml.calls, [])

    def test_content_type(self):
        st, body, _ = post(self.ad, ctype="text/plain")
        self.assertEqual((st, body["error"]), (415, "unsupported_type"))
        self.assertEqual(post(self.ad, ctype=None)[0], 200)
        self.assertEqual(post(self.ad, ctype="application/octet-stream")[0], 200)
        self.assertEqual(post(self.ad, ctype="image/jpeg; charset=binary")[0], 200)

    def test_empty_body(self):
        st, body, _ = post(self.ad, body=b"")
        self.assertEqual((st, body["error"]), (400, "empty_body"))

    def test_chunked_not_accepted(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.ad.port, timeout=10)
        conn.putrequest("POST", "/v1/score")
        conn.putheader("Authorization", "Bearer " + TOKEN)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["error"]), (411, "length_required"))
        conn.close()

    def test_short_body(self):
        s = __import__("socket").create_connection(("127.0.0.1", self.ad.port), timeout=10)
        s.sendall((f"POST /v1/score HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                   f"Content-Length: 1000\r\n\r\n").encode() + JPEG[:50])
        s.shutdown(__import__("socket").SHUT_WR)
        data = b""
        while True:                      # the server closes the connection after its answer
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        self.assertIn(b"400", data.split(b"\r\n")[0])
        self.assertIn(b"short_body", data)

    def test_unknown_paths_and_methods(self):
        self.assertEqual(post(self.ad, path="/nope")[0], 404)
        self.assertEqual(get(self.ad.port, "/v1/score")[0], 404)
        self.assertEqual(get(self.ad.port, "/")[0], 404)
        conn = http.client.HTTPConnection("127.0.0.1", self.ad.port, timeout=10)
        conn.request("DELETE", "/v1/score")
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()


class Scoring(Base):
    def test_round_trip(self):
        st, body, hdr = post(self.ad)
        self.assertEqual(st, 200)
        self.assertEqual(body["api"], 1)
        self.assertAlmostEqual(body["score"], 0.43, places=6)         # 0.31 + 0.12
        self.assertEqual(body["detections"][0], {"label": "failure", "conf": 0.31, "box": [100.0, 120.0, 50.0, 40.0]})
        self.assertEqual(len(body["detections"]), 2)
        self.assertIsInstance(body["ms"], int)
        self.assertEqual(hdr.get("Cache-Control"), "no-store")
        # ml_api received the identical bytes from our internal listener, with its own token
        call = self.ml.calls[0]
        self.assertEqual(call["fetches"], [(200, JPEG)])
        self.assertEqual(call["auth"], "Bearer " + INTERNAL)
        self.assertNotIn(TOKEN, repr(call))                      # the caller's token never goes inward
        self.assertEqual(len(self.ad.frames), 0, "the picture must be forgotten afterwards")

    def test_no_detections_is_score_zero(self):
        CANNED_BACKUP = list(CANNED["detections"])
        CANNED["detections"][:] = []
        try:
            st, body, _ = post(self.ad)
        finally:
            CANNED["detections"][:] = CANNED_BACKUP
        self.assertEqual((st, body["score"], body["detections"]), (200, 0.0, []))

    def test_url_is_random_and_single_use(self):
        self.ml.mode = "fetch_twice"
        post(self.ad)
        post(self.ad)
        urls = [c["url"] for c in self.ml.calls]
        self.assertNotEqual(urls[0], urls[1])
        for u in urls:
            self.assertRegex(u, r"^http://127\.0\.0\.1:\d+/_f/[A-Za-z0-9_-]{30,}$")
        for c in self.ml.calls:
            self.assertEqual([s for s, _ in c["fetches"]], [200, 404], "second fetch of the same URL must fail")
        # and it stays gone afterwards
        u = urllib.parse.urlparse(urls[0])
        self.assertEqual(get(u.port, u.path)[0], 404)

    def test_picture_listener_rejects_guesses(self):
        for path in ("/_f/", "/_f/abc", "/", "/_f/../healthz", "/healthz"):
            self.assertEqual(get(self.ad.image_port, path)[0], 404, path)
        conn = http.client.HTTPConnection("127.0.0.1", self.ad.image_port, timeout=5)
        conn.request("POST", "/_f/x", body=b"1")
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()
        # the picture listener does not serve the scoring API, nor the public one the pictures
        self.assertEqual(get(self.ad.port, "/_f/abc")[0], 404)

    def test_head_does_not_consume_picture(self):
        fid = self.ad.frames.put(JPEG)
        conn = http.client.HTTPConnection("127.0.0.1", self.ad.image_port, timeout=5)
        conn.request("HEAD", f"/_f/{fid}")
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()
        self.assertEqual(get(self.ad.image_port, f"/_f/{fid}"), (200, JPEG))
        self.assertEqual(get(self.ad.image_port, f"/_f/{fid}")[0], 404)

    def test_retry_when_ml_api_could_not_fetch(self):
        self.ml.mode = "400_once"
        st, body, _ = post(self.ad)
        self.assertEqual(st, 200)
        self.assertEqual(len(self.ml.calls), 2)
        self.assertNotEqual(self.ml.calls[0]["url"], self.ml.calls[1]["url"])
        self.assertEqual(len(self.ad.frames), 0)

    def test_ml_api_down(self):
        self.ml.stop()
        st, body, _ = post(self.ad)
        self.assertEqual((st, body["error"]), (502, "detector_unreachable"))
        self.assertEqual(len(self.ad.frames), 0)
        st, body = get(self.ad.port, "/healthz")
        self.assertEqual((st, body["status"], body["ml_api"]), (503, "degraded", "unreachable"))
        self.ml = FakeMlApi()           # so tearDown has something to stop

    def test_ml_api_errors_map_to_codes(self):
        self.ml.mode = "garbage"
        self.assertEqual(post(self.ad)[1]["error"], "detector_bad_response")
        self.ml.mode = "boom"
        self.assertEqual(post(self.ad)[1]["error"], "detector_error")
        self.ml.mode = "reject_auth"
        self.assertEqual(post(self.ad)[1]["error"], "detector_auth")
        self.assertEqual(len(self.ad.frames), 0)

    def test_ml_api_timeout(self):
        self.ad.stop()
        cfg = adapter_app.Config({"DETECTOR_TOKEN": TOKEN, "ML_API_URL": self.ml.url, "ML_API_TOKEN": INTERNAL,
                                  "BIND": "127.0.0.1", "PORT": "0", "IMAGE_PORT": "0",
                                  "IMAGE_HOST": "127.0.0.1"})
        cfg.ml_timeout = 0.5
        self.ad = adapter_app.Adapter(cfg).start()
        self.ml.mode = "slow"
        st, body, _ = post(self.ad)
        self.assertEqual((st, body["error"]), (504, "detector_timeout"))
        self.assertEqual(len(self.ad.frames), 0)

    def test_busy(self):
        self.assertTrue(self.ad._gate.acquire(timeout=1))
        try:
            t0 = time.monotonic()
            st, body, hdr = post(self.ad)
            self.assertEqual((st, body["error"], hdr.get("Retry-After")), (503, "busy", "1"))
            self.assertLess(time.monotonic() - t0, 3)
        finally:
            self.ad._gate.release()
        self.assertEqual(post(self.ad)[0], 200)


class Health(Base):
    def test_healthz_needs_no_token_and_leaks_nothing(self):
        st, body = get(self.ad.port, "/healthz")
        self.assertEqual((st, body["status"], body["ml_api"], body["auth"], body["api"]),
                         (200, "ok", "ok", "token", 1))
        self.assertNotIn(TOKEN, json.dumps(body))
        self.assertNotIn(INTERNAL, json.dumps(body))
        self.assertIn("arch", body)

    def test_degraded_when_ml_api_unhealthy(self):
        self.ml.healthy = False
        st, body = get(self.ad.port, "/healthz")
        self.assertEqual((st, body["status"]), (503, "degraded"))


class FrameStoreTests(unittest.TestCase):
    def test_single_use(self):
        fs = adapter_app.FrameStore(30)
        fid = fs.put(b"abc")
        self.assertEqual(fs.take(fid), b"abc")
        self.assertIsNone(fs.take(fid))

    def test_expiry(self):
        now = [100.0]
        fs = adapter_app.FrameStore(30, clock=lambda: now[0])
        a = fs.put(b"a")
        now[0] = 129.0
        b = fs.put(b"b")
        self.assertEqual(len(fs), 2)
        now[0] = 131.0                      # a expired (130), b alive (159)
        self.assertIsNone(fs.take(a))
        self.assertEqual(fs.take(b), b"b")
        self.assertEqual(len(fs), 0)

    def test_ids_are_unique_and_long(self):
        fs = adapter_app.FrameStore(30)
        ids = {fs.put(b"x") for _ in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(all(len(i) >= 30 for i in ids))


class Normalise(unittest.TestCase):
    def test_sum_and_junk(self):
        score, dets = adapter_app.normalise(
            [["failure", 0.25, [1, 2, 3, 4]], ["failure", "0.5", [5, 6, 7, 8]], ["x"], None,
             ["failure", float("nan"), [0, 0, 0, 0]], ["failure", -1, [0, 0, 0, 0]], ["failure", 0.1, "bad"]])
        self.assertAlmostEqual(score, 0.75)
        self.assertEqual([d["conf"] for d in dets], [0.25, 0.5])

    def test_empty(self):
        self.assertEqual(adapter_app.normalise([]), (0.0, []))


if __name__ == "__main__":
    unittest.main()
