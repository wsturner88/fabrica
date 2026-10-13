"""File backup for the Hub — zip up every file a printer's file listing returns.

One background job per printer, never on the request path. Result is
    <root>/<printer>/<printer>-YYYYMMDD-HHMMSS.zip
holding the files (folder paths kept) plus a manifest.json (printer, model,
created, per-file name/size/modified/sha256, totals, and any files that failed
with the reason). The zip is written under a `.part` name and renamed only when
complete, so a half-written archive is never listed or downloadable.

Rules:
  * Bambu: files come over FTPS via BambuPrinter.sd_list()/sd_download(), taking
    the same per-printer FTPS lock the metadata pre-warm uses (one session at a
    time). The SD root's `.3mf` files are included, plus `cache/*.3mf` (jobs sent
    straight from the slicer land in /cache; listed via sd_list_dir). No other
    folder is ever read (ipcam/, logger/, recorder/, timelapse/, image/, hidden
    or system folders). REFUSED while the printer is RUNNING/PREPARE/PAUSE(D), or
    while its state is unknown (MQTT silent) — we cannot prove it is idle. The
    state is re-checked before every file; a print starting mid-backup aborts.
  * OctoPrint: files come over HTTP from /downloads/files/local/<path> with the
    API key header. Allowed while printing — it reads OctoPrint's storage, not
    the printer.
  * Any driver with open_file(name) (PrusaLink, Moonraker): listed with files(),
    streamed over HTTP with the same chunk + sha256 loop as OctoPrint. Whether it
    may run mid-print follows the driver's `backup_mid_print_ok` trait (Moonraker
    True: reads the Pi's disk; PrusaLink False: reads the printer's USB stick, so
    it is refused like Bambu). A driver with neither the trait nor kind
    "octoprint" is refused while busy or unknown.
  * Retention: the newest BACKUP_KEEP (default 5) COMPLETE zips per printer. A partial backup
    (some files failed) or an unreadable zip never counts toward that number and never pushes a
    complete one out: partial ones are kept in a separate pool of at most KEEP_PARTIAL (2).
    Order is by the manifest's timezone-aware `created` time, not the file name, so a DST change
    cannot make an older backup look newer.
  * One job per printer at a time. "all" queues every printer, run one after
    another by a single worker.

This module knows nothing about the web layer or MCP; server.py wires it in.
"""

import datetime
import hashlib
import inspect
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import zipfile

from bambu import PrinterError, PrinterBusy

BUSY_STATES = ("RUNNING", "PREPARE", "PAUSE", "PAUSED")
DEFAULT_KEEP = 5
KEEP_PARTIAL = 2          # partial / unreadable zips kept per printer, besides the complete ones
_STORED_EXT = (".3mf", ".bgcode", ".zip", ".png", ".jpg")   # already compressed
_CHUNK = 256 * 1024
# Extra Bambu SD folders whose .3mf files are backed up (besides the root). Allow-list on
# purpose: ipcam/ logger/ recorder/ timelapse/ image/ and hidden/system folders stay out.
BAMBU_EXTRA_DIRS = ("cache",)

_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_STAMP = r"\d{8}-\d{6}"


class BackupError(Exception):
    """A backup request that cannot proceed. `status` is the HTTP code the
    REST layer answers with; str(e) is a message written for a human/agent."""
    status = 400


class UnknownPrinter(BackupError):
    status = 404


class BackupRunning(BackupError):
    status = 409


class BackupBusy(BackupError):
    status = 423


def keep_count() -> int:
    """How many zips to keep per printer (env BACKUP_KEEP, default 5, min 1)."""
    try:
        return max(1, int(os.environ.get("BACKUP_KEEP", DEFAULT_KEEP)))
    except ValueError:
        return DEFAULT_KEEP


