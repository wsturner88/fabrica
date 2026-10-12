"""Tests for app/notify.py - every channel against a fake server on 127.0.0.1, plus the
Notifier's policy (toggles, mutes, quiet hours, rate limit, test messages).

    python3 tests/test_notify.py

Loopback only; no real Telegram / ntfy / Discord / mail server is ever contacted:
  * Telegram, ntfy, Discord and the generic webhook are one throwaway HTTP server;
  * SMTP is a small threaded fake speaking just enough SMTP for smtplib (EHLO, STARTTLS,
    AUTH PLAIN/LOGIN, MAIL/RCPT/DATA, QUIT), plain, STARTTLS and implicit SSL (the TLS
    variants need the openssl CLI to make a certificate and are skipped without it).
    aiosmtpd is not used (not installed, and Python 3.12+ has no smtpd module).
"""
import base64
import email
import email.policy
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
from email import message_from_bytes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
for _k in ("no_proxy", "NO_PROXY"):
    os.environ[_k] = "127.0.0.1,localhost"

import notify  # noqa: E402
import settings_store as ss  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes" * 20 + b"\xff\xd9"
TG_TOKEN = "987654321:AAFakeTelegramToken_for_tests_only"
DISCORD_SECRET = "dsc_secret_part_ABCdef123456"


# ----------------------------------------------------------------- fake HTTP server

class FakeHTTP:
    """Records every request; `respond(request) -> (status, body_bytes)` can be replaced."""

    def __init__(self):
        self.requests = []
        self.respond = lambda r: (200, b"{}")
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                req = {"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body}
                outer.requests.append(req)
                status, resp = outer.respond(req)
                self.send_response(status)
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            do_POST = do_PUT = do_GET = _handle

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def parse_multipart(req):
    ctype = req["headers"].get("Content-Type", "")
    assert ctype.startswith("multipart/form-data"), ctype
    msg = message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + req["body"])
    out = {}
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        out[name] = {"filename": part.get_filename(), "type": part.get_content_type(),
                     "data": part.get_payload(decode=True)}
    return out


def tg_ok(r):
    return 200, b'{"ok":true,"result":{}}'


# ----------------------------------------------------------------- fake SMTP server

def make_cert(d):
    cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                        "-out", cert, "-days", "2", "-subj", "/CN=localhost"], capture_output=True)
    return (cert, key) if r.returncode == 0 else None


class FakeSMTP:
    def __init__(self, cert=None, implicit_ssl=False, starttls=False, require_auth=None, refuse_rcpt=None):
        self.messages, self.commands = [], []
        self.cert, self.implicit_ssl, self.starttls = cert, implicit_ssl, starttls
        self.require_auth, self.refuse_rcpt = require_auth, refuse_rcpt
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._ctx = None
        if cert:
            self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._ctx.load_cert_chain(*cert)
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(10)
            tls = False
            if self.implicit_ssl:
                conn = self._ctx.wrap_socket(conn, server_side=True)
                tls = True
            f = conn.makefile("rwb", buffering=0)

            def say(line):
                f.write(line.encode() + b"\r\n")

            say("220 fake.local ESMTP ready")
            st = {"from": None, "rcpts": [], "user": None, "pw": None, "tls": tls}
            while True:
                line = f.readline()
                if not line:
                    return
                cmd = line.decode(errors="replace").rstrip("\r\n")
                self.commands.append(cmd.split(" ")[0].upper() if not cmd.upper().startswith("AUTH") else "AUTH")
                up = cmd.upper()
                if up.startswith("EHLO") or up.startswith("HELO"):
                    say("250-fake.local")
                    if self.starttls and not st["tls"]:
                        say("250-STARTTLS")
                    say("250-AUTH PLAIN LOGIN")
                    say("250 8BITMIME")
                elif up == "STARTTLS":
                    say("220 go ahead")
                    conn = self._ctx.wrap_socket(conn, server_side=True)
                    f = conn.makefile("rwb", buffering=0)
                    st["tls"] = True
                elif up.startswith("AUTH PLAIN"):
                    arg = cmd[10:].strip()
                    if not arg:
                        say("334 ")
                        arg = f.readline().decode().strip()
                    _, user, pw = base64.b64decode(arg).decode().split("\0")
                    st["user"], st["pw"] = user, pw
                    if self.require_auth and (user, pw) != self.require_auth:
                        say("535 5.7.8 Authentication credentials invalid")
                    else:
                        say("235 2.7.0 Authentication successful")
                elif up.startswith("AUTH LOGIN"):
                    say("334 VXNlcm5hbWU6")
                    user = base64.b64decode(f.readline().strip()).decode()
                    say("334 UGFzc3dvcmQ6")
                    pw = base64.b64decode(f.readline().strip()).decode()
                    st["user"], st["pw"] = user, pw
                    say("235 ok" if not self.require_auth or (user, pw) == self.require_auth else "535 bad")
                elif up.startswith("MAIL FROM"):
                    st["from"] = cmd.split(":", 1)[1].strip().strip("<>").split(">")[0]
                    say("250 ok")
                elif up.startswith("RCPT TO"):
                    rcpt = cmd.split(":", 1)[1].strip().strip("<>").split(">")[0]
                    if self.refuse_rcpt and rcpt == self.refuse_rcpt:
                        say("550 5.1.1 no such user")
                    else:
                        st["rcpts"].append(rcpt)
                        say("250 ok")
                elif up == "DATA":
                    say("354 end with <CRLF>.<CRLF>")
                    data = b""
                    while True:
                        ln = f.readline()
                        if ln in (b".\r\n", b""):
                            break
                        data += ln[1:] if ln.startswith(b"..") else ln
                    self.messages.append({**st, "data": data})
                    say("250 queued")
                elif up == "RSET":
                    say("250 ok")
                elif up == "NOOP":
                    say("250 ok")
                elif up == "QUIT":
                    say("221 bye")
                    return
                else:
                    say("502 not implemented")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def parsed(msg):
    return message_from_bytes(msg["data"], policy=email.policy.default)


