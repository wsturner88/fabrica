"""Spool memory: which spools did the human print this file with last time?

When a print starts, the Hub notes - per printer and file - which AMS spool (tray, material, colour) fed each filament
of the job. The next time the same file is proposed on the same printer, the proposal starts from those spools (as
long as they are still loaded, wherever they sit now). Nothing here ever starts, changes or sends anything: it is a
small JSON file in the data directory (spool_memory.json, owner-only, written atomically) that only shapes the
PROPOSAL; the human still confirms the plate and taps START.

File shape (version 1):
  {"version": 1, "files": {"<printer>|<file key>": {"last": ts, "uses": n,
       "history": [{"ts": ts, "count": n, "spools": [{"index": 0, "tray": 1, "type": "PETG", "color": "#00FF00",
                                                       "color_name": "GREEN"}, ...]}, ...]}}}
`history` is newest first, at most HISTORY_MAX entries (the same set of spools used again bumps `count` and moves to
the front, so a "most common" rule can be added later); only the newest is used today.
"""
import json
import os
import tempfile
import threading
import time

FILE_NAME = "spool_memory.json"
MAX_FILES = 500          # least recently used files are forgotten first
HISTORY_MAX = 5


def _clean_spool(s):
    """One remembered spool, or None when it is not usable."""
    if not isinstance(s, dict):
        return None
    idx, tray = s.get("index"), s.get("tray")
    if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
        return None
    if isinstance(tray, bool) or not isinstance(tray, int) or tray < 0:
        return None
    typ = str(s.get("type") or "")[:40]
    if not typ:
        return None
    return {"index": idx, "tray": tray, "type": typ, "color": str(s.get("color") or "")[:9],
            "color_name": str(s.get("color_name") or "")[:24]}


def _same_spools(a, b) -> bool:
    key = lambda lst: sorted((s["index"], s["tray"], s["type"].upper(), s["color_name"].upper()) for s in lst)
    return key(a) == key(b)


class SpoolMemory:
    def __init__(self, path, max_files=MAX_FILES, clock=time.time):
        self.path, self.max_files, self.clock = path, max_files, clock
        self._lock = threading.Lock()
        self._files = None            # loaded lazily

    # ---- storage ----------------------------------------------------------------
    def _load(self):
        if self._files is not None:
            return self._files
        files = {}
        try:
            with open(self.path, encoding="utf8") as fh:
                data = json.load(fh)
            raw = data.get("files") if isinstance(data, dict) else None
            for k, v in (raw.items() if isinstance(raw, dict) else []):
                hist = []
                for h in (v.get("history") if isinstance(v, dict) else None) or []:
                    sp = [_clean_spool(s) for s in (h.get("spools") if isinstance(h, dict) else None) or []]
                    if sp and all(sp):
                        hist.append({"ts": float(h.get("ts") or 0), "count": max(1, int(h.get("count") or 1)),
                                     "spools": sp})
                if hist and isinstance(k, str):
                    files[k] = {"last": float(v.get("last") or hist[0]["ts"]), "uses": max(1, int(v.get("uses") or 1)),
                                "history": hist[:HISTORY_MAX]}
        except (OSError, ValueError, TypeError, AttributeError):
            files = {}
        self._files = files
        return files

    def _save(self, files):
        """Atomic: temp file in the same folder, fsync, owner-only, rename. Never raises (a full disk or read-only
        mount must not turn a successful print start into an error)."""
        tmp = None
        try:
            d = os.path.dirname(self.path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=FILE_NAME + ".", suffix=".tmp", dir=d)
            with os.fdopen(fd, "w", encoding="utf8") as fh:
                json.dump({"version": 1, "files": files}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            tmp = None
        except Exception as e:
            print(f"[spool-memory] could not save: {type(e).__name__}: {e}", flush=True)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @staticmethod
    def key(printer, file_key) -> str:
        return f"{str(printer).lower()}|{file_key}"

    # ---- API --------------------------------------------------------------------
    def record(self, printer, file_key, spools) -> bool:
        """Remember the spools a print was started with. `spools`: [{index, tray, type, color, color_name}].
        -> True when something was stored."""
        clean = [_clean_spool(s) for s in (spools or [])]
        if not file_key or not clean or not all(clean):
            return False
        clean.sort(key=lambda s: s["index"])
        k, now = self.key(printer, file_key), self.clock()
        with self._lock:
            files = self._load()
            e = files.pop(k, None) or {"last": now, "uses": 0, "history": []}      # pop + re-add = most recent last
            hist = e["history"]
            same = next((h for h in hist if _same_spools(h["spools"], clean)), None)
            if same is not None:
                hist.remove(same)
                same["count"] += 1
                same["ts"], same["spools"] = now, clean
                hist.insert(0, same)
            else:
                hist.insert(0, {"ts": now, "count": 1, "spools": clean})
            e["history"] = hist[:HISTORY_MAX]
            e["last"], e["uses"] = now, e["uses"] + 1
            files[k] = e
            while len(files) > self.max_files:
                del files[next(iter(files))]                                     # dicts keep insertion order: oldest first
            self._save(files)
        return True

    def lookup(self, printer, file_key):
        """The most recent remembered choice for this printer + file, or None:
        {"ts", "count", "uses", "spools": [...]}"""
        if not file_key:
            return None
        with self._lock:
            e = self._load().get(self.key(printer, file_key))
            if not e or not e["history"]:
                return None
            h = e["history"][0]
            return {"ts": h["ts"], "count": h["count"], "uses": e["uses"], "spools": [dict(s) for s in h["spools"]]}

    def history(self, printer, file_key) -> list:
        with self._lock:
            e = self._load().get(self.key(printer, file_key))
            return [dict(h, spools=[dict(s) for s in h["spools"]]) for h in (e["history"] if e else [])]

    def clear(self):
        with self._lock:
            self._files = {}
            self._save({})

    def __len__(self):
        with self._lock:
            return len(self._load())
