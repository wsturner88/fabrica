"""Tests for the main-page group filter: /dashboard?group=Work and the "All . Work . Home" chip row.

    python3 tests/test_group_switcher.py

Static checks read app/dashboard.html as text. The browser tests load the real page into headless Chrome / Edge with a
stubbed `fetch` (skipped when no browser is found); nothing contacts a printer or the network.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()

BLOCK = HTML[HTML.index("Main page: filter by group. /dashboard"):]
BLOCK = BLOCK[:BLOCK.index("</script>")]


class SwitcherStaticTests(unittest.TestCase):
    def test_wiring_is_delegated_with_no_inline_handlers_and_data_in_attributes(self):
        self.assertIn("...FL_ACTIONS", HTML)
        self.assertIn('data-act="${g === null ? \'flAll\' : \'flGroup\'}"', BLOCK)
        self.assertIn("data-g=\"${esc(g)}\"", BLOCK)
        self.assertNotRegex(BLOCK, r"\bon(click|error|load)\s*=")
        for sink in ("insertAdjacentHTML", "document.write", "outerHTML", "eval("):
            self.assertNotIn(sink, BLOCK)

    def test_every_dynamic_string_is_escaped(self):
        for needle in ("${esc(label)}", "${esc(txt)}", "esc((v.miss.length === 1"):
            self.assertIn(needle, BLOCK)
        # the group names never reach innerHTML except through esc()
        self.assertNotRegex(BLOCK, r"\$\{(?:g|k|label|txt|q)\}")

    def test_the_url_is_rewritten_in_place_not_navigated(self):
        self.assertIn("history.replaceState(", BLOCK)
        self.assertNotRegex(BLOCK, r"location\.(href|assign|replace)\s*=")
        self.assertNotIn("location.reload", BLOCK)
        self.assertIn("searchParams.delete('group')", BLOCK)
        self.assertIn("searchParams.append('group', g)", BLOCK)

    def test_active_chip_is_not_hue_only(self):
        css = re.search(r'\.fl-chip\[aria-pressed="true"\]\s*\{[^}]*\}', HTML, re.S).group(0)
        self.assertIn("font-weight:800", css)
        self.assertIn("text-decoration:underline", css)
        self.assertIn("background:var(--txt)", css)          # inverted fill
        self.assertIn("&#10003; ", BLOCK)                    # a tick in the label itself
        self.assertIn('aria-pressed="${on ?', BLOCK)

    def test_renderfleet_filters_and_hands_only_the_shown_printers_on(self):
        rf = HTML[HTML.index("function renderFleet(data){"):HTML.index("function fitEta()")]
        self.assertIn("flView(data)", rf)
        self.assertIn("flSwRender(view, all.length)", rf)
        self.assertIn("fcAttach(data)", rf)                 # data was narrowed to the shown set above it

    def test_switcher_sits_before_the_banner_and_fleet_so_the_kiosk_rules_still_match(self):
        self.assertRegex(HTML, r'(?s)<div id="fl-sw" class="fl-sw" hidden></div>.*?<div class="hubdown"[^>]*hidden></div>\s*<div id="fleet"></div>')

    def test_phone_rules_exist_and_never_match_the_wall_screen(self):
        self.assertIn("@media (max-width:600px) { .fl-chip { min-height:44px; }", HTML)
        self.assertIn("@media (orientation:landscape) and (max-height:500px) and (max-width:932px) { .fl-sw", HTML)
        self.assertIn(".fl-sw[hidden] { display:none; }", HTML)   # display:flex would otherwise defeat `hidden`
        self.assertIn("overflow-x:auto", HTML[HTML.index(".fl-chips {"):])   # one row; many groups scroll sideways, never wrap


DRIVER = r"""
const ALPHA = '"><img src=x onerror=alert(1)>';
let FLEET = [
  {printer: 'A1', groups: ['Work'], connected: true, state: 'RUNNING', percent: 10, remaining_min: 60, job: 'x', nozzle_c: 1, bed_c: 1, camera: false},
  {printer: 'X1', groups: ['Work', 'Proto'], connected: true, state: 'IDLE', percent: 0, remaining_min: 0, job: '', nozzle_c: 1, bed_c: 1, camera: false},
  {printer: 'Prusa', groups: ['Home'], connected: true, state: 'RUNNING', percent: 10, remaining_min: 60, job: 'x', nozzle_c: 1, bed_c: 1, camera: false},
  {printer: 'Loft', groups: [], connected: true, state: 'IDLE', percent: 0, remaining_min: 0, job: '', nozzle_c: 1, bed_c: 1, camera: false},
  {printer: 'Odd', groups: [ALPHA], connected: true, state: 'IDLE', percent: 0, remaining_min: 0, job: '', nozzle_c: 1, bed_c: 1, camera: false},
];
window.__stubs.push((u, m) => {
  if(u === '/api/fleet') return J(FLEET);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  return null;
});
const names = () => [...document.querySelectorAll('#fleet .pname')].map(e => e.textContent);
const sw = () => ({hidden: $('fl-sw').hidden,
  chips: [...document.querySelectorAll('.fl-chip')].map(b => b.textContent.replace(/\s+/g, ' ').trim()),
  pressed: [...document.querySelectorAll('.fl-chip[aria-pressed=true]')].map(b => b.textContent.replace(/\s+/g, ' ').trim()),
  data: [...document.querySelectorAll('.fl-chip[data-g]')].map(b => b.dataset.g),
  sum: (document.querySelector('.fl-sum') || {}).textContent || '', note: (document.querySelector('.fl-note') || {}).textContent || ''});
