"""Shared fixtures for the start-print confirm dialog (the spool picker): the headless-Chrome driver, realistic AMS jobs, the
measurements the layout tests take, and a screenshot helper (tools/ and the layout tests both use it). Names are generic.

    python3 tests/confirm_fixtures.py OUTDIR      # writes one PNG per scenario and size into OUTDIR

The page is the real app/dashboard.html in an iframe of EXACTLY the screen size, with a stubbed `fetch`; nothing contacts a printer.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_fix_dashboard as tfd  # noqa: E402
from test_layout_5in import WRAP  # noqa: E402

# a wide fallback font, like the Linux kiosk's Chromium has: nothing that measures text may rely on the narrow macOS system font
WIDE = ("\ndocument.head.insertAdjacentHTML('beforeend', '<style>*{font-family:Verdana,\"DejaVu Sans\",sans-serif !important}</style>');"
        " await sleep(50);\n")

# the screens the dialog has to fit: (name, width, height, query string)
SIZES = {
    "kiosk10": (1024, 600, "?group=Work&kiosk=1"),        # the 10-inch office kiosk (and a desktop window of that size)
    "phone": (390, 844, ""),
    "five": (800, 480, "?layout=5in"),                  # the 5-inch wall screen
    "land": (800, 480, ""),                              # a phone held sideways / any 800x480 window without the 5in layout
    "desktop": (1280, 800, ""),
}

SCENARIOS = ("single", "two", "two-worst", "single-warn", "model-bad", "long-name", "no-ams")

DRIVER = r"""
try{ localStorage.setItem('fabrica.filesview', 'list'); }catch(e){}       // the dialog is opened from a file ROW: kiosks would otherwise open the Grid (tests/test_files_grid.py covers that)
const SCN = __SCENARIO__;
const PN = SCN === 'long-name' ? 'Packaging Line Printer 12 East Wing' : 'Printer-A';
const trays = [{tray: 0, slot_1based: 1, type: 'PLA', color_name: 'BLACK'}, {tray: 1, slot_1based: 2, type: 'PETG', color_name: 'GREEN'},
               {tray: 2, slot_1based: 3, type: 'PLA', color_name: 'WHITE'}, {tray: 3, slot_1based: 4, type: 'PETG', color_name: 'ORANGE'}];
const NAME = SCN === 'model-bad' ? 'Wall bracket for the workshop shelf with the long name v12 final.gcode.3mf' : 'Hook.gcode.3mf';
const NOAMS = SCN === 'no-ams';
const snap = {snapshot: {printer: PN, connected: true, state: 'IDLE', job: '', percent: 0, layer: 0, nozzle_c: 25, bed_c: 24, nozzle_target_c: 0, bed_target_c: 0, camera: !NOAMS},
  trays: NOAMS ? [] : trays, controls: {actions: []}, kind: NOAMS ? 'octoprint' : 'bambu', camera: !NOAMS, materials: ['PLA', 'PETG'], palette: ['GREEN', 'BLACK']};
const files = [{name: NAME, print_time: '2h 5m', weight_g: 44, filaments: [{color_name: 'GREEN', type: 'PETG'}, {color_name: 'BLACK', type: 'PLA'}]}];
const same = 'Same spools as your last print of this file (Oct 13)';
const base = {print_time: '2h 5m', weight_g: 44, notes: [same], blocked: null, model_match: true, target_model_name: 'Bambu A1', printer_kind: 'bambu', confirm_string: 'ph-TICKET', file: NAME};
const two = {...base, ams_mapping: [1, 0], mapping_described: 'filament 1 PETG -> slot 2\nfilament 2 PLA -> slot 1',
  filaments: [{index: 0, id: 1, type: 'PETG', color_name: 'GREEN', used_g: 38.4, role: 'main', tray: 1}, {index: 1, id: 2, type: 'PLA', color_name: 'BLACK', used_g: 5.6, tray: 0}]};
const one = {...base, ams_mapping: [1], mapping_described: 'filament 1 PETG -> slot 2', weight_g: 38, print_time: '1h 12m',
  filaments: [{index: 0, id: 1, type: 'PETG', color_name: 'GREEN', used_g: 38, tray: 1}]};
let SUG = SCN === 'single' || SCN === 'single-warn' ? one : two;
if(SCN === 'single-warn') SUG = {...one, notes: [same, '\u26a0 file wants GREEN but convention says WHITE for this part name']};
if(SCN === 'two-worst') SUG = {...two, notes: [same, '⚠ filament 2 is sliced for PLA but slot 2 holds PETG (your choice last time)']};
if(SCN === 'model-bad') SUG = {...two, model_match: false, notes: [same], model_warning: 'THIS FILE WAS SLICED FOR P1S — YOU ARE ON A1.', target_model_name: 'Bambu A1'};
if(NOAMS) SUG = {...base, printer_kind: 'octoprint', ams_mapping: [], mapping_described: 'the loaded filament (no AMS on this printer)', filaments: undefined,
  notes: ['This printer prints with whatever filament is physically loaded \u2014 check it.'], target_model_name: 'Prusa MK4'};
