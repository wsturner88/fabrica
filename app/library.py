"""Global File Library - one place on the Hub for the print files of the whole fleet.

What lives here
  * LIBRARY STORE   <data>/library/files/<name>          the files themselves (0600, written atomically)
                    <data>/library/index.json            what the Hub knows about each file (0600, atomic)
                    <data>/library/thumbs/<sha1>.png     plate previews
                    <data>/library/.incoming/            half-written uploads (never listed, swept at start)
  * METADATA        parsed with the SAME code the printer pages use (bambu.parse_3mf_meta, extract_plate_png,
                    extract_gcode_png, modelcheck, joblog.parts_for_plate) plus a few extra fields read from the
                    same files (layer height, layers, nozzle, temperatures, infill, supports, slicer).
  * MERGE           library files + the files the printers are known to hold (cached listings only) -> one row per
                    file, "on: P1S, A1 - in library".

This module knows nothing about printers, the web layer or MCP. server.py wires the REST routes and the MCP tools
and does everything that touches a printer (so the busy guards live in ONE place: the ones that already protect
the printer pages). Nothing here ever starts a print.

Safety rules of the store
  * File names are untrusted. `sanitize_name` REFUSES (it never "fixes") anything with a path separator, a
    control character, a leading dot, "..", a Windows-hostile character, or an extension that is not one of
    .gcode.3mf / .3mf / .gcode / .bgcode. Folders are a flat label (one level), same rules, no dots at all.
  * Files are flat on disk: <root>/files/<name>. The final path is re-checked to be a direct child of that folder
    and never a symlink.
  * Uploads are streamed into <root>/.incoming (0600, O_EXCL) with a running size cap and moved into place with
    os.replace only when complete and parsed - a crash leaves a hidden temp file, never a half-file in the list.
"""
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import zipfile

import modelcheck
import platecontents
from bambu import META_VERSION, extract_gcode_png, extract_plate_png, open_zip_bounded, parse_3mf_meta, plate_number

ALLOWED_EXT = (".gcode.3mf", ".3mf", ".bgcode", ".gcode")
DEFAULT_MAX_MB = 500
MAX_PARSE_BYTES = 128 * 1024 * 1024        # larger files are stored but their details are not read (RAM)
MAX_NAME_CHARS = 120
MAX_NAME_BYTES = 200
MAX_FOLDER_CHARS = 40
MAX_FOLDERS = 100
MAX_FILES = 5000
_FREE_MARGIN = 50 * 1024 * 1024            # always leave this much free space
_MEMBER_CAP = 8 * 1024 * 1024              # no archive member is read past this (zip bombs)
_HEAD = 1048576
_TAIL = 262144
_NAME_PUNCT = set(" _-.+()[]&,'#@!~=")
_THUMB_RE = re.compile(r"^[0-9a-f]{40}\.png$")


def max_file_bytes() -> int:
    """The per-file cap: FABRICA_LIBRARY_MAX_MB (whole megabytes, 1..4096) or 500 MB."""
    try:
        mb = int(os.environ.get("FABRICA_LIBRARY_MAX_MB", DEFAULT_MAX_MB))
    except ValueError:
        mb = DEFAULT_MAX_MB
    return max(1, min(mb, 4096)) * 1024 * 1024


class LibraryError(Exception):
    """A request the library refuses. str(e) is written for a person; `status` is the HTTP code."""
    status = 400


class InvalidName(LibraryError):
    status = 400


class NotFound(LibraryError):
    status = 404


class Exists(LibraryError):
    status = 409


class TooLarge(LibraryError):
    status = 413


class NoSpace(LibraryError):
    status = 507


# ------------------------------------------------------------------ names

def _has_bad_char(s: str) -> bool:
    for ch in s:
        if unicodedata.category(ch)[0] == "C":           # control, format, unassigned, surrogate
            return True
        if not (ch.isalnum() or ch in _NAME_PUNCT):
            return True
    return False


def ext_of(name: str) -> str:
    low = name.lower()
    for e in ALLOWED_EXT:
        if low.endswith(e):
            return e
    return ""


def kind_of(name: str) -> str:
    e = ext_of(name)
    return {".gcode.3mf": "3mf", ".3mf": "3mf", ".gcode": "gcode", ".bgcode": "bgcode"}.get(e, "")


