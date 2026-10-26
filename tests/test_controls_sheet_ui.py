"""Tests for the Controls pop-up in the dashboard: the Controls button on every printer screen (phone, 10-inch touch / kiosk, desktop, 5-inch with
either theme), the pop-up card (a Move panel with the jog ring, 1 / 10 mm switch, Z and filament columns beside the Nozzle, Bed and Filament cards),
the guided "Change filament..." pop-up (step bar, picture + words, footer), and what each printer family is shown (disabled with the reason in words,
never hidden).

    python3 tests/test_controls_sheet_ui.py

Static part: dashboard.html read as text. Browser part: the real page in headless Chrome / Edge with a stubbed `fetch` (skipped when no browser is
found); NOTHING contacts a printer, a server or the network - every request below goes to the stub, which records it. Names are generic.
CI gotcha: Linux Chrome draws a 15 px scrollbar, so widths are compared with document.documentElement.clientWidth, never a hardcoded viewport width;
the page is also run with the wide fallback font Linux machines have (Verdana / DejaVu Sans), and nothing may be cut off with it.
"""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from test_layout_5in import run_q  # noqa: E402
from test_parity_layouts import PRE, WIDE  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()


def block(start, end):
    i = HTML.index(start)
    return HTML[i:HTML.index(end, i)]


CS_JS = block("// ==== controls sheet ====", "function closeModal()")
CS_CSS = block("/* ==== controls sheet ====", "/* ==== 5-inch wall screen")


# ============================================================================================================ the page, as text

class StaticTests(unittest.TestCase):
    def test_every_sheet_action_is_registered_and_every_registered_one_is_used(self):
        used = set(re.findall(r"""'(cs[A-Z][A-Za-z]*)'""", CS_JS)) - {"csPoll"}
        registered = set(re.findall(r"^  (cs[A-Za-z]+):", CS_JS, re.M))
        for name in used:
            if name in ("csOpen", "csRender", "csAct", "csGo", "csClosed"):
                continue
            self.assertIn(name, registered, name)                      # a data-act that no handler answers is a dead button
        self.assertIn("...CS_ACTIONS,", HTML)                          # merged into the one delegated click handler
        self.assertGreaterEqual(len(registered), 20)

    def test_no_inline_handlers_and_nothing_spliced_into_markup_unescaped(self):
        self.assertNotRegex(CS_JS, r"\bon(click|change|input|error|load)\s*=")                # data-act only
        self.assertNotRegex(CS_JS, r"insertAdjacentHTML|document\.write|outerHTML|eval\(|new Function")
        self.assertNotRegex(CS_JS, r"\.innerHTML\s*\+=")
        # text from the Hub / printer is escaped on the way in
        for needle in ("esc(CUR)", "esc(CS.msg.text)", "esc(m)", "esc(t.slot_1based)", "esc(String(locked.blocked)", "esc(n)"):
            self.assertIn(needle, CS_JS, needle)

    def test_the_button_is_one_static_toolbar_button_and_also_first_under_more(self):
        fn = block("function ctlRender(d){", "// once a second")
        self.assertRegex(HTML, r'<button type="button" id="p-ctlbtn" data-ctl class="btn-ctls ctlmain" onclick="csOpen\(\)"')     # always in the toolbar HTML: no state can hide it
        self.assertLess(HTML.index('id="p-ctlbtn"'), HTML.index('<span id="p-light"'))                          # right group, before Light
        self.assertIn("rare = [ctlsMore]", fn)                                     # More: always, also while printing
        self.assertIn("CTL.rare = [ctlsMore]", fn)                                 # a printer with no actions (PrusaLink) still gets the entry
        self.assertNotIn("ctlsBtn", fn)                                            # the old strip / Light-span copies are gone: ONE button
        self.assertIn("html.lay-5in body:not(.viewonly) #printer .tbar > #p-ctlbtn", HTML)   # visible in the 5-inch header, but a view-only sign-in never sees it

    def test_a_view_only_sign_in_cannot_open_the_sheet(self):
        self.assertIn("if(document.body.classList.contains('viewonly')) return;", block("function csOpen(", "function csPoll"))

    def test_the_sheet_css_sits_before_the_5in_block_and_has_no_hue_only_meaning(self):
        self.assertLess(HTML.index("/* ==== controls sheet ===="), HTML.index("/* ==== 5-inch wall screen"))
        self.assertNotRegex(CS_CSS, r"#(0f0|00ff00|[0-9a-f]{2}ff[0-9a-f]{2})\b", "no green")
        self.assertIn(".cs-chip.on", CS_CSS)                                 # the chosen choice has a frame ...
        self.assertIn("✓ ", CS_JS)                                           # ... and a tick, and its aria-pressed state
        self.assertIn("aria-pressed", CS_JS)
        self.assertIn(".cs-st.now i { border:3px solid", CS_CSS)             # the current step: a thick ring (and a bold word), not a hue
        self.assertIn("'✓' : nd[2]", CS_JS)                                  # a finished step shows a tick

    def test_flip_y_is_wired_from_edit_printer_through_the_hub_to_the_jog_ring(self):
        with open(os.path.join(HERE, "..", "app", "server.py"), encoding="utf8") as fh:
            srv = fh.read()
        self.assertIn('"jog_flip_y": fleet_store.record_jog_flip_y(STORE.get(prn.name))', srv)      # /api/printer/<name> tells the page
        self.assertIn("PM_FLIP_FIELD", HTML)                                                         # Edit printer > More options
        self.assertIn("values.jog_flip_y = $('pm-f-jog_flip_y').value === '1';", HTML)               # ... is saved with the printer
        self.assertIn("const csFlip = () => !!(CTL.d && CTL.d.jog_flip_y);", CS_JS)                  # ... and read by the ring
        self.assertIn("csRing(csFlip(),", CS_JS)

    def test_the_old_load_and_unload_stay_reachable_from_more(self):
        self.assertIn("rare.push(gated('unload'", HTML)
        self.assertIn("rare.push(gated('load'", HTML)
        self.assertIn("function ctlUnloadAsk(", HTML)

    def test_every_limit_lives_on_the_server_too(self):
        import controls
        self.assertEqual((controls.MOVE_MAX_MM, controls.EXTRUDE_MAX_MM, controls.MIN_EXTRUDE_C), (10.0, 10.0, 170))
        self.assertIn("{axis:ax, mm}", CS_JS)                                      # a jog is the axis and a signed 1 or 10, nothing else
        self.assertNotRegex(CS_JS, r"\b(300|120)\b")                               # no temperature limits in the page: the Hub owns them


# ============================================================================================================ the page, in a browser

