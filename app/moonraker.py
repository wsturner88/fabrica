"""Klipper / Moonraker driver for the Print Hub (Voron, Ender-with-Klipper, Creality K1, Sovol, ...).

Moonraker is the HTTP/JSON API server that sits in front of Klipper. This driver
mirrors the public surface of `OctoPrintPrinter` in bambu.py so server.py /
backup.py can treat it the same way (see docs/drivers.md for the wiring recipe).

Endpoints used (Moonraker web API; see "uncertain" notes in docs/drivers.md):
    GET    /server/info                          Moonraker + Klipper connection state, versions
    GET    /printer/info                         Klipper version / state message
    GET    /printer/objects/query?print_stats&virtual_sdcard&display_status
                                  &extruder&heater_bed&webhooks    live status (one request)
    GET    /server/files/list?root=gcodes        every G-code file (recursive, flat)
    GET    /server/files/metadata?filename=      slicer metadata (estimated_time, filament_total, ...)
    GET    /server/files/gcodes/<path>           download a file (Range supported)
    DELETE /server/files/gcodes/<path>           delete a file
    POST   /printer/print/start?filename=        start printing an existing file
    POST   /printer/print/pause|resume|cancel    job control
    POST   /printer/firmware_restart             bring Klipper back after a shutdown/error
    GET    /server/webcams/list                  configured webcams (snapshot_url / stream_url)

Authentication: normally none on a trusted LAN (Moonraker `trusted_clients`).
If your Moonraker needs one, give an API key; it is sent as `X-Api-Key`.

Webcams: Mainsail/Fluidd configure them in Moonraker, usually with RELATIVE urls
such as `/webcam/?action=snapshot` served by nginx on port 80 — NOT on Moonraker's
own port 7125. Relative URLs are therefore resolved against the printer's host
(dropping port 7125; keeping any other port, so a reverse proxy works).
"""

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

from bambu import PrinterError, make_opener, REDIRECT_MSG, strip_userinfo, scrub_text, _require_idle_for_delete
import controls as _controls
from controls import ControlUnsupported
from camera import PollingCameraStream

_CAM_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # credential-free camera fetches may follow redirects
_OPENER = make_opener()     # LAN printer: no env proxy, and a redirect is an error, never followed
_UA = "fabrica-moonraker"
_GCODE_EXT = (".gcode", ".gco", ".g", ".bgcode")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
MOONRAKER_PORT = 7125

_LIGHT_NAME = re.compile(r"light|led|lamp", re.I)      # also covers "caselight"
_LIGHT_KINDS = ("output_pin", "led", "neopixel", "dotstar")
_OBJECTS = "print_stats&virtual_sdcard&display_status&extruder&heater_bed&webhooks"


def _perr(msg: str, status=None) -> PrinterError:
    e = PrinterError(msg)
    e.status = status          # HTTP status when there was one (None for transport errors)
    return e


def _http_msg(e: urllib.error.HTTPError) -> str:
    """Reason for an HTTPError incl. Moonraker's {"error": {"message": ...}} body."""
    detail = ""
    try:
        body = e.read(2048).decode("utf-8", "replace").strip()
        try:
            j = json.loads(body)
            err = j.get("error") if isinstance(j, dict) else None
            detail = err.get("message", "") if isinstance(err, dict) else (err or "")
        except ValueError:
            detail = body[:120] if body and "<" not in body else ""
    except Exception:
        pass
    if 300 <= e.code < 400:
        return f"{e.code} {REDIRECT_MSG}"
    return f"{e.code} {e.reason}" + (f": {detail}" if detail else "")


def _normalize_creds(creds) -> str:
    if not creds:
        return ""
    if isinstance(creds, str):
        return creds
    return creds.get("api_key") or ""


def _strip_ext(name: str) -> str:
    return re.sub(r"\.(b?gcode|gco|g)$", "", name or "", flags=re.I)


