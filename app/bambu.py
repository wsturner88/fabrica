"""Fabrica core — MQTT + FTPS drivers for Bambu Lab printers.

Every protocol decision here was proven live on real hardware (P1S fw 01.07.00.00
in LAN-only mode; A1 fw 01.08.01.00 in cloud mode) on 2026-08-20:
  - MQTT v3.1.1, user `bblp` / <access code>, TLS with verification disabled
    (printer certs chain to Bambu's self-signed BBL CA).
  - ONE long-lived connection per printer. Connection churn destabilises the
    P1-series ESP32 broker and knocks other clients (OE companion) offline.
  - Print start = `project_file` with url `ftp:///<file>` (three slashes, SD root).
    P1S requires LAN-only mode for this; A1 accepts it in cloud mode.
  - `ams_mapping` selects the physical spool per file filament — the feature the
    BTT Panda Touch lacks.
  - FTPS = implicit TLS on :990, passive mode.
"""

import base64
import datetime
import hashlib
import http.client
import io
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from ftplib import FTP_TLS, error_perm

from camera import CameraStream, PollingCameraStream, fetch_jpeg
import controls as _controls
from controls import ControlUnsupported


class PrinterError(Exception):
    """Raised for a printer/backend failure that should be shown to a human,
    not surfaced as an unhandled exception / 500."""


class PrinterUnreachable(PrinterError):
    """The network could not carry the request at all (timeout, refused, no route). Nothing reached the
    printer. Distinct from a printer that answered and said no: it is a transient fault, never a reason
    to "reconnect" anything (the Hub answers it with 503, not 500/409)."""


class ConnectNeedsConfirm(PrinterError):
    """connect_serial() found the printer's controller in a real error state and a restart is the fix, but a
    human has not confirmed it. Nothing was sent. The message is the question to put to the human."""


class PrinterBusy(PrinterError):
    """The printer is not positively idle (printing, preparing, paused, unknown or offline), so a file
    operation that could disturb it was NOT started. The message begins "printer is busy", except sd_list()'s
    plain-language "The file list will appear when <name> finishes printing ..." (no listing ever taken)."""


_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s]*@")


def strip_userinfo(url):
    """`http://user:pass@host:80/x` -> `http://host:80/x`. A password typed into an address must never
    reach a log line, an error message, a snapshot field or an alert (review N-7). Non-strings and
    addresses without userinfo come back unchanged."""
    if not isinstance(url, str) or "@" not in url:
        return url
    return _USERINFO_RE.sub(r"\1", url)


def scrub_text(value) -> str:
    """str(value) with any `scheme://user:pass@` replaced by `scheme://` - for exception text that may
    quote an address."""
    return strip_userinfo(str(value))


# ---------------------------------------------------------------- HTTP drivers: no redirects, ever

REDIRECT_MSG = "printer redirected - check the URL"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A printer that answers 3xx is misconfigured (http->https proxy, a re-used IP, a captive page).
    Following it turns POST into GET (a "successful" start that never happened) and forwards the
    API key / Digest credentials to whatever host the Location names. So never follow: urllib then
    raises an HTTPError carrying the 3xx code, which the drivers report as REDIRECT_MSG."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_opener():
    """urllib opener for a LAN printer: no environment proxy, no redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def is_redirect(e) -> bool:
    return isinstance(e, urllib.error.HTTPError) and 300 <= int(e.code) < 400


# every way a request can fail below the HTTP layer, so callers get a PrinterError, never a raw
# http.client / socket exception
NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


def _require_idle_for_delete(prn, what="deleted"):
    """Driver-level guard for destructive file operations on HTTP printers: a FRESH reading (not the
    3 s cache) must show the printer connected, positively IDLE / FINISH / FAILED, with no start pending.
    Raises PrinterBusy("printer is busy ...") otherwise, so a delete that raced a print start is refused
    here even if the caller checked a moment earlier (review N-12). `what` only words the message
    ("nothing was deleted" / "nothing was uploaded")."""
    fn = getattr(prn, "fresh_snapshot", None) or prn.snapshot
    s = fn()
    state = s.get("state")
    if s.get("connected") and state in _controls.IDLE_STATES and not s.get("start_pending"):
        return
    raise PrinterBusy(f"printer is busy ({getattr(prn, 'name', '?')}: "
                      f"{'state ' + str(state) if s.get('connected') else 'not connected'}"
                      f"{': ' + scrub_text(s['error']) if s.get('error') else ''}"
                      f"{', a print start was just sent' if s.get('start_pending') else ''}) - nothing was {what}.")


# ==== rename ====
# Whole-file download with a size cap and multipart upload bodies, shared by the OctoPrint, PrusaLink and Moonraker
# drivers (file renaming downloads a file and uploads it again under the new name; see app/renamer.py).
FILE_MAX_BYTES = 500 * 1024 * 1024
_DL_CHUNK = 1024 * 1024


def read_capped(resp, max_bytes=FILE_MAX_BYTES) -> bytes:
    """The whole body of an HTTP response, refusing one bigger than `max_bytes` (a declared Content-Length is
    checked first; otherwise the read stops as soon as the cap is passed, so a huge or endless body is never buffered)."""
    cap = int(max_bytes)
    try:
        declared = int(resp.headers.get("Content-Length"))
    except Exception:
        declared = None
    if declared is not None and declared > cap:
        raise PrinterError(f"The file is larger than the {cap // 1048576} MB limit for a download.")
    buf = bytearray()
    while True:
        chunk = resp.read(_DL_CHUNK)
        if not chunk:
            break
        buf += chunk
        if len(buf) > cap:
            raise PrinterError(f"The file is larger than the {cap // 1048576} MB limit for a download.")
    return bytes(buf)


class _ChainReader:
    """A read()-able body made of several byte strings, without joining them into one more copy of the file."""

    def __init__(self, parts):
        self._parts = [memoryview(p) for p in parts]
        self._i = 0
        self.length = sum(len(p) for p in self._parts)

    def __len__(self):
        return self.length

    def read(self, n=-1):
        out = bytearray()
        while self._i < len(self._parts) and (n is None or n < 0 or len(out) < n):
            p = self._parts[self._i]
            take = len(p) if (n is None or n < 0) else n - len(out)
            out += p[:take]
            if take >= len(p):
                self._i += 1
            else:
                self._parts[self._i] = p[take:]
        return bytes(out)


def multipart_form(fields: dict, file_field: str, filename: str, data):
    """(content_type, body, length) for a multipart/form-data upload: the text `fields`, then one file part. `body` has
    .read(); the file bytes are not copied."""
    boundary = "----fabrica" + os.urandom(12).hex()
    parts = []
    for k, v in fields.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode("utf-8"))
    safe = str(filename).replace("\\", "_").replace('"', "_")
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{safe}"\r\n'
                  "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8"))
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = _ChainReader(parts)
    return f"multipart/form-data; boundary={boundary}", body, len(body)
# ==== end rename ====


# ---------------------------------------------------------------- FTPS (implicit)

class ImplicitFTPTLS(FTP_TLS):
    """ftplib speaks explicit FTPS; Bambu printers use implicit TLS on 990."""

    def connect(self, host="", port=990, timeout=30):
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


# ---------------------------------------------------------------- helpers

_MISSING = object()


_LIST_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def ftp_list_epoch(text, now=None):
    """A date from an FTP `LIST` line -> epoch seconds (like the other printer drivers give), or None when it cannot be read.

    LIST dates have no year: `Mon DD HH:MM` is a file from the last ~6 months (this year, or LAST year when this year would
    put it in the future) and `Mon DD  YYYY` an older one (midnight). The printer's own clock zone is not told, so the Hub's
    local time is used: a few hours of slack, which does not matter for ordering files."""
    m = re.fullmatch(r"\s*([A-Za-z]{3})\w*\s+(\d{1,2})\s+(?:(\d{1,2}):(\d{2})|(\d{4}))\s*", str(text or ""))
    if not m or m.group(1).lower() not in _LIST_MONTHS:
        return None
    mon, day = _LIST_MONTHS[m.group(1).lower()], int(m.group(2))
    now = time.time() if now is None else float(now)
    try:
        if m.group(5):
            return datetime.datetime(int(m.group(5)), mon, day).timestamp()
        hh, mm = int(m.group(3)), int(m.group(4))
        year = datetime.datetime.fromtimestamp(now).year
        for y in (year, year - 1):
            try:
                t = datetime.datetime(y, mon, day, hh, mm).timestamp()
            except ValueError:                       # 29 Feb in a year without one: it belongs to the earlier (leap) year
                continue
            if t <= now + 86400:                     # a day of slack for the clocks of printer and Hub
                return t
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _list_modified(text, now=None):
    """The `modified` field of a listed file: epoch seconds, or the printer's own text when it is not a date we can read."""
    t = ftp_list_epoch(text, now)
    return int(t) if t is not None else (text if isinstance(text, (int, float)) else str(text or ""))


def _close_ftp(f):
    """Politely QUIT, else just drop the socket: an FTPS session is never left open on an error."""
    try:
        f.quit()
    except Exception:
        try:
            f.close()
        except Exception:
            pass


def _has_id_list(d: dict) -> bool:
    return any(isinstance(v, list) and v and all(isinstance(x, dict) and "id" in x for x in v) for v in d.values())