FIXTURE = PRE + r"""
const NOW = Math.floor(Date.now() / 1000);
const TEMPS = {PLA: 210, PETG: 235, ABS: 245};
const BEDS = {PLA: 60, PETG: 80, ABS: 100};
const REASON_LINK = "PrusaLink's v1 API has no light, temperature, G-code or filament endpoint (it can only read status and run/stop jobs), so none of these controls can be offered. Use the printer's own screen, or run OctoPrint for these.";
const REASON_MMU = "MMU printer - load/unload from the printer's screen (the MMU picks the slot itself).";
const GATE_BUSY = 'printer is RUNNING: heaters, filament and movement are not touched during a print.';
FLEET = [mk('Rook', ['Home'], 'IDLE', 0, '', {kind: 'moonraker'}), mk('Press', ['Home'], 'RUNNING', 42, 'Bracket x12.gcode', {kind: 'moonraker', nozzle_c: 215, nozzle_target_c: 215, bed_c: 60, bed_target_c: 60}),
  mk('Link', ['Home'], 'IDLE', 0, '', {kind: 'prusalink'}), mk('MK3', ['Home'], 'IDLE', 0, '', {kind: 'octoprint'}), mk('P1S', ['Home'], 'IDLE', 0, '', {kind: 'bambu'}),
  mk('MK4', ['Home'], 'RUNNING', 19, 'Phone Stand.gcode', {kind: 'moonraker', nozzle_c: 245, nozzle_target_c: 245, bed_c: 85, bed_target_c: 85, job_materials: ['PETG']}),
  mk('Bare', ['Home'], 'RUNNING', 19, 'Mystery.gcode', {kind: 'moonraker', nozzle_c: 245, nozzle_target_c: 245, bed_c: 85, bed_target_c: 85})];
const SC = {Rook: {homed: true, how: 'reported', light: 'off'}, Press: {homed: true, how: 'reported', light: 'on'}, Link: {}, MK3: {homed: false, how: 'hub', mmu: true}, P1S: {homed: false, how: 'hub', beta: true, light: 'off'}};
const TRAYS = [{tray: 0, slot_1based: 1, type: 'PLA', color_name: 'GREEN'}, {tray: 1, slot_1based: 2, type: 'PETG', color_name: 'BLACK'}, {tray: 2, slot_1based: 3, type: '', color_name: ''}];
window.__posts = []; window.__fail = {};
const P = n => FLEET.find(x => x.printer === n);
const busyP = p => ['RUNNING', 'PREPARE', 'PAUSE'].includes(p.state);
const detail = name => {
  const p = P(name); if(!p) return null;
  const sc = SC[name] || {}, busy = busyP(p), gate = busy ? GATE_BUSY : null;
  const un = r => ({supported: false, reason: r});
  const ok = (gt, extra) => Object.assign({supported: true, allowed_now: !gt}, sc.beta ? {verified: false} : {}, gt ? {blocked: gt} : {}, extra || {});
  const link = p.kind === 'prusalink', mmu = !!sc.mmu, hot = Number(p.nozzle_c) >= 170;
  const notHomed = !sc.homed ? (sc.how === 'reported' ? 'The axes are not homed. Home first.' : 'The Hub has not homed this printer lately (it cannot read the printer\'s homed state). Home first.') : null;
  const cold = hot ? null : 'Heat the nozzle first: it is ' + p.nozzle_c + ' C and extruding needs at least 170 C.';
  const d = {printer: name, kind: p.kind, state: p.state, connected: true};
  if(link){
    for(const k of ['preheat', 'cooldown', 'heater_off', 'unload', 'load', 'home', 'move', 'extrude', 'change_filament']) d[k] = un(REASON_LINK);
    d.preheat.presets = {PLA: {nozzle: null, bed: BEDS.PLA}, PETG: {nozzle: null, bed: BEDS.PETG}, ABS: {nozzle: null, bed: BEDS.ABS}};
    d.light = Object.assign(un(REASON_LINK), {state: null});
  } else {
    d.light = Object.assign({supported: p.kind !== 'octoprint', state: sc.light || null}, p.kind === 'octoprint' ? {reason: 'OctoPrint has no standard light API; the Hub does not guess.'} : {});
    d.preheat = ok(gate, {presets: {PLA: {nozzle: null, bed: BEDS.PLA}, PETG: {nozzle: null, bed: BEDS.PETG}, ABS: {nozzle: null, bed: BEDS.ABS}}, limits: {nozzle_max_c: 300, bed_max_c: 120}, auto_off_timeout_min: 15, auto_off_in_s: p.auto_off_in_s == null ? null : p.auto_off_in_s});
    d.cooldown = ok(gate); d.heater_off = ok(gate);
    d.unload = mmu ? un(REASON_MMU) : ok(gate, p.kind === 'klipper' ? {} : {verified: false});
    d.load = mmu ? un(REASON_MMU) : ok(gate, {verified: false});
    d.change_filament = mmu ? un(REASON_MMU) : ok(gate, {verified: false});
    d.home = ok(gate, {verified: false}); d.move = ok(gate || notHomed, {verified: false}); d.extrude = ok(gate || cold, {verified: false});
  }
  d.motion = {homed: !!sc.homed, homed_how: sc.how || 'hub', min_extrude_c: 170, nozzle_c: p.nozzle_c, nozzle_target_c: p.nozzle_target_c, max_move_mm: 10, max_extrude_mm: 10, axes: ['X', 'Y', 'Z']};
  d.filament = {temps: TEMPS, needs_nozzle_temp: p.kind !== 'bambu', unload_mm: 100, load_mm: 90};
  const acts = [];
  if(d.light.supported) acts.push('light_on', 'light_off');
  if(d.preheat && d.preheat.supported) acts.push('preheat', 'cooldown', 'heater_off');
  for(const k of ['unload', 'load', 'home', 'move', 'extrude']) if(d[k].supported) acts.push(k);
  const allowed = [];
  if(d.light.supported) allowed.push('light_on', 'light_off');
  for(const k of ['preheat', 'cooldown', 'unload', 'load', 'home', 'move', 'extrude', 'heater_off', 'change_filament']) if(d[k].allowed_now) allowed.push(k);
  return {snapshot: p, trays: p.kind === 'bambu' ? TRAYS : [], camera: false, can_connect: false, kind: p.kind, jog_flip_y: !!window.__flip,
    controls: {actions: acts, allowed_now: allowed, light: d.light.supported ? d.light.state : null, presets: ['PLA', 'PETG', 'ABS'], auto_off_in_s: null, detail: d}};
};
window.__stubs.push((u, m, init) => {
  const path = u.split('?')[0]; let mm;
  if(path === '/api/fleet') return J(FLEET);
  if(path === '/api/ui-version') return J({v: '1'});
  if(path === '/api/auth/me') return J({});
  if(path === '/api/detection') return J({enabled: false});
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/files$/))) return J([]);
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/control$/))){
    const name = decodeURIComponent(mm[1]), b = JSON.parse(init.body), p = P(name), sc = SC[name];
    window.__posts.push(Object.assign({printer: name}, b));
    if(window.__fail[b.action]) return J({error: window.__fail[b.action]}, 409);
    const out = {ok: true, action: b.action, message: b.action + ' sent'};
    if(b.action === 'preheat'){ const n = b.material ? TEMPS[b.material] : (b.nozzle || null), bed = b.preset ? BEDS[b.preset] : null; if(n) p.nozzle_target_c = n; if(bed) p.bed_target_c = bed; out.nozzle = n; out.bed = bed; }
    if(b.action === 'heater_off'){ if(b.heater !== 'bed') p.nozzle_target_c = 0; if(b.heater !== 'nozzle') p.bed_target_c = 0; }
    if(b.action === 'cooldown'){ p.nozzle_target_c = 0; p.bed_target_c = 0; }
    if(b.action === 'home') sc.homed = true;
    if(b.action === 'light_on') sc.light = 'on';
    if(b.action === 'light_off') sc.light = 'off';
    return J(out);
  }
  if((mm = path.match(/^\/api\/printer\/([^/]+)$/))){ const d = detail(decodeURIComponent(mm[1])); return d ? J(d) : J({error: 'no'}, 404); }
  return null;
});
const vis = e => { const b = e.getBoundingClientRect(); const cs = getComputedStyle(e); return b.width > 0 && b.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
const sheetOpen = () => !!document.querySelector('#mbody .cs') && document.getElementById('modal').classList.contains('on');
const T = e => (e ? e.textContent.replace(/\s+/g, ' ').trim() : null);
const btnInfo = b => ({t: T(b), dis: b.disabled || b.getAttribute('aria-disabled') === 'true', act: b.dataset.act || null, pressed: b.getAttribute('aria-pressed'), h: Math.round(b.getBoundingClientRect().height)});
const rect = e => { const b = e.getBoundingClientRect(); return {t: Math.round(b.top), b: Math.round(b.bottom), l: Math.round(b.left), r: Math.round(b.right), w: Math.round(b.width), h: Math.round(b.height)}; };
const sheet = () => {
  const cs = document.querySelector('#mbody .cs'); if(!cs) return null;
  const mb = document.getElementById('mbody'), body = cs.querySelector('.cs-ctl, .cs-wb');
  const btns = [...cs.querySelectorAll('button')].filter(vis);
  const wedges = [...cs.querySelectorAll('.cs-wg, .cs-home')];
  const ringCut = wedges.filter(g => { const pb = g.querySelector('path').getBoundingClientRect(), c = g.querySelector('circle'); const box = c ? c.getBoundingClientRect() : pb;
    return [...g.querySelectorAll('text')].some(x => { const b = x.getBoundingClientRect(); return b.left < box.left - 1 || b.right > box.right + 1 || b.top < box.top - 1 || b.bottom > box.bottom + 1; }); }).map(g => gT(g));
  const panels = [...cs.querySelectorAll('.cs-panel')].map(rect);
  return {text: T(cs), btns: btns.map(btnInfo), ringCut, wedges: wedges.map(g => ({t: gT(g), dis: g.getAttribute('aria-disabled') === 'true'})),
    cut: btns.filter(b => b.scrollWidth > b.clientWidth + 1 || b.scrollHeight > b.clientHeight + 1).map(b => T(b)),
    labCut: [...cs.querySelectorAll('.cs-lbl, .cs-note, .cs-msg, .cs-lock, .cs-say, .cs-mh h2, .cs-pill, .cs-wt h3, .cs-wt p, .cs-bigt, .cs-tgt, .cs-lockmsg, .cs-fact, .cs-list li, .cs-st, .cs-ax')].filter(vis).filter(e => e.scrollWidth > e.clientWidth + 1).map(e => T(e).slice(0, 40)),
    mbW: mb.scrollWidth, mbCW: mb.clientWidth, docSW: document.documentElement.scrollWidth, docCW: document.documentElement.clientWidth,
    bodySH: body ? body.scrollHeight : 0, bodyCH: body ? body.clientHeight : 0, bodySW: body ? body.scrollWidth : 0, bodyCW: body ? body.clientWidth : 0,
    box: rect(mb), vw: document.documentElement.clientWidth, vh: innerHeight, panels, grab: vis(cs.querySelector('.cs-grab') || document.body) && !!cs.querySelector('.cs-grab') && getComputedStyle(cs.querySelector('.cs-grab')).display !== 'none',
    minH: btns.length ? Math.min(...btns.map(b => b.getBoundingClientRect().height)) : 0};
};
const bt = (re, root) => [...(root || document).querySelectorAll('#mbody button')].filter(vis).find(b => re.test(T(b)));
const click = async (re, wait) => { const b = bt(re); if(!b) throw new Error('no button ' + re + ' in: ' + T(document.querySelector('#mbody'))); b.click(); await sleep(wait || 150); return b; };
const tap = async (el, wait) => { el.dispatchEvent(new MouseEvent('click', {bubbles: true})); await sleep(wait || 120); };
const gT = g => [...g.querySelectorAll('text')].map(x => x.textContent).join(' ');
const wedge = lab => [...document.querySelectorAll('#mbody .cs-wg')].find(g => gT(g).startsWith(lab));
const jogZ = sg => document.querySelector('#mbody [data-act=csJog][data-axis=Z][data-sign="' + sg + '"]');
const ctlButton = () => { const c = [...document.querySelectorAll('#printer button.btn-ctls')].filter(vis); return c.length ? {text: T(c[0]), n: c.length, where: c[0].closest('.tbar') ? 'toolbar' : c[0].closest('#p-light') ? 'light' : c[0].closest('#p-ctl-btns') ? 'strip' : 'other', row: Math.round((c[0].getBoundingClientRect().top + c[0].getBoundingClientRect().bottom) / 2 / 14), r: Math.round(c[0].getBoundingClientRect().right), l: Math.round(c[0].getBoundingClientRect().left), vw: document.documentElement.clientWidth, locked: null, h: Math.round(c[0].getBoundingClientRect().height)} : null; };
const posts = () => window.__posts.slice();
const open = async n => { await fleet(); await sleep(120); openPrinter(n); await sleep(600); };
const toolbarRows = () => { const bar = document.querySelector('#printer .tbar'); return new Set([...bar.children].filter(vis).map(e => { const b = e.getBoundingClientRect(); return Math.round((b.top + b.bottom) / 2 / 14); })).size; };
const cards = () => { const tc = [...document.querySelectorAll('#mbody .cs-tc')];
  return {lab: tc.map(c => T(c.querySelector('.cs-lbl'))), now: tc.map(c => T(c.querySelector('.cs-bigt'))), tgt: tc.map(c => T(c.querySelector('.cs-tgt'))), pct: tc.map(c => c.querySelector('.cs-meter').getAttribute('aria-valuenow')),
    chips: tc.map(c => [...c.querySelectorAll('button')].map(btnInfo)), fil: [...document.querySelectorAll('#mbody .cs-fbtns button')].map(btnInfo), seg: [...document.querySelectorAll('#mbody .cs-seg button')].map(btnInfo),
    z: [...document.querySelectorAll('#mbody [data-act=csJog][data-axis=Z]')].map(btnInfo), ex: [...document.querySelectorAll('#mbody .cs-vb[data-act=csExtrude]')].map(btnInfo), lockmsg: T(document.querySelector('#mbody .cs-lockmsg')),
    move: T(document.querySelector('#mbody .cs-move .cs-lbl')), pill: T(document.querySelector('#mbody .cs-pill')), title: T(document.querySelector('#mbody .cs-mh h2')), head: [...document.querySelectorAll('#mbody .cs-mh button')].map(btnInfo)}; };
const bar = () => [...document.querySelectorAll('#mbody .cs-st')].map(n => ({t: n.querySelector('span').childNodes[0].textContent.trim(), st: n.classList.contains('done') ? 'done' : n.classList.contains('now') ? 'now' : 'todo', cur: n.getAttribute('aria-current'), tick: T(n.querySelector('i'))}));
const step = () => T(document.querySelector('#mbody .cs-step'));
const wiz = () => ({pill: step(), bar: bar(), title: T(document.querySelector('#mbody .cs-wt h3')), say: T(document.querySelector('#mbody .cs-wt p')), foot: [...document.querySelectorAll('#mbody .cs-wf button')].map(btnInfo), head: T(document.querySelector('#mbody .cs-mh h2')),
  facts: [...document.querySelectorAll('#mbody .cs-fact')].map(T).join(' | '), ring: T(document.querySelector('#mbody [data-lh=heatring]')), list: [...document.querySelectorAll('#mbody .cs-list li')].map(li => li.childNodes[1].textContent.trim()), msg: T(document.querySelector('#mbody .cs-msg')), cls: (document.querySelector('#mbody .cs-msg') || {}).className});
"""

