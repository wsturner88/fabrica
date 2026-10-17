"""History page: the cost numbers are FILAMENT ONLY by default, with a words-only switch for "+ Print time".

    python3 tests/test_history_cost_view.py

Static checks read dashboard.html as text. The browser check loads the REAL page into headless Chrome / Edge with a stubbed
`fetch` (same harness as test_fix_dashboard.py; skipped when no browser is found). Nothing leaves the process.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_fix_dashboard as tfd  # noqa: E402

HTML = tfd.HTML


class StaticTests(unittest.TestCase):
    def test_the_view_is_remembered_in_a_variable_not_saved(self):
        self.assertIn("machine:null", HTML)                                   # HI.machine: null = follow the setting
        self.assertIn("const hiMach = () => HI.machine == null ? '' : '&machine=' + (HI.machine ? '1' : '0');", HTML)
        # every History request carries it: stats, the job list, one job, the CSV link
        self.assertIn("hfQuery() + hiMach();", HTML)
        self.assertIn("'/api/jobs?limit=100' + hfQuery() + hiMach()", HTML)
        self.assertIn("encodeURIComponent(id) + hiMach().replace('&', '?')", HTML)
        self.assertIn("${esc(hfQuery())}${esc(hiMach())}", HTML)
        body = re.search(r"function hiSetMachine\(on\)\{.*?\}\n", HTML, re.S).group(0)
        self.assertNotIn("/api/settings", body)                               # flipping the chip never writes the setting

    def test_words_not_colour_and_a_real_touch_target(self):
        for words in ("Filament only", "+ Print time", "Which costs to count", "Filament ", "Power ", "Wear ",
                      "Print-time costs (power, wear): switch on in Settings &rarr; Costs"):
            self.assertIn(words, HTML, words)
        self.assertIn('aria-pressed="${!incl}"', HTML)
        self.assertIn('aria-pressed="${incl}"', HTML)
        rule = re.search(r"\.hi-mach button \{[^}]*\}", HTML).group(0)
        self.assertIn("min-height:44px", rule)
        self.assertIn('.hi-mach button[aria-pressed="true"]::before { content:"\\2714\\00a0"; }', HTML)       # a tick, not a colour
        self.assertIn("min-height:44px", re.search(r"\.hi-setlink \{[^}]*\}", HTML).group(0))       # the hint lives in the More dialog now (it made the cost tile twice as tall as the others)
        self.assertNotIn("hi-setlink", HTML[HTML.index("const kpis = `"):HTML.index("const periodLabel")])

    def test_the_link_goes_to_the_costs_section(self):
        self.assertIn("function hiToCosts(){ ST.sec = 'costs'; openSettings(); }", HTML)


DRIVER = r"""
let settingOn = false;                       // what Settings > Costs says; ?machine= overrides it per request
const cost = incl => ({material: 2.0, electricity: 0.5, wear: 1.5, total: incl ? 4.0 : 2.0, total_with_machine: 4.0,
                       wasted_on_failures: incl ? 1.0 : 0.5, includes_machine_costs: incl});
const job = (id, incl) => ({id, printer: 'Printer-A', file: 'bracket', result: 'finished', started_at: 1.7e9, ended_at: 1.7e9 + 3600,
  parts: 2, grams_used: 20, duration_s: 3600, open: false, material: 'PLA', filaments: [], pauses: 0, percent: 100,
  est_seconds: 3600, time_vs_estimate_pct: 0, grams_est: 20,
  cost: {material: 2.0, electricity: 0.5, wear: 1.5, total: incl ? 4.0 : 2.0, total_with_machine: 4.0, includes_machine_costs: incl, per_part: incl ? 2.0 : 1.0}});
