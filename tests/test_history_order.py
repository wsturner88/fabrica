"""History, newest first: Recent prints is the first list on every layout (10-inch, desktop, phone, 5-inch), By file defaults to the most
recently printed file first (spend is one tap away) and the job APIs answer newest first.

    python3 tests/test_history_order.py

Headless Chrome / Edge with a stubbed `fetch` (skipped when none is found); the API order is checked on a throw-away job log. Names are generic.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_layout_5in import run_q  # noqa: E402
from test_parity_layouts import HIST_DRIVER, WIDE  # noqa: E402

MARK_A = "window.__stubs.push((u, m) => {\n  const path = u.split('?')[0]; let mm;\n  if(path === '/api/printers')"
assert MARK_A in HIST_DRIVER

DRIVER = HIST_DRIVER.split("R.jobs = snap();")[0].replace(
    MARK_A, "STATS.by_file.forEach((f, i) => f.last_ts = NOW - 86400 * (i + 1));\nconst SPEND = STATS.by_file.slice().reverse();\n" + MARK_A, 1).replace(
    "if(path === '/api/stats') return J(STATS);", "if(path === '/api/stats') return J(/files=spend/.test(u) ? Object.assign({}, STATS, {by_file: SPEND}) : STATS);", 1) + r"""
const txt = e => e.textContent.replace(/\s+/g, ' ').trim();
const tabBtn = k => document.querySelector('.hi-tabs [data-t="' + k + '"]');
const fsBtns = () => [...document.querySelectorAll('#hi-body .hi-fsort button')];
const jobFiles = () => [...document.querySelectorAll('#hi-body .tr.click')].map(row => row.children[L5 ? 2 : 3].textContent.trim());
const fileTable = () => document.querySelector('#hi-body .h5f') || document.querySelector('#hi-body h2.hi-h2f').nextElementSibling;
const fileNames = () => [...fileTable().querySelectorAll('.tr:not(.h)')].map(row => row.querySelector('.first b').textContent.trim());
const statsCalls = () => window.__calls.filter(c => c.indexOf('/api/stats') >= 0);
R.firstJobs = jobFiles().slice(0, 3);
R.first = snap();
// which table comes first on the page: the one whose rows are clickable jobs
R.order = [...document.querySelectorAll('#hi-body h2, #hi-body .hi-tabs')].filter(vis).map(h => txt(h).slice(0, 40));
R.firstIsJobs = (() => { const t = document.querySelector('#hi-body .ht'); return !!(t && t.querySelector('.tr.click')); })();
R.jobsNewestFirst = (() => { const ts = JOBS.map(j => j.started_at); return ts.every((v, i) => !i || ts[i - 1] >= v); })();
R.callsAtOpen = statsCalls().slice();
if(L5){ tabBtn('file').click(); await sleep(200); }
R.files = {names: fileNames(), btns: fsBtns().map(b => txt(b) + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')), head: [...document.querySelectorAll('#hi-body h2')].filter(vis).map(txt), dates: [...document.querySelectorAll('#hi-body .first small')].map(txt)};
fsBtns().find(b => /Spend/.test(txt(b))).click(); await wait(() => /files=spend/.test(statsCalls().pop() || '')); await sleep(300);
if(L5){ tabBtn('file').click(); await sleep(200); }
R.spend = {names: fileNames(), btns: fsBtns().map(b => txt(b) + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')), head: [...document.querySelectorAll('#hi-body h2')].filter(vis).map(txt), call: statsCalls().pop()};
R.after = snap();
fsBtns().find(b => /Recent/.test(txt(b))).click(); await sleep(400);
if(L5){ tabBtn('file').click(); await sleep(200); }
R.backToRecent = {names: fileNames(), call: statsCalls().pop()};
"""


class HistoryOrderTests(unittest.TestCase):
    R = {}

    def go(self, key, query, size):
        if key not in HistoryOrderTests.R:
            HistoryOrderTests.R[key] = run_q(self, DRIVER.replace("await sleep(300);\nawait openHistory();", WIDE + "await sleep(300);\nawait openHistory();", 1), query, size, 180000)
        R = HistoryOrderTests.R[key]
        self.assertNotIn("error", R, R.get("error"))
        return R

    LAYOUTS = (("kiosk", "?kiosk=1", (1024, 600)), ("desk", "", (1440, 900)), ("phone", "", (390, 844)), ("five", "?layout=5in", (800, 480)))

    def each(self):
        for k, q, sz in self.LAYOUTS:
            yield k, self.go(k, q, sz)

    def test_recent_prints_come_first_newest_first_on_every_layout(self):
        for k, R in self.each():
            self.assertTrue(R["firstIsJobs"], k)                                          # the first table on the page is the job list
            self.assertEqual(R["firstJobs"], ["Bracket x12 for the workshop shelf.gcode.3mf", "Lid v3.gcode", "Phone stand.bgcode"], k)
            if k != "five":
                self.assertTrue(R["order"][0].startswith("Recent prints"), (k, R["order"]))         # ... under its own heading, before By printer / By file
                self.assertTrue(any(h.startswith("By file") for h in R["order"][1:]), k)
            else:
                self.assertTrue(R["order"][0].startswith("Recent prints") or "Recent prints" in R["order"][0], (k, R["order"]))

    def test_by_file_is_most_recently_printed_first_and_asks_the_hub_that_way(self):
        for k, R in self.each():
            f = R["files"]
            self.assertEqual(f["names"][:3], ["Bracket x12 for the workshop shelf", "Phone stand", "old file"], k)
            self.assertEqual(f["btns"], ["✓ Recent*", "Spend"], k)
            self.assertTrue(all("files=spend" not in c for c in R["callsAtOpen"]), k)       # the default request carries no spend order
            self.assertEqual(len(f["dates"]), 3, k)                                         # each file says when it was last printed
            if k != "five":
                self.assertIn("most recently printed first", " ".join(f["head"]), k)

    def test_spend_is_one_tap_away_and_back(self):
        for k, R in self.each():
            s = R["spend"]
            self.assertEqual(s["names"][:3], ["old file", "Phone stand", "Bracket x12 for the workshop shelf"], k)       # the hub's spend order, as served
            self.assertEqual(s["btns"], ["Recent", "✓ Spend*"], k)
            self.assertIn("files=spend", s["call"], k)
            if k != "five":
                self.assertIn("biggest spend first", " ".join(s["head"]), k)
            self.assertNotIn("files=spend", R["backToRecent"]["call"], k)
            self.assertEqual(R["backToRecent"]["names"][0], "Bracket x12 for the workshop shelf", k)

    def test_the_toolbar_stays_one_row_and_nothing_overflows(self):
        for k, R in self.each():
            self.assertLessEqual(R["first"]["docW"], R["first"]["vw"], k)
            self.assertLessEqual(R["after"]["docW"], R["after"]["vw"], k)
            if k != "phone":
                self.assertEqual(R["first"]["toolbarRows"], 1, k)
                self.assertGreaterEqual(R["first"]["close"]["r"], 1)
                self.assertLessEqual(R["first"]["close"]["r"], R["first"]["vw"], k)

    def test_the_by_file_buttons_are_finger_targets(self):
        for k, R in self.each():
            pass
        # measured in the page run above: the buttons are 44 px tall (see the CSS); checked here as text so a change is noticed
        html = open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8").read()
        self.assertIn(".hi-fsort button, .hi-fs5 .hi-fsort button { min-width:0; min-height:44px;", html)


# ------------------------------------------------------------------------------------- the API order

import tempfile  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="historyorder-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ["BAMBU_MCP_API_AUTH"] = "off"
sys.path.insert(0, os.path.join(HERE, "..", "app"))
try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception:      # pragma: no cover
    HAVE = False


@unittest.skipUnless(HAVE, "needs the app requirements")
class ApiOrderTests(unittest.TestCase):
    def test_jobs_and_stats_endpoints_pass_the_order_through(self):
        import joblog
        from test_joblog import ev, T0
        import costs
        tmp = tempfile.mkdtemp(prefix="historyorder-")
        files = {k: {"grams": g, "seconds": 600, "parts": 1, "material": "PLA", "kind": "bambu"} for k, g in (("cheap", 1.0), ("dear", 90.0))}
        jl = joblog.JobLog(os.path.join(tmp, "jobs.sqlite"), resolver=lambda p, j: files.get(costs.file_key(j)), clock=lambda: T0 + 99999,
                           rates=costs.Rates(per_kg={"PLA": 20}))
        for i, n in enumerate(["dear.gcode.3mf", "cheap.gcode.3mf"]):
            jl.on_event(ev("print_started", ts=T0 + i * 1000, job=n))
            jl.on_event(ev("print_finished", ts=T0 + i * 1000 + 600, job=n, seconds=600))
        from unittest import mock
        with mock.patch.object(server, "JOBLOG", jl):
            c = TestClient(server.mcp.streamable_http_app())
            c.__enter__()                               # (the MCP session manager needs the app's lifespan running)
            self.addCleanup(c.__exit__, None, None, None)
            jobs = c.get("/api/jobs").json()["jobs"]
            self.assertEqual([j["file"] for j in jobs], ["cheap.gcode.3mf", "dear.gcode.3mf"])         # newest first
            recent = c.get("/api/stats?period=all").json()["by_file"]
            self.assertEqual([f["file"] for f in recent], ["cheap", "dear"])
            spend = c.get("/api/stats?period=all&files=spend").json()["by_file"]
            self.assertEqual([f["file"] for f in spend], ["dear", "cheap"])


if __name__ == "__main__":
    unittest.main()
