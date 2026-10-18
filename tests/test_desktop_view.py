"""Tests for the view choice (Desktop | 10-inch touch | Phone | 5-inch wall) and the Desktop layout / scaled-to-fill touch layout.

    python3 tests/test_desktop_view.py

* the decision function `fvDecide` is run on its own in node: a matrix of user agent / pointer / touch / size -> the expected view,
  URL overrides, and what "Switch view" remembered;
* the browser tests load the real page into headless Chrome / Edge with a stubbed `fetch` (skipped when none is found): the Desktop layout at 1440 and 1920
  (no 1024 box, many fleet columns, no sideways scroll, one toolbar row), a wide fallback font (the Linux kiosks' Chromium), the URL overrides, scaling to fill
  and that Switch view survives a reload. They run with Chrome told that there is no pointer and no hover (NO_POINTER: what GitHub Actions' headless Chrome
  on Linux reports), so a CI run and a run on a Mac behave the same. The wall-screen layouts (kiosk, 5-inch, phone) have their own test files and are unchanged. Names are generic.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_layout_5in import NO_POINTER, run_q  # noqa: E402
from test_parity_layouts import HIST_DRIVER, PRE, WIDE  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()

NODE = shutil.which("node")
A = HTML.index("const FV_KEY")
FV_JS = HTML[A:HTML.index("const FVIEW = ")]

IPAD_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
IPAD_OLD = "Mozilla/5.0 (iPad; CPU OS 12_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/12.0 Mobile/15E148 Safari/604.1"
WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
PHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
LINUX_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/120.0 Safari/537.36"

MOUSE = dict(fine=True, hover=True, anyFine=True, touch=0)
FINGER = dict(fine=False, hover=False, anyFine=False, touch=5)
NONE = dict(fine=False, hover=False, anyFine=False, touch=0)       # headless Chrome on Linux (GitHub Actions)

# (label, env, view, fill, kiosk)
MATRIX = [
    ("Mac/Windows desktop 1440", dict(ua=WIN_UA, w=1440, h=900, **MOUSE), "desktop", False, False),
    ("desktop 1920x1080", dict(ua=WIN_UA, w=1920, h=1080, **MOUSE), "desktop", False, False),
    ("desktop exactly 1100", dict(ua=WIN_UA, w=1100, h=800, **MOUSE), "desktop", False, False),
    ("desktop 1099 wide: the 10-inch box, as before", dict(ua=WIN_UA, w=1099, h=800, **MOUSE), "touch", False, False),
    ("a 1024x600 mouse window: the box, as before", dict(ua=LINUX_UA, w=1024, h=600, **MOUSE), "touch", False, False),
    ("Mac Safari desktop (UA Macintosh, no touch)", dict(ua=IPAD_UA, w=1512, h=900, **MOUSE), "desktop", False, False),
    ("Windows touch laptop (any-pointer fine + touch)", dict(ua=WIN_UA, w=1536, h=860, fine=True, hover=True, anyFine=True, touch=10), "desktop", False, False),
    ("touch laptop whose primary pointer is the screen", dict(ua=WIN_UA, w=1536, h=860, fine=False, hover=False, anyFine=True, touch=10), "desktop", False, False),
    ("CI headless Chrome on Linux: pointer none, hover none, no touch, 1920 wide", dict(ua=LINUX_UA, w=1920, h=1080, **NONE), "desktop", False, False),
    ("pointer none, hover none, no touch, exactly 1100", dict(ua=LINUX_UA, w=1100, h=800, **NONE), "desktop", False, False),
    ("pointer none, hover none, no touch, 1099: the box", dict(ua=LINUX_UA, w=1099, h=800, **NONE), "touch", False, False),
    ("pointer none + touch points 5, 1920 wide", dict(ua=LINUX_UA, w=1920, h=1080, fine=False, hover=False, anyFine=False, touch=5), "touch", True, False),
    ("pointer none, no touch points but ontouchstart", dict(ua=LINUX_UA, w=1920, h=1080, ontouch=True, **NONE), "touch", False, False),
    ("pointer none, no touch points but a coarse pointer", dict(ua=LINUX_UA, w=1920, h=1080, anyCoarse=True, **NONE), "touch", False, False),
    ("iPad landscape 1180x820", dict(ua=IPAD_UA, w=1180, h=820, **FINGER), "touch", True, False),
    ("iPad portrait 820x1180", dict(ua=IPAD_UA, w=820, h=1180, **FINGER), "touch", True, False),
    ("iPad Pro 12.9 landscape 1366x1024", dict(ua=IPAD_UA, w=1366, h=1024, **FINGER), "touch", True, False),
    ("iPad with a trackpad (any-pointer fine)", dict(ua=IPAD_UA, w=1366, h=1024, fine=False, hover=False, anyFine=True, touch=5), "touch", True, False),
    ("older iPad UA", dict(ua=IPAD_OLD, w=1024, h=768, **FINGER), "touch", True, False),
    ("Android tablet 1280x800", dict(ua=LINUX_UA, w=1280, h=800, **FINGER), "touch", True, False),
    ("phone portrait", dict(ua=PHONE_UA, w=390, h=844, **FINGER), "phone", False, False),
    ("phone landscape (844x390)", dict(ua=PHONE_UA, w=844, h=390, **FINGER), "phone", False, False),
    ("a narrow desktop window", dict(ua=WIN_UA, w=700, h=900, **MOUSE), "phone", False, False),
    ("iPad in a narrow Split View", dict(ua=IPAD_UA, w=507, h=1024, **FINGER), "phone", False, False),
    # the URL wins
    ("layout=5in", dict(ua=LINUX_UA, search="?layout=5in", w=800, h=480, **FINGER), "5in", False, True),
    ("layout=5in on a desktop", dict(ua=WIN_UA, search="?layout=5in", w=1440, h=900, **MOUSE), "5in", False, True),
    ("kiosk=1 on a 1024x600 wall screen", dict(ua=LINUX_UA, search="?group=Work&kiosk=1", w=1024, h=600, **FINGER), "touch", False, True),
    ("kiosk=1 in a big window stays the unscaled box", dict(ua=LINUX_UA, search="?kiosk=1", w=1920, h=1080, **MOUSE), "touch", False, True),
    ("view=kiosk scales to fill and is a kiosk", dict(ua=WIN_UA, search="?view=kiosk", w=1440, h=900, **MOUSE), "touch", True, True),
    ("view=touch scales to fill, has a keyboard", dict(ua=WIN_UA, search="?view=touch", w=1440, h=900, **MOUSE), "touch", True, False),
    ("view=desktop on an iPad", dict(ua=IPAD_UA, search="?view=desktop", w=1180, h=820, **FINGER), "desktop", False, False),
    ("view=5in", dict(ua=WIN_UA, search="?view=5in", w=1440, h=900, **MOUSE), "5in", False, True),
    ("view=phone on a phone", dict(ua=PHONE_UA, search="?view=phone", w=390, h=844, **FINGER), "phone", False, False),
    ("view=phone in a wide window cannot work: ignored", dict(ua=WIN_UA, search="?view=phone", w=1440, h=900, **MOUSE), "desktop", False, False),
    ("view=desktop in a narrow window cannot work: ignored", dict(ua=PHONE_UA, search="?view=desktop", w=390, h=844, **FINGER), "phone", False, False),
    ("a bad view= is ignored", dict(ua=WIN_UA, search="?view=banana", w=1440, h=900, **MOUSE), "desktop", False, False),
    ("kiosk=1 beats a remembered choice", dict(ua=LINUX_UA, search="?kiosk=1", stored="desktop", w=1024, h=600, **FINGER), "touch", False, True),
    ("view= beats a remembered choice", dict(ua=WIN_UA, search="?view=touch", stored="desktop", w=1440, h=900, **MOUSE), "touch", True, False),
    # what Switch view remembered
    ("remembered: desktop on an iPad", dict(ua=IPAD_UA, stored="desktop", w=1180, h=820, **FINGER), "desktop", False, False),
    ("remembered: 10-inch touch on a desktop", dict(ua=WIN_UA, stored="touch", w=1440, h=900, **MOUSE), "touch", True, False),
    ("remembered: 5-inch", dict(ua=WIN_UA, stored="5in", w=1440, h=900, **MOUSE), "5in", False, True),
    ("remembered: phone on a phone", dict(ua=PHONE_UA, stored="phone", w=390, h=844, **FINGER), "phone", False, False),
    ("remembered phone in a wide window: ignored", dict(ua=WIN_UA, stored="phone", w=1440, h=900, **MOUSE), "desktop", False, False),
    ("remembered junk is ignored", dict(ua=WIN_UA, stored="mainframe", w=1440, h=900, **MOUSE), "desktop", False, False),
]


def decide(envs):
    code = FV_JS + "\nconsole.log(JSON.stringify(" + json.dumps(envs) + ".map(e => fvDecide(e))));"
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@unittest.skipUnless(NODE, "no node")
class DetectionMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = decide([{"search": "", "stored": "", **e} for _, e, *_ in MATRIX])

    def test_the_matrix(self):
        for (label, env, view, fill, kiosk), got in zip(MATRIX, self.out):
            self.assertEqual((got["view"], got["fill"], got["kiosk"]), (view, fill, kiosk), label + " -> " + got["why"])

    def test_every_answer_says_why(self):
        for (label, *_), got in zip(MATRIX, self.out):
            self.assertTrue(got["why"], label)
        self.assertTrue(all(g["auto"] == g["why"].startswith("detected") for g in self.out))

    def test_the_wall_screens_never_scale_and_never_change(self):
        for (label, env, view, fill, kiosk), got in zip(MATRIX, self.out):
            if "kiosk=1" in env.get("search", "") or "layout=5in" in env.get("search", ""):
                self.assertFalse(got["fill"], label)
                self.assertTrue(got["kiosk"], label)
        # a remembered choice never beats the wall screens' own URLs, a wall screen cannot become Desktop
        got = decide([dict(ua=LINUX_UA, search="?kiosk=1", stored="desktop", w=1024, h=600, **FINGER),
                      dict(ua=LINUX_UA, search="?layout=5in", stored="desktop", w=800, h=480, **FINGER)])
        self.assertEqual([g["view"] for g in got], ["touch", "5in"])

    def test_the_decision_is_pure_and_survives_missing_input(self):
        got = decide([{}, {"search": None}, {"w": 1440, "h": 900}])
        self.assertTrue(all(g["view"] in ("desktop", "touch", "phone", "5in") for g in got))

    def test_the_5_inch_size_is_never_guessed(self):
        for w, h in ((800, 480), (854, 480), (480, 800)):
            got = decide([dict(ua=LINUX_UA, search="", stored="", w=w, h=h, **FINGER), dict(ua=LINUX_UA, search="", stored="", w=w, h=h, **MOUSE)])
            self.assertNotIn("5in", [g["view"] for g in got], (w, h))


class StaticTests(unittest.TestCase):
    def test_one_function_decides_and_it_is_read_once_in_the_head(self):
        self.assertEqual(HTML.count("const FVIEW = fvDecide(fvEnv());"), 1)
        self.assertLess(HTML.index("const FVIEW = "), HTML.index("<body>"))
        self.assertLess(HTML.index("const FVIEW = "), HTML.index("const L5 = "))

    def test_every_desktop_rule_is_scoped_so_the_wall_screens_and_phone_cannot_see_it(self):
        css = HTML[HTML.index("/* ==== desktop layout (html[data-view"):]
        css = css[:css.index("</style>")]
        body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        n = 0
        for sel, _ in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            sel = sel.strip()
            if sel.startswith("@media"):
                sel = sel.split("{")[-1].strip() if "{" in sel else ""
            for one in sel.split(","):
                one = one.strip()
                if not one or one.startswith("@media"):
                    continue
                n += 1
                self.assertTrue(one.startswith('html[data-view="desktop"]') or one.startswith("html[data-fill]") or one.startswith('html[data-view="touch"]:not([data-kiosk])')
                                or one == ".moreview" or one == ".moreview#more-viewnow", "not scoped: " + one)
        self.assertGreater(n, 80)

    def test_the_desktop_media_queries_are_width_only_for_layout(self):
        css = HTML[HTML.index("/* ==== desktop layout (html[data-view"):]
        css = css[:css.index("</style>")]
        for m in re.findall(r"@media[^{]*\{", css):
            self.assertRegex(m, r"min-width:\d+px", m)          # nothing here may match a 1024x600 wall screen or a phone: only wider windows

    def test_switch_view_is_in_the_more_menu_and_about(self):
        self.assertIn('data-act="viewOpen">Switch view', HTML)
        self.assertIn("Automatic", HTML)
        self.assertIn('id="st-about-view"', HTML)
        self.assertIn("viewSet:   b => fvSwitch(b.dataset.v),", HTML)
        # the wall screens' header and More menu are untouched: the new entries only show on desktop / a keyboard-capable touch layout
        self.assertIn(".moreview { display:none; }", HTML)


PRINTER_STUBS = r"""
window.__stubs.push((u, m) => {
  const path = u.split('?')[0];
  if(path === '/api/printer/Printer-A') return J({snapshot: {printer: 'Printer-A', connected: true, state: 'RUNNING', job: 'Bracket x12', percent: 42, layer: 5, nozzle_c: 220, bed_c: 60, nozzle_target_c: 220, bed_target_c: 60},
    trays: [{tray: 0, slot_1based: 1, type: 'PLA', color_name: 'GREEN'}, {tray: 1, slot_1based: 2, type: 'PETG', color_name: 'BLACK'}], camera: true, can_connect: false, kind: 'bambu', controls: {actions: []}});
  if(path === '/api/printer/Printer-A/files') return J(Array.from({length: 24}, (_, i) => ({name: 'Part ' + i + '.gcode.3mf', print_time: '1h 20m', weight_g: 34, filaments: [{color_name: 'GREEN', type: 'PLA'}]})));
  return null;
});
"""

DESK_DRIVER = PRE + PRINTER_STUBS + r"""
try{ localStorage.setItem('fabrica.filesview', 'grid'); }catch(e){}
FLEET = Array.from({length: 8}, (_, i) => mk('Printer-' + 'ABCDEFGH'[i], ['Home'], ['RUNNING', 'PAUSE', 'IDLE', 'FINISH', 'UNKNOWN'][i % 5], i * 11, i % 5 === 2 ? '' : 'Job number ' + i + '.gcode', {camera: i % 2 === 0}));
const vis = e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && getComputedStyle(e).display !== 'none'; };
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
const page = () => ({dataView: document.documentElement.getAttribute('data-view'), fill: document.documentElement.hasAttribute('data-fill'), cls: document.documentElement.className, vw: innerWidth, vh: innerHeight,
  docW: document.documentElement.scrollWidth, bodyW: Math.round(document.body.getBoundingClientRect().width), bodyH: Math.round(document.body.getBoundingClientRect().height), kiosk: KIOSK, l5: L5, fview: FVIEW.view, why: FVIEW.why});
