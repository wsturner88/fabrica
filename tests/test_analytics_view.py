"""Tests for the Analytics page in dashboard.html: where it is reached from in every layout, that it fits (one toolbar row, little
chrome, nothing but the panel scrolls on the 800x480 screen), that nothing is said by colour alone, and that hostile names stay text.

    python3 tests/test_analytics_view.py

Static checks read dashboard.html as text. The browser checks load the REAL page into headless Chrome / Edge at four window sizes with a stubbed
`fetch` (same harness as test_fix_dashboard.py; skipped when no browser is found). Nothing leaves the process.
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

import test_fix_dashboard as tfd  # noqa: E402

HTML = tfd.HTML


def block(name):
    """The text between `==== name ====` and `==== end name ====` markers (script / style / html comment forms)."""
    return HTML[HTML.index("// ==== analytics ====\n"):HTML.index("// ==== end analytics ====")]


class StaticTests(unittest.TestCase):
    def test_it_is_reached_everywhere_history_is(self):
        # the home row (desktop and the 10-inch wall screen): next to History, hidden on a phone like History is
        self.assertRegex(HTML, r'<button onclick="openHistory\(\)" class="phone-hide"[^>]*>History</button>\s*<button onclick="openAnalytics\(\)" class="phone-hide"[^>]*>Analytics</button>')
        # the More sheet (phone, 5-inch): the entry is beside History's and goes through the same moreGo
        self.assertRegex(HTML, r'<button data-act="moreGo" data-go="history">History</button>\s*<button data-act="moreGo" data-go="analytics">Analytics</button>')
        self.assertIn("analytics: () => openAnalytics()", HTML)
        # desktop / touch views hide every More entry except Backups and Sign out (their pages are in the row): the new one is covered by that rule
        self.assertIn('button[data-go] { display:none; }', HTML.replace('html[data-view="desktop"] #moresheet .morebox button[data-go], html[data-view="touch"]:not([data-kiosk]) #moresheet .morebox button[data-go]:not([data-go="backups"]):not([data-go="signout"])', 'button[data-go]'))
        # the 5-inch header carries only More: every other row button is hidden there
        self.assertIn('html.lay-5in #home > .row:first-child > button:not(#morebtn) { display:none !important; }', HTML)

    def test_the_page_and_its_wiring(self):
        self.assertIn('<section id="analytics" class="screen">', HTML)
        self.assertIn("if(id!=='analytics') anLeave();", HTML)
        self.assertIn("...AN_ACTIONS,", HTML)
        self.assertIn("pmCall('GET', '/api/analytics' + q)", HTML)
        self.assertEqual(len(re.findall(r"<!-- ==== analytics ==== -->", HTML)), 4)       # the two nav buttons, the page, and the style + script block
        self.assertEqual(len(re.findall(r"<!-- ==== end analytics ==== -->", HTML)), 2)

    def test_scripts_parse(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        d = tempfile.mkdtemp(prefix="anview-js-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "all.js")
        with open(p, "w", encoding="utf8") as fh:
            fh.write("\n;\n".join(tfd.BLOCKS))
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_one_toolbar_row_and_the_controls(self):
        for words in ('class="an-grp"', "Which printers", "All printers", "an-more", "More &#8943;", "onclick=\"show('home')\">Close</button>",
                      'role="radiogroup" aria-label="Period"', "AN_PERIODS"):
            self.assertIn(words, HTML, words)
        self.assertIn("aria-checked=\"${AN.period === k}\"", HTML)                  # the chosen period is marked (and the .seg rule adds a tick)
        # the group choice only exists when there are groups
        self.assertIn("const grp = AN.groups.length ?", HTML)
        # CSV lives behind More
        self.assertIn("data-act=\"anCsv\"", HTML)
        self.assertIn("/api/jobs.csv?period=", block("analytics"))

    def test_phone_and_five_inch_rules_exist_and_the_phone_rules_are_undone_on_the_wall_screen(self):
        self.assertIn("html.lay-5in #analytics.on { display:flex;", HTML)
        self.assertIn("html.lay-5in #an-head { flex:none; height:52px;", HTML)
        self.assertIn("html.lay-5in .an-panel .at .tr.h { display:grid;", HTML)       # the phone rule hides the table header; the wall screen shows it again
        self.assertIn("html.lay-5in .an-panel .at .tr > div[data-l]::before { content:none; }", HTML)
        self.assertIn("@media (max-width:760px), (orientation:landscape) and (max-height:500px) and (max-width:932px) {\n    #an-head { flex-wrap:wrap; }", HTML)
        self.assertIn('html[data-view="desktop"] #an-body > * { max-width:1700px; }', HTML)

    def test_nothing_is_said_by_colour_alone(self):
        css = HTML[HTML.index("/* ==== analytics ==== */"):HTML.index("// ==== analytics ====")]
        self.assertIn(".an-chart .an-bad { fill:url(#an-hatch);", css)              # failed bars are HATCHED, finished bars solid
        self.assertIn('<pattern id="an-hatch"', HTML)
        self.assertIn("repeating-linear-gradient(45deg", css)                        # ... and so is the legend swatch
        self.assertIn("&#10004; Finished", HTML)
        self.assertIn("&#10006; Failed or cancelled", HTML)
        self.assertNotRegex(css, r"(?i)(green|#0f0|#00ff00|#2e7d32|lime|\bred\b)")
        for words in ("parts unknown", "Utilization", "Online", "collecting since", "failed or cancelled"):
            self.assertIn(words, HTML, words)
        self.assertIn('role="img" aria-label="', HTML)                               # the chart has a spoken description

    def test_no_foreign_resources_and_every_name_is_escaped(self):
        js = block("analytics")
        self.assertNotRegex(js, r"""(https?:)?//[a-z0-9.-]+\.[a-z]{2,}/""")
        for needle in ("esc(p.printer)", "esc(m.material", "esc(f.file)", "esc(r.reason)", "esc(g)"):
            self.assertIn(needle, js, needle)
        self.assertNotRegex(js, r"""\bon[a-z]+="[^"]*\$\{""")                        # no value spliced into an inline handler


