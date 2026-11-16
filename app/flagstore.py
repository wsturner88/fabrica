"""Flagged pictures: what the spaghetti watch saw when it raised a warning, and what the owner said about it.

One record per warning EPISODE (the run of scored pictures at the warning level, up to EPISODE_PICTURES of them kept as
JPEGs) or per MISSED example (the owner cancelled a print because of spaghetti the detector had not flagged). Each record
is a small JSON next to its pictures in one folder (data/detection/flags/):

    <id>.json      printer, time, mode, paused yes/no, job, per-picture scores, label, ...
    <id>-1.jpg ... the pictures

Labels: "" (not looked at), "real" (a real failure: kept as an example), "false" (a false alarm), "missed" (the detector
did not catch it). The store keeps the newest ~MAX_PICTURES pictures / ~MAX_BYTES and deletes the oldest records beyond that.
Stdlib only; never raises into the caller's loop (storage problems are logged once and the record is simply not kept).
"""

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("bambu.detection")

LABELS = ("", "real", "false", "missed")
MAX_PICTURES = 300
MAX_BYTES = 300 * 1024 * 1024
KIND_FLAG, KIND_MISSED = "flag", "missed"

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_ID_OK = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _slug(printer: str) -> str:
    return (_SAFE.sub("_", str(printer or "printer")).lstrip(".")[:40] or "printer")


