"""Regression tests for the SECOND security review (SECURITY-REVIEW-2-2026-10-12): admin boundary without a
dashboard password, sign-in vs the key check, the login limiter, revocation, detector-test reflection, URL
passwords in alerts, file modes and the small P3 items that live in app/security.py, server.py, settings_store.py,
notify.py and joblog.py.

    python3 tests/test_fix_security2.py

Findings covered (review ids in brackets):
  * keys / the key check / sign-in / devices need the ADMIN password; the bare cookie GET /dashboard hands out is
    not admin in any mode; the first admin password can be chosen by a same-origin dashboard browser     [N-1]
  * dashboard sign-in and a view-only password need the key check ON; legacy installs behave as ON        [N-2]
  * login attempts are counted before the password is checked; exponential back-off; no global lockout;
    IPv6 by /64; X-Forwarded-For only behind BAMBU_MCP_TRUSTED_PROXIES; the CLI clears the limiter        [N-3, N-4]
  * password changes and sign-in switching end the affected sessions and trusted devices                   [N-5]
  * the detector Test shows four fixed fields and refuses loopback / link-local / metadata targets        [N-6]
  * user:password@ is stripped from alerts, error bodies and tool output                                  [N-7]
  * private data files, CSV formula guard, start-ticket model flag, plate_clear, PREHEAT_TIMEOUT_MIN=inf,
    MCP session ownership, Host parsing, CLI --password, NFC passwords, ...                             [P3]
  * the legitimate flows still work: a keyless agent in WARN, the kiosk through its relay, the sleep-timer
    daemon, phones and laptops on the LAN IP / studio-mini

Same safety rules as the other fix tests: the server is imported with a throwaway BAMBU_MCP_CONFIG / CACHE
directory; only loopback fakes are used.
"""
import concurrent.futures
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_fix_security as T  # noqa: E402   (sets the temp environment and imports the server once)

server, security, notify = T.server, T.security, T.notify
import settings_store  # noqa: E402
import joblog  # noqa: E402

HAVE = T.HAVE
APP = T.APP
CLIENT = None
PW = "correct horse battery"
VPW = "look but do not touch"
APW = "admin password 1"
PW_HASH = security.hash_password(PW) if HAVE else None         # PBKDF2 is slow on purpose: hash once per file
VPW_HASH = security.hash_password(VPW) if HAVE else None
APW_HASH = security.hash_password(APW) if HAVE else None
LOCAL = "http://testserver"
cookie_from = T.cookie_from


def setUpModule():
    global CLIENT
    if HAVE:
        CLIENT = T.TestClient(server.mcp.streamable_http_app())
        CLIENT.__enter__()
        T.CLIENT = CLIENT


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    shutil.rmtree(T._TMP, ignore_errors=True)


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


class Base(T.Base):
    """T.Base plus: no passwords at all (a fresh set-up) unless a test sets them; limiters reset."""

    def setUp(self):
        super().setUp()
        a = server.AUTH
        a.clock = time.time
        # offer_dismissed=True: this fixture models an owner who skipped the first-run "Protect your printers" step
        # (while that step is pending, a bare-cookie browser may not change anything - tests/test_first_run_password.py)
        a.dash.set_fields(login_enabled=None, owner_hash=None, viewer_hash=None, admin_hash=None, offer_dismissed=True)
        a.dash.clear_sessions()
        a.dash.remove_devices_where(lambda r: True)
        a.cancel_pairing()
        for lim in (a.limiter, a.pair_limiter, a.admin_limiter):
            lim.reset()
        a.limiter.clock = a.pair_limiter.clock = a.admin_limiter.clock = time.time
        a.env = dict(os.environ)
        a.env.pop("BAMBU_MCP_TRUSTED_PROXIES", None)
        self.a = a
        self.addCleanup(lambda: a.dash.set_fields(login_enabled=None, owner_hash=None, viewer_hash=None, admin_hash=None))

    # ---- who is who
    def dash_cookie(self, host="testserver"):
        """What a plain GET /dashboard hands out (no password set): the bare control cookie."""
        r = self.c.get("/dashboard", headers={"Host": host})
        ck = cookie_from(r)
        self.c.cookies.clear()
        self.assertTrue(ck, r.headers)
        return ck

    def curl(self):
        """Headers of a curl-style client on the Hub's own machine: the cookie, no Origin, Host 127.0.0.1:8271."""
        return {"Cookie": self.dash_cookie("127.0.0.1:8271"), "Host": "127.0.0.1:8271"}

    def browser(self, unlocked=False):
        ck = self.dash_cookie()
        if unlocked:
            ck += f"; {security.ADMIN_COOKIE}={self.a.make_admin_cookie()}"
        return {"Cookie": ck, "Origin": LOCAL}

    def set_admin(self):
        self.a.dash.set_fields(admin_hash=APW_HASH)
        self.a.dash.bump_admin_epoch()

    def set_owner(self, viewer=False):
        fields = {"owner_hash": PW_HASH, "login_enabled": True}
        if viewer:
            fields["viewer_hash"] = VPW_HASH
        self.a.dash.set_fields(**fields)

    def session_headers(self, role="control", unlocked=False):
        value, _age = self.a.start_session(role, "127.0.0.1", "Mozilla/5.0 test")
        ck = f"{security.COOKIE_NAME}={value}"
        if unlocked:
            ck += f"; {security.ADMIN_COOKIE}={self.a.make_admin_cookie()}"
        return {"Cookie": ck, "Origin": LOCAL}


# =================================================================== N-1: the admin boundary

