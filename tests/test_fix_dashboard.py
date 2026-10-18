"""Tests for the dashboard hardening (HARDENING-REVIEW-2026-10-11, P0-6 + P1-14 + kiosk/dashboard items): every
untrusted string is escaped, no untrusted text reaches an inline handler, and the kiosk says so in words when the
Hub stops answering.

    python3 tests/test_fix_dashboard.py

Two kinds of test:
  * static: the HTML is read as text (needs nothing but node, for the syntax check);
  * browser: the REAL dashboard.html is loaded into headless Chrome / Edge with a stubbed `fetch` that serves
    hostile data (the payloads below) and a driver script that walks the screens. Skipped when no browser is
    found. Nothing here starts the Hub, opens a socket, or contacts a printer or the LAN: the page never leaves
    the process (file:// + a fake fetch) and the profile is a throw-away temp directory.
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
APP = os.path.join(HERE, "..", "app")
DASH = os.path.join(APP, "dashboard.html")

with open(DASH, encoding="utf8") as _fh:
    HTML = _fh.read()
BLOCKS = re.findall(r"<script>(.*?)</script>", HTML, re.S)

CHROME = next((p for p in (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
    shutil.which("microsoft-edge")) if p and os.path.exists(p)), None)

P1 = '"><img src=x onerror=alert(1)>'
P2 = "'));alert(1);//"
P3 = "</script><img src=x onerror=alert(1)>"


# ====================================================================== static

class TestStatic(unittest.TestCase):
    def test_scripts_parse_together(self):
        """One program: catches a const declared in two blocks, which per-block checks miss."""
        if not shutil.which("node"):
            self.skipTest("node not installed")
        d = tempfile.mkdtemp(prefix="dashfix-js-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "all.js")
        with open(p, "w", encoding="utf8") as fh:
            fh.write("\n;\n".join(BLOCKS))
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_one_escape_function_and_its_aliases(self):
        self.assertEqual(len(re.findall(r"^const esc = ", HTML, re.M)), 1)
        self.assertIn("const escAttr = esc, pmEsc = esc, bkEsc = esc;", HTML)
        # quotes, angle brackets, ampersand AND the backtick are all handled
        m = re.search(r"^const esc = .*$", HTML, re.M).group(0)
        for ch in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;", "&#96;"):
            self.assertIn(ch, m)
        # the old separate definitions are gone
        self.assertNotRegex(HTML, r"const (pmEsc|bkEsc) = s =>")

    def test_error_card_escapes_its_own_text(self):
        self.assertIn("<h2>${esc(title)}</h2><div class=\"note\">${esc(msg)}</div>", HTML)
        # ... so no caller may pre-escape (that would show &amp; to the user)
        self.assertNotRegex(HTML, r"errorCard\([^;\n]*pmEsc\(")

    # Inline handlers that still interpolate: each is a constant string or an integer-checked number.
    SAFE_HANDLER_INTERPOLATIONS = {
        'onclick="${js}"',                                    # pmBtn(label, js): callers pass constants only
        'onclick="${on}"',                                    # ctl btn(): constants like ctlDo('light_on')
        "onclick=\"pmBackToForm(true, ${skip ? 'true' : 'false'})\"",
        "onclick=\"ctlDo('load',${slot ? '{tray:' + slot + '}' : 'null'})\"",   # slot is Number.isInteger-checked
    }

    def test_no_untrusted_text_in_inline_handlers(self):
        found = set()
        for m in re.finditer(r"""\bon[a-z]+=(["'])((?:(?!\1).)*)\1""", HTML):
            if "${" in m.group(2):
                found.add(m.group(0))
        self.assertEqual(found, self.SAFE_HANDLER_INTERPOLATIONS)

    def test_no_single_quoted_handlers_and_no_string_splicing(self):
        self.assertNotRegex(HTML, r"""\bon[a-z]+='""")
        # '${...}' or '"+x+"' spliced into a JS string inside a handler attribute
        self.assertNotRegex(HTML, r"""onclick="[^"]*'\$\{""")
        self.assertNotIn("askPrint('${", HTML)
        self.assertNotIn("askTray(${t.tray},'", HTML)
        self.assertNotIn("startSelected(${JSON.stringify", HTML)

    def test_delegated_actions_carry_data_in_attributes(self):
        for act in ("openPrinter", "tray", "trayMat", "trayColor", "trayBack", "pickSpool", "start", "delete", "bkStart", "stTest"):
            self.assertRegex(HTML, rf'data-act="{act}"', act)
            self.assertRegex(HTML, rf"\b{act}:\s+(b|\(\))\s+=>", act)
        self.assertIn("e.target.closest('[data-act]')", HTML)

    # Names of the things that come from outside. Any ${...} that mentions one must go through esc()/pmEsc()/bkEsc().
    UNTRUSTED = (r"\b(job|jobs?_name|file|filename|fileShort|shortName|name|label|hostname|model|serial|address|url|type|color_name|"
                 r"user_agent|remote_addr|detail|browser|message|error|e\.message|summary|title|text|note|notes|blocked|"
                 r"mapping_described|printer|path|reason|CUR|brand|prefix|material|result|state_after_8s)\b")

    def _unescaped(self):
        out = []
        for n, line in enumerate(HTML.split("\n"), 1):
            for m in re.finditer(r"\$\{", line):
                i, depth, j = m.end(), 1, m.end()
                while j < len(line) and depth:
                    depth += {"{": 1, "}": -1}.get(line[j], 0)
                    j += 1
                expr = line[i:j - 1]
                if re.fullmatch(r"\s*(esc|pmEsc|bkEsc)\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)\s*", expr):
                    continue
                out.append((n, expr))
        return out

    # Reviewed and safe: the expression only ever holds a constant, a number, or text that was escaped inside it.
    REVIEWED = (
        r"^js$", r"^label$", r"^cls$", r"^chip\[[01]\]$", r"^info$", r"^extra$", r"^rows$", r"^id$", r"^CUR$",
        r"^req$", r"^input$", r"^fields$", r"^banner$", r"^steps$", r"^notes$", r"^head$", r"^intro$", r"^help$",
        r"^inner$", r"^state$", r"^act$", r"^doubt$", r"^tip$", r"^k$", r"^v$", r"^m$", r"^unit$", r"^type$",
        r"^busy$", r"^dis$", r"^dl$", r"^on$", r"^thumb$", r"^warn$", r"^fam$", r"^modelHtml$", r"^spoolHtml$",
        r"^camHtml$", r"^errHtml$", r"^picHtml$", r"^badgeBlock$", r"^soon$", r"^cls \|\| ''$", r"^hiChip\(j\.result\)$", r"^SEC_MODES\.map\(",
    )

    def test_every_interpolation_that_names_untrusted_data_is_escaped(self):
        bad = []
        for n, expr in self._unescaped():
            e = expr.strip()
            if any(re.match(p, e) for p in self.REVIEWED):
                continue
            # a ternary / concat whose untrusted parts are escaped inside is fine; flag raw mentions only
            stripped = re.sub(r"(esc|pmEsc|bkEsc)\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)", "ESC", e)
            if re.search(r"\.map\([^`]*=>\s*`", stripped):                 # a template inside: its own ${} are checked separately
                stripped = stripped.split("`", 1)[0]
            stripped = re.sub(r"(encodeURIComponent|pu)\([^()]*\)", "ENC", stripped)
            stripped = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "''", stripped)
            stripped = re.sub(r"\.map\([^)]*?=>", ".map(", stripped)          # the loop variable's name is not output
            while " ? " in stripped:                                           # the condition of a ternary is not output
                stripped = stripped.split(" ? ", 1)[1]
            if re.search(self.UNTRUSTED, stripped):
                bad.append((n, e[:100]))
        # tray / slot numbers: integers by construction (Number.isInteger filter), shown as digits
        bad = [b for b in bad if not re.match(r"(t\.tray|tray\+?1?|tray ?\+ ?1)", b[1])]
        self.assertEqual(bad, [], "unescaped interpolations of untrusted-looking data")

    def test_no_dynamic_html_sinks_other_than_innerhtml(self):
        for sink in ("insertAdjacentHTML", "document.write", "outerHTML", "srcdoc", "eval(", "new Function"):
            self.assertNotIn(sink, HTML, sink)
        self.assertNotRegex(HTML, r"setTimeout\(['\"`]")
        self.assertNotRegex(HTML, r"\.innerHTML\s*\+=")

    def test_download_links_are_same_origin_paths_only(self):
        self.assertIn("const safeHref = ", HTML)
        self.assertIn('href="${esc(safeHref(latest.download))}"', HTML)

    def test_printer_names_are_url_encoded_in_paths(self):
        self.assertNotRegex(HTML, r"""['"`]/api/printer/['"`]\s*\+\s*CUR""")
        self.assertNotIn("/api/printer/${CUR}", HTML)
        self.assertIn("const papi = (name, tail) => '/api/printer/' + pu(name)", HTML)

    def test_pinned_wiring_other_suites_rely_on_is_kept(self):
        for needle in ('START A PRINT ON <span class="cfprn">${CUR}</span>?', ">START PRINT ON ${CUR}</button>",
                       "Live view of ${CUR} &mdash; check the bed is clear", "confirm += '|model_override'",
                       "model_override:override", "const CF_MS = 3000", "function cfStop", "pmEsc(d.model_warning",
                       'data-ctl class="primary" onclick="askPrint(', "No camera on this printer"):
            self.assertIn(needle, HTML, needle)

    def test_the_csp_still_fits(self):
        """Nothing new loads from outside: no foreign scripts, styles, frames, sockets or absolute fetches."""
        for pat in (r"""<script[^>]+src=["']?(https?:)?//""", r"""<link[^>]+href=["']?(https?:)?//""", r"""@import""",
                    r"""url\(\s*["']?(https?:)?//""", r"""<iframe""", r"""fetch\(\s*["'`]https?:""",
                    r"""new WebSocket""", r"""new EventSource"""):
            self.assertIsNone(re.search(pat, HTML), pat)

    def test_kiosk_offline_markup_and_rules(self):
        self.assertEqual(HTML.count('class="hubdown"'), 2)            # one above the fleet grid, one on the printer screen
        self.assertRegex(HTML, r'<div class="hubdown"[^>]*hidden></div>\s*<div id="fleet"></div>')   # banner is OUTSIDE the grid
        self.assertIn("const HUB_GRACE_MS = 15000", HTML)
        self.assertIn("HUB NOT RESPONDING", HTML)
        self.assertIn("'LAST KNOWN: '", HTML)
        self.assertIn("data may be out of date", HTML.replace("data below may be out of date", "data may be out of date"))
        # every poll has a hard timeout and one request in flight
        self.assertIn("AbortController", re.search(r"async function pollJson.*?\n}\n", HTML, re.S).group(0))
        self.assertIn("if(FL.busy)", HTML)
        self.assertIn("if(PV.busy)", HTML)
        # the card geometry rules are untouched by the stale state (it only changes chip text size, never card size)
        stale = re.findall(r"#fleet[^{]*\.stale[^{]*\{[^}]*\}", HTML)
        self.assertTrue(stale)
        for rule in stale:
            self.assertNotRegex(rule, r"(grid-template|min-height|height:|padding:\s*\d+px\s*;|width:\s*\d)(?!.*chip)" if ".chip" not in rule and ".pname" not in rule else r"(?!)")

    def test_start_sends_the_suggested_single_use_ticket_when_there_is_one(self):
        self.assertIn("suggestedConfirm.indexOf('ph-') === 0", HTML)
        self.assertIn("const confirm = ticket ? suggestedConfirm :", HTML)

    def test_uiv_is_read_at_start_not_a_minute_late(self):
        self.assertRegex(HTML, r"uivCheck\(\);\s+// remember the build")

    def test_timers_survive_a_render_error(self):
        self.assertIn("try{ if(bkShowing()) bkRender(st); }catch(e)", HTML)
        self.assertIn("try{ pmRenderList(); }catch(e)", HTML)
        self.assertIn("try{ hiRender(); }catch(e)", HTML)