# ====================================================================== browser

DRIVER = r"""
const iso = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
const mkBuckets = (n, kind) => Array.from({length: n}, (_, i) => { const d = new Date(2026, 9, 18); const k = n - 1 - i;
  if(kind === 'month') d.setMonth(d.getMonth() - k, 1); else d.setDate(d.getDate() - k * (kind === 'week' ? 7 : 1));
  return {start: iso(d), finished: (i * 3) % 7 + 1, failed: i % 4 === 0 ? 2 : 0, other: i === 3 ? 1 : 0}; });
const TREND = {'7d': [8, 'day'], '30d': [30, 'day'], '90d': [13, 'week'], '1y': [53, 'week'], 'all': [37, 'month']};
let mode = 'full';
const A = (period) => ({period, since: 1.78e9, until: 1.79e9, currency: '$', cost_basis: 'filament', empty: mode === 'empty',
  totals: {prints: 40, finished: 34, failed: 3, cancelled: 2, unknown: 1, running: 1, decided: 39, success_rate: 0.8718, hours: 120.5, grams: 2400, filament_kg: 2.4,
           filament_cost: 52.1, wasted_cost: 3.2, good_parts: 90, parts_unknown_prints: 4, cost_per_good_part: 0.5789},
  by_printer: [{printer: P1, prints: 20, finished: 17, failed: 3, success_rate: 0.85, hours: 60, utilization: 0.31, grams: 1200, cost: 26, online_pct: 99.5, observed_h: 700},
               {printer: 'Printer-B', prints: 0, finished: 0, failed: 0, success_rate: null, hours: 0, utilization: null, grams: 0, cost: 0, online_pct: null, observed_h: 0},
               {printer: 'Printer-C', prints: 20, finished: 17, failed: 2, success_rate: 0.89, hours: 60.5, utilization: 0.12, grams: 1200, cost: 26.1, online_pct: 88.2, observed_h: 700}],
  by_material: [{material: P2, prints: 30, finished: 27, failed: 3, success_rate: 0.9, grams: 2000, cost: 44}, {material: '?', prints: 4, finished: 4, failed: 0, success_rate: 1, grams: 0, cost: 0}],
  failures: {by_outcome: [{outcome: 'failed', count: 3}, {outcome: 'cancelled', count: 2}, {outcome: 'unknown', count: 1}], failed_or_cancelled: 5, wasted_cost: 3.2,
             by_reason: [{reason: P3, outcome: 'failed', count: 3}, {reason: 'no reason recorded', outcome: 'cancelled', count: 2}], reasons_other: 2,
             top_files: [{file: P3, prints: 6, finished: 3, failed: 3, success_rate: 0.5, parts: 0, parts_unknown_prints: 3}]},
  top_files: [{file: P1, prints: 12, finished: 11, failed: 1, success_rate: 0.91, parts: 0, parts_unknown_prints: 11},
              {file: '=cmd|calc', prints: 8, finished: 8, failed: 0, success_rate: 1, parts: 64, parts_unknown_prints: 0},
              {file: 'mixed', prints: 5, finished: 5, failed: 0, success_rate: 1, parts: 20, parts_unknown_prints: 1}],
  trend: {bucket: (TREND[period] || TREND['30d'])[1], buckets: mkBuckets((TREND[period] || TREND['30d'])[0], (TREND[period] || TREND['30d'])[1]), partial_first: true},
  uptime: {available: true, since: 1.789e9, since_iso: '', partial: true}});
const empty = (period) => Object.assign(A(period), {empty: true, totals: {prints: 0, finished: 0, failed: 0, cancelled: 0, unknown: 0, running: 0, decided: 0, success_rate: null,
  hours: 0, grams: 0, filament_kg: 0, filament_cost: 0, wasted_cost: 0, good_parts: 0, parts_unknown_prints: 0, cost_per_good_part: null}, by_printer: [], by_material: [], top_files: []});
window.__stubs.push((u, m) => {
  if(u === '/api/fleet') return J([]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/groups') return J([{name: 'Home', printers: ['Printer-B']}, {name: P1, printers: [P1]}]);
  if(u.startsWith('/api/analytics')){
    if(mode === 'error') return J({error: 'boom ' + P1}, 500);
    const p = /period=([^&]+)/.exec(u); return J(mode === 'empty' ? empty(p && p[1]) : A(p && p[1]));
  }
  return null;
});
const wait = async (f, n) => { for(let i = 0; i < (n || 80) && !f(); i++) await sleep(100); };
const rect = e => e.getBoundingClientRect();
const last = p => window.__calls.filter(c => c.startsWith('GET ' + p)).pop();
const T = R.t = {};
const MINB = FVIEW.view === 'desktop' ? 35 : 43;         // a mouse view uses 36 px controls (like History); every touch layout 44

// ---- where it is reached from
const nb = document.querySelector('#home button[onclick="openAnalytics()"]');
T.navBtn = !!nb; T.navVisible = !!nb && nb.offsetParent !== null;
const hr = document.querySelector('#home > .row:first-child'); T.homeRowH = Math.round(rect(hr).height);
T.moreItem = !!document.querySelector('#moresheet [data-go=analytics]');
moreOpen(); T.moreItemVisible = document.querySelector('#moresheet [data-go=analytics]').offsetParent !== null; moreClose();

// ---- the page
await openAnalytics(); await wait(() => document.querySelector('#an-body .an-t'));
const snap = () => {
  const head = [...document.querySelectorAll('#an-head > h1, #an-ctl > *')].filter(e => e.offsetParent !== null);
  return {headH: Math.round(rect($('an-head')).height), tops: head.map(e => Math.round(rect(e).top)), docH: document.documentElement.scrollHeight, docW: document.documentElement.scrollWidth,
    winH: innerHeight, winW: innerWidth, bodyOverflowX: $('an-body').scrollWidth > $('an-body').clientWidth + 1,
    tiles: [...document.querySelectorAll('.an-t')].filter(e => e.offsetParent !== null).map(t => [...t.querySelectorAll('.l span')].filter(x => x.offsetParent !== null).map(x => x.textContent).join('') + '=' + t.querySelector('.v').textContent),
    rows: new Set(head.map(e => Math.round((rect(e).top + rect(e).height / 2) / 36))).size,
    tilesH: Math.round(rect(document.querySelector('.an-tiles')).height), tabs: [...document.querySelectorAll('.an-tabs button')].map(b => b.textContent),
    tabsH: document.querySelector('.an-tabs') && document.querySelector('.an-tabs').offsetParent ? Math.round(rect(document.querySelector('.an-tabs')).height) : 0,
    panelH: document.querySelector('.an-panel') ? Math.round(rect(document.querySelector('.an-panel')).height) : 0,
    smallButtons: [...document.querySelectorAll('#an-ctl button, #an-ctl select, .an-tabs button')].filter(e => e.offsetParent !== null && rect(e).height < MINB).length,
    injected: injected(), alerts: window.__alerts.length};
};
T.first = snap();
T.stackLabel = (() => { const c = document.querySelector('#an-body .at .tr:not(.h) [data-l]'); return c ? getComputedStyle(c, '::before').content : ''; })();
// every x-axis label stays inside the chart, whatever the period (the edge labels hang inward)
T.labelsOut = {};
for(const p of ['7d', '30d', '90d', '1y', 'all']){
  document.querySelector('.an-per button[data-p="' + p + '"]').click(); await sleep(300);
  const svg = document.querySelector('.an-chart svg'), sb = rect(svg);
  T.labelsOut[p] = {n: svg.querySelectorAll('text[y="' + (parseFloat(svg.getAttribute('height')) - 4) + '"]').length,
    out: [...svg.querySelectorAll('text')].filter(t => { const b = rect(t); return b.left < sb.left - 0.5 || b.right > sb.right + 0.5; }).map(t => t.textContent)};
}
document.querySelector('.an-per button[data-p="30d"]').click(); await sleep(300);
// a 375 px phone: headless Chrome will not open a window narrower than 500, so squeeze the page itself and measure the toolbar again
if(innerWidth <= 600){
  document.body.style.width = '375px'; await sleep(200);
  const g = document.querySelector('.an-grp'), sg = document.querySelector('.an-per');
  T.narrow = {rows: snap().rows, headH: Math.round(rect($('an-head')).height), selectFont: g ? getComputedStyle(g).fontSize : '', selectW: g ? Math.round(rect(g).width) : 0,
    sameRow: g ? Math.abs(rect(g).top - rect(sg).top) < 2 : null, btnH: [...document.querySelectorAll('.an-per button, .an-grp')].map(b => Math.round(rect(b).height)),
    rightEdge: Math.round(rect(sg).right), docW: document.documentElement.scrollWidth};
  document.body.style.width = ''; await sleep(100);
}
T.groupOpts = [...document.querySelectorAll('.an-grp option')].map(o => o.value + '|' + o.textContent);
T.periodBtns = [...document.querySelectorAll('.an-per button')].map(b => b.dataset.p + '=' + b.getAttribute('aria-checked'));

// every panel on the wall screen / every section on the others
const panels = {};
if(L5){
  for(const [k] of AN_TABS){
    document.querySelector('.an-tabs [data-t=' + k + ']').click(); await sleep(80);
    const p = document.querySelector('.an-panel'); panels[k] = {text: p.textContent.slice(0, 400), h: Math.round(rect(p).height), sh: p.scrollHeight, ch: p.clientHeight,
      overflowX: p.scrollWidth > p.clientWidth + 1, head: !!p.querySelector('.tr.h') && p.querySelector('.tr.h').offsetParent !== null, injected: injected()};
  }
  document.querySelector('.an-tabs [data-t=trend]').click(); await sleep(80);
} else {
  panels.all = {text: $('an-body').textContent, secs: document.querySelectorAll('#an-body .an-sec').length, injected: injected()};
}
T.panels = panels;

// the chart: failed = hatched, finished = solid, counts in words
const okB = [...document.querySelectorAll('.an-chart .an-ok')], badB = [...document.querySelectorAll('.an-chart .an-bad')];
T.chart = {ok: okB.length, bad: badB.length, okFill: okB.length ? getComputedStyle(okB[0]).fill : '', badFill: badB.length ? getComputedStyle(badB[0]).fill : '',
  hatch: !!document.querySelector('#an-hatch'), legend: document.querySelector('.an-leg').textContent, label: document.querySelector('.an-chart svg').getAttribute('aria-label'),
  titles: document.querySelectorAll('.an-chart title').length, svgW: Math.round(rect(document.querySelector('.an-chart svg')).width), boxW: Math.round(rect(document.querySelector('.an-chart')).width),
  head: document.querySelector('#an-body h2').textContent};
T.printerCells = [...document.querySelectorAll('#an-body .at')].map(t => t.textContent.replace(/\s+/g, ' ').slice(0, 160));

// ---- choices ask the Hub for what was chosen
document.querySelector('.an-per button[data-p="7d"]').click(); await sleep(300);
T.call7 = last('/api/analytics'); T.periodAfter = [...document.querySelectorAll('.an-per button')].filter(b => b.getAttribute('aria-checked') === 'true').map(b => b.dataset.p);
const sel = document.querySelector('.an-grp'); sel.value = 'Home'; sel.dispatchEvent(new Event('change')); await sleep(300);
T.callHome = last('/api/analytics');
anSetGroup(P1); await sleep(300); T.callHostile = last('/api/analytics');
anSetGroup(''); document.querySelector('.an-per button[data-p="1y"]').click(); await sleep(300); T.callYear = last('/api/analytics');

// ---- More: CSV
document.querySelector('.an-more').click(); await sleep(100);
T.moreHref = document.querySelector('#mbody a.bkdl').getAttribute('href'); T.moreHasCsv = !!document.querySelector('#mbody [data-act=anCsv]'); closeModal();
anSetGroup('Home'); await sleep(300); document.querySelector('.an-more').click(); await sleep(100); T.moreHrefHome = document.querySelector('#mbody a.bkdl').getAttribute('href'); closeModal(); anSetGroup('');
await sleep(300);
const csv = anCsvText(AN.st); T.csvFormula = csv.split('\n').filter(l => l.includes('cmd|calc')).join('|'); T.csvRows = csv.trim().split('\n').length;

// ---- the empty state: one friendly line
mode = 'empty'; document.querySelector('.an-per button[data-p="all"]').click(); await sleep(300);
T.empty = {text: $('an-body').textContent.trim(), tiles: document.querySelectorAll('.an-t').length, h: Math.round(rect(document.querySelector('.an-empty')).height), lines: document.querySelectorAll('#an-body > *').length};
document.querySelector('.an-per button[data-p="30d"]').click(); await sleep(300); T.emptyOther = $('an-body').textContent.trim();
// ---- a Hub that fails
mode = 'error'; anLoad(); await sleep(300); T.err = {text: $('an-body').textContent, retry: !!document.querySelector('#an-body button.big'), injected: injected()};
mode = 'full'; anLoad(); await sleep(300); T.back = !!document.querySelector('#an-body .an-t');
// ---- leaving stops the refresh
show('home'); T.timerCleared = true; T.analyticsHidden = !$('analytics').classList.contains('on');
"""


