"""The Camera section of the Add / Edit printer form, the "Test camera" endpoint, and the Klipper field that used to say "camera".

    python3 tests/test_camera_field.py          (needs the app's dependencies: mcp, starlette, httpx, paho-mqtt)

What went wrong: a camera address (rtsp://user:password@host/stream1) was pasted into the Klipper "Web UI / camera base address" box
because the real "Camera address" box sat at the very bottom of "More options". Now:
  * every printer type has a visible Camera section right after its connection fields (address, example chips, Test camera);
  * the Klipper box is "Klipper web page address" and says it is not for cameras;
  * a camera address typed into a printer address box shows "This looks like a camera address" + a one-tap move;
  * POST /api/camera/test grabs one frame and answers in words (control scope only, rtsp / http / https only, no redirect to
    ftp:, the login never answered or logged).

Everything is local: loopback servers and in-process fakes only; ffmpeg is replaced by a python one-liner. The browser tests load the
real dashboard.html into headless Chrome / Edge with a stubbed `fetch` (skipped when no browser is found).
"""
import base64
import contextlib
import http.server
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="camfield-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)
os.environ.pop("BAMBU_MCP_ALLOWED_HOSTS", None)

import camera  # noqa: E402
import fleet_store  # noqa: E402
import security  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover - the app's deps are not installed
    HAVE = False
    _ERR = e

