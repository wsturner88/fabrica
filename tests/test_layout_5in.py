"""Tests for the 5-inch wall-screen layout: /dashboard?layout=5in (800x480 landscape, capacitive touch), updated for the Quiet Grid look
(default theme; the Bridge skin is covered by test_5in_round3.py, the static rules and the printer board by test_5in_layout.py).

    python3 tests/test_layout_5in.py

Static checks read app/dashboard.html as text. The browser tests load the real page into headless Chrome / Edge at 800x480 with a
stubbed `fetch` (skipped when no browser is found); nothing contacts a printer or the network.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()

CSS_MARK = "/* ==== 5-inch wall screen: /dashboard?layout=5in"
CSS = HTML[HTML.index(CSS_MARK):]
CSS = CSS[:CSS.index("</style>")]
HEAD_JS = HTML[HTML.index("function l5Parse(search)"):]
HEAD_JS = HEAD_JS[:HEAD_JS.index("</script>")]


class FiveInStaticTests(unittest.TestCase):
    def test_it_is_an_explicit_opt_in_read_once_and_never_auto_detected(self):
        self.assertIn("get('layout')", HEAD_JS)
        self.assertIn("=== '5in'", HEAD_JS)
        self.assertIn("const L5 = l5Parse(location.search);", HEAD_JS)
        self.assertIn("if(L5){ document.documentElement.classList.add('lay-5in');", HEAD_JS)
        self.assertIn("if(L5T === 'bridge') document.documentElement.classList.add('th-bridge');", HEAD_JS)   # the optional skin is a second class, never a default
        # the class is set in <head>, before the body paints, and L5 is declared exactly once
        self.assertLess(HTML.index("const L5 = "), HTML.index("<body>"))
        self.assertEqual(HTML.count("const L5 = "), 1)
        # nothing detects the screen by size: no media query may carry the 5-inch size or the class
        self.assertNotIn("@media", CSS)
        for m in re.findall(r"@media[^{]*\{", HTML):
            self.assertNotRegex(m, r"\b(800|480)px", m)
        self.assertNotRegex(HEAD_JS, r"innerWidth|innerHeight|matchMedia|screen\.")

    def test_every_compact_rule_is_scoped_under_the_class(self):
        body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        body = body[body.index("<style>") + 7:] if "<style>" in body else body
        n = 0
        for sel, _ in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            for one in sel.split(","):
                one = one.strip()
                if not one:
                    continue
                n += 1
                # the self-hosted @font-face rules and the hidden-everywhere-else .more5 entries are the only things that may sit outside the class
                self.assertTrue(one.startswith("html.lay-5in") or one == ".more5" or one == "@font-face", "not scoped: " + one)
        self.assertGreater(n, 40)

    def test_the_group_chips_only_rewrite_group(self):
        fn = HTML[HTML.index("function flUrl(){"):HTML.index("function flChoose")]
        self.assertIn("searchParams.delete('group')", fn)
        self.assertIn("searchParams.append('group', g)", fn)
        self.assertNotIn("layout", fn)
        self.assertNotRegex(fn, r"new URLSearchParams|search\s*=|location\.(href|assign|replace)\s*=")
        # ... and keeps every other parameter by starting from the current URL
        self.assertIn("new URL(location.href)", fn)

    def test_the_more_sheet_carries_the_pages_the_5in_header_drops(self):
        sheet = HTML[HTML.index('<div id="moresheet">'):HTML.index('<section id="printer"')]
        self.assertRegex(sheet, r'class="more5" data-act="moreGo" data-go="printers">Printers</button>')
        self.assertRegex(sheet, r'class="more5" data-act="moreGo" data-go="library">Library</button>')
        self.assertIn("printers: () => openPrinters(), library: () => openLibrary()", HTML)
        self.assertIn(".more5 { display:none; }", CSS)          # hidden everywhere else (phones keep their own header buttons)
        self.assertIn("button.more5 { display:block; }", CSS)
        # the five pages' header buttons are hidden by CSS under the class, the same buttons stay for every other layout
        self.assertIn('html.lay-5in #home > .row:first-child > button:not(#morebtn) { display:none !important; }', CSS)

    def test_no_inline_handlers_and_every_dynamic_string_is_escaped(self):
        self.assertNotRegex(CSS, r"\bon[a-z]+\s*=")
        card = HTML[HTML.index("html = data.map((p,i)=>{"):HTML.index("// nothing changed since the last poll")]
        self.assertIn("esc(cp.pct)", card)
        self.assertIn('data-act="openPrinter" data-name="${esc(p.printer)}"', card)
        self.assertNotRegex(card, r"\$\{(?:p\.printer|p\.job|word)\}")
        self.assertNotRegex(card, r"\bon(click|error|load)\s*=")

    def test_every_5in_card_has_a_picture_slot_and_pictures_are_fetched_for_up_to_six_printers(self):
        # Quiet Grid: the old camera-less tiles are gone; each card has the same slot - a still, or a designed placeholder in words
        fa = HTML[HTML.index("function fcAttach(data){"):HTML.index("function fcSync(){")]
        self.assertIn("NO CAMERA", fa)
        self.assertIn("CAMERA OFFLINE", fa)
        self.assertIn("if(L5 && !l5Cams(data.length)){ slot.innerHTML = '<div class=\"ph\">' + IC('cam') + 'CAMERA</div>'; return; }", fa)
        self.assertIn("const l5Cams = n => n >= 1 && n <= 6;", HTML)
        self.assertIn('<div class="cslot" data-i="${i}"></div>', HTML[HTML.index("function l5Card("):HTML.index("function fitEta(")])

    def test_text_has_a_floor_and_the_touch_targets_are_finger_sized(self):
        # Quiet Grid (approved round-3 mockups) trades the old blanket 14px floor for a hierarchy: names 22px, the key number 40px+,
        # supporting lines 12-13px, captions never under 11px. Every button is still 44px tall; the group chips are 40px inside a padded row.
        sizes = [int(x) for x in re.findall(r"font-size:(\d+)px", CSS)]
        self.assertTrue(sizes and min(sizes) >= 11, sorted(set(sizes))[:5])
        self.assertIn("font-size:22px", re.search(r"html\.lay-5in #fleet \.pcard \.pname \{[^}]*\}", CSS).group(0))
        self.assertIn("font-size:40px", re.search(r"html\.lay-5in #fleet \.pcard \.stat \.big \{[^}]*\}", CSS).group(0))
        self.assertIn("size > (L5 ? 14 : 12)", HTML)             # "Done ~5:08 PM" never shrinks below 14px on 5in
        self.assertIn("button, html.lay-5in a.bkdl { min-height:44px;", CSS.replace("html.lay-5in button,", "button,"))
        self.assertRegex(CSS, r"html\.lay-5in \.fl-chip \{[^}]*min-height:40px")
        self.assertRegex(CSS, r"html\.lay-5in #home > \.row:first-child > #morebtn \{[^}]*min-height:44px")


# a fixture of 1-9 printers, some with a camera: every card has a picture slot (a still, or a placeholder in words); 7+ printers fetch nothing
DRIVER = r"""
try{ localStorage.setItem('fabrica.filesview', 'list'); }catch(e){}       // (the file ROWS are looked at here: the 5-inch sheet opens the Grid by default, see tests/test_files_grid.py)
const mk = (printer, groups, state, pct, job, o = {}) => ({printer, groups, connected: state !== 'UNKNOWN', state, percent: pct,
  remaining_min: state === 'RUNNING' ? (o.rem || 116) : 0, job, nozzle_c: state === 'RUNNING' ? 220 : 25, bed_c: state === 'RUNNING' ? 60 : 24, camera: !!o.cam});