DRIVER = FIXTURE + r"""
await sleep(300);
// ---- 1. Rook: idle Klipper, homed, cold
await open('Rook');
R.rook = {btn: ctlButton(), tbar: toolbarRows()};
document.querySelector('#printer button.btn-ctls').click(); await sleep(400);
R.rook.open = sheetOpen(); R.rook.cold = sheet(); R.rook.cards = cards();
// nozzle / bed: a material press sends the material name (the Hub looks the temperature up) / a preset name
window.__posts.length = 0;
await click(/^PETG 235°$/); R.rook.afterNoz = {posts: posts(), chips: cards().chips[0].map(b => b.t + ':' + b.pressed), tgt: cards().tgt[0]}; window.__posts.length = 0;
await click(/^PETG 80°$/); R.rook.afterBed = posts(); window.__posts.length = 0;
await click(/^Off$/); R.rook.afterOff = posts();
// move: the ring wedge says the axis and the sign; the step switch says 1 or 10 mm (default 10)
window.__posts.length = 0;
R.rook.stepDefault = cards().seg.map(b => b.t + ':' + b.pressed);
R.rook.wedges = [...document.querySelectorAll('#mbody .cs-wg')].map(g => ({t: gT(g), axis: g.dataset.axis, sign: g.dataset.sign}));
await tap(wedge('X−')); await tap(jogZ(1)); await tap(wedge('Y+'));
R.rook.jogs10 = posts(); window.__posts.length = 0;
await click(/^1 mm$/); R.rook.step1 = cards().seg.map(b => b.t + ':' + b.pressed); R.rook.zLab = cards().z.map(b => b.t);
await tap(wedge('X+')); await tap(jogZ(-1)); await tap(wedge('Y−'));
R.rook.jogs1 = posts(); window.__posts.length = 0;
await click(/^10 mm$/); await tap(wedge('X+')); R.rook.back10 = posts(); window.__posts.length = 0;
// Home asks once: the first tap sends nothing and shows the question
await tap(document.querySelector('#mbody [data-act=csHome]')); R.rook.homeAsk = {posts: posts(), text: T(document.querySelector('#mbody .cs-confirm')), btns: [...document.querySelectorAll('#mbody .cs-confirm button')].map(T)};
await click(/^No$/); R.rook.homeNo = {posts: posts(), back: !!document.querySelector('#mbody [data-act=csHome]')};
await tap(document.querySelector('#mbody [data-act=csHome]')); await click(/^Yes, home all axes$/, 300); R.rook.homeYes = {posts: posts(), back: !!document.querySelector('#mbody [data-act=csHome]')}; window.__posts.length = 0;
// pull / push: refused while cold (the words say why), allowed once hot
R.rook.coldEx = cards().ex; R.rook.coldMsg = cards().lockmsg;
P('Rook').nozzle_c = 215; P('Rook').nozzle_target_c = 235; await pview(); await sleep(200);
R.rook.hotEx = cards().ex; R.rook.hotMsg = cards().lockmsg;
await click(/Push/); await click(/Pull/); R.rook.exPosts = posts(); window.__posts.length = 0;
R.rook.live = {noz: T(document.querySelector('[data-live=noz]')), tg: T(document.querySelector('[data-live=noztg]')), pct: cards().pct[0]};
// the light
await click(/^Light: OFF$/); R.rook.lightPosts = posts(); window.__posts.length = 0; R.rook.lightAfter = T(bt(/^Light:/));
closeModal(); await sleep(100); R.rook.closed = !sheetOpen();
P('Rook').nozzle_c = 25; P('Rook').nozzle_target_c = 0; SC.Rook.light = 'off';

// ---- 1b. "Flip Y on the jog pad": the top wedge is Y- (front), the bottom one Y+ (back); the sign follows the label
window.__flip = true; await pview(); await sleep(200);
document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
window.__posts.length = 0;
R.flip = {wedges: [...document.querySelectorAll('#mbody .cs-wg')].map(g => ({t: gT(g), axis: g.dataset.axis, sign: g.dataset.sign}))};
await tap(wedge('Y−')); await tap(wedge('Y+')); R.flip.posts = posts(); window.__posts.length = 0;
R.flip.sheet = sheet(); closeModal(); window.__flip = false; await pview(); await sleep(100);

// ---- 2. the guided change on Rook (homed: no Home step)
await pview(); await sleep(200);
document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
await click(/^Change filament/); R.flow = {s1: Object.assign(wiz(), {mats: [...document.querySelectorAll('#mbody .cs-art .cs-chips button')].map(T), bud: sheet()})};
window.__posts.length = 0;
P('Rook').nozzle_c = 142;
await click(/^PETG/, 300); R.flow.s2 = Object.assign(wiz(), {posts: posts(), full: sheet()});
await click(/Back$/); R.flow.back = wiz();
await click(/^PETG/, 300);
window.__posts.length = 0;
P('Rook').nozzle_c = 236; await sleep(2200);        // the sheet polls the printer by itself: hot -> Unload (the printer is homed)
R.flow.s4 = Object.assign(wiz(), {posts: posts()});
await click(/Unload$/, 400); R.flow.s5 = Object.assign(wiz(), {posts: posts(), full: sheet()});
window.__posts.length = 0;
await click(/Load PETG$/, 400); R.flow.s6 = Object.assign(wiz(), {posts: posts()});
window.__posts.length = 0;
R.flow.purgeBtn = btnInfo(bt(/^Purge/)); await click(/^Purge/); R.flow.purge = posts(); window.__posts.length = 0;
await click(/Done$/); R.flow.s7 = wiz();
await click(/^Cool down$/, 300); R.flow.cool = {posts: posts(), main: !!document.querySelector('[data-act=csFlow]'), sheet: sheetOpen()};
window.__posts.length = 0;
// Cancel on a step turns off the heater the flow set
await click(/^Change filament/); P('Rook').nozzle_c = 142; await click(/^ABS/, 300); window.__posts.length = 0;
await click(/Cancel$/, 300); R.flow.cancel = {posts: posts(), main: !!document.querySelector('[data-act=csFlow]')};
// Keep hot leaves the heaters to the auto-off and sends nothing
P('Rook').nozzle_c = 25; P('Rook').nozzle_target_c = 0; await pview(); window.__posts.length = 0;
await click(/^Change filament/); await click(/^PLA/, 200); P('Rook').nozzle_c = 211; await sleep(2200);
await click(/Unload$/, 300); await click(/Load PLA$/, 300); await click(/Done$/); window.__posts.length = 0; await click(/^Keep hot$/, 200);
R.flow.keep = {posts: posts(), text: T(document.querySelector('#mbody .cs-msg')), main: !!document.querySelector('[data-act=csFlow]')};
closeModal(); P('Rook').nozzle_c = 25; P('Rook').nozzle_target_c = 0;

// ---- 3. Rook not homed: the jogs say why they are off; the flow has a Home step (6 nodes) and homes first
SC.Rook.homed = false; await pview(); await sleep(200);
document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
R.unhomed = {main: sheet(), cards: cards()};
window.__posts.length = 0; await tap(wedge('X+')); await tap(jogZ(1)); R.unhomed.posts = posts(); window.__posts.length = 0;
await click(/^Change filament/); R.unhomed.s1 = wiz(); P('Rook').nozzle_c = 100; await click(/^PETG/, 300); window.__posts.length = 0;
R.unhomed.s2 = wiz();
P('Rook').nozzle_c = 236; await sleep(2200);
R.unhomed.s3 = Object.assign(wiz(), {posts: posts()});
await click(/Home now$/, 400); R.unhomed.after = Object.assign(wiz(), {posts: posts()});
closeModal(); P('Rook').nozzle_c = 25; P('Rook').nozzle_target_c = 0; SC.Rook.homed = true;

// ---- 4. the Hub says no: the reason is on screen and the flow goes back a step
await pview(); await sleep(150);
document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
window.__fail.preheat = 'REFUSED: printer is not ready (state UNKNOWN).';
await click(/^Change filament/); await click(/^PLA/, 300);
R.refused = wiz();
delete window.__fail.preheat; closeModal();

// ---- 5. Press: printing. The Controls button is STILL in the toolbar row; the pop-up opens with its parts locked and ONE line in words; the light still works; More has the entry too
await open('Press');
R.press = {btn: ctlButton(), tbar: toolbarRows()};
document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
R.press.opened = sheetOpen(); R.press.sheet = sheet(); R.press.lock = T(document.querySelector('#mbody .cs-lock')); R.press.nlock = document.querySelectorAll('#mbody .cs-lock').length;
R.press.light = btnInfo(bt(/^Light/));
window.__posts.length = 0; { const b = bt(/^PLA 210/); b && b.click(); await sleep(100); await tap(wedge('X+')); R.press.posts = posts(); }
R.press.tg = cards().tgt;
closeModal();
if(L5){ document.getElementById('p-more5').click(); await sleep(250); R.press.more = [...document.querySelectorAll('#pmore-box button')].map(T); document.querySelector('#pmore-box .btn-ctls').click(); await sleep(300); }
else { document.querySelector('.fmore-tb').click(); await sleep(250); R.press.more = [...document.querySelectorAll('#mbody button')].filter(vis).map(T); document.querySelector('#mbody .btn-ctls').click(); await sleep(300); }
R.press.moreOpened = sheetOpen(); closeModal();

// ---- 5b. the material word comes ONLY from the running job's own file: a PETG job at 245 C (the ABS preset's temperature) never says ABS; no material in the file = no word
for(const [k, n] of [['mk4', 'MK4'], ['bare', 'Bare']]){
  await open(n); document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
  R[k] = {tgt: cards().tgt, chips: cards().chips[0].map(b => b.t + ':' + b.pressed), text: T(document.querySelector('#mbody .cs')), btn: ctlButton()}; closeModal();
}

// ---- 6. Link (PrusaLink): everything is shown disabled with the reason in words
await open('Link');
R.link = {btn: ctlButton()};
document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
R.link.sheet = sheet(); R.link.cards = cards(); closeModal();

// ---- 7. MK3 (OctoPrint + MMU): filament is off with the MMU words, heat / move / extrude are not
await open('MK3');
document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
R.mmu = {sheet: sheet(), cards: cards(), change: btnInfo(bt(/^Change filament/))}; closeModal();

// ---- 8. P1S (Bambu): beta words, AMS slots on Load, no material temperature
await open('P1S');
document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
R.bambu = {sheet: sheet(), cards: cards()};
await click(/Load$/, 200); R.bambu.load = {text: T(document.querySelector('#mbody .cs')), slots: [...document.querySelectorAll('#mbody .cs-slot')].map(btnInfo), sheet: sheet(), pill: step(), foot: wiz().foot};
window.__posts.length = 0; await click(/^Slot 2/, 300); R.bambu.posts = posts(); closeModal();

// ---- 9. a view-only sign-in never sees the button and cannot open the sheet
await open('Rook');
document.body.classList.add('viewonly'); await sleep(80);
R.viewonly = {btn: ctlButton()}; csOpen(); await sleep(100); R.viewonly.open = sheetOpen();
document.body.classList.remove('viewonly');
// ---- 10. a temperature unit of Fahrenheit shows Fahrenheit and still sends the material name
PREFS.temp_unit = 'F'; document.querySelector('#printer button.btn-ctls').click(); await sleep(300);
R.fahr = {noz: cards().chips[0].map(b => b.t), now: cards().now}; closeModal(); PREFS.temp_unit = 'C';
"""


