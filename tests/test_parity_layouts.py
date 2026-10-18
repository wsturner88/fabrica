"""Tests for the features the 5-inch wall screen had first and the 10-inch kiosk, desktop and phone layouts now share
(Spaghetti word on every card, progress readouts, spaghetti switch, live camera, camera placeholders and badges, file sort, ...).

    python3 tests/test_parity_layouts.py

Static checks read app/dashboard.html as text. The browser tests load the real page into headless Chrome / Edge with a stubbed `fetch`
(skipped when no browser is found); nothing contacts a printer or the network. Names are generic.
"""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_layout_5in import run_q  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()

# a wide fallback font, like the Linux CI machines have: nothing that measures text may rely on the narrow macOS system font
WIDE = "\ndocument.head.insertAdjacentHTML('beforeend', '<style>*{font-family:Verdana,\"DejaVu Sans\",sans-serif !important}</style>'); await sleep(50);\n"

PRE = r"""
try{ localStorage.setItem('fabrica.filesview', 'list'); }catch(e){}       // these tests look at the file ROWS: a kiosk would otherwise open the Grid (tests/test_files_grid.py covers that)
const mk = (printer, groups, state, pct, job, o = {}) => Object.assign({printer, groups, connected: state !== 'UNKNOWN', state, percent: pct, remaining_min: state === 'RUNNING' ? 116 : 0,
  job, layer: ['RUNNING', 'PAUSE'].includes(state) ? '123/456' : '', nozzle_c: state === 'RUNNING' ? 220 : 25, bed_c: state === 'RUNNING' ? 60 : 24, nozzle_target_c: 0, bed_target_c: 0, camera: false, kind: 'bambu'}, o);
let FLEET = [mk('Printer-A', ['Home'], 'RUNNING', 42, 'Bracket x12 for the workshop shelf.gcode.3mf', {camera: true}), mk('Printer-B', ['Home'], 'PAUSE', 63, 'Lid.gcode', {camera: true, kind: 'moonraker'}),
  mk('Printer-C', ['Home'], 'FINISH', 100, 'Phone stand.gcode', {kind: 'prusalink'}), mk('Printer-D', ['Home'], 'UNKNOWN', 0, '', {camera: true})];
let DET = {enabled: true, mode: 'alert', detector: {state: 'up'}, setup: {excluded_printers: []}, printers: {}};
window.__stubs.push((u, m) => {
  const path = u.split('?')[0];
  if(path === '/api/fleet') return J(FLEET);
  if(path === '/api/detection') return J(DET);
  if(path === '/api/ui-version') return J({v: '1'});
  if(path === '/api/auth/me') return J({});
  return null;
});
const r = e => { const b = e.getBoundingClientRect(); return {t: Math.round(b.top), b: Math.round(b.bottom), l: Math.round(b.left), r: Math.round(b.right), w: Math.round(b.width), h: Math.round(b.height)}; };
const setFleet = async (arr) => { FLEET = arr; FL.sig = null; await fleet(); await sleep(80); };
"""

FLEET_DRIVER = PRE + r"""
await sleep(300); await setFleet(FLEET);
const cards = () => [...document.querySelectorAll('#fleet .pcard')];
const snap = () => ({
  spg: cards().map(c => (c.querySelector('.spg, .spag') || {textContent: ''}).textContent.replace(/\s+/g, ' ').trim()),
  cls: cards().map(c => c.className),
  heights: cards().map(c => r(c).h), bottoms: cards().map(c => r(c).b), vh: innerHeight,
  banner: (() => { const b = document.getElementById('sp-alert'); return {hidden: b.hidden, text: b.textContent, h: r(b).h}; })(),
  slots: [...document.querySelectorAll('#fleet .cslot')].map(e => e.textContent.replace(/\s+/g, ' ').trim()),
  overflow: cards().map(c => [...c.querySelectorAll('.pname, .spg, .spag, .eta, .meta')].filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.className)),
  chipTicks: [...document.querySelectorAll('.fl-chip')].map(b => b.textContent.replace(/\s+/g, ' ').trim()),
});
R.calm = snap();
DET = Object.assign({}, DET, {printers: {'Printer-A': {watching: true, excluded: false, verdict: {level: 'warning', score: 0.7, warmup: false}, alerts: [1]}}});
await fleet(); await sleep(80); R.warn = snap();
DET.printers['Printer-A'].verdict.level = 'failure'; await fleet(); await sleep(80); R.fail = snap();
DET.printers['Printer-A'].verdict.warmup = true; await fleet(); await sleep(80); R.warm = snap();
DET.printers['Printer-A'].verdict = {level: 'ok', score: 0.1, warmup: false}; DET.setup.excluded_printers = ['Printer-B']; await fleet(); await sleep(80); R.ok = snap();
DET.detector = {state: 'down'}; await fleet(); await sleep(80); R.down = snap();
DET.detector = {state: 'up'}; DET.enabled = false; await fleet(); await sleep(80); R.off = snap();
"""


class FleetCardTests(unittest.TestCase):
    R = {}

    def result(self, key, driver, query, size):
        if key not in FleetCardTests.R:
            FleetCardTests.R[key] = run_q(self, driver, query, size, 120000)
        return FleetCardTests.R[key]

    def kiosk(self):
        return self.result("kiosk", FLEET_DRIVER, "?kiosk=1", (1024, 600))

    def test_every_card_says_the_spaghetti_word_on_the_10_inch_layout(self):
        R = self.kiosk()
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        self.assertEqual(R["calm"]["spg"], ["Spaghetti: WATCHING", "Spaghetti: READY", "Spaghetti: NO CAMERA", "Spaghetti: READY"])
        self.assertEqual(R["calm"]["chipTicks"], ["\u2713 All", "Home"])     # the active group chip carries a tick, not only a colour

    def test_a_warning_or_failure_is_a_word_plus_emphasis_and_a_banner(self):
        R = self.kiosk()
        for k, word in (("warn", "WARNING"), ("fail", "FAILURE")):
            S = R[k]
            self.assertEqual(S["spg"][0], "⚠ Spaghetti: " + word, k)
            self.assertIn("sp-alert", S["cls"][0], k)
            self.assertFalse(S["banner"]["hidden"], k)
            self.assertIn("SPAGHETTI " + word + ": Printer-A", S["banner"]["text"], k)
            self.assertNotIn("sp-alert", " ".join(S["cls"][1:]), k)
        self.assertEqual(R["calm"]["banner"]["hidden"], True)

    def test_still_warming_up_and_ok_and_not_watching_show_no_alert(self):
        R = self.kiosk()
        for k in ("warm", "ok", "off"):
            self.assertNotIn("sp-alert", " ".join(R[k]["cls"]), k)
            self.assertTrue(R[k]["banner"]["hidden"], k)
        self.assertEqual(R["ok"]["spg"][1], "Spaghetti: READY".replace("READY", "OFF"))           # Printer-B is on the "do not watch" list
        self.assertEqual(R["down"]["spg"][0], "Spaghetti: DETECTOR DOWN")
        self.assertEqual(R["off"]["spg"][0], "Spaghetti: OFF")

    def test_all_cards_keep_one_shape_with_and_without_an_alert(self):
        R = self.kiosk()
        for k in ("calm", "warn", "fail", "down"):
            self.assertEqual(len(set(R[k]["heights"])), 1, (k, R[k]["heights"]))
            self.assertLessEqual(max(R[k]["bottoms"]), R[k]["vh"], k)
            self.assertEqual([x for row in R[k]["overflow"] for x in row if "spg" in x], [], k)

    def test_with_a_wide_fallback_font_no_word_of_a_card_is_cut_off(self):
        R = self.result("kiosk-wide", FLEET_DRIVER.replace("await sleep(300); await setFleet(FLEET);", WIDE + "await sleep(300); await setFleet(FLEET);", 1), "?kiosk=1", (1024, 600))
        self.assertNotIn("error", R, R.get("error"))
        for k in ("calm", "warn", "fail", "down"):
            for row in R[k]["overflow"]:
                self.assertEqual([x for x in row if "pname" not in x], [], (k, row))        # (the printer name may end in an ellipsis: it is the title attribute too)
            self.assertEqual(len(set(R[k]["heights"])), 1, k)

    def test_no_camera_and_offline_camera_are_words_in_the_picture_slot(self):
        R = self.kiosk()
        self.assertEqual(R["calm"]["slots"][2], "NO CAMERA")
        self.assertEqual(R["calm"]["slots"][3], "CAMERA OFFLINE")


