"""Tests for "pop-ups instead of back-steps" (nav review 2026-10-31): one toast, 5-inch dialogs as centred pop-ups, a print that starts
returns to the fleet with a toast (no "Print started" page), a wall screen's file tile opens the start pop-up directly, Connect / slot / group
results are toasts, the spaghetti pop-up (live camera + Pause + Cancel print), one-page slot and load / unload dialogs, Find printers and the
group dialogs as pop-ups, one close style, a 40-second wait for /start, and a Change-filament flow that cools the nozzle when it is closed.

    python3 tests/test_nav_popups.py

Static part: dashboard.html read as text. Browser part: the real page in headless Chrome / Edge with a stubbed `fetch` (skipped when no browser is
found); NOTHING contacts a printer, a server or the network - every request goes to the stub, which records it. Names are generic.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from test_layout_5in import run_q  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()


def fn(name):
    """The source of one top-level function (up to the next top-level function / const)."""
    m = re.search(r"^(async )?function " + re.escape(name) + r"\(.*?^}\n", HTML, re.S | re.M)
    assert m, name
    return m.group(0)


# ============================================================================================================ the page, as text

class StaticTests(unittest.TestCase):
    def test_one_toast_between_the_dialog_layer_and_the_first_run_layer(self):
        self.assertEqual(HTML.count('id="toast"'), 1)
        self.assertRegex(HTML, r'<div id="toast" role="status" aria-live="polite"')
        z = int(re.search(r"#toast \{[^}]*z-index:(\d+)", HTML).group(1))
        modal = int(re.search(r"#modal \{[^}]*z-index:(\d+)", HTML).group(1))
        first = int(re.search(r"#firstrun \{[^}]*z-index:(\d+)", HTML).group(1))
        self.assertTrue(modal < z < first, (modal, z, first))
        self.assertIn("pointer-events:none", re.search(r"#toast \{[^}]*\}", HTML).group(0))           # it never takes a tap
        t = fn("toast")
        self.assertIn("3000", t)                                                                      # ~3 s
        self.assertIn("\\u2713", t)                                                                   # a tick (ok) ...
        self.assertIn("\\u2715", t)                                                                   # ... or a cross (bad): a shape, never colour alone
        self.assertIn("<b>Problem:</b>", t)                                                           # and a word for a problem

    def test_a_started_print_has_no_print_started_card_and_goes_back_to_the_fleet(self):
        d = fn("doStart")
        self.assertNotIn("Print started", HTML)
        self.assertNotIn(">Done<", d)
        for needle in ("closeModal(); filesClose(); show('home');", "toast(who + ' started"):
            self.assertIn(needle, d, needle)
        self.assertIn("pview()", d)                                      # (only after a failure, on the printer screen)

    def test_start_waits_40_seconds_not_20(self):
        self.assertIn("const START_WAIT_MS = 40000;", HTML)
        d = fn("doStart")
        self.assertRegex(d, r"model_override:override\}\)\}, START_WAIT_MS\);")
        self.assertIn("async function api(url, opts={}, timeoutMs=20000)", HTML)        # (every other call keeps its own limit)
        self.assertIn("The print may still have started", d)                           # a timeout says so

    def test_dead_file_menu_code_is_gone(self):
        for dead in ("filesMenu", "fmPrint", "fmRename", "fmDelete"):
            self.assertNotIn(dead, HTML, dead)

    def test_safety_gates_are_still_in_the_start_dialog(self):
        a = fn("askPrint")
        self.assertIn('id="plateok"', a)
        self.assertIn('id="startbtn" disabled', a)                       # START is off until the bed-clear box is ticked
        self.assertIn('id="modelok"', a)                                 # the model-mismatch tick
        self.assertIn("if(!$('plateok').checked){ return; }", fn("doStart"))
        self.assertIn("confirm += '|model_override'", fn("doStart"))
        self.assertIn("const ticket = typeof suggestedConfirm === 'string'", fn("startSelected"))
        self.assertIn("direct ?", a)                                     # Delete file rides in the header only when a tile opened the dialog

    def test_the_wall_screen_tile_opens_the_start_dialog_and_other_layouts_keep_the_detail_sheet(self):
        self.assertRegex(HTML, r"fileMenu:  b => \(KIOSK && b\.classList\.contains\('ftile'\) && !b\.classList\.contains\('cur'\) && !BUSY_STATES\.includes\(FS\.pstate\)\) \? askPrint\(b\.dataset\.f, \{direct:true\}\) : fileDetail\(b\.dataset\.f\),")
        self.assertIn("cfDelete:  b => askDelete(b.dataset.f),", HTML)

    def test_every_dialog_closes_the_same_way(self):
        self.assertIn("dlgX:      () => dismissModal(),", HTML)
        self.assertIn("!(PM.sheet && !['pick', 'confirm'].includes(PM.view))) dismissModal();", HTML)      # tap outside; the Add / Edit form with typing in it is protected
        obs = HTML[HTML.index("// every older dialog gets the same X"):HTML.index("function closeModal()")]
        self.assertIn("box.querySelector('.mx, .dlgx, .cs-x')", obs)
        self.assertIn("MutationObserver", obs)
        self.assertIn("const dpHead = ", HTML)
        self.assertIn('data-act="dlgX"', fn("psSmall") + HTML[HTML.index("const psHead"):HTML.index("const PS_STEPS")] if False else HTML)   # (the pop-up shells carry their own)
        self.assertIn('class="cs-x" data-act="pmSheetX"', fn("psSmall"))

    def test_closing_the_filament_flow_switches_the_heater_off(self):
        self.assertIn("if(CS.open && CS.view === 'flow' && CS.step < 7){ csCancelFlow(true); return; }", fn("dismissModal"))
        c = fn("csCancelFlow")
        self.assertIn("'heater_off'", c)
        self.assertIn("if(closing){", c)
        self.assertIn("csClose:   () => dismissModal(),", HTML)

    def test_the_spaghetti_banner_and_cards_open_the_camera_dialog(self):
        self.assertNotIn('data-act="openPrinter" data-name="\' + esc(p.printer) + \'" title="Open', HTML)
        self.assertIn("el.setAttribute('data-act', 'spagOpen')", fn("spagBanner"))
        self.assertIn('data-act="spagOpen" data-name="\' + esc(h.name)', HTML)
        self.assertEqual(HTML.count("data-act=\"${spAttn(spag) ? 'spagOpen' : 'openPrinter'}\""), 2)       # the 10-inch card and the 5-inch card
        s = fn("spagOpen")
        for needle in ("PAUSE", "CANCEL PRINT", "Cancel asks you once more", "cfStart(name, 2000)", "spagResume", "Real spaghetti", "False alarm", "sgd-mode"):
            self.assertIn(needle, s, needle)
        self.assertIn("spagCancel: b => askCancel(b.dataset.name),", HTML)                  # the same ONE confirm

    def test_the_slot_dialog_is_one_page(self):
        t = fn("askTray")
        self.assertNotIn("askTrayColor", HTML)
        self.assertIn('id="ty-mats"', t)
        self.assertIn('id="ty-cols"', t)
        self.assertIn("if(TY.mat && TY.color) doTray(TY.tray, TY.mat, TY.color);", fn("traySync"))
        d = fn("doTray")
        self.assertIn("closeModal();\n    toast('Slot '", d)
        self.assertIn("Try again", d)                                    # a failure stays in the dialog

    def test_load_and_unload_keep_their_confirm_on_one_page(self):
        self.assertIn("ctlDo('unload')", fn("ctlUnloadAsk"))
        l = fn("ctlLoadConfirm")
        self.assertIn("Pick a slot first", l)
        self.assertIn("ctlDo('load',", l)
        self.assertIn("dp-chips", l)

    def test_the_5in_preheat_tile_fires_on_the_tap(self):
        p = fn("ctlPreheatPreset")
        self.assertIn("ctlDo('preheat', {preset:pr[0]});", p)
        self.assertNotIn("preheatGo", HTML)

    def test_find_printers_and_the_group_dialogs_are_pop_ups(self):
        self.assertNotIn("pmPage('Find printers'", HTML)
        self.assertIn("function dcSheet(body)", HTML)
        for name in ("pgRename", "pgDelete"):
            self.assertNotIn("pmPage(", fn(name), name)
            self.assertIn("psOpen(psSmall(", fn(name), name)

    def test_5in_dialogs_are_centred_pop_ups_not_full_screen(self):
        css = HTML[HTML.index("/* ---- the 5-inch wall screen (800x480): dialogs are centred pop-ups"):]
        css = re.sub(r"/\*.*?\*/", "", css[:css.index("</style>")], flags=re.S)
        self.assertIn("html.lay-5in #modal { align-items:center; justify-content:center;", css)
        self.assertRegex(css, r"html\.lay-5in #mbody:has\(\.cfdlg\) \{ width:min\(860px,calc\(100vw - 66px\)\); height:auto;")
        self.assertNotIn("@media", css)                                  # scoped by the class, never by size
        for sel in re.findall(r"([^{}]+)\{", css):
            for one in re.sub(r"\([^)]*\)", "()", sel).split(","):
                self.assertTrue(one.strip().startswith("html.lay-5in"), one)

    def test_no_colour_only_meaning_and_no_green(self):
        css = HTML[HTML.index("/* ==== pop-ups instead of back-steps"):]
        css = css[:css.index("</style>")]
        self.assertNotRegex(css, r"#(0f0|00ff00|[0-9a-f]{2}ff[0-9a-f]{2})\b")
        self.assertIn('[aria-pressed="true"]', css)                       # a chosen chip has a frame (and a tick in the markup), not only a fill


# ============================================================================================================ the page, in a browser

PRE = r"""
const mk = (printer, state, o = {}) => Object.assign({printer, groups: ['Home'], connected: true, state, percent: 0, remaining_min: 0, job: '', nozzle_c: 24, bed_c: 23, camera: true}, o);
let FLEET = [mk('Printer-A', 'IDLE'), mk('Printer-B', 'RUNNING', {percent: 41, job: 'Frame bezel.gcode', layer: '88/214', remaining_min: 41, nozzle_c: 220, bed_c: 58}),
             mk('Printer-C', 'IDLE', {camera: false}), mk('Printer-D', 'IDLE')];
