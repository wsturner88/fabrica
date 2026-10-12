"""Tests for the first-run "Protect your printers" step (app/security.py + app/server.py + app/dashboard.html).

    python3 tests/test_first_run_password.py

A FRESH install has API keys ON for agents, but its dashboard is open to the whole network until the owner
sets a dashboard password. So the first time the dashboard opens it asks - set a password (sign-in ON, this
browser stays signed in) or skip (two taps, recorded). Until one of the two happened the Hub refuses browser
writes; agents with API keys are never affected, and an upgrade is never asked.

Everything is local: the server module is imported with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointing at a
throwaway temp directory (never the real config.yml), the only printer is an in-process fake and no host is
contacted. Needs the app's own dependencies (mcp, starlette, httpx); without them the server tests skip.
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

_TMP = tempfile.mkdtemp(prefix="firstrun-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)
os.environ.pop("BAMBU_MCP_ALLOWED_HOSTS", None)
os.environ.pop("BAMBU_MCP_ALLOW_FRAMING", None)
DATA = os.path.join(_TMP, "data")

import security  # noqa: E402
import settings_store  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover - the app's deps are not installed
    HAVE = False
    _ERR = e

PW = "a long first password"
ORIGIN = {"Origin": "http://testserver"}
WRITE = "/api/settings/test/webhook"        # a state-changing route that needs `control` and contacts nothing when unconfigured
APPLIB = None


def slurp(path):
    with open(path) as fh:
        return fh.read()


class FakePrinter:
    kind = "fake"
    has_camera = False
    uses_files_api = False

    def __init__(self, name):
        self.name = name

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": "IDLE", "job": "", "percent": 0,
                "remaining_min": None}

    def refresh(self, wait=0):
        pass

    def ams_trays(self):
        return []


def setUpModule():
    global APPLIB
    if HAVE:
        server.PRINTERS.clear()
        server.PRINTERS["fake"] = FakePrinter("Fake")
        APPLIB = server.mcp.streamable_http_app()


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def browser(ua="Mozilla/5.0 test-browser"):
    """A separate browser: its own cookie jar. It has opened the dashboard, so it holds the no-password cookie."""
    c = TestClient(APPLIB, headers={"user-agent": ua})
    assert c.get("/dashboard").status_code == 200
    return c


def cookie_named(resp, name):
    for c in resp.headers.get_list("set-cookie"):
        if c.startswith(name + "="):
            return c
    return ""


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class Base(unittest.TestCase):
    """A brand-new install: no passwords, nothing answered, key check ON."""

    def setUp(self):
        a = server.AUTH
        a.clock = time.time
        server.SETTINGS.update({"security.api_auth": "on"}, clear=["security.dashboard_password"])
        a.dash.set_fields(login_enabled=None, owner_hash=None, viewer_hash=None, admin_hash=None, offer_dismissed=False)
        a.dash.clear_sessions()
        a.dash.remove_devices_where(lambda r: True)
        a.cancel_pairing()
        a.keys.set_meta(install="fresh")
        for k in a.keys.list_public():
            a.delete_key(k["id"])
        a.limiter = security.LoginLimiter()
        a.pair_limiter = security.LoginLimiter(per_client=5, overall=10)
        a.env = dict(os.environ)
        self.a = a
        self.addCleanup(lambda: a.keys.set_meta(install="fresh"))
        self.addCleanup(lambda: server.SETTINGS.update({"security.api_auth": "on"}, clear=["security.dashboard_password"]))
        _rec, token = a.create_key("agent", "control")
        self.keyhdr = {"Authorization": "Bearer " + token}
        _rec, token = a.create_key("looker", "read")
        self.readhdr = {"Authorization": "Bearer " + token}

    def me(self, c, **kw):
        r = c.get("/api/auth/me", **kw)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def first_run(self, c, pw=PW, **extra):
        return c.post("/api/auth/first-run", headers=ORIGIN, json={"password": pw, "confirm": pw, **extra})


# --------------------------------------------------------------------------- a fresh install is asked

class FreshInstallTests(Base):
    def test_a_fresh_install_requires_the_step_and_says_so(self):
        c = browser()
        me = self.me(c)
        self.assertTrue(me["offer_login"])
        self.assertEqual(me["min_password"], security.MIN_PASSWORD)
        self.assertTrue(self.a.first_run_required())
        self.assertFalse(self.a.login_required())          # nothing is switched on until the owner chooses

    def test_set_password_turns_sign_in_on_and_this_browser_stays_signed_in(self):
        c = browser()
        r = self.first_run(c, device_name="Office laptop")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["dashboard_login"])
        ck = cookie_named(r, security.DEVICE_COOKIE)
        self.assertTrue(ck, r.headers.get_list("set-cookie"))
        self.assertIn("httponly", ck.lower())
        self.assertIn("samesite=strict", ck.lower())
        self.assertTrue(self.a.login_required())
        self.assertTrue(self.a.check_password(PW))
        self.assertEqual(self.a.check_login(PW), "control")
        self.assertIs(self.a.dash.login_enabled, True)
        self.assertTrue(self.a.dash.offer_dismissed)       # answered: it never comes back
        # the same browser is still in (a trusted device), full access, and the step is gone
        me = self.me(c)
        self.assertEqual((me["login_required"], me["role"], me["signed_in_with"], me["offer_login"]),
                         (True, "control", "device", False))
        self.assertEqual(me["device"]["name"], "Office laptop")
        self.assertEqual(c.post(WRITE, headers=ORIGIN, json={}).status_code, 200)
        # another browser has to sign in now
        other = TestClient(APPLIB)
        self.assertEqual(other.get("/api/auth/me").status_code, 401)
        self.assertEqual(other.post("/api/auth/login", headers=ORIGIN, json={"password": PW}).status_code, 200)

    def test_the_password_also_does_the_admin_work_without_making_a_second_admin_password(self):
        c = browser()
        self.assertEqual(self.first_run(c).status_code, 200)
        self.assertFalse(self.a.admin_password_set())        # it stands in for the admin password (admin_state)
        self.assertEqual(self.me(c)["admin"], "ok")
        self.assertEqual(c.get("/api/security").status_code, 200)
        self.assertEqual(c.post("/api/keys", headers=ORIGIN, json={"name": "later", "scope": "read"}).status_code, 201)

    def test_the_password_is_checked(self):
        c = browser()
        for body in ({"password": "short"}, {"password": "        "}, {"password": ""}, {}, {"password": 12345678},
                     {"password": PW, "confirm": "something else"}):
            r = c.post("/api/auth/first-run", headers=ORIGIN, json=body)
            self.assertEqual(r.status_code, 400, (body, r.text))
            self.assertIn("error", r.json())
        self.assertTrue(self.a.first_run_required())          # nothing was saved
        self.assertFalse(self.a.password_set())
        self.assertEqual(self.first_run(c).status_code, 200)

    def test_it_can_be_answered_only_once(self):
        c = browser()
        self.assertEqual(self.first_run(c).status_code, 200)
        self.assertEqual(self.first_run(c, pw="another long password").status_code, 409)
        self.assertTrue(self.a.check_password(PW))            # the first answer stands

    def test_only_the_dashboard_in_the_same_origin_can_answer(self):
        c = browser()
        # a stranger's page in a browser (cross-site) is refused before it reaches the handler
        r = c.post("/api/auth/first-run", headers={"Origin": "http://evil.example"}, json={"password": PW})
        self.assertEqual(r.status_code, 403, r.text)
        # a bare script (no cookie, no key) and an API key (even a control key) cannot set the dashboard password
        self.assertIn(TestClient(APPLIB).post("/api/auth/first-run", json={"password": PW}).status_code, (401, 403))
        r = TestClient(APPLIB).post("/api/auth/first-run", headers=self.keyhdr, json={"password": PW})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self.a.first_run_required())
        self.assertFalse(self.a.password_set())

    def test_the_kiosk_can_still_be_paired_afterwards(self):
        c = browser()
        self.assertEqual(self.first_run(c).status_code, 200)
        r = c.post("/api/pairing", headers=ORIGIN, json={"role": "control", "name": "Wall screen"})
        self.assertEqual(r.status_code, 201, r.text)
        wall = TestClient(APPLIB)
        r = wall.post("/api/auth/pair", headers=ORIGIN, json={"code": r.json()["code"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.me(wall)["signed_in_with"], "device")


# --------------------------------------------------------------------------- skipping

class SkipTests(Base):
    def test_skip_is_recorded_and_the_step_does_not_come_back(self):
        c = browser()
        self.assertTrue(self.me(c)["offer_login"])
        r = c.post("/api/auth/dismiss-offer", headers=ORIGIN)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.me(c)["offer_login"])
        self.assertFalse(self.me(browser())["offer_login"])        # for every browser, not just this one
        self.assertFalse(self.a.first_run_required())
        self.assertFalse(self.a.login_required())                  # skipping switches nothing on
        self.assertFalse(self.a.password_set())
        again = security.DashboardAuthStore(DATA)                  # recorded on disk, so a restart keeps it
        self.assertTrue(again.offer_dismissed)

    def test_skipping_leaves_the_calm_sign_in_off_note_in_settings(self):
        c = browser()
        c.post("/api/auth/dismiss-offer", headers=ORIGIN)
        ids = {w["id"] for w in self.a.warnings()}
        self.assertIn("no_dashboard_password", ids)
        html = slurp(os.path.join(APP, "dashboard.html"))
        self.assertIn("SIGN-IN: NOT REQUIRED", html)               # the Security tab's words for it (never colour alone)

    def test_a_key_cannot_skip_or_answer_for_the_owner(self):
        r = browser().post("/api/auth/dismiss-offer", headers={**ORIGIN, **self.keyhdr})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self.a.first_run_required())

    def test_the_command_line_can_show_the_step_again(self):
        c = browser()
        c.post("/api/auth/dismiss-offer", headers=ORIGIN)
        self.assertFalse(self.a.first_run_required())
        old = os.environ.get("BAMBU_MCP_CACHE")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(security._cli(["reset-first-run"]), 0)
        finally:
            if old is not None:
                os.environ["BAMBU_MCP_CACHE"] = old
        self.assertIn("will show again", out.getvalue())
        self.assertTrue(self.a.first_run_required())               # the running Hub notices on its next request
        self.assertTrue(self.me(browser())["offer_login"])

    def test_the_command_line_says_when_it_cannot_apply(self):
        self.assertEqual(self.first_run(browser()).status_code, 200)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(security._cli(["reset-first-run"]), 0)
        self.assertIn("already set", out.getvalue())
        self.assertFalse(self.a.first_run_required())

    def test_resetting_the_password_on_the_command_line_counts_as_an_answer(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            saved = sys.stdin
            sys.stdin = io.StringIO(PW + "\n")
            try:
                self.assertEqual(security._cli(["reset-dashboard-password", "--password-stdin"]), 0)
            finally:
                sys.stdin = saved
        self.assertFalse(self.a.first_run_required())


# --------------------------------------------------------------------------- upgrades and other modes

class NotForEveryoneTests(Base):
    def test_an_upgrade_is_never_asked_and_never_blocked(self):
        self.a.keys.set_meta(install="upgrade")
        c = browser()
        self.assertFalse(self.me(c)["offer_login"])
        self.assertFalse(self.a.first_run_required())
        self.assertEqual(c.post(WRITE, headers=ORIGIN, json={}).status_code, 200)
        self.assertFalse(self.a.login_required())

    def test_an_upgrade_object_started_from_existing_data_is_never_asked(self):
        d = tempfile.mkdtemp(dir=_TMP)
        with open(os.path.join(d, "printers.json"), "w") as fh:
            fh.write('{"printers": []}')
        st = settings_store.SettingsStore(d, env={})
        a = security.AuthManager(d, st, existing_install=True, env={})
        self.assertEqual(a.keys.meta["install"], "upgrade")
        self.assertFalse(a.first_run_required())
        self.assertFalse(a.offer_login())

    def test_with_the_key_check_lowered_on_purpose_there_is_no_step(self):
        for mode in ("warn", "off"):
            server.SETTINGS.update({"security.api_auth": mode})
            self.assertFalse(self.a.first_run_required(), mode)
            self.assertFalse(self.me(browser())["offer_login"], mode)
            self.assertEqual(browser().post(WRITE, headers=ORIGIN, json={}).status_code, 200, mode)

    def test_an_admin_password_chosen_first_counts_as_a_decision(self):
        self.a.dash.set_fields(admin_hash=security.hash_password("admin password 1"))
        self.assertFalse(self.a.first_run_required())


# --------------------------------------------------------------------------- server-side enforcement

class EnforcementTests(Base):
    def test_browser_writes_are_refused_before_the_step_is_answered(self):
        c = browser()
        for method, path, body in (("post", WRITE, {}),
                                   ("post", "/api/printers", {"name": "x"}),
                                   ("put", "/api/settings", {"values": {}}),
                                   ("post", "/api/security/admin-password", {"password": "an admin password"}),
                                   ("delete", "/api/printers/fake", None)):
            r = getattr(c, method)(path, headers=ORIGIN, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 403, (method, path, r.text))
            j = r.json()
            self.assertTrue(j.get("first_run"), j)
            self.assertIn("Protect your printers", j["error"])          # says where to go, in words
        self.assertIsNone(self.a.dash.owner_hash)
        self.assertFalse(self.a.admin_password_set())

    def test_reading_still_works_so_the_page_can_load(self):
        c = browser()
        for path in ("/api/auth/me", "/api/fleet", "/api/printers", "/api/settings", "/api/health"):
            self.assertEqual(c.get(path).status_code, 200, path)

    def test_writes_work_after_a_password_was_set(self):
        c = browser()
        self.assertEqual(c.post(WRITE, headers=ORIGIN, json={}).status_code, 403)
        self.assertEqual(self.first_run(c).status_code, 200)
        self.assertEqual(c.post(WRITE, headers=ORIGIN, json={}).status_code, 200)

    def test_writes_work_after_skipping(self):
        c = browser()
        self.assertEqual(c.post(WRITE, headers=ORIGIN, json={}).status_code, 403)
        self.assertEqual(c.post("/api/auth/dismiss-offer", headers=ORIGIN).status_code, 200)
        self.assertEqual(c.post(WRITE, headers=ORIGIN, json={}).status_code, 200)

    def test_agents_with_api_keys_are_unaffected(self):
        anon = TestClient(APPLIB)                                       # no browser, no cookie: just an agent
        self.assertEqual(anon.post(WRITE, headers=self.keyhdr, json={}).status_code, 200)
        self.assertEqual(anon.get("/api/printers", headers=self.readhdr).status_code, 200)
        # a read key still cannot write - the usual scope rule, not the first-run rule
        r = anon.post(WRITE, headers=self.readhdr, json={})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.json().get("first_run"))
        # a key sent by a browser that also holds the dashboard cookie is still just a key
        c = browser()
        self.assertEqual(c.post(WRITE, headers={**ORIGIN, **self.keyhdr}, json={}).status_code, 200)
        # and without any credential the usual rule applies
        self.assertEqual(TestClient(APPLIB).post(WRITE, headers=ORIGIN, json={}).status_code, 401)

    def test_the_mcp_endpoint_is_not_touched(self):
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **self.keyhdr}
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "first-run-test", "version": "1"}}}
        with TestClient(APPLIB) as t:
            r = t.post("/mcp", headers=hdr, json=body)
            self.assertEqual(r.status_code, 200, r.text[:200])

    def test_enforcement_ends_with_the_answer_for_everyone(self):
        c = browser()
        other = browser()
        c.post("/api/auth/dismiss-offer", headers=ORIGIN)
        self.assertEqual(other.post(WRITE, headers=ORIGIN, json={}).status_code, 200)


# --------------------------------------------------------------------------- the page

class DashboardPageTests(unittest.TestCase):
    HTML = slurp(os.path.join(APP, "dashboard.html"))

    def test_the_first_run_screen_says_it_in_words(self):
        for text in ("Protect your printers",
                     "Anyone on your network who opens this page could start, stop or heat your printers.",
                     ">Set password<",
                     "Wall screen? Pair it later from Settings",
                     "Skip &mdash; I understand anyone on my network can control my printers",
                     "Skip without a password?", "Sign-in will stay off."):
            self.assertIn(text, self.HTML)

    def test_skipping_needs_a_second_tap_and_goes_through_the_recorded_endpoint(self):
        self.assertRegex(self.HTML, r'data-act="frSkip"')
        self.assertRegex(self.HTML, r'data-act="frSkipYes"')
        self.assertRegex(self.HTML, r"function frSkip\(\)\{[^}]*'fr-skip'")
        self.assertRegex(self.HTML, r"async function frSkipYes\(\)\{[^}]*dismiss-offer")
        self.assertIn("'/api/auth/first-run'", self.HTML)

    def test_the_actions_are_delegated_not_inline_with_data(self):
        for act in ("frSet", "frSkip", "frBack", "frSkipYes"):
            self.assertRegex(self.HTML, rf"\b{act}:\s+\(\)\s+=>")
        # the overlay's markup has no ${...} and every message from the Hub goes in with textContent
        block = self.HTML[self.HTML.index('<div id="firstrun"'):self.HTML.index("<!-- ===== file backup UI")]
        self.assertNotIn("${", block)
        self.assertNotRegex(self.HTML, r"fr-msg'\)\.innerHTML")
        self.assertIn("$('fr-msg').textContent", self.HTML)

    def test_the_old_modal_is_gone_and_the_step_sits_above_everything(self):
        self.assertNotIn("Set an admin password?", self.HTML)
        self.assertNotIn("secOfferNo", self.HTML)
        z = lambda sel: int(re.search(re.escape(sel) + r"[^}]*z-index:(\d+)", self.HTML).group(1))
        self.assertGreater(z("#firstrun {"), z("#modal {"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