const apply = async search => { FLG.sel = flParse(search); FL.sig = null; FLG.sig = null; await fleet(); };
await sleep(300);

R.parse = [flParse('?group=Work'), flParse('?group=work&group=WORK&group=Home'), flParse('?x=1'), flParse('?group=%20%20a%20%20b&group='), flParse('')];
await apply('');                                            R.all = {names: names(), sw: sw()};
await apply('?group=work');                                 R.work = {names: names(), sw: sw()};
await apply('?group=Home&group=proto');                     R.union = {names: names(), sw: sw()};
await apply('?group=Gone');                                 R.gone = {names: names(), sw: sw()};
await apply('?group=Gone&group=Home');                      R.mixed = {names: names(), sw: sw()};
await apply('?group=' + encodeURIComponent(ALPHA));         R.hostile = {names: names(), sw: sw(), injected: injected()};

// tapping: the URL is rewritten, "All" clears it
await apply('');
document.querySelector('.fl-chip[data-g="Home"]').click(); await sleep(50);
R.tapHome = {names: names(), sw: sw(), sel: FLG.sel.slice(), search: location.search};
document.querySelector('[data-act=flAll]').click(); await sleep(50);
R.tapAll = {names: names(), sel: FLG.sel.slice(), search: location.search};

// a deleted group (the next poll no longer has it): everything is shown with a notice, never a blank page
await apply('?group=Home');
FLEET = FLEET.map(p => ({...p, groups: p.groups.filter(g => g !== 'Home')})); FL.sig = null; await fleet();
R.deleted = {names: names(), sw: sw()};
// no group anywhere: no chip row at all
FLEET = FLEET.map(p => ({...p, groups: []})); await apply('');
R.none = {names: names(), sw: sw()};
// an old server whose fleet has no `groups` field
FLEET = FLEET.map(p => { const q = {...p}; delete q.groups; return q; }); await apply('?group=Work');
R.legacy = {names: names(), sw: sw()};
R.alerts2 = window.__alerts.slice();
"""


class SwitcherBrowserTests(unittest.TestCase):
    R = None

    def result(self):
        import test_fix_dashboard as tfd
        if SwitcherBrowserTests.R is None:
            SwitcherBrowserTests.R = tfd.run_page(self, DRIVER, 60000)
        return SwitcherBrowserTests.R

    def test_ran_clean(self):
        R = self.result()
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        self.assertEqual(R["alerts2"], [])

    def test_url_parsing(self):
        R = self.result()
        self.assertEqual(R["parse"], [["Work"], ["work", "Home"], [], ["a b"], []])

    def test_no_filter_shows_everyone_and_all_is_the_active_chip(self):
        R = self.result()["all"]
        self.assertEqual(R["names"], ["A1", "X1", "Prusa", "Loft", "Odd"])
        self.assertFalse(R["sw"]["hidden"])
        self.assertEqual(R["sw"]["chips"], ["✓ All", '"><img src=x onerror=alert(1)>', "Home", "Proto", "Work"])   # A-Z after All; a hostile name is just text
        self.assertEqual(R["sw"]["pressed"], ["✓ All"])
        self.assertEqual(R["sw"]["sum"], "")

    def test_a_group_link_is_case_insensitive_and_summarises_the_filtered_set(self):
        R = self.result()["work"]
        self.assertEqual(R["names"], ["A1", "X1"])
        self.assertEqual(R["sw"]["pressed"], ["✓ Work"])
        self.assertEqual(R["sw"]["sum"], "Work · 2 printers · 1 printing")
        self.assertEqual(R["sw"]["note"], "")

    def test_several_group_params_are_a_union_without_doubling_a_shared_printer(self):
        R = self.result()["union"]
        self.assertEqual(R["names"], ["X1", "Prusa"])
        self.assertEqual(sorted(R["sw"]["pressed"]), ["✓ Home", "✓ Proto"])
        self.assertEqual(R["sw"]["sum"], "Home + Proto · 2 printers · 1 printing")

    def test_an_unknown_group_shows_all_with_a_notice(self):
        R = self.result()["gone"]
        self.assertEqual(R["names"], ["A1", "X1", "Prusa", "Loft", "Odd"])
        self.assertEqual(R["sw"]["note"], "⚠ Group 'Gone' not found — showing all")
        self.assertEqual(R["sw"]["pressed"], ["✓ All"])

    def test_a_mix_of_known_and_unknown_filters_by_the_known_one_and_says_so(self):
        R = self.result()["mixed"]
        self.assertEqual(R["names"], ["Prusa"])
        self.assertEqual(R["sw"]["note"], "⚠ Group 'Gone' not found")

    def test_hostile_group_names_stay_text(self):
        R = self.result()["hostile"]
        self.assertEqual(R["injected"], 0)
        self.assertEqual(R["names"], ["Odd"])
        self.assertIn('"><img src=x onerror=alert(1)>', R["sw"]["data"])      # round-trips as the exact text

    def test_tapping_filters_updates_the_url_and_all_clears_it(self):
        R = self.result()
        self.assertEqual(R["tapHome"]["names"], ["Prusa"])
        self.assertEqual(R["tapHome"]["sel"], ["Home"])
        self.assertEqual(R["tapHome"]["sw"]["pressed"], ["✓ Home"])
        self.assertEqual(R["tapAll"]["names"], ["A1", "X1", "Prusa", "Loft", "Odd"])
        self.assertEqual(R["tapAll"]["sel"], [])
        self.assertNotIn("group", R["tapAll"]["search"])
        # (the file:// test page may refuse replaceState; when it does not, the query must follow the choice)
        if R["tapHome"]["search"]:
            self.assertEqual(R["tapHome"]["search"], "?group=Home")

    def test_a_deleted_group_never_blanks_the_screen(self):
        R = self.result()["deleted"]
        self.assertEqual(len(R["names"]), 5)
        self.assertIn("not found", R["sw"]["note"])

    def test_no_groups_means_no_chip_row(self):
        R = self.result()["none"]
        self.assertTrue(R["sw"]["hidden"])
        self.assertEqual(R["sw"]["chips"], [])
        self.assertEqual(len(R["names"]), 5)

    def test_an_old_server_without_groups_in_the_fleet_still_shows_everything(self):
        R = self.result()["legacy"]
        self.assertEqual(len(R["names"]), 5)
        self.assertIn("not found", R["sw"]["note"])


if __name__ == "__main__":
    unittest.main()
