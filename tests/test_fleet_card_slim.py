"""Tests for the slimmer fleet cards in dashboard.html: no "Nozzle .. Bed .." line, no "Spaghetti: WATCHING" line - a small Spaghetti BADGE (a shape + a word) on the
right end of the "Done ~6:59 PM" row, the camera's update time as a small "9:19 AM" overlay on the picture's corner, the job's material instead of the layer
when the printer reports no layers, and the Spaghetti WARNING block + Pause / Cancel row unchanged.

    python3 tests/test_fleet_card_slim.py

Static part: dashboard.html / server.py read as text. Browser part: the REAL page in headless Chrome / Edge with a stubbed `fetch` (skipped when no browser is
found); nothing contacts a printer or the network. Widths are compared with the page's own width (CI's Linux Chrome draws a 15 px scrollbar), and the page is
also run with the wide fallback font Linux machines have.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from test_layout_5in import run_q  # noqa: E402
from test_parity_layouts import PRE, WIDE  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()
with open(os.path.join(HERE, "..", "app", "server.py"), encoding="utf8") as _fh:
    SERVER = _fh.read()

# card heights of the same four cards BEFORE this change (measured on the previous commit): desktop 1440 x 900, 10-inch 1024 x 600, phone 390 x 844 (printing card)
BEFORE = {"desktop": 471, "kiosk": 413, "phone": 472}

DRIVER = PRE + r"""
// the picture loader reports a fresh frame at once (a stub with no network): the card's update time is then set by the page's own code
window.Image = class { constructor(){ this.onload = null; this.onerror = null; this._s = ''; } set src(v){ this._s = v; setTimeout(() => { if(this.onload) this.onload(); }, 5); } get src(){ return this._s; } };
FLEET = [mk('P1S', ['Home'], 'RUNNING', 7, 'VAULT Stack 1 - P1S_VAULT - 03 Drawer Shell 1F - 1x', {camera: true, remaining_min: 580, layer: '13/1150', nozzle_target_c: 220, bed_target_c: 58, nozzle_c: 220, bed_c: 58}),
  mk('MK4', ['Home'], 'RUNNING', 19, 'Phone Stand - MK4_0.4n_0.2mm_PETG_5h48m', {camera: true, kind: 'prusalink', remaining_min: 297, layer: '', job_materials: ['PETG']}),
  mk('Plain', ['Home'], 'RUNNING', 33, 'No material known.gcode', {camera: true, kind: 'moonraker', remaining_min: 61, layer: ''}),
  mk('Rook', ['Home'], 'IDLE', 0, '', {kind: 'moonraker'}), mk('Voron', ['Home'], 'UNKNOWN', 0, '', {camera: true, kind: 'moonraker'}),
  mk('Idler', ['Home'], 'IDLE', 0, 'Last one.gcode', {camera: true, kind: 'moonraker'})];
