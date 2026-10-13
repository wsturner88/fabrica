"""Tests for API-key authentication (app/security.py + its wiring in app/server.py).

    python3 tests/test_auth.py

Needs the app's own dependencies (mcp, starlette, httpx). Everything is local: the server module is
imported with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointing at a throwaway temp directory (never the
real config.yml), the only printer is an in-process fake, and no host is contacted.
"""
import http.cookies
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

_TMP = tempfile.mkdtemp(prefix="auth-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)          # these tests are about the real default
os.environ.pop("BAMBU_MCP_ALLOWED_HOSTS", None)
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


def slurp(path):
    with open(path) as fh:
        return fh.read()


def spit(path, text):
    with open(path, "w") as fh:
        fh.write(text)


CLIENT = None
PW = "correct horse battery"
ADMIN_PW = "admin password 1"
ADMIN_HASH = security.hash_password(ADMIN_PW) if HAVE else None      # one hash for the whole file (PBKDF2 is slow on purpose)


def setUpModule():
    global CLIENT
    if HAVE:
        server.PRINTERS.clear()
        server.PRINTERS["fake"] = FakePrinter("Fake")
        CLIENT = TestClient(server.mcp.streamable_http_app())
        CLIENT.__enter__()          # MCP's session manager needs the app's lifespan running


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    shutil.rmtree(_TMP, ignore_errors=True)


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


def cookie_from(resp) -> str:
    """'printhub_session=...' from a response's Set-Cookie (empty if none)."""
    raw = resp.headers.get("set-cookie", "")
    return raw.split(";")[0] if raw.startswith(security.COOKIE_NAME + "=") else ""


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                          "clientInfo": {"name": "auth-test", "version": "1"}})


def sse_json(resp):
    """The JSON-RPC message in an MCP response (plain JSON or one SSE 'data:' line)."""
    text = resp.text
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError("no JSON-RPC message in: " + text[:300])


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class Base(unittest.TestCase):
    def setUp(self):
        a = server.AUTH
        server.SETTINGS.update({"security.api_auth": "on"}, clear=["security.dashboard_password"])
        for k in a.keys.list_public():
            a.delete_key(k["id"])
        a.stats.clear()
        a._callers.clear()
        a.last_unauthenticated = None
        a.limiter = security.LoginLimiter()
        a.dash.set_fields(admin_hash=ADMIN_HASH)         # an admin password exists: keys and security need its unlock
        a.clock = time.time
        a.env = dict(os.environ)
        self.c = CLIENT

    def mode(self, m):
        server.SETTINGS.update({"security.api_auth": m})

    def admin(self):
        """Headers of a DASHBOARD browser that has typed the admin password (the dashboard cookie /dashboard hands
        out + the admin unlock cookie): the only thing that may manage keys, sign-in and the key check. An API key -
        even a control key - may not, and neither may the bare no-password cookie (see test_fix_security2)."""
        r = self.c.get("/dashboard")
        ck = cookie_from(r)
        self.c.cookies.clear()
        self.assertTrue(ck, r.headers)
        return {"Cookie": f"{ck}; {security.ADMIN_COOKIE}={server.AUTH.make_admin_cookie()}", "Origin": "http://testserver"}

    def key(self, scope="read", name="t"):
        rec, token = server.AUTH.create_key(name, scope)
        return rec, token

    @staticmethod
    def bearer(token):
        return {"Authorization": "Bearer " + token}

    def mcp_session(self, headers):
        r = self.c.post("/mcp", headers={**MCP_HEADERS, **headers}, json=INIT)
        self.assertEqual(r.status_code, 200, r.text)
        h = {**MCP_HEADERS, **headers, "mcp-session-id": r.headers["mcp-session-id"],
             "mcp-protocol-version": "2025-03-26"}
        self.c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        return h

    def call(self, h, tool, args=None, id_=7):
        r = self.c.post("/mcp", headers=h, json=rpc("tools/call", {"name": tool, "arguments": args or {}}, id_))
        return r, (sse_json(r) if r.status_code == 200 else None)


# --------------------------------------------------------------------------- install -> mode

class InstallModeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="auth-inst-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make(self, existing, env=None):
        st = settings_store.SettingsStore(self.dir, env=env or {})
        return st, security.AuthManager(self.dir, st, existing_install=existing, env=env or {})

    def test_fresh_install_is_on(self):
        self.assertFalse(security.detect_existing_install(self.dir))
        st, a = self.make(False)
        self.assertEqual(a.mode(), "on")
        self.assertEqual(st.get_all()["sources"]["security.api_auth"], "default")
        self.assertEqual(a.keys.meta["install"], "fresh")
        self.assertEqual(a.keys.meta["initial_mode"], "on")

    def test_existing_data_is_detected(self):
        for name in ("printers.json", "settings.json", "secrets.json", "metacache.json"):
            d = tempfile.mkdtemp(dir=self.dir)
            spit(os.path.join(d, name), "{}")
            self.assertTrue(security.detect_existing_install(d), name)
        self.assertTrue(security.detect_existing_install(tempfile.mkdtemp(dir=self.dir), legacy_config=True))
        self.assertFalse(security.detect_existing_install(os.path.join(self.dir, "missing")))

    def test_upgrade_lands_in_warn_once(self):
        spit(os.path.join(self.dir, "printers.json"), '{"printers": []}')
        spit(os.path.join(self.dir, "settings.json"), '{"values": {}}')
        self.assertTrue(security.detect_existing_install(self.dir))
        st, a = self.make(True)
        self.assertEqual(a.mode(), "warn")
        self.assertEqual(st.get_all()["sources"]["security.api_auth"], "saved")
        self.assertEqual(a.keys.meta["install"], "upgrade")
        self.assertEqual(a.keys.meta["initial_mode"], "warn")
        # the owner flips it to on; a restart must NOT put it back to warn
        st.update({"security.api_auth": "on"})
        st2, a2 = self.make(True)
        self.assertEqual(a2.mode(), "on")
        # ...and once the marker exists the data dir no longer counts as "an install to upgrade"
        self.assertFalse(security.detect_existing_install(self.dir))

    def test_env_choice_beats_the_upgrade_default(self):
        spit(os.path.join(self.dir, "printers.json"), "{}")
        st, a = self.make(True, env={"BAMBU_MCP_API_AUTH": "off"})
        self.assertEqual(a.mode(), "off")

    def test_env_sets_the_fresh_default(self):
        st, a = self.make(False, env={"BAMBU_MCP_API_AUTH": "warn"})
        self.assertEqual(a.mode(), "warn")

    def test_real_server_fresh_and_upgrade(self):
        """The wiring in server.py, in a fresh process each: a pre-seeded data dir -> warn, empty -> on."""
        def boot(seed):
            d = tempfile.mkdtemp(prefix="auth-boot-")
            self.addCleanup(shutil.rmtree, d, True)
            if seed:
                os.makedirs(os.path.join(d, "data"))
                spit(os.path.join(d, "data", "printers.json"), '{"version": 1, "printers": []}')
                spit(os.path.join(d, "data", "settings.json"), '{"version": 1, "values": {}}')
            code = ("import sys, json; sys.path.insert(0, %r); import server; "
                    "from starlette.testclient import TestClient; c = TestClient(server.mcp.streamable_http_app()); "
                    "print('@@' + json.dumps([server.SETTINGS.get('security.api_auth'), c.get('/api/fleet').status_code, "
                    "c.get('/api/health').status_code])); sys.stdout.flush(); import os; os._exit(0)") % os.path.abspath(APP)
            env = {**os.environ, "BAMBU_MCP_CONFIG": os.path.join(d, "none.yml"),
                   "BAMBU_MCP_CACHE": os.path.join(d, "data", "metacache.json")}
            env.pop("BAMBU_MCP_API_AUTH", None)
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=90)
            line = [ln for ln in r.stdout.splitlines() if ln.startswith("@@")]
            self.assertTrue(line, r.stdout + r.stderr)
            return json.loads(line[-1][2:])
        self.assertEqual(boot(False), ["on", 401, 200])
        self.assertEqual(boot(True), ["warn", 200, 200])


