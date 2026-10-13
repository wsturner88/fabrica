"""Fabrica — MCP server exposing a Bambu Lab printer fleet to any MCP agent.

Design rules (non-negotiable):
  * COLOR IS ALWAYS TEXT. Colorblind-safe by design — every tool answers with
    color NAMES ("GREEN", "ORANGE"), never bare hex or hue-only signals.
  * start_print is gated: the caller must pass the exact confirm string that
    suggest_mapping / preflight returned. An agent cannot start a job it has
    not accurately described to the human first.
  * One long-lived MQTT session per printer (P1 ESP32 broker hates churn).
"""

import asyncio
import base64
import functools
import hashlib
import hmac
import inspect
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit
import urllib.parse  # library

import yaml
from mcp.server.fastmcp import Context, FastMCP
try:
    from mcp.server.fastmcp import Image as McpImage
except ImportError:  # very old mcp releases: fall back to base64 text for snapshots
    McpImage = None

from bambu import (BambuPrinter, OctoPrintPrinter, PrinterError, extract_gcode_png,
                   extract_plate_png, parse_3mf_meta)
import fleet_store
import modelcheck
import controls
import settings_store
import notify
import events
import security
import joblog
import costs
import discovery
import detection
import agentkit  # ==== agent kit ====

# Every file the Hub creates is private to its owner (0600): settings.json holds a webhook URL / ntfy topic that act
# like passwords, jobs.sqlite holds file names that can carry customer information. (Second security review.)
os.umask(0o077)

CFG_PATH = os.environ.get("BAMBU_MCP_CONFIG", os.path.join(os.path.dirname(__file__), "config.yml"))
CACHE_PATH = os.environ.get("BAMBU_MCP_CACHE", "/data/metacache.json")
# Everything the Hub persists lives in one data directory (the /data volume):
# metacache.json, thumbs/, backups/, and the fleet itself (printers.json + secrets.json).
DATA_DIR = os.path.dirname(CACHE_PATH)
# Plate-preview PNGs live beside the cache, one file per cache key (named by
# the key's sha1 so a filename with slashes/odd chars never touches a path).
THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")

# config.yml is OPTIONAL now. Printers are managed from the dashboard and stored in
# DATA_DIR; this file is only read for (a) the palette / conventions, and (b) a ONE-TIME
# import of a legacy `printers:` list into an empty store (see fleet_store.import_legacy).
DEFAULT_PALETTE = {"GREEN": "21FF06", "ORANGE": "FD8008", "RED": "C12E1F",
                   "BLUE": "0A2989", "BLACK": "000000", "WHITE": "FFFFFF"}


def _load_config(path):
    try:
        if not os.path.isfile(path):      # missing, or a directory left by a docker bind-mount
            return {}
        with open(path) as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[config] could not read {path}: {e} - continuing without it", flush=True)
        return {}


CFG = _load_config(CFG_PATH)

PALETTE = {str(k).upper(): str(v).upper() for k, v in (CFG.get("palette") or DEFAULT_PALETTE).items()}
RULES = CFG.get("conventions") or []

# Bambu generic material profiles: info_idx, nozzle_temp_min, nozzle_temp_max.
# Order is the button order the dashboard renders (most-used first).
MATERIALS = {
    "PLA":  ("GFL99", 190, 240),
    "PETG": ("GFG99", 220, 270),
    "ABS":  ("GFB99", 240, 280),
    "ASA":  ("GFB98", 240, 280),
    "TPU":  ("GFU99", 200, 250),
}

# name.lower() -> live driver. Shared (same dict object) with backup.py; mutated at
# runtime ONLY through FLEET (add / edit / remove), never directly.
PRINTERS: dict = {}
# Decided BEFORE the fleet store exists (importing a legacy config.yml writes printers.json): is this
# data directory already a Hub's? Then API-key authentication starts in WARN, not ON (see security.py).
_EXISTING_INSTALL = security.detect_existing_install(DATA_DIR, legacy_config=bool(CFG.get("printers")))


PRIVATE_DATA_FILES = ("settings.json", "printers.json", "secrets.json", "api_keys.json", "dashboard_auth.json",
                      "jobs.sqlite", "jobs.sqlite-wal", "jobs.sqlite-shm", "metacache.json", "login_limiter.reset",
                      "tray-changes.log")


def harden_data_files(data_dir=None) -> list:
    """Make the Hub's own data files owner-only (0600) - the ones written by an older version were world-readable.
    Returns the names whose mode was changed. Never raises (a read-only mount, a file owned by someone else)."""
    changed = []
    d = data_dir or DATA_DIR
    for name in PRIVATE_DATA_FILES:
        path = os.path.join(d, name)
        try:
            if os.path.isfile(path) and (os.stat(path).st_mode & 0o077):
                os.chmod(path, 0o600)
                changed.append(name)
        except OSError:
            pass
    return changed


harden_data_files()
STORE = fleet_store.FleetStore(DATA_DIR)
_legacy = CFG.get("printers")
if _legacy:
    _imported = STORE.import_legacy(_legacy, source=os.path.basename(CFG_PATH))
    if _imported:
        print(f"[fleet] imported {len(_imported)} printer(s) from {CFG_PATH}: {', '.join(_imported)} "
              f"(now stored in {DATA_DIR}; config.yml printers are no longer read)", flush=True)
FLEET = fleet_store.FleetManager(STORE, PRINTERS)
FLEET.load_all()
# Pre-heat presets (optional `preheat_presets:` in config.yml) and the live-driver lookup the
# pre-heat auto-off uses, so an edited or removed printer is never cooled through a dead driver.
controls.configure(CFG.get("preheat_presets"), resolver=lambda key: PRINTERS.get(key), data_dir=DATA_DIR)

def _is_octo(prn):
    """True for every file-based HTTP printer without an AMS (OctoPrint, PrusaLink,
    Moonraker): they list files with files(), not sd_list(), and send an empty mapping.
    (The name is historical; drivers advertise the trait with `uses_files_api = True`.)"""
    return getattr(prn, "kind", "") == "octoprint" or bool(getattr(prn, "uses_files_api", False))

def _has_camera(prn) -> bool:
    return bool(getattr(prn, "has_camera", False))

def _camera_fields(prn) -> dict:
    """`camera` flag (+ webcam flip flags for OctoPrint webcams) for API output."""
    cam = _has_camera(prn)
    info = prn.camera_info() if cam and hasattr(prn, "camera_info") else None
    return {"camera": cam, **({"camera_orientation": info} if info else {})}


# ------------------------------------------------------------- settings, notifications, events
# SETTINGS = typed settings (data dir settings.json; secrets in secrets.json, written through the
# fleet store so there is one writer). NOTIFIER delivers; WATCHER turns fleet snapshots into events.
# See docs/settings.md. fleet_store= is REQUIRED here: a second writer of secrets.json would
# clobber printer access codes.

SETTINGS = settings_store.SettingsStore(DATA_DIR, fleet_store=STORE)
NOTIFIER = notify.Notifier(SETTINGS)
# API keys + dashboard sessions. First start: fresh data dir -> mode ON, existing data -> WARN (once).
AUTH = security.AuthManager(DATA_DIR, SETTINGS, existing_install=_EXISTING_INSTALL)


def _jpeg_for(name):
    """Camera picture for a notification. Runs on the notifier thread, only when a message is
    really being sent; uses the printer's SHARED camera reader (never a second connection)."""
    prn = PRINTERS.get((name or "").lower())
    if prn is None or not _has_camera(prn):
        return None
    got = prn.camera().get_frame(8.0)
    return got[0] if got else None


def _event_sink(ev):
    """Every print/backup/... event goes to the job log first (so its row exists before a notification
    could link to it) and then to the notifier. Each is exception-safe; neither may break the other."""
    try:
        JOBLOG.on_event(ev)
    except Exception as e:
        print(f"[joblog] could not log {getattr(ev, 'kind', '?')}: {type(e).__name__}: {e}", flush=True)
    try:
        NOTIFIER.submit(ev)
    except Exception as e:
        print(f"[notify] could not queue {getattr(ev, 'kind', '?')}: {type(e).__name__}: {e}", flush=True)


WATCHER = events.EventWatcher(_event_sink, settings=SETTINGS, snapshot_fetcher=_jpeg_for)

# ---- job log + costs (docs/joblog.md). One row per print attempt, priced when it is asked for.
# The resolver answers from caches only (it runs inside the event sink: no network, ever).
_RECENT_STARTS: dict = {}      # printer name (lower) -> {"key", "mapping", "ts"} for prints this Hub started itself
_FILES_SEEN: dict = {}         # printer name (lower) -> {file_key: files() entry} from the last listing we already made
_RECENT_START_TTL = 1800       # seconds a "the Hub started this file with this AMS mapping" note stays usable


def _cached_meta_for(printer_name: str, job: str):
    """The metadata-cache entry for a Bambu job name (matched the way the log matches files:
    case-insensitive, without .gcode.3mf / .3mf). Cache only - never downloads."""
    want = costs.file_key(job)
    if not want:
        return None
    prefix = f"{printer_name}|"
    best = None
    try:
        with _cache_lock:
            cache = _cache_load()
    except Exception:
        return None
    for key, meta in cache.items():
        if not key.startswith(prefix) or not isinstance(meta, dict):
            continue
        name = key[len(prefix):].rsplit("|", 1)[0]
        if costs.file_key(name) == want and not meta.get("error"):
            best = meta
    return best


def _apply_ams_choice(prn, key: str, res: dict) -> dict:
    """If this Hub started the print itself it knows which AMS tray feeds each filament: price what is
    actually loaded rather than what the slicer assumed (type + colour from the tray, grams from the file)."""
    note = _RECENT_STARTS.get(prn.name.lower())
    if not note or note["key"] != key or time.time() - note["ts"] > _RECENT_START_TTL:
        return res
    try:
        trays = {t["tray"]: t for t in prn.ams_trays() if t.get("type")}
    except Exception:
        return res
    out = []
    for f in res.get("filaments") or []:
        f = dict(f)
        meta_id = f.get("id")
        idx = (meta_id - 1) if isinstance(meta_id, int) else len(out)
        tray = trays.get(note["mapping"][idx]) if 0 <= idx < len(note["mapping"]) else None
        if tray:
            f["type"], f["color"] = tray["type"], tray["color"]
        out.append(f)
    if out:
        res = dict(res, filaments=out)
        dom = max(out, key=lambda f: f.get("used_g") or 0)
        res["material"], res["colour"] = dom.get("type"), dom.get("color")
    return res


def _job_resolver(printer, job):
    """JobLog resolver: file facts (grams, time, parts, material) for a job name, from caches only."""
    prn = PRINTERS.get((printer or "").lower())
    key = costs.file_key(job)
    if prn is None or not key:
        return {}
    if _is_octo(prn):
        entry = (_FILES_SEEN.get(prn.name.lower()) or {}).get(key)
        return joblog.resolved_from_file_entry(entry, kind=getattr(prn, "kind", None)) if entry else {}
    meta = _cached_meta_for(prn.name, job)
    if not meta:
        return {}
    res = joblog.resolved_from_cached_meta(meta, parts=meta.get("parts"))
    if res:
        res["kind"] = getattr(prn, "kind", None) or "bambu"
        # the metadata cache keeps the slicer's filament list; keep the AMS ids so the mapping can be applied
        res["filaments"] = [dict(f, id=m.get("id")) for f, m in zip(res.get("filaments") or [], meta.get("filaments") or [])]
        res = _apply_ams_choice(prn, key, res)
    return res


JOBLOG = joblog.JobLog(os.path.join(DATA_DIR, "jobs.sqlite"), resolver=_job_resolver,
                       rates=lambda: costs.Rates.from_settings(SETTINGS.get),
                       tz=SETTINGS.get("general.timezone"))
SETTINGS.subscribe(lambda _changed=None: setattr(JOBLOG, "tz", JOBLOG._tz(SETTINGS.get("general.timezone"))))


def spaghetti_detected(printer: str, score, jpeg=None, level: str = "failure"):
    """The failure detector's hook: a print looks like it has gone wrong. Becomes a `spaghetti_suspected`
    notification (breaks through quiet hours by default). level = "warning" | "failure"."""
    return WATCHER.spaghetti_suspected(printer, score, jpeg, level)


def _apply_control_settings(_changed=None):
    """Push the saved pre-heat settings into controls.py. Called at start-up and after every
    settings change. Only a SAVED (or valid-env) value overrides what controls.py already does,
    so the PREHEAT_TIMEOUT_MIN / config.yml behaviour is unchanged until someone uses the page."""
    try:
        sources = SETTINGS.get_all()["sources"]
        tk, pk = "printers.preheat_auto_off_min", "printers.preheat_presets"
        controls.set_runtime(timeout_min=SETTINGS.get(tk) if sources.get(tk) != "default" else None)
        if sources.get(pk) == "saved":
            saved = SETTINGS.get(pk)
            controls.configure({n: {"nozzle": v["nozzle_c"], "bed": v["bed_c"]} for n, v in saved.items()},
                               exclusive=True)
        else:
            controls.configure(CFG.get("preheat_presets"))
    except Exception as e:
        print(f"[settings] could not apply the pre-heat settings: {e}", flush=True)


_apply_control_settings()
SETTINGS.subscribe(_apply_control_settings)
controls.set_runtime(on_auto_off=lambda name, minutes: WATCHER.preheat_auto_off(name, minutes),
                     on_auto_off_failed=lambda name, why: WATCHER.preheat_auto_off_failed(name, why))


def _backup_finished(job):
    """BackupManager hook, once per job: tell the human how a backup ended."""
    if job.status == "done":
        WATCHER.backup_done(job.printer, files=job.files_done, size_bytes=job.size, name=job.file)
    elif job.status == "partial":
        WATCHER.backup_failed(job.printer, "only partly backed up: " + (job.error or "some files failed"))
    else:
        WATCHER.backup_failed(job.printer, job.error or "unknown error")


EVENT_POLL_SECONDS = 5.0       # driver.snapshot() is cached ~3 s, so this adds no printer traffic


def _fleet_snapshots() -> dict:
    """{printer name: snapshot} for the WHOLE fleet right now. The registry is read through a copy
    (printers can be added / edited / removed at any moment); a driver that fails is reported
    as unreachable rather than left out (a missing name would make the watcher forget it)."""
    out = {}
    for prn in list(PRINTERS.values()):
        try:
            out[prn.name] = prn.snapshot()
        except Exception as e:
            out[prn.name] = {"printer": prn.name, "connected": False, "state": "UNKNOWN",
                             "error": f"{type(e).__name__}: {e}"}
    return out


# ---- spaghetti detection (docs/detection.md). ALERT ONLY: nothing here ever pauses or stops a print.
# The loop is created here but started in __main__ (never on import), and it does nothing until
# Settings > Detection is switched on.
DETECTION_DIR = os.path.join(DATA_DIR, "detection")
DETECTOR_DEFAULT_URL = "http://detector:8080"       # the compose service name ("This machine" mode)


def _detection_has_camera(name):
    prn = PRINTERS.get((name or "").lower())
    return prn is not None and _has_camera(prn)


def _detection_frame(name):
    """(jpeg, timestamp) | None from the printer's SHARED camera reader - no second camera connection."""
    prn = PRINTERS.get((name or "").lower())
    return prn.camera().get_frame(8.0) if prn is not None and _has_camera(prn) else None


def _detection_alert(printer, level, score, jpeg):
    spaghetti_detected(printer, score, jpeg, level)


def _detector_status_hook(state, info):
    """Tell the owner when the detector goes quiet (prints are then unwatched) and when it is back."""
    if state == "down":
        WATCHER.detector_down((info or {}).get("error", ""), (info or {}).get("failures"))
    elif state == "up":
        WATCHER.detector_up()


DETECTION = detection.DetectionLoop(_fleet_snapshots, _detection_frame, _detection_alert,
                                    has_camera=_detection_has_camera, store_dir=DETECTION_DIR,
                                    on_detector=_detector_status_hook)


def _detection_target(mode=None, url=None, token=None):
    """(mode, url, token) the detector client should use. `url` / `token` given (a form being tested) win;
    an empty value falls back to the saved setting, then (mode 'local' only) the environment, then the
    compose default. Never returned to a browser."""
    mode = mode if mode in ("off", "local", "remote") else SETTINGS.get("detection.mode")
    if mode == "off":
        return "off", "", ""
    url = (url.strip() if isinstance(url, str) else "") or SETTINGS.get("detection.url")
    token = (token if isinstance(token, str) else "") or SETTINGS.get_secret("detection.token")
    if mode == "local":
        url = url or os.environ.get("DETECTOR_URL") or DETECTOR_DEFAULT_URL
        token = token or os.environ.get("DETECTOR_TOKEN", "")
    return mode, url, token


_detection_key = [None]


def _apply_detection_settings(_changed=None):
    """Push Settings > Detection into the loop. A new client (address / token changed) resets the back-off."""
    try:
        mode, url, token = _detection_target()
        kw = dict(interval_s=int(SETTINGS.get("detection.interval_s")),
                  sensitivity=SETTINGS.get("detection.sensitivity"),
                  learn_only=SETTINGS.get("detection.learn_only"),
                  cooldown_s=SETTINGS.get("detection.cooldown_min") * 60,
                  excluded=SETTINGS.get("detection.excluded_printers"))
        key = (mode, url, token)
        if key != _detection_key[0]:
            _detection_key[0] = key
            kw["client"] = detection.DetectorClient(url, token) if mode in ("local", "remote") else None
        DETECTION.configure(**kw)
    except Exception as e:
        print(f"[detection] could not apply the settings: {type(e).__name__}: {e}", flush=True)


_apply_detection_settings()
SETTINGS.subscribe(_apply_detection_settings)


def _detection_public_url(url: str) -> str:
    """The address without any user:password@ part - safe to show."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url or "")
        return f"{u.scheme}://{u.hostname}{':' + str(u.port) if u.port else ''}" if u.hostname else ""
    except Exception:
        return ""


def _detection_status() -> dict:
    st = DETECTION.status()
    mode, url, _token = _detection_target()
    st["setup"] = {"mode": mode, "url": _detection_public_url(url), "learn_only": bool(SETTINGS.get("detection.learn_only"))}
    return st


def _detection_line(name: str) -> dict:
    """One short line of words for a printer's screen, e.g. 'Watching - score 0.02 - learn-only'."""
    st = DETECTION.status()
    prn = PRINTERS.get((name or "").lower())
    if not st["enabled"]:
        return {"state": "off", "text": "Off"}
    if prn is None or not _has_camera(prn):
        return {"state": "no_camera", "text": "No camera, so it cannot be watched"}
    p = st["printers"].get(name) or next((v for k, v in st["printers"].items() if k.lower() == name.lower()), None) or {}
    learn = " \u00b7 learn-only" if st["mode"] == "learn" else ""
    if p.get("excluded"):
        return {"state": "excluded", "text": "Off for this printer"}
    if st["detector"]["state"] == "down":
        return {"state": "down", "text": "Detector unreachable - this print is NOT being watched"}
    if not p.get("watching"):
        return {"state": "idle", "text": "Ready - watches while a print is running" + learn}
    v = p.get("verdict")
    cam = p.get("camera")
    if cam == "blind":
        return {"state": "camera", "text": "Camera sees almost nothing - turn the chamber light on" + learn}
    if cam == "no_frame":
        return {"state": "camera", "text": "No camera picture yet" + learn}
    if not v:
        return {"state": "waiting", "text": "Watching - waiting for the first picture" + learn}
    score = f"score {v['score']:.2f}"
    if v.get("warmup"):
        return {"state": "warmup", "text": f"Watching \u00b7 warming up \u00b7 {score}" + learn, "score": v["score"]}
    if v["level"] == "ok":
        return {"state": "ok", "text": f"Watching \u00b7 {score}" + learn, "score": v["score"]}
    word = "WARNING - possible spaghetti" if v["level"] == "warning" else "FAILURE - likely spaghetti"
    return {"state": v["level"], "text": f"{word} \u00b7 {score}" + learn, "score": v["score"]}


def _event_tick():
    fleet = _fleet_snapshots()
    WATCHER.update(fleet)
    try:
        JOBLOG.observe(fleet)        # closes a log row whose end event never came (same tick, no extra traffic)
    except Exception as e:
        print(f"[joblog] observe failed: {type(e).__name__}: {e}", flush=True)


def _event_loop(stop=None, interval=None):
    next_harden = 0.0
    while stop is None or not stop.is_set():
        try:
            _event_tick()
        except Exception as e:
            print(f"[events] {type(e).__name__}: {e}", flush=True)
        if time.monotonic() >= next_harden:       # a writer that sets its own file mode (printers.json) is put right again
            next_harden = time.monotonic() + 60.0
            harden_data_files()
        if stop is not None:
            stop.wait(EVENT_POLL_SECONDS if interval is None else interval)
        else:
            time.sleep(EVENT_POLL_SECONDS if interval is None else interval)


_event_thread = None


def start_event_loop(stop=None, interval=None):
    """Start the (single) background thread that feeds the EventWatcher. Idempotent."""
    global _event_thread
    if _event_thread is not None and _event_thread.is_alive():
        return _event_thread
    _event_thread = threading.Thread(target=_event_loop, args=(stop, interval), daemon=True,
                                     name="event-watcher")
    _event_thread.start()
    return _event_thread

# ------------------------------------------------------------- color helpers

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

def color_name(hex_color: str) -> str:
    """Nearest palette name — so every color leaves this server as a WORD."""
    if not hex_color:
        return "UNKNOWN"
    r, g, b = _hex_to_rgb(hex_color)
    best, dist = "UNKNOWN", 1e9
    for name, h in PALETTE.items():
        pr, pg, pb = _hex_to_rgb(h)
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < dist:
            best, dist = name, d
    return best

def convention_for(filename: str):
    for rule in RULES:
        if re.search(rule["match"], filename, re.I):
            return rule
    return None

# ------------------------------------------------------------- metadata cache

def _cache_load() -> dict:
    try:
        with open(CACHE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}

def _cache_save(c: dict):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as fh:
        json.dump(c, fh)

# metacache.json is one shared file — serialize its read/mutate/write so two
# threads (a live /suggest and a background pre-warm) can't clobber each other.
_cache_lock = threading.Lock()

# Bambu printers tolerate ~one FTPS session at a time. One lock per printer,
# created lazily, so a pre-warm and a live /suggest never open two sessions
# to the same printer at once — but different printers can still run in parallel.
_ftps_locks: dict = {}
_ftps_locks_guard = threading.Lock()

def _ftps_lock(printer_name: str) -> threading.Lock:
    with _ftps_locks_guard:
        lock = _ftps_locks.get(printer_name)
        if lock is None:
            lock = _ftps_locks[printer_name] = threading.Lock()
        return lock

# OctoPrint is plain HTTP, not the one-session-at-a-time FTPS above — this
# lock just keeps two prewarm passes on the same printer from opening
# redundant header downloads; different printers still run in parallel.
_octo_locks: dict = {}
_octo_locks_guard = threading.Lock()

def _octo_lock(printer_name: str) -> threading.Lock:
    with _octo_locks_guard:
        lock = _octo_locks.get(printer_name)
        if lock is None:
            lock = _octo_locks[printer_name] = threading.Lock()
        return lock

def _thumb_path(basename: str) -> str:
    return os.path.join(THUMBS_DIR, basename)

def _save_thumb(key: str, png_bytes):
    """Save a plate-preview PNG for a cache key under THUMBS_DIR. Returns the
    basename to record in the cache entry, or None if the file had no plate
    image — recorded as None (not just absent) so _prewarm never retries it."""
    if not png_bytes:
        return None
    basename = hashlib.sha1(key.encode()).hexdigest() + ".png"
    os.makedirs(THUMBS_DIR, exist_ok=True)
    with open(_thumb_path(basename), "wb") as fh:
        fh.write(png_bytes)
    return basename

def _printer_is_idle(prn) -> bool:
    """True only when the Hub can SEE that the printer is connected and idle (whitelist: an unknown, offline or
    unmapped state counts as busy). A Bambu printer is not asked for files - one FTPS session is enough to
    disturb a print - unless this is true."""
    try:
        s = prn.snapshot()
    except Exception:
        return False
    return bool(s.get("connected", True)) and s.get("state") in controls.IDLE_STATES


METADATA_BUSY = "metadata unavailable while the printer is busy (printing, paused, preparing or not connected)"


def _sd_list(prn):
    """prn.sd_list() under the printer's FTPS lock: the Bambu allows about one FTPS session at a time, and a
    listing that races a download or a delete can knock either one over."""
    with _ftps_lock(prn.name):
        return prn.sd_list()


def _meta_for(prn: BambuPrinter, name: str, size: int, download_if_missing: bool, force: bool = False):
    """force=True re-downloads even if a cache entry already exists — used to
    backfill a thumbnail onto an entry cached before thumbnails existed.
    Never downloads from a printer that is not idle: the cached entry (if any) is served instead, else None."""
    key = f"{prn.name}|{name}|{size}"
    if not force:
        with _cache_lock:
            cache = _cache_load()
            if key in cache:
                return cache[key]
    if not download_if_missing:
        return None
    if not _printer_is_idle(prn):
        with _cache_lock:
            return _cache_load().get(key)
    with _ftps_lock(prn.name):
        # someone else may have downloaded this exact file while we waited for the lock
        if not force:
            with _cache_lock:
                cache = _cache_load()
                if key in cache:
                    return cache[key]
        if not _printer_is_idle(prn):         # a print may have started while we waited for the FTPS lock
            with _cache_lock:
                return _cache_load().get(key)
        data = prn.sd_download(name)
        meta = parse_3mf_meta(data)
        meta["thumb"] = _save_thumb(key, extract_plate_png(data))
        try:                       # how many objects are on plate 1: the job log prices per part
            meta["parts"] = joblog.parts_for_plate(joblog.slice_info_from_3mf(data))
        except Exception:
            meta["parts"] = None
        with _cache_lock:
            cache = _cache_load()
            cache[key] = meta
            _cache_save(cache)
    return meta

def _remember_files(prn, files):
    """Keep the latest files() listing of a file-based printer so the job log can price a print from it
    later without a network request of its own."""
    try:
        _FILES_SEEN[prn.name.lower()] = {costs.file_key(f.get("name")): dict(f) for f in files if f.get("name")}
    except Exception:
        pass


def _octo_thumb_for(prn: OctoPrintPrinter, name: str, size: int):
    """Read-only cache lookup for an OctoPrint file's plate thumbnail. Never
    downloads — a live file listing must not block on the printer's network;
    the actual header pull only happens in _prewarm."""
    key = f"{prn.name}|{name}|{size}"
    with _cache_lock:
        cache = _cache_load()
    entry = cache.get(key)
    return entry.get("thumb") if entry else None