def run_at(testcase, width, height, query=""):
    """Load the real dashboard.html at width x height (the page's own viewport) in headless Chrome with the stubbed fetch; returns the
    driver's findings. Headless Chrome counts its own window frame in --window-size, so the first run measures the difference and
    the page is loaded again at the size that gives exactly width x height."""
    if not tfd.CHROME:
        testcase.skipTest("no Chrome / Edge found for the browser tests")
    d = tempfile.mkdtemp(prefix="anview-chrome-")
    testcase.addCleanup(shutil.rmtree, d, True)
    page = os.path.join(d, "page.html")
    with open(page, "w", encoding="utf8") as fh:
        fh.write(tfd.build_page(DRIVER))
    extra_w = extra_h = 0
    for attempt in range(2):
        cmd = [tfd.CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
               "--user-data-dir=" + os.path.join(d, "profile"), f"--window-size={width + extra_w},{height + extra_h}",
               "--virtual-time-budget=60000", "--dump-dom", "file://" + page + query]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        m = re.search(r"data-result='(.*?)'", r.stdout, re.S) or re.search(r'data-result="(.*?)"', r.stdout, re.S)
        testcase.assertIsNotNone(m, "the driver did not finish: " + r.stderr[-500:] + r.stdout[-300:])
        txt = m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
        out = json.loads(txt)
        testcase.assertNotIn("error", out, out.get("error"))
        f = out["t"]["first"]
        if (f["winW"], f["winH"]) == (width, height) or attempt:
            return out
        extra_w, extra_h = width - f["winW"], height - f["winH"]
    return out


