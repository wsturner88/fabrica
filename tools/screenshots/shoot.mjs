// README screenshots of a REAL Hub, with every file / job name swapped for a made-up demo name and the
// camera pictures swapped for neutral stills. Nothing personal reaches the pictures.
//
//   node tools/screenshots/shoot.mjs <hub-url> <out-dir> [camera-still.jpg ...]
//
// How: a small proxy on 127.0.0.1 sits between headless Chrome and the Hub.
//   * Only reads go through (GET/HEAD) plus POST .../suggest (it only proposes a spool mapping). Every
//     other request is refused with 403, so a screenshot run can never start, stop or change anything.
//   * JSON answers are rewritten: every file / job name becomes a demo name (same real name -> same demo
//     name for the whole run); request bodies are mapped back so the Hub still finds the real file.
//   * /snapshot answers are replaced by the stills you pass (round-robin per printer); with none given the
//     real picture is used - check it has no names, logos or labels in view.
// Needs Node >= 22 and a local Chrome. Never commit the pictures without looking at every one of them.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';

const [HUB, OUT, ...STILLS] = process.argv.slice(2);
if (!HUB || !OUT) { console.error('usage: node shoot.mjs <hub-url> <out-dir> [still.jpg ...]'); process.exit(2); }
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PROXY_PORT = 8399, CDP_PORT = 9333;

const DEMO = ['Phone Stand v3', 'Cable Clip x12', 'Headphone Hook', 'Desk Organizer - Tall', 'Plant Pot 120mm',
  'Gridfinity Bin 2x3', 'Spool Holder Arm', 'Drawer Pull x4', 'Wall Hook - Heavy', 'Keychain Tag x20',
  'Lamp Shade v2', 'Battery Holder AA x8', 'Coaster Set x6', 'Pegboard Tool Rack', 'Raspberry Pi Case',
  'Hose Adapter 3-4in', 'Calibration Cube', 'Benchy', 'Laptop Riser', 'Pen Cup Hex', 'Shelf Bracket x2',
  'Router Wall Mount', 'Clip-on Hook x10', 'Card Holder', 'Bag Clip x6', 'Cord Wrap x8', 'Bottle Opener',
  'Tablet Stand', 'Filament Swatch x12', 'Fan Duct v4', 'Camera Mount', 'Bed Level Test', 'Temp Tower',
  'Retraction Test', 'Flow Rate Test', 'Pressure Advance Test', 'Desk Grommet x2', 'Hinge Pin x8',
  'Label Holder x10', 'Watch Stand', 'Soap Dish', 'Key Rack', 'Mini Planter x3', 'Dice Tower', 'Puzzle Box'];
const toDemo = new Map(), toReal = new Map();
const extOf = s => (s.match(/(\.gcode\.3mf|\.3mf|\.bgcode|\.gcode)$/i) || [''])[0];
function demoName(real) {
  if (toDemo.has(real)) return toDemo.get(real);
  const base = DEMO[toDemo.size % DEMO.length] + (toDemo.size >= DEMO.length ? ' ' + (1 + Math.floor(toDemo.size / DEMO.length)) : '');
  const d = base + extOf(real.split('/').pop());
  toDemo.set(real, d); toReal.set(d, real); return d;
}
const NAME_KEYS = new Set(['name', 'file', 'filename', 'job', 'path', 'file_key', 'title', 'display', 'display_name',
  'subtask_name', 'gcode_file', 'project', 'task', 'basename']);
const looksLikeJob = (k, v, parent) => typeof v === 'string' && v &&
  (k === 'job' || k === 'file' || k === 'filename' || k === 'subtask_name' || k === 'gcode_file' || k === 'file_key' ||
   /\.(3mf|gcode|bgcode)$/i.test(v) || (k === 'name' && parent && ('size' in parent || 'print_time' in parent || 'thumb' in parent || 'grams' in parent)));
function scrub(o, parentKey) {
  if (Array.isArray(o)) return o.map(x => scrub(x, parentKey));
  if (o && typeof o === 'object') {
    const r = {};
    for (const [k, v] of Object.entries(o)) {
      if (NAME_KEYS.has(k) && looksLikeJob(k, v, o)) r[k] = demoName(v);
      else r[k] = scrub(v, k);
    }
    return r;
  }
  return o;
}
function unscrub(o) {
  if (Array.isArray(o)) return o.map(unscrub);
  if (o && typeof o === 'object') { const r = {}; for (const [k, v] of Object.entries(o)) r[k] = unscrub(v); return r; }
  return typeof o === 'string' && toReal.has(o) ? toReal.get(o) : o;
}
const stills = STILLS.map(f => fs.readFileSync(f)), stillFor = new Map();

