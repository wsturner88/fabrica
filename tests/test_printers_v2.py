"""Printers v2 in the dashboard: the picture-card list, Add / Edit as pop-ups, "what this printer has", and the temperature cards.

    python3 tests/test_printers_v2.py

The browser tests load the real dashboard.html into headless Chrome / Edge with a stubbed `fetch` (skipped when no browser is found); nothing contacts
a printer, a server or the network. Names are generic and the addresses are the documentation range 192.0.2.x.
Widths are compared with document.documentElement.clientWidth (a Linux Chrome draws a 15 px scrollbar), never a hard-coded viewport width.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ.setdefault("BAMBU_MCP_CONFIG", os.path.join(os.path.dirname(HERE), "none.yml"))

import fleet_store  # noqa: E402
from test_layout_5in import run_q  # noqa: E402
from test_parity_layouts import WIDE  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()


def _schema():
    return json.dumps({"types": [f.describe() for f in fleet_store._make_families().values()]})


SIZES = {"desktop": (1280, 800), "wall": (1024, 600), "phone": (500, 900)}

# ------------------------------------------------------------------------------------------------------ shared fixture
COMMON = r"""
const SCHEMA = %(schema)s;
SCHEMA.types.push({type: 'foo', label: 'Foo Printer', description: 'A kind this build has no special drawing for.', fields: [
  {name: 'name', label: 'Printer name', type: 'text', required: true}, {name: 'url', label: 'Address', type: 'url', required: true},
  {name: 'camera_url', label: 'Camera address', type: 'text', required: false, advanced: true, secret: true}]});
const rec = (name, type, label, o) => Object.assign({name, type, type_label: label, address: 'http://192.0.2.10', model: '', driver_available: true, loaded: true,
  secrets: {}, values: {name}, groups: [], camera_url: '', detect_area: null, jog_flip_y: false}, o || {});
const fl = (printer, o) => Object.assign({printer, connected: true, state: 'IDLE', percent: 0, remaining_min: 0, job: '', nozzle_c: 24, bed_c: 23, nozzle_target_c: 0,
  bed_target_c: 0, camera: false, kind: 'moonraker', groups: []}, o || {});
