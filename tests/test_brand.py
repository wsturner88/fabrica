"""Tests for the Fabrica brand assets and their public routes (app/static/brand/, the icon routes in app/server.py).

    python3 tests/test_brand.py

Everything is local: the server module is imported with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointing at a
throwaway temp directory (never the real config.yml); no printer or host is contacted.
"""
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
BRAND = os.path.join(APP, "static", "brand")
sys.path.insert(0, APP)

_TMP = tempfile.mkdtemp(prefix="brand-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception:      # pragma: no cover - the app's deps are not installed
    HAVE = False

PNG_SIZES = {"apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512,
             "icon-maskable-512.png": 512, "favicon-32.png": 32}
SVGS = ("mark.svg", "mark-32.svg", "mark-16.svg", "icon-maskable.svg", "logo-lockup.svg", "logo-lockup-light.svg")
PALETTE = {"#0f3b66", "#e8f2fb", "#ffb020", "#5aa5e6"}


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class AssetFileTests(unittest.TestCase):
    def test_every_asset_exists_with_the_right_pixel_size(self):
        for name, n in PNG_SIZES.items():
            with open(os.path.join(BRAND, name), "rb") as fh:
                self.assertEqual(png_size(fh.read()), (n, n), name)

    def test_favicon_ico_holds_16_and_32(self):
        with open(os.path.join(BRAND, "favicon.ico"), "rb") as fh:
            d = fh.read()
        reserved, kind, count = struct.unpack("<HHH", d[:6])
        self.assertEqual((reserved, kind, count), (0, 1, 2))
        sizes = []
        for i in range(count):
            w, h, _c, _r, _p, _b, size, off = struct.unpack("<BBBBHHII", d[6 + 16 * i:22 + 16 * i])
            self.assertEqual(png_size(d[off:off + size]), (w, h))
            sizes.append(w)
        self.assertEqual(sorted(sizes), [16, 32])

    def test_svgs_are_well_formed_and_use_only_brand_colours(self):
        import xml.etree.ElementTree as ET
        for name in SVGS:
            with open(os.path.join(BRAND, name)) as fh:
                text = fh.read()
            ET.fromstring(text)
            used = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", text)}
            allowed = PALETTE | ({"#c77d00", "#1a4f84"} if name == "logo-lockup-light.svg" else set())
            self.assertLessEqual(used, allowed, (name, used - allowed))

    def test_lockups_carry_the_wordmark_and_tagline(self):
        for name in ("logo-lockup.svg", "logo-lockup-light.svg"):
            with open(os.path.join(BRAND, name)) as fh:
                text = fh.read()
            self.assertIn("fabrica", text)
            self.assertIn("printer management, agent-first.", text)
            self.assertIn("IBM Plex Mono", text)


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.PRINTERS.clear()
        cls.c = TestClient(server.mcp.streamable_http_app())
        cls.c.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.c.__exit__(None, None, None)

    def setUp(self):
        server.SETTINGS.update({"security.api_auth": "on"}, clear=["security.dashboard_password"])

    def test_icon_routes_are_public_with_the_right_types(self):
        want = {"/favicon.ico": "image/x-icon", "/favicon.svg": "image/svg+xml",
                "/apple-touch-icon.png": "image/png", "/manifest.webmanifest": "application/manifest+json",
                "/static/brand/mark.svg": "image/svg+xml", "/static/brand/icon-512.png": "image/png",
                "/static/brand/favicon.ico": "image/x-icon", "/static/brand/logo-lockup.svg": "image/svg+xml"}
        for path, ctype in want.items():
            r = self.c.get(path)               # key checking is ON and no key is sent
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers["content-type"].split(";")[0], ctype, path)
            self.assertTrue(r.content, path)
            self.assertTrue(r.headers.get("cache-control"), path)

    def test_assets_are_cached_for_a_day_and_the_manifest_is_not(self):
        self.assertEqual(self.c.get("/favicon.ico").headers["cache-control"], "public, max-age=86400")
        self.assertEqual(self.c.get("/static/brand/mark.svg").headers["cache-control"], "public, max-age=86400")
        self.assertEqual(self.c.get("/manifest.webmanifest").headers["cache-control"], "no-cache")

    def test_head_works(self):
        self.assertEqual(self.c.head("/favicon.ico").status_code, 200)
        self.assertEqual(self.c.head("/static/brand/mark.svg").status_code, 200)

    def test_served_bytes_are_the_files(self):
        with open(os.path.join(BRAND, "icon-192.png"), "rb") as fh:
            self.assertEqual(self.c.get("/static/brand/icon-192.png").content, fh.read())
        with open(os.path.join(BRAND, "apple-touch-icon.png"), "rb") as fh:
            self.assertEqual(self.c.get("/apple-touch-icon.png").content, fh.read())
        with open(os.path.join(BRAND, "mark-32.svg"), "rb") as fh:
            self.assertEqual(self.c.get("/favicon.svg").content, fh.read())

    def test_manifest_is_valid_json_and_complete(self):
        r = self.c.get("/manifest.webmanifest")
        m = json.loads(r.text)
        self.assertEqual((m["name"], m["short_name"], m["display"], m["start_url"]), ("Fabrica", "Fabrica", "standalone", "/dashboard"))
        self.assertEqual((m["theme_color"], m["background_color"]), ("#0f3b66", "#0f3b66"))
        self.assertIn("maskable", {i["purpose"] for i in m["icons"]})
        self.assertGreaterEqual({i["sizes"] for i in m["icons"]}, {"192x192", "512x512"})
        for icon in m["icons"]:                                 # every icon the manifest names is really served
            self.assertEqual(self.c.get(icon["src"]).status_code, 200, icon["src"])
            self.assertEqual(self.c.get(icon["src"]).headers["content-type"].split(";")[0], icon["type"])

    def test_manifest_short_name_follows_the_hub_name(self):
        try:
            server.SETTINGS.update({"general.hub_name": "Shop Hub"})
            self.assertEqual(self.c.get("/manifest.webmanifest").json()["short_name"], "Shop Hub")
            server.SETTINGS.update({"general.hub_name": "A very long hub name indeed"})
            self.assertLessEqual(len(self.c.get("/manifest.webmanifest").json()["short_name"]), 12)
            self.assertEqual(self.c.get("/manifest.webmanifest").json()["name"], "Fabrica")
        finally:
            server.SETTINGS.reset(["general.hub_name"])
        self.assertEqual(self.c.get("/manifest.webmanifest").json()["short_name"], "Fabrica")

    def test_traversal_and_oddities_are_refused(self):
        bad = ["/static/brand/../server.py", "/static/brand/..%2Fserver.py", "/static/brand/%2e%2e/server.py",
               "/static/brand/..%5Cserver.py", "/static/brand/%2e%2e%2fserver.py", "/static/brand/.hidden",
               "/static/brand/..", "/static/brand/mark.svg%00.png", "/static/brand/nope.svg",
               "/static/brand/", "/static/brand/sub/mark.svg", "/static/brand//etc/passwd",
               "/static/brand/mark.svg/..", "/static/dashboard.html", "/static/brand/%252e%252e"]
        for mode, ok in (("on", (400, 401, 404)), ("off", (400, 404))):    # "off" proves the handler itself says no
            server.SETTINGS.update({"security.api_auth": mode})
            for path in bad:
                r = self.c.get(path)
                self.assertIn(r.status_code, ok, (mode, path))
                self.assertNotIn(b"import ", r.content[:200], path)

    def test_only_known_file_types_are_served(self):
        stray = os.path.join(BRAND, "stray.py")
        with open(stray, "w") as fh:
            fh.write("print('x')\n")
        try:
            self.assertEqual(self.c.get("/static/brand/stray.py").status_code, 404)
        finally:
            os.remove(stray)

    def test_a_symlink_cannot_lead_out_of_the_folder(self):
        link = os.path.join(BRAND, "leak.svg")
        target = os.path.join(_TMP, "secret.svg")
        with open(target, "w") as fh:
            fh.write("<svg/>")
        try:
            os.symlink(target, link)
            self.assertEqual(self.c.get("/static/brand/leak.svg").status_code, 404)
        finally:
            if os.path.lexists(link):
                os.remove(link)

    def test_other_methods_are_not_allowed(self):
        self.assertEqual(self.c.post("/static/brand/mark.svg").status_code, 401)     # not public: needs a key
        server.SETTINGS.update({"security.api_auth": "off"})
        self.assertEqual(self.c.post("/static/brand/mark.svg").status_code, 405)
        self.assertEqual(self.c.put("/manifest.webmanifest").status_code, 405)

    def test_dashboard_page_links_the_icons_and_manifest(self):
        html = self.c.get("/dashboard").text
        for needle in ('rel="icon"', 'rel="apple-touch-icon"', 'rel="manifest"', 'name="theme-color"',
                       'apple-mobile-web-app-capable', 'apple-mobile-web-app-title'):
            self.assertIn(needle, html)

    def test_version_is_a_short_token_and_shown_in_settings(self):
        self.assertRegex(server.BUILD_REV, r"^[A-Za-z0-9._-]{1,40}$")
        self.assertEqual(server.SETTINGS.get("agents.version"), server.BUILD_REV)

    def test_build_rev_from_the_environment(self):
        old = os.environ.get("FABRICA_BUILD_REV")
        try:
            os.environ["FABRICA_BUILD_REV"] = "0123456789abcdef0123456789abcdef01234567"
            self.assertEqual(server._build_rev(), "0123456")
            os.environ["FABRICA_BUILD_REV"] = "bad rev; rm -rf"
            self.assertEqual(server._build_rev(), "dev")
            os.environ["FABRICA_BUILD_REV"] = "dev"
            self.assertEqual(server._build_rev(), "dev")
        finally:
            if old is None:
                os.environ.pop("FABRICA_BUILD_REV", None)
            else:
                os.environ["FABRICA_BUILD_REV"] = old


if __name__ == "__main__":
    unittest.main(verbosity=1)