# --------------------------------------------------------------------------- key store

class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="auth-keys-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = [1000.0]
        self.ks = security.KeyStore(self.dir, clock=lambda: self.now[0])
        self.path = os.path.join(self.dir, "api_keys.json")

    def test_key_is_shown_once_and_only_a_hash_is_stored(self):
        rec, token = self.ks.create("Claude Desktop", "control")
        self.assertTrue(token.startswith("ph_") and len(token) >= 40)
        self.assertEqual(rec["prefix"], token[:6])
        raw = slurp(self.path)
        self.assertNotIn(token, raw)
        self.assertNotIn(token[3:], raw)            # not even without the prefix
        doc = json.loads(raw)
        stored = doc["keys"][0]
        self.assertEqual(len(stored["hash"]), 64)
        self.assertNotEqual(stored["hash"], token)
        self.assertEqual(stored["hash"], security._hash_key(stored["salt"], token))
        # what a list may show
        for item in self.ks.list_public():
            self.assertEqual(set(item), {"id", "name", "scope", "prefix", "created", "last_used",
                                         "last_used_path", "revoked", "revoked_at", "created_mode"})
            self.assertNotIn(token, json.dumps(item))

    def test_file_is_0600_and_atomic(self):
        self.ks.create("a", "read")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_each_key_has_its_own_salt(self):
        self.ks.create("a", "read")
        self.ks.create("b", "read")
        s = [k["salt"] for k in json.loads(slurp(self.path))["keys"]]
        self.assertEqual(len(set(s)), 2)

    def test_verify_revoke_delete(self):
        rec, token = self.ks.create("x", "read")
        self.assertEqual(self.ks.verify(token)["id"], rec["id"])
        self.assertIsNone(self.ks.verify(token + "x"))
        self.assertIsNone(self.ks.verify("ph_" + "a" * 43))
        self.assertIsNone(self.ks.verify(""))
        self.assertIsNone(self.ks.verify("nokeyprefix"))
        self.ks.revoke(rec["id"])
        self.assertTrue(self.ks.verify(token)["revoked"])
        self.assertTrue(self.ks.list_public()[0]["revoked_at"])
        self.ks.delete(rec["id"])
        self.assertIsNone(self.ks.verify(token))
        with self.assertRaises(security.AuthError) as cm:
            self.ks.revoke("k_nope")
        self.assertEqual(cm.exception.status, 404)

    def test_validation(self):
        for name in ("", "   ", "x" * 41, "bad\nname", None):
            with self.assertRaises(security.AuthError):
                self.ks.create(name, "read")
        with self.assertRaises(security.AuthError):
            self.ks.create("ok", "admin")
        for i in range(security.MAX_KEYS):
            self.ks.create(f"k{i}", "read")
        with self.assertRaises(security.AuthError) as cm:
            self.ks.create("one too many", "read")
        self.assertEqual(cm.exception.status, 409)

    def test_last_used_is_throttled(self):
        rec, _t = self.ks.create("x", "read")
        self.now[0] += 100
        self.ks.touch(rec["id"], "/api/fleet")        # first write after >60 s since the last save
        on_disk = lambda: json.loads(slurp(self.path))["keys"][0]["last_used"]
        first = on_disk()
        self.assertTrue(first)
        self.assertEqual(json.loads(slurp(self.path))["keys"][0]["last_path"], "/api/fleet")
        self.now[0] += 5
        self.ks.touch(rec["id"], "/api/printers")      # within 60 s: memory only
        self.assertEqual(on_disk(), first)
        self.assertEqual(json.loads(slurp(self.path))["keys"][0]["last_path"], "/api/fleet")
        self.assertEqual(self.ks.list_public()[0]["last_used_path"], "/api/printers")   # list is live
        self.now[0] += 100
        self.ks.touch(rec["id"], "/api/backups")       # window passed: written
        self.assertEqual(json.loads(slurp(self.path))["keys"][0]["last_path"], "/api/backups")

    def test_flush_writes_pending_last_used(self):
        rec, _t = self.ks.create("x", "read")
        self.now[0] += 5
        self.ks.touch(rec["id"], "/api/fleet")
        self.assertIsNone(json.loads(slurp(self.path))["keys"][0]["last_path"])
        self.ks.flush()
        self.assertEqual(json.loads(slurp(self.path))["keys"][0]["last_path"], "/api/fleet")

    def test_a_key_made_by_another_process_is_picked_up(self):
        other = security.KeyStore(self.dir)
        _rec, token = other.create("from the CLI", "control")
        self.assertEqual(self.ks.verify(token)["scope"], "control")

    def test_cli_creates_a_working_key(self):
        env = {**os.environ, "BAMBU_MCP_CACHE": os.path.join(self.dir, "metacache.json")}
        r = subprocess.run([sys.executable, os.path.join(APP, "security.py"), "create-key", "CLI key",
                            "--scope", "read"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        token = [ln for ln in r.stdout.splitlines() if ln.startswith("ph_")][0]
        self.assertEqual(self.ks.verify(token)["name"], "CLI key")
        r = subprocess.run([sys.executable, os.path.join(APP, "security.py"), "list-keys"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertIn("CLI key", r.stdout)
        self.assertNotIn(token, r.stdout)

    def test_corrupt_file_is_moved_aside(self):
        spit(self.path, "{not json")
        ks = security.KeyStore(self.dir)
        self.assertEqual(ks.list_public(), [])
        self.assertTrue([f for f in os.listdir(self.dir) if ".corrupt-" in f])


class SmallPartsTests(unittest.TestCase):
    def test_extract_key(self):
        self.assertEqual(security.extract_key({"authorization": "Bearer ph_abc"}), "ph_abc")
        self.assertEqual(security.extract_key({"authorization": "bearer   ph_abc "}), "ph_abc")
        self.assertEqual(security.extract_key({"x-api-key": " ph_abc "}), "ph_abc")
        self.assertEqual(security.extract_key({"authorization": "Bearer ph_a", "x-api-key": "ph_b"}), "ph_a")
        self.assertIsNone(security.extract_key({}))
        self.assertIsNone(security.extract_key({"authorization": "Basic Zm9vOmJhcg=="}))
        self.assertEqual(security.extract_key({"authorization": "Bearer"}), "")

    def test_password_hash_roundtrip(self):
        h = security.hash_password("hunter2hunter2")
        self.assertTrue(h.startswith("pbkdf2$"))
        self.assertNotIn("hunter2", h)
        self.assertTrue(security.verify_password("hunter2hunter2", h))
        self.assertFalse(security.verify_password("hunter2hunter3", h))
        self.assertFalse(security.verify_password("x", "garbage"))
        self.assertNotEqual(h, security.hash_password("hunter2hunter2"))     # salted

    def test_login_limiter(self):
        now = [0.0]
        lim = security.LoginLimiter(per_client=3, overall=5, window=100, clock=lambda: now[0])
        for _ in range(3):
            self.assertEqual(lim.retry_after("a"), 0)
            lim.failed("a")
        self.assertGreater(lim.retry_after("a"), 0)         # back-off starts after the free tries
        self.assertEqual(lim.retry_after("b"), 0)           # another client is not delayed...
        lim.failed("b"); lim.failed("b")
        self.assertEqual(lim.retry_after("c"), 0)           # ...and a clean address is never locked out for others' failures
        now[0] += 101
        self.assertEqual(lim.retry_after("a"), 0)           # the window passed
        lim.failed("a")
        lim.succeeded("a")
        self.assertEqual(lim.retry_after("a"), 0)

    def test_same_origin_and_host_rules(self):
        so = security.same_origin
        self.assertTrue(so({"host": "hub:8271"}))                                   # no header: not a browser
        self.assertTrue(so({"host": "hub:8271", "origin": "http://hub:8271"}))
        self.assertFalse(so({"host": "hub:8271", "origin": "http://evil.example"}))
        self.assertFalse(so({"host": "hub:8271", "origin": "null"}))
        self.assertFalse(so({"host": "hub:8271", "sec-fetch-site": "cross-site"}))
        self.assertFalse(so({"host": "hub:8271", "sec-fetch-site": "same-site"}))
        self.assertTrue(so({"host": "hub:8271", "sec-fetch-site": "same-origin"}))
        self.assertTrue(so({"host": "127.0.0.1:8271", "origin": "https://hub.ts.net",
                            "x-forwarded-host": "hub.ts.net"}))

    def test_mcp_helpers(self):
        body = json.dumps(rpc("tools/call", {"name": "start_print", "arguments": {}})).encode()
        self.assertEqual(security.mcp_detail(body), "tools/call:start_print")
        self.assertEqual(len(security.refused_tools(body, "read")), 1)
        self.assertEqual(security.refused_tools(body, "control"), [])
        ok = json.dumps(rpc("tools/call", {"name": "printer_status"})).encode()
        self.assertEqual(security.refused_tools(ok, "read"), [])
        for unreadable in (b"not json", b"\xef\xbb\xbf" + body, body.decode().encode("utf-16"),
                           body.decode().encode("utf-32-le"), b"[]", b"[1]", b'{"a":1,"a":2}'):
            with self.assertRaises(ValueError):                  # fail closed: never "nothing refused"
                security.refused_tools(unreadable, "read")
        self.assertEqual(security.tool_scope("brand_new_tool"), "control")          # unknown -> control
        self.assertEqual(security.rest_scope("POST", "/api/brand/new"), "control")
        self.assertEqual(security.rest_scope("HEAD", "/api/fleet"), "read")


# --------------------------------------------------------------------------- scope tables vs the real app

class TablesMatchTheAppTests(Base):
    def test_every_route_has_an_explicit_scope_row(self):
        app = server.mcp.streamable_http_app()
        missing = []
        for r in app.routes:
            path = getattr(r, "path", "")
            if not path.startswith("/api/") or getattr(r, "methods", None) is None:
                continue
            probe = re.sub(r"\{[a-z_]+\}", "x", path)
            for m in r.methods:
                if m in ("HEAD", "OPTIONS"):
                    continue
                if path in security.PUBLIC_PAGES or (m == "POST" and path in security.PUBLIC_POSTS):
                    continue
                if not security.rest_rule_known(m, probe):
                    missing.append(f"{m} {path}")
        self.assertEqual(missing, [], "new route(s) without a row in security.REST_SCOPES (they default to "
                                      "'control', which is safe - add a row to make the decision explicit)")

    def test_every_non_api_route_is_deliberately_public(self):
        """Pages, icons and the manifest live outside /api/: each must be named as public (never silently open)."""
        app = server.mcp.streamable_http_app()
        for r in app.routes:
            path = getattr(r, "path", "")
            if path.startswith("/api/") or path.startswith(server.mcp.settings.streamable_http_path) \
                    or getattr(r, "methods", None) is None:
                continue
            probe = re.sub(r"\{[a-z_]+\}", "x", path)
            self.assertTrue(server.AUTH.is_exempt("GET", probe), f"{path} is neither under /api/ nor listed as public")

    def test_every_tool_has_a_scope_and_none_is_invented(self):
        tools = {t.name for t in server.mcp._tool_manager.list_tools()}
        self.assertEqual(sorted(tools - set(security.TOOL_SCOPES)), [], "tool(s) without a scope in security.TOOL_SCOPES")
        self.assertEqual(sorted(set(security.TOOL_SCOPES) - tools), [], "TOOL_SCOPES names a tool that does not exist")

    def test_the_read_set_is_exactly_the_looking_tools(self):
        read = {n for n, s in security.TOOL_SCOPES.items() if s == "read"}
        self.assertEqual(read, {"list_printers", "printer_status", "ams_state", "list_sd_files", "suggest_mapping",
                                "printer_controls", "get_camera_snapshot", "backup_status", "list_printer_types",
                                "get_settings", "recent_jobs", "job_stats", "cost_per_part", "detection_status",
                                "list_groups", "library_list", "library_file", "file_facts",
                                "fabrica_guide"})
        for dangerous in ("start_print", "cancel_print", "pause_print", "resume_print", "preheat", "set_tray",
                          "delete_sd_file", "add_printer", "update_printer", "remove_printer", "update_settings",
                          "backup_printer_files", "send_test_notification", "list_api_keys", "discover_printers",
                          "score_snapshot", "library_send", "library_save_from_printer"):
            self.assertEqual(security.TOOL_SCOPES[dangerous], "control", dangerous)

    def test_there_is_no_tool_that_creates_or_revokes_keys(self):
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        self.assertEqual({n for n in names if "key" in n}, {"list_api_keys"})

    def test_rest_read_rows(self):
        reads = {(m, p) for m, p, s in security.REST_SCOPES if s == "read"}
        self.assertIn(("GET", "/api/fleet"), reads)
        self.assertNotIn(("GET", "/api/backups/{printer}/{filename}"), reads)
        self.assertNotIn(("GET", "/api/security"), reads)
        for m, p in reads:
            self.assertIn(m, ("GET", "POST"))
            if m == "POST":
                self.assertTrue(p.endswith("/suggest"))


# --------------------------------------------------------------------------- REST behaviour

class RestModeTests(Base):
    def test_on_without_a_key_is_401(self):
        r = self.c.get("/api/fleet")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"error": "API key required"})
        self.assertIn("Bearer", r.headers["www-authenticate"])
        for method, path in (("get", "/api/printers"), ("get", "/api/settings"), ("post", "/api/printer/x/start"),
                             ("put", "/api/settings"), ("get", "/api/security"), ("post", "/api/keys"),
                             ("get", "/api/backups"), ("get", "/api/thumb/" + "a" * 40 + ".png"),
                             ("get", "/api/nonexistent")):
            self.assertEqual(getattr(self.c, method)(path).status_code, 401, path)

    def test_bearer_and_x_api_key_both_work(self):
        _r, token = self.key("read")
        self.assertEqual(self.c.get("/api/fleet", headers=self.bearer(token)).status_code, 200)
        self.assertEqual(self.c.get("/api/fleet", headers={"X-Api-Key": token}).status_code, 200)

    def test_a_key_in_the_query_string_is_ignored(self):
        _r, token = self.key("read")
        for q in ("api_key", "key", "token", "access_token"):
            self.assertEqual(self.c.get(f"/api/fleet?{q}={token}").status_code, 401, q)

    def test_bad_revoked_and_deleted_keys(self):
        rec, token = self.key("control")
        r = self.c.get("/api/fleet", headers=self.bearer("ph_" + "z" * 43))
        self.assertEqual((r.status_code, r.json()["error"]), (401, "API key required"))
        self.assertEqual(self.c.get("/api/fleet", headers=self.bearer("")).status_code, 401)
        server.AUTH.revoke_key(rec["id"])
        r = self.c.get("/api/fleet", headers=self.bearer(token))
        self.assertEqual(r.status_code, 401)
        self.assertIn("revoked", r.json()["hint"])
        server.AUTH.delete_key(rec["id"])
        self.assertEqual(self.c.get("/api/fleet", headers=self.bearer(token)).status_code, 401)

    def test_read_key_can_look_but_not_touch(self):
        _r, token = self.key("read")
        h = self.bearer(token)
        for path in ("/api/fleet", "/api/printers", "/api/printer-types", "/api/settings", "/api/settings/schema",
                     "/api/settings/export", "/api/notifications/recent", "/api/backups", "/api/printer/fake"):
            self.assertEqual(self.c.get(path, headers=h).status_code, 200, path)
        r = self.c.post("/api/printer/fake/suggest", headers=h, json={"filename": "x.3mf"})
        self.assertNotIn(r.status_code, (401, 403))                 # allowed through (the printer is fake)
        for method, path in (("post", "/api/printer/fake/start"), ("post", "/api/printer/fake/action"),
                             ("post", "/api/printer/fake/control"), ("post", "/api/printer/fake/tray"),
                             ("post", "/api/printer/fake/delete"), ("post", "/api/printer/fake/backup"),
                             ("get", "/api/backups/fake/x.zip"), ("post", "/api/printers"),
                             ("post", "/api/printers/test"), ("put", "/api/printers/fake"),
                             ("delete", "/api/printers/fake"), ("put", "/api/settings"),
                             ("post", "/api/settings/import"), ("post", "/api/settings/test/ntfy"),
                             ("get", "/api/security"), ("put", "/api/security"), ("get", "/api/keys"),
                             ("post", "/api/keys"), ("delete", "/api/keys/k_x"), ("post", "/api/keys/k_x/revoke")):
            r = getattr(self.c, method)(path, headers=h)
            self.assertEqual(r.status_code, 403, f"{method.upper()} {path}")
            self.assertIn(r.json()["required_scope"], ("control", "admin"))
        self.assertEqual(server.AUTH.status()["stats"]["refused_wrong_scope"], 20)

    def test_control_key_can_do_everything_except_administer(self):
        _r, token = self.key("control")
        h = self.bearer(token)
        for path in ("/api/security", "/api/keys", "/api/devices"):       # admin: a dashboard browser only
            r = self.c.get(path, headers=h)
            self.assertEqual((r.status_code, r.json()["required_scope"]), (403, "admin"), path)
        self.assertEqual(self.c.post("/api/printer/fake/start", headers=h, json={}).status_code, 400)  # reached the handler
        self.assertEqual(self.c.get("/api/backups/fake/x.zip", headers=h).status_code, 404)

    def test_exempt_routes_need_no_key(self):
        self.assertEqual(self.c.get("/dashboard").status_code, 200)
        self.assertEqual(self.c.get("/").status_code, 200)
        self.assertEqual(self.c.get("/api/ui-version").status_code, 200)
        r = self.c.get("/api/health")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertEqual(self.c.post("/api/auth/login", json={"password": "x"}).status_code, 400)  # reached the handler
        self.assertEqual(self.c.post("/api/auth/logout").status_code, 200)

    def test_exemptions_are_exactly_these(self):
        a = server.AUTH
        for m, p in (("GET", "/"), ("GET", "/dashboard"), ("GET", "/api/ui-version"), ("GET", "/api/health"),
                     ("GET", "/favicon.ico"), ("GET", "/favicon.svg"), ("GET", "/apple-touch-icon.png"),
                     ("GET", "/manifest.webmanifest"), ("GET", "/static/brand/mark.svg"), ("HEAD", "/static/brand/mark.svg"),
                     ("HEAD", "/dashboard"), ("POST", "/api/auth/login"), ("POST", "/api/auth/logout")):
            self.assertTrue(a.is_exempt(m, p), (m, p))
        for m, p in (("POST", "/dashboard"), ("GET", "/api/auth/login"), ("GET", "/api/fleet"),
                     ("GET", "/mcp"), ("POST", "/mcp"), ("GET", "/api/security"), ("GET", "/api/thumb/x"),
                     ("DELETE", "/api/health"), ("GET", "/dashboard/x"), ("POST", "/static/brand/mark.svg"),
                     ("GET", "/static/other.svg"), ("GET", "/static/brand"), ("GET", "/manifest.webmanifest/x")):
            self.assertFalse(a.is_exempt(m, p), (m, p))

    def test_favicon_is_public_and_never_logged(self):
        self.mode("warn")
        r = self.c.get("/favicon.ico")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/x-icon"))
        self.assertEqual(r.content[:4], b"\x00\x00\x01\x00")        # an ICO header, not an empty answer
        self.mode("on")
        self.assertEqual(self.c.get("/favicon.ico").status_code, 200)
        self.assertEqual(self.c.get("/static/brand/mark.svg").status_code, 200)
        self.assertEqual(self.c.get("/manifest.webmanifest").status_code, 200)
        self.assertEqual(server.AUTH.status()["warn"]["recent_callers"], [])

    def test_health_leaks_nothing(self):
        self.assertEqual(self.c.get("/api/health").json(), {"ok": True})

    def test_off_means_no_checks_and_no_counting(self):
        self.mode("off")
        self.assertEqual(self.c.get("/api/fleet").status_code, 200)
        self.assertEqual(self.c.get("/api/security", headers=self.admin()).status_code, 200)
        self.assertEqual(self.c.get("/api/fleet", headers=self.bearer("junk")).status_code, 200)
        st = server.AUTH.status()
        self.assertEqual(st["stats"]["without_key_but_allowed"], 0)
        self.assertEqual(st["warn"]["recent_callers"], [])

    def test_warn_allows_and_logs(self):
        self.mode("warn")
        r = self.c.get("/api/fleet", headers={"User-Agent": "pytest-agent/1.0", "X-Forwarded-For": "10.1.2.3"})
        self.assertEqual(r.status_code, 200)
        for _ in range(4):
            self.c.get("/api/fleet", headers={"User-Agent": "pytest-agent/1.0"})
        self.c.post("/api/printer/fake/start", json={})
        st = server.AUTH.status()
        self.assertEqual(st["stats"]["without_key_but_allowed"], 6)
        last = st["warn"]["last_unauthenticated"]
        self.assertEqual((last["method"], last["path"], last["reason"]), ("POST", "/api/printer/fake/start", "missing"))
        self.assertTrue(last["time"] and last["remote_addr"] == "testclient")
        rows = {(c["method"], c["path"]): c for c in st["warn"]["recent_callers"]}
        fleet = rows[("GET", "/api/fleet")]
        self.assertEqual(fleet["count"], 5)                          # polling is one row with a count
        self.assertEqual(fleet["user_agent"], "pytest-agent/1.0")
        self.assertEqual(fleet["first_seen"][:10], fleet["last_seen"][:10])
        self.assertIn(("POST", "/api/printer/fake/start"), rows)

    def test_warn_logs_an_invalid_key_but_still_allows(self):
        self.mode("warn")
        r = self.c.get("/api/fleet", headers=self.bearer("ph_" + "q" * 43))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(server.AUTH.status()["warn"]["last_unauthenticated"]["reason"], "invalid_key")

    def test_warn_does_not_log_good_keys_and_still_enforces_scope(self):
        self.mode("warn")
        _r, token = self.key("read")
        self.assertEqual(self.c.get("/api/fleet", headers=self.bearer(token)).status_code, 200)
        self.assertEqual(server.AUTH.status()["stats"]["without_key_but_allowed"], 0)
        self.assertEqual(server.AUTH.status()["stats"]["authenticated_with_key"], 1)
        self.assertEqual(self.c.post("/api/printer/fake/start", headers=self.bearer(token), json={}).status_code, 403)

    def test_on_counts_rejections(self):
        self.c.get("/api/fleet")
        self.assertEqual(server.AUTH.status()["stats"]["rejected"], 1)
        self.assertFalse(server.AUTH.status()["warn"]["recent_callers"][0]["allowed"])

    def test_last_used_is_recorded(self):
        rec, token = self.key("read")
        self.c.get("/api/printers", headers=self.bearer(token))
        k = [x for x in server.AUTH.keys.list_public() if x["id"] == rec["id"]][0]
        self.assertEqual(k["last_used_path"], "/api/printers")
        self.assertTrue(k["last_used"])

    def test_security_status_document(self):
        _r, token = self.key("control")
        a = self.admin()
        d = self.c.get("/api/security", headers=a).json()
        self.assertEqual(d["mode"], "on")
        self.assertEqual(d["dashboard_password"], "not set")
        self.assertEqual(d["install"], "fresh")
        self.assertEqual(d["key_counts"], {"active": 1, "revoked": 0, "read": 0, "control": 1})
        self.assertNotIn(token, json.dumps(d))
        for k in ("keys", "warn", "stats", "modes"):
            self.assertIn(k, d)
        self.assertEqual(self.c.get("/api/security", headers=a).headers["cache-control"], "no-store")


class KeyRoutesTests(Base):
    def test_create_list_revoke_delete(self):
        h = self.admin()
        r = self.c.post("/api/keys", headers=h, json={"name": "Claude Desktop", "scope": "read"})
        self.assertEqual(r.status_code, 201)
        made = r.json()
        self.assertTrue(made["key"].startswith("ph_"))
        self.assertEqual((made["name"], made["scope"], made["prefix"]), ("Claude Desktop", "read", made["key"][:6]))
        self.assertEqual(r.headers["cache-control"], "no-store")
        # the new key works...
        self.assertEqual(self.c.get("/api/fleet", headers=self.bearer(made["key"])).status_code, 200)
        # ...and is never shown again, anywhere
        listing = self.c.get("/api/keys", headers=h)
        self.assertNotIn(made["key"], listing.text)
        self.assertNotIn(made["key"], self.c.get("/api/security", headers=h).text)
        self.assertNotIn(made["key"], slurp(os.path.join(DATA, "api_keys.json")))
        # revoke
        r = self.c.post(f"/api/keys/{made['id']}/revoke", headers=h)
        self.assertEqual((r.status_code, r.json()["revoked"]), (200, True))
        self.assertEqual(self.c.get("/api/fleet", headers=self.bearer(made["key"])).status_code, 401)
        self.assertEqual(self.c.post(f"/api/keys/{made['id']}/revoke", headers=h).status_code, 200)   # idempotent
        # delete
        self.assertEqual(self.c.delete(f"/api/keys/{made['id']}", headers=h).status_code, 200)
        self.assertEqual(self.c.delete(f"/api/keys/{made['id']}", headers=h).status_code, 404)
        self.assertEqual(self.c.post("/api/keys/k_nope/revoke", headers=h).status_code, 404)

    def test_default_scope_is_read_and_bad_input_is_400(self):
        h = self.admin()
        self.assertEqual(self.c.post("/api/keys", headers=h, json={"name": "n"}).json()["scope"], "read")
        self.assertEqual(self.c.post("/api/keys", headers=h, json={"name": "n", "scope": "root"}).status_code, 400)
        self.assertEqual(self.c.post("/api/keys", headers=h, json={"scope": "read"}).status_code, 400)
        self.assertEqual(self.c.post("/api/keys", headers=h, content=b"junk").status_code, 415)     # not JSON: refused up front
        self.assertEqual(self.c.post("/api/keys", headers={**h, "Content-Type": "application/json"},
                                     content=b"junk").status_code, 400)

    def test_key_count_shows_in_settings(self):
        self.key("read"); self.key("control")
        v = server.SETTINGS.get("security.active_keys")
        self.assertEqual(v, 2)

    def test_mode_can_be_changed_and_is_validated(self):
        h = self.admin()
        r = self.c.put("/api/security", headers=h, json={"mode": "warn"})
        self.assertEqual((r.status_code, r.json()["mode"]), (200, "warn"))
        self.assertEqual(self.c.put("/api/security", headers=h, json={"mode": "banana"}).status_code, 400)
        self.assertEqual(server.SETTINGS.get("security.api_auth"), "warn")


# --------------------------------------------------------------------------- dashboard cookie

class CookieTests(Base):
    def cookie(self):
        r = self.c.get("/dashboard")
        ck = cookie_from(r)
        self.assertTrue(ck, r.headers)
        return r, ck

    def test_dashboard_sets_a_hardened_cookie(self):
        r, ck = self.cookie()
        raw = r.headers["set-cookie"].lower()
        self.assertIn("httponly", raw)
        self.assertIn("samesite=strict", raw)
        self.assertIn("path=/", raw)
        self.assertNotIn("secure", raw)                       # plain http here
        self.assertIn("max-age=", raw)

    def test_cookie_is_secure_behind_https(self):
        r = self.c.get("/dashboard", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("secure", r.headers["set-cookie"].lower())

    def test_cookie_grants_dashboard_js_same_origin(self):
        _r, ck = self.cookie()
        h = {"Cookie": ck, "Origin": "http://testserver"}
        self.assertEqual(self.c.get("/api/fleet", headers=h).status_code, 200)
        self.assertEqual(self.c.get("/api/printer/fake", headers=h).status_code, 200)
        r = self.c.get("/api/security", headers=h)       # control scope is not admin any more: the admin password is needed
        self.assertEqual((r.status_code, r.json()["admin"]), (403, "locked"))
        r = self.c.put("/api/settings", headers={**h, "Sec-Fetch-Site": "same-origin"},
                       json={"values": {"general.hub_name": "Cookie Hub"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.post("/api/printer/fake/start", headers=h, json={}).status_code, 400)
        server.SETTINGS.reset(["general.hub_name"])

    def test_cookie_without_origin_headers_still_works_for_writes(self):
        _r, ck = self.cookie()                                # curl-style: no Origin, no Sec-Fetch-Site
        self.assertEqual(self.c.post("/api/printer/fake/start", headers={"Cookie": ck}, json={}).status_code, 400)

    def test_cross_site_writes_are_refused(self):
        _r, ck = self.cookie()
        for extra in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                      {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                      {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"}):
            r = self.c.post("/api/printer/fake/action", headers={"Cookie": ck, **extra}, json={"action": "pause"})
            self.assertEqual(r.status_code, 403, extra)
            self.assertEqual(r.json(), {"error": "cross-site request refused"})
        for method in ("put", "delete"):
            r = getattr(self.c, method)("/api/printers/fake", headers={"Cookie": ck, "Origin": "http://evil.example"})
            self.assertEqual(r.status_code, 403)
        self.assertEqual(server.PRINTERS.get("fake") is not None, True)

    def test_cross_site_reads_with_cookie_are_not_blocked_by_csrf_rule_but_samesite_covers_them(self):
        _r, ck = self.cookie()
        self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": ck, "Origin": "http://evil.example"}).status_code, 200)

    def test_forged_tampered_and_expired_cookies_do_not_work(self):
        _r, ck = self.cookie()
        value = ck.split("=", 1)[1]
        for bad in ("printhub_session=v1.9999999999.deadbeef", "printhub_session=" + value[:-3] + "abc",
                    "printhub_session=", "printhub_session=garbage", "other=" + value):
            self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": bad}).status_code, 401, bad)
        server.AUTH.clock = lambda: time.time() + security.COOKIE_SECONDS + 10
        self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": ck}).status_code, 401)

    def test_a_cookie_is_not_an_mcp_credential(self):
        _r, ck = self.cookie()
        r = self.c.post("/mcp", headers={**MCP_HEADERS, "Cookie": ck}, json=INIT)
        self.assertEqual(r.status_code, 401)

    def test_a_key_beats_the_cookie(self):
        _r, ck = self.cookie()
        r = self.c.get("/api/fleet", headers={"Cookie": ck, **self.bearer("ph_" + "q" * 43)})
        self.assertEqual(r.status_code, 401)              # presenting a bad key is not rescued by a cookie

    def test_rotating_the_session_secret_signs_everyone_out(self):
        _r, ck = self.cookie()
        self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": ck}).status_code, 200)
        server.AUTH.rotate_sessions()
        self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": ck}).status_code, 401)

    def test_dns_rebinding_host_gets_no_cookie_and_cannot_use_one(self):
        r = self.c.get("/dashboard", headers={"Host": "attacker.example.com"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cookie_from(r), "")
        _r, ck = self.cookie()
        r = self.c.get("/api/fleet", headers={"Cookie": ck, "Host": "attacker.example.com"})
        self.assertEqual(r.status_code, 401)
        # names a home network really uses are fine
        for host in ("192.168.1.50:8271", "printhub.local:8271", "printhub:8271", "[::1]:8271", "hub.tail1234.ts.net",
                     "localhost:8271"):
            self.assertTrue(cookie_from(self.c.get("/dashboard", headers={"Host": host})), host)
        # a custom name works once the owner lists it
        server.AUTH.env = {"BAMBU_MCP_ALLOWED_HOSTS": "hub.example.com, other.example.org"}
        self.assertTrue(cookie_from(self.c.get("/dashboard", headers={"Host": "hub.example.com"})))

    def test_the_dashboard_html_is_unchanged_by_all_this(self):
        r = self.c.get("/dashboard")
        with open(os.path.join(APP, "dashboard.html")) as fh:
            self.assertEqual(r.text, fh.read())
        self.assertEqual(r.headers["cache-control"], "no-store")


# --------------------------------------------------------------------------- dashboard password

class PasswordTests(Base):
    def set_password(self, pw=PW):
        r = self.c.put("/api/security", headers=self.admin(), json={"dashboard_password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        self.c.cookies.clear()                  # the owner's browser is handed a fresh session; these tests are not it
        return r

    def test_password_is_stored_as_a_hash_only(self):
        self.set_password()
        text = slurp(os.path.join(DATA, "secrets.json"))
        self.assertNotIn(PW, text)
        self.assertIn("pbkdf2$", text)
        self.assertEqual(server.AUTH.status()["dashboard_password"], "set")
        self.assertNotIn(PW, json.dumps(server.SETTINGS.describe()))
        self.assertNotIn("pbkdf2", json.dumps(server.SETTINGS.describe()))      # not even the hash is shown
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(DATA, "secrets.json")).st_mode), 0o600)

    def test_short_password_is_rejected(self):
        r = self.c.put("/api/security", headers=self.admin(), json={"dashboard_password": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(server.AUTH.status()["dashboard_password"], "not set")

    def test_login_flow(self):
        self.set_password()
        page = self.c.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Sign in", page.text)
        self.assertEqual(cookie_from(page), "")                       # no cookie before login
        self.assertEqual(self.c.get("/api/fleet").status_code, 401)
        bad = self.c.post("/api/auth/login", json={"password": "wrong wrong wrong"})
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(cookie_from(bad), "")
        good = self.c.post("/api/auth/login", json={"password": PW})
        self.assertEqual(good.status_code, 200)
        ck = cookie_from(good)
        self.assertTrue(ck)
        self.assertIn("httponly", good.headers["set-cookie"].lower())
        dash = self.c.get("/dashboard", headers={"Cookie": ck})
        with open(os.path.join(APP, "dashboard.html")) as fh:
            self.assertEqual(dash.text, fh.read())                     # the real dashboard now
        self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": ck}).status_code, 200)

    def test_login_is_rate_limited_even_for_the_right_password(self):
        self.set_password()
        for _ in range(3):                                    # three free tries, then a growing wait
            self.assertEqual(self.c.post("/api/auth/login", json={"password": "nope nope nope"}).status_code, 401)
        r = self.c.post("/api/auth/login", json={"password": PW})
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers["retry-after"]), 0)
        self.assertIn("Try again", r.json()["error"])
        server.AUTH.limiter = security.LoginLimiter()
        self.assertEqual(self.c.post("/api/auth/login", json={"password": PW}).status_code, 200)

    def test_login_refuses_cross_site_and_unknown_hosts(self):
        self.set_password()
        r = self.c.post("/api/auth/login", json={"password": PW}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/auth/login", json={"password": PW}, headers={"Host": "attacker.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_an_api_key_is_not_a_dashboard_password(self):
        self.set_password()
        _r, token = self.key("control")
        r = self.c.post("/api/auth/login", json={"password": token})
        self.assertEqual(r.status_code, 401)

    def test_old_cookies_stop_working_when_the_password_is_set_or_cleared(self):
        r = self.c.get("/dashboard")
        old = cookie_from(r)
        self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": old}).status_code, 200)
        self.set_password()
        self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": old}).status_code, 401)
        self.assertIn("Sign in", self.c.get("/dashboard", headers={"Cookie": old}).text)
        ck = cookie_from(self.c.post("/api/auth/login", json={"password": PW}))
        r = self.c.put("/api/security", headers={"Cookie": f"{ck}; {security.ADMIN_COOKIE}={server.AUTH.make_admin_cookie()}",
                                                 "Origin": "http://testserver"},
                       json={"clear_dashboard_password": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(server.AUTH.status()["dashboard_password"], "not set")
        self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": ck}).status_code, 401)   # signed out again
        self.assertTrue(cookie_from(self.c.get("/dashboard")))                                # open dashboard again

    def test_the_browser_that_sets_the_password_stays_signed_in(self):
        old = cookie_from(self.c.get("/dashboard"))
        adm = f"{security.ADMIN_COOKIE}={server.AUTH.make_admin_cookie()}"
        r = self.c.put("/api/security", headers={"Cookie": f"{old}; {adm}", "Origin": "http://testserver"},
                       json={"dashboard_password": PW})
        self.assertEqual(r.status_code, 200)
        fresh = cookie_from(r)
        self.assertTrue(fresh and fresh != old)
        self.assertEqual(self.c.get("/api/fleet", headers={"Cookie": fresh}).status_code, 200)
        # the admin who made the change keeps the unlock (a fresh cookie is handed back)
        self.assertIn(security.ADMIN_COOKIE + "=", " ".join(r.headers.get_list("set-cookie")))
        # the generic settings page does the same
        adm = f"{security.ADMIN_COOKIE}={server.AUTH.make_admin_cookie()}"
        r = self.c.put("/api/settings", headers={"Cookie": f"{fresh}; {adm}"},
                       json={"values": {}, "clear": ["security.dashboard_password"]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(cookie_from(r))

    def test_logout_clears_the_cookie(self):
        r = self.c.post("/api/auth/logout")
        self.assertIn("printhub_session=", r.headers["set-cookie"])
        self.assertIn("max-age=0", r.headers["set-cookie"].lower())

    def test_password_in_off_mode_still_gates_the_page(self):
        self.set_password()
        self.mode("off")
        self.assertIn("Sign in", self.c.get("/dashboard").text)
        # sign-in is only worth something while the key check is on: with it stored as "off" the REST API still
        # behaves as ON for a request that carries no credential (second security review, N-2)
        self.assertEqual(self.c.get("/api/fleet").status_code, 401)


# --------------------------------------------------------------------------- settings integration

class SettingsIntegrationTests(Base):
    def test_security_section_is_in_the_settings_schema(self):
        _r, token = self.key("read")
        d = self.c.get("/api/settings", headers=self.bearer(token)).json()
        sec = [s for s in d["sections"] if s["id"] == "security"][0]
        fields = {f["name"]: f for f in sec["fields"]}
        self.assertEqual(set(fields), {"api_auth", "dashboard_password", "active_keys"})
        self.assertEqual([o["value"] for o in fields["api_auth"]["options"]], ["on", "warn", "off"])
        self.assertTrue(fields["api_auth"]["help"] and fields["dashboard_password"]["secret"])
        self.assertEqual(fields["dashboard_password"]["value"], "not set")
        self.assertEqual(fields["active_keys"]["type"], "info")

    def test_mcp_cannot_change_security_settings(self):
        _r, token = self.key("control")
        h = self.mcp_session(self.bearer(token))
        for args in ({"values": {"security.api_auth": "off"}}, {"values": {"security": {"api_auth": "off"}}},
                     {"values": {}, "clear": ["security.dashboard_password"]},
                     {"values": {"security.dashboard_password": "an agent chose this"}}):
            _r, msg = self.call(h, "update_settings", args)
            text = msg["result"]["content"][0]["text"]
            self.assertIn("REFUSED", text)
            self.assertIn("owner-only", text)
        self.assertEqual(server.SETTINGS.get("security.api_auth"), "on")
        _r, msg = self.call(h, "update_settings", {"values": {"general.hub_name": "From Agent"}})
        self.assertNotIn("REFUSED", msg["result"]["content"][0]["text"])
        server.SETTINGS.reset(["general.hub_name"])

    def test_export_and_import_never_carry_security(self):
        self.mode("warn")
        _r, token = self.key("control")
        h = self.bearer(token)
        exp = self.c.get("/api/settings/export", headers=h).json()
        self.assertFalse([k for k in exp["values"] if k.startswith("security.")])
        res = self.c.post("/api/settings/import", headers=h, json={"values": {"security.api_auth": "off"}}).json()
        self.assertIn("security.api_auth", res["skipped"])
        self.assertEqual(server.SETTINGS.get("security.api_auth"), "warn")


# --------------------------------------------------------------------------- MCP

class McpTests(Base):
    def test_no_key_is_a_real_401_with_www_authenticate(self):
        r = self.c.post("/mcp", headers=MCP_HEADERS, json=INIT)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"error": "API key required"})
        self.assertTrue(r.headers["www-authenticate"].startswith("Bearer"))
        self.assertEqual(self.c.get("/mcp", headers={"Accept": "text/event-stream"}).status_code, 401)
        self.assertEqual(self.c.post("/mcp", headers={**MCP_HEADERS, **self.bearer("ph_" + "x" * 43)}, json=INIT).status_code, 401)

    def test_read_key_initializes_and_reads(self):
        _r, token = self.key("read")
        h = self.mcp_session(self.bearer(token))
        listing = self.c.post("/mcp", headers=h, json=rpc("tools/list", id_=2))
        self.assertEqual(listing.status_code, 200)
        self.assertGreaterEqual(len(sse_json(listing)["result"]["tools"]), 30)
        r, msg = self.call(h, "printer_status", {"printer": "fake"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(msg["result"].get("isError"))
        self.assertIn('"state": "IDLE"', msg["result"]["content"][0]["text"])        # the body was replayed intact
        r, msg = self.call(h, "list_printers")
        self.assertIn("Fake: IDLE", msg["result"]["content"][0]["text"])

    def test_read_key_cannot_call_control_tools(self):
        _r, token = self.key("read")
        h = self.mcp_session(self.bearer(token))
        for tool, args in (("start_print", {"printer": "fake", "filename": "x.3mf", "ams_mapping": [], "confirm": "x"}),
                           ("cancel_print", {"printer": "fake"}), ("pause_print", {"printer": "fake"}),
                           ("preheat", {"printer": "fake"}), ("remove_printer", {"name": "fake"}),
                           ("update_settings", {"values": {}}), ("list_api_keys", {}),
                           ("delete_sd_file", {"printer": "fake", "filename": "a", "confirm": "a"}),
                           ("not_a_real_tool", {})):
            r, msg = self.call(h, tool, args, id_=11)
            self.assertEqual(r.status_code, 200, tool)
            self.assertEqual(msg["id"], 11)
            self.assertTrue(msg["result"]["isError"], tool)
            text = msg["result"]["content"][0]["text"]
            self.assertIn("REFUSED", text)
            self.assertIn("'read' access", text)
            self.assertIn(f"'{tool}' needs a 'control' key", text)
        self.assertIn("fake", server.PRINTERS)             # nothing was removed
        self.assertEqual(server.AUTH.status()["stats"]["refused_wrong_scope"], 9)

    def test_control_key_can_call_control_tools(self):
        _r, token = self.key("control")
        h = self.mcp_session(self.bearer(token))
        r, msg = self.call(h, "pause_print", {"printer": "nonexistent"})
        text = msg["result"]["content"][0]["text"]
        self.assertNotIn("this API key", text)             # got past auth: failed on the unknown printer instead
        self.assertIn("Unknown printer", text)
        r, msg = self.call(h, "list_api_keys")
        listing = json.loads(msg["result"]["content"][0]["text"])
        self.assertEqual(listing["mode"], "on")
        self.assertNotIn(token, msg["result"]["content"][0]["text"])
        self.assertEqual(listing["keys"][0]["scope"], "control")

    def test_x_api_key_header_works_for_mcp(self):
        _r, token = self.key("read")
        h = self.mcp_session({"X-Api-Key": token})
        _r, msg = self.call(h, "list_printers")
        self.assertIn("Fake", msg["result"]["content"][0]["text"])

    def test_revoked_key_loses_mcp_access_mid_session(self):
        rec, token = self.key("read")
        h = self.mcp_session(self.bearer(token))
        server.AUTH.revoke_key(rec["id"])
        r = self.c.post("/mcp", headers=h, json=rpc("tools/list", id_=3))
        self.assertEqual(r.status_code, 401)

    def test_a_batch_containing_a_forbidden_call_is_refused(self):
        _r, token = self.key("read")
        h = self.mcp_session(self.bearer(token))
        batch = [rpc("tools/call", {"name": "list_printers"}, 1), rpc("tools/call", {"name": "start_print"}, 2)]
        r = self.c.post("/mcp", headers=h, json=batch)
        self.assertEqual(r.status_code, 403)
        self.assertIn("control", r.json()["error"]["message"])

    def test_oversized_body_with_a_read_key_is_refused_not_skipped(self):
        _r, token = self.key("read")
        h = self.mcp_session(self.bearer(token))
        big = rpc("tools/call", {"name": "list_printers", "arguments": {"pad": "x" * (security.BODY_SNIFF_LIMIT + 10)}})
        self.assertEqual(self.c.post("/mcp", headers=h, json=big).status_code, 413)

    def test_mcp_in_warn_mode_works_without_a_key_and_logs_what_was_called(self):
        self.mode("warn")
        r = self.c.post("/mcp", headers=MCP_HEADERS, json=INIT)
        self.assertEqual(r.status_code, 200)
        h = {**MCP_HEADERS, "mcp-session-id": r.headers["mcp-session-id"], "mcp-protocol-version": "2025-03-26"}
        self.c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        _r, msg = self.call(h, "list_printers")
        self.assertIn("Fake", msg["result"]["content"][0]["text"])
        details = {c["detail"] for c in server.AUTH.status()["warn"]["recent_callers"] if c["path"] == "/mcp"}
        self.assertIn("initialize", details)
        self.assertIn("tools/call:list_printers", details)

    def test_mcp_off_mode_is_open(self):
        self.mode("off")
        self.assertEqual(self.c.post("/mcp", headers=MCP_HEADERS, json=INIT).status_code, 200)


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class ListKeysToolTests(Base):
    def test_never_exposes_a_key(self):
        rec, token = self.key("read", "agent one")
        out = server.list_api_keys()
        self.assertNotIn(token, out)
        self.assertNotIn(token[3:], out)
        d = json.loads(out)
        self.assertEqual([k["name"] for k in d["keys"]], ["agent one"])
        for forbidden in ("hash", "salt", "key"):
            self.assertNotIn(forbidden, d["keys"][0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