class Common:
    """Checks every layout must pass; each layout class adds its own."""
    SIZE = (1024, 600)
    QUERY = ""
    _cache = {}

    def res(self):
        key = (type(self).__name__, self.SIZE, self.QUERY)
        if key not in Common._cache:
            Common._cache[key] = run_at(self, *self.SIZE, self.QUERY)
        return Common._cache[key]

    def test_no_injection_no_scroll_sideways(self):
        R = self.res()
        self.assertEqual(R["alerts"], [])
        t = R["t"]["first"]
        self.assertEqual((t["injected"], t["alerts"]), (0, 0))
        self.assertLessEqual(t["docW"], t["winW"] + 1)
        self.assertFalse(t["bodyOverflowX"])
        for k, p in R["t"]["panels"].items():
            self.assertEqual(p["injected"], 0, k)
            self.assertFalse(p.get("overflowX", False), k)

    def test_it_is_reached_from_here(self):
        t = self.res()["t"]
        self.assertTrue(t["navBtn"])
        self.assertTrue(t["moreItem"])
        self.assertTrue(t["navVisible"] or t["moreItemVisible"], "neither the row button nor the More entry can be seen in this layout")

    def test_the_six_tiles_say_what_they_are_in_words(self):
        tiles = self.res()["t"]["first"]["tiles"]
        self.assertEqual(len(tiles), 6)
        text = " ".join(tiles).lower()
        for w in ("prints=40", "%", "h", "kg", "$52", "$0.5"):
            self.assertIn(w, text, w)

    MIN_BUTTON = 43                                                                    # a finger; the desktop view (a mouse) uses 36 like History

    def test_touch_targets_are_44px(self):
        f = self.res()["t"]["first"]
        self.assertEqual((f["winW"], f["winH"]), self.SIZE)                          # the page really was this size
        self.assertEqual(f["smallButtons"], 0, f"{f['smallButtons']} controls shorter than {self.MIN_BUTTON}px")

    def test_the_chart_uses_pattern_and_words_not_colour(self):
        c = self.res()["t"]["chart"]
        self.assertGreater(c["ok"], 20)
        self.assertGreater(c["bad"], 5)
        self.assertTrue(c["hatch"])
        self.assertTrue(c["badFill"].startswith("url("), c["badFill"])                  # hatched
        self.assertFalse(c["okFill"].startswith("url("), c["okFill"])                   # solid
        self.assertIn("Finished", c["legend"])
        self.assertIn("Failed or cancelled", c["legend"])
        self.assertIn("hatched", c["label"])
        self.assertEqual(c["titles"], 30)                                               # every bar can say its numbers in words
        self.assertLessEqual(abs(c["svgW"] - c["boxW"]), 30)                            # the chart fits its card
        self.assertIn("Prints per day", c["head"])

    def test_no_x_axis_label_is_clipped_in_any_period(self):
        out = self.res()["t"]["labelsOut"]
        self.assertEqual(sorted(out), ["1y", "30d", "7d", "90d", "all"])
        for p, v in out.items():
            self.assertGreaterEqual(v["n"], 2, p)                                        # there are labels to check
            self.assertEqual(v["out"], [], f"{p}: labels outside the chart: {v['out']}")

    def test_choices_ask_the_hub_for_what_was_chosen(self):
        t = self.res()["t"]
        self.assertIn("period=7d", t["call7"])
        self.assertEqual(t["periodAfter"], ["7d"])
        self.assertIn("period=7d&group=Home", t["callHome"])
        self.assertIn("group=" + "%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E", t["callHostile"])
        self.assertNotIn("group=", t["callYear"])
        self.assertIn("period=1y", t["callYear"])
        self.assertEqual(t["groupOpts"][0], "|All printers")
        self.assertEqual(len(t["groupOpts"]), 3)
        self.assertIn("30d", " ".join(t["periodBtns"]))
        self.assertEqual(len(t["periodBtns"]), 5)

    def test_more_has_both_csvs_and_a_name_cannot_become_a_formula(self):
        t = self.res()["t"]
        self.assertEqual(t["moreHref"], "/api/jobs.csv?period=year")                 # the page was on "1 year" at that point
        self.assertEqual(t["moreHrefHome"], "/api/jobs.csv?period=year&group=Home")
        self.assertTrue(t["moreHasCsv"])
        self.assertIn("\"'=cmd|calc\"", t["csvFormula"])
        self.assertGreater(t["csvRows"], 8)

    def test_empty_state_is_one_friendly_line(self):
        t = self.res()["t"]
        self.assertEqual(t["empty"]["tiles"], 0)
        self.assertEqual(t["empty"]["lines"], 1)
        self.assertIn("No prints logged yet", t["empty"]["text"])
        self.assertLessEqual(t["empty"]["h"], 100)
        self.assertIn("try a longer period", t["emptyOther"])

    def test_a_failing_hub_gets_a_message_and_a_retry_and_recovers(self):
        t = self.res()["t"]
        self.assertIn("Could not read the analytics", t["err"]["text"])
        self.assertIn("boom", t["err"]["text"])
        self.assertTrue(t["err"]["retry"])
        self.assertEqual(t["err"]["injected"], 0)
        self.assertTrue(t["back"])
        self.assertTrue(t["analyticsHidden"])


