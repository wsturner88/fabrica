"""The picture-first GRID view of a printer's files (Grid / List toggle), on a wall kiosk, a 5-inch sheet, a desktop and a phone.

    python3 tests/test_files_grid.py

The page tests load the real dashboard into headless Chrome / Edge with a stubbed `fetch` (skipped when no browser is found); one test
serves the page from a throw-away local server to count the picture requests. Nothing contacts a printer. Names are generic.
"""
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_layout_5in import run_q, WRAP  # noqa: E402
from test_parity_layouts import PRE, WIDE  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()

# about 15 files: 12 with a picture (cached ones and the PrusaLink "ask on request" kind), 3 without; one has two materials, one three; one is printing on Printer-E
FIXTURE_FILES = r"""
const NOW = Math.floor(Date.now() / 1000);
const FIX = [
  ['Bracket x12 for the workshop shelf.gcode.3mf', '2h 5m', ['PLA'], 'p'], ['Phone stand.gcode', '45m', ['PETG'], 'p'],
  ['Cable_clip_set_of_20_large_with_a_very_long_name_that_has_to_wrap_somewhere.gcode', '1h 20m', ['PLA', 'PETG'], 'p'],
  ['Lid.gcode', '35m', ['PLA'], 'p'], ['Gear housing v3.gcode', '3h 10m', ['ABS'], 'l'], ['Spool holder.gcode', '55m', ['PLA'], 'p'],
  ['Planter with a drip tray and a long second line of words.gcode', '4h 40m', ['PETG', 'PLA', 'TPU'], 'p'], ['Hinge pair.gcode', '20m', ['PLA'], 'l'],
  ['Knob.gcode', '12m', ['PLA'], 'p'], ['Vase twist.gcode', '6h 5m', ['PLA'], 'p'], ['Desk organiser.gcode', '2h 30m', ['PETG'], 'l'], ['Wall hook.gcode', '18m', ['PLA'], 'p'],
  ['Test cube.gcode', '8m', ['PLA'], ''], ['Calibration tower.gcode', '1h 5m', [], ''], ['Benchy.gcode', '1h 50m', ['PLA'], ''], ['Broken picture.gcode', '', ['PLA'], 'b'],
];
const fileOf = (x, i) => { const [name, t, mats, pic] = x; const o = {name, print_time: t, modified: NOW - i * 3600, size: 1000 + i * 100, weight_g: 10 + i};
  if(mats.length) o.filaments = mats.map(m => ({type: m, color_name: 'Black'}));
  if(name === 'Knob.gcode') o.last_printed = NOW + 120;                       // an old file that was printed lately: it floats to the top of Recent
  if(pic === 'p') o.thumb = 'pic-' + i + '.png'; else if(pic === 'b') o.thumb = 'broken-' + i + '.png'; else if(pic === 'l') o.thumb_lazy = true;
  return o; };
let FILES = FIX.map(fileOf);
"""