def WIDE_DRIVER(drv):
    """The page is measured with the wide fallback font the Linux CI machines draw (nothing may rely on the narrow macOS system font)."""
    return drv.replace("await sleep(300);\n// ---- 1.", WIDE + "await sleep(300);\n// ---- 1.", 1) if "// ---- 1." in drv else drv


class SheetBrowserTests(unittest.TestCase):
    cache = {}

    def run_layout(self, key, query, size, extra=""):
        k = (key, query, size)
        if k not in SheetBrowserTests.cache:
            drv = DRIVER if not extra else DRIVER.replace("await sleep(300);\n// ---- 1.", extra + "\nawait sleep(300);\n// ---- 1.", 1)
            SheetBrowserTests.cache[k] = run_q(self, WIDE_DRIVER(drv), query, size, 300000)
        R = SheetBrowserTests.cache[k]
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        return R

    LAYOUTS = {
        "phone": ("", (390, 844)),
        "kiosk": ("?kiosk=1", (1024, 600)),
        "desktop": ("?view=desktop", (1400, 800)),
        "5in": ("?layout=5in", (800, 480)),
        "bridge": ("?layout=5in&theme=bridge", (800, 480)),
    }

    def each(self):
        return [(name, self.run_layout(name, q, size)) for name, (q, size) in self.LAYOUTS.items()]

    # ------------------------------------------------------------------ the button, on every layout
    def test_the_controls_button_is_on_every_layout_idle_and_printing_with_a_word_and_a_finger_sized_target(self):
        for name, R in self.each():
            for state, key in (("idle", "rook"), ("printing", "press")):
                b = R[key]["btn"]
                self.assertIsNotNone(b, (name, state))
                self.assertIn("Controls", b["text"], (name, state))                       # a word, not only an icon
                self.assertGreaterEqual(b["h"], 36 if name == "desktop" else 43, (name, state))       # the desktop (mouse) layout is 36 px tall on its whole toolbar
                self.assertEqual(b["n"], 1, (name, state))
                self.assertEqual(b["where"], "toolbar", (name, state))                    # in the top toolbar row, never in a strip
                self.assertGreaterEqual(b["l"], 0, (name, state))
                self.assertLessEqual(b["r"], b["vw"], (name, state))                       # (the page's own width: CI draws a 15 px scrollbar)
            self.assertTrue(R["rook"]["open"], name)
            self.assertTrue(R["press"]["opened"], name)                                    # a printing printer opens the pop-up from the same button
            self.assertTrue(R["press"]["moreOpened"], name)                                # ... and from More

    def test_the_toolbar_stays_one_row_on_the_wide_layouts_idle_and_printing_also_with_a_wide_font(self):
        for name in ("kiosk", "desktop"):
            R = self.run_layout(name, *self.LAYOUTS[name])               # (the driver runs with the wide fallback font the Linux CI machines draw)
            self.assertEqual(R["rook"]["tbar"], 1, name)
            self.assertEqual(R["press"]["tbar"], 1, name)
            self.assertEqual(R["rook"]["btn"]["row"], R["press"]["btn"]["row"], name)

    def test_the_phone_and_5_inch_toolbars_do_not_wrap_past_four_rows_and_the_5_inch_header_stays_one_row(self):
        for name in ("phone",):
            R = self.run_layout(name, *self.LAYOUTS[name])
            self.assertLessEqual(R["press"]["tbar"], 4, name)
            self.assertLessEqual(R["rook"]["tbar"], 3, name)
        for name in ("5in", "bridge"):
            R = self.run_layout(name, *self.LAYOUTS[name])
            self.assertEqual(R["press"]["btn"]["where"], "toolbar", name)

    # ------------------------------------------------------------------ the pop-up fits: no cut words, no sideways scroll
    def test_the_popup_fits_every_layout_with_no_cut_words_and_no_sideways_scroll(self):
        for name, R in self.each():
            s = R["rook"]["cold"]
            self.assertEqual(s["cut"], [], (name, "buttons with cut words"))
            self.assertEqual(s["labCut"], [], (name, "labels with cut words"))
            self.assertEqual(s["ringCut"], [], (name, "ring labels outside their wedge"))
            self.assertLessEqual(s["mbW"], s["mbCW"] + 1, (name, "the dialog scrolls sideways"))
            self.assertLessEqual(s["bodySW"], s["bodyCW"] + 1, (name, "the card body scrolls sideways"))
            self.assertLessEqual(s["docSW"], s["docCW"] + 1, (name, "the page scrolls sideways"))   # the page's own width, never a hardcoded viewport width (CI draws a 15 px scrollbar)
            self.assertGreaterEqual(s["box"]["l"], 0, name)
            self.assertLessEqual(s["box"]["r"], s["vw"] + 1, name)
            self.assertGreaterEqual(s["box"]["t"], 0, name)
            self.assertLessEqual(s["box"]["b"], s["vh"] + 1, name)
            self.assertGreaterEqual(s["minH"], 36 if name == "desktop" else 43, (name, "a finger-sized target"))

    def test_it_is_a_popup_card_centred_on_big_screens_a_bottom_sheet_with_a_grab_handle_on_a_phone(self):
        for name, R in self.each():
            s = R["rook"]["cold"]
            mid = (s["box"]["l"] + s["box"]["r"]) / 2
            if name == "phone":
                self.assertTrue(s["grab"], name)                                             # the grab handle
                self.assertGreaterEqual(s["box"]["b"], s["vh"] - 2, name)                    # the sheet sits on the bottom edge
                self.assertGreater(s["box"]["t"], 0, name)                                   # ... with the dimmed printer screen still showing above it
            else:
                self.assertFalse(s["grab"], name)
                self.assertAlmostEqual(mid, s["vw"] / 2, delta=3, msg=name)                  # centred
                self.assertGreater(s["box"]["t"], 0, name)                                   # a card over the screen, not the whole page
                if name in ("kiosk", "desktop"):
                    self.assertLess(s["box"]["r"] - s["box"]["l"], s["vw"], name)

    def test_on_the_5_inch_screen_nothing_inside_the_card_needs_scrolling(self):
        for name in ("5in", "bridge"):
            R = self.run_layout(name, *self.LAYOUTS[name])
            for label, s in (("idle", R["rook"]["cold"]), ("not homed", R["unhomed"]["main"]), ("printing", R["press"]["sheet"]), ("prusalink", R["link"]["sheet"]),
                             ("mmu", R["mmu"]["sheet"]), ("bambu", R["bambu"]["sheet"]), ("flip", R["flip"]["sheet"]), ("swap", R["flow"]["s5"]["full"]),
                             ("heat", R["flow"]["s2"]["full"]), ("material", R["flow"]["s1"]["bud"]), ("slots", R["bambu"]["load"]["sheet"])):
                self.assertLessEqual(s["bodySH"], s["bodyCH"] + 1, (name, label, "the card needs scrolling"))
                self.assertEqual(s["cut"], [], (name, label))
                self.assertEqual(s["labCut"], [], (name, label))
                self.assertEqual(s["ringCut"], [], (name, label))
                self.assertLessEqual(s["docSW"], s["docCW"] + 1, (name, label))
                self.assertLessEqual(s["bodySW"], s["bodyCW"] + 1, (name, label))

    def test_on_a_phone_the_popup_scrolls_only_up_and_down_and_every_state_fits_the_width(self):
        R = self.run_layout("phone", *self.LAYOUTS["phone"])
        for label, s in (("idle", R["rook"]["cold"]), ("not homed", R["unhomed"]["main"]), ("printing", R["press"]["sheet"]), ("prusalink", R["link"]["sheet"]),
                         ("mmu", R["mmu"]["sheet"]), ("bambu", R["bambu"]["sheet"]), ("swap", R["flow"]["s5"]["full"]), ("heat", R["flow"]["s2"]["full"]),
                         ("material", R["flow"]["s1"]["bud"]), ("slots", R["bambu"]["load"]["sheet"])):
            self.assertEqual(s["cut"], [], label)
            self.assertEqual(s["labCut"], [], label)
            self.assertEqual(s["ringCut"], [], label)
            self.assertLessEqual(s["docSW"], s["docCW"] + 1, label)
            self.assertLessEqual(s["bodySW"], s["bodyCW"] + 1, label)
            self.assertLessEqual(s["box"]["b"], s["vh"] + 1, label)

    def test_the_two_columns_sit_side_by_side_on_wide_screens_and_stack_on_a_phone(self):
        for name, R in self.each():
            mv, noz, bed, fil = R["rook"]["cold"]["panels"]
            if name == "phone":
                self.assertGreaterEqual(noz["t"], mv["b"] - 1, name)
                self.assertGreaterEqual(bed["t"], noz["b"] - 1, name)
                self.assertGreaterEqual(fil["t"], bed["b"] - 1, name)
            else:
                self.assertGreaterEqual(noz["l"], mv["r"] - 1, name)                          # Move left, Nozzle / Bed / Filament in the right column
                self.assertEqual(noz["l"], bed["l"], name)
                self.assertEqual(noz["l"], fil["l"], name)
                self.assertGreaterEqual(bed["t"], noz["b"] - 1, name)
                self.assertGreaterEqual(fil["t"], bed["b"] - 1, name)

    # ------------------------------------------------------------------ what is in it
    def test_the_header_says_the_printer_the_state_in_words_and_has_a_round_close(self):
        for name, R in self.each():
            c = R["rook"]["cards"]
            self.assertEqual(c["title"], "Rook · Controls", name)
            self.assertEqual(c["pill"], "IDLE" if name == "phone" else "IDLE · HOMED", name) if name != "phone" else self.assertIn("IDLE", c["pill"], name)
            self.assertTrue(any("Close" in b["t"] for b in c["head"]), name)
            self.assertTrue(any(b["t"].startswith("Light:") for b in c["head"]), name)

    def test_the_sections_and_their_buttons(self):
        for name, R in self.each():
            c = R["rook"]["cards"]
            self.assertIn("Move", c["move"], name)
            self.assertEqual([re.sub(r"\s+", " ", b["t"]) for b in c["fil"]], ["Change filament…", "⬆ Unload", "⬇ Load"], name)
            self.assertIn("Nozzle", c["lab"][0], name)
            self.assertEqual([b["t"] for b in c["chips"][0]], ["PLA 210°", "PETG 235°", "ABS 245°", "✓ Off"], name)       # the active one has a tick
            self.assertIn("Bed", c["lab"][1], name)
            self.assertEqual([b["t"] for b in c["chips"][1]], ["PLA 60°", "PETG 80°", "ABS 100°", "✓ Off"], name)
            self.assertEqual(c["now"], ["25°", "24°"], name)
            self.assertEqual(c["tgt"], ["heater off", "heater off"], name)                         # the target in words
            self.assertEqual([b["t"] for b in c["seg"]], ["1 mm", "10 mm"], name)
            self.assertEqual([b["t"] for b in c["z"]], ["▲ Up Z+ 10 mm", "▼ Down Z− 10 mm"], name)
            self.assertEqual([b["t"] for b in c["ex"]], ["▲ Pull retract 10", "▼ Push extrude 10"], name)
            self.assertEqual([w["t"] for w in R["rook"]["cold"]["wedges"]], ["Y+ back", "X+ right", "Y− front", "X− left", "HOME"], name)

    def test_words_on_every_button(self):
        for name, R in self.each():
            for b in R["rook"]["cold"]["btns"]:
                self.assertRegex(b["t"], r"[A-Za-z+−✓]", (name, b))
            for w in R["rook"]["cold"]["wedges"]:
                self.assertRegex(w["t"], r"[A-Za-z]", (name, w))

    def test_the_meters_say_the_same_thing_in_numbers(self):
        for name, R in self.each():
            self.assertTrue(all(0 <= int(p) <= 100 for p in R["rook"]["cards"]["pct"]), name)
            self.assertEqual(R["rook"]["live"]["tg"], "→ 235°", name)                     # the target in words - NO material word: nothing in a file says one (a temperature never names a material)
            self.assertEqual(R["rook"]["live"]["noz"], "215°", name)
            self.assertEqual(R["rook"]["live"]["pct"], "91", name)                       # 215 of 235, filling toward the target

    def test_nozzle_and_bed_buttons_send_a_material_or_preset_name_and_the_chosen_one_has_a_tick(self):
        for name, R in self.each():
            r = R["rook"]
            self.assertEqual(r["afterNoz"]["posts"], [{"printer": "Rook", "action": "preheat", "material": "PETG"}], name)
            self.assertIn("✓ PETG 235°:true", r["afterNoz"]["chips"], name)
            self.assertIn("Off:false", r["afterNoz"]["chips"], name)
            self.assertEqual(r["afterNoz"]["tgt"], "→ 235°", name)                      # the chip is ticked, but the words never claim a material the file did not name
            self.assertEqual(r["afterBed"], [{"printer": "Rook", "action": "preheat", "preset": "PETG"}], name)
            self.assertEqual(r["afterOff"], [{"printer": "Rook", "action": "heater_off", "heater": "nozzle"}], name)

    def test_a_jog_wedge_sends_its_axis_and_sign_and_the_step_switch_sends_1_or_10_mm(self):
        for name, R in self.each():
            r = R["rook"]
            self.assertEqual(r["stepDefault"], ["1 mm:false", "10 mm:true"], name)                               # 10 mm is the default
            self.assertEqual(r["wedges"][:4], [{"t": "Y+ back", "axis": "Y", "sign": "1"}, {"t": "X+ right", "axis": "X", "sign": "1"},
                                              {"t": "Y− front", "axis": "Y", "sign": "-1"}, {"t": "X− left", "axis": "X", "sign": "-1"}], name)
            self.assertEqual(r["jogs10"], [{"printer": "Rook", "action": "move", "axis": "X", "mm": -10}, {"printer": "Rook", "action": "move", "axis": "Z", "mm": 10},
                                           {"printer": "Rook", "action": "move", "axis": "Y", "mm": 10}], name)
            self.assertEqual(r["step1"], ["1 mm:true", "10 mm:false"], name)
            self.assertEqual(r["zLab"], ["▲ Up Z+ 1 mm", "▼ Down Z− 1 mm"], name)                                 # the Z buttons say the step
            self.assertEqual(r["jogs1"], [{"printer": "Rook", "action": "move", "axis": "X", "mm": 1}, {"printer": "Rook", "action": "move", "axis": "Z", "mm": -1},
                                          {"printer": "Rook", "action": "move", "axis": "Y", "mm": -1}], name)
            self.assertEqual(r["back10"], [{"printer": "Rook", "action": "move", "axis": "X", "mm": 10}], name)

    def test_flip_y_turns_the_top_wedge_into_y_minus_front_and_the_signs_follow_the_labels(self):
        for name, R in self.each():
            f = R["flip"]
            self.assertEqual([w["t"] for w in f["wedges"]][:4], ["Y− front", "X+ right", "Y+ back", "X− left"], name)       # top = Y− (front)
            self.assertEqual(f["wedges"][0], {"t": "Y− front", "axis": "Y", "sign": "-1"}, name)
            self.assertEqual(f["wedges"][2], {"t": "Y+ back", "axis": "Y", "sign": "1"}, name)
            self.assertEqual(f["posts"], [{"printer": "Rook", "action": "move", "axis": "Y", "mm": -10}, {"printer": "Rook", "action": "move", "axis": "Y", "mm": 10}], name)
            self.assertEqual(R["rook"]["wedges"][0]["t"], "Y+ back", name)                                         # off by default: Y+ on top

    def test_home_asks_once(self):
        for name, R in self.each():
            r = R["rook"]
            self.assertEqual(r["homeAsk"]["posts"], [], name)                      # the first tap only asks
            self.assertIn("Home all axes?", r["homeAsk"]["text"], name)
            self.assertEqual(r["homeAsk"]["btns"], ["Yes, home all axes", "No"], name)
            self.assertEqual(r["homeNo"], {"posts": [], "back": True}, name)
            self.assertEqual(r["homeYes"]["posts"], [{"printer": "Rook", "action": "home", "confirm": True}], name)

    def test_pull_and_push_say_why_when_cold_and_work_when_hot(self):
        for name, R in self.each():
            r = R["rook"]
            self.assertTrue(all(b["dis"] for b in r["coldEx"]), name)
            self.assertEqual(r["coldMsg"], "🔒 Heat the nozzle to 170° first", name)          # the words, under the locked buttons
            self.assertFalse(any(b["dis"] for b in r["hotEx"]), name)
            self.assertIsNone(r["hotMsg"], name)
            self.assertEqual(r["exPosts"], [{"printer": "Rook", "action": "extrude", "mm": 10}, {"printer": "Rook", "action": "extrude", "mm": -10}], name)

    def test_the_light_is_in_the_popup_and_follows_its_state(self):
        for name, R in self.each():
            self.assertEqual(R["rook"]["lightPosts"], [{"printer": "Rook", "action": "light_on"}], name)
            self.assertEqual(R["rook"]["lightAfter"], "Light: ON", name)
            self.assertTrue(R["rook"]["closed"], name)

    # ------------------------------------------------------------------ the guided filament change
    def test_the_step_bar_marks_done_with_a_tick_and_the_current_step_in_words_and_a_thick_ring(self):
        for name, R in self.each():
            f = R["flow"]
            self.assertEqual([(n["t"], n["st"]) for n in f["s1"]["bar"]], [("Material", "now"), ("Heat", "todo"), ("Unload", "todo"), ("Swap", "todo"), ("Load", "todo")], name)
            self.assertEqual([(n["t"], n["st"]) for n in f["s2"]["bar"]], [("Material", "done"), ("Heat", "now"), ("Unload", "todo"), ("Swap", "todo"), ("Load", "todo")], name)
            self.assertEqual([(n["t"], n["st"]) for n in f["s5"]["bar"]], [("Material", "done"), ("Heat", "done"), ("Unload", "done"), ("Swap", "now"), ("Load", "todo")], name)
            for s in ("s1", "s2", "s5"):
                for n in f[s]["bar"]:
                    self.assertEqual(n["cur"], "step" if n["st"] == "now" else None, (name, s, n))      # screen readers hear the current step too
                    self.assertEqual(n["tick"] == "✓", n["st"] == "done", (name, s, n))                  # a tick = done
            self.assertEqual([(n["t"], n["st"]) for n in f["s7"]["bar"]], [(t, "done") for t in ("Material", "Heat", "Unload", "Swap", "Load")], name)

    def test_the_home_step_is_left_out_when_the_printer_is_homed_and_the_numbering_stays_honest(self):
        for name, R in self.each():
            f = R["flow"]
            self.assertEqual(f["s1"]["pill"], "STEP 1 OF 5", name)
            self.assertEqual(f["s2"]["pill"], "STEP 2 OF 5", name)
            self.assertEqual(f["s4"]["pill"], "STEP 3 OF 5", name)                  # Unload is the third of five
            self.assertEqual(f["s5"]["pill"], "STEP 4 OF 5", name)
            self.assertNotIn("Home", [n["t"] for n in f["s4"]["bar"]], name)
            u = R["unhomed"]
            self.assertEqual([n["t"] for n in u["s1"]["bar"]], ["Material", "Heat", "Home", "Unload", "Swap", "Load"], name)       # not homed: six steps
            self.assertEqual(u["s1"]["pill"], "STEP 1 OF 6", name)
            self.assertEqual(u["s3"]["pill"], "STEP 3 OF 6", name)
            self.assertEqual([(n["t"], n["st"]) for n in u["s3"]["bar"]][:4], [("Material", "done"), ("Heat", "done"), ("Home", "now"), ("Unload", "todo")], name)
            self.assertEqual(u["after"]["pill"], "STEP 4 OF 6", name)
            self.assertEqual([(n["t"], n["st"]) for n in u["after"]["bar"]][:4], [("Material", "done"), ("Heat", "done"), ("Home", "done"), ("Unload", "now")], name)

    def test_the_guided_change_walks_the_steps_with_picture_and_words(self):
        for name, R in self.each():
            f = R["flow"]
            self.assertEqual(f["s1"]["head"], "Change filament · Rook", name)
            self.assertEqual(f["s1"]["title"], "Pick the new material", name)
            self.assertEqual(f["s1"]["mats"], ["PLA 210°", "PETG 235°", "ABS 245°"], name)
            self.assertEqual([b["t"] for b in f["s1"]["foot"]], ["✕ Cancel", "‹ Back"], name)
            self.assertEqual(f["s2"]["posts"], [{"printer": "Rook", "action": "preheat", "material": "PETG"}], name)      # the bed is not touched
            self.assertEqual(f["s2"]["title"], "Heating the nozzle", name)
            self.assertIn("142° of 235°", f["s2"]["ring"].replace("°of", "° of"), name)                           # the heating ring in words
            self.assertIn("PETG", f["s2"]["ring"], name)
            self.assertIn("Material PETG | Target 235° | Now 142° | Left", f["s2"]["facts"], name)                        # fact chips
            self.assertEqual([b["t"] for b in f["s2"]["foot"]], ["✕ Cancel", "‹ Back", "⬆ Unload — when hot"], name)   # disabled, with the words why
            self.assertTrue(f["s2"]["foot"][2]["dis"], name)
            self.assertEqual(f["back"]["pill"], "STEP 1 OF 5", name)                                                # Back goes back
            self.assertEqual(f["s4"]["posts"], [], name)                                                            # nothing unloads without a tap
            self.assertEqual(f["s4"]["title"], "Unload the old filament", name)
            self.assertEqual(f["s5"]["posts"], [{"printer": "Rook", "action": "unload", "material": "PETG"}], name)
            self.assertEqual(f["s5"]["title"], "Swap the spool", name)
            self.assertEqual(f["s5"]["list"], ["Pull the old filament fully out", "Cut the new tip at an angle ✂️", "Push it into the head until it stops"], name)
            self.assertEqual([b["t"] for b in f["s5"]["foot"]], ["✕ Cancel", "‹ Back", "⬇ Load PETG"], name)
            self.assertEqual(f["s6"]["posts"], [{"printer": "Rook", "action": "load", "material": "PETG"}], name)
            self.assertIn("Loaded", f["s6"]["title"], name)
            self.assertEqual(f["purge"], [{"printer": "Rook", "action": "extrude", "mm": 10}], name)
            self.assertEqual(f["s7"]["pill"], "DONE", name)
            self.assertIn("cool down now?", f["s7"]["title"], name)
            self.assertEqual([b["t"] for b in f["s7"]["foot"]], ["Keep hot", "Cool down"], name)
            self.assertEqual(f["cool"]["posts"], [{"printer": "Rook", "action": "cooldown"}], name)
            self.assertTrue(f["cool"]["main"], name)

    def test_cancel_turns_off_the_heater_the_flow_set_and_keep_hot_leaves_it_to_the_auto_off(self):
        for name, R in self.each():
            self.assertEqual(R["flow"]["cancel"]["posts"], [{"printer": "Rook", "action": "heater_off", "heater": "nozzle"}], name)
            self.assertTrue(R["flow"]["cancel"]["main"], name)
            self.assertEqual(R["flow"]["keep"]["posts"], [], name)
            self.assertIn("auto-off", R["flow"]["keep"]["text"], name)

    def test_an_unhomed_klipper_is_homed_first_with_one_explicit_tap_and_the_jogs_say_why_they_are_off(self):
        for name, R in self.each():
            u = R["unhomed"]
            self.assertEqual(u["s3"]["posts"], [], name)
            self.assertEqual([b["t"] for b in u["s3"]["foot"]], ["✕ Cancel", "‹ Back", "🏠 Home now"], name)
            self.assertEqual(u["after"]["posts"], [{"printer": "Rook", "action": "home", "confirm": True}], name)
            c = u["cards"]
            self.assertEqual(c["pill"].split(" · ")[-1], "NOT HOMED", name) if name != "phone" else self.assertIn("IDLE", c["pill"], name)
            self.assertTrue(all(w["dis"] for w in u["main"]["wedges"][:4]), name)               # the four wedges are off ...
            self.assertFalse(u["main"]["wedges"][4]["dis"], name)                                # ... and Home is the way out
            self.assertTrue(all(b["dis"] for b in c["z"]), name)
            self.assertEqual(u["posts"], [], name)                                               # an off wedge sends nothing
            self.assertIn("Home first", u["main"]["text"], name)

    def test_when_the_hub_refuses_the_reason_is_on_screen_and_the_flow_goes_back(self):
        for name, R in self.each():
            r = R["refused"]
            self.assertEqual(r["pill"], "STEP 1 OF 5", name)
            self.assertIn("printer is not ready (state UNKNOWN)", r["msg"], name)
            self.assertIn("FAILED", r["msg"], name)                                       # a word, not a colour
            self.assertIn("bad", r["cls"], name)

    # ------------------------------------------------------------------ what each printer family is shown
    def test_a_printing_printer_is_locked_in_words_and_the_sheet_opens_from_the_toolbar_and_from_more(self):
        for name, R in self.each():
            p = R["press"]
            self.assertTrue(any("Controls" in t for t in p["more"]), name)
            self.assertEqual(p["lock"], "🔒 Locked while printing", name)                  # ONE clear line in words ...
            self.assertEqual(p["nlock"], 1, name)                                          # ... not a second reason line
            dis = {b["t"]: b["dis"] for b in p["sheet"]["btns"]}
            for t in ("Change filament…", "Unload", "Load", "PLA 210°", "Off", "Up", "Pull"):
                self.assertTrue(any(t in k and v for k, v in dis.items()), (name, t))
            self.assertTrue(all(w["dis"] for w in p["sheet"]["wedges"]), name)           # the ring is locked as well
            self.assertFalse(p["light"]["dis"], name)                                       # the light still works
            self.assertEqual(p["posts"], [], name)                                          # a disabled button sends nothing

    def test_the_idle_sheet_has_no_locked_line(self):
        for name, R in self.each():
            self.assertNotIn("Locked", R["rook"]["cold"]["text"], name)

    def test_a_material_word_comes_only_from_the_running_jobs_file_never_from_a_temperature(self):
        for name, R in self.each():
            self.assertEqual(R["mk4"]["tgt"][0], "→ 245° PETG", name)                      # the file says PETG (an ABS-hot 245 C): PETG, never ABS
            self.assertNotIn("ABS 245°", R["mk4"]["tgt"][0], name)
            self.assertIn("✓ ABS 245°:true", R["mk4"]["chips"], name)                      # the chip may still tick the preset whose temperature matches ...
            self.assertEqual(R["bare"]["tgt"][0], "→ 245°", name)                          # ... but with no material in any file there is NO word
            self.assertNotIn("ABS", R["bare"]["tgt"][0], name)
            self.assertEqual(R["rook"]["live"]["tg"], "→ 235°", name)                      # an idle printer heated by hand: no job, no word
            self.assertEqual(R["press"]["tg"][0], "→ 215°", name)                          # a running job with no material in its file: no word either

    def test_the_page_never_names_a_material_by_matching_a_temperature(self):
        self.assertNotIn("nozNames.find(nozOn)", HTML)
        self.assertNotIn("bedNames.find(bedOn)", HTML)
        fn = block("function jobMats(s){", "// the material in words")
        self.assertIn("BUSY_STATES.includes(s.state)", fn)                                  # a job that is running now ...
        self.assertIn("job_materials", fn)                                                  # ... the snapshot's list from its file ...
        self.assertIn("FS.files.find(", fn)                                                 # ... or the printer screen's file row of that job
        self.assertNotRegex(fn, r"temps|_target_c|nozzle_c|presets")                       # (never a temperature)

    def test_prusalink_shows_everything_disabled_with_the_reason_in_words(self):
        for name, R in self.each():
            l = R["link"]
            self.assertIsNotNone(l["btn"], name)                                            # the button is there ...
            s = l["sheet"]
            self.assertIn("PrusaLink's v1 API has no light, temperature, G-code or filament endpoint", s["text"], name)   # ... and says why
            self.assertTrue(all(b["dis"] for b in s["btns"] if b["act"] not in ("csClose", "csStep")), name)
            self.assertTrue(all(w["dis"] for w in s["wedges"][:4]), name)
            self.assertEqual([b["t"] for b in l["cards"]["chips"][1]], ["PLA 60°", "PETG 80°", "ABS 100°", "✓ Off"], name)    # the same buttons, just off

    def test_an_mmu_printer_cannot_change_filament_but_can_heat_move_and_extrude(self):
        for name, R in self.each():
            m = R["mmu"]
            self.assertIn("MMU printer - load/unload from the printer's screen", m["sheet"]["text"], name)
            self.assertTrue(all(b["dis"] for b in m["cards"]["fil"]), name)
            self.assertFalse(any(b["dis"] for b in m["cards"]["chips"][0]), name)
            self.assertTrue(m["change"]["dis"], name)

    def test_bambu_move_and_extrude_are_labelled_beta_and_load_picks_an_ams_slot(self):
        for name, R in self.each():
            b = R["bambu"]
            self.assertIn("beta (not yet tried on this printer)", b["sheet"]["text"], name)
            self.assertIn("BETA", b["cards"]["move"], name)
            self.assertEqual([s["t"] for s in b["load"]["slots"]], ["Slot 1 GREEN PLA", "Slot 2 BLACK PETG", "Slot 3 EMPTY"], name)
            self.assertTrue(b["load"]["slots"][2]["dis"], name)                              # an empty slot cannot be loaded
            self.assertEqual(b["posts"], [{"printer": "P1S", "action": "load", "tray": 2}], name)    # no material: the AMS heats by itself

    def test_a_view_only_sign_in_never_sees_or_opens_it(self):
        for name, R in self.each():
            self.assertIsNone(R["viewonly"]["btn"], name)
            self.assertFalse(R["viewonly"]["open"], name)

    def test_fahrenheit_shows_fahrenheit_but_the_hub_still_gets_the_material_name(self):
        for name, R in self.each():
            self.assertEqual(R["fahr"]["noz"], ["PLA 410°F", "PETG 455°F", "ABS 473°F", "✓ Off"], name)
            self.assertEqual(R["fahr"]["now"][0], "77°F", name)