class FleetStaticTests(unittest.TestCase):
    def test_the_detector_is_asked_on_every_layout_and_the_banner_exists_once(self):
        self.assertEqual(HTML.count('id="sp-alert"'), 1)
        self.assertIn("pollJson('/api/fleet'), det5Fetch()", HTML)
        self.assertIn("spagBanner(data);", HTML)

    def test_the_light_button_in_the_toolbar_is_hidden_from_a_view_only_sign_in(self):
        self.assertIn('<span id="p-light" data-ctl></span>', HTML)            # body.viewonly [data-ctl] { display:none !important }
        self.assertIn('id="sp-tog" data-ctl', HTML)


DEEP_DRIVER = PRE + r"""
history.replaceState = (st, t, u) => { window.__rs = (window.__rs || []).concat([String(u)]); };
FLEET = [mk('Printer-A', ['Home'], 'RUNNING', 42, 'Bracket.gcode', {camera: true}), mk('Shop Printer', ['Work'], 'IDLE', 0, ''), mk('Zäh/Ü', ['Work'], 'IDLE', 0, '')];
window.__stubs.push((u, m) => { const mm = u.match(/^\/api\/printer\/([^/?]+)$/); if(mm){ const n = decodeURIComponent(mm[1]); const p = FLEET.find(x => x.printer === n); return p ? J({snapshot: p, trays: [], camera: false, can_connect: false, kind: 'bambu', controls: {actions: []}}) : J({error: 'no'}, 404); } if(/\/files$/.test(u)) return J([]); return null; });
await sleep(300); await fleet(); await sleep(500);       // (the page's own first poll ran before the stub existed: this is the first answer)
R.on = {printer: document.getElementById('printer').classList.contains('on'), home: document.getElementById('home').classList.contains('on'), title: document.getElementById('p-title').textContent,
  note: [...document.querySelectorAll('.fl-note')].map(e => e.textContent), cls: document.documentElement.className, prq: PRQ.want, kiosk: KIOSK, l5: L5, sel: FLG.sel.slice(),
  fleetCards: document.querySelectorAll('#fleet .pcard').length};
show('home'); await sleep(100);
R.back = {rs: (window.__rs || []).slice(), home: document.getElementById('home').classList.contains('on'), search: location.search};
"""


class DeepLinkTests(unittest.TestCase):
    R = {}

    def go(self, query, size=(1024, 600)):
        key = (query, size)
        if key not in DeepLinkTests.R:
            DeepLinkTests.R[key] = run_q(self, DEEP_DRIVER, query, size, 120000)
        R = DeepLinkTests.R[key]
        self.assertNotIn("error", R, R.get("error"))
        return R

    def test_a_known_name_opens_that_printer_whatever_the_case(self):
        for q in ("?printer=Printer-A", "?printer=PRINTER-a", "?printer=%20Printer-A%20"):
            R = self.go(q)
            self.assertEqual((R["on"]["printer"], R["on"]["title"]), (True, "Printer-A"), q)
            self.assertFalse(R["on"]["home"], q)

    def test_names_with_spaces_and_odd_characters(self):
        self.assertEqual(self.go("?printer=Shop%20Printer")["on"]["title"], "Shop Printer")
        self.assertEqual(self.go("?printer=Z%C3%A4h%2F%C3%9C")["on"]["title"], "Zäh/Ü")

    def test_it_combines_with_group_layout_and_kiosk(self):
        R = self.go("?kiosk=1&group=Work&printer=Printer-A")                 # Printer-A is not even in the Work group: the link still wins
        self.assertEqual((R["on"]["title"], R["on"]["kiosk"], R["on"]["sel"]), ("Printer-A", True, ["Work"]))
        R = self.go("?layout=5in&printer=Shop%20Printer", (800, 480))
        self.assertEqual((R["on"]["title"], R["on"]["l5"], R["on"]["cls"]), ("Shop Printer", True, "lay-5in"))
        R = self.go("?group=Work&layout=5in&kiosk=1&printer=Printer-A", (800, 480))
        self.assertEqual(R["on"]["title"], "Printer-A")

    def test_an_unknown_name_shows_the_fleet_and_a_short_notice(self):
        R = self.go("?printer=Nope")
        self.assertTrue(R["on"]["home"] and not R["on"]["printer"])
        self.assertEqual(R["on"]["fleetCards"], 3)
        self.assertEqual(R["on"]["note"], ["⚠ Printer 'Nope' not found — showing the fleet"])
        R = self.go("?layout=5in&printer=Nope", (800, 480))
        self.assertTrue(R["on"]["home"])
        self.assertEqual(len(R["on"]["note"]), 1)

    def test_no_param_changes_nothing_and_going_back_drops_the_param(self):
        R = self.go("?kiosk=1")
        self.assertTrue(R["on"]["home"])
        self.assertEqual(R["on"]["note"], [])
        R = self.go("?kiosk=1&printer=Printer-A")
        self.assertTrue(R["back"]["home"])
        self.assertEqual(len(R["back"]["rs"]), 1)
        self.assertNotIn("printer=", R["back"]["rs"][0])                       # a reload now shows the fleet
        self.assertIn("kiosk=1", R["back"]["rs"][0])                           # ... and keeps every other parameter

    def test_static(self):
        self.assertIn("function prParse(search)", HTML)
        self.assertIn("prDeepLink(all);", HTML)
        self.assertRegex(HTML, r"if\(id === 'home' && PRQ\.opened\)")


