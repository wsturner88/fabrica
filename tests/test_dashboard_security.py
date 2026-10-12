"""Tests for the dashboard sign-in option (app/security.py + its wiring in app/server.py):
the login switch, sessions, trusted devices, pairing codes, the view-only role, security headers and the
offline recovery commands.

    python3 tests/test_dashboard_security.py

Everything is local: the server module is imported with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointing at a
throwaway temp directory (never the real config.yml), the only printer is an in-process fake and no host
is contacted. The one thing that matters most: with the switch OFF (the default) the Hub behaves exactly as
it did before this feature existed.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

_TMP = tempfile.mkdtemp(prefix="dashsec-test-")
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
except Exception as e:      # pragma: no cover
    HAVE = False
    _ERR = e

PW = "correct horse battery"
VPW = "look but do not touch"
ADMIN_HASH = security.hash_password("admin password 1") if HAVE else None      # one hash for the whole file
IPAD = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
PI = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/120.0 Chrome/120.0 Safari/537.36"
APPLIB = None


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


def slurp(path):
    with open(path) as fh:
        return fh.read()


def browser(ua="Mozilla/5.0 test-browser"):
    """A separate browser: its own cookie jar and User-Agent."""
    return TestClient(APPLIB, headers={"user-agent": ua})


def set_cookies(resp):
    return resp.headers.get_list("set-cookie")


def cookie_named(resp, name):
    for c in set_cookies(resp):
        if c.startswith(name + "="):
            return c
    return ""


def cookie_val(resp, name):
    c = cookie_named(resp, name)
    return c.split(";")[0].split("=", 1)[1] if c else ""


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class Base(unittest.TestCase):
    def setUp(self):
        a = server.AUTH
        a.clock = time.time
        server.SETTINGS.update({"security.api_auth": "on"}, clear=["security.dashboard_password"])
        a.dash.set_fields(login_enabled=None, owner_hash=None, viewer_hash=None, offer_dismissed=False,
                          admin_hash=None)
        a.dash.set_config(**security.CONFIG_DEFAULTS)
        a.dash.clear_sessions()
        a.dash.remove_devices_except(None)
        a.cancel_pairing()
        for k in a.keys.list_public():
            a.delete_key(k["id"])
        a.limiter = security.LoginLimiter()
        a.pair_limiter = security.LoginLimiter(per_client=5, overall=10)
        a.env = dict(os.environ)
        self.a = a
        _rec, token = a.create_key("agent", "control")
        self.keyhdr = {"Authorization": "Bearer " + token}        # an agent's control key: may NOT administer
        self._admin_sids = set()
        self.anon = browser()

    @property
    def admin(self):
        """Headers of the OWNER at the dashboard - the only thing that may manage keys, sign-in and the key check
        (an API key, even a control key, is refused there). Sign-in off: the old stateless dashboard cookie.
        Sign-in on: a control session made directly (not through the login page, so the login limiter, the
        device list and - via self.sessions() - the session counts are not disturbed)."""
        a = self.a
        if not a.login_required():
            a.dash.set_fields(admin_hash=ADMIN_HASH)          # no sign-in: the admin password is what guards the admin actions
            unlock = f"{security.ADMIN_COOKIE}={a.make_admin_cookie()}"           # ...and it was typed
            return {"Cookie": f"{security.COOKIE_NAME}={a.make_cookie()}; {unlock}", "Origin": "http://testserver"}
        unlock = f"{security.ADMIN_COOKIE}={a.make_admin_cookie()}"           # (only counts when an admin password exists)
        value, _age = a.start_session("control", "127.0.0.1", "Mozilla/5.0 owner-test")
        self._admin_sids.add(value.split(".")[1])
        return {"Cookie": f"{security.COOKIE_NAME}={value}; {unlock}", "Origin": "http://testserver"}

    def sessions(self):
        """Signed-in sessions, without the owner session self.admin made."""
        return [x for x in self.a.dash.sessions() if x["id"] not in self._admin_sids]

    # ---- helpers
    def enable(self, pw=PW, vpw=None):
        c = browser()
        body = {"dashboard_login": True, "dashboard_password": pw}
        if vpw:
            body["viewer_password"] = vpw
        r = c.put("/api/security", headers=self.admin, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        v = cookie_val(r, security.COOKIE_NAME)          # the owner's own browser is kept signed in: not a test session
        if v.startswith("v2."):
            self._admin_sids.add(v.split(".")[1])
        # Most sign-in tests are about the owner-password-only set-up, where a full-access sign-in IS the admin
        # (no separate admin password): drop the one self.admin made for the sign-in-off call above.
        self.a.dash.set_fields(admin_hash=None)
        return r

    def clock(self, start=None):
        t = [start if start is not None else time.time()]
        self.a.clock = lambda: t[0]
        self.a.limiter = security.LoginLimiter(clock=self.a.clock)
        self.a.pair_limiter = security.LoginLimiter(per_client=5, overall=10, clock=self.a.clock)
        return t

    def signin(self, c=None, pw=PW, remember=False, name=None):
        c = c or browser()
        body = {"password": pw, "remember": remember}
        if name:
            body["device_name"] = name
        r = c.post("/api/auth/login", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return c, r

    def pair_code(self, role="control", name="Wall screen"):
        r = browser().post("/api/pairing", headers=self.admin, json={"role": role, "name": name})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()


# --------------------------------------------------------------------------- the switch is OFF: nothing changes

class LoginOffTests(Base):
    def test_default_is_off_and_the_dashboard_is_exactly_as_before(self):
        self.assertFalse(self.a.login_required())
        r = self.anon.get("/dashboard")
        self.assertEqual(r.text, slurp(os.path.join(APP, "dashboard.html")))
        self.assertEqual(r.headers["cache-control"], "no-store")
        ck = cookie_named(r, security.COOKIE_NAME)
        self.assertTrue(ck.split("=", 1)[1].startswith("v1."), ck)          # the old stateless cookie, as before
        self.assertIn("httponly", ck.lower())
        self.assertIn("samesite=strict", ck.lower())
        self.assertEqual(cookie_named(r, security.DEVICE_COOKIE), "")
        r2 = self.anon.get("/api/fleet")                                     # that cookie is a control credential
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self.a.dash.sessions(), [])                          # no server-side state is created
        self.assertEqual(self.a.dash.devices(), [])

    def test_no_sign_in_routes_do_anything_while_off(self):
        self.assertEqual(browser().post("/api/auth/login", json={"password": "x"}).status_code, 400)
        self.assertEqual(browser().post("/api/auth/pair", json={"code": "123456"}).status_code, 400)
        r = browser().post("/api/pairing", headers=self.admin, json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("sign-in", r.json()["error"].lower())

    def test_me_says_full_access_when_off(self):
        c = browser()
        c.get("/dashboard")
        me = c.get("/api/auth/me").json()
        self.assertEqual((me["login_required"], me["role"], me["view_only"]), (False, "control", False))
        me = browser().get("/api/auth/me", headers=self.keyhdr).json()
        self.assertEqual((me["role"], me["signed_in_with"]), ("control", "key"))

    def test_off_with_key_mode_off_is_wide_open_as_before(self):
        server.SETTINGS.update({"security.api_auth": "off"})
        self.assertEqual(browser().get("/api/fleet").status_code, 200)
        self.assertEqual(browser().get("/dashboard").status_code, 200)
        self.assertEqual(browser().post("/api/printer/fake/start", json={}).status_code, 400)  # reached the handler

    def test_a_password_set_the_old_way_still_means_ask_for_it(self):
        r = browser().put("/api/security", headers=self.admin, json={"dashboard_password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.a.login_required())
        self.assertIn("Pair with a code", self.anon.get("/dashboard").text)
        self.anon.post("/api/auth/login", json={"password": "wrong wrong wrong"})
        self.assertEqual(self.signin()[1].status_code, 200)

    def test_turned_off_explicitly_a_stored_password_does_not_gate(self):
        self.enable()
        r = browser().put("/api/security", headers=self.admin, json={"dashboard_login": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.a.login_required())
        self.assertEqual(browser().get("/dashboard").text, slurp(os.path.join(APP, "dashboard.html")))

    def test_old_install_record_without_the_new_file_loads_clean(self):
        d = tempfile.mkdtemp(dir=_TMP)
        store = security.DashboardAuthStore(d)
        self.assertEqual((store.login_enabled, store.owner_hash, store.viewer_hash), (None, None, None))
        self.assertEqual(store.config(), security.CONFIG_DEFAULTS)
        self.assertEqual(oct(os.stat(os.path.join(d, security.DASH_FILE)).st_mode & 0o777), "0o600")


# --------------------------------------------------------------------------- the switch is ON

class LoginOnTests(Base):
    def test_turning_it_on_needs_the_password_in_the_same_step(self):
        c = browser()
        r = c.put("/api/security", headers=self.admin, json={"dashboard_login": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("password", r.json()["error"])
        self.assertFalse(self.a.login_required())
        r = c.put("/api/security", headers=self.admin, json={"dashboard_login": True, "dashboard_password": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.a.login_required())
        r = c.put("/api/security", headers=self.admin, json={"dashboard_login": "yes", "dashboard_password": PW})
        self.assertEqual(r.status_code, 400)
        self.enable()
        self.assertTrue(self.a.login_required())

    def test_page_is_gated_and_the_caller_stays_signed_in(self):
        c = browser()
        c.get("/dashboard")                                   # this browser had the old cookie
        r = c.put("/api/security", headers=self.admin, json={"dashboard_login": True, "dashboard_password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(cookie_val(r, security.COOKIE_NAME).startswith("v2."))     # a fresh session for the caller
        self.assertEqual(c.get("/dashboard").text, slurp(os.path.join(APP, "dashboard.html")))
        self.assertEqual(c.get("/api/fleet").status_code, 200)
        other = browser()
        page = other.get("/dashboard")
        self.assertIn("Pair with a code", page.text)
        self.assertIn("Remember this device", page.text)
        self.assertEqual(cookie_named(page, security.COOKIE_NAME), "")
        self.assertEqual(other.get("/api/fleet").status_code, 401)

    def test_the_old_stateless_cookie_stops_working_once_sign_in_is_on(self):
        c = browser()
        c.get("/dashboard")
        old = c.cookies.get(security.COOKIE_NAME)
        self.enable()
        r = browser().get("/api/fleet", headers={"Cookie": f"{security.COOKIE_NAME}={old}"})
        self.assertEqual(r.status_code, 401)

    def test_keys_and_public_routes_are_unaffected(self):
        self.enable()
        _r, tok = self.a.create_key("agent", "read")
        self.assertEqual(browser().get("/api/fleet", headers={"Authorization": "Bearer " + tok}).status_code, 200)
        for path in ("/api/health", "/api/ui-version", "/favicon.svg", "/manifest.webmanifest", "/static/brand/mark.svg"):
            self.assertEqual(browser().get(path).status_code, 200, path)

    def test_wrong_password_and_the_right_one(self):
        self.enable()
        c = browser()
        r = c.post("/api/auth/login", json={"password": "wrong wrong wrong"})
        self.assertEqual((r.status_code, r.json()["error"]), (401, "Wrong password."))
        self.assertEqual(set_cookies(r), [])
        self.a.limiter.reset()                      # (three free tries, then a wait: start over for the odd bodies)
        for bad in ({}, {"password": None}, {"password": 5}):
            self.assertEqual(c.post("/api/auth/login", json=bad).status_code, 401)
        self.a.limiter.reset()
        r = c.post("/api/auth/login", json={"password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["role"], r.json()["remembered"]), ("control", False))
        ck = cookie_named(r, security.COOKIE_NAME).lower()
        self.assertIn("httponly", ck)
        self.assertIn("samesite=strict", ck)
        self.assertNotIn("secure", ck)
        self.assertEqual(cookie_named(r, security.DEVICE_COOKIE), "")

    def test_cookies_are_secure_behind_https(self):
        self.enable()
        c = browser()
        r = c.post("/api/auth/login", json={"password": PW, "remember": True}, headers={"X-Forwarded-Proto": "https"})
        self.assertIn("secure", cookie_named(r, security.DEVICE_COOKIE).lower())
        r = browser().post("/api/auth/login", json={"password": PW}, headers={"X-Forwarded-Proto": "https"})
        self.assertIn("secure", cookie_named(r, security.COOKIE_NAME).lower())

    def test_forged_or_tampered_cookies_do_not_work(self):
        self.enable()
        c, r = self.signin()
        good = cookie_val(r, security.COOKIE_NAME)
        ver, sid, exp, sig = good.split(".")
        for bad in (good[:-2] + "00", f"{ver}.{sid}.{int(exp) + 1}.{sig}", f"v2.s_nope.{exp}.{sig}", "v2.x", "junk", ""):
            self.assertEqual(browser().get("/api/fleet", headers={"Cookie": f"{security.COOKIE_NAME}={bad}"}).status_code,
                             401, bad)
        self.assertEqual(browser().get("/api/fleet", headers={"Cookie": f"{security.COOKIE_NAME}={good}"}).status_code, 200)

    def test_csrf_is_still_checked_on_cookie_writes(self):
        self.enable()
        c, _ = self.signin()
        r = c.post("/api/printer/fake/start", json={}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/printer/fake/start", json={}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(c.post("/api/printer/fake/start", json={}).status_code, 400)     # reached the handler
        self.assertEqual(c.post("/api/auth/logout", headers={"Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(c.post("/api/auth/login", json={"password": PW}, headers={"Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(c.post("/api/auth/pair", json={"code": "123456"}, headers={"Origin": "http://evil.example"}).status_code, 403)

    def test_dns_rebinding_host_is_refused(self):
        self.enable()
        c, _ = self.signin()
        self.assertEqual(c.get("/api/fleet", headers={"Host": "attacker.example.com"}).status_code, 401)
        self.assertEqual(browser().post("/api/auth/login", json={"password": PW},
                                        headers={"Host": "attacker.example.com"}).status_code, 403)

    def test_sign_out_ends_the_session(self):
        self.enable()
        c, r = self.signin()
        ck = cookie_val(r, security.COOKIE_NAME)
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(c.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(browser().get("/api/fleet", headers={"Cookie": f"{security.COOKIE_NAME}={ck}"}).status_code, 401)

    def test_turning_it_off_opens_the_page_and_removes_the_password(self):
        self.enable(vpw=VPW)
        r = browser().put("/api/security", headers=self.admin, json={"dashboard_login": False, "clear_dashboard_password": True})
        self.assertEqual(r.status_code, 200)
        st = self.a.status()
        self.assertEqual((st["dashboard_login"], st["dashboard_password"], st["viewer_password"]), (False, "not set", "not set"))
        self.assertEqual(browser().get("/dashboard").text, slurp(os.path.join(APP, "dashboard.html")))

    def test_it_cannot_be_on_without_a_password(self):
        self.enable()
        self.a.dash.set_fields(owner_hash=None)
        self.assertFalse(self.a.login_required())            # no password = no lock, never a lockout

    def test_first_run_offer(self):
        self.assertEqual(self.a.keys.meta["install"], "fresh")
        c = browser()
        c.get("/dashboard")
        self.assertTrue(c.get("/api/auth/me").json()["offer_login"])
        r = c.post("/api/auth/dismiss-offer")          # "not now": not an admin action, the dashboard cookie is enough
        self.assertEqual(r.status_code, 200)
        self.assertFalse(c.get("/api/auth/me").json()["offer_login"])
        self.assertFalse(self.a.login_required())            # an offer, never a switch
        self.a.dash.set_fields(offer_dismissed=False)
        self.enable()
        self.assertFalse(browser().get("/api/auth/me", headers=self.keyhdr).json()["offer_login"])

    def test_an_upgraded_install_is_never_offered_it(self):
        d = tempfile.mkdtemp(dir=_TMP)
        open(os.path.join(d, "printers.json"), "w").write('{"printers": []}')
        st = settings_store.SettingsStore(d, env={})
        a = security.AuthManager(d, st, existing_install=True, env={})
        self.assertEqual(a.keys.meta["install"], "upgrade")
        self.assertFalse(a.offer_login())
        self.assertFalse(a.login_required())

    def test_an_old_saved_password_is_adopted(self):
        d = tempfile.mkdtemp(dir=_TMP)
        st = settings_store.SettingsStore(d, env={})
        st.update({"security.dashboard_password": PW})
        a = security.AuthManager(d, st, existing_install=True, env={})
        self.assertTrue(a.login_required())
        self.assertTrue(a.check_password(PW))
        self.assertEqual(a.check_login(PW), "control")


# --------------------------------------------------------------------------- trusted devices

class DeviceTests(Base):
    def test_remember_issues_a_hashed_rotating_device_token(self):
        self.enable()
        c, r = self.signin(browser(IPAD), remember=True, name="Shop iPad")
        self.assertTrue(r.json()["remembered"])
        ck = cookie_named(r, security.DEVICE_COOKIE)
        self.assertIn("httponly", ck.lower())
        self.assertIn("samesite=strict", ck.lower())
        self.assertIn(f"max-age={90 * 86400}", ck.lower())
        self.assertEqual(cookie_named(r, security.COOKIE_NAME), "")
        token = cookie_val(r, security.DEVICE_COOKIE)
        self.assertEqual(c.get("/api/fleet").status_code, 200)
        self.assertEqual(c.get("/dashboard").text, slurp(os.path.join(APP, "dashboard.html")))
        raw = slurp(os.path.join(DATA, security.DASH_FILE))
        self.assertNotIn(token.split(".", 1)[1], raw)                           # only a hash is stored
        self.assertEqual(oct(os.stat(os.path.join(DATA, security.DASH_FILE)).st_mode & 0o777), "0o600")
        devs = self.a.list_devices()
        self.assertEqual(len(devs), 1)
        self.assertEqual((devs[0]["name"], devs[0]["browser"], devs[0]["role"]), ("Shop iPad", "iPad · Safari", "control"))
        for k in ("first_seen", "last_seen", "last_ip", "expires"):
            self.assertTrue(devs[0][k], k)
        self.assertNotIn("hash", devs[0])
        self.assertNotIn("salt", devs[0])

    def test_default_name_comes_from_the_user_agent(self):
        self.enable()
        self.signin(browser(PI), remember=True)
        self.assertEqual(self.a.list_devices()[0]["name"], "Linux · Chromium")
        self.assertEqual(security.device_label(IPAD), "iPad · Safari")
        self.assertEqual(security.device_label("Mozilla/5.0 (Windows NT 10.0) Chrome/120 Edg/120"), "Windows · Edge")
        self.assertEqual(security.device_label(""), "Device · browser")

    def test_token_rotates_with_a_grace_period(self):
        self.enable()
        t = self.clock()
        c, r = self.signin(remember=True)
        first = cookie_val(r, security.DEVICE_COOKIE)
        self.assertEqual(c.get("/api/fleet").status_code, 200)
        self.assertEqual(cookie_named(c.get("/api/fleet"), security.DEVICE_COOKIE), "")      # not due yet
        t[0] += security.ROTATE_AFTER_S + 5
        r2 = c.get("/api/fleet")
        self.assertEqual(r2.status_code, 200)
        second = cookie_val(r2, security.DEVICE_COOKIE)
        self.assertTrue(second and second != first)
        self.assertEqual(second.split(".")[0], first.split(".")[0])                          # same device, new token
        old = browser()
        self.assertEqual(old.get("/api/fleet", headers={"Cookie": f"{security.DEVICE_COOKIE}={first}"}).status_code, 200)
        # the old token is not rotated again (only the current one is)
        self.assertEqual(cookie_named(old.get("/api/fleet", headers={"Cookie": f"{security.DEVICE_COOKIE}={first}"}),
                                      security.DEVICE_COOKIE), "")
        t[0] += security.ROTATE_GRACE_S + 5
        self.assertEqual(browser().get("/api/fleet", headers={"Cookie": f"{security.DEVICE_COOKIE}={first}"}).status_code, 401)
        self.assertEqual(c.get("/api/fleet").status_code, 200)                               # the browser holds the new one

    def test_a_dashboard_page_load_rotates_too(self):
        self.enable()
        t = self.clock()
        c, _ = self.signin(remember=True)
        t[0] += security.ROTATE_AFTER_S + 5
        r = c.get("/dashboard")
        self.assertTrue(cookie_val(r, security.DEVICE_COOKIE))
        self.assertEqual(c.get("/api/fleet").status_code, 200)

    def test_unused_device_expires_and_use_extends_it(self):
        self.enable()
        t = self.clock()
        c, r = self.signin(remember=True)
        tok = cookie_val(r, security.DEVICE_COOKIE)
        for _ in range(4):                                        # used every 60 days: still trusted after 240
            t[0] += 60 * 86400
            self.assertEqual(c.get("/api/fleet").status_code, 200)
        t[0] += 91 * 86400
        self.assertEqual(c.get("/api/fleet").status_code, 401)
        self.assertEqual(self.a.dash.devices(), [])

    def test_configurable_lifetime(self):
        self.enable()
        t = self.clock()
        browser().put("/api/security", headers=self.admin, json={"device_days": 7})
        c, _ = self.signin(remember=True)
        t[0] += 8 * 86400
        self.assertEqual(c.get("/api/fleet").status_code, 401)

    def test_revoke_one_rename_and_revoke_others(self):
        self.enable()
        c1, _ = self.signin(remember=True, name="One")
        c2, _ = self.signin(remember=True, name="Two")
        c3, _ = self.signin(remember=True, name="Three")
        v = c1.get("/api/devices").json()
        self.assertEqual([d["name"] for d in v["devices"] if d["current"]], ["One"])
        two = next(d for d in v["devices"] if d["name"] == "Two")
        r = c1.patch(f"/api/devices/{two['id']}", json={"name": "Wall screen"})
        self.assertEqual((r.status_code, r.json()["name"]), (200, "Wall screen"))
        self.assertEqual(c1.patch(f"/api/devices/{two['id']}", json={"name": "  "}).status_code, 400)
        self.assertEqual(c1.patch("/api/devices/d_nope", json={"name": "x"}).status_code, 404)
        self.assertEqual(c1.delete(f"/api/devices/{two['id']}").status_code, 200)
        self.assertEqual(c2.get("/api/fleet").status_code, 401)
        self.assertEqual(c3.get("/api/fleet").status_code, 200)
        r = c1.post("/api/devices/revoke-others")
        self.assertEqual((r.status_code, r.json()["revoked"]), (200, 1))
        self.assertEqual(c3.get("/api/fleet").status_code, 401)
        self.assertEqual(c1.get("/api/fleet").status_code, 200)               # the caller keeps its own
        self.assertEqual(c1.delete("/api/devices/d_nope").status_code, 404)

    def test_sign_out_forgets_the_device(self):
        self.enable()
        c, _ = self.signin(remember=True)
        self.assertEqual(c.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.a.dash.devices(), [])
        self.assertEqual(c.get("/api/fleet").status_code, 401)

    def test_a_password_change_ends_full_access_sessions_and_devices(self):
        # second security review N-5: a trusted device used to keep full access for months after the owner changed
        # the password. Now every full-access session and device ends; the one who changed it is re-issued a session.
        self.enable()
        dev, _ = self.signin(remember=True)
        ses, _ = self.signin()
        r = ses.put("/api/security", json={"dashboard_password": "a brand new password"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["revoked"]["devices"], 1)
        self.assertEqual(ses.get("/api/fleet").status_code, 200)              # the changer stays signed in
        self.assertEqual(dev.get("/api/fleet").status_code, 401)              # the trusted device must sign in again
        self.assertEqual(self.a.dash.devices(), [])
        other, _ = self.signin(pw="a brand new password")
        self.assertEqual(self.a.check_login(PW), None)
        self.assertEqual(len(self.sessions()), 2)                       # 'ses' re-issued + 'other'

    def test_enabling_starts_clean(self):
        self.enable()
        dev, _ = self.signin(remember=True)
        browser().put("/api/security", headers=self.admin, json={"dashboard_login": False})
        browser().put("/api/security", headers=self.admin, json={"dashboard_login": True, "dashboard_password": PW})
        self.assertEqual(self.a.dash.devices(), [])
        self.assertEqual(dev.get("/api/fleet").status_code, 401)

    def test_device_list_needs_control(self):
        self.enable(vpw=VPW)
        v, _ = self.signin(pw=VPW)
        for method, path in (("get", "/api/devices"), ("post", "/api/pairing"), ("delete", "/api/pairing"),
                             ("post", "/api/devices/revoke-others"), ("delete", "/api/devices/d_x"),
                             ("patch", "/api/devices/d_x"), ("delete", "/api/sessions/s_x"),
                             ("post", "/api/auth/logout-everywhere")):
            self.assertEqual(getattr(v, method)(path).status_code, 403, f"{method} {path}")


# --------------------------------------------------------------------------- sessions

class SessionTests(Base):
    def test_idle_timeout_for_a_session_that_was_not_remembered(self):
        self.enable()
        t = self.clock()
        c, _ = self.signin()
        t[0] += 11 * 3600
        self.assertEqual(c.get("/api/fleet").status_code, 200)               # activity resets the idle clock
        t[0] += 11 * 3600
        self.assertEqual(c.get("/api/fleet").status_code, 200)
        t[0] += 13 * 3600
        self.assertEqual(c.get("/api/fleet").status_code, 401)               # 13 h idle > 12 h
        self.assertIn("Pair with a code", c.get("/dashboard").text)
        self.assertEqual(self.sessions(), [])

    def test_absolute_maximum(self):
        self.enable()
        t = self.clock()
        c, _ = self.signin()
        start, ended = t[0], None
        for _ in range(40):                                                     # active every 6 h, never idle
            t[0] += 6 * 3600
            if c.get("/api/fleet").status_code != 200:
                ended = t[0] - start
                break
        self.assertIsNotNone(ended)
        self.assertGreaterEqual(ended, 7 * 86400)                                    # default maximum: 7 days
        self.assertLessEqual(ended, 7 * 86400 + 6 * 3600)

    def test_idle_and_max_are_configurable(self):
        self.enable()
        t = self.clock()
        r = browser().put("/api/security", headers=self.admin, json={"session_idle_hours": 1, "session_max_days": 2})
        self.assertEqual(r.json()["session_settings"]["idle_hours"], 1)
        c, _ = self.signin()
        t[0] += 2 * 3600
        self.assertEqual(c.get("/api/fleet").status_code, 401)
        for bad in ({"session_idle_hours": 0}, {"session_idle_hours": 721}, {"session_max_days": "7"},
                    {"device_days": 1.5}, {"device_days": True}):
            self.assertEqual(browser().put("/api/security", headers=self.admin, json=bad).status_code, 400, bad)

    def test_session_list_and_ending_one(self):
        self.enable()
        a, _ = self.signin(browser(IPAD))
        b, _ = self.signin(browser(PI))
        v = a.get("/api/devices").json()
        listed = [x for x in v["sessions"] if x["id"] not in self._admin_sids]       # (minus the owner session enable() left)
        self.assertEqual(len(listed), 2)
        mine = next(s for s in listed if s["current"])
        self.assertEqual(mine["browser"], "iPad · Safari")
        other = next(s for s in listed if not s["current"])
        self.assertEqual(a.delete(f"/api/sessions/{other['id']}").status_code, 200)
        self.assertEqual(b.get("/api/fleet").status_code, 401)
        self.assertEqual(a.delete(f"/api/sessions/{other['id']}").status_code, 404)

    def test_sign_out_everywhere_keeps_api_keys(self):
        self.enable()
        ses, _ = self.signin()
        dev, _ = self.signin(remember=True)
        _r, tok = self.a.create_key("agent", "control")
        r = ses.post("/api/auth/logout-everywhere")
        self.assertEqual((r.status_code, r.json()["devices_revoked"]), (200, 1))
        self.assertEqual(ses.get("/api/fleet").status_code, 401)
        self.assertEqual(dev.get("/api/fleet").status_code, 401)
        self.assertEqual(browser().get("/api/fleet", headers={"Authorization": "Bearer " + tok}).status_code, 200)
        self.assertEqual(browser().get("/api/fleet", headers=self.keyhdr).status_code, 200)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.a.dash.devices(), [])

    def test_login_is_rate_limited_per_address_even_for_the_right_password(self):
        self.enable()
        t = self.clock()
        c = browser()
        for _ in range(3):                                         # three free tries, then the wait doubles each time
            self.assertEqual(c.post("/api/auth/login", json={"password": "nope nope nope"}).status_code, 401)
        r = c.post("/api/auth/login", json={"password": PW})
        self.assertEqual(r.status_code, 429)
        self.assertTrue(int(r.headers["retry-after"]) > 0)
        t[0] += 301
        self.assertEqual(c.post("/api/auth/login", json={"password": PW}).status_code, 200)

    def test_a_flood_from_elsewhere_does_not_lock_a_clean_address(self):
        # second security review N-4: 25 failures from anybody used to refuse everybody, the owner included
        t = [1000.0]
        lim = security.LoginLimiter(per_client=3, overall=10, clock=lambda: t[0])
        for i in range(40):
            lim.failed(f"10.0.0.{i}")
        self.assertEqual(lim.retry_after("10.9.9.9"), 0)           # a fresh address is not refused for others' failures
        self.assertEqual(lim.acquire("10.9.9.9"), 0)
        lim.succeeded("10.9.9.9")


# --------------------------------------------------------------------------- pairing

class PairingTests(Base):
    def test_pairing_flow(self):
        self.enable()
        p = self.pair_code(name="Wall screen")
        self.assertRegex(p["code"], r"^\d{6}$")
        self.assertEqual((p["expires_in"], p["role"]), (security.PAIR_TTL_S, "control"))
        kiosk = browser(PI)
        r = kiosk.post("/api/auth/pair", json={"code": p["code"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["device"]["name"], "Wall screen")
        self.assertIn("httponly", cookie_named(r, security.DEVICE_COOKIE).lower())
        self.assertEqual(kiosk.get("/api/fleet").status_code, 200)
        self.assertEqual(kiosk.get("/dashboard").text, slurp(os.path.join(APP, "dashboard.html")))
        self.assertEqual(browser().post("/api/auth/pair", json={"code": p["code"]}).status_code, 401)   # single use
        self.assertIsNone(self.a.pairing_status())
        dev = self.a.list_devices()[0]
        self.assertEqual((dev["name"], dev["role"], dev["browser"]), ("Wall screen", "control", "Linux · Chromium"))

    def test_code_expires_after_five_minutes(self):
        self.enable()
        t = self.clock()
        p = self.pair_code()
        t[0] += 299
        self.assertIsNotNone(self.a.pairing_status())
        t[0] += 2
        self.assertIsNone(self.a.pairing_status())
        r = browser().post("/api/auth/pair", json={"code": p["code"]})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.a.dash.devices(), [])

    def test_a_new_code_replaces_the_old_and_cancel_works(self):
        self.enable()
        p1 = self.pair_code()
        p2 = self.pair_code()
        if p1["code"] != p2["code"]:
            self.assertEqual(browser().post("/api/auth/pair", json={"code": p1["code"]}).status_code, 401)
        self.assertEqual(browser().delete("/api/pairing", headers=self.admin).status_code, 200)
        self.assertEqual(browser().post("/api/auth/pair", json={"code": p2["code"]}).status_code, 401)

    def test_wrong_guesses_burn_the_code_and_are_rate_limited(self):
        self.enable()
        p = self.pair_code()
        wrong = "000000" if p["code"] != "000000" else "111111"
        c = browser()
        codes = []
        for _ in range(8):
            codes.append(c.post("/api/auth/pair", json={"code": wrong}).status_code)
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(set(codes[5:]), {429})                         # per address
        self.assertIsNone(self.a.pairing_status())                      # burned after 5 wrong guesses
        self.a.pair_limiter = security.LoginLimiter(per_client=5, overall=10)
        self.assertEqual(browser().post("/api/auth/pair", json={"code": p["code"]}).status_code, 401)
        self.assertEqual(self.a.dash.devices(), [])

    def test_brute_force_cannot_win(self):
        self.enable()
        t = self.clock()
        p = self.pair_code()
        won = 0
        for i in range(60):
            guess = f"{(int(p['code']) + 1 + i) % 10 ** 6:06d}"         # never the right one
            if browser().post("/api/auth/pair", json={"code": guess}).status_code == 200:
                won += 1
        self.assertEqual(won, 0)
        self.a.pair_limiter = security.LoginLimiter(per_client=5, overall=10, clock=self.a.clock)
        self.assertEqual(browser().post("/api/auth/pair", json={"code": p["code"]}).status_code, 401)   # burned

    def test_bad_input(self):
        self.enable()
        p = self.pair_code()
        c = browser()
        for body in ({}, {"code": 123456}, {"code": "12345"}, {"code": "abcdef"}, {"code": None}):
            self.assertEqual(c.post("/api/auth/pair", json=body).status_code, 401, body)
        self.assertEqual(browser().post("/api/pairing", headers=self.admin, json={"role": "root"}).status_code, 400)

    def test_view_only_pairing(self):
        self.enable()
        p = self.pair_code(role="read", name="Lobby display")
        k = browser()
        self.assertEqual(k.post("/api/auth/pair", json={"code": p["code"]}).status_code, 200)
        self.assertEqual(k.get("/api/fleet").status_code, 200)
        self.assertEqual(k.post("/api/printer/fake/start", json={}).status_code, 403)
        self.assertEqual(k.get("/api/auth/me").json()["view_only"], True)

    def test_kiosk_api_key_path_is_unchanged(self):
        self.enable()
        _r, tok = self.a.create_key("sleep-timer", "control")
        h = {"Authorization": "Bearer " + tok}
        self.assertEqual(browser().get("/api/fleet", headers=h).status_code, 200)
        self.assertEqual(browser().post("/api/printer/fake/start", headers=h, json={}).status_code, 400)


# --------------------------------------------------------------------------- the view-only role

class ViewerTests(Base):
    def test_viewer_password_rules(self):
        self.enable()
        c = browser()
        self.assertEqual(c.put("/api/security", headers=self.admin, json={"viewer_password": PW}).status_code, 400)   # = owner
        self.assertEqual(c.put("/api/security", headers=self.admin, json={"viewer_password": "short"}).status_code, 400)
        self.assertEqual(c.put("/api/security", headers=self.admin, json={"viewer_password": VPW}).status_code, 200)
        self.assertEqual(self.a.status()["viewer_password"], "set")
        self.a.dash.set_fields(login_enabled=False)
        self.assertEqual(c.put("/api/security", headers=self.admin, json={"viewer_password": VPW}).status_code, 400)  # needs sign-in
        self.a.dash.set_fields(login_enabled=True)
        self.assertEqual(c.put("/api/security", headers=self.admin, json={"clear_viewer_password": True}).status_code, 200)
        self.assertEqual(self.a.status()["viewer_password"], "not set")
        self.assertEqual(browser().post("/api/auth/login", json={"password": VPW}).status_code, 401)

    def test_viewer_can_look_but_not_touch(self):
        self.enable(vpw=VPW)
        v, r = self.signin(pw=VPW)
        self.assertEqual(r.json()["role"], "read")
        self.assertEqual(v.get("/dashboard").text, slurp(os.path.join(APP, "dashboard.html")))
        for path in ("/api/fleet", "/api/printers", "/api/printer/fake", "/api/jobs", "/api/settings", "/api/auth/me"):
            self.assertIn(v.get(path).status_code, (200,), path)
        for method, path in (("post", "/api/printer/fake/start"), ("post", "/api/printer/fake/action"),
                             ("post", "/api/printer/fake/control"), ("post", "/api/printer/fake/tray"),
                             ("post", "/api/printer/fake/delete"), ("post", "/api/printers"),
                             ("put", "/api/settings"), ("put", "/api/security"), ("get", "/api/security"),
                             ("get", "/api/keys"), ("post", "/api/keys"), ("get", "/api/jobs.csv"),
                             ("post", "/api/printer/fake/backup"), ("post", "/api/settings/test/ntfy")):
            r = getattr(v, method)(path, json={}) if method != "get" else v.get(path)
            self.assertEqual(r.status_code, 403, f"{method} {path}")
            self.assertIn("view-only", r.json()["error"])
        me = v.get("/api/auth/me").json()
        self.assertEqual((me["role"], me["view_only"], me["role_words"], me["offer_login"]), ("read", True, "View-only", False))
        owner, _ = self.signin()
        me = owner.get("/api/auth/me").json()
        self.assertEqual((me["role"], me["view_only"]), ("control", False))

    def test_viewer_is_blocked_even_when_the_key_check_is_off(self):
        self.enable(vpw=VPW)
        v, _ = self.signin(pw=VPW)
        server.SETTINGS.update({"security.api_auth": "off"})
        self.assertEqual(v.get("/api/fleet").status_code, 200)
        self.assertEqual(v.post("/api/printer/fake/start", json={}).status_code, 403)
        self.assertEqual(v.put("/api/security", json={"mode": "on"}).status_code, 403)
        self.assertEqual(server.SETTINGS.get("security.api_auth"), "off")

    def test_viewer_can_be_remembered_and_keeps_its_role(self):
        self.enable(vpw=VPW)
        v, _ = self.signin(pw=VPW, remember=True, name="Lobby")
        self.assertEqual(self.a.list_devices()[0]["role"], "read")
        self.assertEqual(v.post("/api/printer/fake/start", json={}).status_code, 403)
        self.assertEqual(v.get("/api/fleet").status_code, 200)

    def test_viewer_cannot_use_the_mcp_endpoint_with_a_cookie(self):
        self.enable(vpw=VPW)
        v, _ = self.signin(pw=VPW)
        self.assertEqual(v.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code, 401)

    def test_dashboard_marks_every_control_for_hiding(self):
        html = slurp(os.path.join(APP, "dashboard.html"))
        self.assertIn("body.viewonly [data-ctl]", html)
        for needle in ('data-ctl onclick="openSettings()"', 'data-ctl onclick="openPrinters()"', 'data-ctl onclick="openBackups()"',
                       'data-ctl onclick="askCancel()"', 'data-ctl class="primary" onclick="askPrint(', 'id="p-controls" class="ctl" data-ctl',
                       "View-only", "VIEW-ONLY"):
            self.assertIn(needle.replace('data-ctl onclick="askCancel()"', 'data-ctl class="danger" onclick="askCancel()"'), html, needle)


# --------------------------------------------------------------------------- headers + connection

class HeaderTests(Base):
    def test_html_pages_carry_the_protective_headers(self):
        self.enable()
        for resp in (self.anon.get("/dashboard"), browser().get("/dashboard")):      # login page and (gated) alike
            h = resp.headers
            self.assertEqual(h["x-frame-options"], "DENY")
            self.assertEqual(h["x-content-type-options"], "nosniff")
            self.assertEqual(h["referrer-policy"], "same-origin")
            csp = h["content-security-policy"]
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertIn("default-src 'self'", csp)
            self.assertIn("object-src 'none'", csp)
            self.assertNotIn("http:", csp.replace("http://", ""))
        c, _ = self.signin()
        self.assertEqual(c.get("/dashboard").headers["x-frame-options"], "DENY")

    def test_headers_with_sign_in_off_and_on_other_responses(self):
        r = self.anon.get("/dashboard")
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        j = self.anon.get("/api/health")
        self.assertEqual(j.headers["x-content-type-options"], "nosniff")
        self.assertNotIn("content-security-policy", j.headers)
        self.assertNotIn("x-frame-options", j.headers)
        u = self.anon.get("/api/fleet")                                        # error responses too
        self.assertEqual(u.headers["referrer-policy"], "same-origin")

    def test_framing_can_be_allowed_on_purpose(self):
        self.a.env = dict(os.environ, BAMBU_MCP_ALLOW_FRAMING="1")
        r = self.anon.get("/dashboard")
        self.assertNotIn("x-frame-options", r.headers)
        self.assertNotIn("frame-ancestors", r.headers["content-security-policy"])

    def test_the_csp_matches_what_the_dashboard_really_does(self):
        """One file, inline scripts and handlers: 'unsafe-inline' is needed - but nothing may load from outside."""
        html = slurp(os.path.join(APP, "dashboard.html"))
        for pat in (r"""<script[^>]+src=["']?(https?:)?//""", r"""<link[^>]+href=["']?(https?:)?//""",
                    r"""@import""", r"""url\(\s*["']?(https?:)?//""", r"""<iframe""", r"""<form[^>]+action=["']?https?:""",
                    r"""fetch\(\s*["'`]https?:""", r"""new WebSocket""", r"""new EventSource"""):
            self.assertIsNone(re.search(pat, html), pat)
        self.assertIn("script-src 'self' 'unsafe-inline'", security.CSP)
        self.assertIn("connect-src 'self'", security.CSP)
        self.assertIn("img-src 'self' data: blob:", security.CSP)           # BLANK gif data: URL + camera blobs

    def test_connection_words(self):
        c = browser()
        c.get("/dashboard")
        me = c.get("/api/auth/me").json()["connection"]
        self.assertFalse(me["secure"])
        self.assertIn("Not secure", me["words"])
        self.assertIn("tailscale serve", me["words"])
        me = c.get("/api/auth/me", headers={"X-Forwarded-Proto": "https"}).json()["connection"]
        self.assertTrue(me["secure"])
        self.assertIn("Secure (HTTPS)", me["words"])
        st = browser().get("/api/security", headers=self.admin).json()
        self.assertFalse(st["connection"]["secure"])
        st = browser().get("/api/security", headers={**self.admin, "X-Forwarded-Proto": "https"}).json()
        self.assertTrue(st["connection"]["secure"])


