"""Screenshots of the spaghetti modes (OFF / WATCH / ACTIVE): the fleet with mixed badges, the 3-way control on a printer screen, Settings > Spaghetti,
the spaghetti pop-up with the Real / False alarm buttons, the Cancel confirm with "It was spaghetti", and the Flagged pictures gallery.

    python3 tools/screenshots/spagshots.py <out-dir> [camera-still.jpg ...]

Same method as navshots.py: the real app/dashboard.html in headless Chrome with a stubbed `fetch`; demo printers called Printer-A..E, made-up files,
neutral stills. The Settings document comes from the real settings_store schema. Nothing contacts a printer, a Hub or the network.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "app"))
import navshots  # noqa: E402
import settings_store  # noqa: E402


def settings_doc():
    d = tempfile.mkdtemp(prefix="spagshot-settings-")
    st = settings_store.SettingsStore(d)
    st.update({"detection.mode": "local", "detection.learn_only": False, "detection.url": "http://detector:8080"})
    return st.describe()


SETUP = r"""
const run = (n, pct, job, o) => mk(n, 'RUNNING', Object.assign({percent: pct, job, layer: '40/200', remaining_min: 60, nozzle_c: 220, bed_c: 58}, o || {}));
FLEET.length = 0;
FLEET.push(run('Printer-A', 32, 'bracket_v3.gcode'), run('Printer-B', 41, 'Frame bezel.gcode'), mk('Printer-C', 'PAUSE', {percent: 57, job: 'clip_t_slot.gcode', remaining_min: 40, layer: '60/150', nozzle_c: 190, bed_c: 58}),
           run('Printer-D', 12, 'spacer_plate.gcode'), run('Printer-E', 77, 'stand_wedge.gcode'), run('Printer-F', 5, 'knob_set.gcode'));
const dp = (mode, o) => Object.assign({watching: true, excluded: mode === 'off', mode, gate: '', confirm: {count: 0, needed: 3}, auto_paused: false, resumed: false, flag_id: null,
  baseline_frames: 360, learn_frames: 360, learning: '', state: 'RUNNING', camera: 'ok', frames_scored: 80, alerts: [],
  verdict: {level: 'ok', score: 0.08, warmup: false, at: Date.now() / 1000 - 6}}, o || {});
const warn = {level: 'warning', score: 0.74, warmup: false, at: Date.now() / 1000 - 8};
DET = {enabled: true, mode: 'alert', interval_s: 10, confirm_pictures: 3, auto_pause: true, detector: {state: 'up'},
  setup: {mode: 'local', excluded_printers: ['Printer-E'], confirm_pictures: 3, confirm_choices: [2, 3, 4, 6],
          printer_modes: {'Printer-A': 'watch', 'Printer-B': 'active', 'Printer-C': 'active', 'Printer-D': 'active', 'Printer-E': 'off', 'Printer-F': 'watch'}},
  printers: {'Printer-A': dp('watch'),
    'Printer-B': dp('active', {verdict: warn, confirm: {count: 2, needed: 3}, flag_id: 'flag-b1'}),
    'Printer-C': dp('active', {watching: false, state: 'PAUSE', auto_paused: true, verdict: warn, flag_id: 'flag-c1'}),
    'Printer-D': dp('active', {baseline_frames: 204, gate: 'learning'}),
    'Printer-E': dp('off', {watching: false}),
    'Printer-F': dp('watch', {baseline_frames: 61})}};
const FLAGS = [
  {id: 'flag-b1', kind: 'flag', printer: 'Printer-B', ts: Date.now() / 1000 - 90, mode: 'active', job: 'Frame bezel.gcode', level: 'warning', paused: false, label: '', score: 0.74, pictures: 3},
  {id: 'flag-c1', kind: 'flag', printer: 'Printer-C', ts: Date.now() / 1000 - 3600, mode: 'active', job: 'clip_t_slot.gcode', level: 'failure', paused: true, label: 'real', score: 0.93, pictures: 4},
  {id: 'flag-a1', kind: 'flag', printer: 'Printer-A', ts: Date.now() / 1000 - 7200, mode: 'watch', job: 'bracket_v3.gcode', level: 'warning', paused: false, label: 'false', score: 0.61, pictures: 2},
  {id: 'miss-d1', kind: 'missed', printer: 'Printer-D', ts: Date.now() / 1000 - 90000, mode: 'watch', job: 'spacer_plate.gcode', level: '', paused: false, label: 'missed', score: 0.12, pictures: 1},
  {id: 'flag-f1', kind: 'flag', printer: 'Printer-F', ts: Date.now() / 1000 - 172800, mode: 'watch', job: 'knob_set.gcode', level: 'warning', paused: false, label: '', score: 0.55, pictures: 1},
  {id: 'flag-a2', kind: 'flag', printer: 'Printer-A', ts: Date.now() / 1000 - 200000, mode: 'watch', job: 'bracket_v3.gcode', level: 'warning', paused: false, label: 'real', score: 0.88, pictures: 5}];