# ----------------------------------------------------------------- channel tests

class RedactTests(unittest.TestCase):
    def test_redact_known_and_shaped_secrets(self):
        t = notify.redact(f"bad https://api.telegram.org/bot{TG_TOKEN}/send and {DISCORD_SECRET} "
                          "at /webhooks/123456789/abcDEF_tok-en.x header Bearer abcdef123456", [DISCORD_SECRET])
        for leak in (TG_TOKEN, "AAFakeTelegram", DISCORD_SECRET, "abcDEF_tok", "abcdef123456"):
            self.assertNotIn(leak, t)

    def test_short_secrets_not_blanket_replaced(self):
        self.assertEqual(notify.redact("hello world", ["a"]), "hello world")


class HttpChannelTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeHTTP()
        self.addCleanup(self.srv.close)

    # -- Telegram
    def tg(self, **cfg):
        c = {"bot_token": TG_TOKEN, "chat_id": "-100123", **cfg}
        return notify.TelegramChannel(c, api_base=self.srv.url)

    def test_telegram_send_message(self):
        self.srv.respond = tg_ok
        ok, err = self.tg().send("P1S: print FINISHED", "Benchy done.", level="info")
        self.assertEqual((ok, err), (True, None))
        r = self.srv.requests[0]
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["path"], f"/bot{TG_TOKEN}/sendMessage")
        j = json.loads(r["body"])
        self.assertEqual(j["chat_id"], "-100123")
        self.assertEqual(j["text"], "P1S: print FINISHED\nBenchy done.")

    def test_telegram_send_photo_multipart(self):
        self.srv.respond = tg_ok
        ok, err = self.tg().send("Title", "Body text", image_jpeg=JPEG)
        self.assertTrue(ok, err)
        self.assertEqual(len(self.srv.requests), 1)
        r = self.srv.requests[0]
        self.assertTrue(r["path"].endswith("/sendPhoto"))
        parts = parse_multipart(r)
        self.assertEqual(parts["chat_id"]["data"], b"-100123")
        self.assertEqual(parts["caption"]["data"].decode(), "Title\nBody text")
        self.assertEqual(parts["photo"]["data"], JPEG)
        self.assertEqual(parts["photo"]["filename"], "snapshot.jpg")
        self.assertEqual(parts["photo"]["type"], "image/jpeg")

    def test_telegram_long_text_with_photo_also_sends_full_message(self):
        self.srv.respond = tg_ok
        ok, _ = self.tg().send("T", "x" * 1500, image_jpeg=JPEG)
        self.assertTrue(ok)
        self.assertEqual([r["path"].rsplit("/", 1)[1] for r in self.srv.requests], ["sendPhoto", "sendMessage"])
        self.assertLessEqual(len(parse_multipart(self.srv.requests[0])["caption"]["data"].decode()), 1024)

    def test_telegram_errors_are_clean_and_secret_free(self):
        self.srv.respond = lambda r: (401, b'{"ok":false,"error_code":401,"description":"Unauthorized"}')
        ok, err = self.tg().send("a", "b")
        self.assertFalse(ok)
        self.assertIn("bot token", err)
        self.assertNotIn(TG_TOKEN, err)
        self.srv.respond = lambda r: (400, f'{{"ok":false,"description":"Bad Request: chat not found {TG_TOKEN}"}}'.encode())
        ok, err = self.tg().send("a", "b")
        self.assertFalse(ok)
        self.assertIn("chat not found", err)
        self.assertNotIn(TG_TOKEN, err)
        self.assertNotIn("AAFake", err)

    # -- ntfy
    def ntfy(self, **cfg):
        return notify.NtfyChannel({"server_url": self.srv.url, "topic": "my topic", "token": "", **cfg})

    def test_ntfy_headers(self):
        ok, err = self.ntfy(token="tk_secrettoken123").send("P1S: print FAILED", "Spaghetti.\nSecond line", level="failure")
        self.assertTrue(ok, err)
        r = self.srv.requests[0]
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["path"], "/my%20topic")
        h = {k.lower(): v for k, v in r["headers"].items()}
        self.assertEqual(h["title"], "P1S: print FAILED")
        self.assertEqual(h["priority"], "5")
        self.assertEqual(h["tags"], "rotating_light")
        self.assertEqual(h["authorization"], "Bearer tk_secrettoken123")
        self.assertEqual(r["body"].decode(), "Spaghetti.\nSecond line")

    def test_ntfy_priority_by_level_and_unicode_title(self):
        for level, prio, tag in (("info", "3", "information_source"), ("warning", "4", "warning")):
            self.srv.requests.clear()
            self.ntfy().send("t", "b", level=level)
            h = {k.lower(): v for k, v in self.srv.requests[0]["headers"].items()}
            self.assertEqual((h["priority"], h["tags"]), (prio, tag))
        self.srv.requests.clear()
        ok, err = self.ntfy().send("Kühler Drücker ✓", "b")
        self.assertTrue(ok, err)
        title = {k.lower(): v for k, v in self.srv.requests[0]["headers"].items()}["title"]
        self.assertEqual(str(email.header.make_header(email.header.decode_header(title))), "Kühler Drücker ✓")

    def test_ntfy_image_put_attachment(self):
        ok, err = self.ntfy().send("Title", "Line1\nLine2", image_jpeg=JPEG)
        self.assertTrue(ok, err)
        r = self.srv.requests[0]
        self.assertEqual(r["method"], "PUT")
        self.assertEqual(r["body"], JPEG)
        h = {k.lower(): v for k, v in r["headers"].items()}
        self.assertEqual(h["filename"], "snapshot.jpg")
        self.assertEqual(h["title"], "Title")
        self.assertEqual(h["message"], "Line1\\nLine2")

    def test_ntfy_image_falls_back_to_text_when_attachments_refused(self):
        self.srv.respond = lambda r: (413, b"attachment too large") if r["method"] == "PUT" else (200, b"{}")
        ok, err = self.ntfy().send("Title", "Body", image_jpeg=JPEG)
        self.assertTrue(ok, err)
        self.assertEqual([r["method"] for r in self.srv.requests], ["PUT", "POST"])

    def test_ntfy_http_error(self):
        self.srv.respond = lambda r: (403, b'{"code":40301,"error":"forbidden"}')
        ok, err = self.ntfy().send("a", "b")
        self.assertFalse(ok)
        self.assertIn("403", err)
        # the server's own error text is not echoed (a Test button must not read an internal service's answer)
        self.assertNotIn("forbidden", err)
        self.assertIn("check the token", err)

    # -- Discord
    def discord(self, **cfg):
        return notify.DiscordChannel({"webhook_url": f"{self.srv.url}/api/webhooks/123456789/{DISCORD_SECRET}", **cfg})

    def test_discord_json(self):
        self.srv.respond = lambda r: (204, b"")
        ok, err = self.discord().send("Title", "Body")
        self.assertTrue(ok, err)
        r = self.srv.requests[0]
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["headers"]["Content-Type"], "application/json")
        self.assertNotIn("Python-urllib", r["headers"]["User-Agent"])
        j = json.loads(r["body"])
        self.assertEqual(j["content"], "**Title**\nBody")

    def test_discord_multipart_with_file(self):
        self.srv.respond = lambda r: (204, b"")
        ok, err = self.discord().send("Title", "Body", image_jpeg=JPEG)
        self.assertTrue(ok, err)
        parts = parse_multipart(self.srv.requests[0])
        self.assertEqual(json.loads(parts["payload_json"]["data"])["content"], "**Title**\nBody")
        self.assertEqual(parts["files[0]"]["data"], JPEG)
        self.assertEqual(parts["files[0]"]["filename"], "snapshot.jpg")

    def test_discord_error_never_leaks_the_webhook_secret(self):
        self.srv.respond = lambda r: (404, f'{{"message":"Unknown Webhook {DISCORD_SECRET}"}}'.encode())
        ok, err = self.discord().send("a", "b")
        self.assertFalse(ok)
        self.assertIn("404", err)
        self.assertNotIn(DISCORD_SECRET, err)

    # -- generic webhook
    def test_webhook_payload_and_bearer(self):
        ch = notify.WebhookChannel({"url": self.srv.url + "/hook", "bearer": "bearer_value_123"})
        ok, err = ch.send("Title", "Body", image_jpeg=JPEG, level="warning",
                          meta={"event": "print_paused", "printer": "P1S"})
        self.assertTrue(ok, err)
        r = self.srv.requests[0]
        self.assertEqual(r["path"], "/hook")
        self.assertEqual(r["headers"]["Authorization"], "Bearer bearer_value_123")
        j = json.loads(r["body"])
        self.assertEqual({k: j[k] for k in ("source", "version", "title", "body", "level", "event", "printer")},
                         {"source": "print-hub", "version": 1, "title": "Title", "body": "Body",
                          "level": "warning", "event": "print_paused", "printer": "P1S"})
        self.assertEqual(base64.b64decode(j["image_jpeg_base64"]), JPEG)
        self.assertRegex(j["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        ch.send("t", "b")
        self.assertNotIn("image_jpeg_base64", json.loads(self.srv.requests[1]["body"]))

    def test_webhook_non_2xx_is_failure(self):
        self.srv.respond = lambda r: (500, b"boom")
        ok, err = notify.WebhookChannel({"url": self.srv.url}).send("a", "b")
        self.assertFalse(ok)
        self.assertIn("500", err)


class RobustnessTests(unittest.TestCase):
    def test_missing_fields_reported(self):
        ok, err = notify.TelegramChannel({"bot_token": "", "chat_id": ""}).send("a", "b")
        self.assertFalse(ok)
        self.assertIn("bot token", err)
        self.assertIn("chat id", err)

    def test_never_raises_on_unreachable_or_garbage(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                           # nothing listens here now
        for ch in (notify.TelegramChannel({"bot_token": TG_TOKEN, "chat_id": "1"}, api_base=f"http://127.0.0.1:{port}", timeout=2),
                   notify.NtfyChannel({"server_url": f"http://127.0.0.1:{port}", "topic": "t"}, timeout=2),
                   notify.DiscordChannel({"webhook_url": f"http://127.0.0.1:{port}/api/webhooks/1/{DISCORD_SECRET}"}, timeout=2),
                   notify.WebhookChannel({"url": f"http://127.0.0.1:{port}/"}, timeout=2),
                   notify.WebhookChannel({"url": "http://[bad"}, timeout=2),
                   notify.EmailChannel({"host": "127.0.0.1", "port": port, "tls": "none", "from_addr": "a@example.com",
                                        "to_addr": "b@example.com"}, timeout=2)):
            ok, err = ch.send("a", "b", image_jpeg=JPEG)
            self.assertFalse(ok, type(ch).__name__)
            self.assertTrue(err)
            self.assertNotIn(TG_TOKEN, err)
            self.assertNotIn(DISCORD_SECRET, err)

    def test_slow_server_times_out(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)                                         # accepts the TCP connection, never answers
        self.addCleanup(s.close)
        t0 = time.time()
        ok, err = notify.WebhookChannel({"url": f"http://127.0.0.1:{s.getsockname()[1]}/"}, timeout=1).send("a", "b")
        self.assertFalse(ok)
        self.assertLess(time.time() - t0, 6)

    def test_unknown_level_treated_as_info(self):
        srv = FakeHTTP()
        self.addCleanup(srv.close)
        ok, _ = notify.NtfyChannel({"server_url": srv.url, "topic": "t"}).send("a", "b", level="catastrophe")
        self.assertTrue(ok)


class EmailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certdir = tempfile.mkdtemp()
        cls.cert = make_cert(cls.certdir) if shutil.which("openssl") else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.certdir, True)

    def chan(self, srv, **kw):
        cfg = {"host": "127.0.0.1", "port": srv.port, "tls": "none", "username": "", "password": "",
               "from_addr": "Fabrica <hub@example.com>", "to_addr": "me@example.com, you@example.com",
               "verify_cert": False}
        cfg.update(kw)
        return notify.EmailChannel(cfg, timeout=5)

    def test_plain_with_image_attachment(self):
        srv = FakeSMTP()
        self.addCleanup(srv.close)
        ok, err = self.chan(srv).send("P1S: print FINISHED", "Benchy done.\nSecond line", image_jpeg=JPEG)
        self.assertTrue(ok, err)
        m = srv.messages[0]
        self.assertEqual(m["from"], "hub@example.com")
        self.assertEqual(m["rcpts"], ["me@example.com", "you@example.com"])
        self.assertFalse(m["tls"])
        msg = parsed(m)
        self.assertEqual(msg["Subject"], "P1S: print FINISHED")
        self.assertIn("Fabrica", msg["From"])
        self.assertIn("Benchy done.", msg.get_body(("plain",)).get_content())
        atts = list(msg.iter_attachments())
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0].get_content_type(), "image/jpeg")
        self.assertEqual(atts[0].get_filename(), "snapshot.jpg")
        self.assertEqual(atts[0].get_content(), JPEG)

    def test_no_image_no_attachment_and_unicode_subject(self):
        srv = FakeSMTP()
        self.addCleanup(srv.close)
        ok, err = self.chan(srv).send("Drücker ✓", "b")
        self.assertTrue(ok, err)
        msg = parsed(srv.messages[0])
        self.assertEqual(msg["Subject"], "Drücker ✓")
        self.assertEqual(list(msg.iter_attachments()), [])

    def test_auth_success_and_failure_without_leaking_password(self):
        srv = FakeSMTP(require_auth=("mailuser", "s3cretPassw0rd"))
        self.addCleanup(srv.close)
        ok, err = self.chan(srv, username="mailuser", password="s3cretPassw0rd").send("a", "b")
        self.assertTrue(ok, err)
        self.assertEqual(srv.messages[0]["user"], "mailuser")
        ok, err = self.chan(srv, username="mailuser", password="WrongPassw0rd!").send("a", "b")
        self.assertFalse(ok)
        self.assertIn("username/password", err)
        self.assertNotIn("WrongPassw0rd", err)

    def test_refused_recipient(self):
        srv = FakeSMTP(refuse_rcpt="you@example.com")
        self.addCleanup(srv.close)
        ok, err = self.chan(srv, to_addr="you@example.com").send("a", "b")
        self.assertFalse(ok)
        self.assertIn("you@example.com", err)

    def test_starttls(self):
        if not self.cert:
            self.skipTest("openssl CLI not available")
        srv = FakeSMTP(cert=self.cert, starttls=True)
        self.addCleanup(srv.close)
        ok, err = self.chan(srv, tls="starttls").send("a", "b", image_jpeg=JPEG)
        self.assertTrue(ok, err)
        self.assertTrue(srv.messages[0]["tls"])
        self.assertIn("STARTTLS", srv.commands)

    def test_starttls_not_offered_is_an_error_not_a_downgrade(self):
        srv = FakeSMTP()
        self.addCleanup(srv.close)
        ok, err = self.chan(srv, tls="starttls").send("a", "b")
        self.assertFalse(ok)
        self.assertIn("STARTTLS", err)
        self.assertEqual(srv.messages, [])

    def test_implicit_ssl(self):
        if not self.cert:
            self.skipTest("openssl CLI not available")
        srv = FakeSMTP(cert=self.cert, implicit_ssl=True)
        self.addCleanup(srv.close)
        ok, err = self.chan(srv, tls="ssl").send("a", "b")
        self.assertTrue(ok, err)
        self.assertTrue(srv.messages[0]["tls"])

    def test_self_signed_rejected_when_verifying(self):
        if not self.cert:
            self.skipTest("openssl CLI not available")
        srv = FakeSMTP(cert=self.cert, implicit_ssl=True)
        self.addCleanup(srv.close)
        ok, err = self.chan(srv, tls="ssl", verify_cert=True).send("a", "b")
        self.assertFalse(ok)
        self.assertIn("Verify server certificate", err)


