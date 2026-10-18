"""Tests for app/discovery.py (printer discovery by active TCP scan).

    python3 tests/test_discovery.py          (route tests also need starlette + httpx; TLS tests need the openssl CLI)

Loopback only - nothing here touches a real printer, a real network or a real DNS name:
  * every "printer" is a throwaway server on 127.0.0.1 and the scanner's port map is pointed at those
    ports (DiscoveryManager(ports=...)), so a scan can only ever reach loopback;
  * the Bambu fake is a TLS server whose certificate subject CN is a fake serial number (certs are made
    with the openssl CLI; those tests skip if it is missing);
  * SSDP uses a high UDP port on 127.0.0.1 without joining any multicast group;
  * the optional zeroconf library is replaced by a fake module.
"""
import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import discovery  # noqa: E402
from discovery import DiscoveryError, DiscoveryManager, parse_networks  # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None
SERIAL = "01P00A000000001"           # fake; prefix 01P = P1S
LOOP = ["127.0.0.1/32"]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_done(mgr, scan_id, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        st = mgr.status(scan_id)
        if st["finished"]:
            return st
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


def quick(**kw):
    """A manager for tests: no waits, no multicast extras unless asked."""
    base = dict(cooldown=0, ssdp=False, zeroconf=False, connect_timeout=0.5, http_timeout=1.0)
    base.update(kw)
    return DiscoveryManager(**base)


def scan(mgr, networks=LOOP):
    return wait_done(mgr, mgr.start(networks)["scan_id"])


# ----------------------------------------------------------------- fake servers

REQUESTS = []        # (port, method, path, has_credentials) for every HTTP request any fake received


def make_http(routes, server_header=None):
    """routes: path -> (status, headers dict, body bytes|obj). Anything else -> 404 (empty)."""
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self):
            port = self.server.server_address[1]
            creds = any(h in self.headers for h in ("Authorization", "X-Api-Key", "Cookie"))
            REQUESTS.append((port, self.command, self.path, creds))
            hit = routes.get(self.path.split("?")[0])
            if hit is None:
                status, headers, body = 404, {}, b""
            else:
                status, headers, body = hit
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
                headers = {"Content-Type": "application/json", **headers}
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            if server_header:
                self.send_header("Server", server_header)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST = do_PUT = do_DELETE = _do
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def moonraker_routes(hostname="voron", state="ready"):
    return {"/server/info": (200, {}, {"result": {"klippy_state": state, "moonraker_version": "v0.9.3-1"}}),
            "/printer/info": (200, {}, {"result": {"hostname": hostname, "software_version": "v0.12.0",
                                                    "state": state}})}


OCTO_LOCKED = {"/api/version": (403, {}, b"Forbidden"),
               "/": (200, {"Content-Type": "text/html"},
                     b"<html><head><title>OctoPrint Login</title></head><body></body></html>")}
OCTO_OPEN = {"/api/version": (200, {}, {"api": "0.1", "server": "1.10.2", "text": "OctoPrint 1.10.2"})}
PRUSA_OPEN = {"/api/version": (200, {}, {"api": "2.0.0", "server": "2.1.2", "original": "PrusaLink 0.7.2",
                                          "text": "PrusaLink"}),
              "/api/v1/info": (200, {}, {"hostname": "mk4-shop", "nozzle_diameter": 0.4, "mmu": False,
                                         "serial": "CZPX1234X567XC89012", "min_extrusion_temp": 170})}
PRUSA_DIGEST = {"/api/version": (401, {"WWW-Authenticate": 'Digest realm="Printer API", nonce="abc", qop="auth"'}, b""),
                "/api/v1/info": (401, {"WWW-Authenticate": 'Digest realm="Printer API", nonce="abc"'}, b"")}
PLAIN_WEB = {"/": (200, {"Content-Type": "text/html"}, b"<html><title>Router admin</title></html>")}


class FakeTLS:
    """Accepts TLS connections, completes the handshake (so the client can read our certificate) and hangs up."""
    def __init__(self, certfile, keyfile):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(certfile, keyfile)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.hits += 1
            try:
                with self.ctx.wrap_socket(c, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                pass

    def close(self):
        self.sock.close()


class FakeListener:
    """A plain TCP port that just accepts (stands in for FTPS 990 / camera 6000)."""
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
                c.close()
            except OSError:
                return

    def close(self):
        self.sock.close()


def sh(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_certs(d, serial=SERIAL, issuer_cn="BBL Device CA"):
    """(leaf cert, key) with subject CN=serial issued by a CA named issuer_cn; plus a self-signed pair."""
    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{d}/ca.key", "-out", f"{d}/ca.pem",
       "-subj", f"/CN={issuer_cn}/O=Fake Test CA", "-days", "2")
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{d}/leaf.key", "-out", f"{d}/leaf.csr",
       "-subj", f"/CN={serial}/O=Fake Printer/C=CN")
    sh("openssl", "x509", "-req", "-in", f"{d}/leaf.csr", "-CA", f"{d}/ca.pem", "-CAkey", f"{d}/ca.key",
       "-CAcreateserial", "-out", f"{d}/leaf.pem", "-days", "2")
    return f"{d}/leaf.pem", f"{d}/leaf.key"


def make_selfsigned(d, name, cn):
    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{d}/{name}.key",
       "-out", f"{d}/{name}.pem", "-subj", f"/CN={cn}", "-days", "2")
    return f"{d}/{name}.pem", f"{d}/{name}.key"


def pem_to_der(path):
    with open(path) as fh:
        return ssl.PEM_cert_to_DER_cert(fh.read())


def closed_ports(**over):
    ports = {"bambu_mqtt": free_port(), "bambu_ftps": free_port(), "bambu_camera": free_port(),
             "moonraker": free_port(), "http": (free_port(),)}
    ports.update(over)
    return ports


# ----------------------------------------------------------------- tests

