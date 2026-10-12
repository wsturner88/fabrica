"""API-key authentication for Fabrica.

Threat: anything that can reach the Hub's port (malware, a guest laptop, a prompt-injected
agent) can drive the printers. This module puts a key check in front of BOTH faces of the Hub:
the MCP endpoint (/mcp) and the REST API (/api/*).

Three modes (setting `security.api_auth`, env BAMBU_MCP_API_AUTH is the default):

  on    every request needs a valid API key (or, for /api/*, the dashboard's session cookie);
        otherwise 401 {"error": "API key required"}.
  warn  requests without a valid key still work, but are counted and logged (GET /api/security
        shows who called what) so the owner sees what would break before switching to `on`.
  off   no checks at all.

A fresh install starts in `on`. An install that already has data (printers.json, settings.json,
...) is upgrading from a version without keys: it is put into `warn` ONCE (recorded in
api_keys.json) so live agents keep working until the owner flips it.

Keys: named, generated here (`ph_` + 256 random bits), shown exactly once, kept only as a salted
SHA-256 in <data>/api_keys.json (mode 0600, atomic writes). Two scopes:
  read     status, files, camera, settings (secrets are redacted), lists - never changes anything
  control  everything, including starting / cancelling prints, fleet edits, settings, key management
Every REST route (REST_SCOPES) and every MCP tool (TOOL_SCOPES) is mapped to a scope below;
anything not listed needs `control`.

Dashboard: the page itself (/dashboard, /) is public and hands a browser an HttpOnly,
SameSite=Strict session cookie (HMAC-signed with a secret kept in api_keys.json) that grants
`control` to same-origin /api/* requests, so the touch kiosk and phones need no typing. That means
that WITHOUT a dashboard password the dashboard is protected only by the network. With
a dashboard password and sign-in turned on, the cookie is issued only after a login (rate limited).

Dashboard sign-in (all opt-in; OFF unless the owner turns it on, so an install that never asked for
it behaves exactly as described above):
  * `login_required()` - Settings > Security "Require sign-in to open the dashboard" (or, for an install
    that already had a dashboard password, that password). Needs the owner password; an optional second
    "view-only" password signs in with `read` scope.
  * Sessions (non-remembered): server-side records, signed cookie `v2.<id>.<exp>.<sig>`, idle timeout +
    absolute maximum, listed and ended in the Security tab; "sign out everywhere" rotates the cookie secret.
  * Trusted devices ("remember this device"): long-lived rotating token in an HttpOnly cookie, stored
    hashed with a name, last seen and last address; revocable; a one-time 6-digit pairing code adds a
    device that has no keyboard (the wall screen).
  * Rate limits on sign-in and pairing, security headers (CSP, frame, nosniff, referrer) on every page.
  * Offline recovery: `python security.py disable-dashboard-login | reset-dashboard-password |
    list-devices | revoke-device ID` (state lives in <data>/dashboard_auth.json, re-read when it changes).

First run (2026-10): a FRESH install is asked once, by the dashboard's full-screen "Protect your printers" step, to
choose a dashboard password (POST /api/auth/first-run: password set, sign-in ON, that browser becomes a trusted device;
the dashboard password stands in for the admin password) or to skip (POST /api/auth/dismiss-offer, recorded as
`offer_dismissed`). While neither happened - `AuthManager.first_run_required()`: fresh install, no password, not skipped,
key check ON - AuthMiddleware refuses state-changing requests that rely on the dashboard cookie (403, first_run: true).
API keys, reads and MCP are never affected; an upgrade is never asked. `python security.py reset-first-run` asks again.

Admin password (2026-10, second security review N-1): the admin actions - API keys, the key-check mode, trusted
devices, pairing, sessions, the dashboard / view-only / admin passwords and every `security.*` setting - need an
ADMIN PASSWORD, separate from the (optional) dashboard password. Without one they are refused ("Set an admin
password in Settings > Security to manage keys and security"); the very first admin password can be set from a
same-origin dashboard browser. The dashboard itself still opens without a password if the owner wants that, but
then anyone who can reach it can operate the printers - they just cannot mint keys or turn the key check off.
An admin password is entered once (POST /api/auth/admin-unlock) and gives a 30-minute HttpOnly cookie.

Hardening (2026-10, in force in EVERY mode - off, warn and on):
  * Browser boundary: a state-changing request (anything but GET/HEAD/OPTIONS) is refused when the browser says
    it is cross-site (Origin that is not this Hub's own, or Sec-Fetch-Site other than same-origin/none), when
    its Host header is not a local name or IP (DNS rebinding; BAMBU_MCP_ALLOWED_HOSTS lists extra names) unless
    it carries a valid API key, and when it has a body that is not Content-Type: application/json (so a
    "simple" cross-site form/fetch can never reach a handler). In warn/off mode an unauthenticated read
    from a foreign Host is refused too. Agents and curl (no Origin) are not affected.
  * Admin routes (REST_SCOPES scope "admin": API keys, /api/security, trusted devices, pairing codes, sessions)
    need a DASHBOARD browser that has typed the ADMIN PASSWORD (or, with no admin password, a full-access sign-in
    with the dashboard password), never an API key and never the bare no-password cookie, in every mode. An agent's
    control key can therefore not mint keys, pair a device, set the dashboard password or switch the key check
    off - and neither can another machine that merely fetched /dashboard. security.* settings through
    PUT /api/settings need the same.
  * Dashboard sign-in (and a view-only password) only counts while the key check is ON: it cannot be switched on
    otherwise, the key check cannot be lowered while it is on, and an older install in that state is treated as ON
    for the REST API (a request without a cookie is refused whatever the stored mode says).
  * Sign-in attempts are counted before the password is checked (no parallel-guess race), back off exponentially per
    address (IPv6 by /64; X-Forwarded-For only behind BAMBU_MCP_TRUSTED_PROXIES) and never lock out a clean address
    because of a flood from elsewhere. The offline CLI clears them.
  * Changing the dashboard / admin / view-only password, or switching sign-in on or off, signs out the affected
    sessions and trusted devices (API keys are never touched).
  * An MCP session id belongs to the key that opened it.
  * MCP bodies: for a non-control key the body must be plain UTF-8 JSON (no BOM, no UTF-16/32, no duplicate
    keys); anything the scope check cannot read is refused (fail closed). Each tool also re-checks its
    scope from the request that carried it (defence in depth, see server._tool).

Standalone on purpose (stdlib only, no import of server.py): the ASGI middleware is plain ASGI.
"""

import atexit
import collections
import contextvars
import hashlib
import hmac
import http.cookies
import ipaddress
import json
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlsplit

KEYS_FILE = "api_keys.json"
SCHEMA_VERSION = 1
MODES = ("off", "warn", "on")
SCOPES = ("read", "control")
SCOPE_RANK = {"read": 1, "control": 2, "admin": 3}     # "admin" = REST routes only; a key is never above control
KEY_PREFIX = "ph_"
MAX_KEYS = 50
MAX_NAME = 40
COOKIE_NAME = "printhub_session"
COOKIE_SECONDS = 30 * 24 * 3600
LAST_USED_FLUSH_S = 60.0           # last_used is kept in memory and written at most this often
BODY_SNIFF_LIMIT = 256 * 1024      # largest MCP body the middleware will buffer to look inside
MAX_CALLERS = 200                  # distinct (caller, path) rows kept for the warn log
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PBKDF2_ROUNDS = 200_000

DASH_FILE = "dashboard_auth.json"            # dashboard login state: switch, password hashes, devices, sessions
DEVICE_COOKIE = "printhub_device"
CONFIG_DEFAULTS = {"idle_hours": 12, "max_days": 7, "device_days": 90}
CONFIG_LIMITS = {"idle_hours": (1, 720), "max_days": (1, 90), "device_days": (1, 365)}
ROTATE_AFTER_S = 24 * 3600         # a trusted-device token is replaced by a new one after this long
ROTATE_GRACE_S = 120               # ...and the previous one still works this long (parallel requests, a lost response)
PAIR_TTL_S = 300                   # a pairing code lives 5 minutes
PAIR_MAX_FAILS = 5                 # wrong guesses (from anybody) that burn the active code
MAX_DEVICES = 50
MAX_SESSIONS = 100
MIN_PASSWORD = 8
ADMIN_COOKIE = "printhub_admin"      # proof that the admin password was typed recently (HttpOnly, SameSite=Strict)
ADMIN_COOKIE_SECONDS = 30 * 60
LIMITER_RESET_FILE = "login_limiter.reset"   # the offline CLI touches this; the running server clears its limiter
ADMIN_UNSET_MSG = "Set an admin password in Settings \u2192 Security to manage keys and security."
ADMIN_LOCKED_MSG = ("Enter the admin password in Settings \u2192 Security (Unlock) to manage keys and security. "
                    "API keys can never do this.")
ROLE_WORDS = {"control": "Full access", "read": "View-only"}

# Paths that never need a key. Nothing else is exempt.
PUBLIC_PAGES = ("/", "/dashboard", "/api/ui-version", "/api/health",       # GET / HEAD
                # brand assets (icons, manifest): browsers fetch them without credentials, and they hold no data
                "/favicon.ico", "/favicon.svg", "/apple-touch-icon.png", "/manifest.webmanifest")
PUBLIC_PREFIXES = ("/static/brand/",)                                      # GET / HEAD: the files in app/static/brand/
PUBLIC_POSTS = ("/api/auth/login", "/api/auth/logout", "/api/auth/pair")   # POST
# While a fresh install is waiting for its first-run "Protect your printers" step, a dashboard browser (cookie, no key) may
# change nothing except these two: choose the password, or skip the step.
FIRST_RUN_POSTS = ("/api/auth/first-run", "/api/auth/dismiss-offer")
FIRST_RUN_MSG = ("This Hub is new and has no dashboard password yet. Open the dashboard in a browser and finish the "
                 "'Protect your printers' step (set a password, or skip it) before changing anything. API keys are "
                 "not affected.")


# ---------------------------------------------------------------- scope tables

# (method, path template, scope). {x} matches one path segment. HEAD is treated as GET.
# Explicit on purpose, `control` rows included, so a reader (and a test) can see that every
# route was decided. A route that is not listed needs `control`.
REST_SCOPES = [
    # --- read: look, never touch
    ("GET", "/api/fleet", "read"),
    ("GET", "/api/printers", "read"),
    ("GET", "/api/printer-types", "read"),
    ("GET", "/api/printer/{name}", "read"),
    ("GET", "/api/printer/{name}/controls", "read"),
    ("GET", "/api/printer/{name}/snapshot", "read"),
    ("GET", "/api/printer/{name}/stream", "read"),
    ("GET", "/api/printer/{name}/files", "read"),
    ("POST", "/api/printer/{name}/suggest", "read"),         # computes a mapping, changes nothing
    ("GET", "/api/thumb/{basename}", "read"),
    ("GET", "/api/backups", "read"),                          # the list, not the zip files
    ("GET", "/api/settings", "read"),                         # secrets are already redacted
    ("GET", "/api/settings/schema", "read"),
    ("GET", "/api/settings/export", "read"),
    ("GET", "/api/notifications/recent", "read"),
    ("GET", "/api/jobs", "read"),                             # job history and cost figures
    ("GET", "/api/jobs/{id}", "read"),
    ("GET", "/api/stats", "read"),
    ("GET", "/api/costs/file", "read"),
    ("GET", "/api/detection", "read"),                        # detector state and per-printer verdicts
    ("GET", "/api/discover/suggestions", "read"),             # the networks the Hub would offer to scan
    ("GET", "/api/discover/{id}", "read"),                    # the progress / result of a scan that was started with control
    ("GET", "/api/auth/me", "read"),                          # who am I (role, view-only?) - lets the page hide buttons
    ("POST", "/api/auth/admin-unlock", "control"),            # type the admin password (the handler refuses API keys)
    ("POST", "/api/auth/admin-lock", "control"),
    ("POST", "/api/auth/dismiss-offer", "control"),           # "skip" on the first-run step
    ("POST", "/api/auth/first-run", "control"),               # first-run "Protect your printers": choose the dashboard password
    ("POST", "/api/security/admin-password", "control"),      # first admin password: any same-origin dashboard browser; later ones: the handler checks
    # --- control: anything that acts, spends, sends or reconfigures
    ("POST", "/api/printer/{name}/start", "control"),
    ("POST", "/api/printer/{name}/action", "control"),        # cancel / pause / resume / light / connect
    ("POST", "/api/printer/{name}/control", "control"),
    ("POST", "/api/printer/{name}/tray", "control"),
    ("POST", "/api/printer/{name}/delete", "control"),
    ("POST", "/api/printer/{name}/backup", "control"),
    ("GET", "/api/backups/{printer}/{filename}", "control"),  # downloading a backup = all your files
    ("POST", "/api/printers/test", "control"),                # makes the Hub contact an address
    ("POST", "/api/printers", "control"),
    ("PUT", "/api/printers/{name}", "control"),
    ("DELETE", "/api/printers/{name}", "control"),
    ("PUT", "/api/settings", "control"),
    ("POST", "/api/settings/import", "control"),
    ("POST", "/api/settings/test/{channel}", "control"),      # sends a message to a configured channel
    ("GET", "/api/jobs.csv", "control"),                      # a data export: job names can carry order or customer info
    ("POST", "/api/discover", "control"),                     # starts a network scan
    ("DELETE", "/api/discover/{id}", "control"),
    ("POST", "/api/detection/test", "control"),               # makes the Hub contact an address
    ("POST", "/api/printer/{name}/detection/score", "control"),   # sends a camera picture to the detector
    # --- admin: who may get in and what the keys may do - a signed-in DASHBOARD browser only, never an API key
    ("GET", "/api/security", "admin"),
    ("PUT", "/api/security", "admin"),
    ("GET", "/api/keys", "admin"),
    ("POST", "/api/keys", "admin"),
    ("DELETE", "/api/keys/{id}", "admin"),
    ("POST", "/api/keys/{id}/revoke", "admin"),
    ("GET", "/api/devices", "admin"),                         # also shows the active pairing code
    ("PATCH", "/api/devices/{id}", "admin"),
    ("DELETE", "/api/devices/{id}", "admin"),
    ("POST", "/api/devices/revoke-others", "admin"),
    ("POST", "/api/pairing", "admin"),
    ("DELETE", "/api/pairing", "admin"),
    ("DELETE", "/api/sessions/{id}", "admin"),
    ("POST", "/api/auth/logout-everywhere", "admin"),
]

