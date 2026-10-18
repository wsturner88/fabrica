"""Per-printer camera address (rtsp:// via ffmpeg, or an http(s) JPEG URL) + the PrusaLink single Password.

    python3 tests/test_camera_url.py

Loopback only. ffmpeg is never needed: the reader's command line is replaced by a tiny python script that
prints JPEG-looking frames. (A real ffmpeg + RTSP server end-to-end run lives outside the suite.)
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import camera  # noqa: E402
import fleet_store  # noqa: E402
from prusalink import PrusaLinkPrinter  # noqa: E402

SECRET_URL = "rtsp://admin:Sup3rS3cret@192.168.1.60:554/stream1"
FRAME = b"\xff\xd8\xff\xe0" + b"JPEGDATA" * 4 + b"\xff\xd9"


def wait_for(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class FakeFfmpegStream(camera.RtspCameraStream):
    """RtspCameraStream whose `ffmpeg` is a python one-liner."""

    def __init__(self, script, url="rtsp://u:pw1234@10.9.9.9/live", **kw):
        super().__init__(url, label="t", **kw)
        self.script = script

    def _cmd(self, exe):
        return [sys.executable, "-c", self.script]


FRAMES_FOREVER = ("import sys,time\nf=%r\nwhile True:\n sys.stdout.buffer.write(f);sys.stdout.buffer.flush();time.sleep(0.05)" % FRAME)
FAIL_WITH_SECRET = ("import sys\nsys.stderr.write('rtsp://u:pw1234@10.9.9.9/live: 401 Unauthorized pw1234\\n');"
                    "sys.exit(1)")


class Splitter(unittest.TestCase):
    def test_frames_split_across_chunks_and_noise_dropped(self):
        s = camera.JpegSplitter()
        data = b"noise" + FRAME + FRAME + FRAME[:10]
        got = []
        for i in range(0, len(data), 7):
            got += s.feed(data[i:i + 7])
        self.assertEqual(got, [FRAME, FRAME])
        self.assertEqual(s.feed(FRAME[10:]), [FRAME])

    def test_runaway_frame_is_dropped(self):
        s = camera.JpegSplitter(max_frame=100)
        self.assertEqual(s.feed(b"\xff\xd8" + b"x" * 500), [])
        self.assertEqual(s.feed(FRAME), [FRAME])


class Validation(unittest.TestCase):
    def test_good_addresses(self):
        for u in ("rtsp://192.168.1.60/live", "rtsps://cam.local:322/s", "http://10.0.0.5/snap.jpg",
                  "https://u:p%40ss@10.0.0.5/snap.jpg", SECRET_URL, ""):
            self.assertEqual(camera.validate_camera_url(u), u)

    def test_bad_addresses_and_injection_attempts(self):
        for u in ("ftp://x/y", "file:///etc/passwd", "rtsp://", "rtsp:///live", "-i rtsp://x/y", "javascript:alert(1)",
                  "rtsp://h/live; rm -rf /", "rtsp://h/a b", "rtsp://h/`id`", "rtsp://h/x\ny", "rtsp://h/\"q\"",
                  "rtsp://ho st/x", "rtsp://h;rm/x", "rtsp://h/x\x00y", "rtsp://***@h/x", "rtsp://h:99999/x",
                  "rtsp://h/" + "a" * 700):
            with self.assertRaises(camera.CameraUrlError, msg=u):
                camera.validate_camera_url(u)

    def test_shell_metacharacters_are_never_a_shell(self):
        # `$()` / `;` inside a QUERY are legal URL text and travel as ONE argv element
        url = camera.validate_camera_url("rtsp://10.0.0.1/live?a=$HOME&b=1")
        cmd = camera._ffmpeg_cmd("/x/ffmpeg", url)
        self.assertEqual(cmd[cmd.index("-i") + 1], url)
        src = open(os.path.join(HERE, "..", "app", "camera.py")).read()
        self.assertNotIn("shell=True", src)


class Redaction(unittest.TestCase):
    def test_functions(self):
        self.assertEqual(camera.redact_url(SECRET_URL), "rtsp://***@192.168.1.60:554/stream1")
        self.assertEqual(camera.plain_url(SECRET_URL), "rtsp://192.168.1.60:554/stream1")
        self.assertEqual(camera.redact_url("rtsp://192.168.1.60/live"), "rtsp://192.168.1.60/live")
        t = camera.redact_text("failed rtsp://admin:Sup3rS3cret@h/x and Sup3rS3cret alone, admin too", SECRET_URL)
        self.assertNotIn("Sup3rS3cret", t)
        self.assertNotIn("admin", t)

    def test_ffmpeg_failure_text_has_no_credentials(self):
        cam = FakeFfmpegStream(FAIL_WITH_SECRET, idle_timeout=5)
        with mock.patch.object(camera, "find_ffmpeg", return_value="/x/ffmpeg"), \
                mock.patch.object(camera, "BACKOFF_MIN", 0.05):
            self.assertIsNone(cam.get_frame(1.5))
            cam.stop()
        self.assertIn("rejected", cam.last_error)
        self.assertIn("10.9.9.9", cam.last_error)
        for bad in ("pw1234", "u:pw"):
            self.assertNotIn(bad, cam.last_error)

    def test_log_lines_have_no_credentials(self):
        cam = FakeFfmpegStream(FAIL_WITH_SECRET, idle_timeout=5)
        with mock.patch.object(camera, "find_ffmpeg", return_value="/x/ffmpeg"), \
                mock.patch.object(camera, "BACKOFF_MIN", 0.05), self.assertLogs("bambu.camera", "INFO") as lg:
            cam.get_frame(1.5)
            cam.stop()
        self.assertNotIn("pw1234", "\n".join(lg.output))

    def test_snapshot_url_errors_have_no_credentials(self):
        with self.assertRaises(ConnectionError) as cm:
            camera.fetch_snapshot_url("http://admin:Sup3rS3cret@127.0.0.1:1/snap.jpg", 1)
        self.assertNotIn("Sup3rS3cret", str(cm.exception))


class Reader(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(camera, "find_ffmpeg", return_value="/x/ffmpeg")
        p.start()
        self.addCleanup(p.stop)

    def test_frames_arrive_and_process_dies_when_demand_ends(self):
        cam = FakeFfmpegStream(FRAMES_FOREVER, idle_timeout=0.6)
        cam.add_viewer()
        got = cam.get_frame(5)
        self.assertEqual(got[0], FRAME)
        self.assertIsNone(cam.last_error)
        proc = cam.proc
        self.assertIsNone(proc.poll())
        time.sleep(1.0)
        self.assertIsNone(proc.poll(), "a viewer is attached: the single ffmpeg keeps running")
        self.assertEqual(cam.spawns, 1)
        cam.remove_viewer()
        self.assertTrue(wait_for(lambda: proc.poll() is not None, 6), "ffmpeg must be killed after the last viewer")
        self.assertTrue(wait_for(lambda: cam._thread is None, 6))
        self.assertIsNone(cam.latest()[1])           # no stale picture from before it went idle

    def test_no_demand_never_starts_ffmpeg(self):
        cam = FakeFfmpegStream(FRAMES_FOREVER)
        time.sleep(0.2)
        self.assertEqual(cam.spawns, 0)

    def test_restart_with_backoff_on_failure(self):
        cam = FakeFfmpegStream("import sys\nsys.exit(1)", idle_timeout=30)
        with mock.patch.object(camera, "BACKOFF_MIN", 0.1), mock.patch.object(camera, "BACKOFF_MAX", 0.4):
            cam.add_viewer()
            self.assertTrue(wait_for(lambda: cam.spawns >= 3, 8))
            cam.stop()
        self.assertIn("camera not reachable at rtsp://10.9.9.9/live", cam.last_error)

    def test_stream_that_dies_after_frames_resets_and_reconnects(self):
        script = "import sys\nsys.stdout.buffer.write(%r);sys.stdout.buffer.flush()" % FRAME
        cam = FakeFfmpegStream(script, idle_timeout=30)
        with mock.patch.object(camera, "BACKOFF_MIN", 0.05):
            cam.add_viewer()
            self.assertTrue(wait_for(lambda: cam.spawns >= 2, 6))
            cam.stop()

    def test_missing_ffmpeg_is_a_clear_error_and_nothing_crashes(self):
        with mock.patch.object(camera, "find_ffmpeg", return_value=None), \
                mock.patch.object(camera, "BACKOFF_MIN", 0.05):
            cam = camera.RtspCameraStream("rtsp://10.0.0.2/live", label="x", idle_timeout=5)
            self.assertIsNone(cam.get_frame(0.5))
            cam.stop()
            self.assertEqual(cam.last_error, "RTSP cameras need the full image (ffmpeg)")
            ok, msg = camera.check_camera("rtsp://10.0.0.2/live")
            self.assertFalse(ok)
            self.assertIn("ffmpeg", msg)


class _Snap(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        _Snap.seen.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.end_headers()
        self.wfile.write(FRAME)

    def log_message(self, *a):
        pass


class SnapshotUrl(unittest.TestCase):
    def test_http_snapshot_with_login_becomes_basic_auth(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Snap)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = f"http://cam:pw%40x@127.0.0.1:{srv.server_address[1]}/snap.jpg"
        ok, msg = camera.check_camera(url, 3)
        self.assertTrue(ok, msg)
        self.assertNotIn("pw", msg)
        self.assertTrue(_Snap.seen[-1].startswith("Basic "))
        cam = camera.make_camera_stream(url, "s")
        self.assertIsInstance(cam, camera.SnapshotUrlCameraStream)
        self.assertEqual(cam.get_frame(4)[0], FRAME)
        cam.stop()


class FleetCamera(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.printers = {}
        self.store = fleet_store.FleetStore(self.d)
        self.mgr = fleet_store.FleetManager(self.store, self.printers, log=lambda *a: None)

    def add(self, **extra):
        return self.mgr.add("prusalink", {"name": "CoreOne", "url": "http://127.0.0.1:9", "password": "pw", **extra},
                            skip_test=True)

    def test_every_family_offers_an_advanced_camera_field(self):
        for t in self.mgr.types():
            f = next(f for f in t["fields"] if f["name"] == "camera_url")
            self.assertTrue(f.get("advanced"))
            self.assertFalse(f.get("required"))

    def test_printer_without_native_camera_gets_one_and_login_never_leaks(self):
        pub = self.add(camera_url=SECRET_URL)["printer"]
        blob = json.dumps(pub)
        self.assertNotIn("Sup3rS3cret", blob)
        self.assertNotIn("admin:", blob)
        self.assertEqual(pub["camera_url"], "rtsp://***@192.168.1.60:554/stream1")
        prn = self.printers["coreone"]
        self.assertTrue(prn.has_camera)
        self.assertIsInstance(prn.camera(), camera.RtspCameraStream)
        self.assertEqual(prn._override_url, SECRET_URL)
        self.assertEqual(prn.camera_info()["rotate90"], False)
        # the stored login is in secrets.json only (0600), never printers.json
        self.assertNotIn("Sup3rS3cret", open(os.path.join(self.d, "printers.json")).read())
        self.assertIn("Sup3rS3cret", open(os.path.join(self.d, "secrets.json")).read())
        self.assertEqual(oct(os.stat(os.path.join(self.d, "secrets.json")).st_mode & 0o777), "0o600")
        self.assertNotIn("Sup3rS3cret", json.dumps(self.mgr.list_public()))
        self.mgr.remove("CoreOne", force=True)

    def test_disconnect_stops_the_camera(self):
        self.add(camera_url="rtsp://192.168.1.60/live")
        prn = self.printers["coreone"]
        cam = prn.camera()
        self.mgr.remove("CoreOne", force=True)
        self.assertTrue(cam._stopped)

    def test_edit_keep_clear_and_replace(self):
        self.add(camera_url=SECRET_URL)
        stored = lambda: self.store.secret("CoreOne", "camera_url")   # noqa: E731
        # an edit that does not mention the camera keeps it
        self.mgr.update("CoreOne", {"model": "Core One"}, skip_test=True, force=True)
        self.assertEqual(stored(), SECRET_URL)
        # sending the shown (login hidden) form back keeps the stored login
        self.mgr.update("CoreOne", {"camera_url": "rtsp://***@192.168.1.60:554/stream1", "model": "C1"},
                        skip_test=True, force=True)
        self.assertEqual(stored(), SECRET_URL)
        # moving the PRINTER does not drop the camera (it is not sent to the printer)
        self.mgr.update("CoreOne", {"url": "http://127.0.0.1:10", "password": "pw"}, skip_test=True, force=True)
        self.assertEqual(stored(), SECRET_URL)
        # a new address replaces it
        self.mgr.update("CoreOne", {"camera_url": "rtsp://192.168.1.61/live"}, skip_test=True, force=True)
        self.assertEqual(stored(), "rtsp://192.168.1.61/live")
        self.assertIsInstance(self.printers["coreone"].camera(), camera.RtspCameraStream)
        # blank removes it; the printer is back to the driver's own camera
        self.mgr.update("CoreOne", {"camera_url": ""}, skip_test=True, force=True)
        self.assertEqual(stored(), "")
        self.assertIsNone(getattr(self.printers["coreone"], "_override_url", None))
        self.assertEqual(self.mgr.list_public()[0]["camera_url"], "")

    def test_bad_camera_address_is_refused_with_a_plain_message(self):
        with self.assertRaises(fleet_store.FleetError) as cm:
            self.add(camera_url="rtsp://h/x; reboot")
        self.assertNotIn("reboot", str(cm.exception).split("characters")[0] or "")
        self.assertEqual(self.store.records(), [])

    def test_test_connection_grabs_one_frame_and_warns_not_fails(self):
        with mock.patch.object(fleet_store.PrusaLinkPrinter, "probe",
                               return_value={"ok": True, "model": "Core One", "firmware": "6", "has_camera": False}), \
                mock.patch.object(camera, "check_camera", return_value=(True, "Camera picture received from x")) as chk:
            r = self.mgr.test("prusalink", {"name": "N", "url": "http://127.0.0.1:9", "password": "pw",
                                            "camera_url": SECRET_URL})
            chk.assert_called_once()
            self.assertTrue(r["ok"] and r["has_camera"] and r["camera_check"]["ok"])
            self.assertNotIn("Sup3rS3cret", json.dumps(r))
        with mock.patch.object(fleet_store.PrusaLinkPrinter, "probe",
                               return_value={"ok": True, "model": "Core One", "firmware": "6", "has_camera": False}), \
                mock.patch.object(camera, "check_camera", return_value=(False, "camera not reachable at rtsp://h/x")):
            r = self.mgr.test("prusalink", {"name": "N", "url": "http://127.0.0.1:9", "password": "pw",
                                            "camera_url": "rtsp://h/x"})
            self.assertTrue(r["ok"])
            self.assertFalse(r["camera_check"]["ok"])
            self.assertIn("camera not reachable", r["message"])

    def test_migration_of_snapshot_url_and_api_key(self):
        self.store.add({"name": "Old", "type": "prusalink", "url": "http://127.0.0.1:9", "username": "maker",
                        "storage": "usb", "snapshot_url": "/webcam/snap.jpg", "source": "dashboard"},
                       {"api_key": "KEY123"})
        self.store.add({"name": "Old2", "type": "prusalink", "url": "http://127.0.0.1:9", "snapshot_url":
                        "http://10.0.0.7/cam.jpg", "source": "dashboard"}, {"password": "pw"})
        self.mgr.load_all()
        rec = self.store.get("Old")
        self.assertNotIn("snapshot_url", rec)
        self.assertEqual(self.store.secret("Old", "camera_url"), "http://127.0.0.1:9/webcam/snap.jpg")
        self.assertEqual(self.store.secret("Old", "password"), "KEY123")
        self.assertEqual(self.store.secret("Old", "api_key"), "")
        self.assertEqual(self.store.secret("Old2", "camera_url"), "http://10.0.0.7/cam.jpg")
        self.assertEqual(self.store.secret("Old2", "password"), "pw")
        self.assertTrue(self.printers["old"].has_camera)
        for p in list(self.printers.values()):
            fleet_store.stop_driver(p)


class _Prusa(http.server.BaseHTTPRequestHandler):
    """/api/v1/status: needs X-Api-Key == KEY (digest is never satisfied) or, in 'digest' mode, nothing works."""
    KEY = "the-key"
    hits = []

    def do_GET(self):
        _Prusa.hits.append((self.path, self.headers.get("X-Api-Key"), bool(self.headers.get("Authorization"))))
        if self.headers.get("X-Api-Key") == self.KEY:
            body = json.dumps({"printer": {"state": "IDLE"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Digest realm="Printer API", nonce="abc", qop="auth", algorithm=MD5')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class PrusaPassword(unittest.TestCase):
    def setUp(self):
        _Prusa.hits.clear()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Prusa)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def test_password_that_is_really_an_api_key_works_and_is_remembered(self):
        p = PrusaLinkPrinter("p", self.url, {"password": "the-key"})
        st, data = p._call("GET", "/api/v1/status")
        self.assertEqual(st, 200)
        self.assertEqual(p._auth_mode, "apikey")
        _Prusa.hits.clear()
        p._call("GET", "/api/v1/status")
        self.assertEqual(len(_Prusa.hits), 1, "remembered: no more digest round trips")
        self.assertEqual(_Prusa.hits[0][1], "the-key")

    def test_wrong_password_is_still_a_clear_rejection(self):
        p = PrusaLinkPrinter("p", self.url, {"password": "nope"})
        r = PrusaLinkPrinter.probe(self.url, {"password": "nope"})
        self.assertFalse(r["ok"])
        self.assertIn("wrong password", r["error"])
        self.assertIsNone(p._auth_mode)

    def test_probe_ok_with_key_in_the_password_box(self):
        self.assertTrue(PrusaLinkPrinter.probe(self.url, {"password": "the-key"})["ok"])

    def test_stored_api_key_still_honoured(self):
        p = PrusaLinkPrinter("p", self.url, {"api_key": "the-key"})
        self.assertEqual(p._call("GET", "/api/v1/status")[0], 200)

    def test_form_schema(self):
        fam = fleet_store.FAMILIES["prusalink"]
        names = [f["name"] for f in fam.fields]
        self.assertEqual(names, ["name", "url", "password", "username", "storage", "model", "camera_url"])
        with self.assertRaises(fleet_store.FleetError):
            fleet_store.normalize_values(fam, {"name": "A", "url": "1.2.3.4"})        # password now required
        v = fleet_store.normalize_values(fam, {"name": "A", "url": "192.168.1.50", "password": "x"})
        self.assertEqual(v["url"], "http://192.168.1.50")
        self.assertEqual(v["username"], "maker")
        url_f = next(f for f in fam.fields if f["name"] == "url")
        self.assertEqual(url_f["placeholder"], "192.168.1.50")
        self.assertIn("http:// is added for you", url_f["help"])
        for t in ("octoprint", "moonraker"):
            u = next(f for f in fleet_store.FAMILIES[t].fields if f["name"] == "url")
            self.assertIn("http:// is added for you", u["help"])
            self.assertNotIn("http://", u["placeholder"])


if __name__ == "__main__":
    unittest.main()
