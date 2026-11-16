"""Screenshots of the pop-up flows (start pop-up, fleet with the toast, spaghetti pop-up, Find printers, slot pop-up) at 800x480 (layout=5in) and 1024x600.

    python3 tools/screenshots/navshots.py <out-dir> [camera-still.jpg ...]

How: the real app/dashboard.html in headless Chrome with a stubbed `fetch` (the same fakes the tests use, tests/test_nav_popups.py): demo printers called
Printer-A..D, made-up files, neutral stills for the cameras, a drawn part for the file pictures. Nothing contacts a printer, a Hub or the network.
"""
import base64
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tests"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import test_fix_dashboard as tfd  # noqa: E402
from test_layout_5in import WRAP  # noqa: E402
from test_nav_popups import PRE  # noqa: E402

PART = ("data:image/svg+xml;utf8," + "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 160 100'><rect width='160' height='100' fill='%230d0f12'/>"
        "<ellipse cx='80' cy='82' rx='52' ry='9' fill='%23000' opacity='.5'/><path d='M22 50 L80 20 L138 50 L138 78 L108 78 L108 62 L80 76 L52 62 L52 78 L22 78Z' fill='%23aebfd8'/>"
        "<path d='M22 50 L52 62 L52 78 L22 78Z' fill='%236f7f99'/><path d='M138 50 L108 62 L108 78 L138 78Z' fill='%236f7f99'/><path d='M52 62 L80 76 L108 62 L80 48Z' fill='%23d6e2f5'/></svg>")


def still_uri(path):
    with open(path, "rb") as fh:
        return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()


def prelude(stills):
    uris = [still_uri(p) for p in stills] or [PART]
    return ("const STILLS = " + repr(uris).replace("'", '"') + ", PART = \"" + PART + "\";\n" + r"""
const srcD = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
Object.defineProperty(HTMLImageElement.prototype, 'src', {configurable: true, get(){ return srcD.get.call(this); }, set(v){
  v = String(v);
  if(v.indexOf('/snapshot') >= 0 || v.indexOf('/api/detection/flags/') >= 0){ let h = 0; for(const c of v.split('?')[0]) h = (h * 31 + c.charCodeAt(0)) >>> 0; v = STILLS[h % STILLS.length]; }
  else if(v.indexOf('/api/thumb/') >= 0 || v.indexOf('/filethumb') >= 0) v = PART;
  srcD.set.call(this, v); }});
window.thumbSrc = () => PART;            // (the file pictures are written into the page as markup, so they skip the setter above)
""")


# each flow ends with the state to photograph; `freeze` keeps the toast on screen
FLOWS = {
    "start": r"""
document.querySelector('#fleet .pcard').click(); await sleep(500);
const fb = document.getElementById('p-filesbtn'); if(fb && getComputedStyle(fb).display !== 'none') fb.click(); await sleep(400);
for(let i = 0; i < 20 && !q('#files .ftile'); i++) await sleep(100);
q('#files .ftile').click(); await sleep(900); q('#plateok').click();
""",
    "started": r"""
document.head.insertAdjacentHTML('beforeend', '<style>#toast, #toast.on { transition:none !important; }</style>');     // (a still picture: no fade in progress)
document.querySelector('#fleet .pcard').click(); await sleep(500);
const fb = document.getElementById('p-filesbtn'); if(fb && getComputedStyle(fb).display !== 'none') fb.click(); await sleep(400);
for(let i = 0; i < 20 && !q('#files .ftile'); i++) await sleep(100);
q('#files .ftile').click(); await sleep(700); q('#plateok').click(); q('#startbtn').click(); await sleep(800);
FLEET[0] = Object.assign(FLEET[0], {state: 'RUNNING', percent: 1, job: 'bracket_v3.gcode', remaining_min: 112, nozzle_c: 24, bed_c: 31}); FL.sig = null; await fleet(); await sleep(300);
clearTimeout(TOAST.t);
""",
    "spaghetti": r"""
DET.printers = {'Printer-B': {watching: true, excluded: false, verdict: {level: 'warning', score: 0.7, warmup: false, at: Date.now() / 1000 - 12}, alerts: [1]}};
FL.sig = null; await fleet(); await sleep(200); document.getElementById('sp-alert').click(); await sleep(1200);
""",
    "findprinters": r"""
window.__stubs.push((u, m) => {
  if(u === '/api/printers') return J({printers: [{name: 'Printer-A', type: 'moonraker', type_label: 'Klipper', address: 'x', model: '', driver_available: true, loaded: true, secrets: {}, values: {name: 'Printer-A'}, groups: ['Home']}]});
  if(u.indexOf('/api/printer-types') === 0) return J({types: []});
  if(u.indexOf('/api/discover/suggestions') === 0) return J({networks: ['192.168.1.0/24'], sources: {'192.168.1.0/24': 'your Hub'}, note: ''});
  return null; });
await openPrinters(); await sleep(400); await pmDiscover(); await sleep(500);
""",
    "slot": r"""
CUR = 'Printer-A'; window._materials = ['PLA', 'PETG', 'ABS', 'ASA', 'TPU']; window._palette = ['GREEN', 'ORANGE', 'RED', 'BLUE', 'BLACK', 'WHITE'];
askTray(0, 'PLA'); await sleep(400);
""",
}


def shoot(flow, query, size, out, stills):
    drv = prelude(stills) + PRE + "\n" + FLOWS[flow]
    page_html = tfd.build_page(drv)
    d = tempfile.mkdtemp(prefix="navshot-")
    page = os.path.join(d, "page.html")
    with open(page, "w", encoding="utf8") as fh:
        fh.write(page_html.replace("url(/static/brand/", "url(file://" + os.path.join(HERE, "..", "..", "app", "static", "brand") + "/").replace('src="/static/brand/', 'src="file://' + os.path.join(HERE, "..", "..", "app", "static", "brand") + "/"))
    wrap = os.path.join(d, "wrap.html")
    with open(wrap, "w", encoding="utf8") as fh:
        fh.write(WRAP % {"src": "file://" + page + query, "w": size[0], "h": size[1]})
    raw = os.path.join(d, "raw.png")
    cmd = [tfd.CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
           "--allow-file-access-from-files", "--force-device-scale-factor=1", "--user-data-dir=" + os.path.join(d, "profile"),
           "--window-size=%d,%d" % (max(size[0], 1024), size[1] + 200), "--virtual-time-budget=12000", "--screenshot=" + raw, "file://" + wrap]
    subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    from PIL import Image
    Image.open(raw).crop((0, 0, size[0], size[1])).save(out)
    print(out)


if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    stills = sys.argv[2:]
    for tag, query, size in (("5in", "?group=Home&layout=5in", (800, 480)), ("10in", "?kiosk=1", (1024, 600))):
        for flow in FLOWS:
            shoot(flow, query, size, os.path.join(out, "%s-%s.png" % (flow, tag)), stills)