# ============================================================================================================ Edit printer > More options

EDIT_DRIVER = r"""
const rec = {name: 'Rook', type: 'moonraker', type_label: 'Klipper', address: 'a', model: '', driver_available: true, loaded: true, secrets: {}, values: {name: 'Rook', url: 'http://x'}, groups: [], jog_flip_y: false};
const FIELDS = [{name: 'name', label: 'Printer name', type: 'text', required: true}, {name: 'url', label: 'Address', type: 'url', required: true},
  {name: 'camera_url', label: 'Camera address', type: 'text', required: false, advanced: true, secret: true, shown_redacted: true}];
window.__bodies = [];
window.__stubs.push((u, m, init) => {
  if(u === '/api/fleet') return J([]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: []});
  if(u === '/api/groups') return J([]);
  if(u === '/api/printers') return J({printers: [rec]});
  if(u.startsWith('/api/printer-types')) return J({types: [{type: 'moonraker', label: 'Klipper', description: 'd', fields: FIELDS}]});
  if(/^\/api\/printers\/[^/]+$/.test(u) && m === 'PUT'){ window.__bodies.push(JSON.parse(init.body)); return J({printer: rec, changed: ['jog_flip_y']}); }
  return null;
});
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
await sleep(200);
await openPrinters(); await wait(() => document.querySelector('.pmrow'));
await pmEdit('Rook'); await wait(() => document.getElementById('pm-form'));
const f = document.getElementById('pm-f-jog_flip_y');
R.field = !!f; R.label = f ? document.querySelector('label[for=pm-f-jog_flip_y]').textContent : null;
R.options = f ? [...f.options].map(o => o.textContent) : null; R.inMore = !!(f && f.closest('#pm-more-body')); R.hiddenAtFirst = document.getElementById('pm-more-body').hidden;
R.before = {save: $('pm-save').textContent, dis: $('pm-save').disabled};
f.value = '1'; pmDirty();
R.after = {save: $('pm-save').textContent, dis: $('pm-save').disabled, skipHidden: $('pm-skip').hidden};          // a display setting: no connection test needed
$('pm-save').click(); await wait(() => window.__bodies.length);
R.sent = window.__bodies[0] || null;
"""


class EditPrinterTests(unittest.TestCase):
    def test_flip_y_is_in_more_options_off_by_default_and_saves_without_a_connection_test(self):
        R = run_q(self, EDIT_DRIVER_WRAPPED(), "", (1024, 600), 120000)
        self.assertNotIn("error", R, R.get("error"))
        self.assertTrue(R["field"])
        self.assertEqual(R["label"].replace(" (optional)", ""), "Flip Y on the jog pad")
        self.assertEqual(R["options"], ["Off - Y+ (back) on top", "On - Y- (front) on top"])
        self.assertTrue(R["inMore"])
        self.assertTrue(R["hiddenAtFirst"])                                           # tucked away under More options
        self.assertEqual(R["before"], {"save": "No changes", "dis": True})
        self.assertEqual(R["after"], {"save": "Save changes", "dis": False, "skipHidden": True})       # live at once, no "test first"
        self.assertIs(R["sent"]["values"]["jog_flip_y"], True)
        self.assertEqual(R["sent"]["values"]["name"], "Rook")


def EDIT_DRIVER_WRAPPED():
    return PRE + EDIT_DRIVER


if __name__ == "__main__":
    unittest.main(verbosity=1)