# ----------------------------------------------------------------- quiet hours

class QuietHoursTests(unittest.TestCase):
    def at(self, hh, mm=0, tz="UTC", day=15):
        import datetime
        return datetime.datetime(2026, 3, day, hh, mm, tzinfo=datetime.timezone.utc).timestamp()

    def test_overnight_range(self):
        q = lambda h, m=0: notify.in_quiet_hours(self.at(h, m), "22:00", "07:00")
        self.assertTrue(q(22, 0))
        self.assertTrue(q(23, 30))
        self.assertTrue(q(0, 0))
        self.assertTrue(q(6, 59))
        self.assertFalse(q(7, 0))
        self.assertFalse(q(12, 0))
        self.assertFalse(q(21, 59))

    def test_same_day_range_and_empty_range(self):
        self.assertTrue(notify.in_quiet_hours(self.at(14), "13:00", "15:00"))
        self.assertFalse(notify.in_quiet_hours(self.at(15), "13:00", "15:00"))
        self.assertFalse(notify.in_quiet_hours(self.at(12, 59), "13:00", "15:00"))
        self.assertFalse(notify.in_quiet_hours(self.at(3), "08:00", "08:00"))

    def test_uses_the_configured_time_zone(self):
        try:
            import zoneinfo
            zoneinfo.ZoneInfo("America/Chicago")
        except Exception:
            self.skipTest("no tz database")
        # 03:00 UTC on 15 March 2026 = 22:00 (Sunday 15th minus 1 day...) CDT is UTC-5 after 8 March
        ts = self.at(3)
        self.assertTrue(notify.in_quiet_hours(ts, "22:00", "07:00", "America/Chicago"))      # 22:00 local
        self.assertFalse(notify.in_quiet_hours(ts, "23:00", "07:00", "America/Chicago"))
        self.assertFalse(notify.in_quiet_hours(self.at(15), "22:00", "07:00", "America/Chicago"))   # 10:00 local


