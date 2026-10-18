"""PrusaLink driver for Fabrica (Prusa MK4/MK4S/MK3.9/Core One/XL/Mini, firmware 5/6+).

PrusaLink is the web service built into Prusa printers. It speaks a small REST
API ("v1") over plain HTTP on the LAN. This driver mirrors the public surface of
`OctoPrintPrinter` in bambu.py so server.py / backup.py can treat it the same
way (see docs/drivers.md for the wiring recipe).

Endpoints used (PrusaLink OpenAPI v1; see "uncertain" notes in docs/drivers.md):
    GET    /api/version                        reachability + PrusaLink version (often unauthenticated)
    GET    /api/v1/info                        printer info (nozzle, hostname, ...)
    GET    /api/v1/status                      printer state, temperatures, job progress
    GET    /api/v1/job                         current job (id, file name); 204 when none
    PUT    /api/v1/job/{id}/pause|resume       pause / resume
    DELETE /api/v1/job/{id}                    cancel (stop) the job
    GET    /api/v1/files/{storage}/{path}      list a folder (JSON) / file info
    GET    /api/v1/files/{storage}/{path}/raw  download a file (fallback: the plain path)
    POST   /api/v1/files/{storage}/{path}      start printing an existing file
    DELETE /api/v1/files/{storage}/{path}      delete a file
    GET    /api/v1/cameras, /api/v1/cameras/{id}/snap   local camera, if the firmware has one

Authentication: ONE secret, the "Password". It is used for HTTP Digest (user, usually "maker");
if the printer rejects that (older firmware shows an API key instead) the same value is retried
once as the `X-Api-Key` header and the working way is remembered. A separate stored `api_key`
(old saved printers / config.yml) is still honoured and sent as before. Credentials are only ever sent to the PrusaLink host
itself, never to a separately configured camera URL on another host.

Files are `.gcode` / `.bgcode` on the printer's USB stick (storage "usb").
Prusa cameras are normally cloud-only (Prusa Connect), so `has_camera` is True
only if the firmware exposes a local camera or the user configures a
`snapshot_url`.
"""

import base64
import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import http.client

from bambu import (PrinterError, PrinterBusy, make_opener, is_redirect, REDIRECT_MSG, strip_userinfo, scrub_text,
                   _require_idle_for_delete, read_capped, FILE_MAX_BYTES)   # read_capped, FILE_MAX_BYTES: rename
import controls as _controls
from controls import ControlUnsupported
from camera import PollingCameraStream, _Done

# One opener for every request: never go through an HTTP(S)_PROXY from the environment for what is,
# by definition, a LAN printer, and NEVER follow a redirect (a 3xx would turn POST into GET - a "started"
# print that never started - and forward the Digest / API-key credentials to whatever host it names).
_OPENER = make_opener()
_CAM_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # credential-free camera fetches may follow redirects
_UA = "fabrica-prusalink"

_PARAM_RE = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,"]+))')
_GCODE_EXT = (".gcode", ".bgcode", ".gco", ".g", ".bgc")   # ".bgc" = the 8.3 short name of a .bgcode on the FAT USB stick


# ---------------------------------------------------------------- Digest auth

def _parse_params(s: str) -> dict:
    return {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
            for m in _PARAM_RE.finditer(s)}


class DigestAuth:
    """Client side of RFC 7616 / 2617 HTTP Digest (MD5 and SHA-256, qop=auth, -sess).

    Unlike urllib's built-in handler this caches the server's challenge and
    pre-authenticates later requests (incrementing the nonce count), so a poll
    costs one round trip instead of two. If the nonce goes stale the 401 carries
    a fresh challenge and the caller retries once. Falls back to Basic when that
    is all the server offers. Thread-safe."""

    _HASHES = {"MD5": hashlib.md5, "SHA-256": hashlib.sha256}

    def __init__(self, username: str, password: str):
        self.username, self.password = username, password
        self._ch = None          # parsed Digest challenge
        self._basic = False
        self._nc = 0
        self._lock = threading.Lock()

    def learn(self, www_authenticate_headers) -> bool:
        """Remember the challenge(s) from a 401. True if we can answer one."""
        digest = basic = None
        for h in www_authenticate_headers or []:
            scheme, _, rest = h.strip().partition(" ")
            if scheme.lower() == "digest":
                p = _parse_params(rest)
                algo = p.get("algorithm", "MD5").upper().replace("-SESS", "")
                if "nonce" in p and algo in self._HASHES:
                    digest = p
            elif scheme.lower() == "basic":
                basic = True
        with self._lock:
            if digest:
                self._ch, self._basic, self._nc = digest, False, 0
                return True
            if basic:
                self._ch, self._basic = None, True
                return True
        return False

    def header(self, method: str, uri: str):
        """Authorization header value for this request, or None if no challenge is known yet."""
        with self._lock:
            if self._basic:
                tok = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
                return "Basic " + tok
            ch = self._ch
            if not ch:
                return None
            self._nc += 1
            nc = f"{self._nc:08x}"
        algo = ch.get("algorithm", "MD5")
        sess = algo.upper().endswith("-SESS")
        H = self._HASHES[algo.upper().replace("-SESS", "")]

        def h(s):
            return H(s.encode()).hexdigest()

        realm, nonce = ch.get("realm", ""), ch["nonce"]
        cnonce = os.urandom(8).hex()
        ha1 = h(f"{self.username}:{realm}:{self.password}")
        if sess:
            ha1 = h(f"{ha1}:{nonce}:{cnonce}")
        ha2 = h(f"{method}:{uri}")
        qops = [q.strip() for q in ch.get("qop", "").split(",") if q.strip()]
        if "auth" in qops:
            resp = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        else:
            resp = h(f"{ha1}:{nonce}:{ha2}")
        parts = [f'username="{self.username}"', f'realm="{realm}"', f'nonce="{nonce}"',
                 f'uri="{uri}"', f'response="{resp}"']
        if "algorithm" in ch:
            parts.append(f"algorithm={algo}")
        if "opaque" in ch:
            parts.append(f'opaque="{ch["opaque"]}"')
        if "auth" in qops:
            parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
        return "Digest " + ", ".join(parts)


