"""Tests for the desktop "show the full name when it is cut short" tooltip helper in dashboard.html (cutTitleFor + the mouseover handler).

    python3 tests/test_hover_titles.py

The bug: hovering the History page on a desktop browser showed a native tooltip holding the ENTIRE list's text, because the helper treated any element that
scrolls (overflow auto / scroll) as "cut short". Now only real truncated TEXT gets a title: text-overflow ellipsis or a line clamp, overflow hidden (never
auto / scroll), a text leaf (at most two child elements) and at most 160 characters.

Static part: dashboard.html read as text. Browser part: the REAL page (desktop view) in headless Chrome / Edge with a stubbed `fetch`; nothing leaves the
process (skipped when no browser is found).
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from test_layout_5in import run_q  # noqa: E402
from test_parity_layouts import PRE, HIST_DRIVER, WIDE  # noqa: E402
import test_analytics_view as av  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()

# what the sweep does on every page: hover EVERY element under `root` (the mouse can rest anywhere), then report every title that appeared
SWEEP = r"""
const hov = el => el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
const before = new Set([...document.querySelectorAll('[title]')]);
const sweep = (root) => {
  const els = [root, ...root.querySelectorAll('*')];
  els.forEach(hov);
  const got = [...root.querySelectorAll('[title]'), ...(root.hasAttribute && root.hasAttribute('title') ? [root] : [])].filter(e => !before.has(e));
  const cs = e => getComputedStyle(e);
  return {n: els.length, titles: got.map(e => ({len: e.title.length, tag: e.tagName, cls: String(e.className).slice(0, 30), ox: cs(e).overflowX, oy: cs(e).overflowY, kids: e.children.length})),
    longest: Math.max(0, ...got.map(e => e.title.length)),
    scrollTitled: got.filter(e => /auto|scroll/.test(cs(e).overflowX + ' ' + cs(e).overflowY)).length};
};
"""

SYNTH = r"""
// synthetic cases: real truncated text still gets its full name; nothing else does
const mkd = (css, html) => { const d = document.createElement('div'); d.style.cssText = 'position:fixed;left:0;top:0;' + css; d.innerHTML = html || ''; document.body.appendChild(d); return d; };
const LONG = 'A very long file name that cannot fit in sixty pixels at all.gcode.3mf';
const ell = mkd('width:60px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis', LONG);
const clamp = mkd('width:60px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical', LONG);
const scrollEll = mkd('width:60px;overflow:auto;white-space:nowrap;text-overflow:ellipsis', LONG);                           // a scroll container with an ellipsis: still a scroller
const scrollList = mkd('width:200px;height:30px;overflow-y:auto', Array.from({length: 30}, (_, i) => '<div>row ' + i + ' of a long list</div>').join(''));
const rowEll = mkd('width:60px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis', '<b>a</b><i>b</i><u>c</u><s>' + LONG + '</s>');   // a row with 4 child elements
const longText = mkd('width:60px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis', LONG.repeat(5));                   // more than 160 characters: no tooltip at all
for(const e of [ell, clamp, scrollEll, scrollList, scrollList.firstElementChild, rowEll, longText]) hov(e);
R.synth = {ell: ell.title, clamp: clamp.title, scrollEll: scrollEll.title, scrollList: scrollList.title, scrollListKid: scrollList.firstElementChild.title, rowEll: rowEll.title, longText: longText.title,
  longLen: longText.textContent.length};
[ell, clamp, scrollEll, scrollList, rowEll, longText].forEach(e => e.remove());
"""

HISTORY = HIST_DRIVER.split("R.jobs = snap();")[0] + "R.jobs = snap();\n" + SWEEP + r"""
{ const r1 = sweep(document.getElementById('hi-body')); R.history = r1;
  const cont = [document.getElementById('hi-body'), document.querySelector('#hi-body .hi-wide'), document.querySelector('#hi-body .hi-list')].filter(Boolean), row = document.querySelector('#hi-body .tr.click');
  R.histEls = {list: cont.length, row: !!row, scrolls: cont.map(c => c.scrollHeight > c.clientHeight + 2 || getComputedStyle(c).overflowY)}; cont.forEach(hov); if(row) hov(row);
  R.histTitles = [...cont.map(c => c.title), row ? row.title : null]; }