# ====================================================================== browser

PREAMBLE = r"""
window.__alerts = []; window.alert = function(m){ window.__alerts.push(String(m)); };
window.__calls = []; window.__down = false; window.__stubs = [];
window.__J = (o, s) => new Response(JSON.stringify(o), {status: s || 200, headers: {'Content-Type': 'application/json'}});
window.fetch = async function(u, init){
  u = String(u); const m = (init && init.method) || 'GET';
  window.__calls.push(m + ' ' + u + (init && init.body ? ' ' + init.body : ''));
  if(window.__down && u.indexOf('/api/') === 0) throw new TypeError('Failed to fetch');
  for(const f of window.__stubs){ const r = f(u, m, init); if(r) return r; }
  return window.__J({error: 'not stubbed: ' + u}, 404);
};
"""

# the shared hostile fixtures
FIXTURE = r"""
const P1 = '"><img src=x onerror=alert(1)>', P2 = "'));alert(1);//", P3 = '<\/script><img src=x onerror=alert(1)>';   // \/ so the payload cannot close the driver's own <script> tag
const sleep = ms => new Promise(r => setTimeout(r, ms));
const J = window.__J;
const injected = () => document.querySelectorAll('img[src="x"], img[onerror], svg[onload], iframe').length;
"""


def build_page(driver):
    """dashboard.html with the fake fetch first in <head> and the driver last in <body>."""
    head = HTML.replace("<head>", "<head><script>" + PREAMBLE + "</script>", 1)
    drv = ("<script>(async () => { const R = {}; try { " + FIXTURE + driver +
           " } catch(e) { R.error = String(e && e.stack || e); }"
           " R.alerts = window.__alerts; document.documentElement.setAttribute('data-result', JSON.stringify(R)); })();</script></body>")
    idx = head.rindex("</body>")
    return head[:idx] + drv + head[idx + len("</body>"):]