const SETS = {
  two: [mk('Printer-A', ['Home'], 'RUNNING', 42, 'Benchy hull.3mf', {cam: true}), mk('Printer-B', ['Home'], 'IDLE', 0, '')],
  four: [mk('Printer-A', ['Home'], 'RUNNING', 42, 'Bracket x12 for the workshop shelf.3mf', {cam: true}), mk('Printer-B', ['Home', 'Proto'], 'PAUSE', 63, 'Lid.3mf', {cam: true}),
         mk('Printer-C', ['Home'], 'FINISH', 100, 'Phone stand.3mf'), mk('Printer-D', ['Home'], 'UNKNOWN', 0, '')],
  six: Array.from({length: 6}, (_, i) => mk('P' + (i + 1), ['Work'], i % 2 ? 'IDLE' : 'RUNNING', i % 2 ? 0 : 30, i % 2 ? '' : 'Job ' + i, {rem: i === 0 ? 1700 : 90})),
  nine: Array.from({length: 9}, (_, i) => mk('P' + (i + 1), ['Work'], 'IDLE', 0, '')),
  work: [mk('Printer-E', ['Work'], 'IDLE', 0, ''), mk('Printer-A', ['Home'], 'RUNNING', 42, 'x')],
};
let FLEET = SETS.four, SUG_MULTI = false;
window.__stubs.push((u, m) => {
  if(u === '/api/fleet') return J(FLEET);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/printer/Printer-A') return J({snapshot: {printer: 'Printer-A', connected: true, state: 'RUNNING', job: 'Bracket x12', percent: 42, layer: 5, nozzle_c: 220, bed_c: 60, nozzle_target_c: 220, bed_target_c: 60},
    trays: [{tray: 0, slot_1based: 1, type: 'PLA', color_name: 'GREEN'}, {tray: 1, slot_1based: 2, type: 'PETG', color_name: 'BLACK'}, {tray: 2, slot_1based: 3, type: 'PLA', color_name: 'WHITE'}, {tray: 3, slot_1based: 4, type: 'PLA', color_name: 'RED'}],
    camera: false, can_connect: false, kind: 'bambu', controls: {actions: []}});
  if(u === '/api/printer/Printer-A/files') return J([{name: 'Bracket x12.gcode.3mf', print_time: '1h 20m', weight_g: 34, filaments: [{color_name: 'GREEN', type: 'PLA'}]},
    {name: 'Lid.gcode.3mf', print_time: '40m', weight_g: 12, filaments: [{color_name: 'BLACK', type: 'PETG'}]}]);
  if(u === '/api/printer/Printer-A/suggest') return J({file: 'Two colour lid.gcode.3mf', ams_mapping: SUG_MULTI ? [0, 1] : [0], confirm_string: 'ph-x', mapping_described: 'Slot 1', notes: [], model_match: true,
    target_model_name: 'Bambu P1S', printer_kind: 'bambu', print_time: '3h', weight_g: 71,
    filaments: SUG_MULTI ? [{index: 0, type: 'PETG', color_name: 'BLACK', used_g: 52, role: 'main'}, {index: 1, type: 'PLA', color_name: 'WHITE', used_g: 19}] : [{index: 0, type: 'PLA', color_name: 'GREEN', used_g: 34}]});
  return null;
});
window.__rs = [];
history.replaceState = (s, t, u) => { window.__rs.push(String(u)); };      // record the rewrite instead of doing it (file:// may refuse it)

