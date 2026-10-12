"""Job log: one row per print attempt, so every print is accounted for and priced.

Fed by two things the Hub already has, and nothing else:

  * `JobLog.on_event(event)` - the EventWatcher sink (print_started / print_paused /
    print_finished / print_failed events from events.py);
  * `JobLog.observe(fleet)` - the same {printer: snapshot} dict the watcher gets, used to track
    progress and to close jobs whose end event never arrived (Hub restart, printer unplugged).

JobLog NEVER talks to a printer. File facts (grams, estimated time, part count, material)
come from a `resolver(printer, job_name) -> dict | None` callback the Hub supplies, which should
answer from caches only. Helpers here turn the things the Hub holds into that dict:
`resolved_from_3mf` (bytes of a .gcode.3mf), `resolved_from_cached_meta` (the dict
`bambu.parse_3mf_meta` produced), `resolved_from_file_entry` (a driver `files()` entry).

Storage: SQLite (stdlib), WAL mode, `schema_migrations` table; default path /data/jobs.sqlite.
Queries: recent_jobs, job, stats, file_costs, material_usage, export_csv. Money math lives in
costs.py and is applied at query time. See docs/joblog.md.

Stdlib only.
"""

import csv
import datetime
import io
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

import costs as _costs

ACTIVE_STATES = ("PREPARE", "RUNNING", "PAUSE", "PAUSED")
RESULTS = ("running", "finished", "failed", "cancelled", "unknown")
UNNAMED = ("", "the current job")        # events.py's placeholder when a printer reports no name

# ============================================================================ part counting

def _attrs(tag: str) -> dict:
    return dict(re.findall(r'([\w:.-]+)\s*=\s*"([^"]*)"', tag))


def _build_plate(position: int, metas: list, objects: list, filaments: list) -> dict:
    """Shared by the XML and the regex path: each argument is a list of attribute dicts."""
    md = {m.get("key"): m.get("value") for m in metas if m.get("key")}
    live = [o for o in objects if str(o.get("skipped", "false")).lower() != "true"]
    fils = []
    for f in filaments:
        col = re.match(r"#?([0-9A-Fa-f]{6})", f.get("color", "") or "")
        fils.append({"id": _int(f.get("id")), "type": f.get("type") or None,
                     "color": ("#" + col.group(1).upper()) if col else None,
                     "used_g": _costs._f(f.get("used_g")), "used_m": _costs._f(f.get("used_m"))})
    weight = _costs._f(md.get("weight"))
    if weight is None:
        gs = [f["used_g"] for f in fils if f["used_g"] is not None]
        weight = round(sum(gs), 2) if gs else None
    return {"index": _int(md.get("index")) or position,
            "parts": len(live) or None,                      # no <object> entries -> unknown, not 0
            "objects": [o.get("name") for o in live if o.get("name")],
            "weight_g": weight, "prediction_s": _int(md.get("prediction")),
            "printer_model_id": md.get("printer_model_id"), "filaments": fils}


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_slice_info(text) -> dict:
    """Per-plate facts from a Bambu `Metadata/slice_info.config`:

        {"plates": [{"index", "parts", "objects", "weight_g", "prediction_s",
                     "printer_model_id", "filaments": [...]}], "error": None | "..."}

    `parts` is the number of `<object>` entries in the plate (objects marked skipped="true"
    are not printed and not counted). Never raises: malformed XML falls back to a tolerant
    regex scan, and anything unreadable yields {"plates": [], "error": ...}."""
    info = {"plates": [], "error": None}
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf8", "ignore")
    if not isinstance(text, str) or not text.strip():
        info["error"] = "empty slice_info"
        return info
    plates = None
    if "<!DOCTYPE" not in text and "<!ENTITY" not in text:   # never feed entity tricks to the XML parser
        try:
            root = ET.fromstring(text)
            plates = []
            for i, pl in enumerate(root.iter("plate"), 1):
                plates.append(_build_plate(
                    i, [dict(e.attrib) for e in pl.iter("metadata")],
                    [dict(e.attrib) for e in pl.iter("object")],
                    [dict(e.attrib) for e in pl.iter("filament")]))
        except ET.ParseError:
            plates = None
    if plates is None:                                      # malformed (truncated download, stray bytes)
        plates = []
        for i, chunk in enumerate([c for c in re.split(r"(?=<plate\b)", text) if c.startswith("<plate")], 1):
            chunk = chunk.split("</plate>")[0]
            plates.append(_build_plate(
                i, [_attrs(t) for t in re.findall(r"<metadata\b[^<>]*", chunk)],
                [_attrs(t) for t in re.findall(r"<object\b[^<>]*", chunk)],
                [_attrs(t) for t in re.findall(r"<filament\b[^<>]*", chunk)]))
        info["error"] = "slice_info malformed; read leniently" if plates else "slice_info unreadable"
    info["plates"] = plates
    if not plates and not info["error"]:
        info["error"] = "no plates in slice_info"
    return info


