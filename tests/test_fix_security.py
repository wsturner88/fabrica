"""Regression tests for the 2026-10-11 hardening review: security and start-print findings.

    python3 tests/test_fix_security.py

Covers (review ids in brackets):
  * a read-only key cannot reach control tools by re-encoding the MCP body (BOM, UTF-16/32, batches,
    duplicate keys): fail closed, plus the per-tool re-check                                   [P0-1]
  * the browser boundary in off / warn / on alike: cross-site, DNS rebinding, non-JSON bodies;
    keys / sign-in / key-check changes need the dashboard, never an API key                     [P0-2, P1-3]
  * the start confirm is a single-use, expiring, bound ticket; a bed-clear statement; a per-printer
    start lock; nothing about the expected value is ever echoed                                 [P0-3, P1-5]
  * "Test" buttons and saves never send a saved secret to a different address                    [P0-8]
  * sync MCP tools no longer block the event loop                                                [P1-1]
  * read-only callers get no webhook URL / ntfy topic / SMTP user / user:pass@ URLs               [P1-2]
  * no FTPS downloads from a busy printer or for a read-only caller; FTPS under the lock         [P1-4]

Everything is loopback or in-process: the server module is imported with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE
pointing at a throwaway temp directory (never the real config.yml); no real printer or host is contacted.
"""
import asyncio
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

_TMP = tempfile.mkdtemp(prefix="fixsec-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)
os.environ.pop("BAMBU_MCP_ALLOWED_HOSTS", None)
os.environ["BAMBU_MCP_ALLOW_LOOPBACK_DETECTOR"] = "1"       # the detector "Test" tests talk to fakes on 127.0.0.1

import notify  # noqa: E402
import security  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover
    HAVE = False
    _ERR = e

REAL_SLEEP = time.sleep
CLIENT = None
ADMIN_HASH = security.hash_password("admin password 1")        # one hash for the file (PBKDF2 is slow on purpose)


def setUpModule():
    global CLIENT
    if HAVE:
        CLIENT = TestClient(server.mcp.streamable_http_app())
        CLIENT.__enter__()


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    shutil.rmtree(_TMP, ignore_errors=True)


# ---------------------------------------------------------------- fakes

def make_3mf(filaments=1) -> bytes:
    fil = "".join(f'<filament id="{i}" tray_info_idx="GFA00" type="PLA" color="#FF0000" used_m="1.0" used_g="3.0"/>'
                  for i in range(1, filaments + 1))
    info = ('<?xml version="1.0" encoding="UTF-8"?><config><plate><metadata key="index" value="1"/>'
            '<metadata key="printer_model_id" value="N2S"/><metadata key="prediction" value="3600"/>'
            '<metadata key="weight" value="12.5"/>' + fil + '</plate></config>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", info)
        z.writestr("Metadata/plate_1.gcode", "G28\n")
    return buf.getvalue()


class FakeBambu:
    kind = "bambu"
    has_camera = False
    uses_files_api = False

    def __init__(self, name="A1", state="IDLE"):
        self.name, self.model, self.serial = name, "Bambu Lab A1", "03900000000000A"
        self.state = state
        self.files = {}
        self.calls = []
        self.started = []
        self.trays = [{"tray": 0, "slot_1based": 1, "type": "PLA", "color": "#FF0000"},
                      {"tray": 1, "slot_1based": 2, "type": "PLA", "color": "#0000FF"}]
        self.start_gate = None            # threading.Event the start waits for
        self.lock_held_in_list = []

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": self.state, "job": "", "percent": 0,
                "remaining_min": None}

    def refresh(self, wait=0):
        pass

    def ams_trays(self):
        return [dict(t) for t in self.trays]

    def sd_list(self):
        self.lock_held_in_list.append(server._ftps_lock(self.name).locked())
        self.calls.append("LIST")
        return [{"name": n, "size": len(b), "modified": ""} for n, b in self.files.items()]

    def sd_download(self, filename):
        self.calls.append("RETR " + filename)
        return self.files[filename]

    def sd_delete(self, filename):
        self.calls.append("DELE " + filename)

    def start_print(self, filename, ams_mapping):
        if self.start_gate is not None:
            self.start_gate.wait(5)
        self.started.append((filename, list(ams_mapping)))

    def stop(self):
        self.calls.append("stop")

    def pause(self):
        self.calls.append("pause")


def downloads(prn):
    return [c for c in prn.calls if c.startswith("RETR")]


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})


def sse_json(resp):
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError("no JSON-RPC message in: " + resp.text[:300])


def tool_text(msg):
    return msg["result"]["content"][0]["text"]


def cookie_from(resp) -> str:
    raw = resp.headers.get("set-cookie", "")
    return raw.split(";")[0] if raw.startswith(security.COOKIE_NAME + "=") else ""


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class Base(unittest.TestCase):
    def setUp(self):
        a = server.AUTH
        server.SETTINGS.update({"security.api_auth": "on"}, clear=["security.dashboard_password"])
        a.dash.set_fields(login_enabled=None, owner_hash=None, viewer_hash=None, admin_hash=ADMIN_HASH)
        for k in a.keys.list_public():
            a.delete_key(k["id"])
        a.stats.clear()
        a.env = dict(os.environ)
        server.PRINTERS.clear()
        server._START_PENDING.clear()
        server.START_TICKETS.clear()
        server.SPOOL_MEMORY.clear()                          # a remembered choice from an earlier test must not steer this one
        server.START_TICKETS.clock = time.time
        try:
            os.remove(server.CACHE_PATH)
        except OSError:
            pass
        self.c = CLIENT
        self.c.cookies.clear()
        p = mock.patch.object(server.time, "sleep")        # start_print / cancel_print settle waits
        p.start()
        self.addCleanup(p.stop)

    def mode(self, m):
        server.SETTINGS.update({"security.api_auth": m})

    def key(self, scope="read"):
        _rec, token = server.AUTH.create_key("t-" + scope, scope)
        return {"Authorization": "Bearer " + token}

    def owner(self):
        """The OWNER at the dashboard: the cookie /dashboard hands out + the admin unlock (the admin password typed)."""
        r = self.c.get("/dashboard")
        ck = cookie_from(r)
        self.c.cookies.clear()
        self.assertTrue(ck)
        return {"Cookie": f"{ck}; {security.ADMIN_COOKIE}={server.AUTH.make_admin_cookie()}", "Origin": "http://testserver"}

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

    def add_bambu(self, name="A1", state="IDLE", files=("job.gcode.3mf",), filaments=1):
        prn = FakeBambu(name, state)
        for f in files:
            prn.files[f] = make_3mf(filaments)
        server.PRINTERS[name.lower()] = prn
        return prn