DRIVER = PRE + FIXTURE_FILES + r"""
FLEET = [mk('Printer-D', ['Home'], 'IDLE', 0, '', {kind: 'moonraker'}), mk('Printer-E', ['Home'], 'RUNNING', 40, 'Lid.gcode', {kind: 'moonraker'})];
const IMGS = [];                                   // every picture address handed to an <img>, in order
{ const d = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
  const PX = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==';      // pictures "load" (a 1-pixel image) except the one called broken-*, which fails on purpose
  Object.defineProperty(HTMLImageElement.prototype, 'src', {get: d.get, set(v){ IMGS.push(String(v)); d.set.call(this, window.__realImgs || /broken/.test(v) ? v : PX); }, configurable: true}); }
window.__stubs.push((u, m) => {
  const path = u.split('?')[0]; let mm;
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/files$/))) return J(FILES);
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/filedetail$/))){
    const f = FILES.find(x => x.name === decodeURIComponent((u.match(/file=([^&]*)/) || [])[1] || ''));
    return f ? J(Object.assign({}, f, {display_name: f.name, print_seconds: 5400, layer_mm: 0.2, nozzle_mm: 0.4, storage: 'Hub', cost: {available: false}, can_rename: true})) : J({error: 'no'}, 404);
  }
  if((mm = path.match(/^\/api\/printer\/([^/]+)$/))){
    const p = FLEET.find(x => x.printer === decodeURIComponent(mm[1])); if(!p) return null;
    return J({snapshot: p, trays: [], camera: false, can_connect: false, kind: p.kind, controls: {actions: [], allowed_now: [], light: ''}, detection: {state: 'no_camera', text: 'No camera'}});
  }
  return null;
});
const vis = e => { if(!e) return false; const b = e.getBoundingClientRect(); const cs = getComputedStyle(e); return b.width > 0 && b.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
const $q = s => Array.from(document.querySelectorAll(s));
const tiles = () => $q('#files .ftile');
const viewBtns = () => L5 ? $q('#files .fview button') : $q('#p-view button');
const sortBtns = () => L5 ? $q('#files .fsort button') : $q('#p-sort button');
const txt = e => e.textContent.replace(/\s+/g, ' ').trim();
const scroller = () => { const fr = document.querySelector('#files .frows'); for(const e of [document.getElementById('files'), fr]) if(e && /auto|scroll/.test(getComputedStyle(e).overflowY)) return e; return document.scrollingElement; };
const snap = () => {
  const f = document.getElementById('files'), fb = f.getBoundingClientRect(), g = document.querySelector('#files .fgrid'), T = tiles();
  const first = T.length ? T[0].getBoundingClientRect() : null;
  const sc = scroller(), sb = sc === document.scrollingElement ? {top: 0, bottom: innerHeight} : sc.getBoundingClientRect();
  const lefts = new Set(T.filter(t => first && Math.abs(t.getBoundingClientRect().top - first.top) < 2).map(t => Math.round(t.getBoundingClientRect().left)));
  const fullyIn = T.filter(t => { const b = t.getBoundingClientRect(); return b.top >= sb.top - 1 && b.bottom <= sb.bottom + 1; });
  const tops = new Set(fullyIn.map(t => Math.round(t.getBoundingClientRect().top)));
  const picRows = new Set(T.filter(t => { const b = t.querySelector('.fpic').getBoundingClientRect(); return b.top >= sb.top - 1 && b.bottom <= sb.bottom + 1; }).map(t => Math.round(t.getBoundingClientRect().top)));
  const vb = viewBtns();
  return {
    view: f.dataset.view, tiles: T.length, rows: $q('#files .frow').length, cols: lefts.size, rowsFullyVisible: tops.size, rowsTouched: new Set(T.filter(t => t.getBoundingClientRect().top < sb.bottom - 1 && t.getBoundingClientRect().bottom > sb.top).map(t => Math.round(t.getBoundingClientRect().top))).size, strip: document.getElementById('printer').classList.contains('strip'), picRowsVisible: picRows.size,
    tileW: first ? Math.round(first.width) : 0, tileH: first ? Math.round(first.height) : 0, minTileH: T.length ? Math.round(Math.min(...T.map(t => t.getBoundingClientRect().height))) : 0,
    picW: T.length ? Math.round(T[0].querySelector('.fpic').getBoundingClientRect().width) : 0, picH: T.length ? Math.round(T[0].querySelector('.fpic').getBoundingClientRect().height) : 0,
    filesH: Math.round(fb.height), filesTop: Math.round(fb.top), scrollerH: Math.round(sb.bottom - sb.top),
    names: T.map(t => txt(t.querySelector('.ftname'))), hints: T.map(t => { const h = t.querySelector('.fhint'); return h ? txt(h) : ''; }), rowMetas: $q('#files .frow .fmeta').map(txt), rowNames: $q('#files .frow .fname').map(txt), nameHs: T.map(t => Math.round(t.querySelector('.ftname').getBoundingClientRect().height)),
    nameLine: T.length ? parseFloat(getComputedStyle(T[0].querySelector('.ftname')).lineHeight) : 0,
    nameClipped: T.map(t => { const n = t.querySelector('.ftname'); return n.scrollHeight > n.clientHeight + 1; }),
    nameBelowPic: T.every(t => t.querySelector('.ftname').getBoundingClientRect().top >= t.querySelector('.fpic').getBoundingClientRect().bottom - 1),
    nopic: T.filter(t => t.querySelector('.fpic .nopic')).map(t => txt(t.querySelector('.nopic'))),
    badges: T.map(t => { const b = t.querySelector('.fbadge'); return b ? Array.from(b.children).map(txt).join(' ') : ''; }),
    times: T.map(t => { const x = t.querySelector('.ftime'); return x ? txt(x) : ''; }),
    metaOver: T.filter(t => { const m = t.querySelector('.ftmeta'); return m.scrollWidth > m.clientWidth + 1; }).length,
    tagged: T.filter(t => t.querySelector('.nowtag')).map(t => txt(t)),
    curCount: T.filter(t => t.classList.contains('cur')).length,
    noButtons: T.every(t => !t.querySelector('button')),
    docW: document.documentElement.scrollWidth, docH: document.documentElement.scrollHeight, vw: innerWidth, vh: innerHeight, bodyH: document.body.scrollHeight,
    tilesInsideBox: T.every(t => { const b = t.getBoundingClientRect(); return b.left >= -1 && b.right <= innerWidth + 1; }),
    scrollerCanScroll: sc.scrollHeight > sc.clientHeight + 2,
    view_btns: vb.map(b => txt(b) + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')), view_vis: vb.every(vis) && vb.length === 2,
    view_h: vb.length ? Math.min(...vb.map(b => Math.round(b.getBoundingClientRect().height))) : 0,
    tbarRows: (() => { const bar = document.querySelector('#printer .tbar'); return new Set(Array.from(bar.children).filter(vis).map(e => { const b = e.getBoundingClientRect(); return Math.round((b.top + b.bottom) / 2 / 14); })).size; })(),
    sortBtns: sortBtns().map(b => txt(b) + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')),
    refreshR: (() => { const b = L5 ? document.querySelector('#files .frefresh') : document.getElementById('p-frefresh'); return b && vis(b) ? Math.round(b.getBoundingClientRect().right) : null; })(),
    headOver: L5 ? (() => { const h = document.querySelector('#files .fhead'); return h ? h.scrollWidth > h.clientWidth + 1 : false; })() : false,
    imgs: IMGS.length, imgsNow: $q('#files img.ftimg').filter(i => i.getAttribute('src')).length, imgsAll: $q('#files img.ftimg').length + $q('#files .ftile .nopic').length,
  };
};
const frames = async n => { for(let i = 0; i < n; i++) await Promise.race([new Promise(r => requestAnimationFrame(() => r())), sleep(100)]); };     // let the page draw (the picture loader runs on drawn frames)
const scrolled = () => { scroller().dispatchEvent(new Event('scroll')); document.dispatchEvent(new Event('scroll')); };      // (a headless page does not always draw, so the scroll event is sent by hand)
const nap = async ms => { await sleep(ms); await frames(1); };
const clickBtn = (list, word) => { const b = list().find(x => txt(x).replace('✓ ', '') === word); b.click(); return !!b; };
await sleep(300); await fleet(); await sleep(150);
try{ localStorage.removeItem('fabrica.filesview'); }catch(e){}
openPrinter('Printer-D'); await sleep(700);
if(L5){ filesOpen(); await sleep(300); }
await sleep(300);
R.def = snap();                                                    // nothing chosen yet: the layout's own default
R.defStore = (() => { try{ return localStorage.getItem('fabrica.filesview'); }catch(e){ return 'err'; } })();
clickBtn(viewBtns, 'Grid'); await sleep(400);                 // (an explicit pick, even where Grid is already the default)
R.grid = snap();
R.gridStore = localStorage.getItem('fabrica.filesview');
// lazy pictures: how many were handed out before any scrolling, and after scrolling to the bottom
R.lazyBefore = {imgs: IMGS.length, now: R.grid.imgsNow, all: R.grid.imgsAll};
const sc = scroller();
if(sc === document.scrollingElement) window.scrollTo(0, 0);
// tap a tile: the detail sheet; Back; the scroll place is where it was
sc.scrollTop = Math.min(160, sc.scrollHeight);  window.scrollTo(0, sc === document.scrollingElement ? Math.min(400, document.documentElement.scrollHeight) : 0); await sleep(300);
R.scrollSet = sc === document.scrollingElement ? window.scrollY : sc.scrollTop;
const visTile = tiles().find(t => { const b = t.getBoundingClientRect(); return b.top > 0 && b.bottom < innerHeight; }) || tiles()[0];
R.tapName = visTile.dataset.f;
visTile.click(); await sleep(500);
R.detail = {on: document.getElementById('modal').classList.contains('on'), name: (document.querySelector('#mbody .fd-name') || {textContent: ''}).textContent, hasPrint: !!document.querySelector('#mbody [data-act="fdPrint"]'),
  hasDelete: !!document.querySelector('#mbody [data-act="fdDelete"]'), hasBack: !!document.querySelector('#mbody [data-act="fdBack"]'), calls: window.__calls.filter(c => /filedetail/.test(c)).length};
document.querySelector('#mbody [data-act="fdBack"]').click(); await sleep(300);
R.backOn = document.getElementById('modal').classList.contains('on');
R.scrollBack = sc === document.scrollingElement ? window.scrollY : sc.scrollTop;
// scroll to the bottom: the rest of the pictures are handed out
sc.scrollTop = sc.scrollHeight; if(sc === document.scrollingElement) window.scrollTo(0, document.documentElement.scrollHeight); scrolled(); await nap(500);
R.bottom = snap();                                              // the last tile is on screen now: its picture (the one that fails) has been asked for
sc.scrollTop = 0; if(sc === document.scrollingElement) window.scrollTo(0, 0); await sleep(200);
// sort: Name, Time; Date again
R.byRecent = {names: snap().names, hints: snap().hints};                       // the default order (Recent)
clickBtn(sortBtns, 'Name'); await sleep(200); R.byName = snap().names;
clickBtn(sortBtns, 'Time'); await sleep(200); R.byTime = {names: snap().names, times: snap().times};
clickBtn(sortBtns, 'Date'); await sleep(200); R.byDate = snap().names; R.dateHints = snap().hints;
// refresh: a new file appears first (newest first), in the grid
window.__calls.length = 0; FILES = [{name: 'Zed just sent.gcode', print_time: '10m', modified: NOW + 60, size: 500}].concat(FILES);
(L5 ? document.querySelector('#files .frefresh') : document.getElementById('p-frefresh')).click(); await sleep(500);
R.refresh = {names: snap().names.slice(0, 2), calls: window.__calls.filter(c => /\/files/.test(c)), view: snap().view, tiles: snap().tiles};
clickBtn(sortBtns, 'Recent'); await sleep(200); R.recentAfter = {names: snap().names.slice(0, 3), hints: snap().hints.slice(0, 3), btns: snap().sortBtns};
// List: the rows come back, the choice is stored; leave and come back: still List; switch back to Grid
clickBtn(viewBtns, 'List'); await sleep(300);
R.list = snap(); R.listRecent = {names: snap().rowNames.slice(0, 3), metas: snap().rowMetas.slice(0, 3)}; R.listStore = localStorage.getItem('fabrica.filesview');
show('home'); await sleep(200); openPrinter('Printer-D'); await sleep(700); if(L5){ filesOpen(); await sleep(300); }
R.listAgain = snap();
clickBtn(viewBtns, 'Grid'); await sleep(300);
R.gridAgain = snap();
// a printer that is printing: its file says so
show('home'); await sleep(200); openPrinter('Printer-E'); await sleep(900); if(L5){ filesOpen(); await sleep(300); }
R.busy = snap();
R.busyTile = (() => { const t = tiles().find(x => x.classList.contains('cur')); return t ? {name: t.dataset.f, text: txt(t)} : null; })();
"""