# ---------------------------------------------------------------- the driver

def _normalize_creds(creds) -> dict:
    """creds may be None, an API-key string, or {"username","password","api_key"}."""
    if not creds:
        return {"username": "", "password": "", "api_key": ""}
    if isinstance(creds, str):
        return {"username": "", "password": "", "api_key": creds}
    return {"username": creds.get("username") or "maker",
            "password": creds.get("password") or "",
            "api_key": creds.get("api_key") or ""}


def _strip_ext(name: str) -> str:
    return re.sub(r"\.(b?gcode|gco|g)$", "", name or "", flags=re.I)


def _http_msg(e: urllib.error.HTTPError) -> str:
    """Short human reason for an HTTPError, including a JSON/text body message if present."""
    detail = ""
    try:
        body = e.read(2048).decode("utf-8", "replace").strip()
        try:
            j = json.loads(body)
            detail = (j.get("message") or j.get("error") or j.get("title") or "") if isinstance(j, dict) else ""
            if isinstance(detail, dict):
                detail = detail.get("message", "")
        except ValueError:
            detail = body[:120] if body and "<" not in body else ""
    except Exception:
        pass
    if 300 <= e.code < 400:
        return f"{e.code} {REDIRECT_MSG}"
    return f"{e.code} {e.reason}" + (f": {detail}" if detail else "")