# =================================================================== P0-1: MCP body encodings

class McpScopeBypassTests(Base):
    def setUp(self):
        super().setUp()
        self.prn = self.add_bambu()
        self.rk = self.key("read")
        self.h = self.mcp_session(self.rk)
        server.SETTINGS.reset(["general.hub_name"])
        self.before = server.SETTINGS.get("general.hub_name")

    def attack(self, body, headers=None):
        return self.c.post("/mcp", headers=headers or self.h, content=body)

    def call_doc(self, name="PWNED"):
        return rpc("tools/call", {"name": "update_settings", "arguments": {"values": {"general.hub_name": name}}}, 9)

    def assert_untouched(self):
        self.assertEqual(server.SETTINGS.get("general.hub_name"), self.before)
        self.assertEqual(self.prn.calls, [])

    def test_plain_utf8_is_refused_as_before(self):
        r = self.attack(json.dumps(self.call_doc()).encode())
        self.assertIn("needs a 'control' key", tool_text(sse_json(r)))
        self.assert_untouched()

    def test_bom_utf16_utf32_and_friends_are_refused(self):
        doc = json.dumps(self.call_doc()).encode().decode()
        bodies = {"utf-8 BOM": b"\xef\xbb\xbf" + doc.encode(), "utf-16": doc.encode("utf-16"),
                  "utf-16-le": doc.encode("utf-16-le"), "utf-16-be": doc.encode("utf-16-be"),
                  "utf-32": doc.encode("utf-32"), "utf-32-le": doc.encode("utf-32-le"),
                  "utf-32-be": doc.encode("utf-32-be"), "not json": b"{nope", "empty": b"", "scalar": b"7",
                  "empty batch": b"[]", "batch of scalars": b"[1, 2]",
                  "duplicate keys": b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_settings"},'
                                    b'"params":{"name":"update_settings","arguments":{"values":{"general.hub_name":"X"}}}}'}
        for label, body in bodies.items():
            with self.subTest(label):
                r = self.attack(body)
                self.assertEqual(r.status_code, 400, (label, r.text[:200]))
                self.assertIn("cannot be read", r.json()["error"])
        self.assert_untouched()
        self.assertGreaterEqual(server.AUTH.status()["stats"]["refused_wrong_scope"], len(bodies))

    def test_a_batch_with_one_control_call_is_refused(self):
        batch = [rpc("tools/call", {"name": "list_printers", "arguments": {}}, 1), self.call_doc()]
        r = self.attack(json.dumps(batch).encode())
        self.assertEqual(r.status_code, 403)
        self.assertIn("update_settings", r.json()["error"]["message"])
        self.assert_untouched()

    def test_a_batch_of_reads_still_works(self):
        batch = [rpc("tools/call", {"name": "list_printers", "arguments": {}}, 1),
                 rpc("tools/call", {"name": "list_printers", "arguments": {}}, 2)]
        r = self.attack(json.dumps(batch).encode())           # passes our check (the SDK itself refuses batches)
        self.assertNotEqual(r.status_code, 403)
        self.assertNotIn("cannot be read", r.text)

    def test_a_control_key_may_send_any_encoding_it_is_not_checked_by_body(self):
        h = self.mcp_session(self.key("control"))
        r = self.c.post("/mcp", headers=h, content=json.dumps(self.call_doc("by-control")).encode())
        self.assertEqual(r.status_code, 200)
        server.SETTINGS.reset(["general.hub_name"])

    def test_each_tool_rechecks_its_scope_even_if_the_middleware_misread_the_body(self):
        # simulate the middleware finding nothing wrong with a control call by a read key
        with mock.patch.object(security, "refused_tools", return_value=[]):
            r, msg = self.call(self.h, "update_settings", {"values": {"general.hub_name": "PWNED"}})
        self.assertEqual(r.status_code, 200)
        text = json.dumps(msg)
        self.assertIn("needs a 'control' key", text)
        self.assertTrue(msg["result"]["isError"])
        self.assert_untouched()

    def test_tool_caller_scope_unit(self):
        class Req:
            def __init__(self, ident):
                self.scope = {"auth": ident}

        class Ctx:
            def __init__(self, ident):
                self.request_context = type("RC", (), {"request": Req(ident)})()
        read = security.Identity("key", "read", "k", "n")
        ctrl = security.Identity("key", "control", "k", "n")
        self.assertEqual(server._tool_caller_scope(Ctx(ctrl), "start_print"), "control")
        self.assertEqual(server._tool_caller_scope(Ctx(read), "printer_status"), "read")
        with self.assertRaises(ValueError):
            server._tool_caller_scope(Ctx(read), "start_print")
        with self.assertRaises(ValueError):
            server._tool_caller_scope(Ctx(read), "some_new_tool")                  # unknown -> control
        self.assertIsNone(server._tool_caller_scope(None, "start_print"))           # key check off / no request

    def test_tools_keep_their_shape(self):
        tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
        self.assertEqual(set(tools), set(security.TOOL_SCOPES))
        for t in tools.values():
            self.assertNotIn("ctx", t.parameters.get("properties", {}), t.name)
        self.assertIn("bed_clear_confirmed_by_human", tools["start_print"].parameters["properties"])
        self.assertIn("bed_clear_confirmed_by_human", server.start_print.__doc__)
        self.assertTrue(callable(server.start_print) and not asyncio.iscoroutinefunction(server.start_print))


# =================================================================== P0-2 / P1-3: the browser boundary