def _merge_id_lists(dst: list, src: list):
    """Merge a report's list of {"id": ...} dicts (AMS units, trays) into the known one. A list that
    names only SOME of the known ids is a partial update: those entries are merged field by field and
    the rest are kept (replacing would wipe them). A list that names every known id is a full report:
    its entries replace the old ones - except an entry that itself holds id-lists (an AMS unit), which
    is merged so its trays get the same treatment. Returns the merged list, or None when the lists are
    not id-keyed dicts (plain replace)."""
    def ids(lst):
        out = []
        for x in lst:
            if not isinstance(x, dict) or "id" not in x:
                return None
            out.append(str(x["id"]))
        return out
    di, si = ids(dst), ids(src)
    if not dst or di is None or si is None:
        return None
    partial = not set(di) <= set(si)
    pos = {i: n for n, i in enumerate(di)}
    merged = list(dst) if partial else []
    for item, i in zip(src, si):
        if i in pos and (partial or _has_id_list(item)):
            cur = dict(dst[pos[i]])
            _deep_merge(cur, item)
            item = cur
        if partial and i in pos:
            merged[pos[i]] = item
        else:
            merged.append(item)
    return merged


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        cur = dst.get(k)
        if isinstance(v, dict) and isinstance(cur, dict):
            _deep_merge(cur, v)
        elif isinstance(v, list) and isinstance(cur, list):
            merged = _merge_id_lists(cur, v)
            dst[k] = merged if merged is not None else v
        elif isinstance(cur, dict) and not isinstance(v, dict):
            continue                         # a scalar / null never replaces a known sub-tree
        else:
            dst[k] = v


_MEMBER_CAP = 8 * 1024 * 1024      # no 3MF member is ever read past this (a zip bomb must not eat the Hub's RAM)


def _zread(z, name, cap=_MEMBER_CAP):
    if z.getinfo(name).file_size > cap:
        raise ValueError(f"{name} is larger than {cap} bytes")
    return z.read(name)


_ZIP_MAX_ENTRIES = 10_000          # a real .3mf holds a few dozen members; more is a hostile file
_ZIP_MAX_DIR_BYTES = 4 * 1024 * 1024


def open_zip_bounded(data: bytes) -> "zipfile.ZipFile":
    """zipfile.ZipFile(BytesIO(data)) - but a file whose central directory is huge (millions of empty members fit in
    a few MB) is refused BEFORE zipfile builds one ZipInfo per member (ValueError). The end-of-central-directory
    record is read directly: entry count and directory size; a ZIP64 marker (0xFFFF / 0xFFFFFFFF) means 'too many'."""
    tail = data[-65600:]
    i = tail.rfind(b"PK\x05\x06")
    if i >= 0 and len(tail) - i >= 22:
        entries = int.from_bytes(tail[i + 10:i + 12], "little")
        dir_size = int.from_bytes(tail[i + 12:i + 16], "little")
        if entries > _ZIP_MAX_ENTRIES or dir_size > _ZIP_MAX_DIR_BYTES:
            raise ValueError("too many entries in the archive")
    return zipfile.ZipFile(io.BytesIO(data))


def parse_3mf_meta(data: bytes) -> dict:
    """Extract print metadata from a .gcode.3mf's Metadata/slice_info.config."""
    try:
        z = open_zip_bounded(data)
    except ValueError as e:
        return {"error": str(e)}
    si = [n for n in z.namelist() if n.endswith("slice_info.config")]
    if not si:
        return {"error": "no slice_info.config"}
    try:
        t = _zread(z, si[0]).decode("utf8", "ignore")
    except ValueError as e:
        return {"error": str(e)}
    out = {}
    m = re.search(r'printer_model_id"\s+value="([^"]+)"', t)
    out["printer_model_id"] = m.group(1) if m else None
    # the slicer's printer NAME (project_settings.config: "printer_model": "Bambu Lab P1S") - a second opinion
    # the "sliced for another printer" check uses when the printer_model_id code is one it does not know
    out["printer_model"] = None
    try:
        ps = [n for n in z.namelist() if n.endswith("project_settings.config")]
        if ps:
            m = re.search(r'"printer_model"\s*:\s*"([^"]{1,60})"', _zread(z, ps[0]).decode("utf8", "ignore"))
            out["printer_model"] = m.group(1) if m else None
    except Exception:
        pass
    m = re.search(r'prediction"\s+value="(\d+)"', t)
    if m:
        s = int(m.group(1))
        out["print_seconds"] = s
        out["print_time"] = f"{s // 3600}h{(s % 3600) // 60:02d}m"
    m = re.search(r'weight"\s+value="([\d.]+)"', t)
    out["weight_g"] = float(m.group(1)) if m else None
    # Attribute order on <filament> varies between slicer versions (id/tray_info_idx/
    # type/color/used_m/used_g in no guaranteed order), so pull each tag's attributes
    # individually instead of one big ordered regex.
    filaments = []
    for tag in re.findall(r"<filament\b[^>]*/?>", t):
        m_id = re.search(r'\bid="(\d+)"', tag)
        m_type = re.search(r'\btype="([^"]*)"', tag)
        m_color = re.search(r'\bcolor="#?([0-9A-Fa-f]{6})', tag)
        if not (m_id and m_type and m_color):
            continue
        m_used_g = re.search(r'\bused_g="([\d.]+)"', tag)
        filaments.append({
            "id": int(m_id.group(1)),
            "type": m_type.group(1),
            "color": "#" + m_color.group(1).upper()[:6],
            "used_g": float(m_used_g.group(1)) if m_used_g else None,
        })
    out["filaments"] = filaments
    plates = [n for n in z.namelist() if re.match(r"Metadata/plate_\d+\.gcode$", n)]
    out["plates"] = plates
    return out


def extract_plate_png(data: bytes):
    """Pull plate 1's preview image out of a downloaded .gcode.3mf, if the
    slicer embedded one. Prefers the full-size render; falls back to the
    thumbnail. Returns None (not an error) when neither is present — some
    older/odd exports omit both."""
    z = open_zip_bounded(data)
    names = set(z.namelist())
    for cand in ("Metadata/plate_1.png", "Metadata/plate_1_small.png"):
        if cand in names:
            try:
                return _zread(z, cand)
            except ValueError:
                return None
    return None


# PrusaSlicer embeds preview images as base64 inside gcode comments, one block
# per format, e.g.:
#   ; thumbnail begin 640x480 83500
#   ; iVBORw0KGgo...
#   ; thumbnail end
# `thumbnail begin` (no suffix) is always PNG; `thumbnail_QOI`/`thumbnail_JPG`
# blocks have the same shape in other formats and are deliberately not matched
# here (the literal space after "thumbnail" excludes the "_QOI"/"_JPG" ones).
_GCODE_PNG_RE = re.compile(
    rb"; thumbnail begin (\d+)x(\d+) \d+\r?\n(.*?); thumbnail end",
    re.S,
)


def extract_gcode_png(data: bytes):
    """Pull the largest embedded PNG plate preview out of a slice of a
    PrusaSlicer gcode file's header. Returns None (not an error) when there's
    no PNG thumbnail block, or nothing in it decodes to a real PNG — some
    files carry only QOI/JPG previews, or the header slice got truncated
    mid-block."""
    best, best_area = None, -1
    for m in _GCODE_PNG_RE.finditer(data):
        area = int(m.group(1)) * int(m.group(2))
        if area <= best_area:
            continue
        body = bytearray()
        for line in m.group(3).splitlines():
            if line.startswith(b"; "):
                body += line[2:]
            elif line.startswith(b";"):
                body += line[1:]
        try:
            png = base64.b64decode(bytes(body))
        except Exception:
            continue
        if not png.startswith(b"\x89PNG"):
            continue
        best, best_area = png, area
    return best


# ---------------------------------------------------------------- hard faults (review N-10)
#
# What a Bambu reports when something is wrong, and which of it STOPS the Hub from starting a print,
# pre-heating, or moving filament. THE OWNER TUNES THIS BLOCK - the defaults are deliberately conservative
# about false blocks (a printer that is merely idle must never be refused) but they do refuse a machine
# that is sitting on an unacknowledged error.
#
#   print_error  an integer in the `print` report; 0 = none. The same 32-bit value is shown on the printer
#                as "XXXX-XXXX" (e.g. 0300_400C). It stays set after a failure until it is dismissed on
#                the printer or a new job starts.
#   hms          a list of {"attr": int, "code": int}. The currently active Health Management System
#                notices; an empty list when none. attr>>24 is the module (0x03 mc, 0x05 mainboard,
#                0x07 ams, 0x08 toolhead, 0x0C xcam); code>>16 is the severity:
#                1 fatal, 2 serious, 3 common, 4 info.
#                Source for both encodings: the community Home Assistant integration greghesp/ha-bambulab
#                (custom_components/bambu_lab/pybambu/const.py HMS_SEVERITY_LEVELS / HMS_MODULES,
#                utils.py get_HMS_severity = code >> 16, get_HMS_module = (attr >> 24) & 0xFF; models.py
#                HMSList / PrintError). Bambu publishes no official spec of the MQTT fields, and this
#                mapping has NOT been checked against a real printer here - treat it as unverified.
#
# DEFAULT RULE: a printer has a `fault` when
#   (a) print_error is non-zero, is not listed in FAULT_IGNORE_PRINT_ERRORS, and the printer is not in
#       state RUNNING (a progressing print owns its own errors), OR
#   (b) any active HMS notice has a severity in FAULT_BLOCKING_HMS_SEVERITIES and is not listed in
#       FAULT_IGNORE_HMS.
# While `fault` is set: no start, no pre-heat, no unload/load (cool-down and light still work).
# The only print_error ignored by default is 0x0300400C, the value Bambu reports after a user cancels.