# Every MCP tool -> scope, enforced on each tools/call. An unknown tool needs `control`.
# Key CREATION is deliberately not a tool: an agent must not be able to mint keys for itself.
TOOL_SCOPES = {
    # read
    "list_printers": "read",
    "printer_status": "read",
    "ams_state": "read",
    "list_sd_files": "read",
    "suggest_mapping": "read",
    "printer_controls": "read",
    "get_camera_snapshot": "read",
    "backup_status": "read",
    "list_printer_types": "read",
    "get_settings": "read",
    "recent_jobs": "read",
    "job_stats": "read",
    "cost_per_part": "read",
    "detection_status": "read",
    # control
    "start_print": "control",
    "cancel_print": "control",
    "pause_print": "control",
    "resume_print": "control",
    "connect_printer": "control",
    "set_light": "control",
    "chamber_light": "control",
    "preheat": "control",
    "cooldown": "control",
    "unload_filament": "control",
    "load_filament": "control",
    "set_tray": "control",
    "delete_sd_file": "control",
    "backup_printer_files": "control",
    "test_printer_connection": "control",
    "add_printer": "control",
    "update_printer": "control",
    "remove_printer": "control",
    "update_settings": "control",
    "send_test_notification": "control",
    "list_api_keys": "control",
    "discover_printers": "control",        # scans networks
    "score_snapshot": "control",           # sends a camera picture to the detector
}


def _compile_rest():
    rows = []
    for method, tpl, scope in REST_SCOPES:
        rx = "^" + re.sub(r"\\\{[a-z]+\\\}", "[^/]+", re.escape(tpl)) + "/?$"
        rows.append((method, re.compile(rx), scope))
    return rows


_REST_RX = _compile_rest()


def rest_scope(method: str, path: str) -> str:
    """Scope a REST request needs. Unlisted routes -> control."""
    method = "GET" if method.upper() == "HEAD" else method.upper()
    for m, rx, scope in _REST_RX:
        if m == method and rx.match(path):
            return scope
    return "control"


def rest_rule_known(method: str, path: str) -> bool:
    method = "GET" if method.upper() == "HEAD" else method.upper()
    return any(m == method and rx.match(path) for m, rx, _s in _REST_RX)


def tool_scope(name) -> str:
    return TOOL_SCOPES.get(name, "control") if isinstance(name, str) else "control"


def ident_rank(ident) -> int:
    """Authority of an Identity for read / control decisions: tops out at control, for a key and for a dashboard
    cookie alike. ADMIN is not decided here any more (the no-password cookie used to count as admin - N-1): see
    AuthManager.admin_verdict, which needs the admin password."""
    if ident is None or ident.scope not in SCOPE_RANK:
        return 0
    return SCOPE_RANK[ident.scope]


# Who is asking, for code that runs under a request (a REST handler, an MCP tool): "control" | "read" | None
# (None = no credential was needed / presented, e.g. key check Off - the legacy "everything" view).
CALLER_SCOPE = contextvars.ContextVar("printhub_caller_scope", default=None)


def caller_read_only() -> bool:
    return CALLER_SCOPE.get() == "read"


# ---------------------------------------------------------------- redaction for read-only callers

HIDDEN = "(hidden - needs full access)"


def strip_userinfo(url):
    """http://user:pw@host:80/x -> http://host:80/x. Anything that is not a URL with userinfo is returned as is."""
    if not isinstance(url, str) or "://" not in url or "@" not in url:
        return url
    try:
        u = urlsplit(url)
        if "@" not in (u.netloc or ""):
            return url
        host = u.netloc.rsplit("@", 1)[1]
        return u._replace(netloc=host).geturl()
    except ValueError:
        return url


_USERINFO_RX = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s\"'<>]*@")


def scrub_text(text):
    """Any `scheme://user:password@host` inside free text (an error message, an alert body) -> `scheme://host`."""
    if not isinstance(text, str) or "@" not in text or "://" not in text:
        return text
    return _USERINFO_RX.sub(r"\1", text)


def scrub_urls(obj):
    """A copy of a JSON-like value with the user:password@ part removed from every URL string in it."""
    if isinstance(obj, str):
        return strip_userinfo(obj)
    if isinstance(obj, list):
        return [scrub_urls(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_urls(v) for k, v in obj.items()}
    return obj


# Plain (non-secret) settings that still act like a password: the webhook URL (a Home Assistant / n8n webhook id
# IS the credential), the ntfy topic (on ntfy.sh anyone who knows the topic reads it), the SMTP login name and
# the Telegram chat id. A read-only caller sees "(hidden...)" instead. (The Discord URL is a secret setting
# already: it only ever shows "set" / "not set".)
READ_HIDDEN_SETTINGS = ("webhook.url", "ntfy.topic", "email.username", "telegram.chat_id")


def _hide(v):
    return HIDDEN if v not in (None, "", [], False) else v


def redact_settings_doc(doc):
    """describe()-style document (sections -> fields -> value) made safe for a read-only caller."""
    if not isinstance(doc, dict):
        return doc
    for sec in doc.get("sections") or []:
        for f in sec.get("fields") or []:
            if f.get("key") in READ_HIDDEN_SETTINGS and "value" in f:
                f["value"] = _hide(f["value"])
            elif isinstance(f.get("value"), str):
                f["value"] = strip_userinfo(f["value"])
    return doc


def redact_settings_values(values):
    """{key: value} (the export document's `values`) made safe for a read-only caller."""
    if not isinstance(values, dict):
        return values
    return {k: (_hide(v) if k in READ_HIDDEN_SETTINGS else scrub_urls(v)) for k, v in values.items()}


# ---------------------------------------------------------------- small helpers

class AuthError(Exception):
    """A key/security request that cannot be honoured (status = HTTP status)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _atomic_write(path: str, text: str, mode: int):
    """temp file in the same directory + fsync + rename (same pattern as fleet_store)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as e:
        bad = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, bad)
        except OSError:
            bad = "(could not move it aside)"
        print(f"[security] {path} was unreadable ({e}); moved to {bad}, starting empty", flush=True)
        return default


def _hash_key(salt: str, token: str) -> str:
    return hashlib.sha256((salt + token).encode("utf-8", "replace")).hexdigest()


def _nfc(password: str) -> str:
    return unicodedata.normalize("NFC", password)