@unittest.skipUnless(tfd.CHROME, "no Chrome / Edge found for the browser tests")
class KioskTenInch(Common, unittest.TestCase):
    """1024x600 wall screen (?kiosk=1): the home row carries a button for it, the page is one scrolling column."""
    QUERY = "?kiosk=1"

    def test_one_toolbar_row_and_the_home_row_still_fits(self):
        t = self.res()["t"]
        self.assertTrue(t["navVisible"])
        self.assertLessEqual(t["homeRowH"], 60)                                          # adding a button did not wrap the row
        self.assertLessEqual(t["first"]["headH"], 64)
        self.assertEqual(t["first"]["rows"], 1)                                           # title and controls share one row

    def test_everything_is_there_in_order(self):
        p = self.res()["t"]["panels"]["all"]
        self.assertEqual(p["secs"], 5)
        for words in ("Per printer", "Utilization", "Online", "By material", "Most printed files", "Failures", "parts unknown", "collecting since",
                      "Files that fail most", "more with other reasons", "(not recorded)", "no reason recorded"):
            self.assertIn(words, p["text"], words)

    def test_unknown_parts_are_said_and_known_parts_are_added(self):
        text = self.res()["t"]["panels"]["all"]["text"]
        self.assertIn("parts unknown", text)
        self.assertRegex(text, r"20\s*\+ 1 unknown")
        self.assertIn("64", text)