FAULT_IGNORE_PRINT_ERRORS = frozenset({0x0300400C})       # user cancelled: normal, not a fault
FAULT_BLOCKING_HMS_SEVERITIES = ("fatal", "serious")      # add "common" to be stricter, remove "serious" to relax
FAULT_IGNORE_HMS = frozenset()                            # "AAAA_BBBB_CCCC_DDDD" (attr hi/lo, code hi/lo) strings
FAULT_BLOCK_ON_PRINT_ERROR = True                         # False = only HMS severities can create a fault

HMS_SEVERITIES = {1: "fatal", 2: "serious", 3: "common", 4: "info"}
HMS_MODULES = {0x03: "mc", 0x05: "mainboard", 0x07: "ams", 0x08: "toolhead", 0x0C: "xcam"}
# Messages for codes we are sure about. Anything else is shown as its code (look it up in Bambu's wiki).
KNOWN_ERROR_TEXT = {0x0300400C: "the print was cancelled"}


def _as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip(), 0)
    except (TypeError, ValueError):
        return None


def hms_code_text(attr: int, code: int) -> str:
    return f"{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}"


def bambu_fault(print_block: dict, state: str):
    """The `fault` dict for one Bambu `print` report, or None when there is none (see the rules above).
    {"source": "print_error"|"hms", "code": "0500_8011" | "HMS_...", "severity": str, "message": str,
     "hms": [...]}. Never raises."""
    try:
        hits = []
        pe = _as_int(print_block.get("print_error", 0)) or 0
        if (FAULT_BLOCK_ON_PRINT_ERROR and pe and pe not in FAULT_IGNORE_PRINT_ERRORS and state != "RUNNING"):
            code = f"{pe >> 16 & 0xFFFF:04X}_{pe & 0xFFFF:04X}"
            mod = HMS_MODULES.get(pe >> 24 & 0xFF)
            hits.append({"source": "print_error", "code": code, "severity": "unknown",
                         "message": KNOWN_ERROR_TEXT.get(pe) or
                         f"the printer reports error {code}" + (f" ({mod})" if mod else "")})
        raw = print_block.get("hms")
        for item in (raw if isinstance(raw, list) else []):
            if not isinstance(item, dict):
                continue
            attr, code = _as_int(item.get("attr")), _as_int(item.get("code"))
            if not attr and not code:
                continue
            attr, code = attr or 0, code or 0
            sev = HMS_SEVERITIES.get(code >> 16, "unknown")
            text = hms_code_text(attr, code)
            if sev in FAULT_BLOCKING_HMS_SEVERITIES and text not in FAULT_IGNORE_HMS:
                mod = HMS_MODULES.get(attr >> 24 & 0xFF)
                hits.append({"source": "hms", "code": "HMS_" + text, "severity": sev,
                             "message": f"{sev} printer notice HMS_{text}" + (f" ({mod})" if mod else "")})
        if not hits:
            return None
        first = hits[0]
        return {"source": first["source"], "code": first["code"], "severity": first["severity"],
                "message": first["message"] + (f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""),
                "hms": [h["code"] for h in hits[:8]]}
    except Exception:
        return None


# ---------------------------------------------------------------- printer driver