def run_page(testcase, driver, budget_ms=60000):
    if not CHROME:
        testcase.skipTest("no Chrome / Edge found for the browser tests")
    d = tempfile.mkdtemp(prefix="dashfix-chrome-")
    testcase.addCleanup(shutil.rmtree, d, True)
    page = os.path.join(d, "page.html")
    with open(page, "w", encoding="utf8") as fh:
        fh.write(build_page(driver))
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
           "--disable-extensions", "--user-data-dir=" + os.path.join(d, "profile"),
           "--window-size=1024,600", f"--virtual-time-budget={budget_ms}", "--dump-dom", "file://" + page]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    m = re.search(r"data-result='(.*?)'", r.stdout, re.S) or re.search(r'data-result="(.*?)"', r.stdout, re.S)
    testcase.assertIsNotNone(m, "the driver did not finish: " + r.stderr[-500:] + r.stdout[-300:])
    txt = m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    return json.loads(txt)


XSS_DRIVER = r"""
const job = i => ({id: i, printer: P1, file: P3, result: P1, started_at: 1.7e9, ended_at: 1.7e9 + 3600, parts: 1, grams_used: 5,
  duration_s: 3600, cost: {total: 1.5}, open: false, material: P1, error: P1, filaments: [{type: P1, used_g: 3}], pauses: 0, percent: 100,
  est_seconds: 3000, time_vs_estimate_pct: 5, grams_est: 5});
const files = [{name: 'a' + P1 + '.gcode.3mf', print_time: '1h', weight_g: 5, filaments: [{color_name: P1, type: P2}]},
               {name: "Bob's " + P2 + " \u{1F600} é中 & </b>.gcode.3mf", print_time: P1, weight_g: 5, filaments: [{color_name: 'RED', type: P3}], thumb: P1}];
const found = [{id: P1, family: 'bambu', label: P3, model: P1, hostname: P2, address: P1, url: P3, serial: P1, confidence: 'low',
                already_added: false, prefill: {values: {name: P1, url: P3}}}];
const trays = [{tray: 0, slot_1based: 1, type: P2, color_name: P1}, {tray: 1, slot_1based: 2, type: P3, color_name: P3}];
const snap = state => ({snapshot: {printer: P1, connected: true, state, job: P3, percent: 5, layer: 2, nozzle_c: 1, bed_c: 1, nozzle_target_c: 0, bed_target_c: 0},
  trays, controls: {actions: []}, kind: 'bambu', materials: [P1, 'PLA'], palette: [P2, 'RED']});
let state = 'RUNNING';
window.__stubs.push((u, m) => {
  if(u === '/api/fleet') return J([{printer: P1, connected: true, state, percent: 50, remaining_min: 60, job: P3 + P1, nozzle_c: 200, bed_c: 60, camera: false}]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: [
    {id: 'channels', group: 'g' + P1, label: P1, description: P3, help: P3, test: P2 + P1, fields: [
      {key: 'k1', name: 'a', label: P1, help: P3, type: 'string', value: P1, source: 'env', env: P1, placeholder: P1, section: 'channels'},
      {key: 'k2', name: 'b', label: P1, help: P3, type: 'int', value: 5, min: P1, max: P3, unit: P1, section: 'channels'},
      {key: 'k3', name: 'c', label: P1, type: 'enum', value: 'a', options: [{value: P1, label: P3}, {value: 'a', label: P2}], section: 'channels'},
      {key: 'k4', name: 'd', label: P1, type: 'info', value: P3, section: 'channels'},
      {key: 'k5', name: 'e', label: P1, type: 'multienum', value: [P1], options: [{value: P1, label: P3}], section: 'channels'},
      {key: 'k6', name: 'f', label: P1, type: 'presets', value: {[P1]: {nozzle_c: 200, bed_c: 60}}, section: 'channels'}]}]});
  if(u === '/api/printers') return J({printers: [{name: P1, type: 'bambu', type_label: P3, address: P2, model: P1, driver_available: true, loaded: true, secrets: {[P1]: 'set'}, values: {name: P1}}]});
  if(u.startsWith('/api/printer-types')) return J({types: [{type: 'bambu', label: P1, description: P3, fields: [{name: 'name', label: P1, type: 'text', help: P3, required: true, placeholder: P1, guide_step: P1}],
    setup_guide: {title: P1, intro: P3, steps: [{id: P1, title: P1, text: P3}], notes: [{id: P2, kind: 'important', title: P1, text: P3}]}}]});
  if(u.startsWith('/api/discover/suggestions')) return J({networks: [P1], sources: {[P1]: P3}, note: P3});
  if(u === '/api/discover' && m === 'POST') return J({scan_id: P1, total: 3, networks: [P1]});
  if(u.startsWith('/api/discover/')) return J({state: 'done', finished: true, done: 3, total: 3, found, networks: [P1], error: P1});
  if(u.startsWith('/api/stats')) return J({jobs: 1, finished: 1, failed: 0, cancelled: 0, unknown: 0, running: 0, success_rate: 1, grams_used: 5, by_material: {[P1]: 5},
    by_printer: {[P1]: {jobs: 1, finished: 1, failed: 0, cancelled: 0, success_rate: 1, hours: 1, cost_total: 1}},
    by_file: [{file: P3, jobs: 1, success_rate: 1, good_parts: 1, true_cost_per_part: 1, clean_cost_per_part: 1}], cost: {total: 1.5, wasted_on_failures: 0}, currency: P1, since: null});
  if(u.startsWith('/api/jobs/')) return J(job(7));
  if(u.startsWith('/api/jobs')) return J({jobs: [job(P1)]});
  if(u === '/api/notifications/recent') return J({channels: [P1], notifications: [{title: P1, event: P3, printer: P1, time: 1.7e9, sent: false, skipped: P1, channels: {[P1]: {error: P3}}}]});
  if(u === '/api/backups') return J({keep_per_printer: 2, running: [], recent: [], printers: [{name: P1, dir: P1}],
    backups: {[P1]: [{created: 'zzz' + P1, file_count: 1, size: 1, download: 'javascript:alert(1)'}]}});
  if(u === '/api/security') return J({mode: 'warn', dashboard_login: true, dashboard_password: 'set', viewer_password: 'not set', session_settings: {idle_hours: 4, max_days: 7, device_days: 90},
    connection: {secure: false}, key_counts: {active: 1}, keys: [{id: P1, name: P1, scope: P1, prefix: P1, created: P1, revoked: false}],
    warn: {recent_callers: [{user_agent: P1, remote_addr: P3, method: P1, path: P3, detail: P1, last_seen: P1, count: 1, allowed: true}]}, stats: {}});
  if(u === '/api/devices') return J({settings: {device_days: 90}, pairing: null, devices: [{id: P1, name: P1, current: false, role_words: P1, browser: P3, first_seen: P1, last_seen: P1, last_ip: P1, expires: P1}],
    sessions: [{id: P1, browser: P1, current: false, role_words: P1, started: P1, last_seen: P1, last_ip: P1, ends: P1}]});
  if(u.startsWith('/api/detection')) return J({enabled: true, mode: P1, detector: {state: 'down', error: P3, retry_in_s: P1, last_ms: P1}});
  if(/^\/api\/printer\/[^/]+\/files/.test(u)) return J(files);
  if(/^\/api\/printer\/[^/]+\/suggest$/.test(u)) return J({file: 'x' + P3 + '.gcode.3mf', ams_mapping: [0], confirm_string: 'CONFIRM-TOKEN', notes: [P1, P3], print_time: P1, weight_g: 3,
    mapping_described: P3, model_match: false, model_warning: P1, target_model_name: P1, printer_kind: 'bambu'});
  if(/^\/api\/printer\/[^/]+$/.test(u)) return J(snap(state));
  return null;
});
const step = k => { R[k] = {injected: injected(), alerts: window.__alerts.length}; };
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };

// positive control: the detector sees a real injection
document.body.insertAdjacentHTML('beforeend', '<img id="ctl" src=x onerror="alert(\'control\')">'); await sleep(200);
R.control = window.__alerts.slice(); window.__alerts.length = 0; document.getElementById('ctl').remove();

await wait(() => document.querySelector('.pcard')); step('fleet');
R.cardName = document.querySelector('.pcard').dataset.name; R.cardJob = document.querySelector('.pcard .job').textContent;
document.querySelector('.pcard').click(); await wait(() => document.querySelector('.frow') && $('p-trays').innerHTML); step('printerScreen');
R.title = $('p-title').textContent; R.trayMat = document.querySelector('#p-trays [data-act=tray]').dataset.mat;
R.fileData = [...document.querySelectorAll('.frow button.primary')].map(b => b.dataset.f);
R.fileNames = [...document.querySelectorAll('.frow .fname')].map(e => e.textContent);
// tray change
document.querySelector('#p-trays [data-act=tray]').click(); await sleep(100); step('trayStep1');
const curBtn = [...$('mbody').querySelectorAll('[data-act=trayMat]')].find(b => b.dataset.cur); R.curMat = curBtn && curBtn.dataset.cur;
$('mbody').querySelector('[data-act=trayMat]').click(); await sleep(100); step('trayStep2');
R.trayHeading = $('mbody').querySelector('h2').textContent;
$('mbody').querySelector('[data-act=trayColor]').click(); await sleep(300); step('trayDone');   // 404 stub -> error text echoed
closeModal();
// delete + cancel dialogs
[...document.querySelectorAll('.frow .fdelete')].find(b => b.dataset.f.startsWith('Bob')).click(); await sleep(100); step('delete');
R.delFile = DEL.file; closeModal();
askCancel(); step('cancel'); closeModal();
errorCard(P1, P3); step('errorCard'); closeModal();
// confirm dialog
document.querySelector('.frow .primary').click(); await wait(() => $('startbtn')); step('confirm');
R.startText = $('startbtn') && $('startbtn').textContent;
R.spoolTrays = [...document.querySelectorAll('#spoolgrid [data-tray]')].map(b => b.dataset.tray);
closeModal();
// the other pages
state = 'IDLE';
await openPrinters(); await sleep(300); step('printers');
await pmEdit(P1); await sleep(200); step('printerForm');
await pmAskRemove(P1); await sleep(100); step('printerRemove');
await pmAddStart(); await sleep(200); step('addPick');
await pmDiscover(); await sleep(300); pmDcScan(); await sleep(1500); step('discovery');
await pmAddFound(P1); await sleep(200); step('addFound');
await openHistory(); await sleep(500); step('history');
document.querySelector('#hi-body .tr.click').click(); await sleep(400); step('historyDetail'); closeModal();
await openBackups(); await sleep(500); step('backups');
R.bkHref = [...document.querySelectorAll('#bk-body .bkdl')].map(a => a.getAttribute('href')); closeModal();
ST.sec = null; await openSettings(); await sleep(500); step('settings');
for(const s of ST.data.sections) { stSelect(s.id); await sleep(100); }
step('settingsTabs');
ST.sec = 'security'; await openSettings(); await sleep(800); step('security');
stSelect('channels'); ST.sec = 'notifications'; stSelect('notifications'); await sleep(500); step('notifications');
"""


