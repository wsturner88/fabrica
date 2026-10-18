"""Tests for app/prusalink.py — stdlib only, no printers, no external network.

    python3 tests/test_prusalink.py

Everything runs against throwaway fake PrusaLink servers on 127.0.0.1 (random
ports). The fake implements real HTTP Digest auth (nonce tracking, nc replay
check, stale-nonce rotation) and is itself cross-checked against urllib's
stock Digest client, so a green run means the driver's hand-rolled Digest
really interoperates with a standards-following server.
"""
import hashlib
import http.server
import json
import os
import re
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from bambu import PrinterError  # noqa: E402
from prusalink import PrusaLinkPrinter  # noqa: E402
import prusalink_fixtures as FX  # noqa: E402

USER, PW, KEY = "maker", "s3cret-pw", "test-api-key"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-frame\xff\xd9"
BIG = (b"; thumbnail begin 16x16 100\n" + b"G1 X1 Y1\n" * 5000)


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class FakePrusaLink:
    """A tiny PrusaLink. Attributes are poked directly by tests."""

    def __init__(self, auth="digest", dl_style="raw", ignore_range=False,
                 rotate_after=0, camera=False, core=None):
        self.auth, self.dl_style, self.ignore_range = auth, dl_style, ignore_range
        # core: {short name: file} - answer like a Core One class machine (see prusalink_fixtures.py): a listing with long
        # names and picture refs but no size, per-file info with `size`, pictures under /thumb/l/<storage>/<short>
        self.core = core
        self.rotate_after, self.camera = rotate_after, camera
        self.state = "IDLE"
        self.job = None                      # {"id":.., "name":.., "progress":.., "time_remaining":..}
        self.files = {"benchy.gcode": BIG, "sub dir/bracket v2.bgcode": b"BGCODE" * 100,
                      "notes.txt": b"hello", "fw.bbf": b"firmware"}
        self.log = []                        # (method, unquoted path)
        self.auth_headers = []               # Authorization / X-Api-Key seen on each request
        self.nonces = {}                     # nonce -> last nc
        self.uses = 0
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            # ---- helpers
            def reply(self, code, body=b"", ctype="application/json", extra=None):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def challenge(self, stale=False):
                nonce = hashlib.md5(os.urandom(8)).hexdigest()
                with fake.lock:
                    fake.nonces[nonce] = 0
                    fake.uses = 0
                h = (f'Digest realm="Printer API", nonce="{nonce}", opaque="op", '
                     f'qop="auth", algorithm=MD5' + (', stale=true' if stale else ""))
                self.reply(401, b"", extra={"WWW-Authenticate": h})

            def digest_ok(self):
                a = self.headers.get("Authorization", "")
                if not a.startswith("Digest "):
                    return None
                p = {m[0]: (m[1] or m[2]) for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', a)}
                nonce = p.get("nonce")
                with fake.lock:
                    if nonce not in fake.nonces:
                        return "stale"
                    if fake.rotate_after and fake.uses >= fake.rotate_after:
                        del fake.nonces[nonce]
                        return "stale"
                    nc = int(p.get("nc", "0"), 16)
                    if nc <= fake.nonces[nonce]:
                        return False            # replay
                    fake.nonces[nonce] = nc
                    fake.uses += 1
                if p.get("username") != USER or p.get("uri") != self.path:
                    return False
                ha1 = md5(f"{USER}:Printer API:{PW}")
                ha2 = md5(f"{self.command}:{self.path}")
                want = md5(f"{ha1}:{nonce}:{p['nc']}:{p['cnonce']}:auth:{ha2}")
                return want == p.get("response")

            def authed(self):
                key = self.headers.get("X-Api-Key")
                with fake.lock:
                    fake.auth_headers.append((self.headers.get("Authorization"), key))
                if fake.auth in ("key", "either") and key == KEY:
                    return True
                if fake.auth in ("digest", "either"):
                    r = self.digest_ok()
                    if r is True:
                        return True
                    self.challenge(stale=(r == "stale"))
                    return False
                self.reply(401, {"message": "Unauthorized"})
                return False

            # ---- routing
            def route(self):
                path = urllib.parse.unquote(self.path.split("?")[0])
                with fake.lock:
                    fake.log.append((self.command, path))
                if self.command == "GET" and path == "/api/version":
                    return self.reply(200, {"api": "0.1", "server": "2.1.2", "text": "PrusaLink"})
                if path.startswith("/snapshot-other"):
                    return self.reply(200, JPEG, "image/jpeg")
                if not self.authed():
                    return
                m, f = self.command, fake
                if path == "/api/v1/info":
                    return self.reply(200, {"printer_type": "MK4S", "nozzle_diameter": 0.4,
                                            "hostname": "prusa-test"})
                if path == "/api/v1/status":
                    hot = f.state != "IDLE"        # an idle printer has its heaters off (the prewarm stands down for a heating one)
                    st = {"printer": {"state": f.state, "temp_nozzle": 215.0 if hot else 25.0,
                                      "target_nozzle": 215.0 if hot else 0.0,
                                      "temp_bed": 60.0 if hot else 24.0, "target_bed": 60.0 if hot else 0.0}}
                    if f.job:
                        st["job"] = {"id": f.job["id"], "progress": f.job["progress"],
                                     "time_remaining": f.job["time_remaining"], "time_printing": 10}
                    return self.reply(200, st)
                if path == "/api/v1/job" and m == "GET":
                    if not f.job:
                        return self.reply(204)
                    return self.reply(200, {"id": f.job["id"], "state": f.state, "progress": f.job["progress"],
                                            "file": {"name": f.job["name"] + ".gcode",
                                                     "display_name": f.job["name"] + ".gcode"}})
                jm = re.fullmatch(r"/api/v1/job/(\d+)(/pause|/resume)?", path)
                if jm and m in ("PUT", "DELETE"):
                    return self.reply(204)
                if path == "/api/v1/cameras":
                    cams = [{"camera_id": "cam1", "connected": True}] if f.camera else []
                    return self.reply(200, {"camera_list": cams})
                if path == "/api/v1/cameras/cam1/snap" and f.camera:
                    return self.reply(200, JPEG, "image/jpeg")
                if f.core is not None and (path.startswith("/api/v1/files/usb") or path.startswith("/thumb/")):
                    return self.core_route(m, path)
                if path.startswith("/api/v1/files/usb"):
                    return self.files_route(m, path[len("/api/v1/files/usb"):])
                self.reply(404, {"message": "not found"})

            def core_route(self, m, path):
                f = fake
                if path.startswith("/thumb/"):
                    parts = path.split("/")            # "", "thumb", "l"|"s", "usb", short
                    short = "/".join(parts[4:])
                    ent = f.core.get(short)
                    if m != "GET" or parts[2] != "l" or not ent or not ent.get("png"):
                        return self.reply(404, {"message": "no thumbnail"})
                    return self.reply(200, ent["png"], "image/png")
                rest = path[len("/api/v1/files/usb"):]
                if m == "GET" and rest.endswith("/") and rest.strip("/") == "SUBDIR":
                    return self.reply(200, {"type": "FOLDER", "name": "SUBDIR", "ro": False, "children": []})
                if m == "GET" and rest in ("", "/"):
                    kids = [FX.listing_entry("usb", k, v) for k, v in f.core.items()]
                    kids.append({"name": "SUBDIR", "ro": False, "type": "FOLDER", "m_timestamp": 1756799943,
                                 "display_name": "SUBDIR"})
                    return self.reply(200, {"type": "FOLDER", "name": "usb", "ro": False, "children": kids})
                short = rest.strip("/")
                ent = f.core.get(short)
                if ent is None:
                    return self.reply(404, {"message": "no such file"})
                if m == "GET":
                    info = FX.listing_entry("usb", short, ent)
                    info["size"] = ent["size"]
                    if ent.get("meta"):
                        info["meta"] = ent["meta"]
                    return self.reply(200, info)
                if m == "DELETE":
                    if f.state == "PRINTING":
                        return self.reply(409, {"message": "File is being printed"})
                    del f.core[short]
                    return self.reply(204)
                self.reply(405, {})

            def files_route(self, m, rest):
                f = fake
                if m == "GET" and (rest in ("", "/") or rest.endswith("/")):
                    folder = rest.strip("/")
                    kids, dirs = [], set()
                    for p, data in f.files.items():
                        if folder and not p.startswith(folder + "/"):
                            continue
                        sub = p[len(folder) + 1:] if folder else p
                        if "/" in sub:
                            dirs.add(sub.split("/")[0])
                            continue
                        typ = "FIRMWARE" if sub.endswith(".bbf") else (
                            "PRINT_FILE" if sub.endswith((".gcode", ".bgcode")) else "FILE")
                        kids.append({"type": typ, "name": sub, "display_name": sub, "size": len(data),
                                     "m_timestamp": 1700000000, "refs": {"download": "/usb/" + p}})
                    kids += [{"type": "FOLDER", "name": d} for d in sorted(dirs)]
                    return self.reply(200, {"type": "FOLDER", "name": "usb", "children": kids})
                raw = rest.endswith("/raw")
                p = rest[1:-4] if raw else rest[1:]
                if m == "GET":
                    if p not in f.files:
                        return self.reply(404, {"message": "no such file"})
                    is_download = raw if f.dl_style == "raw" else not raw
                    if not is_download:
                        if f.dl_style == "plain" and raw:
                            return self.reply(404, {"message": "no /raw here"})
                        return self.reply(200, {"type": "PRINT_FILE", "name": p.split("/")[-1],
                                                "size": len(f.files[p])})
                    data = f.files[p]
                    rng = self.headers.get("Range")
                    if rng and not f.ignore_range:
                        mm = re.fullmatch(r"bytes=(\d+)-(\d+)", rng)
                        a, b = int(mm.group(1)), int(mm.group(2))
                        return self.reply(206, data[a:b + 1], "application/octet-stream")
                    return self.reply(200, data, "application/octet-stream")
                if m == "POST":
                    if p not in f.files:
                        return self.reply(404, {"message": "no such file"})
                    if f.state == "PRINTING":
                        return self.reply(409, {"message": "Printer is busy"})
                    return self.reply(204)
                if m == "DELETE":
                    if p not in f.files:
                        return self.reply(404, {"message": "no such file"})
                    del f.files[p]
                    return self.reply(204)
                self.reply(405, {})

            do_GET = do_POST = do_PUT = do_DELETE = route

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def calls(self, method=None):
        return [p for m, p in self.log if method is None or m == method]


def free_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


CREDS = {"username": USER, "password": PW}


class FakeServerIsStandard(unittest.TestCase):
    """The fake's Digest must satisfy urllib's stock client, else the driver tests prove nothing."""

    def test_stock_urllib_digest_client_authenticates(self):
        fake = FakePrusaLink()
        try:
            pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            pm.add_password(None, fake.url, USER, PW)
            op = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm))
            with op.open(fake.url + "/api/v1/status", timeout=5) as r:
                self.assertEqual(json.loads(r.read())["printer"]["state"], "IDLE")
            bad = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            bad.add_password(None, fake.url, USER, "wrong")
            op2 = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(bad))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                op2.open(fake.url + "/api/v1/status", timeout=5)
            self.assertEqual(cm.exception.code, 401)
        finally:
            fake.close()


