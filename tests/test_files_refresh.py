"""The files list stays current: a visible Refresh button on every layout, a background look when the files open with an
old list, an idle re-check, and the Hub's politeness rules for the automatic looks.

    python3 tests/test_files_refresh.py

The page tests load the real dashboard into headless Chrome / Edge with a stubbed `fetch` (skipped when no browser is found);
the server tests use a fake Moonraker on 127.0.0.1. Nothing contacts a printer. Names are generic.
"""
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="filesrefresh-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)

from test_layout_5in import run_q  # noqa: E402
from test_parity_layouts import PRE  # noqa: E402

with open(os.path.join(APP, "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()

DRIVER = PRE + r"""
const NOW = Math.floor(Date.now() / 1000);
FLEET = [mk('Printer-D', ['Home'], 'IDLE', 0, '', {kind: 'moonraker'}), mk('Printer-E', ['Home'], 'RUNNING', 40, 'Lid.gcode', {kind: 'moonraker'})];
let FILES = [{name: 'Old part.gcode', print_time: '40m', modified: NOW - 86400, size: 1000},
             {name: 'Older part.gcode', print_time: '25m', modified: NOW - 86400 * 3, size: 900}];
let SKIP = false, SLOW = 0;
window.__stubs.push((u, m) => {
  const path = u.split('?')[0]; let mm;
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/files$/))){
    if(/bg=1/.test(u) && SKIP) return J({skipped: true, why: 'the printer is heating'});
    return J(FILES);
  }
  if((mm = path.match(/^\/api\/printer\/([^/]+)$/))){
    const p = FLEET.find(x => x.printer === decodeURIComponent(mm[1])); if(!p) return null;
    return J({snapshot: p, trays: [], camera: false, can_connect: false, kind: p.kind, controls: {actions: [], allowed_now: [], light: ''}, detection: {state: 'no_camera', text: 'No camera'}});
  }
  return null;
});
const vis = e => { if(!e) return false; const b = e.getBoundingClientRect(); const cs = getComputedStyle(e); return b.width > 0 && b.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
const fileCalls = () => window.__calls.filter(c => /\/files/.test(c));
const names = () => [...document.querySelectorAll('#files .frow .fname')].map(e => e.textContent);
const rowsOfTbar = () => { const bar = document.querySelector('#printer .tbar'); return new Set([...bar.children].filter(vis).map(e => { const b = e.getBoundingClientRect(); return Math.round((b.top + b.bottom) / 2 / 14); })).size; };
const btn = () => L5 ? document.querySelector('#files .fhead .frefresh') : document.getElementById('p-frefresh');
const NEW = {name: 'Zed just sent.gcode', print_time: '10m', modified: NOW, size: 500};
await sleep(300); await fleet(); await sleep(150);
openPrinter('Printer-D'); await sleep(600);
if(L5){ filesOpen(); await sleep(200); }
R.open = {calls: fileCalls().slice(), names: names(), label: btn() ? btn().textContent.replace(/\s+/g, ' ').trim() : null, vis: vis(btn()), h: btn() ? Math.round(btn().getBoundingClientRect().height) : 0,
  w: btn() ? Math.round(btn().getBoundingClientRect().width) : 0, tbarRows: rowsOfTbar(), docW: document.documentElement.scrollWidth, vw: innerWidth, btnR: btn() ? Math.round(btn().getBoundingClientRect().right) : 0, moreHasRefresh: [...document.querySelectorAll('button')].some(b => /details/i.test(b.textContent)),
  dupHidden: !document.querySelector('#printer .tbar > button[onclick^="loadFiles"]')};
// the tap: asks with refresh=1 (and no bg), says Refreshing... meanwhile, then shows the new file at the top (Date, newest first)
window.__calls.length = 0; FILES = [NEW].concat(FILES);
btn().click(); R.during = {label: btn().textContent.replace(/\s+/g, ' ').trim(), dis: btn().disabled};
await sleep(300);
R.tap = {calls: fileCalls().slice(), names: names(), label: btn().textContent.replace(/\s+/g, ' ').trim(), dis: btn().disabled, key: FS.key};
// a sort the user chose is kept by the refresh
FS.key = 'name'; filesRender(); window.__calls.length = 0; btn().click(); await sleep(300); R.keepSort = {names: names(), key: FS.key};
FS.key = 'recent'; filesRender();
// open with a list 40 s old: the cached list is on screen at once, ONE background look follows
const reopen = async (ageMs) => {
  show('home'); await sleep(100);
  FS.cache['Printer-D'].at = Date.now() - ageMs;
  window.__calls.length = 0; openPrinter('Printer-D');
  const atOnce = names();
  await sleep(500); if(L5){ filesOpen(); await sleep(300); }
  return {atOnce, calls: fileCalls().slice()};
};
R.stale = await reopen(40000);
R.fresh = await reopen(5000);
// the Hub says "not now" (heating): the list on screen stays, nothing breaks
SKIP = true; FILES = [{name: 'Other.gcode', modified: NOW, size: 1}];
R.skipped = await reopen(40000); R.skippedNames = names();
SKIP = false;
// the explicit button always asks, even right after a skipped look
window.__calls.length = 0; btn().click(); await sleep(300); R.forced = {calls: fileCalls().slice(), names: names()};
// idle re-check every 60 s on a showing, idle screen; never while the printer prints; never when the list is young
FS.at = Date.now() - 5000; window.__calls.length = 0; await sleep(11000); R.timerYoung = fileCalls().slice();
FS.at = Date.now() - 61000; window.__calls.length = 0; await sleep(11000); R.timerOld = fileCalls().slice();
FLEET[0].state = 'RUNNING'; await pview(); await sleep(100); FS.at = Date.now() - 61000; window.__calls.length = 0; await sleep(11000); R.timerBusy = fileCalls().slice();
"""


class PageTests(unittest.TestCase):
    R = {}

    def go(self, key, query, size):
        if key not in PageTests.R:
            PageTests.R[key] = run_q(self, DRIVER, query, size, 240000)
        R = PageTests.R[key]
        self.assertNotIn("error", R, R.get("error"))
        return R

    def check(self, R, layout):
        o = R["open"]
        self.assertEqual(len(o["calls"]), 1, o)                              # the first open reads the list once, plainly
        self.assertNotIn("refresh=1", o["calls"][0])
        self.assertTrue(o["vis"], layout)
        self.assertIn("Refresh", o["label"])                                  # a word label, not only an arrow
        self.assertGreaterEqual(o["h"], 36 if layout == "desktop" else 44, layout)       # 44 px target (a mouse: 36)
        self.assertGreaterEqual(o["w"], 44, layout)
        self.assertLessEqual(o["docW"], o["vw"] + 1, layout)                  # no sideways scroll from the new button
        self.assertLessEqual(o["btnR"], o["vw"], layout)
        self.assertFalse(o["moreHasRefresh"], layout)                         # no duplicate behind More
        self.assertEqual(R["during"]["label"], "Refreshing…")                 # busy word
        self.assertTrue(R["during"]["dis"])
        t = R["tap"]
        self.assertEqual(len(t["calls"]), 1, t)
        self.assertIn("refresh=1", t["calls"][0])
        self.assertNotIn("bg=1", t["calls"][0])                               # a tap is never the polite background kind
        self.assertEqual(t["names"][0], "Zed just sent")                    # updated list, newest first
        self.assertIn("Refresh", t["label"])
        self.assertFalse(t["dis"])
        self.assertEqual(R["keepSort"]["key"], "name")                        # the chosen sort survives a refresh
        self.assertEqual(R["keepSort"]["names"][-1], "Zed just sent")   # sorted by name: the new file sits last, not first
        # open with an old list: cached list at once, exactly one background look
        s = R["stale"]
        self.assertEqual(len(s["atOnce"]), 3, layout)                         # the cached list is on screen before any answer
        self.assertEqual(len(s["calls"]), 1, s)
        self.assertIn("refresh=1", s["calls"][0])
        self.assertIn("bg=1", s["calls"][0])
        self.assertEqual(R["fresh"]["calls"], [], layout)                     # a 5 s old list is not asked again
        # the Hub declines (heating): one look was made, the list stays
        self.assertEqual(len(R["skipped"]["calls"]), 1)
        self.assertEqual(R["skippedNames"][0], "Zed just sent")
        self.assertEqual(len(R["forced"]["calls"]), 1)                        # the button is not stopped by an earlier skip
        self.assertNotIn("bg=1", R["forced"]["calls"][0])
        self.assertEqual(R["forced"]["names"][0], "Other")
        # idle timer
        self.assertEqual(R["timerYoung"], [], layout)
        self.assertEqual(len(R["timerOld"]), 1, layout)
        self.assertIn("bg=1", R["timerOld"][0])
        self.assertEqual(R["timerBusy"], [], layout)

    def test_desktop_and_kiosk_10in(self):
        R = self.go("desk", "?group=Home", (1024, 600))
        self.check(R, "10in")
        self.assertEqual(R["open"]["tbarRows"], 1)                            # still ONE toolbar row

    def test_wide_desktop(self):
        self.check(self.go("wide", "?group=Home", (1440, 900)), "desktop")

    def test_phone(self):
        R = self.go("phone", "?group=Home", (390, 800))
        self.check(R, "phone")
        self.assertLessEqual(R["open"]["tbarRows"], 3)                        # a phone: title row, the sort buttons, then Grid / List + Refresh + More (a wrap, never a clipped button)

    def test_five_inch_files_sheet(self):
        self.check(self.go("l5", "?group=Home&layout=5in", (800, 480)), "5in")


class StaticTests(unittest.TestCase):
    def test_the_slow_entries_are_gone_from_both_more_places(self):
        for needle in ("Refresh details (slow)", 'data-act="pmRefresh"', "Refresh (slow)"):
            self.assertNotIn(needle, HTML)
        self.assertIn('id="p-frefresh" data-act="filesRefresh"', HTML)
        self.assertIn('class="frefresh" data-act="filesRefresh"', HTML)
        self.assertIn("filesRefresh: () => filesRefresh(true)", HTML)

    def test_the_button_is_not_a_control_so_a_view_only_sign_in_still_has_it(self):
        self.assertNotRegex(HTML, r'<button[^>]*id="p-frefresh"[^>]*data-ctl')
        self.assertNotRegex(HTML, r'<button[^>]*class="frefresh"[^>]*data-ctl')

    def test_the_automatic_looks_are_marked_background_and_the_button_is_not(self):
        self.assertIn("'?refresh=1' + (manual ? '' : '&bg=1')", HTML)
        self.assertIn("filesRefresh(false)", HTML)

    def test_no_idle_recheck_for_bambu_or_a_printing_printer(self):
        i = HTML.index("window._kind === 'bambu'")
        self.assertIn("BUSY_STATES.includes(FS.pstate)", HTML[i - 200:i + 200])


try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception:       # pragma: no cover
    HAVE = False


@unittest.skipUnless(HAVE, "needs the app requirements")
class ServerPolitenessTests(unittest.TestCase):
    NAME = "Klip"

    def setUp(self):
        from test_safe_connect import Fake, moon_routes
        from moonraker import MoonrakerPrinter
        import controls
        from unittest import mock
        self.moon_routes, self.controls = moon_routes, controls
        self.fake = Fake(moon_routes(files=("a.gcode",)))
        self.addCleanup(self.fake.close)
        self.prn = MoonrakerPrinter(self.NAME, self.fake.url)
        self.prn._cam_probed = __import__("time").time()
        server.PRINTERS[self.NAME.lower()] = self.prn
        self.addCleanup(server.PRINTERS.pop, self.NAME.lower(), None)
        server.SETTINGS.update({"security.api_auth": "off"})
        self.client = TestClient(server.mcp.streamable_http_app())
        controls.reset_for_tests()
        p = mock.patch.object(server, "_prewarm")
        p.start()
        self.addCleanup(p.stop)

    def routes(self, **kw):
        self.fake.routes = self.moon_routes(files=("a.gcode",), **kw)
        self.fake.log.clear()
        self.prn.refresh()

    def listed(self):
        return [p for m, p in self.fake.log if p == "/server/files/list"]

    def test_the_button_always_lists_even_while_heating(self):
        self.routes(bed_target=80.0)
        r = self.client.get(f"/api/printer/{self.NAME}/files?refresh=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([f["name"] for f in r.json()], ["a.gcode"])
        self.assertEqual(len(self.listed()), 1)

    def test_a_background_look_is_skipped_while_heating(self):
        self.routes(bed_target=80.0)
        r = self.client.get(f"/api/printer/{self.NAME}/files?refresh=1&bg=1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["skipped"])
        self.assertIn("heating", r.json()["why"])
        self.assertEqual(self.listed(), [])                                    # not even the file list was read

    def test_a_background_look_is_skipped_right_after_a_control_action(self):
        self.routes()
        self.controls.note_control(self.prn)
        r = self.client.get(f"/api/printer/{self.NAME}/files?bg=1")
        self.assertTrue(r.json()["skipped"])
        self.assertEqual(self.listed(), [])

    def test_a_background_look_on_a_calm_printer_lists(self):
        self.routes()
        r = self.client.get(f"/api/printer/{self.NAME}/files?refresh=1&bg=1")
        self.assertEqual([f["name"] for f in r.json()], ["a.gcode"])
        self.assertEqual(len(self.listed()), 1)

    def test_a_background_look_at_an_unreachable_printer_is_skipped_not_a_503(self):
        from test_safe_connect import free_port_url
        self.prn.url = free_port_url()
        self.prn.refresh()
        r = self.client.get(f"/api/printer/{self.NAME}/files?bg=1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["skipped"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
