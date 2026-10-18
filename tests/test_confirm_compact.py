"""The start-print confirm dialog (the spool picker) fits the screen.

    python3 tests/test_confirm_compact.py

The real app/dashboard.html runs in headless Chrome / Edge in an iframe of exactly the screen size, with a stubbed `fetch` and generic
printer / AMS fixtures (tests/confirm_fixtures.py); nothing contacts a printer. Screens: the 10-inch kiosk (1024x600), the 5-inch wall
screen (800x480), the same size without the 5in layout, a phone (390x844) and a desktop window. Each is also run with a wide fallback
font (Verdana / DejaVu), because the Pi kiosk is Linux Chromium and the narrow macOS system font hides wrapping.

What must hold: nothing scrolls on the 1024x600 / 800x480 screens (a phone may scroll INSIDE the dialog), the bed-clear tick, Back and START
are on screen without scrolling (a phone: at the top and at the bottom of the scroll), every button is finger-sized, material mismatches
are said in words, a chosen spool carries a tick, the two-filament swap still works, and the start request is unchanged.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import confirm_fixtures as cf  # noqa: E402
import test_fix_dashboard as tfd  # noqa: E402

HTML = tfd.HTML
# screens where the whole dialog must fit without any scrolling
FIT = ("kiosk10", "five", "land", "desktop")


class ConfirmStaticTests(unittest.TestCase):
    def test_the_footer_is_outside_the_scrolling_part_and_carries_the_tick(self):
        dlg = HTML[HTML.index('$(\'mbody\').innerHTML = `<div class="cfdlg'):]
        dlg = dlg[:dlg.index("if(s.camera) cfStart();")]
        self.assertLess(dlg.index('class="cfscroll"'), dlg.index('class="cfactions"'))
        foot = dlg[dlg.index('class="cfactions"'):]
        for needle in ("${plateHtml}", 'onclick="closeModal()">Back</button>', 'id="startbtn"', 'data-act="start"'):
            self.assertIn(needle, foot, needle)
        self.assertNotIn('id="plateok"', dlg[:dlg.index('class="cfactions"')])                  # one tick, in the footer, for every kind of job
        self.assertIn('id="plateok" onchange="armStart()">', HTML)
        self.assertIn("CONFIRM: THE PRINT BED IS CLEAR", HTML)
        css = HTML[HTML.index("/* ==== start-print confirm"):HTML.index("/* phones: the page is a fixed")]
        self.assertNotIn("position:sticky", css)                                                  # no sticky-footer padding hacks any more

    def test_no_old_confirm_rules_are_left_behind_in_the_phone_or_landscape_blocks(self):
        for stale in (".cfactions { position:sticky", ".cfactions { bottom:", ".cfdlg { margin:", "#spoolgrid { grid-template-columns:repeat(2",
                      ".cfcam, .cfnocam { display:none; }", ".cfmulti .cfactions .toggle"):
            self.assertNotIn(stale, HTML, stale)

    def test_a_chosen_spool_is_a_tick_and_a_thick_border_never_colour_alone(self):
        fn = HTML[HTML.index("function highlightSpool(){"):HTML.index("function startSelected(){")]
        self.assertIn("aria-pressed", fn)
        self.assertIn("\\u2714", fn)
        self.assertNotIn("style.background", fn)
        self.assertIn('.msgrid button[aria-pressed="true"] { border:3px solid var(--accent);', HTML)

    def test_secondary_text_is_short_muted_lines_and_the_file_name_is_one_line(self):
        self.assertIn(".cffile .cfname { min-width:0; flex:0 1 auto; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }", HTML)
        self.assertIn("cfName:    b =>", HTML)                                   # tap: the whole name
        self.assertIn('title="${esc(String(d.display_name || d.file))}"', HTML)   # hover / long-press: the whole name
        self.assertIn("class=\"cfnote${String(n).trim().charAt(0) === '\\u26a0' ? ' cfwarn' : ''}\"", HTML)

    def test_the_confirm_wiring_other_suites_rely_on_is_unchanged(self):
        for needle in ("suggestedConfirm.indexOf('ph-') === 0", "const confirm = ticket ? suggestedConfirm :", "model_override:override",
                       "if(override) confirm += '|model_override';", "if(!$('plateok').checked){ return; }",
                       "const on = $('plateok').checked && (!window._modelBad || ($('modelok') && $('modelok').checked));"):
            self.assertIn(needle, HTML, needle)


@unittest.skipUnless(tfd.CHROME, "no Chrome / Edge found for the browser tests")
class ConfirmBrowserTests(unittest.TestCase):
    R = {}

    def run_it(self, scenario, key, wide=False):
        k = (scenario, key, wide)
        if k not in ConfirmBrowserTests.R:
            ConfirmBrowserTests.R[k] = cf.measure(self, scenario, key, wide)
            self.assertNotIn("error", ConfirmBrowserTests.R[k], ConfirmBrowserTests.R[k].get("error"))
            self.assertEqual(ConfirmBrowserTests.R[k]["alerts"], [])
        return ConfirmBrowserTests.R[k]

    # ---------------------------------------------------------------- fits, without scrolling
    def check_fits(self, scenario, key, wide):
        R = self.run_it(scenario, key, wide)
        label = f"{scenario}@{key}{' wide-font' if wide else ''}"
        for stage in ("open", "pre", "after"):
            M = R[stage]
            box = M["mbox"]
            self.assertTrue(box["t"] >= 0 and box["l"] >= 0 and box["b"] <= M["vh"] and box["r"] <= M["vw"], (label, stage, box))
            self.assertLessEqual(M["docW"], M["vw"], (label, stage, "page is wider than the screen"))
            self.assertFalse(M["hscroll"], (label, stage, "the dialog scrolls sideways"))
            cs = M["cscroll"]
            self.assertLessEqual(cs[0], cs[1], (label, stage, "the dialog has to scroll: content %d px in %d px" % tuple(cs)))
            self.assertEqual(M["clipped"], [], (label, stage))
            for k in ("startVis", "plateVis", "labelVis", "backVis"):
                self.assertTrue(M[k], (label, stage, k))
            if scenario == "model-bad":
                self.assertTrue(M["modelVis"], (label, stage, "the model-mismatch tick is off screen"))
        self.assertTrue(R["fnameOpen"]["startVis"] and R["fnameOpen"]["plateVis"], (label, "after opening the whole file name"))

    def test_everything_fits_without_scrolling_on_1024x600(self):
        for scn in cf.SCENARIOS:
            self.check_fits(scn, "kiosk10", False)

    def test_everything_fits_without_scrolling_on_800x480(self):
        for scn in cf.SCENARIOS:
            self.check_fits(scn, "five", False)

    def test_a_printer_without_an_ams_or_a_camera_fits_everywhere(self):
        for key in ("kiosk10", "five"):
            self.check_fits("no-ams", key, False)
        self.check_fits("no-ams", "five", True)
        self.check_phone("no-ams", False)
        R = self.run_it("no-ams", "five")
        self.assertIn("the loaded filament (no AMS on this printer)", R["pre"]["text"])
        self.assertIn("This printer prints with whatever filament is physically loaded", R["pre"]["text"])
        self.assertIn("No camera on this printer", R["pre"]["text"])
        self.assertEqual(R["started"][0]["ams_mapping"], [])

    def test_800x480_without_the_5in_layout_and_a_desktop_window_fit_too(self):
        self.check_fits("two-worst", "land", False)
        self.check_fits("model-bad", "land", False)
        self.check_fits("two-worst", "desktop", False)

    def test_with_a_wide_fallback_font_it_still_fits_on_1024x600(self):
        for scn in ("two-worst", "model-bad", "single-warn", "long-name"):
            self.check_fits(scn, "kiosk10", True)

    def test_with_a_wide_fallback_font_it_still_fits_on_800x480(self):
        for scn in ("two-worst", "model-bad", "single-warn", "long-name"):
            self.check_fits(scn, "five", True)

    # ---------------------------------------------------------------- the phone: scrolls inside, START and the tick never leave
    def check_phone(self, scenario, wide):
        R = self.run_it(scenario, "phone", wide)
        label = f"{scenario}@phone{' wide-font' if wide else ''}"
        M = R["pre"]
        self.assertEqual((M["mbox"]["t"], M["mbox"]["b"], M["mbox"]["l"], M["mbox"]["r"]), (0, 844, 0, 390), label)    # a full-screen sheet
        self.assertFalse(M["hscroll"], label)
        self.assertEqual(M["clipped"], [], label)
        for stage in ("open", "pre", "after", "top", "bottom"):                                                         # at the top AND at the bottom of the scroll
            for k in ("startVis", "plateVis", "labelVis", "backVis"):
                self.assertTrue(R[stage][k], (label, stage, k))
        self.assertEqual(R["bottom"]["foot"]["b"], 844, label)

    def test_a_phone_scrolls_inside_the_dialog_and_start_and_the_tick_stay_on_screen(self):
        for scn in ("single", "two-worst", "model-bad", "long-name"):
            self.check_phone(scn, False)
        self.check_phone("two-worst", True)
        self.check_phone("model-bad", True)

    # ---------------------------------------------------------------- touch, words
    def test_every_button_and_the_tick_rows_are_finger_sized(self):
        for scn, key, wide in (("two-worst", "kiosk10", False), ("model-bad", "kiosk10", False), ("two-worst", "five", False),
                               ("model-bad", "five", True), ("two-worst", "five", True), ("single", "five", False), ("two-worst", "phone", False),
                               ("model-bad", "phone", True), ("two-worst", "land", False), ("two-worst", "desktop", False)):
            R = self.run_it(scn, key, wide)
            for stage in ("pre", "after"):
                M = R[stage]
                self.assertGreaterEqual(M["minBtnH"], 44, (scn, key, stage, [b for b in M["buttons"] if b["h"] < 44]))
                self.assertGreaterEqual(M["toggle"]["h"], 44, (scn, key))                       # the whole tick row (box + words) is the target
                if M["modelToggle"]:
                    self.assertGreaterEqual(M["modelToggle"]["h"], 44, (scn, key))
            self.assertGreaterEqual(R["pre"]["start"]["h"], 44, (scn, key))
            self.assertGreaterEqual(R["pre"]["back"]["h"], 44, (scn, key))
            self.assertGreaterEqual(R["pre"]["back"]["w"], 44, (scn, key))

    def test_the_material_mismatch_is_said_in_words_on_every_screen(self):
        for key in ("kiosk10", "five", "phone", "land"):
            R = self.run_it("two-worst", key)
            txt = R["pre"]["text"]
            self.assertIn("File wants PETG — Slot 1 holds PLA. Allowed — check it is on purpose.", txt, key)
            self.assertIn("File wants PLA — Slot 2 holds PETG. Allowed — check it is on purpose.", txt, key)
            self.assertIn("Swapped: Filament 1 now uses Slot 1.", txt, key)
            self.assertIn("Same spools as your last print of this file (Oct 13)", txt, key)            # nothing was dropped: every note is still there
            self.assertIn("filament 2 is sliced for PLA but slot 2 holds PETG (your choice last time)", txt, key)
            for part in ("Live view of Printer-A", "check the bed is clear", "Bambu A1", "2h 5m", "44 g", "Filament 1", "main", "file colour GREEN", "38 g",
                         "5.6 g", "suggested", "START A PRINT ON Printer-A?"):
                self.assertIn(part, txt.replace(" ?", "?"), (key, part))

    def test_the_model_mismatch_warning_and_its_tick_are_still_required(self):
        for key in ("kiosk10", "five", "phone"):
            R = self.run_it("model-bad", key)
            self.assertIn("THIS FILE WAS SLICED FOR P1S", R["pre"]["text"], key)
            self.assertIn("I know this file is safe on this printer", R["pre"]["text"], key)
            self.assertFalse(R["armed"]["disabled"], key)                  # (the driver ticked both)
            self.assertEqual(R["started"][0]["model_override"], True, key)
            self.assertEqual(R["started"][0]["confirm"], "ph-TICKET|model_override", key)

    def test_a_chosen_spool_has_a_tick_in_words_not_only_a_colour(self):
        R = self.run_it("single", "five")
        on = [b for b in R["picked"] if b["on"] == "true"]
        self.assertEqual([b["tray"] for b in on], ["2"])
        self.assertTrue(on[0]["text"].startswith("✔ Slot 3"), on)
        self.assertTrue(all(not b["text"].startswith("✔") for b in R["picked"] if b["on"] != "true"))
        self.assertIn("★ suggested", " ".join(b["text"] for b in R["picked"]))
        self.assertEqual(R["started"][0]["ams_mapping"], [2])
        self.assertIn("Tap the spool to print with:", R["pre"]["text"])

    def test_the_file_name_is_one_line_and_the_whole_name_is_a_tap_away(self):
        R = self.run_it("model-bad", "five")
        self.assertTrue(R["fname"]["cut"])                                 # a long name ends in an ellipsis ...
        self.assertEqual(R["title"], "Wall bracket for the workshop shelf with the long name v12 final.gcode.3mf")        # ... the whole name is its title
        self.assertTrue(R["fnameOpen"]["open"])                            # ... and a tap shows all of it
        self.assertEqual(R["fnameOpen"]["aria"], "true")
        self.assertFalse(R["fnameOpen"]["cut"])
        self.assertLess(R["fname"]["h"], 30)                                # one line (about 21 px) before the tap

    # ---------------------------------------------------------------- behaviour is unchanged
    def test_the_two_filament_swap_and_the_start_request_still_work(self):
        for key in ("kiosk10", "five", "phone"):
            R = self.run_it("two-worst", key)
            self.assertEqual(len(R["started"]), 1, key)
            s = R["started"][0]
            self.assertEqual(s["ams_mapping"], [0, 1], key)               # filament 2 -> the PETG slot, so filament 1 swapped onto Slot 1
            self.assertEqual((s["confirm"], s["plate_clear"], s["model_override"], s["filename"]), ("ph-TICKET", True, False, "Hook.gcode.3mf"), key)

    def test_start_is_off_until_the_bed_is_confirmed(self):
        R = self.run_it("two", "five")
        self.assertTrue(R["disabledBefore"])                                # nothing starts without the human's tick
        self.assertFalse(R["armed"]["disabled"])
        self.assertEqual(R["armed"]["text"], "START PRINT ON Printer-A")
        self.assertEqual(len(R["started"]), 1)


if __name__ == "__main__":
    unittest.main()