def slice_info_from_3mf(data: bytes) -> dict:
    """parse_slice_info() for the bytes of a .gcode.3mf (zip). Never raises."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        names = [n for n in z.namelist() if n.endswith("slice_info.config")]
        if not names:
            return {"plates": [], "error": "no slice_info.config"}
        return parse_slice_info(z.read(names[0]))
    except Exception as e:
        return {"plates": [], "error": f"unreadable 3mf: {type(e).__name__}"}


def _pick_plate(info: dict, plate=None):
    plates = info.get("plates") or []
    if not plates:
        return None
    if plate is not None:
        for p in plates:
            if p.get("index") == plate:
                return p
    return plates[0]


def parts_for_plate(info: dict, plate=None):
    """Part count for a plate (default: the first), or None if unknown."""
    p = _pick_plate(info, plate)
    return p["parts"] if p else None


# ============================================================================ resolver helpers

def _dominant(fils):
    best = None
    for f in fils or []:
        if best is None or (f.get("used_g") or 0) > (best.get("used_g") or 0):
            best = f
    return best


def normalize_resolved(d) -> dict:
    """Coerce a resolver answer to the canonical keys:
    file, plate, grams, seconds, parts, parts_source, material, colour, filaments, model.
    Anything missing is None. Returns {} for a non-dict."""
    if not isinstance(d, dict):
        return {}
    fils = [{"type": f.get("type"), "color": f.get("color"), "used_g": _costs._f(f.get("used_g"))}
            for f in (d.get("filaments") or []) if isinstance(f, dict)]
    dom = _dominant(fils)
    parts = _int(d.get("parts"))
    return {
        "file": d.get("file") or None, "plate": _int(d.get("plate")),
        "grams": _costs._f(d.get("grams")), "seconds": _costs._f(d.get("seconds")),
        "parts": parts if parts and parts > 0 else None,
        "parts_source": d.get("parts_source") or ("slice_info" if parts else None),
        "material": d.get("material") or (dom or {}).get("type"),
        "colour": d.get("colour") or d.get("color") or (dom or {}).get("color"),
        "filaments": fils, "model": d.get("model") or d.get("printer_model_id"),
        "kind": d.get("kind"),
    }


def resolved_from_3mf(data: bytes, plate=None, file=None, model=None) -> dict:
    """Resolver answer from the bytes of a printer-side .gcode.3mf: grams, time, part count,
    material and colour of the chosen plate (first plate by default)."""
    info = slice_info_from_3mf(data)
    p = _pick_plate(info, plate)
    if not p:
        return {}
    return normalize_resolved({
        "file": file, "plate": p["index"], "grams": p["weight_g"], "seconds": p["prediction_s"],
        "parts": p["parts"], "filaments": p["filaments"], "model": model or p["printer_model_id"],
        "kind": "bambu"})


def resolved_from_cached_meta(meta, parts=None, plate=None, file=None, model=None) -> dict:
    """Resolver answer from the dict `bambu.parse_3mf_meta` returns (what the Hub's metadata cache
    holds): weight_g, print_seconds, filaments, printer_model_id. That dict carries no part
    count, so pass `parts` (e.g. stored by the Hub next to the cache entry via
    parse_slice_info); without it the part count stays unknown and costs assume 1."""
    if not isinstance(meta, dict) or meta.get("error"):
        return {}
    return normalize_resolved({
        "file": file, "plate": plate, "grams": meta.get("weight_g"), "seconds": meta.get("print_seconds"),
        "parts": parts, "parts_source": "slice_info" if parts else None,
        "filaments": meta.get("filaments"), "model": model or meta.get("printer_model_id"), "kind": "bambu"})


def resolved_from_file_entry(entry, material=None, kind=None) -> dict:
    """Resolver answer from an OctoPrint / PrusaLink / Moonraker `files()` entry
    (print_seconds + filament_mm). Grams are derived from the length (1.75 mm, density by
    material; pass `material` if known, else PLA is assumed). One part per print is assumed."""
    if not isinstance(entry, dict):
        return {}
    mat = material or "PLA"
    grams = _costs.grams_from_mm(entry.get("filament_mm"), mat)
    return normalize_resolved({
        "file": entry.get("name"), "grams": grams, "seconds": entry.get("print_seconds"),
        "parts": 1, "parts_source": "assumed", "material": mat if grams else material,
        "filaments": [{"type": mat, "used_g": grams}] if grams else [], "kind": kind})


# ============================================================================ the log

MIGRATIONS = [
    (1, """
    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        printer     TEXT NOT NULL,
        kind        TEXT,
        model       TEXT,
        file        TEXT,
        file_key    TEXT,
        plate       INTEGER,
        started_at  REAL,
        ended_at    REAL,
        duration_s  REAL,
        result      TEXT NOT NULL DEFAULT 'running',
        percent     REAL,
        est_seconds REAL,
        grams_est   REAL,
        grams_used  REAL,
        material    TEXT,
        colour      TEXT,
        filaments   TEXT,
        parts       INTEGER,
        parts_source TEXT,
        pauses      INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        ext_job_id  TEXT,
        source      TEXT NOT NULL DEFAULT 'event',
        last_seen_at REAL
    );
    CREATE INDEX jobs_printer_started ON jobs (printer, started_at);
    CREATE INDEX jobs_file_key ON jobs (file_key);
    CREATE INDEX jobs_open ON jobs (ended_at);
    """),
]

_COLUMNS = ("id printer kind model file file_key plate started_at ended_at duration_s result percent "
            "est_seconds grams_est grams_used material colour filaments parts parts_source pauses error "
            "ext_job_id source last_seen_at").split()


def _period_seconds(period):
    """'day'/'week'/'month'/'year'/'all'/'<N>d' -> seconds (None = all time)."""
    p = str(period or "month").strip().lower()
    named = {"day": 86400, "today": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
    if p in named:
        return named[p]
    if p in ("all", "forever", ""):
        return None
    m = re.fullmatch(r"(\d{1,4})\s*d(ays?)?", p)
    if m:
        return int(m.group(1)) * 86400
    raise ValueError(f"unknown period {period!r}: use day, week, month, year, all or <N>d")


class JobLog:
    """One row per print attempt. Never lets a storage problem escape into the Hub:

      * a corrupt / unreadable / locked / full database at start-up is moved aside
        (`jobs.sqlite.corrupt-<time>`, with its -wal/-shm) and a fresh one is created;
      * if even a fresh file cannot be made, the log runs from memory (`degraded` says why)
        so the Hub keeps going and prints are still tracked until the next restart;
      * corruption discovered later (a query fails with "malformed") is recovered the same way.
    Every recovery is logged loudly and listed in `storage_status()`."""

    CLOCK_STEP_S = 30.0                      # a wall-clock jump bigger than this (vs monotonic) is a step
    KEEP_QUARANTINED = 5

    def __init__(self, path, resolver=None, rates=None, clock=time.time, tz="UTC",
                 orphan_grace_s=120, stale_after_s=48 * 3600, dup_window_s=300, touch_every_s=60,
                 mono=None):
        self.path = path
        self.resolver = resolver              # callable(printer, job_name) -> dict | None
        self._rates = rates                   # Rates | callable() -> Rates | None
        self.clock = clock
        # Elapsed time must survive the wall clock being stepped (a Pi without an RTC boots with a stale
        # time and NTP jumps it): with the real clock use time.monotonic; an injected test clock is its own.
        self._mono_override = mono
        self._last_clock = None               # (wall, mono) at the previous look
        self._clock_offset = 0.0              # total wall-clock step seen since start
        self._start_offset = {}               # job id -> _clock_offset when its row was made
        self.orphan_grace_s = orphan_grace_s
        self.stale_after_s = stale_after_s
        self.dup_window_s = dup_window_s
        self.touch_every_s = touch_every_s
        self.tz = self._tz(tz)
        self._lock = threading.RLock()
        self._idle_since = {}                 # printer -> first time seen not-active while a job was open
        self._touched = {}                    # job id -> last time we wrote progress
        self.degraded = None                  # reason string while running from memory
        self.recoveries = []                  # [{"at","reason","moved_to"}] this process, newest last (bounded)
        self._mem_uri = None
        self._keeper = None                   # holds the shared in-memory database open
        self._last_recovery = 0.0
        self._open_database()

    # ---- plumbing ---------------------------------------------------------

    @staticmethod
    def _tz(name):
        if isinstance(name, datetime.tzinfo):
            return name
        try:
            import zoneinfo
            return zoneinfo.ZoneInfo(str(name or "UTC"))
        except Exception:
            if name and str(name).upper() != "UTC":
                print(f"[joblog] time zone {name!r} is not available on this machine (no tz database?) - "
                      "using UTC for job dates", flush=True)
            return datetime.timezone.utc

    def rates(self):
        r = self._rates() if callable(self._rates) else self._rates
        return r if isinstance(r, _costs.Rates) else _costs.Rates()

    def _conn(self):
        if self._mem_uri:                     # degraded: shared in-memory database
            c = sqlite3.connect(self._mem_uri, uri=True, timeout=10)
            c.row_factory = sqlite3.Row
            return c
        c = sqlite3.connect(self.path, timeout=10)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            c.close()                         # never leak a handle on a damaged file
            raise
        return c

    # ---- storage safety: open defensively, recover, never raise into the Hub ----------------

    @staticmethod
    def _is_corruption(e) -> bool:
        code = getattr(e, "sqlite_errorcode", None)
        if code is not None and (code & 0xFF) in (11, 26):          # SQLITE_CORRUPT, SQLITE_NOTADB
            return True
        msg = str(e).lower()
        return any(w in msg for w in ("malformed", "not a database", "corrupt", "disk image"))

    @classmethod
    def _is_storage_fault(cls, e) -> bool:
        """Corruption, a lock that will not clear, a full or unwritable disk: the FILE is the problem, so
        setting it aside helps. Anything else (a broken migration, a programming error) is not the file's
        fault: it is never moved, the log just runs from memory."""
        if isinstance(e, OSError):
            return True
        if cls._is_corruption(e):
            return True
        msg = str(e).lower()
        return any(w in msg for w in ("locked", "busy", "full", "unable to open", "readonly", "read-only",
                                      "i/o error", "disk"))

    def _check_integrity(self):
        c = self._conn()
        try:
            row = c.execute("PRAGMA quick_check(1)").fetchone()
            if row and str(row[0]).lower() != "ok":
                raise sqlite3.DatabaseError(f"database disk image is malformed (quick_check: {row[0]})")
        finally:
            c.close()

    def _open_database(self):
        """Constructor body: make sure there is a usable database, whatever state the file is in."""
        d = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(d, exist_ok=True)
            if os.path.exists(self.path):
                self._check_integrity()
            self.migrate()
            return
        except (sqlite3.Error, OSError) as e:
            err, first = e, f"{type(e).__name__}: {e}"
        print(f"[joblog] !!! the job log {self.path} cannot be opened ({first})", flush=True)
        self._recover(first, startup=True, move=self._is_storage_fault(err))

    def _quarantine(self, reason) -> str:
        """Move the database (and -wal/-shm) aside as jobs.sqlite.corrupt-<time>. Returns the new
        name, or None if there was nothing to move / the move failed."""
        if not os.path.exists(self.path):
            for ext in ("-wal", "-shm"):
                try:
                    os.remove(self.path + ext)
                except OSError:
                    pass
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        dest, n = f"{self.path}.corrupt-{stamp}", 1
        while os.path.exists(dest):
            n += 1
            dest = f"{self.path}.corrupt-{stamp}-{n}"
        try:
            os.replace(self.path, dest)
        except OSError as e:
            print(f"[joblog] !!! could not move {self.path} aside ({e})", flush=True)
            return None
        for ext in ("-wal", "-shm"):
            try:
                os.replace(self.path + ext, dest + ext)
            except OSError:
                pass
        self._prune_quarantine()
        return dest

    def _prune_quarantine(self):
        base = os.path.basename(self.path) + ".corrupt-"
        d = os.path.dirname(os.path.abspath(self.path))
        try:
            old = sorted((f for f in os.listdir(d) if f.startswith(base) and not f.endswith(("-wal", "-shm"))),
                         reverse=True)
            for f in old[self.KEEP_QUARANTINED:]:
                for ext in ("", "-wal", "-shm"):
                    try:
                        os.remove(os.path.join(d, f + ext))
                    except OSError:
                        pass
        except OSError:
            pass

    def _recover(self, reason, startup=False, move=True) -> bool:
        """Set the unusable database aside and start a fresh one; if that fails too, run from memory.
        Returns True when the log is usable again (it always is, one way or the other)."""
        with self._lock:
            now = time.monotonic()
            repeated = (not startup) and self._last_recovery and now - self._last_recovery < 30.0
            self._last_recovery = now
            moved = None
            if not self._mem_uri and move:
                moved = self._quarantine(reason)
            self._touched.clear()
            self._idle_since.clear()
            self._start_offset.clear()
            self.recoveries.append({"at": self.clock(), "reason": str(reason)[:200], "moved_to": moved})
            del self.recoveries[:-10]
            if moved:
                print(f"[joblog] !!! old job log moved to {moved}; starting a new, empty one. "
                      "Job history before this point is in that file.", flush=True)
            if not move:
                reason = f"{reason}; the file was left untouched"
            elif not self._mem_uri and not repeated:
                try:
                    self.migrate()
                    return True
                except (sqlite3.Error, OSError) as e:
                    reason = f"{reason}; a new file could not be made either ({type(e).__name__}: {e})"
            elif repeated:
                reason = f"{reason}; corrupted again within 30 s"
            return self._go_memory(reason)

    def _go_memory(self, reason) -> bool:
        self.degraded = str(reason)[:300]
        if not self._mem_uri:
            self._mem_uri = f"file:joblog-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = sqlite3.connect(self._mem_uri, uri=True, check_same_thread=False)
        print(f"[joblog] !!! job log is running from MEMORY ONLY ({self.degraded}); "
              "prints are still tracked but are lost on restart. Fix the disk and restart the Hub.",
              flush=True)
        try:
            self.migrate()
        except Exception as e:                # cannot happen with a memory db; still never raise
            print(f"[joblog] !!! in-memory job log failed too: {type(e).__name__}: {e}", flush=True)
            return False
        return True

    def close(self):
        """Release the in-memory fallback database, if any (connections are otherwise per call)."""
        keeper, self._keeper = self._keeper, None
        if keeper is not None:
            try:
                keeper.close()
            except Exception:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def storage_status(self) -> dict:
        """For the dashboard / health: is the log healthy, and what was recovered."""
        return {"ok": self.degraded is None and not self.recoveries, "degraded": self.degraded,
                "path": self.path, "recoveries": list(self.recoveries)}

    def _guard(self, fn):
        """Run a database operation; on corruption set the file aside, recover and try once more."""
        try:
            return fn()
        except sqlite3.DatabaseError as e:
            if not self._is_corruption(e):
                raise
            print(f"[joblog] !!! database error: {e}", flush=True)
            self._recover(f"{type(e).__name__}: {e}")
            return fn()

    def _mono(self):
        """Monotonic seconds. Looked up at call time because `.clock` may be swapped after construction
        (tests do): with the real wall clock this is time.monotonic, with any other clock it is that clock."""
        if self._mono_override is not None:
            return self._mono_override()
        return time.monotonic() if self.clock is time.time else self.clock()

    def _note_clock(self):
        """Notice a wall-clock step (NTP after boot, manual change) so durations ignore it."""
        w, m = self.clock(), self._mono()
        if self._last_clock is not None:
            step = (w - self._last_clock[0]) - (m - self._last_clock[1])
            if abs(step) > self.CLOCK_STEP_S:
                self._clock_offset += step
                print(f"[joblog] the system clock stepped by {step:+.0f} s; job durations ignore it", flush=True)
        self._last_clock = (w, m)

    def _duration(self, row, end):
        start = row["started_at"]
        if start is None:
            return None
        d = end - start
        if row["id"] in self._start_offset:
            d -= self._clock_offset - self._start_offset[row["id"]]
        return max(0.0, d)

    def migrate(self):
        """Apply any migration not yet recorded. Safe to call any number of times."""
        with self._lock:
            c = self._conn()
            c.isolation_level = None
            try:
                c.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                          "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
                done = {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
                for version, sql in sorted(self.MIGRATIONS, key=lambda m: m[0]):
                    if version in done:
                        continue
                    c.execute("BEGIN")       # DDL + bookkeeping in ONE transaction: all or nothing
                    try:
                        for stmt in [s for s in sql.split(";\n") if s.strip()]:
                            c.execute(stmt)
                        c.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                                  (version, self.clock()))
                        c.execute("COMMIT")
                    except Exception:
                        c.execute("ROLLBACK")
                        raise
            finally:
                c.close()

    MIGRATIONS = MIGRATIONS

    def schema_version(self) -> int:
        def go():
            c = self._conn()
            try:
                r = c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                return r[0] or 0
            finally:
                c.close()
        with self._lock:
            return self._guard(go)

    def _q(self, sql, args=()):
        def go():
            c = self._conn()
            try:
                return [dict(r) for r in c.execute(sql, args).fetchall()]
            finally:
                c.close()
        with self._lock:
            return self._guard(go)

    def _exec(self, sql, args=()):
        def go():
            c = self._conn()
            try:
                with c:
                    cur = c.execute(sql, args)
                    return cur.lastrowid
            finally:
                c.close()
        with self._lock:
            return self._guard(go)

    def _update(self, jid, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))

    def _insert(self, **fields) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self._exec(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))

    def _resolve(self, printer, job) -> dict:
        if self.resolver is None:
            return {}
        try:
            return normalize_resolved(self.resolver(printer, job))
        except Exception as e:                 # a broken resolver must never break logging
            print(f"[joblog] resolver failed for {printer}/{job}: {type(e).__name__}: {e}", flush=True)
            return {}

    # ---- finding rows -----------------------------------------------------

    def _open_rows(self, printer):
        return self._q("SELECT * FROM jobs WHERE printer=? AND ended_at IS NULL ORDER BY started_at DESC, id DESC",
                       (printer,))

    @staticmethod
    def _label(job) -> str:
        j = (job or "").strip()
        return "" if j.lower() in UNNAMED else j

    # ---- the EventWatcher sink --------------------------------------------

    def on_event(self, ev):
        """EventWatcher sink. Accepts events.Event objects or dicts with the same keys.
        Returns the affected job id, or None for events that are not about a print.
        Never raises."""
        try:
            kind = ev.get("kind") if isinstance(ev, dict) else ev.kind
            printer = ev.get("printer") if isinstance(ev, dict) else ev.printer
            ts = ev.get("ts") if isinstance(ev, dict) else ev.ts
            data = (ev.get("data") if isinstance(ev, dict) else ev.data) or {}
            ts = float(ts if ts is not None else self.clock())
            if not printer:
                return None
            with self._lock:
                self._note_clock()
                if kind == "print_started":
                    return self._on_started(printer, ts, data)
                if kind == "print_paused":
                    return self._on_paused(printer, ts, data)
                if kind == "print_finished":
                    return self._on_end(printer, ts, data, "finished")
                if kind == "print_failed":
                    return self._on_end(printer, ts, data, "cancelled" if data.get("cancelled") else "failed")
        except Exception as e:
            print(f"[joblog] event failed: {type(e).__name__}: {e}", flush=True)
        return None

    def _on_started(self, printer, ts, data):
        job = self._label(data.get("job"))
        key = _costs.file_key(job)
        rows = self._open_rows(printer)
        for r in rows:
            if key and r["file_key"] == key or (not key and not r["file_key"]):
                return r["id"]                 # the same print announced twice: keep the original row
        for r in rows:                         # an older job never closed: it ended without telling us
            self._close(r, ts, "unknown", error="superseded: a new print started before this one reported an end")
        res = self._resolve(printer, job)
        jid = self._insert(printer=printer, kind=res.get("kind"), model=res.get("model"), file=job or None,
                           file_key=key or None, plate=res.get("plate"), started_at=ts, result="running",
                           percent=0.0, est_seconds=res.get("seconds"), grams_est=res.get("grams"),
                           material=res.get("material"), colour=res.get("colour"),
                           filaments=json.dumps(res.get("filaments") or []), parts=res.get("parts"),
                           parts_source=res.get("parts_source"), ext_job_id=data.get("job_id"),
                           last_seen_at=ts, source="event")
        self._start_offset[jid] = self._clock_offset          # so a later clock step is not counted as print time
        return jid

    def _on_paused(self, printer, ts, data):
        rows = self._open_rows(printer)
        if not rows:
            return None
        r = rows[0]
        fields = {"pauses": (r["pauses"] or 0) + 1, "last_seen_at": ts}
        pc = _costs._f(data.get("percent"))
        if pc is not None:
            fields["percent"] = pc
        self._update(r["id"], **fields)
        return r["id"]

    def _match_open(self, printer, key):
        rows = self._open_rows(printer)
        if not rows:
            return None
        r = rows[0]
        if key and r["file_key"] and r["file_key"] != key:
            # the printer reports a different job than the one we logged: close the stale row
            return None
        return r

    def _on_end(self, printer, ts, data, result):
        job = self._label(data.get("job"))
        key = _costs.file_key(job)
        row = self._match_open(printer, key)
        if row is None:
            # (a) the end event for a row the orphan sweep just closed: finish it properly
            recent = self._q("SELECT * FROM jobs WHERE printer=? AND source='event' "
                             "AND ((result='unknown' AND error LIKE 'closed: printer seen idle%') "
                             "OR error LIKE 'ended while the Hub was not watching%') AND last_seen_at >= ? "
                             "AND id=(SELECT MAX(id) FROM jobs WHERE printer=?) ORDER BY id DESC LIMIT 1",
                             (printer, ts - 900, printer))
            if recent and (not key or recent[0]["file_key"] in (None, key)):
                row = recent[0]
            else:
                # (b) a duplicate delivery of an end we already logged
                dup = self._q("SELECT id FROM jobs WHERE printer=? AND result IN ('finished','failed','cancelled') "
                              "AND (file_key IS ? OR file_key=?) AND ended_at IS NOT NULL AND ABS(ended_at-?) <= ?",
                              (printer, key or None, key, ts, self.dup_window_s))
                if dup:
                    return dup[0]["id"]
                return self._late_row(printer, ts, data, result, job, key)
        pc = 100.0 if result == "finished" else _costs._f(data.get("percent"))
        if pc is None:
            pc = row["percent"]
        self._close(row, ts, result, percent=pc, error=(data.get("error") or None),
                    seconds=_costs._f(data.get("seconds")))
        return row["id"]

    def _late_row(self, printer, ts, data, result, job, key):
        """An end event with no start on record (Hub restarted mid-print, or the DB is new):
        log what we know. Start time is back-dated by the reported duration when there is one."""
        res = self._resolve(printer, job)
        secs = _costs._f(data.get("seconds"))
        pc = 100.0 if result == "finished" else _costs._f(data.get("percent"))
        jid = self._insert(printer=printer, kind=res.get("kind"), model=res.get("model"), file=job or None,
                           file_key=key or None, plate=res.get("plate"),
                           started_at=(ts - secs) if secs else None, ended_at=ts, duration_s=secs,
                           result=result, percent=pc, est_seconds=res.get("seconds"),
                           grams_est=res.get("grams"), grams_used=self._used(res.get("grams"), result, pc),
                           material=res.get("material"), colour=res.get("colour"),
                           filaments=json.dumps(res.get("filaments") or []), parts=res.get("parts"),
                           parts_source=res.get("parts_source"), error=data.get("error") or None,
                           ext_job_id=data.get("job_id"), source="late", last_seen_at=ts)
        return jid

    @staticmethod
    def _used(grams, result, percent):
        g = _costs._f(grams)
        if g is None:
            return None
        if result == "finished":
            return round(g, 3)
        pc = _costs._f(percent)
        return None if pc is None else round(g * min(1.0, max(0.0, pc / 100.0)), 3)

    def _close(self, row, ts, result, percent=None, error=None, seconds=None, swept_at=None):
        """Finish an open (or just-orphaned) row. Back-fills file facts if they were not
        available when the print started."""
        fields = {"ended_at": ts, "result": result}
        if percent is not None:
            fields["percent"] = percent
        pc = percent if percent is not None else row["percent"]
        start = row["started_at"]
        if seconds is not None and seconds > 0:
            fields["duration_s"] = seconds
        elif start is not None:
            fields["duration_s"] = self._duration(row, ts)
        fields["error"] = error or None          # also clears an orphan note when a late end arrives
        if swept_at is not None:
            fields["last_seen_at"] = swept_at     # when the orphan sweep happened (lets a late end event find the row)
        grams = row["grams_est"]
        if grams is None and row["file"] is not None:
            res = self._resolve(row["printer"], row["file"])
            if res:
                grams = res.get("grams")
                for k, col in (("grams", "grams_est"), ("seconds", "est_seconds"), ("parts", "parts"),
                               ("parts_source", "parts_source"), ("material", "material"),
                               ("colour", "colour"), ("model", "model"), ("kind", "kind")):
                    if row[col] is None and res.get(k) is not None:
                        fields[col] = res[k]
                if res.get("filaments") and not json.loads(row["filaments"] or "[]"):
                    fields["filaments"] = json.dumps(res["filaments"])
        fields["grams_used"] = self._used(grams, result, pc)
        self._update(row["id"], **fields)
        self._touched.pop(row["id"], None)
        self._start_offset.pop(row["id"], None)
        self._idle_since.pop(row["printer"], None)

    # ---- fleet observation ------------------------------------------------

    def observe(self, fleet: dict, now=None) -> list:
        """Feed the same {printer: snapshot} dict the EventWatcher gets. Records progress of open
        jobs and closes orphans:
          * the printer is reachable and has been not-printing for `orphan_grace_s`;
          * or it reports a different job than the open row;
          * or the printer is gone / unreachable and the row has been silent for `stale_after_s`.
        Orphans are closed as `unknown` at the last time the job was seen active (so the
        duration is honest). Returns the ids closed."""
        now = self.clock() if now is None else now
        closed = []
        try:
            with self._lock:
                self._note_clock()
                for row in self._q("SELECT * FROM jobs WHERE ended_at IS NULL"):
                    jid = self._observe_one(row, (fleet or {}).get(row["printer"]), now)
                    if jid:
                        closed.append(jid)
        except Exception as e:
            print(f"[joblog] observe failed: {type(e).__name__}: {e}", flush=True)
        return closed

    def _observe_one(self, row, snap, now):
        printer = row["printer"]
        seen = row["last_seen_at"] or row["started_at"] or now
        reachable = isinstance(snap, dict) and bool(snap.get("connected")) and \
            str(snap.get("state") or "UNKNOWN").upper() not in ("UNKNOWN", "DISCONNECTED")
        if not reachable:
            if now - seen >= self.stale_after_s:
                self._close(row, seen, "unknown", error="closed: printer unreachable, end never reported")
                return row["id"]
            return None
        state = str(snap.get("state") or "").upper()
        if state in ACTIVE_STATES:
            self._idle_since.pop(printer, None)
            name = self._label(snap.get("job"))
            if name and row["file_key"] and _costs.file_key(name) != row["file_key"]:
                self._close(row, seen, "unknown", error="closed: printer moved on to a different job")
                return row["id"]
            pc = _costs._f(snap.get("percent"))
            last = self._touched.get(row["id"], 0)
            if (pc is not None and pc != row["percent"]) and (now - last >= self.touch_every_s or pc >= 100):
                self._update(row["id"], percent=pc, last_seen_at=now)
                self._touched[row["id"]] = now
            elif now - last >= self.touch_every_s:
                self._update(row["id"], last_seen_at=now)
                self._touched[row["id"]] = now
            return None
        # reachable and not printing
        since = self._idle_since.setdefault(printer, now)
        if now - since >= self.orphan_grace_s:
            done = self._finished_while_away(row, snap, seen)
            if done:
                result, ended, secs = done
                self._close(row, ended, result, percent=100.0 if result == "finished" else None,
                            error="ended while the Hub was not watching; end time is approximate",
                            seconds=secs, swept_at=now)
                return row["id"]
            self._close(row, max(seen, row["started_at"] or 0), "unknown",
                        error="closed: printer seen idle, end event not received", swept_at=now)
            return row["id"]
        return None

    def _finished_while_away(self, row, snap, seen):
        """The Hub was down (or missed the event) while a print ended, and the printer still reports it
        (FINISH / FAILED, same job): a known outcome, not 'unknown'. -> (result, ended_at, seconds) | None.
        The end time is approximate: the slicer estimate when there is one, else the last time it was seen."""
        state = str(snap.get("state") or "").upper()
        if state not in ("FINISH", "FAILED"):
            return None
        name = self._label(snap.get("job"))
        if name and row["file_key"] and _costs.file_key(name) != row["file_key"]:
            return None
        start = row["started_at"]
        if state == "FAILED":
            ended = max(seen, start or 0)
            return "failed", ended, (ended - start) if start is not None and ended > start else None
        pc = _costs._f(snap.get("percent"))
        if pc is not None and pc < 98:
            return None
        est = _costs._f(row["est_seconds"])
        if start is not None and est and est > 0:
            return "finished", max(seen, start + est), est
        ended = max(seen, start or 0)
        return "finished", ended, (ended - start) if start is not None and ended > start else None

    # ---- queries ----------------------------------------------------------

    def _public(self, r: dict, with_cost=True) -> dict:
        d = dict(r)
        fil = d.get("filaments")
        if not isinstance(fil, list):          # a row from the database holds JSON text; a row already made public holds the list
            try:
                fil = json.loads(fil or "[]")
            except ValueError:
                fil = []
        d["filaments"] = fil
        d["open"] = d.get("ended_at") is None
        for k, out in (("started_at", "started_iso"), ("ended_at", "ended_iso")):
            d[out] = self._iso(d.get(k))
        est, dur = d.get("est_seconds"), d.get("duration_s")
        d["time_vs_estimate_pct"] = round((dur / est - 1) * 100, 1) if est and dur and d["result"] == "finished" else None
        if with_cost:
            rates = self.rates()
            if d["open"] or d["result"] == "unknown":
                d["cost"] = None
            else:
                d["cost"] = _costs.job_cost(d, rates)
        return d

    def _iso(self, ts):
        if ts is None:
            return None
        return datetime.datetime.fromtimestamp(ts, self.tz).isoformat(timespec="seconds")

    def recent_jobs(self, limit=50, printer=None, result=None) -> list:
        """Newest first. `printer` filters (case-insensitive); `result` is one of RESULTS."""
        limit = max(1, min(int(limit or 50), 1000))
        where, args = [], []
        if printer:
            where.append("LOWER(printer)=LOWER(?)")
            args.append(printer)
        if result:
            if result not in RESULTS:
                raise ValueError(f"result must be one of {', '.join(RESULTS)}")
            where.append("result=?")
            args.append(result)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY COALESCE(started_at, ended_at) DESC, id DESC LIMIT ?"
        return [self._public(r) for r in self._q(sql, (*args, limit))]

    def job(self, job_id):
        rows = self._q("SELECT * FROM jobs WHERE id=?", (int(job_id),))
        return self._public(rows[0]) if rows else None

    def _window(self, period, printer=None, now=None):
        secs = _period_seconds(period)
        now = self.clock() if now is None else now
        since = None if secs is None else now - secs
        where, args = [], []
        if since is not None:
            where.append("COALESCE(ended_at, started_at) >= ?")
            args.append(since)
        if printer:
            where.append("LOWER(printer)=LOWER(?)")
            args.append(printer)
        rows = self._q("SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
                       + " ORDER BY COALESCE(started_at, ended_at)", tuple(args))
        return since, now, [self._public(r, with_cost=False) for r in rows]

    def stats(self, period="month", printer=None, now=None, series_bucket=None) -> dict:
        """Totals for the trailing `period` (day = 24 h, week = 7 d, month = 30 d, year, all, or
        '<N>d'), optionally one printer: counts, success rate, hours, grams, cost split, clean and
        true cost per part, and breakdowns by printer, material and file (with the material
        series per day / week / month)."""
        since, until, rows = self._window(period, printer, now)
        rates = self.rates()
        out = {"period": period, "since": since, "until": until, "printer": printer,
               "currency": rates.currency}
        out.update(_costs.aggregate(rows, rates))
        by_printer, by_file, by_mat = {}, {}, {}
        for r in rows:
            by_printer.setdefault(r["printer"], []).append(r)
            by_file.setdefault(_costs.file_key(r.get("file")) or "(unnamed)", []).append(r)
            if r["result"] in ("finished", "failed", "cancelled"):
                for t, g in _costs.material_grams(r).items():
                    by_mat[t] = round(by_mat.get(t, 0.0) + g, 2)
        slim = ("jobs", "finished", "failed", "cancelled", "success_rate", "hours", "grams_used",
                "good_parts", "true_cost_per_part", "clean_cost_per_part")
        out["by_printer"] = {}
        for k, v in sorted(by_printer.items()):
            a = _costs.aggregate(v, rates)
            out["by_printer"][k] = {s: a[s] for s in slim}
            out["by_printer"][k]["cost_total"] = a["cost"]["total"]
        files = []
        for k, v in by_file.items():
            a = _costs.aggregate(v, rates)
            files.append({"file": k, **{s: a[s] for s in slim}, "cost_total": a["cost"]["total"]})
        files.sort(key=lambda f: (-f["cost_total"], f["file"]))
        out["by_file"] = files[:25]
        out["by_material"] = by_mat
        bucket = series_bucket or ("month" if str(period).lower() in ("year", "all") else "day")
        out["material_series"] = _costs.material_series(rows, bucket, self.tz)
        out["series_bucket"] = bucket
        return out

    def material_usage(self, bucket="day", period="month", printer=None, now=None) -> list:
        """Grams per day / week / month, by material, over `period`."""
        if bucket not in ("day", "week", "month"):
            raise ValueError("bucket must be day, week or month")
        _, _, rows = self._window(period, printer, now)
        return _costs.material_series(rows, bucket, self.tz)

    def file_costs(self, name, last_n=None, printer=None) -> dict:
        """Cost per part for one file over its last N attempts (default: the Costs 'window_n')."""
        key = _costs.file_key(name)
        if not key:
            raise ValueError("file name required")
        where, args = ["file_key=?"], [key]
        if printer:
            where.append("LOWER(printer)=LOWER(?)")
            args.append(printer)
        rows = [self._public(r, with_cost=False)
                for r in self._q("SELECT * FROM jobs WHERE " + " AND ".join(where), tuple(args))]
        rates = self.rates()
        out = {"file": name, "file_key": key, "printer": printer, "currency": rates.currency}
        out.update(_costs.file_costs(rows, rates, last_n))
        out["running"] = sum(1 for r in rows if r["result"] == "running")
        return out

    CSV_COLUMNS = ["id", "printer", "model", "file", "plate", "started", "ended", "duration_min", "result",
                   "percent", "est_min", "grams_est", "grams_used", "material", "colour", "parts", "pauses",
                   "material_cost", "electricity_cost", "wear_cost", "total_cost", "cost_per_part", "error"]

    # characters a spreadsheet reads as the start of a formula - the ASCII ones and their full-width / math look-alikes
    _CSV_TRIGGERS = frozenset("=+-@\t\r" "\uff1d\uff0b\uff0d\uff20" "\u2212\u2013\u2014\ufe62\ufe63\ufe66")
    # whitespace and invisible characters a spreadsheet skips over before it decides: they must not hide the trigger
    _CSV_SKIP = frozenset(" \n\x0b\x0c\x85\xa0\u1680\u180e\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
                          "\u2009\u200a\u200b\u200c\u200d\u200e\u200f\u2028\u2029\u202a\u202b\u202c\u202d\u202e"
                          "\u202f\u205f\u2060\u2066\u2067\u2068\u2069\u3000\ufeff")

    @classmethod
    def _csv_safe(cls, v):
        """A file name like "=cmd|..." must not become a spreadsheet formula, however it is dressed: after leading
        spaces / newlines / zero-width characters, with a full-width `＝`, as a tab or a carriage return. A cell
        that starts with any of them gets a leading apostrophe (text, not a formula)."""
        if not isinstance(v, str) or not v:
            return v
        i = 0
        while i < len(v) and v[i] in cls._CSV_SKIP:
            i += 1
        if v[0] in cls._CSV_TRIGGERS or (i < len(v) and v[i] in cls._CSV_TRIGGERS):
            return "'" + v
        return v

    def export_csv(self, printer=None, period="all", fp=None, now=None) -> str:
        """All jobs in the period as CSV (oldest first), with costs at the current rates.
        Writes to `fp` if given; always returns the text."""
        _, _, rows = self._window(period, printer, now)
        rates = self.rates()
        buf = io.StringIO()
        # every cell quoted: in a semicolon-locale Excel an unquoted `x;=1+1` would split into two cells, the second a formula
        csv.writer(buf, lineterminator="\n").writerow(self.CSV_COLUMNS)
        w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_ALL)
        for r in rows:
            j = self._public(r)
            c = j.get("cost") or {}
            w.writerow([self._csv_safe(x) if x is not None else "" for x in [
                j["id"], j["printer"], j["model"], j["file"], j["plate"], j["started_iso"], j["ended_iso"],
                None if j["duration_s"] is None else round(j["duration_s"] / 60, 1), j["result"],
                j["percent"], None if j["est_seconds"] is None else round(j["est_seconds"] / 60, 1),
                j["grams_est"], j["grams_used"], j["material"], j["colour"], j["parts"], j["pauses"],
                c.get("material"), c.get("electricity"), c.get("wear"), c.get("total"), c.get("per_part"),
                j["error"]]])
        text = buf.getvalue()
        if fp is not None:
            fp.write(text)
        return text