def _octo_meta_for(prn: OctoPrintPrinter, name: str, size: int):
    """Download just the gcode header and cache its PrusaSlicer-embedded plate
    thumbnail, if it has one. Unlike Bambu's _meta_for there's no slice_info
    to parse here — OctoPrint's own file API already supplies print time/
    filament — so the cache entry holds only {"thumb": ...}. Only called from
    _prewarm, off the request path."""
    key = f"{prn.name}|{name}|{size}"
    with _octo_lock(prn.name):
        # someone else may have finished this exact file while we waited for the lock
        with _cache_lock:
            cache = _cache_load()
            if "thumb" in cache.get(key, {}) and "slicer_model" in cache.get(key, {}):
                return cache[key]
        data = prn.download_head(name)
        thumb = _save_thumb(key, extract_gcode_png(data))
        # the printer model the slicer named in the head ("sliced for another printer" check);
        # None = looked and found nothing, so it is not downloaded again
        entry_new = {"thumb": thumb, "slicer_model": modelcheck.gcode_head_model(data) or None}
        with _cache_lock:
            cache = _cache_load()
            cache[key] = {**(cache.get(key) or {}), **entry_new}
            _cache_save(cache)
    return entry_new

def _cache_purge(printer_name: str, filename: str):
    """Drop every cached metadata entry for a printer+filename (any cached
    size) and its thumb PNG, if any — called after a successful SD delete so
    a stale entry doesn't outlive the file it describes."""
    prefix = f"{printer_name}|{filename}|"
    with _cache_lock:
        cache = _cache_load()
        keys = [k for k in cache if k.startswith(prefix)]
        if not keys:
            return
        for k in keys:
            thumb = cache.pop(k, {}).get("thumb")
            if thumb:
                try:
                    os.remove(_thumb_path(thumb))
                except OSError:
                    pass
        _cache_save(cache)

def _pick(name: str):
    prn = PRINTERS.get(name.lower())
    if not prn:
        known = ', '.join(p.name for p in list(PRINTERS.values())) or "none added yet"
        raise ValueError(f"Unknown printer '{name}'. Known: {known}")
    return prn

def _describe_meta(meta: dict) -> dict:
    if not meta:
        return {"metadata": "not cached — call with refresh_metadata=true (slow: full download; a control key and an idle printer are needed)"}
    fil = [{"type": f["type"], "color_name": color_name(f["color"]), "hex": f["color"]}
           for f in meta.get("filaments", [])]
    return {"print_time": meta.get("print_time"), "weight_g": meta.get("weight_g"),
            "sliced_for": meta.get("printer_model_id"), "filaments": fil}

# one background pre-warm at a time per printer — repeated dashboard polls
# (fleet refresh, tapping into a printer) must not stack up duplicate threads
_prewarm_running: set = set()
_prewarm_guard = threading.Lock()

def _prewarm_bambu(prn: BambuPrinter):
    if not _printer_is_idle(prn):
        return  # don't pull a big file off a printer that's mid-job (or whose state we cannot see)
    files = _sd_list(prn)
    with _cache_lock:
        cache = _cache_load()
    # (file, force) — force=True means "already cached, but from before
    # thumbnails existed: re-download once to backfill the thumb". An
    # entry with "thumb": null was already tried and had no plate image,
    # so it's left alone rather than retried forever.
    todo = []
    for f in files:
        entry = cache.get(f"{prn.name}|{f['name']}|{f['size']}")
        if entry is None:
            todo.append((f, False))
        elif "thumb" not in entry:
            todo.append((f, True))
    if not todo:
        return
    print(f"[prewarm] {prn.name}: warming {len(todo)} file(s)", flush=True)
    for f, force in todo:
        # re-check every file: a print may have started since we began, and
        # a long FTPS pull during a job is exactly what this guard exists to avoid
        if not _printer_is_idle(prn):
            print(f"[prewarm] {prn.name}: job started — stopping early", flush=True)
            return
        try:
            _meta_for(prn, f["name"], f["size"], True, force=force)
            print(f"[prewarm] {prn.name}: cached {f['name']}", flush=True)
        except Exception as e:
            print(f"[prewarm] {prn.name}: failed on {f['name']}: {e}", flush=True)
        time.sleep(1)  # give a waiting user request a chance at the FTPS lock
    print(f"[prewarm] {prn.name}: done", flush=True)


def _prewarm_octo(prn: OctoPrintPrinter):
    # No idle gate here, unlike the Bambu pass: this reads 1 MB of a file off
    # the OctoPrint Pi's disk over HTTP — the printer itself never sees it, so
    # it's safe mid-job (and MK4 jobs run 9h+; waiting would leave rows blank
    # for most of a day).
    files = prn.files()
    _remember_files(prn, files)
    with _cache_lock:
        cache = _cache_load()
    # unlike Bambu there's no "backfill an old entry" case — the cache entry
    # for an OctoPrint file only ever holds "thumb", so missing the key
    # entirely and missing "thumb" on it are the same condition
    todo = [f for f in files if "thumb" not in cache.get(f"{prn.name}|{f['name']}|{f['size']}", {})]
    if not todo:
        return
    print(f"[prewarm] {prn.name}: warming {len(todo)} file(s)", flush=True)
    for f in todo:
        try:
            _octo_meta_for(prn, f["name"], f["size"])
            print(f"[prewarm] {prn.name}: cached {f['name']}", flush=True)
        except Exception as e:
            print(f"[prewarm] {prn.name}: failed on {f['name']}: {e}", flush=True)
        time.sleep(1)  # give a waiting user request a chance at the header download
    print(f"[prewarm] {prn.name}: done", flush=True)


def _prewarm(printer_name: str):
    """Background: quietly download+cache metadata for a printer's uncached
    files (SD-card .3mf metadata for Bambu, gcode-header plate thumbnails for
    OctoPrint), so a later /suggest or file listing never has to eat a slow
    download itself. Never raises — a failure here must not affect any live
    request."""
    with _prewarm_guard:
        if printer_name in _prewarm_running:
            return
        _prewarm_running.add(printer_name)
    try:
        prn = PRINTERS.get(printer_name.lower())
        if not prn:
            return
        if _is_octo(prn):
            _prewarm_octo(prn)
        else:
            _prewarm_bambu(prn)
    except Exception as e:
        print(f"[prewarm] {printer_name}: aborted: {e}", flush=True)
    finally:
        with _prewarm_guard:
            _prewarm_running.discard(printer_name)

# ------------------------------------------------------------- MCP tools

mcp = FastMCP("fabrica",
              host=os.environ.get("BAMBU_MCP_HOST", "0.0.0.0"),
              port=int(os.environ.get("BAMBU_MCP_PORT", "8271")))

# Put the API-key check in front of EVERYTHING the app serves (/mcp, /api/*, the dashboard).
# Done by wrapping the app factory so mcp.run(), tests and any embedding get the same app.
_plain_streamable_http_app = mcp.streamable_http_app


def _secured_streamable_http_app(*args, **kwargs):
    app = _plain_streamable_http_app(*args, **kwargs)
    app.add_middleware(security.AuthMiddleware, auth=AUTH, mcp_path=mcp.settings.streamable_http_path)
    return app


mcp.streamable_http_app = _secured_streamable_http_app


def _tool_caller_scope(ctx, name):
    """Second line of defence behind security.AuthMiddleware: the scope of the key that sent THIS tool call,
    read from the very request that carried it. A key that may not use `name` is refused here as well, so a body
    the middleware misread could never reach a control tool. -> "read" | "control" | None (no credential
    needed or presented, e.g. key check Off). Raises ValueError (the refusal text) when not allowed."""
    try:
        req = ctx.request_context.request if ctx is not None else None
    except Exception:
        req = None
    ident = req.scope.get("auth") if req is not None else None
    if ident is None or ident.kind not in ("key", "cookie"):
        return None
    if security.ident_rank(ident) < security.SCOPE_RANK[security.tool_scope(name)]:
        raise ValueError(security.tool_refusal_text(name, ident.scope))
    return ident.scope


def _tool(fn):
    """Register `fn` as an MCP tool. Two things @mcp.tool() alone does not do:
      * a plain `def` tool runs inline on the event loop in FastMCP, so one that sleeps (start_print waits 8 s)
        or talks FTPS froze the whole Hub - REST, the dashboard, the cameras, every other agent. Here every
        sync tool runs in a worker thread (asyncio.to_thread); async tools are awaited as they are.
      * the caller's scope is re-checked (see _tool_caller_scope) and exposed to the tool body through
        security.CALLER_SCOPE (for redaction and for "a read key never triggers downloads").
    Returns `fn` itself, unchanged, so the REST routes and the tests can still call the plain function."""
    sig = inspect.signature(fn)
    name = fn.__name__
    is_async = inspect.iscoroutinefunction(fn)

    @functools.wraps(fn)
    async def wrapper(*args, ctx: Context = None, **kwargs):
        scope = _tool_caller_scope(ctx, name)
        token = security.CALLER_SCOPE.set(scope)
        try:
            res = await fn(*args, **kwargs) if is_async else await asyncio.to_thread(fn, *args, **kwargs)
            return security.scrub_text(res) if isinstance(res, str) else res
        except Exception as e:
            clean = security.scrub_text(str(e))
            if clean != str(e):                         # a printer URL with user:password@ inside an error message
                raise RuntimeError(clean) from None
            raise
        finally:
            security.CALLER_SCOPE.reset(token)

    wrapper.__signature__ = sig.replace(parameters=[
        *sig.parameters.values(),
        inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context)])
    wrapper.__annotations__ = {**getattr(fn, "__annotations__", {}), "ctx": Context}
    mcp.add_tool(wrapper, name=name, description=fn.__doc__)
    return fn


def _kit_line() -> str:      # ==== agent kit ==== one line so a running agent notices a new kit version
    return f"Fabrica kit {KIT.version}\n" if KIT.version else ""


@_tool
def list_printers() -> str:
    """List all printers with live state (idle/printing, job, progress) and the groups each is in (see list_groups)."""
    if not PRINTERS:
        return (_kit_line() + "No printers added yet. Use list_printer_types to see what can be added, "
                "then add_printer (or ask the human to use the dashboard's Printers button).")
    out = []
    group_of = FLEET.store.groups_by_printer()
    for prn in list(PRINTERS.values()):
        s = prn.snapshot()
        line = f'{s["printer"]}: {s["state"]}'
        if s["state"] == "RUNNING":
            line += f' — "{s["job"]}" {s["percent"]}% (~{s["remaining_min"]} min left)'
        if not s["connected"]:
            line += "  [MQTT DISCONNECTED]"
        gl = group_of.get(s["printer"]) or next((v for k, v in group_of.items() if k.lower() == str(s["printer"]).lower()), [])
        if gl:
            line += f'  [groups: {", ".join(gl)}]'
        out.append(line)
    return _kit_line() + "\n".join(out)


@_tool
def printer_status(printer: str) -> str:
    """Full live status for one printer: state, temps, job, progress, errors."""
    prn = _pick(printer)
    prn.refresh(wait=2.5)
    return json.dumps(prn.snapshot(), indent=2)


@_tool
def ams_state(printer: str) -> str:
    """AMS tray readout. Colors are given as NAMES, never hues (colorblind-safe)."""
    prn = _pick(printer)
    if _is_octo(prn):
        return f"{prn.name} has no AMS — it prints the loaded filament."
    rows = []
    for t in prn.ams_trays():
        if t["type"]:
            rows.append(f'Tray {t["tray"]} (slot {t["slot_1based"]}): '
                        f'{color_name(t["color"])} {t["type"]}  [{t["color"]}]')
        else:
            rows.append(f'Tray {t["tray"]} (slot {t["slot_1based"]}): EMPTY')
    return "\n".join(rows) or "No AMS data yet — try printer_status first."


@_tool
def list_sd_files(printer: str, refresh_metadata: bool = False) -> str:
    """List printable .3mf jobs on the printer's SD card. With
    refresh_metadata=true, downloads uncached files to extract print time,
    weight and filament colors (slow — ~30s per file on the P1S). Needs a control key and an idle printer:
    a read-only key, or a printer that is printing, only ever gets what is already cached."""
    prn = _pick(printer)
    refresh_metadata = bool(refresh_metadata) and not security.caller_read_only()
    if _is_octo(prn):
        files = prn.files()
        _remember_files(prn, files)
        for f in files:
            f["thumb"] = _octo_thumb_for(prn, f["name"], f["size"])
        return json.dumps(files, indent=2)
    files = _sd_list(prn)
    out = []
    for f in files:
        meta = _meta_for(prn, f["name"], f["size"], refresh_metadata)
        entry = {"name": f["name"], "size_mb": round(f["size"] / 1048576, 2),
                 "modified": f["modified"], "thumb": meta.get("thumb") if meta else None,
                 **_describe_meta(meta)}
        rule = convention_for(f["name"])
        if rule and meta:
            wanted = {color_name(fl["color"]) for fl in meta.get("filaments", [])}
            allowed = set(rule["color"].upper().split("|"))
            if wanted and not (wanted & allowed):
                entry["convention_warning"] = (
                    f'file is sliced {"/".join(sorted(wanted))} but convention for this part '
                    f'is {rule["color"]} — probable slicing error; use ams_mapping to fix at start')
        out.append(entry)
    return json.dumps(out, indent=2)


def _model_check(prn, filename: str, entries: list) -> dict:
    """The 'sliced for another printer' verdict (see modelcheck.py) from cached metadata entries
    for this file (several when the file was re-saved at another size - any mismatch wins).
    Pure cache reads: no printer traffic. -> {sliced_for, target_model, model_match, model_warning, ...}"""
    target = modelcheck.target_model_for(prn)
    best = None
    for e in entries:
        if not isinstance(e, dict):
            continue
        sliced = (e.get("slicer_model") or "") if _is_octo(prn) else modelcheck.file_model_bambu(e)
        r = modelcheck.check(sliced, target)
        if r["model_match"] is False:
            best = r
            break
        if best is None or (best["model_match"] == "unknown" and r["model_match"] is True):
            best = r
    return best or modelcheck.check("", target)


def _model_check_cached(prn, filename: str) -> dict:
    """Authoritative re-check at start time, straight from the metadata cache (suggest_mapping filled it)."""
    prefix = f"{prn.name}|{filename}|"
    with _cache_lock:
        cache = _cache_load()
    return _model_check(prn, filename, [v for k, v in cache.items() if k.startswith(prefix)])


def _model_next_step(mc: dict, normal: str) -> str:
    if mc.get("model_match") is False:
        return (f"MODEL MISMATCH \u2014 {mc['model_warning']} Do NOT call start_print unless the human has read "
                "this warning and explicitly confirmed this file is safe on this printer; then pass "
                f"model_override=True and add \"|{modelcheck.MODEL_OVERRIDE_TAG}\" to the end of the confirm "
                "string. Without that, start_print refuses. " + normal)
    if mc.get("model_match") == "unknown":
        return normal + " (Note: " + mc["model_warning"] + ")"
    return normal


# ---------------------------------------------------------------- start tickets (the confirm "token")
# suggest_mapping issues a random, single-use, short-lived ticket; start_print needs it back (the `confirm`
# argument / field) and consumes it. It is kept on the server and bound to the printer connection, the file
# (and its size on the card), the proposed AMS mapping and a fingerprint of what is loaded in the AMS, so a
# confirm can no longer be written by the caller, replayed, guessed from an error message, or carried over to
# another printer, file, mapping or spool setup. Nothing about the expected value is ever echoed back.
START_TICKET_TTL_S = 300      # long enough to walk to the printer and look at the bed, short enough to be useless later
START_PENDING_S = 60          # after a start was SENT, another one for the same printer waits this long
MAX_START_TICKETS = 200


class StartTickets:
    def __init__(self, ttl=START_TICKET_TTL_S, clock=time.time):
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._t = {}

    def _prune(self, now):
        for k in [k for k, t in self._t.items() if t["expires"] <= now]:
            del self._t[k]
        while len(self._t) > MAX_START_TICKETS:
            del self._t[min(self._t, key=lambda k: self._t[k]["created"])]

    def issue(self, prn, filename, size, mapping, ams_fp, model_mismatch=False) -> str:
        nonce = "ph-" + secrets.token_urlsafe(18)
        now = self.clock()
        with self._lock:
            self._prune(now)
            self._t[nonce] = {"prn": prn, "printer": prn.name.lower(), "file": filename, "size": size,
                              "mapping": list(mapping), "ams_fp": ams_fp, "model_mismatch": bool(model_mismatch),
                              "created": now, "expires": now + self.ttl}
        return nonce

    def peek(self, nonce, prn, filename):
        """-> (ticket, None) or (None, why). Does not consume. `why` never contains the expected value."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            t = self._t.get(nonce) if isinstance(nonce, str) else None
        if t is None:
            return None, ("it is not a current confirm string - it is wrong, was already used, or has expired "
                          f"(they last {self.ttl // 60} minutes and work once)")
        if t["prn"] is not prn:
            return None, ("it was issued for a different printer (or the printer's connection was edited or "
                          "re-added since)")
        if t["file"] != filename:
            return None, "it was issued for a different file"
        return t, None

    def consume(self, nonce) -> bool:
        with self._lock:
            return self._t.pop(nonce, None) is not None

    def clear(self):
        with self._lock:
            self._t.clear()


START_TICKETS = StartTickets()
_start_locks: dict = {}
_start_locks_guard = threading.Lock()
_START_PENDING: dict = {}      # printer (lower) -> monotonic time a start was last SENT to it


def _start_lock(printer_name: str) -> threading.Lock:
    with _start_locks_guard:
        lock = _start_locks.get(printer_name.lower())
        if lock is None:
            lock = _start_locks[printer_name.lower()] = threading.Lock()
        return lock


def _ams_fingerprint(prn) -> str:
    """What is loaded: tray id + material + colour of every tray, hashed. '' for printers without an AMS."""
    if _is_octo(prn):
        return ""
    try:
        rows = sorted((int(t["tray"]), str(t.get("type") or ""), str(t.get("color") or "").upper())
                      for t in prn.ams_trays())
    except Exception:
        rows = []
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:24]


def _mapping_problem(want: list, got, prn, allow_remap: bool):
    """Why `got` may not be used with a ticket that proposed `want`, or None. Tray numbers must be real trays that
    hold filament (an invented tray is refused); strict mode also needs exactly the proposed mapping. allow_remap
    (the dashboard, i.e. the human at the screen picking another spool) lets the used slots point at any other
    loaded tray, but never changes which filament slots are used."""
    if (not isinstance(got, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in got)):
        return "ams_mapping must be a list of whole numbers"
    if len(got) != len(want) or any((a >= 0) != (b >= 0) for a, b in zip(got, want)):
        return "ams_mapping uses different filament slots than the suggestion"
    if got != want and not allow_remap:
        return "ams_mapping differs from the one suggest_mapping proposed; run suggest_mapping again"
    used = [x for x in got if x >= 0]
    if len(set(used)) != len(used):
        return "ams_mapping uses the same tray twice"
    if used:
        loaded = {int(t["tray"]) for t in prn.ams_trays() if t.get("type")}
        bad = [x for x in used if x not in loaded]
        if bad:
            return f"tray {bad[0]} does not hold any filament"
    return None


# Pure mapping logic, pulled out of suggest_mapping so it's independently testable
# (no printer/network calls inside — just filaments + trays + a convention rule).
def _map_filaments(filaments: list, trays: list, rule) -> tuple:
    """Assign each USED slicer filament (by its 1-based `id`) to a free AMS tray.

    Match order per filament, never reusing a tray already claimed by another
    filament in this job: (1) same type + target color, (2) same type any
    color (with a note), (3) nothing of that type free at all — single-filament
    jobs fall back to the first tray (today's behavior, the user can tap a
    different spool on the dashboard); multi-filament jobs are unsafe to guess
    at, so that filament is left unmapped (-1) and `blocked` is set.

    The part-color convention only steers the PRIMARY filament (largest
    used_g, or the first filament if none have used_g) — a support/interface
    filament keeps the file's own color and gets no convention warning.

    Returns (mapping, clauses, notes, blocked):
      mapping  — 0-based tray ids, list length = max slicer filament id,
                 indexed by id-1 (`ams_mapping` for project_file), -1 = unused/unmatched
      clauses  — one human line per used filament, e.g. "filament 1 PETG → slot 2 (GREEN PETG)"
      notes    — ⚠ warnings to surface to the human
      blocked  — ⛔ string if the job cannot be safely started as-is, else None
    """
    if not filaments:
        return [], [], [], None

    used_gs = [f.get("used_g") for f in filaments]
    if any(g is not None for g in used_gs):
        primary = max(filaments, key=lambda f: f.get("used_g") or 0)
    else:
        primary = filaments[0]  # no used_g anywhere (old cache entry, or slicer omitted it)

    max_id = max((f.get("id") or i + 1) for i, f in enumerate(filaments))
    mapping = [-1] * max_id
    clauses, notes, blocked_msgs = [], [], []
    claimed: set = set()
    multi = len(filaments) > 1

    for i, fl in enumerate(filaments):
        fid = fl.get("id") or (i + 1)  # shouldn't happen, but fall back to list position
        slot_idx = fid - 1
        want_type = fl["type"]
        want_color = color_name(fl["color"])
        target_colors = [want_color]
        if rule and fl is primary:
            conv = rule["color"].upper().split("|")
            if want_color not in conv:
                notes.append(f'⚠ file wants {want_color} but convention says {rule["color"]} '
                             f'— recommending the CONVENTION color')
                target_colors = conv

        chosen, note = None, None
        # (1) same type + target color, unclaimed
        for tc in target_colors:
            for t in trays:
                if t["tray"] not in claimed and t["type"] == want_type and color_name(t["color"]) == tc:
                    chosen = t
                    break
            if chosen:
                break
        # (2) same type, any color, unclaimed
        if not chosen:
            for t in trays:
                if t["tray"] not in claimed and t["type"] == want_type:
                    chosen = t
                    note = (f'⚠ no {want_type} in {"/".join(target_colors)} loaded — using slot '
                            f'{t["slot_1based"]} ({color_name(t["color"])} {want_type})')
                    break
        # (3) no tray of that type free at all
        if not chosen:
            if multi:
                blocked_msgs.append(
                    f'⛔ this job needs {want_type} ({"/".join(target_colors)}) but no free '
                    f'{want_type} spool is registered in the AMS — load it and set the '
                    f"slot's material with Change")
            elif trays:
                chosen = trays[0]
                note = (f'⚠ no {want_type} in {"/".join(target_colors)} loaded — '
                        f'falling back to tray {chosen["tray"]} '
                        f'({color_name(chosen["color"])}); swap spools if that is wrong')
        if note:
            notes.append(note)

        if chosen:
            claimed.add(chosen["tray"])
            mapping[slot_idx] = chosen["tray"]
            clauses.append(f'filament {fid} {want_type} → slot {chosen["slot_1based"]} '
                           f'({color_name(chosen["color"])} {want_type})')
        else:
            clauses.append(f'filament {fid} {want_type} → UNASSIGNED')

    blocked = " | ".join(blocked_msgs) if blocked_msgs else None
    return mapping, clauses, notes, blocked


@_tool
def suggest_mapping(printer: str, filename: str) -> str:
    """Work out which AMS tray(s) a job should use — applying the user's part-color
    conventions — and return the confirm_string start_print requires. The confirm_string is a
    single-use code that expires after 5 minutes and is tied to this printer, this file and this
    tray mapping: relay the plan to the human, get their go-ahead and their statement that the
    bed is clear, then call start_print with it. It cannot be written by hand or reused.
    A read-only key never makes the Hub download anything; a Bambu that is printing is not asked
    for files either (cached information only)."""
    prn = _pick(printer)
    may_download = not security.caller_read_only()
    if _is_octo(prn):
        files = {f["name"]: f for f in prn.files()}
        if filename not in files:
            return f"'{filename}' not on {prn.name}."
        f = files[filename]
        size = f.get("size") or 0
        if may_download:
            try:        # one head download (cached): thumbnail + the printer model the slicer wrote
                entry = _octo_meta_for(prn, filename, size)
            except Exception:
                entry = {}
        else:
            with _cache_lock:
                entry = _cache_load().get(f"{prn.name}|{filename}|{size}") or {}
        mc = _model_check(prn, filename, [entry])
        confirm = START_TICKETS.issue(prn, filename, size, [], "", mc.get("model_match") is False)
        return json.dumps({
            "file": filename, "printer": prn.name, "printer_kind": getattr(prn, "kind", ""),
            "print_time": f.get("print_time"), "weight_g": None,
            "ams_mapping": [], "mapping_described": "the loaded filament (no AMS on this printer)",
            "notes": ["This printer prints with whatever filament is physically loaded — check it."],
            "blocked": None, **mc,
            "confirm_string": confirm, "confirm_expires_in_s": START_TICKET_TTL_S,
            "next_step": _model_next_step(mc, "Ask the human: (1) is the build plate clear? (2) is the right "
                                              "filament loaded? Then call start_print with this confirm_string "
                                              "and bed_clear_confirmed_by_human=true (only once the human said so)."),
        }, indent=2)
    files = {f["name"]: f for f in _sd_list(prn)}
    if filename not in files:
        return f"'{filename}' not on {prn.name}'s SD card."
    size = files[filename]["size"]
    meta = _meta_for(prn, filename, size, may_download)
    if meta is None:
        why = ("a read-only key never makes the Hub download files" if not may_download
               else METADATA_BUSY if not _printer_is_idle(prn) else "it could not be read")
        return (f"METADATA UNAVAILABLE for '{filename}' on {prn.name}: {why}. Nothing was downloaded. "
                "Open the file on the dashboard once the printer is idle (or ask for a control key), "
                "then run suggest_mapping again.")
    trays = [t for t in prn.ams_trays() if t["type"]]
    rule = convention_for(filename)

    mapping, clauses, notes, blocked = _map_filaments(meta.get("filaments", []), trays, rule)
    mapping_described = "\n".join(clauses)
    mc = _model_check(prn, filename, [meta])
    confirm = "" if blocked else START_TICKETS.issue(prn, filename, size, mapping, _ams_fingerprint(prn),
                                                     mc.get("model_match") is False)
    if blocked:
        next_step = f"BLOCKED — {blocked} Resolve that first; do not call start_print."
    else:
        next_step = _model_next_step(mc, "Ask the human: (1) is the build plate clear? (2) approve "
                     f"printing with: {'; '.join(clauses)}? Then call start_print with this confirm_string "
                     "and bed_clear_confirmed_by_human=true (only once the human said so).")
    return json.dumps({
        "file": filename, "printer": prn.name, "printer_kind": getattr(prn, "kind", ""),
        "print_time": meta.get("print_time"), "weight_g": meta.get("weight_g"),
        "ams_mapping": mapping, "mapping_described": mapping_described,
        "notes": notes,
        "blocked": blocked, **mc,
        "confirm_string": confirm, "confirm_expires_in_s": START_TICKET_TTL_S if confirm else None,
        "next_step": next_step,
    }, indent=2)


_NOT_CONFIRMED = ("Run suggest_mapping, relay its plan to the human, and use the confirm_string it returns "
                  "(single use, valid for 5 minutes).")


@_tool
def start_print(printer: str, filename: str, ams_mapping: list[int], confirm: str,
                model_override: bool = False, bed_clear_confirmed_by_human: bool = False) -> str:
    """START A PRINT from the SD card. Needs, in this order: (1) suggest_mapping, whose confirm_string
    is passed back here as `confirm` — a single-use code (valid 5 minutes) tied to this printer, file
    and tray mapping, so it proves the job was described to the human first; (2) the human's go-ahead;
    (3) bed_clear_confirmed_by_human=True, which is THE HUMAN'S statement that the build plate is
    clear — only ever set it after the human told you so in this conversation; never on your own,
    never as a default. Without it, or with a missing / used / expired / mismatching confirm, the
    start is REFUSED (and nothing about the expected value is revealed).
    If suggest_mapping says model_match is false (the file was sliced for a different printer
    model than this one) the start is REFUSED. Only when the human has read that warning and
    explicitly said the file is safe on this printer, pass model_override=True and add
    "|model_override" to the end of the confirm string. Agents must ask the human first;
    never set model_override on your own."""
    return _start_print(printer, filename, ams_mapping, confirm, model_override,
                        bed_clear_confirmed_by_human, allow_remap=False)


def _start_print(printer, filename, ams_mapping, confirm, model_override, bed_clear, allow_remap) -> str:
    """The whole start gate. `allow_remap` is only ever True for the dashboard (a human at the screen picks the
    spool); an agent always has to use the mapping suggest_mapping proposed."""
    prn = _pick(printer)
    if bed_clear is not True:
        return ("REFUSED: bed_clear_confirmed_by_human is not true. That flag is the HUMAN's statement that the "
                "build plate is clear - ask the human, and only when they say it is clear call again with "
                "bed_clear_confirmed_by_human=true.")
    if not isinstance(confirm, str) or not confirm.strip():
        return "REFUSED: no confirm string. " + _NOT_CONFIRMED
    if not isinstance(ams_mapping, list):
        return "REFUSED: ams_mapping must be a list of whole numbers."
    # OctoPrint printers legitimately send an empty mapping (no AMS to map). A Bambu printer never should - an
    # all -1 mapping means suggest_mapping blocked this job.
    if not _is_octo(prn) and not any(isinstance(t, int) and not isinstance(t, bool) and t >= 0 for t in ams_mapping):
        return ("REFUSED: no valid tray in ams_mapping — suggest_mapping blocked this job (see its notes). "
                "Load the missing filament and re-run suggest_mapping.")
    mc = _model_check_cached(prn, filename)
    if mc["model_match"] is False and not model_override:
        return (f"REFUSED: {mc['model_warning']} Starting it needs the human to confirm the file is safe on "
                f"{prn.name}; then call again with model_override=True.")
    nonce, _sep, tag = confirm.strip().partition("|")
    if tag != (modelcheck.MODEL_OVERRIDE_TAG if model_override else ""):
        return ("REFUSED: confirm string mismatch — the model_override flag and the "
                f"\"|{modelcheck.MODEL_OVERRIDE_TAG}\" part of the confirm string must go together. "
                + _NOT_CONFIRMED)
    lock = _start_lock(prn.name)
    if not lock.acquire(blocking=False):
        return f"REFUSED: a start on {prn.name} is already being sent. Wait for it to show up on the printer."
    try:
        return _start_locked(prn, filename, ams_mapping, nonce, allow_remap, model_override)
    finally:
        lock.release()


def _start_locked(prn, filename, ams_mapping, nonce, allow_remap, model_override=False) -> str:
    key = prn.name.lower()
    ticket, why = START_TICKETS.peek(nonce, prn, filename)
    if ticket is None:
        return f"REFUSED: confirm string not accepted — {why}. " + _NOT_CONFIRMED
    if ticket.get("model_mismatch") and not model_override:
        # the file was described as sliced for another printer model; the cached model check may be gone by now,
        # but the ticket remembers - without the human's override it never starts
        return (f"REFUSED: suggest_mapping said this file was sliced for a different printer model than "
                f"{prn.name}. Starting it needs the human to confirm the file is safe on {prn.name}; then call "
                "again with model_override=True.")
    last = _START_PENDING.get(key)
    if last is not None and time.monotonic() - last < START_PENDING_S:
        return (f"REFUSED: a start was sent to {prn.name} {int(time.monotonic() - last)} s ago and may still be "
                "settling. Check printer_status; if it did not start, try again in a minute.")
    s = prn.snapshot()
    if not s.get("connected") or s["state"] in ("UNKNOWN", "DISCONNECTED"):
        return (f"REFUSED: {prn.name} is not connected (state {s['state']}). "
                "Connect the printer first, then retry.")
    if s["state"] not in controls.IDLE_STATES or s.get("fault"):          # whitelist: anything unrecognised is "busy"
        if s["state"] in controls.BUSY_STATES:
            return f"REFUSED: {prn.name} is {s['state']} on '{s.get('job')}'. Cancel or wait first."
        return f"REFUSED: {prn.name} is not ready (state {s['state']}{', reporting a fault' if s.get('fault') else ''}). Check the printer first."
    problem = _mapping_problem(ticket["mapping"], ams_mapping, prn, allow_remap)
    if problem:
        return f"REFUSED: {problem}. " + _NOT_CONFIRMED
    if not _is_octo(prn) and _ams_fingerprint(prn) != ticket["ams_fp"]:
        return ("REFUSED: what is loaded in the AMS changed since suggest_mapping ran. Run suggest_mapping again "
                "so the human sees the current spools.")
    try:                                   # the file must still be the one that was described
        listing = prn.files() if _is_octo(prn) else _sd_list(prn)
    except Exception as e:
        return f"REFUSED: could not re-read {prn.name}'s file list ({e}); nothing was started."
    entry = next((f for f in listing if f.get("name") == filename), None)
    if entry is None:
        return f"REFUSED: '{filename}' is no longer on {prn.name}. Run suggest_mapping again."
    if (entry.get("size") or 0) != (ticket["size"] or 0):
        return f"REFUSED: '{filename}' on {prn.name} changed since suggest_mapping ran. Run it again."
    if not START_TICKETS.consume(nonce):    # single use: a parallel request got there first
        return "REFUSED: confirm string not accepted — it was already used. " + _NOT_CONFIRMED
    try:
        prn.start_print(filename, ams_mapping)
    except Exception as e:
        return f"REFUSED: {e}"
    _START_PENDING[key] = time.monotonic()
    _RECENT_STARTS[key] = {"key": costs.file_key(filename), "mapping": list(ams_mapping), "ts": time.time()}
    time.sleep(8)
    s2 = prn.snapshot()
    if s2.get("state") in controls.BUSY_STATES:
        _START_PENDING.pop(key, None)       # it took: the busy state guards the printer from here on
    return json.dumps({"sent": True, "state_after_8s": s2["state"],
                       "job": s2["job"],
                       "note": "PREPARE/RUNNING = accepted. If state unchanged, the printer "
                               "may not be in LAN mode (P1S requires it) or is settling — "
                               "check printer_status in 30s."}, indent=2)


@_tool
def connect_printer(printer: str) -> str:
    """Reconnect a printer's host to its controller: OctoPrint opens its serial
    connection (saved port/baud); Moonraker issues a Klipper FIRMWARE_RESTART.
    Bambu and PrusaLink printers connect on their own and don't need this."""
    prn = _pick(printer)
    if not hasattr(prn, "connect_serial"):
        return f"{prn.name}: not applicable — this printer type connects automatically."
    try:
        return prn.connect_serial()
    except Exception as e:
        return f"REFUSED: {e}"