class NetworkParsing(unittest.TestCase):
    def test_valid_and_normalised(self):
        self.assertEqual([str(n) for n in parse_networks(["192.168.1.0/24"])], ["192.168.1.0/24"])
        self.assertEqual([str(n) for n in parse_networks(["192.168.1.77/24"])], ["192.168.1.0/24"])   # host bits tolerated
        self.assertEqual([str(n) for n in parse_networks("192.168.1.0/24, 10.0.0.0/24")],
                         ["10.0.0.0/24", "192.168.1.0/24"])
        self.assertEqual([str(n) for n in parse_networks(["192.168.1.50"])], ["192.168.1.50/32"])
        self.assertEqual([str(n) for n in parse_networks(["192.168.1.0/255.255.255.0"])], ["192.168.1.0/24"])
        self.assertEqual([str(n) for n in parse_networks(["100.100.1.0/24"])], ["100.100.1.0/24"])   # VPN overlay range
        self.assertEqual([str(n) for n in parse_networks(["127.0.0.1/32"])], ["127.0.0.1/32"])

    def test_cap_is_a_slash_22(self):
        self.assertEqual(len(discovery.hosts_of(parse_networks(["10.1.0.0/22"]))), 1022)
        for bad in ("10.1.0.0/21", "10.0.0.0/16", "192.168.0.0/16", "10.0.0.0/8"):
            with self.assertRaises(DiscoveryError, msg=bad) as cm:
                parse_networks([bad])
            self.assertIn("too big", str(cm.exception))

    def test_rejects_public_ipv6_and_garbage(self):
        for bad, word in (("8.8.8.0/24", "private"), ("1.1.1.1", "private"), ("172.32.0.0/24", "private"),
                          ("192.169.0.0/24", "private"), ("fe80::/64", "IPv6"), ("not-a-network", "not a network"),
                          ("192.168.1.0/33", "not a network"), ("192.168.1.256", "not a network")):
            with self.assertRaises(DiscoveryError, msg=bad) as cm:
                parse_networks([bad])
            self.assertIn(word, str(cm.exception), bad)
        for empty in ([], [""], "  ", None, 5):
            with self.assertRaises(DiscoveryError):
                parse_networks(empty)

    def test_dedup_overlap_and_total_cap(self):
        nets = parse_networks(["192.168.1.0/24", "192.168.1.0/25", "192.168.1.9", "192.168.1.0/24"])
        self.assertEqual([str(n) for n in nets], ["192.168.1.0/24"])
        self.assertEqual(len(discovery.hosts_of(nets)), 254)
        with self.assertRaises(DiscoveryError) as cm:                  # 3 x /22 = 3066 > 2048
            parse_networks(["10.1.0.0/22", "10.2.0.0/22", "10.3.0.0/22"])
        self.assertIn("at most", str(cm.exception))
        with self.assertRaises(DiscoveryError):                        # 9 separate networks
            parse_networks([f"10.{i}.0.0/30" for i in range(9)])

    def test_host_lists(self):
        self.assertEqual(discovery.hosts_of(parse_networks(["192.168.1.0/30"])), ["192.168.1.1", "192.168.1.2"])
        self.assertEqual(discovery.hosts_of(parse_networks(["192.168.1.4/31"])), ["192.168.1.4", "192.168.1.5"])
        self.assertEqual(discovery.hosts_of(parse_networks(["192.168.1.9/32"])), ["192.168.1.9"])


@unittest.skipUnless(HAVE_OPENSSL, "needs the openssl CLI")
class CertReading(unittest.TestCase):
    def test_subject_and_issuer_cn(self):
        with tempfile.TemporaryDirectory() as d:
            cert, _ = make_certs(d)
            names = discovery.parse_cert_names(pem_to_der(cert))
            self.assertEqual(names["subject"]["CN"], SERIAL)
            self.assertEqual(names["subject"]["O"], "Fake Printer")
            self.assertEqual(names["subject"]["C"], "CN")
            self.assertEqual(names["issuer"]["CN"], "BBL Device CA")
            self.assertEqual(names["issuer"]["O"], "Fake Test CA")
            # cross-check against the stdlib's own (private) decoder when it is there
            try:
                dec = ssl._ssl._test_decode_cert(cert)
                flat = {k: v for rdn in dec["subject"] for k, v in rdn}
                self.assertEqual(flat["commonName"], SERIAL)
            except AttributeError:
                pass

    def test_garbage_is_not_a_crash(self):
        for junk in (b"", b"\x30", b"\x30\x03\x01", b"hello world", b"\x30\x82\xff\xff" + b"\x00" * 10, None):
            self.assertEqual(discovery.parse_cert_names(junk if junk is not None else b""), {})


@unittest.skipUnless(HAVE_OPENSSL, "needs the openssl CLI")
class BambuFingerprint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="disc-certs-")
        cls.leaf = make_certs(cls.dir)
        cls.other = make_selfsigned(cls.dir, "mosq", "mosquitto-broker")
        cls.unknown = make_selfsigned(cls.dir, "unk", "ZZZ00A000000001")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_serial_and_model_from_the_certificate(self):
        tls, ftps = FakeTLS(*self.leaf), FakeListener()
        self.addCleanup(tls.close)
        self.addCleanup(ftps.close)
        st = scan(quick(ports=closed_ports(bambu_mqtt=tls.port, bambu_ftps=ftps.port)))
        self.assertEqual((st["state"], st["done"], st["total"]), ("done", 1, 1))
        self.assertEqual(len(st["found"]), 1)
        f = st["found"][0]
        self.assertEqual(f["family"], "bambu")
        self.assertEqual(f["address"], "127.0.0.1")
        self.assertEqual(f["port"], tls.port)
        self.assertEqual(f["serial"], SERIAL)
        self.assertEqual(f["model"], "Bambu Lab P1S")
        self.assertEqual(f["confidence"], "high")
        self.assertTrue(f["details"]["ftps_open"])
        self.assertEqual(f["details"]["cert_issuer"], "BBL Device CA")
        self.assertFalse(f["already_added"])
        self.assertEqual(f["prefill"], {"type": "bambu", "values": {"name": "P1S", "ip": "127.0.0.1", "serial": SERIAL}})
        self.assertEqual(f["suggested_name"], "P1S")

    def test_issuer_alone_corroborates(self):
        tls = FakeTLS(*self.leaf)
        self.addCleanup(tls.close)
        st = scan(quick(ports=closed_ports(bambu_mqtt=tls.port)))        # 990 / 6000 closed
        self.assertEqual([f["serial"] for f in st["found"]], [SERIAL])
        self.assertEqual(st["found"][0]["confidence"], "high")

    def test_other_tls_broker_is_not_a_bambu(self):
        tls = FakeTLS(*self.other)
        self.addCleanup(tls.close)
        self.assertEqual(scan(quick(ports=closed_ports(bambu_mqtt=tls.port)))["found"], [])

    def test_serial_shaped_cn_needs_corroboration(self):
        tls = FakeTLS(*self.unknown)           # self-signed, unknown prefix, nothing else open
        self.addCleanup(tls.close)
        self.assertEqual(scan(quick(ports=closed_ports(bambu_mqtt=tls.port)))["found"], [])
        ftps = FakeListener()
        self.addCleanup(ftps.close)
        st = scan(quick(ports=closed_ports(bambu_mqtt=tls.port, bambu_ftps=ftps.port)))
        self.assertEqual(len(st["found"]), 1)
        f = st["found"][0]
        self.assertEqual((f["serial"], f["model"], f["confidence"]), ("ZZZ00A000000001", None, "high"))
        cam = FakeListener()                   # camera port alone is weaker corroboration
        self.addCleanup(cam.close)
        st = scan(quick(ports=closed_ports(bambu_mqtt=tls.port, bambu_camera=cam.port)))
        self.assertEqual(st["found"][0]["confidence"], "medium")

    def test_non_tls_port_8883_is_ignored(self):
        plain = FakeListener()                 # accepts then hangs up: no handshake possible
        self.addCleanup(plain.close)
        self.assertEqual(scan(quick(ports=closed_ports(bambu_mqtt=plain.port)))["found"], [])