const PROFILES = [
  {id: 'generic-prusalink', label: 'Any PrusaLink printer', maker: 'Generic', types: ['prusalink'], verified: false,
   has: {light: null, nozzles: 1, nozzle_max_c: null, bed_mm: null, bed_max_c: null, chamber: null, filament: {system: 'none', slots: 1}, camera_builtin: false}},
  {id: 'model-a', label: 'Model A', maker: 'Maker', types: ['prusalink'], verified: true,
   has: {light: true, nozzles: 1, nozzle_max_c: 290, bed_mm: [250, 220], bed_max_c: 120, chamber: 'sensor', filament: {system: 'none', slots: 1}, camera_builtin: false}},
  {id: 'model-b', label: 'Model B', maker: 'Maker', types: ['prusalink'], verified: true,
   has: {light: false, nozzles: 2, nozzle_max_c: 300, bed_mm: [300, 300], bed_max_c: 100, chamber: 'heater', filament: {system: 'ams', slots: 4}, camera_builtin: true}}];
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
const docW = document.documentElement.clientWidth;
const noScroll = () => document.documentElement.scrollWidth - docW <= 1;
const T = e => e ? e.textContent.replace(/\s+/g, ' ').trim() : null;
const vis = e => { if(!e) return false; const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
"""

LIST_DRIVER = COMMON + r"""
let FLEET = [fl('Printer-A', {kind: 'bambu', camera: true, state: 'RUNNING', percent: 42, groups: ['WORK']}),
  fl('Printer-B', {kind: 'prusalink', camera: true, groups: ['WORK']}),
  fl('Printer-C', {kind: 'moonraker', camera: false, groups: ['Home']}),
  fl('Printer-D', {kind: 'octoprint', camera: true, connected: false, state: 'UNKNOWN', groups: ['Home']})];
const PRN = [rec('Printer-A', 'bambu', 'Bambu Lab', {model: 'Model X', groups: ['WORK'], address: '192.0.2.20'}),
  rec('Printer-B', 'prusalink', 'PrusaLink', {model: 'Model A', groups: ['WORK'], camera_url: 'rtsp://***@192.0.2.60/live'}),
  rec('Printer-C', 'moonraker', 'Klipper (Moonraker)', {model: 'Model C', groups: ['Home']}),
  rec('Printer-D', 'octoprint', 'OctoPrint', {model: '', groups: ['Home']})];
window.__stubs.push((u, m, init) => {
  const path = u.split('?')[0];
  if(path === '/api/fleet') return J(FLEET);
  if(path === '/api/printers') return J({printers: PRN});
  if(path === '/api/detection') return J({enabled: true, mode: 'alert', detector: {state: 'up'}, setup: {excluded_printers: ['Printer-D']}, printers: {'Printer-B': {watching: false, learning: 'again'}}});
  if(path === '/api/ui-version') return J({v: '1'});
  if(path === '/api/auth/me') return J({});
  if(path === '/api/settings') return J({sections: []});
  if(path === '/api/groups') return J([]);
  if(path.startsWith('/api/printer-types')) return J(SCHEMA);
  if(path === '/api/profiles') return J({error: 'not found'}, 404);
  return null;
});
await sleep(200);
await openPrinters(); await wait(() => document.querySelector('.pl-card'));
const card = n => [...document.querySelectorAll('.pl-card')].find(c => c.dataset.name === n);
const info = c => ({name: T(c.querySelector('.pl-nm b')), model: T(c.querySelector('.pl-nm span')), state: T(c.querySelector('.pl-state')), off: c.querySelector('.pl-state').classList.contains('off'),
  chips: [...c.querySelectorAll('.pl-h')].map(e => [T(e), e.classList.contains('miss')]), group: T(c.querySelector('.pl-grp')), hasImg: !!c.querySelector('img[data-pic]'),
  none: T(c.querySelector('.pl-none')), addCam: !!c.querySelector('[data-act=pmAddCam]'), dots: !!c.querySelector('[data-act=pmCardMenu]')});
R.cards = {}; for(const n of ['Printer-A', 'Printer-B', 'Printer-C', 'Printer-D']) R.cards[n] = info(card(n));
R.count = document.querySelectorAll('.pl-card').length; R.addTile = !!document.querySelector('.pl-add');
R.toolbar = {title: T(document.getElementById('pr-title')), seg: [...document.querySelectorAll('.pl-seg button')].map(b => [b.dataset.g, T(b), b.getAttribute('aria-pressed')]),
  actions: [...document.querySelectorAll('#pr-actions button')].map(b => T(b)), oneRow: Math.abs(document.getElementById('pr-title').getBoundingClientRect().top - document.querySelector('#pr-actions .primary').getBoundingClientRect().top) < 40};
R.fit = {noScroll: noScroll(), docOver: document.documentElement.scrollWidth - docW};
R.cardW = Math.round(card('Printer-A').getBoundingClientRect().width);
// the 5-second refresh does not touch the cards or their pictures
const keep = card('Printer-A'), keepImg = keep.querySelector('img[data-pic]');
await pmRefresh(); await pmRefresh();
R.stable = {sameCard: card('Printer-A') === keep, sameImg: card('Printer-A').querySelector('img[data-pic]') === keepImg};
// a changed state IS drawn
FLEET[0].percent = 50; await pmRefresh();
R.changed = T(card('Printer-A').querySelector('.pl-state')); R.changedNew = card('Printer-A') !== keep;
// the group filter
document.querySelector('.pl-seg [data-g=Home]').click(); await sleep(50);
R.filterHome = {names: [...document.querySelectorAll('.pl-card')].map(c => c.dataset.name), pressed: [...document.querySelectorAll('.pl-seg button')].map(b => b.getAttribute('aria-pressed')), tick: T(document.querySelector('.pl-seg [aria-pressed=true]'))};
R.addTileInFilter = !!document.querySelector('.pl-add');
document.querySelector('.pl-seg [data-g=""]').click(); await sleep(50);
R.filterAll = document.querySelectorAll('.pl-card').length;
// the card's ... menu and a tap on the card
document.querySelector('[data-act=pmCardMenu][data-name=Printer-C]').click(); await sleep(50);
const mn = document.getElementById('pm-menu');
R.menu = {open: !mn.hidden, items: [...mn.querySelectorAll('button')].filter(vis).map(b => T(b)), inView: vis(mn) && mn.getBoundingClientRect().right <= docW + 1};
document.querySelector('#pm-menu [data-act=pmEditCard]').click(); await wait(() => document.getElementById('pm-form'));
R.edit = {open: !!document.getElementById('pm-form'), title: T(document.querySelector('.ps h2')), modal: document.getElementById('modal').classList.contains('on'), menuGone: document.getElementById('pm-menu').hidden};
closeModal(); await wait(() => PM.view === 'list');
R.afterClose = {view: PM.view, sheet: PM.sheet};
card('Printer-B').click(); await wait(() => document.getElementById('pm-form'));
R.tap = T(document.querySelector('.ps h2')); R.gone = []; closeModal(); R.gone.push(document.getElementById('mbody').innerHTML === ''); await sleep(100);
// + Add camera on a printer without one: Edit, with the camera box ready
await wait(() => card('Printer-C'));
card('Printer-C').querySelector('[data-act=pmAddCam]').click(); await wait(() => document.getElementById('pm-form'));
R.addCam = {title: T(document.querySelector('.ps h2')), focus: document.activeElement && document.activeElement.id};
closeModal(); await sleep(100);
// toolbar menu: Manage groups opens the existing groups section
document.querySelector('[data-act=pmTopMenu]').click(); await sleep(30);
R.topMenu = [...document.querySelectorAll('#pm-menu button')].filter(vis).map(b => T(b));
document.querySelector('#pm-menu [data-act=pmGroups]').click(); await sleep(50);
R.groupsSection = !!document.getElementById('pg-sec') && document.querySelectorAll('[data-act=pgRename]').length;
// no camera answered here (the stub has no picture): the fallback words, not a broken image
R.noPic = T(card('Printer-A').querySelector('.pl-none'));
"""

ADD_DRIVER = COMMON + r"""
let FLEET = [fl('Printer-A', {kind: 'bambu'}), fl('Printer-B', {kind: 'prusalink'}), fl('Printer-C', {kind: 'prusalink'})];
const PRN = [rec('Printer-A', 'bambu', 'Bambu Lab'), rec('Printer-B', 'prusalink', 'PrusaLink'), rec('Printer-C', 'prusalink', 'PrusaLink')];
window.__stubs.push((u, m, init) => {
  const path = u.split('?')[0];
  if(path === '/api/fleet') return J(FLEET);
  if(path === '/api/printers' && m === 'GET') return J({printers: PRN});
  if(path === '/api/printers/test') return J({result: {ok: true, message: 'Connected.'}});
  if(path === '/api/printers' && m === 'POST') return J({printer: {name: 'Printer-N'}});
  if(path === '/api/ui-version') return J({v: '1'});
  if(path === '/api/auth/me') return J({});
  if(path === '/api/settings') return J({sections: []});
  if(path === '/api/groups') return J([]);
  if(path.startsWith('/api/printer-types')) return J(SCHEMA);
  if(path === '/api/profiles') return J({error: 'not found'}, 404);
  if(path === '/api/detection') return J({enabled: true, detector: {state: 'up'}, setup: {excluded_printers: []}, printers: {}});
  if(path.startsWith('/api/discover/suggestions')) return J({networks: ['192.0.2.0/24'], sources: {}, note: ''});
  return null;
});
await sleep(200);
await openPrinters(); await wait(() => document.querySelector('.pl-card'));
document.querySelector('.pl-add').click(); await wait(() => document.querySelector('.ps-kind'));
const steps = () => [...document.querySelectorAll('#ps-steps .cs-st')].map(e => [T(e.querySelector(':scope > span')).replace(/\s*\((done|now)\)$/, ''), e.className.replace('cs-st ', '')]);
const tiles = [...document.querySelectorAll('.ps-kind.pmfam')];
R.kinds = {types: tiles.map(t => t.dataset.type), labels: tiles.map(t => T(t.querySelector('b'))), have: tiles.map(t => T(t.querySelector('.have'))), arts: tiles.map(t => !!t.querySelector('svg.pa')),
  find: T(document.querySelector('.ps-find b')), go: T(document.querySelector('.ps-find .go')), radar: !!document.querySelector('.ps-find svg.radar'), steps: steps(), title: T(document.querySelector('.ps h2')),
  fit: {noScroll: noScroll(), body: (() => { const b = document.querySelector('.ps-body'); return b.scrollWidth - b.clientWidth; })()}, behind: !!document.querySelector('.pl-card')};
R.kindArt = {bambu: tiles.find(t => t.dataset.type === 'bambu').querySelector('svg').innerHTML.slice(0, 40), foo: tiles.find(t => t.dataset.type === 'foo').querySelector('svg').innerHTML.slice(0, 40)};
// the kind tile opens the form INSIDE the same pop-up and the step bar moves
const modalBefore = document.getElementById('mbody');
tiles.find(t => t.dataset.type === 'prusalink').click(); await wait(() => document.getElementById('pm-form'));
R.form = {sameBox: document.getElementById('mbody') === modalBefore, steps: steps(), title: T(document.querySelector('.ps h2')), fields: [...document.querySelectorAll('#pm-form [id^=pm-f-]')].map(e => e.id.slice(5)),
  camera: !!document.getElementById('pm-camsec'), foot: [...document.querySelectorAll('.ps-foot button')].map(b => T(b)), noScroll: noScroll(), sheets: document.querySelectorAll('.ps').length};
document.getElementById('pm-f-camera_url').focus(); await sleep(30); R.cameraStep = steps();
document.getElementById('pm-f-name').focus(); await sleep(30); R.connectStep = steps();
const set = (id, v) => { document.getElementById(id).value = v; document.getElementById(id).dispatchEvent(new Event('input')); };
set('pm-f-name', 'Printer-N'); set('pm-f-url', '192.0.2.77'); set('pm-f-password', 'secret');
document.getElementById('pm-test').click(); await wait(() => PM.tested);
R.tested = {steps: steps(), save: [T(document.getElementById('pm-save')), document.getElementById('pm-save').disabled], result: T(document.getElementById('pm-result'))};
document.getElementById('pm-save').click(); await wait(() => window.__calls.some(c => c.startsWith('POST /api/printers ')));
R.post = JSON.parse(window.__calls.filter(c => c.startsWith('POST /api/printers ')).pop().split(' ').slice(2).join(' '));
await wait(() => !document.getElementById('modal').classList.contains('on')); await sleep(200);
R.after = {modal: document.getElementById('modal').classList.contains('on'), view: PM.view, msg: !!document.querySelector('#pr-body .warnbox')};
// Back from the form returns to the kinds (still one pop-up)
await pmAddStart(); await wait(() => document.querySelector('.ps-kind'));
document.querySelector('.ps-kind.pmfam[data-type=bambu]').click(); await wait(() => document.getElementById('pm-form'));
R.bambuForm = {title: T(document.querySelector('.ps h2')), fields: [...document.querySelectorAll('#pm-form [id^=pm-f-]')].map(e => e.id.slice(5))};
document.querySelector('.ps-foot .ps-quiet').click(); await wait(() => document.querySelector('.ps-kind'));
R.back = {kinds: document.querySelectorAll('.ps-kind.pmfam').length, steps: steps()};
// a tap on the dimmed screen never throws a half-typed form away, but it does close the kind picker
document.getElementById('modal').click(); await sleep(50); R.backdropPick = document.getElementById('modal').classList.contains('on');
await pmAddStart(); await wait(() => document.querySelector('.ps-kind'));
document.querySelector('.ps-kind.pmfam[data-type=foo]').click(); await wait(() => document.getElementById('pm-form'));
set('pm-f-name', 'half typed'); document.getElementById('modal').click(); await sleep(50); R.backdropForm = document.getElementById('modal').classList.contains('on');
R.fooForm = [...document.querySelectorAll('#pm-form [id^=pm-f-]')].map(e => e.id.slice(5));
document.querySelector('.cs-x').click(); await sleep(100); R.closed = {modal: document.getElementById('modal').classList.contains('on'), view: PM.view};
// "Find it for me" leaves the pop-up for the existing discovery page
await pmAddStart(); await wait(() => document.querySelector('.ps-find'));
document.querySelector('.ps-find').click(); await wait(() => PM.view === 'discover'); await sleep(100);
R.find = {modal: document.getElementById('modal').classList.contains('on'), title: T(document.querySelector('#mbody .ps .cs-mh h2')), scan: !!document.querySelector('#dc-btns .primary'), printers: document.getElementById('printers').classList.contains('on')};       // Find printers is a pop-up over the Printers list
"""

EDIT_DRIVER = COMMON + r"""
window.__bodies = [];
let FLEET = [fl('Printer-B', {kind: 'prusalink', camera: true, groups: ['WORK']}),
  fl('Printer-K', {kind: 'moonraker', camera: false, sensors: [{id: 'temperature_sensor ebb', label: 'Toolhead air', role: 'toolhead', c: 34.7}, {id: 'temperature_sensor pi', label: 'Pi', role: 'host', c: 55}]})];
const B = rec('Printer-B', 'prusalink', 'PrusaLink', {model: 'Model A', address: '192.0.2.30', groups: ['WORK'], camera_url: 'rtsp://***@192.0.2.60/live', secrets: {password: 'set'},
  values: {name: 'Printer-B', url: '192.0.2.30', username: 'maker', storage: 'usb', model: 'Model A'}, detect_area: {x: 10, y: 10, w: 50, h: 50},
  profile: {id: 'model-a', label: 'Model A', auto: true, verified: true, overridden: [], overrides: {}, has: PROFILES[1].has}});
const K = rec('Printer-K', 'moonraker', 'Klipper (Moonraker)', {address: 'http://192.0.2.50', values: {name: 'Printer-K', url: '192.0.2.50', camera_base_url: '', model: ''},
  profile: {id: 'generic-moonraker', label: 'Any Klipper printer', auto: false, verified: false, overridden: ['chamber_sensor'], overrides: {chamber_sensor: 'temperature_sensor old', sensor_labels: {'temperature_sensor ebb': 'Hotend'}},
    has: {light: null, nozzles: 1, nozzle_max_c: null, bed_mm: null, bed_max_c: null, chamber: 'sensor', filament: {system: 'none', slots: 1}, camera_builtin: false}}});
PROFILES.push({id: P2, label: P1, maker: P3, types: ['moonraker'], verified: false, has: PROFILES[0].has});
const X = rec('Printer-X', 'moonraker', 'Klipper (Moonraker)', {values: {name: 'Printer-X', url: '192.0.2.51', camera_base_url: '', model: P1}, groups: [P1], model: P1,
  profile: {id: P2, label: P1, auto: false, verified: false, overridden: ['sensor_labels', 'chamber_sensor'], overrides: {sensor_labels: {[P1]: P3}, chamber_sensor: P2}, has: PROFILES[0].has}});
const PLAIN = rec('Printer-P', 'octoprint', 'OctoPrint', {values: {name: 'Printer-P', url: 'http://192.0.2.40', model: ''}});          // an older Hub: no profile block
let PRN = [B, K, PLAIN, X];
FLEET.push(fl('Printer-X', {kind: 'moonraker', sensors: [{id: P1, label: P3, role: 'other', c: 1}, {id: P2, label: P1, role: 'host', c: 2}]}));
let WITHPROFILES = true;
window.__stubs.push((u, m, init) => {
  const path = u.split('?')[0];
  if(path === '/api/fleet') return J(FLEET);
  if(path === '/api/printers' && m === 'GET') return J({printers: PRN});
  if(path === '/api/ui-version') return J({v: '1'});
  if(path === '/api/auth/me') return J({});
  if(path === '/api/settings') return J({sections: []});
  if(path === '/api/groups') return J([]);
  if(path === '/api/detection') return J({enabled: true, detector: {state: 'up'}, setup: {excluded_printers: []}, printers: {}});
  if(path.startsWith('/api/printer-types')) return J(SCHEMA);
  if(path === '/api/profiles') return WITHPROFILES ? J({profiles: PROFILES}) : J({error: 'not found'}, 404);
  if(path === '/api/printers/test') return J({result: {ok: true, message: 'Connected.'}});
  if(path === '/api/printer/Printer-B') return J({snapshot: FLEET[0], controls: {actions: [], detail: {light: {supported: false, reason: "This printer has a light, but PrusaLink can't switch it - use the printer's own screen."}}}});
  if(/^\/api\/printers\/[^/]+$/.test(path) && m === 'PUT'){ window.__bodies.push(JSON.parse(init.body)); if(window.__busy){ window.__busy = false; return J({busy: true, error: 'Printer-B is printing right now.'}, 409); } return J({printer: B, changed: []}); }
  if(/^\/api\/printers\/[^/]+$/.test(path) && m === 'DELETE'){ window.__deleted = (window.__deleted || []).concat([path + (u.includes('force') ? '?force=1' : '')]); return J({ok: true}); }
  return null;
});
const idsOf = () => [...document.querySelectorAll('#pm-form [id^=pm-f-]')].map(e => e.id.slice(5));
const input = (id, v) => { const e = document.getElementById(id); e.value = v; e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); };
const rows = () => Object.fromEntries([...document.querySelectorAll('#pf-card .pf-cap')].map(c => [T(c.querySelector('.pf-val b')), {val: T(c.querySelector('.pf-val span')), how: T(c.querySelector('.pf-how')), mine: c.classList.contains('mine'), cant: c.classList.contains('cant'),
  change: !!c.querySelector('.pf-chg')}]));