@_tool
def cancel_print(printer: str) -> str:
    """Cancel the current job. (Post-cancel state reads FAILED — that is Bambu's
    normal 'aborted by user', not an error.)"""
    prn = _pick(printer)
    try:
        prn.stop()
    except PrinterError as e:                       # e.g. a Bambu whose MQTT link is down: NOT sent
        return f"FAILED: {e}"
    _START_PENDING.pop(prn.name.lower(), None)      # a cancelled start can be followed by a new one
    time.sleep(4)
    return f"stop sent — state now: {prn.snapshot()['state']}"


@_tool
def pause_print(printer: str) -> str:
    """Pause the current job."""
    try:
        _pick(printer).pause()
    except PrinterError as e:
        return f"FAILED: {e}"
    return "pause sent"


@_tool
def resume_print(printer: str) -> str:
    """Resume a paused job."""
    try:
        _pick(printer).resume()
    except PrinterError as e:
        return f"FAILED: {e}"
    return "resume sent"


# ------------------------------------------------------------- printer controls
# light / pre-heat / cool-down / unload / load, for every printer family that supports them.
# Logic and safety rules live in controls.py; drivers expose control_caps() + primitives.
# _control() is shared by the MCP tools and the REST routes: -> (http_status, json_payload).

def _control(printer: str, action: str, **kw) -> tuple:
    try:
        prn = _pick(printer)
    except ValueError as e:
        return 404, {"error": str(e)}
    try:
        return 200, controls.perform(prn, action, **kw)
    except controls.ControlError as e:
        tag = {"refused": "REFUSED", "unsupported": "UNSUPPORTED", "invalid": "INVALID"}[e.code]
        return e.status, {"error": f"{tag}: {e}", "code": e.code, "reason": str(e)}
    except Exception as e:      # the printer / its host failed (PrinterError, MQTT not connected, ...)
        return 502, {"error": f"FAILED: {e}", "code": "failed", "reason": str(e)}


def _control_text(printer: str, action: str, **kw) -> str:
    status, payload = _control(printer, action, **kw)
    if status == 404:
        raise ValueError(payload["error"])      # same as every other tool: unknown printer
    return payload["message"] if status == 200 else payload["error"]


@_tool
def printer_controls(printer: str) -> str:
    """What this printer's controls can do RIGHT NOW (JSON): light (+ on/off state), preheat
    (+ material presets, limits, seconds until the automatic cool-down), cooldown, unload,
    load. Each entry has `supported` (can this printer do it at all; if not, `reason` says why),
    `allowed_now` (false while printing/paused/busy, with `blocked` saying why) and optional
    `note` / `verified: false` where the command is written from documentation and has not been
    tried on real hardware. `actions` lists everything supported. Call this before offering a
    control to the human."""
    return json.dumps(controls.describe(_pick(printer)), indent=2)


@_tool
def set_light(printer: str, on: bool) -> str:
    """Turn the printer's light on or off. Always allowed (even mid-print). Bambu: chamber light;
    Klipper: any output_pin/led/neopixel whose name contains light, led or lamp. OctoPrint and
    PrusaLink have no light API and answer UNSUPPORTED with the reason."""
    return _control_text(printer, "light_on" if on else "light_off")


@_tool
def chamber_light(printer: str, on: bool) -> str:
    """Turn the chamber light on/off (older name for set_light)."""
    return set_light(printer, on)


@_tool
def preheat(printer: str, preset: str = None, nozzle: int = None, bed: int = None) -> str:
    """Pre-heat the nozzle and bed so a print or filament change can start warm. Give a material
    `preset` (PLA, PETG, ABS, TPU by default; see printer_controls for the configured list) and/or
    explicit `nozzle` / `bed` temperatures in C (explicit values override the preset; nozzle max 300,
    bed max 120, anything higher is rejected). REFUSED while the printer is printing, preparing or
    paused. The Hub switches the heaters off again after the auto-off time (Settings > Printers & safety,
    env PREHEAT_TIMEOUT_MIN, default 15 min) unless a print starts. The auto-off cannot be disabled (1-240 min);
    `cooldown` switches the heaters off sooner. Calling it again restarts that timer."""
    return _control_text(printer, "preheat", preset=preset, nozzle=nozzle, bed=bed)


@_tool
def cooldown(printer: str) -> str:
    """Switch the nozzle and bed heaters off and cancel the pending pre-heat auto-off. REFUSED
    while the printer is printing, preparing or paused (heaters are not touched mid-print)."""
    return _control_text(printer, "cooldown")


@_tool
def unload_filament(printer: str) -> str:
    """Unload the filament (the printer heats the nozzle itself). Bambu: AMS printers only;
    Klipper: only if the printer.cfg has an UNLOAD_FILAMENT macro; OctoPrint: Prusa M702;
    PrusaLink: unsupported. REFUSED while printing/paused. Ask the human first - it moves the
    extruder and the hot nozzle."""
    return _control_text(printer, "unload")


@_tool
def load_filament(printer: str, tray: int = None) -> str:
    """Load filament (the printer heats the nozzle itself). Bambu: AMS only, `tray` is the 1-based
    AMS slot (optional when exactly one slot holds filament); Klipper: needs a LOAD_FILAMENT macro;
    OctoPrint: Prusa M701; PrusaLink: unsupported. REFUSED while printing/paused."""
    return _control_text(printer, "load", tray=tray)


CAMERA_WAIT_SECONDS = 8.0   # how long a snapshot waits for the first frame


@_tool
async def get_camera_snapshot(printer: str):
    """Current chamber-camera picture (JPEG) for a printer — use it to check on
    a print: spaghetti, a detached part, an empty plate. Works for Bambu P1/A1-series
    printers and for OctoPrint printers with a webcam; others answer with an
    error. The first call wakes the camera (a couple of seconds); the server keeps
    ONE shared camera connection/poller per printer and stops it ~30 s after the
    last request."""
    prn = _pick(printer)
    if not _has_camera(prn):
        raise ValueError(f"{prn.name} has no camera available through this server.")
    got = await prn.camera().aget_frame(CAMERA_WAIT_SECONDS)
    if got is None:
        raise ValueError(f"No camera frame from {prn.name} within {CAMERA_WAIT_SECONDS:.0f} s — "
                         "is the printer on and reachable?")
    jpeg, _ts = got
    if McpImage is not None:
        return McpImage(data=jpeg, format="jpeg")
    return base64.b64encode(jpeg).decode()   # fallback: base64 JPEG as text


TRAY_LOG = os.path.join(os.path.dirname(CACHE_PATH), "tray-changes.log")


def _log_tray(entry: dict) -> None:
    """One JSON line per tray change, to stdout AND a file on the /data volume —
    so the record survives container restarts (a restart wiped the evidence
    for the 9/23 relabel that silently didn't stick)."""
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
    line = json.dumps(entry)
    print("TRAY " + line, flush=True)
    try:
        with open(TRAY_LOG, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"TRAY log write failed: {e}", flush=True)


def _tray_label(t: dict) -> str:
    if not t or not t.get("tray_type"):
        return "EMPTY/unlabelled"
    return f'{color_name("#" + t.get("tray_color", "")[:6])} {t["tray_type"]}'


def set_tray_verified(printer: str, tray_id: int, material: str, color: str) -> dict:
    """Relabel an AMS tray and CONFIRM the printer took it: send, then watch the
    printer's own report until type + color + profile match; resend once if
    not. Returns {"ok": bool, "message": str}. Never reports success on faith."""
    prn = _pick(printer)
    if _is_octo(prn):
        return {"ok": False, "message": f"{printer} has no AMS trays to set."}
    name = color.upper()
    if name not in PALETTE:
        return {"ok": False, "message": f"Unknown color '{color}'. Palette: {', '.join(PALETTE)}"}
    mat = material.upper()
    if mat not in MATERIALS:
        return {"ok": False, "message": f"Unknown material '{material}'. Materials: {', '.join(MATERIALS)}"}
    slot = f"slot {tray_id + 1}"
    if not prn.snapshot().get("connected"):
        msg = f"{printer} is not connected to the Hub right now, so {slot} was NOT changed. Try again in a minute."
        _log_tray({"printer": printer, "slot": tray_id + 1, "want": f"{name} {mat}", "ok": False, "why": "not connected"})
        return {"ok": False, "message": msg}

    info_idx, temp_min, temp_max = MATERIALS[mat]
    want_hex = PALETTE[name][:6]
    before = _tray_label(prn.tray_raw(tray_id))
    state = prn.snapshot().get("state")

    def matches():
        t = prn.tray_raw(tray_id)
        return (t.get("tray_type", "").upper() == mat
                and t.get("tray_color", "")[:6].upper() == want_hex
                and t.get("tray_info_idx") == info_idx)

    t0 = time.time()
    ok, attempts = False, 0
    for attempt in (1, 2):
        attempts = attempt
        prn.set_tray(tray_id, mat, PALETTE[name],
                     temp_min=temp_min, temp_max=temp_max, info_idx=info_idx)
        if attempt == 2:
            prn.request({"pushing": {"sequence_id": str(int(time.time())),
                                     "command": "pushall", "version": 1, "push_target": 1}})
        deadline = time.time() + 12
        while time.time() < deadline:
            if matches():
                ok = True
                break
            time.sleep(0.5)
        if ok:
            break

    after = _tray_label(prn.tray_raw(tray_id))
    secs = round(time.time() - t0, 1)
    _log_tray({"printer": printer, "slot": tray_id + 1, "want": f"{name} {mat}",
               "before": before, "after": after, "ok": ok, "attempts": attempts,
               "seconds": secs, "printer_state": state})
    if ok:
        return {"ok": True, "message": f"{printer} {slot} is now {name} {mat} — confirmed by the printer."}
    return {"ok": False, "message": (f"{printer} did NOT take the change. {slot} still reads {after} "
                                     f"(sent twice, waited {secs:.0f} s; printer was {state}). "
                                     f"Try again, or set it on the printer's screen.")}


@_tool
def set_tray(printer: str, tray_id: int, material: str, color: str) -> str:
    """Register a tray after a spool swap. `material` is PLA/PETG/ABS/ASA/TPU;
    `color` is a palette NAME (GREEN, ORANGE, RED, BLUE, BLACK, WHITE...) — the
    server supplies the exact hex and the material's generic temp profile;
    nobody ever has to pick or verify a hue by eye. Waits for the printer to
    confirm the new label and says plainly if it did not stick."""
    res = set_tray_verified(printer, tray_id, material, color)
    return ("OK — " if res["ok"] else "FAILED — ") + res["message"] + "\n" + ams_state(printer)


@_tool
def delete_sd_file(printer: str, filename: str, confirm: str) -> str:
    """Delete a file from the SD card. confirm must equal the filename.
    Refuses while a job is active, and if filename isn't on the printer's
    current file list (guards against typos deleting the wrong/no file)."""
    if confirm != filename:
        return "REFUSED: confirm must equal filename exactly."
    prn = _pick(printer)
    s = prn.snapshot()
    if not s.get("connected", True) or s.get("state") not in controls.IDLE_STATES or s.get("fault"):
        return (f"REFUSED: {prn.name} is {s.get('state')} — delete is only allowed when the printer is connected "
                "and idle (not while a job is active, or when its state is unknown)")
    if _is_octo(prn):
        files = prn.files()
        if filename not in {f["name"] for f in files}:
            return f"REFUSED: '{filename}' is not on {prn.name}'s current file list."
        prn.sd_delete(filename)
    else:
        with _ftps_lock(prn.name):                  # list + delete in ONE FTPS lock hold
            if filename not in {f["name"] for f in prn.sd_list()}:
                return f"REFUSED: '{filename}' is not on {prn.name}'s current file list."
            prn.sd_delete(filename)
    _cache_purge(prn.name, filename)
    return f"deleted {filename}"


# ------------------------------------------------------------- dashboard web UI
# Same brain, second face: a touch dashboard served from this container.
# All write paths reuse the exact gates the MCP tools enforce.

from starlette.requests import Request
from starlette.responses import (HTMLResponse, JSONResponse, Response,
                                 StreamingResponse)

_DASH_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")
# thumb basenames are always sha1-of-cache-key + ".png" (see _save_thumb) —
# enforce that shape so the path param can never walk out of THUMBS_DIR.
_THUMB_NAME_RE = re.compile(r"^[0-9a-f]{40}\.png$")


async def _run(fn, *a, **kw):
    """Run a synchronous (blocking) printer call off the event loop, so a slow
    or stuck printer call (e.g. the 8s settle in start_print) doesn't freeze
    fleet polling / other requests."""
    return await asyncio.to_thread(fn, *a, **kw)


def _request_identity(request):
    """Who sent this request: the Identity the middleware decided, else (key check Off) what the dashboard
    cookies say, else None. Only key / cookie identities count."""
    ident = request.scope.get("auth")
    if ident is None or ident.kind == "none":
        try:
            ident = AUTH.cookie_identity(dict(request.headers), request.method)
        except Exception:
            ident = None
    return ident if ident is not None and ident.kind in ("key", "cookie") else None


def _is_dashboard_admin(request) -> bool:
    """May this request do admin things (keys, the key check, sign-in, devices, security.* settings)? Only a
    dashboard browser that has typed the ADMIN PASSWORD (or, with no admin password but a dashboard password, a
    full-access sign-in). An API key - even a control key - never; the no-password cookie alone never."""
    ident = _request_identity(request)
    return AUTH.admin_verdict(dict(request.headers), ident) == "ok"


def _admin_refused(request, what="This"):
    """The 403 for a request that wanted admin rights and does not have them, in words (unset / locked / key)."""
    ident = _request_identity(request)
    verdict = AUTH.admin_verdict(dict(request.headers), ident)
    if verdict == "unset":
        return JSONResponse({"error": security.ADMIN_UNSET_MSG, "admin": "unset", "required_scope": "admin"}, status_code=403)
    if verdict == "locked":
        return JSONResponse({"error": security.ADMIN_LOCKED_MSG, "admin": "locked", "required_scope": "admin"}, status_code=403)
    return JSONResponse({"error": f"{what} can only be changed from the dashboard (Settings > Security) after "
                                  "unlocking it with the admin password - not with an API key.",
                         "required_scope": "admin"}, status_code=403)


def _safe_route(fn):
    """Wrap a custom_route handler so ANY exception inside it becomes a JSON
    error body with a 500 status — never a bare/non-JSON error page. The
    dashboard's api() helper always gets JSON back, even on a crash.
    Also makes the caller's scope visible to the handler (and the threads it starts) through
    security.CALLER_SCOPE, which is what redaction and 'a read key never downloads' look at."""
    async def wrapper(request):
        ident = _request_identity(request)
        token = security.CALLER_SCOPE.set(ident.scope if ident is not None else None)
        try:
            return await fn(request)
        except Exception as e:
            return JSONResponse({"error": security.scrub_text(f"{type(e).__name__}: {e}")}, status_code=500)
        finally:
            security.CALLER_SCOPE.reset(token)
    wrapper.__name__ = getattr(fn, "__name__", "route")
    return wrapper


# ============================================================= file backup
# Zip every file a printer lists into /data/backups/<printer>/ (see backup.py).
# Self-contained block: MCP tools + REST routes, nothing else in this file
# depends on it.

import backup as _backup
from starlette.responses import FileResponse

_BACKUPS = _backup.BackupManager(
    PRINTERS, root=os.path.join(os.path.dirname(CACHE_PATH), "backups"),
    ftps_lock=_ftps_lock,
    keep=lambda: SETTINGS.get("backups.keep_per_printer"),     # Settings > env BACKUP_KEEP > 5
    on_finish=_backup_finished)


@_tool
def backup_printer_files(printer: str) -> str:
    """Back up every file on a printer's file list (Bambu SD-card .3mf jobs, or
    OctoPrint gcode) into one zip on the Hub: /data/backups/<printer>/
    <printer>-YYYYMMDD-HHMMSS.zip, with a manifest.json (sizes, sha256, failures).
    `printer` is a printer name, or "all" to back up every printer one after
    another. Returns immediately with a job id; the copy runs in the background
    (minutes for a full SD card) — call backup_status to watch progress.
    Bambu printers REFUSE while printing/paused (one FTPS session; same rule as
    metadata refresh) — retry when idle. OctoPrint is allowed mid-print. Only one
    backup per printer at a time. Keeps the newest N zips (Settings > Backups, env BACKUP_KEEP, default 5)."""
    try:
        return json.dumps(_BACKUPS.start(printer), indent=2)
    except _backup.BackupError as e:
        return f"REFUSED: {e}"


@_tool
def backup_status() -> str:
    """Backup jobs and finished backups. `running` = queued/in-progress jobs with
    files_done/files_total and bytes; `recent` = last finished jobs (status done,
    partial = zip written but some files failed — see failed_files, or failed
    with the reason in `error`); `backups` = per printer, newest first, each
    with its REST `download` path (GET it on the Hub's web port to fetch the zip)."""
    return json.dumps(_BACKUPS.status(), indent=2)


def _backup_error(e):
    return JSONResponse({"error": str(e)}, status_code=getattr(e, "status", 400))


@mcp.custom_route("/api/printer/{name}/backup", methods=["POST"])
@_safe_route
async def _api_backup_start(request: Request):
    try:
        res = await _run(_BACKUPS.start, request.path_params["name"])
    except _backup.BackupError as e:
        return _backup_error(e)
    return JSONResponse(res, status_code=202)


@mcp.custom_route("/api/backups", methods=["GET"])
@_safe_route
async def _api_backups(request: Request):
    return JSONResponse(await _run(_BACKUPS.status))


@mcp.custom_route("/api/backups/{printer}/{filename}", methods=["GET"])
@_safe_route
async def _api_backup_download(request: Request):
    path = await _run(_BACKUPS.resolve_download,
                      request.path_params["printer"], request.path_params["filename"])
    if not path:
        return JSONResponse({"error": "no such backup"}, status_code=404)
    return FileResponse(path, media_type="application/zip",
                        filename=os.path.basename(path))

# ============================================================= end file backup


# ==== library ====
# Global File Library: one file store on the Hub for the whole fleet (see library.py and docs/library.md).
# Everything that touches a printer is here, and only through the guards the printer pages already use:
#   * no FTPS session on a Bambu unless it is positively idle (_printer_is_idle + the driver's own _require_idle),
#   * the printer's FTPS lock around every session,
#   * sending a file NEVER starts a print - the print itself stays in suggest_mapping / start_print (ticket,
#     bed-clear confirm, spool mapping). Nothing in this block calls start_print.

import library as _library
from bambu import PrinterBusy

LIB = _library.Library(os.path.join(DATA_DIR, "library"))
LIB.sweep_incoming()
LIBRARY_MAX_FILES_PER_SEND = 20
LIBRARY_MAX_PRINTERS_PER_SEND = 12


def _library_error(e):
    return JSONResponse({"error": str(e)}, status_code=getattr(e, "status", 400))


def _library_printer_files() -> dict:
    """{printer name: [{name, size, meta, thumb}]} from listings the Hub already holds. NO printer traffic: a Bambu's
    last idle listing (memory / disk), a file-based printer's last live listing, plus the metadata cache."""
    with _cache_lock:
        cache = _cache_load()
    out = {}
    for prn in list(PRINTERS.values()):
        try:
            if _is_octo(prn):
                rows = list((_FILES_SEEN.get(prn.name.lower()) or {}).values())
            elif hasattr(prn, "sd_listing_cached"):
                rows = prn.sd_listing_cached()
            else:
                rows = []
        except Exception:
            rows = []
        files = []
        for f in rows:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            meta = cache.get(f"{prn.name}|{f['name']}|{f.get('size') or 0}")
            files.append({"name": f["name"], "size": int(f.get("size") or 0), "meta": meta if isinstance(meta, dict) else None,
                          "thumb": (meta or {}).get("thumb") if isinstance(meta, dict) else None})
        out[prn.name] = files
    return out


def _library_printers_info() -> list:
    out = []
    for prn in list(PRINTERS.values()):
        try:
            s = prn.snapshot()
            idle = _printer_is_idle(prn)
        except Exception:
            s, idle = {}, False
        out.append({"name": prn.name, "kind": getattr(prn, "kind", ""), "state": s.get("state"),
                    "idle": bool(idle),
                    "can_receive": (hasattr(prn, "upload") if _is_octo(prn) else hasattr(prn, "sd_upload")),   # ==== rename ==== (upload for HTTP printers is beta)
                    "beta": bool(_is_octo(prn)),
                    "model": modelcheck.check("", modelcheck.target_model_for(prn))["target_model"]})
    return out