def sanitize_name(raw) -> str:
    """The library name for `raw`, or InvalidName. Strict: it refuses, it does not repair."""
    if not isinstance(raw, str):
        raise InvalidName("The file name must be text.")
    name = unicodedata.normalize("NFC", raw).strip()
    if not name:
        raise InvalidName("The file name is empty.")
    if len(name) > MAX_NAME_CHARS or len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise InvalidName(f"The file name is too long (at most {MAX_NAME_CHARS} characters).")
    if "/" in name or "\\" in name or "\x00" in name:
        raise InvalidName("A file name cannot contain / or \\ (folders are chosen separately).")
    if name.startswith(".") or ".." in name or name.endswith((".", " ")):
        raise InvalidName("A file name cannot start with a dot, contain '..' or end with a dot or space.")
    if _has_bad_char(name):
        raise InvalidName("The file name has a character that is not allowed. Letters, digits, spaces and "
                          "_ - . + ( ) [ ] & , ' # @ ! ~ = are fine.")
    if not ext_of(name):
        raise InvalidName("Only .3mf, .gcode.3mf, .gcode and .bgcode files can be kept in the library.")
    if len(name) == len(ext_of(name)):
        raise InvalidName("The file name has nothing before the extension.")
    return name


def name_from_printer_path(raw) -> str:
    """A library name for a file that lives on a printer (its path may be 'cache/job.gcode.3mf'): the last
    segment, with anything not allowed turned into '_'. Still goes through sanitize_name."""
    last = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    last = unicodedata.normalize("NFC", last).strip().lstrip(".")
    last = "".join("_" if (unicodedata.category(c)[0] == "C" or not (c.isalnum() or c in _NAME_PUNCT)) else c
                   for c in last)
    last = last.replace("..", "_").rstrip(". ")
    return sanitize_name(last)