""" + SYNTH

ANALYTICS = av.DRIVER.split("// ---- the page")[0] + r"""
await openAnalytics(); await wait(() => document.querySelector('#an-body .an-t')); await sleep(300);
""" + SWEEP + "R.analytics = sweep(document.getElementById('an-body'));\n"

FLEET = PRE + r"""
const LONGJOB = 'A really long job name for the shelf bracket set - twelve parts, both sides, with the lid and the clips and the spare feet.gcode.3mf';      // ~130 chars: cut by the card's 2-line clamp
FLEET = [mk('P1S', ['Home'], 'RUNNING', 7, 'VAULT Stack 1 - P1S_VAULT - 03 Drawer Shell 1F - 1x', {camera: true, remaining_min: 580, layer: '13/1150'}),
  mk('MK4', ['Home'], 'RUNNING', 19, LONGJOB, {camera: true, kind: 'prusalink', remaining_min: 297}), mk('Rook', ['Home'], 'IDLE', 0, '', {kind: 'moonraker'}),
  mk('Voron', ['Home'], 'UNKNOWN', 0, '', {camera: true, kind: 'moonraker'}), mk('Idler', ['Home'], 'IDLE', 0, 'Last one.gcode', {camera: true, kind: 'moonraker'}), mk('Press', ['Home'], 'PAUSE', 50, 'Paused one.gcode', {kind: 'moonraker'})];