await sleep(300); await setFleet(FLEET);
const cards = () => [...document.querySelectorAll('#fleet .pcard')];
R.fleet = Object.assign(page(), {cols: new Set(cards().map(c => r(c).l)).size, n: cards().length, heights: new Set(cards().map(c => r(c).h)).size, widths: new Set(cards().map(c => r(c).w)).size,
  overflow: cards().map(c => [...c.querySelectorAll('.eta, .meta, .spg, .job')].filter(e => e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).display !== '-webkit-box').map(e => e.className)).flat(),
  header: [...document.querySelectorAll('#home > .row:first-child > *')].filter(vis).map(e => Math.round(r(e).t / 20)).filter((v, i, a) => a.indexOf(v) === i).length,
  more: vis(document.getElementById('morebtn')), hdrH: Math.min(...[...document.querySelectorAll('#home > .row:first-child > button')].filter(vis).map(b => r(b).h))});
// a mouse hovering a name that is cut short gets the whole text as a tooltip
const t = document.createElement('div'); t.id = 'tt'; t.style.cssText = 'position:fixed;left:0;top:0;width:60px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis'; t.textContent = 'A very long file name that cannot fit.gcode.3mf'; document.body.appendChild(t);
t.dispatchEvent(new MouseEvent('mouseover', {bubbles: true})); R.tooltip = t.title; t.remove();
R.canRename = typeof rnOpen === 'function' && !KIOSK;
// the printer screen
openPrinter('Printer-A'); await wait(() => document.querySelector('#files .ftile, #files .frow'), 80); await sleep(400);
const tb = [...document.querySelectorAll('#printer .tbar > *')].filter(vis);
const cam = document.getElementById('cam'), prim = document.getElementById('p-prim'), files = document.getElementById('files');
R.printer = Object.assign(page(), {toolbarSpan: Math.max(...tb.map(e => r(e).t)) - Math.min(...tb.map(e => r(e).t)), toolbarH: r(document.querySelector('#printer .tbar')).h, toolbarN: tb.length,
  cam: r(cam), camOn: cam.classList.contains('on'), prim: r(prim), files: r(files), strip: document.getElementById('printer').classList.contains('strip'), tiles: document.querySelectorAll('#files .ftile').length,
  tileCols: new Set([...document.querySelectorAll('#files .ftile')].map(e => r(e).l)).size, camAspect: r(cam).w / r(cam).h, minBtn: Math.min(...tb.filter(e => e.tagName === 'BUTTON').map(e => r(e).h))});