def _library_rows(query="", folder="", hide_printer_only=False) -> list:
    rows = _library.merge_rows(LIB.entries(), _library_printer_files(), color_name=color_name)
    return _library.filter_rows(rows, query, folder, hide_printer_only)


def _library_snapshot(query="", folder="", hide_printer_only=False) -> dict:
    rows = _library_rows(query, folder, hide_printer_only)
    allrows = rows if not (query or folder or hide_printer_only) else _library_rows()
    return {"rows": rows, "folders": LIB.folders(), "printers": _library_printers_info(),
            "limits": {"max_file_mb": LIB.max_bytes // 1048576},
            "counts": {"library": sum(1 for r in allrows if r["in_library"]),
                       "printer_only": sum(1 for r in allrows if not r["in_library"])}}


def _library_seed_meta(prn, entry: dict):
    """After a file was sent to a Bambu: put what the library already knows about it into the metadata cache under the
    printer's own key, so the start-print confirm does not have to download the file back off the printer."""
    meta = entry.get("meta") or {}
    if entry.get("kind") != "3mf" or meta.get("error"):
        return
    key = f"{prn.name}|{entry['name']}|{entry['size']}"
    with _cache_lock:
        cache = _cache_load()
        if key in cache:
            return
    png = None
    tp = LIB.thumb_path(entry.get("thumb") or "")
    if tp:
        try:
            with open(tp, "rb") as fh:
                png = fh.read()
        except OSError:
            png = None
    seeded = {k: meta.get(k) for k in ("printer_model_id", "printer_model", "print_seconds", "print_time", "weight_g",
                                       "filaments", "plates", "parts")}
    seeded["thumb"] = _save_thumb(key, png)
    with _cache_lock:
        cache = _cache_load()
        cache.setdefault(key, seeded)
        _cache_save(cache)


def _library_send_one(entry: dict, prn, model_override: bool) -> dict:
    """Upload one library file to one printer. Refuses (never forces) a printer that is not positively idle, a printer
    type the Hub cannot upload to, a file sliced for another model (unless the human overrode), and a name that
    is already taken on the card by a different file. Returns a result row; never raises."""
    name = entry["name"]
    r = {"name": name, "printer": prn.name, "ok": False, "status": "refused", "code": "", "reason": ""}

    def refuse(code, reason):
        r.update(status="refused", code=code, reason=reason)
        return r
    http_up = _is_octo(prn) and hasattr(prn, "upload")          # ==== rename ==== OctoPrint / PrusaLink / Klipper upload: beta
    if not (http_up or (not _is_octo(prn) and hasattr(prn, "sd_upload"))):
        return refuse("unsupported", f"{prn.name} cannot receive files from the Hub (Bambu LAN printers take .3mf; OctoPrint, PrusaLink and Klipper take .gcode / .bgcode, in beta).")
    if http_up:
        if entry.get("kind") not in ("gcode", "bgcode"):
            return refuse("wrong_type", f"{prn.name} prints .gcode / .bgcode files; '{name}' is a {entry.get('kind') or 'different'} file.")
        r["note"] = "Sending to this printer type is " + BETA_NOTE + "."
    elif entry.get("kind") != "3mf":
        return refuse("wrong_type", f"{prn.name} prints .3mf / .gcode.3mf files; '{name}' is a {entry.get('kind') or 'different'} file.")
    mc = modelcheck.check(_library.sliced_for_name(entry.get("meta")), modelcheck.target_model_for(prn))
    r["model_match"], r["sliced_for"], r["target_model"] = mc["model_match"], mc["sliced_for"], mc["target_model"]
    if mc["model_match"] is False and not model_override:
        return refuse("model_mismatch", mc["model_warning"] + " It was not sent.")
    if mc["model_match"] == "unknown":
        r["note"] = (r.get("note", "") + " " + mc["model_warning"]).strip()
    if not _printer_is_idle(prn):
        return refuse("busy", f"{prn.name} is busy (printing, paused, preparing or not connected). Nothing was sent.")
    try:
        path = LIB.file_path(name)
        with (_octo_lock(prn.name) if http_up else _ftps_lock(prn.name)):
            if not _printer_is_idle(prn):               # a print may have started while we waited for the lock
                return refuse("busy", f"{prn.name} became busy. Nothing was sent.")
            if http_up:
                # Never fall back to the cached listing before a write: OctoPrint / Klipper uploads REPLACE a file of the same
                # name, so without a live listing we cannot know the name is free. (Bambu: the driver's own idle-only FTPS list.)
                try:
                    rows = [dict(f) for f in _backup._files_for_backup(prn) if f.get("origin", "local") == "local"]
                except Exception as e:
                    return refuse("list_failed", f"Could not read {prn.name}'s file list right now ({security.scrub_text(str(e))[:120]}), "
                                                 "so the Hub cannot tell whether the name is free. Nothing was sent.")
            else:
                rows = [dict(f) for f in prn.sd_list()]                       # the driver refuses (no FTPS) if it is not idle
            # Names are compared case-insensitively for EVERY driver: SD cards (FAT/exFAT) and USB sticks ignore case, so a
            # same-letters name in another case would be silently replaced.
            low = name.lower()
            same = [f for f in rows if str(f.get("name") or "").lower() == low]
            exact = next((f for f in same if f.get("name") == name), None)
            if exact is not None and len(same) == 1 and int(exact.get("size") or 0) == entry["size"]:
                r.update(ok=True, status="already_there", reason=f"'{name}' is already on {prn.name}.")
                return r
            if same:
                other = same[0].get("name")
                return refuse("name_taken", f"A different file called '{other}' is already on {prn.name} (names are compared ignoring upper/lower case). "
                                            "Delete or rename it on the printer's page first - the Hub never overwrites a file there.")
            with open(path, "rb") as fh:
                data = fh.read()
            (prn.upload if http_up else prn.sd_upload)(name, data)       # the driver checks "positively idle" again before it connects
    except PrinterBusy as e:
        return refuse("busy", str(e))
    except _library.LibraryError as e:
        return refuse("missing", str(e))
    except Exception as e:
        r.update(status="failed", code="failed", reason=security.scrub_text(f"Sending to {prn.name} failed: {e}"))
        return r
    try:
        _library_seed_meta(prn, entry)
    except Exception:
        pass
    print(f"[library] sent {name} to {prn.name}", flush=True)
    r.update(ok=True, status="sent", reason=f"Sent to {prn.name}. It is on the printer's file list; nothing was started.",
             next_step="To print it: open the printer's file list and tap Print (or suggest_mapping, then ask the human). "
                       "The bed-clear confirm and the spool mapping are still required.")
    return r


def _library_send(names, printers, model_override=False) -> list:
    if isinstance(names, str):
        names = [names]
    if isinstance(printers, str):
        printers = [printers]
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise _library.LibraryError("Say which library file(s) to send.")
    if not isinstance(printers, list) or not printers or not all(isinstance(n, str) for n in printers):
        raise _library.LibraryError("Say which printer(s) to send to.")
    if len(names) > LIBRARY_MAX_FILES_PER_SEND or len(printers) > LIBRARY_MAX_PRINTERS_PER_SEND:
        raise _library.LibraryError("Too many files or printers in one request.")
    entries = [LIB.get(n) for n in dict.fromkeys(names)]                  # NotFound before anything is sent
    targets = []
    for pn in dict.fromkeys(printers):
        prn = PRINTERS.get(pn.lower())
        if prn is None:
            raise _library.NotFound(f"Unknown printer '{pn}'.")
        targets.append(prn)
    out = []
    for prn in targets:
        for e in entries:
            out.append(_library_send_one(e, prn, model_override is True))
    return out


def _sd_download_capped(prn, filename, cap):
    """A Bambu SD download that stops reading at `cap` bytes (the driver raises PrinterError). A test double without the
    keyword is called the plain way."""
    try:
        takes_cap = "max_bytes" in inspect.signature(prn.sd_download).parameters
    except (TypeError, ValueError):
        takes_cap = False
    return prn.sd_download(filename, max_bytes=cap) if takes_cap else prn.sd_download(filename)


def _library_save(printer: str, filename: str, folder: str = "", replace: bool = False) -> dict:
    """Copy one file off a printer into the library. A Bambu is only asked when it is positively idle (and its
    listing is read under the FTPS lock); a PrusaLink printer follows the backup rule (idle only); OctoPrint and
    Moonraker read the host's own storage and are fine mid-print."""
    prn = _pick(printer)
    if not isinstance(filename, str) or not filename.strip():
        raise _library.InvalidName("Say which file on the printer to save.")
    clean = _library.name_from_printer_path(filename)
    folder = _library.sanitize_folder(folder)
    LIB.check_can_add(clean, replace)
    cap = LIB.max_bytes
    src = f"printer:{prn.name}"
    if _is_octo(prn):
        reason = _backup.BackupManager._busy_reason(prn)       # the backup rules: which drivers may be read mid-print
        if reason:
            raise _library.LibraryError(reason)
        row = next((f for f in prn.files() if f.get("name") == filename), None)
        if row is None:
            raise _library.NotFound(f"'{filename}' is not on {prn.name}'s file list.")
        if row.get("origin", "local") != "local":
            raise _library.LibraryError(f"'{filename}' is stored on '{row.get('origin')}', not downloadable.")
        if int(row.get("size") or 0) > cap:
            raise _library.TooLarge(f"The file is larger than the {cap // 1048576} MB limit for one library file.")
        LIB.ensure_dirs()
        try:
            if getattr(prn, "kind", "") == "octoprint":
                resp = prn._get_raw("/downloads/files/local/" + urllib.parse.quote(filename, safe="/"), timeout=30)
            else:
                resp = prn.open_file(filename, timeout=30)
            tmp, _none, size, sha = _backup._stream_to_temp(resp, LIB.incoming, max_bytes=cap)
        except _backup.StreamTooLarge:
            raise _library.TooLarge(f"The file is larger than the {cap // 1048576} MB limit for one library file.")
        except PrinterBusy as e:
            raise _library.LibraryError(str(e))
        except _library.LibraryError:
            raise
        except Exception as e:
            raise _library.LibraryError(security.scrub_text(f"Could not download '{filename}' from {prn.name}: {e}"))
        return LIB.add_streamed_file(tmp, clean, sha, size, folder, src, replace)
    if not _printer_is_idle(prn):
        raise _library.LibraryError(f"{prn.name} is busy (printing, paused, preparing or not connected). The Hub does not "
                                    "read a printer's SD card then; try again when it is idle.")
    with _ftps_lock(prn.name):
        if not _printer_is_idle(prn):
            raise _library.LibraryError(f"{prn.name} became busy. Nothing was downloaded.")
        listing = {f["name"]: f for f in prn.sd_list()}
        if filename not in listing:
            raise _library.NotFound(f"'{filename}' is not on {prn.name}'s SD card.")
        if int(listing[filename].get("size") or 0) > cap:
            raise _library.TooLarge(f"The file is larger than the {cap // 1048576} MB limit for one library file.")
        try:
            data = _sd_download_capped(prn, filename, cap)
        except PrinterBusy as e:
            raise _library.LibraryError(str(e))
        except PrinterError as e:
            if "limit" in str(e):
                raise _library.TooLarge(f"The file is larger than the {cap // 1048576} MB limit for one library file.")
            raise _library.LibraryError(security.scrub_text(f"Could not download '{filename}' from {prn.name}: {e}"))
        if len(data) > cap:
            raise _library.TooLarge(f"The file is larger than the {cap // 1048576} MB limit for one library file.")
    inc = LIB.begin_incoming()
    inc.write(data)
    return LIB.ingest(inc, clean, folder, src, replace)


def _library_scan() -> list:
    """Re-read what the printers hold: HTTP printers are listed live; a Bambu only when positively idle (one FTPS
    listing under its lock) - a busy one keeps the last listing the Hub saw. Then the metadata pre-warm (the same
    one opening a printer's page starts) fills in details and previews of idle printers, in the background."""
    def one(prn):
        try:
            if _is_octo(prn):
                files = prn.files()
                _remember_files(prn, files)
                return {"printer": prn.name, "ok": True, "files": len(files)}
            if not _printer_is_idle(prn):
                return {"printer": prn.name, "ok": True, "skipped": "busy - showing the last list the Hub saw"}
            return {"printer": prn.name, "ok": True, "files": len(_sd_list(prn))}
        except PrinterBusy as e:
            return {"printer": prn.name, "ok": True, "skipped": str(e)}
        except Exception as e:
            return {"printer": prn.name, "ok": False, "error": security.scrub_text(f"{type(e).__name__}: {e}")[:200]}
    prns = list(PRINTERS.values())
    if not prns:
        return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(prns))) as ex:
        res = list(ex.map(one, prns))
    for prn in prns:
        threading.Thread(target=_prewarm, args=(prn.name,), daemon=True).start()
    return res


def _library_public(entry: dict) -> dict:
    """An index entry as the API shows it (no disk paths)."""
    rows = [r for r in _library_rows() if r["in_library"] and r["name"] == entry["name"]]
    return rows[0] if rows else {"name": entry["name"]}


# ---- MCP tools (an agent can look, save and send; it can never start a print from here)

def _library_words(rows) -> list:
    out = []
    for r in rows:
        out.append({"name": r["name"], "size_mb": round(r["size"] / 1048576, 2), "folder": r["folder"] or None,
                    "in_library": r["in_library"], "on_printers": r["printers"], "print_time": r["print_time"],
                    "weight_g": r["weight_g"], "sliced_for": r["sliced_for"],
                    "filaments": [f"{f['color_name']} {f['type']}" + (f" {round(f['used_g'])} g" if f.get("used_g") else "")
                                  for f in r["filaments"]]})
    return out


@_tool
def library_list(query: str = "", folder: str = "") -> str:
    """List the Hub's file library: files stored on the Hub plus the files the printers are known to hold (from cached
    listings - this never contacts a printer). `query` matches the file name, folder, printer, 'sliced for' model and
    filament colour/type words; `folder` limits to one library folder. Each row says whether the file is
    `in_library` and which printers it is `on_printers`. Colours are WORDS."""
    try:
        rows = _library_rows(query, _library.sanitize_folder(folder))
    except _library.LibraryError as e:
        return f"REFUSED: {e}"
    return json.dumps({"files": _library_words(rows), "folders": LIB.folders()}, indent=2)


@_tool
def library_file(name: str) -> str:
    """Details of one library file: print time, weight, layer height, layers, nozzle, temperatures, infill, supports,
    slicer, the printer model it was sliced for, per-filament colour words + grams, and which printers hold it."""
    try:
        LIB.get(name)
    except _library.LibraryError as e:
        return f"REFUSED: {e}"
    row = next((r for r in _library_rows() if r["in_library"] and r["name"].lower() == name.strip().lower()), None)
    if row is None:
        return f"REFUSED: '{name}' is not in the library."
    keep = ("name", "size", "folder", "added", "source", "printers", "print_time", "weight_g", "layer_height", "layers",
            "nozzle", "nozzle_temp", "bed_temp", "infill", "supports", "slicer", "sliced_for", "parts", "details_error")
    d = {k: row[k] for k in keep}
    d["filaments"] = [{"colour": f["color_name"], "type": f["type"], "grams": f.get("used_g")} for f in row["filaments"]]
    return json.dumps(d, indent=2)


@_tool
def library_send(name: str, printers: list[str], model_override: bool = False) -> str:
    """UPLOAD a library file to one or more printers' storage. It does NOT start a print - after it, the normal flow
    applies: suggest_mapping, the human's go-ahead and bed-clear statement, then start_print. Only idle printers accept
    a file (a printing, paused or offline one is refused, per printer, and nothing is sent to it); Bambu LAN printers
    take .3mf and, in beta, OctoPrint / PrusaLink / Klipper printers take .gcode / .bgcode; a file that was sliced for a different printer model is refused unless the
    human has read the warning and said it is safe - only then pass model_override=True (never on your own).
    A file already on the printer (names compared ignoring upper/lower case) is not overwritten. Returns one result per printer."""
    try:
        res = _library_send([name], printers, model_override is True)
    except _library.LibraryError as e:
        return f"REFUSED: {e}"
    return json.dumps({"results": [{k: v for k, v in r.items() if k != "name"} for r in res],
                       "note": "Nothing was started. To print: suggest_mapping, ask the human, then start_print."}, indent=2)


@_tool
def library_save_from_printer(printer: str, filename: str) -> str:
    """Copy one file from a printer into the Hub's library (a download). A Bambu printer is only asked when it is idle
    (the Hub never reads the SD card of a printing one); OctoPrint / Moonraker follow the backup rules. The copy keeps the
    original file; it is listed in the library with its details and plate preview."""
    try:
        e = _library_save(printer, filename)
    except _library.LibraryError as ex:
        return f"REFUSED: {ex}"
    except ValueError as ex:                    # unknown printer
        return f"REFUSED: {ex}"
    return json.dumps({"saved": e["name"], "size_mb": round(e["size"] / 1048576, 2)}, indent=2)


# ---- REST