class AdminBoundaryTests(Base):
    UNSET = "Set an admin password in Settings → Security to manage keys and security."

    def attack_calls(self, h):
        return (("post", "/api/keys", {"name": "mine", "scope": "control"}),
                ("get", "/api/keys", None),
                ("put", "/api/security", {"mode": "off"}),
                ("put", "/api/security", {"dashboard_login": True, "dashboard_password": "attacker chose it"}),
                ("get", "/api/security", None),
                ("get", "/api/devices", None),
                ("post", "/api/pairing", {}),
                ("post", "/api/auth/logout-everywhere", None),
                ("delete", "/api/sessions/s_x", None))

    def test_the_bare_dashboard_cookie_is_not_admin_in_any_mode(self):
        # the exact attack of the review: curl-style GET /dashboard (Host 127.0.0.1:8271, no Origin), then use the cookie
        for m in ("on", "warn", "off"):
            self.mode(m)
            h = self.curl()
            for method, path, body in self.attack_calls(h):
                r = getattr(self.c, method)(path, headers=h, **({"json": body} if body is not None else {}))
                self.assertEqual(r.status_code, 403, (m, method, path, r.text))
                self.assertEqual(r.json().get("admin"), "unset", (m, method, path))
                self.assertEqual(r.json()["error"], self.UNSET)
            r = self.c.put("/api/settings", headers=h, json={"values": {"security.api_auth": "off"}})
            self.assertEqual((r.status_code, r.json().get("admin")), (403, "unset"), m)
            self.assertEqual(self.a.mode(), m)                       # nothing changed
            self.assertFalse(self.a.password_set())
            self.assertEqual(self.a.keys.counts()["active"], 0)
        self.assertEqual(self.a.dash.sessions(), [])

    def test_the_same_with_an_admin_password_set_but_not_typed(self):
        self.set_admin()
        for m in ("on", "warn", "off"):
            self.mode(m)
            h = self.curl()
            for method, path, body in self.attack_calls(h):
                r = getattr(self.c, method)(path, headers=h, **({"json": body} if body is not None else {}))
                self.assertEqual((r.status_code, r.json().get("admin")), (403, "locked"), (m, method, path))
        self.assertEqual(self.a.keys.counts()["active"], 0)

    def test_everything_that_is_not_admin_still_works_with_the_bare_cookie(self):
        self.add_bambu()
        for m in ("on", "warn", "off"):
            self.mode(m)
            h = self.curl()
            self.assertEqual(self.c.get("/api/fleet", headers=h).status_code, 200, m)
            self.assertEqual(self.c.post("/api/printer/A1/action", headers=h, json={"action": "pause"}).status_code, 200, m)
            r = self.c.put("/api/settings", headers=h, json={"values": {"general.hub_name": "Shop Hub"}})
            self.assertEqual(r.status_code, 200, (m, r.text))
            server.SETTINGS.reset(["general.hub_name"])
            self.assertEqual(self.c.get("/api/auth/me", headers=h).json()["admin"], "unset")

    def test_the_first_admin_password_comes_from_a_same_origin_dashboard_browser_only(self):
        for m in ("on", "warn"):
            self.mode(m)
            body = {"password": APW}
            # curl: cookie but no Origin / Sec-Fetch-Site -> not positively same-origin
            r = self.c.post("/api/security/admin-password", headers=self.curl(), json=body)
            self.assertEqual(r.status_code, 403, (m, r.text))
            # a page on another site
            r = self.c.post("/api/security/admin-password", json=body,
                            headers={"Cookie": self.dash_cookie(), "Origin": "https://evil.example",
                                     "Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status_code, 403, m)
            # a script with no cookie at all (warn lets it through the key check; the handler still refuses)
            r = self.c.post("/api/security/admin-password", json=body, headers={"Origin": LOCAL})
            self.assertEqual(r.status_code, 403 if m == "warn" else 401, m)
            self.assertFalse(self.a.admin_password_set(), m)
        # the dashboard in a browser
        h = self.browser()
        r = self.c.post("/api/security/admin-password", headers=h, json={"password": "short"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/security/admin-password", headers=h, json={"password": APW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.a.admin_password_set())
        self.assertIn(security.ADMIN_COOKIE + "=", cookie_named(r, security.ADMIN_COOKIE))
        self.assertIn("httponly", cookie_named(r, security.ADMIN_COOKIE).lower())
        self.assertIn("samesite=strict", cookie_named(r, security.ADMIN_COOKIE).lower())
        # ...and that browser can now manage keys
        h2 = {**h, "Cookie": h["Cookie"] + f"; {security.ADMIN_COOKIE}={cookie_val(r, security.ADMIN_COOKIE)}"}
        r = self.c.post("/api/keys", headers=h2, json={"name": "agent", "scope": "read"})
        self.assertEqual(r.status_code, 201, r.text)
        # a second "first" password from a bare cookie is refused: it is locked now
        r = self.c.post("/api/security/admin-password", headers=h, json={"password": "another one 22"})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(security.verify_password(APW, self.a.dash.admin_hash))

    def test_unlock_with_the_admin_password(self):
        self.set_admin()
        h = self.browser()
        r = self.c.post("/api/auth/admin-unlock", headers=h, json={"password": "wrong wrong wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(cookie_named(r, security.ADMIN_COOKIE), "")
        r = self.c.post("/api/auth/admin-unlock", headers=h, json={"password": APW})
        self.assertEqual(r.status_code, 200, r.text)
        v = cookie_val(r, security.ADMIN_COOKIE)
        self.assertTrue(v.startswith("a1."))
        self.assertIn("max-age=1800", cookie_named(r, security.ADMIN_COOKIE).lower())
        h2 = {**h, "Cookie": h["Cookie"] + f"; {security.ADMIN_COOKIE}={v}"}
        self.assertEqual(self.c.get("/api/security", headers=h2).status_code, 200)
        self.assertEqual(self.c.get("/api/auth/me", headers=h2).json()["admin"], "ok")
        # a forged / altered cookie is nothing
        bad = v[:-4] + ("0000" if not v.endswith("0000") else "1111")
        h3 = {**h, "Cookie": h["Cookie"] + f"; {security.ADMIN_COOKIE}={bad}"}
        self.assertEqual(self.c.get("/api/security", headers=h3).status_code, 403)
        # lock
        r = self.c.post("/api/auth/admin-lock", headers=h2)
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age=0", cookie_named(r, security.ADMIN_COOKIE).lower())

    def test_unlock_without_any_password_says_what_to_do(self):
        r = self.c.post("/api/auth/admin-unlock", headers=self.browser(), json={"password": "whatever 123"})
        self.assertEqual((r.status_code, r.json()["admin"]), (403, "unset"))
        self.assertEqual(r.json()["error"], self.UNSET)

    def test_unlock_is_rate_limited_and_counted_before_it_is_checked(self):
        self.set_admin()
        h = self.browser()
        for _ in range(3):
            self.assertEqual(self.c.post("/api/auth/admin-unlock", headers=h, json={"password": "nope nope nope"}).status_code, 401)
        r = self.c.post("/api/auth/admin-unlock", headers=h, json={"password": APW})       # even the right one waits
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers["retry-after"]), 0)

    def test_an_api_key_is_never_admin_not_even_with_the_unlock_cookie(self):
        self.set_admin()
        k = self.key("control")
        unlock = f"{security.ADMIN_COOKIE}={self.a.make_admin_cookie()}"
        for m in ("on", "warn", "off"):
            self.mode(m)
            if m != "off":           # (with the check off a key is not looked at; the cookie alone is the proof)
                r = self.c.post("/api/keys", headers={**k, "Cookie": unlock}, json={"name": "x"})
                self.assertEqual((r.status_code, r.json()["required_scope"]), (403, "admin"), m)
            r = self.c.post("/api/keys", headers=k, json={"name": "x"})
            self.assertEqual((r.status_code, r.json()["required_scope"]), (403, "admin"), m)
            r = self.c.post("/api/auth/admin-unlock", headers={**k, "Origin": LOCAL}, json={"password": APW})
            self.assertIn(r.status_code, (403,), m) if m != "off" else self.assertIn(r.status_code, (200, 403))
        self.assertEqual(self.a.keys.counts()["active"], 1)

    def test_a_view_only_sign_in_is_never_admin_even_with_the_unlock_cookie(self):
        self.set_owner(viewer=True)
        self.set_admin()
        h = self.session_headers("read", unlocked=True)
        self.assertEqual(self.c.get("/api/security", headers=h).status_code, 403)
        self.assertEqual(self.c.post("/api/keys", headers=h, json={"name": "x"}).status_code, 403)

    def test_changing_the_admin_password_ends_every_earlier_unlock(self):
        self.set_admin()
        old = self.browser(unlocked=True)
        self.assertEqual(self.c.get("/api/security", headers=old).status_code, 200)
        r = self.c.post("/api/security/admin-password", headers=old, json={"password": "a new admin pw 9"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get("/api/security", headers=old).status_code, 403)           # the old unlock is dead
        fresh = {**old, "Cookie": old["Cookie"].split(";")[0] + f"; {security.ADMIN_COOKIE}={cookie_val(r, security.ADMIN_COOKIE)}"}
        self.assertEqual(self.c.get("/api/security", headers=fresh).status_code, 200)         # the changer is re-issued one

    def test_changing_or_clearing_it_from_a_locked_browser_needs_the_current_password(self):
        self.set_admin()
        h = self.browser()
        r = self.c.post("/api/security/admin-password", headers=h, json={"password": "a new admin pw 9"})
        self.assertEqual((r.status_code, r.json()["admin"]), (401, "locked"))
        r = self.c.post("/api/security/admin-password", headers=h,
                        json={"password": "a new admin pw 9", "current_password": APW})
        self.assertEqual(r.status_code, 200, r.text)
        h = self.browser(unlocked=True)
        r = self.c.post("/api/security/admin-password", headers=h, json={"clear": True})          # clearing: always the password
        self.assertEqual(r.status_code, 401)
        r = self.c.post("/api/security/admin-password", headers=h, json={"clear": True, "current_password": "a new admin pw 9"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.a.admin_password_set())

    def test_the_admin_password_may_not_equal_the_view_only_password(self):
        self.set_owner(viewer=True)
        h = self.session_headers("control")
        r = self.c.post("/api/security/admin-password", headers=h, json={"password": VPW})
        self.assertEqual(r.status_code, 400)

    def test_a_dashboard_password_without_an_admin_password_still_counts_as_admin_when_signed_in(self):
        self.set_owner()
        self.assertEqual(self.c.get("/api/security", headers=self.session_headers("control")).status_code, 200)
        self.assertEqual(self.c.get("/api/security", headers=self.session_headers("read")).status_code, 403)
        # with an admin password too, the sign-in alone is not enough
        self.set_admin()
        self.assertEqual(self.c.get("/api/security", headers=self.session_headers("control")).status_code, 403)
        self.assertEqual(self.c.get("/api/security", headers=self.session_headers("control", unlocked=True)).status_code, 200)

    def test_with_sign_in_off_the_dashboard_password_unlocks(self):
        self.a.dash.set_fields(owner_hash=PW_HASH, login_enabled=False)
        h = self.browser()
        r = self.c.post("/api/auth/admin-unlock", headers=h, json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        v = cookie_val(r, security.ADMIN_COOKIE)
        h2 = {**h, "Cookie": h["Cookie"] + f"; {security.ADMIN_COOKIE}={v}"}
        self.assertEqual(self.c.get("/api/security", headers=h2).status_code, 200)

    def test_the_first_run_prompt_asks_for_the_admin_password_and_goes_away(self):
        self.assertEqual(self.a.keys.meta["install"], "fresh")
        self.a.dash.set_fields(offer_dismissed=False)          # the step has not been answered yet
        h = self.browser()
        me = self.c.get("/api/auth/me", headers=h).json()
        self.assertTrue(me["offer_login"])
        self.assertEqual((me["admin"], me["admin_password"]), ("unset", False))
        self.assertEqual(self.c.post("/api/auth/dismiss-offer", headers=h).status_code, 200)
        self.assertFalse(self.c.get("/api/auth/me", headers=h).json()["offer_login"])
        self.a.dash.set_fields(offer_dismissed=False)
        self.set_admin()
        self.assertFalse(self.c.get("/api/auth/me", headers=h).json()["offer_login"])
        # a key cannot dismiss it
        self.assertEqual(self.c.post("/api/auth/dismiss-offer", headers=self.key("control")).status_code, 403)

    def test_warnings_say_it_in_words(self):
        ids = {w["id"] for w in self.a.warnings()}
        self.assertIn("no_admin_password", ids)
        self.assertIn("no_dashboard_password", ids)
        self.set_admin()
        self.assertNotIn("no_admin_password", {w["id"] for w in self.a.warnings()})
        self.assertEqual(self.a.status()["admin_password"], "set")

    def test_the_cli_can_reset_and_clear_the_admin_password(self):
        with mock.patch("sys.stdin", io.StringIO("brand new admin pw\n")):
            self.assertEqual(security._cli(["reset-admin-password", "--password-stdin"]), 0)
        self.a.dash._sync()
        self.assertTrue(security.verify_password("brand new admin pw", self.a.dash.admin_hash))
        self.assertEqual(security._cli(["clear-admin-password"]), 0)
        self.assertFalse(self.a.admin_password_set())


# =================================================================== N-2: sign-in only counts while the key check is ON

class SignInNeedsKeyCheckTests(Base):
    def setUp(self):
        super().setUp()
        self.set_admin()
        self.prn = self.add_bambu()

    def test_sign_in_cannot_be_turned_on_in_warn_or_off(self):
        h = self.browser(unlocked=True)
        for m in ("warn", "off"):
            self.mode(m)
            r = self.c.put("/api/security", headers=h, json={"dashboard_login": True, "dashboard_password": PW})
            self.assertEqual(r.status_code, 400, (m, r.text))
            self.assertIn("API key check", r.json()["error"])
            self.assertIn("API keys", r.json()["error"])
            self.assertFalse(self.a.login_required(), m)
            self.assertEqual(self.a.mode(), m)
            # the view-only password too
            r = self.c.put("/api/security", headers=h, json={"viewer_password": VPW})
            self.assertEqual(r.status_code, 400, m)
            # and the generic settings route
            r = self.c.put("/api/settings", headers=h, json={"values": {"security.dashboard_password": PW}})
            self.assertEqual(r.status_code, 400, (m, r.text))
            self.assertFalse(self.a.password_set(), m)

    def test_it_can_be_turned_on_together_with_the_key_check(self):
        self.mode("warn")
        h = self.browser(unlocked=True)
        r = self.c.put("/api/security", headers=h, json={"mode": "on", "dashboard_login": True, "dashboard_password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((self.a.mode(), self.a.login_required()), ("on", True))

    def test_the_key_check_cannot_be_lowered_while_sign_in_is_on(self):
        self.mode("on")
        self.set_owner(viewer=True)
        h = self.session_headers("control", unlocked=True)
        for m in ("warn", "off"):
            r = self.c.put("/api/security", headers=h, json={"mode": m})
            self.assertEqual(r.status_code, 400, m)
            r = self.c.put("/api/settings", headers=h, json={"values": {"security.api_auth": m}})
            self.assertEqual(r.status_code, 400, m)
            self.assertEqual(self.a.mode(), "on")
        self.assertEqual(self.c.put("/api/security", headers=h, json={"mode": "on"}).status_code, 200)

    def test_a_cookieless_request_gets_no_control_when_sign_in_is_on_whatever_the_stored_mode(self):
        # an install that was set up before this rule: sign-in on, key check stored as warn / off
        self.set_owner(viewer=True)
        for m in ("warn", "off"):
            self.mode(m)
            self.prn.calls.clear()
            r = self.c.post("/api/printer/A1/action", json={"action": "pause"})              # no cookie at all
            self.assertEqual(r.status_code, 401, m)
            self.assertEqual(self.prn.calls, [], m)
            self.assertEqual(self.c.get("/api/fleet").status_code, 401, m)
            self.assertEqual(self.c.get("/api/jobs.csv").status_code, 401, m)
            self.assertEqual(self.c.put("/api/settings", json={"values": {"general.hub_name": "x"}}).status_code, 401, m)
            # a view-only browser that simply drops its cookie gets no more than it had
            vh = self.session_headers("read")
            self.assertEqual(self.c.post("/api/printer/A1/action", headers=vh, json={"action": "pause"}).status_code, 403, m)
            self.assertEqual(self.prn.calls, [], m)
            # the real owner is fine
            oh = self.session_headers("control")
            self.assertEqual(self.c.post("/api/printer/A1/action", headers=oh, json={"action": "pause"}).status_code, 200, m)
            # a key still works
            self.assertEqual(self.c.get("/api/fleet", headers=self.key("read")).status_code, 200, m)
        # public pages stay public
        for path in ("/api/health", "/dashboard", "/favicon.svg"):
            self.assertEqual(self.c.get(path).status_code, 200, path)

    def test_mcp_keeps_the_modes_leniency_for_a_legacy_install(self):
        self.set_owner()
        self.mode("warn")
        h = self.mcp_session({})
        r, msg = self.call(h, "pause_print", {"printer": "A1"})
        self.assertEqual(r.status_code, 200)

    def test_the_warning_is_there_for_a_legacy_install(self):
        self.set_owner()
        self.mode("warn")
        self.assertIn("signin_needs_key_check", {w["id"] for w in self.a.warnings()})


# =================================================================== N-3 / N-4: the login limiter

class LimiterTests(Base):
    def test_parallel_guesses_from_one_address_are_not_all_checked(self):
        lim = security.LoginLimiter(per_client=3)
        admitted = []

        def go(_):
            if lim.acquire("1.2.3.4") == 0:
                admitted.append(1)
        with concurrent.futures.ThreadPoolExecutor(40) as ex:
            list(ex.map(go, range(40)))
        self.assertLessEqual(len(admitted), 3)

    def test_parallel_guesses_over_http(self):
        self.mode("on")
        self.set_owner()
        for lim in (self.a.limiter,):
            lim.reset()
        statuses = []

        def go(_):
            r = CLIENT.post("/api/auth/login", json={"password": "wrong wrong wrong"}, headers={"Origin": LOCAL})
            statuses.append(r.status_code)
        with concurrent.futures.ThreadPoolExecutor(30) as ex:
            list(ex.map(go, range(30)))
        self.assertLessEqual(statuses.count(401), 3, statuses)
        self.assertGreaterEqual(statuses.count(429), 25, statuses)

    def test_back_off_doubles_and_is_capped(self):
        t = [1000.0]
        lim = security.LoginLimiter(per_client=3, cap=60, clock=lambda: t[0])
        waits = []
        for _ in range(9):
            w = lim.acquire("a")
            while w:                                   # wait it out, as a patient client would
                t[0] += w
                w = lim.acquire("a")
            lim.failed("a")
            waits.append(lim.retry_after("a"))
        self.assertEqual(waits[2], 2)
        self.assertEqual(waits[3], 4)
        self.assertEqual(waits[4], 8)
        self.assertEqual(waits[5], 16)
        self.assertEqual(waits[6], 32)
        self.assertEqual(waits[7], 60)                                    # capped, not hours
        self.assertEqual(waits[8], 60)

    def test_a_flood_from_elsewhere_does_not_lock_out_a_clean_address(self):
        t = [1000.0]
        lim = security.LoginLimiter(per_client=3, overall=20, clock=lambda: t[0])
        for i in range(300):
            lim.failed(f"198.51.100.{i % 250}")
        self.assertEqual(lim.acquire("192.168.1.50"), 0)        # the owner, who has not failed yet
        lim.succeeded("192.168.1.50")                           # ...types the right password: in
        self.assertEqual(lim.retry_after("192.168.1.50"), 0)
        self.assertGreater(lim.retry_after("198.51.100.1"), 0)  # an address that is already guessing is slowed
        self.assertEqual(lim.acquire("192.168.1.51"), 0)        # a typo during the flood costs a short wait, not a lockout
        lim.failed("192.168.1.51")
        self.assertLessEqual(lim.retry_after("192.168.1.51"), 2)
        t[0] += 3
        self.assertEqual(lim.acquire("192.168.1.51"), 0)
        lim.succeeded("192.168.1.51")

    def test_the_right_password_wipes_the_record(self):
        lim = security.LoginLimiter(per_client=3)
        for _ in range(2):
            self.assertEqual(lim.acquire("a"), 0)
            lim.failed("a")
        self.assertEqual(lim.acquire("a"), 0)
        lim.succeeded("a")
        self.assertEqual(lim.retry_after("a"), 0)
        for _ in range(3):
            self.assertEqual(lim.acquire("a"), 0)
            lim.failed("a")
        self.assertGreater(lim.retry_after("a"), 0)

    def test_an_attempt_that_never_reached_a_verdict_is_given_back(self):
        lim = security.LoginLimiter(per_client=1)
        self.assertEqual(lim.acquire("a"), 0)
        lim.release("a")
        self.assertEqual(lim.acquire("a"), 0)

    def test_ipv6_is_grouped_by_64_and_v4_mapped_is_v4(self):
        k = security.limiter_key
        self.assertEqual(k("2001:db8:1:2::1"), k("2001:db8:1:2:ffff:ffff:ffff:ffff"))
        self.assertNotEqual(k("2001:db8:1:2::1"), k("2001:db8:1:3::1"))
        self.assertEqual(k("::ffff:192.0.2.7"), k("192.0.2.7"))
        self.assertEqual(k("192.0.2.7"), "192.0.2.7")

    def test_x_forwarded_for_counts_only_behind_a_trusted_proxy(self):
        a = self.a
        h = {"x-forwarded-for": "203.0.113.9"}
        self.assertEqual(a.limiter_client(h, "127.0.0.1"), "127.0.0.1")             # not configured: ignored
        a.env = {"BAMBU_MCP_TRUSTED_PROXIES": "127.0.0.1, 10.0.0.0/8"}
        self.assertEqual(a.limiter_client(h, "127.0.0.1"), "203.0.113.9")           # the proxy says who it is
        self.assertEqual(a.limiter_client({"x-forwarded-for": "198.51.100.1, 10.1.2.3"}, "10.0.0.5"), "198.51.100.1")
        self.assertEqual(a.limiter_client(h, "192.168.1.50"), "192.168.1.50")       # a stranger's header: ignored
        self.assertEqual(a.limiter_client({"x-forwarded-for": "garbage"}, "127.0.0.1"), "127.0.0.1")

    def test_x_forwarded_for_does_not_help_a_guesser_over_http(self):
        self.mode("on")
        self.set_owner()
        for i in range(8):
            r = self.c.post("/api/auth/login", json={"password": "wrong wrong wrong"},
                            headers={"Origin": LOCAL, "X-Forwarded-For": f"203.0.113.{i}"})
        self.assertEqual(r.status_code, 429)

    def test_the_cli_clears_the_running_limiters(self):
        for _ in range(3):
            self.a.limiter.acquire("c"); self.a.limiter.failed("c")
        self.a.admin_limiter.acquire("c"); self.a.admin_limiter.failed("c")
        self.assertGreater(self.a.limiter.retry_after("c"), 0)
        time.sleep(0.02)
        self.assertEqual(security._cli(["disable-dashboard-login"]), 0)
        self.assertEqual(self.a.limiter.retry_after("c"), 0)
        self.assertEqual(self.a.admin_limiter.retry_after("c"), 0)
        for _ in range(3):
            self.a.limiter.acquire("c"); self.a.limiter.failed("c")
        time.sleep(0.02)
        with mock.patch("sys.stdin", io.StringIO("a new dashboard pw\n")):
            self.assertEqual(security._cli(["reset-dashboard-password", "--password-stdin"]), 0)
        self.assertEqual(self.a.limiter.retry_after("c"), 0)

    def test_the_cli_password_on_the_command_line_is_warned_about(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(security._cli(["reset-dashboard-password", "--password", "a new dashboard pw"]), 0)
        self.assertIn("process list", err.getvalue())
        self.assertIn("--password-stdin", err.getvalue())


# =================================================================== N-5: revocation

class RevocationTests(Base):
    def setUp(self):
        super().setUp()
        self.mode("on")
        self.set_owner(viewer=True)
        self.set_admin()
        self.ctl_dev, self.ctl_cookie, _ = self.make_device("control", "Wall")
        self.ro_dev, self.ro_cookie, _ = self.make_device("read", "Guest")
        self.admin = self.session_headers("control", unlocked=True)
        self.key_hdr = self.key("control")

    def make_device(self, role, name):
        dev, value, age = self.a.add_device(role, name, "10.0.0.5", "Mozilla/5.0 (iPad) Safari/605")
        return dev, value, age

    def dev_status(self, cookie_value):
        return self.c.get("/api/fleet", headers={"Cookie": f"{security.DEVICE_COOKIE}={cookie_value}"}).status_code

    def test_changing_the_dashboard_password_ends_full_access_devices_and_sessions_only(self):
        other = self.session_headers("control")
        read_sess = self.session_headers("read")
        r = self.c.put("/api/security", headers=self.admin, json={"dashboard_password": "a brand new password"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["revoked"]["devices"], 1)
        self.assertEqual(self.dev_status(self.ctl_cookie), 401)             # the wall screen must sign in / pair again
        self.assertEqual(self.dev_status(self.ro_cookie), 200)              # view-only is untouched
        self.assertEqual(self.c.get("/api/fleet", headers=other).status_code, 401)
        self.assertEqual(self.c.get("/api/fleet", headers=read_sess).status_code, 200)
        self.assertEqual(self.c.get("/api/fleet", headers=self.key_hdr).status_code, 200)       # keys are never touched
        # the one who changed it is handed a fresh session and keeps the unlock
        self.assertTrue(cookie_val(r, security.COOKIE_NAME).startswith("v2."))
        self.assertTrue(cookie_val(r, security.ADMIN_COOKIE))

    def test_changing_or_removing_the_view_only_password_ends_view_only_access(self):
        read_sess = self.session_headers("read")
        r = self.c.put("/api/security", headers=self.admin, json={"viewer_password": "another viewer pw 5"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.dev_status(self.ro_cookie), 401)
        self.assertEqual(self.c.get("/api/fleet", headers=read_sess).status_code, 401)
        self.assertEqual(self.dev_status(self.ctl_cookie), 200)             # full access is untouched
        ro2, ro2_cookie, _ = self.make_device("read", "Guest 2")
        r = self.c.put("/api/security", headers=self.admin, json={"clear_viewer_password": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.dev_status(ro2_cookie), 401)
        self.assertEqual(self.dev_status(self.ctl_cookie), 200)

    def test_turning_sign_in_off_ends_devices_and_they_do_not_come_back_with_it(self):
        r = self.c.put("/api/security", headers=self.admin, json={"dashboard_login": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.a.dash.devices(), [])                         # forgotten, not parked
        r = self.c.put("/api/security", headers=self.browser(unlocked=True),
                       json={"dashboard_login": True, "dashboard_password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.dev_status(self.ctl_cookie), 401)
        self.assertEqual(self.dev_status(self.ro_cookie), 401)

    def test_turning_sign_in_on_signs_everyone_out(self):
        self.a.dash.set_fields(login_enabled=False)
        d, cookie, _ = self.make_device("control", "Old tablet")
        self.set_owner()
        self.a.dash.set_fields(login_enabled=False)
        r = self.c.put("/api/security", headers=self.browser(unlocked=True), json={"dashboard_login": True, "dashboard_password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.a.dash.devices(), [])

    def test_the_cli_password_reset_forgets_full_access_devices(self):
        with mock.patch("sys.stdin", io.StringIO("reset by the cli 99\n")):
            self.assertEqual(security._cli(["reset-dashboard-password", "--password-stdin"]), 0)
        self.assertEqual(self.dev_status(self.ctl_cookie), 401)
        self.assertEqual(self.dev_status(self.ro_cookie), 200)

    def test_the_owner_password_may_not_be_the_view_only_one(self):
        r = self.c.put("/api/security", headers=self.admin, json={"dashboard_password": VPW})
        self.assertEqual(r.status_code, 400, r.text)
        r = self.c.put("/api/security", headers=self.admin, json={"viewer_password": PW})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_non_remembered_session_cookie_is_a_browser_session_cookie(self):
        self.a.limiter.reset()
        r = self.c.post("/api/auth/login", json={"password": PW}, headers={"Origin": LOCAL})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("max-age", cookie_named(r, security.COOKIE_NAME).lower())
        self.c.cookies.clear()


# =================================================================== N-6: the detector Test

class Planted:
    """A loopback 'internal service' with a planted value in every answer, to see whether it is reflected."""

    def __init__(self, status=200, body=None):
        self.paths = []
        outer = self
        payload = body if body is not None else json.dumps(
            {"api": 1, "status": "ok", "svc": "PLANTED-INTERNAL-VALUE", "n": 7, "secret": {"k": "PLANTED-NESTED"}}).encode()

        class H(T.BaseHTTPRequestHandler):
            def _h(self):
                outer.paths.append(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            do_GET = do_POST = _h

            def log_message(self, *a):
                pass
        self.httpd = T.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class DetectorTestTests(Base):
    def setUp(self):
        super().setUp()
        self.mode("off")
        self.h = self.browser()

    def post(self, body):
        return self.c.post("/api/detection/test", headers=self.h, json=body)

    def test_only_four_fixed_fields_come_back_never_the_raw_body(self):
        for status in (200, 500, 403):
            p = Planted(status)
            self.addCleanup(p.close)
            r = self.post({"mode": "remote", "url": p.url})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("PLANTED", r.text, status)
            self.assertEqual(set(r.json()["detector"]), {"reachable", "token_ok", "healthy", "version"}, status)

    def test_a_trailing_hash_or_question_mark_cannot_swallow_the_healthz_path(self):
        p = Planted()
        self.addCleanup(p.close)
        for suffix in ("#", "?", "?x=1", "#frag", "/#"):
            p.paths.clear()
            self.post({"mode": "remote", "url": p.url + "/secret" + suffix})
            self.assertTrue(p.paths and all(path.startswith("/secret/healthz") or path == "/secret/v1/score" or path.startswith("/secret/v1")
                                           for path in p.paths), (suffix, p.paths))
            self.assertNotIn("/secret", [x for x in p.paths if x in ("/secret",)])

    def test_loopback_link_local_and_metadata_targets_are_refused_without_a_connection(self):
        p = Planted()
        self.addCleanup(p.close)
        with mock.patch.dict(os.environ, {"BAMBU_MCP_ALLOW_LOOPBACK_DETECTOR": ""}):
            for url in (p.url, "http://localhost:9", "http://[::1]:9", "http://127.1:9", "http://0.0.0.0:9",
                        "http://169.254.169.254/latest/meta-data", "http://[::ffff:127.0.0.1]:9",
                        "http://100.100.100.200"):
                p.paths.clear()
                r = self.post({"mode": "remote", "url": url})
                self.assertEqual(r.status_code, 200, url)
                j = r.json()
                self.assertFalse(j["ok"], url)
                self.assertIn("will not test an address", j["summary"], url)
                self.assertEqual(p.paths, [], url)
            # LAN addresses, the compose default and host.docker.internal are fine
            self.assertIsNone(server._detector_target_refusal("remote", "http://192.168.1.20:8090"))
            self.assertIsNone(server._detector_target_refusal("remote", "http://host.docker.internal:8090"))
            self.assertIsNone(server._detector_target_refusal("local", server.DETECTOR_DEFAULT_URL))
            with mock.patch.dict(os.environ, {"DETECTOR_URL": "http://127.0.0.1:8090"}):
                self.assertIsNone(server._detector_target_refusal("local", "http://127.0.0.1:8090"))
                self.assertIsNotNone(server._detector_target_refusal("remote", "http://127.0.0.1:8090"))
            self.assertEqual(server._clean_detector_url("http://u:p@10.0.0.5:8090/x?y=1#z"), "http://10.0.0.5:8090/x")

    def test_the_opt_in_env_allows_a_detector_next_to_a_non_docker_hub(self):
        p = Planted()
        self.addCleanup(p.close)
        with mock.patch.dict(os.environ, {"BAMBU_MCP_ALLOW_LOOPBACK_DETECTOR": "1"}):
            self.assertTrue(self.post({"mode": "remote", "url": p.url}).json()["reachable"])


# =================================================================== N-7: user:password@ in what leaves the Hub

class UrlPasswordTests(Base):
    PW_URL = "http://admin:hunter2@192.168.1.60/api/version"

    def test_scrub_text(self):
        s = security.scrub_text
        self.assertEqual(s(f"Could not reach {self.PW_URL}: refused"), "Could not reach http://192.168.1.60/api/version: refused")
        self.assertEqual(s("two https://a:b@h1/x and ftp://u@h2"), "two https://h1/x and ftp://h2")
        self.assertEqual(s("mail me@example.com at https://example.com"), "mail me@example.com at https://example.com")
        self.assertEqual(s("p@ss in http://u:p@ss@host/x"), "p@ss in http://host/x")
        self.assertEqual(s(None), None)
        self.assertEqual(s("no urls here"), "no urls here")

    def test_alerts_never_carry_a_url_password(self):
        cap = T.Capture()
        self.addCleanup(cap.close)
        seen = []
        orig = notify.http_request

        def spy(method, url, headers=None, data=None, timeout=10):
            seen.append(data or b"")
            return orig(method, url, headers, data, timeout)
        server.SETTINGS.update({"webhook.url": cap.url + "/h"})
        self.addCleanup(lambda: server.SETTINGS.reset(["webhook.url"]))
        ch = notify.WebhookChannel({"url": cap.url + "/h"})
        with mock.patch.object(notify, "http_request", spy):
            ok, err = ch.send(f"A1 offline: {self.PW_URL}", f"Could not reach {self.PW_URL} and https://u:hunter2@other.local/x", None, "failure", {})
        self.assertTrue(ok, err)
        blob = b"".join(seen).decode()
        self.assertNotIn("hunter2", blob)
        self.assertIn("http://192.168.1.60/api/version", blob)

    def test_the_notification_history_and_error_text_are_scrubbed(self):
        class Ev:
            kind, printer, level, ts = "offline", "A1", "failure", None
            title = f"Offline: {UrlPasswordTests.PW_URL}"
            body = "x"
        server.NOTIFIER._record(Ev(), {"sent": False, "skipped": "test", "channels": {}})
        self.assertNotIn("hunter2", json.dumps(server.NOTIFIER.recent()))
        ch = notify.WebhookChannel({"url": "http://admin:hunter2@127.0.0.1:9/hook"})
        ok, err = ch.send("t", "b")
        self.assertFalse(ok)
        self.assertNotIn("hunter2", err)

    def test_route_errors_and_tool_output_are_scrubbed(self):
        with mock.patch.object(server.SETTINGS, "describe", side_effect=RuntimeError(f"cannot reach {self.PW_URL}")):
            r = self.c.get("/api/settings", headers=self.browser())
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("hunter2", r.text)
        self.assertIn("192.168.1.60", r.text)

        class Boom(T.FakeBambu):
            def snapshot(self):
                raise RuntimeError(f"printer unreachable at {UrlPasswordTests.PW_URL}")
        server.PRINTERS["a1"] = Boom("A1")
        self.mode("warn")
        h = self.mcp_session({})
        r, msg = self.call(h, "printer_status", {"printer": "A1"})
        self.assertNotIn("hunter2", r.text)


# =================================================================== files, CSV, small P3 items

class FileModeTests(Base):
    def test_the_server_sets_a_private_umask(self):
        old = os.umask(0o022)
        os.umask(old)
        self.assertEqual(old & 0o077, 0o077)

    def test_harden_data_files_fixes_old_world_readable_files(self):
        d = tempfile.mkdtemp(prefix="modes-")
        self.addCleanup(shutil.rmtree, d, True)
        for n in server.PRIVATE_DATA_FILES + ("notes.txt",):
            p = os.path.join(d, n)
            with open(p, "w") as fh:
                fh.write("{}")
            os.chmod(p, 0o644)
        changed = server.harden_data_files(d)
        self.assertIn("settings.json", changed)
        self.assertIn("printers.json", changed)
        self.assertIn("jobs.sqlite", changed)
        for n in server.PRIVATE_DATA_FILES:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, n)).st_mode), 0o600, n)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "notes.txt")).st_mode), 0o644)       # only the Hub's own files
        self.assertEqual(server.harden_data_files(d), [])

    def test_settings_json_and_the_other_files_are_written_0600(self):
        server.SETTINGS.update({"general.hub_name": "Mode Test"})
        self.addCleanup(lambda: server.SETTINGS.reset(["general.hub_name"]))
        data = server.DATA_DIR
        for n in ("settings.json", "api_keys.json", "dashboard_auth.json", "jobs.sqlite"):
            p = os.path.join(data, n)
            if os.path.exists(p):
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode) & 0o077, 0, n)
        self.assertTrue(os.path.exists(os.path.join(data, "settings.json")))


class CsvGuardTests(unittest.TestCase):
    def test_every_way_of_starting_a_formula_is_neutralised(self):
        g = joblog.JobLog._csv_safe
        for v in ("=1+1", "+1", "-1+1", "@SUM(A1)", "\t=1", "\r=1", " =1+1", "   =cmd|' /c calc'!A0", "\n=1+1",
                  " =1+1", "​=1+1", "＝1+1", "＋1", "－1", "＠SUM(1)", " ＝1+1",
                  "− 1", "﻿=1+1"):
            self.assertTrue(g(v).startswith("'"), repr(v))
        for v in ("benchy.3mf", "a=b", "x;=1+1", "", "1 - 2", "plate 1", 5, None, 1.5):
            self.assertEqual(g(v), v, repr(v))

    def test_every_cell_of_the_export_is_quoted(self):
        log = joblog.JobLog(os.path.join(tempfile.mkdtemp(prefix="csvq-"), "jobs.sqlite"))
        out = log.export_csv()
        header = out.splitlines()[0]
        self.assertTrue(header.startswith("id,printer,model"))


class SmallP3Tests(Base):
    def test_preheat_timeout_env_of_infinity_does_not_break_the_settings_page(self):
        for bad in ("inf", "-inf", "nan", "1e999", "huge"):
            st = settings_store.SettingsStore(tempfile.mkdtemp(prefix="inf-"), env={"PREHEAT_TIMEOUT_MIN": bad})
            doc = st.describe()                                    # used to raise OverflowError -> HTTP 500
            self.assertTrue(doc["sections"], bad)
            self.assertEqual(st.get("printers.preheat_auto_off_min"), 15, bad)
        field = settings_store.FIELDS["printers.preheat_auto_off_min"]
        with self.assertRaises(ValueError):
            settings_store.coerce(field, float("inf"))
        with self.assertRaises(ValueError):
            settings_store.coerce(field, "1e999")

    def test_rest_plate_clear_must_be_literally_true(self):
        prn = self.add_bambu()
        self.mode("off")
        d = self.c.post("/api/printer/A1/suggest", json={"filename": "job.gcode.3mf"}).json()
        for bad in ("no", "false", "yes", 1, "true", [True], {"a": 1}, None, 0):
            r = self.c.post("/api/printer/A1/start", json={"filename": "job.gcode.3mf", "ams_mapping": d["ams_mapping"],
                                                          "confirm": d["confirm_string"], "plate_clear": bad})
            self.assertEqual(r.status_code, 400, repr(bad))
            self.assertEqual(r.json()["error"], "plate_clear must be confirmed")
        self.assertEqual(prn.started, [])
        r = self.c.post("/api/printer/A1/start", json={"filename": "job.gcode.3mf", "ams_mapping": d["ams_mapping"],
                                                      "confirm": d["confirm_string"], "plate_clear": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(prn.started), 1)

    def test_a_ticket_that_remembers_a_model_mismatch_does_not_start_without_the_override(self):
        prn = self.add_bambu()
        server._START_PENDING.clear()
        size = len(prn.files["job.gcode.3mf"])
        nonce = server.START_TICKETS.issue(prn, "job.gcode.3mf", size, [0], server._ams_fingerprint(prn), True)
        # the cached model check is gone by the time of the start (cache entry evicted / metadata missing)
        with mock.patch.object(server, "_model_check_cached", return_value={"model_match": None, "model_warning": ""}):
            out = server.start_print("A1", "job.gcode.3mf", [0], nonce, bed_clear_confirmed_by_human=True)
        self.assertTrue(out.startswith("REFUSED"), out)
        self.assertIn("different printer model", out)
        self.assertEqual(prn.started, [])
        # the refusal keeps the ticket; with the human's override (and its tag) it goes through
        with mock.patch.object(server, "_model_check_cached", return_value={"model_match": None, "model_warning": ""}):
            out = server.start_print("A1", "job.gcode.3mf", [0], nonce + "|" + server.modelcheck.MODEL_OVERRIDE_TAG,
                                     model_override=True, bed_clear_confirmed_by_human=True)
        self.assertIn('"sent": true', out)

    def test_host_header_edge_cases(self):
        ok = lambda host: self.a.host_ok({"host": host})
        for good in ("127.0.0.1:8271", "localhost:8271", "192.168.1.203:8271", "studio-mini:8271", "hub:80",
                     "[::1]:8271", "pi.local", "box.lan:8271", "hub.tail1234.ts.net", "localhost."):
            self.assertTrue(ok(good), good)
        for bad in ("127.0.0.1:8271@evil.com", "localhost:evil.com", "evil.com", "evil.example:8271", "a b",
                    "127.0.0.1:99999", "127.0.0.1:0", "user@localhost", "localhost/evil", "evil.com?.local", "[::1", "x#y.local"):
            self.assertFalse(ok(bad), bad)
        self.a.env = {"BAMBU_MCP_ALLOWED_HOSTS": "hub.example.com:8271, Other.Example.org"}
        self.assertTrue(ok("hub.example.com"))
        self.assertTrue(ok("hub.example.com:8271"))
        self.assertTrue(ok("other.example.org:1234"))
        self.assertFalse(ok("hub.example.com.evil.net"))

    def test_x_forwarded_host_is_only_trusted_from_a_listed_proxy_when_proxies_are_configured(self):
        h = {"host": "localhost:8271", "origin": "https://hub.tail1.ts.net", "x-forwarded-host": "hub.tail1.ts.net"}
        self.assertTrue(security.same_origin(h))                                      # default: as before
        self.assertFalse(security.same_origin(h, trust_forwarded=False))
        self.assertTrue(self.a.forwarded_host_trusted("203.0.113.5"))                 # nothing configured: as before
        self.a.env = {"BAMBU_MCP_TRUSTED_PROXIES": "127.0.0.1"}
        self.assertTrue(self.a.forwarded_host_trusted("127.0.0.1"))
        self.assertFalse(self.a.forwarded_host_trusted("203.0.113.5"))

    def test_strict_same_origin(self):
        so = security.same_origin_strict
        self.assertFalse(so({"host": "hub"}))                                         # curl: neither header
        self.assertTrue(so({"host": "hub", "origin": "http://hub"}))
        self.assertTrue(so({"host": "hub", "sec-fetch-site": "same-origin"}))
        self.assertFalse(so({"host": "hub", "sec-fetch-site": "none"}))
        self.assertFalse(so({"host": "hub", "origin": "http://evil"}))

    def test_passwords_are_normalised_so_a_phone_and_a_mac_agree(self):
        nfc, nfd = "café au lait 12", "café au lait 12"
        h = security.hash_password(nfd)
        self.assertTrue(security.verify_password(nfc, h))
        self.assertTrue(security.verify_password(nfd, h))
        legacy = "pbkdf2$1000$" + "ab" * 16 + "$" + __import__("hashlib").pbkdf2_hmac(
            "sha256", nfd.encode(), bytes.fromhex("ab" * 16), 1000).hex()
        self.assertTrue(security.verify_password(nfd, legacy))                        # a hash made before the change
        self.assertFalse(security.verify_password("other", h))

    def test_stray_spaces_round_the_password_do_not_lock_the_owner_out(self):
        self.set_owner(viewer=True)
        self.assertEqual(self.a.check_login(PW), "control")
        self.assertEqual(self.a.check_login("  " + PW + " "), "control")
        self.assertEqual(self.a.check_login(VPW + " "), "read")
        self.assertIsNone(self.a.check_login("nope " + PW))

    def test_device_names_lose_bidi_and_control_characters(self):
        self.assertEqual(security.clean_name("Wall‮ screen\x85​!"), "Wall  screen  !")
        self.assertEqual(security.clean_name("  ‮  ", "fallback"), "fallback")


class McpSessionOwnershipTests(Base):
    def setUp(self):
        super().setUp()
        self.add_bambu()
        self.mode("on")
        self.ctl, self.ro = self.key("control"), self.key("read")

    def test_a_session_belongs_to_the_key_that_opened_it(self):
        h = self.mcp_session(self.ctl)
        # the read key learns the id and tries to use or end it
        r = self.c.delete("/mcp", headers={**T.MCP_HEADERS, **self.ro, "mcp-session-id": h["mcp-session-id"]})
        self.assertEqual(r.status_code, 403, r.text)
        r = self.c.post("/mcp", headers={**h, **self.ro}, json=T.rpc("tools/list"))
        self.assertEqual(r.status_code, 403)
        # a keyless caller (warn mode) cannot ride it either
        self.mode("warn")
        r = self.c.post("/mcp", headers={k: v for k, v in h.items() if k != "Authorization"}, json=T.rpc("tools/list"))
        self.assertEqual(r.status_code, 403)
        # the owner of the session still can
        self.mode("on")
        r, msg = self.call(h, "list_printers")
        self.assertEqual(r.status_code, 200)
        r = self.c.delete("/mcp", headers={**T.MCP_HEADERS, **self.ctl, "mcp-session-id": h["mcp-session-id"]})
        self.assertIn(r.status_code, (200, 202, 204))

    def test_keyless_sessions_in_warn_still_work_for_an_agent(self):
        self.mode("warn")
        h = self.mcp_session({})
        r, msg = self.call(h, "list_printers")
        self.assertEqual(r.status_code, 200)
        self.assertIn("A1", T.tool_text(msg))


# =================================================================== the legitimate flows still work

class LegitFlowTests(Base):
    """The owner's real set-up: WARN mode, no dashboard password, a wall kiosk on a relay, a phone and a laptop on
    the LAN, a keyless agent on MCP, and the sleep-timer daemon. (No admin password yet.)"""

    def setUp(self):
        super().setUp()
        self.mode("warn")
        self.prn = self.add_bambu()

    def test_agent_keyless_mcp_suggest_then_start_in_warn(self):
        h = self.mcp_session({})
        _r, msg = self.call(h, "suggest_mapping", {"printer": "A1", "filename": "job.gcode.3mf"})
        d = json.loads(T.tool_text(msg))
        _r, msg = self.call(h, "start_print", {"printer": "A1", "filename": "job.gcode.3mf", "ams_mapping": d["ams_mapping"],
                                               "confirm": d["confirm_string"], "bed_clear_confirmed_by_human": True})
        self.assertIn('"sent": true', T.tool_text(msg))
        self.assertEqual(len(self.prn.started), 1)

    def test_the_kiosk_through_its_relay(self):
        h = {"Host": "localhost:8271"}
        r = self.c.get("/dashboard", headers=h)
        self.assertEqual(r.status_code, 200)
        ck = cookie_from(r)
        self.assertTrue(ck)
        kiosk = {"Cookie": ck, "Host": "localhost:8271", "Origin": "http://localhost:8271", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.c.get("/api/fleet", headers=kiosk).status_code, 200)
        self.assertEqual(self.c.post("/api/printer/A1/action", headers=kiosk, json={"action": "pause"}).status_code, 200)
        self.assertEqual(self.c.get("/api/auth/me", headers=kiosk).status_code, 200)
        # in ON mode too (cookie = control)
        self.mode("on")
        self.assertEqual(self.c.post("/api/printer/A1/action", headers=kiosk, json={"action": "pause"}).status_code, 200)

    def test_the_sleep_timer_daemon(self):
        r = self.c.get("/api/fleet", headers={"Host": "127.0.0.1:8271", "User-Agent": "Python-urllib/3.11"})
        self.assertEqual(r.status_code, 200)

    def test_phones_and_laptops_on_the_lan(self):
        for host in ("192.168.1.203:8271", "studio-mini:8271", "studio-mini.local:8271", "[fd7a:115c::1]:8271",
                     "100.101.102.103:8271"):
            ck = cookie_from(self.c.get("/dashboard", headers={"Host": host}))
            self.assertTrue(ck, host)
            self.c.cookies.clear()
            h = {"Cookie": ck, "Host": host, "Origin": f"http://{host}"}
            self.assertEqual(self.c.post("/api/printer/A1/action", headers=h, json={"action": "pause"}).status_code, 200, host)
            self.assertEqual(self.c.get("/api/auth/me", headers=h).json()["login_required"], False, host)

    def test_the_owner_is_asked_for_an_admin_password_only_when_he_opens_security(self):
        h = self.browser()
        self.assertEqual(self.c.get("/api/auth/me", headers=h).json()["admin"], "unset")
        self.assertEqual(self.c.get("/api/security", headers=h).status_code, 403)
        # nothing else changes for him
        self.assertEqual(self.c.get("/api/settings", headers=h).status_code, 200)
        self.assertEqual(self.c.get("/api/jobs.csv", headers=h).status_code, 200)
        self.assertEqual(self.c.get("/api/printers", headers=h).status_code, 200)
        self.assertEqual(self.c.post("/api/settings/test/webhook", headers=h, json={}).status_code, 200)
        # the first-run prompt is only for a fresh install
        self.a.keys.set_meta(install="upgrade")
        self.addCleanup(lambda: self.a.keys.set_meta(install="fresh"))
        self.assertFalse(self.c.get("/api/auth/me", headers=h).json()["offer_login"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