class MoonrakerPrinter:
    kind = "moonraker"
    # --- driver traits the server/backup can branch on instead of `kind == "octoprint"`
    uses_files_api = True          # file listing is files(), not sd_list()
    backup_mid_print_ok = True     # files live on the Klipper host's disk, not on the mcu
    WEBCAM_OK_TTL = 600
    WEBCAM_NONE_TTL = 60
    SNAPSHOT_TTL = 3
    META_LOOKUPS_PER_LISTING = 100

    CONFIG_FIELDS = [
        {"name": "name", "label": "Printer name", "type": "text", "required": True,
         "help": "Short name agents and the dashboard use for this printer, e.g. Voron. Letters, digits, "
                 "dash, underscore or dot - no spaces. It cannot be changed later."},
        {"name": "url", "label": "Moonraker address", "type": "url", "required": True,
         "placeholder": "http://192.168.1.60", "guide_step": "address",
         "help": "Address of the Klipper host (Raspberry Pi etc.) - the one you type in a browser to open "
                 "Mainsail or Fluidd. Moonraker's port 7125 is used automatically; plain "
                 "http://<address> is enough."},
        {"name": "api_key", "label": "API key", "type": "password", "required": False,
         "secret": True, "guide_step": "trusted",
         "help": "Only if Moonraker asks for one. Most home setups do not: Moonraker trusts the local "
                 "network if it is listed under [authorization] trusted_clients in moonraker.conf. "
                 "Leave empty unless the test says a key is needed."},
        {"name": "camera_base_url", "label": "Web UI / camera base address", "type": "url",
         "required": False, "placeholder": "http://192.168.1.60", "guide_step": "camera",
         "help": "Optional. Where relative webcam links (like /webcam/?action=snapshot) are served "
                 "from, if not the same host on port 80. The webcam itself comes from the Mainsail or "
                 "Fluidd settings automatically."},
        {"name": "model", "label": "Model", "type": "text", "required": False,
         "placeholder": "Voron 2.4", "help": "Display only."},
    ]
    CONFIG_REQUIRE_ONE_OF = []

    def __init__(self, name, url, creds=None, model="", camera_base_url=""):
        self.name, self.model = name, model
        self.url = strip_userinfo(url).rstrip("/")       # a password typed into the address is dropped (N-7)
        self.api_key = _normalize_creds(creds)
        self.camera_base_url = (camera_base_url or "").rstrip("/")
        self._last = None
        self._last_ts = 0.0
        self._last_err = None
        self._ok = False
        self._snap_lock = threading.Lock()
        self._meta = {}                    # (name, size, modified) -> metadata dict
        self._cam_lock = threading.Lock()
        self._cam_guard = threading.Lock()
        self._cam_probed = 0.0
        self._cam_ok = False
        self._cam_info = None              # {"snapshot": abs url, "stream": ..., flip/rotate flags}
        self._cam_refreshing = False
        self._camera = None
        self._caps_cache = None            # (timestamp, {lights, macros, extruder, bed})

    # ---- construction helpers ----------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict, env=None):
        """Build from one `printers:` entry of config.yml. The API key (if any) comes from
        the env var named by `api_key_env`; an inline `api_key` is accepted too."""
        env = os.environ if env is None else env
        if not cfg.get("url"):
            raise PrinterError(f"Moonraker printer '{cfg.get('name', '?')}' needs a 'url'.")
        key = env.get(cfg["api_key_env"], "") if cfg.get("api_key_env") else cfg.get("api_key", "")
        return cls(cfg["name"], cfg["url"], key, cfg.get("model", ""),
                   camera_base_url=cfg.get("camera_base_url", ""))

    # ---- HTTP plumbing ------------------------------------------------------

    def _open(self, method, path, headers=None, timeout=8):
        h = {"User-Agent": _UA, **(headers or {})}
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        try:
            req = urllib.request.Request(self.url + path, method=method, headers=h,
                                         data=b"" if method in ("POST", "PUT") else None)
        except ValueError as e:
            raise urllib.error.URLError(f"invalid address {self.url!r}") from e
        return _OPENER.open(req, timeout=timeout)

    def _call(self, method, path, headers=None, timeout=8, what="request"):
        """Perform a request, return the unwrapped Moonraker `result` (or None). Raises PrinterError."""
        try:
            with self._open(method, path, headers=headers, timeout=timeout) as r:
                raw = r.read(16 * 1024 * 1024)
                status = r.status
        except urllib.error.HTTPError as e:
            try:
                if e.code in (401, 403):
                    raise _perr("Moonraker requires authentication (HTTP %d) — set a valid API key, "
                                "or add this machine to Moonraker's trusted_clients." % e.code,
                                e.code) from e
                raise _perr(f"Moonraker refused to {what} ({_http_msg(e)}).", e.code) from e
            finally:
                e.close()
        except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException) as e:
            raise _perr(f"Could not reach Moonraker at {self.url}: {scrub_text(e)}") from e
        if not 200 <= status < 300:
            raise _perr(f"Moonraker did not accept the request to {what} (HTTP {status}).", status)
        if not raw.strip():
            return None
        try:
            j = json.loads(raw)
        except ValueError:
            raise _perr(f"{self.url} did not answer like Moonraker (not JSON).") from None
        return j.get("result", j) if isinstance(j, dict) else j

    def _file_path(self, filename: str) -> str:
        return "/server/files/gcodes/" + urllib.parse.quote(filename.lstrip("/"), safe="/")

    # ---- lifecycle ----------------------------------------------------------

    def connect(self):
        """Moonraker is polled, not streamed; warm the webcam probe in the background."""
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
        self._last_ts = 0.0

    def connect_serial(self):
        """Parity with OctoPrintPrinter.connect_serial: ask Klipper to FIRMWARE_RESTART, which
        is what brings a printer back from 'shutdown'/'error' or a lost MCU connection."""
        self._call("POST", "/printer/firmware_restart", what="restart the firmware", timeout=15)
        self.refresh()
        return f"firmware restart requested for {self.name}"

    # ---- probe (powers "Test connection") ----------------------------------

    @classmethod
    def probe(cls, url, creds=None, timeout=5) -> dict:
        """Check `url` is a reachable Moonraker. Never raises.
        -> {"ok","model","firmware","has_camera","error"} (+ "warning" when Klipper is not ready)."""
        out = {"ok": False, "model": None, "firmware": None, "has_camera": False,
               "error": None, "warning": None}
        try:
            p = cls("probe", url, creds)
            info = p._call("GET", "/server/info", timeout=timeout)
        except PrinterError as e:
            msg = str(e)
            if getattr(e, "status", None) in (401, 403):
                out["error"] = ("Moonraker wants an API key (or this machine is not in its "
                                "trusted_clients). Enter the key and test again.")
            elif "Could not reach" in msg:
                out["error"] = f"Could not connect to {url}: {msg.split(': ', 1)[-1]}"
            elif getattr(e, "status", None) == 404 or "not like Moonraker" in msg or "not JSON" in msg:
                out["error"] = f"{url} does not look like Moonraker ({msg})"
            else:
                out["error"] = msg
            return out
        except Exception as e:
            out["error"] = f"Invalid settings: {e}"
            return out
        if not isinstance(info, dict) or "klippy_state" not in info:
            out["error"] = f"{url} answered, but not like Moonraker (unexpected /server/info reply)."
            return out
        out["ok"] = True
        out["firmware"] = f"Moonraker {info['moonraker_version']}" if info.get("moonraker_version") else None
        if info.get("klippy_state") != "ready":
            out["warning"] = (f"Moonraker is reachable but Klipper is '{info.get('klippy_state')}'"
                              " — the printer will show as not connected until it is restarted.")
        else:
            try:
                pi = p._call("GET", "/printer/info", timeout=timeout) or {}
                if pi.get("software_version"):
                    out["firmware"] = f"Klipper {pi['software_version']} / " + (out["firmware"] or "Moonraker")
            except PrinterError:
                pass
        # Klipper has no notion of "model"; the user supplies it (CONFIG_FIELDS "model").
        try:
            out["has_camera"] = p._discover_camera(timeout=timeout) is not None
        except Exception:
            pass
        return out

    # ---- status -------------------------------------------------------------

    # print_stats.state -> hub state
    STATE_MAP = {
        "standby": "IDLE",
        "printing": "RUNNING",
        "paused": "PAUSE",
        "complete": "FINISH",
        "cancelled": "FAILED",     # user cancel — Bambu calls this FAILED too
        "error": "FAILED",
    }

    @staticmethod
    def _r1(v):
        return round(v, 1) if isinstance(v, (int, float)) else None

    def _poll(self):
        try:
            res = self._call("GET", "/printer/objects/query?" + _OBJECTS, timeout=4, what="read status")
        except PrinterError as e:
            if getattr(e, "status", None) in (500, 503):
                return self._poll_klippy_down()
            self._ok, self._last, self._last_err = False, None, scrub_text(e)
            return
        status = (res or {}).get("status") if isinstance(res, dict) else None
        if not isinstance(status, dict):
            self._ok, self._last, self._last_err = False, None, "unexpected reply from /printer/objects/query"
            return
        self._ok, self._last_err = True, None
        ps, vs = status.get("print_stats") or {}, status.get("virtual_sdcard") or {}
        ds, ex = status.get("display_status") or {}, status.get("extruder") or {}
        bed, wh = status.get("heater_bed") or {}, status.get("webhooks") or {}
        if wh.get("state") not in (None, "ready"):
            return self._set_klippy_down(wh.get("state"), wh.get("state_message"))
        raw = (ps.get("state") or "").lower()
        state = self.STATE_MAP.get(raw, "UNKNOWN")
        progress = vs.get("progress")
        if not progress and ds.get("progress"):
            progress = ds.get("progress")           # M73-driven slicers
        pct = round(progress * 100) if isinstance(progress, (int, float)) else None
        if state == "FINISH":
            pct = 100
        left = None
        dur = ps.get("print_duration")
        if state in ("RUNNING", "PAUSE") and isinstance(progress, (int, float)) and progress >= 0.01 \
                and isinstance(dur, (int, float)):
            left = round(dur * (1 - progress) / progress / 60)
        info = ps.get("info") or {}
        cur, tot = info.get("current_layer"), info.get("total_layer")
        layer = f"{cur if cur is not None else '?'}/{tot}" if tot else ""
        name = "" if state in ("IDLE", "UNKNOWN") else _strip_ext(ps.get("filename") or "")
        self._last = {
            "state": state, "raw": raw, "job": name, "percent": pct, "remaining_min": left,
            "layer": layer,
            "nozzle_c": self._r1(ex.get("temperature")), "nozzle_target_c": self._r1(ex.get("target")),
            "bed_c": self._r1(bed.get("temperature")), "bed_target_c": self._r1(bed.get("target")),
            "print_error": 1 if raw == "error" else 0,
            "fault": raw == "error",        # a hard fault is not "idle": no heater / filament moves, no start
            "message": ps.get("message") if raw == "error" else None,
        }

    def _poll_klippy_down(self):
        """The objects query failed with 5xx: Moonraker is up but Klipper isn't ready."""
        try:
            info = self._call("GET", "/server/info", timeout=4) or {}
        except PrinterError as e:
            self._ok, self._last, self._last_err = False, None, scrub_text(e)
            return
        self._ok, self._last_err = True, None
        self._set_klippy_down(info.get("klippy_state") or "disconnected")

    def _set_klippy_down(self, klippy_state, message=None):
        self._last = {
            "state": "DISCONNECTED", "raw": f"klippy:{klippy_state}", "job": "", "percent": None,
            "remaining_min": None, "layer": "", "nozzle_c": None, "nozzle_target_c": None,
            "bed_c": None, "bed_target_c": None,
            "print_error": 1 if klippy_state in ("shutdown", "error") else 0,
            "message": message or f"Klipper is {klippy_state}",
        }

    @_controls.with_start_pending
    def snapshot(self) -> dict:
        if time.time() - self._last_ts > self.SNAPSHOT_TTL:
            if self._snap_lock.acquire(blocking=(self._last_ts == 0.0)):
                try:
                    if time.time() - self._last_ts > self.SNAPSHOT_TTL:
                        self._poll()
                        self._last_ts = time.time()
                finally:
                    self._snap_lock.release()
        base = {"printer": self.name, "sdcard": None, "kind": self.kind}
        if not self._ok or not self._last:
            return {**base, "connected": False, "state": "UNKNOWN", "job": "", "percent": None,
                    "remaining_min": None, "layer": "", "nozzle_c": None, "nozzle_target_c": None,
                    "bed_c": None, "bed_target_c": None, "print_error": 0, "age_seconds": None,
                    "raw_state": None, "error": self._last_err}
        s = self._last
        return {**base, "connected": True, "state": s["state"], "job": s["job"],
                "percent": s["percent"], "remaining_min": s["remaining_min"], "layer": s["layer"],
                "nozzle_c": s["nozzle_c"], "nozzle_target_c": s["nozzle_target_c"],
                "bed_c": s["bed_c"], "bed_target_c": s["bed_target_c"],
                "print_error": s["print_error"], "fault": bool(s.get("fault")),
                "age_seconds": round(time.time() - self._last_ts),
                "raw_state": s["raw"], "error": s.get("message")}

    def fresh_snapshot(self) -> dict:
        """snapshot() that skips the 3 s cache (safety decisions must not act on an old reading)."""
        self._last_ts = 0.0
        return self.snapshot()

    def ams_trays(self) -> list:
        return []

    # ---- files --------------------------------------------------------------

    def _file_meta(self, name, size, modified):
        key = (name, size, modified)
        if key in self._meta:
            return self._meta[key]
        res = self._call("GET", "/server/files/metadata?" + urllib.parse.urlencode({"filename": name}),
                         timeout=5, what="read file metadata")
        meta = res if isinstance(res, dict) else {}
        if len(self._meta) >= 2000:
            self._meta.clear()         # bounded: old (name, size, modified) keys never accumulate forever
        self._meta[key] = meta
        return meta

    def files(self, with_meta: bool = True) -> list:
        """All G-code files under the `gcodes` root (recursive), OctoPrint-shaped dicts:
        name (path relative to gcodes/), size, modified (epoch s), print_seconds, print_time,
        filament_mm, origin ("local" = downloadable by this driver). Slicer metadata is fetched
        per file (cached) — at most META_LOOKUPS_PER_LISTING new lookups per call, so a first
        listing of a huge library fills in over a few calls rather than stalling."""
        res = self._call("GET", "/server/files/list?root=gcodes", timeout=10, what="list files")
        rows = res if isinstance(res, list) else []
        out, budget = [], self.META_LOOKUPS_PER_LISTING
        for it in rows:
            nm = it.get("path") or it.get("filename") or ""
            if not nm.lower().endswith(_GCODE_EXT):
                continue
            size, mod = it.get("size", 0), it.get("modified")
            meta = {}
            if with_meta:
                key = (nm, size, mod)
                if key in self._meta or budget > 0:
                    if key not in self._meta:
                        budget -= 1
                    try:
                        meta = self._file_meta(nm, size, mod)
                    except PrinterError:
                        meta = {}
            secs = meta.get("estimated_time")
            secs = int(secs) if isinstance(secs, (int, float)) and secs > 0 else None
            fil = meta.get("filament_total")
            out.append({
                "name": nm, "size": size, "modified": mod,
                "print_seconds": secs,
                "print_time": f"{secs // 3600}h{(secs % 3600) // 60:02d}m" if secs else None,
                "filament_mm": float(fil) if isinstance(fil, (int, float)) else None,
                "origin": "local",
            })
        return out

    def open_file(self, filename: str, timeout: float = 30, headers=None):
        """Open a download of `filename` (path relative to gcodes/, as returned by files()).
        Returns the HTTP response — use as a context manager and read() in chunks. This is the
        method backup.py should stream from. Raises PrinterError."""
        try:
            return self._open("GET", self._file_path(filename), headers=headers, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                if e.code in (401, 403):
                    raise _perr("Moonraker requires authentication (HTTP %d)." % e.code, e.code) from e
                raise _perr(f"Moonraker refused the file download ({_http_msg(e)}).", e.code) from e
            finally:
                e.close()
        except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException) as e:
            raise _perr(f"Could not reach Moonraker at {self.url}: {scrub_text(e)}") from e

    def download_head(self, filename: str, nbytes: int = 1048576) -> bytes:
        """First `nbytes` of a file (thumbnails live in the G-code header). Uses Range; if the
        server ignores it, only nbytes+1 are ever read."""
        with self.open_file(filename, timeout=15, headers={"Range": f"bytes=0-{nbytes - 1}"}) as r:
            return r.read(nbytes + 1)[:nbytes]

    # ---- job control --------------------------------------------------------

    def start_print(self, filename, ams_mapping=None, origin="local"):
        q = urllib.parse.urlencode({"filename": filename.lstrip("/")})
        self._call("POST", "/printer/print/start?" + q, what="start the print", timeout=10)
        _controls.mark_start_sent(self)      # N-8: accepted; the auto-off waits until the print shows up
        self.refresh()

    def _job(self, verb):
        self._call("POST", f"/printer/print/{verb}", what=verb, timeout=10)
        self.refresh()

    def stop(self):
        self._job("cancel")
        _controls.clear_start_pending(self)
    def pause(self):  self._job("pause")
    def resume(self): self._job("resume")

    # ---- printer controls (see controls.py) -------------------------------

    CAPS_TTL = 300                 # the object list only changes when Klipper's config does

    def _discover(self):
        """What this Klipper has, from GET /printer/objects/list (cached CAPS_TTL s)."""
        c = self._caps_cache
        if c and time.time() - c[0] < self.CAPS_TTL:
            return c[1]
        res = self._call("GET", "/printer/objects/list", timeout=4, what="list Klipper objects") or {}
        objs = res.get("objects") if isinstance(res, dict) else None
        if not isinstance(objs, list):
            raise _perr("unexpected reply from /printer/objects/list")
        lights, macros = [], {}
        for o in objs:
            kind, _, name = str(o).partition(" ")
            if kind in _LIGHT_KINDS and name and _LIGHT_NAME.search(name):
                lights.append((kind, name))
            elif kind == "gcode_macro" and name:
                macros[name.lower()] = name
        info = {"lights": lights, "macros": macros,
                "extruder": "extruder" in objs, "bed": "heater_bed" in objs}
        self._caps_cache = (time.time(), info)
        return info

    def _light_state(self, lights):
        """'on' / 'off' from the lights' own status objects, None if unreadable."""
        q = "&".join(urllib.parse.quote(f"{k} {n}") for k, n in lights)
        try:
            st = (self._call("GET", "/printer/objects/query?" + q, timeout=4, what="read the lights") or {}
                  ).get("status") or {}
        except PrinterError:
            return None
        known, on = False, False
        for k, n in lights:
            o = st.get(f"{k} {n}")
            if not isinstance(o, dict):
                continue
            if k == "output_pin" and isinstance(o.get("value"), (int, float)):
                known, on = True, on or o["value"] > 0
            elif isinstance(o.get("color_data"), list):
                known = True
                on = on or any(isinstance(px, (list, tuple)) and any(v and v > 0 for v in px)
                               for px in o["color_data"])
        return ("on" if on else "off") if known else None

    def control_caps(self) -> dict:
        down = None
        if not self._last_ts:
            self.snapshot()                  # never polled yet: learn whether Klipper is reachable
        if not self._ok:
            down = f"{self.name} is not reachable ({self._last_err or 'no answer yet'})."
        else:
            try:
                info = self._discover()
            except PrinterError as e:
                down = f"Klipper's object list is not available ({e})."
        if down:
            return _controls.all_unsupported(down)
        lights = info["lights"]
        macro = lambda n: info["macros"].get(n.lower())     # noqa: E731
        un, ld = macro("UNLOAD_FILAMENT"), macro("LOAD_FILAMENT")
        none_macro = "needs a `[gcode_macro %s]` in printer.cfg; this Klipper has none."
        return {
            "light": ({"supported": True, "state": self._light_state(lights), "verified": False,
                       "note": "drives " + ", ".join(f"{k} {n}" for k, n in lights)} if lights else
                      {"supported": False, "state": None,
                       "reason": "No light found: Klipper has no output_pin / led / neopixel / dotstar "
                                 "whose name contains light, led or lamp."}),
            "heat": ({"supported": True, "note": "M104 / M140 via /printer/gcode/script"}
                     if info["extruder"] else
                     {"supported": False, "reason": "Klipper reports no [extruder]."}),
            "unload": ({"supported": True, "verified": False, "note": f"runs your {un} macro"} if un
                       else {"supported": False, "reason": none_macro % "UNLOAD_FILAMENT"}),
            "load": ({"supported": True, "verified": False, "note": f"runs your {ld} macro"} if ld
                     else {"supported": False, "reason": none_macro % "LOAD_FILAMENT"}),
        }

    def controls(self) -> dict:
        return _controls.describe(self)

    def _script(self, script, what, timeout=15):
        q = urllib.parse.urlencode({"script": script}, quote_via=urllib.parse.quote)
        self._call("POST", "/printer/gcode/script?" + q, what=what, timeout=timeout)
        self.refresh()

    def set_light(self, on: bool):
        lights = self._discover()["lights"]
        if not lights:
            raise ControlUnsupported("No light found in Klipper's objects.")
        v = 1 if on else 0
        cmds = [f"SET_PIN PIN={n} VALUE={v}" if k == "output_pin"
                else f"SET_LED LED={n} RED={v} GREEN={v} BLUE={v} WHITE={v}" for k, n in lights]
        self._script("\n".join(cmds), "switch the light")

    def set_temps(self, nozzle=None, bed=None):
        info = self._discover()
        cmds, applied = [], {"nozzle": None, "bed": None}
        if nozzle is not None and info["extruder"]:
            cmds.append(f"M104 S{int(nozzle)}")
            applied["nozzle"] = int(nozzle)
        if bed is not None and info["bed"]:       # a printer without a heated bed just skips it
            cmds.append(f"M140 S{int(bed)}")
            applied["bed"] = int(bed)
        if cmds:
            self._script("\n".join(cmds), "set the temperatures")
        return applied

    def _macro(self, key, what):
        name = self._discover()["macros"].get(key)
        if not name:
            raise ControlUnsupported(f"This Klipper has no {key.upper()} macro.")
        try:
            # a filament macro heats and waits, so the request can outlive a normal timeout
            self._script(name, what, timeout=120)
        except PrinterError as e:
            if getattr(e, "status", None) is None and "timed out" in str(e):
                return          # still running on the printer: accepted
            raise

    def unload_filament(self): self._macro("unload_filament", "unload the filament")
    def load_filament(self, tray=None): self._macro("load_filament", "load the filament")

    def sd_delete(self, filename, origin="local"):
        _require_idle_for_delete(self)       # N-12: re-checked at the moment of the delete
        self._call("DELETE", self._file_path(filename), what="delete the file")
        self._meta = {k: v for k, v in self._meta.items() if k[0] != filename}

    # ---- camera -------------------------------------------------------------

    def _web_base(self) -> str:
        """Where relative webcam URLs live: the printer's host on its web port (not 7125)."""
        if self.camera_base_url:
            return self.camera_base_url
        u = urllib.parse.urlsplit(self.url)
        host = u.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if u.port in (None, MOONRAKER_PORT):
            return f"{u.scheme}://{host}"
        return f"{u.scheme}://{u.netloc}"

    def _abs_url(self, u: str) -> str:
        """Make a webcam URL from Moonraker's config usable from here."""
        if not u:
            return ""
        base = self._web_base()
        full = urllib.parse.urljoin(base + "/", u)
        parts = urllib.parse.urlsplit(full)
        mrk_host = (urllib.parse.urlsplit(self.url).hostname or "").lower()
        if (parts.hostname or "").lower() in _LOCAL_HOSTS and mrk_host not in _LOCAL_HOSTS:
            # "localhost" in the webcam config is the printer host itself, not us.
            h = urllib.parse.urlsplit(base).hostname or mrk_host
            h = f"[{h}]" if ":" in h else h
            netloc = h + (f":{parts.port}" if parts.port else "")
            full = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        return full

    @staticmethod
    def _orientation(flip_h, flip_v, rotation) -> dict:
        """Moonraker (flip + clockwise rotation 0/90/180/270) -> the dashboard's OctoPrint-style
        flags (flip_h, flip_v, rotate90 = 90 deg counter-clockwise)."""
        fh, fv, r90 = bool(flip_h), bool(flip_v), False
        try:
            rot = int(rotation or 0) % 360
        except (TypeError, ValueError):
            rot = 0
        if rot == 180:
            fh, fv = not fh, not fv
        elif rot == 90:             # 90 cw == 270 ccw == rotate90 + 180
            r90, fh, fv = True, not fh, not fv
        elif rot == 270:            # 270 cw == 90 ccw
            r90 = True
        return {"flip_h": fh, "flip_v": fv, "rotate90": r90}

    def _discover_camera(self, timeout=5):
        """-> {"snapshot": abs url, "stream": abs url, "flip_h","flip_v","rotate90"} or None.
        Raises PrinterError on transport errors."""
        cams = []
        try:
            res = self._call("GET", "/server/webcams/list", timeout=timeout)
            cams = [c for c in (res or {}).get("webcams", []) if isinstance(c, dict)]
        except PrinterError as e:
            if getattr(e, "status", None) != 404:
                raise
            # Older Moonraker: Mainsail/Fluidd kept webcams in the database.
            try:
                res = self._call("GET", "/server/database/item?namespace=webcams", timeout=timeout)
                val = (res or {}).get("value") or {}
                for c in (val.values() if isinstance(val, dict) else []):
                    if isinstance(c, dict):
                        cams.append({"enabled": c.get("enabled", True),
                                     "snapshot_url": c.get("urlSnapshot"), "stream_url": c.get("urlStream"),
                                     "flip_horizontal": c.get("flipX"), "flip_vertical": c.get("flipY"),
                                     "rotation": c.get("rotation", 0)})
            except PrinterError:
                return None
        for c in cams:
            if c.get("enabled") is False:
                continue
            snap = c.get("snapshot_url") or ""
            stream = c.get("stream_url") or ""
            if not snap and "action=stream" in stream:
                snap = stream.replace("action=stream", "action=snapshot")
            if not snap:
                continue
            o = self._orientation(c.get("flip_horizontal"), c.get("flip_vertical"), c.get("rotation"))
            return {"snapshot": self._abs_url(snap), "stream": self._abs_url(stream), **o}
        return None

    def _fetch_snapshot(self, timeout=8) -> bytes:
        url = (self._cam_info or {}).get("snapshot")
        if not url:
            raise ConnectionError("no webcam snapshot URL")
        # Webcam servers are separate from Moonraker and normally open on the LAN:
        # the API key is deliberately NOT sent to them.
        with _CAM_OPENER.open(urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=timeout) as r:
            data = r.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024 or data[:2] != b"\xff\xd8":
            raise ValueError("webcam snapshot is not a JPEG")
        return data

    def _probe_and_store(self):     # caller holds _cam_lock
        try:
            info = self._discover_camera()
            if info:
                self._cam_info = info
                self._fetch_snapshot(timeout=5)
                self._cam_ok = True
            else:
                self._cam_ok, self._cam_info = False, None
        except Exception:
            if not self._cam_ok:               # keep the last good answer on a transient error
                self._cam_info = None
        self._cam_probed = time.time()
        self._cam_refreshing = False

    def _refresh_webcam(self):
        with self._cam_lock:
            self._probe_and_store()

    @property
    def has_camera(self) -> bool:
        """True when Moonraker lists a usable webcam AND a test frame was fetched. Cached;
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
        info = self._cam_info
        if not (self._cam_ok and info):
            return None
        return {"flip_h": info["flip_h"], "flip_v": info["flip_v"], "rotate90": info["rotate90"]}

    def camera(self) -> PollingCameraStream:
        """The printer's single shared snapshot poller (thread starts on demand)."""
        with self._cam_guard:
            if self._camera is None:
                self._camera = PollingCameraStream(
                    lambda: (self._cam_info or {}).get("snapshot"), label=self.name)
            return self._camera