@mcp.custom_route("/api/library", methods=["GET"])
@_safe_route
async def _api_library(request: Request):
    q = request.query_params
    try:
        folder = _library.sanitize_folder(q.get("folder", ""))
    except _library.LibraryError as e:
        return _library_error(e)
    snap = await _run(_library_snapshot, q.get("q", "")[:200], folder, q.get("hide_printer_only") == "1")
    return JSONResponse(snap, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/library/files/{name}", methods=["GET"])
@_safe_route
async def _api_library_file(request: Request):
    try:
        e = await _run(LIB.get, request.path_params["name"])
    except _library.LibraryError as ex:
        return _library_error(ex)
    return JSONResponse(await _run(_library_public, e), headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/library/files/{name}/download", methods=["GET"])
@_safe_route
async def _api_library_download(request: Request):
    try:
        e = await _run(LIB.get, request.path_params["name"])
        path = await _run(LIB.file_path, e["name"])
    except _library.LibraryError as ex:
        return _library_error(ex)
    return FileResponse(path, media_type="application/octet-stream", filename=e["name"],
                        headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/library/thumb/{basename}", methods=["GET"])
@_safe_route
async def _api_library_thumb(request: Request):
    p = LIB.thumb_path(request.path_params["basename"])
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)

    def work():
        with open(p, "rb") as fh:
            return fh.read()
    try:
        data = await _run(work)
    except OSError:
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@mcp.custom_route("/api/library/files/{name}", methods=["PUT"])
@_safe_route
async def _api_library_upload(request: Request):
    """The file's bytes as the body (Content-Type: application/octet-stream + X-Fabrica-Upload: 1, see
    security.is_library_upload); ?folder=<label>&replace=1. Streamed to a private temp file with the size cap enforced as
    it arrives, moved into the library only when complete."""
    q = request.query_params
    try:
        clean = _library.sanitize_name(request.path_params["name"])
        folder = _library.sanitize_folder(q.get("folder", ""))
        replace = q.get("replace") == "1"
        try:
            declared = int(request.headers.get("content-length", ""))
        except ValueError:
            declared = None
        await _run(LIB.check_can_add, clean, replace, declared)
        inc = await _run(LIB.begin_incoming)
    except _library.LibraryError as e:
        return _library_error(e)
    try:
        async for chunk in request.stream():
            inc.write(chunk)
        entry = await _run(LIB.ingest, inc, clean, folder, "upload", replace)
    except _library.LibraryError as e:
        inc.discard()
        return _library_error(e)
    except BaseException:
        inc.discard()
        raise
    return JSONResponse(await _run(_library_public, entry), status_code=201)


@mcp.custom_route("/api/library/delete", methods=["POST"])
@_safe_route
async def _api_library_delete(request: Request):
    """{"name", "confirm": <the same name>} - only a file in the LIBRARY; a printer's own files are deleted on its page."""
    body = await _optional_json(request)
    if not isinstance(body.get("name"), str) or body.get("confirm") != body.get("name"):
        return JSONResponse({"error": "REFUSED: confirm must equal the file name exactly."}, status_code=409)
    try:
        gone = await _run(LIB.delete, body["name"])
    except _library.LibraryError as e:
        return _library_error(e)
    return JSONResponse({"result": f"deleted {gone} from the library"})


@mcp.custom_route("/api/library/save", methods=["POST"])
@_safe_route
async def _api_library_save(request: Request):
    """{"printer", "filename", "folder"?, "replace"?}"""
    body = await _optional_json(request)
    try:
        entry = await _run(_library_save, str(body.get("printer") or ""), body.get("filename"),
                           body.get("folder") or "", body.get("replace") is True)
    except ValueError as e:                       # unknown printer
        return JSONResponse({"error": str(e)}, status_code=404)
    except _library.LibraryError as e:
        return _library_error(e)
    return JSONResponse(await _run(_library_public, entry), status_code=201)


@mcp.custom_route("/api/library/send", methods=["POST"])
@_safe_route
async def _api_library_send(request: Request):
    """{"names": [..] | "name", "printers": [..], "model_override"?: true}. Uploads only - never starts a print.
    -> 200 {results: [{name, printer, ok, status: sent|already_there|refused|failed, code, reason, ...}]}"""
    body = await _optional_json(request)
    try:
        res = await _run(_library_send, body.get("names") or body.get("name"), body.get("printers"),
                         body.get("model_override") is True)
    except _library.LibraryError as e:
        return _library_error(e)
    return JSONResponse({"results": res, "sent": sum(1 for r in res if r["ok"]), "refused": sum(1 for r in res if not r["ok"])})


@mcp.custom_route("/api/library/folder", methods=["POST"])
@_safe_route
async def _api_library_folder(request: Request):
    """{"action": "create"|"delete"|"move", "folder", "name"?}  (move: put library file `name` into `folder`; "" = no folder)"""
    body = await _optional_json(request)
    act = body.get("action")
    try:
        if act == "create":
            f = await _run(LIB.create_folder, body.get("folder"))
            return JSONResponse({"result": f"created folder {f}", "folders": LIB.folders()}, status_code=201)
        if act == "delete":
            f = await _run(LIB.delete_folder, body.get("folder"))
            return JSONResponse({"result": f"deleted folder {f}", "folders": LIB.folders()})
        if act == "move":
            e = await _run(LIB.move, body.get("name"), body.get("folder") or "")
            return JSONResponse({"result": f"moved {e['name']}", "folder": e.get("folder", "")})
    except _library.LibraryError as e:
        return _library_error(e)
    return JSONResponse({"error": "action must be create, delete or move"}, status_code=400)


@mcp.custom_route("/api/library/scan", methods=["POST"])
@_safe_route
async def _api_library_scan(request: Request):
    return JSONResponse({"printers": await _run(_library_scan)})

# ==== end library ====


# ==== rename ====
# File renaming MECHANICS (see renamer.py and docs/renaming.md). The Hub knows no naming convention: an agent decides
# the names (with file_facts to help), shows the human the before -> after table from rename_preview, and only after
# the human's yes calls rename_apply with the single-use ticket. Everything that touches a printer is here, behind the
# guards the other file operations use:
#   * nothing is listed, downloaded, uploaded or deleted on a printer that is not positively idle RIGHT NOW,
#   * a Bambu is only ever reached through its one FTPS session at a time (_ftps_lock) and never opens a second MQTT client,
#   * a name that already exists on the printer is never overwritten, the old name is deleted only after the new one is
#     verified there with the same size, and nothing in this block can start a print.

import renamer as _renamer
from bambu import _require_idle_for_delete

RENAME_TICKETS = _renamer.RenameTickets()
_RN_MAX_BYTES = 500 * 1024 * 1024        # largest file the rename flow moves (also bounded by the library's own cap)
_RN_VERIFY_TRIES = 3                     # HTTP printers can take a moment to show a new upload in their listing
_RN_VERIFY_WAIT_S = 1.0
BETA_NOTE = "beta - not yet verified on real hardware"


def _rn_target(location):
    """("library", None) or ("printer", driver). ValueError for an unknown printer."""
    loc = str(location or "").strip()
    if loc.lower() == "library":
        return "library", None
    return "printer", _pick(loc)


def _rn_lock(prn):
    """The driver's file lock: the FTPS lock of a Bambu, the HTTP file lock of the others."""
    return _octo_lock(prn.name) if _is_octo(prn) else _ftps_lock(prn.name)


def _rn_can_move(prn) -> bool:
    return (hasattr(prn, "upload") and hasattr(prn, "download")) if _is_octo(prn) else hasattr(prn, "sd_upload")


def _rn_fresh_idle(prn):
    """PrinterBusy unless the printer is positively idle right now (HTTP printers: a FRESH reading, not the 3 s cache)."""
    if _is_octo(prn):
        _require_idle_for_delete(prn, "changed")
    elif not _printer_is_idle(prn):
        raise PrinterBusy(f"printer is busy ({prn.name}) - no FTPS session was opened.")


def _rn_listing(prn, live_ok=True):
    """([{name, size, ...}], "live" | "cache"). A Bambu is only listed live when positively idle (the driver itself serves
    its last idle listing otherwise); HTTP printers are listed live (their own storage, no FTPS)."""
    if _is_octo(prn):
        try:
            rows = [dict(f) for f in _backup._files_for_backup(prn) if f.get("origin", "local") == "local"]
            return rows, "live"
        except Exception:
            return [dict(f) for f in (_FILES_SEEN.get(prn.name.lower()) or {}).values()], "cache"
    if live_ok and _printer_is_idle(prn):
        try:
            return [dict(f) for f in _sd_list(prn)], "live"
        except PrinterBusy:
            pass
    return [dict(f) for f in prn.sd_listing_cached()], "cache"


def _rn_find(rows, name):
    for f in rows:
        if f.get("name") == name:
            return f
    return None


def _rn_taken(rows, target, ignore=None):
    low = target.lower()
    for f in rows:
        n = str(f.get("name") or "")
        if n.lower() == low and n != ignore:
            return n
    return None


# ---------------------------------------------------------------- file_facts

def _file_facts(location, filename, may_read) -> dict:
    kind, prn = _rn_target(location)
    base_note = None
    if kind == "library":
        e = LIB.get(filename)
        name, size, meta = e["name"], int(e["size"]), e.get("meta") or {}
        d = _library.describe_meta(meta, color_name)
        folder, sha = e.get("folder") or "", e.get("sha256")
        from_ = "library file"
        if e.get("kind") == "3mf":
            parts, psrc = (meta.get("parts"), "slice_info") if isinstance(meta.get("parts"), int) and meta.get("parts") > 0 else (None, "unknown")
        else:
            parts, psrc = 1, "assumed"
        extra = {k: d.get(k) for k in ("layer_height", "layers", "nozzle", "infill", "supports", "slicer")}
        where = "library"
    else:
        rows, src = _rn_listing(prn, True)
        row = _rn_find(rows, filename)
        if row is None:
            raise _library.NotFound(f"'{filename}' is not on {prn.name}'s file list ({'live' if src == 'live' else 'last saved'} list).")
        name, size, sha, folder = row["name"], int(row.get("size") or 0), None, _renamer.folder_of(row["name"])
        busy = not _printer_is_idle(prn)
        with _cache_lock:
            entry = _cache_load().get(f"{prn.name}|{name}|{size}")
        if _is_octo(prn):
            meta = dict(entry) if isinstance(entry, dict) else {}
            secs, mm = row.get("print_seconds"), row.get("filament_mm")
            grams = costs.grams_from_mm(mm, "PLA") if mm else None
            d = _library.describe_meta({**meta, "print_seconds": secs, "print_time": row.get("print_time"), "weight_g": grams}, color_name)
            d["filaments"] = []
            parts, psrc = 1, "assumed"
            extra = {}
            from_ = "live file list + cache"
            base_note = "Gcode files do not say which filament they use; weight assumes PLA density."
        else:
            meta = _meta_for(prn, name, size, may_read and not busy)
            d = _library.describe_meta(meta, color_name)
            parts, psrc = (meta.get("parts"), "slice_info") if isinstance(meta, dict) and isinstance(meta.get("parts"), int) and meta.get("parts") > 0 else (None, "unknown")
            extra = {}
            from_ = "cache" if (busy or src == "cache") else "cache + live file list"
        if busy or src == "cache":
            base_note = ("facts from cache: " + prn.name + " is busy or its live list is not available, so nothing was read "
                         "from the printer itself. " + (base_note or "")).strip()
        where = prn.name
    base = _renamer.basename(name)
    ext = _renamer.file_ext(base)
    others = []
    fleet = _library_printer_files()
    for pn, files in fleet.items():
        for f in files:
            if pn == where and f["name"] == name:
                continue
            if _renamer.basename(f["name"]).lower() == base.lower() and int(f["size"]) == size:
                others.append({"location": pn, "name": f["name"], "match": "name and size"})
    if kind != "library":
        for e in LIB.entries():
            if e["name"].lower() == base.lower() and int(e["size"]) == size:
                others.append({"location": "library", "name": e["name"], "match": "name and size"})
    if kind == "library":
        for e in LIB.entries():
            if e["name"] != name and sha and e.get("sha256") == sha:
                others.append({"location": "library", "name": e["name"], "match": "same content (hash)"})
    try:
        printed = JOBLOG.times_printed(base)
    except Exception:
        printed = None
    out = {"location": where, "name": name, "stem": base[:len(base) - len(ext)] if ext else base, "extension": ext,
           "folder": folder or None, "size_bytes": size, "size_mb": round(size / 1048576, 2),
           "parts": parts, "parts_source": psrc, "sliced_for": d.get("sliced_for"),
           "print_time": d.get("print_time"), "print_seconds": d.get("print_seconds"), "weight_g": d.get("weight_g"),
           "filaments": [{"material": f["type"], "colour": f["color_name"], "grams": f.get("used_g")} for f in d.get("filaments") or []],
           **{k: v for k, v in extra.items() if v is not None},
           "also_on": others, "times_printed": printed, "facts_from": from_,
           "note": base_note or None}
    return out


@_tool
def file_facts(location: str, filename: str) -> str:
    """The facts you need to choose a good file name - the Hub has NO naming rules of its own; yours decide. `location` is
    a printer name or "library". Returns the current name, the extension to keep (`.gcode.3mf` counts as one), size, how
    many OBJECTS are on the plate (`parts`, with `parts_source`: slice_info = counted in the file, assumed = a gcode file
    cannot say so 1 is assumed, unknown), the printer model it was sliced for, print time, grams, the filaments as
    material + colour WORDS, where else the same file exists (other printers / the library), and how often it was
    printed (job history, under any of its names). Reads cached information; a printer that is busy is never contacted
    ("facts from cache" says so)."""
    try:
        return json.dumps(_file_facts(location, filename, not security.caller_read_only()), indent=2)
    except (ValueError, _library.LibraryError) as e:
        return f"REFUSED: {e}"


# ---------------------------------------------------------------- rename_preview

def _rn_table_text(rows) -> str:
    lines = ["| # | Where | Before | After | Result |", "|---|---|---|---|---|"]
    for r in rows:
        res = "OK" if r["status"] == "ok" else "PROBLEM: " + "; ".join(r["problems"])
        if r["status"] == "ok" and r["warnings"]:
            res += " (warning: " + "; ".join(r["warnings"]) + ")"
        lines.append(f"| {r['n']} | {r['location']} | {r['old_name']} | {r['target']} | {res} |")
    return "\n".join(lines)


def _rename_preview(renames) -> dict:
    items = _renamer.parse_items(renames)
    listings, rows, used_old, used_target = {}, [], {}, {}
    batch_old = {(i["location"].lower(), i["old_name"].lower()) for i in items}
    for n, it in enumerate(items, 1):
        row = {"n": n, "location": it["location"], "old_name": it["old_name"], "new_name": it["new_name"],
               "target": _renamer.target_path(it["old_name"], it["new_name"]), "extension": _renamer.file_ext(it["new_name"]),
               "problems": [], "warnings": [], "status": "ok", "printer_idle": None, "method": "", "size": None}
        rows.append(row)
        probs, warns = row["problems"], row["warnings"]
        try:
            kind, prn = _rn_target(it["location"])
        except ValueError as e:
            probs.append(str(e))
            row["status"] = "problem"
            continue
        row["location"] = "library" if kind == "library" else prn.name
        key = (row["location"].lower(), it["old_name"].lower())
        if key in used_old:
            probs.append(f"The same file is listed twice (see #{used_old[key]}).")
        used_old.setdefault(key, n)
        probs.extend(_renamer.name_problems(it["old_name"], it["new_name"]))
        tkey = (row["location"].lower(), row["target"].lower())
        if tkey in used_target:
            probs.append(f"Two files would get the same new name (see #{used_target[tkey]}).")
        used_target.setdefault(tkey, n)
        if tkey in batch_old and row["target"].lower() != it["old_name"].lower():
            probs.append("The new name is the current name of another file in this list. Rename in separate steps (no swaps or chains).")
        if kind == "library":
            row["method"] = _renamer.METHOD_LIBRARY
            try:
                _library.sanitize_name(it["new_name"])
            except _library.LibraryError as e:
                probs.append(str(e))
            try:
                e = LIB.get(it["old_name"])
                row["size"] = int(e["size"])
                if e["name"] != it["old_name"]:
                    probs.append(f"The Library file is called '{e['name']}' (names must match exactly).")
            except _library.LibraryError as ex:
                probs.append(str(ex))
            else:
                try:
                    other = LIB.get(it["new_name"])
                    if other["name"] != e["name"]:
                        probs.append(f"'{other['name']}' is already in the library.")
                except _library.LibraryError:
                    pass
        else:
            row["method"] = _renamer.METHOD_PRINTER
            if not _rn_can_move(prn):
                probs.append(f"{prn.name} cannot receive files from the Hub, so it cannot be renamed this way.")
            elif _is_octo(prn):
                warns.append(BETA_NOTE)
            if prn.name not in listings:
                listings[prn.name] = _rn_listing(prn, True)
            lrows, lsrc = listings[prn.name]
            src = _rn_find(lrows, it["old_name"])
            if src is None:
                probs.append(f"'{it['old_name']}' is not on {prn.name}'s file list" + (" (last saved list)." if lsrc == "cache" else "."))
            else:
                row["size"] = int(src.get("size") or 0)
                if row["size"] > min(_RN_MAX_BYTES, LIB.max_bytes):
                    probs.append("The file is too large to keep a Library copy, so it cannot be renamed this way.")
                try:
                    _library.name_from_printer_path(it["old_name"])
                except _library.LibraryError as e:
                    probs.append("A Library copy of the original cannot be kept: " + str(e))
            taken = _rn_taken(lrows, row["target"])
            if taken is not None:
                if taken.lower() == it["old_name"].lower():
                    probs.append("Only the capital letters change; printers cannot do that safely. Pick a different name.")
                else:
                    probs.append(f"'{taken}' already exists on {prn.name}; nothing is ever overwritten.")
            idle = _printer_is_idle(prn)
            row["printer_idle"] = bool(idle)
            if not idle:
                warns.append(f"{prn.name} is busy now. The rename will be refused at apply unless it is idle then.")
            try:
                s = prn.snapshot()
            except Exception:
                s = {}
            job = str(s.get("job") or "")
            if job and not idle and (job == it["old_name"] or costs.file_key(job) == costs.file_key(_renamer.basename(it["old_name"]))):
                probs.append(f"'{it['old_name']}' is the file {prn.name} is printing right now.")
            if lsrc == "cache" and src is not None:
                warns.append("checked against the last saved file list (the printer is busy); it is re-checked at apply")
        if probs:
            row["status"] = "problem"
    good = [r for r in rows if r["status"] == "ok"]
    ticket = None
    if good:
        items_ok = [{"location": r["location"], "old_name": r["old_name"], "new_name": r["new_name"], "size": r["size"]} for r in good]
        prns = {}
        for r in good:
            if r["location"] != "library":
                prns[r["location"].lower()] = PRINTERS.get(r["location"].lower())
        ticket = RENAME_TICKETS.issue(items_ok, prns)
    return {"ticket": ticket, "ticket_expires_in_s": _renamer.TICKET_TTL_S if ticket else None,
            "count": len(rows), "ok": len(good), "problems": len(rows) - len(good), "table": rows,
            "table_text": _rn_table_text(rows),
            "next_step": ("Show the human the table (Before -> After). Only after they clearly say yes, call rename_apply with this "
                          "ticket and approved_by_human=true. " + (f"The ticket covers the {len(good)} OK row(s) only; fix the "
                          "PROBLEM rows and preview them again." if len(good) < len(rows) else "")) if ticket else
                         "Nothing can be applied: fix the problems and preview again."}


@_tool
def rename_preview(renames: list[dict]) -> str:
    """Check a batch of file renames and get the before -> after table plus a single-use ticket (valid 10 minutes, bound to
    exactly this list). `renames` = [{"location": printer name or "library", "old_name": ..., "new_name": ...}] (up to 100;
    new_name is the bare file name, the folder stays). The Hub checks only what a file system and a printer need: the
    source exists, the new name KEEPS the same extension (`.gcode.3mf` stays `.gcode.3mf`), has no / \\ : * ? " < > | or
    control characters, no leading/trailing space or dot, at most 200 characters, collides with no other file, and is not
    the file being printed. It enforces NO naming convention - yours decide the names. A printer that is busy is a warning
    here and a refusal at apply. Nothing is changed by a preview. Show the table to the human and wait for a clear yes
    before rename_apply. Renaming on Octoprint / PrusaLink / Klipper printers is beta - not yet verified on real hardware."""
    try:
        return json.dumps(_rename_preview(renames), indent=2)
    except _renamer.RenameError as e:
        return f"REFUSED: {e}"


# ---------------------------------------------------------------- rename_apply

def _rn_result(item, status, message, step=None, **extra):
    return {"location": item["location"], "old_name": item["old_name"], "new_name": item["new_name"],
            "status": status, "message": message, **({"step": step} if step else {}), **extra}


def _rn_keep_copy(prn, old_name, data) -> str:
    """Make sure the Library holds the original: skip if a file with the same content is already there. Returns a note,
    raises LibraryError when no copy could be kept."""
    sha = hashlib.sha256(data).hexdigest()
    have = LIB.find_by_sha(sha)
    if have is not None:
        return f"the Library already had an identical copy ('{have['name']}')"
    clean = _library.name_from_printer_path(old_name)
    ext = _library.ext_of(clean)
    stem = clean[:len(clean) - len(ext)]
    last = None
    for i in range(1, 21):
        cand = clean if i == 1 else f"{stem} ({i}){ext}"
        try:
            LIB.add_bytes(cand, data, folder=_renamer.ORIGINALS_FOLDER, source=f"printer:{prn.name}", replace=False)
            return f"a copy was kept in the Library ('{cand}', folder '{_renamer.ORIGINALS_FOLDER}')"
        except _library.Exists as e:
            last = e
    raise last or _library.LibraryError("no free name for the copy")


def _rn_move_cache(prn, old, new, size):
    """Carry the cached details and plate preview over to the new name, so the renamed file does not have to be read again."""
    try:
        with _cache_lock:
            entry = (_cache_load().get(f"{prn.name}|{old}|{size}"))
            entry = dict(entry) if isinstance(entry, dict) else None
        png = None
        if entry and entry.get("thumb"):
            try:
                with open(_thumb_path(entry["thumb"]), "rb") as fh:
                    png = fh.read()
            except OSError:
                png = None
        _cache_purge(prn.name, old)
        if entry:
            key = f"{prn.name}|{new}|{size}"
            entry["thumb"] = _save_thumb(key, png)
            with _cache_lock:
                cache = _cache_load()
                cache[key] = entry
                _cache_save(cache)
    except Exception:
        pass


def _rn_apply_library(it) -> dict:
    if it.get("size") is not None:                      # same ticket binding as a printer item: the file the human saw
        try:
            cur = LIB.get(it["old_name"])
        except _library.LibraryError:
            return _rn_result(it, "skipped", f"skipped: '{it['old_name']}' is no longer in the Library.")
        if int(cur.get("size") or 0) != int(it["size"]):
            return _rn_result(it, "skipped", f"skipped: '{it['old_name']}' changed since the preview (different size). Preview again.")
    try:
        e = LIB.rename(it["old_name"], it["new_name"])
    except _library.LibraryError as ex:
        return _rn_result(it, "failed", f"failed at rename: {ex}", "rename")
    note = ""
    try:
        JOBLOG.record_rename("library", "library", it["old_name"], e["name"])
    except Exception as ex:
        note = f" (the history link could not be saved: {type(ex).__name__})"
    return _rn_result(it, "renamed", f"renamed in the Library{note}")


def _rn_apply_printer(it, prn) -> dict:
    old, new = it["old_name"], it["new_name"]
    target = _renamer.target_path(old, new)
    lock = _rn_lock(prn)
    is_http = _is_octo(prn)
    cap = min(_RN_MAX_BYTES, LIB.max_bytes)

    def refuse_busy(extra=""):
        return _rn_result(it, "refused", f"refused (busy): {prn.name} is not idle, so nothing was touched on it{extra}.")

    def list_now():
        """The printer's list RIGHT NOW (never a saved one): an HTTP printer's own listing, a Bambu's FTPS listing."""
        if is_http:
            return [dict(f) for f in _backup._files_for_backup(prn) if f.get("origin", "local") == "local"]
        return [dict(f) for f in prn.sd_list()]

    # ---- 1. check, list, download (under the file lock, only while positively idle)
    try:
        with lock:
            try:
                _rn_fresh_idle(prn)
            except PrinterBusy:
                return refuse_busy()
            rows = list_now()
            src = _rn_find(rows, old)
            if src is None:
                return _rn_result(it, "skipped", f"skipped: '{old}' is no longer on {prn.name}.")
            if it.get("size") is not None and int(src.get("size") or 0) != int(it["size"]):
                return _rn_result(it, "skipped", f"skipped: '{old}' changed since the preview (different size). Preview again.")
            if _rn_taken(rows, target) is not None:
                return _rn_result(it, "skipped", f"skipped: '{target}' already exists on {prn.name}; nothing is ever overwritten.")
            try:
                data = prn.download(old, max_bytes=cap) if is_http else _sd_download_capped(prn, old, cap)
            except PrinterBusy:
                return refuse_busy()
            except Exception as e:
                return _rn_result(it, "failed", f"failed at download: {security.scrub_text(str(e))} (nothing was changed)", "download")
            if len(data) != int(src.get("size") or 0):
                return _rn_result(it, "failed", "failed at download: the downloaded size does not match the file list (nothing was changed)", "download")
            if len(data) > cap:
                return _rn_result(it, "failed", "failed at download: the file is too large (nothing was changed)", "download")
    except Exception as e:
        return _rn_result(it, "failed", f"failed at download: {security.scrub_text(f'{type(e).__name__}: {e}')} (nothing was changed)", "download")
    # ---- 2. keep a copy of the original in the Library
    try:
        copy_note = _rn_keep_copy(prn, old, data)
    except Exception as e:
        return _rn_result(it, "failed", f"failed at library copy: {security.scrub_text(str(e))} (nothing was changed on the printer)", "library copy")
    # ---- 3. upload under the new name, verify, delete the old name (under the lock again, idle re-checked)
    partial_note = ""
    try:
        with lock:
            try:
                _rn_fresh_idle(prn)
            except PrinterBusy:
                return refuse_busy(f" ({copy_note})")
            rows = list_now()
            src = _rn_find(rows, old)
            if src is None or int(src.get("size") or 0) != len(data):
                return _rn_result(it, "skipped", f"skipped: '{old}' changed on {prn.name} while it was being copied; nothing was changed there.")
            if _rn_taken(rows, target) is not None:
                return _rn_result(it, "skipped", f"skipped: '{target}' appeared on {prn.name}; nothing is ever overwritten.")

            def cleanup_new():
                """Remove OUR half-written new file (it did not exist before this step)."""
                try:
                    cur = _rn_find(list_now(), target)
                    if cur is not None:
                        prn.sd_delete(target)
                        return " The partly written new file was removed."
                except Exception:
                    return " The partly written new file could not be removed - check the printer's file list."
                return ""
            try:
                if is_http:
                    prn.upload(target, data)
                else:
                    prn.sd_upload(target, data)
            except PrinterBusy:
                return refuse_busy(f" ({copy_note})")
            except Exception as e:
                partial_note = cleanup_new()
                return _rn_result(it, "failed", f"failed at upload: {security.scrub_text(str(e))}. The old name was NOT deleted.{partial_note}", "upload")
            seen = None
            for attempt in range(_RN_VERIFY_TRIES if is_http else 1):
                if attempt:
                    time.sleep(_RN_VERIFY_WAIT_S)
                try:
                    seen = _rn_find(list_now(), target)
                except Exception:
                    seen = None
                if seen is not None and int(seen.get("size") or 0) == len(data):
                    break
            if seen is None or int(seen.get("size") or 0) != len(data):
                why = "the new name is not on the printer's file list" if seen is None else \
                    f"the new file has {int(seen.get('size') or 0)} bytes, expected {len(data)}"
                if seen is not None:
                    partial_note = cleanup_new()
                return _rn_result(it, "failed", f"failed at verify: {why}. The old name was NOT deleted.{partial_note}", "verify")
            try:
                prn.sd_delete(old)
            except PrinterBusy:
                return _rn_result(it, "failed", "failed at delete old name: the printer became busy. The new file is in place and the old one "
                                  "remains (both exist); delete the old one when it is idle.", "delete old name")
            except Exception as e:
                return _rn_result(it, "failed", f"failed at delete old name: {security.scrub_text(str(e))}. The new file is in place and the "
                                  "old one remains (both exist).", "delete old name")
    except Exception as e:
        return _rn_result(it, "failed", f"failed at upload: {security.scrub_text(f'{type(e).__name__}: {e}')}. The old name was NOT deleted.", "upload")
    # ---- 4. bookkeeping (never fails the rename)
    _rn_move_cache(prn, old, target, len(data))
    if is_http:
        try:
            seen_files = _FILES_SEEN.setdefault(prn.name.lower(), {})
            seen_files.pop(costs.file_key(old), None)
            seen_files[costs.file_key(target)] = {**(seen or {}), "name": target}
        except Exception:
            pass
    note = ""
    try:
        JOBLOG.record_rename("printer", prn.name, old, target)
    except Exception as ex:
        note = f" (the history link could not be saved: {type(ex).__name__})"
    print(f"[rename] {prn.name}: {old} -> {target}", flush=True)
    return _rn_result(it, "renamed", f"renamed on {prn.name}; {copy_note}{note}", copy=copy_note)


def _rename_apply(ticket, approved) -> dict:
    if approved is not True:
        return {"declined": True, "message": "REFUSED: approved_by_human is not true. That flag is the HUMAN's yes to the before -> after "
                "table - show them the table from rename_preview and only when they clearly agree call again with approved_by_human=true."}
    if not isinstance(ticket, str) or not ticket.strip():
        return {"declined": True, "message": "REFUSED: no ticket. Run rename_preview, show the human the table, and use its ticket "
                "(single use, valid 10 minutes)."}
    t, why = RENAME_TICKETS.peek(ticket)
    if t is None:
        return {"declined": True, "message": f"REFUSED: ticket not accepted - {why}. Run rename_preview again."}
    for loc, drv in t["printers"].items():
        if drv is None or PRINTERS.get(loc) is not drv:
            return {"declined": True, "message": "REFUSED: a printer in this list was edited, removed or re-added since the preview. Preview again."}
    if not RENAME_TICKETS.consume(ticket):
        return {"declined": True, "message": "REFUSED: ticket not accepted - it was already used. Run rename_preview again."}
    results = []
    for it in t["items"]:
        try:
            if it["location"] == "library":
                results.append(_rn_apply_library(it))
            else:
                prn = PRINTERS.get(it["location"].lower())
                results.append(_rn_apply_printer(it, prn) if prn is not None else _rn_result(it, "failed", "failed at check: unknown printer", "check"))
        except Exception as e:                      # one item never stops the others
            results.append(_rn_result(it, "failed", f"failed: {security.scrub_text(f'{type(e).__name__}: {e}')}", "unexpected"))
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("renamed", "refused", "failed", "skipped")}
    return {"results": results, **counts,
            "summary": ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "nothing to do",
            "note": "Nothing was started. Renamed files keep one history: recent_jobs, job_stats and cost_per_part show the newest name."}


@_tool
def rename_apply(ticket: str, approved_by_human: bool = False) -> str:
    """APPLY the renames of a rename_preview ticket. `approved_by_human=True` is THE HUMAN'S yes to the before -> after
    table: only set it after you showed the table and the human clearly agreed in this conversation - never on your own,
    never as a default. Without it, or with a wrong / expired / already used ticket, nothing happens. Items run one by one and
    a failure never stops the others; each reports renamed / refused (busy) / failed at <step> / skipped. On a printer:
    re-check idle -> download -> keep a copy in the Library (folder 'Renamed originals') -> upload under the new name ->
    verify the same size is listed -> only then delete the old name. A busy printer is refused (no session is opened); an
    existing file is never overwritten; if upload or verify fails the old name is NOT deleted. Library files are renamed
    atomically. Nothing here can start a print. Printers other than Bambu are beta - not yet verified on real hardware."""
    out = _rename_apply(ticket, approved_by_human)
    return out["message"] if out.get("declined") else json.dumps(out, indent=2)


@mcp.prompt(name="rename_files", description="How to rename print files safely with the Hub's rename tools.")
def rename_files() -> str:
    return (
        "Renaming print files with Fabrica. The Hub has NO naming rules - yours decide the names; the Hub only gives facts "
        "and a safe way to apply them.\n"
        "1. file_facts(location, filename) for each file (location = printer name or \"library\"): parts on the plate, "
        "filament material + colour, sliced-for model, time, grams, where else it exists, how often it was printed.\n"
        "2. Decide each new name with YOUR OWN naming rules. Keep the extension (.gcode.3mf stays .gcode.3mf); give only "
        "the file name, the folder stays.\n"
        "3. rename_preview([{location, old_name, new_name}, ...]) -> a before/after table, per-row problems and a "
        "single-use ticket (10 minutes).\n"
        "4. Show the human the table. Wait for a clear yes in this conversation.\n"
        "5. Only then rename_apply(ticket, approved_by_human=true). Busy printers are refused; a copy of each original is "
        "kept in the Library; the old name is deleted only after the new one is verified. Report each row's result.\n"
        "Never set approved_by_human on your own. Octoprint / PrusaLink / Klipper renames are beta (not yet verified on real hardware).")


# ---- REST

@mcp.custom_route("/api/files/facts", methods=["GET"])
@_safe_route
async def _api_file_facts(request: Request):
    q = request.query_params
    try:
        d = await _run(_file_facts, q.get("location", ""), q.get("name", ""), not security.caller_read_only())
    except _library.LibraryError as e:
        return _library_error(e)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse(d, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/rename/preview", methods=["POST"])
@_safe_route
async def _api_rename_preview(request: Request):
    """{"renames": [{location, old_name, new_name}, ...]} -> the table + ticket. Changes nothing."""
    body = await _optional_json(request)
    try:
        return JSONResponse(await _run(_rename_preview, body.get("renames")), headers={"Cache-Control": "no-store"})
    except _renamer.RenameError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/api/rename/apply", methods=["POST"])
@_safe_route
async def _api_rename_apply(request: Request):
    """{"ticket", "approved_by_human": true} - the dashboard sends it when the person taps Apply."""
    body = await _optional_json(request)
    out = await _run(_rename_apply, body.get("ticket"), body.get("approved_by_human"))
    if out.get("declined"):
        return JSONResponse({"error": out["message"]}, status_code=409)
    return JSONResponse(out, headers={"Cache-Control": "no-store"})

# ==== end rename ====


# ============================================================= printer management
# Add / edit / remove printers while the Hub runs (see fleet_store.py). Self-contained:
# MCP tools + REST routes. Secrets (access codes, API keys, passwords) are write-only
# everywhere in here: nothing below ever returns one, only "set" / "not set".

def _backup_guard(name: str):
    """Refuse to change a printer while a file backup of it is queued or running."""
    for j in _BACKUPS.status()["running"]:
        if j["printer"].lower() == name.lower():
            return (f"A backup of {name} is {j['status']} ({j['files_done']}/{j['files_total']} files). "
                    "Changing or removing the printer now would cut it short.")
    return None


FLEET.guards.append(_backup_guard)


def _fleet_json(e: "fleet_store.FleetError"):
    return JSONResponse(e.payload(), status_code=e.status)


async def _json_body(request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise fleet_store.FleetError("The request body must be JSON.")
    if not isinstance(body, dict):
        raise fleet_store.FleetError("The request body must be a JSON object.")
    return body


@_tool
async def list_printer_types() -> str:
    """List the kinds of printer this Hub can add, and the exact fields add_printer needs
    for each. Call this first when asked to add a printer. Returns JSON:
    [{type, label, description, fields: [{name, label, type, required, secret, help,
    options?}], require_one_of?}]. Field type 'password' means a secret (access code, API
    key): it is write-only - once saved it is never shown again by any tool or page. Also
    shows the printers already added (secrets redacted, only "set"/"not set") so you can
    avoid duplicates."""
    printers = FLEET.list_public()
    if security.caller_read_only():
        printers = security.scrub_urls(printers)
    return json.dumps({"types": FLEET.types(), "printers": printers}, indent=2)


@_tool
async def test_printer_connection(type: str, values: dict, existing_printer: str = "") -> str:
    """Check that a printer is reachable and the credentials work, WITHOUT saving anything.
    `type` is a key from list_printer_types (bambu, octoprint, ...). `values` maps field
    names to values, e.g. for bambu {"name": "P1S", "ip": "192.168.1.100", "serial": "...",
    "access_code": "..."}. When re-testing an already-added printer, pass its name as
    `existing_printer` and leave secret fields blank - the stored ones are used. Returns JSON
    {ok, model, firmware, has_camera, error, message}; `message` is plain English you can
    relay to the human. Bambu tests briefly open (and close) one MQTT session."""
    try:
        res = await _run(FLEET.test, type, values, existing_printer or None)
    except fleet_store.FleetError as e:
        return f"REFUSED: {e}"
    return json.dumps(res, indent=2)


@_tool
async def add_printer(type: str, values: dict, skip_test: bool = False) -> str:
    """Add a printer to the Hub; it appears on the dashboard and in every tool immediately
    (no restart). `type` and `values` are as in list_printer_types / test_printer_connection.
    The connection is tested first and the printer is NOT added if the test fails (the error
    says why, in plain English) - set skip_test=true only if the human insists on saving a
    printer that is switched off. Names: letters/digits/dash/underscore/dot, no spaces,
    unique, permanent. The access code / API key is stored on the Hub (mode-0600 file) and
    can never be read back. Tip: never repeat a secret in chat after using it."""
    try:
        res = await _run(FLEET.add, type, values, bool(skip_test))
    except fleet_store.ProbeFailed as e:
        return f"REFUSED: connection test failed - {e}. Fix the details, or pass skip_test=true to save anyway."
    except fleet_store.FleetError as e:
        return f"REFUSED: {e}"
    return json.dumps(res, indent=2)


@_tool
async def update_printer(name: str, values: dict, skip_test: bool = False, force: bool = False) -> str:
    """Change an existing printer's settings (new IP address, rotated access code, new API
    key, model...). `values` needs only the fields to change; a blank or missing secret keeps
    the stored one. The printer's name cannot change (remove + add instead). If connection
    details change they are tested first (skip_test=true to skip). The old connection is fully
    closed before the new one opens. REFUSED while the printer is printing, paused, or being
    backed up unless force=true - and only use force after telling the human, because the Hub
    loses its connection to the running job (the print itself continues on the printer)."""
    try:
        res = await _run(FLEET.update, name, values, bool(skip_test), bool(force))
    except fleet_store.ProbeFailed as e:
        return f"REFUSED: connection test failed - {e}. Fix the details, or pass skip_test=true to save anyway."
    except fleet_store.FleetError as e:
        return f"REFUSED: {e}"
    return json.dumps(res, indent=2)


@_tool
async def remove_printer(name: str, force: bool = False) -> str:
    """Remove a printer from the Hub: its connection is closed and its saved address and
    secrets are deleted. The printer itself is untouched, and existing backups and cached
    thumbnails stay on disk. REFUSED while the printer is printing, paused, or being backed up
    unless force=true - only use force after the human confirmed (the Hub loses sight of the
    running job; the print itself continues). Ask the human to confirm before removing at all."""
    try:
        res = await _run(FLEET.remove, name, bool(force))
    except fleet_store.FleetError as e:
        return f"REFUSED: {e}"
    return json.dumps(res, indent=2)


@mcp.custom_route("/api/printer-types", methods=["GET"])
@_safe_route
async def _api_printer_types(request: Request):
    return JSONResponse({"types": FLEET.types()})


@mcp.custom_route("/api/printers", methods=["GET"])
@_safe_route
async def _api_printers(request: Request):
    printers = FLEET.list_public()
    if security.caller_read_only():             # user:password@ inside a printer / camera URL is a credential
        printers = security.scrub_urls(printers)
    return JSONResponse({"printers": printers})


@mcp.custom_route("/api/printers/test", methods=["POST"])
@_safe_route
async def _api_printers_test(request: Request):
    try:
        body = await _json_body(request)
        res = await _run(FLEET.test, body.get("type"), body.get("values"), body.get("name") or None)
    except fleet_store.FleetError as e:
        return _fleet_json(e)
    return JSONResponse({"result": res})


@mcp.custom_route("/api/printers", methods=["POST"])
@_safe_route
async def _api_printers_add(request: Request):
    try:
        body = await _json_body(request)
        res = await _run(FLEET.add, body.get("type"), body.get("values"), bool(body.get("skip_test")))
    except fleet_store.FleetError as e:
        return _fleet_json(e)
    return JSONResponse(res, status_code=201)


@mcp.custom_route("/api/printers/{name}", methods=["PUT"])
@_safe_route
async def _api_printers_update(request: Request):
    try:
        body = await _json_body(request)
        res = await _run(FLEET.update, request.path_params["name"], body.get("values"),
                         bool(body.get("skip_test")), bool(body.get("force")))
    except fleet_store.FleetError as e:
        return _fleet_json(e)
    return JSONResponse(res)


@mcp.custom_route("/api/printers/{name}", methods=["DELETE"])
@_safe_route
async def _api_printers_remove(request: Request):
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    try:
        res = await _run(FLEET.remove, request.path_params["name"], force)
    except fleet_store.FleetError as e:
        return _fleet_json(e)
    return JSONResponse(res)

# ============================================================= printer groups
# A group is a label on printers ("Production", "Workshop"): metadata kept with the printer in printers.json
# (fleet_store.py). Changing one never touches a printer connection. Reads need `read`, changes need `control` -
# exactly like the printer routes above; History/cost filters on groups are in the job history block below.

@_tool
def list_groups() -> str:
    """The printer groups in use (a label such as "Production" or "Workshop" that a printer can carry; a printer may
    be in several) with the printers in each: [{name, printers: [...]}]. Use a group name as the `group` filter of
    job_stats / recent_jobs / cost_per_part."""
    return json.dumps(FLEET.list_groups(), indent=2)


@_tool
async def set_printer_groups(printer: str, groups: list[str]) -> str:
    """Set which groups a printer is in (replaces its list; [] clears it). Up to 8 groups of at most 32 characters,
    no slash or comma, matched without regard to case. Only a label: the printer's connection is not touched and
    it works while a print is running. Returns the printer with its groups."""
    try:
        res = await _run(FLEET.set_groups, printer, groups)
    except fleet_store.FleetError as e:
        return f"REFUSED: {e}"
    return json.dumps(res, indent=2)


@mcp.custom_route("/api/groups", methods=["GET"])
@_safe_route
async def _api_groups(request: Request):
    return JSONResponse(FLEET.list_groups())


@mcp.custom_route("/api/printers/{name}/groups", methods=["PUT"])
@_safe_route
async def _api_printers_groups(request: Request):
    try:
        body = await _json_body(request)
        res = await _run(FLEET.set_groups, request.path_params["name"], body.get("groups"))
    except fleet_store.FleetError as e:
        return _fleet_json(e)
    return JSONResponse(res)


@mcp.custom_route("/api/groups/rename", methods=["POST"])
@_safe_route
async def _api_groups_rename(request: Request):
    try:
        body = await _json_body(request)
        old = body.get("from", body.get("name"))
        new = body.get("to", body.get("new_name"))
        if not isinstance(old, str) or not isinstance(new, str):
            raise fleet_store.FleetError('Send {"from": "<current name>", "to": "<new name>"}.')
        res = await _run(FLEET.rename_group, old, new)
    except fleet_store.FleetError as e:
        return _fleet_json(e)
    return JSONResponse(res)


@mcp.custom_route("/api/groups/{name}", methods=["DELETE"])
@_safe_route
async def _api_groups_delete(request: Request):
    try:
        res = await _run(FLEET.delete_group, request.path_params["name"])
    except fleet_store.FleetError as e:
        return _fleet_json(e)
    return JSONResponse(res)

# ============================================================= end printer groups

# ============================================================= end printer management


@mcp.custom_route("/", methods=["GET"])
@_safe_route
async def _root(request: Request):
    return HTMLResponse('<meta http-equiv="refresh" content="0; url=/dashboard">')


def _is_secure(request: Request) -> bool:
    return security.secure_request(request.url.scheme, request.headers)


def _client_ip(request: Request) -> str:
    return security.client_ip(dict(request.headers), request.client.host if request.client else "")


def _remote(request: Request) -> str:
    return request.client.host if request.client else ""


def _limiter_client(request: Request) -> str:
    """The bucket a sign-in / pairing / unlock attempt is counted in: the connecting address (IPv6 by /64), or - only
    behind a proxy listed in BAMBU_MCP_TRUSTED_PROXIES - the client that proxy forwarded."""
    return AUTH.limiter_client(dict(request.headers), _remote(request))


def _same_origin(request: Request, strict=False) -> bool:
    h = dict(request.headers)
    trust = AUTH.forwarded_host_trusted(_remote(request))
    return security.same_origin_strict(h, trust) if strict else security.same_origin(h, trust)


def _set_session_cookie(request: Request, response):
    """Give a browser the dashboard session cookie (HttpOnly, SameSite=Strict; Secure over https).
    Not given when the Host header looks like a stranger's name (DNS-rebinding guard)."""
    if AUTH.host_ok(request.headers):
        response.set_cookie(**AUTH.cookie_kwargs(_is_secure(request)))
    return response


def _set_login_cookies(request: Request, response, session=None, device=None):
    """Sign-in cookies: a session (value, max_age) and/or a trusted device (value, max_age)."""
    secure = _is_secure(request)
    if session:
        response.set_cookie(security.COOKIE_NAME, session[0], max_age=session[1], path="/",
                            httponly=True, samesite="strict", secure=secure)
    if device:
        response.set_cookie(security.DEVICE_COOKIE, device[0], max_age=device[1], path="/",
                            httponly=True, samesite="strict", secure=secure)
    return response


@mcp.custom_route("/dashboard", methods=["GET"])
@_safe_route
async def _dashboard(request: Request):
    """The page is public; what it can DO needs the session cookie issued here. With a dashboard
    password set, the cookie is only issued by /api/auth/login and this serves the login page."""
    if AUTH.login_required():
        ident = AUTH.cookie_identity(dict(request.headers), "GET")
        if ident is None or ident.kind != "cookie":
            return HTMLResponse(security.LOGIN_PAGE, headers={"Cache-Control": "no-store"})
        AUTH.touch(ident, "/dashboard", _client_ip(request))
        with open(_DASH_PATH) as fh:
            resp = HTMLResponse(fh.read(), headers={"Cache-Control": "no-store"})
        if ident.device_id:                      # a trusted device: swap its token when it is due
            fresh = AUTH.maybe_rotate_device(dict(request.headers), _is_secure(request))
            if fresh:
                resp.headers.append("set-cookie", fresh)
        return resp
    with open(_DASH_PATH) as fh:
        resp = HTMLResponse(fh.read(), headers={"Cache-Control": "no-store"})
    return _set_session_cookie(request, resp)


# ------------------------------------------------------------------ brand assets (public)
# Icons + web-app manifest. Public on purpose (see security.PUBLIC_PAGES): browsers and "add to home
# screen" fetch them without credentials, and they carry no data. Only files that sit directly in
# app/static/brand/ are ever served.
_BRAND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "brand")
_BRAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BRAND_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
                ".webmanifest": "application/manifest+json"}
_BRAND_CACHE = {"Cache-Control": "public, max-age=86400"}
BRAND_NAME, BRAND_SHORT, BRAND_TAGLINE = "Fabrica — Print Farm", "Fabrica", "printer management, agent-first."
BRAND_COLORS = {"blueprint": "#0f3b66", "drafting": "#e8f2fb", "amber": "#ffb020", "sky": "#5aa5e6"}


def _build_rev() -> str:
    """Short build id shown in Settings > Agents / MCP. FABRICA_BUILD_REV (set by the Docker build)
    wins; a source checkout asks git once; otherwise "dev"."""
    rev = (os.environ.get("FABRICA_BUILD_REV") or "").strip()
    if not rev:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.exists(os.path.join(root, ".git")):
            try:
                import subprocess
                rev = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True,
                                     text=True, timeout=3).stdout.strip()
            except Exception:
                rev = ""
    if re.fullmatch(r"[0-9a-f]{12,40}", rev):
        rev = rev[:7]
    return rev if re.fullmatch(r"[A-Za-z0-9._-]{1,40}", rev) else "dev"