def sanitize_folder(raw) -> str:
    """'' (no folder) or a clean one-level folder label; InvalidName otherwise."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise InvalidName("The folder name must be text.")
    f = unicodedata.normalize("NFC", raw).strip()
    if not f:
        return ""
    if len(f) > MAX_FOLDER_CHARS:
        raise InvalidName(f"The folder name is too long (at most {MAX_FOLDER_CHARS} characters).")
    if f.startswith((".", " ")) or f.endswith((".", " ")) or "." in f:
        raise InvalidName("A folder name cannot contain dots.")
    if _has_bad_char(f) or "/" in f or "\\" in f:
        raise InvalidName("The folder name has a character that is not allowed. Letters, digits, spaces and "
                          "_ - + ( ) [ ] & , ' # @ ! ~ = are fine.")
    return f


# ------------------------------------------------------------------ reading a file's details

def _iso(ts=None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts if ts is not None else time.time())) + "Z"


def _fmt_time(seconds):
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return None
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 0 else None


def _num(v):
    try:
        f = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) and abs(f) < 1e9 else f


def _first(v):
    """Slicer settings are lists with one entry per extruder / filament: the first usable entry."""
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def _zcap(z, name, cap=_MEMBER_CAP):
    if z.getinfo(name).file_size > cap:
        raise ValueError(f"{name} is too large")
    return z.read(name)


_BED_KEYS = {"cool plate": "cool_plate_temp", "engineering plate": "eng_plate_temp",
             "high temp plate": "hot_plate_temp", "textured pei plate": "textured_plate_temp",
             "textured cool plate": "cool_plate_temp"}


def _extras_3mf(data: bytes) -> dict:
    """Layer height, layers, nozzle, temperatures, infill, supports and slicer of a Bambu-style .3mf. Best effort:
    every field is optional and a read problem just leaves it out."""
    out = {}
    z = open_zip_bounded(data)
    names = z.namelist()
    settings = {}
    for n in names:
        if n.endswith("project_settings.config"):
            try:
                doc = json.loads(_zcap(z, n).decode("utf8", "ignore"))
                settings = doc if isinstance(doc, dict) else {}
            except Exception:
                settings = {}
            break
    out["layer_height"] = _num(settings.get("layer_height"))
    out["nozzle"] = _num(_first(settings.get("nozzle_diameter")))
    inf = settings.get("sparse_infill_density")
    out["infill"] = str(inf) if inf not in (None, "") else None
    if "enable_support" in settings:
        out["supports"] = str(settings.get("enable_support")).strip().lower() in ("1", "true", "yes")
    out["_noz_temps"] = settings.get("nozzle_temperature") if isinstance(settings.get("nozzle_temperature"), list) else None
    bed_key = _BED_KEYS.get(str(settings.get("curr_bed_type") or "").strip().lower())
    beds = settings.get(bed_key) if bed_key else None
    out["_bed_temps"] = beds if isinstance(beds, list) else None
    si_text = None
    for n in names:                                        # plate-level facts the slicer wrote itself
        if n.endswith("slice_info.config"):
            try:
                t = _zcap(z, n).decode("utf8", "ignore")
            except Exception:
                break
            si_text = t
            m = re.search(r'key="support_used"\s+value="([^"]*)"', t)
            if m and "supports" not in out:
                out["supports"] = m.group(1).strip().lower() == "true"
            if out["nozzle"] is None:
                m = re.search(r'key="nozzle_diameters"\s+value="([\d.]+)', t)
                out["nozzle"] = _num(m.group(1)) if m else None
            break
    for n in names:                                        # the application that wrote the file
        if n.endswith("3dmodel.model"):
            try:
                with z.open(n) as fh:
                    head = fh.read(16384).decode("utf8", "ignore")
                m = re.search(r'name="Application"[^>]*>([^<]{1,60})<', head)
                if m:
                    out["slicer"] = re.sub(r"[^A-Za-z0-9 ._+\-]", "", m.group(1)).strip()[:60] or None
            except Exception:
                pass
            break
    plate = f"Metadata/plate_{plate_number(names, si_text)}.gcode"      # the plate this file holds (plate-4 export: plate_4)
    if plate in names:
        try:
            with z.open(plate) as fh:
                head = fh.read(262144).decode("utf8", "ignore")
            m = re.search(r"total layer number:\s*(\d+)", head)
            out["layers"] = int(m.group(1)) if m else None
        except Exception:
            pass
    return out


def _hms_to_seconds(text):
    total, seen = 0, False
    for num, unit in re.findall(r"(\d+)\s*([dhms])", str(text)):
        total += int(num) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        seen = True
    return total if seen else None


def _gcode_setting(text, key):
    m = re.search(r"^[; \t]*" + re.escape(key) + r"[ \t]*=[ \t]*([^\r\n\x00]+)", text, re.M)
    return m.group(1).strip() if m else None


def _gcode_facts(head: bytes, tail: bytes) -> dict:
    """Facts a PrusaSlicer / OrcaSlicer style gcode file states in its comments (the settings block is usually at the
    END of the file, the thumbnail and the model check in the head). bgcode keeps the same keys as key=value text."""
    text = (head.decode("utf8", "ignore") + "\n" + tail.decode("utf8", "ignore"))
    out = {}
    out["layer_height"] = _num(_gcode_setting(text, "layer_height"))
    out["nozzle"] = _num((_gcode_setting(text, "nozzle_diameter") or "").split(",")[0])
    out["infill"] = _gcode_setting(text, "fill_density")
    sup = _gcode_setting(text, "support_material")
    if sup is not None:
        out["supports"] = sup.strip() in ("1", "true")
    m = re.search(r"^;\s*generated by ([^\r\n]{1,60})", text, re.M)
    if m:
        out["slicer"] = re.sub(r"\s+on\s+\d{4}-.*$", "", re.sub(r"[^A-Za-z0-9 ._+\-]", "", m.group(1))).strip()[:60] or None
    for rx in (r"total layers count\s*=\s*(\d+)", r"total layer number:\s*(\d+)"):
        m = re.search(rx, text)
        if m:
            out["layers"] = int(m.group(1))
            break
    secs = _hms_to_seconds(_gcode_setting(text, "estimated printing time (normal mode)"))
    if secs is not None:
        out["print_seconds"] = secs
    temps = [_num(x) for x in (_gcode_setting(text, "temperature") or "").split(",") if _num(x) is not None]
    beds = [_num(x) for x in (_gcode_setting(text, "bed_temperature") or "").split(",") if _num(x) is not None]
    types = [t.strip() for t in (_gcode_setting(text, "filament_type") or "").split(";") if t.strip()]
    colours = [c.strip() for c in (_gcode_setting(text, "filament_colour") or "").split(";") if c.strip()]
    grams = [_num(x) for x in (_gcode_setting(text, "filament used [g]") or "").split(",")]
    total_g = _num(_gcode_setting(text, "total filament used [g]"))
    fil = []
    for i, t in enumerate(types[:16]):
        col = colours[i] if i < len(colours) else ""
        m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", col)
        fil.append({"id": i + 1, "type": re.sub(r"[^A-Za-z0-9 +\-]", "", t)[:20],
                    "color": ("#" + m.group(1).upper()) if m else None,
                    "used_g": grams[i] if i < len(grams) else None,
                    "nozzle_temp": temps[i] if i < len(temps) else (temps[0] if temps else None)})
    out["filaments"] = fil
    out["weight_g"] = total_g if total_g is not None else (sum(g for g in grams if g) or None)
    out["bed_temp"] = beds[0] if beds else None
    return out


def extract_meta(path: str, kind: str):
    """(meta, png_bytes_or_None) for a file on disk. NEVER raises: a file that cannot be read gets
    {"error": "..."} and the library still keeps it. `meta` has the keys of bambu.parse_3mf_meta (so
    modelcheck.file_model_bambu(meta) works on it) plus the extra fields."""
    try:
        size = os.path.getsize(path)
        if kind == "3mf":
            if size > MAX_PARSE_BYTES:
                return {"error": "too large to read the details"}, None
            with open(path, "rb") as fh:
                data = fh.read()
            meta = parse_3mf_meta(data)
            if meta.get("error"):
                return {"error": meta["error"]}, _safe(extract_plate_png, data)
            png = _safe(extract_plate_png, data)
            try:
                import joblog
                info = joblog.slice_info_from_3mf(data)
                meta["parts"] = joblog.parts_for_plate(info, meta.get("plate_n"))
                meta["plate"] = platecontents.from_slice_info(info) if info.get("plates") else platecontents.unknown()   # ==== plate contents ====
            except Exception:
                meta["parts"] = None
            extras = {}
            try:
                extras = _extras_3mf(data)
            except Exception:
                pass
            noz_t, bed_t = extras.pop("_noz_temps", None), extras.pop("_bed_temps", None)
            for f in meta.get("filaments") or []:
                idx = f.get("id", 1) - 1
                f["nozzle_temp"] = _num(noz_t[idx]) if noz_t and 0 <= idx < len(noz_t) else None
            first = (meta.get("filaments") or [{}])[0].get("id", 1) - 1
            extras["bed_temp"] = _num(bed_t[first]) if bed_t and 0 <= first < len(bed_t) else None
            meta.update({k: v for k, v in extras.items() if v is not None})
            return meta, png
        # gcode / bgcode: only the head and the tail are read, never the whole file
        with open(path, "rb") as fh:
            head = fh.read(_HEAD)
            tail = b""
            if size > _HEAD:
                fh.seek(max(_HEAD, size - _TAIL))
                tail = fh.read(_TAIL)
        meta = {"slicer_model": modelcheck.gcode_head_model(head + tail) or None}
        facts = _gcode_facts(head, tail)
        if facts.get("print_seconds") is not None:
            meta["print_seconds"] = facts["print_seconds"]
            meta["print_time"] = _fmt_time(facts["print_seconds"])
        meta.update({k: v for k, v in facts.items() if v is not None and k != "print_seconds"})
        meta["plate"] = platecontents.from_path(path, kind)                        # ==== plate contents ====
        return meta, _safe(extract_gcode_png, head)
    except Exception as e:                                   # a damaged file is still a file
        return {"error": f"could not read the details ({type(e).__name__})"}, None


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


# ------------------------------------------------------------------ describing (library + printer cache alike)

def sliced_for_name(meta) -> str:
    """The printer model a file says it was sliced for ('' = it does not say)."""
    if not isinstance(meta, dict):
        return ""
    return modelcheck.file_model_bambu(meta) or modelcheck._clean_display(meta.get("slicer_model") or "")


def describe_meta(meta, color_name=lambda h: "UNKNOWN") -> dict:
    """The detail fields of one file, in the shape the dashboard and the agents get. `meta` is a library meta dict or
    a metadata-cache entry of a printer's file (fewer fields: the rest are None)."""
    meta = meta if isinstance(meta, dict) else {}
    fil = []
    for f in meta.get("filaments") or []:
        if not isinstance(f, dict):
            continue
        hexc = f.get("color")
        fil.append({"type": f.get("type") or "?", "color_name": color_name(hexc) if hexc else "UNKNOWN",
                    "hex": hexc, "used_g": f.get("used_g"), "nozzle_temp": f.get("nozzle_temp")})
    sliced = sliced_for_name(meta)
    temps = [f["nozzle_temp"] for f in fil if f.get("nozzle_temp") is not None]
    return {
        "print_time": meta.get("print_time"), "print_seconds": meta.get("print_seconds"),
        "weight_g": meta.get("weight_g"), "layer_height": meta.get("layer_height"), "layers": meta.get("layers"),
        "nozzle": meta.get("nozzle"), "nozzle_temp": max(temps) if temps else None, "bed_temp": meta.get("bed_temp"),
        "infill": meta.get("infill"), "supports": meta.get("supports"), "slicer": meta.get("slicer"),
        "sliced_for": modelcheck.check(sliced, "")["sliced_for"] if sliced else None,
        "filaments": fil, "parts": meta.get("parts"),
        "plate": platecontents.clean_result(meta["plate"]) if isinstance(meta.get("plate"), dict) else None,   # ==== plate contents ====
        "details_error": meta.get("error"),
    }