# --------------------------------------------------------------------------- scope tables

class ScopeTableTests(Base):
    def test_new_routes_are_classified(self):
        # sign-in management is "admin" since the 2026-10 hardening: a dashboard browser, never an API key
        want = {("GET", "/api/auth/me"): "read", ("GET", "/api/devices"): "admin", ("PATCH", "/api/devices/x"): "admin",
                ("DELETE", "/api/devices/x"): "admin", ("POST", "/api/devices/revoke-others"): "admin",
                ("POST", "/api/pairing"): "admin", ("DELETE", "/api/pairing"): "admin",
                ("DELETE", "/api/sessions/x"): "admin", ("POST", "/api/auth/logout-everywhere"): "admin"}
        for (m, p), scope in want.items():
            self.assertTrue(security.rest_rule_known(m, p), (m, p))
            self.assertEqual(security.rest_scope(m, p), scope, (m, p))
        self.assertIn("/api/auth/pair", security.PUBLIC_POSTS)

    def test_public_posts_are_exactly_login_logout_pair(self):
        self.assertEqual(set(security.PUBLIC_POSTS), {"/api/auth/login", "/api/auth/logout", "/api/auth/pair"})
        self.assertFalse(self.a.is_exempt("POST", "/api/pairing"))
        self.assertFalse(self.a.is_exempt("POST", "/api/auth/logout-everywhere"))
        self.assertFalse(self.a.is_exempt("GET", "/api/devices"))