"""

HIST_PART = HIST_DRIVER.split("R.jobs = snap();")[0] + r"""
R.page = {dataView: document.documentElement.getAttribute('data-view'), docW: document.documentElement.scrollWidth, vw: innerWidth,
  cols: (() => { const w = document.querySelector('.hi-wide'); return w ? new Set([...w.querySelectorAll('.ht')].map(e => r(e).l)).size : 0; })(),
  tables: [...document.querySelectorAll('#hi-body .ht')].map(e => ({l: r(e).l, w: r(e).w, t: r(e).t})), recentCols: (() => { const row = document.querySelector('#hi-body .tr.click'); return row ? getComputedStyle(row).gridTemplateColumns.split(' ').length : 0; })(),
  headings: [...document.querySelectorAll('#hi-body h2')].map(h => h.textContent.replace(/\s+/g, ' ').trim().slice(0, 20)), toolbarTops: new Set([...document.querySelectorAll('#hi-head > h1, #hi-ctl > *')].filter(vis).map(e => Math.round(r(e).t / 20))).size};
"""


def ok(testcase, R):
    testcase.assertNotIn("error", R, R.get("error"))
    return R


class DesktopBrowserTests(unittest.TestCase):
    R = {}

    def go(self, key, driver, query, size, budget=150000):
        if key not in DesktopBrowserTests.R:
            DesktopBrowserTests.R[key] = run_q(self, driver, query, size, budget, blink_settings=NO_POINTER)
        return ok(self, DesktopBrowserTests.R[key])

    def d1440(self):
        return self.go("d1440", DESK_DRIVER, "", (1440, 900))

    def d1920(self):
        return self.go("d1920", DESK_DRIVER, "", (1920, 1080))

    def wide(self):
        return self.go("wide", DESK_DRIVER.replace("await sleep(300); await setFleet(FLEET);", WIDE + "await sleep(300); await setFleet(FLEET);", 1), "", (1440, 900))

    def test_a_big_window_with_a_mouse_is_the_desktop_layout_not_the_1024_box(self):
        for R in (self.d1440(), self.d1920()):
            F = R["fleet"]
            self.assertEqual((F["dataView"], F["fview"], F["kiosk"], F["l5"], F["cls"]), ("desktop", "desktop", False, False, ""))
            self.assertEqual(F["bodyW"], F["vw"])                     # the whole window, not a 1024 box
            self.assertEqual(F["bodyH"], F["vh"])
            self.assertLessEqual(F["docW"], F["vw"])
            self.assertIn("detected", F["why"])

    def test_the_fleet_has_many_equal_cards_per_row(self):
        for R, least in ((self.d1440(), 4), (self.d1920(), 5)):
            F = R["fleet"]
            self.assertGreaterEqual(F["cols"], least, F)
            self.assertGreater(F["cols"], 3)
            self.assertEqual((F["heights"], F["widths"]), (1, 1), F)             # the same card geography whatever the content
            self.assertEqual(F["n"], 8)
            self.assertEqual(F["overflow"], [])

    def test_the_header_is_one_row_with_mouse_sized_buttons_and_a_more_menu(self):
        for R in (self.d1440(), self.d1920()):
            F = R["fleet"]
            self.assertEqual(F["header"], 1, F)
            self.assertTrue(F["more"])
            self.assertGreaterEqual(F["hdrH"], 32)
            self.assertLessEqual(F["hdrH"], 40)

    def test_keyboard_features_are_available_and_cut_off_names_get_a_tooltip(self):
        R = self.d1440()
        self.assertTrue(R["canRename"])
        self.assertIn("A very long file name", R["tooltip"])

    def test_the_printer_screen_has_a_big_camera_beside_the_progress_with_the_files_below(self):
        for R in (self.d1440(), self.d1920()):
            P = R["printer"]
            self.assertTrue(P["camOn"], P)
            self.assertGreater(P["cam"]["w"], 560, P)
            self.assertGreater(P["cam"]["h"], 300, P)
            self.assertLess(P["cam"]["r"], P["prim"]["l"] + 1, P)                 # the camera is left of the progress card ...
            self.assertLess(abs(P["cam"]["t"] - P["prim"]["t"]), 4, P)            # ... on the same band
            self.assertGreater(P["files"]["t"], P["cam"]["b"] - 2, P)             # the files are below it
            self.assertFalse(P["strip"], P)
            self.assertLessEqual(P["docW"], P["vw"])
            self.assertAlmostEqual(P["camAspect"], 16 / 9, delta=0.2)

    def test_one_toolbar_row_on_the_printer_screen(self):
        for R in (self.d1440(), self.d1920(), self.wide()):
            P = R["printer"]
            self.assertLess(P["toolbarSpan"], 14, P)                           # every control on one line
            self.assertLess(P["toolbarH"], 60, P)
            self.assertGreaterEqual(P["minBtn"], 32, P)

    def test_the_picture_grid_fills_the_width_with_more_columns(self):
        for R, least in ((self.d1440(), 6), (self.d1920(), 8)):
            P = R["printer"]
            self.assertGreaterEqual(P["tileCols"], least, P)
            self.assertGreater(P["tiles"], 8)

    def test_the_history_tables_sit_side_by_side_without_sideways_scroll(self):
        for key, size, cols in (("h1440", (1440, 900), 2), ("h1920", (1920, 1080), 3)):
            R = self.go(key, HIST_PART, "", size)
            H = R["page"]
            self.assertEqual(H["dataView"], "desktop")
            self.assertLessEqual(H["docW"], H["vw"], H)
            self.assertEqual(H["cols"], cols, H)                                # Recent prints | By printer | By file (two columns when it is not wide enough)
            self.assertEqual(H["headings"][0][:6], "Recent")
            self.assertEqual(H["toolbarTops"], 1, H)
            self.assertEqual(H["recentCols"], 8, H)                            # the wide table keeps every column

    def test_a_wide_fallback_font_changes_nothing_important(self):
        R = self.wide()
        F, P = R["fleet"], R["printer"]
        self.assertGreater(F["cols"], 3)
        self.assertEqual((F["heights"], F["widths"]), (1, 1))
        self.assertEqual(F["overflow"], [])
        self.assertLessEqual(F["docW"], F["vw"])
        self.assertLessEqual(P["docW"], P["vw"])
        self.assertEqual(F["header"], 1)


OVERRIDE_DRIVER = PRE + r"""
await sleep(300); await setFleet(FLEET);
const b = document.body.getBoundingClientRect(), cs = getComputedStyle(document.body);
R.p = {dataView: document.documentElement.getAttribute('data-view'), fill: document.documentElement.hasAttribute('data-fill'), cls: document.documentElement.className, kioskAttr: document.documentElement.hasAttribute('data-kiosk'),
  vw: innerWidth, vh: innerHeight, bodyW: cs.width, bodyH: cs.height, transform: cs.transform, kiosk: KIOSK, l5: L5, view: FVIEW.view, why: FVIEW.why, docW: document.documentElement.scrollWidth,
  cols: new Set([...document.querySelectorAll('#fleet .pcard')].map(c => Math.round(c.getBoundingClientRect().left))).size,
  more: getComputedStyle(document.getElementById('morebtn')).display, backups: getComputedStyle(document.querySelector('#home > .row:first-child > button[onclick^="openBackups"]')).display,
  screenH: Math.round(document.querySelector('.screen.on').getBoundingClientRect().height)};