def _basename(path) -> str:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1]


def merge_rows(entries, printer_files, color_name=lambda h: "UNKNOWN", thumb_url=lambda b: "/api/thumb/" + b,
               lib_thumb_url=lambda b: "/api/library/thumb/" + b) -> list:
    """One row per file across the library and the printers.

    entries        the library index entries (Library.entries())
    printer_files  {printer: [{"name": path-on-printer, "size": int, "meta": cache entry or None, "thumb": basename or None}]}
                   (cached listings only - building this never talks to a printer)
    Rows match on (file name, size). A printer file that is not in the library is a "printer-only" row; its details
    come from the metadata cache (fewer fields than a library file).
    """
    rows, by_key = [], {}
    for e in entries:
        d = describe_meta(e.get("meta"), color_name)
        row = {"id": "l:" + e["name"], "name": e["name"], "size": e["size"], "folder": e.get("folder", ""),
               "in_library": True, "printers": [], "on": [], "added": e.get("added"), "source": e.get("source", ""),
               "kind": e.get("kind", ""), "sha256": e.get("sha256"),
               "thumb_url": lib_thumb_url(e["thumb"]) if e.get("thumb") else None, **d}
        rows.append(row)
        by_key[(e["name"].lower(), e["size"])] = row
    for printer, files in sorted(printer_files.items(), key=lambda kv: kv[0].lower()):
        for f in files or []:
            path = str(f.get("name") or "")
            base = _basename(path)
            if not base or not kind_of(base):
                continue
            size = int(f.get("size") or 0)
            row = by_key.get((base.lower(), size))
            if row is None:
                d = describe_meta(f.get("meta"), color_name)
                row = {"id": f"p:{base.lower()}:{size}", "name": base, "size": size, "folder": "",
                       "in_library": False, "printers": [], "on": [], "added": None, "source": "printer",
                       "kind": kind_of(base), "sha256": None,
                       "thumb_url": thumb_url(f["thumb"]) if f.get("thumb") else None, **d}
                rows.append(row)
                by_key[(base.lower(), size)] = row
            if printer not in row["printers"]:
                row["printers"].append(printer)
                row["on"].append({"printer": printer, "path": path})
    rows.sort(key=lambda r: (r["name"].casefold(), r["size"]))
    return rows


