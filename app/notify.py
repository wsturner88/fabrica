"""Notification channels + the Notifier that decides what gets sent where.

Channels (stdlib only - urllib, smtplib, email; nothing to install):

    TelegramChannel   Bot API sendMessage / sendPhoto (multipart)
    NtfyChannel       HTTP POST with Title / Priority / Tags headers; PUT for a picture
    EmailChannel      smtplib, STARTTLS / SSL / none, picture attached
    DiscordChannel    webhook, multipart with the picture as a file
    WebhookChannel    JSON POST (documented in docs/settings.md)

Every channel has the same call:

    ok, error = channel.send(title, body, image_jpeg=None, level="info")

It never raises, has a hard timeout, and `error` is a sentence fit for a human with every
secret (tokens, passwords, webhook URLs) already removed.

Notifier sits on top: given an Event (see events.py) it applies the master switch, the
per-event toggle, the per-printer mute list, quiet hours (overnight ranges, with a list of
events that break through) and a duplicate rate limit, then fans out to every enabled
channel in parallel. send_test(channel) bypasses all of that.

Nothing here talks to a real service on its own: API base URLs are constructor arguments
(Telegram's `api_base`) or user settings (ntfy server, SMTP host, webhook URLs), so tests
point them at fake servers on localhost.
"""

import atexit
import base64
import collections
import concurrent.futures
import datetime
import email.header
import email.utils
import html as _html
import json
import queue
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import weakref
from email.message import EmailMessage

import security                      # standalone (stdlib only): scrub_text strips user:password@ from URLs in free text
import settings_store as ss

DEFAULT_TIMEOUT = 10.0
USER_AGENT = "Fabrica-Notify/1.0"
LEVELS = ("info", "warning", "failure")
TELEGRAM_API = "https://api.telegram.org"


# ---------------------------------------------------------------- redaction

_PATTERNS = [
    re.compile(r"bot\d{4,}:[A-Za-z0-9_-]{10,}"),                    # Telegram token inside a URL
    re.compile(r"(?<![A-Za-z0-9])\d{6,}:[A-Za-z0-9_-]{20,}"),        # bare Telegram token
    re.compile(r"/webhooks/\d+/[A-Za-z0-9_.-]+"),                    # Discord webhook path
    re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"),      # auth header echoed back
]


def redact(text, secrets=()) -> str:
    """Remove every known secret value (and anything shaped like a bot token / webhook
    path / auth header) from `text`."""
    t = str(text)
    for s in sorted((x for x in secrets if x and len(x) >= 3), key=len, reverse=True):
        t = t.replace(s, "***")
        t = t.replace(urllib.parse.quote(s, safe=""), "***")
    for p in _PATTERNS:
        t = p.sub("***", t)
    return t


class ChannelError(Exception):
    """Internal: a failure with a message already fit for a human."""