const R5 = () => document.getElementById('fleet');
const r = e => { const b = e.getBoundingClientRect(); return {t: Math.round(b.top), b: Math.round(b.bottom), l: Math.round(b.left), r: Math.round(b.right), w: Math.round(b.width), h: Math.round(b.height)}; };
const cards = () => [...document.querySelectorAll('#fleet .pcard')];
const inside = b => b.t >= 0 && b.l >= 0 && b.b <= innerHeight && b.r <= innerWidth;
const px = (e, p) => parseFloat(getComputedStyle(e)[p]);
const txt = (c, q) => { const e = c.querySelector(q); return e ? e.textContent.trim() : ''; };
const snap = () => ({
  cls: document.documentElement.className, vw: innerWidth, vh: innerHeight, n: R5().dataset.n || null,
  docH: document.documentElement.scrollHeight, docW: document.documentElement.scrollWidth,
  names: [...document.querySelectorAll('#fleet .pname')].map(e => e.textContent),
  cards: cards().map(r), fleet: r(R5()), fleetScroll: [R5().scrollHeight, R5().clientHeight],
  allInside: cards().every(c => inside(r(c))),
  cols: new Set(cards().map(c => r(c).l)).size, rows: new Set(cards().map(c => r(c).t)).size,
  slots: [...document.querySelectorAll('#fleet .cslot')].length,
  slotText: [...document.querySelectorAll('#fleet .cslot')].map(e => (e.querySelector('.ph') || {textContent: ''}).textContent.trim()),
  slotCams: document.querySelectorAll('#fleet .cslot .cardcam').length,
  slotBoxes: [...document.querySelectorAll('#fleet .cslot')].map(r),
  fonts: cards().map(c => ({name: px(c.querySelector('.pname'), 'fontSize'), big: px(c.querySelector('.stat .big'), 'fontSize'),
    sub: px(c.querySelector('.stat .sub'), 'fontSize'), spag: px(c.querySelector('.spag'), 'fontSize')})),
  chips: cards().map(c => txt(c, '.chip')),
  bigs: cards().map(c => txt(c, '.stat .big')),
  subs: cards().map(c => txt(c, '.stat .sub')),
  files: cards().map(c => txt(c, '.stat .file')),
  spags: cards().map(c => txt(c, '.spag')),
  kinds: cards().map(c => (c.className.match(/k-(\w+)/) || [])[1]),
  barw: cards().map(c => c.querySelector('.bar i').style.width),
  overflowX: cards().map(c => [...c.querySelectorAll('.pname, .stat .big, .stat .sub, .stat .file')].filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.className)),
  header: r(document.querySelector('#home > #morebtn, #morebtn')),
  hdrButtons: [...document.querySelectorAll('#home > .row:first-child > button')].map(b => ({t: b.title || b.textContent.trim(), shown: getComputedStyle(b).display !== 'none', h: r(b).h, w: r(b).w})),
  chipsRow: document.getElementById('fl-sw').hidden ? null : r(document.getElementById('fl-sw')),
  chipHeights: [...document.querySelectorAll('.fl-chip')].map(b => r(b).h),
  hub: (() => { const b = document.querySelector('#home .hubdown'); return b.hidden ? null : {...r(b), text: b.textContent}; })(),
  fetches: window.__calls.filter(c => c.includes('/snapshot')).length,
});
const set = async (arr, query) => { FLEET = arr; FL.sig = null; await fleet(); await sleep(50); };
await sleep(300);
await set(SETS.four);

R.first = snap();                                       // ?group=Home&layout=5in on first load, 4 printers (Home has all four)
R.parse = ['?layout=5in', '?layout=5IN', '?layout= 5in ', '?group=Home&layout=5in', '?layout=5in&layout=7in', '?layout=', '?layout=7in', '?layout=5inx', '?xlayout=5in', '?group=5in', '', '?layout'].map(q => l5Parse(q));
R.parseNull = [l5Parse(undefined), l5Parse(null)];