const vis = e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && getComputedStyle(e).display !== 'none'; };
const txt = e => e ? e.textContent.replace(/\s+/g, ' ').trim() : null;
const cards = () => [...document.querySelectorAll('#fleet .pcard')];
const snap = () => ({vw: document.documentElement.clientWidth, docW: document.documentElement.scrollWidth, vh: innerHeight, cards: cards().map(c => {
  const q = s => c.querySelector(s), sp = q('.spb'), st = q('.camstat'), blk = q('.meta.spg.alert, .spag.alert'), cr = r(c);
  return {name: c.dataset.name, kind: c.className, h: cr.h, w: cr.w, b: cr.b, text: txt(c), tmp: c.querySelectorAll('.meta.tmp').length, tempWords: /Nozzle \d|Bed \d/.test(txt(c)), temps: !!q('.temps'),
    spgLines: [...c.querySelectorAll('.meta.spg:not(.alert)')].length, updated: /Updated/.test(txt(c)),
    eta: txt(q('.eta')), pct: txt(q('.meta.pct')), sub: txt(q('.stat .sub')), xtra: txt(q('.stat .xtra')),
    badge: sp ? {word: txt(sp), title: sp.title, aria: sp.getAttribute('aria-label'), role: sp.getAttribute('role'), cls: sp.className, r: r(sp), bw: parseFloat(getComputedStyle(sp).borderTopWidth), svg: !!sp.querySelector('svg use'), icon: sp.querySelector('use') ? sp.querySelector('use').getAttribute('href') : null,
      inside: r(sp).l >= cr.l && r(sp).r <= cr.r + 0.5, vis: vis(sp), sameRow: q('.eta') ? Math.abs((r(sp).t + r(sp).b) / 2 - (r(q('.eta')).t + r(q('.eta')).b) / 2) <= 6 : null, atRight: q('.erow') ? r(q('.erow')).r - r(sp).r <= 2 : null} : null,
    stat: st ? {text: txt(st), vis: vis(st), rect: r(st), title: st.title} : null, cam: q('.cardcam') ? r(q('.cardcam')) : null, camimg: q('.cardcam .camimg') ? r(q('.cardcam .camimg')) : null,
    spq: [...c.querySelectorAll('.spq button')].map(txt), block: blk ? {text: txt(blk), bg: getComputedStyle(blk).backgroundColor, color: getComputedStyle(blk).color, h: r(blk).h, role: blk.getAttribute('role'), act: blk.dataset.act} : null,
    shadow: getComputedStyle(c).boxShadow, over: [...c.querySelectorAll('.eta, .meta, .stat > *, .erow')].filter(e => e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).display !== '-webkit-box' && !e.classList.contains('tail')).map(e => String(e.className))};
}), banner: (() => { const b = document.getElementById('sp-alert'); return {hidden: b.hidden, text: b.textContent}; })()});
await sleep(300); await setFleet(FLEET); await sleep(200);
FC && Object.values(FC.items).forEach(it => { camLoad(it); }); await sleep(300);
R.calm = snap();
DET = Object.assign({}, DET, {printers: {'P1S': {watching: true, excluded: false, verdict: {level: 'warning', score: 0.7, warmup: false}, alerts: [1]}, 'MK4': {watching: true, excluded: true}}});
await fleet(); await sleep(150); R.warn = snap();
DET.printers.P1S.verdict.level = 'failure'; await fleet(); await sleep(150); R.fail = snap();
DET.printers.P1S.verdict = {level: 'ok', score: 0.1, warmup: false}; DET.detector = {state: 'down'}; await fleet(); await sleep(150); R.down = snap();
"""


class StaticTests(unittest.TestCase):
    def test_the_card_has_no_temperature_line_and_no_spaghetti_line_of_words(self):
        i = HTML.index("function renderFleet(data){")
        fn = HTML[i:HTML.index("// nothing changed since the last poll", i)]
        self.assertNotIn("meta tmp", fn)
        self.assertNotIn("Nozzle ${tnum", fn)
        self.assertIn('<div class="erow">', fn)
        self.assertIn("spBadge(spag)", fn)
        self.assertIn('class="meta spg alert"', fn)                           # the BIG warning block is kept
        self.assertIn("spQuick(p.printer, p.state)", fn)                      # ... and the Pause / Cancel row

    def test_the_badge_is_a_shape_plus_a_word_with_the_old_title_text(self):
        i = HTML.index("const SPB = {")
        fn = HTML[i:HTML.index("}\n", HTML.index("function spBadge(sp){", i)) + 2]
        for k, ic, w in (("on", "eye", "OK"), ("off", "eyeoff", "OFF"), ("nocamera", "camoff", "NO CAM"), ("alert", "warn", "WARN")):
            self.assertRegex(fn, r"%s:\['%s','%s'\]" % (k, ic, w))
        self.assertIn("'Spaghetti: ' + sp.word", fn)                          # "Spaghetti: WATCHING" / "WARNING" / "OFF" / "NO CAMERA" stay as the title + aria text
        self.assertIn('role="img"', fn)
        self.assertIn('id="i-warn"', HTML)                                     # the triangle

    def test_the_update_time_is_a_small_overlay_on_the_picture(self):
        self.assertIn("clockShort()", HTML)
        self.assertRegex(HTML, r"html:not\(\.lay-5in\) \.cardcam \.camstat \{ position:absolute; right:7px; bottom:7px;")

    def test_the_material_comes_from_the_hub_snapshot_cached_file_facts(self):
        self.assertIn("def _job_materials(prn, snap)", SERVER)
        self.assertIn("_with_job_materials(prn, LINKS.apply(prn.name, prn.snapshot()))", SERVER)     # /api/fleet and /api/printer/<name>
        self.assertEqual(SERVER.count("_with_job_materials(prn, LINKS.apply("), 2)
        self.assertIn('xtra = k === \'paused\' ? \'Tap to resume or cancel\' : (hasLy ?', HTML)
        self.assertIn("jobMats(p).slice(0, 2).join(' + ')", HTML)


class ServerTests(unittest.TestCase):
    """_job_materials answers from caches only and only while a print runs (no network, no guess)."""

    def test_only_a_running_job_with_a_file_that_names_its_materials(self):
        import server  # noqa: E402
        class Prn:
            name = "Mk4"
        server._JOBMAT_MEMO.clear()
        server._FILES_SEEN["mk4"] = {"phone stand": {"name": "Phone Stand.gcode", "materials": ["petg"]}, "multi": {"name": "multi.gcode", "materials": ["PLA", "pla", "PETG"]},
                                     "single": {"name": "single.gcode", "filament_type": "ABS"}}
        try:
            f = server._job_materials
            self.assertEqual(f(Prn, {"job": "Phone Stand", "state": "RUNNING"}), ["PETG"])
            self.assertEqual(f(Prn, {"job": "multi.gcode", "state": "PAUSE"}), ["PLA", "PETG"])
            self.assertEqual(f(Prn, {"job": "single", "state": "RUNNING"}), ["ABS"])
            self.assertEqual(f(Prn, {"job": "Phone Stand", "state": "IDLE"}), [])             # a finished / idle printer: nothing
            self.assertEqual(f(Prn, {"job": "", "state": "RUNNING"}), [])
            self.assertEqual(f(Prn, {"job": "unknown file", "state": "RUNNING"}), [])        # not in any cache: no word
            self.assertNotIn("job_materials", server._with_job_materials(Prn, {"job": "unknown file", "state": "RUNNING"}))
            self.assertEqual(server._with_job_materials(Prn, {"job": "single", "state": "RUNNING"})["job_materials"], ["ABS"])
        finally:
            server._FILES_SEEN.pop("mk4", None)
            server._JOBMAT_MEMO.clear()


    def test_a_job_whose_file_sits_in_a_folder_is_still_found(self):
        """OctoPrint names the running job without its folder; the file list keys it with the folder (MK4, 10/26)."""
        import server  # noqa: E402
        class Prn:
            name = "Mk4"
        server._JOBMAT_MEMO.clear()
        server._FILES_SEEN["mk4"] = {
            "jobs/phone stand - mk4_0.4n_0.2mm_petg_5h48m": {"name": "Jobs/Phone Stand - MK4_0.4n_0.2mm_PETG_5h48m.gcode", "materials": ["PETG"]},
            "a/twin": {"name": "a/twin.gcode", "materials": ["PLA"]}, "b/twin": {"name": "b/twin.gcode", "materials": ["ABS"]}}
        try:
            f = server._job_materials
            self.assertEqual(f(Prn, {"job": "Phone Stand - MK4_0.4n_0.2mm_PETG_5h48m", "state": "RUNNING"}), ["PETG"])
            self.assertEqual(f(Prn, {"job": "twin", "state": "RUNNING"}), [])                   # same name in two folders: ambiguous -> no word
            self.assertEqual(server._seen_entry(Prn, "Phone Stand - MK4_0.4n_0.2mm_PETG_5h48m.gcode")["materials"], ["PETG"])
            # the live case: the raw OctoPrint listing has NO materials - they sit in the metadata cache (G-code header read)
            server._JOBMAT_MEMO.clear()
            server._FILES_SEEN["mk4"] = {"jobs/phone stand": {"name": "Jobs/Phone Stand.gcode", "size": 123}}
            with server._cache_lock:
                cache = server._cache_load(); cache["Mk4|Jobs/Phone Stand.gcode|123"] = {"materials": ["PETG"], "weight_g": 99.9}; server._cache_save(cache)
            self.assertEqual(f(Prn, {"job": "Phone Stand", "state": "RUNNING"}), ["PETG"])
            self.assertEqual(server._seen_entry(Prn, "Phone Stand")["weight_g"], 99.9)
        finally:
            server._FILES_SEEN.pop("mk4", None)
            server._JOBMAT_MEMO.clear()


class BrowserTests(unittest.TestCase):
    R = {}

    LAYOUTS = {"desktop": ("?view=desktop", (1440, 900)), "kiosk": ("?kiosk=1", (1024, 600)), "phone": ("", (390, 844))}

    def go(self, name, wide=False):
        k = (name, wide)
        if k not in BrowserTests.R:
            q, size = self.LAYOUTS.get(name) or {"5in": ("?layout=5in", (800, 480)), "bridge": ("?layout=5in&theme=bridge", (800, 480))}[name]
            drv = DRIVER.replace("await sleep(300); await setFleet(FLEET);", (WIDE if wide else "") + "await sleep(300); await setFleet(FLEET);", 1)
            BrowserTests.R[k] = run_q(self, drv, q, size, 120000)
        R = BrowserTests.R[k]
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        return R

    @staticmethod
    def card(S, name):
        return next(c for c in S["cards"] if c["name"] == name)

    # ------------------------------------------------------------------ the lines that are gone
    def test_no_card_has_a_nozzle_bed_line_or_a_spaghetti_line_of_words_or_an_updated_line(self):
        for lay in ("desktop", "kiosk", "phone"):
            S = self.go(lay)["calm"]
            for c in S["cards"]:
                self.assertEqual(c["tmp"], 0, (lay, c["name"]))
                self.assertFalse(c["tempWords"], (lay, c["name"], c["text"]))
                self.assertEqual(c["spgLines"], 0, (lay, c["name"]))
                self.assertNotIn("Spaghetti:", c["text"], (lay, c["name"]))
                self.assertFalse(c["updated"], (lay, c["name"]))                   # "Updated 9:19:03 AM" left the card's text

    def test_the_card_is_shorter_than_before_on_every_layout(self):
        for lay in ("desktop", "kiosk", "phone"):
            S = self.go(lay)["calm"]
            self.assertLessEqual(self.card(S, "P1S")["h"], BEFORE[lay] - 55, (lay, self.card(S, "P1S")["h"]))        # the mock-up: about 75 px shorter
            for c in S["cards"]:
                self.assertLessEqual(c["h"], BEFORE[lay] - 55, (lay, c["name"]))

    # ------------------------------------------------------------------ the badge
    def test_the_badge_sits_at_the_right_end_of_the_done_row_with_a_shape_a_word_and_the_old_title(self):
        for lay in ("desktop", "kiosk", "phone"):
            S = self.go(lay)["calm"]
            for name, word, title in (("P1S", "OK", "Spaghetti: WATCHING"), ("MK4", "OK", "Spaghetti: WATCHING"), ("Rook", "NO CAM", "Spaghetti: NO CAMERA"), ("Idler", "READY", "Spaghetti: READY")):
                b = self.card(S, name)["badge"]
                self.assertIsNotNone(b, (lay, name))
                self.assertEqual((b["word"], b["title"], b["aria"], b["role"]), (word, title, title, "img"), (lay, name))
                self.assertTrue(b["svg"], (lay, name))                           # a shape ...
                self.assertTrue(b["vis"] and b["inside"], (lay, name))
                self.assertTrue(b["sameRow"] and b["atRight"], (lay, name, b))   # ... on the "Done ~6:59 PM" row, at its right end
            self.assertEqual(self.card(S, "P1S")["badge"]["icon"], "#i-eye", lay)
            self.assertEqual(self.card(S, "Rook")["badge"]["icon"], "#i-camoff", lay)
            self.assertIsNone(self.card(S, "Voron")["badge"], lay)               # an offline printer: its words ("No answer from the printer") keep the whole row

    def test_the_off_for_this_printer_badge_has_the_eye_with_a_slash(self):
        for lay in ("desktop", "kiosk", "phone"):
            S = self.go(lay)["warn"]
            b = self.card(S, "MK4")["badge"]
            self.assertEqual((b["word"], b["title"], b["icon"]), ("OFF", "Spaghetti: OFF", "#i-eyeoff"), lay)

    def test_a_warning_is_a_triangle_the_word_warn_and_a_bold_frame(self):
        for lay in ("desktop", "kiosk", "phone"):
            for key, word, full in (("warn", "WARN", "WARNING"), ("fail", "FAIL", "FAILURE")):
                S = self.go(lay)[key]
                b = self.card(S, "P1S")["badge"]
                self.assertEqual((b["word"], b["title"], b["icon"]), (word, "Spaghetti: " + full, "#i-warn"), (lay, key))
                self.assertGreaterEqual(b["bw"], 3, (lay, key))                   # the bold frame (an ok badge has 1.5)
            ok = self.card(self.go(lay)["calm"], "P1S")["badge"]
            self.assertLess(ok["bw"], 3, lay)

    def test_a_detector_that_is_down_says_so_with_its_own_shape_and_word(self):
        for lay in ("desktop", "kiosk", "phone"):
            b = self.card(self.go(lay)["down"], "P1S")["badge"]
            self.assertEqual((b["word"], b["title"], b["icon"]), ("DOWN", "Spaghetti: DETECTOR DOWN", "#i-warn"), lay)

    # ------------------------------------------------------------------ the warning state is not weaker
    def test_the_warning_block_pause_cancel_row_frame_and_banner_are_still_there(self):
        for lay in ("desktop", "kiosk", "phone"):
            for key, word in (("warn", "WARNING"), ("fail", "FAILURE")):
                S = self.go(lay)[key]
                c = self.card(S, "P1S")
                self.assertEqual(c["block"]["text"], "⚠ Spaghetti: " + word, (lay, key))
                self.assertEqual((c["block"]["role"], c["block"]["act"]), ("button", "openPrinter"), (lay, key))     # tap = open that printer
                self.assertEqual(c["block"]["bg"] != "rgba(0, 0, 0, 0)", True, (lay, key))                          # an inverted block, not colour alone
                self.assertNotEqual(c["block"]["bg"], c["block"]["color"], (lay, key))
                self.assertEqual(c["spq"], ["Pause", "Cancel"], (lay, key))                                           # the same row as before
                self.assertIn("sp-alert", c["kind"], (lay, key))
                self.assertIn("3px", c["shadow"], (lay, key))                                                         # the heavy frame
                self.assertFalse(S["banner"]["hidden"], (lay, key))
                self.assertIn("P1S", S["banner"]["text"], (lay, key))
                for other in S["cards"]:
                    if other["name"] != "P1S":
                        self.assertNotIn("sp-alert", other["kind"], (lay, key, other["name"]))
                        self.assertIsNone(other["block"], (lay, key, other["name"]))
            self.assertIsNone(self.card(self.go(lay)["calm"], "P1S")["block"], lay)

    # ------------------------------------------------------------------ the update time
    def test_the_update_time_is_a_small_overlay_in_the_pictures_corner(self):
        for lay in ("desktop", "kiosk", "phone"):
            S = self.go(lay)["calm"]
            for name in ("P1S", "MK4"):
                c = self.card(S, name)
                self.assertIsNotNone(c["stat"], (lay, name))
                self.assertRegex(c["stat"]["text"], r"^\d{1,2}:\d\d [AP]M$", (lay, name))                           # "9:19 AM": no seconds, no word "Updated"
                self.assertRegex(c["stat"]["title"], r"^Updated \d{1,2}:\d\d:\d\d [AP]M$", (lay, name))             # (the seconds are in the tooltip)
                self.assertTrue(c["stat"]["vis"], (lay, name))
                s, p = c["stat"]["rect"], c["camimg"]
                self.assertTrue(p["l"] <= s["l"] and s["r"] <= p["r"] + 1 and p["t"] <= s["t"] and s["b"] <= p["b"] + 1, (lay, name, s, p))          # inside the picture ...
                self.assertGreater(s["l"] + s["r"], p["l"] + p["r"], (lay, name))                                   # ... on its right half ...
                self.assertGreater(s["t"] + s["b"], p["t"] + p["b"], (lay, name))                                   # ... and its lower half: the corner
                self.assertLessEqual(s["h"], 28, (lay, name))
                self.assertLessEqual(c["cam"]["h"], c["camimg"]["h"] + 1, (lay, name))                              # the picture is the whole camera element: nothing hangs below it

    # ------------------------------------------------------------------ the info line
    def test_the_info_line_keeps_the_layer_or_says_the_job_material_when_there_are_no_layers(self):
        for lay in ("desktop", "phone"):
            S = self.go(lay)["calm"]
            self.assertEqual(self.card(S, "P1S")["pct"], "7% · 9h 40m left · Layer 13 / 1150", lay)
            self.assertEqual(self.card(S, "MK4")["pct"], "19% · 4h 57m left · PETG", lay)         # no layers: the job's material (known from its file)
            self.assertEqual(self.card(S, "Plain")["pct"], "33% · 1h 1m left", lay)               # no layers and no material known: nothing is made up
            self.assertEqual(self.card(S, "Rook")["pct"], "", lay) if self.card(S, "Rook")["pct"] is not None else None
        self.assertNotIn("ABS", " ".join(c["pct"] or "" for c in self.go("desktop")["calm"]["cards"]))

    def test_idle_and_offline_cards_are_slim_too(self):
        for lay in ("desktop", "kiosk", "phone"):
            S = self.go(lay)["calm"]
            for name in ("Rook", "Idler", "Voron"):
                c = self.card(S, name)
                self.assertEqual((c["tmp"], c["spgLines"], c["updated"]), (0, 0, False), (lay, name))
                self.assertFalse(c["tempWords"], (lay, name))
            self.assertEqual(self.card(S, "Rook")["eta"], "Ready to print", lay)
            self.assertEqual(self.card(S, "Voron")["eta"], "No answer from the printer", lay)

    def test_with_a_wide_fallback_font_nothing_runs_off_the_card_or_the_page(self):
        for lay in ("desktop", "kiosk", "phone"):
            S = self.go(lay, wide=True)["calm"]
            self.assertLessEqual(S["docW"], S["vw"], lay)                                         # the page's own width (a 15 px scrollbar on CI)
            for c in S["cards"]:
                self.assertEqual([x for x in c["over"] if "pname" not in x], [], (lay, c["name"]))
                if c["badge"]:
                    self.assertTrue(c["badge"]["inside"], (lay, c["name"]))
            for key in ("warn", "down"):
                for c in self.go(lay, wide=True)[key]["cards"]:
                    if c["badge"]:
                        self.assertTrue(c["badge"]["inside"], (lay, key, c["name"]))

    # ------------------------------------------------------------------ the 5-inch screen (either theme)
    def test_the_5_inch_card_has_the_badge_instead_of_the_words_and_the_material_instead_of_the_layer(self):
        for lay in ("5in", "bridge"):
            R = self.go(lay)
            S = R["calm"]
            p1, mk4 = self.card(S, "P1S"), self.card(S, "MK4")
            self.assertEqual((p1["badge"]["word"], p1["badge"]["title"]), ("OK", "Spaghetti: WATCHING"), lay)
            self.assertEqual(self.card(S, "Rook")["badge"]["title"], "Spaghetti: NO CAMERA", lay)
            for c in S["cards"]:
                self.assertNotIn("Spaghetti:", c["text"], (lay, c["name"]))
                self.assertEqual(c["tmp"], 0, (lay, c["name"]))
            self.assertRegex((p1["xtra"] or "").upper(), r"LAYER 13 / 1150", lay)
            self.assertEqual((mk4["xtra"] or "").upper(), "PETG", lay)                            # no layers: the material
            self.assertEqual(self.card(S, "Plain")["xtra"], "", lay)
            w = R["warn"]
            self.assertEqual(self.card(w, "P1S")["block"]["text"].replace("⚠ ", "").upper(), "SPAGHETTI: WARNING", lay)    # the inverted block, as before
            self.assertEqual(self.card(w, "P1S")["spq"], ["Pause", "Cancel"], lay)
            self.assertEqual(self.card(w, "MK4")["badge"]["title"], "Spaghetti: OFF", lay)


if __name__ == "__main__":
    unittest.main()
