"""Static checks for the phone / Home Screen layout of the dashboard (portrait + landscape phones).

    python3 tests/test_phone_layout.py

Reads app/dashboard.html and app/server.py as text; starts nothing and opens no sockets.
The 1024x600 wall screen must not match any phone rule (it is checked in the screenshots, and here by the
media-query conditions: nothing phone-only may match a window that is 1024 px wide and 600 px tall).
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()
with open(os.path.join(HERE, "..", "app", "server.py"), encoding="utf8") as _fh:
    SERVER = _fh.read()

BANNER = "/* ==== phone layout ==== */"
PORTRAIT = "(max-width:600px)"
LANDSCAPE = "(orientation:landscape) and (max-height:500px) and (max-width:932px)"


def phone_block():
    i = HTML.index(BANNER if BANNER in HTML else "/* ==== phone layout ====")
    j = HTML.index("</style>", i)
    return HTML[i:j]


class TestPhoneLayout(unittest.TestCase):
    def test_viewport_covers_the_notch_and_status_bar_is_set(self):
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"', HTML)
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', HTML)
        self.assertIn('name="apple-mobile-web-app-status-bar-style" content="black-translucent"', HTML)
        self.assertIn('rel="apple-touch-icon" href="/apple-touch-icon.png"', HTML)
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', HTML)
        self.assertNotRegex(HTML, r"user-scalable\s*=\s*no")           # pinch-zoom stays available (accessibility)

    def test_phone_block_exists_and_uses_safe_areas(self):
        blk = phone_block()
        self.assertIn("/* ==== phone layout ====", HTML)
        for side in ("top", "bottom", "left", "right"):
            self.assertIn(f"env(safe-area-inset-{side}", blk, side)
        self.assertIn("var(--sat)", blk)
        self.assertIn("var(--sab)", blk)

    def test_phone_media_queries_never_match_the_wall_screen(self):
        """The wall screen is 1024x600 landscape: portrait phones are <= 600 wide, landscape phones <= 500 tall."""
        self.assertIn("@media (max-width:600px), " + LANDSCAPE, HTML)
        self.assertIn("@media (max-width:600px) {", HTML)
        self.assertIn("@media " + LANDSCAPE + " {", HTML)
        # the old phone breakpoint was widened to landscape phones, never to anything taller than 500 px
        self.assertNotIn("@media (max-width:760px) {", HTML)
        self.assertGreaterEqual(HTML.count("@media (max-width:760px), " + LANDSCAPE), 6)

    def test_inputs_are_16px_so_ios_does_not_zoom(self):
        blk = phone_block()
        self.assertRegex(blk, r"input, select, textarea \{ font-size:16px; \}")
        for sel, size in re.findall(r"\n\s*([^{}\n]*(?:input|select|\.stin)[^{}\n]*)\{[^}]*?font-size:(\d+)px", HTML):
            if "checkbox" in sel or "radio" in sel:
                continue
            self.assertGreaterEqual(int(size), 16, sel)

    def test_double_tap_zoom_is_off_on_controls(self):
        self.assertIn("touch-action:manipulation", phone_block())

    def test_touch_targets_and_sheets(self):
        blk = phone_block()
        self.assertIn("button, a.bkdl { min-height:44px; }", blk)
        self.assertRegex(blk, r"\.cfactions \{ position:sticky;")                 # START stays on screen
        self.assertRegex(blk, r"#modal \{ align-items:stretch;")                  # dialogs are full-screen sheets
        self.assertIn("#spoolgrid { grid-template-columns:repeat(2,minmax(0,1fr)) !important; }", blk)
        self.assertRegex(blk, r"\.sttabs \{[^}]*overflow-x:auto")                 # settings tabs scroll in their own strip

    def test_reload_button_stays_in_the_header(self):
        self.assertIn('title="Reload">&#8635;</button>', HTML)
        self.assertIn('button[title="Reload"]', phone_block())

    def test_manifest_fields(self):
        i = SERVER.index('@mcp.custom_route("/manifest.webmanifest"')
        body = SERVER[i:SERVER.index("@mcp.custom_route", i + 10)]
        for needle in ('"display": "standalone"', '"start_url": "/dashboard"', '"scope": "/"', '"id": "/dashboard"',
                       '"theme_color"', '"background_color"', 'icon("icon-192.png", 192)', 'icon("icon-512.png", 512)',
                       'icon("icon-maskable-512.png", 512, "maskable")'):
            self.assertIn(needle, body, needle)

    def test_no_service_worker_yet(self):
        self.assertNotIn("serviceWorker", HTML)                       # needs HTTPS; see "Use it on your phone" in docs/GUIDE.md

    def test_guide_has_the_phone_paragraph(self):
        with open(os.path.join(HERE, "..", "docs", "GUIDE.md"), encoding="utf8") as fh:
            g = fh.read()
        self.assertIn("### Use it on your phone", g)
        self.assertIn("Add to Home Screen", g)
        self.assertIn("tailscale serve", g)


if __name__ == "__main__":
    unittest.main()