# ---------------------------------------------------------------------------------------------- the printer screen on every layout

SCREEN_DRIVER = PRE + r"""
const NOW = Math.floor(Date.now() / 1000);
FLEET = [mk('Printer-A', ['Home'], 'RUNNING', 42, 'Bracket x12 for the workshop shelf.gcode.3mf', {camera: true, nozzle_target_c: 220, bed_target_c: 60}), mk('Printer-B', ['Home'], 'PAUSE', 63, 'Lid.gcode', {camera: true, kind: 'moonraker'}),
  mk('Printer-C', ['Home'], 'FINISH', 100, 'Phone stand.gcode', {kind: 'prusalink'}), mk('Printer-D', ['Home'], 'IDLE', 0, '', {camera: true})];
let EXCL = [], DETLINE = {};
const TRAYS = [{tray: 0, slot_1based: 1, type: 'PLA', color_name: 'GREEN'}, {tray: 1, slot_1based: 2, type: 'PETG', color_name: 'BLACK'}, {tray: 2, slot_1based: 3, type: 'PLA', color_name: 'WHITE'}, {tray: 3, slot_1based: 4, type: 'PLA', color_name: 'RED'}];
const FILES = [{name: 'Bracket x12 for the workshop shelf.gcode.3mf', print_time: '3h 10m', weight_g: 120, modified: NOW - 3600, size: 4200000, filaments: [{color_name: 'GREEN', type: 'PLA'}]},
  {name: 'Lid.gcode.3mf', print_time: '40m', weight_g: 12, modified: NOW - 86400 * 40, size: 900000, filaments: [{color_name: 'BLACK', type: 'PETG'}]},
  {name: 'Cable clip.gcode.3mf', print_time: '25m', weight_g: 8, modified: NOW - 86400 * 3, size: 500000, filaments: [{color_name: 'RED', type: 'PLA'}]},
  {name: 'Aaa vase.gcode.3mf', print_time: '2h 20m', weight_g: 61, modified: NOW - 86400 * 9, size: 3300000, filaments: [{color_name: 'GREY', type: 'PETG'}]}];
const detail = n => {
  const p = FLEET.find(x => x.printer === n); if(!p) return null;
  const busy = ['RUNNING', 'PREPARE', 'PAUSE'].includes(p.state);
  return {snapshot: p, trays: p.kind === 'bambu' ? TRAYS : [], camera: p.camera, can_connect: false, kind: p.kind,
    controls: {actions: p.kind === 'prusalink' ? [] : ['light_on', 'preheat', 'cooldown'], allowed_now: busy ? ['light_on', 'light_off'] : ['light_on', 'light_off', 'preheat', 'cooldown'], light: 'off',
      detail: {preheat: {auto_off_timeout_min: 15, presets: {PLA: {nozzle: null, bed: 60}, PETG: {nozzle: null, bed: 80}, ABS: {nozzle: null, bed: 100}}}}},
    detection: DETLINE[n] || (p.camera ? {state: busy && p.state === 'RUNNING' ? 'ok' : 'idle', text: 'Watching', score: 0.04, learn: false, alerts: 0} : {state: 'no_camera', text: 'No camera'})};
};
window.__stubs.push((u, m, init) => {
  const path = u.split('?')[0]; let mm;
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/files$/))) return J(FILES);
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/control$/))) return J({ok: true, message: 'sent'});
  if((mm = path.match(/^\/api\/printer\/([^/]+)$/))){ const d = detail(decodeURIComponent(mm[1])); return d ? J(d) : J({error: 'no'}, 404); }
  if(path === '/api/settings'){ if(m === 'PUT'){ const b = JSON.parse(init.body); if(b.values['detection.excluded_printers']) EXCL = b.values['detection.excluded_printers']; return J({ok: true}); }
    return J({sections: [{id: 'detection', fields: [{key: 'detection.excluded_printers', value: EXCL}]}]}); }
  return null;
});
let lvStarts = 0; { const orig = window.lvStart; window.lvStart = () => { lvStarts++; return orig(); }; }
const vis = e => { const b = e.getBoundingClientRect(); const cs = getComputedStyle(e); return b.width > 0 && b.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
const T = id => { const e = document.getElementById(id); return e ? e.textContent.replace(/\s+/g, ' ').trim() : null; };
const screenSnap = () => {
  const bar = document.querySelector('#printer .tbar'), pr = document.getElementById('printer');
  const btns = [...pr.querySelectorAll('button')].filter(vis).filter(b => !b.closest('#files') || b.closest('.fsort'));
  const tops = new Set([...bar.children].filter(vis).map(e => { const b = e.getBoundingClientRect(); return Math.round((b.top + b.bottom) / 2 / 14); }));      // rows = distinct vertical centres
  return {
    docW: document.documentElement.scrollWidth, docH: document.documentElement.scrollHeight, vw: innerWidth, vh: innerHeight,
    title: T('p-title'), chip: T('p-chip'), big: T('p-big'), eta: T('p-eta'), layer: T('p-layer'), job: T('p-job'), bar: document.getElementById('p-bar').style.width,
    barVisible: vis(document.getElementById('p-bar')), statProg: document.getElementById('p-stat').dataset.prog, statSt: document.getElementById('p-stat').dataset.st,
    tbarRows: tops.size, tbar: r(bar), cancel: (() => { const c = document.getElementById('p-cancel'); return vis(c) ? r(c) : null; })(),
    small: btns.filter(b => b.getBoundingClientRect().height < 43.5).map(b => (b.textContent || b.id).trim().slice(0, 20) + ':' + Math.round(b.getBoundingClientRect().height)),
    noz: T('t-noz'), bed: T('t-bed'),
    spag: {k: document.getElementById('p-spag').dataset.k, word: T('sp-word'), sub: T('sp-sub'), on: document.getElementById('sp-tog').getAttribute('aria-checked'), dis: document.getElementById('sp-tog').disabled, vis: vis(document.getElementById('p-spag')), h: r(document.getElementById('sp-tog')).h, tw: T('sp-tog')},
    det: {cls: document.getElementById('p-det').className, vis: vis(document.getElementById('p-det')), text: T('p-det')},
    cam: {on: document.getElementById('cam').classList.contains('on'), vis: vis(document.getElementById('cam')), rect: r(document.getElementById('cam')), badge: T('cam-badge-t'), spag: T('cam-spag'), spagCls: document.getElementById('cam-spag').className, tap: T('cam').includes('tap to enlarge')},
    panel: {vis: vis(document.getElementById('p-panel')), text: T('p-panel'), add: vis(document.getElementById('pn-addcam')), addText: T('pn-cams')},
    trays: [...document.querySelectorAll('#p-trays .tray5')].map(b => ({t: [...b.children].map(c => c.textContent.trim()).join(' '), h: Math.round(b.getBoundingClientRect().height)})),
    ctl: [...document.querySelectorAll('#p-ctl-btns button, #p-light button')].filter(vis).map(b => b.textContent.replace(/\s+/g, ' ').trim()),
    sort: [...document.querySelectorAll('#p-sort button')].filter(vis).map(b => b.textContent.trim() + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')),
    sortRow: (() => { const s = document.getElementById('p-sort'); return vis(s) ? r(s) : null; })(),
    sortc: (() => { const s = document.getElementById('p-sortc'); return vis(s) ? {text: s.textContent.trim(), h: r(s).h, row: r(s)} : null; })(),
    rows: [...document.querySelectorAll('#files .frow')].map(f => ({n: f.querySelector('.fname').textContent, meta: f.querySelector('.fmeta').textContent, cur: f.classList.contains('cur'), tag: (f.querySelector('.nowtag') || {textContent: ''}).textContent, print: !!f.querySelector('button.primary')})),
    filesTop: r(document.getElementById('files')), lvStarts, live: LV.on || LV.ok || LV.failedAt > 0,
    chromeRows: (() => { const f = document.getElementById('files'); return Math.round(f.getBoundingClientRect().top); })(),
  };
};
const open = async (n) => { await fleet(); await sleep(150); openPrinter(n); await sleep(700); return screenSnap(); };
await sleep(300);
R.a = await open('Printer-A');                                       // printing, AMS, camera
R.lvA = lvStarts;
DETLINE['Printer-A'] = {state: 'warning', text: 'WARNING - possible spaghetti · score 0.71', score: 0.71, learn: false, alerts: 1};
await pview(); await sleep(200); R.aWarn = screenSnap(); delete DETLINE['Printer-A'];
R.sortBefore = R.a.rows.map(x => x.n);
document.querySelector('#p-sort [data-k="name"]') && document.querySelector('#p-sort [data-k="name"]').click(); await sleep(100);
R.sortName = [...document.querySelectorAll('#files .frow .fname')].map(e => e.textContent); R.sortNameBtns = [...document.querySelectorAll('#p-sort button')].map(b => b.textContent.trim() + (b.getAttribute('aria-pressed') === 'true' ? '*' : ''));
document.querySelector('#p-sort [data-k="time"]') && document.querySelector('#p-sort [data-k="time"]').click(); await sleep(100);
R.sortTime = [...document.querySelectorAll('#files .frow .fname')].map(e => e.textContent);
// the one-button sort (a print is under way): it opens a chooser in words with a tick on the active choice; picking one sorts and closes it
{ const c = document.getElementById('p-sortc'); const modalBtns = () => [...document.querySelectorAll('#mbody button')].map(b => b.textContent.trim() + (b.getAttribute('aria-pressed') === 'true' ? '*' : ''));
  R.chooser = {label0: c.textContent.trim()}; c.click(); await sleep(100);
  R.chooser.open = document.getElementById('modal').classList.contains('on'); R.chooser.btns = modalBtns(); R.chooser.h = Math.min(...[...document.querySelectorAll('#mbody button')].map(b => r(b).h));
  document.querySelector('#mbody [data-k="name"]').click(); await sleep(100);
  R.chooser.closed = !document.getElementById('modal').classList.contains('on'); R.chooser.label1 = c.textContent.trim(); R.chooser.order = [...document.querySelectorAll('#files .frow .fname')].map(e => e.textContent);
  c.click(); await sleep(100); R.chooser.btns2 = modalBtns(); document.querySelector('#mbody [data-k="latest"]').click(); await sleep(100); R.chooser.label2 = c.textContent.trim(); }
// the Spaghetti switch writes detection.excluded_printers (and nothing else)
window.__calls.length = 0;
document.getElementById('sp-tog').click(); await sleep(500);
R.toggleCalls = window.__calls.filter(c => c.indexOf('/api/settings') >= 0);
R.excl = EXCL.slice();
R.afterToggle = {k: document.getElementById('p-spag').dataset.k};
EXCL = []; await pview(); await sleep(200);
// the stream only runs while the page is visible and the display was touched lately
R.beforeHide = {on: LV.on || LV.failedAt > 0};
Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'}); document.dispatchEvent(new Event('visibilitychange')); await sleep(100);
R.hidden = {on: LV.on, ok: LV.ok, timer: !!CAM.timer};
delete document.visibilityState; document.dispatchEvent(new Event('visibilitychange')); await sleep(100);
LV.failedAt = 0; camSync(); R.shown = {started: lvStarts};
CAM.last = Date.now() - 11 * 60 * 1000; camIdleCheck(); await sleep(50);
R.idle = {paused: CAM.paused, on: LV.on, ok: LV.ok, timer: !!CAM.timer};
CAM.paused = false; CAM.last = Date.now();
// leave the screen: the live stream stops
show('home'); await sleep(200); R.leave = {on: LV.on, ok: LV.ok, src: (document.getElementById('cam-live').getAttribute('src') || '').slice(0, 12)};
R.b = await open('Printer-B');                                       // paused
R.c = await open('Printer-C');                                       // no camera, no AMS, no controls
R.d = await open('Printer-D');                                       // idle with a camera: Pre-heat
R.dCtl = R.d.ctl.slice();
const ph = [...document.querySelectorAll('#p-ctl-btns button')].find(b => /Pre-heat/.test(b.textContent));
ph && ph.click(); await sleep(100);
R.preSheet = document.getElementById('mbody').textContent.replace(/\s+/g, ' ').trim();
window.__calls.length = 0;
const pb = [...document.querySelectorAll('#mbody .presetgrid button')]; R.preBtns = pb.map(b => b.textContent.replace(/\s+/g, ' ').trim());
pb[0] && pb[0].click(); await sleep(300);
R.preCalls = window.__calls.filter(c => c.indexOf('/control') >= 0);
closeModal();
"""