class HttpFingerprints(unittest.TestCase):
    def serve(self, routes, **kw):
        srv = make_http(routes, **kw)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv.server_address[1]

    def one(self, routes, http_extra=(), moonraker=None, **kw):
        port = self.serve(routes, **kw)
        ports = closed_ports(http=(port, *http_extra))
        if moonraker:
            ports["moonraker"] = moonraker
        st = scan(quick(ports=ports))
        return port, st

    def test_moonraker_and_hostname(self):
        port = self.serve(moonraker_routes("voron"))
        st = scan(quick(ports=closed_ports(moonraker=port)))
        self.assertEqual(len(st["found"]), 1)
        f = st["found"][0]
        self.assertEqual((f["family"], f["hostname"], f["port"]), ("moonraker", "voron", port))
        self.assertEqual(f["details"]["klippy_state"], "ready")
        self.assertEqual(f["details"]["klipper_version"], "v0.12.0")
        self.assertEqual(f["url"], f"http://127.0.0.1:{port}")
        self.assertEqual(f["suggested_name"], "voron")
        self.assertEqual(f["prefill"]["values"], {"name": "voron", "url": f"http://127.0.0.1:{port}"})

    def test_moonraker_unwrapped_reply_and_not_ready(self):
        port = self.serve({"/server/info": (200, {}, {"klippy_state": "shutdown", "moonraker_version": "v0.8"})})
        st = scan(quick(ports=closed_ports(moonraker=port)))
        self.assertEqual([f["family"] for f in st["found"]], ["moonraker"])
        self.assertEqual(st["found"][0]["details"]["klippy_state"], "shutdown")
        self.assertIsNone(st["found"][0]["hostname"])

    def test_moonraker_wants_a_key(self):
        port = self.serve({"/server/info": (401, {}, {"error": {"code": 401, "message": "Unauthorized"}})},
                          server_header="TornadoServer/6.4")
        f = scan(quick(ports=closed_ports(moonraker=port)))["found"][0]
        self.assertEqual((f["family"], f["confidence"], f["details"]), ("moonraker", "medium", {"auth_required": True}))

    def test_a_401_json_from_something_else_is_not_moonraker(self):
        port = self.serve({"/server/info": (401, {}, {"error": {"code": 401}})}, server_header="nginx")
        self.assertEqual(scan(quick(ports=closed_ports(moonraker=port)))["found"], [])

    def test_octoprint_locked_api_found_by_page_title(self):
        port, st = self.one(OCTO_LOCKED)
        self.assertEqual([(f["family"], f["port"]) for f in st["found"]], [("octoprint", port)])
        self.assertTrue(st["found"][0]["details"]["api_locked"])
        self.assertEqual(st["found"][0]["url"], f"http://127.0.0.1:{port}")

    def test_octoprint_open_api(self):
        _, st = self.one(OCTO_OPEN)
        self.assertEqual(st["found"][0]["family"], "octoprint")
        self.assertEqual(st["found"][0]["details"]["version"], "1.10.2")

    def test_prusalink_open_version_and_info(self):
        _, st = self.one(PRUSA_OPEN)
        f = st["found"][0]
        self.assertEqual((f["family"], f["hostname"], f["suggested_name"]), ("prusalink", "mk4-shop", "mk4-shop"))
        self.assertEqual(f["details"]["nozzle_diameter"], 0.4)
        self.assertEqual(f["details"]["version"], "PrusaLink 0.7.2")

    def test_prusalink_digest_challenge(self):
        _, st = self.one(PRUSA_DIGEST)
        f = st["found"][0]
        self.assertEqual((f["family"], f["confidence"]), ("prusalink", "high"))
        self.assertEqual((f["details"]["auth"], f["details"]["realm"]), ("digest", "Printer API"))
        self.assertIsNone(f["hostname"])               # /api/v1/info is locked: no hostname, no crash
        self.assertEqual(f["suggested_name"], "PrusaLink-1")

    def test_prusalink_page_title_only(self):
        _, st = self.one({"/api/version": (404, {}, b""),
                          "/": (200, {}, b"<html><title>PrusaLink</title></html>")})
        self.assertEqual((st["found"][0]["family"], st["found"][0]["confidence"]), ("prusalink", "medium"))

    def test_ordinary_web_servers_are_not_printers(self):
        _, st = self.one(PLAIN_WEB)
        self.assertEqual(st["found"], [])
        _, st = self.one({"/api/version": (200, {}, {"version": "1.2.3"}), "/": (200, {}, b"<title>Some app</title>")})
        self.assertEqual(st["found"], [])
        _, st = self.one({"/api/version": (401, {"WWW-Authenticate": 'Basic realm="x"'}, b"")})
        self.assertEqual(st["found"], [])

    def test_all_families_on_one_host_one_scan(self):
        mk = self.serve(moonraker_routes("voron"))
        octo = self.serve(OCTO_LOCKED)
        prusa = self.serve(PRUSA_OPEN)
        plain = self.serve(PLAIN_WEB)
        st = scan(quick(ports=closed_ports(moonraker=mk, http=(plain, octo, prusa))))
        self.assertEqual(sorted(f["family"] for f in st["found"]), ["moonraker", "octoprint", "prusalink"])

    def test_same_family_on_two_ports_is_reported_once(self):
        a, b = self.serve(OCTO_LOCKED), self.serve(OCTO_LOCKED)
        st = scan(quick(ports=closed_ports(http=(a, b))))
        self.assertEqual([(f["family"], f["port"]) for f in st["found"]], [("octoprint", a)])     # first listed port wins
        mk = self.serve(moonraker_routes("voron"))
        front = self.serve(moonraker_routes("voron"))                  # Mainsail-style proxy on "port 80"
        st = scan(quick(ports=closed_ports(moonraker=mk, http=(front,))))
        self.assertEqual([(f["family"], f["port"]) for f in st["found"]], [("moonraker", mk)])    # Moonraker's own port wins

    def test_closed_ports_find_nothing(self):
        st = scan(quick(ports=closed_ports()))
        self.assertEqual((st["state"], st["done"], st["total"], st["found"]), ("done", 1, 1, []))

    def test_only_get_requests_and_no_credentials(self):
        del REQUESTS[:]
        mk, octo, prusa = self.serve(moonraker_routes()), self.serve(OCTO_LOCKED), self.serve(PRUSA_OPEN)
        scan(quick(ports=closed_ports(moonraker=mk, http=(octo, prusa))))
        self.assertTrue(REQUESTS)
        self.assertEqual({r[1] for r in REQUESTS}, {"GET"})
        self.assertFalse([r for r in REQUESTS if r[3]], "a request carried credentials")
        allowed = {"/server/info", "/printer/info", "/api/version", "/api/v1/info", "/"}
        self.assertLessEqual({r[2] for r in REQUESTS}, allowed)