class BrowserBoundaryTests(Base):
    XSITE = {"Host": "192.168.1.50:8271", "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site",
             "Content-Type": "text/plain;charset=UTF-8"}

    def setUp(self):
        super().setUp()
        self.prn = self.add_bambu()

    def test_cross_site_simple_post_is_refused_in_every_mode(self):
        for m in ("off", "warn", "on"):
            self.mode(m)
            r = self.c.post("/api/printer/A1/action", headers=self.XSITE, content=b'{"action":"pause"}')
            self.assertEqual(r.status_code, 403, m)
            r = self.c.post("/api/settings/import", headers=self.XSITE,
                            content=json.dumps({"values": {"webhook.url": "https://evil.example/c"}}).encode())
            self.assertEqual(r.status_code, 403, m)
        self.assertNotIn("pause", self.prn.calls)
        self.assertEqual(server.SETTINGS.get("webhook.url"), "")

    def test_cross_site_with_json_content_type_is_refused_too(self):
        for m in ("off", "warn", "on"):
            self.mode(m)
            r = self.c.post("/api/printer/A1/action", json={"action": "pause"},
                            headers={"Origin": "https://evil.example"})
            self.assertEqual(r.status_code, 403, m)
            r = self.c.put("/api/settings", json={"values": {}}, headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status_code, 403, m)

    def test_foreign_host_cannot_write_in_any_mode_and_rebinding_cannot_mint_a_key(self):
        reb = {"Host": "rebind.evil.example:8271"}
        for m in ("off", "warn"):
            self.mode(m)
            r = self.c.post("/api/keys", headers={**reb, "Content-Type": "application/json"},
                            json={"name": "x", "scope": "control"})
            self.assertEqual(r.status_code, 403, m)
            self.assertEqual(server.AUTH.keys.counts()["active"], 0)
            r = self.c.post("/api/printer/A1/action", headers=reb, json={"action": "pause"})
            self.assertEqual(r.status_code, 403, m)
            # ...and, unauthenticated, it cannot read either
            self.assertEqual(self.c.get("/api/settings", headers=reb).status_code, 403, m)
            self.assertEqual(self.c.get("/api/fleet", headers=reb).status_code, 403, m)
        self.assertNotIn("pause", self.prn.calls)
        self.mode("on")
        self.assertEqual(self.c.post("/api/keys", headers=reb, json={}).status_code, 403)
        # the public bits stay public
        self.mode("warn")
        self.assertEqual(self.c.get("/api/health", headers=reb).status_code, 200)

    def test_a_valid_key_works_from_any_host_name(self):
        k = {**self.key("control"), "Host": "hub.example.com"}
        self.assertEqual(self.c.get("/api/fleet", headers=k).status_code, 200)
        self.assertEqual(self.c.post("/api/printer/A1/action", headers=k, json={"action": "pause"}).status_code, 200)

    def test_non_json_bodies_are_refused_and_json_still_works(self):
        for m in ("off", "warn", "on"):
            self.mode(m)
            h = self.key("control") if m == "on" else {}
            for body, ctype in ((b'{"action":"pause"}', "text/plain"), (b"action=pause", "application/x-www-form-urlencoded"),
                                (b'{"action":"pause"}', None)):
                hh = {**h, **({"Content-Type": ctype} if ctype else {})}
                r = self.c.post("/api/printer/A1/action", headers=hh, content=body)
                self.assertEqual(r.status_code, 415, (m, ctype))
            r = self.c.post("/api/printer/A1/action", headers=h, json={"action": "pause"})
            self.assertEqual(r.status_code, 200, m)
            r = self.c.post("/api/printer/A1/action", headers={**h, "Content-Type": "application/json; charset=utf-8"},
                            content=b'{"action":"pause"}')
            self.assertEqual(r.status_code, 200, m)
        # a body-less POST is fine
        self.mode("warn")
        self.assertNotEqual(self.c.post("/api/auth/logout").status_code, 415)

    def test_agents_and_curl_without_origin_keep_working_in_warn_and_off(self):
        for m in ("off", "warn"):
            self.mode(m)
            r = self.c.post("/api/printer/A1/action", json={"action": "pause"})        # keyless, no Origin: a live agent
            self.assertEqual(r.status_code, 200, m)
        self.mode("warn")
        h = self.mcp_session({})
        r, msg = self.call(h, "pause_print", {"printer": "A1"})
        self.assertEqual(tool_text(msg), "pause sent")

    # ---- admin: a key can never administer ----

    def test_a_control_key_cannot_mint_keys_pair_devices_or_switch_the_check_off(self):
        self.mode("on")
        k = self.key("control")
        calls = (("post", "/api/keys", {"name": "mine", "scope": "control"}), ("get", "/api/keys", None),
                 ("put", "/api/security", {"mode": "off"}), ("put", "/api/security", {"dashboard_password": "attacker chose"}),
                 ("get", "/api/security", None), ("post", "/api/pairing", {}), ("get", "/api/devices", None),
                 ("post", "/api/auth/logout-everywhere", None), ("delete", "/api/sessions/s_x", None))
        for method, path, body in calls:
            r = getattr(self.c, method)(path, headers=k, **({"json": body} if body is not None else {}))
            self.assertEqual((r.status_code, r.json().get("required_scope")), (403, "admin"), (method, path))
        self.assertEqual(server.AUTH.mode(), "on")
        self.assertFalse(server.AUTH.password_set())
        self.assertEqual(server.AUTH.keys.counts()["active"], 1)          # only the agent's own key
        # the same through the generic settings routes
        r = self.c.put("/api/settings", headers=k, json={"values": {"security.api_auth": "off"}})
        self.assertEqual((r.status_code, r.json()["required_scope"]), (403, "admin"))
        r = self.c.put("/api/settings", headers=k, json={"values": {}, "clear": ["security.dashboard_password"]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(server.AUTH.mode(), "on")
        # ...but an ordinary setting is fine
        self.assertEqual(self.c.put("/api/settings", headers=k, json={"values": {"general.hub_name": "ok"}}).status_code, 200)
        server.SETTINGS.reset(["general.hub_name"])

    def test_the_dashboard_owner_can(self):
        for m in ("on", "warn", "off"):
            self.mode(m)
            o = self.owner()
            r = self.c.post("/api/keys", headers=o, json={"name": "k-" + m, "scope": "read"})
            self.assertEqual(r.status_code, 201, m)
            r = self.c.put("/api/security", headers=o, json={"mode": m})
            self.assertEqual(r.status_code, 200, (m, r.text))
            r = self.c.put("/api/settings", headers=o, json={"values": {"security.api_auth": m}})
            self.assertEqual(r.status_code, 200, m)

    def test_nobody_without_a_dashboard_cookie_administers_in_any_mode(self):
        for m, want in (("off", 403), ("warn", 401), ("on", 401)):
            self.mode(m)
            self.assertEqual(self.c.post("/api/keys", json={"name": "x"}).status_code, want, m)
            self.assertEqual(self.c.put("/api/security", json={"mode": "off"}).status_code, want, m)

    def test_a_view_only_sign_in_cannot_administer(self):
        server.AUTH.dash.set_fields(owner_hash=security.hash_password("owner password 1"),
                                    viewer_hash=security.hash_password("viewer password 1"), login_enabled=True)
        r = self.c.post("/api/auth/login", json={"password": "viewer password 1"})
        self.assertEqual(r.status_code, 200, r.text)
        ck = cookie_from(r)
        self.c.cookies.clear()
        r = self.c.get("/api/security", headers={"Cookie": ck})
        self.assertEqual(r.status_code, 403)

    def test_keys_made_without_the_check_are_remembered(self):
        self.mode("warn")
        self.c.post("/api/keys", headers=self.owner(), json={"name": "warn-made"})
        self.mode("on")
        st = server.AUTH.status()
        self.assertEqual(len(st["keys_made_without_check"]), 1)
        self.assertTrue(any(w["id"] == "no_dashboard_password" for w in st["warnings"]))


# =================================================================== P0-3 / P1-5: the start ticket

class StartTicketTests(Base):
    def setUp(self):
        super().setUp()
        self.prn = self.add_bambu(files=("job.gcode.3mf", "other.gcode.3mf"))

    def suggest(self, name="job.gcode.3mf", printer="A1"):
        return json.loads(server.suggest_mapping(printer, name))

    def start(self, d, name="job.gcode.3mf", printer="A1", **kw):
        kw.setdefault("bed_clear_confirmed_by_human", True)
        return server.start_print(printer, name, kw.pop("mapping", d["ams_mapping"]),
                                  kw.pop("confirm", d["confirm_string"]), **kw)

    def test_suggest_then_start_works_and_the_ticket_is_single_use(self):
        d = self.suggest()
        self.assertTrue(d["confirm_string"].startswith("ph-") and len(d["confirm_string"]) > 20)
        out = self.start(d)
        self.assertIn('"sent": true', out)
        self.assertEqual(self.prn.started, [("job.gcode.3mf", [0])])
        server._START_PENDING.clear()
        again = self.start(d)
        self.assertTrue(again.startswith("REFUSED"))
        self.assertEqual(len(self.prn.started), 1)

    def test_two_suggestions_give_two_different_tickets(self):
        self.assertNotEqual(self.suggest()["confirm_string"], self.suggest()["confirm_string"])

    def test_the_old_guessable_string_and_made_up_strings_are_refused_without_echoing_anything(self):
        d = self.suggest()
        for bad in ("job.gcode.3mf|A1|0", "guess", "", "ph-" + "x" * 24, d["confirm_string"][:-1], "ph-"):
            out = self.start(d, confirm=bad)
            self.assertTrue(out.startswith("REFUSED"), bad)
            self.assertNotIn("Expected", out)
            self.assertNotIn("job.gcode.3mf|A1|0", out)
            self.assertNotIn(d["confirm_string"], out)
        self.assertEqual(self.prn.started, [])
        # none of that burned the real ticket
        self.assertIn('"sent": true', self.start(d))

    def test_refusals_never_contain_the_expected_value_even_on_a_binding_mismatch(self):
        d = self.suggest()
        out = self.start(d, name="other.gcode.3mf")
        self.assertIn("different file", out)
        self.assertNotIn(d["confirm_string"], out)

    def test_bed_clear_statement_is_required_and_does_not_burn_the_ticket(self):
        d = self.suggest()
        for flag in (False, None, "yes", 1):
            out = server.start_print("A1", "job.gcode.3mf", d["ams_mapping"], d["confirm_string"],
                                     bed_clear_confirmed_by_human=flag)
            self.assertIn("bed_clear_confirmed_by_human", out)
            self.assertTrue(out.startswith("REFUSED"))
        out = server.start_print("A1", "job.gcode.3mf", d["ams_mapping"], d["confirm_string"])
        self.assertTrue(out.startswith("REFUSED"))
        self.assertEqual(self.prn.started, [])
        self.assertIn('"sent": true', self.start(d))
        doc = server.start_print.__doc__
        self.assertIn("HUMAN'S statement", doc)

    def test_a_ticket_expires(self):
        d = self.suggest()
        now = time.time()
        server.START_TICKETS.clock = lambda: now + server.START_TICKET_TTL_S + 1
        out = self.start(d)
        self.assertTrue(out.startswith("REFUSED"))
        self.assertEqual(self.prn.started, [])

    def test_a_ticket_is_bound_to_printer_file_mapping_and_the_ams(self):
        other = self.add_bambu("B2", files=("job.gcode.3mf",))
        d = self.suggest()
        self.assertIn("different printer", self.start(d, printer="B2"))
        self.assertIn("different file", self.start(d, name="other.gcode.3mf"))
        # an invented mapping (a tray that does not exist) and one that differs from the proposal
        self.assertTrue(self.start(d, mapping=[7]).startswith("REFUSED"))
        self.assertIn("differs", self.start(d, mapping=[1]))            # exists, but is not what was proposed
        self.assertTrue(self.start(d, mapping=[0, 0]).startswith("REFUSED"))
        self.assertTrue(self.start(d, mapping=[True]).startswith("REFUSED"))
        self.assertEqual((self.prn.started, other.started), ([], []))
        # the spools are swapped after the human was told the plan
        self.prn.trays[0]["color"] = "#00FF00"
        self.assertIn("AMS changed", self.start(d))
        self.prn.trays[0]["color"] = "#FF0000"
        self.assertIn('"sent": true', self.start(d))

    def test_the_file_changing_on_the_card_is_noticed(self):
        d = self.suggest()
        self.prn.files["job.gcode.3mf"] = make_3mf(1) + b"\x00" * 10
        self.assertIn("changed since", self.start(d))
        self.prn.files.pop("job.gcode.3mf")
        self.assertIn("no longer on", self.start(d))
        self.assertEqual(self.prn.started, [])

    def test_a_printer_that_was_edited_or_re_added_invalidates_the_ticket(self):
        d = self.suggest()
        replacement = FakeBambu("A1")
        replacement.files = dict(self.prn.files)
        server.PRINTERS["a1"] = replacement
        self.assertIn("different printer", self.start(d))
        self.assertEqual(replacement.started, [])

    def test_busy_or_unready_printers_are_refused_and_keep_the_ticket(self):
        d = self.suggest()
        for st in ("RUNNING", "PAUSE", "PREPARE", "SLICING", "INIT", "UNKNOWN", "OFFLINE"):
            self.prn.state = st
            self.assertTrue(self.start(d).startswith("REFUSED"), st)
        self.assertEqual(self.prn.started, [])
        self.prn.state = "FINISH"
        self.assertIn('"sent": true', self.start(d))

    def test_a_blocked_job_gets_no_ticket(self):
        prn = self.add_bambu("M3", files=("multi.gcode.3mf",), filaments=3)       # three filaments, two spools
        d = json.loads(server.suggest_mapping("M3", "multi.gcode.3mf"))
        self.assertTrue(d["blocked"])
        self.assertEqual(d["confirm_string"], "")
        self.assertTrue(server.start_print("M3", "multi.gcode.3mf", [0, 1, -1], "", bed_clear_confirmed_by_human=True)
                        .startswith("REFUSED"))
        self.assertEqual(prn.started, [])

    def test_a_second_start_waits_for_the_first_to_settle_and_cancel_clears_it(self):
        d1, d2 = self.suggest(), self.suggest("other.gcode.3mf")
        self.assertIn('"sent": true', self.start(d1))
        out = self.start(d2, name="other.gcode.3mf")
        self.assertIn("may still be settling", out)
        server.cancel_print("A1")
        self.assertIn('"sent": true', self.start(d2, name="other.gcode.3mf"))

    def test_two_simultaneous_starts_only_send_one(self):
        d1, d2 = self.suggest(), self.suggest()
        self.prn.start_gate = threading.Event()
        results = []
        t1 = threading.Thread(target=lambda: results.append(self.start(d1)))
        t1.start()
        deadline = time.time() + 5
        while not server._start_lock("A1").locked() and time.time() < deadline:
            REAL_SLEEP(0.01)
        second = self.start(d2)
        self.assertIn("already being sent", second)
        self.prn.start_gate.set()
        t1.join(5)
        self.assertEqual(len(self.prn.started), 1)
        self.assertIn('"sent": true', results[0])

    def test_a_failed_start_is_reported_and_not_marked_pending(self):
        d = self.suggest()
        with mock.patch.object(self.prn, "start_print", side_effect=RuntimeError("FTPS: boom")):
            out = self.start(d)
        self.assertTrue(out.startswith("REFUSED: FTPS: boom"))
        self.assertNotIn("a1", server._START_PENDING)

    def test_rest_agents_must_use_the_proposed_mapping_but_the_dashboard_may_pick_a_spool(self):
        self.mode("on")
        k = self.key("control")
        d = self.c.post("/api/printer/A1/suggest", headers=k, json={"filename": "job.gcode.3mf"}).json()
        body = {"filename": "job.gcode.3mf", "ams_mapping": [1], "confirm": d["confirm_string"], "plate_clear": True}
        r = self.c.post("/api/printer/A1/start", headers=k, json=body)                  # an agent re-mapping: no
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.prn.started, [])
        o = self.owner()
        r = self.c.post("/api/printer/A1/start", headers=o, json={**body, "ams_mapping": [7]})   # invented tray: never
        self.assertEqual(r.status_code, 409)
        r = self.c.post("/api/printer/A1/start", headers=o, json=body)                  # the human picked spool 2
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.prn.started, [("job.gcode.3mf", [1])])

    def test_rest_plate_clear_is_still_required_and_missing_fields_are_400(self):
        self.mode("off")
        d = self.c.post("/api/printer/A1/suggest", json={"filename": "job.gcode.3mf"}).json()
        r = self.c.post("/api/printer/A1/start", json={"filename": "job.gcode.3mf", "ams_mapping": [0],
                                                       "confirm": d["confirm_string"]})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/printer/A1/start", json={"plate_clear": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.post("/api/printer/A1/suggest", json={}).status_code, 400)

    def test_agent_style_keyless_mcp_in_warn_mode_still_starts_with_suggest_then_start(self):
        self.mode("warn")
        h = self.mcp_session({})
        _r, msg = self.call(h, "suggest_mapping", {"printer": "A1", "filename": "job.gcode.3mf"})
        d = json.loads(tool_text(msg))
        self.assertTrue(d["confirm_string"].startswith("ph-"))
        _r, msg = self.call(h, "start_print", {"printer": "A1", "filename": "job.gcode.3mf",
                                               "ams_mapping": d["ams_mapping"], "confirm": d["confirm_string"],
                                               "bed_clear_confirmed_by_human": True})
        self.assertIn('"sent": true', tool_text(msg))
        self.assertEqual(self.prn.started, [("job.gcode.3mf", [0])])
        # without the human's statement it is refused with instructions
        d2 = json.loads(tool_text(self.call(h, "suggest_mapping", {"printer": "A1", "filename": "other.gcode.3mf"})[1]))
        server._START_PENDING.clear()
        _r, msg = self.call(h, "start_print", {"printer": "A1", "filename": "other.gcode.3mf",
                                               "ams_mapping": d2["ams_mapping"], "confirm": d2["confirm_string"]})
        self.assertIn("bed_clear_confirmed_by_human", tool_text(msg))
        self.assertEqual(len(self.prn.started), 1)