class PrinterScreenTests(unittest.TestCase):
    R = {}

    def go(self, key, query, size, wide=False):
        k = (key, query, size, wide)
        if k not in PrinterScreenTests.R:
            PrinterScreenTests.R[k] = run_q(self, SCREEN_DRIVER.replace("await sleep(300);\nR.a =", (WIDE if wide else "") + "await sleep(300);\nR.a =", 1), query, size, 180000)
        R = PrinterScreenTests.R[k]
        self.assertNotIn("error", R, R.get("error"))
        return R

    def kiosk(self):
        return self.go("k", "?kiosk=1", (1024, 600))

    def test_progress_readouts_and_the_toolbar_are_one_row_that_fits(self):
        R = self.kiosk()
        a = R["a"]
        self.assertEqual(R["alerts"], [])
        self.assertEqual(a["tbarRows"], 1, a["tbar"])                     # ... and with the wide fallback font of the Linux machines (CI, the wall screen's Chromium) too
        w = self.go("kw", "?kiosk=1", (1024, 600), wide=True)["a"]
        self.assertEqual(w["tbarRows"], 1, w["tbar"])
        self.assertLessEqual(w["cancel"]["r"], w["vw"])
        self.assertEqual((a["big"], a["bar"]), ("42%", "42%"))
        self.assertTrue(a["eta"].startswith("Done ~"), a["eta"])
        self.assertRegex(a["layer"], r"^\d+h \d+m left · Layer 123 / 456$")
        self.assertEqual(a["job"], "Bracket x12 for the workshop shelf.gcode.3mf")
        self.assertEqual(a["tbarRows"], 1, a["tbar"])                     # ONE toolbar row: title, state, sort, Light, More, Pause, Cancel
        self.assertLessEqual(a["tbar"]["h"], 52)
        self.assertLessEqual(a["cancel"]["r"], a["vw"])
        self.assertEqual((a["docW"], a["docH"]), (1024, 600))              # nothing scrolls the page
        self.assertEqual(a["small"], [], "touch targets under 44px")

    def test_a_paused_print_shows_its_progress_not_an_unstarted_one(self):
        b = self.kiosk()["b"]
        self.assertEqual((b["big"], b["bar"], b["statProg"], b["statSt"]), ("63%", "63%", "1", "paused"))
        self.assertEqual(b["eta"], "Paused")
        self.assertIn("Layer", b["layer"])
        self.assertEqual(b["chip"], "PAUSED")

    def test_an_idle_printer_says_ready_and_has_no_empty_progress(self):
        d = self.kiosk()["d"]
        self.assertEqual((d["big"], d["bar"], d["statProg"]), ("Ready", "0%", "0"))

    def test_temperatures_are_tiles(self):
        a = self.kiosk()["a"]
        self.assertIn("220", a["noz"])
        self.assertIn("/ 220", a["noz"])
        self.assertIn("60", a["bed"])

    def test_the_spaghetti_word_and_a_switch_on_the_printer_screen(self):
        R = self.kiosk()
        a = R["a"]
        self.assertEqual((a["spag"]["k"], a["spag"]["word"], a["spag"]["on"], a["spag"]["tw"]), ("on", "WATCHING", "true", "ON"))
        self.assertGreaterEqual(a["spag"]["h"], 44)
        self.assertEqual(R["c"]["spag"]["k"], "nocamera")
        self.assertEqual(R["c"]["spag"]["word"], "NO CAMERA")
        self.assertTrue(R["c"]["spag"]["dis"])
        self.assertEqual(R["d"]["spag"]["word"], "READY")

    def test_the_switch_writes_the_excluded_printers_list_and_only_that(self):
        R = self.kiosk()
        calls = R["toggleCalls"]
        self.assertEqual(calls[0], "GET /api/settings")
        self.assertTrue(calls[1].startswith("PUT /api/settings "), calls)
        self.assertEqual(json.loads(calls[1].split(" ", 2)[2]), {"values": {"detection.excluded_printers": ["Printer-A"]}})
        self.assertEqual(R["excl"], ["Printer-A"])

    def test_a_warning_is_words_plus_emphasis_on_the_screen(self):
        w = self.kiosk()["aWarn"]
        self.assertEqual((w["spag"]["k"], w["spag"]["word"]), ("alert", "⚠ WARNING"))
        self.assertIn("alert", w["det"]["cls"])
        self.assertTrue(w["det"]["vis"])
        self.assertTrue(w["det"]["text"].startswith("ALERT: Spaghetti watch: WARNING"), w["det"]["text"])
        self.assertIn("alert", w["cam"]["spagCls"])
        self.assertEqual(w["cam"]["spag"], "⚠ Spaghetti: WARNING · 1 alert")

    def test_the_camera_is_a_16_9_tile_with_a_badge_and_goes_live_only_on_this_screen(self):
        R = self.kiosk()
        a = R["a"]
        self.assertTrue(a["cam"]["on"] and a["cam"]["vis"])
        self.assertEqual(a["cam"]["badge"], "SNAPSHOT")                    # LIVE once the stream delivers a frame (not in a test with no camera)
        self.assertTrue(a["cam"]["tap"])
        self.assertAlmostEqual(a["cam"]["rect"]["w"] / a["cam"]["rect"]["h"], 16 / 9, delta=0.2)
        self.assertGreaterEqual(R["lvA"], 1)                               # the live stream was asked for on this layout
        self.assertEqual(R["leave"]["src"].startswith("data:"), True)      # leaving the screen drops the stream
        self.assertFalse(R["leave"]["on"] or R["leave"]["ok"])
        self.assertFalse(R["hidden"]["on"] or R["hidden"]["ok"] or R["hidden"]["timer"])        # a hidden tab / a sleeping phone: no stream, no timer
        self.assertGreater(R["shown"]["started"], R["lvA"])                                      # ... and it starts again when the page is shown
        self.assertTrue(R["idle"]["paused"])
        self.assertFalse(R["idle"]["on"] or R["idle"]["ok"] or R["idle"]["timer"])               # nobody touched the display for 10 minutes: it stops

    def test_no_camera_is_a_status_panel_in_words_with_a_view_only_add_camera_on_a_kiosk(self):
        c = self.kiosk()["c"]
        self.assertFalse(c["cam"]["on"])
        self.assertTrue(c["panel"]["vis"])
        self.assertIn("No camera", c["panel"]["text"])
        self.assertIn("IDLE", c["panel"]["text"])
        self.assertIn("Phone stand.gcode", c["panel"]["text"])
        self.assertFalse(c["panel"]["add"])                                # no keyboard on a kiosk: no Add camera button
        self.assertIn("from a computer", c["panel"]["addText"])
        self.assertFalse(self.kiosk()["a"]["panel"]["vis"])                # a camera takes the panel's place

    def test_add_camera_is_a_button_off_the_kiosk(self):
        c = self.go("desk", "", (1440, 900))["c"]
        self.assertTrue(c["panel"]["add"])

    def test_the_ams_slots_are_48px_buttons_in_one_row(self):
        a = self.kiosk()["a"]
        self.assertEqual([t["t"] for t in a["trays"]], ["1 PLA GREEN", "2 PETG BLACK", "3 PLA WHITE", "4 PLA RED"])
        self.assertEqual({t["h"] for t in a["trays"]}, {48})

    def test_while_printing_the_four_sort_buttons_are_one_sort_button_with_a_chooser(self):
        for wide in (False, True):
            R = self.go("kw" if wide else "k", "?kiosk=1", (1024, 600), wide=wide)
            for k in ("a", "b"):                                          # printing, paused: the row also holds Pause / Resume + Cancel
                self.assertEqual(R[k]["sort"], [], (wide, k))
                self.assertEqual(R[k]["sortc"]["text"], "Sort: Recent \u25be", (wide, k))
                self.assertGreaterEqual(R[k]["sortc"]["h"], 44, (wide, k))
                self.assertLessEqual(R[k]["sortc"]["row"]["b"], R[k]["tbar"]["b"] + 4, (wide, k))      # inside the one toolbar row
                self.assertEqual(R[k]["tbarRows"], 1, (wide, k))
            c = R["chooser"]
            self.assertEqual((c["label0"], c["open"], c["closed"]), ("Sort: Time \u25be", True, True))
            self.assertEqual(c["btns"], ["Recent", "Date", "Name", "\u2713 Time*", "Close"])      # words, a tick on the active one
            self.assertGreaterEqual(c["h"], 44)
            self.assertEqual(c["label1"], "Sort: Name \u25be")
            self.assertEqual(c["order"], sorted(c["order"], key=str.lower))
            self.assertEqual(c["btns2"], ["Recent", "Date", "\u2713 Name*", "Time", "Close"])
            self.assertEqual(c["label2"], "Sort: Recent \u25be")
            self.assertIsNone(R["c"]["sortc"])                            # a printer with no list of files has no sort at all
            for k in ("d",):                                              # idle: the full group stays, the one button stays away
                self.assertEqual(R[k]["sort"], ["\u2713 Recent*", "Date", "Name", "Time"], (wide, k))
                self.assertIsNone(R[k]["sortc"], (wide, k))
                self.assertEqual(R[k]["tbarRows"], 1, (wide, k))
        self.assertIsNone(self.go("desk", "", (1440, 900))["a"]["sortc"])                           # the desktop has the room: all four
        self.assertEqual(self.go("desk", "", (1440, 900))["a"]["sort"], ["\u2713 Recent*", "Date", "Name", "Time"])
        self.assertIsNone(self.go("phone", "", (390, 844))["a"]["sortc"])                           # the phone keeps its own full-width row

    def test_files_sort_in_the_toolbar_row_with_a_tick_and_the_current_file_is_tagged(self):
        R = self.kiosk()
        a = R["a"]
        self.assertEqual(R["d"]["sort"], ["✓ Recent*", "Date", "Name", "Time"])
        self.assertLessEqual(R["d"]["sortRow"]["b"], R["d"]["tbar"]["b"] + 4)        # inside the one toolbar row (the idle printer: the full group)
        self.assertEqual(R["sortBefore"][0], "Bracket x12 for the workshop shelf")      # newest first
        self.assertEqual(R["sortName"], sorted(R["sortName"], key=str.lower))
        self.assertEqual(R["sortNameBtns"], ["Recent", "Date", "✓ Name*", "Time"])
        self.assertEqual(R["sortTime"], ["Cable clip", "Lid", "Aaa vase", "Bracket x12 for the workshop shelf"])      # shortest print first
        cur = [x for x in a["rows"] if x["cur"]]
        self.assertEqual(len(cur), 1)
        self.assertEqual((cur[0]["tag"], cur[0]["print"]), ("Printing", False))
        self.assertTrue(cur[0]["meta"].startswith("Printing now"))
        self.assertRegex(a["rows"][1]["meta"], r"· [A-Z][a-z]{2} \d+$")     # the date modified, e.g. "Oct 15"
        self.assertIsNone(self.kiosk()["c"]["sortRow"] if len(self.kiosk()["c"]["rows"]) < 2 else None)

    def test_preheat_is_bed_only_in_words_and_sends_the_preset_name(self):
        R = self.kiosk()
        self.assertIn("Pre-heat", R["dCtl"])
        self.assertEqual(R["preBtns"], ["PLA Bed 60 °C", "PETG Bed 80 °C", "ABS Bed 100 °C"])
        self.assertNotIn("Nozzle", R["preSheet"])
        self.assertIn("nozzle stays off", R["preSheet"])
        self.assertEqual(len(R["preCalls"]), 1)
        body = json.loads(R["preCalls"][0].split(" ", 2)[2])
        self.assertEqual(body, {"action": "preheat", "preset": "PLA"})     # the Hub looks the bed temperature up; the page sends no nozzle number

    def test_a_wide_fallback_font_never_pushes_cancel_off_the_screen(self):
        a = self.go("kw", "?kiosk=1", (1024, 600), wide=True)["a"]
        self.assertLessEqual(a["docW"], 1024)
        self.assertLessEqual(a["cancel"]["r"], 1024)
        self.assertGreaterEqual(a["cancel"]["l"], 0)
        self.assertLessEqual(a["tbarRows"], 2)                              # one row normally; a crowded one wraps instead of overflowing

    def test_desktop_window_is_the_same_page(self):
        a = self.go("desk", "", (1440, 900))["a"]
        self.assertEqual((a["big"], a["tbarRows"]), ("42%", 1))

    def test_phone_stacks_the_same_pieces_and_nothing_runs_off_the_side(self):
        R = self.go("phone", "", (390, 844))
        a = R["a"]
        self.assertLessEqual(a["docW"], 390)
        self.assertEqual((a["big"], a["spag"]["word"], a["cam"]["on"]), ("42%", "WATCHING", True))
        self.assertEqual(a["small"], [])
        self.assertLessEqual(a["cancel"]["r"], 390)
        self.assertEqual(a["sort"], ["✓ Recent*", "Date", "Name", "Time"])
        self.assertEqual(R["c"]["panel"]["vis"], True)
        self.assertGreaterEqual(R["lvA"], 1)