# ---------------------------------------------------------------- small HTTP helpers

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would re-send the Authorization header (webhook bearer, ntfy token) to whatever
    host answered, and turn a POST into a GET. It is reported as an error instead."""

    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def http_request(method, url, headers=None, data=None, timeout=DEFAULT_TIMEOUT):
    """(status, body_bytes). HTTP error statuses (3xx included - redirects are never followed) are
    returned, not raised; only network level failures raise ChannelError."""
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(4096)
        except Exception:
            body = b""
        finally:
            e.close()
        return e.code, body
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise ChannelError(f"could not reach the server ({reason})") from None
    except (TimeoutError, OSError) as e:
        raise ChannelError(f"could not reach the server ({e})") from None
    except ValueError as e:
        raise ChannelError(f"bad address ({e})") from None


def multipart(fields: dict, files: list):
    """files: [(field_name, filename, content_type, bytes)]. Returns (body, content_type)."""
    boundary = "----Fabrica" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
                     + str(v).encode("utf-8") + b"\r\n")
    for name, fname, ctype, blob in files:
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                      f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
                     + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _http_error_text(status, body: bytes, show_body=False) -> str:
    """'server answered 404 (hint)'. The server's own error text is only shown when `show_body` (a fixed, trusted
    host such as Telegram): the Test button of a webhook / ntfy channel must not become a way to read the answer
    of whatever internal service its URL points at."""
    snippet = ""
    if not show_body:
        body = b""
    try:
        j = json.loads(body.decode("utf-8", "replace"))
        if isinstance(j, dict):
            snippet = str(j.get("description") or j.get("error") or j.get("message") or "")
    except ValueError:
        snippet = body.decode("utf-8", "replace").strip()
    snippet = " ".join(snippet.split())[:200]
    hint = {301: "moved - use the new address", 302: "moved - use the new address",
            303: "moved - use the new address", 307: "moved - use the new address",
            308: "moved - use the new address", 401: "check the token/credentials", 403: "not allowed - check the token or permissions",
            404: "not found - check the address, topic or token", 429: "rate limited - try again later"}.get(status, "")
    return f"server answered {status}" + (f": {snippet}" if snippet else "") + (f" ({hint})" if hint and not snippet else "")


def _clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _one_line(text):
    return " ".join(str(text).split())


# ---------------------------------------------------------------- channels

class Channel:
    NAME = ""
    LABEL = ""
    REQUIRED = ()          # (config key, human label)

    def __init__(self, cfg: dict, timeout=DEFAULT_TIMEOUT, **opts):
        self.cfg = dict(cfg or {})
        self.timeout = timeout
        self.opts = opts

    def missing(self) -> list:
        return [label for key, label in self.REQUIRED if not str(self.cfg.get(key) or "").strip()]

    def _secret_values(self):
        out = []
        for f in ss.SECTIONS[self.NAME]["fields"]:
            if not f["secret"]:
                continue
            v = str(self.cfg.get(f["name"]) or "")
            out.append(v)
            if "://" in v:             # a secret URL: its token lives in the path / query pieces
                u = urllib.parse.urlparse(v)
                out += [seg for seg in u.path.split("/") if len(seg) >= 6]
                out += [x for q in urllib.parse.parse_qsl(u.query) for x in q if len(x) >= 6]
        return out

    def send(self, title, body, image_jpeg=None, level="info", meta=None):
        gaps = self.missing()
        if gaps:
            return False, "not set up yet - missing " + ", ".join(gaps)
        try:
            # a printer's address with user:password@ in it must never leave the Hub inside an alert (N-7)
            meta = dict(meta or {})
            if meta.get("html"):
                meta["html"] = security.scrub_text(str(meta["html"]))
            self._send(security.scrub_text(str(title or "")), security.scrub_text(str(body or "")),
                       image_jpeg or None, level if level in LEVELS else "info", meta)
            return True, None
        except ChannelError as e:
            return False, security.scrub_text(redact(str(e), self._secret_values()))
        except Exception as e:     # never raise to the caller
            return False, security.scrub_text(redact(f"unexpected error ({type(e).__name__}: {e})", self._secret_values()))

    def _send(self, title, body, image, level, meta):
        raise NotImplementedError


class TelegramChannel(Channel):
    NAME, LABEL = "telegram", "Telegram"
    REQUIRED = (("bot_token", "bot token"), ("chat_id", "chat id"))

    def _call(self, method, data=None, files=None):
        base = (self.opts.get("api_base") or TELEGRAM_API).rstrip("/")
        url = f"{base}/bot{self.cfg['bot_token'].strip()}/{method}"
        if files:
            blob, ctype = multipart(data or {}, files)
            status, body = http_request("POST", url, {"Content-Type": ctype}, blob, self.timeout)
        else:
            status, body = http_request("POST", url, {"Content-Type": "application/json"},
                                        json.dumps(data or {}).encode(), self.timeout)
        try:
            j = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            j = {}
        if status != 200 or not (isinstance(j, dict) and j.get("ok")):
            desc = (j.get("description") if isinstance(j, dict) else "") or _http_error_text(status, body, show_body=True)
            if status == 401 or "Unauthorized" in str(desc):
                desc = "Telegram rejected the bot token (check it with @BotFather)"
            raise ChannelError(f"Telegram: {desc}")

    CAPTION_MAX = 1024

    def _post(self, chat, text, image, parse):
        """One message: the picture with `text` as its caption when it fits, else the picture and then the text.
        `parse` = "HTML" when `text` is Telegram HTML. A picture Telegram refuses (too big, not a JPEG) is dropped
        and the text goes out alone - the words matter more than the camera frame."""
        extra = {"parse_mode": parse} if parse else {}
        plain_len = len(_html.unescape(re.sub(r"<[^>]+>", "", text))) if parse else len(text)
        if image:
            caption = text if plain_len <= self.CAPTION_MAX else None
            try:
                self._call("sendPhoto", {"chat_id": chat, **({"caption": caption, **extra} if caption else {})},
                           [("photo", "snapshot.jpg", "image/jpeg", image)])
                if caption:
                    return
            except ChannelError as e:
                if str(e).startswith("Telegram: ") and "parse entities" not in str(e):
                    image = None             # Telegram answered and said no to the picture: text only
                else:
                    raise
        self._call("sendMessage", {"chat_id": chat, "text": text, **extra,
                                   "link_preview_options": {"is_disabled": True}})

    def _send(self, title, body, image, level, meta):
        chat = self.cfg["chat_id"].strip()
        text = f"{title}\n{body}".strip()
        rich = (meta or {}).get("html")
        if rich and len(rich) <= 4000:
            try:
                return self._post(chat, rich, image, "HTML")
            except ChannelError as e:
                if "parse entities" not in str(e):
                    raise                    # only a markup problem falls back to plain text
        if image:
            caption = _clip(text, self.CAPTION_MAX)
            self._call("sendPhoto", {"chat_id": chat, "caption": caption},
                       [("photo", "snapshot.jpg", "image/jpeg", image)])
            if len(text) > self.CAPTION_MAX:
                self._call("sendMessage", {"chat_id": chat, "text": _clip(text, 4096),
                                           "link_preview_options": {"is_disabled": True}})
        else:
            self._call("sendMessage", {"chat_id": chat, "text": _clip(text, 4096),
                                       "link_preview_options": {"is_disabled": True}})


def _hdr(value: str) -> str:
    """HTTP header values must be latin-1; ntfy accepts RFC 2047 for anything else."""
    v = _one_line(value)
    try:
        v.encode("ascii")
        return v
    except UnicodeEncodeError:
        return email.header.Header(v, "utf-8").encode()


class NtfyChannel(Channel):
    NAME, LABEL = "ntfy", "ntfy"
    REQUIRED = (("server_url", "server URL"), ("topic", "topic"))
    PRIORITY = {"info": "3", "warning": "4", "failure": "5"}
    TAGS = {"info": "information_source", "warning": "warning", "failure": "rotating_light"}

    def _send(self, title, body, image, level, meta):
        url = self.cfg["server_url"].strip().rstrip("/") + "/" + urllib.parse.quote(self.cfg["topic"].strip(), safe="")
        hdrs = {"Priority": self.PRIORITY[level], "Tags": self.TAGS[level]}
        if title:
            hdrs["Title"] = _hdr(title)
        if self.cfg.get("token"):
            hdrs["Authorization"] = "Bearer " + self.cfg["token"].strip()
        if image:
            h2 = dict(hdrs, Filename="snapshot.jpg", **{"Content-Type": "image/jpeg"})
            h2["Message"] = _hdr(_clip(body, 500).replace("\n", "\\n")) if body else ""
            if not h2["Message"]:
                h2.pop("Message")
            try:
                status, resp = http_request("PUT", url, h2, image, self.timeout)
                if 200 <= status < 300:
                    return
            except ChannelError:
                pass            # fall through: a plain text message still beats nothing
        hdrs["Content-Type"] = "text/plain; charset=utf-8"
        status, resp = http_request("POST", url, hdrs, _clip(body or title, 4000).encode("utf-8"), self.timeout)
        if not 200 <= status < 300:
            raise ChannelError("ntfy: " + _http_error_text(status, resp))


class EmailChannel(Channel):
    NAME, LABEL = "email", "Email"
    REQUIRED = (("host", "SMTP server"), ("from_addr", "from address"), ("to_addr", "to address"))

    def _send(self, title, body, image, level, meta):
        cfg = self.cfg
        recipients = [a.strip() for a in re.split(r"[,;]", cfg["to_addr"]) if a.strip()]
        if not recipients:
            raise ChannelError("no recipient address")
        msg = EmailMessage()
        msg["Subject"] = _one_line(title) or "Fabrica"
        msg["From"] = cfg["from_addr"].strip()
        msg["To"] = ", ".join(recipients)
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain="print-hub.local")
        msg.set_content(body or title)
        if image:
            msg.add_attachment(image, maintype="image", subtype="jpeg", filename="snapshot.jpg")
        mode = (cfg.get("tls") or "starttls").lower()
        port = int(cfg.get("port") or (465 if mode == "ssl" else 587))
        ctx = self.opts.get("ssl_context")
        if ctx is None:
            ctx = ssl.create_default_context()
            if cfg.get("verify_cert") is False:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        envelope_from = email.utils.parseaddr(cfg["from_addr"])[1] or cfg["from_addr"].strip()
        try:
            if mode == "ssl":
                srv = smtplib.SMTP_SSL(cfg["host"].strip(), port, timeout=self.timeout, context=ctx)
            else:
                srv = smtplib.SMTP(cfg["host"].strip(), port, timeout=self.timeout)
            with srv:
                srv.ehlo()
                if mode == "starttls":
                    if not srv.has_extn("starttls"):
                        raise ChannelError("the server does not offer STARTTLS - pick a different "
                                           "encryption setting or port")
                    srv.starttls(context=ctx)
                    srv.ehlo()
                if cfg.get("username"):
                    srv.login(cfg["username"], cfg.get("password") or "")
                srv.send_message(msg, from_addr=envelope_from, to_addrs=recipients)
        except smtplib.SMTPAuthenticationError:
            raise ChannelError("the mail server rejected the username/password "
                               "(Gmail, Outlook and iCloud need an app password)") from None
        except smtplib.SMTPRecipientsRefused as e:
            raise ChannelError("the mail server refused the recipient address: " +
                               ", ".join(str(a) for a in e.recipients)) from None
        except smtplib.SMTPException as e:
            raise ChannelError(f"mail server error ({type(e).__name__}: {e})") from None
        except ssl.SSLError as e:
            raise ChannelError(f"TLS problem ({e.reason or e}) - for a self-signed LAN server turn off "
                               "'Verify server certificate'") from None
        except (OSError, TimeoutError) as e:
            raise ChannelError(f"could not connect to {cfg['host']}:{port} ({e})") from None


class DiscordChannel(Channel):
    NAME, LABEL = "discord", "Discord"
    REQUIRED = (("webhook_url", "webhook URL"),)

    def _send(self, title, body, image, level, meta):
        text = _clip((f"**{title}**\n{body}" if title else body), 2000)
        url = self.cfg["webhook_url"].strip()
        payload = {"content": text, "username": meta.get("hub_name") or "Fabrica"}
        if image:
            blob, ctype = multipart({"payload_json": json.dumps(payload)},
                                    [("files[0]", "snapshot.jpg", "image/jpeg", image)])
            status, resp = http_request("POST", url, {"Content-Type": ctype}, blob, self.timeout)
        else:
            status, resp = http_request("POST", url, {"Content-Type": "application/json"},
                                        json.dumps(payload).encode(), self.timeout)
        if not 200 <= status < 300:
            raise ChannelError("Discord: " + _http_error_text(status, resp))


class WebhookChannel(Channel):
    NAME, LABEL = "webhook", "Webhook"
    REQUIRED = (("url", "URL"),)

    def _send(self, title, body, image, level, meta):
        payload = {"source": "print-hub", "version": 1, "title": title, "body": body, "level": level,
                   "event": meta.get("event", ""), "printer": meta.get("printer", ""),
                   "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if image:
            payload["image_jpeg_base64"] = base64.b64encode(image).decode()
        hdrs = {"Content-Type": "application/json"}
        if self.cfg.get("bearer"):
            hdrs["Authorization"] = "Bearer " + self.cfg["bearer"].strip()
        status, resp = http_request("POST", self.cfg["url"].strip(), hdrs,
                                    json.dumps(payload).encode(), self.timeout)
        if not 200 <= status < 300:
            raise ChannelError("webhook: " + _http_error_text(status, resp))


CHANNEL_CLASSES = {c.NAME: c for c in (TelegramChannel, NtfyChannel, EmailChannel, DiscordChannel, WebhookChannel)}


def build_channel(name: str, cfg: dict, timeout=DEFAULT_TIMEOUT, **opts) -> Channel:
    cls = CHANNEL_CLASSES.get(name)
    if cls is None:
        raise KeyError(name)
    return cls(cfg, timeout=timeout, **opts)


# ---------------------------------------------------------------- quiet hours

def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(now_ts: float, start: str, end: str, tz_name: str = "UTC") -> bool:
    """True if local time (in tz_name) is inside [start, end). start > end means the range
    crosses midnight (22:00 -> 07:00). start == end is an empty range."""
    local = datetime.datetime.fromtimestamp(now_ts, ss.tzinfo_for(tz_name))
    cur, a, b = local.hour * 60 + local.minute, _minutes(start), _minutes(end)
    if a == b:
        return False
    return a <= cur < b if a < b else (cur >= a or cur < b)


# ---------------------------------------------------------------- the Notifier

def _origin(url) -> tuple:
    """(scheme, host, port) of a URL - what a bearer token or password is actually sent to."""
    try:
        u = urllib.parse.urlsplit(str(url or "").strip())
        port = u.port or {"http": 80, "https": 443}.get(u.scheme.lower(), 0)
        return (u.scheme.lower(), (u.hostname or "").lower(), port)
    except ValueError:
        return ("?", str(url or "").strip().lower(), 0)


def destination(channel: str, cfg: dict) -> tuple:
    """Where a channel's secrets go: webhook = its URL's origin; ntfy = server origin + topic; email = SMTP host,
    port and encryption. Telegram (fixed api host) and Discord (the secret is the URL itself) cannot be redirected
    by changing another field. Used to decide whether a saved secret may be reused in a test with a changed form."""
    cfg = cfg or {}
    if channel == "webhook":
        # the PATH counts too: a webhook id often is the credential, and a bearer saved for one path must not
        # follow a changed path to another endpoint on the same host
        try:
            path = urllib.parse.urlsplit(str(cfg.get("url") or "").strip()).path.rstrip("/")
        except ValueError:
            path = "?"
        return (_origin(cfg.get("url")), path)
    if channel == "ntfy":
        return (_origin(cfg.get("server_url")), str(cfg.get("topic") or "").strip())
    if channel == "email":
        return (str(cfg.get("host") or "").strip().lower(), str(cfg.get("port") or ""), str(cfg.get("tls") or ""),
                "verify" if cfg.get("verify_cert", True) is not False else "no-verify")
    return ()


class Notifier:
    """Decide and deliver. `settings` is a SettingsStore. Thread-safe.

    notify(event)  - synchronous; returns a result dict (blocks on the network).
    submit(event)  - queue it for a background worker; use this from a poll loop.
    send_test(ch)  - one test message, bypassing every toggle."""

    RETRY_DELAYS = (10.0, 60.0, 300.0)       # seconds before attempt 2, 3, 4 of a failed delivery
    MAX_PENDING_RETRIES = 100                 # bounded: beyond this a failed alert is logged and dropped

    def __init__(self, settings, clock=time.time, channel_options=None, timeout=DEFAULT_TIMEOUT,
                 retry_delays=None, max_pending_retries=None):
        self.settings = settings
        self.clock = clock
        self.timeout = timeout
        self.channel_options = channel_options or {}     # {"telegram": {"api_base": "http://127.0.0.1:1234"}}
        self._last_sent = {}
        self._lock = threading.Lock()
        self._history = collections.deque(maxlen=50)
        self._q = queue.Queue(maxsize=200)
        self._worker = None
        self._pool = None
        self.retry_delays = tuple(self.RETRY_DELAYS if retry_delays is None else retry_delays)
        self.max_pending_retries = self.MAX_PENDING_RETRIES if max_pending_retries is None else max_pending_retries
        self._rcv = threading.Condition()          # guards _retries / _retry_thread / _closed
        self._retries = {}                         # dedupe key -> {"due","event","channels","attempt","ok_before"}
        self._retry_thread = None
        self._closed = False
        self._exit_hook = False

    # ---- plumbing ---------------------------------------------------------

    def _channel(self, name, overrides=None):
        cfg = self.settings.channel_config(name)
        saved_dest = destination(name, cfg)
        fields = {x["name"]: x for x in ss.SECTIONS[name]["fields"]}
        blank_secrets = {n for n, x in fields.items() if x["secret"]}     # secrets the form did NOT supply
        for k, v in (overrides or {}).items():
            f = fields.get(k)
            if f is None or v is None:
                continue
            if f["secret"] and (not str(v).strip() or str(v).strip() in ss.SECRET_PLACEHOLDERS):
                continue              # blank secret in a test form = use the saved one (see the check below)
            try:
                cfg[k] = ss.coerce(f, v)
            except ValueError as e:
                raise ChannelError(f"{f['label']} {e}") from None
            blank_secrets.discard(k)
        dropped = []
        if overrides and destination(name, cfg) != saved_dest:
            # The test form points somewhere else than what is saved. A saved secret must never ride along to a new
            # destination (that would let anyone who can press Test read it back out): the secret has to be typed.
            for n in sorted(blank_secrets):
                if cfg.get(n):
                    cfg[n] = ""
                    dropped.append(fields[n]["label"])
        ch = build_channel(name, cfg, self.timeout, **self.channel_options.get(name, {}))
        ch.dropped_secrets = dropped
        return ch

    def _hub_name(self):
        return self.settings.get("general.hub_name") or "Fabrica"

    def enabled_channels(self) -> list:
        out = []
        for name in ss.CHANNELS:
            try:
                cfg = self.settings.channel_config(name)
            except KeyError:
                continue
            if cfg.get("enabled") and not build_channel(name, cfg).missing():
                out.append(name)
        return out

    def recent(self, limit=20) -> list:
        """Newest-first delivery log (titles and per-channel results; no secrets, no bodies)."""
        with self._lock:
            return list(self._history)[::-1][:limit]

    # ---- decisions --------------------------------------------------------

    def should_send(self, event):
        """(send?, reason). Pure policy - no network, does not record the rate limit."""
        g = self.settings.get
        kind = getattr(event, "kind", "")
        if kind not in ss.EVENT_KINDS:
            return False, "unknown_event"
        if not g("notifications.enabled"):
            return False, "notifications_off"
        if not g(f"notifications.{kind}"):
            return False, "event_off"
        printer = (getattr(event, "printer", "") or "").lower()
        if printer and printer in {p.lower() for p in g("notifications.muted_printers")}:
            return False, "printer_muted"
        now = getattr(event, "ts", None) or self.clock()
        if g("notifications.quiet_enabled") and kind not in g("notifications.quiet_breakthrough"):
            if in_quiet_hours(now, g("notifications.quiet_start"), g("notifications.quiet_end"),
                              g("general.timezone")):
                return False, "quiet_hours"
        return True, ""

    @staticmethod
    def _dedupe_key(event):
        return ((getattr(event, "printer", "") or "").lower(), event.kind, getattr(event, "dedupe_key", "") or "")

    def _window_s(self) -> int:
        return int(self.settings.get("notifications.duplicate_window_min") or 0) * 60

    def _is_duplicate(self, event) -> bool:
        """True if an identical alert was DELIVERED inside the window. Only looks; a send that failed
        everywhere never counts (see _mark_sent), so its resend is never blocked."""
        window = self._window_s()
        if window <= 0:
            return False
        now = self.clock()
        with self._lock:
            last = self._last_sent.get(self._dedupe_key(event))
        return last is not None and now - last < window

    def _mark_sent(self, event):
        """Start the duplicate window - called only after at least one channel took the message."""
        window = self._window_s()
        if window <= 0:
            return
        now = self.clock()
        with self._lock:
            self._last_sent[self._dedupe_key(event)] = now
            if len(self._last_sent) > 500:
                for k in [k for k, t in self._last_sent.items() if now - t > max(window, 3600)]:
                    self._last_sent.pop(k, None)

    def _duplicate(self, event) -> bool:      # kept for callers that want the old check-and-record call
        if self._is_duplicate(event):
            return True
        self._mark_sent(event)
        return False

    # ---- delivery ---------------------------------------------------------

    def notify(self, event) -> dict:
        """Apply the rules and deliver. Result: {"sent": bool, "skipped": reason|None,
        "channels": {name: {"ok": bool, "error": str|None}}}. Never raises."""
        try:
            return self._notify(event)
        except Exception as e:
            return {"sent": False, "skipped": f"internal error ({type(e).__name__})", "channels": {}}

    def _notify(self, event):
        ok, reason = self.should_send(event)
        if not ok:
            return self._record(event, {"sent": False, "skipped": reason, "channels": {}})
        chans = self.enabled_channels()
        if not chans:
            return self._record(event, {"sent": False, "skipped": "no_channels", "channels": {}})
        if self._is_duplicate(event):
            return self._record(event, {"sent": False, "skipped": "duplicate", "channels": {}})
        self._cancel_retry(self._dedupe_key(event))        # a fresh identical alert replaces a waiting retry
        image = self._image_for(event)
        meta = self._meta(event)
        res = self._fan_out(chans, event.title, event.body, image, getattr(event, "level", None)
                            or ss.event_level(event.kind), meta)
        sent = any(r["ok"] for r in res.values())
        if sent:
            self._mark_sent(event)                          # recorded only now: a failure never eats the resend
        failed = [n for n, r in res.items() if not r["ok"]]
        retry_in = self._schedule_retry(event, failed, 1, sent) if failed else None
        self._log_failures(event, res, 1, retry_in)
        return self._record(event, {"sent": sent, "skipped": None, "channels": res, "attempt": 1,
                                    **({"retry_in_s": retry_in} if retry_in is not None else {})})

    def _meta(self, event) -> dict:
        meta = {"event": event.kind, "printer": getattr(event, "printer", ""), "hub_name": self._hub_name()}
        if getattr(event, "html", None):
            meta["html"] = event.html            # Telegram-flavoured twin of title+body; other channels ignore it
        return meta

    def _image_for(self, event):
        if not self.settings.get("notifications.include_snapshot"):
            return None
        getter = getattr(event, "get_image", None)
        if not getter:
            return None
        try:
            return getter()
        except Exception:
            return None

    def _log_failures(self, event, res, attempt, retry_in):
        for name, r in res.items():
            if not r["ok"]:
                nxt = f"; retrying in {retry_in:.0f} s" if retry_in is not None else "; giving up"
                print(security.scrub_text(f"[notify] could not deliver '{getattr(event, 'title', '')}' to {name} "
                                          f"(attempt {attempt}): {r['error']}{nxt}"), flush=True)

    # ---- retry of failed deliveries -----------------------------------------

    def _schedule_retry(self, event, channels, attempt, ok_before):
        """Queue another try of `channels` after the next backoff step. Returns the delay, or None when
        the retries are used up / the queue is full / the notifier is closed."""
        if attempt > len(self.retry_delays) or not channels:
            return None
        delay = float(self.retry_delays[attempt - 1])
        key = self._dedupe_key(event)
        with self._rcv:
            if self._closed:
                return None
            if key not in self._retries and len(self._retries) >= self.max_pending_retries:
                print(f"[notify] retry queue full ({self.max_pending_retries}): '{getattr(event, 'title', '')}' "
                      "will not be retried", flush=True)
                return None
            self._retries[key] = {"due": time.monotonic() + delay, "event": event, "channels": list(channels),
                                  "attempt": attempt + 1, "ok_before": ok_before}
            if self._retry_thread is None or not self._retry_thread.is_alive():
                self._retry_thread = threading.Thread(target=self._retry_loop, name="notifier-retry", daemon=True)
                self._retry_thread.start()
            self._rcv.notify_all()
        return delay

    def _cancel_retry(self, key):
        with self._rcv:
            self._retries.pop(key, None)

    def pending_retries(self) -> int:
        with self._rcv:
            return len(self._retries)

    def _retry_loop(self):
        while True:
            with self._rcv:
                ent = None
                while not self._closed:
                    if not self._retries:
                        self._rcv.wait(30.0)
                        if not self._retries:
                            self._retry_thread = None        # idle: let the thread go; the next retry starts one
                            return
                        continue
                    key, cand = min(self._retries.items(), key=lambda kv: kv[1]["due"])
                    wait = cand["due"] - time.monotonic()
                    if wait <= 0:
                        del self._retries[key]
                        ent = cand
                        break
                    self._rcv.wait(wait)
                if ent is None:
                    return
            try:
                self._do_retry(ent)
            except Exception as e:                           # the retry thread must outlive any bug
                print(f"[notify] retry failed: {type(e).__name__}: {e}", flush=True)

    def _do_retry(self, ent):
        event, attempt = ent["event"], ent["attempt"]
        ok, reason = self.should_send(event)                 # the owner may have muted it meanwhile
        enabled = self.enabled_channels()
        chans = [c for c in ent["channels"] if c in enabled]
        if not ok or not chans:
            self._record(event, {"sent": False, "skipped": reason or "no_channels", "channels": {},
                                 "attempt": attempt})
            return
        meta = self._meta(event)
        late = "(Sent late: an earlier delivery attempt failed.)"
        if meta.get("html"):
            meta["html"] += "\n" + _html.escape(late, quote=False)
        body = f"{event.body}\n{late}"
        res = self._fan_out(chans, event.title, body, self._image_for(event),
                            getattr(event, "level", None) or ss.event_level(event.kind), meta)
        sent = any(r["ok"] for r in res.values())
        if sent and not ent["ok_before"]:
            self._mark_sent(event)
        failed = [n for n, r in res.items() if not r["ok"]]
        retry_in = self._schedule_retry(event, failed, attempt, ent["ok_before"] or sent) if failed else None
        self._log_failures(event, res, attempt, retry_in)
        self._record(event, {"sent": sent, "skipped": None, "channels": res, "attempt": attempt,
                             **({"retry_in_s": retry_in} if retry_in is not None else {})})

    def _fan_out(self, names, title, body, image, level, meta, overrides=None) -> dict:
        with self._lock:
            if self._pool is None:
                self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="notify")
            pool = self._pool

        def one(name):
            try:
                ch = self._channel(name, overrides)
            except ChannelError as e:
                return name, {"ok": False, "error": str(e)}
            ok, err = ch.send(title, body, image, level, meta)
            return name, {"ok": ok, "error": err}

        return dict(pool.map(one, names))

    def _record(self, event, result):
        with self._lock:
            self._history.append({"time": getattr(event, "ts", None) or self.clock(),
                                  "event": getattr(event, "kind", ""), "printer": getattr(event, "printer", ""),
                                  "title": security.scrub_text(str(getattr(event, "title", ""))), **result})
        return result

    def send_test(self, channel: str, values: dict = None) -> dict:
        """Send one test message to `channel`, ignoring the master switch, event toggles, quiet
        hours and the channel's own enabled flag. `values` may hold unsaved form fields
        (a blank secret falls back to the saved one). Returns {"channel","ok","error"}."""
        if channel not in CHANNEL_CLASSES:
            return {"channel": channel, "ok": False,
                    "error": f"unknown channel '{channel}' (use one of: {', '.join(CHANNEL_CLASSES)})"}
        try:
            ch = self._channel(channel, values)
        except ChannelError as e:
            return {"channel": channel, "ok": False, "error": str(e)}
        stamp = ss.format_datetime(self.clock(), self.settings.get("general.timezone"),
                                   self.settings.get("general.datetime_format"))
        ok, err = ch.send(f"{self._hub_name()}: test message",
                          f"This is a test from your Fabrica hub, sent at {stamp}. "
                          "If you can read this, notifications to this channel work.",
                          None, "info", {"event": "test", "printer": "", "hub_name": self._hub_name()})
        with self._lock:
            self._history.append({"time": self.clock(), "event": "test", "printer": "", "title": "test message",
                                  "sent": ok, "skipped": None, "channels": {channel: {"ok": ok, "error": err}}})
        out = {"channel": channel, "ok": ok, "error": err}
        if getattr(ch, "dropped_secrets", None):
            out["note"] = ("The saved " + ", ".join(ch.dropped_secrets) + " was not used because the address "
                           "was changed - type it again in the form to test with it.")
        return out

    # ---- background queue -------------------------------------------------

    def submit(self, event) -> bool:
        """Queue for the background worker (non-blocking; False - and logged - if the queue is full)."""
        if self._worker is None or not self._worker.is_alive():
            with self._lock:
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._run, name="notifier", daemon=True)
                    self._worker.start()
                    if not self._exit_hook:                    # deliver what is queued when the Hub shuts down
                        self._exit_hook = True
                        atexit.register(_shutdown_ref, weakref.ref(self))
        try:
            self._q.put_nowait(event)
            return True
        except queue.Full:
            print(f"[notify] queue full ({self._q.maxsize}): dropped '{getattr(event, 'title', '')}'", flush=True)
            try:
                self._record(event, {"sent": False, "skipped": "queue_full", "channels": {}})
            except Exception:
                pass
            return False

    def _run(self):
        while True:
            ev = self._q.get()
            try:
                self.notify(ev)
            except BaseException as e:                         # notify() never raises; belt and braces
                print(f"[notify] worker error: {type(e).__name__}: {e}", flush=True)
            finally:
                self._q.task_done()

    def drain(self, timeout=10.0) -> bool:
        """Wait until the queue is empty (tests / graceful shutdown)."""
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def shutdown(self, timeout=5.0) -> bool:
        """Graceful stop: deliver what is queued (up to `timeout`), then drop pending retries.
        Safe to call more than once. True if nothing was left undelivered."""
        done = self.drain(timeout)
        with self._rcv:
            left = len(self._retries)
        if left:
            print(f"[notify] shutting down with {left} alert(s) still waiting for a retry", flush=True)
        self.close()
        return done and not left

    def close(self):
        with self._rcv:
            self._closed = True
            self._retries.clear()
            self._rcv.notify_all()
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)


def _shutdown_ref(ref):
    n = ref()
    if n is not None:
        try:
            n.shutdown(5.0)
        except Exception:
            pass
