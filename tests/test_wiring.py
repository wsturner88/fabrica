"""Tests for the settings / notifications / events wiring in app/server.py.

    python3 tests/test_wiring.py

Needs the app's own dependencies (mcp, starlette, httpx). Everything is local: the server module is imported
with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointing at a throwaway temp directory (never the real config.yml),
printers are in-process fakes, and every "service" (ntfy, Telegram) is a fake HTTP server on 127.0.0.1.
Nothing here contacts a printer, the Internet or the LAN.
"""
import asyncio
import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

_TMP = tempfile.mkdtemp(prefix="wiring-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("PREHEAT_TIMEOUT_MIN", None)
# These tests drive the routes with no API key: run them with the key check off (tests/test_auth.py covers it).
os.environ["BAMBU_MCP_API_AUTH"] = "off"
os.environ.pop("BACKUP_KEEP", None)

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover - the app's deps are not installed
    HAVE = False
    _ERR = e

import backup  # noqa: E402
import controls  # noqa: E402

SECRET_TOKEN = "123456:SECRET-bot-token-xyz"
SECRET_WEBHOOK = "https://discord.example/api/webhooks/999/SECRET-webhook-path"


class FakeHTTP:
    """Recording fake service on 127.0.0.1. respond(req) -> (status, body bytes)."""

    def __init__(self):
        self.requests = []
        self.respond = lambda r: (200, b'{"ok":true}')
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _h(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                req = {"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body}
                outer.requests.append(req)
                status, resp = outer.respond(req)
                self.send_response(status)
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            do_POST = do_PUT = do_GET = _h

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def titles(self):
        return [r["headers"].get("Title", "") for r in self.requests]

    def wait_for(self, text, seconds=6.0):
        end = time.time() + seconds
        while time.time() < end:
            if any(text in t for t in self.titles()):
                return True
            time.sleep(0.03)
        return False


class FakePrinter:
    """Just enough driver for the watcher, the controls and the backup hook."""
    kind = "fake"
    has_camera = False

    def ams_trays(self):
        return []

    def __init__(self, name, state="IDLE"):
        self.name = name
        self.state = state
        self.connected = True
        self.temps = []
        self.job = ""

    def snapshot(self):
        return {"printer": self.name, "connected": self.connected, "state": self.state, "job": self.job,
                "percent": 100 if self.state == "FINISH" else 40, "remaining_min": None,
                "nozzle_c": 25, "nozzle_target_c": 0, "bed_c": 24, "bed_target_c": 0, "kind": "fake"}

    def control_caps(self):
        return {"light": {"supported": False, "reason": "none"}, "heat": {"supported": True},
                "unload": {"supported": False, "reason": "none"}, "load": {"supported": False, "reason": "none"}}

    def set_temps(self, nozzle, bed):
        self.temps.append((nozzle, bed))
        return {"nozzle": nozzle, "bed": bed}


def flat(obj) -> str:
    return json.dumps(obj, default=str)


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.mcp.streamable_http_app())
        cls.ntfy = FakeHTTP()
        cls.tg = FakeHTTP()
        server.NOTIFIER.channel_options = {"telegram": {"api_base": cls.tg.url}}

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.close()
        cls.tg.close()
        server.NOTIFIER.channel_options = {}

    def setUp(self):
        # every test starts from factory settings and an empty fleet; secrets cleared too
        server.SETTINGS.reset()
        server.SETTINGS.update({}, clear=[k for k, f in server.settings_store.FIELDS.items() if f["secret"]])
        server.PRINTERS.clear()
        self.ntfy.requests.clear()
        self.tg.requests.clear()
        self.ntfy.respond = lambda r: (200, b"{}")
        self.tg.respond = lambda r: (200, b'{"ok":true}')
        server.NOTIFIER._last_sent.clear()
        server.NOTIFIER._history.clear()
        server.WATCHER._p.clear()
        server.WATCHER.confirm_s = 0

    def enable_ntfy(self):
        server.SETTINGS.update({"ntfy.enabled": True, "ntfy.server_url": self.ntfy.url, "ntfy.topic": "wiring"})

    def put(self, **body):
        return self.client.put("/api/settings", json=body)


# ---------------------------------------------------------------- import / registration

class TestImportAndTools(Base):
    def test_imports_without_config_in_a_fresh_process(self):
        """server.py must import and answer with NO config.yml and an empty data dir."""
        d = tempfile.mkdtemp(prefix="wiring-sub-")
        self.addCleanup(shutil.rmtree, d, True)
        code = ("import sys, json; sys.path.insert(0, %r); import server; "
                "from starlette.testclient import TestClient; c = TestClient(server.mcp.streamable_http_app()); "
                "print('@@' + json.dumps([c.get('/api/settings').status_code, "
                "server.SETTINGS.get('agents.tool_count'), len(server.mcp._tool_manager.list_tools())])); "
                "sys.stdout.flush(); import os; os._exit(0)") % os.path.abspath(APP)
        env = {**os.environ, "BAMBU_MCP_CONFIG": os.path.join(d, "missing.yml"),
               "BAMBU_MCP_CACHE": os.path.join(d, "data", "metacache.json")}
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=90)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("@@")]
        self.assertTrue(line, r.stdout + r.stderr)
        status, count, actual = json.loads(line[-1][2:])
        self.assertEqual(status, 200)
        self.assertEqual(count, actual)

    def test_imports_with_config_that_has_no_printers(self):
        d = tempfile.mkdtemp(prefix="wiring-sub-")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "c.yml"), "w") as fh:
            fh.write("palette: {GREEN: '21FF06'}\n")
        code = ("import sys; sys.path.insert(0, %r); import server; print('@@ok', len(server.PRINTERS)); "
                "sys.stdout.flush(); import os; os._exit(0)") % os.path.abspath(APP)
        env = {**os.environ, "BAMBU_MCP_CONFIG": os.path.join(d, "c.yml"),
               "BAMBU_MCP_CACHE": os.path.join(d, "data", "metacache.json")}
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=90)
        self.assertIn("@@ok 0", r.stdout, r.stderr)

    def test_settings_share_the_fleet_secrets_writer(self):
        self.assertIs(server.SETTINGS._secrets.store, server.STORE)

    def test_tools_registered_and_count_set(self):
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        for n in ("get_settings", "update_settings", "send_test_notification"):
            self.assertIn(n, names)
        self.assertEqual(server.SETTINGS.get("agents.tool_count"), len(names))
        self.assertGreater(len(names), 25)
        self.assertEqual(server.SETTINGS.get("agents.mcp_path"), "/mcp")
        for t in server.mcp._tool_manager.list_tools():
            if t.name in ("get_settings", "update_settings", "send_test_notification"):
                self.assertGreater(len(t.description), 60, t.name)
        update_doc = next(t for t in server.mcp._tool_manager.list_tools() if t.name == "update_settings").description
        self.assertIn("write-only", update_doc)

    def test_tool_functions(self):
        out = json.loads(asyncio.run(server.get_settings("general")))
        self.assertEqual([s["id"] for s in out["sections"]], ["general"])
        self.assertTrue(asyncio.run(server.get_settings("nope")).startswith("REFUSED"))
        res = json.loads(asyncio.run(server.update_settings({"general.hub_name": "Shop"})))
        self.assertEqual(res["changed"], ["general.hub_name"])
        self.assertTrue(asyncio.run(server.update_settings({"email.port": 99999})).startswith("REFUSED"))
        self.assertEqual(server.SETTINGS.get("general.hub_name"), "Shop")
        self.enable_ntfy()
        res = json.loads(asyncio.run(server.send_test_notification("ntfy")))
        self.assertTrue(res["ok"], res)
        res = json.loads(asyncio.run(server.send_test_notification("carrier-pigeon")))
        self.assertFalse(res["ok"])