window.__stubs.unshift((u, m, init) => {
  if(u.indexOf('/api/detection/flags/') === 0 && /\/label$/.test(u)) return J({flag: FLAGS[0]});
  if(u.indexOf('/api/detection/flags') === 0) return J({flags: FLAGS, counts: {all: 6, real: 2, false: 1, missed: 1, none: 2, printers: ['Printer-A', 'Printer-B', 'Printer-C', 'Printer-D', 'Printer-F']}});
  if(u === '/api/settings') return J(SETTINGS_DOC);
  if(u === '/api/printers') return J({printers: FLEET.map(p => ({name: p.printer, type: 'moonraker', type_label: 'Klipper', address: 'x', model: '', driver_available: true, loaded: true, secrets: {}, values: {name: p.printer}, groups: ['Home']}))});
  return null; });
FL.sig = null; await fleet(); await sleep(250);
"""

FLOWS = {
    "fleet": "",
    "fleet-more": "for(const e of [document.scrollingElement, document.getElementById('home'), document.getElementById('fleet')]) if(e) e.scrollTop = 99999; window.scrollTo(0, 99999); await sleep(300);",
    "control": r"""
window.__stubs.unshift((u) => u === '/api/printer/Printer-A' ? J(Object.assign(snapA({state: 'RUNNING', job: 'bracket_v3.gcode', percent: 32, remaining_min: 60, nozzle_c: 220, bed_c: 58, nozzle_target_c: 220, bed_target_c: 58}), {detection: {state: 'ok', text: 'Watching', mode: 'watch', gate: '', confirm: {count: 0, needed: 3}, baseline_frames: 360, learn_frames: 360, learning: '', score: 0.08}})) : null);
await openPrinter('Printer-A'); await sleep(900);
""",
    "idle": r"""
window.__stubs.unshift((u) => u === '/api/printer/Printer-A' ? J(Object.assign(snapA({state: 'IDLE', job: '', percent: 0, nozzle_c: 24, bed_c: 23}), {detection: {state: 'idle', text: 'Ready', mode: 'watch', gate: '', confirm: {count: 0, needed: 3}, baseline_frames: 360, learn_frames: 360, learning: ''}})) : null);
FLEET[0] = mk('Printer-A', 'IDLE'); FL.sig = null; await fleet(); await openPrinter('Printer-A'); await sleep(900);
""",
    "control-warn": r"""
const snapB = () => snapA({printer: 'Printer-B', state: 'RUNNING', job: 'Frame bezel.gcode', percent: 41, remaining_min: 41, nozzle_c: 220, bed_c: 58, nozzle_target_c: 220, bed_target_c: 58});
window.__stubs.unshift((u) => u === '/api/printer/Printer-B' ? J(Object.assign(snapB(), {detection: {state: 'warning', text: 'WARNING', mode: 'active', gate: '', confirm: {count: 2, needed: 3}, flag_id: 'flag-b1', baseline_frames: 360, learn_frames: 360, learning: ''}})) : null);
await openPrinter('Printer-B'); await sleep(900);
""",
    "settings": r"""
await openSettings(); await sleep(500); stSelect('detection'); await sleep(500); await detLoad(); await sleep(300); document.getElementById('st-sp-rows').scrollIntoView({block: 'start'}); window.scrollBy(0, -50); await sleep(300);
""",
    "settings-more": r"""
await openSettings(); await sleep(500); stSelect('detection'); await sleep(500); await detLoad(); await sleep(300); const rr = document.querySelectorAll('#st-sp-rows .sprow'); rr[rr.length - 4].scrollIntoView({block: 'start'}); await sleep(300);
""",
    "popup": "FL.sig = null; await fleet(); spagOpen('Printer-B'); await sleep(1200);",
    "popup-paused": "FL.sig = null; await fleet(); spagOpen('Printer-C'); await sleep(1200);",
    "cancel": "askCancel('Printer-B'); await sleep(400); document.getElementById('cn-spag').click(); await sleep(200);",
    "gallery": "await galOpen(); await sleep(500); document.querySelectorAll('.gld img').forEach(i => { i.src = i.getAttribute('src'); }); await sleep(600);",
    "gallery-detail": "await galOpen(); await sleep(500); galPick('flag-b1'); await sleep(500); document.querySelectorAll('.gld img').forEach(i => { i.src = i.getAttribute('src'); }); await sleep(600);",
}

if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    stills = sys.argv[2:]
    doc = json.dumps(settings_doc())
    for k, v in FLOWS.items():
        navshots.FLOWS["spag-" + k] = ("const SETTINGS_DOC = " + doc + ";\n" + SETUP.replace("FLEET.push(", "FLEET.push(") + v)
    only = [a for a in os.environ.get("ONLY", "").split(",") if a]
    for tag, query, size in (("5in", "?group=Home&layout=5in", (800, 480)), ("10in", "?kiosk=1", (1024, 600))):
        for flow in FLOWS:
            if only and flow not in only:
                continue
            navshots.shoot("spag-" + flow, query, size, os.path.join(out, "%s-%s.png" % (flow, tag)), stills)