# --------------------------------------------------------------------------- offline recovery

class CliTests(Base):
    def run_cli(self, *args, data=None):
        env = dict(os.environ, BAMBU_MCP_CACHE=os.path.join(data or DATA, "metacache.json"))
        return subprocess.run([sys.executable, os.path.join(APP, "security.py"), *args], capture_output=True,
                              text=True, env=env, timeout=60)

    def test_disable_dashboard_login_unlocks_a_running_hub(self):
        self.enable()
        self.assertIn("Pair with a code", browser().get("/dashboard").text)
        p = self.run_cli("disable-dashboard-login")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("OFF", p.stdout)
        self.assertFalse(self.a.login_required())                       # picked up by the running server
        self.assertEqual(browser().get("/dashboard").text, slurp(os.path.join(APP, "dashboard.html")))

    def test_reset_dashboard_password_signs_everyone_out(self):
        self.enable()
        ses, _ = self.signin()
        p = self.run_cli("reset-dashboard-password", "--password", "an entirely new secret")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("an entirely new secret", p.stdout + p.stderr)
        self.assertEqual(ses.get("/api/fleet").status_code, 401)
        self.assertEqual(browser().post("/api/auth/login", json={"password": PW}).status_code, 401)
        self.assertEqual(browser().post("/api/auth/login", json={"password": "an entirely new secret"}).status_code, 200)
        self.assertTrue(self.a.login_required())

    def test_reset_rejects_a_short_password_and_keeps_the_old_one(self):
        self.enable()
        p = self.run_cli("reset-dashboard-password", "--password", "short")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(browser().post("/api/auth/login", json={"password": PW}).status_code, 200)

    def test_list_and_revoke_devices(self):
        self.enable()
        c1, _ = self.signin(remember=True, name="Wall screen")
        c2, _ = self.signin(remember=True, name="Laptop")
        p = self.run_cli("list-devices")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Wall screen", p.stdout)
        self.assertIn("Laptop", p.stdout)
        self.assertNotRegex(p.stdout, r"[A-Za-z0-9_-]{40,}")            # no token, no hash
        dev = next(d for d in self.a.list_devices() if d["name"] == "Wall screen")
        p = self.run_cli("revoke-device", dev["id"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(c1.get("/api/fleet").status_code, 401)
        self.assertEqual(c2.get("/api/fleet").status_code, 200)
        self.assertEqual(self.run_cli("revoke-device", "d_nope").returncode, 1)
        p = self.run_cli("revoke-device", "all")
        self.assertEqual(c2.get("/api/fleet").status_code, 401)

    def test_the_old_commands_still_work(self):
        p = self.run_cli("create-key", "From the CLI", "--scope", "control")
        self.assertEqual(p.returncode, 0, p.stderr)
        tok = p.stdout.strip().splitlines()[-1]
        self.assertEqual(browser().get("/api/fleet", headers={"Authorization": "Bearer " + tok}).status_code, 200)
        self.assertIn("From the CLI", self.run_cli("list-keys").stdout)

    def test_cli_on_an_empty_directory(self):
        d = tempfile.mkdtemp(dir=_TMP)
        p = self.run_cli("list-devices", data=d)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "No trusted devices."))
        self.assertEqual(self.run_cli("disable-dashboard-login", data=d).returncode, 0)