class BambuPrinter:
    kind = "bambu"
    MQTT_PORT = 8883       # class attribute so tests / unusual setups can point it elsewhere
    PUBLISH_WAIT_S = 3.0   # how long request() waits for paho to hand a command to the socket
    CONFIRM_WAIT_S = 10.0  # how long set_temps() / light() wait for the printer's reports to show the change
    SD_LISTING_DIR = None         # where sd_listing_<printer>.json lives; None = BAMBU_MCP_CACHE's folder, else /data
    WATCHDOG_S = 15.0      # how often the MQTT loop thread is checked (and restarted if it died)

    # Bambu's own gcode_state -> the Hub's state vocabulary. Anything not listed fails closed to UNKNOWN
    # (never "idle"): the start / heater / delete gates then refuse it.
    STATE_MAP = {"IDLE": "IDLE", "PREPARE": "PREPARE", "RUNNING": "RUNNING", "PAUSE": "PAUSE",
                 "FINISH": "FINISH", "FAILED": "FAILED",
                 "SLICING": "PREPARE"}      # the printer is preparing a job it was sent: busy, not idle

    def __init__(self, name, ip, serial, access_code, model=""):
        self.name, self.ip, self.serial = name, ip, serial
        self.access_code, self.model = access_code, model
        self.state: dict = {}
        self.last_msg = 0.0
        self._lock = threading.Lock()
        self._life = threading.RLock()        # serialises connect / disconnect / watchdog restarts
        self._client = None
        self._connected = False
        self._wd_stop = None
        self._camera = None
        self._camera_guard = threading.Lock()
        self._sd_cache = None                 # (time, rows) of the last sd_list() taken while idle (also kept on disk)
        self._sd_io = threading.Lock()        # serialises reads / writes of the on-disk listing

    # ---- camera --------------------------------------------------------

    @property
    def has_camera(self) -> bool:
        """True for printers whose chamber camera speaks the TLS :6000 JPEG
        protocol (P1 / A1 series). X1- and H2-series printers stream RTSP
        instead, which this server does not read, so they report no camera."""
        return not (self.model or "").upper().replace("BAMBU LAB", "").strip().startswith(("X1", "H2"))

    def camera(self) -> CameraStream:
        """The printer's single shared camera connection (created lazily; the
        socket itself only opens when somebody asks for a frame)."""
        with self._camera_guard:
            if self._camera is None:
                self._camera = CameraStream(self.ip, self.access_code, label=self.name)
            return self._camera

    # ---- MQTT ----------------------------------------------------------

    def _seq(self) -> str:
        """A sequence_id that is unique per command (time alone repeats within one second)."""
        with self._lock:
            self._seq_n = max(getattr(self, "_seq_n", 0) + 1, int(time.time()))
            return str(self._seq_n)

    def _new_client(self):
        import paho.mqtt.client as mqtt

        cid = f"fabrica-{self.name}"
        try:  # paho 2.x
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                            client_id=cid, protocol=mqtt.MQTTv311)
        except AttributeError:  # paho 1.x
            c = mqtt.Client(client_id=cid, protocol=mqtt.MQTTv311)
        c.username_pw_set("bblp", self.access_code)
        c.tls_set(cert_reqs=ssl.CERT_NONE)   # BBL CA is self-signed; LAN + access code is the auth
        c.tls_insecure_set(True)
        c.suppress_exceptions = True         # a callback error must never kill paho's network thread
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        # Gentle reconnect: the P1 ESP32 broker hates churn.
        c.reconnect_delay_set(min_delay=5, max_delay=120)
        return c

    def _start_client(self):
        """Create + start ONE client (caller holds _life and has stopped any previous one)."""
        c = self._new_client()
        self._client = c                     # before loop_start: on_connect may fire at once
        c.connect_async(self.ip, self.MQTT_PORT, keepalive=60)
        c.loop_start()
        return c

    def connect(self):
        with self._life:
            self._stop_client()              # connect() twice never leaves two live sessions
            c = self._start_client()
            stop = self._wd_stop = threading.Event()
        threading.Thread(target=self._watchdog, args=(stop,), name=f"mqtt-watchdog-{self.name}",
                         daemon=True).start()
        return c

    def _stop_client(self):
        """Fully stop the current MQTT client (caller holds _life). After it returns no MQTT client for
        this printer is alive."""
        if self._wd_stop is not None:
            self._wd_stop.set()
        c, self._client = self._client, None
        self._connected = False
        if c is not None:
            try:
                c.disconnect()
            except Exception:
                pass
            try:
                c.loop_stop()      # joins the network thread: no reconnect after this
            except Exception:
                pass

    def disconnect(self):
        """Cleanly stop this printer's MQTT session and camera reader. Safe to call
        twice and safe on a printer that never connected. After it returns no
        MQTT client for this printer is alive (the fleet manager relies on that:
        the printer's broker tolerates very few clients, so a replaced or removed
        printer must be fully gone before a new instance connects)."""
        with self._life:
            self._stop_client()
        with self._camera_guard:
            cam, self._camera = self._camera, None
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass

    def _watchdog(self, stop):
        """If paho's network thread ever dies (it should not: callbacks are guarded) the printer would sit
        at its last state forever with connected=True and nothing reconnecting. Notice, say so, and
        replace the client."""
        while not stop.wait(self.WATCHDOG_S):
            try:
                c = self._client
                if c is None:
                    return
                th = getattr(c, "_thread", _MISSING)
                if th is _MISSING or (th is not None and th.is_alive()):
                    continue                # (paho clears _thread when its network loop has exited)
                self._connected = False
                with self._life:
                    if stop.is_set() or self._client is not c:
                        return
                    print(f"[bambu] {self.name}: MQTT network thread died - reconnecting", flush=True)
                    try:
                        c.disconnect()
                    except Exception:
                        pass
                    try:
                        c.loop_stop()
                    except Exception:
                        pass
                    self._client = None
                    self._start_client()
            except Exception as e:      # the watchdog itself must survive
                print(f"[bambu] {self.name}: MQTT watchdog error: {e}", flush=True)

    def _on_connect(self, client, userdata, flags, rc):
        self._connected = (rc == 0)
        if rc == 0:
            try:
                client.subscribe(f"device/{self.serial}/report")
                # straight from the callback (the loop thread): publishing here must not wait on itself
                client.publish(f"device/{self.serial}/request", json.dumps(
                    {"pushing": {"sequence_id": "0", "command": "pushall",
                                 "version": 1, "push_target": 1}}))
            except Exception as e:
                print(f"[bambu] {self.name}: could not subscribe / ask for a status push: {e}", flush=True)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False

    def _on_message(self, client, userdata, msg):
        """Never raises: one malformed report must not stop the status stream."""
        try:
            payload = json.loads(msg.payload)
            if not isinstance(payload, dict):
                return                      # null / list / number / string: not a status report
            payload = {k: v for k, v in payload.items() if isinstance(v, dict)}    # print / system / info
            if not payload:
                return
            with self._lock:
                _deep_merge(self.state, payload)
                self.last_msg = time.time()
        except Exception:
            return

    def request(self, payload: dict):
        """Publish a command and make sure it really left. Raises PrinterError when the MQTT link is down
        or reconnecting (paho DROPS QoS-0 messages then, silently), when paho refuses the publish, or when
        it cannot be handed to the socket in PUBLISH_WAIT_S - so cancel / pause / pre-heat / light report
        the truth instead of 'sent'."""
        c = self._client
        if c is None or not self._connected:
            raise PrinterError(f"{self.name}: not connected to the printer (its MQTT link is down or "
                               "reconnecting) - the command was NOT sent.")
        try:
            info = c.publish(f"device/{self.serial}/request", json.dumps(payload))
        except Exception as e:
            raise PrinterError(f"{self.name}: the command could not be sent ({e}) - it was NOT sent.") from e
        if info.rc != 0:
            try:
                import paho.mqtt.client as mqtt
                why = mqtt.error_string(info.rc)
            except Exception:
                why = f"MQTT error {info.rc}"
            raise PrinterError(f"{self.name}: the printer link refused the command ({why}) - it was NOT sent.")
        if self.PUBLISH_WAIT_S:
            try:
                info.wait_for_publish(timeout=self.PUBLISH_WAIT_S)
                sent = info.is_published()
            except Exception:
                sent = False
            if not sent:
                raise PrinterError(f"{self.name}: the command was not confirmed sent within "
                                   f"{self.PUBLISH_WAIT_S:g} s - the printer link may be down.")

    def refresh(self, wait=3.0):
        """Ask for a full state push and give it a moment to arrive. Best effort: a printer that is
        unreachable simply keeps its last known state (snapshot() says connected=false)."""
        try:
            self.request({"pushing": {"sequence_id": self._seq(),
                                      "command": "pushall", "version": 1,
                                      "push_target": 1}})
        except PrinterError:
            return
        time.sleep(wait)

    # ---- state readers --------------------------------------------------

    @_controls.with_start_pending
    def snapshot(self) -> dict:
        with self._lock:
            p = dict(self.state.get("print", {}))
        raw_state = p.get("gcode_state", "UNKNOWN")
        state = self.STATE_MAP.get(str(raw_state).upper(), "UNKNOWN")
        fault = bambu_fault(p, state)
        out = {
            "printer": self.name,
            "connected": self._connected,
            "state": state,
            "job": p.get("subtask_name", ""),
            "percent": p.get("mc_percent"),
            "remaining_min": p.get("mc_remaining_time"),
            "layer": f'{p.get("layer_num", "?")}/{p.get("total_layer_num", "?")}',
            "nozzle_c": p.get("nozzle_temper"),
            "nozzle_target_c": p.get("nozzle_target_temper"),
            "bed_c": p.get("bed_temper"),
            "bed_target_c": p.get("bed_target_temper"),
            "print_error": p.get("print_error", 0),
            "fault": fault or False,           # dict (code, severity, message) when the printer is in a hard fault
            "sdcard": p.get("sdcard"),
            "age_seconds": round(time.time() - self.last_msg) if self.last_msg else None,
            "kind": "bambu",
        }
        if state != raw_state:
            out["state_raw"] = raw_state      # a state the Hub does not know: shown, never trusted
        return out

    @staticmethod
    def global_tray_id(unit_id, tray_id) -> int:
        """The id the printer's `ams_mapping` uses for a tray: unit*4 + slot for AMS units 0-3 (so unit 1
        tray 0 is 4, not a second 0), and the unit's own id for the single-slot AMS HT (128+, UNVERIFIED).
        With one AMS (unit 0) this is the slot number, exactly as before."""
        if unit_id < 4:
            return unit_id * 4 + tray_id
        return unit_id + tray_id

    @staticmethod
    def split_tray_id(tid: int):
        """Inverse of global_tray_id -> (ams_id, slot_in_unit)."""
        return (tid // 4, tid % 4) if tid < 16 else (tid, 0)

    def _tray_rows(self):
        """[(global tray id, tray dict)] for every tray the printer reports, across all AMS units."""
        with self._lock:
            ams = self.state.get("print", {}).get("ams", {})
            units = ams.get("ams", []) if isinstance(ams, dict) else []
            rows = []
            for ui, unit in enumerate(units if isinstance(units, list) else []):
                if not isinstance(unit, dict):
                    continue
                uid = _as_int(unit.get("id"))
                uid = ui if uid is None else uid
                for t in unit.get("tray", []) if isinstance(unit.get("tray"), list) else []:
                    if not isinstance(t, dict):
                        continue
                    tid = _as_int(t.get("id"))
                    rows.append((self.global_tray_id(uid, -1 if tid is None else tid), dict(t)))
        return rows

    def ams_trays(self) -> list:
        out = []
        for tid, t in self._tray_rows():
            if t.get("tray_type"):
                out.append({"tray": tid, "slot_1based": tid + 1, "type": t["tray_type"],
                            "color": "#" + str(t.get("tray_color", ""))[:6].upper()})
            else:
                out.append({"tray": tid, "slot_1based": tid + 1, "type": None, "color": None})
        return out

    def tray_raw(self, tray_id: int) -> dict:
        """The printer's own last report for one AMS tray (empty dict if none). `tray_id` is the global id
        ams_trays() reports (unit*4 + slot)."""
        for tid, t in self._tray_rows():
            if tid == tray_id:
                return t
        return {}

    # ---- actions ---------------------------------------------------------

    def start_print(self, filename: str, ams_mapping: list, plate="Metadata/plate_1.gcode"):
        from_state = self.STATE_MAP.get(str(self.state.get("print", {}).get("gcode_state", "")).upper())
        self.request({"print": {
            "sequence_id": self._seq(),
            "command": "project_file",
            "param": plate,
            "project_id": "0", "profile_id": "0",
            "task_id": "0", "subtask_id": "0",
            "subtask_name": filename.replace(".gcode.3mf", ""),
            "url": f"ftp:///{filename}",          # three slashes = SD-card root (proven dialect)
            "use_ams": True,
            "ams_mapping": ams_mapping,
            "timelapse": False, "bed_leveling": True,
            "flow_cali": False, "vibration_cali": False,
            "layer_inspect": False,
        }})
        # N-8: the command left the Hub. Until the printer reports PREPARE / RUNNING (or ~2 min pass) the
        # snapshot says start_pending, and neither the auto-off nor any control touches the heaters.
        _controls.mark_start_sent(self, from_state)

    def _simple(self, command):
        self.request({"print": {"sequence_id": self._seq(),
                                "command": command, "param": ""}})

    def stop(self):
        self._simple("stop")
        _controls.clear_start_pending(self)       # a cancelled start is no longer "settling"
    def pause(self):  self._simple("pause")
    def resume(self): self._simple("resume")

    # ---- printer controls (see controls.py) -------------------------------

    def _lights_report(self) -> list:
        with self._lock:
            lr = self.state.get("print", {}).get("lights_report")
        return [x for x in lr if isinstance(x, dict)] if isinstance(lr, list) else []

    def _light_node(self) -> str:
        """`chamber_light` (proven). Printers that report lights but have no chamber light
        (A1: toolhead `work_light`) are driven through the node they do report. Unverified."""
        nodes = [x.get("node") for x in self._lights_report()]
        if nodes and "chamber_light" not in nodes and "work_light" in nodes:
            return "work_light"
        return "chamber_light"

    def _light_state(self):
        node = self._light_node()
        for x in self._lights_report():
            if x.get("node") == node and x.get("mode") in ("on", "off"):
                return x["mode"]
        return None

    def light(self, on: bool):
        """Switch the chamber (or work) light and check the printer's lights_report shows it. Returns
        {"confirmed": True | False | None}: None = the printer reports no lights at all, so it cannot
        be checked; False = sent, but the reports still disagree after CONFIRM_WAIT_S (review N-13)."""
        want = "on" if on else "off"
        self.request({"system": {"sequence_id": self._seq(),
                                 "command": "ledctrl", "led_node": self._light_node(),
                                 "led_mode": want,
                                 "led_on_time": 500, "led_off_time": 500,
                                 "loop_times": 0, "interval_time": 0}})
        if not self._lights_report():
            return {"confirmed": None}                  # this printer reports no lights: cannot be checked
        ok = (self._light_state() if not self.last_msg
              else self._wait_for(self._light_state, lambda v: v == want))
        if ok == want:
            return {"confirmed": True}
        return {"confirmed": False,
                "unconfirmed": f"the printer still reports the light as {ok or 'unknown'}"}

    set_light = light

    def _ams_units(self) -> list:
        with self._lock:
            ams = self.state.get("print", {}).get("ams", {})
        units = ams.get("ams") if isinstance(ams, dict) else None
        return units if isinstance(units, list) else []

    def control_caps(self) -> dict:
        ams = bool(self._ams_units())
        no_ams = ("Needs an AMS: this control drives AMS unload/load, and this printer reports no AMS "
                  "(external-spool filament changes are not supported here).")
        ams_note = ("Sent as the AMS `ams_change_filament` MQTT command (target 255 = unload). "
                    "Dialect taken from public Bambu protocol notes; not yet tried on real hardware.")
        return {
            "light": {"supported": True, "state": self._light_state(),
                      "note": "chamber_light via ledctrl (work_light if that is all the printer reports)"},
            "heat": {"supported": True, "verified": False,
                     "note": "M104/M140 through the MQTT `gcode_line` command. Newer firmware with "
                             "authorization control may reject it outside LAN-only/developer mode. "
                             "Not yet tried on real hardware."},
            "unload": ({"supported": True, "verified": False, "note": ams_note} if ams
                       else {"supported": False, "reason": no_ams}),
            "load": ({"supported": True, "verified": False, "note": ams_note +
                      " Load takes the 1-based AMS slot (`tray`)."} if ams
                     else {"supported": False, "reason": no_ams}),
        }

    def controls(self) -> dict:
        return _controls.describe(self)

    def set_temps(self, nozzle=None, bed=None):
        """Heater targets through gcode_line (M104 nozzle / M140 bed); None leaves one alone."""
        lines = []
        if nozzle is not None:
            lines.append(f"M104 S{int(nozzle)}")
        if bed is not None:
            lines.append(f"M140 S{int(bed)}")
        if not lines:
            return {}
        self.request({"print": {"sequence_id": self._seq(),
                                "command": "gcode_line", "param": "\n".join(lines) + "\n"}})
        # N-13: publishing is not the printer doing it. Read the printer's own reports back.
        def targets():
            with self._lock:
                p = self.state.get("print", {})
                return p.get("nozzle_target_temper"), p.get("bed_target_temper")

        def matches(t):
            for want, got in zip((nozzle, bed), t):
                if want is None:
                    continue
                try:
                    if abs(float(got) - float(want)) > 1.0:
                        return False
                except (TypeError, ValueError):
                    return False
            return True

        # (A printer that has never sent a report cannot confirm anything: no point waiting for it.)
        t = targets() if not self.last_msg else self._wait_for(targets, matches)
        out = {"nozzle": nozzle, "bed": bed, "confirmed": matches(t)}
        if not out["confirmed"]:
            out["unconfirmed"] = (
                f"the printer has not reported a status yet" if not self.last_msg else
                f"the printer still reports targets nozzle {t[0]}, bed {t[1]} after {self.CONFIRM_WAIT_S:g} s")
        return out

    def _wait_for(self, read, ok):
        """Poll read() until ok(value) or CONFIRM_WAIT_S passes (the printer announces a changed target / light
        as a delta report, normally within a second). Returns the last value read. Sends nothing."""
        end = time.monotonic() + float(self.CONFIRM_WAIT_S or 0)
        v = read()
        while not ok(v) and time.monotonic() < end:
            time.sleep(0.1)
            v = read()
        return v

    def _change_filament_temps(self, tray_raw: dict):
        """(current nozzle temp, target temp) for an AMS filament change. The printer heats the
        nozzle itself; the target is the filament's own minimum, but never below 220 C."""
        with self._lock:
            cur = self.state.get("print", {}).get("nozzle_temper")
        try:
            tmin = int(float(tray_raw.get("nozzle_temp_min") or 0))
        except (TypeError, ValueError):
            tmin = 0
        return (int(cur) if isinstance(cur, (int, float)) else 0), min(280, max(tmin, 220))

    def unload_filament(self):
        if not self._ams_units():
            raise ControlUnsupported("Needs an AMS: this printer reports none.")
        with self._lock:
            ams = self.state.get("print", {}).get("ams", {})
        now = str(ams.get("tray_now", ""))
        raw = self.tray_raw(int(now)) if now.isdigit() and int(now) < 16 else {}
        cur, tar = self._change_filament_temps(raw)
        self.request({"print": {"sequence_id": self._seq(),
                                "command": "ams_change_filament", "ams_id": 0,
                                "target": 255, "slot_id": 255,
                                "curr_temp": cur, "tar_temp": tar}})

    def load_filament(self, tray=None):
        """Load from AMS slot `tray` (1-based). With no `tray`, the only loaded slot is used."""
        trays = [t for t in self.ams_trays() if t.get("type")]
        if tray is None:
            if len(trays) != 1:
                raise _controls.ControlInvalid(
                    "Say which AMS slot to load (tray 1-4); "
                    + (f"slots with filament: {', '.join(str(t['slot_1based']) for t in trays)}."
                       if trays else "no slot reports filament."))
            tray = trays[0]["slot_1based"]
        slot = int(tray) - 1
        raw = self.tray_raw(slot)
        if not raw.get("tray_type"):
            raise _controls.ControlInvalid(f"AMS slot {tray} reports no filament.")
        cur, tar = self._change_filament_temps(raw)
        ams_id, slot_id = self.split_tray_id(slot)      # unit 0: ams_id 0, slot_id == slot (as before)
        self.request({"print": {"sequence_id": self._seq(),
                                "command": "ams_change_filament", "ams_id": ams_id,
                                "target": slot, "slot_id": slot_id,
                                "curr_temp": cur, "tar_temp": tar}})

    def set_tray(self, tray_id: int, tray_type: str, color_hex: str,
                 temp_min=220, temp_max=270, info_idx="GFG99"):
        ams_id, slot_id = self.split_tray_id(int(tray_id))     # unit 0: ams_id 0, tray_id unchanged
        self.request({"print": {"sequence_id": self._seq(),
                                "command": "ams_filament_setting", "ams_id": ams_id,
                                "tray_id": slot_id, "tray_info_idx": info_idx,
                                "tray_color": color_hex.lstrip("#").upper()[:6] + "FF",
                                "nozzle_temp_min": temp_min,
                                "nozzle_temp_max": temp_max,
                                "tray_type": tray_type}})

    # ---- FTPS ------------------------------------------------------------

    def _require_idle(self):
        """Raise PrinterBusy unless the printer is positively idle RIGHT NOW (connected, IDLE / FINISH /
        FAILED, no start pending). One FTPS session is enough to disturb a print on these printers, so
        this is checked at the last possible moment: when the FTPS session is about to be opened, which is
        after the caller has taken the printer's FTPS lock (review N-12 / N-14)."""
        s = self.snapshot()
        state = s.get("state")
        if s.get("connected") and state in _controls.IDLE_STATES and not s.get("start_pending"):
            return
        why = (f"state {state}" if s.get("connected") else "not connected")
        if s.get("start_pending"):
            why += ", a print start was just sent"
        raise PrinterBusy(f"printer is busy ({self.name}: {why}) - no FTPS session was opened.")

    def _ftps(self) -> ImplicitFTPTLS:
        self._require_idle()
        f = ImplicitFTPTLS()
        try:
            f.connect(self.ip, 990, timeout=30)
            f.login("bblp", self.access_code)
            f.prot_p()
            f.set_pasv(True)
        except BaseException:
            _close_ftp(f)
            raise
        return f

    # ---- last-idle SD listing (kept in memory AND on disk, so it survives a Hub restart) ----

    def _sd_listing_path(self):
        d = self.SD_LISTING_DIR
        if d is None:
            cache = os.environ.get("BAMBU_MCP_CACHE")
            d = os.path.dirname(cache) if cache else ("/data" if os.path.isdir("/data") else None)
        if not d:
            return None
        safe = re.sub(r"[^a-z0-9._-]", "_", (self.name or "printer").lower()).strip(".") or "printer"
        if safe != self.name:        # sanitising could make two names collide: tell them apart
            safe += "-" + hashlib.sha1(str(self.name).encode("utf-8")).hexdigest()[:8]
        return os.path.join(d, f"sd_listing_{safe}.json")

    def _sd_persist(self, taken_at, rows):
        """Write the listing atomically (temp file + fsync + replace, mode 0600). Never raises."""
        path = self._sd_listing_path()
        if not path:
            return
        with self._sd_io:
            tmp = None
            try:
                d = os.path.dirname(path) or "."
                os.makedirs(d, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
                with os.fdopen(fd, "w") as fh:
                    json.dump({"version": 1, "printer": self.name, "serial": self.serial,
                               "taken_at": taken_at, "files": rows}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                tmp = None
            except Exception as e:
                print(f"[sd-cache] {self.name}: could not save the file listing: {e}", flush=True)
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    def _sd_forget_persisted(self):
        path = self._sd_listing_path()
        if path:
            with self._sd_io:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _sd_load_persisted(self):
        """(taken_at, rows) from disk, or None (missing, unreadable, malformed, or another printer's)."""
        path = self._sd_listing_path()
        if not path:
            return None
        try:
            with self._sd_io, open(path) as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or doc.get("version") != 1 or doc.get("printer") != self.name \
                    or (doc.get("serial") or "") != (self.serial or ""):
                return None
            taken = float(doc["taken_at"])
            # (a listing kept by an older Hub holds the printer's text date, "Jun 20 03:25": read it against the day it was taken)
            rows = [{"name": r["name"], "size": int(r["size"]), "modified": _list_modified(r.get("modified", ""), taken)}
                    for r in doc["files"] if isinstance(r, dict) and isinstance(r.get("name"), str)]
            return (taken, rows)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @property
    def sd_listing_cached_at(self):
        """When the listing sd_list() would serve from its cache was taken (epoch s), or None."""
        c = self._sd_cache
        return c[0] if c else None

    # ==== library ====
    def sd_listing_cached(self) -> list:
        """The SD-card listing the Hub already holds (memory, else the copy kept on disk) - NO network traffic and no
        FTPS session, whatever state the printer is in. [] when no listing was ever taken. Used by the file library
        to show what is on a printer without ever disturbing it."""
        c = self._sd_cache
        if c is None:
            c = self._sd_cache = self._sd_load_persisted()
        return [dict(r) for r in c[1]] if c else []
    # ==== end library ====

    def sd_list(self) -> list:
        """The `.3mf` files in the SD card root. While the printer is not positively idle no FTPS session is
        opened: the last listing taken while it WAS idle is returned, however old (nothing on the card can
        change mid-print through the Hub - uploads and deletes are refused while busy). That listing is
        also kept on disk so it survives a Hub restart. With no listing ever taken a PrinterBusy with a
        plain-language message is raised."""
        try:
            f = self._ftps()
        except PrinterBusy:
            c = self._sd_cache
            if c is None:
                c = self._sd_cache = self._sd_load_persisted()
            if c:
                return [dict(r) for r in c[1]]
            raise PrinterBusy(f"The file list will appear when {self.name} finishes printing "
                              f"(the Hub doesn't read the SD card mid-print).")
        try:
            rows = []
            f.retrlines("LIST", rows.append)
        finally:
            _close_ftp(f)
        out = []
        for r in rows:
            m = re.match(r"[-rwx]{10}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w+\s+\d+\s+[\d:]+)\s+(.+)$", r)
            if m and m.group(3).lower().endswith(".3mf"):
                out.append({"name": m.group(3), "size": int(m.group(1)),
                            "modified": _list_modified(m.group(2))})
        self._sd_cache = (time.time(), [dict(r) for r in out])
        self._sd_persist(*self._sd_cache)
        return out

    _LIST_RE = re.compile(r"[-rwx]{10}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w+\s+\d+\s+[\d:]+)\s+(.+)$")

    def sd_list_dir(self, path: str) -> list:
        """The `.3mf` files directly inside ONE folder of the SD card (not recursive),
        e.g. `sd_list_dir("cache")` -> jobs sent straight from the slicer, which Bambu
        stores in /cache. Rows look like sd_list()'s but `name` carries the folder
        (`cache/job.gcode.3mf`), which is exactly what sd_download() takes. A folder that
        does not exist gives []. `path` must be a single plain folder name (no slashes,
        no dots-prefix, no `..`). sd_list() itself is unchanged."""
        d = (path or "").strip().strip("/")
        if not d or "/" in d or "\\" in d or d.startswith(".") or ".." in d:
            raise ValueError(f"not a plain folder name: {path!r}")
        f = self._ftps()
        try:
            try:
                f.cwd(d)
            except error_perm as e:
                if str(e).startswith("550"):
                    return []          # no such folder on this printer
                raise
            rows = []
            f.retrlines("LIST", rows.append)
        finally:
            _close_ftp(f)
        out = []
        for r in rows:
            m = self._LIST_RE.match(r)       # 'd...' (folders) and 'l...' (links) never match
            if m and not m.group(3).startswith(".") and m.group(3).lower().endswith(".3mf"):
                out.append({"name": f"{d}/{m.group(3)}", "size": int(m.group(1)),
                            "modified": _list_modified(m.group(2))})
        return out

    def sd_download(self, filename: str, max_bytes: int = None) -> bytes:
        """The file's bytes. Reading stops (PrinterError, the session is closed) once it passes `max_bytes`
        (default: FILE_MAX_BYTES), so a file bigger than expected never fills the Hub's memory."""
        cap = FILE_MAX_BYTES if max_bytes is None else int(max_bytes)
        f = self._ftps()
        try:
            buf = io.BytesIO()

            def sink(chunk):
                if buf.tell() + len(chunk) > cap:
                    raise PrinterError(f"The file is larger than the {cap // 1048576} MB limit for a download.")
                buf.write(chunk)
            f.retrbinary(f"RETR {filename}", sink)
        finally:
            _close_ftp(f)
        return buf.getvalue()

    def sd_upload(self, filename: str, data: bytes):
        f = self._ftps()
        try:
            f.storbinary(f"STOR {filename}", io.BytesIO(data))
        finally:
            _close_ftp(f)
        self._sd_cache = None
        self._sd_forget_persisted()           # the card changed: the saved listing no longer matches it

    def sd_delete(self, filename: str):
        f = self._ftps()
        try:
            f.delete(filename)
        finally:
            _close_ftp(f)
        c = self._sd_cache or self._sd_load_persisted()
        if c:
            self._sd_cache = (c[0], [r for r in c[1] if r.get("name") != filename])
            self._sd_persist(*self._sd_cache)


# ---------------------------------------------------------------- OctoPrint driver
# For printers driven by OctoPrint (e.g. a Prusa MK4 on USB). Same read surface
# as BambuPrinter where it makes sense; no AMS, start goes through the job API.

_OPENER = make_opener()     # LAN printer: no env proxy, never follows a redirect


class OctoPrintPrinter:
    kind = "octoprint"
    WEBCAM_OK_TTL = 600     # re-check a working webcam this often (seconds)
    WEBCAM_NONE_TTL = 60    # ...and a missing/broken one this often

    def __init__(self, name, url, api_key, model=""):
        self.name, self.api_key, self.model = name, api_key, model
        self.url = strip_userinfo(url).rstrip("/")      # never keep (or print) a password typed into the address
        self._last = {}
        self._last_ts = 0.0
        self._ok = False
        self._cam_lock = threading.Lock()      # serialises webcam probes
        self._cam_guard = threading.Lock()     # guards lazy creation of the poller
        self._cam_probed = 0.0                 # time.time() of the last probe (0 = never)
        self._cam_ok = False                   # discovered AND a test snapshot succeeded
        self._cam_info = None                  # {"stream", "snapshot", "flip_h", ...} when discovered
        self._cam_refreshing = False
        self._camera = None

    def _redirected(self, e):
        """A 3xx from OctoPrint is an error, never something to follow (see _NoRedirect)."""
        try:
            e.close()
        except Exception:
            pass
        return PrinterError(f"OctoPrint at {self.url} answered {e.code}: {REDIRECT_MSG}.")

    def _open(self, req, timeout):
        try:
            return _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if is_redirect(e):
                raise self._redirected(e) from e
            raise

    def _get(self, path, timeout=4):
        req = urllib.request.Request(self.url + path,
                                     headers={"X-Api-Key": self.api_key})
        with self._open(req, timeout) as r:
            return json.loads(r.read())

    def _post(self, path, payload, timeout=8):
        req = urllib.request.Request(self.url + path, method="POST",
                                     data=json.dumps(payload).encode(),
                                     headers={"X-Api-Key": self.api_key,
                                              "Content-Type": "application/json"})
        with self._open(req, timeout) as r:
            return r.status

    def _delete(self, path, timeout=8):
        req = urllib.request.Request(self.url + path, method="DELETE",
                                     headers={"X-Api-Key": self.api_key})
        with self._open(req, timeout) as r:
            return r.status

    def _get_raw(self, path, headers=None, timeout=15):
        req = urllib.request.Request(self.url + path,
                                     headers={"X-Api-Key": self.api_key, **(headers or {})})
        return self._open(req, timeout)

    def connect(self):  # parity with BambuPrinter; OctoPrint is polled, not streamed
        # Warm the webcam probe in the background so the first dashboard load
        # already knows whether this printer has a camera.
        threading.Thread(target=self._refresh_webcam, daemon=True).start()

    def disconnect(self):
        """Stop the webcam poller (the only long-lived thing an OctoPrint printer
        owns). Safe to call twice."""
        with self._cam_guard:
            cam, self._camera = self._camera, None
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass

    # ---- webcam --------------------------------------------------------

    @staticmethod
    def _is_local_host(url: str) -> bool:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    def _resolve_webcam(self, settings: dict):
        """Work out the webcam URLs a *remote* client can use, from OctoPrint's
        /api/settings. Prefers the classicwebcam plugin's values, falls back to
        the core `webcam` block. A snapshot URL that points at localhost (which
        is the OctoPrint host itself, not us) is derived from the stream URL by
        swapping action=stream for action=snapshot. Returns a dict or None."""
        plug = (settings.get("plugins") or {}).get("classicwebcam") or {}
        core = settings.get("webcam") or {}

        def pick(plug_key, core_key):
            v = plug.get(plug_key)
            return v if v not in (None, "") else core.get(core_key)

        stream = pick("stream", "streamUrl") or ""
        snap = pick("snapshot", "snapshotUrl") or ""
        base = self.url + "/"
        stream_url = urllib.parse.urljoin(base, stream) if stream else ""
        snap_url = urllib.parse.urljoin(base, snap) if snap else ""
        derived = (stream_url.replace("action=stream", "action=snapshot")
                   if "action=stream" in stream_url else "")
        if not snap_url:
            snap_url = derived
        elif self._is_local_host(snap_url):
            # "localhost" here means the OctoPrint host itself, which we usually
            # can't reach; go through OctoPrint's own web server instead.
            snap_url = derived or (snap_url if self._is_local_host(self.url) else "")
        if not snap_url:
            return None
        return {"stream": stream_url, "snapshot": snap_url,
                "flip_h": bool(pick("flipH", "flipH")), "flip_v": bool(pick("flipV", "flipV")),
                "rotate90": bool(pick("rotate90", "rotate90"))}

    def _probe_webcam(self):
        """('ok', info) | ('none', None) | ('error', None). Never raises."""
        try:
            info = self._resolve_webcam(self._get("/api/settings", timeout=5))
        except Exception:
            return "error", None
        if not info:
            return "none", None
        try:
            fetch_jpeg(info["snapshot"], timeout=5)
        except Exception:
            return "error", None
        return "ok", info

    def _probe_and_store(self):   # caller holds _cam_lock
        status, info = self._probe_webcam()
        if status == "ok":
            self._cam_ok, self._cam_info = True, info
        elif status == "none" or not self._cam_ok:
            self._cam_ok, self._cam_info = False, None
        # on a transient 'error' keep the last good answer
        self._cam_probed = time.time()
        self._cam_refreshing = False

    def _refresh_webcam(self):
        with self._cam_lock:
            self._probe_and_store()

    @property
    def has_camera(self) -> bool:
        """True when the OctoPrint webcam was discovered and a test snapshot
        worked. Cached; refreshed occasionally (in the background once known)."""
        if not self._cam_probed:
            with self._cam_lock:         # first ever call: callers wait for ONE probe
                if not self._cam_probed:
                    self._probe_and_store()
        else:
            ttl = self.WEBCAM_OK_TTL if self._cam_ok else self.WEBCAM_NONE_TTL
            if time.time() - self._cam_probed > ttl and not self._cam_refreshing:
                self._cam_refreshing = True
                threading.Thread(target=self._refresh_webcam, daemon=True).start()
        return self._cam_ok

    def camera_info(self):
        """Webcam orientation flags for the dashboard (None when no camera)."""
        info = self._cam_info
        if not (self._cam_ok and info):
            return None
        return {"flip_h": info["flip_h"], "flip_v": info["flip_v"], "rotate90": info["rotate90"]}

    def camera(self) -> PollingCameraStream:
        """The printer's single shared webcam poller (thread starts on demand)."""
        with self._cam_guard:
            if self._camera is None:
                self._camera = PollingCameraStream(
                    lambda: (self._cam_info or {}).get("snapshot"), label=self.name)
            return self._camera

    def refresh(self, wait=0):
        pass

    # OctoPrint's own words for a serial link that is NOT open (GET /api/connection -> current.state). Anything
    # else (Operational, Connecting, Printing, Paused, Cancelling, ...) means the link exists: a "connect"
    # would close it and open it again, which kills a running print.
    _LINK_CLOSED = ("closed", "offline", "error")

    def connect_serial(self, confirm_restart=False):
        """Ask OctoPrint to open its serial connection to the printer (saved port/baud) - but ONLY when the link
        is closed. A reachable OctoPrint whose link is already up (idle or printing) gets nothing: re-sending
        "connect" makes OctoPrint close and reopen the port, which aborts a print. Unreachable OctoPrint:
        nothing sent (PrinterUnreachable). `confirm_restart` is accepted for parity with Moonraker and ignored.
        Invalidates the snapshot cache so the next poll picks up the change immediately."""
        try:
            cur = self._get("/api/connection")
        except urllib.error.HTTPError as e:
            raise PrinterError(f"OctoPrint refused to report its connection state ({e.code} {e.reason}); "
                               "nothing was sent.") from e
        except NET_ERRORS as e:
            raise PrinterUnreachable(f"Can't reach OctoPrint at {self.url} (network): {scrub_text(e)}. "
                                     "Nothing was sent.") from e
        except ValueError as e:
            raise PrinterError(f"{self.url} did not answer like OctoPrint (not JSON); nothing was sent.") from e
        state = str(((cur or {}).get("current") or {}).get("state") or "") if isinstance(cur, dict) else ""
        low = state.strip().lower()
        if not low:
            raise PrinterError("OctoPrint did not say what its connection state is; nothing was sent.")
        if not low.startswith(self._LINK_CLOSED):
            self._last_ts = 0.0
            if any(w in low for w in ("print", "paus", "cancel", "finish", "resum", "sending", "transfer", "starting")):
                raise PrinterBusy(f"{self.name} is busy ({state}); connect is never sent to a printer that is "
                                  "printing - nothing was sent.")
            return f"{self.name} is already connected ({state}); nothing was sent."
        try:
            self._post("/api/connection", {"command": "connect"})
        except urllib.error.HTTPError as e:
            raise PrinterError(f"OctoPrint refused the connect command ({e.code} {e.reason}).") from e
        except NET_ERRORS as e:
            raise PrinterUnreachable(f"Could not reach OctoPrint at {self.url}: {scrub_text(e)}") from e
        self._last_ts = 0.0
        return f"connect requested for {self.name}"

    @_controls.with_start_pending
    def snapshot(self) -> dict:
        now = time.time()
        if now - self._last_ts > 3:
            self._last_ts = now
            try:
                p = self._get("/api/printer?exclude=sd")
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    # Host is reachable but OctoPrint's serial connection to the
                    # printer is closed — not the same failure as an unreachable host.
                    self._ok = True
                    self._last = "DISCONNECTED"
                else:
                    self._ok = False
                    self._last = None
            except Exception:
                self._ok = False
                self._last = None
            else:
                try:
                    j = self._get("/api/job")
                except Exception:
                    j = {}
                self._ok = True
                self._last = (p, j)

        if not self._ok or not self._last:
            return {"printer": self.name, "connected": False, "state": "UNKNOWN",
                    "job": "", "percent": None, "remaining_min": None, "layer": "",
                    "nozzle_c": None, "nozzle_target_c": None, "bed_c": None,
                    "bed_target_c": None, "print_error": 0, "sdcard": None,
                    "age_seconds": None, "kind": "octoprint"}

        if self._last == "DISCONNECTED":
            return {"printer": self.name, "connected": True, "state": "DISCONNECTED",
                    "job": "", "percent": None, "remaining_min": None, "layer": "",
                    "nozzle_c": None, "nozzle_target_c": None, "bed_c": None,
                    "bed_target_c": None, "print_error": 0, "sdcard": None,
                    "age_seconds": round(time.time() - self._last_ts), "kind": "octoprint"}

        p, j = self._last
        flags = p.get("state", {}).get("flags", {})
        if flags.get("printing"):
            state = "RUNNING"
        elif flags.get("paused") or flags.get("pausing"):
            state = "PAUSE"
        elif flags.get("error") or flags.get("closedOrError"):
            state = "UNKNOWN"
        elif flags.get("cancelling") or flags.get("finishing") or flags.get("resuming"):
            state = "PREPARE"       # winding a job up / down: busy, never "idle"
        else:
            state = "IDLE"
        temps = p.get("temperature", {})
        left = j.get("progress", {}).get("printTimeLeft")
        return {
            "printer": self.name, "connected": True, "state": state,
            "job": (j.get("job", {}).get("file", {}).get("name") or "").replace(".gcode", ""),
            "percent": round(j.get("progress", {}).get("completion") or 0),
            "remaining_min": round(left / 60) if left else None,
            "layer": "",
            "nozzle_c": temps.get("tool0", {}).get("actual"),
            "nozzle_target_c": temps.get("tool0", {}).get("target"),
            "bed_c": temps.get("bed", {}).get("actual"),
            "bed_target_c": temps.get("bed", {}).get("target"),
            "print_error": 1 if flags.get("error") else 0,
            "sdcard": None,
            "age_seconds": round(time.time() - self._last_ts),
            "kind": "octoprint",
        }

    def fresh_snapshot(self) -> dict:
        """snapshot() that skips the 3 s cache: safety decisions (the pre-heat auto-off) must not act on a
        reading older than the last few seconds."""
        self._last_ts = 0.0
        return self.snapshot()

    def ams_trays(self) -> list:
        return []

    def files(self) -> list:
        out = []
        data = self._get("/api/files?recursive=true", timeout=10)

        def walk(items):
            for it in items:
                if it.get("type") == "folder":
                    walk(it.get("children", []))
                elif it.get("name", "").lower().endswith((".gcode", ".bgcode")):
                    ana = it.get("gcodeAnalysis", {})
                    secs = ana.get("estimatedPrintTime")
                    fil = ana.get("filament", {}).get("tool0", {})
                    out.append({
                        "name": it.get("path", it["name"]),
                        "size": it.get("size", 0),
                        "modified": it.get("date"),
                        "print_seconds": secs,
                        "print_time": f"{int(secs)//3600}h{(int(secs)%3600)//60:02d}m" if secs else None,
                        "filament_mm": fil.get("length"),
                        "origin": it.get("origin", "local"),
                    })
        walk(data.get("files", []))
        return out

    def download_head(self, filename: str, nbytes: int = 1048576) -> bytes:
        """Fetch just the first `nbytes` of a gcode file's header — enough to
        reach past PrusaSlicer's embedded thumbnail block without pulling the
        whole (often 100+ MB) file. Served at GET /downloads/files/{origin}/{path},
        same auth and path-encoding as everywhere else. Most OctoPrint/nginx
        setups honour the Range header and reply 206 with just that slice, but
        some proxies ignore it and hand back the whole body 200-style — read()
        with a byte cap handles both without ever pulling more than nbytes+1
        bytes off the socket."""
        path = "/downloads/files/local/" + urllib.parse.quote(filename, safe="/")
        try:
            with self._get_raw(path, headers={"Range": f"bytes=0-{nbytes - 1}"}) as r:
                data = r.read(nbytes + 1)
        except urllib.error.HTTPError as e:
            raise PrinterError(f"OctoPrint refused the file download ({e.code} {e.reason}).") from e
        except NET_ERRORS as e:
            raise PrinterError(f"Could not reach OctoPrint at {self.url}: {scrub_text(e)}") from e
        return data[:nbytes]

    def download_tail(self, filename: str, size: int, nbytes: int = 65536):
        """The last `nbytes` of a gcode file (PrusaSlicer / Orca write their config block - material, layer height - at the
        END). Needs the file size (from the listing). Returns None when the server ignored the Range header (a 200 would
        be the whole file; it is never read) and b"" for an empty file. Same auth and path-encoding as download_head."""
        if not isinstance(size, int) or size <= 0:
            return b""
        start = max(0, size - nbytes)
        path = "/downloads/files/local/" + urllib.parse.quote(filename, safe="/")
        try:
            with self._get_raw(path, headers={"Range": f"bytes={start}-{size - 1}"}) as r:
                if start > 0 and getattr(r, "status", 206) != 206:
                    return None
                return r.read(nbytes + 1)[-nbytes:]
        except urllib.error.HTTPError as e:
            raise PrinterError(f"OctoPrint refused the file download ({e.code} {e.reason}).") from e
        except NET_ERRORS as e:
            raise PrinterError(f"Could not reach OctoPrint at {self.url}: {scrub_text(e)}") from e

    # ==== rename ====
    def download(self, filename: str, max_bytes: int = FILE_MAX_BYTES) -> bytes:
        """The whole file (capped at `max_bytes`; PrinterError beyond it)."""
        path = "/downloads/files/local/" + urllib.parse.quote(filename, safe="/")
        try:
            with self._get_raw(path, timeout=60) as r:
                return read_capped(r, max_bytes)
        except urllib.error.HTTPError as e:
            raise PrinterError(f"OctoPrint refused the file download ({e.code} {e.reason}).") from e
        except NET_ERRORS as e:
            raise PrinterError(f"Could not reach OctoPrint at {self.url}: {scrub_text(e)}") from e

    def upload(self, filename: str, data, timeout: float = 300):
        """BETA - not yet verified on real hardware. Upload a file to OctoPrint's local storage:
        POST /api/files/local (multipart: file, select=false, print=false, path=<folder>). Refused unless the printer
        is positively idle right now. It never selects or prints the file. The caller makes sure the name is free."""
        _require_idle_for_delete(self, "uploaded")
        folder, _sep, base = filename.rpartition("/")
        fields = {"select": "false", "print": "false"}
        if folder:
            fields["path"] = folder
        ctype, body, length = multipart_form(fields, "file", base, data)
        req = urllib.request.Request(self.url + "/api/files/local", method="POST", data=body,
                                     headers={"X-Api-Key": self.api_key, "Content-Type": ctype,
                                              "Content-Length": str(length)})
        try:
            with self._open(req, timeout) as r:
                r.read(65536)
        except urllib.error.HTTPError as e:
            raise PrinterError(f"OctoPrint refused the upload ({e.code} {e.reason}).") from e
        except NET_ERRORS as e:
            raise PrinterError(f"Could not reach OctoPrint at {self.url}: {scrub_text(e)}") from e
    # ==== end rename ====

    def start_print(self, filename, ams_mapping=None, origin="local"):
        # OctoPrint file paths are folder/name and routinely contain spaces; urllib
        # refuses a URL with any raw space, so percent-encode the path (keeping the
        # folder separators) before it goes into the request line.
        path = urllib.parse.quote(filename, safe="/")
        try:
            self._post(f"/api/files/{origin}/{path}", {"command": "select", "print": True})
        except urllib.error.HTTPError as e:
            raise PrinterError(
                f"OctoPrint refused the print ({e.code} {e.reason}) — is the printer "
                "connected? Tap Connect and try again."
            ) from e
        except NET_ERRORS as e:
            raise PrinterError(f"Could not reach OctoPrint at {self.url}: {scrub_text(e)}") from e
        _controls.mark_start_sent(self)      # N-8: accepted; the auto-off waits until the print shows up

    def _job_command(self, payload, verb):
        try:
            self._post("/api/job", payload)
        except urllib.error.HTTPError as e:
            raise PrinterError(f"OctoPrint refused to {verb} ({e.code} {e.reason}).") from e
        except NET_ERRORS as e:
            raise PrinterError(f"Could not reach OctoPrint at {self.url}: {scrub_text(e)}") from e

    def stop(self):
        self._job_command({"command": "cancel"}, "cancel")
        _controls.clear_start_pending(self)
    def pause(self):  self._job_command({"command": "pause", "action": "pause"}, "pause")
    def resume(self): self._job_command({"command": "pause", "action": "resume"}, "resume")

    # ---- printer controls (see controls.py) -------------------------------

    def control_caps(self) -> dict:
        fw = ("Sent as G-code through OctoPrint's /api/printer/command. Prusa firmware (M701 load / "
              "M702 unload); other Marlin builds may refuse or ignore it. Not yet tried on real hardware.")
        return {
            "light": {"supported": False, "state": None,
                      "reason": "OctoPrint has no standard light API (it depends on a plugin or a "
                                "printer-specific G-code), so the Hub does not guess."},
            "heat": {"supported": True,
                     "note": "OctoPrint /api/printer/tool and /api/printer/bed targets"},
            "unload": {"supported": True, "verified": False, "note": fw},
            "load": {"supported": True, "verified": False, "note": fw},
        }

    def controls(self) -> dict:
        return _controls.describe(self)

    def set_light(self, on: bool):
        raise ControlUnsupported("OctoPrint has no standard light API; the Hub does not guess.")

    def _ctl_post(self, path, payload, what):
        try:
            self._post(path, payload)
        except urllib.error.HTTPError as e:
            e.close()
            raise PrinterError(f"OctoPrint refused to {what} ({e.code} {e.reason}) - is the "
                               "printer connected?") from e
        except NET_ERRORS as e:
            raise PrinterError(f"Could not reach OctoPrint at {self.url}: {scrub_text(e)}") from e
        self._last_ts = 0.0          # next snapshot() re-reads the new targets

    def set_temps(self, nozzle=None, bed=None):
        if nozzle is not None:
            self._ctl_post("/api/printer/tool",
                           {"command": "target", "targets": {"tool0": int(nozzle)}}, "set the nozzle temperature")
        if bed is not None:
            self._ctl_post("/api/printer/bed", {"command": "target", "target": int(bed)},
                           "set the bed temperature")
        return {"nozzle": nozzle, "bed": bed}

    def unload_filament(self):
        self._ctl_post("/api/printer/command", {"command": "M702"}, "unload the filament")

    def load_filament(self, tray=None):
        self._ctl_post("/api/printer/command", {"command": "M701"}, "load the filament")

    def sd_delete(self, filename, origin="local"):
        _require_idle_for_delete(self)       # a fresh reading, like PrusaLink / Moonraker (review N-12 pattern)
        # Same space/URL problem as start_print: percent-encode the path before
        # it goes into the request line, or http.client raises InvalidURL.
        path = urllib.parse.quote(filename, safe="/")
        try:
            self._delete(f"/api/files/{origin}/{path}")
        except urllib.error.HTTPError as e:
            raise PrinterError(f"OctoPrint refused the delete ({e.code} {e.reason}).") from e
        except NET_ERRORS as e:
            raise PrinterError(f"Could not reach OctoPrint at {self.url}: {scrub_text(e)}") from e