def filter_rows(rows, query="", folder="", hide_printer_only=False) -> list:
    q = " ".join(str(query or "").lower().split())
    out = []
    for r in rows:
        if hide_printer_only and not r["in_library"]:
            continue
        if folder and r.get("folder") != folder:
            continue
        if q:
            hay = " ".join([r["name"], r.get("folder") or "", " ".join(r["printers"]), r.get("sliced_for") or "",
                            r.get("slicer") or "", " ".join(f"{f['color_name']} {f['type']}" for f in r["filaments"])]).lower()
            if not all(tok in hay for tok in q.split()):
                continue
        out.append(r)
    return out


# ------------------------------------------------------------------ the store

class Incoming:
    """A file being received. write() enforces the size cap; the temp file is private and hidden."""

    def __init__(self, lib, cap):
        self._lib = lib
        self.cap = cap
        self.size = 0
        self.sha = hashlib.sha256()
        os.makedirs(lib.incoming, mode=0o700, exist_ok=True)
        self.path = os.path.join(lib.incoming, ".in-" + os.urandom(8).hex())
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._done = False

    def write(self, chunk: bytes):
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > self.cap:
            self.discard()
            raise TooLarge(f"The file is larger than the {self.cap // 1048576} MB limit for one library file.")
        self.sha.update(chunk)
        view = memoryview(chunk)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]

    def finish(self):
        """Flush to disk and close; the temp path stays until the library moves it into place."""
        if self._fd is not None:
            try:
                os.fsync(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None

    def discard(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            os.remove(self.path)
        except OSError:
            pass


class Library:
    def __init__(self, root, max_bytes=None, clock=time.time):
        self.root = os.path.abspath(root)
        self.files_dir = os.path.join(self.root, "files")
        self.thumbs_dir = os.path.join(self.root, "thumbs")
        self.incoming = os.path.join(self.root, ".incoming")
        self.index_path = os.path.join(self.root, "index.json")
        self._max = max_bytes
        self._clock = clock
        self._lock = threading.RLock()
        self._doc = None

    # ---- limits
    @property
    def max_bytes(self) -> int:
        return self._max if self._max else max_file_bytes()

    # ---- disk layout
    def _ensure_dirs(self):
        for d in (self.root, self.files_dir, self.thumbs_dir, self.incoming):
            os.makedirs(d, mode=0o700, exist_ok=True)

    def _path_for(self, name: str) -> str:
        """<files>/<name>, proven to be a direct child of the files folder (the name is already sanitised; this is
        the second lock on the same door)."""
        p = os.path.join(self.files_dir, name)
        if os.path.dirname(os.path.abspath(p)) != os.path.abspath(self.files_dir) or os.path.basename(p) != name:
            raise InvalidName("That file name is not allowed.")
        return p

    def sweep_incoming(self):
        """Delete leftovers of uploads that never finished (a crash, a dropped connection)."""
        try:
            for n in os.listdir(self.incoming):
                try:
                    os.remove(os.path.join(self.incoming, n))
                except OSError:
                    pass
        except OSError:
            pass

    # ---- the index
    def _load(self) -> dict:
        if self._doc is not None:
            return self._doc
        doc = {"version": 1, "folders": [], "files": {}}
        try:
            with open(self.index_path, encoding="utf8") as fh:
                got = json.load(fh)
            if isinstance(got, dict) and isinstance(got.get("files"), dict):
                doc["files"] = {k: v for k, v in got["files"].items() if isinstance(v, dict)}
                doc["folders"] = [f for f in (got.get("folders") or []) if isinstance(f, str)]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            try:                                              # unreadable: keep it for a human, start clean
                os.replace(self.index_path, self.index_path + ".bad")
            except OSError:
                pass
        self._doc = doc
        return doc

    def _save(self):
        self._ensure_dirs()
        tmp = self.index_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as fh:
            json.dump(self._doc, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.index_path)
        try:
            os.chmod(self.index_path, 0o600)
        except OSError:
            pass

    def _find(self, name: str):
        """(stored name, entry) matching `name` exactly or ignoring case, else (None, None)."""
        files = self._load()["files"]
        if name in files:
            return name, files[name]
        low = name.lower()
        for k, v in files.items():
            if k.lower() == low:
                return k, v
        return None, None

    # ---- reading
    def entries(self) -> list:
        """Index entries whose file is still on disk (a file removed by hand just drops out of the list)."""
        with self._lock:
            out = []
            for name, e in self._load()["files"].items():
                if os.path.isfile(os.path.join(self.files_dir, name)):
                    out.append({**e, "name": name})
            return sorted(out, key=lambda e: e["name"].casefold())

    def update_meta(self, name, save=True, **fields) -> bool:
        """Add or replace keys of one file's stored details (a re-read of the same file; nothing else changes).
        ==== plate contents ==== False when the file is gone."""
        with self._lock:
            k, e = self._find(name)
            if e is None:
                return False
            e["meta"] = {**(e.get("meta") if isinstance(e.get("meta"), dict) else {}), **fields}
            if save:
                self._save()
            return True

    def save_index(self):
        with self._lock:
            self._save()

    def refresh_picture(self, name, png, save=True, **fields) -> bool:
        """Replace one file's plate preview (and add/replace keys of its stored details) after a re-read of the same
        file on the Hub's own disk. `png` None keeps the current picture. False when the file is gone."""
        with self._lock:
            k, e = self._find(name)
            if e is None:
                return False
            if png:
                thumb = e.get("thumb") or (hashlib.sha1((k + str(e.get("sha256") or "")).encode()).hexdigest() + ".png")
                self._write_private(os.path.join(self.thumbs_dir, thumb), png)
                e["thumb"] = thumb
            e["meta"] = {**(e.get("meta") if isinstance(e.get("meta"), dict) else {}), **fields}
            if save:
                self._save()
            return True

    def get(self, name) -> dict:
        clean = sanitize_name(name)
        with self._lock:
            k, e = self._find(clean)
            if e is None or not os.path.isfile(os.path.join(self.files_dir, k)):
                raise NotFound(f"'{clean}' is not in the library.")
            return {**e, "name": k}

    def file_path(self, name) -> str:
        e = self.get(name)
        p = self._path_for(e["name"])
        if os.path.islink(p):
            raise NotFound("That file is not available.")
        return p

    def thumb_path(self, basename):
        if not isinstance(basename, str) or not _THUMB_RE.match(basename):
            return None
        p = os.path.join(self.thumbs_dir, basename)
        return p if os.path.isfile(p) and not os.path.islink(p) else None

    def folders(self) -> list:
        with self._lock:
            doc = self._load()
            used = {e.get("folder") for e in doc["files"].values() if e.get("folder")}
            return sorted(set(doc["folders"]) | used, key=str.casefold)

    # ---- writing
    def ensure_dirs(self):
        self._ensure_dirs()

    def begin_incoming(self) -> Incoming:
        self._ensure_dirs()
        return Incoming(self, self.max_bytes)

    def check_can_add(self, name, replace=False, size=None):
        """Refuse early (before any bytes are read) what ingest would refuse later."""
        clean = sanitize_name(name)
        if size is not None and size > self.max_bytes:
            raise TooLarge(f"The file is larger than the {self.max_bytes // 1048576} MB limit for one library file.")
        with self._lock:
            k, e = self._find(clean)
            if e is not None and not replace:
                raise Exists(f"'{k}' is already in the library. Delete it first or upload it with replace.")
            if e is None and len(self._load()["files"]) >= MAX_FILES:
                raise LibraryError(f"The library is full ({MAX_FILES} files).")
        return clean

    def ingest(self, inc: Incoming, name, folder="", source="upload", replace=False) -> dict:
        """Move a finished Incoming into the library: parse its details, write the thumbnail, update the index.
        The Incoming's temp file is consumed on success and removed on failure."""
        try:
            clean = self.check_can_add(name, replace)
            folder = sanitize_folder(folder)
            inc.finish()
            if inc.size <= 0:
                raise InvalidName("The file is empty.")
            free = shutil.disk_usage(self.root).free
            if free < inc.size + _FREE_MARGIN:
                raise NoSpace("The Hub does not have enough free disk space for this file.")
            kind = kind_of(clean)
            meta, png = extract_meta(inc.path, kind)
            thumb = None
            if png:
                thumb = hashlib.sha1((clean + inc.sha.hexdigest()).encode()).hexdigest() + ".png"
            with self._lock:
                k, old = self._find(clean)
                if old is not None and not replace:
                    raise Exists(f"'{k}' is already in the library.")
                doc = self._load()
                if not folder and old is not None:
                    folder = old.get("folder") or ""               # replacing a file keeps its folder
                if folder:
                    known = {x.casefold(): x for x in self.folders()}
                    if folder.casefold() in known:
                        folder = known[folder.casefold()]          # the spelling the folder already has
                    elif len(known) >= MAX_FOLDERS:
                        raise LibraryError(f"Too many folders (at most {MAX_FOLDERS}).")
                    else:
                        doc["folders"].append(folder)
                final = self._path_for(clean if old is None else k)
                os.chmod(inc.path, 0o600)
                os.replace(inc.path, final)                      # atomic: the list never sees a half file
                if thumb:
                    self._write_private(os.path.join(self.thumbs_dir, thumb), png)
                if old is not None and old.get("thumb") and old.get("thumb") != thumb:
                    self._rm(os.path.join(self.thumbs_dir, old["thumb"]))
                entry = {"size": inc.size, "sha256": inc.sha.hexdigest(), "folder": folder, "added": _iso(self._clock()),
                         "source": source, "kind": kind, "meta": meta, "thumb": thumb}
                doc["files"][clean if old is None else k] = entry
                self._save()
                return {**entry, "name": clean if old is None else k}
        finally:
            inc.discard()

    def add_bytes(self, name, data: bytes, folder="", source="upload", replace=False) -> dict:
        inc = self.begin_incoming()
        inc.write(data)
        return self.ingest(inc, name, folder, source, replace)

    def add_streamed_file(self, tmp_path, name, sha256, size, folder="", source="printer", replace=False) -> dict:
        """Adopt an already-downloaded temp file (made by backup._stream_to_temp in this library's .incoming folder)."""
        self._ensure_dirs()
        if os.path.dirname(os.path.abspath(tmp_path)) != os.path.abspath(self.incoming):
            raise LibraryError("internal: the temp file is not in the library's incoming folder.")
        if size > self.max_bytes:
            self._rm(tmp_path)
            raise TooLarge(f"The file is larger than the {self.max_bytes // 1048576} MB limit for one library file.")
        inc = Incoming.__new__(Incoming)
        inc._lib, inc.cap, inc.size, inc.path, inc._fd, inc._done = self, self.max_bytes, size, tmp_path, None, False

        class _H:                                            # only .hexdigest() is used afterwards
            def hexdigest(self_inner):
                return sha256
        inc.sha = _H()
        return self.ingest(inc, name, folder, source, replace)

    def delete(self, name) -> str:
        clean = sanitize_name(name)
        with self._lock:
            k, e = self._find(clean)
            if e is None:
                raise NotFound(f"'{clean}' is not in the library.")
            self._rm(self._path_for(k))
            if e.get("thumb"):
                self._rm(os.path.join(self.thumbs_dir, e["thumb"]))
            del self._load()["files"][k]
            self._save()
            return k

    # ==== rename ====
    def find_by_sha(self, sha256):
        """The first library entry (with its name) whose content hash is `sha256`, else None."""
        if not sha256:
            return None
        with self._lock:
            for name, e in self._load()["files"].items():
                if e.get("sha256") == sha256 and os.path.isfile(os.path.join(self.files_dir, name)):
                    return {**e, "name": name}
        return None

    def rename(self, old, new) -> dict:
        """Give a library file a new name, atomically. The new name goes through sanitize_name and must keep the same
        extension; a different file already called that is never replaced (only a change of capitals in the file's own
        name is allowed to match itself). The file, its plate preview and its index entry move together; the folder
        stays. If the index cannot be saved the file is put back. -> the entry under its new name."""
        old_clean = sanitize_name(old)
        new_clean = sanitize_name(new)
        with self._lock:
            k, e = self._find(old_clean)
            if e is None or not os.path.isfile(os.path.join(self.files_dir, k)):
                raise NotFound(f"'{old_clean}' is not in the library.")
            if ext_of(new_clean) != ext_of(k):
                raise InvalidName(f"The new name must keep the extension {ext_of(k)}.")
            if new_clean == k:
                raise InvalidName("The new name is the same as the old one.")
            other_k, other = self._find(new_clean)
            if other is not None and other_k != k:
                raise Exists(f"'{other_k}' is already in the library.")
            src, dst = self._path_for(k), self._path_for(new_clean)
            if os.path.lexists(dst) and other_k != k:
                raise Exists(f"A file called '{new_clean}' is already there.")
            doc = self._load()
            os.replace(src, dst)                                    # one atomic rename inside the files folder
            old_thumb, new_thumb = e.get("thumb"), None
            if old_thumb:
                cand = hashlib.sha1((new_clean + str(e.get("sha256") or "")).encode()).hexdigest() + ".png"
                try:
                    os.replace(os.path.join(self.thumbs_dir, old_thumb), os.path.join(self.thumbs_dir, cand))
                    new_thumb = cand
                except OSError:
                    new_thumb = old_thumb                           # keep pointing at the preview that still exists
            entry = dict(e)
            if old_thumb:
                entry["thumb"] = new_thumb
            del doc["files"][k]
            doc["files"][new_clean] = entry
            try:
                self._save()
            except BaseException:                                   # the index must always match the disk
                del doc["files"][new_clean]
                doc["files"][k] = e
                try:
                    os.replace(dst, src)
                    if old_thumb and new_thumb and new_thumb != old_thumb:
                        os.replace(os.path.join(self.thumbs_dir, new_thumb), os.path.join(self.thumbs_dir, old_thumb))
                except OSError:
                    pass
                raise
            return {**entry, "name": new_clean}
    # ==== end rename ====

    def create_folder(self, folder) -> str:
        f = sanitize_folder(folder)
        if not f:
            raise InvalidName("Give the folder a name.")
        with self._lock:
            if f.casefold() in {x.casefold() for x in self.folders()}:
                raise Exists(f"The folder '{f}' already exists.")
            if len(self.folders()) >= MAX_FOLDERS:
                raise LibraryError(f"Too many folders (at most {MAX_FOLDERS}).")
            self._load()["folders"].append(f)
            self._save()
            return f

    def delete_folder(self, folder) -> str:
        f = sanitize_folder(folder)
        with self._lock:
            doc = self._load()
            if any(e.get("folder") == f for e in doc["files"].values()):
                raise LibraryError(f"The folder '{f}' still has files in it. Move them out first.")
            if f not in doc["folders"]:
                raise NotFound(f"There is no folder '{f}'.")
            doc["folders"].remove(f)
            self._save()
            return f

    def move(self, name, folder) -> dict:
        clean = sanitize_name(name)
        f = sanitize_folder(folder)
        with self._lock:
            k, e = self._find(clean)
            if e is None:
                raise NotFound(f"'{clean}' is not in the library.")
            if f and f.casefold() not in {x.casefold() for x in self.folders()}:
                raise NotFound(f"There is no folder '{f}'. Create it first.")
            if f:                                           # use the stored spelling
                f = next(x for x in self.folders() if x.casefold() == f.casefold())
            e["folder"] = f
            self._save()
            return {**e, "name": k}

    # ---- small helpers
    @staticmethod
    def _rm(path):
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _write_private(path, data: bytes):
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