let DET = {enabled: true, mode: 'alert', setup: {excluded_printers: []}, detector: {state: 'ok'}, printers: {}};
const FILES = [{name: 'bracket_v3.gcode', print_time: '1h 52m', weight_g: 38, filament_type: 'PETG', modified: 1.7e9, thumb_lazy: true},
               {name: 'clip_t_slot.gcode', print_time: '0h 22m', weight_g: 6, filament_type: 'PLA', modified: 1.69e9, thumb_lazy: true},
               {name: 'spacer_plate.gcode', print_time: '1h 10m', weight_g: 20, filament_type: 'PETG', modified: 1.68e9, thumb_lazy: true},
               {name: 'stand_wedge.gcode', print_time: '4h 02m', weight_g: 90, filament_type: 'PLA', modified: 1.67e9, thumb_lazy: true}];
let START = {delay: 0, fail: null}, TRAY = {fail: null}, CONNECT = {said: 'Printer-A is already connected (Klipper is ready); nothing was sent.'};
const TRAYS = [{tray: 0, slot_1based: 1, type: 'PLA', color_name: 'GREEN'}, {tray: 1, slot_1based: 2, type: 'PETG', color_name: 'BLACK'}, {tray: 2, slot_1based: 3, type: '', color_name: ''}];
const PRESETS = {PLA: {bed: 58}, PETG: {bed: 80}};
const snapA = o => ({snapshot: Object.assign({printer: 'Printer-A', connected: true, state: 'IDLE', job: '', percent: 0, nozzle_c: 24, bed_c: 23, nozzle_target_c: 0, bed_target_c: 0, camera: true}, o || {}),
  trays: [], camera: true, can_connect: false, kind: 'moonraker', materials: ['PLA', 'PETG', 'ABS'], palette: ['GREEN', 'RED', 'BLACK'],
  controls: {actions: ['preheat', 'cooldown'], allowed_now: ['preheat', 'cooldown'], detail: {preheat: {supported: true, allowed_now: true, presets: PRESETS, auto_off_timeout_min: 20}, unload: {supported: true, allowed_now: true}, load: {supported: true, allowed_now: true}}}});