def printer_dirname(name: str) -> str:
    """Filesystem-safe directory/file prefix for a printer name."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".-")
    return safe or "printer"


def _arcname(name: str):
    """Zip entry name for a printer-side path (folders kept). None if unsafe."""
    n = (name or "").replace("\\", "/")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _iso(ts=None) -> str:
    dt = (datetime.datetime.fromtimestamp(ts) if ts is not None
          else datetime.datetime.now())
    return dt.astimezone().isoformat(timespec="seconds")


def _modified(v):
    """Manifest 'modified': OctoPrint gives epoch seconds, Bambu FTPS a text date."""
    if isinstance(v, (int, float)):
        return datetime.datetime.fromtimestamp(
            v, datetime.timezone.utc).isoformat(timespec="seconds")
    return v


class Job:
    def __init__(self, printer: str, batch=None):
        self.id = f"bk-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
        self.printer = printer
        self.batch = batch
        self.status = "queued"       # queued | running | done | partial | failed
        self.files_total = 0
        self.files_done = 0
        self.files_failed = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.current = None
        self.error = None
        self.file = None             # final zip filename once finished
        self.size = None
        self.queued_at = _iso()
        self.started_at = None
        self.finished_at = None
        self.failures = []

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "printer": self.printer, "status": self.status,
            "files_done": self.files_done, "files_total": self.files_total,
            "files_failed": self.files_failed,
            "bytes_done": self.bytes_done, "bytes_total": self.bytes_total,
            "current_file": self.current, "error": self.error,
            "queued_at": self.queued_at, "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.failures:
            d["failed_files"] = list(self.failures)
        if self.file:
            d["file"] = self.file
            d["size"] = self.size
            d["download"] = f"/api/backups/{printer_dirname(self.printer)}/{self.file}"
        return d


class _Abort(Exception):
    """Job-level stop (printer got busy, no space, ...). Message is user-facing."""


class BackupManager:
    def __init__(self, printers: dict, root: str, ftps_lock=None, pause_between_files=0.5,
                 keep=None, on_finish=None):
        self.printers = printers          # name.lower() -> printer driver (shared dict)
        self.root = root
        self._keep = keep                 # callable -> zips to keep per printer (Settings); None = env / default
        self._on_finish = on_finish       # callable(job) once per job when it ends (done / partial / failed)
        self._ftps_lock = ftps_lock or (lambda name: threading.Lock())
        self._pause = pause_between_files
        self._lock = threading.Lock()
        self._jobs = []                   # newest last, capped
        self._active = {}                 # printer name -> Job (queued or running)
        self._manifest_cache = {}         # (path, mtime_ns, size) -> manifest summary

    # ------------------------------------------------------------ requests

    def _pick(self, name: str):
        prn = self.printers.get((name or "").lower())
        if not prn:
            raise UnknownPrinter(f"Unknown printer '{name}'. Known: {', '.join(p.name for p in self.printers.values())}")
        return prn

    @staticmethod
    def _busy_reason(prn):
        """Why a printer may not be backed up right now (None = fine). OctoPrint and
        drivers whose `backup_mid_print_ok` trait is True read someone else's storage
        and are always fine; everything else (Bambu, PrusaLink, unknown drivers) is
        refused while busy or while its state cannot be confirmed idle."""
        if getattr(prn, "kind", "") == "octoprint" or getattr(prn, "backup_mid_print_ok", False):
            return None   # reads OctoPrint's / the host's own storage; safe mid-print
        s = prn.snapshot()
        state = s.get("state")
        ftps = not hasattr(prn, "open_file")      # Bambu: one FTPS session on the SD card
        if state in BUSY_STATES:
            job = s.get("job") or "a job"
            what = ("Backup uses the printer's single FTPS session" if ftps
                    else "Backup reads the printer's own storage")
            return (f"{prn.name} is {state} on '{job}'. {what} and is not allowed "
                    "while a job is active — retry when it is idle.")
        if state in (None, "UNKNOWN") or not s.get("connected"):
            how = "not reporting over MQTT" if ftps else "not reachable"
            return (f"{prn.name} state is unknown ({how}), so it cannot be "
                    "confirmed idle. Check the printer is on and connected, then retry.")
        return None

    def _reserve(self, prn, batch=None) -> Job:
        """Create a queued job for prn or raise. Caller must hold no lock."""
        def running_error(cur):
            return BackupRunning(
                f"A backup of {prn.name} is already {cur.status} (job {cur.id}, "
                f"{cur.files_done}/{cur.files_total} files). Check backup_status.")
        with self._lock:
            cur = self._active.get(prn.name)
            if cur:
                raise running_error(cur)
        # Asking the printer for its state can take seconds (MQTT / HTTP): never while holding the
        # lock that status() and every other request need.
        reason = self._busy_reason(prn)
        if reason:
            raise BackupBusy(reason)
        with self._lock:
            cur = self._active.get(prn.name)            # someone may have reserved it meanwhile
            if cur:
                raise running_error(cur)
            job = Job(prn.name, batch)
            self._active[prn.name] = job
            self._jobs.append(job)
            del self._jobs[:-30]
            return job

    def start(self, printer: str) -> dict:
        """Start a backup of one printer ("all" -> start_all). Returns job info
        immediately; the work happens on a background thread."""
        if (printer or "").strip().lower() == "all":
            return self.start_all()
        prn = self._pick(printer)
        job = self._reserve(prn)
        threading.Thread(target=self._worker, args=([(job, prn)],), daemon=True,
                         name=f"backup-{prn.name}").start()
        return {"job": job.to_dict(), "stored_in": self._dir_hint(prn),
                "note": "Running in the background. Poll backup_status / GET /api/backups."}

    def start_all(self) -> dict:
        """Queue every printer; one worker runs them one after another.
        Printers that are busy/already running are reported, not fatal."""
        queue, skipped = [], []
        batch = f"all-{time.strftime('%H%M%S')}"
        for prn in list(self.printers.values()):
            try:
                queue.append((self._reserve(prn, batch), prn))
            except BackupError as e:
                skipped.append({"printer": prn.name, "reason": str(e)})
        if queue:
            threading.Thread(target=self._worker, args=(queue,), daemon=True,
                             name="backup-all").start()
        if not queue:
            if not skipped:
                raise BackupError("No printers configured.")
            err = BackupRunning if all(s["reason"].startswith("A backup of") for s in skipped) else BackupBusy
            raise err("Nothing started. " + " | ".join(s["reason"] for s in skipped))
        return {"jobs": [j.to_dict() for j, _ in queue], "skipped": skipped,
                "note": "Printers are backed up one after another. Poll backup_status / GET /api/backups."}

    def _dir_hint(self, prn) -> str:
        return os.path.join(self.root, printer_dirname(prn.name))

    # ------------------------------------------------------------ status

    def status(self) -> dict:
        with self._lock:
            jobs = [j.to_dict() for j in self._jobs]
        running = [j for j in jobs if j["status"] in ("queued", "running")]
        recent = [j for j in jobs if j["status"] not in ("queued", "running")][-8:][::-1]
        return {"keep_per_printer": self.keep_count(), "running": running, "recent": recent,
                "backups": self.list_backups(),
                "printers": [{"name": p.name, "dir": printer_dirname(p.name)}
                             for p in self.printers.values()]}

    def list_backups(self) -> dict:
        """{printer_dir: [ {file,size,created,file_count,download}, ... newest first ]}"""
        out = {}
        if not os.path.isdir(self.root):
            return out
        for d in sorted(os.listdir(self.root)):
            pdir = os.path.join(self.root, d)
            if not (_DIR_RE.match(d) and os.path.isdir(pdir)):
                continue
            rows = []
            for z in self._zips(pdir, d):
                info = z["info"]
                rows.append({"file": z["file"], "size": z["size"],
                             "created": info.get("created") or _iso(z["mtime"]),
                             "file_count": info.get("file_count"),
                             "failed_count": info.get("failed_count", 0),
                             "complete": z["complete"],
                             "download": f"/api/backups/{d}/{z['file']}"})
            if rows:
                out[d] = rows
        return out

    def _zips(self, pdir, base) -> list:
        """Every backup zip of one printer, newest first: [{file,path,size,mtime,info,complete,when}].
        `complete` is the manifest's own verdict (an unreadable zip is not complete)."""
        pat = re.compile(rf"^{re.escape(base)}-{_STAMP}\.zip$")
        out = []
        try:
            names = os.listdir(pdir)
        except OSError:
            return out
        for fn in names:
            if not pat.match(fn):
                continue
            path = os.path.join(pdir, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            info = self._manifest_info(path, st)
            out.append({"file": fn, "path": path, "size": st.st_size, "mtime": st.st_mtime, "info": info,
                        "complete": info.get("complete") is True,
                        "when": _epoch(info.get("created"), st.st_mtime)})
        out.sort(key=lambda z: (z["when"], z["file"]), reverse=True)
        return out

    def _manifest_info(self, path, st) -> dict:
        key = (path, st.st_mtime_ns, st.st_size)
        hit = self._manifest_cache.get(key)
        if hit is not None:
            return hit
        info = {}
        try:
            with zipfile.ZipFile(path) as z:
                m = json.loads(z.read("manifest.json"))
                failed = m.get("failed", [])
                done = m.get("complete")
                info = {"created": m.get("created"),
                        "file_count": len(m.get("files", [])),
                        "failed_count": len(failed),
                        "complete": (not failed) if not isinstance(done, bool) else (done and not failed)}
        except Exception:
            info = {}
        if len(self._manifest_cache) > 200:
            self._manifest_cache.clear()
        self._manifest_cache[key] = info
        return info

    def resolve_download(self, printer: str, filename: str):
        """Absolute path of a completed backup zip, or None. Both parts are
        validated strictly: plain names only, must match what we write, must
        live directly under root/<printer>/."""
        if not (_DIR_RE.match(printer or "") and _DIR_RE.match(filename or "")):
            return None
        if not re.match(rf"^{re.escape(printer)}-{_STAMP}\.zip$", filename):
            return None
        path = os.path.join(self.root, printer, filename)
        real_root = os.path.realpath(self.root)
        real = os.path.realpath(path)
        if os.path.dirname(os.path.dirname(real)) != real_root or not os.path.isfile(real):
            return None
        return real

    # ------------------------------------------------------------ worker

    def _worker(self, queue):
        for job, prn in queue:
            try:
                self._run(job, prn)
            except Exception as e:   # belt and braces: never leave a job "running"
                self._finish(job, "failed", f"{type(e).__name__}: {e}")
            finally:
                with self._lock:
                    if self._active.get(prn.name) is job:
                        del self._active[prn.name]

    def keep_count(self) -> int:
        """Zips kept per printer: the Settings value when wired in, else env BACKUP_KEEP / 5."""
        if self._keep is not None:
            try:
                return max(1, int(self._keep()))
            except Exception:
                pass
        return keep_count()

    def _finish(self, job, status, error=None):
        with self._lock:
            first = job.finished_at is None
            job.status, job.error = status, error
            job.current = None
            job.finished_at = _iso()
        print(f"[backup] {job.printer}: {status}"
              + (f" — {error}" if error else "")
              + (f" ({job.file})" if job.file else ""), flush=True)
        if first and self._on_finish is not None:
            try:
                self._on_finish(job)
            except Exception as e:        # a notification problem must never fail a backup
                print(f"[backup] {job.printer}: finish hook failed: {e}", flush=True)

    def _list(self, prn):
        """Normalised file listing: ([{name,size,modified,origin}], [list_failures]).
        list_failures = folders that could not be listed ({name,error}); they are
        carried into the manifest as failed items. Raises _Abort."""
        extra_failed = []
        try:
            if getattr(prn, "kind", "") == "octoprint":
                rows = prn.files()
            elif hasattr(prn, "open_file"):
                rows = _files_for_backup(prn)
            else:
                with self._ftps_lock(prn.name):
                    self._check_still_idle(prn)      # N-12: re-checked AFTER taking the FTPS lock
                    rows = list(prn.sd_list())
                    if hasattr(prn, "sd_list_dir"):
                        for d in BAMBU_EXTRA_DIRS:
                            try:
                                rows += prn.sd_list_dir(d)
                            except Exception as e:     # root listing succeeded: keep going
                                extra_failed.append({"name": d + "/", "error": _err(e)})
        except _Abort:
            raise
        except Exception as e:
            raise _Abort(f"Could not list files on {prn.name}: {_err(e)}")
        return ([{"name": r["name"], "size": int(r.get("size") or 0),
                  "modified": _modified(r.get("modified")),
                  "origin": r.get("origin", "local")} for r in rows], extra_failed)

    def _check_still_idle(self, prn):
        reason = self._busy_reason(prn)
        if reason:
            raise _Abort("Stopped: " + reason)

    def _fetch(self, prn, f, tmp_dir):
        """Download one file. Returns (path_or_None, data_or_None, size, sha256).
        OctoPrint and open_file() drivers stream to a temp file (gcode can be 100+ MB);
        Bambu's existing sd_download() returns bytes."""
        if getattr(prn, "kind", "") == "octoprint":
            if f["origin"] != "local":
                raise ValueError(f"stored on '{f['origin']}', not downloadable")
            url = "/downloads/files/local/" + urllib.parse.quote(f["name"], safe="/")
            try:
                return _stream_to_temp(prn._get_raw(url, timeout=30), tmp_dir)
            except urllib.error.HTTPError as e:
                e.close()
                raise ValueError(f"OctoPrint refused the download ({e.code} {e.reason})")
        if hasattr(prn, "open_file"):
            if f["origin"] != "local":
                raise ValueError(f"stored on '{f['origin']}', not downloadable")
            self._check_still_idle(prn)           # N-12: immediately before the download
            try:
                resp = prn.open_file(f["name"], timeout=30)
            except PrinterBusy as e:              # the driver's own last-moment check: stop cleanly
                raise _Abort("Stopped: " + str(e))
            except PrinterError as e:
                raise ValueError(str(e))          # already worded for a human
            return _stream_to_temp(resp, tmp_dir)
        with self._ftps_lock(prn.name):
            self._check_still_idle(prn)           # N-12: re-checked AFTER taking the FTPS lock
            try:
                data = prn.sd_download(f["name"])
            except PrinterBusy as e:              # the driver's own check, one step later: stop cleanly
                raise _Abort("Stopped: " + str(e))
        return None, data, len(data), hashlib.sha256(data).hexdigest()

    def _run(self, job, prn):
        with self._lock:
            job.status, job.started_at = "running", _iso()
        print(f"[backup] {prn.name}: starting", flush=True)
        # the printer may have started a job while this sat in the "all" queue
        try:
            self._check_still_idle(prn)
            listing, list_failed = self._list(prn)
        except _Abort as e:
            return self._finish(job, "failed", str(e))
        if not listing:
            return self._finish(job, "failed", "No files found to back up (the file listing was empty).")

        pdir = os.path.join(self.root, printer_dirname(prn.name))
        os.makedirs(pdir, exist_ok=True)
        for stale in os.listdir(pdir):          # leftovers from a crashed run
            if stale.endswith(".part") or stale.startswith(".dl-"):
                _rm(os.path.join(pdir, stale))
        total = sum(f["size"] for f in listing)
        with self._lock:
            job.files_total, job.bytes_total = len(listing), total
        free = shutil.disk_usage(pdir).free
        if free < total * 1.05 + 1_000_000:
            return self._finish(job, "failed",
                                f"Not enough free space for {prn.name}'s files: need about "
                                f"{_mb(total)} MB, {_mb(free)} MB free.")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = printer_dirname(prn.name)
        while os.path.exists(os.path.join(pdir, f"{base}-{stamp}.zip")):
            time.sleep(1)
            stamp = time.strftime("%Y%m%d-%H%M%S")
        final = os.path.join(pdir, f"{base}-{stamp}.zip")
        part = final + ".part"

        manifest_files, failed, used = [], list(list_failed), set()
        if failed:
            with self._lock:
                job.files_failed, job.failures = len(failed), list(failed)
        try:
            with zipfile.ZipFile(part, "w", allowZip64=True) as zf:
                for i, f in enumerate(listing):
                    if i:
                        time.sleep(self._pause)   # let a waiting user request grab the FTPS lock
                    self._check_still_idle(prn)
                    with self._lock:
                        job.current = f["name"]
                    arc = _arcname(f["name"])
                    try:
                        if arc is None:
                            raise ValueError("unsafe path in file name")
                        if arc in used or arc == "manifest.json":
                            raise ValueError("duplicate path in listing")
                        path, data, size, sha = self._fetch(prn, f, pdir)
                        try:
                            if f["size"] and size != f["size"]:
                                raise ValueError(f"size mismatch (got {size} bytes, listing says {f['size']})")
                            ctype = (zipfile.ZIP_STORED if arc.lower().endswith(_STORED_EXT)
                                     else zipfile.ZIP_DEFLATED)
                            if path:
                                zf.write(path, arc, compress_type=ctype, compresslevel=1)
                            else:
                                zi = zipfile.ZipInfo(arc, time.localtime()[:6])
                                zi.compress_type = ctype
                                zi.external_attr = 0o644 << 16
                                zf.writestr(zi, data, compresslevel=1)
                        finally:
                            if path:
                                _rm(path)
                        used.add(arc)
                        manifest_files.append({"name": f["name"], "size": size,
                                               "modified": f["modified"], "sha256": sha})
                        with self._lock:
                            job.bytes_done += size
                    except _Abort:
                        raise
                    except Exception as e:
                        failed.append({"name": f["name"], "error": _err(e)})
                        with self._lock:
                            job.files_failed += 1
                            job.failures = list(failed)
                    finally:
                        with self._lock:
                            job.files_done += 1
                if not manifest_files:
                    raise _Abort("Every file failed to download — no backup written. "
                                 f"First error: {failed[0]['error'] if failed else 'unknown'}")
                manifest = {
                    "format": 1, "generator": "fabrica backup",
                    "printer": prn.name, "model": getattr(prn, "model", "") or "",
                    "kind": getattr(prn, "kind", ""), "created": _iso(),
                    "complete": not failed,
                    "totals": {"listed": len(listing), "files": len(manifest_files),
                               "failed": len(failed),
                               "bytes": sum(m["size"] for m in manifest_files)},
                    "files": manifest_files, "failed": failed,
                }
                zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            _fsync_file(part)
            _verify_zip(part, len(manifest_files))
            os.replace(part, final)        # appears in listings only now
            _fsync_dir(pdir)
        except _Abort as e:
            _rm(part)
            return self._finish(job, "failed", str(e))
        except Exception as e:
            _rm(part)
            return self._finish(job, "failed", f"Could not write backup: {_err(e)}")

        with self._lock:
            job.file, job.size = os.path.basename(final), os.path.getsize(final)
        self._prune(pdir, base)
        if failed:
            self._finish(job, "partial",
                         f"{len(failed)} item(s) could not be backed up "
                         "(listed in manifest.json and failed_files).")
        else:
            self._finish(job, "done")

    def _prune(self, pdir, base):
        """Keep the newest keep_count() COMPLETE zips and at most KEEP_PARTIAL partial ones. The two
        pools are independent, so a run of partial backups can never evict a good one, and the
        newest complete backup is always kept (keep_count() is at least 1)."""
        zips = self._zips(pdir, base)
        complete = [z for z in zips if z["complete"]]
        partial = [z for z in zips if not z["complete"]]
        for old in complete[self.keep_count():] + partial[KEEP_PARTIAL:]:
            _rm(old["path"])
            print(f"[backup] {base}: pruned {old['file']}", flush=True)


# ------------------------------------------------------------------ helpers

def _files_for_backup(prn) -> list:
    """files() of an open_file() driver. Skips per-file metadata lookups when the driver
    supports that (Moonraker's with_meta=False) — a backup only needs name/size/modified."""
    try:
        takes_meta = "with_meta" in inspect.signature(prn.files).parameters
    except (TypeError, ValueError):
        takes_meta = False
    return prn.files(with_meta=False) if takes_meta else prn.files()


class StreamTooLarge(Exception):
    """The body being streamed passed the caller's byte cap (the temp file is already gone)."""


def _stream_to_temp(resp, tmp_dir, max_bytes=None):
    """Stream an HTTP response (context manager with .read(n)) into a temp file in
    tmp_dir, hashing as it goes. Returns (tmp_path, None, size, sha256); the caller
    deletes the temp file. The temp file is removed here on any error. With `max_bytes`
    the read stops as soon as the body passes the cap (StreamTooLarge, temp file deleted),
    so a lying listing or an endless body can never fill the disk."""
    tmp = os.path.join(tmp_dir, f".dl-{os.getpid()}-{threading.get_ident()}.tmp")
    h, n = hashlib.sha256(), 0
    try:
        with resp as r, open(tmp, "wb") as out:
            while True:
                chunk = r.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                n += len(chunk)
                if max_bytes is not None and n > max_bytes:
                    raise StreamTooLarge(f"the file is larger than the {int(max_bytes) // 1048576} MB limit")
    except Exception:
        _rm(tmp)
        raise
    return tmp, None, n, h.hexdigest()


def _epoch(created, fallback) -> float:
    """Seconds since the epoch from a manifest's timezone-aware ISO `created`; the file's mtime if absent."""
    try:
        dt = datetime.datetime.fromisoformat(created)
        if dt.tzinfo is not None:
            return dt.timestamp()
    except (TypeError, ValueError):
        pass
    return fallback


def _fsync_file(path):
    with open(path, "rb") as f:
        os.fsync(f.fileno())


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _verify_zip(path, n_files):
    """Cheap sanity check before a new zip replaces older ones: it opens, the central directory lists
    every file plus the manifest, and the manifest parses. (Not a full CRC pass - that reads it all.)"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "manifest.json" not in names or len(names) != n_files + 1:
            raise ValueError(f"the new archive is incomplete ({len(names)} entries, expected {n_files + 1})")
        json.loads(z.read("manifest.json"))


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _mb(n) -> int:
    return int(n / 1048576)


def _err(e) -> str:
    msg = str(e) or type(e).__name__
    return msg if isinstance(e, ValueError) else f"{type(e).__name__}: {msg}"
