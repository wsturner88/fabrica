"""Tests for app/fleet_store.py and the printer-management routes/tools in server.py.

    python3 tests/test_fleet_store.py          (needs: paho-mqtt; the server tests also need mcp+httpx)

Loopback only - nothing here touches a real printer or any other host:
  * OctoPrint is a throwaway HTTP server on 127.0.0.1;
  * Bambu is a tiny in-process MQTT 3.1.1 broker over TLS on 127.0.0.1 (self-signed
    cert made with the openssl CLI; the tests skip if it is missing). It speaks just
    enough of the protocol for paho: CONNECT (checks user bblp + access code),
    SUBSCRIBE, PUBLISH (answers a pushall with a status report), PING, DISCONNECT -
    and counts live clients per client-id so the "never two live MQTT clients for one
    printer" rule is checked against a real socket, not a mock.
"""
import http.server
import json
import os
import shutil
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

import fleet_store  # noqa: E402
from bambu import BambuPrinter  # noqa: E402

try:
    import paho.mqtt.client  # noqa: F401
    HAVE_PAHO = True
except ImportError:
    HAVE_PAHO = False

OCTO_KEY = "octo-test-key-123"
BAMBU_CODE = "12345678"
BAMBU_SERIAL = "01P00A000000001"          # fake; prefix 01P = P1S


def slurp(path):
    with open(path) as fh:
        return fh.read()