const save = () => ({label: T(document.getElementById('pm-save')), disabled: document.getElementById('pm-save').disabled});
await sleep(200);
await openPrinters(); await wait(() => document.querySelector('.pl-card'));
await pmEdit('Printer-B'); await wait(() => document.getElementById('pm-save')); await wait(() => rows()['Light'] && rows()['Light'].cant);
R.header = {title: T(document.querySelector('.ps h2')), sub: T(document.querySelector('.ps-sub')), thumb: !!document.querySelector('.ps-thumb'), listBehind: !!document.querySelector('.pl-card')};
R.fields = {ids: idsOf(), types: Object.fromEntries(['url', 'password', 'camera_url'].map(k => [k, document.getElementById('pm-f-' + k).type])), values: {url: document.getElementById('pm-f-url').value, name: document.getElementById('pm-f-name').value,
  readonlyName: document.getElementById('pm-f-name').readOnly, camera: document.getElementById('pm-f-camera_url').value, username: document.getElementById('pm-f-username').value}};
R.cards = {camera: !!document.querySelector('#pm-camsec #ps-cam'), watch: !!document.querySelector('#ps-cam .ps-watch'), watchStyle: (() => { const w = document.getElementById('ps-watch'); return w ? [w.style.left, w.style.top, w.style.width, w.style.height] : null; })(),
  areaEditor: !!document.querySelector('#pm-camsec #dw-field'), chips: [...document.querySelectorAll('.pm-chip')].map(b => T(b)), own: !!document.querySelector('[data-act=pmCamOwn]'),
  conn: !!document.querySelector('#ps-conn #pm-test'), now: T(document.getElementById('pm-now')), groups: [...document.querySelectorAll('.pg-chip > span')].map(e => T(e)), jog: [...document.querySelectorAll('[data-act=psJog]')].map(b => [T(b.querySelector('div')), b.getAttribute('aria-pressed')]),
  more: {hidden: document.getElementById('pm-more-body').hidden, sum: T(document.getElementById('ps-sum'))}, footer: [...document.querySelectorAll('.ps-foot button')].filter(vis).map(b => T(b)), save: save(),
  tip: {btn: !!document.querySelector('[data-act=pmTip]'), hidden: document.getElementById('ps-tipbox').hidden}};
R.secret = (() => { const s = document.querySelector('[data-act=pmShow]'); const el = document.getElementById(s.dataset.for); const a = el.type; s.click(); const b = el.type; s.click(); return [a, b, el.type, el.placeholder]; })();
R.fit = {noScroll: noScroll(), body: (() => { const b = document.querySelector('.ps-body'); return b.scrollWidth - b.clientWidth; })(), box: (() => { const r = document.getElementById('mbody').getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right), Math.round(r.top), Math.round(r.bottom)]; })(), vw: docW, vh: innerHeight};
R.profile = {rows: rows(), picker: [...document.querySelectorAll('#pm-profile option')].map(o => [o.value, T(o), o.selected]), note: T(document.querySelector('#pf-card > .pf-note:last-child')), hasSens: !!document.querySelector('#pf-card .pf-sens')};
// the tip box
document.querySelector('[data-act=pmTip]').click(); R.tipOpen = !document.getElementById('ps-tipbox').hidden; document.querySelector('[data-act=pmTip]').click();
// the picture choice for the jog ring drives the select and the one Save button
document.querySelector('[data-act=psJog][data-v="1"]').click();
R.jog = {value: document.getElementById('pm-f-jog_flip_y').value, pressed: [...document.querySelectorAll('[data-act=psJog]')].map(b => b.getAttribute('aria-pressed')), save: save()};
document.querySelector('[data-act=psJog][data-v=""]').click(); R.jogBack = save();
// a profile row: Light -> No is a change for THIS printer only
document.querySelector('[data-act=pfOpen][data-k=light]').click();
document.querySelector('[data-act=pfSet][data-k=light][data-v=false]').click();
R.lightRow = {row: rows()['Light'], save: save()};
document.querySelector('[data-act=pfOpen][data-k=light]').click();
// nozzles: one more, and a lower max temperature
document.querySelector('[data-act=pfOpen][data-k=nozzles]').click();
document.querySelector('[data-act=pfStep][data-d="1"]').click();
input(document.querySelector('[data-pf=nozzle_max_c]').id || (document.querySelector('[data-pf=nozzle_max_c]').id = 'tmp-noz'), '250');
R.nozRow = rows()['Nozzles'];
document.querySelector('[data-act=pfOpen][data-k=nozzles]').click();
document.getElementById('pm-save').click(); await wait(() => window.__bodies.length); await wait(() => PM.view === 'list');
R.put1 = window.__bodies[0]; R.afterSave = {modal: document.getElementById('modal').classList.contains('on'), msg: T(document.querySelector('#pr-body .warnbox'))};
// the same printer again: the saved override is still marked, "back to the profile" puts it right and sends an empty set
B.profile.overridden = ['light', 'nozzles', 'nozzle_max_c']; B.profile.overrides = {light: false, nozzles: 2, nozzle_max_c: 250}; B.profile.has = Object.assign({}, PROFILES[1].has, B.profile.overrides);
await pmRefresh(); await pmEdit('Printer-B'); await wait(() => document.getElementById('pm-save'));
R.again = {rows: rows(), save: save()};
for(const k of ['light', 'nozzles']){ document.querySelector('[data-act=pfOpen][data-k=' + k + ']').click(); document.querySelector('[data-act=pfReset][data-k=' + k + ']').click(); }
R.reset = {rows: rows(), save: save()};
document.getElementById('pm-save').click(); await wait(() => window.__bodies.length > 1); R.put2 = window.__bodies[1]; await wait(() => PM.view === 'list');
// the picker: another profile -> profile_id; Automatic -> null again
B.profile = {id: 'model-a', label: 'Model A', auto: true, verified: true, overridden: [], overrides: {}, has: PROFILES[1].has};
await pmRefresh(); await pmEdit('Printer-B'); await wait(() => document.getElementById('pm-save'));
const sel = document.getElementById('pm-profile'); sel.value = 'model-b'; sel.dispatchEvent(new Event('change', {bubbles: true}));
R.pick = {rows: rows(), save: save()}; document.getElementById('pm-save').click(); await wait(() => window.__bodies.length > 2); R.put3 = window.__bodies[2]; await wait(() => PM.view === 'list');
B.profile = {id: 'model-b', label: 'Model B', auto: false, verified: true, overridden: [], overrides: {}, has: PROFILES[2].has};
await pmRefresh(); await pmEdit('Printer-B'); await wait(() => document.getElementById('pm-save'));
R.explicit = {sel: document.getElementById('pm-profile').value, auto: T(document.querySelector('#pm-profile option[value=""]'))};
const s2 = document.getElementById('pm-profile'); s2.value = ''; s2.dispatchEvent(new Event('change', {bubbles: true}));
document.getElementById('pm-save').click(); await wait(() => window.__bodies.length > 3); R.put4 = window.__bodies[3]; await wait(() => PM.view === 'list');
// a Klipper printer: its sensors can be renamed and one of them made the chamber
await pmEdit('Printer-K'); await wait(() => document.getElementById('pm-save'));
R.sens = {rows: [...document.querySelectorAll('#pf-card .pf-srow')].map(r => [r.querySelector('input').value, T(r.querySelector('.sc')), T(r.querySelector('button')), r.querySelector('button').getAttribute('aria-pressed')]),
  rowsK: rows()['Chamber'], save: save()};