// 2, 3 (a hidden fifth), 6 and 9 printers
await set(SETS.two);  R.two = snap();
await set(SETS.six);  R.six = snap();
await set(SETS.nine); R.nine = snap();
await set(SETS.four); R.fourAgain = snap();

// a group chip tap rewrites ?group= and keeps ?layout=5in
await set(SETS.work);
R.chipsBefore = [...document.querySelectorAll('.fl-chip')].map(b => b.textContent.replace(/\s+/g, ' ').trim());
document.querySelector('.fl-chip[data-g="Work"]').click(); await sleep(50);
R.tapWork = {rs: window.__rs.slice(), names: snap().names, cls: document.documentElement.className, sel: FLG.sel.slice()};
document.querySelector('[data-act=flAll]').click(); await sleep(50);
R.tapAll = {rs: window.__rs.slice(), cls: document.documentElement.className, sel: FLG.sel.slice()};

// hub down: the banner stays visible and every tile still fits
await set(SETS.four);
HUB.lastOk = Date.now() - 60000; HUB.enter = Date.now() - 60000; window.__down = true; await fleet(); await sleep(100);
R.hubdown = snap(); window.__down = false; await fleet(); await sleep(100);
R.hubback = snap();

// the More menu: the six pages are reachable from it
document.getElementById('morebtn').click(); await sleep(50);
R.more = {on: document.getElementById('moresheet').classList.contains('on'), expanded: document.getElementById('morebtn').getAttribute('aria-expanded'),
  buttons: [...document.querySelectorAll('#moresheet .morebox button')].filter(b => getComputedStyle(b).display !== 'none').map(b => ({t: b.textContent.trim(), h: r(b).h, inside: inside(r(b))}))};
document.querySelector('#moresheet .morebk').click(); await sleep(50);
R.moreClosed = !document.getElementById('moresheet').classList.contains('on');
// each entry opens its page and closes the sheet
window.__went = []; for(const n of ['openPrinters', 'openLibrary', 'openHistory', 'openAnalytics', 'openBackups', 'openSettings']) window[n] = () => window.__went.push(n);
for(const go of ['printers', 'library', 'history', 'analytics', 'backups', 'settings']){ document.getElementById('morebtn').click(); document.querySelector('#moresheet [data-go="' + go + '"]').click(); }
R.went = window.__went.slice(); R.wentClosed = !document.getElementById('moresheet').classList.contains('on');

// the printer screen and the dialogs at 800x480
const DLG = () => { const mb = document.getElementById('mbody'), st = document.getElementById('startbtn'), pl = document.getElementById('plateok');
  return {modalOn: document.getElementById('modal').classList.contains('on'), mbox: r(mb), scroll: [mb.scrollHeight, mb.clientHeight],
    start: st ? {...r(st), inside: inside(r(st))} : null, plate: pl ? {...r(pl), inside: inside(r(pl))} : null,
    buttons: [...mb.querySelectorAll('button')].map(b => ({t: b.textContent.trim().slice(0, 30), ...r(b), inside: inside(r(b))}))}; };
document.querySelector('#fleet .pcard').click(); await sleep(400);
for(let i = 0; i < 20 && !document.querySelector('#files .frow'); i++) await sleep(100);
const rr = id => { const e = document.getElementById(id); return e ? r(e) : null; };
R.detail = {on: document.getElementById('printer').classList.contains('on'), title: document.getElementById('p-title').textContent,
  docW: document.documentElement.scrollWidth, docH: document.documentElement.scrollHeight, board: rr('printer'), act: rr('p-act'), cam: rr('cam'), panel: rr('p-panel'),
  actButtons: [...document.querySelectorAll('#p-act button')].filter(b => getComputedStyle(b).display !== 'none').map(b => ({t: b.textContent.trim().replace(/\s+/g, ' ').slice(0, 24), h: r(b).h, inside: inside(r(b))}))};
// the files list is a full-screen overlay on 5in, opened from the strip, one Print button per row
document.getElementById('p-filesbtn').click(); await sleep(150);
R.files = {open: document.getElementById('printer').classList.contains('files-open'), docW: document.documentElement.scrollWidth, docH: document.documentElement.scrollHeight,
  rows: [...document.querySelectorAll('#files .frow')].map(row => ({cur: row.classList.contains('cur'), inside: inside(r(row)), h: r(row).h, tag: (row.querySelector('.nowtag') || {textContent: ''}).textContent,
    buttons: [...row.querySelectorAll('button')].filter(b => getComputedStyle(b).display !== 'none').map(b => ({t: b.textContent.trim(), h: r(b).h, inside: inside(r(b))}))}))};