# the stale chip must stay a chip; the cards must not change size or x position
KIOSK_DRIVER = r"""
const names = ['P1S', 'A1', 'MK4', 'MK3', 'Core1', 'Voron', 'Rook'];
const fleet7 = () => names.map((n, k) => ({printer: n, connected: k !== 5, state: k % 3 === 0 ? 'RUNNING' : 'IDLE',
  job: k % 3 === 0 ? 'Bracket v' + k + ' long file name here for wrapping' : '', percent: 30 + k, remaining_min: 75 + k, nozzle_c: 200, bed_c: 60, camera: false}));
window.__stubs.push((u) => {
  if(u === '/api/fleet') return J(fleet7());
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: []});
  return null;
});
const rects = () => [...document.querySelectorAll('.pcard')].map(c => { const r = c.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.width), Math.round(r.height)]; });
const wait = async (f, n) => { for(let i = 0; i < (n || 100) && !f(); i++) await sleep(100); };
const banner = () => document.querySelector('#home .hubdown');
await wait(() => document.querySelectorAll('.pcard').length === 7);
R.cards = document.querySelectorAll('.pcard').length;
R.before = rects(); R.chipsBefore = [...document.querySelectorAll('.pcard .chip')].map(c => c.textContent);
R.c4 = document.getElementById('fleet').classList.contains('c4');
const topBefore = document.querySelector('.pcard').getBoundingClientRect().top;
R.bannerEarly = banner().hidden;
// the Hub goes away: nothing for 10 s must NOT trigger the banner (15 s rule) ...
window.__down = true; await sleep(10000); R.bannerAt10s = banner().hidden;
// ... but 20 s must
await sleep(10000);
R.bannerAt20s = {hidden: banner().hidden, text: banner().textContent};
R.chipsDown = [...document.querySelectorAll('.pcard .chip')].map(c => c.textContent);
R.after = rects(); R.topShift = Math.round(document.querySelector('.pcard').getBoundingClientRect().top - topBefore);
R.bannerOutsideGrid = !document.getElementById('fleet').contains(banner()) && banner().nextElementSibling === document.getElementById('fleet');
R.gridScrolls = document.getElementById('fleet').scrollHeight > document.getElementById('fleet').clientHeight;
R.staleClass = document.getElementById('fleet').classList.contains('stale');
// back again
window.__down = false; await sleep(7000);
R.bannerAfter = banner().hidden; R.chipsAfter = [...document.querySelectorAll('.pcard .chip')].map(c => c.textContent);
R.staleAfter = document.getElementById('fleet').classList.contains('stale');
R.leftoverLive = document.querySelectorAll('[data-live]').length;
// a Hub answering with an error page for >15 s is "not responding" too
window.__stubs.unshift((u) => u === '/api/fleet' ? J({error: 'boom'}, 500) : null);
await sleep(20000); R.bannerOn500 = banner().hidden; R.bannerOn500Text = banner().textContent;
"""