with open(os.path.join(APP, "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()

CLIENT = None
LOGIN_URL = "rtsp://admin:Sup3rS3cret@192.168.1.60:554/stream1"
SHOWN_URL = "rtsp://***@192.168.1.60:554/stream1"
FRAME = b"\xff\xd8\xff\xe0" + b"JPEGDATA" * 8 + b"\xff\xd9"


def setUpModule():
    global CLIENT
    if HAVE:
        server.PRINTERS.clear()
        CLIENT = TestClient(server.mcp.streamable_http_app())
        CLIENT.__enter__()


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    shutil.rmtree(_TMP, ignore_errors=True)


class Loopback:
    """A tiny HTTP server: `routes` maps a path to (status, headers, body)."""

    def __init__(self, routes):
        self.routes, self.seen = routes, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, self.headers.get("Authorization")))
                st, hdr, body = outer.routes.get(self.path, (404, {}, b""))
                self.send_response(st)
                for k, v in hdr.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path="", login=""):
        return f"http://{login}127.0.0.1:{self.port}{path}"


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class Endpoint(unittest.TestCase):
    """POST /api/camera/test"""

    def setUp(self):
        server.SETTINGS.update({"security.api_auth": "on"})
        for k in server.AUTH.keys.list_public():
            server.AUTH.delete_key(k["id"])
        _r, ctl = server.AUTH.create_key("ctl", "control")
        _r, rd = server.AUTH.create_key("rd", "read")
        self.ctl, self.rd = {"Authorization": "Bearer " + ctl}, {"Authorization": "Bearer " + rd}
        self.c = CLIENT
        for p in list(server.FLEET.store.records()):
            server.FLEET.remove(p["name"], True)

    def post(self, body, headers=None):
        return self.c.post("/api/camera/test", headers=headers or self.ctl, json=body)

    def test_a_view_only_key_is_refused_before_anything_is_fetched(self):
        with mock.patch.object(camera, "grab_frame") as g:
            r = self.post({"url": "http://192.168.1.60/snapshot.jpg"}, self.rd)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["required_scope"], "control")
        g.assert_not_called()
        self.assertIn(("POST", "/api/camera/test", "control"), security.REST_SCOPES)

    def test_no_key_at_all_is_refused(self):
        with mock.patch.object(camera, "grab_frame") as g:
            r = self.c.post("/api/camera/test", json={"url": "http://192.168.1.60/snapshot.jpg"})
        self.assertEqual(r.status_code, 401)
        g.assert_not_called()

    def test_only_rtsp_http_https_are_accepted(self):
        bad = ("ftp://192.168.1.60/a.jpg", "file:///etc/passwd", "javascript:alert(1)", "gopher://192.168.1.60/x",
               "-i rtsp://192.168.1.60/live", "192.168.1.60/live", "rtsp://", "data:image/jpeg;base64,AAAA",
               "rtsp://192.168.1.60/live; reboot", "http://192.168.1.60/a b")
        with mock.patch.object(camera, "grab_frame") as g:
            for u in bad:
                r = self.post({"url": u})
                self.assertEqual(r.status_code, 400, u)
                self.assertIn("error", r.json(), u)
            for u in ("", "   ", None):
                self.assertEqual(self.post({"url": u}).status_code, 400)
            self.assertEqual(self.post({}).status_code, 400)
        g.assert_not_called()

    def test_a_good_address_returns_the_picture_and_never_the_login(self):
        with mock.patch.object(camera, "grab_frame", return_value={"ok": True, "reason": "ok", "message": "Camera picture received.",
                                                                  "where": "x", "jpeg": FRAME}) as g:
            r = self.post({"url": LOGIN_URL})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertEqual(base64.b64decode(j["image"].split(",", 1)[1]), FRAME)
        self.assertTrue(j["image"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(g.call_args[0][0], LOGIN_URL)                  # the typed address is what is tested
        self.assertNotIn("Sup3rS3cret", r.text)
        self.assertNotIn("admin:", r.text)
        self.assertEqual(j["where"], "rtsp://192.168.1.60:554/stream1")

    def test_the_shown_redacted_value_is_tested_with_the_stored_login(self):
        server.FLEET.add("prusalink", {"name": "Printer-A", "url": "http://127.0.0.1:9", "password": "pw",
                                       "camera_url": LOGIN_URL}, skip_test=True)
        ok = {"ok": True, "reason": "ok", "message": "Camera picture received.", "where": "x", "jpeg": FRAME}
        with mock.patch.object(camera, "grab_frame", return_value=ok) as g:
            r = self.post({"printer": "Printer-A", "url": SHOWN_URL})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(g.call_args[0][0], LOGIN_URL)
            self.assertNotIn("Sup3rS3cret", r.text)
            # the shown form WITHOUT saying which printer: there is nothing to borrow the login from
            g.reset_mock()
            r = self.post({"url": SHOWN_URL})
            self.assertEqual(r.status_code, 400)
            self.assertNotIn("Sup3rS3cret", r.text)
            # a DIFFERENT address in the shown style never gets the stored login
            r = self.post({"printer": "Printer-A", "url": "rtsp://***@10.9.9.9/other"})
            self.assertEqual(r.status_code, 400)
            # a changed host typed in full is tested as typed, never with the stored login
            r = self.post({"printer": "Printer-A", "url": "rtsp://10.9.9.9/other"})
            self.assertEqual(g.call_args[0][0], "rtsp://10.9.9.9/other")
            self.assertEqual(self.post({"printer": "Nobody", "url": SHOWN_URL}).status_code, 404)
        self.assertEqual(g.call_count, 1)

    def test_the_login_is_in_no_answer_and_no_log_even_when_the_camera_fails(self):
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                answers = [self.post({"url": "http://admin:Sup3rS3cret@127.0.0.1:9/snap.jpg"}).text]       # nothing listens: refused
                srv = Loopback({"/snap.jpg": (401, {}, b"no")})
                self.addCleanup(srv.close)
                answers.append(self.post({"url": srv.url("/snap.jpg", "admin:Sup3rS3cret@")}).text)
                with mock.patch.object(camera, "find_ffmpeg", return_value="/x/ffmpeg"), \
                        mock.patch.object(camera, "_ffmpeg_cmd", return_value=[sys.executable, "-c",
                                          "import sys;sys.stderr.write('rtsp://admin:Sup3rS3cret@192.168.1.60/s: 401 Unauthorized Sup3rS3cret\\n')"]):
                    answers.append(self.post({"url": LOGIN_URL}).text)
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        for blob in answers + [logs.getvalue(), out.getvalue(), err.getvalue()]:
            self.assertNotIn("Sup3rS3cret", blob)
        self.assertIn("Wrong login", answers[1])
        self.assertIn("Wrong login", answers[2])
        self.assertIn("Nothing answered", answers[0])

    def test_a_second_and_third_test_at_once_are_turned_away(self):
        a = fleet_store._CAMERA_TEST_SLOT
        a.acquire()
        a.acquire()
        try:
            with mock.patch.object(camera, "grab_frame") as g:
                r = self.post({"url": "http://192.168.1.60/snapshot.jpg"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("still running", r.json()["error"])
            g.assert_not_called()
        finally:
            a.release()
            a.release()


class Grab(unittest.TestCase):
    """camera.grab_frame: the words for each way a camera can fail."""

    def fake_ffmpeg(self, script):
        return mock.patch.multiple(camera, find_ffmpeg=mock.Mock(return_value="/x/ffmpeg"),
                                   _ffmpeg_cmd=mock.Mock(return_value=[sys.executable, "-c", script]))

    def test_http_picture_and_each_failure(self):
        srv = Loopback({"/ok.jpg": (200, {}, FRAME), "/html": (200, {}, b"<html>hello</html>"), "/login": (401, {}, b""),
                        "/gone": (404, {}, b"")})
        self.addCleanup(srv.close)
        got = {p: camera.grab_frame(srv.url(p)) for p in ("/ok.jpg", "/html", "/login", "/gone")}
        self.assertTrue(got["/ok.jpg"]["ok"])
        self.assertEqual(got["/ok.jpg"]["jpeg"], FRAME)
        self.assertEqual(got["/html"]["message"], "Camera answered but sent no picture.")
        self.assertEqual(got["/login"]["reason"], "wrong_login")
        self.assertEqual(got["/gone"]["reason"], "not_found")
        self.assertEqual(camera.grab_frame("http://127.0.0.1:9/x.jpg")["reason"], "unreachable")

    def test_a_basic_login_in_the_address_is_sent_as_a_header_only(self):
        srv = Loopback({"/ok.jpg": (200, {}, FRAME)})
        self.addCleanup(srv.close)
        self.assertTrue(camera.grab_frame(srv.url("/ok.jpg", "user:pw1234@"))["ok"])
        self.assertTrue(srv.seen[0][1].startswith("Basic "))
        self.assertEqual(srv.seen[0][0], "/ok.jpg")

    def test_a_redirect_to_ftp_or_file_is_refused_and_another_host_never_gets_the_login(self):
        other = Loopback({"/p.jpg": (200, {}, FRAME)})
        self.addCleanup(other.close)
        srv = Loopback({"/ftp": (302, {"Location": "ftp://127.0.0.1/x.jpg"}, b""),
                        "/file": (302, {"Location": "file:///etc/passwd"}, b""),
                        "/same": (302, {"Location": "/ok.jpg"}, b""), "/ok.jpg": (200, {}, FRAME),
                        "/away": (302, {"Location": other.url("/p.jpg")}, b"")})
        self.addCleanup(srv.close)
        for p in ("/ftp", "/file"):
            r = camera.grab_frame(srv.url(p))
            self.assertFalse(r["ok"], p)
        self.assertTrue(camera.grab_frame(srv.url("/same", "u:pw1234@"))["ok"])
        self.assertTrue(camera.grab_frame(srv.url("/away", "u:pw1234@"))["ok"])
        self.assertTrue(any(a for _p, a in srv.seen))
        self.assertEqual([a for _p, a in other.seen], [None])           # the login stayed with the first host

    def test_rtsp_words(self):
        with mock.patch.object(camera, "find_ffmpeg", return_value=None):
            self.assertEqual(camera.grab_frame("rtsp://192.168.1.60/live")["reason"], "no_ffmpeg")
            self.assertIn("full Hub image", camera.grab_frame("rtsp://192.168.1.60/live")["message"])
        cases = {
            "import sys;sys.stdout.buffer.write(%r)" % FRAME: "ok",
            "import sys;sys.stderr.write('401 Unauthorized')": "wrong_login",
            "import sys;sys.stderr.write('Connection to tcp://192.168.1.60:554 failed: Connection refused')": "unreachable",
            "import sys;sys.stderr.write('Error opening input: Connection timed out')": "unreachable",
            "import sys;sys.stderr.write('method DESCRIBE failed: 404 Not Found')": "not_found",
            "import sys;sys.stderr.write('Invalid data found when processing input')": "no_picture",
            "pass": "no_picture",
        }
        for script, reason in cases.items():
            with self.fake_ffmpeg(script):
                self.assertEqual(camera.grab_frame("rtsp://192.168.1.60/live")["reason"], reason, script)

    def test_no_answer_within_the_time_is_nothing_answered(self):
        with self.fake_ffmpeg("import time;time.sleep(30)"):
            self.assertEqual(camera.grab_frame("rtsp://192.168.1.60/live", timeout=0.5)["reason"], "unreachable")

    def test_other_schemes_never_reach_a_fetch(self):
        with mock.patch.object(camera.subprocess, "run") as run, mock.patch.object(camera, "_grab_http") as h:
            for u in ("ftp://x/y", "file:///etc/passwd", "gopher://x"):
                self.assertFalse(camera.grab_frame(u)["ok"])
        run.assert_not_called()
        h.assert_not_called()


class Fleet(unittest.TestCase):
    """Saving a camera address: no printer test, and nothing else changes."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.printers = {}
        self.store = fleet_store.FleetStore(self.d)
        self.mgr = fleet_store.FleetManager(self.store, self.printers, log=lambda *a: None)

    def test_a_camera_only_edit_saves_without_the_printer_test(self):
        self.mgr.add("moonraker", {"name": "Printer-A", "url": "http://127.0.0.1:9"}, skip_test=True)
        before = dict(self.store.get("Printer-A"))
        with mock.patch.object(fleet_store.Family, "probe", side_effect=AssertionError("the printer was probed")):
            res = self.mgr.update("Printer-A", {"camera_url": LOGIN_URL})          # skip_test NOT passed
        self.assertEqual(res["changed"], ["camera_url"])
        self.assertIsNone(res["test"])
        self.assertEqual(self.store.secret("Printer-A", "camera_url"), LOGIN_URL)
        self.assertEqual(res["printer"]["camera_url"], SHOWN_URL)
        # nothing else changed: no watch area appears, no other field moves
        after = dict(self.store.get("Printer-A"))
        for k in set(before) | set(after):
            if k != "updated_at":
                self.assertEqual(before.get(k) or None, after.get(k) or None, k)       # (a blank box and no key are the same thing)
        self.assertIsNone(res["printer"]["detect_area"])

    def test_a_camera_only_edit_keeps_a_watch_area_exactly_as_it_was(self):
        self.mgr.add("moonraker", {"name": "Printer-A", "url": "http://127.0.0.1:9"}, skip_test=True)
        self.mgr.set_detect_area("Printer-A", {"x": 10, "y": 10, "w": 50, "h": 50})
        area = self.mgr.list_public()[0]["detect_area"]
        self.mgr.update("Printer-A", {"camera_url": "http://192.168.1.60/snapshot.jpg"})
        self.assertEqual(self.mgr.list_public()[0]["detect_area"], area)

    def test_the_klipper_box_is_renamed_but_keeps_its_field_name(self):
        t = next(t for t in self.mgr.types() if t["type"] == "moonraker")
        f = next(f for f in t["fields"] if f["name"] == "camera_base_url")
        self.assertEqual(f["label"], "Klipper web page address")
        self.assertIn("Only needed when Mainsail / Fluidd isn't on port 80", f["help"])
        self.assertIn("Not for cameras", f["help"])
        self.assertEqual(f["type"], "url")
        self.assertNotIn("Web UI", json.dumps(t))

    def test_an_rtsp_address_is_still_refused_by_the_klipper_web_page_box(self):
        with self.assertRaises(fleet_store.FleetError):
            self.mgr.add("moonraker", {"name": "Printer-A", "url": "http://127.0.0.1:9",
                                       "camera_base_url": "rtsp://user:password@192.168.1.60:554/stream1"}, skip_test=True)


class Static(unittest.TestCase):
    def test_the_form_markup_and_wording(self):
        for needle in ("PM_CAM_CHIPS", "rtsp://user:password@<camera IP>:554/stream1", "rtsp://<camera IP>/live",
                       "http://<camera IP>/snapshot.jpg", "id=\"pm-camsec\"", "Test camera", "/api/camera/test",
                       "This looks like a camera address.", "Move it to \\u{1F4F7} Camera"):
            self.assertIn(needle, HTML, needle)
        self.assertNotIn("Web UI / camera", HTML)

    def test_the_camera_box_is_not_inside_more_options(self):
        i = HTML.index("function pmForm(")
        form = HTML[i:HTML.index("// What counts as a connection field", i)]
        self.assertIn("camHtml + moreHtml + groupsCard", form)          # Add: the camera card sits above More and Groups (right-hand column); the camera box is never one of More's fields
        self.assertIn("f.name !== 'camera_url'", form)


# ====================================================================== browser

def _schema():
    return json.dumps({"types": [f.describe() for f in fleet_store._make_families().values()]})


DRIVER = r"""
const SCHEMA = %(schema)s;
const SHOT = (() => {      // a mock frame: a real JPEG drawn by the browser itself (a bench, a spool, a nozzle - any picture decodes)
  const c = document.createElement('canvas'); c.width = 640; c.height = 360; const g = c.getContext('2d');
  const gr = g.createLinearGradient(0, 0, 640, 360); gr.addColorStop(0, '#5a6b7c'); gr.addColorStop(1, '#2c3440'); g.fillStyle = gr; g.fillRect(0, 0, 640, 360);
  g.fillStyle = '#c9c2b5'; g.fillRect(120, 250, 400, 14); g.fillStyle = '#e8863a'; g.fillRect(260, 120, 120, 130); g.fillStyle = '#222'; g.fillRect(300, 70, 40, 50);
  g.fillStyle = '#fff'; g.font = '20px sans-serif'; g.fillText('MOCK FRAME', 20, 34);
  return c.toDataURL('image/jpeg', 0.8); })();
const klipper = {name: 'Printer-A', type: 'moonraker', type_label: 'Klipper', address: 'http://192.168.1.60', model: '', driver_available: true, loaded: true,
  secrets: {api_key: 'not set'}, values: {name: 'Printer-A', url: '192.168.1.60', camera_base_url: '', model: '', unload_mm: '', load_mm: ''}, groups: [],
  camera_url: 'rtsp://***@192.168.1.60:554/stream1', detect_area: null};
let camAnswer = {ok: true, reason: 'ok', message: 'Camera picture received.', where: 'rtsp://192.168.1.60:554/stream1', image: SHOT};
window.__stubs.push((u, m, init) => {
  if(u === '/api/fleet') return J([]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: []});
  if(u === '/api/groups') return J([]);
  if(u === '/api/printers') return J({printers: [klipper]});
  if(u.startsWith('/api/printer-types')) return J(SCHEMA);
  if(u === '/api/camera/test') return J(camAnswer, camAnswer.status || 200);
  if(u === '/api/printers/test') return J({result: {ok: false, message: 'x'}});
  if(/^\/api\/printers\/[^/]+$/.test(u) && m === 'PUT') return J({printer: klipper, changed: ['camera_url']});
  return null;
});
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
const vis = e => { if(!e) return false; const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !e.closest('[hidden]'); };
const docW = document.documentElement.clientWidth;
const fit = () => {
  const bad = [...document.querySelectorAll('#pm-camsec, #pm-camsec *, .pm-guard, .pm-guard *')].filter(e => vis(e) && e.getBoundingClientRect().right > docW + 1).map(e => e.tagName + '.' + e.className);
  return {docOver: document.documentElement.scrollWidth - docW, bad};
};
const shape = () => {
  const cam = $('pm-f-camera_url'), sec = $('pm-camsec'), more = $('pm-more-btn'), body = $('pm-more-body');
  const r = sec.getBoundingClientRect();
  return {
    ids: [...document.querySelectorAll('#pm-form input[id^=pm-f-], #pm-form select[id^=pm-f-]')].map(e => e.id.slice(5)),
    camVisible: vis(cam), testVisible: vis($('pm-cam-test')), inMore: !!cam.closest('#pm-more-body'), moreClosed: body.hidden,
    secBeforeMore: !!(sec.compareDocumentPosition(more) & Node.DOCUMENT_POSITION_FOLLOWING),
    chips: [...document.querySelectorAll('.pm-chip')].map(b => [b.textContent, Math.round(b.getBoundingClientRect().height)]),
    heading: sec.querySelector('h3').textContent, top: Math.round(r.top), height: Math.round(r.height), vh: innerHeight, fit: fit()};
};
async function openAdd(t){
  await openPrinters(); await wait(() => $('pr-body'));
  await pmAddStart(); await wait(() => document.querySelector('.pmfam'));
  pmPick(t); await wait(() => $('pm-f-camera_url'));
}
const type = (id, v) => { $(id).value = v; $(id).dispatchEvent(new Event('input')); };

// 1) every printer type: the Camera section is there without opening More options
R.types = {};
for(const t of ['moonraker', 'octoprint', 'prusalink', 'bambu']){ await openAdd(t); R.types[t] = shape(); }

// 2) the chips (Klipper form)
await openAdd('moonraker');
R.chips = [];
for(const b of document.querySelectorAll('.pm-chip')){
  b.click(); const el = $('pm-f-camera_url');
  R.chips.push({label: b.textContent, value: el.value, selected: el.value.slice(el.selectionStart, el.selectionEnd), result: $('pm-cam-result').innerHTML});
}

// 3) the Klipper box is renamed
if($('pm-more-body').hidden) pmMoreToggle();
R.klipperLabel = document.querySelector('label[for=pm-f-camera_base_url]').textContent;
R.klipperHelp = document.querySelector('#pm-f-camera_base_url').closest('.pm-field').querySelector('.pm-help').textContent;
R.klipperInMore = !!$('pm-f-camera_base_url').closest('#pm-more-body');

// 4) paste guard
type('pm-f-camera_url', ''); type('pm-f-camera_base_url', 'http://192.168.1.60');
const guard = id => { const g = document.querySelector('.pm-guard[data-for=' + id + ']'); return g && vis(g) ? g.textContent.replace(/\s+/g, ' ').trim() : null; };
R.guardPlain = guard('pm-f-camera_base_url');
type('pm-f-camera_base_url', 'rtsp://user:password@192.168.1.60:554/stream1');
R.guardRtsp = guard('pm-f-camera_base_url'); R.guardFit = fit();
R.guardShot = {top: Math.round(document.querySelector('.pm-guard[data-for=pm-f-camera_base_url]').getBoundingClientRect().top)};
document.querySelector('.pm-guard-btn[data-for=pm-f-camera_base_url]').click();
R.moved = {camera: $('pm-f-camera_url').value, wrong: $('pm-f-camera_base_url').value, guard: guard('pm-f-camera_base_url')};
type('pm-f-url', 'http://admin:secret99@192.168.1.60');
R.guardLogin = guard('pm-f-url');
type('pm-f-url', '192.168.1.60'); R.guardGone = guard('pm-f-url');
type('pm-f-url', 'rtsp://192.168.1.60/live'); type('pm-f-camera_url', 'http://192.168.1.99/old.jpg');
R.guardReplaces = document.querySelector('.pm-guard-btn[data-for=pm-f-url]').textContent;
type('pm-f-url', '192.168.1.60');

// 5) an unfilled template is caught in words, with no call to the Hub
window.__calls.length = 0;
document.querySelectorAll('.pm-chip')[0].click(); $('pm-cam-test').click(); await sleep(100);
R.unfilled = {text: $('pm-cam-result').textContent, calls: window.__calls.filter(c => c.includes('/api/camera/test')).length};
type('pm-f-camera_url', ''); $('pm-cam-test').click(); await sleep(100);
R.blank = $('pm-cam-result').textContent;

// 6) Test camera on a good address (the Hub's answer is a mock frame)
window.__calls.length = 0;
type('pm-f-camera_url', 'rtsp://user:password@192.168.1.60:554/stream1'); $('pm-cam-test').click();
R.during = {label: $('pm-cam-test').textContent, disabled: $('pm-cam-test').disabled, text: $('pm-cam-result').textContent};
await wait(() => document.querySelector('.pm-campic'));
await wait(() => document.querySelector('.pm-campic').complete);
const pic = document.querySelector('.pm-campic');
R.ok = {text: $('pm-cam-result').textContent, pic: !!pic, loaded: pic.naturalWidth > 0, label: $('pm-cam-test').textContent, disabled: $('pm-cam-test').disabled,
        picBelow: pic.getBoundingClientRect().top >= document.querySelector('.pm-camrow').getBoundingClientRect().bottom - 1,
        body: JSON.parse(window.__calls.filter(c => c.startsWith('POST /api/camera/test')).pop().split(' ').slice(2).join(' ')), fit: fit(), shape: shape()};
// stale answers are dropped, and editing clears the result
type('pm-f-camera_url', 'rtsp://user:password@192.168.1.61:554/stream1'); R.clearedOnEdit = $('pm-cam-result').innerHTML === '';

// 7) the words for each failure
R.fail = {};
for(const [reason, msg] of [['wrong_login', 'Wrong login: the camera refused the user name or password.'], ['unreachable', 'Nothing answered at that address. Check it.'],
                            ['no_picture', 'Camera answered but sent no picture.'], ['no_ffmpeg', 'RTSP needs the full Hub image (it includes ffmpeg).']]){
  camAnswer = {ok: false, reason, message: msg, where: 'x'}; $('pm-cam-test').click();
  await wait(() => $('pm-cam-result').textContent.includes('NO PICTURE'));
  R.fail[reason] = {text: $('pm-cam-result').textContent, pic: !!document.querySelector('.pm-campic'), fit: fit().bad};
  type('pm-f-camera_url', 'rtsp://user:password@192.168.1.6' + reason.length + ':554/stream1');
}
camAnswer = {status: 403, error: 'Testing a camera needs full access.'}; $('pm-cam-test').click();
await wait(() => $('pm-cam-result').textContent.includes('NO PICTURE'));
R.refused = $('pm-cam-result').textContent;
camAnswer = {ok: true, reason: 'ok', message: 'Camera picture received.', where: 'x', image: SHOT};

// 8) Edit printer: the camera is in view, a camera-only change saves with no printer test
await openPrinters(); await wait(() => document.querySelector('.pmrow'));
await pmEdit('Printer-A'); await wait(() => $('pm-save'));
R.edit = {shape: shape(), value: $('pm-f-camera_url').value, save: [$('pm-save').textContent, $('pm-save').disabled]};
window.__calls.length = 0;
$('pm-cam-test').click(); await wait(() => document.querySelector('.pm-campic'));
R.editTest = JSON.parse(window.__calls.filter(c => c.startsWith('POST /api/camera/test')).pop().split(' ').slice(2).join(' '));
type('pm-f-camera_url', 'rtsp://user:password@192.168.1.70:554/stream1');
R.editChanged = [$('pm-save').textContent, $('pm-save').disabled, $('pm-skip').hidden];
$('pm-save').click(); await wait(() => window.__calls.some(c => c.startsWith('PUT ')));
R.put = JSON.parse(window.__calls.filter(c => c.startsWith('PUT')).pop().split(' ').slice(2).join(' '));
R.calls = window.__calls.filter(c => c.includes('/api/printers/test')).length;
"""

class Browser(unittest.TestCase):
    cache = {}
    SIZES = {"phone": (390, 844), "tablet10": (1280, 800), "desktop": (1440, 900)}

    def result(self, size):
        if size not in Browser.cache:
            import test_fix_dashboard as tfd
            import test_layout_5in as t5
            driver = DRIVER % {"schema": _schema()}
            if not tfd.CHROME:
                self.skipTest("no Chrome / Edge found for the browser tests")
            Browser.cache[size] = t5.run_q(self, driver, "", self.SIZES[size], 90000)
        return Browser.cache[size]

    def each_size(self):
        for s in self.SIZES:
            R = self.result(s)
            self.assertNotIn("error", R, f"{s}: {R.get('error')}")
            yield s, R

    def test_camera_section_is_visible_for_every_printer_type_without_more_options(self):
        fams = {f.key: f for f in fleet_store._make_families().values()}
        for s, R in self.each_size():
            for t, sh in R["types"].items():
                basic = [f["name"] for f in fams[t].fields if not f.get("advanced") and f["name"] != "camera_url"]
                self.assertTrue(sh["camVisible"] and sh["testVisible"], (s, t))
                self.assertFalse(sh["inMore"], (s, t))
                self.assertTrue(sh["moreClosed"], (s, t))                       # More options is still shut
                self.assertTrue(sh["secBeforeMore"], (s, t))
                self.assertEqual(sh["ids"][:len(basic) + 1], basic + ["camera_url"], (s, t))   # right after the connection fields
                self.assertNotIn("camera_url", sh["ids"][len(basic) + 1:], (s, t))
                self.assertIn("Camera", sh["heading"])
                self.assertEqual([c[0] for c in sh["chips"]], ["Tapo / Reolink", "Prusa Buddy3D", "JPEG URL"], (s, t))
                self.assertTrue(all(c[1] >= (44 if s == "phone" else 36) for c in sh["chips"]), (s, t, sh["chips"]))      # touch-sized on the phone
                self.assertLessEqual(sh["fit"]["docOver"], 1, (s, t))
                self.assertEqual(sh["fit"]["bad"], [], (s, t))

    def test_chips_fill_a_template_and_select_the_part_to_replace(self):
        for s, R in self.each_size():
            c = R["chips"]
            self.assertEqual([x["value"] for x in c], ["rtsp://user:password@<camera IP>:554/stream1", "rtsp://<camera IP>/live",
                                                       "http://<camera IP>/snapshot.jpg"], s)
            self.assertEqual([x["selected"] for x in c], ["user:password", "<camera IP>", "<camera IP>"], s)

    def test_the_klipper_box_says_it_is_not_for_cameras(self):
        for s, R in self.each_size():
            self.assertTrue(R["klipperLabel"].startswith("Klipper web page address"), s)
            self.assertIn("Only needed when Mainsail / Fluidd isn't on port 80 of the printer's address.", R["klipperHelp"])
            self.assertIn("Not for cameras", R["klipperHelp"])
            self.assertTrue(R["klipperInMore"], s)

    def test_paste_guard_shows_and_moves_the_text(self):
        for s, R in self.each_size():
            self.assertIsNone(R["guardPlain"], s)
            self.assertIn("This looks like a camera address.", R["guardRtsp"])
            self.assertIn("Move it to \U0001F4F7 Camera", R["guardRtsp"])
            self.assertEqual(R["moved"], {"camera": "rtsp://user:password@192.168.1.60:554/stream1", "wrong": "", "guard": None}, s)
            self.assertIn("This looks like a camera address.", R["guardLogin"])           # user:password@ in a web address
            self.assertIsNone(R["guardGone"], s)
            self.assertIn("replaces what is there", R["guardReplaces"])
            self.assertLessEqual(R["guardFit"]["docOver"], 1, s)
            self.assertEqual(R["guardFit"]["bad"], [], s)

    def test_unfilled_templates_and_blank_boxes_are_explained_without_a_call(self):
        for s, R in self.each_size():
            self.assertIn("replace", R["unfilled"]["text"])
            self.assertIn("<camera IP>", R["unfilled"]["text"])
            self.assertEqual(R["unfilled"]["calls"], 0)
            self.assertIn("Type or paste a camera address first", R["blank"])

    def test_test_camera_shows_a_preview_and_the_request_is_right(self):
        for s, R in self.each_size():
            self.assertEqual(R["during"]["label"], "Testing…")
            self.assertTrue(R["during"]["disabled"])
            self.assertIn("TESTING", R["during"]["text"])
            ok = R["ok"]
            self.assertIn("✔ PICTURE", ok["text"])
            self.assertTrue(ok["pic"] and ok["loaded"], s)
            self.assertTrue(ok["picBelow"], s)                                              # the picture sits under the words, never beside them
            self.assertEqual((ok["label"], ok["disabled"]), ("Test camera", False))
            self.assertEqual(ok["body"], {"url": "rtsp://user:password@192.168.1.60:554/stream1"})   # adding: no printer name
            self.assertLessEqual(ok["fit"]["docOver"], 1, s)
            self.assertEqual(ok["fit"]["bad"], [], s)
            self.assertTrue(R["clearedOnEdit"])

    def test_every_failure_is_words_without_a_picture(self):
        for s, R in self.each_size():
            for reason, text in (("wrong_login", "Wrong login"), ("unreachable", "Nothing answered at that address"),
                                 ("no_picture", "Camera answered but sent no picture"), ("no_ffmpeg", "RTSP needs the full Hub image")):
                f = R["fail"][reason]
                self.assertIn(text, f["text"], (s, reason))
                self.assertIn("✖ NO PICTURE", f["text"])
                self.assertFalse(f["pic"])
                self.assertEqual(f["fit"], [])
            self.assertIn("needs full access", R["refused"])

    def test_edit_printer_shows_the_camera_and_a_camera_only_change_saves_without_a_test(self):
        for s, R in self.each_size():
            e = R["edit"]
            self.assertTrue(e["shape"]["camVisible"] and e["shape"]["moreClosed"], s)
            self.assertEqual(e["value"], "rtsp://***@192.168.1.60:554/stream1")             # the login stays hidden
            self.assertEqual(e["save"], ["No changes", True])
            self.assertEqual(R["editTest"], {"printer": "Printer-A", "url": "rtsp://***@192.168.1.60:554/stream1"})
            self.assertEqual(R["editChanged"], ["Save changes", False, True])               # no "(test first)", no skip link
            self.assertEqual(R["put"]["values"]["camera_url"], "rtsp://user:password@192.168.1.70:554/stream1")
            self.assertEqual(R["calls"], 0)                                                  # the printer was never tested


if __name__ == "__main__":
    unittest.main()