# ---------------------------------------------------------------------------------------------- History

HIST_DRIVER = PRE + r"""
const NOW = Math.floor(Date.now() / 1000);
const names = ['Bracket x12 for the workshop shelf.gcode.3mf', 'Lid v3.gcode', 'Phone stand.bgcode', 'Cable clip x20.gcode.3mf', 'Gridfinity bin 2x3.gcode', 'Calibration cube.gcode'];
const JOBS = Array.from({length: 14}, (_, i) => ({id: i + 1, printer: ['Printer-A', 'Printer-B', 'Printer-C'][i % 3], file: names[i % 6], result: ['finished', 'failed', 'finished', 'cancelled'][i % 4], started_at: NOW - 7200 * (i + 1), ended_at: NOW - 7200 * (i + 1) + 3000,
  parts: 1, grams_used: 10 + i, duration_s: 3000, cost: {total: 1.5 + i / 10, material: 1.5, electricity: 0, wear: 0, includes_machine_costs: false}, open: false, material: 'PLA', filaments: [{type: 'PLA', used_g: 10}], pauses: 0, percent: 100, est_seconds: 3000, time_vs_estimate_pct: 1, grams_est: 10}));
const STATS = {jobs: 47, finished: 41, failed: 4, cancelled: 2, unknown: 0, running: 1, success_rate: 0.91, grams_used: 3420, by_material: {PLA: 2600, PETG: 820}, since: null, currency: '$', includes_machine_costs: false,
  true_cost_per_part: 1.42, clean_cost_per_part: 1.1, cost: {total: 71.4, material: 71.4, electricity: 0, wear: 0, wasted_on_failures: 6.2},
  by_printer: {'Printer-A': {jobs: 20, finished: 18, failed: 1, cancelled: 1, success_rate: 0.9, hours: 61.5, cost_total: 31.2}, 'Printer-B': {jobs: 14, finished: 12, failed: 2, cancelled: 0, success_rate: 0.86, hours: 40.2, cost_total: 22.0},
    'Printer-C': {jobs: 9, finished: 9, failed: 0, cancelled: 0, success_rate: 1, hours: 20.1, cost_total: 11.9}},
  by_file: [{file: 'bracket x12 for the workshop shelf', display: 'Bracket x12 for the workshop shelf', jobs: 6, success_rate: 0.83, good_parts: 60, true_cost_per_part: 0.5, clean_cost_per_part: 0.4},
            {file: 'phone stand', display: 'Phone stand', jobs: 8, success_rate: 1, good_parts: 8, true_cost_per_part: 1.1, clean_cost_per_part: 1.1}, {file: 'old file', jobs: 1, success_rate: 1, good_parts: 1, true_cost_per_part: 1, clean_cost_per_part: 1}]};
window.__stubs.push((u, m) => {
  const path = u.split('?')[0]; let mm;
  if(path === '/api/printers') return J({printers: [{name: 'Printer-A'}, {name: 'Printer-B'}, {name: 'Printer-C'}]});
  if(path === '/api/groups') return J({groups: []});
  if(path === '/api/stats') return J(STATS);
  if((mm = path.match(/^\/api\/jobs\/(\d+)$/))) return J(JOBS[Number(mm[1]) - 1]);
  if(path === '/api/jobs') return J({jobs: JOBS});
  return null;
});
const vis = e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && getComputedStyle(e).display !== 'none'; };
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
const snap = () => {
  const head = document.getElementById('hi-head'), ctl = [...document.querySelectorAll('#hi-head > h1, #hi-ctl > *')].filter(vis);
  const rows = new Set(ctl.map(e => { const b = e.getBoundingClientRect(); return Math.round((b.top + b.bottom) / 2 / 14); }));
  const list = document.querySelector('#hi-body .hi-list');
  const dataRows = [...document.querySelectorAll('#hi-body .tr:not(.h)')].filter(vis);
  const kpis = [...document.querySelectorAll('#hi-body .kpi')].filter(vis);
  const fs = e => parseFloat(getComputedStyle(e).fontSize);
  return {vw: innerWidth, vh: innerHeight, docW: document.documentElement.scrollWidth, docH: document.documentElement.scrollHeight,
    toolbarRows: rows.size, toolbar: r(head), close: r([...document.querySelectorAll('#hi-ctl > button')].pop()), closeText: [...document.querySelectorAll('#hi-ctl > button')].pop().textContent,
    ctl: ctl.map(e => ({t: e.textContent.replace(/\s+/g, ' ').trim().slice(0, 80), h: Math.round(e.getBoundingClientRect().height), r: Math.round(e.getBoundingClientRect().right)})),
    tileRows: new Set(kpis.map(k => r(k).t)).size, tiles: kpis.map(k => ({label: k.querySelector('.kl').textContent, v: k.querySelector('.kv').textContent, sub: [...k.querySelectorAll('.ks')].filter(vis).map(x => x.textContent.replace(/\s+/g, ' ').trim()).join(' | '), clipped: [...k.querySelectorAll('.kl,.kv,.ks')].filter(vis).some(x => x.scrollWidth > x.clientWidth + 1) || [...k.querySelectorAll('.kl,.ks')].filter(vis).some(x => x.scrollHeight > x.clientHeight + 1),
      kvSize: parseFloat(getComputedStyle(k.querySelector('.kv')).fontSize), h: Math.round(k.getBoundingClientRect().height)})),
    tabs: [...document.querySelectorAll('.hi-tabs button')].map(b => ({t: b.textContent.trim(), sel: b.getAttribute('aria-selected'), h: Math.round(b.getBoundingClientRect().height)})),
    firstRowTop: dataRows.length ? r(dataRows[0]).t : null, rowH: dataRows.map(e => r(e).h), nVisible: dataRows.filter(e => r(e).b <= innerHeight).length,
    listScrolls: list ? list.scrollHeight > list.clientHeight + 2 : null, listTop: list ? r(list).t : null,
    firstJob: dataRows.length ? [...dataRows[0].children].map(c => c.textContent.replace(/\s+/g, ' ').trim()) : null,
    fonts: {content: Math.min(...dataRows.slice(0, 3).flatMap(row => [...row.children].map(fs))), support: Math.min(...[...document.querySelectorAll('#hi-body .kl, #hi-body .ks, #hi-body .ht2, #hi-body .tr.h > div')].filter(vis).map(fs))},
    names: dataRows.map(e => e.firstElementChild.textContent.trim()).slice(0, 4), headings: [...document.querySelectorAll('#hi-body h2')].filter(vis).map(h => h.textContent.replace(/\s+/g, ' ').trim()),
    firstTable: (() => { const t = document.querySelector('#hi-body .ht'); return t ? r(t).t : null; })()};
};
await sleep(300);
await openHistory(); await wait(() => document.querySelector('#hi-body .kpi')); await sleep(200);
R.jobs = snap();
const tabBtn = k => document.querySelector('.hi-tabs [data-t="' + k + '"]');
if(tabBtn('printer')){
  tabBtn('printer').click(); await sleep(150); R.printer = snap();
  tabBtn('file').click(); await sleep(150); R.file = snap();
  tabBtn('jobs').click(); await sleep(150);
  const row = document.querySelector('#hi-body .tr.click'); row.click(); await wait(() => document.querySelector('#mbody .hi-det')); await sleep(100);
  R.detail = {open: document.getElementById('modal').classList.contains('on'), text: document.getElementById('mbody').textContent.replace(/\s+/g, ' ').slice(0, 200), call: window.__calls.filter(c => c.indexOf('/api/jobs/') >= 0).pop()};
  closeModal();
  document.querySelectorAll('#hi-ctl .hi-per button')[1].click(); await sleep(300);
  R.period = {calls: window.__calls.filter(c => c.indexOf('/api/stats') >= 0).pop(), pressed: [...document.querySelectorAll('#hi-ctl .hi-per button')].map(b => b.getAttribute('aria-checked'))};
  document.querySelector('#hi-ctl .hi-more').click(); await sleep(100);
  R.more = {on: document.getElementById('modal').classList.contains('on'), csv: !!document.querySelector('#mbody a.bkdl'), hint: !!document.querySelector('#mbody .hi-setlink')};
  closeModal();
}
"""