def hash_password(password: str) -> str:
    """Salted PBKDF2-SHA256 -> 'pbkdf2$rounds$salt$hash' (what is stored for a dashboard / view-only / admin
    password). The password is Unicode-normalised (NFC) first, so the same word typed on a phone and on a Mac
    is the same password."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", _nfc(password).encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt, want = stored.split("$")
        if scheme != "pbkdf2":
            return False
        tries = [_nfc(password)]
        if password != tries[0]:                      # a hash made before the normalisation
            tries.append(password)
        ok = False
        for t in tries:
            dk = hashlib.pbkdf2_hmac("sha256", t.encode("utf-8"), bytes.fromhex(salt), int(rounds))
            ok = hmac.compare_digest(dk.hex(), want) or ok
        return ok
    except Exception:
        return False


def _headers(scope) -> dict:
    out = {}
    for k, v in scope.get("headers") or []:
        try:
            out.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
        except Exception:
            pass
    return out


def extract_key(headers: dict):
    """The API key a request presents: Authorization: Bearer <key>, else X-Api-Key. None = no key
    presented at all. Never looks in the query string."""
    auth = headers.get("authorization")
    if auth is not None:
        parts = auth.strip().split(None, 1)
        if len(parts) >= 1 and parts[0].lower() == "bearer":
            return parts[1].strip() if len(parts) == 2 else ""
    xk = headers.get("x-api-key")
    if xk is not None:
        return xk.strip()
    return None


def detect_existing_install(data_dir: str, legacy_config: bool = False) -> bool:
    """True if this data directory already holds a Hub's data (so the Hub is being UPGRADED to a
    version with API keys, and live agents may be calling it). Call BEFORE the fleet store is
    created: importing a legacy config.yml writes printers.json."""
    if os.path.exists(os.path.join(data_dir, KEYS_FILE)):
        return False                       # the decision was already made and recorded
    for name in ("printers.json", "settings.json", "secrets.json", "metacache.json",
                 "tray-changes.log", "backups", "thumbs"):
        if os.path.exists(os.path.join(data_dir, name)):
            return True
    return bool(legacy_config)


# ---------------------------------------------------------------- key store

class KeyStore:
    """<data>/api_keys.json: install record, cookie-signing secret and the keys (hash only).
    Thread-safe. The file is re-read when another process (the CLI) changed it."""

    def __init__(self, data_dir: str, clock=time.time):
        self.path = os.path.join(data_dir, KEYS_FILE)
        self.clock = clock
        self._lock = threading.RLock()
        self._dirty = False
        self._last_flush = 0.0
        self._sig = None
        self._doc = {}
        self._reload()
        if not os.path.exists(self.path):
            self._save()                  # create it now, 0600, with its signing secret

    # ---- persistence ------------------------------------------------------
    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _reload(self):
        with self._lock:
            doc = _read_json(self.path, {})
            doc = doc if isinstance(doc, dict) else {}
            keys = [k for k in (doc.get("keys") or []) if isinstance(k, dict) and k.get("id") and k.get("hash")]
            old = {k["id"]: k for k in (self._doc.get("keys") or [])} if self._doc else {}
            for k in keys:                   # keep the fresher in-memory last_used
                o = old.get(k["id"])
                if o and (o.get("last_used") or "") > (k.get("last_used") or ""):
                    k["last_used"], k["last_path"] = o.get("last_used"), o.get("last_path")
            self._doc = {"version": SCHEMA_VERSION,
                         "meta": doc.get("meta") if isinstance(doc.get("meta"), dict) else {},
                         "session_secret": doc.get("session_secret") if isinstance(doc.get("session_secret"), str)
                         and len(doc.get("session_secret")) >= 32 else secrets.token_hex(32),
                         "keys": keys}
            self._sig = self._stat()

    def _sync(self):
        if self._stat() != self._sig and os.path.exists(self.path):
            self._reload()

    def _save(self):
        with self._lock:
            _atomic_write(self.path, json.dumps(self._doc, indent=2), 0o600)
            self._sig = self._stat()
            self._dirty = False
            self._last_flush = self.clock()

    def flush(self):
        """Write pending last_used updates now."""
        with self._lock:
            if self._dirty:
                try:
                    self._sync()
                    self._save()
                except OSError as e:
                    print(f"[security] could not save {self.path}: {e}", flush=True)

    # ---- meta / signing secret --------------------------------------------
    @property
    def meta(self) -> dict:
        with self._lock:
            return dict(self._doc["meta"])

    def set_meta(self, **kw):
        with self._lock:
            self._doc["meta"].update(kw)
            self._save()

    def session_secret(self) -> bytes:
        with self._lock:
            self._sync()
            return self._doc["session_secret"].encode()

    def rotate_session_secret(self):
        """Invalidate every dashboard session cookie."""
        with self._lock:
            self._sync()
            self._doc["session_secret"] = secrets.token_hex(32)
            self._save()

    # ---- keys -------------------------------------------------------------
    @staticmethod
    def public(rec: dict) -> dict:
        """What a list may show: never the key, never its hash or salt."""
        return {"id": rec["id"], "name": rec.get("name", ""), "scope": rec.get("scope", "read"),
                "prefix": rec.get("prefix", ""), "created": rec.get("created"),
                "last_used": rec.get("last_used"), "last_used_path": rec.get("last_path"),
                "revoked": bool(rec.get("revoked")), "revoked_at": rec.get("revoked_at"),
                "created_mode": rec.get("created_mode")}

    def list_public(self) -> list:
        with self._lock:
            self._sync()
            return [self.public(k) for k in self._doc["keys"]]

    def counts(self) -> dict:
        with self._lock:
            ks = self._doc["keys"]
            active = [k for k in ks if not k.get("revoked")]
            return {"active": len(active), "revoked": len(ks) - len(active),
                    "read": sum(1 for k in active if k.get("scope") == "read"),
                    "control": sum(1 for k in active if k.get("scope") == "control")}

    def create(self, name, scope, mode=None) -> tuple:
        """-> (public record, the key). The key exists in plain text only in this return value.
        `mode` = the key-check mode in force when it was made (kept so keys minted while the check was warn/off
        can be reviewed before switching to on)."""
        name = (name if isinstance(name, str) else "").strip()
        if not name or len(name) > MAX_NAME or re.search(r"[\x00-\x1f\x7f]", name):
            raise AuthError(f"A key needs a name of 1-{MAX_NAME} characters, e.g. 'Claude Desktop'.")
        if scope not in SCOPES:
            raise AuthError("scope must be 'read' or 'control'.")
        with self._lock:
            self._sync()
            if len(self._doc["keys"]) >= MAX_KEYS:
                raise AuthError(f"At most {MAX_KEYS} keys (revoked ones count until deleted).", 409)
            token = KEY_PREFIX + secrets.token_urlsafe(32)
            salt = secrets.token_hex(16)
            rec = {"id": "k_" + secrets.token_hex(4), "name": name, "scope": scope, "prefix": token[:6],
                   "salt": salt, "hash": _hash_key(salt, token), "created": _now(),
                   "last_used": None, "last_path": None, "revoked": False, "revoked_at": None,
                   "created_mode": mode}
            self._doc["keys"].append(rec)
            self._save()
            return self.public(rec), token

    def _find(self, key_id):
        for k in self._doc["keys"]:
            if k["id"] == key_id:
                return k
        raise AuthError(f"No such key '{key_id}'.", 404)

    def revoke(self, key_id) -> dict:
        with self._lock:
            self._sync()
            rec = self._find(key_id)
            if not rec.get("revoked"):
                rec["revoked"], rec["revoked_at"] = True, _now()
                self._save()
            return self.public(rec)

    def delete(self, key_id) -> dict:
        with self._lock:
            self._sync()
            rec = self._find(key_id)
            self._doc["keys"].remove(rec)
            self._save()
            return self.public(rec)

    def _match(self, token):
        found = None
        with self._lock:
            for r in self._doc["keys"]:        # no early exit: the time does not depend on which matched
                h = r.get("hash")
                if isinstance(h, str) and hmac.compare_digest(_hash_key(r.get("salt", ""), token), h):
                    found = r
        return dict(found) if found else None

    def verify(self, token):
        """The matching record (copy, may be revoked) or None."""
        if not isinstance(token, str) or not token.startswith(KEY_PREFIX) or len(token) > 200:
            return None
        rec = self._match(token)
        if rec is None and self._stat() != self._sig:
            with self._lock:
                self._reload()
            rec = self._match(token)
        return rec

    def touch(self, key_id, path):
        """Remember the key was just used. In memory every time; on disk at most once a minute."""
        with self._lock:
            for k in self._doc["keys"]:
                if k["id"] == key_id:
                    k["last_used"], k["last_path"] = _now(), (path or "")[:120]
                    self._dirty = True
                    break
            if self._dirty and self.clock() - self._last_flush >= LAST_USED_FLUSH_S:
                try:
                    self._sync()
                    self._save()
                except OSError as e:
                    print(f"[security] could not save {self.path}: {e}", flush=True)
                    self._last_flush = self.clock()


# ---------------------------------------------------------------- dashboard sign-in store

def device_label(ua: str) -> str:
    """'iPad · Safari', 'Windows · Edge', ... from a User-Agent string (a default name the owner can change)."""
    u = ua or ""
    if "iPad" in u:
        os_ = "iPad"
    elif "iPhone" in u or "iPod" in u:
        os_ = "iPhone"
    elif "Android" in u:
        os_ = "Android"
    elif "CrOS" in u:
        os_ = "ChromeOS"
    elif "Windows" in u:
        os_ = "Windows"
    elif "Macintosh" in u or "Mac OS X" in u:
        os_ = "Mac"
    elif "Linux" in u or "X11" in u:
        os_ = "Linux"
    else:
        os_ = "Device"
    if "Edg/" in u or "EdgA/" in u or "EdgiOS/" in u:
        br = "Edge"
    elif "OPR/" in u:
        br = "Opera"
    elif "Firefox/" in u or "FxiOS/" in u:
        br = "Firefox"
    elif "Chromium" in u:
        br = "Chromium"
    elif "CriOS/" in u or "Chrome/" in u:
        br = "Chrome"
    elif "Safari/" in u:
        br = "Safari"
    else:
        br = "browser"
    return f"{os_} · {br}"


def clean_name(name, fallback="") -> str:
    # control characters, C1 controls and the bidi / zero-width marks that can make a name read backwards
    name = re.sub("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", " ",
                  name if isinstance(name, str) else "").strip()
    return (name or fallback)[:MAX_NAME]


def _iso(ts) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class DashboardAuthStore:
    """<data>/dashboard_auth.json (0600): the sign-in switch, the owner / view-only password hashes, the
    session settings, trusted devices (token hash only) and sessions. Thread-safe. Like KeyStore the file
    is re-read when another process (the offline CLI) changed it, so a lockout can be undone while the
    Hub runs. Times are epoch seconds from `clock`."""

    def __init__(self, data_dir: str, clock=time.time):
        self.path = os.path.join(data_dir, DASH_FILE)
        self.clock = clock
        self._lock = threading.RLock()
        self._dirty = False
        self._last_flush = 0.0
        self._sig = None
        self._doc = {}
        self._reload()
        if not os.path.exists(self.path):
            self._save()

    # ---- persistence ------------------------------------------------------
    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    @staticmethod
    def _recs(doc, name):
        return [r for r in (doc.get(name) or []) if isinstance(r, dict) and r.get("id")]

    def _reload(self):
        with self._lock:
            doc = _read_json(self.path, {})
            doc = doc if isinstance(doc, dict) else {}
            cfg = doc.get("config") if isinstance(doc.get("config"), dict) else {}
            new = {"version": SCHEMA_VERSION,
                   "login_enabled": doc.get("login_enabled") if isinstance(doc.get("login_enabled"), bool) else None,
                   "owner_hash": doc.get("owner_hash") if isinstance(doc.get("owner_hash"), str) and doc.get("owner_hash") else None,
                   "viewer_hash": doc.get("viewer_hash") if isinstance(doc.get("viewer_hash"), str) and doc.get("viewer_hash") else None,
                   "admin_hash": doc.get("admin_hash") if isinstance(doc.get("admin_hash"), str) and doc.get("admin_hash") else None,
                   "admin_epoch": doc.get("admin_epoch") if isinstance(doc.get("admin_epoch"), int)
                   and not isinstance(doc.get("admin_epoch"), bool) else 0,
                   "offer_dismissed": bool(doc.get("offer_dismissed")),
                   "config": {k: cfg[k] for k in CONFIG_DEFAULTS if isinstance(cfg.get(k), int) and not isinstance(cfg.get(k), bool)},
                   "devices": self._recs(doc, "devices"), "sessions": self._recs(doc, "sessions")}
            for name in ("devices", "sessions"):          # keep the fresher in-memory last_seen
                old = {r["id"]: r for r in (self._doc.get(name) or [])}
                for r in new[name]:
                    o = old.get(r["id"])
                    if o and (o.get("last_seen") or 0) > (r.get("last_seen") or 0):
                        r["last_seen"], r["ip"] = o.get("last_seen"), o.get("ip", r.get("ip"))
            self._doc = new
            self._sig = self._stat()

    def _sync(self):
        if self._stat() != self._sig and os.path.exists(self.path):
            self._reload()

    def _save(self):
        with self._lock:
            _atomic_write(self.path, json.dumps(self._doc, indent=2), 0o600)
            self._sig = self._stat()
            self._dirty = False
            self._last_flush = self.clock()

    def flush(self):
        with self._lock:
            if self._dirty:
                try:
                    self._sync()
                    self._save()
                except OSError as e:
                    print(f"[security] could not save {self.path}: {e}", flush=True)

    def _touch_save(self):
        """After an in-memory last_seen change: write at most once a minute."""
        self._dirty = True
        if self.clock() - self._last_flush >= LAST_USED_FLUSH_S:
            try:
                self._sync()
                self._save()
            except OSError as e:
                print(f"[security] could not save {self.path}: {e}", flush=True)
                self._last_flush = self.clock()

    # ---- switch, passwords, settings --------------------------------------
    @property
    def login_enabled(self):
        with self._lock:
            self._sync()
            return self._doc["login_enabled"]

    @property
    def owner_hash(self):
        with self._lock:
            self._sync()
            return self._doc["owner_hash"]

    @property
    def viewer_hash(self):
        with self._lock:
            self._sync()
            return self._doc["viewer_hash"]

    @property
    def admin_hash(self):
        with self._lock:
            self._sync()
            return self._doc["admin_hash"]

    @property
    def admin_epoch(self) -> int:
        with self._lock:
            self._sync()
            return self._doc["admin_epoch"]

    def bump_admin_epoch(self) -> int:
        """Every admin unlock cookie issued so far stops working."""
        with self._lock:
            self._sync()
            self._doc["admin_epoch"] = int(self._doc["admin_epoch"]) + 1
            self._save()
            return self._doc["admin_epoch"]

    @property
    def offer_dismissed(self) -> bool:
        with self._lock:
            self._sync()
            return self._doc["offer_dismissed"]

    def set_fields(self, **kw):
        """login_enabled / owner_hash / viewer_hash / admin_hash / offer_dismissed, saved at once."""
        with self._lock:
            self._sync()
            for k, v in kw.items():
                if k not in ("login_enabled", "owner_hash", "viewer_hash", "admin_hash", "offer_dismissed"):
                    raise KeyError(k)
                self._doc[k] = v
            self._save()

    def config(self) -> dict:
        with self._lock:
            self._sync()
            out = dict(CONFIG_DEFAULTS)
            for k, v in self._doc["config"].items():
                lo, hi = CONFIG_LIMITS[k]
                out[k] = min(hi, max(lo, v))
            return out

    def set_config(self, **kw):
        with self._lock:
            self._sync()
            for k, v in kw.items():
                if k not in CONFIG_DEFAULTS:
                    raise KeyError(k)
                self._doc["config"][k] = int(v)
            self._save()

    # ---- devices -------------------------------------------------------------
    def add_device(self, rec: dict):
        with self._lock:
            self._sync()
            devs = self._doc["devices"]
            if len(devs) >= MAX_DEVICES:
                devs.sort(key=lambda r: r.get("last_seen") or 0)
                del devs[:len(devs) - MAX_DEVICES + 1]          # forget the least recently used
            devs.append(rec)
            self._save()

    def get_device(self, dev_id):
        with self._lock:
            self._sync()
            for r in self._doc["devices"]:
                if r["id"] == dev_id:
                    return r            # live record: callers copy before they leave the lock
        return None

    def devices(self) -> list:
        with self._lock:
            self._sync()
            return [dict(r) for r in self._doc["devices"]]

    def update_device(self, dev_id, **kw):
        with self._lock:
            self._sync()
            r = self.get_device(dev_id)
            if r is None:
                raise AuthError(f"No such device '{dev_id}'.", 404)
            r.update(kw)
            self._save()
            return dict(r)

    def touch_device(self, dev_id, ip=None):
        with self._lock:
            r = self.get_device(dev_id)
            if r is not None:
                r["last_seen"] = int(self.clock())
                if ip:
                    r["ip"] = ip
                self._touch_save()

    def remove_device(self, dev_id):
        with self._lock:
            self._sync()
            r = self.get_device(dev_id)
            if r is None:
                raise AuthError(f"No such device '{dev_id}'.", 404)
            self._doc["devices"].remove(r)
            self._save()
            return dict(r)

    def remove_devices_where(self, pred) -> int:
        """Forget every trusted device for which pred(record) is true."""
        with self._lock:
            self._sync()
            keep = [r for r in self._doc["devices"] if not pred(r)]
            n = len(self._doc["devices"]) - len(keep)
            if n:
                self._doc["devices"] = keep
                self._save()
            return n

    def remove_devices_except(self, keep_id=None) -> int:
        with self._lock:
            self._sync()
            keep = [r for r in self._doc["devices"] if r["id"] == keep_id]
            n = len(self._doc["devices"]) - len(keep)
            if n:
                self._doc["devices"] = keep
                self._save()
            return n

    # ---- sessions ------------------------------------------------------------
    def add_session(self, rec: dict):
        with self._lock:
            self._sync()
            ses = self._doc["sessions"]
            if len(ses) >= MAX_SESSIONS:
                ses.sort(key=lambda r: r.get("last_seen") or 0)
                del ses[:len(ses) - MAX_SESSIONS + 1]
            ses.append(rec)
            self._save()

    def get_session(self, sid):
        with self._lock:
            self._sync()
            for r in self._doc["sessions"]:
                if r["id"] == sid:
                    return r
        return None

    def sessions(self) -> list:
        with self._lock:
            self._sync()
            return [dict(r) for r in self._doc["sessions"]]

    def touch_session(self, sid, ip=None):
        with self._lock:
            r = self.get_session(sid)
            if r is not None:
                r["last_seen"] = int(self.clock())
                if ip:
                    r["ip"] = ip
                self._touch_save()

    def remove_session(self, sid) -> bool:
        with self._lock:
            self._sync()
            r = self.get_session(sid)
            if r is None:
                return False
            self._doc["sessions"].remove(r)
            self._save()
            return True

    def clear_sessions(self) -> int:
        with self._lock:
            self._sync()
            n = len(self._doc["sessions"])
            if n:
                self._doc["sessions"] = []
                self._save()
            return n

    def prune_sessions(self, keep) -> int:
        """Drop sessions for which keep(rec) is false."""
        with self._lock:
            self._sync()
            kept = [r for r in self._doc["sessions"] if keep(r)]
            n = len(self._doc["sessions"]) - len(kept)
            if n:
                self._doc["sessions"] = kept
                self._save()
            return n


# ---------------------------------------------------------------- login rate limit

def trusted_proxies(value) -> list:
    """BAMBU_MCP_TRUSTED_PROXIES ("127.0.0.1, 10.0.0.0/8") -> [ip_network]. Unparseable entries are ignored."""
    out = []
    for part in str(value or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            pass
    return out


def _ip_or_none(text):
    try:
        ip = ipaddress.ip_address(str(text).strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def limiter_key(remote, xff="", trusted=()) -> str:
    """The bucket a login attempt is counted in. The connecting address - or, ONLY when that address is a proxy
    listed in BAMBU_MCP_TRUSTED_PROXIES, the nearest X-Forwarded-For entry that is not itself a trusted proxy.
    Without that setting X-Forwarded-For is ignored entirely (anybody can write it). IPv6 addresses are grouped
    by /64 (one machine owns a whole /64), so rotating the low bits buys nothing."""
    peer = _ip_or_none(remote)
    who = peer
    if peer is not None and trusted and any(peer in n for n in trusted):
        chain = [x.strip() for x in str(xff or "").split(",") if x.strip()]
        for entry in reversed(chain):
            ip = _ip_or_none(entry)
            if ip is None:
                break                        # garbage in the chain: do not trust any of it
            who = ip
            if not any(ip in n for n in trusted):
                break
    if who is None:
        return str(remote or "")[:64]
    if isinstance(who, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{who}/64", strict=False))
    return str(who)


class LoginLimiter:
    """Failed password attempts, per address (`key`) - atomic and with a back-off instead of a lockout.

      * `acquire(client)` is checked BEFORE the password is verified and, when it admits the attempt, counts it as
        a failure at once (a reservation). So 40 simultaneous guesses from one address cannot all slip through
        while the first is still being hashed. `succeeded()` wipes the address's record; `failed()` confirms it.
      * The first `per_client` attempts are free. After that each further failure doubles the wait, up to `cap`
        seconds: 2, 4, 8, ... 60. The owner who mistyped a few times waits seconds, a script hammering the page
        waits a minute between guesses.
      * Nobody is locked out because of OTHER addresses: a clean address is never refused for what a flood from
        elsewhere did. During a flood (`overall` failures in the last minute) addresses that already failed
        are additionally spaced one second apart, and at most `max_inflight` passwords are hashed at once.
      * `reset_file`: when its modification time changes (the offline CLI touches it) every record is forgotten.
    """

    def __init__(self, per_client=3, overall=60, window=900.0, clock=time.time, base=2.0, cap=60.0,
                 max_inflight=4, reset_file=None):
        self.per_client, self.overall, self.window, self.clock = per_client, overall, window, clock
        self.base, self.cap, self.max_inflight = base, cap, max_inflight
        self._fails = collections.defaultdict(list)       # client -> timestamps (reservations included)
        self._pending = collections.defaultdict(int)      # client -> reservations not yet confirmed
        self._all = []
        self._inflight = 0
        self._lock = threading.Lock()
        self.reset_file = reset_file
        self._reset_seen = self._reset_mtime()

    def _reset_mtime(self):
        if not self.reset_file:
            return None
        try:
            return os.stat(self.reset_file).st_mtime_ns
        except OSError:
            return None

    def _check_reset(self):
        m = self._reset_mtime()
        if m != self._reset_seen:
            self._reset_seen = m
            self._fails.clear()
            self._pending.clear()
            self._all = []
            self._inflight = 0

    def reset(self):
        with self._lock:
            self._fails.clear()
            self._pending.clear()
            self._all = []
            self._inflight = 0

    def _prune(self, now):
        cut = now - self.window
        self._all = [t for t in self._all if t > cut]
        for k in list(self._fails):
            self._fails[k] = [t for t in self._fails[k] if t > cut]
            if not self._fails[k] and not self._pending.get(k):
                del self._fails[k]
                self._pending.pop(k, None)

    def _delay(self, n, free=None) -> float:
        free = self.per_client if free is None else free
        if n < free:
            return 0.0
        return min(self.cap, float(self.base) ** (n - free + 1))

    def _wait(self, client, now) -> float:
        mine = self._fails.get(client)
        if not mine:
            return 0.0                                # an address that has not failed is never delayed
        free = self.per_client
        if self.overall and sum(1 for t in self._all if t > now - 60.0) >= self.overall:
            free = min(free, 1)                       # a flood is going on: addresses that already failed get no more free tries
        return max(0.0, mine[-1] + self._delay(len(mine), free) - now)

    @staticmethod
    def _secs(w) -> int:
        return max(1, int(math.ceil(w))) if w > 0 else 0

    def retry_after(self, client) -> int:
        """0 = an attempt would be admitted now; else seconds to wait. Does not reserve anything."""
        with self._lock:
            self._check_reset()
            now = self.clock()
            self._prune(now)
            if self._inflight >= self.max_inflight:
                return 1
            return self._secs(self._wait(client, now))

    def acquire(self, client) -> int:
        """Ask to try a password. 0 = go ahead (the attempt is already counted; report it with succeeded() or
        failed()); else the seconds to wait."""
        with self._lock:
            self._check_reset()
            now = self.clock()
            self._prune(now)
            if self._inflight >= self.max_inflight:
                return 1
            w = self._secs(self._wait(client, now))
            if w:
                return w
            self._fails[client].append(now)
            self._all.append(now)
            self._pending[client] += 1
            self._inflight += 1
            return 0

    def _settle(self, client):
        if self._pending.get(client, 0) > 0:
            self._pending[client] -= 1
            self._inflight = max(0, self._inflight - 1)
            return True
        return False

    def failed(self, client):
        with self._lock:
            if not self._settle(client):                 # not reserved through acquire(): count it now
                now = self.clock()
                self._fails[client].append(now)
                self._all.append(now)

    def succeeded(self, client):
        with self._lock:
            self._settle(client)
            for t in self._fails.pop(client, []):          # (one entry each: two clients can share a timestamp)
                try:
                    self._all.remove(t)
                except ValueError:
                    pass
            self._pending.pop(client, None)

    def release(self, client):
        """An attempt that ended without a verdict (an internal error): give the reservation back."""
        with self._lock:
            if self._settle(client) and self._fails.get(client):
                t = self._fails[client].pop()
                try:
                    self._all.remove(t)
                except ValueError:
                    pass


def touch_limiter_reset(data_dir):
    """Tell a running Hub to forget its login-attempt records (used by the offline CLI after it resets a password)."""
    path = os.path.join(data_dir, LIMITER_RESET_FILE)
    try:
        _atomic_write(path, str(time.time_ns()), 0o600)
    except OSError as e:
        print(f"[security] could not write {path}: {e}", flush=True)


# ---------------------------------------------------------------- the manager

class Identity:
    """Who a request is. kind: key | cookie | none. problem (kind none): missing | invalid_key |
    revoked_key | cross_site."""
    __slots__ = ("kind", "scope", "key_id", "key_name", "problem", "sid", "device_id")

    def __init__(self, kind="none", scope=None, key_id=None, key_name=None, problem="missing",
                 sid=None, device_id=None):
        self.kind, self.scope, self.key_id, self.key_name = kind, scope, key_id, key_name
        self.problem = None if kind != "none" else problem
        self.sid, self.device_id = sid, device_id         # a dashboard session / a trusted device (kind "cookie")


class AuthManager:
    """Decides who may do what, and remembers what it saw. `settings` is the Hub's SettingsStore."""

    def __init__(self, data_dir, settings, existing_install=False, env=None, clock=time.time):
        self.settings = settings
        self.env = os.environ if env is None else env
        self.clock = clock
        self.keys = KeyStore(data_dir, clock=clock)
        self.dash = DashboardAuthStore(data_dir, clock=lambda: self.clock())
        reset = os.path.join(data_dir, LIMITER_RESET_FILE)
        self.limiter = LoginLimiter(clock=clock, reset_file=reset)                    # dashboard sign-in
        self.pair_limiter = LoginLimiter(per_client=5, overall=10, clock=clock, reset_file=reset)
        self.admin_limiter = LoginLimiter(clock=clock, reset_file=reset)              # admin unlock
        self._pair = None                  # the one active pairing code (memory only)
        self._lock = threading.Lock()
        self._first_run_lock = threading.Lock()
        self._callers = collections.OrderedDict()
        self.stats = collections.Counter()
        self.last_unauthenticated = None
        self.last_revocation = None        # what the last owner-password change signed out ({"sessions": n, "devices": n})
        self._first_run(existing_install)
        self._adopt_settings_password()
        settings.subscribe(self._settings_changed)
        self._publish_counts()
        atexit.register(self.keys.flush)
        atexit.register(self.dash.flush)

    # ---- first run: fresh -> on, upgrade -> warn (once) -----------------------
    def _first_run(self, existing_install):
        if self.keys.meta.get("install"):
            return
        install = "upgrade" if existing_install else "fresh"
        initial = self.mode()
        if (install == "upgrade" and not self.env.get("BAMBU_MCP_API_AUTH")
                and self.settings.get_all()["sources"].get("security.api_auth") == "default"):
            self.settings.update({"security.api_auth": "warn"})
            initial = "warn"
        self.keys.set_meta(install=install, decided_at=_now(), initial_mode=initial)
        if install == "upgrade":
            print("[security] existing install found: API-key authentication starts in WARN mode "
                  "(requests without a key still work, but are logged). Create keys, give them to your "
                  "agents, then switch to ON in Settings > Security.", flush=True)
        else:
            print("[security] new install: API-key authentication is ON. Create a key in the "
                  "dashboard (Security) or: python security.py create-key NAME --scope read|control",
                  flush=True)

    def mode(self) -> str:
        try:
            m = self.settings.get("security.api_auth")
        except Exception:
            m = "on"
        return m if m in MODES else "on"

    def _publish_counts(self):
        try:
            self.settings.set_runtime_info(active_keys=self.keys.counts()["active"])
        except Exception:
            pass

    def _adopt_settings_password(self):
        """A dashboard password saved by a version before the sign-in switch lives in the settings
        secrets; the sign-in store owns it from now on (the offline CLI can change it there)."""
        try:
            if not self.dash.owner_hash and self.settings.is_secret_set("security.dashboard_password"):
                self.dash.set_fields(owner_hash=self.settings.get_secret("security.dashboard_password"))
        except Exception as e:
            print(f"[security] could not adopt the saved dashboard password: {e}", flush=True)

    def _settings_changed(self, changed):
        if "security.dashboard_password" in changed:
            h = None
            try:
                h = self.settings.get_secret("security.dashboard_password") if \
                    self.settings.is_secret_set("security.dashboard_password") else None
                self.dash.set_fields(owner_hash=h or None)
                if h:
                    self.dash.set_fields(offer_dismissed=True)      # decided: the first-run step never comes back
                if not h:                           # no owner password = no sign-in, no view-only password
                    self.dash.set_fields(login_enabled=None, viewer_hash=None)
            except Exception as e:
                print(f"[security] could not store the dashboard password: {e}", flush=True)
            # a new (or removed) owner password ends every full-access session and trusted device (N-5);
            # view-only ones are untouched unless the password is gone altogether
            if h:
                self.last_revocation = self.revoke_role("control")
            else:
                sessions = self.dash.clear_sessions()
                self.last_revocation = {"sessions": sessions, "devices": self.sign_out_everywhere()}

    # ---- keys (the REST/MCP layers call these) --------------------------------
    def create_key(self, name, scope):
        res = self.keys.create(name, scope, mode=self.mode())
        self._publish_counts()
        return res

    def revoke_key(self, key_id):
        res = self.keys.revoke(key_id)
        self._publish_counts()
        return res

    def delete_key(self, key_id):
        res = self.keys.delete(key_id)
        self._publish_counts()
        return res

    def rotate_sessions(self):
        """Every session cookie stops working. Trusted devices and API keys are not touched."""
        self.keys.rotate_session_secret()
        self.dash.clear_sessions()

    def revoke_role(self, role) -> dict:
        """End every dashboard session and forget every trusted device of one role ("control" = full access,
        "read" = view-only). API keys are never touched. A full-access change also ends every admin unlock."""
        sessions = self.dash.prune_sessions(lambda r: r.get("role") != role)
        devices = self.dash.remove_devices_where(lambda r: r.get("role") == role)
        if role == "control":
            self.dash.bump_admin_epoch()
        return {"sessions": sessions, "devices": devices}

    # ---- who is calling ---------------------------------------------------------
    def is_exempt(self, method: str, path: str) -> bool:
        method = method.upper()
        if method in ("GET", "HEAD") and (path in PUBLIC_PAGES or path.startswith(PUBLIC_PREFIXES)):
            return True
        return method == "POST" and path in PUBLIC_POSTS

    def identify(self, headers: dict, method: str, allow_cookie=True) -> Identity:
        token = extract_key(headers)
        if token is not None:
            rec = self.keys.verify(token)
            if rec is None:
                return Identity(problem="invalid_key")
            if rec.get("revoked"):
                return Identity(problem="revoked_key", key_id=rec["id"], key_name=rec.get("name"))
            return Identity("key", rec.get("scope", "read"), rec["id"], rec.get("name"))
        if allow_cookie:
            ident = self.cookie_identity(headers, method)
            if ident is not None:
                return ident
        return Identity(problem="missing")

    def cookie_identity(self, headers: dict, method: str = "GET"):
        """Who the dashboard cookies say this is: an Identity (kind cookie, or none/cross_site when a
        write did not come from the dashboard's own origin), else None.
        Sign-in off: the old stateless cookie (control). Sign-in on: a live session, else a trusted device."""
        if not self.host_ok(headers):
            return None
        raw = cookie_value(headers)
        if not self.login_required():
            if raw and self.valid_cookie(raw):
                if method.upper() not in SAFE_METHODS and not same_origin(headers):
                    return Identity(problem="cross_site")
                return Identity("cookie", "control")
            return None
        sess = self._session_from_cookie(raw) if raw else None
        dev = None
        if sess is None:
            rawd = cookie_value(headers, DEVICE_COOKIE)
            dev = self.device_from_cookie(rawd)[0] if rawd else None
        if sess is None and dev is None:
            return None
        if method.upper() not in SAFE_METHODS and not same_origin(headers):
            return Identity(problem="cross_site")
        if sess is not None:
            return Identity("cookie", sess.get("role", "read"), sid=sess["id"])
        return Identity("cookie", dev.get("role", "read"), device_id=dev["id"])

    def touch(self, ident: Identity, path: str, remote=None):
        if ident.kind == "key":
            self.stats["authenticated_key"] += 1
            self.keys.touch(ident.key_id, path)
        elif ident.kind == "cookie":
            self.stats["authenticated_cookie"] += 1
            if ident.sid:
                self.dash.touch_session(ident.sid, remote or None)
            elif ident.device_id:
                self.dash.touch_device(ident.device_id, remote or None)

    # ---- dashboard cookie -----------------------------------------------------------
    def make_cookie(self) -> str:
        exp = int(self.clock()) + COOKIE_SECONDS
        payload = f"v1.{exp}"
        sig = hmac.new(self.keys.session_secret(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def valid_cookie(self, raw: str) -> bool:
        try:
            ver, exp, sig = raw.split(".")
            if ver != "v1" or int(exp) < self.clock():
                return False
            want = hmac.new(self.keys.session_secret(), f"{ver}.{exp}".encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, want)
        except Exception:
            return False

    def cookie_kwargs(self, secure: bool) -> dict:
        return {"key": COOKIE_NAME, "value": self.make_cookie(), "max_age": COOKIE_SECONDS, "path": "/",
                "httponly": True, "samesite": "strict", "secure": bool(secure)}

    # ---- sign-in sessions (only when login_required) ---------------------------------
    def _sign(self, payload: str) -> str:
        return hmac.new(self.keys.session_secret(), payload.encode(), hashlib.sha256).hexdigest()

    def start_session(self, role, ip="", ua="") -> tuple:
        """-> (cookie value, max_age seconds) for a NON-remembered sign-in. The record is server-side so it
        can time out, be listed and be ended."""
        cfg = self.dash.config()
        max_s = cfg["max_days"] * 86400
        now = int(self.clock())
        self.dash.prune_sessions(lambda r: self._session_alive(r, now, cfg))
        sid = "s_" + secrets.token_hex(6)
        self.dash.add_session({"id": sid, "role": role if role in SCOPES else "read", "created": now,
                               "last_seen": now, "ip": (ip or "")[:64], "label": device_label(ua)})
        payload = f"v2.{sid}.{now + max_s}"
        return f"{payload}.{self._sign(payload)}", max_s

    def _session_alive(self, rec, now, cfg) -> bool:
        return (now - (rec.get("last_seen") or 0) <= cfg["idle_hours"] * 3600
                and now - (rec.get("created") or 0) <= cfg["max_days"] * 86400)

    def _session_from_cookie(self, raw):
        """The live session record a v2 cookie names (copy), else None. Idle / absolute limits apply."""
        try:
            ver, sid, exp, sig = raw.split(".")
            if ver != "v2" or int(exp) < self.clock():
                return None
            if not hmac.compare_digest(sig, self._sign(f"{ver}.{sid}.{exp}")):
                return None
        except Exception:
            return None
        rec = self.dash.get_session(sid)
        if rec is None:
            return None
        now = self.clock()
        if not self._session_alive(rec, now, self.dash.config()):
            self.dash.remove_session(sid)
            return None
        return dict(rec)

    # ---- trusted devices ------------------------------------------------------------
    def _new_device_token(self, rec: dict) -> str:
        token = secrets.token_urlsafe(32)
        rec["salt"] = secrets.token_hex(16)
        rec["hash"] = _hash_key(rec["salt"], token)
        return f"{rec['id']}.{token}"

    def add_device(self, role, name, ip="", ua="") -> tuple:
        """-> (public record, cookie value, max_age). The token exists in plain text only in the cookie."""
        now = int(self.clock())
        rec = {"id": "d_" + secrets.token_hex(4), "name": clean_name(name, device_label(ua)),
               "role": role if role in SCOPES else "read", "created": now, "last_seen": now,
               "rotated": now, "ip": (ip or "")[:64], "label": device_label(ua)}
        value = self._new_device_token(rec)
        self.dash.add_device(rec)
        return self.device_public(rec), value, self.dash.config()["device_days"] * 86400

    def device_from_cookie(self, raw):
        """(device record copy, matched_current_token) or (None, False). A token that was replaced by a
        rotation still works for ROTATE_GRACE_S."""
        if not isinstance(raw, str) or "." not in raw or len(raw) > 200:
            return None, False
        dev_id, token = raw.split(".", 1)
        rec = self.dash.get_device(dev_id)
        found, current = None, False
        if rec is not None:
            h = _hash_key(rec.get("salt", ""), token)
            if hmac.compare_digest(h, rec.get("hash") or ""):
                found, current = rec, True
            elif rec.get("prev_hash") and (rec.get("prev_until") or 0) >= self.clock() \
                    and hmac.compare_digest(_hash_key(rec.get("prev_salt", ""), token), rec["prev_hash"]):
                found = rec
        else:
            hmac.compare_digest(_hash_key("x", token), "0" * 64)        # same work whether or not the id exists
        if found is None:
            return None, False
        life = self.dash.config()["device_days"] * 86400
        if self.clock() - (found.get("last_seen") or 0) > life:
            try:
                self.dash.remove_device(found["id"])
            except AuthError:
                pass
            return None, False
        return dict(found), current

    def maybe_rotate_device(self, headers: dict, secure: bool):
        """If the presented trusted-device token is due for replacement, replace it and return the
        Set-Cookie header value for the new one (else None)."""
        raw = cookie_value(headers, DEVICE_COOKIE)
        dev, current = self.device_from_cookie(raw) if raw else (None, False)
        if dev is None or not current or self.clock() - (dev.get("rotated") or 0) < ROTATE_AFTER_S:
            return None
        with self.dash._lock:
            rec = self.dash.get_device(dev["id"])
            if rec is None:
                return None
            rec["prev_salt"], rec["prev_hash"] = rec.get("salt"), rec.get("hash")
            rec["prev_until"] = int(self.clock()) + ROTATE_GRACE_S
            value = self._new_device_token(rec)
            rec["rotated"] = int(self.clock())
            self.dash._save()
        return cookie_header(DEVICE_COOKIE, value, self.dash.config()["device_days"] * 86400, secure)

    def device_public(self, rec: dict) -> dict:
        life = self.dash.config()["device_days"] * 86400
        return {"id": rec["id"], "name": rec.get("name", ""), "role": rec.get("role", "read"),
                "role_words": ROLE_WORDS.get(rec.get("role"), "View-only"), "browser": rec.get("label", ""),
                "first_seen": _iso(rec.get("created")), "last_seen": _iso(rec.get("last_seen")),
                "last_ip": rec.get("ip", ""), "expires": _iso((rec.get("last_seen") or 0) + life)}

    def session_public(self, rec: dict) -> dict:
        cfg = self.dash.config()
        end = min((rec.get("created") or 0) + cfg["max_days"] * 86400,
                  (rec.get("last_seen") or 0) + cfg["idle_hours"] * 3600)
        return {"id": rec["id"], "role": rec.get("role", "read"), "role_words": ROLE_WORDS.get(rec.get("role"), "View-only"),
                "browser": rec.get("label", ""), "started": _iso(rec.get("created")),
                "last_seen": _iso(rec.get("last_seen")), "last_ip": rec.get("ip", ""), "ends": _iso(end)}

    def list_devices(self) -> list:
        return [self.device_public(r) for r in sorted(self.dash.devices(), key=lambda r: -(r.get("last_seen") or 0))]

    def list_sessions(self) -> list:
        cfg, now = self.dash.config(), self.clock()
        live = [r for r in self.dash.sessions() if self._session_alive(r, now, cfg)]
        return [self.session_public(r) for r in sorted(live, key=lambda r: -(r.get("last_seen") or 0))]

    def rename_device(self, dev_id, name):
        name = clean_name(name)
        if not name:
            raise AuthError(f"A device needs a name of 1-{MAX_NAME} characters, e.g. 'Wall screen'.")
        return self.device_public(self.dash.update_device(dev_id, name=name))

    def revoke_device(self, dev_id):
        return self.device_public(self.dash.remove_device(dev_id))

    def revoke_other_devices(self, keep_id=None) -> int:
        return self.dash.remove_devices_except(keep_id)

    def sign_out_everywhere(self):
        """Every session and every trusted device is gone (and every admin unlock); API keys keep working."""
        self.keys.rotate_session_secret()
        self.dash.clear_sessions()
        self.dash.bump_admin_epoch()
        return self.dash.remove_devices_except(None)

    def sign_out(self, headers: dict):
        """This browser: end its session and forget its trusted device."""
        raw = cookie_value(headers)
        sess = self._session_from_cookie(raw) if raw else None
        if sess:
            self.dash.remove_session(sess["id"])
        rawd = cookie_value(headers, DEVICE_COOKIE)
        dev = self.device_from_cookie(rawd)[0] if rawd else None
        if dev:
            try:
                self.dash.remove_device(dev["id"])
            except AuthError:
                pass

    # ---- one-time pairing code (for a screen without a keyboard) ------------------------
    def new_pairing(self, role="control", name="") -> dict:
        if not self.login_required():
            raise AuthError("Turn on 'Require sign-in' first - there is nothing to pair with otherwise.")
        if role not in SCOPES:
            raise AuthError("role must be 'control' or 'read'.")
        with self._lock:
            self._pair = {"code": f"{secrets.randbelow(10 ** 6):06d}", "role": role, "name": clean_name(name),
                          "expires": self.clock() + PAIR_TTL_S, "fails": 0}
        return self.pairing_status()

    def pairing_status(self):
        with self._lock:
            p = self._pair
            if p is None or p["expires"] <= self.clock():
                self._pair = None
                return None
            return {"code": p["code"], "role": p["role"], "role_words": ROLE_WORDS[p["role"]], "name": p["name"],
                    "expires_in": int(round(p["expires"] - self.clock())), "ttl": PAIR_TTL_S}

    def cancel_pairing(self):
        with self._lock:
            self._pair = None

    def redeem_pairing(self, code, ip="", ua="") -> tuple:
        """-> (device public record, cookie value, max_age) or AuthError. A wrong guess counts against the
        active code: after PAIR_MAX_FAILS the code is burned, so six digits cannot be brute-forced."""
        if not self.login_required():
            raise AuthError("No sign-in is needed - there is nothing to pair with.", 400)
        code = code.strip() if isinstance(code, str) else ""
        with self._lock:
            p = self._pair
            if p is not None and p["expires"] <= self.clock():
                p = self._pair = None
            ok = p is not None and re.fullmatch(r"\d{6}", code) is not None and hmac.compare_digest(code, p["code"])
            if not ok:
                if p is not None:
                    p["fails"] += 1
                    if p["fails"] >= PAIR_MAX_FAILS:
                        self._pair = None
                raise AuthError("That code is not right, or it has expired. Ask for a new one on the Hub.", 401)
            self._pair = None                      # single use
            role, name = p["role"], p["name"]
        return self.add_device(role, name, ip, ua)

    def allowed_hosts(self) -> set:
        """BAMBU_MCP_ALLOWED_HOSTS as bare lower-case names: an entry written with a port ("hub.example.com:8271")
        counts for the name."""
        out = set()
        for h in (self.env.get("BAMBU_MCP_ALLOWED_HOSTS") or "").split(","):
            parsed = parse_host(h)
            if parsed:
                out.add(parsed)
        return out

    def trusted_proxy_nets(self) -> list:
        return trusted_proxies(self.env.get("BAMBU_MCP_TRUSTED_PROXIES"))

    def limiter_client(self, headers: dict, remote: str) -> str:
        """The bucket a login / pairing / unlock attempt is counted in (see limiter_key)."""
        return limiter_key(remote, (headers or {}).get("x-forwarded-for", ""), self.trusted_proxy_nets())

    def host_ok(self, headers: dict) -> bool:
        """Does the Host header look like a private/own name? Stops DNS-rebinding: a web page on
        evil.example that resolves to this Hub must not be handed (or use) a session cookie.
        Allowed: IP addresses, localhost, single-label and .local/.lan/.internal/.ts.net-style names,
        plus anything in BAMBU_MCP_ALLOWED_HOSTS. A header that is not a plain `name[:port]` (userinfo, a
        non-numeric port, odd characters) is refused."""
        raw = headers.get("host")
        if raw is None or not raw.strip():
            return True
        host = parse_host(raw)
        if host is None:
            return False
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        if "." not in host or host.endswith(PRIVATE_SUFFIXES) or host in self.allowed_hosts():
            return True
        return False

    # ---- the browser boundary (every mode) ---------------------------------------------
    def valid_key(self, headers: dict) -> bool:
        """Does the request carry an API key that exists and is not revoked? (A web page cannot: the browser
        never attaches an Authorization header cross-site, and the Hub sends no CORS headers.)"""
        token = extract_key(headers)
        if token is None:
            return False
        rec = self.keys.verify(token)
        return bool(rec) and not rec.get("revoked")

    def forwarded_host_trusted(self, remote) -> bool:
        """May X-Forwarded-Host stand in for Host in the same-origin check? Always, unless BAMBU_MCP_TRUSTED_PROXIES
        lists proxies - then only a request that really came from one of them."""
        nets = self.trusted_proxy_nets()
        if not nets:
            return True
        ip = _ip_or_none(remote)
        return ip is not None and any(ip in n for n in nets)

    def browser_guard(self, headers: dict, method: str, path: str, mode: str = "on", remote=None):
        """Checks that apply in off, warn and on alike. -> None (carry on) or (status, message) to refuse.
        State-changing requests only (reads in warn/off from a foreign Host are covered below):
          * cross-site (Origin not ours / Sec-Fetch-Site not same-origin|none)  -> 403
          * Host is not a local name or IP, and no valid API key              -> 403   (DNS rebinding)
          * a request body that is not application/json                        -> 415   (blocks "simple" forms)
        Agents and curl send no Origin and keep working."""
        method = method.upper()
        if method not in SAFE_METHODS:
            if not same_origin(headers, trust_forwarded=self.forwarded_host_trusted(remote)):
                return 403, "cross-site request refused"
            if not self.host_ok(headers) and not self.valid_key(headers):
                return 403, ("request refused: the Host name is not one of this Hub's own names. If you reach the Hub "
                             "by a custom name, add it to BAMBU_MCP_ALLOWED_HOSTS.")
            if method in ("POST", "PUT", "PATCH", "DELETE") and has_body(headers) \
                    and not is_json_type(headers.get("content-type")):
                return 415, "the request body must be JSON (Content-Type: application/json)"
        elif mode != "on" and not self.is_exempt(method, path) and not self.host_ok(headers) \
                and not self.valid_key(headers):
            return 403, ("request refused: the Host name is not one of this Hub's own names. If you reach the Hub "
                         "by a custom name, add it to BAMBU_MCP_ALLOWED_HOSTS.")
        return None

    # ---- dashboard password ----------------------------------------------------------
    def password_set(self) -> bool:
        return bool(self.dash.owner_hash)

    def viewer_password_set(self) -> bool:
        return bool(self.dash.viewer_hash)

    def check_password(self, password) -> bool:
        stored = self.dash.owner_hash
        return bool(stored) and isinstance(password, str) and verify_password(password, stored)

    def check_login(self, password):
        """'control' (owner password), 'read' (view-only password) or None. Both are always compared. A password
        typed with stray spaces round the ends also matches (the settings form always stripped them when saving)."""
        if not isinstance(password, str) or not password or len(password) > 500:
            return None
        o, v = self.dash.owner_hash, self.dash.viewer_hash
        tries = [password] + ([password.strip()] if password.strip() and password.strip() != password else [])
        owner_ok = viewer_ok = False
        for t in tries:
            owner_ok = owner_ok or (bool(o) and verify_password(t, o))
            viewer_ok = viewer_ok or (bool(v) and verify_password(t, v))
        return "control" if owner_ok else "read" if viewer_ok else None

    # ---- admin password -----------------------------------------------------------------
    def admin_password_set(self) -> bool:
        return bool(self.dash.admin_hash)

    def check_admin_password(self, password) -> bool:
        """The admin password; while none is set, the dashboard (owner) password stands in for it."""
        stored = self.dash.admin_hash or self.dash.owner_hash
        if not stored or not isinstance(password, str) or not password or len(password) > 500:
            return False
        tries = [password] + ([password.strip()] if password.strip() and password.strip() != password else [])
        return any(verify_password(t, stored) for t in tries)

    def check_viewer_password(self, password) -> bool:
        v = self.dash.viewer_hash
        return bool(v) and isinstance(password, str) and bool(password) and verify_password(password, v)

    def _admin_sig(self, payload: str) -> str:
        return hmac.new(self.keys.session_secret(), b"admin|" + payload.encode(), hashlib.sha256).hexdigest()

    def make_admin_cookie(self) -> str:
        """The proof that the admin password was just typed: a 30-minute signed token tied to the current admin
        epoch (changing the admin / dashboard password, or signing everyone out, invalidates every one)."""
        payload = f"a1.{int(self.clock()) + ADMIN_COOKIE_SECONDS}.{self.dash.admin_epoch}"
        return f"{payload}.{self._admin_sig(payload)}"

    def admin_cookie_header(self, secure: bool) -> str:
        return cookie_header(ADMIN_COOKIE, self.make_admin_cookie(), ADMIN_COOKIE_SECONDS, secure)

    def admin_unlocked(self, headers: dict) -> bool:
        raw = cookie_value(headers, ADMIN_COOKIE)
        if not raw or len(raw) > 200:
            return False
        try:
            ver, exp, epoch, sig = raw.split(".")
            if ver != "a1" or int(exp) < self.clock() or int(epoch) != self.dash.admin_epoch:
                return False
            if not (self.dash.admin_hash or self.dash.owner_hash):
                return False
            return hmac.compare_digest(sig, self._admin_sig(f"{ver}.{exp}.{epoch}"))
        except Exception:
            return False

    def admin_state(self, headers: dict, ident=None) -> str:
        """'ok' | 'locked' | 'unset' - may THIS browser do admin things (keys, key check, devices, passwords)?
          * an admin password is set: only after it was typed (the unlock cookie);
          * else a dashboard password is set: a full-access sign-in with it (session / trusted device) counts, and
            the unlock cookie (typed dashboard password) works when sign-in is off;
          * else nothing is set: refused ('unset') - until the first admin password is chosen."""
        has_admin, has_owner = bool(self.dash.admin_hash), bool(self.dash.owner_hash)
        if not (has_admin or has_owner):
            return "unset"
        if self.admin_unlocked(headers):
            return "ok"
        if (not has_admin and self.login_required() and ident is not None
                and ident.kind == "cookie" and ident.scope == "control"):
            return "ok"
        return "locked"

    def admin_verdict(self, headers: dict, ident) -> str:
        """For an admin route: 'ok' | 'key' | 'view_only' | 'cross_site' | 'anonymous' | 'unset' | 'locked'."""
        if ident is not None:
            if ident.kind == "key":
                return "key"
            if ident.kind == "none" and ident.problem == "cross_site":
                return "cross_site"
            if ident.kind == "cookie" and ident.scope != "control":
                return "view_only"
        state = self.admin_state(headers, ident)
        if state == "ok":
            return "ok"
        if ident is None or ident.kind == "none":
            return "anonymous"
        return state

    def set_admin_password(self, new: str):
        """Choose (or change) the admin password. Every earlier unlock stops working."""
        self.dash.set_fields(admin_hash=hash_password(new))
        self.dash.bump_admin_epoch()

    def clear_admin_password(self):
        self.dash.set_fields(admin_hash=None)
        self.dash.bump_admin_epoch()

    def login_required(self) -> bool:
        """Must the dashboard be signed in to? Off unless the owner turned it on (Settings > Security) -
        or, for an install that had a dashboard password before the switch existed, that password. Never
        true without an owner password, so it cannot lock everybody out."""
        if not self.dash.owner_hash:
            return False
        en = self.dash.login_enabled
        return True if en is None else bool(en)

    def offer_login(self) -> bool:
        """A brand-new install that has not yet decided about a dashboard password (set one, or skip)."""
        return (self.keys.meta.get("install") == "fresh" and not self.dash.offer_dismissed
                and self.dash.login_enabled is None and not self.dash.owner_hash and not self.dash.admin_hash)

    def first_run_required(self) -> bool:
        """Is this Hub waiting for the first-run "Protect your printers" step? Only a FRESH install that has no
        dashboard password and has not skipped the step. Never on an upgrade (its owner chose how it is protected),
        and not while the API key check is Warn / Off (the owner lowered it on purpose, and dashboard sign-in
        cannot be switched on then - it would only be decoration)."""
        return self.offer_login() and self.mode() == "on"

    def complete_first_run(self, password, ip="", ua="", device_name=None) -> tuple:
        """The first-run step: this password becomes the dashboard password, sign-in is switched ON, and the browser
        that asked is made a trusted device (-> public device record, cookie value, max_age). The dashboard password
        also stands in for the admin password (see admin_state) - no separate admin password is created, so the
        owner can still choose a different one later. Raises AuthError (409 when the step is not pending)."""
        with self._first_run_lock:
            if not self.first_run_required():
                raise AuthError("There is nothing to set up: this Hub already has its first-run answer.", 409)
            if not isinstance(password, str) or not (MIN_PASSWORD <= len(password) <= 200) or not password.strip():
                raise AuthError(f"The password needs {MIN_PASSWORD}-200 characters.", 400)
            self.settings.update({"security.dashboard_password": password})     # _settings_changed stores the hash
            if not self.dash.owner_hash:
                raise AuthError("The password could not be saved.", 500)
            self.dash.set_fields(login_enabled=True, offer_dismissed=True)
            return self.add_device("control", device_name, ip, ua)

    def skip_first_run(self):
        """'Skip' on the first-run step: recorded, so the step does not come back. Sign-in stays off."""
        self.dash.set_fields(offer_dismissed=True)

    def session_config(self) -> dict:
        return self.dash.config()

    def allow_framing(self) -> bool:
        return (self.env.get("BAMBU_MCP_ALLOW_FRAMING") or "").strip().lower() in ("1", "true", "yes")

    # ---- the warn log -----------------------------------------------------------------
    def record_unauthenticated(self, method, path, headers, remote, problem, detail=None, allowed=True):
        """Count a request that came without a usable credential. allowed=True: warn mode let it through."""
        ua = (headers.get("user-agent") or "")[:200]
        xff = (headers.get("x-forwarded-for") or "")[:100]
        now = _now()
        key = (remote, ua, method, path[:200], detail or "")
        with self._lock:
            self.stats["unauthenticated_allowed" if allowed else "rejected"] += 1
            row = self._callers.pop(key, None)
            if row is None:
                row = {"remote_addr": remote, "user_agent": ua, "method": method, "path": path[:200],
                       "detail": detail, "count": 0, "first_seen": now, "reason": problem,
                       "allowed": allowed}
                if xff:
                    row["forwarded_for"] = xff
            row["count"] += 1
            row["last_seen"], row["reason"], row["allowed"] = now, problem, allowed
            self._callers[key] = row
            while len(self._callers) > MAX_CALLERS:
                self._callers.popitem(last=False)
            self.last_unauthenticated = {k: row[k] for k in
                                         ("remote_addr", "user_agent", "method", "path", "detail", "reason")
                                         } | {"time": now, "allowed": allowed}

    def warnings(self) -> list:
        """Things the owner should fix, in words (shown on the Security page)."""
        out = []
        if not self.admin_password_set():
            out.append({"id": "no_admin_password",
                        "text": ("No admin password is set, so API keys and the security settings cannot be managed. "
                                 "Set one in Settings \u2192 Security." if not self.password_set() else
                                 "No separate admin password is set: a full-access sign-in can manage keys and security. "
                                 "Set an admin password for a second lock.")})
        if not self.password_set():
            out.append({"id": "no_dashboard_password",
                        "text": "No dashboard password is set: anyone who can open this Hub's address in a browser "
                                "can operate the printers. They cannot create keys or turn the key check off "
                                "without the admin password."})
        if self.login_required() and self.mode() != "on":
            out.append({"id": "signin_needs_key_check",
                        "text": "Dashboard sign-in is on but the API key check is not: requests without a "
                                "cookie are still only checked as the key check allows. Switch the key check to On."})
        if self.mode() != "on":
            out.append({"id": "key_check_" + self.mode(),
                        "text": ("The key check is OFF: nothing is checked." if self.mode() == "off" else
                                 "The key check is in WARN mode: requests without a key still work. Switch to ON once "
                                 "your agents have keys.")})
        return out

    def count_forbidden(self):
        with self._lock:
            self.stats["forbidden"] += 1

    def status(self) -> dict:
        with self._lock:
            callers = list(self._callers.values())[::-1]       # newest activity first
            stats = dict(self.stats)
            last = dict(self.last_unauthenticated) if self.last_unauthenticated else None
        src = self.settings.get_all()["sources"].get("security.api_auth", "default")
        return {
            "mode": self.mode(), "mode_source": src, "modes": list(MODES),
            "install": self.keys.meta.get("install"), "initial_mode": self.keys.meta.get("initial_mode"),
            "dashboard_password": "set" if self.password_set() else "not set",
            "admin_password": "set" if self.admin_password_set() else "not set",
            "dashboard_login": self.login_required(),
            "viewer_password": "set" if self.viewer_password_set() else "not set",
            "session_settings": {**self.dash.config(), "limits": {k: list(v) for k, v in CONFIG_LIMITS.items()},
                                 "min_password": MIN_PASSWORD},
            "keys": self.keys.list_public(), "key_counts": self.keys.counts(),
            "warnings": self.warnings(),
            "keys_made_without_check": [k["id"] for k in self.keys.list_public()
                                        if k.get("created_mode") in ("warn", "off") and not k.get("revoked")],
            "stats": {"since_start": True, "authenticated_with_key": stats.get("authenticated_key", 0),
                      "authenticated_with_dashboard": stats.get("authenticated_cookie", 0),
                      "without_key_but_allowed": stats.get("unauthenticated_allowed", 0),
                      "rejected": stats.get("rejected", 0), "refused_wrong_scope": stats.get("forbidden", 0)},
            "warn": {"last_unauthenticated": last, "recent_callers": callers[:25],
                     "note": "Requests that arrived without a valid key since the Hub started, grouped by "
                             "caller and path. In warn mode these still worked; in on mode they get 401."},
        }


PRIVATE_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain",
                    ".intranet", ".corp", ".ts.net")


def cookie_value(headers: dict, name: str = COOKIE_NAME):
    raw = headers.get("cookie")
    if not raw:
        return None
    try:
        jar = http.cookies.SimpleCookie()
        jar.load(raw)
        m = jar.get(name)
        return m.value if m else None
    except Exception:
        return None


def cookie_header(name: str, value: str, max_age: int, secure: bool) -> str:
    """A Set-Cookie header value: HttpOnly, SameSite=Strict, Secure over https."""
    return (f"{name}={value}; Max-Age={int(max_age)}; Path=/; HttpOnly; SameSite=Strict"
            + ("; Secure" if secure else ""))


def client_ip(headers: dict, remote: str) -> str:
    """The address to show for a device: the connecting address, or - when that is this machine
    (a local reverse proxy such as tailscale serve) - the first X-Forwarded-For entry. Display only,
    never used for a decision."""
    try:
        loop = ipaddress.ip_address(remote).is_loopback
    except ValueError:
        loop = False
    xff = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return (xff if loop and xff else remote or "")[:64]


def connection_info(scheme: str, headers: dict) -> dict:
    """Is this request encrypted? In words for the Security tab and the login page."""
    secure = secure_request(scheme, headers)
    return {"secure": secure, "scheme": "https" if secure else "http",
            "words": ("Secure (HTTPS): passwords and sign-in cookies are encrypted on the way to the Hub."
                      if secure else
                      "Not secure (plain http): a password typed here travels unencrypted and anyone watching "
                      "the network could read it. Put the Hub behind HTTPS, for example tailscale serve.")}


_HOST_CHARS = re.compile(r"^[a-z0-9_.-]+$")


def parse_host(raw):
    """The bare, lower-case host of a Host header value (`name`, `name:port`, `1.2.3.4:port`, `[::1]:port`), or None
    when it is not that: userinfo (`a@b`), a path, spaces, a port that is not a number 1-65535, odd characters."""
    host = (raw or "").strip().lower()
    if not host or len(host) > 255 or re.search(r"[@/\\?#\s,;]", host):
        return None
    port = None
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return None
        rest, host = host[end + 1:], host[1:end]
        if rest:
            if not rest.startswith(":"):
                return None
            port = rest[1:]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return None
    elif host.count(":") == 1:
        host, port = host.rsplit(":", 1)
    elif host.count(":") >= 2:              # a bare IPv6 address (browsers always bracket it)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return None
        return host
    if port is not None and not (port.isdigit() and len(port) <= 5 and 1 <= int(port) <= 65535):
        return None
    host = host.rstrip(".")
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    return host if _HOST_CHARS.match(host) else None


def same_origin(headers: dict, trust_forwarded: bool = True) -> bool:
    """CSRF check for cookie-authenticated writes: the browser says the request is same-origin. A request that
    carries neither Origin nor Sec-Fetch-Site (curl, an agent) passes - see same_origin_strict for the
    stricter form. `trust_forwarded`: may X-Forwarded-Host stand in for Host (a reverse proxy rewrote it)?"""
    sfs = headers.get("sec-fetch-site")
    if sfs is not None and sfs.strip().lower() not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    if origin is not None:
        try:
            netloc = urlsplit(origin).netloc.lower()
        except ValueError:
            return False
        host = (headers.get("host") or "").strip().lower()
        fwd = (headers.get("x-forwarded-host") or "").split(",")[0].strip().lower() if trust_forwarded else ""
        if not netloc or netloc not in (host, fwd):
            return False
    return True


def same_origin_strict(headers: dict, trust_forwarded: bool = True) -> bool:
    """Same-origin AND positively so: Sec-Fetch-Site is same-origin, or an Origin header matches this Hub. Every
    browser sends one of the two on a fetch() POST/PUT; a bare curl sends neither and fails. (A script can still
    forge the headers - this only stops the naive request, which is why the first admin password is also
    something to set early.)"""
    if not same_origin(headers, trust_forwarded):
        return False
    sfs = headers.get("sec-fetch-site")
    return (sfs is not None and sfs.strip().lower() == "same-origin") or headers.get("origin") is not None


def is_json_type(content_type) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct == "application/json" or (ct.startswith("application/") and ct.endswith("+json"))


def has_body(headers: dict) -> bool:
    cl = headers.get("content-length")
    if cl is None:
        return "transfer-encoding" in headers
    return cl.strip() not in ("", "0")


def secure_request(scheme: str, headers: dict) -> bool:
    return scheme == "https" or (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https"


# ---------------------------------------------------------------- ASGI middleware

def _json_response(payload, status=200, headers=None):
    from starlette.responses import JSONResponse
    return JSONResponse(payload, status_code=status, headers=headers)


async def _buffer(receive, limit):
    """Read the request body (up to `limit` bytes). -> (body or None when it is bigger, receive)
    where `receive` replays everything read so the app still sees the whole request."""
    msgs, body, size, more = [], b"", 0, True
    while more:
        msg = await receive()
        msgs.append(msg)
        if msg["type"] != "http.request":
            break
        chunk = msg.get("body", b"")
        size += len(chunk)
        if size > limit:
            body = None
            break
        body += chunk
        more = msg.get("more_body", False)
    queue = list(msgs)

    async def replay():
        if queue:
            return queue.pop(0)
        return await receive()

    return body, replay


def _no_duplicate_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def parse_rpc_strict(body):
    """The JSON-RPC messages of a request body as a list of dicts - or None when the body is anything the
    scope check cannot read with certainty. Strict on purpose: the MCP SDK's own parser is more forgiving
    (it also reads a UTF-8 BOM, UTF-16 and UTF-32), and anything one side reads that the other does not is a
    way past the check. Only plain UTF-8 JSON, no BOM, no duplicate keys, an object or a non-empty array of
    objects, is accepted; the caller must refuse the rest (fail closed)."""
    if not isinstance(body, (bytes, bytearray)):
        return None
    raw = bytes(body)
    if raw.startswith(b"\xef\xbb\xbf"):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        return None
    if isinstance(doc, dict):
        return [doc]
    if isinstance(doc, list) and doc and all(isinstance(m, dict) for m in doc):
        return doc
    return None


def _rpc_messages(body):
    return parse_rpc_strict(body)


def mcp_detail(body):
    """Short label for the warn log: 'tools/call:start_print', 'initialize', ..."""
    if not body:
        return None
    msgs = _rpc_messages(body)
    if not msgs:
        return "(unreadable body)"
    labels = []
    for m in msgs[:3]:
        meth = m.get("method")
        if meth == "tools/call" and isinstance(m.get("params"), dict):
            meth = f"tools/call:{m['params'].get('name')}"
        labels.append(str(meth))
    return ",".join(labels)[:100]


def refused_tools(body, scope):
    """[(message dict, tool name)] for tools/call messages `scope` may not make. A body that cannot be read
    at all raises ValueError (see parse_rpc_strict) - the caller must refuse it."""
    msgs = _rpc_messages(body)
    if msgs is None:
        raise ValueError("unreadable MCP request body")
    out = []
    for m in msgs:
        if m.get("method") == "tools/call":
            params = m.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if SCOPE_RANK[scope] < SCOPE_RANK[tool_scope(name)]:
                out.append((m, name))
    return out


def tool_refusal_text(name, scope):
    return (f"REFUSED: this API key has '{scope}' access, and '{name}' needs a 'control' key. "
            "Ask the owner for a control key (dashboard > Security), or use a read-only tool.")


# Headers added to every response (and, for HTML, the page protections). The dashboard is ONE file with inline
# scripts, inline event handlers and inline styles, so script-src / style-src must allow 'unsafe-inline' -
# a nonce or hash policy would need the page split up. Everything else is locked down: no foreign
# scripts, styles, images, frames, forms or plug-ins, and nobody may frame the page (clickjacking).
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; font-src 'self' data:; "
       "object-src 'none'; base-uri 'none'; form-action 'self'")
CSP_FRAMES = "; frame-ancestors 'none'"


class _Out:
    """Wraps `send`: adds the security headers (and any Set-Cookie queued for this response). For an MCP request it
    also tells `on_session` the session id the response created (see AuthMiddleware: ids are bound to a principal)."""

    def __init__(self, send, allow_framing, on_session=None):
        self.send, self.allow_framing, self.cookies, self.on_session = send, allow_framing, [], on_session

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            hdrs = list(message.get("headers") or [])
            have = {k.lower() for k, _v in hdrs}
            ctype = next((v for k, v in hdrs if k.lower() == b"content-type"), b"").lower()
            add = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin")]
            if ctype.startswith(b"text/html"):
                if not self.allow_framing:
                    add.append((b"x-frame-options", b"DENY"))
                add.append((b"content-security-policy",
                            (CSP + ("" if self.allow_framing else CSP_FRAMES)).encode()))
            hdrs += [(k, v) for k, v in add if k not in have]
            hdrs += [(b"set-cookie", c.encode("latin-1")) for c in self.cookies]
            message = {**message, "headers": hdrs}
            if self.on_session is not None:
                sid = next((v for k, v in hdrs if k.lower() == b"mcp-session-id"), None)
                if sid:
                    try:
                        self.on_session(sid.decode("latin-1"))
                    except Exception:
                        pass
        await self.send(message)


MAX_MCP_SESSIONS = 2000


class AuthMiddleware:
    """Pure ASGI. Wraps the whole app (MCP endpoint + REST routes + dashboard)."""

    def __init__(self, app, auth: AuthManager, mcp_path="/mcp"):
        self.app, self.auth = app, auth
        self.mcp_path = "/" + mcp_path.strip("/")
        self._sessions = collections.OrderedDict()          # MCP session id -> the principal that created it
        self._sessions_lock = threading.Lock()

    def _is_mcp(self, path):
        return path == self.mcp_path or path.startswith(self.mcp_path + "/")

    # ---- MCP sessions belong to whoever opened them ------------------------------------------
    @staticmethod
    def _principal(ident) -> str:
        return f"key:{ident.key_id}" if ident is not None and ident.kind == "key" else "anonymous"

    def _session_conflict(self, headers, principal):
        """A 403 response when this MCP request names a session that a DIFFERENT principal opened, else None."""
        sid = headers.get("mcp-session-id")
        owner = self._session_owner(sid) if sid else None
        if owner is not None and owner != principal:
            self.auth.count_forbidden()
            return _json_response(
                {"error": "this MCP session was opened by a different key; open your own with initialize"}, 403)
        return None

    def _bind_session(self, sid, principal):
        with self._sessions_lock:
            self._sessions[sid] = principal
            self._sessions.move_to_end(sid)
            while len(self._sessions) > MAX_MCP_SESSIONS:
                self._sessions.popitem(last=False)

    def _session_owner(self, sid):
        with self._sessions_lock:
            return self._sessions.get(sid)

    def _principal_for(self, headers):
        """Who is behind an MCP request, as a string. Only a valid key makes a named principal."""
        token = extract_key(headers)
        if token is not None:
            rec = self.auth.keys.verify(token)
            if rec is not None and not rec.get("revoked"):
                return f"key:{rec['id']}"
        return "anonymous"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        auth = self.auth
        method, path = scope["method"].upper(), scope["path"]
        headers = _headers(scope)
        is_mcp = self._is_mcp(path)
        remote = (scope.get("client") or ("",))[0] or ""
        who = ["anonymous"]                      # the principal of an MCP request; set once the key is known
        send = out = _Out(send, auth.allow_framing(),
                          on_session=(lambda sid: self._bind_session(sid, who[0])) if is_mcp else None)
        mode = auth.mode()
        # The browser boundary applies in EVERY mode (off/warn/on): cross-site, rebinding, non-JSON bodies.
        refusal = auth.browser_guard(headers, method, path, mode, remote)
        if refusal is not None:
            auth.stats["browser_guard"] += 1
            return await _json_response({"error": refusal[1]}, refusal[0])(scope, receive, send)
        admin = (not is_mcp) and rest_scope(method, path) == "admin" and not auth.is_exempt(method, path)
        # Dashboard sign-in only means something while the key check is ON (otherwise a request that simply sends
        # no cookie is let through in warn / off). An install that has sign-in on with the check off or in warn
        # (it was set up before this rule) gets ON for the REST API; MCP keeps the mode's leniency.
        eff_mode = "on" if (mode != "on" and not is_mcp and auth.login_required()) else mode
        if eff_mode == "off" or auth.is_exempt(method, path):
            if is_mcp:
                who[0] = self._principal_for(headers)
                conflict = self._session_conflict(headers, who[0])
                if conflict is not None:
                    return await conflict(scope, receive, send)
            if eff_mode == "off" and not auth.is_exempt(method, path) and admin:
                ident = auth.cookie_identity(headers, method)
                verdict = auth.admin_verdict(headers, ident)
                if verdict != "ok":
                    return await self._admin_refusal(verdict, ident)(scope, receive, send)
            return await self.app(scope, receive, send)
        ident = auth.identify(headers, method, allow_cookie=not is_mcp)
        scope["auth"] = ident
        who[0] = self._principal(ident)

        # A fresh install that has not yet answered "Protect your printers": a browser (the no-password dashboard
        # cookie) may not change anything until it has - API keys and reads are unaffected.
        if (ident.kind == "cookie" and method not in SAFE_METHODS and not is_mcp
                and not (method == "POST" and path in FIRST_RUN_POSTS) and auth.first_run_required()):
            auth.count_forbidden()
            return await _json_response({"error": FIRST_RUN_MSG, "first_run": True}, 403)(scope, receive, send)

        if admin:
            verdict = auth.admin_verdict(headers, ident)
            if verdict == "anonymous":
                # never open to an unauthenticated caller (warn mode included) and never to an API key
                auth.record_unauthenticated(method, path, headers, remote, ident.problem, None, allowed=False)
                return await self._deny(ident)(scope, receive, send)
            if verdict != "ok":
                auth.count_forbidden()
                return await self._admin_refusal(verdict, ident)(scope, receive, send)
            if ident.kind != "none":
                auth.touch(ident, path, client_ip(headers, remote))
            return await self.app(scope, receive, send)

        if ident.kind == "none":
            detail = None
            if is_mcp and method == "POST":
                body, receive = await _buffer(receive, BODY_SNIFF_LIMIT)
                detail = mcp_detail(body)
            auth.record_unauthenticated(method, path, headers, remote, ident.problem, detail,
                                        allowed=(eff_mode == "warn"))
            if eff_mode == "warn":
                if is_mcp:
                    conflict = self._session_conflict(headers, who[0])
                    if conflict is not None:
                        return await conflict(scope, receive, send)
                return await self.app(scope, receive, send)
            return await self._deny(ident)(scope, receive, send)

        if is_mcp:
            conflict = self._session_conflict(headers, who[0])
            if conflict is not None:
                return await conflict(scope, receive, send)
        need = "read" if is_mcp else rest_scope(method, path)
        if ident_rank(ident) < SCOPE_RANK[need]:
            auth.count_forbidden()
            return await _json_response(
                {"error": (f"This API key has '{ident.scope}' access; this needs a 'control' key."
                           if ident.kind == "key" else
                           "This sign-in is view-only; this needs full access."),
                 "required_scope": need}, 403)(scope, receive, send)
        if is_mcp and method == "POST" and ident.scope != "control":
            body, receive = await _buffer(receive, BODY_SNIFF_LIMIT)
            if body is None:
                auth.count_forbidden()
                return await _json_response({"error": "request too large for a read-only key"}, 413)(
                    scope, receive, send)
            try:
                bad = refused_tools(body, ident.scope)
            except ValueError:
                # fail closed: a body the check cannot read (BOM, UTF-16/32, bad JSON, duplicate keys...) is
                # never handed to the MCP SDK, whose parser is more forgiving than ours
                auth.count_forbidden()
                return await _json_response(
                    {"error": "this request body cannot be read; a read-only key must send plain UTF-8 JSON-RPC "
                              "(no byte-order mark, no UTF-16/32, no duplicate keys)"}, 400)(scope, receive, send)
            if bad:
                auth.count_forbidden()
                return await self._tool_refused(bad, body, ident.scope)(scope, receive, send)
        auth.touch(ident, path, client_ip(headers, remote))
        if ident.kind == "cookie" and ident.device_id:
            fresh = auth.maybe_rotate_device(headers, secure_request(scope.get("scheme", "http"), headers))
            if fresh:
                out.cookies.append(fresh)
        return await self.app(scope, receive, send)

    @staticmethod
    def _admin_refusal(verdict, ident=None):
        if verdict == "cross_site":
            return _json_response({"error": "cross-site request refused"}, 403)
        if verdict == "unset":
            return _json_response({"error": ADMIN_UNSET_MSG, "admin": "unset", "required_scope": "admin"}, 403)
        if verdict == "locked":
            return _json_response({"error": ADMIN_LOCKED_MSG, "admin": "locked", "required_scope": "admin"}, 403)
        return _json_response({"error": "This needs the dashboard: sign in to it in a browser and unlock it with the "
                                        "admin password. API keys (and view-only sign-ins) cannot manage keys, "
                                        "sign-in or the key check.",
                               "required_scope": "admin"}, 403)

    @staticmethod
    def _deny(ident):
        if ident.problem == "cross_site":
            return _json_response({"error": "cross-site request refused"}, 403)
        body = {"error": "API key required"}
        if ident.problem in ("invalid_key", "revoked_key"):
            body["hint"] = ("that API key is not valid" if ident.problem == "invalid_key"
                            else "that API key has been revoked")
        return _json_response(body, 401, headers={"WWW-Authenticate": 'Bearer realm="print-hub"'})

    @staticmethod
    def _tool_refused(bad, body, scope):
        msgs = _rpc_messages(body) or []
        if len(msgs) == 1:                  # the normal case: answer the call itself, as a tool error
            m, name = bad[0]
            return _json_response({"jsonrpc": "2.0", "id": m.get("id"),
                                   "result": {"content": [{"type": "text", "text": tool_refusal_text(name, scope)}],
                                              "isError": True}})
        names = ", ".join(sorted({str(n) for _m, n in bad}))
        return _json_response({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32001, "message": tool_refusal_text(names, scope)}}, 403)