const which = u => { const m = /[?&]machine=([01])/.exec(u); return m ? m[1] === '1' : settingOn; };
window.__stubs.push((u, m) => {
  if(u === '/api/fleet') return J([]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: [{id: 'costs', group: 'hub', label: 'Costs', description: 'd', help: 'h', fields: [
    {key: 'costs.include_machine_costs', name: 'include_machine_costs', label: 'Include print time costs', help: 'h', type: 'bool', value: false, section: 'costs'}]}]});
  if(u === '/api/printers') return J({printers: [{name: 'Printer-A'}]});
  if(u === '/api/groups') return J([]);
  if(u.startsWith('/api/stats')) { const i = which(u); return J({jobs: 2, finished: 2, failed: 0, cancelled: 0, unknown: 0, running: 0, success_rate: 1, grams_used: 40,
    by_material: {PLA: 40}, by_printer: {'Printer-A': {jobs: 2, finished: 2, failed: 0, cancelled: 0, success_rate: 1, hours: 2, cost_total: i ? 4.0 : 2.0}},
    by_file: [], cost: cost(i), includes_machine_costs: i, true_cost_per_part: i ? 2.0 : 1.0, clean_cost_per_part: i ? 2.0 : 1.0, currency: '$', since: null}); }
  if(/^\/api\/jobs\/\d/.test(u)) return J(job(7, which(u)));
  if(u.startsWith('/api/jobs')) { const i = which(u); return J({jobs: [job(7, i)], currency: '$', includes_machine_costs: i}); }
  return null;
});
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
const kpi = () => [...document.querySelectorAll('#hi-body .kpi')].find(k => k.querySelector('.kl').textContent.startsWith('Cost'));
const csvLink = () => { hiMore(); const h = document.querySelector('#mbody a.bkdl').getAttribute('href'); closeModal(); return h; };
const hintOf = () => { hiMore(); const h = document.querySelector('#mbody .hi-setlink'); const r = {has: !!h, text: h ? h.textContent : '', h: h ? Math.round(h.getBoundingClientRect().height) : 0}; closeModal(); return r; };
const snap = () => { const k = kpi(), b = [...document.querySelectorAll('#hi-ctl .hi-mach button')];
  return {label: k.querySelector('.kl').textContent, kv: k.querySelector('.kv').textContent, ks: k.querySelector('.ks').textContent,
          pressed: b.map(x => x.textContent + '=' + x.getAttribute('aria-pressed')),
          hint: hintOf().has, hintText: hintOf().text, hintH: hintOf().h, inTile: !!k.querySelector('.hi-setlink'),
          heights: b.map(x => Math.round(x.getBoundingClientRect().height)), overflow: k.scrollWidth > k.clientWidth + 1,
          csv: csvLink()}; };
const last = p => window.__calls.filter(c => c.startsWith('GET ' + p)).pop();
const pressedBtn = t => [...document.querySelectorAll('#hi-ctl .hi-mach button')].find(x => x.textContent === t);

await openHistory(); await wait(() => document.querySelector('#hi-body .kpi'));
R.first = snap(); R.firstStats = last('/api/stats'); R.firstJobs = last('/api/jobs?');
R.rowCost = document.querySelector('#hi-body .tr.click [data-l=Cost]').textContent;
R.printerHead = [...document.querySelectorAll('#hi-body h2')].map(h => h.textContent);

pressedBtn('+ Print time').click(); await sleep(400);
R.on = snap(); R.onStats = last('/api/stats'); R.onJobs = last('/api/jobs?');
R.onRowCost = document.querySelector('#hi-body .tr.click [data-l=Cost]').textContent;
document.querySelector('#hi-body .tr.click').click(); await wait(() => document.querySelector('#mbody .hi-det'));
R.onDetail = document.querySelector('#mbody').textContent; R.onDetailCall = last('/api/jobs/7');
closeModal();

pressedBtn('Filament only').click(); await sleep(400);
R.off = snap(); R.offStats = last('/api/stats');
$('mbody').innerHTML = '';                         // closeModal leaves the old sheet in place: wait for the NEW one
document.querySelector('#hi-body .tr.click').click(); await wait(() => document.querySelector('#mbody .hi-det'));
R.offDetail = document.querySelector('#mbody').textContent; R.offDetailCall = last('/api/jobs/7');
closeModal();