_MARK = "await sleep(300); await fleet();"
_head, _tail = DRIVER.split(_MARK, 1)
DRIVER = _head + _MARK + _tail.replace("await sleep(", "await nap(")          # every wait also lets the page draw a frame or two, as a real screen does


def go(testcase, key, query, size, driver=DRIVER, budget=240000):
    cache = GridBrowserTests.R
    if key not in cache:
        cache[key] = run_q(testcase, driver, query, size, budget)
    R = cache[key]
    testcase.assertNotIn("error", R, R.get("error"))
    return R


class GridBrowserTests(unittest.TestCase):
    R = {}

    def kiosk(self):
        return go(self, "kiosk", "?kiosk=1", (1024, 600))

    def desk(self):
        return go(self, "desk", "", (1440, 900))

    def phone(self):
        return go(self, "phone", "", (390, 844))

    def five(self):
        return go(self, "five", "?layout=5in", (800, 480))

    # ---- the toggle, its default and its memory
    def test_grid_is_the_default_on_a_kiosk_and_the_5_inch_sheet_and_list_everywhere_else(self):
        self.assertEqual(self.kiosk()["def"]["view"], "grid")
        self.assertEqual(self.five()["def"]["view"], "grid")
        self.assertEqual(self.desk()["def"]["view"], "list")
        self.assertEqual(self.phone()["def"]["view"], "list")
        for R in (self.kiosk(), self.five(), self.desk(), self.phone()):
            self.assertIsNone(R["defStore"])                       # a default is not a choice: nothing is stored until someone picks

    def test_the_toggle_is_two_words_with_a_tick_on_the_active_one(self):
        for name, R, active in (("kiosk", self.kiosk(), "Grid"), ("five", self.five(), "Grid"), ("desk", self.desk(), "List"), ("phone", self.phone(), "List")):
            self.assertEqual(sorted(x.replace("✓ ", "").rstrip("*") for x in R["def"]["view_btns"]), ["Grid", "List"], name)
            self.assertTrue(R["def"]["view_vis"], name)
            ticked = [x for x in R["def"]["view_btns"] if x.startswith("✓")]
            self.assertEqual(len(ticked), 1, name)
            self.assertEqual(ticked[0], "✓ " + active + "*", name)
            self.assertGreaterEqual(R["def"]["view_h"], 36 if name in ("five", "desk") else 44, name)         # a real touch target (a mouse: 36)

    def test_the_choice_is_remembered_and_survives_leaving_the_printer(self):
        for name, R in (("kiosk", self.kiosk()), ("five", self.five()), ("desk", self.desk()), ("phone", self.phone())):
            self.assertEqual(R["gridStore"], "grid", name)
            self.assertEqual(R["list"]["view"], "list", name)
            self.assertEqual(R["listStore"], "list", name)
            self.assertGreater(R["list"]["rows"], 0, name)
            self.assertEqual(R["list"]["tiles"], 0, name)
            self.assertEqual(R["listAgain"]["view"], "list", name)           # left the printer and came back: still List
            self.assertEqual(R["gridAgain"]["view"], "grid", name)

    # ---- the status band shrinks to one strip while the grid shows and nothing is printing
    def test_the_band_is_one_strip_while_idle_and_full_while_printing_or_in_the_list(self):
        for name, R in (("kiosk", self.kiosk()), ("phone", self.phone())):
            self.assertTrue(R["grid"]["strip"], name)
            self.assertFalse(R["list"]["strip"], name)            # the list keeps the full band
            self.assertFalse(R["busy"]["strip"], name)            # a print under way keeps its progress card
        self.assertFalse(self.five()["grid"]["strip"])            # (the 5-inch sheet has no band)
        self.assertFalse(self.desk()["grid"]["strip"])            # (the desktop layout keeps its big camera band: there is room for both)
        k = self.kiosk()
        self.assertLess(k["grid"]["filesTop"], k["list"]["filesTop"] - 100, (k["grid"]["filesTop"], k["list"]["filesTop"]))
        self.assertLessEqual(k["grid"]["filesTop"], 135)

    # ---- Recent: the default order, the last print lifts a file
    def test_recent_is_the_first_sort_and_the_default(self):
        for name, R in (("kiosk", self.kiosk()), ("five", self.five()), ("desk", self.desk()), ("phone", self.phone())):
            self.assertEqual(R["grid"]["sortBtns"][0], "Recent*" if name == "five" else "\u2713 Recent*", name)       # (the 5-inch sheet marks the active one by its look)
            self.assertEqual([x.replace("\u2713 ", "").rstrip("*") for x in R["grid"]["sortBtns"]][:4], ["Recent", "Date", "Name", "Time"], name)
            self.assertEqual(R["byRecent"]["names"][:2], ["Knob", "Bracket x12 for the workshop shelf"], name)       # the printed-lately file, then the newest
            self.assertEqual((R["def"]["names"] or R["def"]["rowNames"])[:2], ["Knob", "Bracket x12 for the workshop shelf"], name)       # the default view of that screen

    def test_a_file_lifted_by_its_last_print_says_when(self):
        for name, R in (("kiosk", self.kiosk()), ("five", self.five()), ("desk", self.desk()), ("phone", self.phone())):
            h = R["byRecent"]["hints"]
            self.assertRegex(h[0], r"^printed \w+ \d+$", name)
            self.assertEqual([x for x in h[1:] if x], [], name)                             # only the lifted one
            self.assertEqual([x for x in R["dateHints"] if x], [], name)                    # in the Date order nothing is lifted
            self.assertEqual(R["recentAfter"]["names"][:2], ["Knob", "Zed just sent"], name)   # a refreshed list keeps the Recent order
            self.assertEqual(R["recentAfter"]["names"][2], "Bracket x12 for the workshop shelf", name)
            self.assertIn("printed", R["listRecent"]["metas"][0], name)                     # and the List rows say it too
            self.assertEqual(R["listRecent"]["names"][0], "Knob", name)

    # ---- the grid itself
    def test_columns_follow_the_screen(self):
        self.assertEqual(self.kiosk()["grid"]["cols"], 5)         # 1024 wide: about 5
        self.assertEqual(self.five()["grid"]["cols"], 4)          # 800 wide: about 4
        self.assertEqual(self.phone()["grid"]["cols"], 2)
        self.assertGreaterEqual(self.desk()["grid"]["cols"], 6)    # the desktop layout uses the window: more columns than the wall screen (see tests/test_desktop_view.py)
        d = self.desk()["grid"]
        self.assertLessEqual(d["docW"], d["vw"])

    def test_the_wall_screens_show_two_rows_and_pictures_big_enough_to_recognise(self):
        k = self.kiosk()["grid"]
        self.assertGreaterEqual(k["rowsFullyVisible"], 2, k)                      # two whole rows (names too) ...
        self.assertGreaterEqual(k["rowsTouched"], 3, k)                           # ... and the start of a third: it is plain that the list goes on
        for name, R in (("kiosk", self.kiosk()), ("five", self.five())):
            g = R["grid"]
            self.assertGreaterEqual(g["picRowsVisible"], 2, (name, g["picRowsVisible"], g["scrollerH"], g["tileH"]))      # the pictures of two rows are whole on the screen
            self.assertGreaterEqual(g["picW"], 170, name)
            self.assertGreaterEqual(g["picH"], 105, name)
        self.assertGreaterEqual(self.five()["grid"]["rowsFullyVisible"], 2)       # on the 5-inch sheet the whole of two rows (names too)
        self.assertGreaterEqual(self.desk()["grid"]["picRowsVisible"], 2)         # (a desktop window is the same 600px page)
        self.assertGreaterEqual(self.phone()["grid"]["picRowsVisible"], 2)        # a phone sees two rows without scrolling past the band

    def test_every_tile_is_a_big_touch_target(self):
        for name in ("kiosk", "five", "desk", "phone"):
            R = go(self, name, {"kiosk": "?kiosk=1", "five": "?layout=5in", "desk": "", "phone": ""}[name], {"kiosk": (1024, 600), "five": (800, 480), "desk": (1440, 900), "phone": (390, 844)}[name])
            g = R["grid"]
            self.assertGreaterEqual(g["minTileH"], 44, name)
            self.assertGreaterEqual(g["tileW"], 44, name)
            self.assertTrue(g["noButtons"], name)                   # no Print / Delete on a tile: a tap opens the detail sheet

    def test_the_name_sits_under_the_picture_in_two_lines_at_most(self):
        for name in ("kiosk", "five", "desk", "phone"):
            g = go(self, name, {"kiosk": "?kiosk=1", "five": "?layout=5in", "desk": "", "phone": ""}[name], {"kiosk": (1024, 600), "five": (800, 480), "desk": (1440, 900), "phone": (390, 844)}[name])["grid"]
            self.assertTrue(g["nameBelowPic"], name)
            self.assertEqual(g["names"][0], "Knob", name)                                          # (Recent: Knob was printed lately) the extension is not shown
            for h in g["nameHs"]:
                self.assertLessEqual(h, 2 * g["nameLine"] + 2, name)
            self.assertTrue(any(g["nameClipped"]), name)             # the very long names are cut at two lines (the detail sheet has the whole name)

    def test_a_file_without_a_picture_says_no_picture_in_words(self):
        for R in (self.kiosk(), self.five(), self.desk(), self.phone()):
            self.assertIn(len(R["grid"]["nopic"]), (3, 4))                            # the files that have no picture (+ the failing one when it is already near the screen)
            self.assertEqual(R["bottom"]["nopic"], ["NO PICTURE"] * 4)            # + the one whose picture could not be loaded (never a broken-image icon)               # three files with no picture + one whose picture fails to load

    def test_print_time_and_the_material_in_words_on_one_small_line(self):
        g = self.kiosk()["grid"]
        by = {n: (g["times"][i], g["badges"][i]) for i, n in enumerate(g["names"])}
        self.assertEqual(by["Bracket x12 for the workshop shelf"], ("2h 5m", "PLA"))
        self.assertEqual(by["Phone stand"], ("45m", "PETG"))
        self.assertEqual(by["Cable_clip_set_of_20_large_with_a_very_long_name_that_has_to_wrap_somewhere"], ("1h 20m", "PLA PETG"))      # two materials, stacked
        self.assertEqual(by["Planter with a drip tray and a long second line of words"][1], "PETG +2")                                   # three: the first and how many more
        self.assertEqual(by["Calibration tower"][1], "")                                                                                  # no material known = no badge, never a guess
        self.assertEqual(g["metaOver"], 0)

    def test_the_printing_file_carries_the_word_printing(self):
        for name, R in (("kiosk", self.kiosk()), ("five", self.five()), ("desk", self.desk()), ("phone", self.phone())):
            b = R["busy"]
            self.assertEqual(b["curCount"], 1, name)
            self.assertEqual(len(b["tagged"]), 1, name)
            self.assertIn("Printing", b["tagged"][0], name)
            self.assertEqual(R["busyTile"]["name"], "Lid.gcode", name)
            self.assertEqual(R["grid"]["tagged"], [], name)           # an idle printer's grid has none

    def test_a_tap_opens_the_detail_sheet_and_back_keeps_the_scroll_place(self):
        for name in ("kiosk", "five", "desk", "phone"):
            R = go(self, name, {"kiosk": "?kiosk=1", "five": "?layout=5in", "desk": "", "phone": ""}[name], {"kiosk": (1024, 600), "five": (800, 480), "desk": (1440, 900), "phone": (390, 844)}[name])
            d = R["detail"]
            self.assertTrue(d["on"], name)
            self.assertTrue(d["hasPrint"] and d["hasDelete"] and d["hasBack"], name)
            self.assertEqual(d["calls"], 1, name)
            self.assertTrue(d["name"].startswith(R["tapName"].replace(".gcode.3mf", "").replace(".gcode", "")[:12]), (name, d["name"], R["tapName"]))
            self.assertFalse(R["backOn"], name)
            self.assertEqual(R["scrollBack"], R["scrollSet"], name)
        self.assertGreater(self.kiosk()["scrollSet"], 0)              # (the kiosk's box really did scroll before the tap)

    def test_sort_and_refresh_apply_to_the_grid(self):
        for name, R in (("kiosk", self.kiosk()), ("five", self.five()), ("desk", self.desk()), ("phone", self.phone())):
            self.assertEqual(R["byName"][:3], sorted(R["byName"], key=lambda s: s.lower())[:3], name)
            self.assertEqual(R["byName"][0], "Benchy", name)
            self.assertEqual(R["byTime"]["names"][0], "Test cube", name)                  # shortest first
            self.assertEqual(R["byDate"][0], "Bracket x12 for the workshop shelf", name)  # Date: newest first again
            self.assertEqual(R["byDate"][-1], "Broken picture", name)
            r = R["refresh"]
            self.assertEqual(r["view"], "grid", name)
            self.assertEqual(r["names"][0], "Zed just sent", name)                       # the new file, newest first
            self.assertEqual(len(r["calls"]), 1, name)
            self.assertIn("refresh=1", r["calls"][0], name)

    # ---- the screen as a whole
    def test_no_page_overflow_and_the_wall_screens_do_not_scroll_the_page(self):
        for name in ("kiosk", "five", "desk", "phone"):
            R = go(self, name, {"kiosk": "?kiosk=1", "five": "?layout=5in", "desk": "", "phone": ""}[name], {"kiosk": (1024, 600), "five": (800, 480), "desk": (1440, 900), "phone": (390, 844)}[name])
            for key in ("grid", "list", "busy"):
                g = R[key]
                self.assertLessEqual(g["docW"], g["vw"], (name, key))
                self.assertTrue(g["tilesInsideBox"] or key == "list", (name, key))
        for name in ("kiosk", "five"):
            R = go(self, name, {"kiosk": "?kiosk=1", "five": "?layout=5in"}[name], {"kiosk": (1024, 600), "five": (800, 480)}[name])
            self.assertLessEqual(R["grid"]["docH"], R["grid"]["vh"], name)         # the list scrolls inside its box
            self.assertTrue(R["grid"]["scrollerCanScroll"], name)

    def test_the_toolbar_stays_one_row_on_the_wall_and_desktop(self):
        for name in ("kiosk", "desk"):
            R = go(self, name, {"kiosk": "?kiosk=1", "desk": ""}[name], {"kiosk": (1024, 600), "desk": (1440, 900)}[name])
            self.assertEqual(R["grid"]["tbarRows"], 1, name)
            self.assertEqual(R["list"]["tbarRows"], 1, name)
        f = self.five()["grid"]
        self.assertFalse(f["headOver"])
        self.assertLessEqual(f["refreshR"], f["vw"])

    # ---- wide fallback font (the Linux kiosks)
    def test_with_a_wide_fallback_font_nothing_overflows(self):
        for name, query, size in (("kiosk-wide", "?kiosk=1", (1024, 600)), ("five-wide", "?layout=5in", (800, 480)), ("phone-wide", "", (390, 844))):
            R = go(self, name, query, size, DRIVER.replace("await sleep(300); await fleet();", WIDE + "await sleep(300); await fleet();", 1))
            g = R["grid"]
            self.assertEqual(g["view"], "grid", name)
            self.assertEqual(g["metaOver"], 0, name)
            self.assertLessEqual(g["docW"], g["vw"], name)
            for h in g["nameHs"]:
                self.assertLessEqual(h, 2 * g["nameLine"] + 2, name)
            self.assertEqual(g["tbarRows"], 1 if name == "kiosk-wide" else g["tbarRows"], name)
            self.assertFalse(g["headOver"], name)
            if name != "phone-wide":
                self.assertLessEqual(g["refreshR"], g["vw"], name)
                self.assertGreaterEqual(g["picRowsVisible"], 2, name)
                self.assertEqual(g["cols"], 5 if name == "kiosk-wide" else 4, name)
            if name == "kiosk-wide":
                self.assertLessEqual(g["docH"], g["vh"])