# ---------------------------------------------------------------- dashboard login page

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fabrica - sign in</title>
<link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f3b66">
<style>
:root{--bg:#f4f5f7;--fg:#1b1f24;--card:#fff;--line:#c9ced6;--accent:#0f3b66;--on:#fff;--err:#b00020;--dim:#58606b}
@media (prefers-color-scheme: dark){:root{--bg:#14171c;--fg:#e8eaed;--card:#1d2128;--line:#3a414b;--accent:#5aa5e6;--on:#0b1520;--err:#ff8a80;--dim:#a3abb5}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;width:min(360px,92vw);margin:12px 0}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 14px;color:var(--dim)}
input[type=password],input[type=text],button{width:100%;font:inherit;padding:12px;border-radius:8px;border:1px solid var(--line)}
input[type=password],input[type=text]{background:transparent;color:inherit;margin-bottom:12px}
button{background:var(--accent);color:var(--on);border:0;font-weight:600;cursor:pointer;min-height:48px}
button.alt{background:transparent;color:var(--fg);border:1px solid var(--line);margin-top:10px}
button:disabled{opacity:.55;cursor:default}
label.chk{display:flex;gap:10px;align-items:center;margin:2px 0 12px;font-size:16px;min-height:32px}
label.chk input{width:22px;height:22px;margin:0}
label.nm{display:block;font-size:14px;color:var(--dim);margin:0 0 4px}
#msg,#pmsg{min-height:1.4em;margin-top:12px;color:var(--err);font-weight:600}
#conn{margin-top:10px;font-size:13px;color:var(--dim)}
#slots{display:flex;gap:8px;justify-content:center;margin:4px 0 12px}
#slots span{width:44px;height:56px;border:2px solid var(--line);border-radius:8px;display:flex;align-items:center;justify-content:center;font:700 28px ui-monospace,Menlo,monospace}
#slots span.on{border-color:var(--accent)}
#pad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
#pad button{min-height:60px;font-size:24px;padding:6px}
#pad button.fn{background:transparent;color:var(--fg);border:1px solid var(--line);font-size:16px}
@media (max-height:640px){.card{padding:14px;margin:4px 0}#pad button{min-height:50px;font-size:22px}#slots span{height:46px}p{margin-bottom:8px}}
</style></head><body>
<main>
<form id="f" class="card"><img src="/static/brand/mark.svg" width="40" height="40" alt="" style="display:block;margin:0 0 10px"><h1>Fabrica</h1><p>Sign in to open the dashboard.</p>
<input id="pw" type="password" autocomplete="current-password" autofocus aria-label="Dashboard password" placeholder="Password">
<label class="chk"><input id="rem" type="checkbox"> Remember this device</label>
<div id="remrow" hidden><label class="nm" for="dn">Name this device</label><input id="dn" type="text" maxlength="40" autocomplete="off" placeholder="e.g. Shop iPad"></div>
<button type="submit" id="go">Sign in</button>
<button type="button" class="alt" id="topair">Pair with a code from the Hub</button>
<div id="msg" role="alert"></div><div id="conn"></div></form>
<div id="pair" class="card" hidden><h1>Pair this screen</h1><p>On the Hub, open Settings, Security and press Add a device. Type the 6-digit code it shows.</p>
<div id="slots" aria-live="polite" aria-label="Code typed so far"></div>
<div id="pad"></div>
<div id="pmsg" role="alert"></div>
<button type="button" class="alt" id="toform">Back to password</button></div>
</main>
<script>
(function(){
var $=function(i){return document.getElementById(i)};
$('conn').textContent = location.protocol==='https:' ? 'Connection: secure (HTTPS).' :
  'Connection: not secure (plain http). The password is sent unencrypted; use HTTPS if others share this network.';
function guess(){var u=navigator.userAgent||'',p=navigator.platform||'',os='Device',br='browser';
  if(/iPad/.test(u)||(/Mac/.test(p)&&navigator.maxTouchPoints>1))os='iPad';else if(/iPhone|iPod/.test(u))os='iPhone';
  else if(/Android/.test(u))os='Android';else if(/CrOS/.test(u))os='ChromeOS';else if(/Windows/.test(u))os='Windows';
  else if(/Mac/.test(u))os='Mac';else if(/Linux|X11/.test(u))os='Linux';
  if(/Edg\\//.test(u))br='Edge';else if(/Firefox|FxiOS/.test(u))br='Firefox';else if(/Chromium/.test(u))br='Chromium';
  else if(/CriOS|Chrome\\//.test(u))br='Chrome';else if(/Safari\\//.test(u))br='Safari';
  return os+' \u00b7 '+br;}
$('dn').value=guess();
$('rem').addEventListener('change',function(){$('remrow').hidden=!this.checked;});
async function post(url,body){
  var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  var j={};try{j=await r.json();}catch(x){}
  return {ok:r.ok,status:r.status,body:j};
}
$('f').addEventListener('submit',async function(e){
  e.preventDefault();
  var m=$('msg'); m.textContent=''; $('go').disabled=true;
  try{
    var rem=$('rem').checked;
    var r=await post('/api/auth/login',{password:$('pw').value,remember:rem,device_name:rem?$('dn').value:''});
    if(r.ok){location.reload();return;}
    m.textContent=r.body.error||('Sign-in failed ('+r.status+')');
  }catch(x){m.textContent='Could not reach the Hub.';}
  $('go').disabled=false;
});
var code='';
function draw(){var h='';for(var i=0;i<6;i++)h+='<span'+(i<code.length?' class="on"':'')+'>'+(i<code.length?code[i]:'')+'</span>';$('slots').innerHTML=h;}
var keys=['1','2','3','4','5','6','7','8','9','Clear','0','Back'];
keys.forEach(function(k){var b=document.createElement('button');b.type='button';b.textContent=k;
  if(k==='Clear'||k==='Back')b.className='fn';
  b.addEventListener('click',function(){press(k);});$('pad').appendChild(b);});
async function press(k){
  $('pmsg').textContent='';
  if(k==='Clear')code='';else if(k==='Back')code=code.slice(0,-1);else if(code.length<6)code+=k;
  draw();
  if(code.length===6){
    $('pad').querySelectorAll('button').forEach(function(b){b.disabled=true;});
    try{
      var r=await post('/api/auth/pair',{code:code});
      if(r.ok){location.reload();return;}
      $('pmsg').textContent=r.body.error||('Pairing failed ('+r.status+')');
    }catch(x){$('pmsg').textContent='Could not reach the Hub.';}
    code='';draw();
    $('pad').querySelectorAll('button').forEach(function(b){b.disabled=false;});
  }
}
document.addEventListener('keydown',function(e){if(!$('pair').hidden&&/^[0-9]$/.test(e.key))press(e.key);});
$('topair').addEventListener('click',function(){$('f').hidden=true;$('pair').hidden=false;code='';draw();});
$('toform').addEventListener('click',function(){$('pair').hidden=true;$('f').hidden=false;$('pw').focus();});
draw();
})();
</script></body></html>
"""


# ---------------------------------------------------------------- command line

def _read_new_password(args, label):
    """The new password for a CLI command: from --password-stdin (one line), else asked for twice at the prompt.
    `--password VALUE` still works but is on the command line (visible in `ps` and the shell history): warned."""
    import getpass
    if getattr(args, "password", None) is not None:
        print("warning: --password puts the password in the process list and your shell history. "
              "Prefer --password-stdin or the prompt.", file=sys.stderr)
        return args.password
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\r\n")
    pw = getpass.getpass(f"New {label}: ")
    if pw != getpass.getpass("Again: "):
        print("error: the two passwords differ.", file=sys.stderr)
        return None
    return pw


def _cli(argv):
    """python security.py create-key NAME [--scope read|control]   |   list-keys
       python security.py disable-dashboard-login | reset-dashboard-password [--password-stdin] | list-devices | revoke-device ID
       python security.py reset-first-run
       python security.py reset-admin-password [--password-stdin] | clear-admin-password
    Works on the same data directory as the server (BAMBU_MCP_CACHE's folder); the running server picks the
    change up on its next request (and forgets its failed-sign-in records). This is also the way back in if every
    key, or the dashboard / admin password, was lost."""
    import argparse
    ap = argparse.ArgumentParser(prog="security.py", description="Manage Fabrica API keys and dashboard sign-in offline.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-key", help="make a key and print it once")
    c.add_argument("name")
    c.add_argument("--scope", choices=SCOPES, default="read")
    sub.add_parser("list-keys", help="list keys (never shows the key itself)")
    sub.add_parser("disable-dashboard-login", help="turn the dashboard sign-in OFF (keeps devices and keys); "
                                                   "use this when you are locked out")
    r = sub.add_parser("reset-dashboard-password", help="set a new owner password and sign every full-access browser and device out")
    r.add_argument("--password", help="the new password (8+ characters) - NOT recommended: it shows in `ps` and shell history")
    r.add_argument("--password-stdin", action="store_true", help="read the new password from standard input")
    ra = sub.add_parser("reset-admin-password", help="set a new admin password (the one that guards keys and security settings)")
    ra.add_argument("--password", help="the new password - NOT recommended: it shows in `ps` and shell history")
    ra.add_argument("--password-stdin", action="store_true", help="read the new password from standard input")
    sub.add_parser("clear-admin-password", help="remove the admin password (admin actions are then refused until a new one is set)")
    sub.add_parser("reset-first-run", help="show the first-run 'Protect your printers' step again (only on a new install "
                                           "that has no dashboard password)")
    sub.add_parser("list-devices", help="list trusted devices (never shows a token)")
    d = sub.add_parser("revoke-device", help="forget one trusted device (or 'all')")
    d.add_argument("device_id", metavar="ID")
    args = ap.parse_args(argv)
    if os.environ.get("BAMBU_MCP_CACHE"):
        data_dir = os.path.dirname(os.environ["BAMBU_MCP_CACHE"])
    else:
        data_dir = os.path.dirname("/data/metacache.json")
        print(f"note: BAMBU_MCP_CACHE is not set, so this works on {data_dir} (the Docker default).", file=sys.stderr)
    store = KeyStore(data_dir)
    if args.cmd == "create-key":
        try:
            rec, token = store.create(args.name, args.scope)
        except AuthError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"Created {rec['scope']} key '{rec['name']}' ({rec['id']}). Copy it now - it is never shown again:\n")
        print(token)
        return 0
    if args.cmd == "list-keys":
        for k in store.list_public():
            print(f"{k['id']}  {k['scope']:<8} {k['prefix']}...  {'REVOKED ' if k['revoked'] else ''}{k['name']}"
                  f"  last used: {k['last_used'] or 'never'}")
        return 0
    dash = DashboardAuthStore(data_dir)
    if args.cmd == "disable-dashboard-login":
        dash.set_fields(login_enabled=False)
        touch_limiter_reset(data_dir)
        print("Dashboard sign-in is now OFF. The dashboard opens without a password again "
              "(the running Hub notices on its next request). Turn it back on in Settings > Security.")
        return 0
    if args.cmd == "reset-dashboard-password":
        pw = _read_new_password(args, "dashboard password")
        if pw is None:
            return 1
        if len(pw) < MIN_PASSWORD or len(pw) > 200:
            print(f"error: a password needs {MIN_PASSWORD}-200 characters.", file=sys.stderr)
            return 1
        dash.set_fields(owner_hash=hash_password(pw), offer_dismissed=True)
        store.rotate_session_secret()
        dash.clear_sessions()
        gone = dash.remove_devices_where(lambda r: r.get("role") == "control")
        dash.bump_admin_epoch()
        touch_limiter_reset(data_dir)
        state = "ON" if (dash.login_enabled is None or dash.login_enabled) else "OFF (turn it on in Settings > Security)"
        print(f"Dashboard password changed; every signed-in browser must sign in again and {gone} full-access trusted "
              f"device(s) were forgotten. Sign-in is {state}. View-only devices were not touched - revoke them with "
              "revoke-device if needed.")
        return 0
    if args.cmd == "reset-first-run":
        dash.set_fields(offer_dismissed=False)
        if store.meta.get("install") != "fresh":
            print("Noted, but this Hub was an existing install when it first started, so it never shows the first-run "
                  "step. Set a dashboard password in Settings > Security instead.")
        elif dash.owner_hash:
            print("Noted, but a dashboard password is already set, so the first-run step stays hidden.")
        else:
            print("The first-run step ('Protect your printers') will show again the next time the dashboard is opened "
                  "(it needs the API key check to be On).")
        touch_limiter_reset(data_dir)
        return 0
    if args.cmd == "reset-admin-password":
        pw = _read_new_password(args, "admin password")
        if pw is None:
            return 1
        if len(pw) < MIN_PASSWORD or len(pw) > 200:
            print(f"error: a password needs {MIN_PASSWORD}-200 characters.", file=sys.stderr)
            return 1
        dash.set_fields(admin_hash=hash_password(pw))
        dash.bump_admin_epoch()
        touch_limiter_reset(data_dir)
        print("Admin password changed; everyone who had unlocked the admin actions must unlock again.")
        return 0
    if args.cmd == "clear-admin-password":
        dash.set_fields(admin_hash=None)
        dash.bump_admin_epoch()
        touch_limiter_reset(data_dir)
        print("Admin password removed. Admin actions are refused until a new one is set (Settings > Security).")
        return 0

    def when(ts):
        return _iso(ts) or "never"
    if args.cmd == "list-devices":
        devs = sorted(dash.devices(), key=lambda r: -(r.get("last_seen") or 0))
        if not devs:
            print("No trusted devices.")
        for r in devs:
            print(f"{r['id']}  {ROLE_WORDS.get(r.get('role'), 'View-only'):<11} {r.get('name', '')}"
                  f"  ({r.get('label', '')})  first seen {when(r.get('created'))}  last seen {when(r.get('last_seen'))}"
                  f"  from {r.get('ip') or 'unknown'}")
        return 0
    if args.device_id.lower() == "all":
        n = dash.remove_devices_except(None)
        print(f"Revoked {n} device(s).")
        return 0
    try:
        rec = dash.remove_device(args.device_id)
    except AuthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Revoked device {rec['id']} ({rec.get('name', '')}). It must sign in again.")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