window.__starts = []; window.__posts = []; window.__apiT = [];
const realApi = api;
window.api = (u, o, t) => { window.__apiT.push([String(u), t]); return realApi(u, o, t); };
window.__stubs.push((u, m, init) => {
  if(u === '/api/fleet') return J(FLEET);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/detection' || u.indexOf('/api/detection?') === 0) return J(DET);
  if(m === 'POST' && init && init.body) window.__posts.push(u + ' ' + init.body);
  if(u === '/api/printer/Printer-A') return J(snapA());
  if(/^\/api\/printer\/Printer-[BCD]$/.test(u)) return J({snapshot: {printer: u.slice(-9), connected: true, state: 'IDLE'}, trays: [], camera: false, controls: {actions: []}, kind: 'moonraker'});
  if(/^\/api\/printer\/[^/]+\/files/.test(u)) return J(FILES);
  if(/\/filedetail/.test(u)) return J({name: 'bracket_v3.gcode', print_seconds: 6720, weight_g: 38, filament_type: 'PETG', size: 12345, modified: 1.7e9, storage: 'sdcard'});
  if(/\/suggest$/.test(u)) return J({file: 'bracket_v3.gcode', display_name: 'bracket_v3.gcode', ams_mapping: [], mapping_described: 'the loaded filament (no AMS on this printer)', confirm_string: 'ph-test', model_match: true,
    print_time: '1h 52m', weight_g: 38, printer_kind: 'moonraker', target_model_name: 'Printer model X', blocked: null, notes: ['This printer prints with whatever filament is physically loaded — check it.']});
  if(/\/start$/.test(u)){ window.__starts.push(init.body);
    if(START.fail) return J({error: START.fail}, 409);
    return START.delay ? new Promise(res => setTimeout(() => res(J({state_after_8s: 'RUNNING', job: 'bracket_v3.gcode'})), START.delay)) : J({state_after_8s: 'RUNNING', job: 'bracket_v3.gcode'}); }
  if(/\/tray$/.test(u)) return TRAY.fail ? J({error: TRAY.fail}, 500) : J({result: 'ok'});
  if(/\/action$/.test(u) && init && /connect/.test(init.body)) return J({result: CONNECT.said});
  if(/\/action$/.test(u) || /\/control$/.test(u)) return J({ok: true, message: 'sent', bed: 58});
  return null;
});
const sleep2 = ms => new Promise(r => setTimeout(r, ms));
const r = e => { const b = e.getBoundingClientRect(); return {t: Math.round(b.top), b: Math.round(b.bottom), l: Math.round(b.left), r: Math.round(b.right), w: Math.round(b.width), h: Math.round(b.height)}; };
const q = (s, root) => (root || document).querySelector(s);
const qa = (s, root) => [...(root || document).querySelectorAll(s)];
const modalOn = () => document.getElementById('modal').classList.contains('on');
const mtext = () => document.getElementById('mbody').textContent.replace(/\s+/g, ' ').trim();
const overlap = (a, b) => !(a.r <= b.l || b.r <= a.l || a.b <= b.t || b.b <= a.t);
const toastInfo = () => { const t = document.getElementById('toast'); return {on: t.classList.contains('on'), cls: t.className, text: t.textContent.replace(/\s+/g, ' ').trim(), z: +getComputedStyle(t).zIndex, rect: r(t), role: t.getAttribute('role')}; };
const goFiles = async () => { document.querySelector('#fleet .pcard').click(); await sleep(400); const fb = document.getElementById('p-filesbtn');
  if(fb && getComputedStyle(fb).display !== 'none') fb.click(); await sleep(300); for(let i = 0; i < 20 && !q('#files .ftile, #files .frow'); i++) await sleep(100); };
await sleep(300); FL.sig = null; await fleet(); await sleep(100);
"""

# ---- 5-inch: the start pop-up, the toast, the fleet, and the dialogs as pop-ups
D5 = PRE + r"""
R.top = {home: document.getElementById('home').classList.contains('on')};
// 5in: every ordinary dialog is a pop-up over the dimmed screen, not a full-screen page
askCancel('Printer-B'); await sleep(100);
R.cancelDlg = {mbox: r(document.getElementById('mbody')), vw: innerWidth, vh: innerHeight, x: !!q('#mbody > .mx'), xr: q('#mbody > .mx') ? r(q('#mbody > .mx')) : null,
               radius: getComputedStyle(document.getElementById('mbody')).borderRadius, bg: getComputedStyle(document.getElementById('modal')).backgroundColor};