settingOn = true;                                  // the setting is switched on; the next visit follows it, the chip did not save anything
R.putCalls = window.__calls.filter(c => c.startsWith('PUT')).length;
await openHistory(); await wait(() => document.querySelector('#hi-body .kpi')); await sleep(200);
R.reopened = snap(); R.reopenedStats = last('/api/stats');
hiMore(); document.querySelector('#mbody .hi-setlink') && document.querySelector('#mbody .hi-setlink').click();
settingOn = false;
await openHistory(); await wait(() => document.querySelector('#hi-body .kpi')); await sleep(200);
hiMore(); document.querySelector('#mbody .hi-setlink').click(); await sleep(500);
R.settingsOn = document.getElementById('settings').classList.contains('on'); R.stSec = ST.sec;
R.putCalls2 = window.__calls.filter(c => c.startsWith('PUT')).length;
"""


@unittest.skipUnless(tfd.CHROME, "no Chrome / Edge found for the browser tests")
class BrowserTests(unittest.TestCase):
    R = None

    def result(self):
        if BrowserTests.R is None:
            BrowserTests.R = tfd.run_page(self, DRIVER, 60000)
        return BrowserTests.R

    def test_driver_ran(self):
        R = self.result()
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])

    def test_default_view_is_filament_only_with_a_hint_and_a_switch(self):
        R = self.result()
        f = R["first"]
        self.assertNotIn("machine=", R["firstStats"])                         # no param: the server follows the setting
        self.assertNotIn("machine=", R["firstJobs"])
        self.assertIn("filament only", f["label"])
        self.assertEqual(f["kv"], "$2.00")
        self.assertNotIn("Filament only", f["ks"])                              # said once, in the tile's title
        self.assertNotIn("Power", f["ks"])
        self.assertTrue(f["hint"])
        self.assertFalse(f["inTile"])
        self.assertGreaterEqual(f["hintH"], 44)
        self.assertIn("Settings", f["hintText"])
        self.assertIn("Costs", f["hintText"])
        self.assertEqual(f["pressed"], ["Filament only=true", "+ Print time=false"])
        self.assertTrue(all(h >= 44 for h in f["heights"]), f["heights"])
        self.assertFalse(f["overflow"])
        self.assertEqual(R["rowCost"], "$2.00")
        self.assertTrue(any("filament only" in h for h in R["printerHead"]), R["printerHead"])

    def test_plus_print_time_refetches_with_machine_1_and_shows_the_split_in_words(self):
        R = self.result()
        o = R["on"]
        self.assertIn("machine=1", R["onStats"])
        self.assertIn("machine=1", R["onJobs"])
        self.assertIn("with print time", o["label"])
        self.assertEqual(o["kv"], "$4.00")
        self.assertIn("Filament $2.00", o["ks"])
        self.assertIn("Power $0.50", o["ks"])
        self.assertIn("Wear $1.50", o["ks"])
        self.assertFalse(o["hint"])
        self.assertEqual(o["pressed"], ["Filament only=false", "+ Print time=true"])
        self.assertIn("machine=1", o["csv"])
        self.assertEqual(R["onRowCost"], "$4.00")
        self.assertIn("machine=1", R["onDetailCall"])
        self.assertIn("filament + power + wear", R["onDetail"])
        self.assertIn("Filament cost", R["onDetail"])
        self.assertNotIn("not counted", R["onDetail"])

    def test_back_to_filament_only_sends_machine_0_and_the_job_detail_shows_both(self):
        R = self.result()
        self.assertIn("machine=0", R["offStats"])
        self.assertEqual(R["off"]["kv"], "$2.00")
        self.assertTrue(R["off"]["hint"])
        self.assertIn("machine=0", R["offDetailCall"])
        d = R["offDetail"]
        self.assertIn("Power (not counted)", d)
        self.assertIn("Machine wear (not counted)", d)
        self.assertIn("filament only", d)
        self.assertIn("With print time", d)
        self.assertIn("$4.00", d)

    def test_the_chip_never_saves_and_the_view_resets_to_the_setting_on_reopen(self):
        R = self.result()
        self.assertEqual(R["putCalls"], 0)
        self.assertNotIn("machine=", R["reopenedStats"])
        self.assertIn("with print time", R["reopened"]["label"])              # the setting was on: the view follows it
        self.assertEqual(R["putCalls2"], 0)

    def test_the_hint_opens_settings_at_costs(self):
        R = self.result()
        self.assertTrue(R["settingsOn"])
        self.assertEqual(R["stSec"], "costs")


if __name__ == "__main__":
    unittest.main()