class Base(unittest.TestCase):
    auth = "digest"
    fake_kw = {}

    def setUp(self):
        self.fake = FakePrusaLink(auth=self.auth, **self.fake_kw)
        self.p = PrusaLinkPrinter("T", self.fake.url, CREDS, "Prusa MK4S")

    def tearDown(self):
        self.fake.close()


class TestProbe(unittest.TestCase):
    def test_ok_digest(self):
        f = FakePrusaLink(camera=True)
        try:
            r = PrusaLinkPrinter.probe(f.url, CREDS, timeout=3)
            self.assertEqual(r, {"ok": True, "model": "MK4S", "firmware": "2.1.2",
                                 "has_camera": True, "error": None})
        finally:
            f.close()

    def test_ok_no_camera(self):
        f = FakePrusaLink()
        try:
            r = PrusaLinkPrinter.probe(f.url, CREDS, timeout=3)
            self.assertTrue(r["ok"])
            self.assertFalse(r["has_camera"])
        finally:
            f.close()

    def test_ok_api_key(self):
        f = FakePrusaLink(auth="key")
        try:
            self.assertTrue(PrusaLinkPrinter.probe(f.url, KEY, timeout=3)["ok"])
            self.assertTrue(PrusaLinkPrinter.probe(f.url, {"api_key": KEY}, timeout=3)["ok"])
        finally:
            f.close()

    def test_bad_password(self):
        f = FakePrusaLink()
        try:
            r = PrusaLinkPrinter.probe(f.url, {"username": USER, "password": "nope"}, timeout=3)
            self.assertFalse(r["ok"])
            self.assertIn("Authentication failed", r["error"])
            self.assertIsNone(r["model"])
        finally:
            f.close()

    def test_no_credentials(self):
        f = FakePrusaLink()
        try:
            r = PrusaLinkPrinter.probe(f.url, None, timeout=3)
            self.assertFalse(r["ok"])
            self.assertIn("Authentication failed", r["error"])
        finally:
            f.close()

    def test_wrong_api_key(self):
        f = FakePrusaLink(auth="key")
        try:
            r = PrusaLinkPrinter.probe(f.url, "bad", timeout=3)
            self.assertFalse(r["ok"])
            self.assertIn("Authentication failed", r["error"])
        finally:
            f.close()

    def test_unreachable(self):
        r = PrusaLinkPrinter.probe(free_port_url(), CREDS, timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("Could not connect", r["error"])

    def test_not_prusalink(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        try:
            r = PrusaLinkPrinter.probe(f"http://127.0.0.1:{srv.server_address[1]}", CREDS, timeout=3)
            self.assertFalse(r["ok"])
            self.assertIn("does not look like PrusaLink", r["error"])
        finally:
            srv.shutdown(); srv.server_close()

    def test_probe_never_raises_on_garbage(self):
        r = PrusaLinkPrinter.probe("not a url", CREDS, timeout=1)
        self.assertFalse(r["ok"])
        self.assertIsInstance(r["error"], str)


class TestDigestClient(Base):
    def test_one_challenge_then_preauth(self):
        for _ in range(4):
            self.p.refresh()
            self.assertTrue(self.p.snapshot()["connected"])
        # 4 status calls, but only the first needed a 401 round trip
        self.assertEqual(self.fake.calls().count("/api/v1/status"), 5)
        unauth = [h for h in self.fake.auth_headers if h[0] is None]
        self.assertEqual(len(unauth), 1)

    def test_wrong_password_reports_disconnected(self):
        p = PrusaLinkPrinter("T", self.fake.url, {"username": USER, "password": "bad"})
        s = p.snapshot()
        self.assertFalse(s["connected"])
        self.assertEqual(s["state"], "UNKNOWN")
        self.assertIn("credentials", s["error"])

    def test_stale_nonce_is_renegotiated(self):
        fake = FakePrusaLink(rotate_after=2)
        try:
            p = PrusaLinkPrinter("T", fake.url, CREDS)
            for _ in range(8):
                p.refresh()
                self.assertTrue(p.snapshot()["connected"])
        finally:
            fake.close()


class TestSnapshot(Base):
    def snap(self):
        self.p.refresh()
        return self.p.snapshot()

    def test_keys_match_octoprint_shape(self):
        s = self.snap()
        octo_keys = {"printer", "connected", "state", "job", "percent", "remaining_min", "layer",
                     "nozzle_c", "nozzle_target_c", "bed_c", "bed_target_c", "print_error",
                     "sdcard", "age_seconds", "kind"}
        self.assertTrue(octo_keys <= set(s), octo_keys - set(s))
        self.assertEqual(s["kind"], "prusalink")

    def test_state_mapping_every_state(self):
        expect = {"IDLE": "IDLE", "READY": "IDLE", "BUSY": "PREPARE", "PRINTING": "RUNNING",
                  "PAUSED": "PAUSE", "FINISHED": "FINISH", "STOPPED": "FAILED", "ERROR": "FAILED",
                  "SOMETHING_NEW": "UNKNOWN"}
        for raw, want in expect.items():
            self.fake.state, self.fake.job = raw, None
            s = self.snap()
            self.assertEqual(s["state"], want, raw)
            self.assertTrue(s["connected"])
            self.assertEqual(s["raw_state"], raw)

    def test_attention_is_pause_with_job_idle_without(self):
        self.fake.state, self.fake.job = "ATTENTION", None
        self.assertEqual(self.snap()["state"], "IDLE")
        self.assertEqual(self.snap()["print_error"], 1)
        self.fake.job = {"id": 5, "name": "x", "progress": 10, "time_remaining": 600}
        self.assertEqual(self.snap()["state"], "PAUSE")

    def test_printing_fields(self):
        self.fake.state = "PRINTING"
        self.fake.job = {"id": 7, "name": "benchy", "progress": 41.6, "time_remaining": 5400}
        s = self.snap()
        self.assertEqual((s["job"], s["percent"], s["remaining_min"]), ("benchy", 42, 90))
        self.assertEqual((s["nozzle_c"], s["nozzle_target_c"], s["bed_c"], s["bed_target_c"]),
                         (215.0, 215.0, 60.0, 60.0))
        self.assertEqual(s["layer"], "")

    def test_job_name_fetched_once_per_job(self):
        self.fake.state = "PRINTING"
        self.fake.job = {"id": 7, "name": "benchy", "progress": 1, "time_remaining": 60}
        for _ in range(3):
            self.snap()
        self.assertEqual(self.fake.calls("GET").count("/api/v1/job"), 1)

    def test_finish_keeps_name_and_100_percent(self):
        self.fake.state = "PRINTING"
        self.fake.job = {"id": 7, "name": "benchy", "progress": 99, "time_remaining": 30}
        self.snap()
        self.fake.state, self.fake.job = "FINISHED", None
        s = self.snap()
        self.assertEqual((s["state"], s["job"], s["percent"]), ("FINISH", "benchy", 100))

    def test_unreachable_is_unknown_not_connected(self):
        p = PrusaLinkPrinter("T", free_port_url(), CREDS)
        s = p.snapshot()
        self.assertFalse(s["connected"])
        self.assertEqual(s["state"], "UNKNOWN")
        self.assertIsNotNone(s["error"])

    def test_cache_within_ttl(self):
        self.snap()
        n = self.fake.calls().count("/api/v1/status")
        self.p.snapshot(); self.p.snapshot()
        self.assertEqual(self.fake.calls().count("/api/v1/status"), n)


class TestFiles(Base):
    def test_listing_shape_and_recursion(self):
        rows = {r["name"]: r for r in self.p.files()}
        self.assertEqual(set(rows), {"benchy.gcode", "sub dir/bracket v2.bgcode"})   # no txt, no firmware
        r = rows["benchy.gcode"]
        for k in ("name", "size", "modified", "print_seconds", "print_time", "filament_mm", "origin"):
            self.assertIn(k, r)
        self.assertEqual((r["size"], r["modified"], r["origin"]), (len(BIG), 1700000000, "local"))

    def test_listing_metadata_when_present(self):
        rows = PrusaLinkPrinter._secs({"estimated_print_time": 3725})
        self.assertEqual(rows, 3725)
        self.assertEqual(PrusaLinkPrinter._secs({"estimated printing time (normal mode)": "1h 2m 5s"}), 3725)
        self.assertIsNone(PrusaLinkPrinter._secs({}))

    def test_download_head_with_range(self):
        data = self.p.download_head("benchy.gcode", 64)
        self.assertEqual(data, BIG[:64])

    def test_download_head_when_range_ignored(self):
        fake = FakePrusaLink(ignore_range=True)
        try:
            p = PrusaLinkPrinter("T", fake.url, CREDS)
            self.assertEqual(p.download_head("benchy.gcode", 64), BIG[:64])
        finally:
            fake.close()

    def test_open_file_full_and_spaces(self):
        with self.p.open_file("sub dir/bracket v2.bgcode") as r:
            self.assertEqual(r.read(), b"BGCODE" * 100)
        self.assertIn("/api/v1/files/usb/sub dir/bracket v2.bgcode/raw", self.fake.calls("GET"))

    def test_open_file_plain_style_fallback(self):
        fake = FakePrusaLink(dl_style="plain")
        try:
            p = PrusaLinkPrinter("T", fake.url, CREDS)
            with p.open_file("benchy.gcode") as r:
                self.assertEqual(r.read(), BIG)
            self.assertEqual(p._dl_style, "plain")
            before = fake.calls("GET").count("/api/v1/files/usb/benchy.gcode/raw")
            with p.open_file("benchy.gcode") as r:      # now goes straight to the plain URL
                r.read()
            self.assertEqual(fake.calls("GET").count("/api/v1/files/usb/benchy.gcode/raw"), before)
        finally:
            fake.close()

    def test_download_missing_file_is_printererror(self):
        with self.assertRaises(PrinterError):
            self.p.open_file("nope.gcode")


class TestJobControl(Base):
    def test_start_hits_post_files(self):
        self.p.start_print("sub dir/bracket v2.bgcode", [])
        self.assertIn(("POST", "/api/v1/files/usb/sub dir/bracket v2.bgcode"), self.fake.log)

    def test_start_while_busy_is_printererror_with_reason(self):
        self.fake.state = "PRINTING"
        with self.assertRaises(PrinterError) as cm:
            self.p.start_print("benchy.gcode")
        self.assertIn("409", str(cm.exception))
        self.assertIn("busy", str(cm.exception))

    def test_pause_resume_stop_use_job_id(self):
        self.fake.state = "PRINTING"
        self.fake.job = {"id": 42, "name": "benchy", "progress": 5, "time_remaining": 100}
        self.p.pause(); self.p.resume(); self.p.stop()
        self.assertIn(("PUT", "/api/v1/job/42/pause"), self.fake.log)
        self.assertIn(("PUT", "/api/v1/job/42/resume"), self.fake.log)
        self.assertIn(("DELETE", "/api/v1/job/42"), self.fake.log)

    def test_pause_without_job_is_printererror(self):
        with self.assertRaises(PrinterError):
            self.p.pause()

    def test_delete(self):
        self.p.sd_delete("benchy.gcode")
        self.assertIn(("DELETE", "/api/v1/files/usb/benchy.gcode"), self.fake.log)
        self.assertNotIn("benchy.gcode", self.fake.files)
        with self.assertRaises(PrinterError):
            self.p.sd_delete("benchy.gcode")

    def test_unreachable_is_printererror(self):
        p = PrusaLinkPrinter("T", free_port_url(), CREDS)
        for fn in (p.stop, p.pause, p.resume, lambda: p.start_print("x.gcode"),
                   lambda: p.sd_delete("x.gcode"), p.files):
            with self.assertRaises(PrinterError):
                fn()

    def test_api_key_mode_end_to_end(self):
        fake = FakePrusaLink(auth="key")
        try:
            p = PrusaLinkPrinter("T", fake.url, KEY)
            self.assertTrue(p.snapshot()["connected"])
            self.assertEqual(len(p.files()), 2)
            p.sd_delete("benchy.gcode")
        finally:
            fake.close()


class TestCamera(unittest.TestCase):
    def test_disconnect_stops_the_poller_and_is_idempotent(self):
        f = FakePrusaLink(camera=True)
        try:
            p = PrusaLinkPrinter("T", f.url, CREDS)
            p.disconnect()                                # never connected, no poller yet: harmless
            p.disconnect()
            self.assertTrue(p.has_camera)
            cam = p.camera()
            self.assertIsNotNone(cam.get_frame(timeout=5))
            thread = cam._thread
            self.assertTrue(thread.is_alive())
            p.disconnect()
            self.assertTrue(cam._stopped)
            thread.join(5)
            self.assertFalse(thread.is_alive())           # the reader thread really exits
            p.disconnect()                                # second call: still fine
            self.assertIsNot(p.camera(), cam)             # a later camera() starts a fresh poller
        finally:
            f.close()

    def test_no_camera(self):
        f = FakePrusaLink()
        try:
            p = PrusaLinkPrinter("T", f.url, CREDS)
            self.assertFalse(p.has_camera)
            self.assertIsNone(p.camera_info())
        finally:
            f.close()

    def test_local_camera_discovered_and_streamed_with_digest(self):
        f = FakePrusaLink(camera=True)
        try:
            p = PrusaLinkPrinter("T", f.url, CREDS)
            self.assertTrue(p.has_camera)
            self.assertEqual(set(p.camera_info()), {"flip_h", "flip_v", "rotate90"})
            cam = p.camera()
            got = cam.get_frame(timeout=5)
            self.assertIsNotNone(got)
            self.assertEqual(got[0], JPEG)
            self.assertIs(p.camera(), cam)                # one shared poller
            cam.stop()
        finally:
            f.close()

    def test_configured_snapshot_url_on_other_host_gets_no_credentials(self):
        printer = FakePrusaLink()
        other = FakePrusaLink()         # stands in for a webcam server
        try:
            p = PrusaLinkPrinter("T", printer.url, {"username": USER, "password": PW, "api_key": KEY},
                                 snapshot_url=other.url + "/snapshot-other")
            self.assertTrue(p.has_camera)
            self.assertEqual(p._fetch_snapshot(), JPEG)
            self.assertTrue(other.auth_headers == [] or all(h == (None, None) for h in other.auth_headers))
        finally:
            printer.close(); other.close()

    def test_relative_snapshot_url_on_printer_host_is_authenticated(self):
        f = FakePrusaLink(camera=True)
        try:
            p = PrusaLinkPrinter("T", f.url, CREDS, snapshot_url="/api/v1/cameras/cam1/snap")
            self.assertTrue(p.has_camera)
            self.assertEqual(p._fetch_snapshot(), JPEG)
        finally:
            f.close()


class TestConfig(unittest.TestCase):
    def test_config_fields_shape(self):
        names = [f["name"] for f in PrusaLinkPrinter.CONFIG_FIELDS]
        for need in ("name", "url", "username", "password"):
            self.assertIn(need, names)
        self.assertNotIn("api_key", names)
        self.assertNotIn("snapshot_url", names)
        self.assertTrue(next(f for f in PrusaLinkPrinter.CONFIG_FIELDS if f["name"] == "password")["required"])
        self.assertEqual(getattr(PrusaLinkPrinter, "CONFIG_REQUIRE_ONE_OF", []), [])
        for adv in ("username", "storage", "model"):
            self.assertTrue(next(f for f in PrusaLinkPrinter.CONFIG_FIELDS if f["name"] == adv).get("advanced"))
        for f in PrusaLinkPrinter.CONFIG_FIELDS:
            self.assertTrue({"name", "label", "type", "required", "help"} <= set(f))
            self.assertIn(f["type"], ("text", "password", "url"))
        self.assertTrue(next(f for f in PrusaLinkPrinter.CONFIG_FIELDS if f["name"] == "url")["required"])

    def test_from_config_reads_env_secrets(self):
        p = PrusaLinkPrinter.from_config(
            {"name": "MK4", "type": "prusalink", "url": "http://printer.example/", "model": "MK4S",
             "password_env": "MK4_PW"}, env={"MK4_PW": "pw"})
        self.assertEqual((p.name, p.url, p.username, p.password, p.model),
                         ("MK4", "http://printer.example", "maker", "pw", "MK4S"))
        with self.assertRaises(PrinterError):
            PrusaLinkPrinter.from_config({"name": "x"}, env={})

    def test_contract_surface(self):
        for attr in ("connect", "refresh", "snapshot", "ams_trays", "files", "download_head",
                     "open_file", "start_print", "stop", "pause", "resume", "sd_delete",
                     "camera_info", "camera", "probe", "from_config"):
            self.assertTrue(callable(getattr(PrusaLinkPrinter, attr)), attr)
        self.assertEqual(PrusaLinkPrinter("t", "http://x").ams_trays(), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