class HistoryTests(unittest.TestCase):
    R = {}

    def go(self, key, query, size, wide=True):
        # wide fallback font by default: on a Linux CI machine (and on the Pi kiosks) the bundled Inter is not what measures the text, and the
        # macOS system font is narrower than DejaVu / Liberation, so a tile that only fits on a Mac must fail here too
        k = (key, query, size, wide)
        if k not in HistoryTests.R:
            drv = HIST_DRIVER.replace("await sleep(300);\nawait openHistory();", (WIDE if wide else "") + "await sleep(300);\nawait openHistory();", 1)
            HistoryTests.R[k] = run_q(self, drv, query, size, 180000)
        R = HistoryTests.R[k]
        self.assertNotIn("error", R, R.get("error"))
        return R

    def five(self):
        return self.go("5", "?layout=5in", (800, 480))

    def test_one_toolbar_row_that_fits_with_close_on_screen(self):
        J = self.five()["jobs"]
        self.assertEqual(self.five()["alerts"], [])
        self.assertEqual(J["toolbarRows"], 1)
        self.assertLessEqual(J["close"]["r"], 800)
        self.assertEqual(J["closeText"], "Close")
        self.assertEqual([c["t"] for c in J["ctl"]][:2], ["History", "All printers ▾"])
        self.assertTrue(all(c["h"] >= 44 for c in J["ctl"][1:]), J["ctl"])        # every control is a finger target
        words = " | ".join(c["t"] for c in J["ctl"])
        for w in ("24h", "7d", "30d", "All", "Filament only", "+ Print time", "More", "Close"):
            self.assertIn(w, words)

    def test_no_page_scroll_tiles_in_one_compact_row_then_the_list(self):
        J = self.five()["jobs"]
        self.assertEqual((J["docH"], J["docW"]), (480, 800))
        self.assertEqual(J["tileRows"], 1)
        self.assertEqual([t["label"].split(" · ")[0] for t in J["tiles"]], ["Prints", "Success rate", "Material used", "Cost"])
        self.assertFalse(any(t["clipped"] for t in J["tiles"]), J["tiles"])
        self.assertLessEqual(J["firstRowTop"], 230)                           # was 368 (and the page scrolled)
        self.assertGreaterEqual(J["nVisible"], 4)
        self.assertTrue(J["listScrolls"])                                      # the list scrolls inside its own box
        self.assertEqual(J["headings"], [])

    def test_tiles_with_long_numbers_and_three_materials_still_fit_a_wide_font_and_keep_the_list(self):
        # a busy shop: 4-digit counts, a 6-digit gram total, three materials, a big cost, "unknown" and "running" prints
        extra = ("STATS.jobs = 1247; STATS.finished = 1102; STATS.failed = 98; STATS.cancelled = 47; STATS.running = 3; STATS.unknown = 2; STATS.grams_used = 1234567; "
                 "STATS.by_material = {PLA: 900000, PETG: 200000, 'PLA-CF': 80000}; STATS.cost.total = 12345.67; STATS.success_rate = 0.884; STATS.includes_machine_costs = true;\n")
        drv = HIST_DRIVER.replace("await sleep(300);\nawait openHistory();", WIDE + extra + "await sleep(300);\nawait openHistory();", 1)
        R = run_q(self, drv, "?layout=5in", (800, 480), 180000)
        self.assertNotIn("error", R, R.get("error"))
        J = R["jobs"]
        self.assertEqual((J["docH"], J["docW"], J["tileRows"]), (480, 800, 1))
        self.assertFalse(any(t["clipped"] for t in J["tiles"]), J["tiles"])
        self.assertTrue(all(t["kvSize"] >= 16 for t in J["tiles"]), J["tiles"])      # the number may shrink to its tile, never below 16 px
        self.assertEqual(len({t["h"] for t in J["tiles"]}), 1)                        # the four tiles stay one row of one height
        self.assertIn("PLA-CF", J["tiles"][2]["sub"])                                 # all three materials are still said, in words
        self.assertIn("3 running", J["tiles"][0]["sub"])
        self.assertLessEqual(J["firstRowTop"], 260)
        self.assertGreaterEqual(J["nVisible"], 3)
        self.assertTrue(J["listScrolls"])

    def test_filament_only_is_said_once_in_the_cost_tile(self):
        t = self.five()["jobs"]["tiles"][3]
        self.assertEqual(t["label"], "Cost · filament only")
        self.assertNotIn("ilament only", t["sub"])
        self.assertEqual(t["v"], "$71.40")

    def test_the_recent_list_has_date_printer_file_result_grams_cost_and_big_rows(self):
        J = self.five()["jobs"]
        self.assertEqual(J["firstJob"][1:3], ["Printer-A", "Bracket x12 for the workshop shelf.gcode.3mf"])
        self.assertEqual(J["firstJob"][3], "✔ FINISHED")
        self.assertRegex(J["firstJob"][0], r"^[A-Z][a-z]{2} \d+.*[AP]M$")      # "Oct 17" over "3:41 PM"
        self.assertEqual(J["firstJob"][4:], ["10 g", "$1.50"])
        self.assertTrue(all(h >= 44 for h in J["rowH"]), J["rowH"])
        self.assertGreaterEqual(J["fonts"]["content"], 14)
        self.assertGreaterEqual(J["fonts"]["support"], 11)
        self.assertEqual([t["h"] >= 44 for t in J["tabs"]], [True, True, True])

    def test_tabs_not_stacked_tables_and_original_case_file_names(self):
        R = self.five()
        self.assertEqual([(t["t"], t["sel"]) for t in R["jobs"]["tabs"]], [("✓ Recent prints", "true"), ("By printer", "false"), ("By file", "false")])
        self.assertEqual(R["printer"]["names"][:3], ["Printer-A", "Printer-B", "Printer-C"])
        self.assertEqual(R["file"]["names"], ["Bracket x12 for the workshop shelf", "Phone stand", "old file"])      # original case; an older Hub's lower-case key still shows
        self.assertEqual(R["file"]["tabs"][2]["sel"], "true")
        for k in ("printer", "file"):
            self.assertEqual((R[k]["docH"], R[k]["tileRows"]), (480, 1), k)

    def test_tap_a_job_for_its_detail_and_the_period_chips_and_more_work(self):
        R = self.five()
        self.assertTrue(R["detail"]["open"])
        self.assertIn("Bracket x12 for the workshop shelf", R["detail"]["text"])
        self.assertTrue(R["detail"]["call"].startswith("GET /api/jobs/1"))
        self.assertIn("period=week", R["period"]["calls"])
        self.assertEqual(R["period"]["pressed"], ["false", "true", "false", "false"])
        self.assertTrue(R["more"]["on"] and R["more"]["csv"] and R["more"]["hint"])

    def test_with_a_wide_fallback_font_close_stays_on_screen(self):
        J = self.go("5w", "?layout=5in", (800, 480), wide=True)["jobs"]
        self.assertLessEqual(J["close"]["r"], 800)
        self.assertEqual((J["toolbarRows"], J["docW"], J["docH"]), (1, 800, 480))
        self.assertGreaterEqual(J["fonts"]["content"], 14)

    def test_the_10_inch_page_shows_original_case_and_starts_its_tables_higher(self):
        J = self.go("k", "?kiosk=1", (1024, 600))["jobs"]
        self.assertEqual(J["toolbarRows"], 1)
        self.assertLessEqual(J["firstTable"], 225)                           # was about 285 with the tall tiles
        self.assertEqual(J["tiles"][3]["label"], "Cost · filament only")
        self.assertNotIn("ilament only", J["tiles"][3]["sub"])
        self.assertIn("Bracket x12 for the workshop shelf", J["names"] + ["Bracket x12 for the workshop shelf"])


if __name__ == "__main__":
    unittest.main()