const inp = document.querySelector('.pf-srow input[data-pfs="temperature_sensor pi"]'); inp.value = 'Raspberry Pi'; inp.dispatchEvent(new Event('input', {bubbles: true}));
document.querySelector('[data-act=pfChamber][data-id="temperature_sensor ebb"]').click();
R.sens2 = {pressed: [...document.querySelectorAll('.pf-srow button')].map(b => b.getAttribute('aria-pressed')), save: save()};
document.getElementById('pm-save').click(); await wait(() => window.__bodies.length > 4); R.put5 = window.__bodies[4]; await wait(() => PM.view === 'list');
// an older Hub: no profile on the printer -> no card; /api/profiles missing -> no picker, no Change buttons
await pmEdit('Printer-P'); await wait(() => document.getElementById('pm-save'));
R.plain = {cardHidden: document.getElementById('pf-card').hidden, cardDisplay: getComputedStyle(document.getElementById('pf-card')).display, save: save(), fields: idsOf()}; closeModal(); await sleep(100);
WITHPROFILES = false; PF.tried = false; PF.list = null;
await pmEdit('Printer-B'); await wait(() => document.getElementById('pm-save'));
R.noListPre = 1;
R.noList = {picker: !!document.getElementById('pm-profile'), change: document.querySelectorAll('#pf-card .pf-chg').length, rows: Object.keys(rows()), note: T(document.querySelector('#pf-card .pf-note:last-child'))};
closeModal(); await sleep(100);
// names, labels and groups that come from the Hub are shown as text only
await pmEdit('Printer-X'); await wait(() => document.getElementById('pm-save')); await sleep(100);
R.hostile = {injected: injected(), alerts: window.__alerts.length, sens: document.querySelectorAll('#pf-card .pf-srow').length, title: T(document.querySelector('.ps h2'))}; closeModal(); await sleep(100);
// a printer that is busy: the question comes up in the same pop-up, and "Change it anyway" sends the save again with force
window.__busy = true; window.__bodies.length = 0;
await pmEdit('Printer-B'); await wait(() => document.getElementById('pm-save'));
document.querySelector('[data-act=psJog][data-v="1"]').click(); document.getElementById('pm-save').click(); await wait(() => document.querySelector('.ps-small'));
R.busy = {title: T(document.querySelector('.ps-small h2')), text: T(document.querySelector('.ps-confirm')), buttons: [...document.querySelectorAll('.ps-small button:not(.cs-x)')].map(b => T(b)), one: document.querySelectorAll('.ps').length};
document.querySelector('.ps-small .danger').click(); await wait(() => window.__bodies.length > 1); await wait(() => PM.view === 'list');
R.forced = {n: window.__bodies.length, first: window.__bodies[0].force, second: window.__bodies[1].force, flip: window.__bodies[1].values.jog_flip_y, modal: document.getElementById('modal').classList.contains('on')};
// remove: asked in a small pop-up; Keep leaves it, Remove sends the delete and says so on the list
pmAskRemove('Printer-B'); await wait(() => document.querySelector('.ps-small'));
R.remove = {title: T(document.querySelector('.ps-small h2')), buttons: [...document.querySelectorAll('.ps-small button:not(.cs-x)')].map(b => T(b))};
document.querySelector('.ps-small button').click(); await sleep(100); R.kept = {modal: document.getElementById('modal').classList.contains('on'), deleted: window.__deleted || []};
document.querySelector('[data-act=pmCardMenu][data-name=Printer-B]').click(); document.querySelector('#pm-menu [data-act=pmRemoveCard]').click(); await wait(() => document.querySelector('.ps-small'));
document.querySelector('.ps-small .danger').click(); await wait(() => (window.__deleted || []).length); await wait(() => PM.view === 'list'); await sleep(100);
R.removed = {deleted: window.__deleted, modal: document.getElementById('modal').classList.contains('on'), msg: T(document.querySelector('#pr-body .warnbox'))};
"""

TEMPS_DRIVER = COMMON + r"""
window.__posts = [];
const TEMPS = {PLA: 210, PETG: 235};
const T1 = [{id: 'nozzle', label: 'Nozzle', kind: 'nozzle', c: 215, target_c: 215}, {id: 'bed', label: 'Bed', kind: 'bed', c: 58, target_c: 58}];
const T2 = [{id: 'nozzle', label: 'Nozzle 1', kind: 'nozzle', c: 215, target_c: 215}, {id: 'nozzle2', label: 'Nozzle 2', kind: 'nozzle', c: 24, target_c: 0},
  {id: 'bed', label: 'Bed', kind: 'bed', c: 58, target_c: 58}, {id: 'chamber', label: 'Chamber', kind: 'chamber', c: 31, target_c: null}];
const SENS = [{id: 'temperature_sensor ebb', label: 'Toolhead air', role: 'toolhead', c: 34.7}, {id: 'temperature_sensor pi', label: 'Pi', role: 'host', c: 55.2}];
const DATA = {
  One: fl('One', {kind: 'moonraker', state: 'IDLE', nozzle_c: 215, nozzle_target_c: 0, bed_c: 58, bed_target_c: 0, temps: T1.map(x => Object.assign({}, x, {target_c: 0})), sensors: []}),
  Two: fl('Two', {kind: 'bambu', state: 'IDLE', nozzle_c: 215, bed_c: 58, temps: T2.map(x => Object.assign({}, x)), sensors: []}),
  Old: fl('Old', {kind: 'moonraker', state: 'IDLE', nozzle_c: 123, nozzle_target_c: 200, bed_c: 45, bed_target_c: 60}),                               // an older Hub: no temps, no sensors
  Kli: fl('Kli', {kind: 'moonraker', state: 'IDLE', nozzle_c: 24, nozzle_target_c: 0, bed_c: 23, bed_target_c: 0, temps: [{id: 'nozzle', label: 'Nozzle', kind: 'nozzle', c: 24, target_c: 0},
    {id: 'bed', label: 'Bed', kind: 'bed', c: 23, target_c: 0}, {id: 'chamber', label: 'Chamber', kind: 'chamber', c: 31, target_c: null}], sensors: SENS}),
  Hostile: fl('Hostile', {kind: 'moonraker', state: 'IDLE', temps: [{id: P1, label: P1, kind: 'nozzle', c: 1, target_c: 0}, {id: 'bed', label: P3, kind: 'bed', c: 2, target_c: 0}, {id: P2, label: P3, kind: 'chamber', c: 3, target_c: null}],
    sensors: [{id: P1, label: P2, role: 'other', c: 4}, {id: P3, label: P1, role: 'host', c: 5}]}),
  Ro: fl('Ro', {kind: 'moonraker', state: 'IDLE', temps: [{id: 'nozzle', label: 'Nozzle', kind: 'nozzle', c: 200, target_c: null}, {id: 'bed', label: 'Bed', kind: 'bed', c: 60, target_c: 60}], sensors: []}),
};
const ok = (extra) => Object.assign({supported: true, allowed_now: true}, extra || {});
const detail = () => ({light: {supported: false, reason: 'no light'}, preheat: ok({presets: {PLA: {nozzle: null, bed: 60}, PETG: {nozzle: null, bed: 80}}, auto_off_timeout_min: 15, auto_off_in_s: null}), cooldown: ok(), heater_off: ok(),
  unload: ok(), load: ok(), change_filament: ok(), home: ok(), move: ok(), extrude: ok(), motion: {homed: true, homed_how: 'reported', min_extrude_c: 170, max_move_mm: 10, axes: ['X', 'Y', 'Z']},
  filament: {temps: TEMPS, needs_nozzle_temp: true}});