def wait_for(cond, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


# ----------------------------------------------------------------- fake OctoPrint

class FakeOcto(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("X-Api-Key") != OCTO_KEY:
            return self._send(403, {"error": "forbidden"})
        if self.path.startswith("/api/version"):
            return self._send(200, {"api": "0.1", "server": "1.10.0", "text": "OctoPrint 1.10.0"})
        if self.path.startswith("/api/settings"):
            return self._send(200, {"webcam": {}, "plugins": {}})
        if self.path.startswith("/api/printer"):
            return self._send(200, {"state": {"flags": {"operational": True}}, "temperature": {}})
        if self.path.startswith("/api/job"):
            return self._send(200, {"job": {"file": {}}, "progress": {}})
        self._send(404, {"error": "nope"})


class NotOcto(http.server.BaseHTTPRequestHandler):
    """Answers HTTP but is not OctoPrint."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def closed_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ----------------------------------------------------------------- fake Bambu MQTT broker

class FakeBroker:
    def __init__(self, certfile, keyfile, code=BAMBU_CODE, serial=BAMBU_SERIAL, answer=True):
        import socket
        self.code, self.serial, self.answer = code, serial, answer
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(certfile, keyfile)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()
        self.live = {}          # client_id -> live connection count
        self.max_live = {}      # client_id -> highest concurrent count ever seen
        self.connects = 0
        self.bad_logins = 0
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass

    def total_live(self):
        with self.lock:
            return sum(self.live.values())

    def _accept(self):
        while not self._stop:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    @staticmethod
    def _recv(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _packet(self, conn):
        b0 = self._recv(conn, 1)[0]
        mult, length = 1, 0
        while True:
            d = self._recv(conn, 1)[0]
            length += (d & 127) * mult
            if not d & 128:
                break
            mult *= 128
        return b0, self._recv(conn, length)

    @staticmethod
    def _publish_packet(topic, payload):
        t = topic.encode()
        body = struct.pack(">H", len(t)) + t + payload
        rl, out = len(body), b""
        while True:
            d, rl = rl % 128, rl // 128
            out += bytes([d | (128 if rl else 0)])
            if not rl:
                break
        return bytes([0x30]) + out + body

    def _serve(self, raw):
        cid, subscribed = None, set()
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        try:
            while True:
                b0, body = self._packet(conn)
                kind = b0 >> 4
                if kind == 1:                                   # CONNECT
                    i = 2 + struct.unpack(">H", body[:2])[0] + 4
                    n = struct.unpack(">H", body[i:i + 2])[0]
                    cid = body[i + 2:i + 2 + n].decode()
                    flags = body[2 + struct.unpack(">H", body[:2])[0] + 1]
                    i += 2 + n
                    user = pw = None
                    if flags & 0x80:
                        n = struct.unpack(">H", body[i:i + 2])[0]
                        user = body[i + 2:i + 2 + n].decode()
                        i += 2 + n
                    if flags & 0x40:
                        n = struct.unpack(">H", body[i:i + 2])[0]
                        pw = body[i + 2:i + 2 + n].decode()
                    if user == "bblp" and pw == self.code:
                        with self.lock:
                            self.connects += 1
                            self.live[cid] = self.live.get(cid, 0) + 1
                            self.max_live[cid] = max(self.max_live.get(cid, 0), self.live[cid])
                        conn.sendall(bytes([0x20, 2, 0, 0]))
                    else:
                        with self.lock:
                            self.bad_logins += 1
                        conn.sendall(bytes([0x20, 2, 0, 5]))      # not authorized
                        cid = None
                        return
                elif kind == 8:                                  # SUBSCRIBE
                    pid = body[:2]
                    n = struct.unpack(">H", body[2:4])[0]
                    subscribed.add(body[4:4 + n].decode())
                    conn.sendall(bytes([0x90, 3]) + pid + bytes([0]))
                elif kind == 3:                                  # PUBLISH (QoS 0 from paho)
                    n = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2 + n].decode()
                    report = f"device/{self.serial}/report"
                    if self.answer and topic == f"device/{self.serial}/request" and report in subscribed:
                        conn.sendall(self._publish_packet(report, json.dumps(
                            {"print": {"gcode_state": "IDLE", "sw_ver": "01.07.00.00"}}).encode()))
                elif kind == 12:                                 # PINGREQ
                    conn.sendall(bytes([0xD0, 0]))
                elif kind == 14:                                 # DISCONNECT
                    return
        except (ConnectionError, OSError, ssl.SSLError, struct.error):
            return
        finally:
            if cid is not None:
                with self.lock:
                    self.live[cid] = max(0, self.live.get(cid, 1) - 1)
            try:
                conn.close()    # wrap_socket() detached `raw`; the TLS socket is what must be closed
            except OSError:
                pass


def make_cert(d):
    cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                        "-out", cert, "-days", "2", "-subj", "/CN=localhost"],
                       capture_output=True)
    return (cert, key) if r.returncode == 0 else None


# ----------------------------------------------------------------- store

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_round_trip_and_secret_separation(self):
        st = fleet_store.FleetStore(self.dir)
        st.add({"name": "P1S", "type": "bambu", "ip": "10.0.0.5", "serial": "S1", "model": "m"},
               {"access_code": "supersecret"})
        again = fleet_store.FleetStore(self.dir)        # fresh process view of the same files
        rec = again.get("p1s")                           # case-insensitive lookup
        self.assertEqual(rec["name"], "P1S")
        self.assertEqual(rec["ip"], "10.0.0.5")
        self.assertEqual(again.secret("P1S", "access_code"), "supersecret")
        with open(os.path.join(self.dir, "printers.json")) as fh:
            self.assertNotIn("supersecret", fh.read())

    def test_secrets_file_mode_and_never_in_public_output(self):
        st = fleet_store.FleetStore(self.dir)
        st.add({"name": "A", "type": "octoprint", "url": "http://x"}, {"api_key": "KEY-DO-NOT-LEAK"})
        mode = stat.S_IMODE(os.stat(os.path.join(self.dir, "secrets.json")).st_mode)
        self.assertEqual(mode, 0o600)
        mgr = fleet_store.FleetManager(st, {}, log=lambda *a: None)
        blob = json.dumps(mgr.list_public()) + json.dumps(mgr.types())
        self.assertNotIn("KEY-DO-NOT-LEAK", blob)
        self.assertEqual(mgr.list_public()[0]["secrets"], {"api_key": "set"})
        # still 0600 after an update rewrite
        st.update("A", {"model": "x"}, {"api_key": "ANOTHER"})
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "secrets.json")).st_mode), 0o600)

    def test_atomic_write_keeps_old_file_on_failure(self):
        st = fleet_store.FleetStore(self.dir)
        st.add({"name": "A", "type": "bambu", "ip": "1.1.1.1", "serial": "S"}, {"access_code": "c1"})
        path = os.path.join(self.dir, "printers.json")
        before = slurp(path)
        real = os.replace

        def boom(src, dst):
            raise OSError("disk full")
        fleet_store.os.replace = boom
        try:
            with self.assertRaises(OSError):
                st.add({"name": "B", "type": "bambu", "ip": "2.2.2.2", "serial": "S"}, {"access_code": "c2"})
        finally:
            fleet_store.os.replace = real
        self.assertEqual(slurp(path), before)                   # old file intact
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])   # no litter
        self.assertIsNone(st.get("B"))                                # memory rolled back too
        self.assertIsNone(st.secret("B", "access_code") or None)

    def test_name_validation(self):
        for good in ("P1S", "Garage-MK4", "a", "x_y.z", "A" * 32):
            self.assertEqual(fleet_store.validate_name(good), good)
        for bad in ("", "  ", "has space", "../x", "a/b", "quo'te", "A" * 33, "-lead", ".dot", "all", "ALL", None):
            with self.assertRaises(fleet_store.FleetError, msg=repr(bad)):
                fleet_store.validate_name(bad)
        st = fleet_store.FleetStore(self.dir)
        st.add({"name": "P1S", "type": "bambu"}, {})
        with self.assertRaises(fleet_store.Conflict):
            st.add({"name": "p1s", "type": "bambu"}, {})              # unique, case-insensitive

    def test_corrupt_file_is_moved_aside_not_lost(self):
        with open(os.path.join(self.dir, "printers.json"), "w") as fh:
            fh.write("{not json")
        st = fleet_store.FleetStore(self.dir)
        self.assertEqual(st.records(), [])
        self.assertTrue(any(".corrupt-" in f for f in os.listdir(self.dir)))


class LegacyImportTests(unittest.TestCase):
    CFG = [
        {"name": "P1S", "model": "Bambu Lab P1S", "ip": "192.0.2.10", "serial": "SER1",
         "access_code_env": "T_P1S_CODE"},
        {"name": "A1", "model": "Bambu Lab A1", "ip": "192.0.2.11", "serial": "SER2",
         "access_code_env": "T_A1_CODE"},
        {"name": "MK4", "type": "octoprint", "url": "http://192.0.2.12/", "model": "Prusa MK4S",
         "api_key_env": "T_MK4_KEY"},
    ]
    ENV = {"T_P1S_CODE": "code-p1s", "T_A1_CODE": "code-a1", "T_MK4_KEY": "key-mk4"}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_import_resolves_env_into_secrets(self):
        st = fleet_store.FleetStore(self.dir, env=self.ENV)
        self.assertEqual(st.import_legacy(self.CFG, env=self.ENV), ["P1S", "A1", "MK4"])
        self.assertEqual(st.secret("P1S", "access_code"), "code-p1s")
        self.assertEqual(st.secret("MK4", "api_key"), "key-mk4")
        self.assertEqual(st.get("MK4")["url"], "http://192.0.2.12")
        self.assertEqual(st.get("MK4")["type"], "octoprint")
        self.assertEqual(st.get("A1")["serial"], "SER2")
        # secrets are in secrets.json (0600), not printers.json
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "secrets.json")).st_mode), 0o600)
        self.assertNotIn("code-p1s", slurp(os.path.join(self.dir, "printers.json")))
        with open(os.path.join(self.dir, "secrets.json")) as fh:
            self.assertEqual(json.load(fh)["P1S"], {"access_code": "code-p1s"})

    def test_survives_restart_without_env_and_config(self):
        fleet_store.FleetStore(self.dir, env=self.ENV).import_legacy(self.CFG, env=self.ENV)
        later = fleet_store.FleetStore(self.dir, env={})      # no .env, no config any more
        self.assertEqual([r["name"] for r in later.records()], ["P1S", "A1", "MK4"])
        self.assertEqual(later.secret("A1", "access_code"), "code-a1")

    def test_never_reimports(self):
        st = fleet_store.FleetStore(self.dir, env=self.ENV)
        st.import_legacy(self.CFG, env=self.ENV)
        # non-empty store: a changed config is ignored
        again = fleet_store.FleetStore(self.dir, env=self.ENV)
        self.assertEqual(again.import_legacy([{"name": "NEW", "ip": "1.2.3.4", "serial": "S"}], env=self.ENV), [])
        self.assertIsNone(again.get("NEW"))
        # empty again (user removed everything): still no resurrection
        for n in ("P1S", "A1", "MK4"):
            again.remove(n)
        third = fleet_store.FleetStore(self.dir, env=self.ENV)
        self.assertEqual(third.import_legacy(self.CFG, env=self.ENV), [])
        self.assertEqual(third.records(), [])

    def test_missing_env_is_not_lost_later(self):
        st = fleet_store.FleetStore(self.dir, env={})
        st.import_legacy(self.CFG[:1], env={})
        self.assertFalse(st.secret_is_set("P1S", "access_code"))
        st.env = {"T_P1S_CODE": "late-code"}                  # .env fixed afterwards
        self.assertEqual(st.secret("P1S", "access_code"), "late-code")

    def test_bad_entries_skipped(self):
        st = fleet_store.FleetStore(self.dir, env={})
        got = st.import_legacy([{"name": "ok1", "ip": "1.1.1.1", "serial": "S"},
                                {"name": "", "ip": "x"}, {"name": "w", "type": "weird"}, "junk"], env={})
        self.assertEqual(got, ["ok1"])


# ----------------------------------------------------------------- octoprint via the manager

class OctoManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = serve(FakeOcto)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.printers = {}
        self.mgr = fleet_store.FleetManager(fleet_store.FleetStore(self.dir), self.printers, log=lambda *a: None)
        self.addCleanup(lambda: [fleet_store.stop_driver(p) for p in list(self.printers.values())])
        old = fleet_store.PROBE_TIMEOUT
        fleet_store.PROBE_TIMEOUT = 2.0
        self.addCleanup(setattr, fleet_store, "PROBE_TIMEOUT", old)

    def vals(self, **kw):
        return {"name": "MK4", "url": self.url, "api_key": OCTO_KEY, **kw}

    def test_probe_ok_and_failures(self):
        ok = self.mgr.test("octoprint", self.vals())
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["firmware"], "1.10.0")
        self.assertIn("Connected", ok["message"])
        bad = self.mgr.test("octoprint", self.vals(api_key="wrong"))
        self.assertFalse(bad["ok"])
        self.assertIn("API key", bad["message"])
        down = self.mgr.test("octoprint", self.vals(url=f"http://127.0.0.1:{closed_port()}"))
        self.assertFalse(down["ok"])
        self.assertIn("Can't reach", down["message"])
        other = serve(NotOcto)
        try:
            no = self.mgr.test("octoprint", self.vals(url=f"http://127.0.0.1:{other.server_address[1]}"))
        finally:
            other.shutdown()
            other.server_close()
        self.assertFalse(no["ok"])
        self.assertIn("does not look like OctoPrint", no["message"])

    def test_add_edit_remove_lifecycle(self):
        res = self.mgr.add("octoprint", self.vals(model="Test Model"))
        self.assertTrue(res["test"]["ok"])
        self.assertIn("mk4", self.printers)
        self.assertEqual(self.printers["mk4"].api_key, OCTO_KEY)
        self.assertNotIn(OCTO_KEY, json.dumps(res))
        self.assertEqual(res["printer"]["secrets"], {"api_key": "set"})
        first = self.printers["mk4"]
        # edit: blank secret keeps the key; model change swaps in a new driver instance
        up = self.mgr.update("MK4", {"model": "New Model", "api_key": ""})
        self.assertEqual(up["changed"], ["model"])
        self.assertIsNot(self.printers["mk4"], first)
        self.assertEqual(self.printers["mk4"].model, "New Model")
        self.assertEqual(self.printers["mk4"].api_key, OCTO_KEY)
        # edit to a bad key is refused by the test and leaves everything as it was
        keep = self.printers["mk4"]
        with self.assertRaises(fleet_store.ProbeFailed):
            self.mgr.update("MK4", {"api_key": "nope"})
        self.assertIs(self.printers["mk4"], keep)
        self.assertEqual(self.mgr.store.secret("MK4", "api_key"), OCTO_KEY)
        # unchanged edit is a no-op
        self.assertEqual(self.mgr.update("MK4", {"model": "New Model"})["changed"], [])
        # name is permanent
        with self.assertRaises(fleet_store.FleetError):
            self.mgr.update("MK4", {"name": "Other"})
        # duplicates refused
        with self.assertRaises(fleet_store.Conflict):
            self.mgr.add("octoprint", self.vals(name="mk4"))
        self.mgr.remove("MK4")
        self.assertEqual(self.printers, {})
        self.assertEqual(self.mgr.store.records(), [])
        with self.assertRaises(fleet_store.NotFound):
            self.mgr.remove("MK4")

    def test_failed_test_saves_nothing_and_skip_test_does(self):
        with self.assertRaises(fleet_store.ProbeFailed) as cm:
            self.mgr.add("octoprint", self.vals(api_key="wrong"))
        self.assertIn("API key", str(cm.exception))
        self.assertEqual(self.mgr.store.records(), [])
        self.assertEqual(self.printers, {})
        self.mgr.add("octoprint", self.vals(api_key="wrong"), skip_test=True)
        self.assertIn("mk4", self.printers)

    def test_validation_messages(self):
        for bad in ({"name": "has space"}, {"url": ""}, {"api_key": ""}, {"url": "ftp://x"}):
            with self.assertRaises(fleet_store.FleetError, msg=bad):
                self.mgr.add("octoprint", self.vals(**bad), skip_test=True)
        with self.assertRaises(fleet_store.FleetError):
            self.mgr.add("nonesuch", self.vals())

    def test_url_without_scheme_gets_http(self):
        res = self.mgr.add("octoprint", self.vals(url=self.url.replace("http://", "")))
        self.assertEqual(res["printer"]["address"], self.url)

    def test_refuses_remove_and_edit_while_printing_unless_forced(self):
        self.mgr.add("octoprint", self.vals())
        prn = self.printers["mk4"]
        prn.snapshot = lambda: {"state": "RUNNING", "job": "benchy"}
        with self.assertRaises(fleet_store.Busy) as cm:
            self.mgr.remove("MK4")
        self.assertIn("RUNNING", str(cm.exception))
        self.assertTrue(cm.exception.payload()["busy"])
        with self.assertRaises(fleet_store.Busy):
            self.mgr.update("MK4", {"model": "x"})
        self.assertIn("mk4", self.printers)
        self.mgr.update("MK4", {"model": "x"}, force=True)
        self.assertEqual(self.printers["mk4"].model, "x")
        self.printers["mk4"].snapshot = lambda: {"state": "PAUSE", "job": "j"}
        with self.assertRaises(fleet_store.Busy):
            self.mgr.remove("MK4")
        self.mgr.remove("MK4", force=True)
        self.assertEqual(self.printers, {})

    def test_guards_block_changes(self):
        self.mgr.add("octoprint", self.vals())
        self.mgr.guards.append(lambda n: "a backup is running" if n.lower() == "mk4" else None)
        with self.assertRaises(fleet_store.Busy):
            self.mgr.remove("MK4")
        self.mgr.remove("MK4", force=True)

    def test_load_all_rebuilds_fleet_from_disk(self):
        self.mgr.add("octoprint", self.vals())
        printers2 = {}
        mgr2 = fleet_store.FleetManager(fleet_store.FleetStore(self.dir), printers2, log=lambda *a: None)
        mgr2.load_all()
        self.assertEqual(printers2["mk4"].api_key, OCTO_KEY)
        fleet_store.stop_driver(printers2["mk4"])

    def test_unavailable_type_is_kept_not_lost(self):
        self.mgr.store.add({"name": "Future", "type": "someday"}, {})
        printers2 = {}
        mgr2 = fleet_store.FleetManager(fleet_store.FleetStore(self.dir), printers2, log=lambda *a: None)
        mgr2.load_all()
        self.assertEqual(printers2, {})
        pub = mgr2.list_public()[0]
        self.assertFalse(pub["loaded"])
        self.assertFalse(pub["driver_available"])


# ----------------------------------------------------------------- bambu via a fake broker

@unittest.skipUnless(HAVE_PAHO, "paho-mqtt not installed")
class BambuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certdir = tempfile.mkdtemp()
        cls.cert = make_cert(cls.certdir) if shutil.which("openssl") else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.certdir, True)

    def setUp(self):
        if not self.cert:
            self.skipTest("openssl CLI not available to make a test certificate")
        self.broker = FakeBroker(*self.cert)
        self.addCleanup(self.broker.close)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.printers = {}
        self.mgr = fleet_store.FleetManager(fleet_store.FleetStore(self.dir), self.printers, log=lambda *a: None)
        self.addCleanup(lambda: [fleet_store.stop_driver(p) for p in list(self.printers.values())])
        old_port, old_to = BambuPrinter.MQTT_PORT, fleet_store.PROBE_TIMEOUT
        BambuPrinter.MQTT_PORT, fleet_store.PROBE_TIMEOUT = self.broker.port, 2.0
        self.addCleanup(setattr, BambuPrinter, "MQTT_PORT", old_port)
        self.addCleanup(setattr, fleet_store, "PROBE_TIMEOUT", old_to)

    def vals(self, **kw):
        return {"name": "P1S", "ip": "127.0.0.1", "serial": BAMBU_SERIAL, "access_code": BAMBU_CODE, **kw}

    def test_probe_ok_reads_model_from_serial_and_disconnects(self):
        res = self.mgr.test("bambu", self.vals())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["model"], "Bambu Lab P1S")
        self.assertEqual(res["firmware"], "01.07.00.00")
        self.assertTrue(res["has_camera"])
        self.assertIn("Connected — Bambu Lab P1S", res["message"])
        self.assertTrue(wait_for(lambda: self.broker.total_live() == 0), "probe left an MQTT client connected")
        self.assertEqual(self.broker.connects, 1)

    def test_probe_wrong_access_code(self):
        res = self.mgr.test("bambu", self.vals(access_code="00000000"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "bad_code")
        self.assertIn("Wrong access code", res["message"])
        self.assertIn("LAN Only or Developer Mode", res["hint"])      # points back into the setup guide
        self.assertEqual(res["guide_step"], "code")

    def test_probe_wrong_serial(self):
        res = self.mgr.test("bambu", self.vals(serial="01P00A999999999"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "serial_mismatch")
        self.assertIn("serial number", res["message"])
        self.assertEqual(res["guide_step"], "serial")
        self.assertTrue(wait_for(lambda: self.broker.total_live() == 0))

    def test_probe_unreachable(self):
        BambuPrinter.MQTT_PORT = closed_port()
        res = self.mgr.test("bambu", self.vals())
        self.assertFalse(res["ok"])
        self.assertIn(res["code"], ("refused", "unreachable"))
        self.assertEqual(res["guide_step"], "network")
        self.assertIn("same network", res["hint"])

    def test_probe_x1_has_no_camera(self):
        res = self.mgr.test("bambu", self.vals(model="Bambu Lab X1 Carbon"))
        self.assertTrue(res["ok"])
        self.assertIn("Bambu Lab P1S", res["message"])      # serial prefix wins for the model name
        self.assertTrue(fleet_store._bambu_has_camera("Bambu Lab P1S"))
        self.assertFalse(fleet_store._bambu_has_camera("Bambu Lab X1 Carbon"))

    def test_add_connects_one_client_and_remove_leaves_none(self):
        res = self.mgr.add("bambu", self.vals())
        self.assertEqual(res["printer"]["model"], "Bambu Lab P1S")            # remembered from the serial
        self.assertNotIn(BAMBU_CODE, json.dumps(res))
        prn = self.printers["p1s"]
        self.assertTrue(wait_for(lambda: prn.snapshot()["state"] == "IDLE"), "never got a status report")
        self.assertEqual(self.broker.live.get("fabrica-P1S"), 1)
        self.mgr.remove("P1S")
        self.assertTrue(wait_for(lambda: self.broker.total_live() == 0), "remove left an MQTT client alive")
        self.assertEqual(self.printers, {})
        self.assertIsNone(prn._client)
        before = self.broker.connects
        time.sleep(0.8)                                                        # no zombie reconnects
        self.assertEqual(self.broker.connects, before)

    def test_edit_swaps_instance_without_overlap(self):
        self.mgr.add("bambu", self.vals(), skip_test=True)
        old = self.printers["p1s"]
        self.assertTrue(wait_for(lambda: self.broker.live.get("fabrica-P1S") == 1))
        # rotate the access code on the (fake) printer, then edit it in the Hub
        self.broker.code = "87654321"
        self.assertTrue(wait_for(lambda: True))
        self.mgr.update("P1S", {"access_code": "87654321"})
        new = self.printers["p1s"]
        self.assertIsNot(new, old)
        self.assertIsNone(old._client)                                          # old one fully stopped
        self.assertTrue(wait_for(lambda: new.snapshot()["state"] == "IDLE"), "new instance never connected")
        self.assertEqual(self.broker.live.get("fabrica-P1S"), 1)
        self.assertEqual(self.broker.max_live.get("fabrica-P1S"), 1,
                         "two MQTT clients for one printer were alive at the same time")
        self.assertEqual(self.mgr.store.secret("P1S", "access_code"), "87654321")
        # and a second edit (new IP spelling) again never overlaps. The address changes, so the access
        # code must be typed again: a saved secret is never carried to a new address.
        self.mgr.update("P1S", {"ip": "localhost", "access_code": "87654321"}, skip_test=True)
        self.assertTrue(wait_for(lambda: self.printers["p1s"].snapshot()["state"] == "IDLE"))
        self.assertEqual(self.broker.max_live.get("fabrica-P1S"), 1)

    def test_refuses_remove_while_running(self):
        self.mgr.add("bambu", self.vals())
        prn = self.printers["p1s"]
        self.assertTrue(wait_for(lambda: prn.snapshot()["state"] == "IDLE"))
        with prn._lock:
            prn.state = {"print": {"gcode_state": "RUNNING", "subtask_name": "benchy"}}
        with self.assertRaises(fleet_store.Busy):
            self.mgr.remove("P1S")
        with self.assertRaises(fleet_store.Busy):
            self.mgr.update("P1S", {"model": "Bambu Lab P1P"})
        self.assertIs(self.printers["p1s"], prn)
        self.assertEqual(self.broker.live.get("fabrica-P1S"), 1)
        self.mgr.remove("P1S", force=True)
        self.assertTrue(wait_for(lambda: self.broker.total_live() == 0))

    def test_bambu_field_validation(self):
        for bad in ({"ip": "http://1.2.3.4"}, {"ip": "1.2.3.4/x"}, {"serial": "abc"}, {"access_code": "a b c d"},
                    {"access_code": "12"}, {"serial": ""}):
            with self.assertRaises(fleet_store.FleetError, msg=bad):
                self.mgr.add("bambu", self.vals(**bad), skip_test=True)

    def test_disconnect_is_idempotent_and_safe_before_connect(self):
        p = BambuPrinter("x", "127.0.0.1", "S", "c")
        p.disconnect()
        p.disconnect()
        p.MQTT_PORT = self.broker.port
        p2 = BambuPrinter("y", "127.0.0.1", BAMBU_SERIAL, BAMBU_CODE)
        p2.connect()
        self.assertTrue(wait_for(lambda: self.broker.live.get("fabrica-y") == 1))
        p2.disconnect()
        p2.disconnect()
        self.assertTrue(wait_for(lambda: self.broker.live.get("fabrica-y") == 0))


class OtherFamiliesTests(unittest.TestCase):
    """PrusaLink / Moonraker are adapted generically from their CONFIG_FIELDS + constructor."""

    def test_prusalink_build_and_validation(self):
        fam = fleet_store.FAMILIES.get("prusalink")
        if fam is None:
            self.skipTest("prusalink driver not importable")
        v = fleet_store.normalize_values(fam, {"name": "MK4", "url": "192.0.2.9", "password": "pw", "storage": "usb"})
        self.assertEqual(v["url"], "http://192.0.2.9")
        prn = fam.build(v)
        self.assertEqual((prn.password, prn.username, prn.storage), ("pw", "maker", "usb"))
        with self.assertRaises(fleet_store.FleetError):      # needs a password or an API key
            fleet_store.normalize_values(fam, {"name": "MK4", "url": "http://192.0.2.9"})
        plain, secrets = fleet_store.split_values(fam, v)
        self.assertEqual(set(secrets), {"password"})
        self.assertNotIn("password", plain)

    def test_moonraker_build(self):
        fam = fleet_store.FAMILIES.get("moonraker")
        if fam is None:
            self.skipTest("moonraker driver not importable")
        v = fleet_store.normalize_values(fam, {"name": "Voron", "url": "http://192.0.2.7"})
        prn = fam.build(v)
        self.assertEqual(prn.kind, "moonraker")
        self.assertEqual(prn.url, "http://192.0.2.7")
        d = fam.describe()
        self.assertIn("api_key", [f["name"] for f in d["fields"] if f.get("secret")])

    def _disconnect_on_edit_and_remove(self, key, extra):
        if fleet_store.FAMILIES.get(key) is None:
            self.skipTest(f"{key} driver not importable")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        printers = {}
        mgr = fleet_store.FleetManager(fleet_store.FleetStore(d), printers, log=lambda *a: None)
        dead = f"http://127.0.0.1:{closed_port()}"       # nothing listens: no network host is contacted
        mgr.add(key, {"name": "P", "url": dead, **extra}, skip_test=True)
        old = printers["p"]
        old_cam = old.camera()
        calls = []
        orig = old.disconnect
        old.disconnect = lambda: (calls.append("old"), orig())
        # new address -> the secret is entered again (a stored one is never reused for a new destination)
        mgr.update("P", {"url": f"http://127.0.0.1:{closed_port()}", **extra}, skip_test=True, force=True)
        self.assertEqual(calls, ["old"])                 # the replaced driver was disconnected...
        self.assertTrue(old_cam._stopped)                # ...which stopped its camera poller
        self.assertIsNot(printers["p"], old)
        new = printers["p"]
        new_cam = new.camera()
        orig_new = new.disconnect
        new.disconnect = lambda: (calls.append("new"), orig_new())
        mgr.remove("P", force=True)
        self.assertEqual(calls, ["old", "new"])
        self.assertTrue(new_cam._stopped)
        self.assertEqual(printers, {})

    def test_prusalink_disconnected_on_edit_and_remove(self):
        self._disconnect_on_edit_and_remove("prusalink", {"password": "pw"})

    def test_moonraker_disconnected_on_edit_and_remove(self):
        self._disconnect_on_edit_and_remove("moonraker", {})


# ----------------------------------------------------------------- the server (routes + tools)

SERVER_SCRIPT = r'''
import asyncio, json, os, sys
sys.path.insert(0, os.environ["APP_DIR"])
import server
from starlette.testclient import TestClient
c = TestClient(server.mcp.streamable_http_app())
out = {}
def call(m, path, **kw):
    r = c.request(m, path, **kw)
    return r.status_code, r.text
steps = json.loads(os.environ["STEPS"])
for key, (m, path, kw) in steps.items():
    out[key] = call(m, path, **kw)
out["tools"] = sorted(t.name for t in asyncio.run(server.mcp.list_tools()))
out["printers"] = sorted(p.name for p in server.PRINTERS.values())
out["palette"] = sorted(server.PALETTE)
print("@@" + json.dumps(out))
sys.stdout.flush()
os._exit(0)
'''


def run_server_script(env_extra, steps):
    # routes are called without an API key here: key check off (tests/test_auth.py covers the check)
    env = {**os.environ, "APP_DIR": APP, "STEPS": json.dumps(steps), "BAMBU_MCP_API_AUTH": "off", **env_extra}
    for k in list(env):
        if k.startswith("T_") and k not in env_extra:
            del env[k]
    r = subprocess.run([sys.executable, "-c", SERVER_SCRIPT], env=env, capture_output=True, text=True, timeout=90)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("@@")]
    if not line:
        raise AssertionError(f"server script failed:\n{r.stdout}\n{r.stderr}")
    return json.loads(line[-1][2:]), r.stdout


try:
    import mcp.server.fastmcp  # noqa: F401
    import httpx  # noqa: F401
    HAVE_SERVER = True
except ImportError:
    HAVE_SERVER = False


@unittest.skipUnless(HAVE_SERVER, "mcp + httpx needed to import the server")
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = serve(FakeOcto)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"BAMBU_MCP_CONFIG": os.path.join(self.dir, "no-such-config.yml"),
                    "BAMBU_MCP_CACHE": os.path.join(self.dir, "data", "metacache.json")}

    def test_starts_with_no_config_and_manages_printers_over_rest(self):
        octo = {"name": "Bench", "url": self.url, "api_key": OCTO_KEY, "model": "Test"}
        steps = {
            "fleet0": ["GET", "/api/fleet", {}],
            "list0": ["GET", "/api/printers", {}],
            "types": ["GET", "/api/printer-types", {}],
            "badtest": ["POST", "/api/printers/test",
                        {"json": {"type": "octoprint", "values": {**octo, "api_key": "wrong"}}}],
            "goodtest": ["POST", "/api/printers/test", {"json": {"type": "octoprint", "values": octo}}],
            "nobody": ["POST", "/api/printers", {"content": "not json", "headers": {"Content-Type": "application/json"}}],
            "addbad": ["POST", "/api/printers",
                       {"json": {"type": "octoprint", "values": {**octo, "api_key": "wrong"}}}],
            "add": ["POST", "/api/printers", {"json": {"type": "octoprint", "values": octo}}],
            "adddup": ["POST", "/api/printers", {"json": {"type": "octoprint", "values": octo}, }],
            "list1": ["GET", "/api/printers", {}],
            "fleet1": ["GET", "/api/fleet", {}],
            "edit": ["PUT", "/api/printers/Bench", {"json": {"values": {"model": "Renamed"}}}],
            "editmissing": ["PUT", "/api/printers/Nope", {"json": {"values": {"model": "x"}}}],
            "backups": ["GET", "/api/backups", {}],
            "camera404": ["GET", "/api/printer/Nope/snapshot", {}],
            "remove": ["DELETE", "/api/printers/Bench", {}],
            "fleet2": ["GET", "/api/fleet", {}],
            "dash": ["GET", "/dashboard", {}],
        }
        out, _ = run_server_script(self.env, steps)
        code = lambda k: out[k][0]   # noqa: E731
        body = lambda k: json.loads(out[k][1])   # noqa: E731
        self.assertEqual(code("fleet0"), 200)
        self.assertEqual(body("fleet0"), [])
        self.assertEqual(body("list0"), {"printers": []})
        self.assertEqual([t["type"] for t in body("types")["types"]][:2], ["bambu", "octoprint"])
        bambu = body("types")["types"][0]
        self.assertEqual([f["name"] for f in bambu["fields"]], ["name", "ip", "serial", "access_code", "model"])
        self.assertFalse(body("badtest")["result"]["ok"])
        self.assertTrue(body("goodtest")["result"]["ok"])
        self.assertEqual(code("nobody"), 400)
        self.assertEqual(code("addbad"), 422)
        self.assertIn("API key", body("addbad")["error"])
        self.assertEqual(code("add"), 201)
        self.assertEqual(code("adddup"), 409)
        self.assertEqual(body("list1")["printers"][0]["secrets"], {"api_key": "set"})
        self.assertEqual([p["printer"] for p in body("fleet1")], ["Bench"])
        self.assertEqual(code("edit"), 200)
        self.assertEqual(code("editmissing"), 404)
        self.assertEqual(code("backups"), 200)
        self.assertEqual(code("camera404"), 404)
        self.assertEqual(code("remove"), 200)
        self.assertEqual(body("fleet2"), [])
        self.assertEqual(code("dash"), 200)
        # tools registered
        for t in ("list_printer_types", "test_printer_connection", "add_printer", "update_printer",
                  "remove_printer", "list_printers", "backup_printer_files", "get_camera_snapshot"):
            self.assertIn(t, out["tools"])
        # the key never appeared in ANY response body
        self.assertFalse([k for k, v in out.items() if k not in ("tools", "printers", "palette")
                          and OCTO_KEY in v[1]])
        # palette falls back to the built-in default with no config
        self.assertIn("GREEN", out["palette"])
        # files on disk: store was created only because we added something; secrets 0600
        data = os.path.join(self.dir, "data")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(data, "secrets.json")).st_mode), 0o600)

    def test_no_files_written_on_a_clean_start(self):
        out, _ = run_server_script(self.env, {"fleet": ["GET", "/api/fleet", {}]})
        self.assertEqual(out["printers"], [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "data", "printers.json")))

    def test_config_that_is_a_directory_is_ignored(self):
        os.mkdir(os.path.join(self.dir, "cfgdir"))      # what a docker bind mount of a missing file leaves
        out, _ = run_server_script({**self.env, "BAMBU_MCP_CONFIG": os.path.join(self.dir, "cfgdir")},
                                   {"fleet": ["GET", "/api/fleet", {}]})
        self.assertEqual(out["printers"], [])

    def test_legacy_config_imports_once_then_store_wins(self):
        cfg = os.path.join(self.dir, "config.yml")
        with open(cfg, "w") as fh:
            fh.write(f"""
printers:
  - name: P1S
    model: "Bambu Lab P1S"
    ip: 127.0.0.1
    serial: {BAMBU_SERIAL}
    access_code_env: T_P1S_CODE
  - name: A1
    ip: 127.0.0.1
    serial: 03900A000000002
    access_code_env: T_A1_CODE
  - name: MK4
    type: octoprint
    url: {self.url}
    api_key_env: T_MK4_KEY
palette:
  GREEN: "21FF06"
  PINK: "FF00AA"
conventions:
  - match: "widget"
    color: PINK
""")
        env = {**self.env, "BAMBU_MCP_CONFIG": cfg, "T_P1S_CODE": "code-p1s", "T_A1_CODE": "code-a1",
               "T_MK4_KEY": OCTO_KEY}
        steps = {"list": ["GET", "/api/printers", {}]}
        out, stdout = run_server_script(env, steps)
        self.assertEqual(out["printers"], ["A1", "MK4", "P1S"])
        self.assertIn("imported 3 printer(s)", stdout)
        self.assertEqual(out["palette"], ["GREEN", "PINK"])          # palette still read from config.yml
        listed = json.loads(out["list"][1])["printers"]
        self.assertEqual({p["name"]: p["secrets"] for p in listed},
                         {"P1S": {"access_code": "set"}, "A1": {"access_code": "set"}, "MK4": {"api_key": "set"}})
        self.assertNotIn("code-p1s", out["list"][1])
        self.assertNotIn(OCTO_KEY, out["list"][1])
        data = os.path.join(self.dir, "data")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(data, "secrets.json")).st_mode), 0o600)
        # restart: no env vars at all, and a DIFFERENT config - the store wins, nothing re-imported
        with open(cfg, "w") as fh:
            fh.write("printers:\n  - {name: Ghost, ip: 127.0.0.1, serial: GHOSTGHOST1, access_code_env: X}\n")
        env2 = {k: v for k, v in env.items() if not k.startswith("T_")}
        out2, stdout2 = run_server_script(env2, steps)
        self.assertEqual(out2["printers"], ["A1", "MK4", "P1S"])
        self.assertNotIn("imported", stdout2)
        self.assertEqual({p["name"]: p["secrets"] for p in json.loads(out2["list"][1])["printers"]},
                         {"P1S": {"access_code": "set"}, "A1": {"access_code": "set"}, "MK4": {"api_key": "set"}})


class SetupGuideTests(unittest.TestCase):
    """The Add printer wizard's step-by-step guides (setup_guides.py): same text for the
    dashboard and for agents, every link resolves, tips map from the probe errors, and the
    public repo carries nothing personal."""

    def types(self):
        return {t["type"]: t for t in fleet_store.FleetManager(
            fleet_store.FleetStore(tempfile.mkdtemp()), {}, log=lambda *a: None).types()}

    def test_every_family_has_a_guide_agents_get_too(self):
        types = self.types()
        self.assertTrue({"bambu", "octoprint"} <= set(types))
        for key, t in types.items():
            g = t["setup_guide"]
            self.assertTrue(g and g["title"] and g["intro"], key)
            ids = [x["id"] for x in g["steps"] + g["notes"]]
            self.assertEqual(len(ids), len(set(ids)), f"{key}: duplicate ids")
            for x in g["steps"]:
                self.assertTrue(x["title"] and x["text"], (key, x["id"]))
            self.assertEqual(g["steps"][-1]["id"], "test")
            self.assertTrue(all(n["kind"] in ("important", "info") for n in g["notes"]))
            # every field's "where do I find this" link and every tip points at a real step
            for f in t["fields"]:
                if f.get("guide_step"):
                    self.assertIn(f["guide_step"], ids, (key, f["name"]))
            for tr in t["troubleshooting"]:
                self.assertIn(tr["step"], ids, (key, tr["when"]))
                self.assertTrue(tr["when"] and tr["tip"])

    def test_guides_cover_what_a_stranger_needs(self):
        types = self.types()

        def text(k):
            g = types[k]["setup_guide"]
            return " ".join(x["title"] + " " + x["text"] for x in g["steps"] + g["notes"])
        b = text("bambu")
        for needle in ("LAN Only", "Developer Mode", "IP address", "serial number", "access code",
                       "can change", "firmware", "Handy", "Discover printers", "X1 and H2", "ports 8883"):
            self.assertIn(needle, b)
        p = text("prusalink")
        for needle in ("Settings > Network", "maker", "Generate Password", "API key", "pre-heat", "camera"):
            self.assertIn(needle, p)
        o = text("octoprint")
        for needle in ("Application Keys", "Print Hub", "Connect", "webcam"):
            self.assertIn(needle, o)
        m = text("moonraker")
        for needle in ("7125", "trusted_clients", "output_pin", "UNLOAD_FILAMENT", "LOAD_FILAMENT", "webcam"):
            self.assertIn(needle, m)

    def test_nothing_personal_in_the_public_text(self):
        import re
        blob = json.dumps(self.types())
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", blob):
            self.assertTrue(ip.startswith(("192.168.1.", "192.0.2.")) or ip == "127.0.0.1", ip)
        self.assertFalse(re.search(r"\b(?:01[PS]|039|030|00[MW]|094|093)[0-9A-Z]{12}\b", blob))   # a real-looking serial
        self.assertFalse(re.search(r"\b[0-9a-f]{8}\b", blob.replace("moonraker", "")))        # a real-looking access code

    def test_failed_probes_map_to_guide_tips_without_changing_the_error(self):
        cases = {
            "prusalink": [("Authentication failed \u2014 wrong username/password or API key. Find them on the printer: "
                           "Settings > Network > PrusaLink.", "creds", "maker"),
                          ("Could not connect to http://192.0.2.9: timed out", "enable", "browser"),
                          ("http://192.0.2.9 does not look like PrusaLink (404)", "ip", "OctoPrint")],
            "moonraker": [("Moonraker wants an API key (or this machine is not in its trusted_clients). "
                           "Enter the key and test again.", "trusted", "trusted_clients"),
                          ("Could not connect to http://192.0.2.9: refused", "address", "7125"),
                          ("http://192.0.2.9 does not look like Moonraker (HTTP 404)", "address", "Mainsail")],
            "octoprint": [("x", "apikey", "Application Keys", "bad_key"), ("x", "url", "same network", "unreachable"),
                          ("x", "url", "different printer type", "not_octoprint")],
            "bambu": [("x", "code", "LAN Only or Developer Mode", "bad_code"),
                      ("x", "network", "same network", "timeout"), ("x", "network", "same network", "refused"),
                      ("x", "code", "Bambu Studio", "dropped"), ("x", "serial", "serial number", "serial_mismatch")],
        }
        fams = fleet_store.FAMILIES
        for key, rows in cases.items():
            if key not in fams:
                continue
            for row in rows:
                err, step, word = row[:3]
                res = {"ok": False, "error": err, "code": row[3] if len(row) > 3 else None}
                fam = fleet_store.Family(key, "L", "d", [], lambda v, t, r=res: dict(r), lambda v: None)
                out = fam.probe({})
                self.assertEqual(out["guide_step"], step, (key, err))
                self.assertIn(word, out["hint"], (key, err))
                self.assertEqual(out["error"], err)                  # the probe's own text is untouched
                self.assertEqual(out["message"], err)
                self.assertIn("Tip: ", str(fleet_store.ProbeFailed(out)))   # agents and REST callers see it too
        ok = fleet_store.Family("bambu", "L", "d", [], lambda v, t: {"ok": True, "model": "M"}, lambda v: None).probe({})
        self.assertIsNone(ok["hint"])
        bad_input = fleet_store.Family("bambu", "L", "d", [], lambda v, t: {"ok": False, "code": "bad_input",
                                       "error": "Fill in the IP address"}, lambda v: None).probe({})
        self.assertIsNone(bad_input["hint"])

    def test_real_driver_error_strings_still_match_their_tips(self):
        """The hints key off the drivers' own error text; if a driver rewords it this fails."""
        for key in ("prusalink", "moonraker"):
            if key not in fleet_store.FAMILIES:
                self.skipTest(f"{key} driver not importable")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        dead = f"http://127.0.0.1:{closed_port()}"           # nothing listens: no network host is contacted
        for key in ("prusalink", "moonraker"):
            out = fleet_store.FAMILIES[key].probe({"url": dead, "password": "x"})
            self.assertFalse(out["ok"])
            self.assertTrue(out["hint"], f"{key}: no tip for: {out['message']}")

    def test_dashboard_renders_the_guide(self):
        html = slurp(os.path.join(APP, "dashboard.html"))
        for needle in ("pmGuideHtml", "pmGuideGo", "pm_guide_seen_", "setup_guide", "guide_step", "CHECK THIS STEP",
                       "Show me how to find this", "IMPORTANT", "GOOD TO KNOW"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