"""


class OverrideTests(unittest.TestCase):
    R = {}

    def go(self, query, size):
        key = (query, size)
        if key not in OverrideTests.R:
            OverrideTests.R[key] = run_q(self, OVERRIDE_DRIVER, query, size, 120000, blink_settings=NO_POINTER)
        return ok(self, OverrideTests.R[key])["p"]

    def test_the_wall_screens_are_exactly_what_they_were(self):
        p = self.go("?kiosk=1", (1024, 600))
        self.assertEqual((p["dataView"], p["fill"], p["kiosk"], p["l5"], p["cls"]), ("touch", False, True, False, ""))
        self.assertEqual((p["bodyW"], p["bodyH"], p["transform"], p["more"]), ("1024px", "600px", "none", "none"))
        p = self.go("?kiosk=1", (1440, 900))                               # a kiosk URL in a big window is still the plain box
        self.assertEqual((p["dataView"], p["fill"], p["bodyW"], p["bodyH"], p["transform"]), ("touch", False, "1024px", "600px", "none"))
        p = self.go("?layout=5in", (800, 480))
        self.assertEqual((p["dataView"], p["cls"], p["kiosk"], p["l5"], p["fill"]), ("5in", "lay-5in", True, True, False))
        p = self.go("", (390, 844))
        self.assertEqual((p["dataView"], p["fill"], p["kiosk"], p["cls"]), ("phone", False, False, ""))
        self.assertEqual(p["cols"], 1)

    def test_view_kiosk_and_view_touch_scale_the_1024_layout_to_fill(self):
        p = self.go("?view=touch", (1180, 820))
        self.assertTrue(p["fill"])
        self.assertEqual(p["bodyW"], "1024px")
        self.assertTrue(p["transform"].startswith("matrix(1.15"), p["transform"])
        self.assertAlmostEqual(p["screenH"], 820, delta=2)          # the screen (measured after the scale) is as tall as the window
        self.assertLessEqual(p["docW"], p["vw"])
        self.assertFalse(p["kiosk"])                     # an iPad has a keyboard on screen: Rename etc. stay
        self.assertEqual(p["more"], "block")             # and it gets the More menu (Switch view)
        p = self.go("?view=kiosk", (1180, 820))
        self.assertTrue(p["kiosk"] and p["fill"])
        self.assertEqual(p["more"], "none")              # no keyboard: no More menu
        p = self.go("?view=touch", (820, 1180))          # portrait: scaled down to the width, the page is taller than 600
        self.assertTrue(p["fill"])
        self.assertAlmostEqual(p["screenH"], 1180, delta=3)
        p = self.go("?view=kiosk", (1024, 600))          # an exact 1024x600 window needs no scaling at all
        self.assertEqual((p["fill"], p["transform"]), (False, "none"))

    def test_view_desktop_and_view_5in(self):
        p = self.go("?view=desktop", (1180, 820))
        self.assertEqual((p["dataView"], p["fill"], p["kiosk"], p["more"]), ("desktop", False, False, "block"))
        self.assertGreaterEqual(p["cols"], 3)
        self.assertLessEqual(p["docW"], p["vw"])
        p = self.go("?view=5in", (800, 480))
        self.assertEqual((p["dataView"], p["cls"], p["kiosk"], p["l5"]), ("5in", "lay-5in", True, True))

    def test_a_view_that_cannot_work_in_the_window_is_ignored_and_says_so(self):
        p = self.go("?view=phone", (1440, 900))
        self.assertEqual(p["dataView"], "desktop")
        self.assertIn("ignored", p["why"])

    def test_the_header_of_a_touch_layout_with_a_keyboard_swaps_backups_for_more(self):
        p = self.go("?view=touch", (1180, 820))
        self.assertEqual((p["more"], p["backups"]), ("block", "none"))
        p = self.go("?kiosk=1", (1024, 600))
        self.assertEqual((p["more"], p["backups"]), ("none", "block"))


PERSIST_DRIVER = PRE + r"""
await sleep(300);
if(!sessionStorage.getItem('ph2')){
  sessionStorage.setItem('ph2', '1');
  let went = ''; fvGo = u => { went = u; };
  fvSwitch('touch');
  sessionStorage.setItem('ph1', JSON.stringify({went, stored: localStorage.getItem('fabrica.view')}));
  location.replace(went); await sleep(60000);      // what the browser does with the new address
}
R.ph1 = JSON.parse(sessionStorage.getItem('ph1'));
R.after = {view: FVIEW.view, fill: FVIEW.fill, why: FVIEW.why, dataView: document.documentElement.getAttribute('data-view'), attr: document.documentElement.hasAttribute('data-fill')};
// the menu: it says which view this is and why, every choice is a button, the current choice carries a tick
document.querySelector('#morebtn').click(); await sleep(100);
R.menu = {on: document.getElementById('moresheet').classList.contains('on'), now: document.getElementById('more-viewnow').textContent, items: [...document.querySelectorAll('#moresheet .morebox button')].filter(b => getComputedStyle(b).display !== 'none').map(b => b.textContent.trim())};
document.querySelector('[data-act="viewOpen"]').click(); await sleep(100);
R.dlg = {on: document.getElementById('modal').classList.contains('on'), btns: [...document.querySelectorAll('#mbody .fvopt')].map(b => b.textContent.replace(/\s+/g, ' ').trim()), pressed: [...document.querySelectorAll('#mbody .fvopt')].map(b => b.getAttribute('aria-pressed')), dis: [...document.querySelectorAll('#mbody .fvopt')].map(b => b.disabled)};
let went2 = ''; fvGo = u => { went2 = u; };
document.querySelector('#mbody .fvopt[data-v="desktop"]').click(); R.desk = {went: went2, stored: localStorage.getItem('fabrica.view')};
document.querySelector('#mbody .fvopt[data-v=""]').click(); R.auto = {stored: localStorage.getItem('fabrica.view')};
const about = FVIEW.why; R.about = about;
"""


class SwitchViewTests(unittest.TestCase):
    R = None

    def result(self):
        if SwitchViewTests.R is None:
            SwitchViewTests.R = ok(self, run_q(self, PERSIST_DRIVER, "?group=Home&view=desktop&layout=x", (1440, 900), 150000, blink_settings=NO_POINTER))
        return SwitchViewTests.R

    def test_the_choice_is_remembered_in_localstorage_and_survives_a_reload(self):
        R = self.result()
        self.assertEqual(R["ph1"]["stored"], "touch")
        self.assertNotRegex(R["ph1"]["went"], r"view=|layout=|kiosk=")                     # the URL is stripped so the remembered choice can win
        self.assertIn("group=Home", R["ph1"]["went"])                                      # everything else in the URL is kept

    def test_after_the_reload_the_remembered_view_is_used(self):
        R = self.result()["after"]
        self.assertEqual((R["view"], R["fill"], R["dataView"], R["attr"]), ("touch", True, "touch", True))
        self.assertIn("remembered", R["why"])

    def test_the_menu_and_the_dialog(self):
        R = self.result()
        self.assertTrue(R["menu"]["on"])
        self.assertIn("View: 10-inch touch (remembered", R["menu"]["now"])
        self.assertIn("Switch view ▸", R["menu"]["items"])
        self.assertEqual(len(R["dlg"]["btns"]), 5)
        self.assertTrue(R["dlg"]["btns"][0].startswith("Automatic"))
        self.assertEqual([b[:4] for b in R["dlg"]["btns"] if b.startswith("\u2713")], ["\u2713 10"])      # the current (remembered) one carries a tick: 10-inch touch
        self.assertEqual(R["dlg"]["dis"], [False, False, False, True, False])                # Phone cannot work in a 1440 window
        self.assertEqual(R["desk"]["stored"], "desktop")
        self.assertNotRegex(R["desk"]["went"], r"view=|layout=|kiosk=")
        self.assertIsNone(R["auto"]["stored"])                                               # Automatic clears it


if __name__ == "__main__":
    unittest.main()