@unittest.skipUnless(tfd.CHROME, "no Chrome / Edge found for the browser tests")
class Desktop(Common, unittest.TestCase):
    SIZE = (1440, 900)

    def test_one_toolbar_row(self):
        t = self.res()["t"]
        self.assertLessEqual(t["first"]["headH"], 60)
        self.assertEqual(t["first"]["rows"], 1)
        self.assertTrue(t["navVisible"])


@unittest.skipUnless(tfd.CHROME, "no Chrome / Edge found for the browser tests")
class Phone(Common, unittest.TestCase):
    SIZE = (500, 844)                                                                  # (headless Chrome will not make a window narrower than 500; the 375 px phone was checked by hand)

    def test_two_toolbar_rows_at_most_and_the_nav_is_in_more(self):
        t = self.res()["t"]
        self.assertFalse(t["navVisible"])                                                # the row button is not shown on a phone ...
        self.assertTrue(t["moreItemVisible"])                                            # ... it is in More
        self.assertLessEqual(t["first"]["headH"], 110)
        self.assertEqual(t["first"]["rows"], 2)

    def test_on_a_375px_phone_the_toolbar_is_two_rows_with_the_picker_and_periods_sharing_one(self):
        n = self.res()["t"]["narrow"]
        self.assertEqual(n["rows"], 2)                                                   # row 1: title + More + Close; row 2: group picker + periods
        self.assertLessEqual(n["headH"], 100)
        self.assertTrue(n["sameRow"])
        self.assertEqual(n["selectFont"], "16px")                                        # no iOS zoom
        self.assertGreaterEqual(n["selectW"], 100)                                       # still readable
        self.assertTrue(all(h >= 44 for h in n["btnH"]), n["btnH"])
        self.assertLessEqual(n["rightEdge"], 375)

    def test_tables_stack_with_their_labels(self):
        R = self.res()["t"]
        self.assertIn("Prints", R["stackLabel"])                                        # each cell carries its column name (CSS ::before) when the table is stacked
        self.assertEqual(len(R["panels"]["all"]["text"]) > 500, True)