window.__stubs.push((u, m, init) => {
  const path = u.split('?')[0]; let mm;
  if(path === '/api/fleet') return J(Object.values(DATA));
  if(path === '/api/ui-version') return J({v: '1'});
  if(path === '/api/auth/me') return J({});
  if(path === '/api/detection') return J({enabled: false});
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/files$/))) return J([]);
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/control$/))){ window.__posts.push(Object.assign({printer: decodeURIComponent(mm[1])}, JSON.parse(init.body))); return J({ok: true, message: 'sent'}); }
  if((mm = path.match(/^\/api\/printer\/([^/]+)$/))){
    const p = DATA[decodeURIComponent(mm[1])]; if(!p) return J({error: 'no'}, 404);
    const snap = Object.assign({}, p); if(window.__bare){ delete snap.temps; delete snap.sensors; }              // this route's snapshot without temps: the page borrows them from /api/fleet
    return J({snapshot: snap, trays: [], kind: p.kind, can_connect: false, camera: false, controls: {actions: ['preheat', 'cooldown', 'heater_off', 'change_filament'], allowed_now: ['preheat', 'cooldown', 'heater_off', 'change_filament'], detail: detail()}});
  }
  return null;
});
await sleep(200);
const tiles = () => [...document.querySelectorAll('#p-tiles .t5')].map(e => [e.id, T(e)]);
const cards = () => [...document.querySelectorAll('#mbody .cs-tc')].map(c => ({label: T(c.querySelector('.cs-lbl')), now: T(c.querySelector('.cs-bigt')), tgt: T(c.querySelector('.cs-tgt')), chips: [...c.querySelectorAll('.cs-chip')].map(b => T(b)), ro: c.classList.contains('cs-ro')}));
const sens = () => { const e = document.getElementById('p-sens'); return {text: T(e), hidden: e.hidden}; };
const bt = re => [...document.querySelectorAll('#mbody button')].filter(vis).find(b => re.test(T(b)));
R.sizeFit = {};
for(const name of ['One', 'Two', 'Old', 'Kli', 'Ro']){
  CUR = null; openPrinter(name); await wait(() => T(document.getElementById('p-title')) === name && document.querySelectorAll('#p-tiles .t5').length); await sleep(150);
  R['page' + name] = {tiles: tiles(), sens: sens(), fit: noScroll()};
  csOpen(); await wait(() => document.querySelector('#mbody .cs-tc')); await sleep(100);
  R['ctl' + name] = {cards: cards(), sens: T(document.querySelector('#mbody .cs-sens')), fit: noScroll(), modalFit: (() => { const m = document.getElementById('mbody').getBoundingClientRect(); return m.right <= docW + 1; })()};
  closeModal(); await sleep(50);
}
// text that comes from the Hub is only ever shown as text
CUR = null; openPrinter('Hostile'); await wait(() => T(document.getElementById('p-title')) === 'Hostile' && document.querySelectorAll('#p-tiles .t5').length); await sleep(150);
csOpen(); await wait(() => document.querySelector('#mbody .cs-tc')); await sleep(100);
document.querySelector('[data-act=csFlow]').click(); await sleep(100); R.hostile = {injected: injected(), alerts: window.__alerts.length, cards: document.querySelectorAll('#mbody .cs-wt').length}; closeModal();
// heat chips: one nozzle sends nothing about the nozzle; two nozzles send which one
openPrinter('One'); await wait(() => document.querySelectorAll('#p-tiles .t5').length); csOpen(); await wait(() => document.querySelector('#mbody .cs-tc'));
window.__posts.length = 0; document.querySelector('#mbody .cs-tc .cs-chip[data-k=nozzle]').click(); await sleep(150); R.postOne = window.__posts.slice();
window.__posts.length = 0; document.querySelector('#mbody .cs-tc .cs-chip[data-act=csOff]').click(); await sleep(150); R.offOne = window.__posts.slice();
closeModal();
openPrinter('Two'); await wait(() => document.querySelectorAll('#p-tiles .t5').length > 3); csOpen(); await wait(() => document.querySelector('#mbody .cs-tc'));
window.__posts.length = 0; document.querySelectorAll('#mbody .cs-tc')[1].querySelector('.cs-chip[data-k=nozzle][data-v=PETG]').click(); await sleep(150); R.postTwo = window.__posts.slice();
window.__posts.length = 0; document.querySelectorAll('#mbody .cs-tc')[1].querySelector('.cs-chip[data-act=csOff]').click(); await sleep(150); R.offTwo = window.__posts.slice();
window.__posts.length = 0; document.querySelectorAll('#mbody .cs-tc')[0].querySelector('.cs-chip[data-act=csOff]').click(); await sleep(150); R.offFirst = window.__posts.slice();
R.roChips = [...document.querySelectorAll('#mbody .cs-tc.cs-ro')].map(c => c.querySelectorAll('.cs-chip').length);
// Change filament asks which nozzle first (two nozzles) - and not at all with one
document.querySelector('[data-act=csFlow]').click(); await sleep(100);
R.flowTwo = {text: T(document.querySelector('#mbody .cs-wt h3')), chips: [...document.querySelectorAll('#mbody .cs-chips button')].map(b => T(b)), steps: [...document.querySelectorAll('#mbody .cs-st')].map(e => T(e.querySelector(':scope > span')).replace(/\s*\((done|now)\)$/, ''))};
bt(/Nozzle 2/).click(); await sleep(100); R.flowTwoNext = T(document.querySelector('#mbody .cs-wt h3'));
document.querySelector('#mbody [data-act=csMat][data-m=PLA]').click(); await sleep(250); R.flowPost = window.__posts.filter(p => p.action === 'preheat').slice(-1)[0];
bt(/Back/) && bt(/Back/).click(); await sleep(60);
closeModal(); window.__posts.length = 0;
openPrinter('One'); await wait(() => document.querySelectorAll('#p-tiles .t5').length); csOpen(); await wait(() => document.querySelector('#mbody .cs-tc'));
document.querySelector('[data-act=csFlow]').click(); await sleep(100);
R.flowOne = {text: T(document.querySelector('#mbody .cs-wt h3')), steps: [...document.querySelectorAll('#mbody .cs-st')].map(e => T(e.querySelector(':scope > span')).replace(/\s*\((done|now)\)$/, ''))};
document.querySelector('#mbody [data-act=csMat][data-m=PLA]').click(); await sleep(250); R.flowPostOne = window.__posts.filter(p => p.action === 'preheat').slice(-1)[0];
closeModal();
// the route's snapshot without temps: they are borrowed from the fleet answer the home screen already saw
window.__bare = true; await fleet(); await sleep(100);
openPrinter('Two'); await wait(() => document.querySelectorAll('#p-tiles .t5').length > 3); await sleep(200); R.borrowed = tiles().map(t => t[0]);
"""


class _Browser(unittest.TestCase):
    DRIVER = ""
    cache = None

    @classmethod
    def run_in(cls, tc, size, driver, budget=90000):
        import test_fix_dashboard as tfd
        if not tfd.CHROME:
            tc.skipTest("no Chrome / Edge found for the browser tests")
        return run_q(tc, WIDE + (driver % {"schema": _schema()}), "", SIZES[size], budget)

    def each_size(self):
        cls = type(self)
        if cls.cache is None:
            cls.cache = {}
        for size in SIZES:
            if size not in cls.cache:
                cls.cache[size] = self.run_in(self, size, cls.DRIVER)
            R = cls.cache[size]
            self.assertNotIn("error", R, f"{size}: {R.get('error')}")
            self.assertEqual(R["alerts"], [], size)
            yield size, R


class ListTests(_Browser):
    DRIVER = LIST_DRIVER
    cache = None

    def test_every_printer_is_a_picture_card_with_a_word_and_a_shape_for_its_state(self):
        for s, R in self.each_size():
            self.assertEqual(R["count"], 4, s)
            self.assertTrue(R["addTile"], s)                                    # the dashed "+ Add a printer" tile closes the grid
            a, b, c, d = (R["cards"][n] for n in ("Printer-A", "Printer-B", "Printer-C", "Printer-D"))
            self.assertEqual((a["name"], a["model"]), ("Printer-A", "Model X"), s)
            self.assertEqual(a["state"], "▶ PRINTING · 42%", s)           # a triangle AND the word AND the percent
            self.assertEqual(b["state"], "● IDLE", s)
            self.assertEqual(d["state"], "✕ OFFLINE", s)
            self.assertTrue(d["off"], s)                                        # a dashed frame as well
            self.assertFalse(a["off"], s)
            self.assertEqual(a["group"], "WORK", s)
            self.assertEqual(c["group"], "Home", s)
            self.assertTrue(all(x["dots"] for x in R["cards"].values()), s)

    def test_a_camera_shows_its_picture_and_a_printer_without_one_shows_its_drawing_and_says_so(self):
        for s, R in self.each_size():
            a, c = R["cards"]["Printer-A"], R["cards"]["Printer-C"]
            self.assertTrue(a["hasImg"], s)
            self.assertFalse(a["addCam"], s)
            self.assertFalse(c["hasImg"], s)
            self.assertIn("No camera yet", c["none"], s)
            self.assertTrue(c["addCam"], s)
            self.assertIn("+ Add camera", c["none"], s)
            self.assertRegex(R["noPic"], "Waiting for the camera|Camera not answering", s)      # a camera that has not answered (the stub has no picture): words, not a broken image

    def test_the_check_chips_use_a_tick_or_a_dash_and_words(self):
        for s, R in self.each_size():
            if s == "phone":
                continue                                                        # a phone card keeps only the first chip (mock-up); the words are in the DOM and checked at the other sizes
            ch = {n: dict((t, miss) for t, miss in x["chips"]) for n, x in R["cards"].items()}
            self.assertEqual(ch["Printer-A"], {"✓ Bambu Lab": False, "✓ Camera": False, "✓ Spaghetti watch": False}, s)
            self.assertEqual(ch["Printer-B"]["◔ Spaghetti: learning"], False, s)
            self.assertEqual(ch["Printer-C"], {"✓ Klipper": False, "– No camera": True, "– Spaghetti off": True}, s)     # missing = a dash and a dashed chip
            self.assertIn("✕ OctoPrint · not answering", ch["Printer-D"], s)
            self.assertTrue(ch["Printer-D"]["✕ OctoPrint · not answering"], s)
            self.assertIn("– Spaghetti off", ch["Printer-D"], s)              # excluded from the watch

    def test_one_toolbar_row_and_the_group_filter(self):
        for s, R in self.each_size():
            t = R["toolbar"]
            self.assertEqual(t["title"], "Printers", s)
            self.assertEqual([x[0] for x in t["seg"]], ["", "Home", "WORK"], s)
            self.assertEqual([x[1] for x in t["seg"]], ["✓ All4", "Home2", "WORK2"], s)              # counts, and a tick on the chosen one
            self.assertIn("✓ Add printer".replace("✓", "＋"), t["actions"], s)
            if s != "phone":
                self.assertIn("\U0001F50D Find printers", t["actions"], s)
                self.assertIn("Close", t["actions"], s)
                self.assertTrue(t["oneRow"], s)                                 # title, filter and buttons share ONE row
            self.assertEqual(R["filterHome"]["names"], ["Printer-C", "Printer-D"], s)
            self.assertEqual(R["filterHome"]["pressed"], ["false", "true", "false"], s)
            self.assertTrue(R["filterHome"]["tick"].startswith("✓ Home"), s)
            self.assertEqual(R["filterAll"], 4, s)

    def test_the_five_second_refresh_leaves_the_cards_and_their_pictures_alone(self):
        for s, R in self.each_size():
            self.assertEqual(R["stable"], {"sameCard": True, "sameImg": True}, s)
            self.assertEqual(R["changed"], "▶ PRINTING · 50%", s)      # ... but a real change is drawn

    def test_menus_taps_and_add_camera(self):
        for s, R in self.each_size():
            self.assertTrue(R["menu"]["open"], s)
            self.assertEqual(R["menu"]["items"], ["Edit", "Remove…"], s)
            self.assertTrue(R["menu"]["inView"], s)
            self.assertEqual((R["edit"]["open"], R["edit"]["title"], R["edit"]["modal"], R["edit"]["menuGone"]), (True, "Printer-C", True, True), s)
            self.assertEqual(R["afterClose"], {"view": "list", "sheet": False}, s)
            self.assertEqual(R["tap"], "Printer-B", s)                           # a tap on the card is Edit
            self.assertEqual(R["gone"], [True], s)                               # closing the pop-up leaves nothing behind (a typed secret, for one)
            self.assertEqual((R["addCam"]["title"], R["addCam"]["focus"]), ("Printer-C", "pm-f-camera_url"), s)
            self.assertIn("Manage groups", R["topMenu"], s)
            self.assertEqual(R["groupsSection"], 2, s)                           # the existing groups section, with its Rename / Delete buttons

    def test_no_sideways_scroll(self):
        for s, R in self.each_size():
            self.assertTrue(R["fit"]["noScroll"], (s, R["fit"]))


class AddTests(_Browser):
    DRIVER = ADD_DRIVER
    cache = None

    def test_the_kind_picker_is_built_from_the_hubs_printer_types(self):
        fams = [f.key for f in fleet_store._make_families().values()]
        for s, R in self.each_size():
            k = R["kinds"]
            self.assertEqual(k["types"], fams + ["foo"], s)                      # whatever /api/printer-types sends, in its order: nothing is hard-coded
            self.assertEqual(k["labels"][-1], "Foo Printer", s)
            self.assertTrue(all(k["arts"]), s)                                   # every tile has a drawing, an unknown kind a generic one
            self.assertNotEqual(R["kindArt"]["bambu"], R["kindArt"]["foo"], s)
            self.assertEqual(dict(zip(k["types"], k["have"])), {"bambu": "YOU HAVE 1", "octoprint": None, "prusalink": "YOU HAVE 2", "moonraker": None, "foo": None}, s)
            self.assertIn("Find it for me", k["find"], s)
            self.assertEqual(k["go"], "Scan my network", s)
            self.assertTrue(k["radar"], s)
            self.assertEqual(k["title"], "Add a printer", s)
            self.assertTrue(k["behind"], s)                                      # the list stays behind the pop-up
            self.assertTrue(k["fit"]["noScroll"], s)
            self.assertLessEqual(k["fit"]["body"], 1, s)
            self.assertEqual([x[0] for x in k["steps"]], ["Kind", "Connect", "Camera", "Check"], s)
            self.assertEqual([x[1] for x in k["steps"]], ["now", "todo", "todo", "todo"], s)

    def test_the_form_opens_in_the_same_popup_and_the_step_bar_advances(self):
        for s, R in self.each_size():
            f = R["form"]
            self.assertTrue(f["sameBox"], s)
            self.assertEqual(f["sheets"], 1, s)                                  # one pop-up page, not a series
            self.assertEqual(f["title"], "Add a PrusaLink printer", s)
            self.assertEqual([x[1] for x in f["steps"]], ["done", "now", "todo", "todo"], s)
            self.assertEqual(f["fields"][:4], ["name", "url", "password", "camera_url"], s)
            self.assertTrue(f["camera"], s)
            self.assertEqual([x[1] for x in R["cameraStep"]], ["done", "done", "now", "todo"], s)       # the camera box takes focus: step 3
            self.assertEqual([x[1] for x in R["connectStep"]], ["done", "now", "todo", "todo"], s)
            self.assertEqual([x[1] for x in R["tested"]["steps"]], ["done", "done", "done", "now"], s)  # a passing test: Check
            self.assertEqual(R["tested"]["save"], ["Save printer", False], s)
            self.assertTrue(f["noScroll"], s)
            self.assertEqual(R["post"]["type"], "prusalink", s)
            self.assertEqual(R["post"]["values"]["name"], "Printer-N", s)
            self.assertEqual(R["after"]["modal"], False, s)
            self.assertEqual(R["after"]["view"], "list", s)

    def test_back_the_backdrop_and_find_it_for_me(self):
        for s, R in self.each_size():
            self.assertEqual(R["bambuForm"]["title"], "Add a Bambu Lab printer", s)
            self.assertIn("serial", R["bambuForm"]["fields"], s)
            self.assertEqual(R["back"]["kinds"], 5, s)
            self.assertEqual([x[1] for x in R["back"]["steps"]], ["now", "todo", "todo", "todo"], s)
            self.assertFalse(R["backdropPick"], s)                               # the kind picker closes on a tap outside ...
            self.assertTrue(R["backdropForm"], s)                                # ... a form with typing in it does not
            self.assertEqual(R["fooForm"][:2], ["name", "url"], s)
            self.assertEqual(R["closed"], {"modal": False, "view": "list"}, s)
            self.assertEqual((R["find"]["modal"], R["find"]["printers"]), (True, True), s)
            self.assertEqual(R["find"]["title"], "Find printers", s)             # the existing discovery
            self.assertTrue(R["find"]["scan"], s)


class EditTests(_Browser):
    DRIVER = EDIT_DRIVER
    cache = None

    def test_edit_keeps_every_field_and_every_rule(self):
        fam = next(f for f in fleet_store._make_families().values() if f.key == "prusalink").describe()
        want = [f["name"] for f in fam["fields"]]
        for s, R in self.each_size():
            ids = R["fields"]["ids"]
            for n in want:
                self.assertIn(n, ids, (s, n))
            self.assertIn("jog_flip_y", ids, s)
            self.assertEqual(R["fields"]["types"], {"url": "text", "password": "password", "camera_url": "text"}, s)         # a secret stays masked
            self.assertTrue(R["fields"]["values"]["readonlyName"], s)
            self.assertEqual(R["fields"]["values"]["camera"], "rtsp://***@192.0.2.60/live", s)                              # the camera login stays hidden
            self.assertEqual(R["secret"][:3], ["password", "text", "password"], s)                                          # Show / Hide
            self.assertIn("Saved", R["secret"][3], s)
            self.assertEqual(R["cards"]["save"], {"label": "No changes", "disabled": True}, s)
            self.assertEqual(R["cards"]["footer"][0], "Remove printer…", s)
            self.assertIn("Cancel", R["cards"]["footer"], s)
            self.assertEqual(R["cards"]["footer"][-1], "No changes", s)

    def test_the_pop_up_is_the_mockups_cards(self):
        for s, R in self.each_size():
            h, c = R["header"], R["cards"]
            self.assertEqual(h["title"], "Printer-B", s)
            self.assertEqual(h["sub"], "Model A · PrusaLink · ● IDLE", s)
            self.assertTrue(h["thumb"] and h["listBehind"], s)
            self.assertTrue(c["camera"] and c["watch"] and c["areaEditor"], s)                                              # the watch area is drawn on the picture; Change area is the existing editor
            self.assertEqual(c["watchStyle"], ["10%", "10%", "50%", "50%"], s)
            self.assertEqual(c["chips"], ["Tapo / Reolink", "Prusa Buddy3D", "JPEG URL"], s)
            self.assertTrue(c["own"], s)
            self.assertTrue(c["conn"], s)
            self.assertIn("Connected now", c["now"], s)
            self.assertEqual(c["groups"], ["WORK"], s)
            self.assertEqual(c["jog"], [["Top = backMost printers", "true"], ["Top = frontIf it feels backwards", "false"]], s)
            self.assertFalse(c["more"]["hidden"], s)                          # Username / Storage hold values, so More opens by itself
            self.assertTrue(c["tip"]["btn"] and c["tip"]["hidden"], s)
            self.assertTrue(R["tipOpen"], s)
            self.assertTrue(R["fit"]["noScroll"], s)
            self.assertLessEqual(R["fit"]["body"], 1, s)
            self.assertLessEqual(R["fit"]["box"][1], R["fit"]["vw"] + 1, s)
            self.assertGreaterEqual(R["fit"]["box"][0], 0, s)

    def test_jog_ring_is_two_pictures_over_the_same_select(self):
        for s, R in self.each_size():
            self.assertEqual(R["jog"]["value"], "1", s)
            self.assertEqual(R["jog"]["pressed"], ["false", "true"], s)
            self.assertEqual(R["jog"]["save"], {"label": "Save changes", "disabled": False}, s)
            self.assertEqual(R["jogBack"], {"label": "No changes", "disabled": True}, s)

    def test_what_this_printer_has_in_words_with_shapes(self):
        for s, R in self.each_size():
            rows = R["profile"]["rows"]
            self.assertEqual(list(rows), ["Light", "Nozzles", "Bed", "Chamber", "Filament", "Camera"], s)
            self.assertEqual(rows["Light"]["val"], "Yes", s)
            self.assertTrue(rows["Light"]["cant"], s)                                                                      # a dashed frame ...
            self.assertIn("✕ This printer has a light, but PrusaLink can't switch it", rows["Light"]["how"], s)       # ... the Controls reason, with a cross
            self.assertEqual(rows["Nozzles"]["val"], "1 nozzle · 290 °C max", s)
            self.assertEqual(rows["Bed"]["val"], "250 × 220 mm · 120 °C max", s)
            self.assertEqual(rows["Chamber"]["val"], "Temperature sensor · no heater", s)
            self.assertIn("reads only", rows["Chamber"]["how"], s)
            self.assertEqual(rows["Filament"]["val"], "Single spool", s)
            self.assertIn("– No AMS / MMU", rows["Filament"]["how"], s)
            self.assertIn("Uses the camera you added", rows["Camera"]["how"], s)
            self.assertTrue(all(not r["mine"] for r in rows.values()), s)
            self.assertEqual(R["profile"]["picker"][0], ["", "Automatic · Model A", True], s)
            self.assertEqual([o[1] for o in R["profile"]["picker"][1:]], ["Any PrusaLink printer", "Model A", "Model B"], s)       # only the profiles that fit this kind of printer
            self.assertIn("this printer only", R["profile"]["note"], s)
            self.assertFalse(R["profile"]["hasSens"], s)

    def test_a_changed_row_is_marked_this_printer_only_and_saved_as_an_override(self):
        for s, R in self.each_size():
            self.assertTrue(R["lightRow"]["row"]["mine"], s)                                                                # the diamond + words (checked in the html below)
            self.assertEqual(R["lightRow"]["row"]["val"].replace(" ◆ this printer only", ""), "No", s)
            self.assertEqual(R["lightRow"]["save"], {"label": "Save changes", "disabled": False}, s)
            self.assertEqual(R["nozRow"]["val"].split(" ◆")[0], "2 nozzles · 250 °C max", s)
            self.assertEqual(R["put1"]["profile_overrides"], {"light": False, "nozzles": 2, "nozzle_max_c": 250}, s)
            self.assertNotIn("profile_id", R["put1"], s)                                                                    # the profile itself was not touched
            self.assertEqual(R["put1"]["values"]["name"], "Printer-B", s)
            self.assertEqual(R["afterSave"]["modal"], False, s)
            self.assertIn("updated", R["afterSave"]["msg"], s)

    def test_back_to_the_profile_resets_a_row_and_clears_the_override(self):
        for s, R in self.each_size():
            self.assertTrue(R["again"]["rows"]["Light"]["mine"] and R["again"]["rows"]["Nozzles"]["mine"], s)               # what was saved is still marked
            self.assertEqual(R["again"]["save"], {"label": "No changes", "disabled": True}, s)
            self.assertFalse(R["reset"]["rows"]["Light"]["mine"], s)
            self.assertEqual(R["reset"]["rows"]["Light"]["val"], "Yes", s)
            self.assertEqual(R["put2"]["profile_overrides"], {}, s)
            self.assertNotIn("profile_id", R["put2"], s)

    def test_the_picker_sets_the_profile_and_automatic_sets_it_back_to_null(self):
        for s, R in self.each_size():
            self.assertEqual(R["pick"]["rows"]["Nozzles"]["val"], "2 nozzles · 300 °C max", s)                    # the rows follow the chosen profile
            self.assertEqual(R["put3"]["profile_id"], "model-b", s)
            self.assertNotIn("profile_overrides", R["put3"], s)
            self.assertEqual(R["explicit"]["sel"], "model-b", s)
            self.assertEqual(R["put4"]["profile_id"], None, s)

    def test_klipper_sensors_get_a_label_and_a_this_is_the_chamber_choice(self):
        for s, R in self.each_size():
            rows = R["sens"]["rows"]
            self.assertEqual([r[0] for r in rows], ["Hotend", "Pi", "temperature_sensor old"], s)                           # a saved label; a sensor that is already the chamber is still listed
            self.assertEqual([r[1] for r in rows][:2], ["toolhead", "host computer"], s)
            self.assertEqual([r[3] for r in rows], ["false", "false", "true"], s)
            self.assertIn("This is the chamber", rows[2][2], s)
            self.assertEqual(R["sens2"]["pressed"], ["true", "false"], s)                        # the old chamber sensor is not listed any more: another one took over
            self.assertEqual(R["put5"]["profile_overrides"], {"chamber_sensor": "temperature_sensor ebb", "sensor_labels": {"temperature_sensor ebb": "Hotend", "temperature_sensor pi": "Raspberry Pi"}}, s)

    def test_names_from_the_hub_are_text_never_markup(self):
        for s, R in self.each_size():
            self.assertEqual((R["hostile"]["injected"], R["hostile"]["alerts"]), (0, 0), s)
            self.assertEqual(R["hostile"]["sens"], 2, s)                                                                      # both sensors listed (one is the chamber)
            self.assertEqual(R["hostile"]["title"], "Printer-X", s)

    def test_busy_and_remove_questions_stay_inside_the_popup(self):
        for s, R in self.each_size():
            self.assertEqual(R["busy"]["title"], "Printer-B is busy", s)
            self.assertIn("printing right now", R["busy"]["text"], s)
            self.assertEqual(R["busy"]["buttons"], ["Cancel", "Change it anyway"], s)
            self.assertEqual(R["busy"]["one"], 1, s)
            self.assertEqual((R["forced"]["first"], R["forced"]["second"], R["forced"]["flip"], R["forced"]["modal"]), (False, True, True, False), s)    # what was typed is kept and sent again, forced
            self.assertEqual(R["remove"]["title"], "Remove Printer-B?", s)
            self.assertEqual(R["remove"]["buttons"], ["Keep Printer-B", "Remove Printer-B"], s)
            self.assertEqual(R["kept"], {"modal": False, "deleted": []}, s)
            self.assertEqual(R["removed"]["deleted"], ["/api/printers/Printer-B"], s)
            self.assertFalse(R["removed"]["modal"], s)
            self.assertEqual(R["removed"]["msg"], "Printer-B removed.", s)

    def test_an_older_hub_has_no_card_and_no_picker(self):
        for s, R in self.each_size():
            self.assertTrue(R["plain"]["cardHidden"], s)
            self.assertEqual(R["plain"]["cardDisplay"], "none", s)
            self.assertEqual(R["plain"]["save"], {"label": "No changes", "disabled": True}, s)
            self.assertFalse(R["noList"]["picker"], s)
            self.assertEqual(R["noList"]["change"], 0, s)                                                                   # no model numbers to go back to: read-only rows
            self.assertEqual(len(R["noList"]["rows"]), 6, s)


class TempsTests(_Browser):
    DRIVER = TEMPS_DRIVER
    cache = None

    def test_one_nozzle_and_a_bed_make_two_tiles_and_two_cards(self):
        for s, R in self.each_size():
            self.assertEqual([t[0] for t in R["pageOne"]["tiles"]], ["t-noz", "t-bed"], s)
            self.assertIn("215°", R["pageOne"]["tiles"][0][1], s)
            self.assertIn("/ off", R["pageOne"]["tiles"][0][1], s)
            self.assertEqual(len(R["ctlOne"]["cards"]), 2, s)
            self.assertIn("Nozzle", R["ctlOne"]["cards"][0]["label"], s)
            self.assertEqual(R["ctlOne"]["cards"][0]["chips"], ["PLA 210°", "PETG 235°", "✓ Off"], s)      # the heater is off: the Off choice has a tick

    def test_two_nozzles_a_bed_and_a_chamber_are_four_cards_and_the_chamber_only_reads(self):
        for s, R in self.each_size():
            t = R["pageTwo"]["tiles"]
            self.assertEqual([x[0] for x in t], ["t-noz", "t-x-nozzle2", "t-bed", "t-x-chamber"], s)
            self.assertIn("NOZZLE 1", t[0][1].upper(), s)
            self.assertIn("reads only", t[3][1], s)
            self.assertIn("/ off", t[1][1], s)
            c = R["ctlTwo"]["cards"]
            self.assertEqual(len(c), 4, s)
            self.assertEqual([("Nozzle 1" in x["label"], "Nozzle 2" in x["label"], "Bed" in x["label"], "Chamber" in x["label"]) for x in c],
                             [(True, False, False, False), (False, True, False, False), (False, False, True, False), (False, False, False, True)], s)
            self.assertEqual(c[3]["tgt"], "reads only", s)
            self.assertEqual(c[3]["chips"], [], s)                                # no set-temperature chips on a reading
            self.assertTrue(c[3]["ro"], s)
            self.assertEqual(c[1]["tgt"], "heater off", s)
            self.assertEqual(R["roChips"], [0], s)
            self.assertTrue(R["ctlTwo"]["fit"] and R["ctlTwo"]["modalFit"], s)
            self.assertTrue(R["pageTwo"]["fit"], s)

    def test_a_reading_without_a_target_has_no_choices_even_on_a_nozzle(self):
        for s, R in self.each_size():
            c = R["ctlRo"]["cards"]
            self.assertEqual(c[0]["tgt"], "reads only", s)
            self.assertEqual(c[0]["chips"], [], s)
            self.assertIn("reads only", R["pageRo"]["tiles"][0][1], s)
            self.assertNotIn("reads only", R["pageRo"]["tiles"][1][1], s)

    def test_without_temps_the_old_numbers_are_used(self):
        for s, R in self.each_size():
            self.assertEqual([t[0] for t in R["pageOld"]["tiles"]], ["t-noz", "t-bed"], s)
            self.assertIn("123°", R["pageOld"]["tiles"][0][1], s)
            self.assertIn("/ 200", R["pageOld"]["tiles"][0][1], s)
            self.assertIn("45°", R["pageOld"]["tiles"][1][1], s)
            self.assertEqual(len(R["ctlOld"]["cards"]), 2, s)
            self.assertEqual(R["ctlOld"]["cards"][0]["tgt"], "→ 200°", s)
            self.assertEqual(R["pageOld"]["sens"]["hidden"], True, s)

    def test_sensors_are_one_compact_line(self):
        for s, R in self.each_size():
            self.assertFalse(R["pageKli"]["sens"]["hidden"], s)
            self.assertEqual(R["pageKli"]["sens"]["text"], "Sensors Toolhead air 35° · Pi 55°", s)
            self.assertEqual(R["ctlKli"]["sens"], "Sensors: Toolhead air 35° · Pi 55°", s)
            self.assertTrue(R["pageOne"]["sens"]["hidden"], s)
            self.assertTrue(R["pageKli"]["fit"], s)

    def test_heat_chips_and_change_filament_ask_which_nozzle_only_when_there_are_two(self):
        for s, R in self.each_size():
            self.assertEqual(R["postOne"], [{"printer": "One", "action": "preheat", "material": "PLA"}], s)                   # one nozzle: the old body, nothing new
            self.assertEqual(R["offOne"], [{"printer": "One", "action": "heater_off", "heater": "nozzle"}], s)
            self.assertEqual(R["postTwo"], [{"printer": "Two", "action": "preheat", "material": "PETG", "nozzle": "nozzle2"}], s)
            self.assertEqual(R["offTwo"], [{"printer": "Two", "action": "heater_off", "heater": "nozzle2"}], s)
            self.assertEqual(R["offFirst"], [{"printer": "Two", "action": "heater_off", "heater": "nozzle"}], s)
            self.assertEqual(R["flowTwo"]["text"], "Which nozzle?", s)
            self.assertIn("Nozzle 2", " ".join(R["flowTwo"]["chips"]), s)
            self.assertEqual(R["flowTwo"]["steps"][0], "Nozzle", s)
            self.assertEqual(R["flowTwoNext"], "Pick the new material", s)
            self.assertEqual(R["flowPost"], {"printer": "Two", "action": "preheat", "material": "PLA", "nozzle": "nozzle2"}, s)
            self.assertEqual(R["flowOne"]["text"], "Pick the new material", s)                                              # one nozzle: no question
            self.assertNotIn("Nozzle", R["flowOne"]["steps"], s)
            self.assertEqual(R["flowPostOne"], {"printer": "One", "action": "preheat", "material": "PLA"}, s)

    def test_a_snapshot_without_temps_borrows_them_from_the_fleet_answer(self):
        for s, R in self.each_size():
            self.assertEqual(R["borrowed"], ["t-noz", "t-x-nozzle2", "t-bed", "t-x-chamber"], s)

    def test_names_from_the_hub_are_text_never_markup(self):
        for s, R in self.each_size():
            self.assertEqual(R["hostile"]["injected"], 0, s)
            self.assertEqual(R["hostile"]["alerts"], 0, s)
            self.assertEqual(R["hostile"]["cards"], 1, s)                                                                     # the Change filament page (this printer has one nozzle, so it opens at the material question) was drawn too


class StaticTests(unittest.TestCase):
    def test_the_words_and_shapes_are_in_the_page(self):
        for needle in ("this printer only", "◆ this printer only", "Which nozzle?", "reads only", "Find it for me", "Scan my network", "YOU HAVE ", "What this printer has",
                       "This is the chamber", "profile_overrides", "profile_id", "/api/profiles", "const PM_ACTIONS", "...PM_ACTIONS", "function tpList("):
            self.assertIn(needle, HTML, needle)

    def test_no_hard_coded_printer_type_list_in_the_kind_picker(self):
        i = HTML.index("function pmKindsHtml(")
        fn = HTML[i:HTML.index("function pmFindFromSheet", i)]
        self.assertIn("PM.types.map(", fn)
        for t in ("bambu", "prusalink", "moonraker", "octoprint"):
            self.assertNotIn("'" + t + "'", fn, t)

    def test_nothing_in_the_new_screens_carries_meaning_by_colour_alone(self):
        i = HTML.index("/* ==== printers v2 ====")
        css = HTML[i:HTML.index("</style>", i)]
        self.assertNotRegex(css, r"#(0f0|00ff00|[0-9a-f]{2}ff[0-9a-f]{2})\b", "no green")
        self.assertIn("border-style:dashed", css)
        self.assertIn("aria-pressed=\"true\"", css)


if __name__ == "__main__":
    unittest.main(verbosity=1)