@unittest.skipUnless(CHROME, "no Chrome / Edge found for the browser tests")
class TestBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xss = None

    def xss_run(self):
        if TestBrowser.xss is None:
            TestBrowser.xss = run_page(self, XSS_DRIVER, 90000)
        return TestBrowser.xss

    def test_the_driver_finished_without_error(self):
        R = self.xss_run()
        self.assertNotIn("error", R, R.get("error"))

    def test_the_detector_works(self):
        self.assertEqual(self.xss_run()["control"], ["control"])

    def test_nothing_executes_and_nothing_is_injected_on_any_screen(self):
        R = self.xss_run()
        self.assertEqual(R["alerts"], [])
        steps = [k for k, v in R.items() if isinstance(v, dict) and "injected" in v]
        for want in ("fleet", "printerScreen", "trayStep1", "trayStep2", "trayDone", "delete", "cancel", "errorCard", "confirm",
                     "printers", "printerForm", "printerRemove", "addPick", "discovery", "addFound", "history", "historyDetail",
                     "backups", "settings", "settingsTabs", "security", "notifications"):
            self.assertIn(want, steps)
        for k in steps:
            self.assertEqual(R[k]["injected"], 0, k)
            self.assertEqual(R[k]["alerts"], 0, k)

    def test_hostile_text_is_shown_as_text_and_round_trips_through_data_attributes(self):
        R = self.xss_run()
        self.assertEqual(R["cardName"], P1)
        self.assertEqual(R["cardJob"], P3 + P1)
        self.assertEqual(R["title"], P1)
        self.assertEqual(R["trayMat"], P2)
        self.assertEqual(R["curMat"], P2)                                # "(current)" mark + Back keep the exact text
        self.assertEqual(R["delFile"], "Bob's " + P2 + " \U0001F600 é中 & </b>.gcode.3mf")
        self.assertIn("a" + P1 + ".gcode.3mf", R["fileData"])            # an apostrophe or quote no longer breaks Print / Delete
        self.assertIn("Bob's " + P2 + " \U0001F600 \u00e9\u4e2d & </b>", R["fileNames"])
        self.assertEqual(R["spoolTrays"], ["0", "1"])
        self.assertIn("START PRINT ON", R["startText"])
        self.assertIn(P1, R["startText"])

    def test_a_javascript_download_link_is_neutralised(self):
        self.assertEqual(self.xss_run()["bkHref"], ["#"])

    def test_kiosk_says_so_in_words_when_the_hub_stops_answering(self):
        R = run_page(self, KIOSK_DRIVER, 120000)
        self.assertEqual(R["cards"], 7)
        self.assertTrue(R["c4"])
        self.assertTrue(R["bannerEarly"])
        self.assertTrue(R["bannerAt10s"], "the banner must wait for 15 s of silence")
        b = R["bannerAt20s"]
        self.assertFalse(b["hidden"])
        self.assertTrue(b["text"].startswith("HUB NOT RESPONDING"), b["text"])
        self.assertIn("last update", b["text"])
        self.assertIn("data below may be out of date", b["text"])
        self.assertEqual(R["chipsDown"], ["LAST KNOWN: " + c for c in R["chipsBefore"]])
        self.assertTrue(R["staleClass"])
        self.assertTrue(R["bannerOutsideGrid"])
        self.assertTrue(R["gridScrolls"])
        # identical geography: same x, width and height for every card; the banner only pushes the grid down
        self.assertEqual(R["before"], R["after"])
        self.assertGreater(R["topShift"], 0)

    def test_kiosk_recovers_by_itself(self):
        R = run_page(self, KIOSK_DRIVER, 120000)
        self.assertTrue(R["bannerAfter"])
        self.assertEqual(R["chipsAfter"], R["chipsBefore"])
        self.assertFalse(R["staleAfter"])
        self.assertEqual(R["leftoverLive"], 0)

    def test_an_erroring_hub_counts_as_not_responding(self):
        R = run_page(self, KIOSK_DRIVER, 120000)
        self.assertFalse(R["bannerOn500"])
        self.assertIn("HTTP 500", R["bannerOn500Text"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