class HostLogic(unittest.TestCase):
    def test_silent_host_is_abandoned_only_after_all_three_key_ports_timed_out(self):
        calls = []
        m = quick()
        m._tcp_probe = lambda ip, port: calls.append(port) or "silent"
        self.assertEqual(m._scan_host("10.9.9.9"), [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(set(calls), {80, 7125, 8883})       # port 80 is one of them: a Pi that only opens 80 is not empty

    def test_two_silent_key_ports_do_not_end_the_host(self):
        calls = []
        m = quick()
        m._tcp_probe = lambda ip, port: calls.append(port) or ("open" if port == 80 else "silent")
        m._fingerprint_http = lambda ip, port: None
        m._scan_host("10.9.9.9")
        self.assertGreaterEqual(len(calls), 5)               # carried on past the silent ports

    def test_a_host_that_answers_gets_every_port_tried(self):
        calls = []
        m = quick()
        m._tcp_probe = lambda ip, port: calls.append(port) or ("silent" if len(calls) == 1 else "closed")
        self.assertEqual(m._scan_host("10.9.9.9"), [])
        self.assertEqual(len(calls), len(set(calls)))
        self.assertGreaterEqual(len(calls), 5)

    def test_concurrency_is_bounded_and_progress_counts_hosts(self):
        lock, state = threading.Lock(), {"now": 0, "max": 0, "n": 0}

        def slow(ip):
            with lock:
                state["now"] += 1
                state["n"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.03)
            with lock:
                state["now"] -= 1
            return []
        m = quick(workers=4)
        m._scan_host = slow
        sid = m.start(["127.0.0.0/26"])["scan_id"]           # 62 hosts
        seen_mid = False
        while not m.status(sid)["finished"]:
            st = m.status(sid)
            seen_mid = seen_mid or 0 < st["done"] < st["total"]
            time.sleep(0.01)
        st = m.status(sid)
        self.assertTrue(seen_mid, "progress never showed a partial count")
        self.assertEqual((st["done"], st["total"], state["n"]), (62, 62, 62))
        self.assertLessEqual(state["max"], 4)
        self.assertGreaterEqual(state["max"], 2)

    def test_worker_cap(self):
        self.assertEqual(DiscoveryManager(workers=100000).workers, discovery.MAX_WORKERS)
        self.assertEqual(DiscoveryManager(workers=0).workers, 1)

    def test_a_crashing_host_does_not_kill_the_scan(self):
        m = quick()

        def boom(ip):
            raise RuntimeError("x")
        m._scan_host = boom
        st = scan(m, ["127.0.0.0/30"])
        self.assertEqual((st["state"], st["done"], st["found"]), ("done", 2, []))


class FleetAwareness(unittest.TestCase):
    def test_already_added_by_url_and_by_serial(self):
        octo = make_http(OCTO_LOCKED)
        self.addCleanup(octo.shutdown)
        self.addCleanup(octo.server_close)
        mk = make_http(moonraker_routes())
        self.addCleanup(mk.shutdown)
        self.addCleanup(mk.server_close)
        recs = [{"name": "Shop-Octo", "type": "octoprint", "address": "http://127.0.0.1:5000",
                 "values": {"url": "http://127.0.0.1:5000"}}]
        ports = closed_ports(moonraker=mk.server_address[1], http=(octo.server_address[1],))
        st = scan(quick(ports=ports, fleet_records=lambda: recs))
        by = {f["family"]: f for f in st["found"]}
        self.assertTrue(by["octoprint"]["already_added"])
        self.assertEqual(by["octoprint"]["already_added_as"], "Shop-Octo")
        self.assertFalse(by["moonraker"]["already_added"])          # same host, different family
        self.assertIsNone(by["moonraker"]["already_added_as"])

    @unittest.skipUnless(HAVE_OPENSSL, "needs the openssl CLI")
    def test_bambu_matched_by_serial_even_if_ip_changed(self):
        with tempfile.TemporaryDirectory() as d:
            tls = FakeTLS(*make_certs(d))
            self.addCleanup(tls.close)
            recs = [{"name": "Bench", "type": "bambu", "address": "192.168.1.7",
                     "values": {"ip": "192.168.1.7", "serial": SERIAL.lower()}}]
            st = scan(quick(ports=closed_ports(bambu_mqtt=tls.port), fleet_records=lambda: recs))
            self.assertEqual([(f["already_added"], f["already_added_as"]) for f in st["found"]], [(True, "Bench")])

    def test_suggested_name_avoids_existing_names(self):
        mk = make_http(moonraker_routes("voron"))
        self.addCleanup(mk.shutdown)
        self.addCleanup(mk.server_close)
        recs = [{"name": "Voron", "type": "moonraker", "address": "http://10.0.0.5", "values": {"url": "http://10.0.0.5"}}]
        st = scan(quick(ports=closed_ports(moonraker=mk.server_address[1]), fleet_records=lambda: recs))
        self.assertEqual(st["found"][0]["suggested_name"], "voron-2")
        self.assertFalse(st["found"][0]["already_added"])

    def test_broken_fleet_provider_is_harmless(self):
        def boom():
            raise RuntimeError("x")
        m = quick(ports=closed_ports(), fleet_records=boom, local_ip=lambda: None)
        self.assertEqual(m.suggestions()["networks"], [])
        self.assertEqual(scan(m)["found"], [])

    def test_suggestions_from_configured_printers(self):
        recs = [
            {"name": "A", "type": "bambu", "address": "192.168.1.50", "values": {"ip": "192.168.1.50"}},
            {"name": "B", "type": "bambu", "address": "192.168.1.51", "values": {"ip": "192.168.1.51"}},
            {"name": "C", "type": "octoprint", "address": "http://10.0.5.9:5000", "values": {"url": "http://10.0.5.9:5000"}},
            {"name": "D", "type": "moonraker", "address": "http://voron.local", "values": {"url": "http://voron.local"}},
            {"name": "E", "type": "bambu", "address": "8.8.8.8", "values": {"ip": "8.8.8.8"}},
            {"name": "F", "type": "bambu", "address": "", "values": {}},
        ]
        m = DiscoveryManager(fleet_records=lambda: recs, local_ip=lambda: "192.168.1.77")
        s = m.suggestions()
        self.assertEqual(s["networks"], ["192.168.1.0/24", "10.0.5.0/24"])        # de-duplicated, hub's /24 already there
        self.assertEqual(s["sources"]["192.168.1.0/24"], "used by A")
        self.assertIn("Docker", s["note"])
        # a different LAN for the hub itself is appended; a Docker-bridge address is not
        m = DiscoveryManager(fleet_records=lambda: recs, local_ip=lambda: "192.168.9.4")
        self.assertEqual(m.suggestions()["networks"][-1], "192.168.9.0/24")
        self.assertEqual(m.suggestions()["sources"]["192.168.9.0/24"], "this Hub's own network")
        for docker in ("172.17.0.2", "172.20.0.3", "127.0.0.1"):
            m = DiscoveryManager(fleet_records=lambda: recs, local_ip=lambda d=docker: d)
            self.assertEqual(m.suggestions()["networks"], ["192.168.1.0/24", "10.0.5.0/24"], docker)
        self.assertEqual(DiscoveryManager(local_ip=lambda: None).suggestions()["networks"], [])

    def test_start_without_networks_uses_the_suggestions(self):
        recs = [{"name": "A", "type": "bambu", "address": "127.0.0.1", "values": {"ip": "127.0.0.1"}}]
        m = quick(ports=closed_ports(), fleet_records=lambda: recs, local_ip=lambda: None)
        r = m.start(None)
        self.assertEqual(r["networks"], ["127.0.0.0/24"])
        wait_done(m, r["scan_id"])
        with self.assertRaises(DiscoveryError):
            quick(local_ip=lambda: None).start(None)       # nothing to suggest -> a clear error


# ----------------------------------------------------------------- a pretend routed network

class SimNet:
    """Stands in for socket.create_connection (DiscoveryManager(connector=...)): a network with a round-trip
    time, hosts that answer or stay silent, and a burst limit - connects beyond `limit` in flight at once are
    silently dropped, like a congested NAT or subnet route. Real answers come from loopback fakes."""

    def __init__(self, latency=0.0, limit=None):
        self.latency, self.limit = latency, limit
        self.alive = set()            # hosts that refuse closed ports (they answer, so they are there)
        self.ports = {}               # (ip, port) -> loopback port that really serves it
        self.dead_ports = set()       # (ip, port) a firewall drops: never an answer
        self.drop_first = {}          # (ip, port) -> connects to drop before it starts answering
        self.extra = {}               # (ip, port) -> extra seconds of latency
        self.lock = threading.Lock()
        self.inflight = self.peak = self.dropped = 0
        self.far_inflight = self.far_peak = 0     # connects made by the far scan's own pool (not the round-trip check)

    def host(self, ip, **ports):
        self.alive.add(ip)

    def serve(self, ip, port, target):
        self.alive.add(ip)
        self.ports[(ip, port)] = target

    def __call__(self, ip, port, timeout):
        key = (ip, port)
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            far = threading.current_thread().name.startswith("disc-far")
            if far:
                self.far_inflight += 1
                self.far_peak = max(self.far_peak, self.far_inflight)
            over = self.limit is not None and self.inflight > self.limit
            silent = over or key in self.dead_ports or ip not in self.alive
            if over:
                self.dropped += 1
            elif not silent and self.drop_first.get(key, 0) > 0:
                self.drop_first[key] -= 1
                silent = True
        try:
            lat = self.latency + self.extra.get(key, 0.0)
            if silent or lat > timeout:
                time.sleep(timeout)
                raise socket.timeout("simulated: no answer")
            if lat:
                time.sleep(lat)
            target = self.ports.get(key)
            if target is None:
                raise ConnectionRefusedError("simulated: closed")
            return socket.create_connection(("127.0.0.1", target), timeout=timeout)
        finally:
            with self.lock:
                self.inflight -= 1
                if far:
                    self.far_inflight -= 1


def sim_mgr(net, **kw):
    """A manager on a SimNet. Timeouts are scaled down ~5x from the real defaults so the tests stay quick."""
    base = dict(cooldown=0, ssdp=False, zeroconf=False, connector=net, local_ip=lambda: "172.17.0.2",
                workers=64, connect_timeout=0.05, http_timeout=0.8, host_timeout=6.0, scan_timeout=60.0,
                far_workers=12, far_connect_timeout=0.3, far_http_timeout=0.8, far_start=3, far_step=2,
                far_tick=0.05, probe_timeout=0.2, probe_seconds=1.0, pause_seconds=2.0, retry_workers=4,
                retry_connect_timeout=0.4, retry_seconds=20.0, cancel_grace=0.5)
    base.update(kw)
    return DiscoveryManager(**base)


def watch(mgr, sid, timeout=40.0):
    """Poll a scan to its end; -> (final status, every distinct status line seen on the way)."""
    seen, end = [], time.time() + timeout
    while time.time() < end:
        st = mgr.status(sid)
        seen.append(st)
        if st["finished"]:
            return st, seen
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


class RoutedNetworks(unittest.TestCase):
    """Far (routed / slow) networks: a small ramping pool, a long connect timeout, a canary, a second look."""

    def serve(self, routes):
        srv = make_http(routes)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv.server_address[1]

    def routed(self, **kw):
        """A /28 with a 20 ms round trip, a burst limit of 5, a gateway and three printers."""
        net = SimNet(latency=0.02, **kw)
        net.host("10.77.1.1")
        for h in ("10.77.1.2", "10.77.1.3"):
            net.host(h)
        net.serve("10.77.1.5", 7125, self.serve(moonraker_routes("klipper-a")))
        net.serve("10.77.1.9", 80, self.serve(PRUSA_OPEN))
        net.serve("10.77.1.12", 7125, self.serve(moonraker_routes("klipper-b")))
        return net

    WANT = ["10.77.1.12", "10.77.1.5", "10.77.1.9"]

    @staticmethod
    def addrs(st):
        return sorted(f["address"] for f in st["found"])

    def test_the_simulated_burst_limit_really_breaks_the_old_fast_settings(self):
        net = self.routed(limit=5)
        st = scan(sim_mgr(net, far_rtt_ms=1e9, retry=False), ["10.77.1.0/28"])      # never "far": 64 workers, 0.05 s
        self.assertEqual(st["profiles"][0]["kind"], "near")
        self.assertGreater(net.dropped, 10)
        self.assertLess(len(st["found"]), 3, "the burst limit should have cost real printers")

    def test_far_network_finds_every_printer_under_a_burst_limit(self):
        net = self.routed(limit=5)
        m = sim_mgr(net)
        st, seen = watch(m, m.start(["10.77.1.0/28"])["scan_id"])
        self.assertEqual(self.addrs(st), self.WANT)
        self.assertEqual(st["state"], "done")
        self.assertEqual((st["remote"], st["profiles"][0]["kind"]), (True, "far"))
        self.assertEqual(st["remote_networks"], ["10.77.1.0/28"])
        self.assertGreaterEqual(st["profiles"][0]["rtt_ms"], 15)
        self.assertTrue(st["slowed"], "the pool was never found to be too big - the limit did not bite")
        phases = {x["phase"] for x in seen}
        self.assertLessEqual({"scanning"}, phases)
        self.assertTrue(any(x["eta_s"] is not None for x in seen), "no time estimate while scanning")
        done = [x["done"] for x in seen]
        self.assertEqual(done, sorted(done))                  # progress only moves forward
        self.assertEqual(st["done"], st["total"])

    def test_healthy_far_network_costs_no_second_look(self):
        net = self.routed()                                    # same network, no burst limit
        st = scan(sim_mgr(net), ["10.77.1.0/28"])
        self.assertEqual(self.addrs(st), self.WANT)
        self.assertEqual((st["slowed"], st["recheck"]), (False, {"total": 0, "done": 0}))

    def test_far_pool_starts_small_and_stays_within_its_cap(self):
        net = self.routed()
        m = sim_mgr(net, far_workers=8, far_start=2, far_step=2)
        st, _ = watch(m, m.start(["10.77.1.0/28"])["scan_id"])
        self.assertEqual(self.addrs(st), self.WANT)
        self.assertLessEqual(net.far_peak, 8)
        self.assertGreaterEqual(net.far_peak, 4)

    def test_local_network_stays_fast_and_wide(self):
        net = SimNet(latency=0.0)
        net.host("10.77.2.1")
        net.serve("10.77.2.7", 7125, self.serve(moonraker_routes("klipper-a")))
        net.serve("10.77.2.99", 80, self.serve(PRUSA_OPEN))
        m = sim_mgr(net)
        t0 = time.time()
        st = scan(m, ["10.77.2.0/24"])
        took = time.time() - t0
        self.assertEqual(self.addrs(st), ["10.77.2.7", "10.77.2.99"])
        self.assertEqual((st["profiles"][0]["kind"], st["remote"]), ("near", False))
        self.assertEqual((st["slowed"], st["recheck"]["total"]), (False, 0))
        self.assertLess(took, 6.0, "a local /24 must stay quick")
        self.assertGreater(net.peak, 20, "a local network is scanned with the wide pool")

    def test_what_makes_a_network_far(self):
        def kind(latency, net_cidr="10.77.3.0/24", **kw):
            net = SimNet(latency=latency)
            net.host("10.77.3.1")
            m = sim_mgr(net, **kw)
            return m._classify(None, discovery.ipaddress.ip_network(net_cidr), time.monotonic() + 3)
        self.assertEqual(kind(0.0)["kind"], "near")
        far = kind(0.03)
        self.assertEqual(far["kind"], "far")
        self.assertTrue(far["canaries"], "a far network keeps the hosts that answered as canaries")
        self.assertEqual(kind(0.03, far_rtt_ms=100)["kind"], "near")          # the limit is a setting
        # nobody answered the quick check: unknown, so treated as far
        c = sim_mgr(SimNet())._classify(None, discovery.ipaddress.ip_network("10.77.3.0/24"), time.monotonic() + 3)
        self.assertEqual((c["kind"], c["canaries"]), ("far", []))
        # the Hub's own network is near without asking; a VPN overlay range is far whatever its speed
        m = sim_mgr(SimNet(), local_ip=lambda: "10.77.3.50")
        self.assertEqual(m._classify(None, discovery.ipaddress.ip_network("10.77.3.0/24"), time.monotonic())["kind"], "near")
        m = sim_mgr(SimNet())
        self.assertEqual(m._classify(None, discovery.ipaddress.ip_network("100.100.5.0/24"), time.monotonic() + 2)["kind"], "far")

    def test_known_printers_are_the_canaries(self):
        net = self.routed()
        recs = [{"name": "A", "type": "moonraker", "address": "http://10.77.1.5:7125",
                 "values": {"url": "http://10.77.1.5:7125"}},
                {"name": "B", "type": "bambu", "address": "10.77.1.13", "values": {"ip": "10.77.1.13"}}]
        m = sim_mgr(net, fleet_records=lambda: recs)
        self.assertEqual(m._known_endpoints(discovery.ipaddress.ip_network("10.77.1.0/28")),
                         [("10.77.1.5", 7125), ("10.77.1.13", 8883)])
        c = m._classify(None, discovery.ipaddress.ip_network("10.77.1.0/28"), time.monotonic() + 3)
        self.assertEqual(c["canaries"][0], ("10.77.1.5", 7125))

    def test_a_known_printer_that_came_back_silent_is_looked_at_again(self):
        net = self.routed()
        net.drop_first.update({("10.77.1.5", 80): 1, ("10.77.1.5", 8883): 1,
                               ("10.77.1.5", 7125): 2})        # the first connects are lost (the round-trip check eats one)
        recs = [{"name": "A", "type": "moonraker", "address": "http://10.77.1.5:7125",
                 "values": {"url": "http://10.77.1.5:7125"}}]
        st = scan(sim_mgr(net, fleet_records=lambda: recs), ["10.77.1.0/28"])
        self.assertEqual(self.addrs(st), self.WANT)
        self.assertEqual(st["recheck"], {"total": 1, "done": 1})
        mine = [f for f in st["found"] if f["address"] == "10.77.1.5"][0]
        self.assertTrue(mine["already_added"])
        # without the second look the same printer is missed - that is what the retry buys
        net2 = self.routed()
        net2.drop_first.update({("10.77.1.5", 80): 1, ("10.77.1.5", 8883): 1, ("10.77.1.5", 7125): 2})
        st = scan(sim_mgr(net2, fleet_records=lambda: recs, retry=False), ["10.77.1.0/28"])
        self.assertNotIn("10.77.1.5", self.addrs(st))

    def test_hosts_probed_while_the_route_was_dropping_are_looked_at_again(self):
        net = self.routed(limit=5)
        m = sim_mgr(net)
        st = scan(m, ["10.77.1.0/28"])
        self.assertEqual(self.addrs(st), self.WANT)
        self.assertTrue(st["slowed"])
        self.assertGreaterEqual(st["recheck"]["total"], 1)     # the tainted hosts were rechecked
        self.assertEqual(st["recheck"]["done"], st["recheck"]["total"])

    def test_a_far_network_that_answered_nothing_gets_a_full_second_look(self):
        net = SimNet(latency=0.02)                             # no gateway answers: no canary, nothing vouches for the silence
        net.serve("10.77.4.3", 7125, self.serve(moonraker_routes("klipper-a")))
        for pt in (80, 7125, 8883):
            net.drop_first[("10.77.4.3", pt)] = 1
        st = scan(sim_mgr(net, probe_seconds=0.6), ["10.77.4.0/29"])
        self.assertEqual(self.addrs(st), ["10.77.4.3"])
        self.assertGreaterEqual(st["recheck"]["total"], 1)
        self.assertEqual(sim_mgr(net, retry=False).retry, False)

    def test_a_slow_host_is_not_called_empty(self):
        def make(retry):
            net = SimNet(latency=0.02)
            net.serve("10.77.5.4", 7125, self.serve(moonraker_routes("klipper-a")))
            for pt in (80, 7125, 8883):
                net.extra[("10.77.5.4", pt)] = 0.3             # answers, but slower than the connect timeout
            return sim_mgr(net, far_connect_timeout=0.15, retry_connect_timeout=0.8, probe_seconds=0.6, retry=retry)
        self.assertEqual(self.addrs(scan(make(False), ["10.77.5.0/29"])), [])
        st = scan(make(True), ["10.77.5.0/29"])
        self.assertEqual(self.addrs(st), ["10.77.5.4"])

    def test_a_host_that_only_opens_port_80_is_found_in_the_first_pass(self):
        net = SimNet(latency=0.0)
        net.serve("10.77.6.4", 80, self.serve(PRUSA_OPEN))
        net.dead_ports |= {("10.77.6.4", 8883), ("10.77.6.4", 7125)}     # a firewall that drops everything else
        st = scan(sim_mgr(net, retry=False), ["10.77.6.0/29"])
        self.assertEqual([(f["family"], f["address"]) for f in st["found"]], [("prusalink", "10.77.6.4")])

    def test_the_scan_has_a_hard_time_limit_and_says_so(self):
        net = SimNet(latency=0.02)                             # 254 silent addresses would take ~10 s here
        m = sim_mgr(net, scan_timeout=1.5, far_workers=4, far_start=4, probe_seconds=0.4)
        t0 = time.time()
        st = scan(m, ["10.77.7.0/24"])
        self.assertLess(time.time() - t0, 5.0)
        self.assertEqual(st["state"], "done")
        self.assertTrue(st["note"].startswith("Stopped at the time limit"), st["note"])

    def test_the_second_look_is_time_boxed(self):
        net = SimNet(latency=0.02)
        m = sim_mgr(net, retry_seconds=1.0, retry_workers=2, probe_seconds=0.4, far_workers=16,
                    far_connect_timeout=0.15, retry_connect_timeout=0.4)
        t0 = time.time()
        st = scan(m, ["10.77.8.0/25"])                          # 126 silent hosts, all of them retried in a full second look
        self.assertEqual(st["state"], "done")
        self.assertLess(st["recheck"]["done"], st["recheck"]["total"])
        self.assertIn("Looked again at", st["note"])
        self.assertLess(time.time() - t0, 20.0)


class GateTests(unittest.TestCase):
    def test_slow_start_halve_and_remember_the_ceiling(self):
        g = discovery._Gate(cap=16, start=4, step=2)
        self.assertEqual(g.window, 4)
        g.grow()
        g.grow()
        self.assertEqual(g.window, 8)
        g.shrink()                                             # overloaded at 8: back to 4, never beyond 6 again
        self.assertEqual((g.window, g.cap), (4, 6))
        for _ in range(10):
            g.grow()
        self.assertEqual(g.window, 6)
        for _ in range(10):
            g.shrink()
        self.assertGreaterEqual(g.window, 2)                   # never below the floor

    def test_the_gate_holds_hosts_back_while_paused(self):
        g = discovery._Gate(cap=4, start=4)
        cancel = threading.Event()
        g.pause(True)
        out = []
        th = threading.Thread(target=lambda: out.append(g.enter(cancel)))
        th.start()
        time.sleep(0.25)
        self.assertEqual(out, [])
        g.pause(False)
        th.join(2)
        self.assertEqual(out, [True])
        g.leave()
        cancel.set()
        self.assertFalse(g.enter(cancel))


class Lifecycle(unittest.TestCase):
    def test_one_scan_at_a_time_and_cooldown(self):
        gate = threading.Event()
        m = quick(cooldown=0.3)
        m._scan_host = lambda ip: gate.wait(5) and []
        first = m.start(["127.0.0.0/30"])
        with self.assertRaises(discovery.Busy) as cm:
            m.start(["127.0.0.0/30"])
        self.assertEqual((cm.exception.status, cm.exception.payload()["scan_id"]), (409, first["scan_id"]))
        gate.set()
        wait_done(m, first["scan_id"])
        with self.assertRaises(discovery.RateLimited) as cm:
            m.start(["127.0.0.0/30"])
        self.assertEqual(cm.exception.status, 429)
        self.assertIn("retry_after", cm.exception.payload())
        time.sleep(0.35)
        wait_done(m, m.start(["127.0.0.0/30"])["scan_id"])

    def test_cancel(self):
        m = quick(workers=2)
        m._scan_host = lambda ip: time.sleep(0.05) or []
        sid = m.start(["10.1.0.0/22"])["scan_id"]            # 1022 hosts
        time.sleep(0.1)
        self.assertTrue(m.cancel(sid)["cancelling"])
        st = wait_done(m, sid)
        self.assertEqual(st["state"], "cancelled")
        self.assertLess(st["done"], st["total"] + 1)

    def test_unknown_scan_and_history_cap(self):
        m = quick(keep=3, ports=closed_ports())
        with self.assertRaises(discovery.NotFound):
            m.status("nope")
        with self.assertRaises(discovery.NotFound):
            m.cancel("nope")
        ids = [wait_done(m, m.start(LOOP)["scan_id"])["scan_id"] for _ in range(5)]
        with self.assertRaises(discovery.NotFound):
            m.status(ids[0])
        self.assertEqual(m.status(ids[-1])["state"], "done")

    def test_scan_and_wait(self):
        mk = make_http(moonraker_routes("voron"))
        self.addCleanup(mk.shutdown)
        self.addCleanup(mk.server_close)
        m = quick(ports=closed_ports(moonraker=mk.server_address[1]))
        st = m.scan_and_wait(LOOP, timeout=20)
        self.assertEqual((st["state"], [f["family"] for f in st["found"]]), ("done", ["moonraker"]))

    def test_results_are_sorted_by_address(self):
        m = quick()
        mk = {"family": "moonraker", "port": 7125, "model": None, "serial": None, "hostname": None, "details": {}}
        m._scan_host = lambda ip: [dict(mk, address=ip)] if ip.endswith((".3", ".10", ".2")) else []
        st = scan(m, ["127.0.0.0/28"])
        self.assertEqual([f["address"] for f in st["found"]], ["127.0.0.2", "127.0.0.3", "127.0.0.10"])


class SuggestName(unittest.TestCase):
    def test_always_a_valid_unique_fleet_name(self):
        import re
        rx = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
        cases = [{"family": "bambu", "address": "1.1.1.1", "model": "Bambu Lab X1 Carbon", "serial": "00M00A123456789"},
                 {"family": "moonraker", "address": "1.1.1.2", "hostname": "Mainsail OS.local"},
                 {"family": "octoprint", "address": "1.1.1.3"},
                 {"family": "prusalink", "address": "1.1.1.4", "hostname": "___"},
                 {"family": "moonraker", "address": "1.1.1.5", "hostname": "x" * 80},
                 {"family": "moonraker", "address": "1.1.1.6", "hostname": "all"}]
        names = [discovery.suggest_name(c, ["P1S", "p1s-2"]) for c in cases]
        for n in names:
            self.assertRegex(n, rx)
        self.assertEqual(names[0], "X1-Carbon")
        self.assertNotEqual(names[5].lower(), "all")
        self.assertEqual(discovery.suggest_name({"family": "bambu", "address": "1", "model": "Bambu Lab P1S"}, ["P1S", "p1s-2"]), "P1S-3")


class MulticastExtras(unittest.TestCase):
    SSDP = (b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nCache-Control: max-age=1800\r\n"
            b"Location: 127.0.0.1\r\nNT: urn:bambulab-com:device:3dprinter:1\r\nNTS: ssdp:alive\r\n"
            b"USN: 01P00A000000001\r\nServer: UPnP/1.0\r\nDevModel.bambu.com: C12\r\n"
            b"DevName.bambu.com: Fake Bench P1S\r\nDevConnect.bambu.com: lan\r\nDevBind.bambu.com: free\r\n\r\n")

    def test_parse_ssdp(self):
        r = discovery.parse_ssdp(self.SSDP, ("127.0.0.9", 1))
        self.assertEqual(r, {"ip": "127.0.0.1", "serial": SERIAL, "name": "Fake Bench P1S",
                             "model_code": "C12", "connect": "lan"})
        # no usable Location -> the sender's address
        r = discovery.parse_ssdp(self.SSDP.replace(b"Location: 127.0.0.1", b"Location: nope"), ("127.0.0.9", 1))
        self.assertEqual(r["ip"], "127.0.0.9")
        self.assertIsNone(discovery.parse_ssdp(b"NOTIFY * HTTP/1.1\r\nUSN: uuid:1234\r\nNT: upnp:rootdevice\r\n\r\n", ("1.1.1.1", 1)))
        self.assertIsNone(discovery.parse_ssdp(b"\xff\xfe garbage", ("1.1.1.1", 1)))

    def _send_repeatedly(self, port, seconds=2.0):
        stop = time.time() + seconds

        def go():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            while time.time() < stop:
                s.sendto(self.SSDP, ("127.0.0.1", port))
                time.sleep(0.05)
            s.close()
        th = threading.Thread(target=go, daemon=True)
        th.start()
        return th

    def test_ssdp_listener_adds_a_printer_the_scan_could_not_see(self):
        up = free_port()
        self._send_repeatedly(up)
        m = quick(ssdp=True, ssdp_seconds=0.8, ssdp_ports=(up,), ssdp_bind="127.0.0.1", ssdp_join=False,
                  ports=closed_ports())
        st = scan(m)
        self.assertEqual(st["extras"]["ssdp_heard"], 1)
        self.assertEqual(len(st["found"]), 1)
        f = st["found"][0]
        self.assertEqual((f["family"], f["source"], f["serial"], f["hostname"], f["model"]),
                         ("bambu", "ssdp", SERIAL, "Fake Bench P1S", "Bambu Lab P1S"))
        self.assertEqual(f["prefill"]["values"]["serial"], SERIAL)

    def test_ssdp_outside_the_scanned_networks_is_ignored(self):
        up = free_port()
        self._send_repeatedly(up)
        m = quick(ssdp=True, ssdp_seconds=0.8, ssdp_ports=(up,), ssdp_bind="127.0.0.1", ssdp_join=False,
                  ports=closed_ports())
        st = scan(m, ["127.0.0.2/32"])            # the NOTIFY says 127.0.0.1, which we were not asked to scan
        self.assertEqual(st["found"], [])

    @unittest.skipUnless(HAVE_OPENSSL, "needs the openssl CLI")
    def test_ssdp_fills_in_the_hostname_of_a_tcp_finding(self):
        with tempfile.TemporaryDirectory() as d:
            tls = FakeTLS(*make_certs(d))
            self.addCleanup(tls.close)
            up = free_port()
            self._send_repeatedly(up)
            m = quick(ssdp=True, ssdp_seconds=0.8, ssdp_ports=(up,), ssdp_bind="127.0.0.1", ssdp_join=False,
                      ports=closed_ports(bambu_mqtt=tls.port))
            st = scan(m)
            self.assertEqual(len(st["found"]), 1)
            f = st["found"][0]
            self.assertEqual((f["source"], f["hostname"], f["serial"]), ("tcp", "Fake Bench P1S", SERIAL))
            self.assertEqual(f["details"]["ssdp_model_code"], "C12")
            self.assertEqual(f["suggested_name"], "Fake-Bench-P1S")

    def test_ssdp_port_in_use_is_not_an_error(self):
        busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        busy.bind(("127.0.0.1", 0))
        self.addCleanup(busy.close)
        m = quick(ssdp=True, ssdp_seconds=0.2, ssdp_ports=(busy.getsockname()[1],), ssdp_bind="127.0.0.1",
                  ssdp_join=False, ports=closed_ports())
        self.assertEqual(scan(m)["state"], "done")          # SO_REUSEADDR may or may not allow it; either way no crash

    def test_zeroconf_names_a_found_printer(self):
        class State:
            Added = "added"

        class Info:
            server = "octopi-bench.local."
            def parsed_addresses(self): return ["127.0.0.1"]

        class ZC:
            closed = False
            def get_service_info(self, t, n, timeout=0): return Info()
            def close(self): ZC.closed = True

        class Browser:
            def __init__(self, zc, types, handlers=None):
                for h in handlers:
                    h(zc, types[0], "octopi._octoprint._tcp.local.", State.Added)
            def cancel(self): pass

        fake = type("zeroconf", (), {"Zeroconf": ZC, "ServiceBrowser": Browser, "ServiceStateChange": State})
        srv = make_http(OCTO_LOCKED)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        m = quick(zeroconf=True, zeroconf_seconds=0.05, zeroconf_module=fake,
                  ports=closed_ports(http=(srv.server_address[1],)))
        st = scan(m)
        self.assertEqual(st["found"][0]["hostname"], "octopi-bench.local")
        self.assertEqual(st["found"][0]["suggested_name"], "octopi-bench")
        self.assertTrue(ZC.closed)

    def test_missing_zeroconf_library_is_fine(self):
        self.assertNotIn("zeroconf", sys.modules)         # it is not a dependency of the Hub
        m = quick(zeroconf=True, zeroconf_seconds=0.05, ports=closed_ports())
        self.assertEqual(scan(m)["state"], "done")


try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    HAVE_STARLETTE = True
except Exception:
    HAVE_STARLETTE = False


@unittest.skipUnless(HAVE_STARLETTE, "needs starlette + httpx")
class HttpContract(unittest.TestCase):
    def client(self, mgr):
        app = Starlette(routes=[Route(p, fn, methods=ms) for p, ms, fn in discovery.routes(mgr)])
        return TestClient(app)

    def test_contract_end_to_end(self):
        mk = make_http(moonraker_routes("voron"))
        self.addCleanup(mk.shutdown)
        self.addCleanup(mk.server_close)
        recs = [{"name": "Old", "type": "bambu", "address": "127.0.0.9", "values": {"ip": "127.0.0.9"}}]
        mgr = quick(ports=closed_ports(moonraker=mk.server_address[1]), fleet_records=lambda: recs, local_ip=lambda: None)
        c = self.client(mgr)

        r = c.get("/api/discover/suggestions")              # must not be swallowed by /{scan_id}
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["networks"], ["127.0.0.0/24"])

        r = c.post("/api/discover", json={"networks": ["127.0.0.1/32"]})
        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertEqual((body["total"], body["networks"]), (1, ["127.0.0.1/32"]))
        sid = body["scan_id"]
        end = time.time() + 20
        while time.time() < end:
            st = c.get(f"/api/discover/{sid}").json()
            if st["finished"]:
                break
            time.sleep(0.02)
        self.assertEqual((st["state"], st["done"], st["total"]), ("done", 1, 1))
        f = st["found"][0]
        for key in ("family", "address", "port", "model", "serial", "hostname", "already_added", "details"):
            self.assertIn(key, f)
        self.assertEqual((f["family"], f["hostname"]), ("moonraker", "voron"))
        json.dumps(st)                                      # fully serialisable

    def test_errors(self):
        mgr = quick(ports=closed_ports(), local_ip=lambda: None)
        c = self.client(mgr)
        r = c.post("/api/discover", json={"networks": ["8.8.8.0/24"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("private", r.json()["error"])
        self.assertEqual(c.post("/api/discover", json={"networks": ["10.0.0.0/8"]}).status_code, 400)
        self.assertEqual(c.post("/api/discover", content=b"not json").status_code, 400)     # nothing to suggest either
        self.assertEqual(c.get("/api/discover/nope").status_code, 404)
        self.assertEqual(c.delete("/api/discover/nope").status_code, 404)

    def test_busy_and_cancel(self):
        gate = threading.Event()
        mgr = quick(local_ip=lambda: None)
        mgr._scan_host = lambda ip: gate.wait(5) and []
        c = self.client(mgr)
        sid = c.post("/api/discover", json={"networks": ["127.0.0.0/30"]}).json()["scan_id"]
        r = c.post("/api/discover", json={"networks": ["127.0.0.0/30"]})
        self.assertEqual((r.status_code, r.json()["busy"], r.json()["scan_id"]), (409, True, sid))
        self.assertEqual(c.delete(f"/api/discover/{sid}").json()["cancelling"], True)
        gate.set()
        wait_done(mgr, sid)
        self.assertEqual(c.get(f"/api/discover/{sid}").json()["state"], "cancelled")


class Safety(unittest.TestCase):
    def test_default_ports_are_only_printer_ports(self):
        flat = set()
        for v in discovery.DEFAULT_PORTS.values():
            flat.update([v] if isinstance(v, int) else v)
        self.assertEqual(flat, {8883, 990, 6000, 7125, 80, 5000})

    def test_module_is_stdlib_only(self):
        with open(os.path.join(HERE, "..", "app", "discovery.py")) as fh:
            src = fh.read()
        for third in ("import requests", "import paho", "import cryptography", "import zeroconf\n", "import httpx"):
            self.assertNotIn(third, src.replace("            import zeroconf as mod", ""))


if __name__ == "__main__":
    unittest.main(verbosity=1)