window.__starts = [];
window.__stubs.push((u, m, init) => {
  if(u === '/api/fleet') return J([{printer: PN, groups: ['Work'], connected: true, state: 'IDLE', percent: 0, remaining_min: 0, job: '', nozzle_c: 25, bed_c: 24, camera: true}]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: []});
  if(/^\/api\/printer\/[^/]+\/files/.test(u)) return J(files);
  if(/\/suggest$/.test(u)) return J(SUG);
  if(/\/start$/.test(u) && m === 'POST'){ window.__starts.push(JSON.parse(init.body)); return J({state_after_8s: 'PREPARE', job: 'Hook'}); }
  if(/^\/api\/printer\/[^/]+$/.test(u)) return J(snap);
  return null;
});
__WIDE__
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
const r = e => { if(!e) return null; const b = e.getBoundingClientRect(); return {t: Math.round(b.top), b: Math.round(b.bottom), l: Math.round(b.left), r: Math.round(b.right), w: Math.round(b.width), h: Math.round(b.height)}; };
const inside = b => !!b && b.t >= 0 && b.l >= 0 && b.b <= innerHeight && b.r <= innerWidth;
const vis = e => { const b = r(e); if(!b) return false; const mb = r(document.getElementById('mbody')); return inside(b) && b.t >= mb.t - 1 && b.b <= mb.b + 1; };
const MEAS = () => {
  const mb = document.getElementById('mbody'), dlg = document.querySelector('.cfdlg');
  const st = document.getElementById('startbtn'), pl = document.getElementById('plateok'), mo = document.getElementById('modelok');
  const btns = [...document.querySelectorAll('#mbody button')].filter(b => getComputedStyle(b).display !== 'none').map(b => ({t: b.textContent.trim().replace(/\s+/g, ' ').slice(0, 28), ...r(b)}));
  const lab = pl ? pl.closest('.toggle') : null;
  const clipped = [...document.querySelectorAll('#mbody .cffile, #mbody .mshead, #mbody .msgrid button, #mbody label, #mbody .cfactions button')]
    .filter(e => e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).textOverflow !== 'ellipsis').map(e => e.className || e.tagName);
  return {vw: innerWidth, vh: innerHeight, docW: document.documentElement.scrollWidth, docH: document.documentElement.scrollHeight,
    bodyScrollH: document.body.scrollHeight, cscroll: (cs => cs ? [cs.scrollHeight, cs.clientHeight] : null)(document.querySelector('.cfscroll')), mbox: r(mb), mscroll: [mb.scrollHeight, mb.clientHeight], dlg: r(dlg),
    start: r(st), toggle: r(lab), modelToggle: mo ? r(mo.closest('.toggle')) : null, startVis: vis(st), plate: r(pl), plateVis: vis(pl), labelVis: vis(lab), modelVis: mo ? vis(mo) : null,
    back: r(document.querySelector('.cfactions button:not(#startbtn)')), backVis: vis(document.querySelector('.cfactions button:not(#startbtn)')),
    buttons: btns, minBtnH: Math.min(...btns.map(b => b.h)), clipped, hscroll: (cs => cs ? cs.scrollWidth > cs.clientWidth + 1 : null)(document.querySelector('.cfscroll')),
    rows: [...document.querySelectorAll('#msrows .msrow')].map(e => r(e)), spoolgrid: r(document.getElementById('spoolgrid')),
    foot: r(document.querySelector('.cfactions')), head: r(document.querySelector('.cfhead')),
    cam: r(document.querySelector('.cfcam, .cfnocam')),
    text: mb.innerText.replace(/\s+/g, ' ').trim(), warn: [...document.querySelectorAll('#mbody .mswarn, #mbody .cfmis, #mbody .warnbox')].map(e => e.textContent.replace(/\s+/g, ' ').trim()),
    fam: (document.querySelector('.cfsub') || {textContent: ''}).textContent};
};
const tap = (idx, tray) => { document.querySelector('#msrows [data-idx="' + idx + '"][data-tray="' + tray + '"]').click(); };
const fakePic = () => { const im = document.getElementById('cfimg'); if(!im) return; CF.shown = true; document.getElementById('cfnote').style.display = 'none';
  im.src = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#2b3340"/><rect x="40" y="110" width="240" height="50" fill="#46515f"/><rect x="120" y="60" width="80" height="60" fill="#3f8f5f"/></svg>'); im.classList.add('has'); };

await wait(() => document.querySelector('.pcard'));
document.querySelector('.pcard').click();
await wait(() => document.querySelector('.frow'));
[...document.querySelectorAll('.frow')][0].querySelector('button.primary').click();
await wait(() => document.getElementById('startbtn'));
fakePic();
await sleep(100);
R.open = MEAS();
if(SCN === 'two-worst'){ tap(1, 1); await sleep(80); fakePic(); }       // swap: both filaments now sit on the other material -> two warnings + the swap line
R.pre = MEAS();
// @@SHOT
R.disabledBefore = document.getElementById('startbtn').disabled;
document.getElementById('plateok').click();
if(document.getElementById('modelok')) document.getElementById('modelok').click();
await sleep(60);
R.armed = {disabled: document.getElementById('startbtn').disabled, text: document.getElementById('startbtn').textContent};
R.after = MEAS();
// the sheet scrolled to the very top and to the very bottom (a phone scrolls inside the dialog): Start and the tick must be on screen both times
const mb = document.getElementById('mbody');
mb.scrollTop = 0; await sleep(40); R.top = MEAS(); mb.scrollTop = 99999; await sleep(40); R.bottom = MEAS(); mb.scrollTop = 0; await sleep(40);
R.title = (document.querySelector('.cffile') || {}).title || '';
// the long file name: one line with an ellipsis, the whole name on tap
const fn = document.querySelector('.cffile .cfname');
R.fname = {cut: fn.scrollWidth > fn.clientWidth + 1, text: fn.textContent, h: r(fn).h};
document.querySelector('.cffile').click(); await sleep(60);
R.fnameOpen = {open: document.querySelector('.cffile').classList.contains('open'), aria: document.querySelector('.cffile').getAttribute('aria-expanded'), cut: fn.scrollWidth > fn.clientWidth + 1, ...(() => { const m = MEAS(); return {startVis: m.startVis, plateVis: m.plateVis, cs: m.cscroll}; })()};
document.querySelector('.cffile').click(); await sleep(40);
// a single-filament job: tapping a spool marks it with a tick (not only a colour)
if(document.getElementById('spoolgrid')){
  document.querySelector('#spoolgrid [data-tray="2"]').click(); await sleep(40);
  R.picked = [...document.querySelectorAll('#spoolgrid button')].map(b => ({tray: b.dataset.tray, on: b.getAttribute('aria-pressed'), text: b.textContent.replace(/\s+/g, ' ').trim()}));
}
// START sends the picked spools with the single-use ticket
document.getElementById('startbtn').click(); await sleep(300);
R.started = window.__starts.slice();
"""


def build_driver(scenario, wide=False):
    return DRIVER.replace("__SCENARIO__", json.dumps(scenario)).replace("__WIDE__", WIDE if wide else "")


def _page(testcase, driver):
    d = tempfile.mkdtemp(prefix="confirm-chrome-")
    if testcase is not None:
        testcase.addCleanup(shutil.rmtree, d, True)
    page = os.path.join(d, "page.html")
    with open(page, "w", encoding="utf8") as fh:
        fh.write(tfd.build_page(driver))
    return d, page


def _wrap(d, page, size, query):
    wrap = os.path.join(d, "wrap.html")
    with open(wrap, "w", encoding="utf8") as fh:
        fh.write(WRAP % {"src": "file://" + page + query, "w": size[0], "h": size[1]})
    return wrap


def _chrome(d, wrap, size, extra, budget=90000):
    return ["" + tfd.CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--allow-file-access-from-files", "--force-device-scale-factor=1", "--user-data-dir=" + os.path.join(d, "profile"),
            "--window-size=%d,%d" % (max(size[0], 1024), size[1] + 200), f"--virtual-time-budget={budget}"] + extra + ["file://" + wrap]


def measure(testcase, scenario, key, wide=False):
    """Open the dialog for `scenario` at the screen `key`; returns the measurements dict R."""
    import test_layout_5in
    w, h, q = SIZES[key]
    return test_layout_5in.run_q(testcase, build_driver(scenario, wide), q, (w, h), 90000)


def screenshot(scenario, key, out_png, wide=False):
    """PNG of exactly the screen size with the dialog open (and unarmed: how it first appears)."""
    from PIL import Image
    w, h, q = SIZES[key]
    drv = build_driver(scenario, wide).split("// @@SHOT")[0]
    d, page = _page(None, drv)
    try:
        wrap = _wrap(d, page, (w, h), q)
        shot = os.path.join(d, "s.png")
        subprocess.run(_chrome(d, wrap, (w, h), ["--screenshot=" + shot]), capture_output=True, text=True, timeout=180)
        Image.open(shot).crop((0, 0, w, h)).save(out_png)
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    sizes = sys.argv[2].split(",") if len(sys.argv) > 2 else list(SIZES)
    for key in sizes:
        for scn in SCENARIOS:
            p = os.path.join(out, f"{key}-{scn}.png")
            screenshot(scn, key, p)
            print(p)