class GridStaticTests(unittest.TestCase):
    def test_the_toggle_and_the_lazy_loader_are_wired(self):
        self.assertIn('id="p-view"', HTML)
        self.assertIn("fview:     b => filesViewSet(b.dataset.v)", HTML)
        self.assertIn("function filesLazy()", HTML)
        self.assertIn("fabrica.filesview", HTML)

    def test_a_tile_carries_no_untrusted_text_outside_esc(self):
        body = re.search(r"function fileTileHtml\(f\)\{.*?\n\}\n", HTML, re.S).group(0)
        for m in re.finditer(r"\$\{([^}]*)\}", body):
            self.assertRegex(m.group(1), r"esc\(|^(cur \?|pic|time|badgeHtml\(f\))", m.group(1))     # the rest are strings built from esc() just above


# ---------------------------------------------------------------- the picture requests, counted by a real (local) server

class _Handler(http.server.BaseHTTPRequestHandler):
    PAGE = b""
    HITS = []
    PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f01f0005000201a2d9c1840000000049454e44ae426082")

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/thumb/"):
            _Handler.HITS.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(self.PNG)
        elif self.path.startswith("/wrap.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((WRAP % {"src": "/page.html?kiosk=1", "w": 1024, "h": 600}).encode("utf8"))
        elif self.path.startswith("/page.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.PAGE)
        else:
            self.send_response(404)
            self.end_headers()


def lots_of_files_driver(n):
    return DRIVER.split(_MARK)[0].replace("let FILES = FIX.map(fileOf);", "") + r"""
window.__realImgs = true; try{ localStorage.setItem('fabrica.filesview', 'grid'); }catch(e){}
let FILES = []; for(let i = 0; i < %d; i++) FILES.push({name: 'Part number ' + i + '.gcode', print_time: '30m', modified: NOW - i * 60, size: 1000, thumb: 'big-' + i + '.png', filaments: [{type: 'PLA', color_name: 'Black'}]});
await nap(300); await fleet(); await nap(150);
openPrinter('Printer-D'); await nap(1500);
R.tiles = document.querySelectorAll('#files .ftile').length;
R.view = document.getElementById('files').dataset.view;
R.before = IMGS.length;
const box = document.getElementById('files'); box.scrollTop = box.scrollHeight; scrolled(); await nap(1500);
R.after = IMGS.length; R.imgsLeft = document.querySelectorAll('#files img[data-src]').length;
""" % n


class GridRequestTests(unittest.TestCase):
    def test_a_hundred_file_printer_does_not_fetch_a_hundred_pictures(self):
        import test_fix_dashboard as tfd
        if not tfd.CHROME:
            self.skipTest("no Chrome / Edge found for the browser tests")
        d = tempfile.mkdtemp(prefix="gridreq-")
        self.addCleanup(shutil.rmtree, d, True)
        _Handler.PAGE = tfd.build_page(lots_of_files_driver(100)).encode("utf8")
        _Handler.HITS = []
        http.server.ThreadingHTTPServer.request_queue_size = 256       # (the default backlog of 5 refuses a burst of picture requests)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        cmd = [tfd.CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--allow-file-access-from-files",
               "--force-device-scale-factor=1", "--user-data-dir=" + os.path.join(d, "profile"), "--window-size=1024,800", "--virtual-time-budget=60000", "--dump-dom", "http://127.0.0.1:%d/wrap.html" % port]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        m = re.search(r"data-result='(.*?)'", r.stdout, re.S) or re.search(r'data-result="(.*?)"', r.stdout, re.S)
        self.assertIsNotNone(m, r.stderr[-300:])
        R = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["tiles"], 100)
        self.assertEqual(R["view"], "grid")
        self.assertGreater(R["before"], 0)                            # the pictures on screen load...
        self.assertLessEqual(R["before"], 25, R)                      # ...and the other ~80 do not
        self.assertGreater(R["after"], R["before"], R)                # scrolling to the bottom fetches the pictures that came near the screen
        self.assertLess(R["after"], 50, R)                            # (the ones in between were scrolled past, not all fetched)
        self.assertEqual(len(set(_Handler.HITS)), R["after"])         # what the page handed out is exactly what the server was asked for


if __name__ == "__main__":
    unittest.main()