# ----------------------------------------------------------------- Notifier

class Ev:
    def __init__(self, kind="print_finished", printer="P1S", ts=None, dedupe_key="", image=None, title=None, body="Body"):
        self.kind, self.printer, self.ts, self.dedupe_key = kind, printer, ts, dedupe_key
        self.title = title or f"{printer}: {kind}"
        self.body, self.level = body, ss.event_level(kind)
        self._image, self.image_calls = image, 0

    def get_image(self):
        self.image_calls += 1
        return self._image


class NotifierBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.srv = FakeHTTP()
        self.addCleanup(self.srv.close)
        self.settings = ss.SettingsStore(self.dir, env={})
        self.now = [1_800_000_000.0]
        self.n = notify.Notifier(self.settings, clock=lambda: self.now[0], timeout=5,
                                 channel_options={"telegram": {"api_base": self.srv.url}})
        self.addCleanup(self.n.close)
        self.settings.update({"ntfy": {"enabled": True, "server_url": self.srv.url, "topic": "hub"}})

    def received(self):
        return [r for r in self.srv.requests]

    def at(self, hh, mm=0):
        import datetime
        return datetime.datetime(2026, 3, 15, hh, mm, tzinfo=datetime.timezone.utc).timestamp()


class NotifierPolicyTests(NotifierBase):
    def test_basic_delivery(self):
        res = self.n.notify(Ev())
        self.assertTrue(res["sent"])
        self.assertEqual(res["channels"]["ntfy"], {"ok": True, "error": None})
        self.assertEqual(len(self.received()), 1)

    def test_master_switch_and_event_toggle(self):
        self.settings.update({"notifications.print_finished": False})
        self.assertEqual(self.n.notify(Ev())["skipped"], "event_off")
        self.settings.update({"notifications.print_finished": True, "notifications.enabled": False})
        self.assertEqual(self.n.notify(Ev())["skipped"], "notifications_off")
        self.assertEqual(self.received(), [])
        self.settings.update({"notifications.enabled": True})
        self.assertEqual(self.n.notify(Ev("print_started"))["skipped"], "event_off")     # off by default
        self.assertEqual(self.n.notify(Ev("made_up"))["skipped"], "unknown_event")

    def test_muted_printer(self):
        self.settings.update({"notifications.muted_printers": ["p1s"]})
        self.assertEqual(self.n.notify(Ev(printer="P1S"))["skipped"], "printer_muted")
        self.assertTrue(self.n.notify(Ev(printer="A1"))["sent"])

    def test_no_channels(self):
        self.settings.update({"ntfy.enabled": False})
        self.assertEqual(self.n.notify(Ev())["skipped"], "no_channels")
        self.settings.update({"ntfy.enabled": True, "ntfy.topic": ""})          # enabled but incomplete
        self.assertEqual(self.n.notify(Ev())["skipped"], "no_channels")

    def test_quiet_hours_overnight_with_failure_breakthrough(self):
        self.settings.update({"notifications.quiet_enabled": True})              # 22:00 -> 07:00 UTC
        night = self.at(2, 30)
        self.assertEqual(self.n.notify(Ev("print_finished", ts=night))["skipped"], "quiet_hours")
        self.assertEqual(self.n.notify(Ev("printer_offline", ts=night))["skipped"], "quiet_hours")
        self.assertTrue(self.n.notify(Ev("print_failed", ts=night))["sent"])
        self.assertTrue(self.n.notify(Ev("backup_failed", ts=night))["sent"])
        self.assertTrue(self.n.notify(Ev("spaghetti_suspected", ts=night))["sent"])
        self.assertTrue(self.n.notify(Ev("print_finished", ts=self.at(12)))["sent"])         # daytime
        self.assertTrue(self.n.notify(Ev("print_finished", printer="B", ts=self.at(7, 0)))["sent"])   # end exclusive

    def test_quiet_hours_breakthrough_is_configurable(self):
        self.settings.update({"notifications.quiet_enabled": True,
                              "notifications.quiet_breakthrough": ["print_finished"]})
        night = self.at(23)
        self.assertTrue(self.n.notify(Ev("print_finished", ts=night))["sent"])
        self.assertEqual(self.n.notify(Ev("print_failed", ts=night))["skipped"], "quiet_hours")

    def test_quiet_hours_off_means_always(self):
        self.assertTrue(self.n.notify(Ev(ts=self.at(3)))["sent"])

    def test_rate_limit_same_printer_and_event(self):
        self.assertTrue(self.n.notify(Ev("printer_offline"))["sent"])
        self.now[0] += 60
        self.assertEqual(self.n.notify(Ev("printer_offline"))["skipped"], "duplicate")
        self.assertTrue(self.n.notify(Ev("printer_offline", printer="A1"))["sent"])        # other printer
        self.assertTrue(self.n.notify(Ev("printer_online"))["sent"])                       # other event
        self.now[0] += 5 * 60
        self.assertTrue(self.n.notify(Ev("printer_offline"))["sent"])                      # window passed
        self.assertEqual(len(self.received()), 4)

    def test_rate_limit_keyed_on_job_and_configurable(self):
        self.assertTrue(self.n.notify(Ev("print_finished", dedupe_key="a.3mf"))["sent"])
        self.assertTrue(self.n.notify(Ev("print_finished", dedupe_key="b.3mf"))["sent"])
        self.assertEqual(self.n.notify(Ev("print_finished", dedupe_key="b.3mf"))["skipped"], "duplicate")
        self.settings.update({"notifications.duplicate_window_min": 0})
        self.assertTrue(self.n.notify(Ev("print_finished", dedupe_key="b.3mf"))["sent"])

    def test_skipped_events_do_not_consume_the_rate_limit(self):
        self.settings.update({"notifications.print_finished": False})
        self.n.notify(Ev())
        self.settings.update({"notifications.print_finished": True})
        self.assertTrue(self.n.notify(Ev())["sent"])

    def test_snapshot_attached_only_when_enabled(self):
        ev = Ev(image=JPEG)
        self.n.notify(ev)
        self.assertEqual(ev.image_calls, 1)
        self.assertEqual(self.received()[0]["method"], "PUT")
        self.assertEqual(self.received()[0]["body"], JPEG)
        self.settings.update({"notifications.include_snapshot": False})
        ev2 = Ev(printer="A1", image=JPEG)
        self.n.notify(ev2)
        self.assertEqual(ev2.image_calls, 0)
        self.assertEqual(self.received()[1]["method"], "POST")

    def test_image_fetch_failure_still_sends_text(self):
        class Boom(Ev):
            def get_image(self):
                raise RuntimeError("camera exploded")
        self.assertTrue(self.n.notify(Boom())["sent"])
        self.assertEqual(self.received()[0]["method"], "POST")

    def test_fan_out_to_several_channels_one_failing(self):
        hook = FakeHTTP()
        self.addCleanup(hook.close)
        hook.respond = lambda r: (500, b"nope")
        self.settings.update({"webhook": {"enabled": True, "url": hook.url + "/h"}})
        self.settings.update({"telegram": {"enabled": True, "chat_id": "5", "bot_token": TG_TOKEN}})
        self.srv.respond = lambda r: tg_ok(r) if r["path"].startswith("/bot") else (200, b"{}")
        res = self.n.notify(Ev())
        self.assertTrue(res["sent"])
        self.assertEqual({k: v["ok"] for k, v in res["channels"].items()},
                         {"telegram": True, "ntfy": True, "webhook": False})
        self.assertIn("500", res["channels"]["webhook"]["error"])
        self.assertTrue(any(r["path"].endswith("/sendMessage") for r in self.srv.requests))
        self.assertEqual(self.n.recent()[0]["event"], "print_finished")
        self.assertNotIn(TG_TOKEN, json.dumps(self.n.recent()))

    def test_submit_runs_in_background(self):
        self.assertTrue(self.n.submit(Ev()))
        self.assertTrue(self.n.drain(5))
        self.assertEqual(len(self.received()), 1)

    def test_notify_never_raises(self):
        class Weird:
            kind = "print_finished"
        self.assertFalse(self.n.notify(Weird())["sent"])


