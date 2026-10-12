// Render the Fabrica brand PNGs + favicon.ico from the SVGs in app/static/brand/.
// No installs needed: uses a local Chrome/Edge (headless, DevTools protocol) and Node >= 22.
//   node tools/brand/render.mjs [path-to-chrome]
// Output is pixel-exact (deviceScaleFactor 1, clip = the icon size) and transparent where the SVG is.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BRAND = path.resolve(HERE, '../../app/static/brand');
const CHROME = process.argv[2] || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'fabrica-brand-'));
const PORT = 9400 + Math.floor(Math.random() * 400);

// The Apple touch icon must be a full-bleed square (iOS rounds it itself and paints transparency black).
const bleed = fs.readFileSync(path.join(BRAND, 'mark.svg'), 'utf8').replace('rx="22"', 'rx="0"');
fs.writeFileSync(path.join(TMP, 'bleed.svg'), bleed);

const JOBS = [  // [source svg, output png, size]
  [path.join(TMP, 'bleed.svg'), 'apple-touch-icon.png', 180],
  [path.join(BRAND, 'mark.svg'), 'icon-192.png', 192],
  [path.join(BRAND, 'mark.svg'), 'icon-512.png', 512],
  [path.join(BRAND, 'icon-maskable.svg'), 'icon-maskable-512.png', 512],
  [path.join(BRAND, 'mark-32.svg'), 'favicon-32.png', 32],
  [path.join(BRAND, 'mark-16.svg'), path.join(TMP, 'favicon-16.png'), 16],
];

const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${TMP}/profile`,
  '--no-first-run', '--hide-scrollbars', 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let tabs;
for (let i = 0; i < 50 && !tabs; i++) {
  await sleep(200);
  try { tabs = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json(); } catch (e) { /* not up yet */ }
}
const ws = new WebSocket(tabs.find(t => t.type === 'page').webSocketDebuggerUrl);
await new Promise(r => (ws.onopen = r));
let id = 0; const pend = {};
ws.onmessage = m => { const d = JSON.parse(m.data); if (d.id && pend[d.id]) { pend[d.id](d); delete pend[d.id]; } };
const send = (method, params = {}) => new Promise(r => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });

await send('Page.enable');
await send('Emulation.setDefaultBackgroundColorOverride', { color: { r: 0, g: 0, b: 0, a: 0 } });
for (const [src, out, n] of JOBS) {
  await send('Emulation.setDeviceMetricsOverride', { width: n, height: n, deviceScaleFactor: 1, mobile: false });
  const page = path.join(TMP, 'p.html');
  fs.writeFileSync(page, `<!doctype html><html><body style="margin:0;background:transparent"><img src="file://${src}" width="${n}" height="${n}" style="display:block"></body></html>`);
  await send('Page.navigate', { url: 'file://' + page });
  await sleep(500);
  const r = await send('Page.captureScreenshot', { format: 'png', clip: { x: 0, y: 0, width: n, height: n, scale: 1 }, captureBeyondViewport: false });
  fs.writeFileSync(path.isAbsolute(out) ? out : path.join(BRAND, out), Buffer.from(r.result.data, 'base64'));
}

// favicon.ico = 16 + 32, both stored as PNG (supported by every browser since IE 9 / Vista).
const pngs = [[16, fs.readFileSync(path.join(TMP, 'favicon-16.png'))], [32, fs.readFileSync(path.join(BRAND, 'favicon-32.png'))]];
const head = Buffer.alloc(6); head.writeUInt16LE(1, 2); head.writeUInt16LE(pngs.length, 4);
let off = 6 + 16 * pngs.length; const dirs = [];
for (const [n, buf] of pngs) {
  const e = Buffer.alloc(16);
  e.writeUInt8(n, 0); e.writeUInt8(n, 1); e.writeUInt16LE(1, 4); e.writeUInt16LE(32, 6); e.writeUInt32LE(buf.length, 8); e.writeUInt32LE(off, 12);
  dirs.push(e); off += buf.length;
}
fs.writeFileSync(path.join(BRAND, 'favicon.ico'), Buffer.concat([head, ...dirs, ...pngs.map(p => p[1])]));
ws.close(); chrome.kill(); await new Promise(r => chrome.on('exit', r));
fs.rmSync(TMP, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
console.log('done');