class PrusaLinkPrinter:
    kind = "prusalink"
    # --- driver traits the server/backup can branch on instead of `kind == "octoprint"`
    uses_files_api = True          # file listing is files(), not sd_list()
    backup_mid_print_ok = False    # reads the printer's USB stick -> don't pull files mid-print
    WEBCAM_OK_TTL = 600
    WEBCAM_NONE_TTL = 60
    SNAPSHOT_TTL = 3               # seconds a status poll is reused

    CONFIG_FIELDS = [
        {"name": "name", "label": "Printer name", "type": "text", "required": True,
         "help": "Short name agents and the dashboard use for this printer, e.g. MK4. Letters, digits, "
                 "dash, underscore or dot - no spaces. It cannot be changed later."},
        {"name": "url", "label": "PrusaLink address", "type": "url", "required": True,
         "placeholder": "192.168.1.50", "guide_step": "ip",
         "help": "The printer's IP address, e.g. 192.168.1.50 (http:// is added for you). Find it on the "
                 "printer screen: Settings > Network. PrusaLink must be switched on there too. Check it by "
                 "typing the address in a browser: you should see the PrusaLink login page. Give the "
                 "printer a DHCP reservation so the address never changes."},
        {"name": "password", "label": "Password", "type": "password", "required": True,
         "secret": True, "guide_step": "creds",
         "help": "The PrusaLink password, shown on the printer: Settings > Network > PrusaLink. Case "
                 "matters. 'Generate Password' there makes a new one and the old one stops working. "
                 "Older firmware shows an API key instead of a password: paste that here, it works too."},
        {"name": "username", "label": "Username", "type": "text", "required": False, "advanced": True,
         "default": "maker", "guide_step": "creds",
         "help": "PrusaLink login, almost always 'maker' (shown on the printer: Settings > Network > "
                 "PrusaLink)."},
        {"name": "storage", "label": "Storage", "type": "text", "required": False, "default": "usb",
         "advanced": True, "guide_step": "usb",
         "help": "Which printer storage holds your G-code. 'usb' for the USB stick (almost always "
                 "right - keep a stick inserted)."},
        {"name": "model", "label": "Model", "type": "text", "required": False, "advanced": True,
         "placeholder": "Prusa MK4S", "help": "Display only."},
    ]
    # (The camera address is a per-printer field every driver gets from fleet_store.CAMERA_FIELD; the old
    # `snapshot_url` field became that, and an old stored `api_key` is now the password - see
    # FleetManager._migrate_legacy.)

    def __init__(self, name, url, creds=None, model="", storage="usb", snapshot_url=""):
        self.name, self.model = name, model
        self.url = strip_userinfo(url).rstrip("/")       # a password typed into the address is dropped (N-7)
        c = _normalize_creds(creds)
        self.username, self.password, self.api_key = c["username"], c["password"], c["api_key"]
        self.storage = (storage or "usb").strip("/")
        self.snapshot_url = snapshot_url or ""
        self._auth_mode = None           # None | "digest" | "apikey": what the password turned out to be
        self._digest = DigestAuth(self.username, self.password) if self.password else None
        self._dl_style = None            # "raw" | "plain" once we know which download URL works
        self._last = None                # last parsed status dict (or None when failing)
        self._last_ts = 0.0
        self._last_err = None
        self._ok = False
        self._snap_lock = threading.Lock()
        self._job_cache = (None, "")     # (job id, display name)
        self._cam_lock = threading.Lock()
        self._cam_guard = threading.Lock()
        self._cam_probed = 0.0
        self._cam_ok = False
        self._cam_url = None             # absolute snapshot URL when a camera was found
        self._cam_auth = False           # True when that URL is on the PrusaLink host (send creds)
        self._cam_refreshing = False
        self._camera = None
        self._finfo = {}                 # (path, m_timestamp) -> per-file details already read (size, print time, ...)
        self._finfo_lock = threading.Lock()

    # ---- construction helpers ----------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict, env=None):
        """Build from one `printers:` entry of config.yml. Secrets come from
        environment variables named by `password_env` / `api_key_env`; inline
        `password` / `api_key` values are accepted too (UI-created printers)."""
        env = os.environ if env is None else env
        if not cfg.get("url"):
            raise PrinterError(f"PrusaLink printer '{cfg.get('name', '?')}' needs a 'url'.")
        creds = {
            "username": cfg.get("username") or "maker",
            "password": env.get(cfg["password_env"], "") if cfg.get("password_env") else cfg.get("password", ""),
            "api_key": env.get(cfg["api_key_env"], "") if cfg.get("api_key_env") else cfg.get("api_key", ""),
        }
        return cls(cfg["name"], cfg["url"], creds, cfg.get("model", ""),
                   storage=cfg.get("storage", "usb"), snapshot_url=cfg.get("snapshot_url", ""))

    # ---- HTTP plumbing ------------------------------------------------------

    def _target(self, path: str) -> str:
        """Request-target (path?query) as it goes on the wire — what Digest signs."""
        u = urllib.parse.urlsplit(self.url + path)
        return u.path + (("?" + u.query) if u.query else "")

    def _open(self, method, path, body=None, headers=None, timeout=8):
        """Open a request to the printer (raises urllib errors). Caller closes the response.

        Auth: HTTP Digest with username + password. If the printer still answers 401 and the only secret we
        hold is that password, it is sent once as `X-Api-Key` instead (older firmware shows an API key where
        newer shows a password) and whichever worked is remembered. A separately stored `api_key` (older
        saved printers / config.yml) is sent as before."""
        learned = tried_key = False
        for _ in range(3):
            key_mode = self._auth_mode == "apikey" or tried_key
            h = {"User-Agent": _UA, **(headers or {})}
            if self.api_key:
                h["X-Api-Key"] = self.api_key
            elif key_mode and self.password:
                h["X-Api-Key"] = self.password
            if self._digest and not key_mode:
                a = self._digest.header(method, self._target(path))
                if a:
                    h["Authorization"] = a
            try:
                req = urllib.request.Request(self.url + path, data=body, method=method, headers=h)
            except ValueError as e:      # malformed address ("unknown url type", ...)
                raise urllib.error.URLError(f"invalid address {self.url!r}") from e
            try:
                r = _OPENER.open(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    if (self._digest and not key_mode and not learned
                            and self._digest.learn(e.headers.get_all("WWW-Authenticate"))):
                        learned = True
                        e.close()
                        continue    # retry once with the (fresh) challenge
                    if self.password and not self.api_key and not key_mode and "Authorization" in h:
                        tried_key = True
                        e.close()
                        continue    # the password is not a login: maybe it is this firmware's API key
                raise
            if self.password and not self.api_key and ("Authorization" in h or "X-Api-Key" in h):
                self._auth_mode = "apikey" if key_mode else "digest"
            return r
        raise AssertionError("unreachable")

    def _call(self, method, path, body=None, headers=None, timeout=8, what="request"):
        """Perform a request and return (status, parsed_json_or_None). Raises PrinterError."""
        try:
            with self._open(method, path, body=body, headers=headers, timeout=timeout) as r:
                raw = r.read(8 * 1024 * 1024)
                status = r.status
        except urllib.error.HTTPError as e:
            try:
                if e.code in (401, 403):
                    raise PrinterError("PrusaLink rejected the credentials (HTTP %d) — check the "
                                       "password." % e.code) from e
                raise PrinterError(f"PrusaLink refused to {what} ({_http_msg(e)}).") from e
            finally:
                e.close()
        except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException) as e:
            raise PrinterError(f"Could not reach PrusaLink at {self.url}: {scrub_text(e)}") from e
        if not 200 <= status < 300:
            raise PrinterError(f"PrusaLink did not accept the request to {what} (HTTP {status}).")
        if not raw.strip():
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, None

    def _get_json(self, path, timeout=4, what="read"):
        return self._call("GET", path, timeout=timeout, what=what)[1]

    def _file_path(self, filename: str) -> str:
        return f"/api/v1/files/{urllib.parse.quote(self.storage, safe='')}/" + \
            urllib.parse.quote(filename.lstrip("/"), safe="/")

    # ---- lifecycle ----------------------------------------------------------

    def connect(self):
        """PrusaLink is polled, not streamed; just warm the camera probe in the background."""
        threading.Thread(target=self._refresh_webcam, daemon=True).start()

    def disconnect(self):
        """Stop the shared snapshot poller (the only long-lived thing this driver owns;
        nothing else is held open). Idempotent, safe before connect(), never raises."""
        with self._cam_guard:
            cam, self._camera = self._camera, None
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass

    def refresh(self, wait=0):
        """Drop the cached status so the next snapshot() polls the printer."""
        self._last_ts = 0.0

    # ---- probe (powers "Test connection") ----------------------------------

    @classmethod
    def probe(cls, url, creds=None, timeout=5, snapshot_url="", storage="usb") -> dict:
        """Check that `url` is a reachable PrusaLink and that the credentials work.
        Never raises. -> {"ok","model","firmware","has_camera","error"}."""
        out = {"ok": False, "model": None, "firmware": None, "has_camera": False, "error": None}
        try:
            p = cls("probe", url, creds, storage=storage, snapshot_url=snapshot_url)
        except Exception as e:
            out["error"] = f"Invalid settings: {e}"
            return out
        # 1. is it PrusaLink at all? (/api/version is normally open, so also checks reachability)
        version = None
        try:
            _, version = p._call("GET", "/api/version", timeout=timeout)
        except PrinterError as e:
            if "rejected the credentials" not in str(e) and "Could not reach" in str(e):
                out["error"] = f"Could not connect to {p.url}: {str(e).split(': ', 1)[-1]}"
                return out
        # 2. credentials + status on a protected endpoint
        try:
            _, st = p._call("GET", "/api/v1/status", timeout=timeout)
        except PrinterError as e:
            msg = str(e)
            if "rejected the credentials" in msg:
                out["error"] = ("Authentication failed — wrong password. "
                                "Find it on the printer: Settings > Network > PrusaLink.")
            elif "Could not reach" in msg:
                out["error"] = f"Could not connect to {p.url}: {msg.split(': ', 1)[-1]}"
            else:
                out["error"] = f"{p.url} does not look like PrusaLink ({msg})"
            return out
        if not isinstance(st, dict) or "printer" not in st:
            out["error"] = f"{p.url} answered, but not like PrusaLink (unexpected /api/v1/status reply)."
            return out
        out["ok"] = True
        if isinstance(version, dict):
            out["firmware"] = version.get("server") or version.get("text") or version.get("original")
        try:
            info = p._get_json("/api/v1/info", timeout=timeout) or {}
            out["model"] = info.get("printer_type") or info.get("model") or None
            out["firmware"] = out["firmware"] or info.get("firmware")
        except PrinterError:
            pass
        try:
            out["has_camera"] = p._discover_camera(timeout=timeout)[0] is not None
        except Exception:
            pass
        return out

    # ---- status -------------------------------------------------------------

    # PrusaLink printer.state -> hub state.  (BUSY = homing/loading/etc.; ATTENTION =
    # needs a human, e.g. filament runout, which holds the job like a pause.)
    STATE_MAP = {
        "IDLE": "IDLE", "READY": "IDLE",
        "BUSY": "PREPARE",
        "PRINTING": "RUNNING",
        "PAUSED": "PAUSE",
        "ATTENTION": "PAUSE",       # IDLE when there is no job (see _map_state)
        "FINISHED": "FINISH",
        "STOPPED": "FAILED",        # cancelled by the user — Bambu calls this FAILED too
        "ERROR": "FAILED",
    }

    @classmethod
    def _map_state(cls, raw: str, has_job: bool) -> str:
        raw = (raw or "").upper()
        if raw == "ATTENTION" and not has_job:
            return "IDLE"
        return cls.STATE_MAP.get(raw, "UNKNOWN")

    def _job_name(self, job_id):
        """Display name of the current job, fetched once per job id."""
        if job_id is not None and self._job_cache[0] == job_id and self._job_cache[1]:
            return self._job_cache[1]
        try:
            j = self._get_json("/api/v1/job", timeout=4) or {}
        except PrinterError:
            return self._job_cache[1]
        f = j.get("file") or {}
        name = _strip_ext(f.get("display_name") or f.get("name") or "")
        if name:
            self._job_cache = (j.get("id", job_id), name)
        return name

    def _poll(self):
        """One status read -> self._last / self._ok / self._last_err."""
        try:
            st = self._get_json("/api/v1/status", timeout=4)
        except PrinterError as e:
            self._ok, self._last, self._last_err = False, None, scrub_text(e)
            return
        if not isinstance(st, dict):
            self._ok, self._last, self._last_err = False, None, "unexpected reply from /api/v1/status"
            return
        self._ok, self._last_err = True, None
        printer, job = st.get("printer") or {}, st.get("job") or {}
        raw = (printer.get("state") or "").upper()
        has_job = bool(job) and job.get("id") is not None
        state = self._map_state(raw, has_job)
        name = ""
        if state in ("RUNNING", "PAUSE", "PREPARE") and has_job:
            name = self._job_name(job.get("id"))
        elif state in ("FINISH", "FAILED"):
            name = self._job_cache[1]
        progress = job.get("progress")
        if progress is None and state == "FINISH":
            progress = 100
        left = job.get("time_remaining")
        self._last = {
            "state": state, "raw": raw, "job": name,
            "percent": round(progress) if isinstance(progress, (int, float)) else None,
            "remaining_min": round(left / 60) if isinstance(left, (int, float)) and left > 0 else None,
            "z_mm": printer.get("axis_z") if isinstance(printer.get("axis_z"), (int, float))
                    and not isinstance(printer.get("axis_z"), bool) else None,
            "nozzle_c": printer.get("temp_nozzle"), "nozzle_target_c": printer.get("target_nozzle"),
            "bed_c": printer.get("temp_bed"), "bed_target_c": printer.get("target_bed"),
            "print_error": 1 if raw in ("ERROR", "ATTENTION") else 0,
            "fault": raw == "ERROR",       # a hard fault is not "idle": no heater / filament moves, no start
        }

    @_controls.with_start_pending
    def snapshot(self) -> dict:
        if time.time() - self._last_ts > self.SNAPSHOT_TTL:
            # First-ever call blocks for the poll; later callers during a slow poll reuse the cache.
            if self._snap_lock.acquire(blocking=(self._last_ts == 0.0)):
                try:
                    if time.time() - self._last_ts > self.SNAPSHOT_TTL:
                        self._poll()
                        self._last_ts = time.time()
                finally:
                    self._snap_lock.release()
        base = {"printer": self.name, "layer": "", "sdcard": None, "kind": self.kind}
        if not self._ok or not self._last:
            return {**base, "connected": False, "state": "UNKNOWN", "job": "", "percent": None,
                    "remaining_min": None, "nozzle_c": None, "nozzle_target_c": None, "bed_c": None,
                    "bed_target_c": None, "print_error": 0, "age_seconds": None,
                    "raw_state": None, "error": self._last_err}
        s = self._last
        return {**base, "connected": True, "state": s["state"], "job": s["job"],
                "percent": s["percent"], "remaining_min": s["remaining_min"],
                "z_mm": s.get("z_mm"),
                "nozzle_c": s["nozzle_c"], "nozzle_target_c": s["nozzle_target_c"],
                "bed_c": s["bed_c"], "bed_target_c": s["bed_target_c"],
                "print_error": s["print_error"], "fault": bool(s.get("fault")),
                "age_seconds": round(time.time() - self._last_ts),
                "raw_state": s["raw"], "error": None}

    def fresh_snapshot(self) -> dict:
        """snapshot() that skips the 3 s cache (safety decisions must not act on an old reading)."""
        self._last_ts = 0.0
        return self.snapshot()

    def ams_trays(self) -> list:
        return []

    # ---- files --------------------------------------------------------------

    @staticmethod
    def _secs(meta: dict):
        """Best-effort print-time seconds from whatever metadata the listing carries."""
        v = meta.get("estimated_print_time", meta.get("estimated_printing_time"))
        if isinstance(v, (int, float)):
            return int(v)
        s = meta.get("estimated printing time (normal mode)") or meta.get("estimated_printing_time_normal_mode")
        if isinstance(s, str):
            m = re.fullmatch(r"\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*", s)
            if m and any(m.groups()):
                d, h, mi, sec = (int(x or 0) for x in m.groups())
                return d * 86400 + h * 3600 + mi * 60 + sec
        return None

    def files(self) -> list:
        """All G-code files on the configured storage (recursive), OctoPrint-shaped dicts:
        name (path relative to the storage root), size, modified (epoch s), print_seconds,
        print_time, filament_mm, origin ("local" = downloadable by this driver)."""
        out, budget = [], [60]          # at most 60 folder reads per call

        def walk(rel_folder, depth):
            path = f"/api/v1/files/{urllib.parse.quote(self.storage, safe='')}/" + \
                urllib.parse.quote(rel_folder, safe="/")
            if not path.endswith("/"):
                path += "/"
            budget[0] -= 1
            data = self._get_json(path, timeout=10, what="list files") or {}
            for it in data.get("children") or []:
                nm = it.get("name") or ""
                rel = (rel_folder + nm) if nm else ""
                if not nm:
                    continue
                kind = (it.get("type") or "").upper()
                if kind in ("FOLDER", "DIR", "DIRECTORY"):
                    if it.get("children"):       # already expanded inline
                        for c in it["children"]:
                            self._add_child(out, rel + "/", c)
                    elif depth < 4 and budget[0] > 0:
                        walk(rel + "/", depth + 1)
                else:
                    self._add_child(out, rel_folder, it)

        walk("", 0)
        return out

    # ---- per-file details ---------------------------------------------------
    # A real Core One (firmware 6.5, PrusaLink 2.1) lists files WITHOUT size or metadata: each entry is only
    # {name (8.3 short name), display_name (the long name), type, m_timestamp, ro, refs{icon, thumbnail, download}}.
    # GET /api/v1/files/<storage>/<path> adds `size` (and a `meta` object on firmware that has one). The details
    # are therefore read lazily, one small GET per file, and remembered per (path, m_timestamp) so a changed
    # file is read again. Print time and filament type are also taken from the long name when the slicer put
    # them there (PrusaSlicer's default: <name>_<nozzle>n_<layer>mm_<filament>_<printer>_<1h25m>.gcode).

    _TIME_RE = re.compile(r"_(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\.(?:b?gcode|gco|g)$", re.I)
    _TYPE_RE = re.compile(r"_\d+(?:\.\d+)?mm_([A-Za-z0-9+\-]+(?:\.[A-Za-z0-9+\-]+)*)_")
    _LAYER_RE = re.compile(r"_(\d+(?:\.\d+)?)n_(\d+(?:\.\d+)?)mm_")

    @classmethod
    def _secs_from_name(cls, display_name):
        m = cls._TIME_RE.search(display_name or "")
        if not m or not any(m.groups()):
            return None
        d, h, mi, sec = (int(x or 0) for x in m.groups())
        total = d * 86400 + h * 3600 + mi * 60 + sec
        return total or None

    @classmethod
    def _facts_from_name(cls, display_name):
        """{print_seconds, filament_type, nozzle_mm, layer_mm} that the slicer wrote into the file name (any may be missing)."""
        out = {}
        secs = cls._secs_from_name(display_name)
        if secs:
            out["print_seconds"] = secs
        m = cls._TYPE_RE.search(display_name or "")
        if m:
            mats = [x.upper() for x in m.group(1).split(".") if x]
            out["materials"], out["filament_type"] = mats, mats[0]
        m = cls._LAYER_RE.search(display_name or "")
        if m:
            out["nozzle_mm"], out["layer_mm"] = float(m.group(1)), float(m.group(2))
        return out

    @staticmethod
    def _num(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            m = re.match(r"\s*(-?\d+(?:\.\d+)?)", v)
            return float(m.group(1)) if m else None
        return None

    @classmethod
    def _facts_from_meta(cls, meta, display_name=""):
        """Details out of a PrusaLink `meta` object (all keys optional; absent on some firmware)."""
        meta = meta if isinstance(meta, dict) else {}
        out = {}
        secs = cls._secs(meta)
        if secs:
            out["print_seconds"] = secs
        mm = cls._num(meta.get("filament used [mm]", meta.get("filament_length")))
        if mm is not None:
            out["filament_mm"] = mm
        g = cls._num(meta.get("filament used [g]", meta.get("filament_weight")))
        if g is not None:
            out["weight_g"] = g
        ft = meta.get("filament_type", meta.get("filament type"))
        if isinstance(ft, str) and ft.strip():
            mats = [x.strip().upper() for x in re.split(r"[;,]", ft) if x.strip()]
            out["materials"], out["filament_type"] = mats, mats[0]
        nz = cls._num(meta.get("nozzle_diameter"))
        if nz:
            out["nozzle_mm"] = nz
        lh = cls._num(meta.get("layer_height"))
        if lh:
            out["layer_mm"] = lh
        return out

    def remember_details(self, name, rev, details):
        """Seed the in-memory details of a file (the Hub restores them from its disk cache after a restart)."""
        if isinstance(details, dict):
            with self._finfo_lock:
                self._finfo[(name, rev)] = dict(details)

    def known_details(self, name, rev):
        with self._finfo_lock:
            return dict(self._finfo.get((name, rev)) or {})

    def file_details(self, name, rev=None):
        """ONE read-only GET /api/v1/files/<storage>/<path>: size + whatever `meta` the firmware gives.
        Remembered per (name, rev). Returns the details dict ({"size", ...}); raises PrinterError."""
        data = self._get_json(self._file_path(name), timeout=8, what="read the file details")
        data = data if isinstance(data, dict) else {}
        out = {}
        size = data.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            out["size"] = size
        out.update(self._facts_from_meta(data.get("meta"), data.get("display_name") or ""))
        self.remember_details(name, rev, out)
        return out

    def thumbnail_png(self, name, ref=None, max_bytes=4 * 1024 * 1024):
        """The part picture of a file as PNG bytes (one read-only GET of the printer's own thumbnail service, never a G-code download).
        `ref` is the `refs.thumbnail` the listing gave ("/thumb/l/<storage>/<path>" - the large size; the "/thumb/s/" icon is smaller
        and not always present). Returns None when the printer has no picture for the file (404) or sends something that is not a PNG."""
        if not (isinstance(ref, str) and ref.startswith("/thumb/") and ".." not in ref and "?" not in ref and "#" not in ref):
            ref = f"/thumb/l/{urllib.parse.quote(self.storage, safe='')}/" + urllib.parse.quote(name.lstrip("/"), safe="/~")
        else:
            ref = urllib.parse.quote(urllib.parse.unquote(ref), safe="/~")
        try:
            with self._open("GET", ref, timeout=10) as r:
                data = r.read(max_bytes + 1)
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            if code in (404, 204):
                return None
            if code in (401, 403):
                raise PrinterError("PrusaLink rejected the credentials (HTTP %d) - check the password." % code) from e
            raise PrinterError(f"PrusaLink could not give the picture (HTTP {code}).") from e
        except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException) as e:
            raise PrinterError(f"Could not reach PrusaLink at {self.url}: {scrub_text(e)}") from e
        if len(data) > max_bytes or data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return data

    def _add_child(self, out, rel_folder, it):
        nm = it.get("name") or ""
        kind = (it.get("type") or "").upper()
        if kind in ("FIRMWARE", "FOLDER") or not (kind == "PRINT_FILE" or nm.lower().endswith(_GCODE_EXT)):
            return
        path = rel_folder + nm
        mts = it.get("m_timestamp", it.get("modified"))
        long_name = it.get("display_name") or nm
        meta = it.get("meta") or {}
        secs = self._secs({**it, **meta})
        facts = {**self._facts_from_name(long_name), **self.known_details(path, mts),
                 **self._facts_from_meta(meta, long_name)}
        if secs:
            facts["print_seconds"] = secs
        secs = facts.get("print_seconds")
        fil = facts.get("filament_mm", it.get("filament_length"))
        try:
            fil = float(fil) if fil is not None else None
        except (TypeError, ValueError):
            fil = None
        size = it.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            size = facts.get("size") or 0
        refs = it.get("refs") if isinstance(it.get("refs"), dict) else {}
        ref = refs.get("thumbnail") if isinstance(refs.get("thumbnail"), str) else None
        out.append({
            "name": path,
            "size": size,
            "modified": mts,
            "print_seconds": secs,
            "print_time": f"{secs // 3600}h{(secs % 3600) // 60:02d}m" if secs else None,
            "filament_mm": fil,
            "weight_g": facts.get("weight_g"),
            "filament_type": facts.get("filament_type"),
            "materials": list(facts.get("materials") or []),
            "nozzle_mm": facts.get("nozzle_mm"),
            "layer_mm": facts.get("layer_mm"),
            "origin": "local",
            "storage": self.storage,
            "display_name": long_name,
            "thumb_ref": ref,
            "rev": mts,
        })

    def _download_candidates(self, filename):
        base = self._file_path(filename)
        order = [base + "/raw", base]
        if self._dl_style == "plain":
            order.reverse()
        return order

    def open_file(self, filename: str, timeout: float = 30, headers=None):
        """Open a download of `filename` (path relative to the storage root, as returned
        by files()). Returns the HTTP response — use as a context manager and read()
        in chunks. This is the method backup.py should stream from (PrusaLink files can
        be 100+ MB; never read it all into memory). Raises PrinterError."""
        self._refuse_if_printing()
        last = None
        for path in self._download_candidates(filename):
            try:
                r = self._open("GET", path, headers=headers, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise PrinterError("PrusaLink rejected the credentials (HTTP %d)." % e.code) from e
                if is_redirect(e):
                    e.close()
                    raise PrinterError(f"PrusaLink at {self.url} answered {e.code}: {REDIRECT_MSG}.") from e
                last = f"{e.code} {e.reason}"
                e.close()
                continue
            except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException) as e:
                raise PrinterError(f"Could not reach PrusaLink at {self.url}: {scrub_text(e)}") from e
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype.startswith("application/json"):    # that was the file *info*, not the file
                r.close()
                last = "endpoint returned file info, not the file"
                continue
            self._dl_style = "raw" if path.endswith("/raw") else "plain"
            return r
        raise PrinterError(f"PrusaLink refused the file download ({last}).")

    def _refuse_if_printing(self):
        """The files are on the printer's USB stick, which the print itself is reading: no download while
        it is positively busy (RUNNING / PREPARE / PAUSE, or a start pending). Uses the <=3 s status cache."""
        s = self.snapshot()
        if (s.get("connected") and s.get("state") in _controls.BUSY_STATES) or s.get("start_pending"):
            raise PrinterBusy(f"printer is busy ({self.name}: {s.get('state')}) - the USB stick is in use, "
                              "no file was downloaded.")

    def download_head(self, filename: str, nbytes: int = 1048576) -> bytes:
        """First `nbytes` of a file (thumbnails/metadata live in the G-code header).
        Sends a Range header; if the printer ignores it only nbytes+1 are ever read."""
        with self.open_file(filename, timeout=15, headers={"Range": f"bytes=0-{nbytes - 1}"}) as r:
            return r.read(nbytes + 1)[:nbytes]

    # ==== rename ====
    def download(self, filename: str, max_bytes: int = FILE_MAX_BYTES) -> bytes:
        """The whole file (capped at `max_bytes`; PrinterError beyond it). Refused while a print is running
        (open_file's own guard: the USB stick is in use)."""
        with self.open_file(filename, timeout=60) as r:
            try:
                return read_capped(r, max_bytes)
            except (socket.timeout, OSError, http.client.HTTPException) as e:
                raise PrinterError(f"Could not read the file from PrusaLink at {self.url}: {scrub_text(e)}") from e

    def upload(self, filename: str, data, timeout: float = 300):
        """BETA - not yet verified on real hardware. Upload a file to the printer's storage:
        PUT /api/v1/files/<storage>/<path>, the file's bytes as the body (application/octet-stream), with
        `Overwrite: ?0` (an existing file is NEVER replaced; the older spelling `Overwrite-File: ?0` is sent as well)
        and `Print-After-Upload: ?0` (it never starts a print). Same Digest / API-key authentication as every other call.
        Refused unless the printer is positively idle right now. The caller makes sure the name is free."""
        _require_idle_for_delete(self, "uploaded")
        body = bytes(data)
        self._call("PUT", self._file_path(filename), body=body, timeout=timeout, what="upload the file",
                   headers={"Content-Type": "application/octet-stream", "Overwrite": "?0", "Overwrite-File": "?0",
                            "Print-After-Upload": "?0", "Content-Length": str(len(body))})
    # ==== end rename ====

    # ---- job control --------------------------------------------------------

    def start_print(self, filename, ams_mapping=None, origin="local"):
        """Start printing an existing file on the printer's storage (no upload)."""
        self._call("POST", self._file_path(filename), what="start the print")
        _controls.mark_start_sent(self)      # N-8: accepted; the auto-off waits until the print shows up
        self.refresh()

    def _job_id(self):
        j = self._get_json("/api/v1/job", timeout=5)
        if not isinstance(j, dict) or j.get("id") is None:
            raise PrinterError("PrusaLink has no active job to control.")
        return j["id"]

    def _job_action(self, method, suffix, verb):
        jid = self._job_id()
        self._call(method, f"/api/v1/job/{jid}{suffix}", body=b"" if method == "PUT" else None,
                   what=verb)
        self.refresh()

    def stop(self):
        self._job_action("DELETE", "", "cancel the job")
        _controls.clear_start_pending(self)
    def pause(self):  self._job_action("PUT", "/pause", "pause")
    def resume(self): self._job_action("PUT", "/resume", "resume")

    # ---- printer controls (see controls.py) -------------------------------

    _NO_CONTROLS = ("PrusaLink's v1 API has no light, temperature, G-code or filament endpoint "
                    "(it can only read status and run/stop jobs), so none of these controls can "
                    "be offered. Use the printer's own screen, or run OctoPrint for these.")

    def control_caps(self) -> dict:
        return _controls.all_unsupported(self._NO_CONTROLS)

    def controls(self) -> dict:
        return _controls.describe(self)

    def set_light(self, on):               raise ControlUnsupported(self._NO_CONTROLS)
    def set_temps(self, nozzle=None, bed=None): raise ControlUnsupported(self._NO_CONTROLS)
    def unload_filament(self):             raise ControlUnsupported(self._NO_CONTROLS)
    def load_filament(self, tray=None):    raise ControlUnsupported(self._NO_CONTROLS)

    def sd_delete(self, filename, origin="local"):
        _require_idle_for_delete(self)       # N-12: re-checked at the moment of the delete (USB stick)
        self._call("DELETE", self._file_path(filename), what="delete the file")
        with self._finfo_lock:
            for k in [k for k in self._finfo if k[0] == filename]:
                del self._finfo[k]

    # ---- camera -------------------------------------------------------------

    def _discover_camera(self, timeout=5):
        """-> (absolute_snapshot_url | None, send_printer_credentials: bool). Raises on transport errors."""
        if self.snapshot_url:
            url = urllib.parse.urljoin(self.url + "/", self.snapshot_url)
            return url, url.startswith(self.url + "/")
        try:
            data = self._get_json("/api/v1/cameras", timeout=timeout)
        except PrinterError:
            return None, False
        items = []
        if isinstance(data, dict):
            items = data.get("camera_list") or data.get("cameras") or []
        elif isinstance(data, list):
            items = data
        for c in items:
            if not isinstance(c, dict):
                continue
            cid = c.get("camera_id", c.get("id"))
            ok = c.get("connected", c.get("detected", True))
            if cid is not None and ok:
                return f"{self.url}/api/v1/cameras/{urllib.parse.quote(str(cid), safe='')}/snap", True
        return None, False

    def _fetch_snapshot(self, timeout=8) -> bytes:
        url = self._cam_url
        if not url:
            raise ConnectionError("no camera snapshot URL")
        if self._cam_auth:
            with self._open("GET", url[len(self.url):], timeout=timeout) as r:
                data = r.read(8 * 1024 * 1024 + 1)
        else:                                  # foreign host: never send PrusaLink credentials
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with _CAM_OPENER.open(req, timeout=timeout) as r:
                data = r.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024 or data[:2] != b"\xff\xd8":
            raise ValueError("camera snapshot is not a JPEG")
        return data

    def _probe_and_store(self):     # caller holds _cam_lock
        try:
            url, auth = self._discover_camera()
            if url:
                self._cam_url, self._cam_auth = url, auth
                self._fetch_snapshot(timeout=5)
                self._cam_ok = True
            else:
                self._cam_ok, self._cam_url = False, None
        except Exception:
            if not self._cam_ok:               # keep the last good answer on a transient error
                self._cam_url = None
        self._cam_probed = time.time()
        self._cam_refreshing = False

    def _refresh_webcam(self):
        with self._cam_lock:
            self._probe_and_store()

    @property
    def has_camera(self) -> bool:
        """True when a snapshot source exists AND a test frame was fetched. Cached;
        re-checked in the background (same behaviour as OctoPrintPrinter)."""
        if not self._cam_probed:
            with self._cam_lock:
                if not self._cam_probed:
                    self._probe_and_store()
        else:
            ttl = self.WEBCAM_OK_TTL if self._cam_ok else self.WEBCAM_NONE_TTL
            if time.time() - self._cam_probed > ttl and not self._cam_refreshing:
                self._cam_refreshing = True
                threading.Thread(target=self._refresh_webcam, daemon=True).start()
        return self._cam_ok

    def camera_info(self):
        """Orientation flags for the dashboard (None when there is no camera)."""
        if not self._cam_ok:
            return None
        return {"flip_h": False, "flip_v": False, "rotate90": False}

    def camera(self) -> PollingCameraStream:
        """The printer's single shared snapshot poller (thread starts on demand)."""
        with self._cam_guard:
            if self._camera is None:
                self._camera = _FetchPoller(self._fetch_snapshot, label=self.name)
            return self._camera


class _FetchPoller(PollingCameraStream):
    """PollingCameraStream whose frames come from a callable (so the fetch can carry
    Digest auth, which a bare URL cannot). Reuses the base class's demand/idle/
    backoff machinery unchanged."""

    def __init__(self, fetch_fn, label="camera", **kw):
        super().__init__(lambda: "unused", label=label, **kw)
        self._fetch_fn = fetch_fn

    def _session(self):
        while True:
            if self._idle_or_stopped():
                raise _Done()
            self._store_frame(self._fetch_fn())
            self.fetches += 1
            self.connected = True
            end = time.monotonic() + self.interval
            while time.monotonic() < end:
                if self._idle_or_stopped():
                    raise _Done()
                time.sleep(0.1)