const proxy = http.createServer(async (req, res) => {
  const allowed = req.method === 'GET' || req.method === 'HEAD' || (req.method === 'POST' && /\/suggest$/.test(req.url));
  if (!allowed) { res.writeHead(403); return res.end('{"error":"screenshot proxy: read-only"}'); }
  const m = req.url.match(/^\/api\/printer\/([^/]+)\/snapshot/);
  if (m && stills.length) {
    if (!stillFor.has(m[1])) stillFor.set(m[1], stills[stillFor.size % stills.length]);
    res.writeHead(200, { 'Content-Type': 'image/jpeg', 'Cache-Control': 'no-store' }); return res.end(stillFor.get(m[1]));
  }
  let body;
  if (req.method === 'POST') {
    const chunks = []; for await (const c of req) chunks.push(c);
    try { body = JSON.stringify(unscrub(JSON.parse(Buffer.concat(chunks).toString()))); } catch { body = Buffer.concat(chunks); }
  }
  const url = new URL(req.url, HUB);
  for (const [k, v] of [...url.searchParams]) if (toReal.has(v)) url.searchParams.set(k, toReal.get(v));
  url.pathname = url.pathname.split('/').map(s => { const d = decodeURIComponent(s); return toReal.has(d) ? encodeURIComponent(toReal.get(d)) : s; }).join('/');
  try {
    const r = await fetch(url, { method: req.method, body, headers: { 'Content-Type': req.headers['content-type'] || 'application/json',
      Origin: HUB, Accept: req.headers.accept || '*/*' } });
    const ct = r.headers.get('content-type') || '';
    let buf = Buffer.from(await r.arrayBuffer());
    if (ct.includes('json')) { try { buf = Buffer.from(JSON.stringify(scrub(JSON.parse(buf.toString())))); } catch {} }
    res.writeHead(r.status, { 'Content-Type': ct, 'Cache-Control': 'no-store' }); res.end(buf);
  } catch (e) { res.writeHead(502); res.end(String(e)); }
});
await new Promise(r => proxy.listen(PROXY_PORT, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${PROXY_PORT}`;

// ---- headless Chrome via the DevTools protocol (no installs; same pattern as tools/brand/render.mjs)
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'shots-'));
fs.mkdirSync(OUT, { recursive: true });
const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${TMP}/profile`,
  '--no-first-run', '--hide-scrollbars', 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let tabs;
for (let i = 0; i < 50 && !tabs; i++) { await sleep(200); try { tabs = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json`)).json(); } catch {} }
const ws = new WebSocket(tabs.find(t => t.type === 'page').webSocketDebuggerUrl);
await new Promise(r => (ws.onopen = r));
let id = 0; const pend = {};
ws.onmessage = m => { const d = JSON.parse(m.data); if (d.id && pend[d.id]) { pend[d.id](d); delete pend[d.id]; } };
const send = (method, params = {}) => new Promise(r => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
const js = async expr => (await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })).result?.result?.value;
const shot = async (name, h) => {
  const r = await send('Page.captureScreenshot', { format: 'png', ...(h ? { clip: { x: 0, y: 0, width: 1024, height: h, scale: 1 } } : {}) });
  fs.writeFileSync(path.join(OUT, name + '.png'), Buffer.from(r.result.data, 'base64')); console.log('saved', name);
};
const home = async () => { await js(`show('home')`); await sleep(1200); };

await send('Page.enable'); await send('Runtime.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1024, height: 600, deviceScaleFactor: 2, mobile: false });
await send('Page.navigate', { url: BASE + '/dashboard' });
await sleep(9000);
const fleetH = await js(`Math.ceil(document.querySelector('#fleet').getBoundingClientRect().bottom + 14)`);
await shot('fleet', Math.min(600, fleetH || 600));

const fleet = await js(`(async()=>{ const f = await (await fetch('/api/fleet')).json(); return (Array.isArray(f) ? f : (f.printers || [])).map(p => [p.printer, p.state, !!p.connected]); })()`);
const idle = (fleet || []).find(p => p[2] && !['RUNNING', 'PREPARE', 'PAUSE'].includes(p[1]));
const busy = (fleet || []).find(p => p[1] === 'RUNNING');
if (busy) { await js(`openPrinter(${JSON.stringify(busy[0])})`); await sleep(9000); await shot('printer-printing'); await home(); }
if (idle) {
  await js(`openPrinter(${JSON.stringify(idle[0])})`); await sleep(10000); await shot('printer-files');
  const ok = await js(`(()=>{ const b = [...document.querySelectorAll('#files button')].find(b => /^\\s*Print\\s*$/.test(b.textContent) && !b.disabled); if(!b) return false; b.click(); return true; })()`);
  if (ok) { await sleep(7000); await shot('start-print'); await js(`closeModal()`); }
  await home();
}
await js(`openHistory()`); await sleep(5000); await shot('history'); await home();
await js(`openSettings()`); await sleep(4000); await shot('settings'); await home();

ws.close(); chrome.kill(); await new Promise(r => chrome.on('exit', r)); proxy.close();
fs.rmSync(TMP, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
console.log('names swapped:', toDemo.size, '- now LOOK at every picture before committing it');