if(typeof filesClose === 'function') filesClose(); else document.getElementById('printer').classList.remove('files-open');
askCancel(); await sleep(100); R.cancel = DLG(); closeModal();
SUG_MULTI = false; await askPrint('Bracket x12.gcode.3mf'); await sleep(200); R.confirm = DLG();
document.getElementById('plateok').click(); R.confirmArmed = {disabled: document.getElementById('startbtn').disabled}; closeModal();
SUG_MULTI = true; await askPrint('Two colour lid.gcode.3mf'); await sleep(200); R.multi = DLG();
document.getElementById('plateok').click(); R.multiArmed = {disabled: document.getElementById('startbtn').disabled};
// the sticky footer: scroll the sheet to the top and to the bottom, START must be on screen both times
const mb = document.getElementById('mbody');
mb.scrollTop = 0; await sleep(50); R.multiTop = DLG(); mb.scrollTop = 9999; await sleep(50); R.multiBottom = DLG();
closeModal();
askTray(0, 'PLA'); await sleep(50); R.tray = DLG(); closeModal();
R.alerts2 = window.__alerts.slice();
"""

NOPARAM = r"""
const mk = (printer, state) => ({printer, groups: ['Home'], connected: true, state, percent: 10, remaining_min: 60, job: 'x', nozzle_c: 1, bed_c: 1, camera: true});
window.__stubs.push((u, m) => {
  if(u === '/api/fleet') return J([mk('A', 'RUNNING'), mk('B', 'IDLE'), mk('C', 'IDLE'), mk('D', 'IDLE')]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  return null;
});
await sleep(300); FL.sig = null; await fleet(); await sleep(50);
R.cls = document.documentElement.className;
R.l5 = L5;
R.n = document.getElementById('fleet').dataset.n === undefined ? null : document.getElementById('fleet').dataset.n;
R.cams = document.querySelectorAll('#fleet .cardcam').length;
R.vw = innerWidth; R.vh = innerHeight;
R.moreVisible = getComputedStyle(document.getElementById('morebtn')).display;
R.more5 = [...document.querySelectorAll('.more5')].map(b => getComputedStyle(b).display);
R.hdr = [...document.querySelectorAll('#home > .row:first-child > button')].map(b => [b.textContent.trim(), getComputedStyle(b).display]);
R.cols = new Set([...document.querySelectorAll('#fleet .pcard')].map(c => Math.round(c.getBoundingClientRect().left))).size;
"""


WRAP = """<!doctype html><html><body style="margin:0;background:#000"><iframe id="f" src="%(src)s" style="width:%(w)dpx;height:%(h)dpx;border:0;display:block"></iframe>
<script>const f = document.getElementById('f'); let n = 0;
const t = setInterval(() => { n++; try { const v = f.contentDocument.documentElement.getAttribute('data-result');
  if(v){ document.documentElement.setAttribute('data-result', v); clearInterval(t); }
  else if(n > 2000){ document.documentElement.setAttribute('data-result', JSON.stringify({error: 'the driver did not finish'})); clearInterval(t); } }
  catch(e){ document.documentElement.setAttribute('data-result', JSON.stringify({error: 'iframe: ' + e})); clearInterval(t); } }, 100);</script></body></html>"""


# What GitHub Actions' headless Chrome on Ubuntu reports: pointer "none" and hover "none" (no fine pointer, no hover), and no touch.
NO_POINTER = "primaryPointerType=1,availablePointerTypes=1,primaryHoverType=1,availableHoverTypes=1"


def run_q(testcase, driver, query, size, budget=60000, bundled_fonts=False, blink_settings=""):
    """tests/test_fix_dashboard.run_page, but the page sits in an iframe of EXACTLY `size` (headless Chrome's own window is a few dozen px
    short of --window-size, so the viewport the page sees is made explicit) and its URL carries a query string.
    `blink_settings` is passed to Chrome as --blink-settings (see NO_POINTER)"""
    import test_fix_dashboard as tfd
    if not tfd.CHROME:
        testcase.skipTest("no Chrome / Edge found for the browser tests")
    d = tempfile.mkdtemp(prefix="dash5in-chrome-")
    testcase.addCleanup(shutil.rmtree, d, True)
    page = os.path.join(d, "page.html")
    html = tfd.build_page(driver)
    if bundled_fonts:      # the page is opened from file://, where the absolute /static/brand/ urls resolve to nothing: point them at the repo's own woff2 files
        html = html.replace("url(/static/brand/", "url(file://" + os.path.join(HERE, "..", "app", "static", "brand") + "/")
    with open(page, "w", encoding="utf8") as fh:
        fh.write(html)
    wrap = os.path.join(d, "wrap.html")
    with open(wrap, "w", encoding="utf8") as fh:
        fh.write(WRAP % {"src": "file://" + page + query, "w": size[0], "h": size[1]})
    cmd = [tfd.CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
           "--allow-file-access-from-files", "--force-device-scale-factor=1", "--user-data-dir=" + os.path.join(d, "profile"),
           "--window-size=%d,%d" % (max(size[0], 1024), size[1] + 200), f"--virtual-time-budget={budget}", "--dump-dom", "file://" + wrap]
    if blink_settings:
        cmd.insert(-1, "--blink-settings=" + blink_settings)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    m = re.search(r"data-result='(.*?)'", r.stdout, re.S) or re.search(r'data-result="(.*?)"', r.stdout, re.S)
    testcase.assertIsNotNone(m, "the driver did not finish: " + r.stderr[-500:] + r.stdout[-300:])
    txt = m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    return json.loads(txt)


class FiveInBrowserTests(unittest.TestCase):
    R = None
    W, H = 800, 480

    def result(self):
        if FiveInBrowserTests.R is None:
            FiveInBrowserTests.R = run_q(self, DRIVER, "?group=Home&layout=5in", (self.W, self.H), 120000)
        return FiveInBrowserTests.R

    def test_ran_clean_at_800x480(self):
        R = self.result()
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        self.assertEqual(R["alerts2"], [])
        self.assertEqual((R["first"]["vw"], R["first"]["vh"]), (800, 480))

    def test_param_parsing(self):
        R = self.result()
        self.assertEqual(R["parse"], [True, True, True, True, True, False, False, False, False, False, False, False])
        self.assertEqual(R["parseNull"], [False, False])

    def test_the_class_is_applied_from_the_param_and_the_grid_is_sized_by_count(self):
        R = self.result()
        self.assertEqual(R["first"]["cls"], "lay-5in")
        self.assertEqual(R["first"]["n"], "4")
        self.assertEqual(R["two"]["n"], "2")
        self.assertEqual(R["six"]["n"], "6")
        self.assertEqual(R["nine"]["n"], "many")

    def test_four_printers_are_a_2x2_grid_with_no_page_scroll(self):
        S = self.result()["first"]
        self.assertEqual(S["names"], ["Printer-A", "Printer-B", "Printer-C", "Printer-D"])
        self.assertEqual((S["cols"], S["rows"]), (2, 2))
        self.assertTrue(S["allInside"], S["cards"])
        self.assertLessEqual(S["docH"], S["vh"])
        self.assertLessEqual(S["docW"], S["vw"])
        self.assertEqual(S["fleetScroll"][0], S["fleetScroll"][1])         # the grid itself does not scroll either
        self.assertEqual(S["fleet"]["b"], 470)                               # header 52 . grid . 10px of margin: one fixed 800x480 page

    def test_one_or_two_printers_are_one_row_of_taller_cards(self):
        S = self.result()["two"]
        self.assertEqual((S["cols"], S["rows"]), (2, 1))
        self.assertTrue(S["allInside"])
        self.assertGreater(min(c["h"] for c in S["cards"]), 400)            # taller than the 204px 2x2 cards
        self.assertLessEqual(S["docH"], S["vh"])
        self.assertEqual(self.result()["two"]["fleetScroll"][0], self.result()["two"]["fleetScroll"][1])

    def test_five_or_more_printers_keep_their_card_size_and_the_grid_scrolls_not_the_page(self):
        R = self.result()
        for k, rows in (("six", 3), ("nine", 5)):
            S = R[k]
            self.assertEqual((S["cols"], S["rows"]), (2, rows), k)
            self.assertEqual({c["h"] for c in S["cards"]}, {204}, k)         # cards never shrink ...
            self.assertGreater(S["fleetScroll"][0], S["fleetScroll"][1], k)   # ... the grid scrolls inside itself
            self.assertLessEqual(S["docH"], S["vh"], k)
            self.assertLessEqual(S["fleet"]["b"], S["vh"], k)
        self.assertEqual(R["six"]["n"], "6")

    def test_every_card_has_the_same_picture_slot_and_says_in_words_when_there_is_no_camera(self):
        R = self.result()
        for k in ("first", "two", "six", "nine", "fourAgain"):
            self.assertEqual(R[k]["slots"], len(R[k]["names"]), k)             # the same slot on every card
        for k in ("first", "six", "nine", "fourAgain"):
            self.assertEqual({(b["w"], b["h"]) for b in R[k]["slotBoxes"]}, {(168, 94)}, k)
        for b in R["two"]["slotBoxes"]:                                          # 1-2 printers: a bigger 16:9 picture
            self.assertAlmostEqual(b["w"] / b["h"], 16 / 9, delta=0.05)
        first = R["first"]
        self.assertEqual(first["slotCams"], 2)                              # the two printers with a camera get a still ...
        self.assertEqual(first["slotText"], ["", "", "NO CAMERA", "NO CAMERA"])   # ... the others say so in words, not a blank box
        self.assertEqual(R["six"]["slotText"], ["NO CAMERA"] * 6)            # (the "six" fixture has no cameras at all)

    def test_names_and_the_key_number_keep_their_size_and_fit_their_slots(self):
        R = self.result()
        for k in ("first", "two", "six", "nine"):
            for f in R[k]["fonts"]:
                # Inter is not loaded here (file:// page), so a wider fallback font (DejaVu / Liberation on Linux) may need the key number
                # and the name to shrink to their slot: they never grow past, and never cut below, their floor. With the bundled Inter
                # (FiveInBundledFontTests) they keep exactly 40 / 22.
                self.assertLessEqual(f["name"], 22, (k, f))
                self.assertGreaterEqual(f["name"], 16, (k, f))
                self.assertLessEqual(f["big"], 40, (k, f))
                self.assertGreaterEqual(f["big"], 24, (k, f))
                self.assertGreaterEqual(f["sub"], 13)
                self.assertGreaterEqual(f["spag"], 12)
            self.assertEqual([x for row in R[k]["overflowX"] for x in row if x in ("pname", "big")], [], k)
        self.assertEqual(R["first"]["bigs"][0], "42%")                      # Printer-A is printing: big percent ...
        self.assertRegex(R["first"]["subs"][0], r"\d{1,2}:\d\d [AP]M \u00b7 1h 56m left")   # ... "~1:26 PM . 1h 56m left"

    def test_state_is_words_on_every_card_and_a_paused_print_shows_how_far_it_got(self):
        first = self.result()["first"]
        self.assertEqual(first["chips"], ["PRINTING", "PAUSED", "IDLE (done)", "OFFLINE"])
        self.assertEqual(first["kinds"], ["printing", "paused", "idle", "error"])
        self.assertEqual(first["bigs"], ["42%", "63%", "IDLE", "OFFLINE"])
        self.assertEqual(first["barw"], ["42%", "63%", "0%", "0%"])
        self.assertTrue(all(s.startswith("Spaghetti: ") for s in first["spags"]), first["spags"])

    def test_the_header_is_one_slim_row_and_the_pages_move_into_the_menu(self):
        R = self.result()
        H = R["first"]["header"]
        self.assertGreaterEqual(H["h"], 44)
        self.assertLessEqual(H["b"], 52)
        shown = {b["t"] for b in R["first"]["hdrButtons"] if b["shown"]}
        self.assertEqual(shown, {"More \u25be"})
        for b in R["first"]["hdrButtons"]:
            if b["shown"]:
                self.assertGreaterEqual(b["h"], 44, b)
                self.assertGreaterEqual(b["w"], 44, b)
        self.assertTrue(R["more"]["on"])
        self.assertEqual(R["more"]["expanded"], "true")
        self.assertEqual([b["t"] for b in R["more"]["buttons"]], ["Printers", "Library", "History", "Analytics", "Reload", "Backups", "Settings", "Close"])
        for b in R["more"]["buttons"]:
            self.assertGreaterEqual(b["h"], 44)
            self.assertTrue(b["inside"], b)
        self.assertTrue(R["moreClosed"])
        self.assertEqual(R["went"], ["openPrinters", "openLibrary", "openHistory", "openAnalytics", "openBackups", "openSettings"])
        self.assertTrue(R["wentClosed"])

    def test_group_chips_stay_one_compact_touchable_row(self):
        S = self.result()["first"]
        self.assertIsNotNone(S["chipsRow"])
        self.assertLessEqual(S["chipsRow"]["h"], 52)                        # inside the one 52px header row
        self.assertEqual(S["chipsRow"]["t"] >= 0 and S["chipsRow"]["b"] <= 52, True)
        self.assertTrue(all(h >= 40 for h in S["chipHeights"]), S["chipHeights"])

    def test_the_param_survives_a_group_chip_tap(self):
        R = self.result()
        self.assertEqual(R["chipsBefore"], ["All", "\u2713 Home", "Work"])   # the page was opened on ?group=Home: the active chip carries a tick (colorblind rule)
        t = R["tapWork"]
        self.assertEqual(t["names"], ["Printer-E"])
        self.assertEqual(t["cls"], "lay-5in")
        self.assertEqual(len(t["rs"]), 1)
        self.assertRegex(t["rs"][0], r"[?&]layout=5in")                    # ... the rewrite keeps layout ...
        self.assertRegex(t["rs"][0], r"[?&]group=Work")                    # ... and sets group
        # "All" clears group only
        self.assertEqual(len(R["tapAll"]["rs"]), 2)
        self.assertRegex(R["tapAll"]["rs"][1], r"[?&]layout=5in")
        self.assertNotIn("group", R["tapAll"]["rs"][1])
        self.assertEqual(R["tapAll"]["cls"], "lay-5in")

    def test_the_hub_down_banner_stays_visible_and_the_cards_keep_their_size(self):
        R = self.result()
        S = R["hubdown"]
        self.assertIsNotNone(S["hub"])
        self.assertIn("HUB NOT RESPONDING", S["hub"]["text"])
        self.assertGreaterEqual(S["hub"]["t"], S["header"]["b"] - 2)
        self.assertLessEqual(S["hub"]["b"], S["cards"][0]["t"])           # the banner sits above the cards, never over them
        self.assertLessEqual(S["hub"]["h"], 56)                           # at most two lines
        self.assertEqual({c["h"] for c in S["cards"]}, {204})             # the banner took height: the cards keep their size and the grid scrolls inside itself
        self.assertGreater(S["fleetScroll"][0], S["fleetScroll"][1])
        self.assertLessEqual(S["fleet"]["b"], S["vh"])
        self.assertLessEqual(S["docH"], S["vh"])
        self.assertTrue(all(c.startswith("LAST KNOWN: ") for c in S["chips"]), S["chips"])
        self.assertIsNone(R["hubback"]["hub"])                            # the next good answer clears it
        self.assertEqual(R["hubback"]["chips"], ["PRINTING", "PAUSED", "IDLE (done)", "OFFLINE"])
        self.assertTrue(R["hubback"]["allInside"])

    def test_the_printer_screen_is_one_fixed_800x480_board(self):
        D = self.result()["detail"]
        self.assertTrue(D["on"])
        self.assertEqual(D["title"], "Printer-A")
        self.assertLessEqual(D["docW"], 800)
        self.assertLessEqual(D["docH"], 480)                              # nothing scrolls
        self.assertEqual((D["board"]["w"], D["board"]["h"]), (800, 480))
        self.assertEqual(D["act"]["h"], 56)                               # the one action strip
        self.assertLessEqual(D["act"]["b"], 480)
        self.assertEqual(max(D["cam"]["w"], D["panel"]["w"]), 528)        # the camera (or, with none, the status panel in its place), 528 wide
        self.assertTrue(D["actButtons"], D)
        for b in D["actButtons"]:
            self.assertTrue(b["inside"], b)
            self.assertGreaterEqual(b["h"], 44, b)

    def test_the_files_list_is_a_full_screen_overlay_with_one_print_button_per_row(self):
        F = self.result()["files"]
        self.assertTrue(F["open"])
        self.assertLessEqual(F["docW"], 800)
        self.assertLessEqual(F["docH"], 480)
        self.assertEqual(len(F["rows"]), 2)
        cur, other = F["rows"]
        self.assertTrue(cur["cur"])
        self.assertEqual(cur["tag"], "Printing")                          # the file being printed says so instead of offering Print
        self.assertEqual(cur["buttons"], [])
        self.assertFalse(other["cur"])
        self.assertEqual(len(other["buttons"]), 1)                        # Rename / Delete sit behind the name (the sheet), not on the row
        self.assertGreaterEqual(other["buttons"][0]["h"], 44)
        for row in F["rows"]:
            self.assertTrue(row["inside"], row)

    def test_dialogs_keep_their_buttons_on_screen(self):
        R = self.result()
        for k in ("cancel", "confirm", "multi", "multiTop", "multiBottom", "tray"):
            self.assertTrue(R[k]["modalOn"], k)
            for b in R[k]["buttons"]:
                self.assertTrue(b["inside"], (k, b))
                self.assertGreaterEqual(b["b"] - b["t"], 44, (k, b))
        for k in ("confirm", "multi", "multiTop", "multiBottom"):
            self.assertTrue(R[k]["start"]["inside"], k)
            self.assertTrue(R[k]["plate"]["inside"], k)
        self.assertTrue(R["confirmArmed"]["disabled"] is False)
        self.assertTrue(R["multiArmed"]["disabled"] is False)

    def test_no_param_means_no_class_for_a_landscape_phone_sized_window(self):
        R = run_q(self, NOPARAM, "?group=Home", (800, 480))
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        self.assertEqual(R["cls"], "")
        self.assertIs(R["l5"], False)
        self.assertIsNone(R["n"])
        self.assertEqual(R["cams"], 4)                         # the phone / kiosk tiles still carry their camera
        self.assertEqual(R["moreVisible"], "block")            # ... and the phone header (More menu) is the phone one,
        self.assertEqual([h for h in R["hdr"] if h[1] == "none" and h[0] in ("Printers", "Library")], [])   # which keeps Printers + Library
        self.assertEqual(R["more5"], ["none", "none", "none"])  # the 5in-only menu entries (Printers, Library, Reload) stay hidden

    def test_no_param_on_the_wall_kiosk_and_a_phone_either(self):
        for size in ((1024, 600), (390, 844)):
            R = run_q(self, NOPARAM, "", size)
            self.assertEqual(R["cls"], "", size)
            self.assertIs(R["l5"], False)
            self.assertIsNone(R["n"])
            self.assertEqual(R["cams"], 4)
            self.assertEqual(R["more5"], ["none", "none", "none"])
        R = run_q(self, NOPARAM, "?layout=7in&layout=", (800, 480))
        self.assertEqual(R["cls"], "")


class FiveInBundledFontTests(unittest.TestCase):
    """The same fixtures with the repo's own Inter / JetBrains Mono / Antonio files loaded: the key number and the name keep their full size
    and fit, the same on macOS and on Linux."""
    R = None

    def result(self):
        if FiveInBundledFontTests.R is None:
            FiveInBundledFontTests.R = run_q(self, DRIVER + "\nawait document.fonts.ready; await new Promise(r => setTimeout(r, 300)); R.interLoaded = document.fonts.check('800 40px Inter');",
                                             "?group=Home&layout=5in", (800, 480), 120000, bundled_fonts=True)
        return FiveInBundledFontTests.R

    def test_names_and_the_key_number_keep_their_full_size_with_the_bundled_fonts(self):
        R = self.result()
        self.assertNotIn("error", R, R.get("error"))
        self.assertTrue(R["interLoaded"], "the bundled Inter did not load")
        for k in ("first", "two", "six", "nine"):
            for f in R[k]["fonts"]:
                self.assertEqual((f["name"], f["big"]), (22, 40), (k, f))
            self.assertEqual([x for row in R[k]["overflowX"] for x in row if x in ("pname", "big")], [], k)


if __name__ == "__main__":
    unittest.main()