@unittest.skipUnless(tfd.CHROME, "no Chrome / Edge found for the browser tests")
class FiveInch(Common, unittest.TestCase):
    """800x480 wall screen (?layout=5in): ONE toolbar row, six tiles in a row, tabs, one panel; nothing scrolls but the panel."""
    SIZE = (800, 480)
    QUERY = "?layout=5in"

    def test_it_fits_the_screen_and_little_of_it_is_chrome(self):
        f = self.res()["t"]["first"]
        self.assertLessEqual(f["docH"], f["winH"] + 1, "the page itself must not scroll on the 800x480 screen")
        self.assertLessEqual(f["headH"], 56)
        self.assertEqual(f["rows"], 1)
        chrome = f["headH"] + f["tilesH"] + f["tabsH"]
        self.assertLessEqual(chrome, 190, f"toolbar + tiles + tabs = {chrome}px of 480")
        self.assertGreaterEqual(f["panelH"], 250, "the content panel keeps most of the height")
        self.assertEqual(f["tabs"], ["✓ Trend", "Printers", "Materials", "Failures", "Top files"])
        self.assertTrue(self.res()["t"]["navVisible"] is False and self.res()["t"]["moreItemVisible"])    # reached through More

    def test_each_tab_has_content_a_header_row_and_scrolls_only_inside(self):
        panels = self.res()["t"]["panels"]
        self.assertEqual(sorted(panels), ["failures", "files", "materials", "printers", "trend"])
        for k, p in panels.items():
            self.assertGreater(len(p["text"]), 40, k)
            self.assertLessEqual(p["h"], 330, k)
        for k in ("printers", "materials", "files", "failures"):
            self.assertTrue(panels[k]["head"], k + " keeps its column headings on this screen")
        self.assertIn("Utilization", panels["printers"]["text"])
        self.assertIn("parts unknown", panels["files"]["text"])
        self.assertIn("no reason recorded", panels["failures"]["text"])

    def test_tiles_use_the_short_words_here(self):
        tiles = " ".join(self.res()["t"]["first"]["tiles"])
        for w in ("Prints=40", "Success=87%", "Hours=121 h", "Filament=2.40 kg", "Cost=$52.10", "Cost / part=$0.579"):
            self.assertIn(w, tiles, w)


@unittest.skipUnless(tfd.CHROME, "no Chrome / Edge found for the browser tests")
class FiveInchBridgeTheme(FiveInch):
    """The optional Bridge skin (uppercase, condensed) must not break the fit."""
    QUERY = "?layout=5in&theme=bridge"


if __name__ == "__main__":
    unittest.main()