# =================================================================== P0-8: test buttons and saves vs saved secrets

class Capture:
    """A loopback HTTP endpoint that records what reaches it."""

    def __init__(self):
        self.seen = []
        cap = self

        class H(BaseHTTPRequestHandler):
            def _h(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                cap.seen.append((self.command, self.path, dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok","score":0.1}')
            do_GET = do_POST = do_PUT = _h

            def log_message(self, *a):
                pass
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def headers_blob(self):
        return json.dumps([h for _m, _p, h in self.seen])

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class SecretRebindTests(Base):
    def setUp(self):
        super().setUp()
        self.mode("off")
        self.real, self.evil = Capture(), Capture()
        self.addCleanup(self.real.close)
        self.addCleanup(self.evil.close)
        server.SETTINGS.update({"webhook.url": self.real.url + "/hook", "webhook.bearer": "WEBHOOK-SECRET-123",
                                "ntfy.server_url": self.real.url, "ntfy.topic": "printhub-topic",
                                "ntfy.token": "NTFY-SECRET-456",
                                "email.host": "smtp.example.com", "email.port": 587, "email.password": "SMTP-PW-789",
                                "email.username": "owner@example.com",
                                "detection.mode": "remote", "detection.url": self.real.url,
                                "detection.token": "DET-SECRET-000"})
        self.addCleanup(lambda: server.SETTINGS.reset(
            ["webhook.url", "ntfy.server_url", "ntfy.topic", "email.host", "email.port", "email.username",
             "detection.mode", "detection.url"]))
        self.addCleanup(lambda: server.SETTINGS.update(
            {}, clear=["webhook.bearer", "ntfy.token", "email.password", "detection.token"]))
        self.o = self.owner()

    def test_webhook_test_to_a_new_url_never_carries_the_saved_bearer(self):
        r = self.c.post("/api/settings/test/webhook", headers=self.o, json={"values": {"url": self.evil.url + "/steal"}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(len(self.evil.seen), 1)
        self.assertNotIn("WEBHOOK-SECRET-123", self.evil.headers_blob())
        self.assertIn("not used", r.json()["note"])

    def test_webhook_test_to_the_saved_url_still_uses_the_saved_bearer(self):
        r = self.c.post("/api/settings/test/webhook", headers=self.o, json={"values": {"url": self.real.url + "/hook"}})
        self.assertTrue(r.json()["ok"])
        self.assertIn("WEBHOOK-SECRET-123", self.real.headers_blob())
        self.assertNotIn("note", r.json())

    def test_a_webhook_path_change_on_the_same_host_does_not_keep_the_bearer(self):
        # second security review (P3): the path is part of where a bearer goes - a webhook id often IS the credential
        self.real.seen.clear()
        r = self.c.post("/api/settings/test/webhook", headers=self.o, json={"values": {"url": self.real.url + "/other-path"}})
        self.assertTrue(r.json()["ok"])
        self.assertNotIn("WEBHOOK-SECRET-123", self.real.headers_blob())
        self.assertIn("not used", r.json()["note"])

    def test_a_secret_typed_into_the_form_is_used_for_the_new_address(self):
        self.c.post("/api/settings/test/webhook", headers=self.o,
                    json={"values": {"url": self.evil.url + "/x", "bearer": "TYPED-NOW"}})
        self.assertIn("TYPED-NOW", self.evil.headers_blob())
        self.assertNotIn("WEBHOOK-SECRET-123", self.evil.headers_blob())

    def test_ntfy_test_to_a_new_server_or_topic_drops_the_saved_token(self):
        self.c.post("/api/settings/test/ntfy", headers=self.o, json={"values": {"server_url": self.evil.url}})
        self.assertNotIn("NTFY-SECRET-456", self.evil.headers_blob())
        self.c.post("/api/settings/test/ntfy", headers=self.o, json={"values": {"topic": "another-topic"}})
        self.assertNotIn("NTFY-SECRET-456", self.real.headers_blob())
        self.c.post("/api/settings/test/ntfy", headers=self.o, json={"values": {}})
        self.assertIn("NTFY-SECRET-456", self.real.headers_blob())

    def test_email_destination_rule(self):
        ch = server.NOTIFIER._channel("email", {"host": "127.0.0.1", "port": 2525})
        self.assertEqual(ch.cfg["password"], "")
        self.assertEqual(ch.dropped_secrets, ["Password"])
        ch = server.NOTIFIER._channel("email", {"tls": "none"})
        self.assertEqual(ch.cfg["password"], "")
        ch = server.NOTIFIER._channel("email", {"host": "127.0.0.1", "password": "typed"})
        self.assertEqual(ch.cfg["password"], "typed")
        ch = server.NOTIFIER._channel("email", {"to_addr": "x@example.com"})              # not a destination field
        self.assertEqual(ch.cfg["password"], "SMTP-PW-789")
        ch = server.NOTIFIER._channel("email", None)
        self.assertEqual(ch.cfg["password"], "SMTP-PW-789")

    def test_destination_unit(self):
        d = notify.destination
        self.assertEqual(d("webhook", {"url": "HTTP://Host.example:80/a"}), d("webhook", {"url": "http://host.example/a/"}))
        self.assertNotEqual(d("webhook", {"url": "http://host.example/a"}), d("webhook", {"url": "http://host.example/b"}))
        self.assertNotEqual(d("webhook", {"url": "http://host.example"}), d("webhook", {"url": "https://host.example"}))
        self.assertEqual(d("telegram", {"chat_id": "1"}), d("telegram", {"chat_id": "2"}))

    def test_detector_test_to_a_new_address_never_carries_the_saved_token(self):
        r = self.c.post("/api/detection/test", headers=self.o, json={"mode": "remote", "url": self.evil.url})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.evil.seen)
        self.assertNotIn("DET-SECRET-000", self.evil.headers_blob())
        self.assertFalse(r.json()["token_given"])
        self.assertIn("not used", r.json()["note"])
        self.real.seen.clear()
        r = self.c.post("/api/detection/test", headers=self.o, json={"mode": "remote", "url": self.real.url})
        self.assertTrue(r.json()["token_given"])
        self.assertIn("DET-SECRET-000", self.real.headers_blob())
        self.evil.seen.clear()
        self.c.post("/api/detection/test", headers=self.o,
                    json={"mode": "remote", "url": self.evil.url, "token": "TYPED-DET"})
        self.assertIn("TYPED-DET", self.evil.headers_blob())

    def test_printer_test_and_edit_to_a_new_address_need_the_key_typed_again(self):
        server.FLEET.add("octoprint", {"name": "mk4", "url": self.real.url, "api_key": "OCTO-KEY-SECRET"}, True)
        self.addCleanup(lambda: server.FLEET.remove("mk4", True))
        self.evil.seen.clear()
        r = self.c.post("/api/printers/test", headers=self.o,
                        json={"type": "octoprint", "name": "mk4", "values": {"url": self.evil.url}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.evil.seen, [])
        r = self.c.put("/api/printers/mk4", headers=self.o, json={"values": {"url": self.evil.url}, "skip_test": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(server.STORE.get("mk4")["url"], self.real.url)
        self.assertEqual(self.evil.seen, [])
        # MCP tools too
        h = self.mcp_session(self.key("control"))
        self.mode("on")
        h = self.mcp_session(self.key("control"))
        _r, msg = self.call(h, "test_printer_connection", {"type": "octoprint", "values": {"url": self.evil.url},
                                                           "existing_printer": "mk4"})
        self.assertIn("REFUSED", tool_text(msg))
        self.assertEqual(self.evil.seen, [])

    def test_saving_a_new_address_without_retyping_the_secret_is_refused_but_retyping_works(self):
        for body, want in (
                ({"webhook.url": self.evil.url + "/h"}, "webhook.bearer"),
                ({"ntfy.server_url": self.evil.url}, "ntfy.token"),
                ({"ntfy.topic": "else"}, "ntfy.token"),
                ({"email.host": "mail.evil.example"}, "email.password"),
                ({"email": {"port": 25}}, "email.password"),
                ({"detection.url": self.evil.url}, "detection.token")):
            r = self.c.put("/api/settings", headers=self.o, json={"values": body})
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(want, r.json()["errors"], body)
        self.assertEqual(server.SETTINGS.get("webhook.url"), self.real.url + "/hook")
        r = self.c.put("/api/settings", headers=self.o,
                       json={"values": {"webhook.url": self.evil.url + "/h", "webhook.bearer": "NEW-BEARER"}})
        self.assertEqual(r.status_code, 200, r.text)
        # changing an unrelated field, or clearing the secret on purpose, is fine
        self.assertEqual(self.c.put("/api/settings", headers=self.o, json={"values": {"general.hub_name": "x"}}).status_code, 200)
        r = self.c.put("/api/settings", headers=self.o, json={"values": {"ntfy.topic": "else"}, "clear": ["ntfy.token"]})
        self.assertEqual(r.status_code, 200, r.text)
        server.SETTINGS.reset(["general.hub_name"])

    def test_the_mcp_update_settings_tool_has_the_same_rule(self):
        self.mode("on")
        h = self.mcp_session(self.key("control"))
        _r, msg = self.call(h, "update_settings", {"values": {"webhook.url": self.evil.url + "/h"}})
        self.assertIn("REFUSED", tool_text(msg))
        self.assertEqual(server.SETTINGS.get("webhook.url"), self.real.url + "/hook")

    def test_switching_the_detector_off_and_on_again_at_the_same_address_is_not_a_move(self):
        self.assertEqual(self.c.put("/api/settings", headers=self.o, json={"values": {"detection.mode": "off"}}).status_code, 200)
        self.assertEqual(self.c.put("/api/settings", headers=self.o, json={"values": {"detection.mode": "remote"}}).status_code, 200)

    def test_import_cannot_redirect_a_channel_either(self):
        r = self.c.post("/api/settings/import", headers=self.o, json={"values": {"webhook.url": self.evil.url + "/h"}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(server.SETTINGS.get("webhook.url"), self.real.url + "/hook")


# =================================================================== P1-1: the event loop

class EventLoopTests(Base):
    def test_a_slow_sync_tool_does_not_stall_the_hub(self):
        import httpx

        class SlowPrinter(FakeBambu):
            def stop(self):
                REAL_SLEEP(1.5)               # a printer call that takes a while

        server.PRINTERS["slow"] = SlowPrinter("Slow")
        self.mode("off")
        app = server.mcp.streamable_http_app()
        timings = {}

        async def main():
            ticks = []

            async def heartbeat():
                while True:
                    ticks.append(time.time())
                    await asyncio.sleep(0.05)
            hb = asyncio.create_task(heartbeat())
            t0 = time.time()
            call = asyncio.create_task(server.mcp.call_tool("cancel_print", {"printer": "Slow"}))
            await asyncio.sleep(0.3)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as hc:
                t1 = time.time()
                resp = await hc.get("/api/health")
                timings["health"] = time.time() - t1
                t1 = time.time()
                await hc.get("/api/fleet")
                timings["fleet"] = time.time() - t1
            out = await call
            timings["tool"] = time.time() - t0
            hb.cancel()
            timings["worst_gap"] = max(b - a for a, b in zip(ticks, ticks[1:]))
            return resp.status_code, out
        status, out = asyncio.run(main())
        self.assertEqual(status, 200)
        self.assertGreaterEqual(timings["tool"], 1.4)           # the tool really did take its time...
        self.assertLess(timings["health"], 0.5, timings)        # ...and the Hub kept answering meanwhile
        self.assertLess(timings["fleet"], 0.5, timings)
        self.assertLess(timings["worst_gap"], 0.5, timings)     # the event loop itself never froze
        self.assertIn("stop sent", json.dumps(out, default=str))


# =================================================================== P1-2: what a read-only caller may see

class RedactionTests(Base):
    def setUp(self):
        super().setUp()
        server.SETTINGS.update({"webhook.url": "http://homeassistant.local:8123/api/webhook/SECRET-WEBHOOK-ID-abc123",
                                "ntfy.topic": "my-secret-topic-zz9", "telegram.chat_id": "-100998877",
                                "email.username": "owner@example.com",
                                "discord.webhook_url": "https://discord.com/api/webhooks/1/DISCORD-TOKEN"})
        self.addCleanup(lambda: server.SETTINGS.reset(["webhook.url", "ntfy.topic", "telegram.chat_id", "email.username"]))
        self.addCleanup(lambda: server.SETTINGS.update({}, clear=["discord.webhook_url"]))
        self.rk, self.ck = self.key("read"), self.key("control")

    def values(self, doc):
        return {f["key"]: f["value"] for s in doc["sections"] for f in s["fields"]}

    def test_read_key_sees_none_of_them_on_rest(self):
        v = self.values(self.c.get("/api/settings", headers=self.rk).json())
        for k in ("webhook.url", "ntfy.topic", "telegram.chat_id", "email.username"):
            self.assertEqual(v[k], security.HIDDEN, k)
        self.assertEqual(v["discord.webhook_url"], "set")
        self.assertEqual(v["ntfy.server_url"], "https://ntfy.sh")             # ordinary values stay
        exp = self.c.get("/api/settings/export", headers=self.rk).json()["values"]
        self.assertEqual(exp["webhook.url"], security.HIDDEN)
        blob = self.c.get("/api/settings", headers=self.rk).text + json.dumps(exp)
        for secret in ("SECRET-WEBHOOK-ID", "my-secret-topic-zz9", "owner@example.com", "-100998877", "DISCORD-TOKEN"):
            self.assertNotIn(secret, blob)

    def test_control_still_sees_and_can_edit_them(self):
        v = self.values(self.c.get("/api/settings", headers=self.ck).json())
        self.assertIn("SECRET-WEBHOOK-ID", v["webhook.url"])
        self.assertEqual(v["ntfy.topic"], "my-secret-topic-zz9")
        self.assertIn("SECRET-WEBHOOK-ID", self.c.get("/api/settings/export", headers=self.ck).json()["values"]["webhook.url"])

    def test_read_key_over_mcp(self):
        h = self.mcp_session(self.rk)
        _r, msg = self.call(h, "get_settings", {"section": "webhook"})
        self.assertNotIn("SECRET-WEBHOOK-ID", tool_text(msg))
        self.assertIn("hidden", tool_text(msg))
        h2 = self.mcp_session(self.ck)
        _r, msg = self.call(h2, "get_settings", {"section": "webhook"})
        self.assertIn("SECRET-WEBHOOK-ID", tool_text(msg))

    def test_view_only_dashboard_sign_in_is_redacted_too(self):
        server.AUTH.dash.set_fields(owner_hash=security.hash_password("owner password 1"),
                                    viewer_hash=security.hash_password("viewer password 1"), login_enabled=True)
        self.c.cookies.clear()
        r = self.c.post("/api/auth/login", json={"password": "viewer password 1"})
        ck = cookie_from(r)
        self.c.cookies.clear()
        txt = self.c.get("/api/settings", headers={"Cookie": ck}).text
        self.assertNotIn("SECRET-WEBHOOK-ID", txt)
        server.AUTH.dash.set_fields(login_enabled=None, owner_hash=None, viewer_hash=None)

    def test_printer_urls_lose_their_user_and_password_for_read_callers_only(self):
        server.FLEET.add("octoprint", {"name": "pi", "url": "http://admin:hunter2@127.0.0.1:9/", "api_key": "K"}, True)
        self.addCleanup(lambda: server.FLEET.remove("pi", True))
        read = self.c.get("/api/printers", headers=self.rk)
        self.assertNotIn("hunter2", read.text)
        self.assertNotIn("admin:", read.text)
        self.assertIn("127.0.0.1:9", read.text)
        self.assertIn("hunter2", self.c.get("/api/printers", headers=self.ck).text)     # the edit form needs it
        h = self.mcp_session(self.rk)
        _r, msg = self.call(h, "list_printer_types")
        self.assertNotIn("hunter2", tool_text(msg))

    def test_strip_userinfo_unit(self):
        self.assertEqual(security.strip_userinfo("http://u:p@host:80/x?y=1"), "http://host:80/x?y=1")
        self.assertEqual(security.strip_userinfo("http://host/a@b"), "http://host/a@b")
        self.assertEqual(security.strip_userinfo("not a url"), "not a url")
        self.assertEqual(security.scrub_urls({"a": ["http://u:p@h/"], "b": 3}), {"a": ["http://h/"], "b": 3})


# =================================================================== P1-4: FTPS discipline

class FtpsDisciplineTests(Base):
    def test_a_printing_bambu_is_never_asked_for_a_file(self):
        prn = self.add_bambu(state="RUNNING")
        out = server.suggest_mapping("A1", "job.gcode.3mf")
        self.assertIn("METADATA UNAVAILABLE", out)
        server.list_sd_files("A1", refresh_metadata=True)
        server._prewarm_bambu(prn)
        for st in ("PAUSE", "PREPARE", "UNKNOWN", "SLICING"):
            prn.state = st
            server.suggest_mapping("A1", "job.gcode.3mf")
            server.list_sd_files("A1", refresh_metadata=True)
        self.assertEqual(downloads(prn), [])
        self.assertTrue(prn.calls)                                   # listings are fine

    def test_a_printing_bambu_serves_what_is_cached(self):
        prn = self.add_bambu()
        server.suggest_mapping("A1", "job.gcode.3mf")                # idle: downloads once
        self.assertEqual(len(downloads(prn)), 1)
        prn.state = "RUNNING"
        d = json.loads(server.suggest_mapping("A1", "job.gcode.3mf"))
        self.assertEqual(d["file"], "job.gcode.3mf")
        self.assertEqual(len(downloads(prn)), 1)

    def test_a_read_only_caller_never_triggers_a_download(self):
        prn = self.add_bambu()
        self.mode("on")
        rk = self.key("read")
        r = self.c.post("/api/printer/A1/suggest", headers=rk, json={"filename": "job.gcode.3mf"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("never makes the Hub download", r.json()["error"])
        r = self.c.get("/api/printer/A1/files?refresh=1", headers=rk)
        self.assertEqual(r.status_code, 200)
        h = self.mcp_session(rk)
        _r, msg = self.call(h, "suggest_mapping", {"printer": "A1", "filename": "job.gcode.3mf"})
        self.assertIn("METADATA UNAVAILABLE", tool_text(msg))
        _r, msg = self.call(h, "list_sd_files", {"printer": "A1", "refresh_metadata": True})
        REAL_SLEEP(0.3)                                              # (no prewarm thread was started either)
        self.assertEqual(downloads(prn), [])
        # a control key on an idle printer does download
        self.assertEqual(self.c.post("/api/printer/A1/suggest", headers=self.key("control"),
                                     json={"filename": "job.gcode.3mf"}).status_code, 200)
        self.assertEqual(len(downloads(prn)), 1)
        # and once cached, the read key can use it
        r = self.c.post("/api/printer/A1/suggest", headers=rk, json={"filename": "job.gcode.3mf"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(downloads(prn)), 1)

    def test_every_listing_runs_under_the_ftps_lock(self):
        prn = self.add_bambu()
        server.list_sd_files("A1")
        server.suggest_mapping("A1", "job.gcode.3mf")
        server._prewarm_bambu(prn)
        server.delete_sd_file("A1", "other.gcode.3mf", "other.gcode.3mf")
        self.assertTrue(prn.lock_held_in_list and all(prn.lock_held_in_list), prn.lock_held_in_list)

    def test_delete_needs_a_positively_idle_printer(self):
        prn = self.add_bambu(files=("a.gcode.3mf",))
        for st in ("RUNNING", "UNKNOWN", "SLICING", "PAUSE"):
            prn.state = st
            self.assertTrue(server.delete_sd_file("A1", "a.gcode.3mf", "a.gcode.3mf").startswith("REFUSED"), st)
        self.assertNotIn("DELE a.gcode.3mf", prn.calls)
        prn.state = "FINISH"
        self.assertEqual(server.delete_sd_file("A1", "a.gcode.3mf", "a.gcode.3mf"), "deleted a.gcode.3mf")


# =================================================================== small things

class MiscTests(Base):
    def test_pause_cancel_failures_are_reported_not_swallowed(self):
        class Down(FakeBambu):
            def stop(self):
                raise server.PrinterError("MQTT not connected")

            def pause(self):
                raise server.PrinterError("MQTT not connected")
        server.PRINTERS["a1"] = Down("A1")
        self.assertTrue(server.cancel_print("A1").startswith("FAILED"))
        self.assertTrue(server.pause_print("A1").startswith("FAILED"))
        self.mode("off")
        r = self.c.post("/api/printer/A1/action", json={"action": "pause"})
        self.assertEqual(r.status_code, 502)

    def test_health_stays_public_and_minimal(self):
        r = self.c.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertNotIn("path", r.text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