await sleep(300); await setFleet(FLEET); await sleep(200);
""" + SWEEP + r"""
const cardsNow = [...document.querySelectorAll('#fleet .pcard')];
const lastC = cardsNow[cardsNow.length - 1], lr = lastC.getBoundingClientRect(), grid = document.getElementById('fleet');
const hole = document.elementFromPoint(lr.right + 24, (lr.top + lr.bottom) / 2);          // the empty area of the grid next to the last card
const blank = [lastC, lastC.querySelector('.erow') || lastC.querySelector('.stat'), lastC.querySelector('.cslot') || lastC.querySelector('.r2'), lastC.querySelector('.prow')].filter(Boolean);
const targets = [grid, hole, document.getElementById('home'), document.body, ...cardsNow, ...blank].filter(Boolean);
targets.forEach(hov);
R.fleet = sweep(grid);
R.fleetHole = {tag: hole ? hole.tagName + '#' + hole.id : null, isGrid: hole === grid, w: Math.round(lr.right + 24), vw: innerWidth, rows: new Set(cardsNow.map(c => Math.round(c.getBoundingClientRect().top))).size};
R.fleetTitled = targets.map(e => e.title || '');
R.fleetBox = grid.title; R.cardTitles = cardsNow.map(c => c.title);
R.scrolls = getComputedStyle(grid).overflowY;
const jobEl = [...document.querySelectorAll('#fleet .pcard')].find(c => c.dataset.name === 'MK4').querySelector('.job');
R.job = {cut: jobEl.scrollHeight > jobEl.clientHeight + 1, title: jobEl.title, text: jobEl.textContent};
R.allTitles = [...document.querySelectorAll('#fleet [title]')].map(e => ({tag: e.tagName, cls: String(e.className).slice(0, 24), len: e.title.length}));
"""


class StaticTests(unittest.TestCase):
    def test_the_helper_only_titles_real_truncated_text(self):
        i = HTML.index("function cutTitleFor(el){")
        fn = HTML[i:HTML.index("if(FVIEW.view === 'desktop') document.addEventListener('mouseover'", i)]
        self.assertIn("DT_MAX = 160", HTML)
        self.assertIn("overflowX", fn)                                     # scroll containers (auto / scroll) are never "cut short text"
        self.assertIn("overflowY", fn)
        self.assertIn("textOverflow", fn)                                  # an ellipsis ...
        self.assertIn("webkitLineClamp", fn)                               # ... or a line clamp
        self.assertIn("el.children.length > 2", fn)                        # a text leaf, not a row / list / panel
        self.assertIn("t.length <= DT_MAX", fn)                            # and capped
        self.assertNotIn("!== 'visible'", fn)                              # the old rule (anything that is not visible) is gone

    def test_the_handler_walks_up_at_most_four_ancestors_through_the_helper(self):
        i = HTML.index("if(FVIEW.view === 'desktop') document.addEventListener('mouseover'")
        h = HTML[i:i + 500]
        self.assertIn("n < 4", h)
        self.assertIn("cutTitleFor(el)", h)


class BrowserTests(unittest.TestCase):
    R = {}

    def go(self, key, driver, size=(1440, 900), wide=True):
        if key not in BrowserTests.R:
            drv = driver.replace("await sleep(300);", (WIDE if wide else "") + "await sleep(300);", 1) if wide and "await sleep(300);" in driver else driver
            BrowserTests.R[key] = run_q(self, drv, "?view=desktop", size, 120000)
        R = BrowserTests.R[key]
        self.assertNotIn("error", R, R.get("error"))
        return R

    def test_a_genuinely_cut_name_still_gets_its_full_name_and_nothing_else_does(self):
        S = self.go("history", HISTORY, (1440, 380))["synth"]
        full = "A very long file name that cannot fit in sixty pixels at all.gcode.3mf"
        self.assertEqual(S["ell"], full)                                   # text-overflow: ellipsis
        self.assertEqual(S["clamp"], full)                                 # a 2-line clamp
        self.assertEqual(S["scrollEll"], "")                               # overflow auto is a scroller, even with an ellipsis
        self.assertEqual(S["scrollList"], "")                              # a scrolling list never gets its whole text as a tooltip
        self.assertEqual(S["scrollListKid"], "")                           # (a plain row of it is not truncated text either)
        self.assertEqual(S["rowEll"], "")                                  # a row with several child elements
        self.assertEqual(S["longText"], "")                                # more than 160 characters: skipped
        self.assertGreater(S["longLen"], 160)

    def test_hovering_the_history_page_sets_no_long_title_and_none_on_a_scroll_container(self):
        R = self.go("history", HISTORY, (1440, 380))          # a short window: the History list really scrolls
        H = R["history"]
        self.assertGreater(H["n"], 50)                                     # the sweep really covered the whole page
        self.assertLessEqual(H["longest"], 160, H["titles"])
        self.assertEqual(H["scrollTitled"], 0, H["titles"])
        self.assertTrue(R["histEls"]["list"] >= 1 and R["histEls"]["row"])
        self.assertIn(True, R["histEls"]["scrolls"])                       # (a container that overflows: the case that used to put the whole list in a tooltip)
        self.assertEqual([t for t in R["histTitles"] if t], [])            # the list container(s) and a table row: no title at all

    def test_the_same_on_analytics(self):
        A = self.go("analytics", ANALYTICS)["analytics"]
        self.assertGreater(A["n"], 30)
        self.assertLessEqual(A["longest"], 160, A["titles"])
        self.assertEqual(A["scrollTitled"], 0, A["titles"])

    def test_hovering_the_fleet_grid_its_empty_area_and_a_cards_blank_area_sets_no_title(self):
        R = self.go("fleet", FLEET)
        F = R["fleet"]
        self.assertGreater(F["n"], 20)
        self.assertEqual(R["fleetHole"]["rows"], 2)                        # six cards in two rows: the last row has empty room beside its cards
        self.assertTrue(R["fleetHole"]["isGrid"], R["fleetHole"])         # (a) the point beside the last card really is the grid itself
        self.assertEqual(R["scrolls"], "auto")                             # ... a scroll container
        self.assertEqual(R["fleetBox"], "")                                # the grid gets no title
        self.assertEqual(R["cardTitles"], [""] * 6)                        # (b) no card gets one (hovering the card, its rows, its picture slot)
        self.assertEqual([t for t in R["fleetTitled"] if t], [])           # (a)+(b): grid, the empty spot, the page, body, every card, blank rows of the last card
        self.assertLessEqual(F["longest"], 160, F["titles"])
        self.assertLessEqual(max([t["len"] for t in R["allTitles"]] + [0]), 160, R["allTitles"])
        self.assertEqual(F["scrollTitled"], 0, F["titles"])

    def test_a_really_cut_job_name_inside_a_card_still_gets_its_full_name(self):
        J = self.go("fleet", FLEET)["job"]
        self.assertTrue(J["cut"], J)                                       # the 2-line clamp really cuts this name
        self.assertEqual(J["title"], J["text"])                            # ... and the tooltip is the whole name


if __name__ == "__main__":
    unittest.main()
