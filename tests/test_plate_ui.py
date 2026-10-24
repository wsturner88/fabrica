"""Plate contents on the screens: the file list / grid badge, the detail sheet line, the start-print dialog line, and the Klipper card warning.

    python3 tests/test_plate_ui.py

Static checks on app/dashboard.html plus browser runs (headless Chrome / Edge, skipped when none is found) of the real page with a stubbed
`fetch`: the 5-inch wall screen (800x480 with ?layout=5in), the 10-inch kiosk (1024x600), a phone (390x844) and a desktop window. Each is
also run with a wide fallback font (the Pi kiosk's Linux Chromium). Nothing contacts a printer. Names are generic.

The rules the owner set: never encode meaning in colour alone (the badge is words in a frame), vertical space is precious (the badge is
inline in the text line that already exists, the warning replaces an existing card line - no row is added anywhere), and an "unknown"
plate shows nothing at all.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_fix_dashboard as tfd  # noqa: E402
from test_layout_5in import run_q  # noqa: E402
from test_parity_layouts import PRE, WIDE  # noqa: E402
import confirm_fixtures as cf  # noqa: E402

HTML = tfd.HTML
NODE = shutil.which("node")
MISSING = 'No object labels — can\'t count parts or cancel one object. Turn on "Label objects" in the slicer.'
WARN = "⚠️ Exclude objects not set up in printer.cfg"
PLATE = {"labels": "present", "source": "m486", "total": 6, "groups": [{"name": "Hose Clip", "count": 4}, {"name": "Hook", "count": 2}],
         "summary": "4× Hose Clip + 2× Hook", "plate": 1}


class StaticTests(unittest.TestCase):
    def test_one_marked_block_of_each_kind_and_the_text_the_owner_asked_for(self):
        self.assertIn("/* ==== plate contents ====", HTML)
        self.assertIn("/* ==== end plate contents ==== */", HTML)
        self.assertIn("// ==== plate contents ====", HTML)
        self.assertIn("// ==== end plate contents ====", HTML)
        self.assertIn("const PLATE_MISSING = 'No object labels \\u2014 can\\'t count parts or cancel one object. Turn on \"Label objects\" in the slicer.';", HTML)
        self.assertIn("\\u26a0\\ufe0f No labels", HTML)
        self.assertIn("Plate: ", HTML)
        self.assertIn("Exclude objects not set up in printer.cfg", HTML)

    def test_meaning_never_rests_on_colour(self):
        css = HTML[HTML.index("/* ==== plate contents ===="):HTML.index("/* ==== end plate contents ==== */")]
        for rule in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            props = rule[1]
            self.assertNotRegex(props, r"#[0-9a-fA-F]{3,6}\b", rule[0])          # no hard-coded hue
            self.assertNotIn("var(--ok)", props)
            self.assertNotIn("var(--bad)", props)
            self.assertNotIn("var(--warn)", props)
        self.assertIn("border:1px solid var(--txt)", css)                          # a frame + bold words

    def test_the_hooks_in_the_existing_screens_are_one_expression_each(self):
        self.assertIn("${cur ? '' : plateBadge(f)}${meta}", HTML)                  # inline in the file row's existing second line
        self.assertIn("plateTile(f)", HTML)                                        # an overlay on the grid tile's picture
        self.assertIn("rows += plateRow(d);", HTML)                                # one row of the detail sheet
        self.assertIn("plateMissing(r) ? '\\u26a0\\ufe0f No labels' : ''", HTML)            # the library's second line says it in words too (no frame, no new row)
        self.assertIn("${row('Plate', plateSummary(r)", HTML)                      # and its detail panel has the Plate row
        self.assertIn("${cfPlateHtml(d)}${spoolHtml}${notesCf}", HTML)             # the start dialog: above the spool rows, in the column that has room
        self.assertIn("if(k === 'idle' && exclWarn(p)) xtra = exclWarn(p);", HTML)  # replaces an existing card line
        self.assertEqual(HTML.count("exclWarn(s)"), 1)                             # the printer screen: the one existing status line (not repeated in the status panel)

    def test_a_print_is_never_blocked_by_the_plate_line(self):
        fn = HTML[HTML.index("function cfPlateHtml(d){"):HTML.index("// a Klipper printer without [exclude_object]")]
        self.assertNotIn("disabled", fn)
        self.assertNotIn("startbtn", fn)


@unittest.skipUnless(NODE, "needs node")
class PageFunctions(unittest.TestCase):
    """The page's own helpers, run in node (no DOM)."""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            return
        a = HTML.index("// ==== plate contents ====\n// What is on the plate")
        b = HTML.index("// ==== end plate contents ====\nfunction fileRowHtml")
        pre = "const esc = s => String(s == null ? '' : s).replace(/[&<>\"'`]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;','`':'&#96;'}[c]));\n"
        cls.js = pre + HTML[a:b] + "\nmodule.exports = {plateBadge, plateTile, plateRow, cfPlateHtml, exclWarn, exwCls, plateSummary, plateMissing, PLATE_MISSING};"

    def run_js(self, body):
        r = subprocess.run([NODE, "-e", self.js + "\nconst M = module.exports;\n" + body], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_each_helper_for_present_missing_and_unknown(self):
        out = self.run_js("""
const present = {plate: %s}, missing = {plate: {labels: 'missing', summary: ''}}, unknown = {plate: {labels: 'unknown', summary: ''}}, none = {};
console.log(JSON.stringify({
  badge: [M.plateBadge(present), M.plateBadge(missing), M.plateBadge(unknown), M.plateBadge(none), M.plateBadge(null)],
  tile: [M.plateTile(present), M.plateTile(missing), M.plateTile(unknown)],
  row: [M.plateRow(present), M.plateRow(missing), M.plateRow(unknown), M.plateRow(none)],
  cf: [M.cfPlateHtml(present), M.cfPlateHtml(missing), M.cfPlateHtml(unknown), M.cfPlateHtml(none)],
}));
""" % json.dumps(PLATE))
        b = out["badge"]
        self.assertEqual((b[0], b[2], b[3], b[4]), ("", "", "", ""))             # present / unknown / nothing: no badge
        self.assertIn("⚠️ No labels", b[1])
        self.assertIn('class="pbadge"', b[1])
        self.assertIn('aria-label="' + MISSING.replace('"', "&quot;").replace("'", "&#39;"), b[1])
        self.assertEqual((out["tile"][0], out["tile"][2]), ("", ""))
        self.assertIn('class="fplate"', out["tile"][1])
        r = out["row"]
        self.assertEqual(r[0], "<dt>Plate</dt><dd>4× Hose Clip + 2× Hook</dd>")
        self.assertIn("No object labels — can&#39;t count parts or cancel one object. Turn on &quot;Label objects&quot; in the slicer.", r[1])
        self.assertEqual((r[2], r[3]), ("", ""))                                  # unknown shows nothing
        c = out["cf"]
        self.assertIn(">Plate: 4× Hose Clip + 2× Hook</div>", c[0])
        self.assertIn("cfwarn", c[1])
        self.assertIn('>⚠️ No object labels — can\'t count parts or cancel one object</div>', c[1])
        self.assertIn('title="No object labels — can&#39;t count parts or cancel one object. Turn on &quot;Label objects&quot; in the slicer."', c[1])
        self.assertEqual((c[2], c[3]), ("", ""))

    def test_a_hostile_summary_is_escaped(self):
        out = self.run_js("""
const evil = {plate: {labels: 'present', summary: '<img src=x onerror=alert(1)>"\\'', groups: []}};
console.log(JSON.stringify([M.plateRow(evil), M.cfPlateHtml(evil)]));
""")
        for h in out:
            self.assertNotIn("<img", h)
            self.assertIn("&lt;img", h)

    def test_the_card_warning_only_for_an_explicit_false(self):
        out = self.run_js("""
console.log(JSON.stringify([M.exclWarn({exclude_object: false, exclude_object_note: 'Exclude objects not set up in printer.cfg'}), M.exclWarn({exclude_object: false}),
  M.exclWarn({exclude_object: true}), M.exclWarn({exclude_object: null}), M.exclWarn({}), M.exclWarn(null), M.exwCls('\\u26a0\\ufe0f x'), M.exwCls('Last job')]));
""")
        self.assertEqual(out[0], WARN)
        self.assertEqual(out[1], WARN)
        self.assertEqual(out[2:6], ["", "", "", ""])
        self.assertEqual(out[6:], [" exw", ""])


# ---------------------------------------------------------------------------------------------- the real page in a browser
DRIVER = PRE + r"""
const NOW = Math.floor(Date.now() / 1000);
const PL = %(plate)s;
const mkf = (name, plate, extra = {}) => Object.assign({name, print_time: '1h 20m', modified: NOW - 3600, size: 5000, weight_g: 12, filaments: [{type: 'PLA', color_name: 'Black'}], plate}, extra);
let FILES = [mkf('Clips x6.gcode', PL), mkf('Bare plate.gcode', {labels: 'missing', source: null, total: null, groups: [], summary: ''}),
             mkf('Unread.gcode', {labels: 'unknown', source: null, total: null, groups: [], summary: ''}), mkf('Old entry.gcode', undefined),
             mkf('A rather long name for a file that has labels missing and a long second line.gcode', {labels: 'missing', source: null, total: null, groups: [], summary: ''})];
FLEET = [mk('Printer-A', ['Home'], 'IDLE', 0, 'Phone stand.gcode', {kind: 'moonraker', exclude_object: false, exclude_object_note: 'Exclude objects not set up in printer.cfg'}),
         mk('Printer-B', ['Home'], 'IDLE', 0, 'Lid.gcode', {kind: 'moonraker', exclude_object: true, exclude_object_note: null}),
         mk('Printer-C', ['Home'], 'IDLE', 0, 'Knob.gcode', {kind: 'bambu'}),
         mk('Printer-D', ['Home'], 'RUNNING', 40, 'Lid.gcode', {kind: 'moonraker', exclude_object: false, exclude_object_note: 'Exclude objects not set up in printer.cfg'})];
window.__stubs.push((u, m) => {
  const path = u.split('?')[0]; let mm;
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/files$/))) return J(FILES);
  if((mm = path.match(/^\/api\/printer\/([^/]+)\/filedetail$/))){
    const f = FILES.find(x => x.name === decodeURIComponent((u.match(/file=([^&]*)/) || [])[1] || ''));
    return f ? J(Object.assign({}, f, {display_name: f.name, print_seconds: 5400, layer_mm: 0.2, nozzle_mm: 0.4, storage: 'Klipper (gcodes folder)', cost: {available: false}, can_rename: true})) : J({error: 'no'}, 404);
  }
  if((mm = path.match(/^\/api\/printer\/([^/]+)$/))){
    const p = FLEET.find(x => x.printer === decodeURIComponent(mm[1])); if(!p) return null;
    return J({snapshot: p, trays: [], camera: false, can_connect: false, kind: p.kind, controls: {actions: [], allowed_now: [], light: ''}, detection: {state: 'no_camera', text: 'No camera'}});
  }
  return null;
});
__WIDE__
const txt = e => e ? e.textContent.replace(/\s+/g, ' ').trim() : '';
const rect = e => { const b = e.getBoundingClientRect(); return {t: Math.round(b.top), b: Math.round(b.bottom), l: Math.round(b.left), r: Math.round(b.right), w: Math.round(b.width), h: Math.round(b.height)}; };
const inView = e => { const b = e.getBoundingClientRect(); return b.left >= -1 && b.right <= innerWidth + 1 && b.top >= -1 && b.bottom <= innerHeight + 1; };
const inWidth = e => { const b = e.getBoundingClientRect(); return b.left >= -1 && b.right <= innerWidth + 1; };
const $q = s => [...document.querySelectorAll(s)];
await sleep(300); await fleet(); await sleep(200);
// ---- the fleet cards
const cardLine = c => c.querySelector('.xtra, .meta.pct');
R.cards = $q('#fleet .pcard').map(c => ({name: c.dataset.name, line: txt(cardLine(c)), exw: !!c.querySelector('.exw'), title: c.querySelector('.exw') ? c.querySelector('.exw').title : '', h: rect(c).h, bottom: rect(c).b,
  clipped: [...c.querySelectorAll('.exw')].some(e => e.scrollWidth > e.clientWidth + 1), font: (c.querySelector('.exw') ? parseFloat(getComputedStyle(c.querySelector('.exw')).fontSize) : 0)}));
R.vh = innerHeight;
// ---- the printer screen of a Klipper printer without exclude_object (idle)
openPrinter('Printer-A'); await sleep(700); if(L5){ filesOpen(); await sleep(300); }
R.screenA = {eta: txt($('p-eta')), layer: txt($('p-layer')), sub: txt($('pn-sub'))};
// ---- the file list (rows): the badge sits in the existing second line
const listBtn = $q('#p-view button, #files .fview button').find(b => txt(b).replace('✓ ', '') === 'List'); if(listBtn){ listBtn.click(); await sleep(300); }
const rows = () => $q('#files .frow');
R.rows = rows().map(row => ({name: txt(row.querySelector('.fname')), meta: txt(row.querySelector('.fmeta')), badge: txt(row.querySelector('.pbadge')), h: rect(row).h, bottom: rect(row).b,
  badgeIn: row.querySelector('.pbadge') ? inWidth(row.querySelector('.pbadge')) : null, metaLines: Math.round(rect(row.querySelector('.fmeta')).h / (parseFloat(getComputedStyle(row.querySelector('.fmeta')).lineHeight) || 16)),
  aria: row.querySelector('.pbadge') ? row.querySelector('.pbadge').getAttribute('aria-label') : ''}));
R.rowBadges = $q('#files .pbadge').length; R.rowCount = rows().length;
// ---- the grid: the badge is an overlay on the picture
const gridBtn = $q('#p-view button, #files .fview button').find(b => txt(b).replace('✓ ', '') === 'Grid'); if(gridBtn){ gridBtn.click(); await sleep(400); }
const tiles = () => $q('#files .ftile');
R.tiles = tiles().map(t => ({name: txt(t.querySelector('.ftname')), plate: txt(t.querySelector('.fplate')), h: rect(t).h, inPic: t.querySelector('.fplate') ? (b => b.left >= t.querySelector('.fpic').getBoundingClientRect().left - 1 && b.right <= t.querySelector('.fpic').getBoundingClientRect().right + 1 && b.bottom <= t.querySelector('.fpic').getBoundingClientRect().bottom + 1)(t.querySelector('.fplate').getBoundingClientRect()) : null}));
// ---- the detail sheet, for each kind
R.sheets = {};
for(const n of ['Clips x6.gcode', 'Bare plate.gcode', 'Unread.gcode', 'Old entry.gcode']){
  await fileDetail(n); await sleep(500);
  const mb = document.getElementById('mbody');
  const rowsDl = $q('#mbody .fd-dl dt').map(txt);
  R.sheets[n] = {text: txt(mb), dts: rowsDl, plateDd: (dt => dt ? txt(dt.nextElementSibling) : null)($q('#mbody .fd-dl dt').find(d => txt(d) === 'Plate')),
    print: inView(mb.querySelector('[data-act="fdPrint"]')), back: inView(mb.querySelector('[data-act="fdBack"]')),
    scroll: [mb.scrollHeight, mb.clientHeight], docW: document.documentElement.scrollWidth, vw: innerWidth};
  closeModal(); await sleep(100);
}
"""


def drv(wide=False):
    return DRIVER.replace("%(plate)s", json.dumps(PLATE)).replace("__WIDE__", WIDE if wide else "")


class BrowserTests(unittest.TestCase):
    R = {}
    SIZES = {"five": ("?layout=5in", (800, 480)), "kiosk": ("?kiosk=1", (1024, 600)), "phone": ("", (390, 844)), "desk": ("", (1440, 900))}

    def go(self, key, wide=False):
        k = (key, wide)
        if k not in BrowserTests.R:
            q, size = self.SIZES[key]
            BrowserTests.R[k] = run_q(self, drv(wide), q, size, 240000)
        R = BrowserTests.R[k]
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        return R

    def each(self, fn, wide=False):
        for key in self.SIZES:
            fn(key, self.go(key, wide))

    # ---------------------------------------------------------------- the file list / grid
    def test_only_the_file_without_labels_gets_a_badge_and_it_is_in_words(self):
        def check(key, R):
            self.assertEqual(R["rowCount"], 5, key)
            self.assertEqual(R["rowBadges"], 2, key)                                       # the two "missing" files; present / unknown / old entry: none
            by = {r["name"]: r for r in R["rows"]}
            for n in ("Bare plate", "A rather long name for a file that has labels missing and a long second line"):
                self.assertEqual(by[n]["badge"], "⚠️ No labels", (key, n))
                self.assertTrue(by[n]["badgeIn"], (key, n, "the badge is wider than the screen"))
                self.assertIn("No object labels", by[n]["aria"], key)
                self.assertTrue(by[n]["meta"].startswith("⚠️ No labels"), (key, by[n]["meta"]))      # first thing of the existing second line
            for n in ("Clips x6", "Unread", "Old entry"):
                self.assertEqual(by[n]["badge"], "", (key, n))
                self.assertNotIn("No labels", by[n]["meta"], key)
        self.each(check)

    def test_the_badge_adds_no_height_to_a_row(self):
        def check(key, R):
            by = {r["name"]: r for r in R["rows"]}
            plain = by["Unread"]["h"]
            self.assertLessEqual(abs(by["Bare plate"]["h"] - plain), 2, (key, "a row with the badge is as tall as one without (sub-pixel rounding aside)"))
            if key != "phone":                                                                    # (a phone row has room for a wrapped second line: its height is the picture + buttons)
                self.assertEqual(by["Bare plate"]["metaLines"], by["Unread"]["metaLines"], (key, "the badge does not add a line of text"))
            if key == "five":
                self.assertEqual({r["h"] for r in R["rows"]}, {53}, key)                    # the 5-inch sheet's fixed row height
        self.each(check)
        self.each(check, wide=True)

    def test_the_grid_tile_shows_the_word_over_the_picture_and_keeps_its_height(self):
        def check(key, R):
            tiles = {t["name"]: t for t in R["tiles"]}
            self.assertEqual(len(tiles), 5, key)
            self.assertEqual(tiles["Bare plate"]["plate"], "⚠️ No labels", key)
            self.assertTrue(tiles["Bare plate"]["inPic"], key)
            for n in ("Clips x6", "Unread", "Old entry"):
                self.assertEqual(tiles[n]["plate"], "", (key, n))
            self.assertEqual(len({t["h"] for t in R["tiles"]}), 1, (key, "all tiles are one height"))
        self.each(check)
        self.each(check, wide=True)

    # ---------------------------------------------------------------- the detail sheet
    def test_the_sheet_says_what_is_on_the_plate_or_that_labels_are_missing_or_nothing(self):
        def check(key, R):
            S = R["sheets"]
            self.assertEqual(S["Clips x6.gcode"]["plateDd"], "4× Hose Clip + 2× Hook", key)
            self.assertEqual(S["Bare plate.gcode"]["plateDd"], "⚠️ " + MISSING, key)
            for n in ("Unread.gcode", "Old entry.gcode"):
                self.assertIsNone(S[n]["plateDd"], (key, n))                                  # unknown: no row, no word
                self.assertNotIn("Plate", S[n]["dts"], key)
            self.assertEqual(S["Clips x6.gcode"]["dts"].count("Plate"), 1, key)
            for n, s in S.items():
                self.assertTrue(s["print"] and s["back"], (key, n, "Print / Back must stay on screen"))
                self.assertLessEqual(s["docW"], s["vw"], key)
        self.each(check)
        self.each(check, wide=True)

    # ---------------------------------------------------------------- the Klipper cards and screen
    def test_a_klipper_card_without_exclude_object_says_so_in_words_and_keeps_the_card_shape(self):
        def check(key, R):
            cards = {c["name"]: c for c in R["cards"]}
            self.assertEqual(len(cards), 4, key)
            line = cards["Printer-A"]["line"]
            if key in ("five", "kiosk"):                                                       # a narrow card slot may use a shorter wording; the whole sentence is its tooltip
                self.assertIn(line, [WARN, "\u26a0\ufe0f Exclude objects not set up", "\u26a0\ufe0f No exclude objects"], key)
                self.assertEqual(cards["Printer-A"]["title"], "Exclude objects not set up in printer.cfg", key)
            else:
                self.assertEqual(line, WARN, key)
            self.assertTrue(cards["Printer-A"]["exw"], key)
            self.assertFalse(cards["Printer-A"]["clipped"], (key, "the warning must be read whole"))
            for n in ("Printer-B", "Printer-C"):                                              # has it / not a Klipper printer: nothing
                self.assertNotIn("Exclude objects", cards[n]["line"], (key, n))
                self.assertFalse(cards[n]["exw"], (key, n))
            self.assertNotIn("Exclude objects", cards["Printer-D"]["line"], (key, "a printing card keeps its live numbers"))
            self.assertEqual(len({c["h"] for c in R["cards"]}), 1, (key, [c["h"] for c in R["cards"]]))
            if key != "phone":                                                                    # (a phone stacks the cards and scrolls the page)
                self.assertLessEqual(max(c["bottom"] for c in R["cards"]), R["vh"], key)
            self.assertGreaterEqual(cards["Printer-A"]["font"], 10, key)                       # shrunk to fit, never to nothing
        self.each(check)
        self.each(check, wide=True)

    def test_the_printer_screen_of_that_printer_says_it_too(self):
        def check(key, R):
            self.assertEqual(R["screenA"]["layer"], WARN, key)
            self.assertEqual(R["screenA"]["sub"], "Ready to print", key)               # said once, not twice
        for key in ("five", "kiosk", "desk"):
            check(key, self.go(key))


@unittest.skipUnless(tfd.CHROME, "no Chrome / Edge found for the browser tests")
class ConfirmDialogTests(unittest.TestCase):
    """The start-print dialog (the spool picker): one compact line, or the warning; it never blocks START."""
    R = {}

    def run_it(self, scn, key, wide=False):
        k = (scn, key, wide)
        if k not in ConfirmDialogTests.R:
            ConfirmDialogTests.R[k] = cf.measure(self, scn, key, wide)
            self.assertNotIn("error", ConfirmDialogTests.R[k], ConfirmDialogTests.R[k].get("error"))
        return ConfirmDialogTests.R[k]

    def test_the_line_is_there_in_words_on_every_screen(self):
        for key in ("kiosk10", "five", "phone", "land", "desktop"):
            R = self.run_it("plate", key)
            self.assertIn("Plate: 4× Hose Clip + 2× Hook", R["pre"]["text"], key)
            R = self.run_it("plate-missing", key)
            self.assertIn("⚠️ No object labels — can't count parts or cancel one object", R["pre"]["text"], key)
            R = self.run_it("two", key)
            self.assertNotIn("Plate:", R["pre"]["text"], key)                                  # nothing known: nothing said
            self.assertNotIn("No object labels", R["pre"]["text"], key)

    def test_the_worst_case_still_fits_and_start_still_works(self):
        for scn in ("plate-missing-worst",):
            for key, wide in (("kiosk10", False), ("kiosk10", True), ("five", False), ("five", True), ("land", False), ("desktop", False)):
                R = self.run_it(scn, key, wide)
                for stage in ("open", "pre", "after"):
                    cs = R[stage]["cscroll"]
                    self.assertLessEqual(cs[0], cs[1], (scn, key, wide, stage, "the dialog has to scroll: %d px in %d px" % tuple(cs)))
                self.assertIn("No object labels", R["pre"]["text"], (scn, key))
                self.assertEqual(R["started"][0]["model_override"], True, (scn, key))
        for key, wide in (("kiosk10", False), ("kiosk10", True), ("five", False), ("five", True), ("land", False), ("desktop", False)):
            R = self.run_it("plate-worst", key, wide)
            label = (key, wide)
            for stage in ("open", "pre", "after"):
                M = R[stage]
                cs = M["cscroll"]
                self.assertLessEqual(cs[0], cs[1], (label, stage, "the dialog has to scroll: %d px in %d px" % tuple(cs)))
                for k in ("startVis", "plateVis", "labelVis", "backVis"):
                    self.assertTrue(M[k], (label, stage, k))
            self.assertIn("Plate: 4× Hose Clip with a long name", R["pre"]["text"], label)
            self.assertFalse(R["armed"]["disabled"], label)                                   # ticked bed + model: START is live
            self.assertEqual(R["started"][0]["model_override"], True, label)

    def test_a_warning_never_blocks_a_print(self):
        R = self.run_it("plate-missing", "five")
        self.assertTrue(R["disabledBefore"])                                                   # (disabled until the bed is confirmed, as ever)
        self.assertFalse(R["armed"]["disabled"])
        self.assertEqual(R["started"][0]["confirm"], "ph-TICKET")
        self.assertEqual(R["started"][0]["ams_mapping"], [1, 0])

    def test_the_phone_scrolls_inside_and_keeps_start_on_screen(self):
        R = self.run_it("plate-worst", "phone")
        for stage in ("open", "pre", "after", "top", "bottom"):
            for k in ("startVis", "plateVis", "labelVis", "backVis"):
                self.assertTrue(R[stage][k], (stage, k))


if __name__ == "__main__":
    unittest.main(verbosity=1)