class SendTestTests(NotifierBase):
    def test_bypasses_every_toggle(self):
        self.settings.update({"notifications.enabled": False, "notifications.quiet_enabled": True,
                              "ntfy.enabled": False, "notifications.muted_printers": ["P1S"]})
        res = self.n.send_test("ntfy")
        self.assertEqual(res, {"channel": "ntfy", "ok": True, "error": None})
        r = self.received()[0]
        self.assertIn("test", r["body"].decode().lower())

    def test_unknown_channel_and_unconfigured(self):
        self.assertFalse(self.n.send_test("carrier-pigeon")["ok"])
        res = self.n.send_test("telegram")
        self.assertFalse(res["ok"])
        self.assertIn("missing", res["error"])

    def test_unsaved_form_values_override_and_blank_secret_uses_saved(self):
        self.settings.update({"telegram": {"bot_token": TG_TOKEN, "chat_id": "1"}})
        self.srv.respond = tg_ok
        res = self.n.send_test("telegram", {"chat_id": "777", "bot_token": ""})
        self.assertTrue(res["ok"], res)
        r = self.received()[-1]
        self.assertIn(TG_TOKEN, r["path"])
        self.assertEqual(json.loads(r["body"])["chat_id"], "777")
        self.assertEqual(self.settings.get("telegram.chat_id"), "1")        # nothing was saved
        bad = self.n.send_test("telegram", {"chat_id": "x" * 100})
        self.assertFalse(bad["ok"])

    def test_error_reply_has_no_secret(self):
        self.settings.update({"telegram": {"bot_token": TG_TOKEN, "chat_id": "1"}})
        self.srv.respond = lambda r: (401, b'{"ok":false,"description":"Unauthorized"}')
        res = self.n.send_test("telegram")
        self.assertFalse(res["ok"])
        self.assertNotIn(TG_TOKEN, json.dumps(res))

    def test_email_test_through_settings(self):
        smtp = FakeSMTP()
        self.addCleanup(smtp.close)
        self.settings.update({"email": {"enabled": True, "host": "127.0.0.1", "port": smtp.port, "tls": "none",
                                        "from_addr": "hub@example.com", "to_addr": "me@example.com"}})
        res = self.n.send_test("email")
        self.assertTrue(res["ok"], res)
        self.assertIn("test message", parsed(smtp.messages[0])["Subject"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