BUILD_REV = _build_rev()


def _brand_file(name: str):
    """Response for app/static/brand/<name>, or None when the name is not a plain file there."""
    if not _BRAND_NAME_RE.match(name or ""):
        return None
    ext = os.path.splitext(name)[1].lower()
    path = os.path.join(_BRAND_DIR, name)
    if ext not in _BRAND_TYPES or os.path.dirname(os.path.realpath(path)) != os.path.realpath(_BRAND_DIR) \
            or not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return Response(fh.read(), media_type=_BRAND_TYPES[ext], headers=_BRAND_CACHE)


def _brand_or_404(name: str):
    return _brand_file(name) or JSONResponse({"error": "not found"}, status_code=404)


@mcp.custom_route("/favicon.ico", methods=["GET"])
@_safe_route
async def _favicon(request: Request):
    return _brand_or_404("favicon.ico")


@mcp.custom_route("/favicon.svg", methods=["GET"])
@_safe_route
async def _favicon_svg(request: Request):
    return _brand_or_404("mark-32.svg")        # the small-size mark: reads at 16-32 px


@mcp.custom_route("/apple-touch-icon.png", methods=["GET"])
@_safe_route
async def _apple_touch_icon(request: Request):
    return _brand_or_404("apple-touch-icon.png")


@mcp.custom_route("/static/brand/{filename}", methods=["GET"])
@_safe_route
async def _static_brand(request: Request):
    return _brand_or_404(request.path_params["filename"])