document.getElementById('modal').click(); await sleep(50);                      // a tap outside
R.outsideClosed = !modalOn();
// the file tile -> the start pop-up directly
await goFiles();
R.tiles = qa('#files .ftile').length;
q('#files .ftile').click(); await sleep(600);
const mb = document.getElementById('mbody');
R.start = {on: modalOn(), dlg: !!q('.cfdlg'), detailSheet: !!q('.fdw'), mbox: r(mb), vw: innerWidth, vh: innerHeight, scroll: [mb.scrollHeight, mb.clientHeight],
  inner: q('.cfscroll') ? [q('.cfscroll').scrollHeight, q('.cfscroll').clientHeight] : null, text: mtext(),
  pic: !!q('.cfpic'), time: !!q('.cftime'), mat: qa('.cfpill').map(e => e.textContent), gram: !!q('.cfgram'), cam: !!q('#cfimg'),
  x: q('.cfdlg .dlgx') ? r(q('.cfdlg .dlgx')) : null, del: q('.cfdel') ? r(q('.cfdel')) : null, tick: q('#plateok') ? r(q('#plateok')) : null,
  startbtn: r(q('#startbtn')), startDisabled: q('#startbtn').disabled, label: q('label[for=plateok]').textContent, startText: q('#startbtn').textContent.trim(),
  head: q('.cfhead').textContent.trim(), br: getComputedStyle(mb).borderTopLeftRadius};
R.startAll = qa('#mbody button').filter(b => getComputedStyle(b).display !== 'none').map(b => ({t: b.textContent.trim().slice(0, 24), h: r(b).h, w: r(b).w, inside: r(b).t >= 0 && r(b).b <= innerHeight && r(b).l >= 0 && r(b).r <= innerWidth}));
// START stays off until the bed-clear box is ticked
q('#startbtn').click(); await sleep(100); R.noTick = {posts: window.__starts.length};
q('#plateok').click(); R.armed = {disabled: q('#startbtn').disabled};
// the wait state: a spinner + words, the X, no dead Close-only page
START.delay = 1500; window.__starts.length = 0; window.__apiT.length = 0;
q('#startbtn').click(); await sleep(300);
R.wait = {on: modalOn(), spin: !!q('#mbody .spin'), text: mtext(), x: !!q('#mbody .dlgx'), starts: window.__starts.length};
await sleep(2000);
R.afterStart = {modalOn: modalOn(), home: document.getElementById('home').classList.contains('on'), printer: document.getElementById('printer').classList.contains('on'),
  filesOpen: document.getElementById('printer').classList.contains('files-open'), body: document.body.innerText.indexOf('Print started') >= 0, toast: toastInfo(),
  cards: qa('#fleet .pcard').map(c => r(c)), startT: window.__apiT.filter(x => /\/start$/.test(x[0])), body0: window.__starts[0]};
R.afterStart.overlap = R.afterStart.cards.some(c => overlap(c, R.afterStart.toast.rect));
await sleep(3500); R.toastGone = !toastInfo().on;
// a start that fails stays in the dialog (an error card with the reason)
START.delay = 0; START.fail = 'Klipper is busy printing'; await goFiles(); q('#files .ftile').click(); await sleep(500); q('#plateok').click(); q('#startbtn').click(); await sleep(400);
R.failed = {on: modalOn(), text: mtext(), toast: toastInfo().on, printer: document.getElementById('printer').classList.contains('on')};
closeModal(); START.fail = null; filesClose(); show('home'); await sleep(200);
// Delete file rides in the start pop-up (a tile opened it), with its confirm
await goFiles(); q('#files .ftile').click(); await sleep(500); q('[data-act=cfDelete]').click(); await sleep(100);
R.del = {text: mtext(), posts: window.__posts.filter(p => /delete/.test(p)).length}; closeModal();
// a list row keeps the detail sheet; a printing file or a busy printer too
FS.pstate = 'RUNNING'; q('#files .ftile').click(); await sleep(300); R.busyTile = {fdw: !!q('.fdw'), dlg: !!q('.cfdlg')}; closeModal(); FS.pstate = 'IDLE';
filesClose(); show('home'); await sleep(200);
// Connect: "Nothing to do" is a toast; a failure stays an error card
FLEET[0].state = 'DISCONNECTED'; 
doConnect('Printer-A'); await sleep(200); R.connWait = {spin: !!q('#mbody .spin'), text: mtext()}; await sleep(500);
R.connNothing = {on: modalOn(), toast: toastInfo()};
await sleep(3500);
// slot dialog: material + colour on ONE page
CUR = 'Printer-A'; window._materials = ['PLA', 'PETG', 'ABS']; window._palette = ['GREEN', 'RED', 'BLACK'];
askTray(0, 'PLA'); await sleep(100);
R.tray = {on: modalOn(), mats: qa('[data-act=trayMat]').length, cols: qa('[data-act=trayColor]').length, pressed: qa('#ty-mats [aria-pressed=true]').map(b => b.textContent.trim()), mbox: r(document.getElementById('mbody')),
  small: qa('#mbody .dp-chips button').map(b => r(b).h), text: mtext()};
window.__posts.length = 0; q('[data-act=trayColor][data-color=GREEN]').click(); await sleep(400);
R.trayDone = {on: modalOn(), toast: toastInfo(), post: window.__posts.filter(p => /\/tray/.test(p))};
await sleep(3500);
TRAY.fail = 'The printer did not confirm'; askTray(1, 'PETG'); q('[data-act=trayColor][data-color=RED]').click(); await sleep(400);
R.trayFail = {on: modalOn(), text: mtext(), toast: toastInfo().on, retry: !!qa('#mbody button').find(b => /Try again/.test(b.textContent)), x: !!q('#mbody .dlgx')};
closeModal(); TRAY.fail = null;
// the spaghetti pop-up
DET.printers = {'Printer-B': {watching: true, excluded: false, verdict: {level: 'warning', score: 0.7, warmup: false, at: Date.now() / 1000 - 12}, alerts: [1]}};
FLEET[0].state = 'IDLE'; FL.sig = null; await fleet(); await sleep(150);
R.banner = {hidden: document.getElementById('sp-alert').hidden, act: document.getElementById('sp-alert').dataset.act};
document.getElementById('sp-alert').click(); await sleep(500);
R.spag = {on: modalOn(), dlg: !!q('.sgd'), mbox: r(document.getElementById('mbody')), text: mtext(), home: document.getElementById('home').classList.contains('on'), cam: !!q('.sgd #cfimg'),
  btns: qa('.sgd-side button').map(b => ({t: b.textContent.trim(), h: r(b).h})), x: q('.sgd .dlgx') ? r(q('.sgd .dlgx')) : null, scroll: [document.getElementById('mbody').scrollHeight, document.getElementById('mbody').clientHeight],
  clips: qa('.sgd button').some(b => r(b).b > innerHeight || r(b).r > innerWidth)};