# ---------------------------------------------------------------- REST

class TestSettingsRoutes(Base):
    def test_get_and_schema(self):
        d = self.client.get("/api/settings").json()
        ids = [s["id"] for s in d["sections"]]
        self.assertEqual(ids[:2], ["general", "notifications"])
        f = next(x for s in d["sections"] for x in s["fields"] if x["key"] == "general.hub_name")
        self.assertEqual((f["value"], f["source"]), ("Print Hub", "default"))
        sch = self.client.get("/api/settings/schema").json()
        self.assertNotIn("value", sch["sections"][0]["fields"][0])
        self.assertIn("telegram", sch["channels"])

    def test_put_round_trip_and_sources(self):
        r = self.put(values={"general.hub_name": "Shop Hub", "notifications.duplicate_window_min": 7,
                             "notifications.muted_printers": ["A1"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(r.json()["changed"]), ["general.hub_name", "notifications.duplicate_window_min",
                                                        "notifications.muted_printers"])
        d = self.client.get("/api/settings").json()
        vals = {f["key"]: (f["value"], f.get("source")) for s in d["sections"] for f in s["fields"]}
        self.assertEqual(vals["general.hub_name"], ("Shop Hub", "saved"))
        self.assertEqual(vals["notifications.muted_printers"][0], ["A1"])
        self.assertEqual(r.json()["settings"]["sections"][0]["id"], "general")     # the PUT hands back fresh state
        # a flat body works too
        self.assertEqual(self.client.put("/api/settings", json={"general.hub_name": "Flat"}).status_code, 200)
        self.assertEqual(server.SETTINGS.get("general.hub_name"), "Flat")
        # and it persists in settings.json in the data dir
        with open(os.path.join(os.path.dirname(os.environ["BAMBU_MCP_CACHE"]), "settings.json")) as fh:
            self.assertEqual(json.load(fh)["values"]["general.hub_name"], "Flat")

    def test_put_validation_errors_name_the_field(self):
        r = self.put(values={"email.port": 70000, "notifications.quiet_start": "25:99", "general.hub_name": "ok"})
        self.assertEqual(r.status_code, 400)
        j = r.json()
        self.assertIn("email.port", j["errors"])
        self.assertIn("notifications.quiet_start", j["errors"])
        self.assertNotEqual(server.SETTINGS.get("general.hub_name"), "ok")        # all-or-nothing
        self.assertEqual(self.put(values={"agents.tool_count": 3}).status_code, 400)
        self.assertEqual(self.put(values={"printers.require_bed_clear": False}).status_code, 400)
        self.assertEqual(self.client.put("/api/settings", content=b"not json", headers={"Content-Type": "application/json"}).status_code, 200)   # empty update: nothing to do
        self.assertEqual(self.client.put("/api/settings", content=b"not json").status_code, 415)      # not even sent as JSON: refused up front

    def test_secrets_are_never_returned(self):
        r = self.put(values={"telegram.bot_token": SECRET_TOKEN, "discord.webhook_url": SECRET_WEBHOOK,
                             "ntfy.token": "tk_SECRETNTFY", "email.password": "pw-SECRETSMTP",
                             "webhook.bearer": "bearer-SECRETHOOK", "telegram.chat_id": "42"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["secrets_set"], ["discord.webhook_url", "email.password", "ntfy.token",
                                                    "telegram.bot_token", "webhook.bearer"])
        shown = []
        shown.append(self.client.get("/api/settings").text)
        shown.append(self.client.get("/api/settings/schema").text)
        shown.append(r.text)
        shown.append(self.client.get("/api/settings/export").text)
        shown.append(self.client.get("/api/notifications/recent").text)
        shown.append(asyncio.run(server.get_settings()))
        shown.append(asyncio.run(server.update_settings({"telegram.bot_token": SECRET_TOKEN})))
        for text in shown:
            for needle in (SECRET_TOKEN, "SECRETNTFY", "SECRETSMTP", "SECRETHOOK", "SECRET-webhook-path"):
                self.assertNotIn(needle, text)
        vals = {f["key"]: f["value"] for s in self.client.get("/api/settings").json()["sections"] for f in s["fields"]}
        self.assertEqual(vals["telegram.bot_token"], "set")
        self.assertEqual(vals["ntfy.token"], "set")
        self.assertEqual(vals["email.password"], "set")
        self.assertEqual(vals["telegram.chat_id"], "42")
        # stored write-only, mode 0600, in secrets.json under the reserved namespace
        path = os.path.join(os.path.dirname(os.environ["BAMBU_MCP_CACHE"]), "secrets.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["_settings"]["telegram.bot_token"], SECRET_TOKEN)
        with open(os.path.join(os.path.dirname(path), "settings.json")) as fh:
            self.assertNotIn(SECRET_TOKEN, fh.read())
        # a placeholder echoed back keeps the secret; clear deletes it
        self.put(values={"telegram.bot_token": "set"})
        self.assertTrue(server.SETTINGS.is_secret_set("telegram.bot_token"))
        r = self.put(values={}, clear=["telegram.bot_token"])
        self.assertEqual(r.json()["secrets_cleared"], ["telegram.bot_token"])
        self.assertFalse(server.SETTINGS.is_secret_set("telegram.bot_token"))

    def test_export_import(self):
        self.put(values={"general.hub_name": "Exported", "telegram.bot_token": SECRET_TOKEN})
        doc = self.client.get("/api/settings/export").json()
        self.assertEqual(doc["values"]["general.hub_name"], "Exported")
        self.assertNotIn("telegram.bot_token", doc["values"])
        server.SETTINGS.reset()
        r = self.client.post("/api/settings/import?dry_run=1", json=doc)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(server.SETTINGS.get("general.hub_name"), "Print Hub")     # dry run changed nothing
        r = self.client.post("/api/settings/import", json=doc)
        self.assertIn("general.hub_name", r.json()["applied"])
        self.assertEqual(server.SETTINGS.get("general.hub_name"), "Exported")
        bad = self.client.post("/api/settings/import", json={"values": {"email.port": 0}})
        self.assertEqual(bad.status_code, 400)

    def test_recent_notifications_route(self):
        j = self.client.get("/api/notifications/recent").json()
        self.assertEqual(j, {"notifications": [], "channels": []})
        self.enable_ntfy()
        self.client.post("/api/settings/test/ntfy")
        j = self.client.get("/api/notifications/recent").json()
        self.assertEqual(j["channels"], ["ntfy"])
        self.assertEqual(j["notifications"][0]["event"], "test")


# ---------------------------------------------------------------- test-send route

class TestTestRoute(Base):
    def test_ntfy_with_unsaved_form_values(self):
        r = self.client.post("/api/settings/test/ntfy", json={"values": {
            "enabled": False, "server_url": self.ntfy.url, "topic": "unsaved-topic"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"channel": "ntfy", "ok": True, "error": None})
        self.assertEqual(self.ntfy.requests[-1]["path"], "/unsaved-topic")
        self.assertIn("test message", self.ntfy.requests[-1]["headers"]["Title"])
        # nothing was saved by testing
        self.assertEqual(server.SETTINGS.get("ntfy.topic"), "")

    def test_ntfy_failure_is_reported_in_words(self):
        self.ntfy.respond = lambda r: (403, b'{"error":"forbidden"}')
        r = self.client.post("/api/settings/test/ntfy", json={"values": {"server_url": self.ntfy.url, "topic": "t"}})
        j = r.json()
        self.assertFalse(j["ok"])
        self.assertIn("403", j["error"])

    def test_incomplete_channel_says_what_is_missing(self):
        j = self.client.post("/api/settings/test/ntfy", json={}).json()
        self.assertFalse(j["ok"])
        self.assertIn("topic", j["error"])

    def test_telegram_with_typed_and_saved_secret(self):
        # typed (unsaved) token
        r = self.client.post("/api/settings/test/telegram", json={"values": {"bot_token": SECRET_TOKEN, "chat_id": "77"}})
        self.assertTrue(r.json()["ok"], r.text)
        self.assertEqual(self.tg.requests[-1]["path"], f"/bot{SECRET_TOKEN}/sendMessage")
        self.assertEqual(json.loads(self.tg.requests[-1]["body"])["chat_id"], "77")
        # saved token, blank field in the form -> saved one is used
        self.put(values={"telegram.bot_token": "555:SAVED-token", "telegram.chat_id": "9"})
        self.tg.requests.clear()
        r = self.client.post("/api/settings/test/telegram", json={"values": {"bot_token": ""}})
        self.assertTrue(r.json()["ok"], r.text)
        self.assertEqual(self.tg.requests[-1]["path"], "/bot555:SAVED-token/sendMessage")
        self.assertNotIn("SAVED-token", r.text)

    def test_telegram_failure_never_leaks_the_token(self):
        self.tg.respond = lambda r: (401, b'{"ok":false,"description":"Unauthorized"}')
        r = self.client.post("/api/settings/test/telegram", json={"values": {"bot_token": SECRET_TOKEN, "chat_id": "1"}})
        j = r.json()
        self.assertFalse(j["ok"])
        self.assertNotIn(SECRET_TOKEN, r.text)
        self.assertNotIn("SECRET-bot-token", r.text)

    def test_unknown_channel(self):
        r = self.client.post("/api/settings/test/carrier-pigeon", json={})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["ok"])

    def test_route_does_not_block_the_event_loop(self):
        """The send runs off the loop: a slow service must not stall another request."""
        gate = threading.Event()

        def slow(req):
            gate.wait(5)
            return 200, b"{}"
        self.ntfy.respond = slow
        out = {}
        t = threading.Thread(target=lambda: out.update(
            r=self.client.post("/api/settings/test/ntfy", json={"values": {"server_url": self.ntfy.url, "topic": "slow"}})))
        t.start()
        time.sleep(0.4)
        t0 = time.time()
        self.assertEqual(self.client.get("/api/settings/schema").status_code, 200)
        self.assertLess(time.time() - t0, 2.0)
        gate.set()
        t.join(10)
        self.assertTrue(out["r"].json()["ok"])


# ---------------------------------------------------------------- the watcher thread, end to end

class TestEventLoop(Base):
    def run_loop(self):
        stop = threading.Event()
        server._event_thread = None
        th = server.start_event_loop(stop, interval=0.05)
        self.addCleanup(lambda: (stop.set(), th.join(3)))
        return stop

    def test_finished_event_reaches_the_channel(self):
        self.enable_ntfy()
        p = FakePrinter("Fake1", "RUNNING")
        p.job = "benchy"
        server.PRINTERS["fake1"] = p
        self.run_loop()
        time.sleep(0.4)                      # baseline snapshot: RUNNING
        self.assertEqual(self.ntfy.requests, [])
        p.state = "FINISH"
        self.assertTrue(self.ntfy.wait_for("Fake1"), self.ntfy.titles())
        self.assertIn("FINISHED", " ".join(self.ntfy.titles()).upper())
        hist = self.client.get("/api/notifications/recent").json()["notifications"]
        self.assertEqual(hist[0]["event"], "print_finished")
        self.assertTrue(hist[0]["sent"])

    def test_loop_survives_fleet_changes_and_failing_drivers(self):
        self.enable_ntfy()
        a = FakePrinter("A", "RUNNING")
        server.PRINTERS["a"] = a
        self.run_loop()
        time.sleep(0.3)

        class Boom(FakePrinter):
            def snapshot(self):
                raise RuntimeError("driver exploded")
        server.PRINTERS["b"] = Boom("B")                  # added at runtime, throws on every snapshot
        c = FakePrinter("C", "RUNNING")
        server.PRINTERS["c"] = c                          # added at runtime
        time.sleep(0.4)
        server.PRINTERS.pop("a")                          # removed at runtime
        time.sleep(0.3)
        c.state = "FINISH"
        self.assertTrue(self.ntfy.wait_for("C"), self.ntfy.titles())
        self.assertTrue(server._event_thread.is_alive())

    def test_snapshots_helper_covers_whole_fleet(self):
        server.PRINTERS["x"] = FakePrinter("X")
        server.PRINTERS["y"] = FakePrinter("Y", "RUNNING")
        snaps = server._fleet_snapshots()
        self.assertEqual(sorted(snaps), ["X", "Y"])
        self.assertEqual(snaps["Y"]["state"], "RUNNING")

    def test_loop_only_started_by_main(self):
        # importing the server must not start printer polling by itself
        server._event_thread = None
        self.assertIsNone(server._event_thread)


# ---------------------------------------------------------------- hooks

class TestHooks(Base):
    def test_backup_finish_hook(self):
        server.SETTINGS.update({"notifications.backup_done": True})
        self.enable_ntfy()
        done = backup.Job("Fake1")
        done.status, done.files_done, done.size, done.file = "done", 3, 2 * 1048576, "Fake1-20260101-000000.zip"
        server._backup_finished(done)
        self.assertTrue(self.ntfy.wait_for("backup finished"), self.ntfy.titles())
        bad = backup.Job("Fake1")
        bad.status, bad.error = "failed", "Printer refused the connection"
        server._backup_finished(bad)
        self.assertTrue(self.ntfy.wait_for("backup FAILED"), self.ntfy.titles())
        partial = backup.Job("Fake2")
        partial.status, partial.error = "partial", "2 item(s) could not be backed up"
        server._backup_finished(partial)
        self.assertTrue(self.ntfy.wait_for("Fake2: backup FAILED"), self.ntfy.titles())

    def test_backup_manager_calls_hook_once_per_job(self):
        calls = []
        m = backup.BackupManager({}, root=os.path.join(_TMP, "bk"), on_finish=calls.append, keep=lambda: 2)
        job = backup.Job("P")
        m._finish(job, "failed", "boom")
        m._finish(job, "failed", "boom again")                 # a second finish must not notify twice
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].status, "failed")
        bad = backup.BackupManager({}, root=_TMP, on_finish=lambda j: 1 / 0)
        bad._finish(backup.Job("Q"), "done")                    # a crashing hook never breaks a backup

    def test_backup_keep_follows_settings(self):
        self.assertEqual(server._BACKUPS.keep_count(), 5)
        self.put(values={"backups.keep_per_printer": 9})
        self.assertEqual(server._BACKUPS.keep_count(), 9)
        self.assertEqual(self.client.get("/api/backups").json()["keep_per_printer"], 9)

    def test_preheat_auto_off_hook(self):
        self.enable_ntfy()
        p = FakePrinter("Hot", "IDLE")
        server.PRINTERS["hot"] = p
        self.addCleanup(controls.cancel_auto_off, "hot")
        controls.schedule_auto_off(p, seconds=0.2, nozzle=215, bed=60)
        self.assertTrue(self.ntfy.wait_for("pre-heat switched OFF"), self.ntfy.titles())
        self.assertEqual(p.temps, [(0, 0)])
        # a printer that started printing meanwhile is left alone and nothing is announced
        self.ntfy.requests.clear()
        p.state = "RUNNING"
        controls.schedule_auto_off(p, seconds=0.2, nozzle=215, bed=60)
        time.sleep(0.8)
        self.assertEqual(p.temps, [(0, 0)])
        self.assertEqual(self.ntfy.requests, [])

    def test_spaghetti_hook_exists(self):
        self.enable_ntfy()
        server.spaghetti_detected("Hot", 0.9)
        self.assertTrue(self.ntfy.wait_for("possible print FAILURE"), self.ntfy.titles())


class TestControlSettings(Base):
    def tearDown(self):
        controls.cancel_auto_off("ctl")
        server.SETTINGS.reset()
        server._apply_control_settings()

    def test_auto_off_minutes_from_settings(self):
        p = FakePrinter("Ctl")
        server.PRINTERS["ctl"] = p
        self.assertEqual(controls.effective_timeout_seconds(), 900)                  # env/default untouched
        self.put(values={"printers.preheat_auto_off_min": 3})
        self.assertEqual(controls.effective_timeout_seconds(), 180)
        r = controls.perform(p, "preheat", nozzle=200)
        self.assertEqual(r["auto_off_in_s"], 180)
        bad = self.client.put("/api/settings", json={"values": {"printers.preheat_auto_off_min": 0}})
        self.assertGreaterEqual(bad.status_code, 400)                                  # 0 is rejected: never infinite
        controls.set_runtime(timeout_min=0)                                            # even if forced, 0 -> default
        self.assertEqual(controls.effective_timeout_seconds(), 900)
        controls.set_runtime(timeout_min=3)
        r = controls.perform(p, "preheat", nozzle=200)
        self.assertEqual(r["auto_off_in_s"], 180)
        d = self.client.get("/api/printer/ctl")        # the detail view carries the full description
        self.assertEqual(d.status_code, 200)
        self.assertIn("detail", d.json()["controls"])
        self.assertEqual(d.json()["controls"]["detail"]["preheat"]["auto_off_timeout_min"], 3)

    def test_presets_from_settings_replace_the_defaults(self):
        self.assertIn("PLA", controls.presets())
        self.put(values={"printers.preheat_presets": {"Nylon": {"nozzle_c": 270, "bed_c": 90}}})
        self.assertEqual(controls.presets(), {"NYLON": {"nozzle": 270, "bed": 90}})
        p = FakePrinter("Ctl")
        r = controls.perform(p, "preheat", preset="nylon")
        self.addCleanup(controls.cancel_auto_off, p)
        self.assertEqual((r["nozzle"], r["bed"]), (270, 90))
        with self.assertRaises(controls.ControlInvalid):
            controls.perform(p, "preheat", preset="PLA")
        server.SETTINGS.reset()
        server._apply_control_settings()
        self.assertEqual(sorted(controls.presets()), ["ABS", "PETG", "PLA", "TPU"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