# --------------------------------------------------------------------------- the store itself

class StoreTests(unittest.TestCase):
    def test_external_edit_is_picked_up_and_last_seen_is_not_lost(self):
        d = tempfile.mkdtemp(dir=_TMP)
        t = [1000.0]
        a = security.DashboardAuthStore(d, clock=lambda: t[0])
        a.add_device({"id": "d_1", "name": "x", "role": "control", "created": 1000, "last_seen": 1000, "hash": "h", "salt": "s"})
        t[0] = 1010.0
        a.touch_device("d_1", "10.0.0.5")                               # in memory only (throttled)
        b = security.DashboardAuthStore(d, clock=lambda: t[0])
        b.set_fields(login_enabled=False)                                # another process writes
        self.assertIs(a.login_enabled, False)
        self.assertEqual(a.get_device("d_1")["last_seen"], 1010)         # the fresher in-memory value survived the reload

    def test_corrupt_file_is_moved_aside(self):
        d = tempfile.mkdtemp(dir=_TMP)
        open(os.path.join(d, security.DASH_FILE), "w").write("{nope")
        s = security.DashboardAuthStore(d)
        self.assertEqual(s.devices(), [])
        self.assertTrue([n for n in os.listdir(d) if ".corrupt-" in n])

    def test_caps(self):
        d = tempfile.mkdtemp(dir=_TMP)
        s = security.DashboardAuthStore(d)
        for i in range(security.MAX_DEVICES + 5):
            s.add_device({"id": f"d_{i}", "last_seen": i})
        self.assertEqual(len(s.devices()), security.MAX_DEVICES)
        self.assertIsNone(s.get_device("d_0"))                           # the least recently used went


if __name__ == "__main__":
    unittest.main()