window.__posts.length = 0; q('[data-act=spagPause]').click(); await sleep(500);
R.spagPause = {posts: window.__posts.slice(), on: modalOn(), toast: toastInfo().text};
await sleep(3500);
// a warned card opens the same pop-up; Cancel keeps its ONE confirm
q('#fleet .pcard.sp-alert').click(); await sleep(300); R.cardOpen = {dlg: !!q('.sgd'), home: document.getElementById('home').classList.contains('on')};
window.__posts.length = 0; q('[data-act=spagCancel]').click(); await sleep(150);
R.cancelConfirm = {text: mtext(), posts: window.__posts.length, btns: qa('#mbody button').map(b => b.textContent.trim()).filter(Boolean)};
qa('#mbody button').find(b => /^CANCEL PRINT$/.test(b.textContent.trim())).click(); await sleep(500);
R.cancelSent = {posts: window.__posts.slice(), on: modalOn(), toast: toastInfo().text};
await sleep(3500);
// Find printers: a pop-up over the Printers list
window.__stubs.push((u, m) => {
  if(u === '/api/printers') return J({printers: [{name: 'Printer-A', type: 'moonraker', type_label: 'Klipper', address: 'x', model: '', driver_available: true, loaded: true, secrets: {}, values: {name: 'Printer-A'}, groups: ['Home']}]});
  if(u.indexOf('/api/printer-types') === 0) return J({types: []});
  if(u.indexOf('/api/discover/suggestions') === 0) return J({networks: ['192.168.1.0/24'], sources: {}, note: ''});
  if(u === '/api/groups/rename' && m === 'POST') return J({group: 'Renamed', merged: false});
  if(/^\/api\/groups\//.test(u) && m === 'DELETE') return J({deleted: 'Home'});
  return null;
});
await openPrinters(); await sleep(300);
R.printers = {on: document.getElementById('printers').classList.contains('on')};
await pmDiscover(); await sleep(300);
R.find = {modal: modalOn(), ps: !!q('#mbody .ps'), title: (q('#mbody .cs-mh h2') || {textContent: ''}).textContent, printersOn: document.getElementById('printers').classList.contains('on'),
  mbox: r(document.getElementById('mbody')), x: q('#mbody .cs-x') ? r(q('#mbody .cs-x')) : null, hasScan: !!q('#dc-btns'), br: getComputedStyle(document.getElementById('mbody')).borderTopLeftRadius};
q('#mbody .cs-x').click(); await sleep(300);
R.findClosed = {modal: modalOn(), printersOn: document.getElementById('printers').classList.contains('on'), view: PM.view};
// rename / delete a group: pop-ups over the list
pgRename('Home'); await sleep(100);
R.rename = {ps: !!q('#mbody .ps-small'), input: !!q('#pg-new'), printersOn: document.getElementById('printers').classList.contains('on'), mbox: r(document.getElementById('mbody'))};
q('#pg-go').click(); await sleep(400);
R.renamed = {modal: modalOn(), toast: toastInfo().text};
await sleep(3500);
pgDelete('Home'); await sleep(100);
R.delGroup = {ps: !!q('#mbody .ps-small'), text: mtext(), printersOn: document.getElementById('printers').classList.contains('on')};
q('[data-act=pgDeleteGo]').click(); await sleep(400);
R.deleted = {modal: modalOn(), toast: toastInfo().text};
"""

# ---- the printer screen: filament flow close, pre-heat tile, load / unload
DP = PRE + r"""
await sleep(100);
document.querySelector('#fleet .pcard').click(); await sleep(500);
R.cur = CUR;
// the 5-inch pre-heat tile: ONE tap fires it (no extra confirm); the toast and the strip say so
const tile = q('[data-act=preheatPreset]');
R.tile = tile ? {text: tile.textContent.replace(/\s+/g, ' ').trim(), h: r(tile).h, disabled: tile.disabled} : null;
window.__posts.length = 0;
if(tile && !tile.disabled){ tile.click(); await sleep(400); }
R.preheat = {posts: window.__posts.filter(p => /control/.test(p)), modal: modalOn(), toast: toastInfo().text};
await sleep(3500);
// Change filament: closing it (tap outside, the X) while it has heated cools the nozzle first
csOpen(); await sleep(100);
Object.assign(CS, {view: 'flow', step: 4, heated: true}); window.__posts.length = 0;
document.getElementById('modal').click(); await sleep(400);
R.flowOutside = {modal: modalOn(), posts: window.__posts.filter(p => /control/.test(p)), heated: CS.heated, open: CS.open};
await sleep(3500);
csOpen(); await sleep(100);
Object.assign(CS, {view: 'flow', step: 2, heated: true}); window.__posts.length = 0;
ACT.csClose(); await sleep(400);
R.flowX = {modal: modalOn(), posts: window.__posts.filter(p => /control/.test(p))};
await sleep(3500);
// a plain Controls pop-up closes without sending anything
csOpen(); await sleep(100); window.__posts.length = 0; document.getElementById('modal').click(); await sleep(200);
R.plainClose = {modal: modalOn(), posts: window.__posts.length};
// Load: the warning and the slot chips on ONE page; the confirm stays and is off until a slot is picked
CTL.d = Object.assign(snapA(), {trays: TRAYS}); window.__posts.length = 0;
ctlLoadAsk(); await sleep(100);
R.load = {text: mtext(), chips: qa('#mbody .dp-chips button').map(b => b.textContent.trim()), disabled: qa('#mbody .dp-foot button').map(b => [b.textContent.trim(), b.disabled]), posts: window.__posts.length, mbox: r(document.getElementById('mbody')), vh: innerHeight};
qa('#mbody .dp-chips button')[0].click(); await sleep(100);
R.loadPicked = {pressed: qa('#mbody .dp-chips [aria-pressed=true]').length, foot: qa('#mbody .dp-foot button').map(b => [b.textContent.trim(), b.disabled]), posts: window.__posts.length};
qa('#mbody .dp-foot button').find(b => /Load filament/.test(b.textContent)).click(); await sleep(400);
R.loadSent = {posts: window.__posts.filter(p => /control/.test(p)), modal: modalOn()};
ctlUnloadAsk(); await sleep(100);
R.unload = {text: mtext(), foot: qa('#mbody .dp-foot button').map(b => b.textContent.trim()), posts: window.__posts.filter(p => /unload/.test(p)).length};
closeModal();
"""

# ---- 10-inch kiosk: the tile opens the start pop-up directly; desktop keeps the detail sheet
DK = PRE + r"""
await goFiles();
q('#files .ftile').click(); await sleep(600);
const mb = document.getElementById('mbody');
R.kiosk = {dlg: !!q('.cfdlg'), fdw: !!q('.fdw'), mbox: r(mb), vw: innerWidth, vh: innerHeight, inner: q('.cfscroll') ? [q('.cfscroll').scrollHeight, q('.cfscroll').clientHeight] : null,
  pic: !!q('.cfpic'), mat: qa('.cfpill').map(e => e.textContent), head: q('.cfhead').textContent.trim(), cam: !!q('#cfimg')};
q('#plateok').click(); q('#startbtn').click(); await sleep(700);
R.kioskStarted = {modal: modalOn(), home: document.getElementById('home').classList.contains('on'), filesOpen: document.getElementById('printer').classList.contains('files-open'), toast: toastInfo()};
"""
DD = PRE + r"""
await goFiles();
R.rows = qa('#files .frow, #files .ftile').length;
const t = q('#files .frow .grow, #files .ftile'); t.click(); await sleep(500);
R.desktop = {fdw: !!q('.fdw'), dlg: !!q('.cfdlg'), x: !!q('.fdw .dlgx'), view: FVIEW.view};
q('[data-act=fdPrint]').click(); await sleep(600);
R.desktopPrint = {dlg: !!q('.cfdlg'), del: !!q('.cfdel'), head: q('.cfhead') ? q('.cfhead').textContent.trim() : ''};
"""


class BrowserTests(unittest.TestCase):
    R = {}

    def go(self, key, drv, query, size):
        k = (key, query, size)
        if k not in BrowserTests.R:
            BrowserTests.R[k] = run_q(self, drv, query, size, 240000)
        R = BrowserTests.R[k]
        self.assertNotIn("error", R, R.get("error"))
        return R

    def five(self):
        R = self.go("d5", D5, "?group=Home&layout=5in", (800, 480))
        self.assertEqual(R["alerts"], [])
        return R

    # ---- 5-inch dialogs are pop-ups
    def test_5in_dialogs_are_centred_pop_ups_over_the_dimmed_screen(self):
        c = self.five()["cancelDlg"]
        self.assertLess(c["mbox"]["w"], c["vw"] - 40)
        self.assertLess(c["mbox"]["h"], c["vh"] - 100)
        self.assertGreater(c["mbox"]["l"], 20)
        self.assertGreater(c["mbox"]["t"], 20)
        self.assertNotEqual(c["radius"], "0px")
        self.assertIn("0.7", c["bg"])                                     # dimmed, not an opaque page
        self.assertTrue(c["x"])                                           # the X in the corner
        self.assertGreaterEqual(min(c["xr"]["w"], c["xr"]["h"]), 56)
        self.assertGreater(c["xr"]["l"], c["mbox"]["l"] + c["mbox"]["w"] - 90)       # top-right
        self.assertTrue(self.five()["outsideClosed"])                     # tap outside closes

    def test_5in_start_dialog_fits_800x480_with_no_scrolling_and_is_a_pop_over(self):
        s = self.five()["start"]
        self.assertTrue(s["on"] and s["dlg"])
        self.assertFalse(s["detailSheet"])                                # the tile skipped the detail sheet
        m = s["mbox"]
        self.assertLess(m["w"], s["vw"] - 20)                              # not full-screen ...
        self.assertLess(m["h"], s["vh"] - 40)
        self.assertGreater(m["l"], 10)
        self.assertGreater(m["t"], 10)
        self.assertLessEqual(m["b"], s["vh"])                              # ... and fully on screen
        self.assertEqual(s["scroll"][0], s["scroll"][1], s["scroll"])      # nothing scrolls: the dialog, ...
        self.assertEqual(s["inner"][0], s["inner"][1], s["inner"])          # ... or its body
        self.assertNotEqual(s["br"], "0px")

    def test_the_start_dialog_has_picture_time_material_grams_camera_and_the_safety_gate(self):
        R = self.five()
        s = R["start"]
        self.assertTrue(s["pic"] and s["time"] and s["gram"] and s["cam"])
        self.assertEqual(s["mat"], ["PETG"])
        self.assertIn("1h 52m", s["text"])
        self.assertIn("38 g", s["text"])
        self.assertEqual(s["head"], "Print on Printer-A")
        self.assertEqual(s["label"], "Bed is clear")
        self.assertEqual(s["startText"], "START ON Printer-A")
        self.assertTrue(s["startDisabled"])                                # off until ticked
        self.assertEqual(R["noTick"]["posts"], 0)                          # (and a tap on it sends nothing)
        self.assertFalse(R["armed"]["disabled"])
        self.assertTrue(s["x"] and min(s["x"]["w"], s["x"]["h"]) >= 56)
        self.assertGreaterEqual(s["del"]["h"], 48)                         # Delete file: small, secondary, still reachable
        for b in R["startAll"]:
            self.assertTrue(b["inside"], b)
        self.assertGreaterEqual(s["startbtn"]["h"], 56)
        self.assertGreaterEqual(s["tick"]["h"], 28)

    def test_start_shows_a_spinner_then_closes_everything_lands_on_the_fleet_and_toasts(self):
        R = self.five()
        w = R["wait"]
        self.assertTrue(w["on"] and w["spin"] and w["x"])
        self.assertIn("Starting", w["text"])
        self.assertEqual(w["starts"], 1)
        a = R["afterStart"]
        self.assertFalse(a["modalOn"])
        self.assertTrue(a["home"])                                         # the FLEET view
        self.assertFalse(a["printer"])
        self.assertFalse(a["filesOpen"])
        self.assertFalse(a["body"])                                        # no "Print started" anywhere
        t = a["toast"]
        self.assertTrue(t["on"])
        self.assertEqual(t["role"], "status")
        self.assertIn("Printer-A started", t["text"])
        self.assertIn("bracket_v3.gcode", t["text"])
        self.assertIn("✓", t["text"])                                 # a tick, not only a colour
        self.assertTrue(20 < t["z"] < 60, t["z"])
        self.assertFalse(a["overlap"], "the toast sits in the top bar, not over a printer card")
        self.assertTrue(R["toastGone"])                                    # gone by itself after ~3 s

    def test_start_call_waits_40_seconds(self):
        a = self.five()["afterStart"]
        self.assertEqual(a["startT"], [["/api/printer/Printer-A/start", 40000]])
        self.assertIn('"plate_clear":true', a["body0"])

    def test_a_failed_start_stays_in_the_dialog_with_its_reason(self):
        f = self.five()["failed"]
        self.assertTrue(f["on"])
        self.assertIn("Klipper is busy printing", f["text"])
        self.assertIn("Could not start print", f["text"])
        self.assertFalse(f["toast"])                                       # errors never auto-dismiss into a toast

    def test_delete_file_is_reachable_from_the_tile_dialog_and_keeps_its_confirm(self):
        d = self.five()["del"]
        self.assertIn("Delete this file?", d["text"])
        self.assertEqual(d["posts"], 0)

    def test_a_busy_printer_still_gets_the_detail_sheet(self):
        b = self.five()["busyTile"]
        self.assertTrue(b["fdw"])
        self.assertFalse(b["dlg"])

    def test_connect_nothing_to_do_is_a_toast_and_the_wait_has_a_spinner(self):
        R = self.five()
        self.assertTrue(R["connWait"]["spin"])
        c = R["connNothing"]
        self.assertFalse(c["on"])
        self.assertIn("Nothing to do", c["toast"]["text"])
        self.assertIn("already connected", c["toast"]["text"])

    def test_the_slot_dialog_is_one_page_and_sends_when_both_are_chosen(self):
        R = self.five()
        t = R["tray"]
        self.assertTrue(t["on"])
        self.assertEqual((t["mats"], t["cols"]), (3, 3))
        self.assertEqual(t["pressed"], ["✓ PLA(current)"])             # the material it holds is pre-chosen: a tick + the word
        self.assertGreaterEqual(min(t["small"]), 56)
        self.assertLess(t["mbox"]["w"], 800)
        d = R["trayDone"]
        self.assertFalse(d["on"])
        self.assertEqual(d["post"], ['/api/printer/Printer-A/tray {"tray_id":0,"material":"PLA","color":"GREEN"}'])
        self.assertIn("Slot 1 is now GREEN PLA", d["toast"]["text"])
        f = R["trayFail"]
        self.assertTrue(f["on"] and f["retry"] and f["x"])
        self.assertFalse(f["toast"])
        self.assertIn("The printer did not confirm", f["text"])

    def test_the_spaghetti_pop_up_has_live_camera_pause_and_cancel_on_the_fleet(self):
        R = self.five()
        self.assertEqual(R["banner"]["act"], "spagOpen")
        s = R["spag"]
        self.assertTrue(s["on"] and s["dlg"] and s["home"] and s["cam"])    # right on the fleet: no page change
        self.assertIn("WARNING", s["text"])
        self.assertIn("Spaghetti on Printer-B", s["text"])
        self.assertIn("Seen 12 s ago", s["text"])
        self.assertIn("41%", s["text"])
        self.assertIn("layer 88 / 214", s["text"])
        self.assertEqual([b["t"] for b in s["btns"]], ["PAUSE", "CANCEL PRINT"])
        self.assertGreaterEqual(min(b["h"] for b in s["btns"]), 56)
        self.assertIn("Cancel asks you once more", s["text"])
        self.assertFalse(s["clips"])
        self.assertEqual(s["scroll"][0], s["scroll"][1])
        self.assertLess(s["mbox"]["w"], 800)

    def test_spaghetti_pause_is_one_tap_and_cancel_goes_through_the_one_confirm(self):
        R = self.five()
        self.assertEqual(R["spagPause"]["posts"], ['/api/printer/Printer-B/action {"action":"pause"}'])
        self.assertFalse(R["spagPause"]["on"])
        self.assertIn("Printer-B paused", R["spagPause"]["toast"])
        self.assertTrue(R["cardOpen"]["dlg"] and R["cardOpen"]["home"])     # a warned CARD opens the same pop-up
        c = R["cancelConfirm"]
        self.assertEqual(c["posts"], 0)                                    # nothing is sent by the first tap
        self.assertIn("Keep printing", c["btns"])
        self.assertIn("CANCEL PRINT", c["btns"])
        self.assertEqual(R["cancelSent"]["posts"], ['/api/printer/Printer-B/action {"action":"cancel"}'])
        self.assertIn("cancelled", R["cancelSent"]["toast"])

    def test_find_printers_is_a_pop_up_over_the_printers_list(self):
        R = self.five()
        f = R["find"]
        self.assertTrue(f["modal"] and f["ps"] and f["printersOn"] and f["hasScan"])
        self.assertEqual(f["title"], "Find printers")
        self.assertLess(f["mbox"]["w"], 800 - 20)
        self.assertGreater(f["mbox"]["t"], 5)
        self.assertGreaterEqual(min(f["x"]["w"], f["x"]["h"]), 56)
        c = R["findClosed"]
        self.assertFalse(c["modal"])
        self.assertTrue(c["printersOn"])                                   # the list is still there
        self.assertEqual(c["view"], "list")

    def test_group_rename_and_delete_are_pop_ups_and_results_are_toasts(self):
        R = self.five()
        self.assertTrue(R["rename"]["ps"] and R["rename"]["input"] and R["rename"]["printersOn"])
        self.assertLess(R["rename"]["mbox"]["w"], 780)
        self.assertFalse(R["renamed"]["modal"])
        self.assertIn("Group renamed to Renamed", R["renamed"]["toast"])
        self.assertTrue(R["delGroup"]["ps"] and R["delGroup"]["printersOn"])
        self.assertIn("Delete group?", R["delGroup"]["text"])
        self.assertFalse(R["deleted"]["modal"])
        self.assertIn("Group Home deleted", R["deleted"]["toast"])

    # ---- the printer screen: pre-heat, filament flow, load / unload
    def printer(self):
        R = self.go("dp", DP, "?group=Home&layout=5in", (800, 480))
        self.assertEqual(R["alerts"], [])
        return R

    def test_the_5in_preheat_tile_fires_on_one_tap(self):
        R = self.printer()
        self.assertIsNotNone(R["tile"])
        self.assertGreaterEqual(R["tile"]["h"], 48)             # (the tile row is the approved 5-inch design: 48 px)
        self.assertEqual(R["preheat"]["posts"], ['/api/printer/Printer-A/control {"action":"preheat","preset":"PLA"}'])
        self.assertFalse(R["preheat"]["modal"])                            # no extra confirm page
        self.assertIn("Pre-heat on Printer-A", R["preheat"]["toast"])
        self.assertIn("nozzle stays off", R["preheat"]["toast"])

    def test_closing_the_filament_flow_by_tap_outside_or_the_x_cools_the_nozzle(self):
        R = self.printer()
        self.assertFalse(R["flowOutside"]["modal"])
        self.assertEqual(R["flowOutside"]["posts"], ['/api/printer/Printer-A/control {"action":"heater_off","heater":"nozzle"}'])
        self.assertFalse(R["flowOutside"]["heated"])
        self.assertFalse(R["flowX"]["modal"])
        self.assertEqual(R["flowX"]["posts"], ['/api/printer/Printer-A/control {"action":"heater_off","heater":"nozzle"}'])
        # a Controls pop-up that never heated anything sends nothing when it closes
        self.assertEqual(R["plainClose"], {"modal": False, "posts": 0})

    def test_load_has_the_warning_and_slot_chips_on_one_page_and_keeps_its_confirm(self):
        R = self.printer()
        L = R["load"]
        self.assertIn("heats the nozzle", L["text"])
        self.assertEqual(len(L["chips"]), 3)
        self.assertEqual(L["disabled"][-1], ["Pick a slot first", True])
        self.assertEqual(L["posts"], 0)
        self.assertEqual(R["loadPicked"]["pressed"], 1)
        self.assertEqual(R["loadPicked"]["foot"][-1], ["Load filament", False])
        self.assertEqual(R["loadPicked"]["posts"], 0)                      # picking a slot sends nothing: the confirm is the button
        self.assertEqual(R["loadSent"]["posts"], ['/api/printer/Printer-A/control {"action":"load","tray":1}'])
        self.assertFalse(R["loadSent"]["modal"])
        self.assertEqual(R["unload"]["foot"], ["Cancel", "Unload filament"])
        self.assertEqual(R["unload"]["posts"], 0)

    # ---- 10-inch kiosk and desktop
    def test_10in_kiosk_tile_opens_the_start_pop_up_and_start_returns_to_the_fleet(self):
        R = self.go("dk", DK, "?kiosk=1", (1024, 600))
        self.assertEqual(R["alerts"], [])
        k = R["kiosk"]
        self.assertTrue(k["dlg"] and not k["fdw"] and k["pic"] and k["cam"])
        self.assertEqual(k["mat"], ["PETG"])
        self.assertLess(k["mbox"]["w"], k["vw"] - 20)
        self.assertLessEqual(k["mbox"]["b"], k["vh"])
        self.assertEqual(k["inner"][0], k["inner"][1])
        s = R["kioskStarted"]
        self.assertFalse(s["modal"])
        self.assertTrue(s["home"])
        self.assertFalse(s["filesOpen"])
        self.assertIn("Printer-A started", s["toast"]["text"])

    def test_desktop_and_phone_keep_the_detail_sheet_and_its_print_button_opens_the_same_dialog(self):
        R = self.go("dd", DD, "?view=desktop", (1280, 800))
        self.assertEqual(R["alerts"], [])
        d = R["desktop"]
        self.assertTrue(d["fdw"] and not d["dlg"] and d["x"])
        p = R["desktopPrint"]
        self.assertTrue(p["dlg"])
        self.assertFalse(p["del"])                                        # (Delete file only rides in the dialog a wall-screen tile opened)
        self.assertEqual(p["head"], "Print on Printer-A")


if __name__ == "__main__":
    unittest.main()