class FlagStore:
    def __init__(self, directory, max_pictures=MAX_PICTURES, max_bytes=MAX_BYTES, clock=time.time):
        self.dir = directory
        self.max_pictures = int(max_pictures)
        self.max_bytes = int(max_bytes)
        self._clock = clock
        self._lock = threading.RLock()
        self._warned = False
        self._seq = 0

    # ---- writing ---------------------------------------------------------------------

    def _new_id(self, printer, ts):
        self._seq += 1
        return time.strftime("%Y%m%d-%H%M%S", time.gmtime(ts)) + "-" + _slug(printer) + "-%d" % self._seq

    def _io(self, e, what):
        if not self._warned:
            self._warned = True
            log.warning("flagged pictures: could not %s: %s", what, e)

    def _write(self, rec):
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, rec["id"] + ".json.tmp")
        with open(tmp, "w") as f:
            json.dump(rec, f, separators=(",", ":"))
        os.replace(tmp, os.path.join(self.dir, rec["id"] + ".json"))

    def new_record(self, printer, kind=KIND_FLAG, mode="", job="", level="", paused=False, ts=None):
        """A new, empty record -> its dict (no JSON written until the first picture is added), or None."""
        ts = float(ts if ts is not None else self._clock())
        with self._lock:
            rid = self._new_id(printer, ts)
            return {"id": rid, "kind": kind, "printer": str(printer), "ts": round(ts, 1), "mode": mode, "job": str(job or ""),
                    "level": level, "paused": bool(paused), "pause_reason": "", "label": "missed" if kind == KIND_MISSED else "",
                    "labelled_at": None, "baseline": False, "pictures": [], "scores": []}

    def add_picture(self, rec, jpeg, score=None, ts=None) -> bool:
        """Append one picture to a record (kept in `rec`, written to disk). False = not stored (storage problem)."""
        if not rec or not jpeg:
            return False
        with self._lock:
            try:
                n = len(rec["pictures"]) + 1
                name = "%s-%d.jpg" % (rec["id"], n)
                os.makedirs(self.dir, exist_ok=True)
                tmp = os.path.join(self.dir, name + ".tmp")
                with open(tmp, "wb") as f:
                    f.write(jpeg)
                os.replace(tmp, os.path.join(self.dir, name))
                rec["pictures"].append({"file": name, "ts": round(float(ts if ts is not None else self._clock()), 1),
                                        "score": round(float(score), 3) if score is not None else None})
                if score is not None:
                    rec["scores"].append(round(float(score), 3))
                self._write(rec)
            except (OSError, ValueError, TypeError) as e:
                self._io(e, "save a picture")
                return False
            self.prune()
            return True

    def save(self, rec):
        """Write a changed record (paused, label ...)."""
        with self._lock:
            try:
                self._write(rec)
            except OSError as e:
                self._io(e, "save a record")

    # ---- reading ---------------------------------------------------------------------

    def _load_all(self):
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, n)) as f:
                    d = json.load(f)
                if isinstance(d, dict) and _ID_OK.match(str(d.get("id", ""))) and isinstance(d.get("pictures"), list):
                    out.append(d)
            except (OSError, ValueError):
                continue
        return out

    def list(self, printer=None, label=None, limit=200):
        """Newest first. `label` = "real" / "false" / "missed" / "none" (unlabelled) / None (all)."""
        with self._lock:
            recs = self._load_all()
        if printer:
            recs = [r for r in recs if str(r.get("printer", "")).lower() == str(printer).lower()]
        if label:
            want = "" if label == "none" else label
            recs = [r for r in recs if (r.get("label") or "") == want]
        recs.sort(key=lambda r: (r.get("ts") or 0, r.get("id")), reverse=True)
        return recs[:max(1, int(limit))]

    def counts(self):
        with self._lock:
            recs = self._load_all()
        c = {"all": len(recs), "real": 0, "false": 0, "missed": 0, "none": 0}
        for r in recs:
            c[(r.get("label") or "none")] = c.get(r.get("label") or "none", 0) + 1
        c["printers"] = sorted({str(r.get("printer", "")) for r in recs})
        return c

    def get(self, rid):
        if not _ID_OK.match(str(rid or "")):
            return None
        with self._lock:
            try:
                with open(os.path.join(self.dir, rid + ".json")) as f:
                    d = json.load(f)
                return d if isinstance(d, dict) else None
            except (OSError, ValueError):
                return None

    def image(self, rid, n=None):
        """JPEG bytes of picture n (1-based; None / 0 = the last one) of a record, or None."""
        rec = self.get(rid)
        if not rec or not rec.get("pictures"):
            return None
        pics = rec["pictures"]
        try:
            n = int(n) if n else len(pics)
        except (TypeError, ValueError):
            n = len(pics)
        if not 1 <= n <= len(pics):
            return None
        name = str(pics[n - 1].get("file", ""))
        if not _ID_OK.match(name):
            return None
        try:
            with open(os.path.join(self.dir, name), "rb") as f:
                return f.read()
        except OSError:
            return None

    def set_label(self, rid, label):
        """-> the updated record, or None when there is no such record. `label` "none" / "" clears it."""
        label = "" if label in ("none", None) else str(label)
        if label not in LABELS:
            raise ValueError("label must be real, false, missed or none")
        with self._lock:
            rec = self.get(rid)
            if rec is None:
                return None
            rec["label"] = label
            rec["labelled_at"] = round(self._clock(), 1) if label else None
            self._write(rec)
            return rec

    # ---- the cap ---------------------------------------------------------------------

    def prune(self):
        """Delete the oldest records until at most max_pictures pictures and max_bytes remain."""
        with self._lock:
            recs = self._load_all()
            sizes = {}
            total_pics = total_bytes = 0
            for r in recs:
                b = 0
                for p in r["pictures"]:
                    try:
                        b += os.path.getsize(os.path.join(self.dir, str(p.get("file", ""))))
                    except OSError:
                        pass
                sizes[r["id"]] = b
                total_pics += len(r["pictures"])
                total_bytes += b
            if total_pics <= self.max_pictures and total_bytes <= self.max_bytes:
                return 0
            recs.sort(key=lambda r: (r.get("ts") or 0, r.get("id")))          # oldest first
            gone = 0
            for r in recs[:-1]:                                                  # never the newest record
                if total_pics <= self.max_pictures and total_bytes <= self.max_bytes:
                    break
                self._delete(r)
                total_pics -= len(r["pictures"])
                total_bytes -= sizes.get(r["id"], 0)
                gone += 1
            return gone

    def _delete(self, rec):
        for p in rec.get("pictures", []):
            try:
                os.unlink(os.path.join(self.dir, str(p.get("file", ""))))
            except OSError:
                pass
        try:
            os.unlink(os.path.join(self.dir, rec["id"] + ".json"))
        except OSError:
            pass