@mcp.custom_route("/manifest.webmanifest", methods=["GET"])
@_safe_route
async def _manifest(request: Request):
    hub = str(SETTINGS.get("general.hub_name") or "").strip()
    short = BRAND_SHORT if hub in ("", "Print Hub", "Fabrica", "Fabrica Farm", BRAND_NAME) else hub[:12].strip()
    icon = lambda f, n, purpose="any": {"src": f"/static/brand/{f}", "sizes": f"{n}x{n}", "type": "image/png", "purpose": purpose}
    doc = {"name": BRAND_NAME, "short_name": short or BRAND_SHORT, "description": BRAND_TAGLINE,
           "id": "/dashboard", "start_url": "/dashboard", "scope": "/", "display": "standalone", "orientation": "any",
           "theme_color": BRAND_COLORS["blueprint"], "background_color": BRAND_COLORS["blueprint"],
           "icons": [icon("icon-192.png", 192), icon("icon-512.png", 512),
                     icon("icon-maskable-512.png", 512, "maskable"),
                     {"src": "/static/brand/mark.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]}
    return Response(json.dumps(doc), media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@mcp.custom_route("/api/ui-version", methods=["GET"])
async def _api_ui_version(request: Request):
    return JSONResponse({"v": str(os.path.getmtime(_DASH_PATH))})


@mcp.custom_route("/api/thumb/{basename}", methods=["GET"])
@_safe_route
async def _api_thumb(request: Request):
    basename = request.path_params["basename"]
    if not _THUMB_NAME_RE.match(basename):
        return JSONResponse({"error": "invalid thumbnail name"}, status_code=404)
    path = _thumb_path(basename)

    def work():
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None
    data = await _run(work)
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@mcp.custom_route("/api/fleet", methods=["GET"])
@_safe_route
async def _api_fleet(request: Request):
    def work():
        group_of = {k.lower(): v for k, v in FLEET.store.groups_by_printer().items()}
        return [{**prn.snapshot(), **_camera_fields(prn), "groups": group_of.get(str(prn.name).lower(), [])}
                for prn in list(PRINTERS.values())]
    out = await _run(work)
    return JSONResponse(out)


@mcp.custom_route("/api/printer/{name}", methods=["GET"])
@_safe_route
async def _api_printer(request: Request):
    name = request.path_params["name"]

    def work():
        prn = _pick(name)
        trays = []
        for t in prn.ams_trays():
            trays.append({**t, "color_name": color_name(t["color"]) if t["color"] else None})
        return {"snapshot": {**prn.snapshot(), **_camera_fields(prn)}, "trays": trays,
                "palette": sorted(PALETTE.keys()), "materials": list(MATERIALS),
                "kind": prn.kind, "can_connect": hasattr(prn, "connect_serial"),
                "controls": controls.summary(prn),
                "detection": _detection_line(prn.name),
                **_camera_fields(prn)}
    return JSONResponse(await _run(work))


@mcp.custom_route("/api/printer/{name}/controls", methods=["GET"])
@_safe_route
async def _api_controls(request: Request):
    name = request.path_params["name"]
    if name.lower() not in PRINTERS:
        return JSONResponse({"error": f"Unknown printer '{name}'."}, status_code=404)
    return JSONResponse(await _run(lambda: controls.describe(PRINTERS[name.lower()])))


@mcp.custom_route("/api/printer/{name}/control", methods=["POST"])
@_safe_route
async def _api_control(request: Request):
    """{"action": light_on|light_off|preheat|cooldown|unload|load, "preset"?, "nozzle"?, "bed"?, "tray"?}
    -> 200 {ok, message, ...} | 400 invalid | 404 unknown printer | 409 REFUSED (busy / not ready)
    | 422 UNSUPPORTED (with reason) | 502 the printer failed."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "INVALID: the request body must be a JSON object.",
                             "code": "invalid"}, status_code=400)
    kw = {k: body.get(k) for k in ("preset", "nozzle", "bed", "tray")}
    status, payload = await _run(_control, request.path_params["name"], str(body.get("action")), **kw)
    return JSONResponse(payload, status_code=status)


# ------------------------------------------------------------- camera routes
# Every viewer shares the printer's single camera connection (see camera.py):
# a snapshot is just "latest cached frame", an MJPEG stream is "each new cached
# frame" — neither opens a socket to the printer itself.

STREAM_MAX_SECONDS = 1800    # fallback; Settings > Camera "Live view limit" wins (a forgotten tab can't hold the camera forever)
STREAM_STALL_SECONDS = 30    # end a stream if the printer sends nothing for this long


def _camera_or_error(name: str):
    """(printer, None) or (None, JSONResponse) for an unknown / camera-less printer."""
    prn = PRINTERS.get(name.lower())
    if prn is None:
        return None, JSONResponse({"error": f"unknown printer '{name}'"}, status_code=404)
    if not _has_camera(prn):
        return None, JSONResponse({"error": "no camera"}, status_code=404)
    return prn, None


@mcp.custom_route("/api/printer/{name}/snapshot", methods=["GET"])
@_safe_route
async def _api_snapshot(request: Request):
    prn, err = _camera_or_error(request.path_params["name"])
    if err:
        return err
    got = await prn.camera().aget_frame(CAMERA_WAIT_SECONDS)
    if got is None:
        return JSONResponse({"error": "camera unavailable — no frame received"}, status_code=503,
                            headers={"Cache-Control": "no-store"})
    return Response(got[0], media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/printer/{name}/stream", methods=["GET"])
@_safe_route
async def _api_stream(request: Request):
    prn, err = _camera_or_error(request.path_params["name"])
    if err:
        return err
    cam = prn.camera()

    async def frames():
        cam.add_viewer()          # a connected viewer counts as demand
        try:
            seq = 0
            started = last_new = time.monotonic()
            try:
                max_s = int(SETTINGS.get("camera.live_max_min")) * 60
            except Exception:
                max_s = STREAM_MAX_SECONDS
            while time.monotonic() - started < max_s:
                cam.touch()
                new_seq, jpeg, ts = cam.latest()
                if jpeg is not None and new_seq != seq and time.time() - ts <= 10:
                    seq, last_new = new_seq, time.monotonic()
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                elif time.monotonic() - last_new > STREAM_STALL_SECONDS:
                    return        # printer went quiet: end cleanly, the viewer can retry
                await asyncio.sleep(0.1)
        finally:
            cam.remove_viewer()

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/printer/{name}/files", methods=["GET"])
@_safe_route
async def _api_files(request: Request):
    refresh = request.query_params.get("refresh") == "1"
    name = request.path_params["name"]
    raw = await _run(list_sd_files, name, refresh_metadata=refresh)
    out = json.loads(raw)
    # quietly cache metadata for anything still uncached, so the next tap of
    # PRINT on this printer doesn't have to eat a full FTPS download itself
    if not security.caller_read_only():       # a read-only caller never makes the Hub download anything
        threading.Thread(target=_prewarm, args=(name,), daemon=True).start()
    return JSONResponse(out)


@mcp.custom_route("/api/printer/{name}/suggest", methods=["POST"])
@_safe_route
async def _api_suggest(request: Request):
    """{"filename"} -> the plan, incl. `confirm_string`: a single-use code (5 min) the start needs back."""
    body = await _optional_json(request)
    if not isinstance(body.get("filename"), str) or not body["filename"]:
        return JSONResponse({"error": "filename is required"}, status_code=400)
    name = request.path_params["name"]
    raw = await _run(suggest_mapping, name, body["filename"])
    try:
        return JSONResponse(json.loads(raw))
    except Exception:
        return JSONResponse({"error": raw}, status_code=400)


@mcp.custom_route("/api/printer/{name}/start", methods=["POST"])
@_safe_route
async def _api_start(request: Request):
    """{"filename", "ams_mapping", "confirm", "plate_clear": true, "model_override"?}. `confirm` is the
    `confirm_string` from the /suggest answer, passed back unchanged (+ "|model_override" when the human ticked the
    override). `plate_clear` is the human's tick of the bed-clear box. A dashboard browser may send another
    `ams_mapping` than the suggested one (the human picked a different spool) as long as it uses the same filament
    slots and trays that really hold filament; any other caller must send the suggested mapping."""
    body = await _optional_json(request)
    if body.get("plate_clear") is not True:             # literally true: "no", "false", 1 and "yes" are not the human's tick
        return JSONResponse({"error": "plate_clear must be confirmed"}, status_code=400)
    for k in ("filename", "ams_mapping", "confirm"):
        if k not in body:
            return JSONResponse({"error": f"{k} is required"}, status_code=400)
    name = request.path_params["name"]
    ident = _request_identity(request)
    raw = await _run(_start_print, name, body["filename"], body["ams_mapping"], body["confirm"],
                     body.get("model_override") is True, True, bool(ident is not None and ident.kind == "cookie"))
    if raw.startswith("REFUSED"):
        return JSONResponse({"error": raw}, status_code=409)
    return JSONResponse(json.loads(raw))


@mcp.custom_route("/api/printer/{name}/action", methods=["POST"])
@_safe_route
async def _api_action(request: Request):
    body = await request.json()
    name = request.path_params["name"]
    act = body.get("action")
    if act == "cancel":
        res = await _run(cancel_print, name)
    elif act == "pause":
        res = await _run(pause_print, name)
    elif act == "resume":
        res = await _run(resume_print, name)
    elif act in ("light_on", "light_off"):
        res = await _run(chamber_light, name, act == "light_on")
    elif act == "connect":
        res = await _run(connect_printer, name)
    else:
        return JSONResponse({"error": f"unknown action {act}"}, status_code=400)
    if isinstance(res, str) and res.startswith("REFUSED"):
        return JSONResponse({"error": res}, status_code=409)
    if isinstance(res, str) and res.startswith(("UNSUPPORTED", "INVALID")):
        return JSONResponse({"error": res}, status_code=422 if res.startswith("UNSUPPORTED") else 400)
    if isinstance(res, str) and res.startswith("FAILED"):
        return JSONResponse({"error": res}, status_code=502)
    return JSONResponse({"result": res})


@mcp.custom_route("/api/printer/{name}/tray", methods=["POST"])
@_safe_route
async def _api_tray(request: Request):
    body = await request.json()
    name = request.path_params["name"]
    res = await _run(set_tray_verified, name, int(body["tray_id"]), body["material"], body["color"])
    if not res["ok"]:
        return JSONResponse({"error": res["message"]}, status_code=409)
    return JSONResponse({"result": res["message"]})


@mcp.custom_route("/api/printer/{name}/delete", methods=["POST"])
@_safe_route
async def _api_delete(request: Request):
    body = await request.json()
    name = request.path_params["name"]
    raw = await _run(delete_sd_file, name, body["filename"], body["confirm"])
    if raw.startswith("REFUSED"):
        return JSONResponse({"error": raw}, status_code=409)
    return JSONResponse({"result": raw})


# ============================================================= settings + notification API
# Settings page backend: REST for the dashboard, MCP tools for agents. Both go through the same
# SettingsStore, which validates everything (all-or-nothing) and keeps secrets write-only: nothing
# in this block ever returns a secret, only "set" / "not set". These routes can point the Hub at
# any URL and change where alerts go, so they have the same trust level as add_printer.

def _settings_json(e: "settings_store.SettingsError"):
    return JSONResponse(e.payload(), status_code=e.status)


async def _optional_json(request) -> dict:
    """The request body as a dict; an empty or non-object body is {}."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _new_value_given(v) -> bool:
    return isinstance(v, str) and bool(v.strip()) and v.strip() not in settings_store.SECRET_PLACEHOLDERS


def _detector_destination(mode, url) -> str:
    """Where the detector token would be sent for this mode + address (the saved address counts while the mode is
    Off, so Off -> On at the same address is not a move)."""
    url = (url or "").strip() or (os.environ.get("DETECTOR_URL") or DETECTOR_DEFAULT_URL if mode == "local" else "")
    return "|".join(str(x) for x in notify._origin(url)) if url else ""


def _guard_secret_rebind(values, clear=()):
    """Raises SettingsError when a save would point a notification channel (or the detector) at a different
    address while keeping the secret that was saved for the old one: the stored webhook bearer, ntfy token, SMTP
    password or detector token must never silently follow a new URL somewhere else. Changing the address and typing
    the secret again in the same save is fine."""
    flat = settings_store.SettingsStore._flatten(values)
    cleared = set(clear or [])
    errors = {}
    for ch in settings_store.CHANNELS:
        sec = settings_store.SECTIONS[ch]
        saved = SETTINGS.channel_config(ch)
        merged = dict(saved)
        for f in sec["fields"]:
            key = f["key"]
            if key in flat and not f["secret"]:
                try:
                    merged[f["name"]] = settings_store.coerce(f, flat[key])
                except ValueError:
                    pass                          # the update itself reports the bad value
        if notify.destination(ch, merged) == notify.destination(ch, saved):
            continue
        for f in sec["fields"]:
            if f["secret"] and f["key"] not in cleared and SETTINGS.is_secret_set(f["key"]) \
                    and not _new_value_given(flat.get(f["key"])):
                errors[f["key"]] = (f"The address of {sec['label']} was changed, so the saved {f['label']} is not "
                                    "kept for it. Type it again in this save (or clear it).")
    if any(k in flat for k in ("detection.url", "detection.mode")) and "detection.token" not in cleared \
            and SETTINGS.is_secret_set("detection.token") and not _new_value_given(flat.get("detection.token")):
        now_mode, now_url = SETTINGS.get("detection.mode"), SETTINGS.get("detection.url")
        new_mode = flat.get("detection.mode", now_mode)
        new_dest = _detector_destination(new_mode, flat.get("detection.url", now_url))
        if new_mode != "off" and new_dest != _detector_destination(now_mode, now_url):
            errors["detection.token"] = ("The detector's address was changed, so the saved token is not kept for "
                                         "it. Type the token again in this save (or clear it).")
    if errors:
        raise settings_store.SettingsError("A saved secret is never reused for a different address.", errors)


def _security_keys(values, clear=()) -> list:
    try:
        flat = settings_store.SettingsStore._flatten(values)
    except settings_store.SettingsError:
        return []
    return sorted({k for k in flat if str(k).startswith("security.")}
                  | {k for k in (clear or []) if str(k).startswith("security.")})


@_tool
async def get_settings(section: str = "") -> str:
    """The Hub's settings (JSON): every section with its fields, current value, where the value
    comes from (saved / env / default), type, limits and help text. `section` limits it to one
    (general, notifications, telegram, ntfy, email, discord, webhook, printers, backups, camera,
    security, agents). Keys are `<section>.<name>`, e.g. notifications.quiet_start. Secrets (bot tokens,
    passwords, webhook URLs) are write-only: they show as "set" / "not set", never their value."""
    d = SETTINGS.describe()
    if security.caller_read_only():
        security.redact_settings_doc(d)
    if section:
        d["sections"] = [s for s in d["sections"] if s["id"] == section.strip().lower()]
        if not d["sections"]:
            return "REFUSED: unknown section '%s'. Known: %s" % (
                section, ", ".join(s["id"] for s in SETTINGS.schema()["sections"]))
    return json.dumps(d, indent=2)


@_tool
async def update_settings(values: dict, clear: list[str] = []) -> str:
    """Change settings. `values` maps `<section>.<name>` to the new value, e.g.
    {"notifications.quiet_enabled": true, "notifications.quiet_start": "22:00",
    "printers.preheat_auto_off_min": 10}; a {"telegram": {"chat_id": "123"}} form also works.
    All-or-nothing: one invalid value rejects everything and says which field and why. Secrets
    (telegram.bot_token, ntfy.token, email.password, discord.webhook_url, webhook.bearer) are
    write-only - send a new one to replace it, list its key in `clear` to delete it, and never
    repeat it in chat afterwards. Changing a notification channel redirects where alerts go, so
    only do it when the human asked. Use send_test_notification afterwards.
    The `security.*` settings (API key check, dashboard password) cannot be changed here on purpose:
    an agent must not be able to switch off the check that limits it. The owner changes them in the
    dashboard (Settings > Security)."""
    try:
        locked = sorted({k for k in settings_store.SettingsStore._flatten(values) if str(k).startswith("security.")}
                        | {k for k in (clear or []) if str(k).startswith("security.")})
    except settings_store.SettingsError:
        locked = []
    if locked:
        return ("REFUSED: " + ", ".join(locked) + " cannot be changed through MCP - API keys and the key check "
                "are owner-only. Ask the human to change them in the dashboard (Settings > Security).")
    try:
        _guard_secret_rebind(values, clear)
        res = await _run(SETTINGS.update, values, clear)
    except settings_store.SettingsError as e:
        return "REFUSED: " + json.dumps(e.payload())
    return json.dumps(res, indent=2)


@_tool
async def send_test_notification(channel: str) -> str:
    """Send one test message to a notification channel (telegram, ntfy, email, discord, webhook)
    using its SAVED settings, and report whether it was delivered. Ignores the master switch,
    event toggles and quiet hours. Returns JSON {channel, ok, error}."""
    return json.dumps(await _run(NOTIFIER.send_test, channel.strip().lower()), indent=2)


@mcp.custom_route("/api/settings", methods=["GET"])
@_safe_route
async def _api_settings_get(request: Request):
    doc = await _run(SETTINGS.describe)
    if security.caller_read_only():             # webhook URL, ntfy topic, SMTP user... act like passwords
        security.redact_settings_doc(doc)
    return JSONResponse(doc)


@mcp.custom_route("/api/settings", methods=["PUT"])
@_safe_route
async def _api_settings_put(request: Request):
    """{"values": {"key": value}, "clear": ["telegram.bot_token"]} (a flat object is accepted too)
    -> 200 {changed, secrets_set, secrets_cleared, settings} | 400 {error, errors: {key: why}}."""
    body = await _optional_json(request)
    values = body["values"] if isinstance(body.get("values"), dict) else {k: v for k, v in body.items() if k != "clear"}
    clear = body.get("clear") if isinstance(body.get("clear"), list) else []
    pre = AUTH.cookie_identity(dict(request.headers), "GET")
    was_required = AUTH.login_required()
    was_unlocked = AUTH.admin_unlocked(dict(request.headers))
    locked = _security_keys(values, clear)
    if locked and not _is_dashboard_admin(request):
        return _admin_refused(request, ", ".join(locked))
    if locked:
        flat = settings_store.SettingsStore._flatten(values) if isinstance(values, dict) else {}
        problem = _signin_needs_key_check(
            target_mode=flat.get("security.api_auth", AUTH.mode()),
            will_require=bool(AUTH.password_set() and "security.dashboard_password" not in (clear or []))
            or (isinstance(flat.get("security.dashboard_password"), str) and bool(flat["security.dashboard_password"].strip())),
            has_viewer=AUTH.viewer_password_set() and "security.dashboard_password" not in (clear or []))
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
    try:
        await _run(_guard_secret_rebind, values, clear)
        res = await _run(SETTINGS.update, values, clear)
    except settings_store.SettingsError as e:
        return _settings_json(e)
    res["settings"] = await _run(SETTINGS.describe)
    resp = await _keep_signed_in(request, JSONResponse(res), pre, was_required,
                                 "security.dashboard_password" in res["secrets_set"] + res["secrets_cleared"])
    return _renew_admin_unlock(request, resp, was_unlocked)


@mcp.custom_route("/api/settings/schema", methods=["GET"])
@_safe_route
async def _api_settings_schema(request: Request):
    return JSONResponse(SETTINGS.schema())


@mcp.custom_route("/api/settings/test/{channel}", methods=["POST"])
@_safe_route
async def _api_settings_test(request: Request):
    """Send a test message. Optional {"values": {...}} = the (unsaved) form fields by short name;
    a blank secret falls back to the saved one. Blocks on the network, so it runs off the loop."""
    channel = request.path_params["channel"].lower()
    if channel not in settings_store.CHANNELS:
        return JSONResponse({"channel": channel, "ok": False,
                             "error": "unknown channel (use one of: %s)" % ", ".join(settings_store.CHANNELS)},
                            status_code=404)
    body = await _optional_json(request)
    values = body.get("values") if isinstance(body.get("values"), dict) else None
    return JSONResponse(await _run(NOTIFIER.send_test, channel, values))


@mcp.custom_route("/api/settings/export", methods=["GET"])
@_safe_route
async def _api_settings_export(request: Request):
    doc = SETTINGS.export_settings()
    if security.caller_read_only():
        doc["values"] = security.redact_settings_values(doc.get("values"))
    return JSONResponse(doc,
                        headers={"Content-Disposition": 'attachment; filename="print-hub-settings.json"'})


@mcp.custom_route("/api/settings/import", methods=["POST"])
@_safe_route
async def _api_settings_import(request: Request):
    dry = request.query_params.get("dry_run", "").lower() in ("1", "true", "yes")
    body = await _optional_json(request)
    try:
        if not dry:
            await _run(_guard_secret_rebind, body.get("values") if isinstance(body.get("values"), dict) else body)
        res = await _run(SETTINGS.import_settings, body, dry)
    except settings_store.SettingsError as e:
        return _settings_json(e)
    return JSONResponse(res)


@mcp.custom_route("/api/notifications/recent", methods=["GET"])
@_safe_route
async def _api_notifications_recent(request: Request):
    def work():
        return {"notifications": NOTIFIER.recent(), "channels": NOTIFIER.enabled_channels()}
    return JSONResponse(await _run(work))

# ============================================================= end settings + notification API


# ============================================================= security: API keys, dashboard login
# The check itself is security.AuthMiddleware (wrapped around the whole app above). These are the
# routes that manage it. Keys are generated here, returned ONCE by POST /api/keys and never again;
# the key list shows names, scopes, a 6-character hint and dates only. Key management is a REST
# (dashboard) matter and deliberately NOT an MCP tool, apart from the read-only list_api_keys:
# an agent must not be able to mint itself a key or switch the check off.

def _signin_needs_key_check(target_mode, will_require, has_viewer):
    """Dashboard sign-in and a view-only password only mean something while the API key check is ON: in warn / off
    a request that simply carries no cookie is still let through, so the sign-in would be decoration (second
    security review, N-2). -> the sentence to refuse with, or None."""
    if target_mode == "on" or not (will_require or has_viewer):
        return None
    return ("Dashboard sign-in and the view-only password only protect the Hub while the API key check is ON - "
            "with it on Warn or Off, anything that sends no cookie (a script, curl, another computer) is still let "
            "through. Switch the key check to ON first (agents and scripts will then need API keys), or do it in "
            "the same step.")


def _renew_admin_unlock(request: Request, resp, was_unlocked):
    """A change that ends every admin unlock (password changes, sign-out-everywhere) must not throw the admin who
    made it back to the unlock prompt in the middle of their work: hand this browser a fresh unlock cookie."""
    if was_unlocked and (AUTH.admin_password_set() or AUTH.password_set()) and AUTH.host_ok(request.headers):
        resp.headers.append("set-cookie", AUTH.admin_cookie_header(_is_secure(request)))
    return resp


async def _keep_signed_in(request: Request, resp, pre, was_required, password_changed):
    """A password change signs out every FULL-ACCESS session and trusted device (view-only ones stay; API keys are
    never touched). The browser that made the change - if it was using a session, a trusted device or the
    old cookie - gets a fresh session, so the owner is not thrown to the login page by their own save."""
    if pre is None or pre.kind != "cookie":
        return resp
    now_required = AUTH.login_required()
    if not (password_changed or now_required != was_required):
        return resp
    if now_required:
        if AUTH.host_ok(request.headers):
            value, age = await _run(AUTH.start_session, "control", _client_ip(request), request.headers.get("user-agent", ""))
            _set_login_cookies(request, resp, session=(value, None))
        if pre.device_id:
            resp.delete_cookie(security.DEVICE_COOKIE, path="/")      # that device was forgotten with the rest
        return resp
    if pre.device_id:
        resp.delete_cookie(security.DEVICE_COOKIE, path="/")
    return _set_session_cookie(request, resp)


def _auth_error(e: "security.AuthError"):
    return JSONResponse({"error": str(e)}, status_code=e.status)


@_tool
def list_api_keys() -> str:
    """List the Hub's API keys (JSON): id, name, scope (read|control), a 6-character hint, created,
    last used (time + path), revoked - plus the current mode (on|warn|off). Never shows a key.
    Creating, revoking and deleting keys is NOT possible through MCP on purpose (an agent must not mint
    keys for itself); the owner does it in the dashboard (Security) or with POST /api/keys."""
    st = AUTH.status()
    return json.dumps({"mode": st["mode"], "dashboard_password": st["dashboard_password"],
                       "key_counts": st["key_counts"], "keys": st["keys"]}, indent=2)


@mcp.custom_route("/api/health", methods=["GET"])
async def _api_health(request: Request):
    """Public and minimal on purpose: is the Hub up. No printer data, no mode, no secrets."""
    out = {"ok": True}
    try:                                    # public: a yes/no only (paths and details are not for strangers)
        if not JOBLOG.storage_status().get("ok", True):
            out["storage"] = "degraded"
    except Exception:
        pass
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/auth/login", methods=["POST"])
@_safe_route
async def _api_auth_login(request: Request):
    """{"password": "...", "remember"?: bool, "device_name"?: "..."} -> 200 + the session cookie (or, with
    remember, a trusted-device cookie) | 401 wrong | 429 too many tries | 400 no sign-in is on.
    The owner password signs in with full access; the optional view-only password with read access.
    Only these passwords unlock this; API keys are never accepted here."""
    if not AUTH.host_ok(request.headers) or not _same_origin(request):
        return JSONResponse({"error": "request refused (unrecognised host or cross-site)"}, status_code=403)
    if not AUTH.login_required():
        return JSONResponse({"error": "No dashboard password is set - no sign-in is needed." if not AUTH.password_set()
                             else "Sign-in is turned off - no sign-in is needed."}, status_code=400)
    client = _limiter_client(request)
    # Counted BEFORE the password is checked: simultaneous guesses cannot all slip through while the first is hashed.
    wait = AUTH.limiter.acquire(client)
    if wait:
        return JSONResponse({"error": f"Too many wrong passwords from this address. Try again in {wait} seconds."},
                            status_code=429, headers={"Retry-After": str(wait)})
    try:
        body = await _optional_json(request)
        role = await _run(AUTH.check_login, body.get("password"))
    except BaseException:
        AUTH.limiter.release(client)
        raise
    if role is None:
        AUTH.limiter.failed(client)
        return JSONResponse({"error": "Wrong password."}, status_code=401)
    AUTH.limiter.succeeded(client)
    ip, ua = _client_ip(request), request.headers.get("user-agent", "")
    out = {"ok": True, "role": role, "role_words": security.ROLE_WORDS[role], "remembered": False}
    if body.get("remember") is True:
        dev, value, age = await _run(AUTH.add_device, role, body.get("device_name"), ip, ua)
        out.update(remembered=True, device={"id": dev["id"], "name": dev["name"]})
        return _set_login_cookies(request, JSONResponse(out), device=(value, age))
    value, age = await _run(AUTH.start_session, role, ip, ua)
    return _set_login_cookies(request, JSONResponse(out), session=(value, None))      # a browser-session cookie: gone when the browser closes


@mcp.custom_route("/api/auth/pair", methods=["POST"])
@_safe_route
async def _api_auth_pair(request: Request):
    """{"code": "123456"} -> 200 + a trusted-device cookie | 401 wrong or expired code | 429 too many tries.
    The code is made on the Hub (Settings > Security > Add a device) and works once, for 5 minutes. Public,
    like login: it is the sign-in for a screen with no keyboard."""
    if not AUTH.host_ok(request.headers) or not _same_origin(request):
        return JSONResponse({"error": "request refused (unrecognised host or cross-site)"}, status_code=403)
    if not AUTH.login_required():
        return JSONResponse({"error": "No sign-in is needed - there is nothing to pair with."}, status_code=400)
    client = _limiter_client(request)
    wait = AUTH.pair_limiter.acquire(client)
    if wait:
        return JSONResponse({"error": f"Too many wrong codes. Try again in {wait} seconds."},
                            status_code=429, headers={"Retry-After": str(wait)})
    body = await _optional_json(request)
    try:
        dev, value, age = await _run(AUTH.redeem_pairing, body.get("code"), _client_ip(request),
                                     request.headers.get("user-agent", ""))
    except security.AuthError as e:
        if e.status == 401:
            AUTH.pair_limiter.failed(client)
        else:
            AUTH.pair_limiter.release(client)
        return _auth_error(e)
    except BaseException:
        AUTH.pair_limiter.release(client)
        raise
    AUTH.pair_limiter.succeeded(client)
    return _set_login_cookies(request, JSONResponse({"ok": True, "role": dev["role"], "role_words": dev["role_words"],
                                                     "device": {"id": dev["id"], "name": dev["name"]}}),
                              device=(value, age))


def _clear_login_cookies(resp):
    resp.delete_cookie(security.COOKIE_NAME, path="/")
    resp.delete_cookie(security.DEVICE_COOKIE, path="/")
    resp.delete_cookie(security.ADMIN_COOKIE, path="/")
    return resp


@mcp.custom_route("/api/auth/logout", methods=["POST"])
@_safe_route
async def _api_auth_logout(request: Request):
    """Sign this browser out: its session ends and its trusted-device token (if any) is forgotten."""
    if not _same_origin(request):
        return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    await _run(AUTH.sign_out, dict(request.headers))
    return _clear_login_cookies(JSONResponse({"ok": True}))


@mcp.custom_route("/api/auth/logout-everywhere", methods=["POST"])
@_safe_route
async def _api_auth_logout_everywhere(request: Request):
    """Every session and every trusted device is signed out (this browser too). API keys keep working."""
    n = await _run(AUTH.sign_out_everywhere)
    return _clear_login_cookies(JSONResponse({"ok": True, "devices_revoked": n}))


def _whoami(request: Request):
    """The Identity of this request: what the middleware decided, else (key check Off) from the cookies."""
    ident = request.scope.get("auth")
    if ident is None or ident.kind == "none":
        ident = AUTH.cookie_identity(dict(request.headers), request.method)
    return ident if ident is not None and ident.kind != "none" else None


@mcp.custom_route("/api/auth/me", methods=["GET"])
@_safe_route
async def _api_auth_me(request: Request):
    """Who is calling and may they change things? The dashboard reads this to hide the buttons a
    view-only sign-in cannot use. Sign-in off = full access, as it always was."""
    ident = _whoami(request)
    required = AUTH.login_required()
    if ident is None and required:
        return JSONResponse({"error": "Sign in first.", "login_required": True}, status_code=401)
    role = ident.scope if ident is not None else "control"
    how = ("key" if ident.kind == "key" else "device" if ident.device_id else "session") if ident is not None else "open"
    out = {"login_required": required, "role": role, "role_words": security.ROLE_WORDS.get(role, "View-only"),
           "view_only": role != "control", "signed_in_with": how,
           "connection": security.connection_info(request.url.scheme, dict(request.headers)),
           # a fresh install that has not answered "Protect your printers" yet (set a password, or skip)
           "offer_login": bool(role == "control" and AUTH.first_run_required()),
           "min_password": security.MIN_PASSWORD,
           "admin_password": AUTH.admin_password_set(),
           # unset: nobody can manage keys / security yet - choose an admin password; locked: type it to unlock;
           # ok: unlocked for the next 30 minutes
           "admin": AUTH.admin_state(dict(request.headers), ident) if role == "control" else "view_only"}
    if ident is not None and ident.device_id:
        dev = AUTH.dash.get_device(ident.device_id)
        out["device"] = {"id": ident.device_id, "name": (dev or {}).get("name", "")}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/auth/admin-unlock", methods=["POST"])
@_safe_route
async def _api_admin_unlock(request: Request):
    """{"password": "..."} -> 200 + a 30-minute admin cookie | 401 wrong | 429 too many tries | 403 no password yet.
    The admin password (or, while none is set, the dashboard password) unlocks keys, the key check, devices and the
    other admin actions for this browser. An API key can never do this."""
    ident = _request_identity(request)
    if not AUTH.host_ok(request.headers) or not _same_origin(request):
        return JSONResponse({"error": "request refused (unrecognised host or cross-site)"}, status_code=403)
    if ident is not None and ident.kind == "key":
        return JSONResponse({"error": "An API key cannot unlock the admin actions; use the dashboard.",
                             "required_scope": "admin"}, status_code=403)
    if not (AUTH.admin_password_set() or AUTH.password_set()):
        return JSONResponse({"error": security.ADMIN_UNSET_MSG, "admin": "unset"}, status_code=403)
    client = _limiter_client(request)
    wait = AUTH.admin_limiter.acquire(client)
    if wait:
        return JSONResponse({"error": f"Too many wrong passwords from this address. Try again in {wait} seconds."},
                            status_code=429, headers={"Retry-After": str(wait)})
    try:
        body = await _optional_json(request)
        ok = await _run(AUTH.check_admin_password, body.get("password"))
    except BaseException:
        AUTH.admin_limiter.release(client)
        raise
    if not ok:
        AUTH.admin_limiter.failed(client)
        return JSONResponse({"error": "Wrong password."}, status_code=401)
    AUTH.admin_limiter.succeeded(client)
    resp = JSONResponse({"ok": True, "admin": "ok", "minutes": security.ADMIN_COOKIE_SECONDS // 60},
                        headers={"Cache-Control": "no-store"})
    resp.headers.append("set-cookie", AUTH.admin_cookie_header(_is_secure(request)))
    return resp


@mcp.custom_route("/api/auth/admin-lock", methods=["POST"])
@_safe_route
async def _api_admin_lock(request: Request):
    """Forget the admin unlock in this browser."""
    resp = JSONResponse({"ok": True, "admin": "locked"})
    resp.delete_cookie(security.ADMIN_COOKIE, path="/")
    return resp


@mcp.custom_route("/api/auth/dismiss-offer", methods=["POST"])
@_safe_route
async def _api_dismiss_offer(request: Request):
    """'Skip' on the first-run "Protect your printers" step. Not an admin action: it is recorded, the step does not
    come back, and nothing else changes (sign-in stays off; Settings > Security keeps saying so)."""
    ident = _request_identity(request)
    if ident is None or ident.kind != "cookie" or ident.scope != "control":
        return JSONResponse({"error": "Only the dashboard can do this."}, status_code=403)
    if not AUTH.host_ok(request.headers) or not _same_origin(request):
        return JSONResponse({"error": "request refused (unrecognised host or cross-site)"}, status_code=403)
    await _run(AUTH.skip_first_run)
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/auth/first-run", methods=["POST"])
@_safe_route
async def _api_first_run(request: Request):
    """{"password": "...", "device_name"?: "..."} -> 200 + a trusted-device cookie | 400 bad password | 403 not the
    dashboard | 409 nothing to set up. Only while GET /api/auth/me says offer_login (a FRESH install with no dashboard
    password that has not skipped the step). Sets the dashboard password, turns sign-in ON and makes this browser a
    trusted device so the owner stays signed in. Needs no admin rights (there is no password to unlock with yet) -
    exactly like choosing the first admin password - but must come from the dashboard in a browser, same-origin."""
    ident = _request_identity(request)
    if not AUTH.host_ok(request.headers) or not _same_origin(request, strict=True):
        return JSONResponse({"error": "request refused: this must come from the dashboard in a browser "
                                      "(unrecognised host or not same-origin)."}, status_code=403)
    if ident is None or ident.kind != "cookie" or ident.scope != "control":
        return JSONResponse({"error": "Open the dashboard in a browser to set the password.",
                             "required_scope": "admin"}, status_code=403)
    body = await _optional_json(request)
    pw = body.get("password")
    if body.get("confirm") is not None and body.get("confirm") != pw:
        return JSONResponse({"error": "The two passwords differ."}, status_code=400)
    try:
        dev, value, age = await _run(AUTH.complete_first_run, pw, _client_ip(request),
                                     request.headers.get("user-agent", ""), body.get("device_name"))
    except security.AuthError as e:
        return _auth_error(e)
    except settings_store.SettingsError as e:
        return _settings_json(e)
    resp = JSONResponse({"ok": True, "dashboard_login": True, "device": {"id": dev["id"], "name": dev["name"]},
                         "note": "Dashboard password set and sign-in is on. This browser stays signed in. "
                                 "Pair a wall screen later from Settings > Security."},
                        headers={"Cache-Control": "no-store"})
    resp.delete_cookie(security.COOKIE_NAME, path="/")         # the old no-password cookie is of no use any more
    return _set_login_cookies(request, resp, device=(value, age))


@mcp.custom_route("/api/security/admin-password", methods=["POST"])
@_safe_route
async def _api_admin_password(request: Request):
    """{"password": "new", "current_password"?: "...", "clear"?: true}. The FIRST admin password can be chosen by any
    dashboard browser that is same-origin (CSRF-checked). After that, changing it needs the admin unlock (or the
    current password in the body); clearing it needs the current password."""
    ident = _request_identity(request)
    if not AUTH.host_ok(request.headers) or not _same_origin(request, strict=True):
        return JSONResponse({"error": "request refused: this must come from the dashboard in a browser "
                                      "(unrecognised host or not same-origin)."}, status_code=403)
    if ident is None or ident.kind != "cookie" or ident.scope != "control":
        return JSONResponse({"error": "Open the dashboard in a browser to set the admin password.",
                             "required_scope": "admin"}, status_code=403)
    hdrs = dict(request.headers)
    body = await _optional_json(request)
    state = AUTH.admin_state(hdrs, ident)
    had_admin = AUTH.admin_password_set()
    if body.get("clear") is True and not had_admin:
        return JSONResponse({"error": "There is no admin password to remove."}, status_code=400)
    if state == "locked" or body.get("clear") is True:
        # an existing password has to be proven: the unlock cookie or the current password in this request
        proven = AUTH.admin_unlocked(hdrs) and body.get("clear") is not True
        if not proven:
            client = _limiter_client(request)
            wait = AUTH.admin_limiter.acquire(client)
            if wait:
                return JSONResponse({"error": f"Too many wrong passwords from this address. Try again in {wait} seconds."},
                                    status_code=429, headers={"Retry-After": str(wait)})
            ok = await _run(AUTH.check_admin_password, body.get("current_password"))
            if not ok:
                AUTH.admin_limiter.failed(client)
                return JSONResponse({"error": "The current admin password is needed and was not right.",
                                     "admin": "locked"}, status_code=401)
            AUTH.admin_limiter.succeeded(client)
    if body.get("clear") is True:
        if not had_admin:
            return JSONResponse({"error": "There is no admin password to remove."}, status_code=400)
        await _run(AUTH.clear_admin_password)
        resp = JSONResponse({"ok": True, "admin": AUTH.admin_state(hdrs, ident), "admin_password": False,
                             "note": "Admin password removed. Everyone who had unlocked the admin actions is locked again."})
        resp.delete_cookie(security.ADMIN_COOKIE, path="/")
        return resp
    pw = body.get("password")
    if not isinstance(pw, str) or not (security.MIN_PASSWORD <= len(pw) <= 200) or not pw.strip():
        return JSONResponse({"error": f"The admin password needs {security.MIN_PASSWORD}-200 characters."}, status_code=400)
    if AUTH.check_viewer_password(pw):
        return JSONResponse({"error": "The admin password must differ from the view-only password."}, status_code=400)
    await _run(AUTH.set_admin_password, pw)
    resp = JSONResponse({"ok": True, "admin": "ok", "admin_password": True,
                         "note": "Admin password saved. Anyone who had unlocked the admin actions before is locked "
                                 "again; this browser is unlocked for 30 minutes."},
                        headers={"Cache-Control": "no-store"})
    resp.headers.append("set-cookie", AUTH.admin_cookie_header(_is_secure(request)))
    return resp


@mcp.custom_route("/api/devices", methods=["GET"])
@_safe_route
async def _api_devices(request: Request):
    """Trusted devices, signed-in sessions and the active pairing code (control only)."""
    ident = _whoami(request)

    def work():
        devs, sess = AUTH.list_devices(), AUTH.list_sessions()
        for d in devs:
            d["current"] = bool(ident is not None and ident.device_id == d["id"])
        for x in sess:
            x["current"] = bool(ident is not None and ident.sid == x["id"])
        return {"login_required": AUTH.login_required(), "devices": devs, "sessions": sess,
                "pairing": AUTH.pairing_status(), "settings": AUTH.session_config()}
    return JSONResponse(await _run(work), headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/devices/revoke-others", methods=["POST"])
@_safe_route
async def _api_devices_revoke_others(request: Request):
    """Forget every trusted device except the one making this request (if it is one)."""
    ident = _whoami(request)
    n = await _run(AUTH.revoke_other_devices, ident.device_id if ident is not None else None)
    return JSONResponse({"ok": True, "revoked": n})


@mcp.custom_route("/api/devices/{dev_id}", methods=["PATCH"])
@_safe_route
async def _api_devices_rename(request: Request):
    body = await _optional_json(request)
    try:
        return JSONResponse(await _run(AUTH.rename_device, request.path_params["dev_id"], body.get("name")))
    except security.AuthError as e:
        return _auth_error(e)


@mcp.custom_route("/api/devices/{dev_id}", methods=["DELETE"])
@_safe_route
async def _api_devices_revoke(request: Request):
    ident = _whoami(request)
    try:
        rec = await _run(AUTH.revoke_device, request.path_params["dev_id"])
    except security.AuthError as e:
        return _auth_error(e)
    resp = JSONResponse({"ok": True, "device": rec})
    if ident is not None and ident.device_id == rec["id"]:
        _clear_login_cookies(resp)
    return resp


@mcp.custom_route("/api/pairing", methods=["POST"])
@_safe_route
async def _api_pairing_new(request: Request):
    """{"role"?: "control|read", "name"?: "Wall screen"} -> {code, expires_in, ...}. One code at a time; it
    works once, for 5 minutes, on the sign-in page's "Pair with a code"."""
    body = await _optional_json(request)
    try:
        res = await _run(AUTH.new_pairing, body.get("role") or "control", body.get("name") or "")
    except security.AuthError as e:
        return _auth_error(e)
    return JSONResponse(res, status_code=201, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/pairing", methods=["DELETE"])
@_safe_route
async def _api_pairing_cancel(request: Request):
    await _run(AUTH.cancel_pairing)
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/sessions/{sid}", methods=["DELETE"])
@_safe_route
async def _api_sessions_end(request: Request):
    ident = _whoami(request)
    sid = request.path_params["sid"]
    if not await _run(AUTH.dash.remove_session, sid):
        return JSONResponse({"error": f"No such session '{sid}'."}, status_code=404)
    resp = JSONResponse({"ok": True})
    if ident is not None and ident.sid == sid:
        _clear_login_cookies(resp)
    return resp


@mcp.custom_route("/api/security", methods=["GET"])
@_safe_route
async def _api_security_get(request: Request):
    """Mode, sign-in state, key list (no secrets), counts, and the tail of the warn log."""
    out = await _run(AUTH.status)
    out["connection"] = security.connection_info(request.url.scheme, dict(request.headers))
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


_CONFIG_BODY = {"session_idle_hours": "idle_hours", "session_max_days": "max_days", "device_days": "device_days"}


@mcp.custom_route("/api/security", methods=["PUT"])
@_safe_route
async def _api_security_put(request: Request):
    """{"mode"?: "on|warn|off", "dashboard_login"?: bool, "dashboard_password"?: "new",
        "clear_dashboard_password"?: true, "viewer_password"?: "new", "clear_viewer_password"?: true,
        "session_idle_hours"?: int, "session_max_days"?: int, "device_days"?: int, "dismiss_login_offer"?: true}
    -> the same document as GET (+ "changed", "revoked"). Admin only (the admin password unlocks it).
    Turning sign-in on needs the password in the same request AND the key check ON (in warn / off the sign-in
    would be decoration: a request without a cookie still gets through). A new or removed dashboard password
    signs out every full-access browser and trusted device; a new or removed view-only password signs out the
    view-only ones; turning sign-in on or off signs everybody out. The caller's own browser is handed a fresh
    session. API keys are never touched."""
    body = await _optional_json(request)
    pre = AUTH.cookie_identity(dict(request.headers), "GET")        # who is asking, before anything changes
    was_required = AUTH.login_required()
    was_unlocked = AUTH.admin_unlocked(dict(request.headers))
    values, clear = {}, []
    if body.get("mode") is not None:
        values["security.api_auth"] = body["mode"]
    pw = body.get("dashboard_password")
    has_pw = isinstance(pw, str) and bool(pw.strip())
    if has_pw:
        values["security.dashboard_password"] = pw
    clear_pw = bool(body.get("clear_dashboard_password"))
    if clear_pw:
        clear.append("security.dashboard_password")
    login = body.get("dashboard_login")
    if login is not None and not isinstance(login, bool):
        return JSONResponse({"error": "dashboard_login must be true or false."}, status_code=400)
    if clear_pw and login is True:
        return JSONResponse({"error": "Cannot turn sign-in on and remove the password in one step."}, status_code=400)
    if login is True and not has_pw and not was_required:
        return JSONResponse({"error": "To turn sign-in on, choose a password in the same step "
                                      "(so you cannot lock yourself out)."}, status_code=400)
    vp = body.get("viewer_password")
    has_vp = isinstance(vp, str) and bool(vp.strip())
    fields, cfg = {}, {}
    will_require = not clear_pw and login is not False and (was_required or login is True or has_pw)
    if has_vp:
        if not will_require:
            return JSONResponse({"error": "Turn on sign-in first - a view-only password only works with it."}, status_code=400)
        if not (security.MIN_PASSWORD <= len(vp) <= 200):
            return JSONResponse({"error": f"The view-only password needs {security.MIN_PASSWORD}-200 characters."}, status_code=400)
        same = (pw == vp) if has_pw else await _run(AUTH.check_password, vp)
        if same:
            return JSONResponse({"error": "The view-only password must differ from the full-access password."}, status_code=400)
        if await _run(AUTH.check_admin_password, vp) and AUTH.admin_password_set():
            return JSONResponse({"error": "The view-only password must differ from the admin password."}, status_code=400)
    elif has_pw and AUTH.viewer_password_set() and not body.get("clear_viewer_password") and login is not False:
        if await _run(AUTH.check_viewer_password, pw):          # the owner password may not equal the existing view-only one
            return JSONResponse({"error": "The full-access password must differ from the view-only password."}, status_code=400)
    for name, key in _CONFIG_BODY.items():
        if body.get(name) is None:
            continue
        lo, hi = security.CONFIG_LIMITS[key]
        v = body[name]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return JSONResponse({"error": f"{name} must be a whole number from {lo} to {hi}."}, status_code=400)
        cfg[key] = v
    keeps_viewer = AUTH.viewer_password_set() and not body.get("clear_viewer_password") and login is not False and not clear_pw
    problem = _signin_needs_key_check(values.get("security.api_auth", AUTH.mode()), will_require, has_vp or (will_require and keeps_viewer))
    if problem:
        return JSONResponse({"error": problem, "needs": "mode_on"}, status_code=400)
    AUTH.last_revocation = None
    try:
        res = await _run(SETTINGS.update, values, clear)
    except settings_store.SettingsError as e:
        return _settings_json(e)
    changed = res["changed"] + res["secrets_set"] + res["secrets_cleared"]
    revoked = {"sessions": 0, "devices": 0}
    for k, v in (AUTH.last_revocation or {}).items():       # the owner password was set / changed / cleared
        revoked[k] += v

    def revoke(role=None):
        r = AUTH.revoke_role(role) if role else {"sessions": AUTH.dash.clear_sessions(), "devices": AUTH.sign_out_everywhere()}
        revoked["sessions"] += r["sessions"]
        revoked["devices"] += r["devices"]
    if not clear_pw:
        if login is not None:
            fields["login_enabled"] = login
        elif has_pw:
            fields["login_enabled"] = True            # choosing a password has always meant "ask for it"
    if has_vp:
        fields["viewer_hash"] = await _run(security.hash_password, vp)
        changed.append("viewer_password")
    if body.get("clear_viewer_password") or login is False:
        had_viewer = AUTH.viewer_password_set()
        fields["viewer_hash"] = None if not has_vp else fields["viewer_hash"]
        if body.get("clear_viewer_password") and had_viewer:
            changed.append("viewer_password")
    if body.get("dismiss_login_offer"):
        fields["offer_dismissed"] = True
    toggled = ("login_enabled" in fields and bool(fields["login_enabled"]) != was_required) or (clear_pw and was_required)
    if fields:
        await _run(AUTH.dash.set_fields, **fields)
    if toggled:
        changed.append("dashboard_login")
        revoke(None)                                   # sign-in switched on or off: nothing from before stays trusted
    elif "security.dashboard_password" in changed:
        pass                                           # AUTH._settings_changed already ended the full-access ones
    if "viewer_password" in changed and not toggled:
        revoke("read")
    if cfg:
        await _run(AUTH.dash.set_config, **cfg)
        changed.append("session_settings")
    out = await _run(AUTH.status)
    out["connection"] = security.connection_info(request.url.scheme, dict(request.headers))
    out["changed"] = changed
    out["revoked"] = revoked
    out["admin"] = AUTH.admin_state(dict(request.headers), pre)
    resp = JSONResponse(out, headers={"Cache-Control": "no-store"})
    resp = await _keep_signed_in(request, resp, pre, was_required, "security.dashboard_password" in changed)
    return _renew_admin_unlock(request, resp, was_unlocked)


@mcp.custom_route("/api/keys", methods=["GET"])
@_safe_route
async def _api_keys_list(request: Request):
    return JSONResponse({"keys": AUTH.keys.list_public(), "key_counts": AUTH.keys.counts()},
                        headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/keys", methods=["POST"])
@_safe_route
async def _api_keys_create(request: Request):
    """{"name": "Claude Desktop", "scope": "read|control"} -> 201 {key, id, name, scope, prefix, created, note}.
    `key` is the ONLY time the key is ever shown - it is stored as a salted hash."""
    body = await _optional_json(request)
    try:
        rec, token = await _run(AUTH.create_key, body.get("name"), body.get("scope") or "read")
    except security.AuthError as e:
        return _auth_error(e)
    return JSONResponse({**rec, "key": token, "note": "Copy this key now. It is not stored and cannot be shown again."},
                        status_code=201, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/keys/{key_id}/revoke", methods=["POST"])
@_safe_route
async def _api_keys_revoke(request: Request):
    try:
        return JSONResponse(await _run(AUTH.revoke_key, request.path_params["key_id"]))
    except security.AuthError as e:
        return _auth_error(e)


@mcp.custom_route("/api/keys/{key_id}", methods=["DELETE"])
@_safe_route
async def _api_keys_delete(request: Request):
    try:
        return JSONResponse(await _run(AUTH.delete_key, request.path_params["key_id"]))
    except security.AuthError as e:
        return _auth_error(e)

# ============================================================= end security


# ============================================================= job history + costs
# One row per print attempt (joblog.py), priced at the Costs settings when you ask (costs.py).
# Reads are `read` scope; the CSV export is `control` (file names can carry order or customer info).

def _bad_request(e):
    return JSONResponse({"error": str(e)}, status_code=400)


def _int_param(request, name, default):
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


def _job_filter(groups, printers):
    """History filter -> (printer argument for JobLog, description). `groups` / `printers` are lists of names; the
    result is the UNION: a printer counts if it is named or is in any named group. (None, None) = no filter.
    A group nobody is in (or that does not exist) selects nothing - it never falls back to "everything"."""
    res = FLEET.store.resolve_filter(groups, printers)
    if not res["active"]:
        return None, None
    info = {"groups": res["groups"], "unknown_groups": res["unknown_groups"],
            "printers": [p for p in (printers or []) if isinstance(p, str) and p.strip()],
            "resolved_printers": res["printers"]}
    arg = res["printers"][0] if len(res["printers"]) == 1 and not groups else res["printers"]
    return arg, info


def _request_filter(request):
    """?group=A&group=B&printer=X&printer=Y -> _job_filter(...)."""
    q = request.query_params
    return _job_filter(q.getlist("group"), q.getlist("printer"))


@mcp.custom_route("/api/jobs", methods=["GET"])
@_safe_route
async def _api_jobs(request: Request):
    q = request.query_params
    pf, info = _request_filter(request)
    try:
        jobs = await _run(JOBLOG.recent_jobs, _int_param(request, "limit", 50), pf, q.get("result") or None)
    except ValueError as e:
        return _bad_request(e)
    return JSONResponse({"jobs": jobs, "currency": JOBLOG.rates().currency, **({"filter": info} if info else {})})


@mcp.custom_route("/api/jobs.csv", methods=["GET"])
@_safe_route
async def _api_jobs_csv(request: Request):
    q = request.query_params
    pf, _info = _request_filter(request)
    try:
        text = await _run(JOBLOG.export_csv, pf, q.get("period") or "all")
    except ValueError as e:
        return _bad_request(e)
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="jobs.csv"', "Cache-Control": "no-store"})


@mcp.custom_route("/api/jobs/{job_id}", methods=["GET"])
@_safe_route
async def _api_job(request: Request):
    try:
        job = await _run(JOBLOG.job, request.path_params["job_id"])
    except ValueError:
        job = None
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return JSONResponse({**job, "currency": JOBLOG.rates().currency})


@mcp.custom_route("/api/stats", methods=["GET"])
@_safe_route
async def _api_stats(request: Request):
    q = request.query_params
    pf, info = _request_filter(request)
    try:
        st = await _run(JOBLOG.stats, q.get("period") or "month", pf, None, None, FLEET.store.group_index())
    except ValueError as e:
        return _bad_request(e)
    return JSONResponse({**st, **({"filter": info} if info else {})})


@mcp.custom_route("/api/costs/file", methods=["GET"])
@_safe_route
async def _api_costs_file(request: Request):
    q = request.query_params
    last_n = _int_param(request, "last_n", 0) or None
    pf, info = _request_filter(request)
    try:
        d = await _run(JOBLOG.file_costs, q.get("name") or "", last_n, pf)
    except ValueError as e:
        return _bad_request(e)
    return JSONResponse({**d, **({"filter": info} if info else {})})


def _money(rates, v) -> str:
    return "n/a" if v is None else f"{rates.currency}{v:.2f}" if abs(v) >= 1 else f"{rates.currency}{v:.3f}"


def _tool_job_filter(printer, printers, group):
    """The history filter an MCP tool was given -> (printer argument, description) or a REFUSED text.
    `printer` (one) + `printers` (a list) + `group` (one name, or several separated by commas) are a UNION."""
    names = ([printer] if printer else []) + [p for p in (printers or []) if isinstance(p, str)]
    groups = [g.strip() for g in str(group or "").split(",") if g.strip()]
    pf, info = _job_filter(groups, names)
    if info and info["unknown_groups"]:
        known = [g["name"] for g in FLEET.store.group_index()]
        return None, None, (f"REFUSED: no group named {', '.join(repr(g) for g in info['unknown_groups'])}. "
                            f"Groups in use: {', '.join(known) or '(none - use set_printer_groups first)'}.")
    return pf, info, None


@_tool
async def recent_jobs(limit: int = 20, printer: str = "", printers: list[str] | None = None, group: str = "") -> str:
    """The latest print attempts (newest first): printer, file, result (FINISHED / FAILED / CANCELLED /
    UNKNOWN / RUNNING), parts, grams, duration and what each cost at the Costs settings. Optionally filter to one
    printer, a list of `printers`, and/or a `group` (several groups: comma-separated; see list_groups). Filters add
    up (a print counts if it ran on any named printer or any printer in a named group)."""
    pf, info, refused = _tool_job_filter(printer, printers, group)
    if refused:
        return refused
    try:
        jobs = await _run(JOBLOG.recent_jobs, limit, pf)
    except ValueError as e:
        return f"REFUSED: {e}"
    return json.dumps({"currency": JOBLOG.rates().currency, **({"filter": info} if info else {}), "jobs": jobs}, indent=2)


@_tool
async def job_stats(period: str = "month", printer: str = "", printers: list[str] | None = None, group: str = "") -> str:
    """Print totals for a period (day, week, month, year, all, or '<N>d'): jobs, success rate, hours, grams,
    cost split, true and clean cost per part, and breakdowns by printer, material and file - plus `by_group`
    (prints, success, hours, grams and cost per printer group; a printer in two groups counts in both). Optionally
    filter to one printer, a list of `printers`, and/or a `group` (several groups: comma-separated; see
    list_groups): totals then cover only those printers (union). Handy for business-vs-hobby cost at tax time."""
    pf, info, refused = _tool_job_filter(printer, printers, group)
    if refused:
        return refused
    try:
        st = await _run(JOBLOG.stats, period, pf, None, None, FLEET.store.group_index())
        return json.dumps({**st, **({"filter": info} if info else {})}, indent=2)
    except ValueError as e:
        return f"REFUSED: {e}"


@_tool
async def cost_per_part(file: str, printers: list[str] | None = None, group: str = "") -> str:
    """What one part of this file really costs: TRUE cost per part (failed and cancelled attempts charged to the
    good parts) against the CLEAN cost per part (finished prints only), the success rate, and how many attempts
    the figures are based on. `file` may be written with or without .gcode.3mf. Optionally count only attempts on
    a list of `printers` and/or a `group` (comma-separated for several; see list_groups)."""
    pf, _info, refused = _tool_job_filter("", printers, group)
    if refused:
        return refused
    try:
        d = await _run(JOBLOG.file_costs, file, None, pf)
    except ValueError as e:
        return f"REFUSED: {e}"
    r = JOBLOG.rates()
    if not d["attempts"]:
        return json.dumps({"summary": f"No finished, failed or cancelled prints of '{file}' are logged yet.", **d}, indent=2)
    rate = "no success rate yet" if d["success_rate"] is None else f"{d['success_rate'] * 100:.0f}% of attempts finished"
    if d["true_cost_per_part"] is None:
        d["summary"] = (f"'{file}': {d['attempts']} attempt(s), none finished, so there is no cost per part yet "
                        f"({_money(r, d['cost']['wasted_on_failures'])} spent on failures).")
    else:
        d["summary"] = (f"'{file}': true cost per part {_money(r, d['true_cost_per_part'])} "
                        f"(clean {_money(r, d['clean_cost_per_part'])} + waste {_money(r, d['waste_per_part'])}), "
                        f"{rate}, based on the latest {d['attempts']} attempt(s).")
    return json.dumps(d, indent=2)

# ============================================================= end job history + costs


# ============================================================= printer discovery
DISCOVERY = discovery.DiscoveryManager(FLEET.list_public)
for _path, _methods, _fn in discovery.routes(DISCOVERY):         # keep this order: /suggestions before /{scan_id}
    mcp.custom_route(_path, methods=_methods)(_safe_route(_fn))


@_tool
async def discover_printers(networks: list[str] | None = None) -> str:
    """Scan the given networks (CIDR, e.g. 192.168.1.0/24; default: the networks your printers use) for
    Bambu Lab, OctoPrint, PrusaLink and Klipper printers. Read-only on the printers, but it does open
    connections across those networks, so only do it when the human asked. Takes up to a minute."""
    try:
        return json.dumps(await _run(DISCOVERY.scan_and_wait, networks), indent=2)
    except discovery.DiscoveryError as e:
        return f"REFUSED: {e}"

# ============================================================= end printer discovery


# ============================================================= spaghetti detection
# Alert only. The loop (DETECTION, above) runs only when Settings > Detection is on; these are its windows.

_CLOUD_METADATA_IPS = {"169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254"}


def _clean_detector_url(url) -> str:
    """scheme://host[:port][/path] - no user:password@, no ?query, no #fragment (a trailing '#' or '?' would otherwise
    swallow the '/healthz' the Hub appends and turn the Test into a free GET of any path on any service)."""
    try:
        u = urlsplit((url or "").strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            return (url or "").strip()
        host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
        return f"{u.scheme}://{host}{':' + str(u.port) if u.port else ''}{u.path.rstrip('/')}"
    except ValueError:
        return (url or "").strip()


def _detector_target_refusal(mode, url):
    """A sentence if the Detection Test must not contact `url`, else None. The Test would otherwise let anyone with
    control (WARN mode, the no-password dashboard) probe services that only listen on the Hub's own machine
    (loopback), link-local addresses (cloud metadata) and so on. Allowed: the 'This machine' default / DETECTOR_URL
    in local mode (its compose name resolves on the private network) and host.docker.internal. Set
    BAMBU_MCP_ALLOW_LOOPBACK_DETECTOR=1 when the Hub runs outside Docker next to a detector on 127.0.0.1."""
    try:
        u = urlsplit(url or "")
        host = (u.hostname or "").lower()
    except ValueError:
        return None
    if not host or u.scheme not in ("http", "https"):
        return None                                     # "bad_config": the client reports that itself
    if host == "host.docker.internal":
        return None
    if mode == "local":
        for own in (DETECTOR_DEFAULT_URL, os.environ.get("DETECTOR_URL") or ""):
            try:
                if own and (urlsplit(own).hostname or "").lower() == host:
                    return None
            except ValueError:
                pass
    if (os.environ.get("BAMBU_MCP_ALLOW_LOOPBACK_DETECTOR") or "").strip().lower() in ("1", "true", "yes"):
        return None
    addrs = []
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(host, None)]
        except (OSError, ValueError):
            addrs = []
    for ip in addrs:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or str(ip) in _CLOUD_METADATA_IPS:
            return ("The Hub will not test an address on its own machine or a link-local address (that is how a "
                    "Test could be used to read other services). Use the detector's LAN address, or "
                    "host.docker.internal if it runs on the same computer as the Hub.")
    return None


def _detector_summary(health, auth) -> dict:
    """The only part of the detector's own answer that is ever shown: four fixed fields, whatever it sent."""
    info = health.get("info") if isinstance(health.get("info"), dict) else {}
    api = info.get("api")
    version = info.get("version")
    out = {"reachable": bool(health.get("ok")) or health.get("status") in ("ok", "degraded"),
           "token_ok": True if auth.ok else False if auth.code == "unauthorized" else None,
           "healthy": health.get("status") == "ok" and bool(health.get("ok")),
           "version": ""}
    if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z._+-]{1,32}", version):
        out["version"] = version
    elif isinstance(api, int) and not isinstance(api, bool) and 0 <= api < 1000:
        out["version"] = f"API {api}"
    return out


def _detection_words(mode, url, health, auth) -> dict:
    """The result of a connection test as plain sentences."""
    where = _detection_public_url(url)
    if not health["ok"]:
        if health["status"] == "bad_config":
            return {"ok": False, "reachable": False, "token_ok": None,
                    "summary": "Type the detector's address first, starting with http:// - for example http://192.168.1.20:8090."}
        if health["status"] == "unreachable":
            return {"ok": False, "reachable": False, "token_ok": None,
                    "summary": f"The detector is not reachable - tried {where}. Is it running, and is this the right address and port?"}
        return {"ok": False, "reachable": True, "token_ok": None,
                "summary": "The detector is up but not healthy. The model can take up to 2 minutes to load after a start - try again shortly."}
    if auth.ok:
        return {"ok": True, "reachable": True, "token_ok": True,
                "summary": f"Connected to the detector at {where}: it is healthy and accepted the token."}
    if auth.code == "unauthorized":
        return {"ok": False, "reachable": True, "token_ok": False,
                "summary": "The detector is running, but it rejected the token. Copy DETECTOR_TOKEN from the detector's .env file into Detector token and test again."}
    return {"ok": False, "reachable": True, "token_ok": None,
            "summary": "The detector is running, but the token check did not work (it did not answer the way a "
                       "detector should). Check the address, the port and the detector's log."}


@mcp.custom_route("/api/detection", methods=["GET"])
@_safe_route
async def _api_detection(request: Request):
    return JSONResponse(await _run(_detection_status), headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/detection/test", methods=["POST"])
@_safe_route
async def _api_detection_test(request: Request):
    """{"mode"?, "url"?, "token"?} = the form as it is on screen (unsaved is fine; a blank token falls back to the
    saved one, then the environment). Checks /healthz and then the token. The answer is in words and never
    contains the token."""
    body = await _optional_json(request)
    saved_dest = _detector_destination(SETTINGS.get("detection.mode"), SETTINGS.get("detection.url"))
    mode, url, token = _detection_target(str(body.get("mode") or ""), body.get("url"), body.get("token"))
    url = _clean_detector_url(url)
    token_dropped = False
    if not _new_value_given(body.get("token")) and token and _detector_destination(mode, url) != saved_dest:
        token, token_dropped = "", True      # a saved (or environment) token never follows a changed address
    if mode == "off":
        return JSONResponse({"ok": False, "reachable": False, "token_ok": None,
                             "summary": "Detection is set to Off. Choose 'This machine' or 'Another machine' first, then test."})

    def work():
        refusal = _detector_target_refusal(mode, url)
        if refusal:
            return {"ok": False, "reachable": False, "token_ok": None, "summary": refusal,
                    "address": _detection_public_url(url), "token_given": bool(token)}
        client = detection.DetectorClient(url, token)
        health = client.health()
        auth = client.check_auth() if health["ok"] else detection.ScoreResult(False, code="skipped")
        out = _detection_words(mode, url, health, auth)
        out["detector"] = _detector_summary(health, auth)       # never the raw body of whatever answered
        out["address"] = _detection_public_url(url)
        out["token_given"] = bool(token)
        if token_dropped:
            out["note"] = ("The saved token was not used because the address was changed - type the token again "
                           "in the form to test with it.")
        return out
    return JSONResponse(await _run(work))


@mcp.custom_route("/api/printer/{name}/detection/score", methods=["POST"])
@_safe_route
async def _api_detection_score(request: Request):
    name = request.path_params["name"]
    prn = PRINTERS.get(name.lower())
    if prn is None:
        return JSONResponse({"error": f"unknown printer '{name}'"}, status_code=404)
    res = await _run(DETECTION.score_once, prn.name)
    return JSONResponse(res, status_code=200 if res.get("ok") else 409)


@_tool
async def detection_status() -> str:
    """Spaghetti detection at a glance: whether it is on, learn-only or alerting, whether the detector is reachable,
    and for each printer the latest verdict (OK / WARNING / FAILURE), score and any camera problem. It only alerts;
    it never pauses or stops a print."""
    return json.dumps(await _run(_detection_status), indent=2)


@_tool
async def score_snapshot(printer: str) -> str:
    """Score the printer's current camera picture once, right now (0 = looks fine, higher = looks like spaghetti).
    A raw one-picture number: it does not affect the smoothed score or alerts. Sends one picture to the detector."""
    prn = _pick(printer)
    return json.dumps(await _run(DETECTION.score_once, prn.name), indent=2)

# ============================================================= end spaghetti detection



# ==== agent kit ==== (guides, version, change detection: app/agentkit.py). Keep this block LAST, after every tool.
KIT = agentkit.Kit(DATA_DIR, BUILD_REV)


@_tool
def fabrica_guide(topic: str = "start", since: str = "") -> str:
    """Fabrica's built-in guide for AI agents: what the tools are for and the rules you must follow. Call it with
    no arguments first. Topics: start, printing, library, history, renaming, changes. Every reply begins with the
    kit version; if any Fabrica reply shows a different version than you know, call topic 'changes' with
    since=<the version you know> to see what is new."""
    return KIT.guide(topic, since)


@mcp.custom_route("/api/agent-kit", methods=["GET"])
@_safe_route
async def _api_agent_kit(request: Request):
    return JSONResponse(KIT.summary())


agentkit.register(mcp, KIT)
# ==== end agent kit ====

def _count_tools() -> int:
    try:
        return len(mcp._tool_manager.list_tools())
    except Exception:
        return len(asyncio.run(mcp.list_tools()))


# Everything above has registered its tools by now: tell the Agents section the real number.
SETTINGS.set_runtime_info(tool_count=_count_tools(), mcp_path=mcp.settings.streamable_http_path, version=BUILD_REV,
                          agent_kit=KIT.finish({t.name: t.description for t in mcp._tool_manager.list_tools()},
                                               [p.name for p in mcp._prompt_manager.list_prompts()]))
mcp._mcp_server.instructions = KIT.instructions()        # ==== agent kit ==== sent to every client on connect



if __name__ == "__main__":
    import atexit
    start_event_loop()
    DETECTION.start()                     # idle until Settings > Detection is switched on
    atexit.register(DETECTION.stop)       # saves each printer's learned baseline
    mcp.run(transport="streamable-http")
