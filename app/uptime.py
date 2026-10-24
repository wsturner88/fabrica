"""Printer uptime history: how much of the time was each printer reachable? (Feeds the Analytics page.)

Nothing else in the Hub keeps this: the notifier (events.py) debounces a printer going offline into one
message and forgets it, and linkstate.py only holds the last reading for a few seconds. So this module keeps a tiny log:

  * `<data dir>/uptime.jsonl`       one JSON line per CHANGE - never per poll:
        {"t": 1760000000.0, "k": "start", "last": 1759990000.0}     the Hub started (it was last alive at "last")
        {"t": 1760000045.0, "k": "p", "n": "Printer-A", "up": 1}    "Printer-A" became reachable (1) / unreachable (0) at t
        {"t": ..., "k": "p", "n": "Printer-A", "up": null}          the printer was removed from the Hub
  * `<data dir>/uptime_alive.json`  {"t": ...}: overwritten (not appended) at most every 10 minutes and once at exit, so the
        time the Hub was NOT running is left out of the percentage instead of counting as "online".

A change is written only after it has lasted `settle_s` (45 s) and is dated from when it began, so a Wi-Fi blip, or the
few seconds a printer needs to connect after the Hub starts, never reaches the log. The log is bounded: past
`max_bytes` (256 KB, about 3,500 lines) the oldest 40 % are dropped and one starting line per printer is written in
their place, so the file never grows without limit and the percentages stay right for what is kept.

"Reachable" is the notifier's definition: the driver says connected and the state is not UNKNOWN / DISCONNECTED.
Never raises into the Hub: every file problem is logged once and the log carries on in memory. Stdlib only.
"""

import json
import os
import threading
import time

OFFLINE_STATES = ("UNKNOWN", "DISCONNECTED")      # same as events.OFFLINE_STATES
FILE_NAME = "uptime.jsonl"
ALIVE_NAME = "uptime_alive.json"


def is_online(snap) -> bool:
    """Is this printer snapshot reachable? (events.EventWatcher._online, repeated so this module stands alone.)"""
    if not isinstance(snap, dict):
        return False
    return bool(snap.get("connected")) and str(snap.get("state") or "UNKNOWN").upper() not in OFFLINE_STATES


class UptimeLog:
    def __init__(self, data_dir, clock=time.time, settle_s=45.0, alive_every_s=600.0, max_bytes=256 * 1024):
        self.path = os.path.join(data_dir, FILE_NAME)
        self.alive_path = os.path.join(data_dir, ALIVE_NAME)
        self.clock = clock
        self.settle_s, self.alive_every_s, self.max_bytes = settle_s, alive_every_s, max_bytes
        self._lock = threading.RLock()
        self._started = False
        self._state = {}                 # printer lower -> {"name", "up" (committed True/False or None), "pending" (up, since) or None}
        self._alive_at = 0.0
        self._size = None
        self._warned = False
        self.writes = 0                  # lines appended by this process (tests, diagnostics)

    # ---- files -----------------------------------------------------------

    def _warn(self, what, e):
        if not self._warned:
            self._warned = True
            print(f"[uptime] {what}: {type(e).__name__}: {e} (uptime history is paused)", flush=True)

    def _read_alive(self):
        try:
            with open(self.alive_path, encoding="utf8") as fh:
                t = float(json.load(fh).get("t"))
            return t
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _write_alive(self, now):
        try:
            tmp = self.alive_path + ".tmp"
            with open(tmp, "w", encoding="utf8") as fh:
                json.dump({"t": round(now, 1)}, fh)
            os.replace(tmp, self.alive_path)
            self._alive_at = now
        except OSError as e:
            self._warn("could not write the heartbeat", e)

    def _append(self, recs):
        if not recs:
            return
        text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs)
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            if self._size is None:
                self._size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            with open(self.path, "a", encoding="utf8") as fh:
                fh.write(text)
            self._size += len(text.encode("utf8"))
            self.writes += len(recs)
        except OSError as e:
            self._warn("could not append to the log", e)
            return
        if self._size > self.max_bytes:
            self._compact()

    def _read(self):
        recs = []
        try:
            with open(self.path, encoding="utf8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue                 # a torn last line (power cut) is skipped, never fatal
                    if isinstance(r, dict) and isinstance(r.get("t"), (int, float)) and r.get("k") in ("start", "p"):
                        recs.append(r)
        except OSError:
            pass
        if any(a["t"] > b["t"] for a, b in zip(recs, recs[1:])):     # a clock that stepped back: keep time order
            recs.sort(key=lambda r: r["t"])
        return recs

    def _compact(self):
        """Drop the oldest 40 % of the lines, keeping what they said about each printer's state at the cut."""
        recs = self._read()
        if len(recs) < 10:
            return
        cut = recs[int(len(recs) * 0.4)]["t"]
        state, in_session = {}, False
        for r in recs:
            if r["t"] >= cut:
                break
            if r["k"] == "start":
                state, in_session = {}, True
            else:
                state[str(r.get("n"))] = r.get("up")
        keep = [r for r in recs if r["t"] >= cut]
        head = []
        if in_session:
            head.append({"t": cut, "k": "start", "last": None})
            head += [{"t": cut, "k": "p", "n": n, "up": up} for n, up in sorted(state.items()) if up is not None]
        if keep and keep[0]["k"] == "start":
            head = []                            # the kept part begins a session of its own: nothing to carry over
        text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in head + keep)
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
            self._size = len(text.encode("utf8"))
        except OSError as e:
            self._warn("could not trim the log", e)

    # ---- recording -------------------------------------------------------

    def _begin(self, now):
        self._started = True
        self._append([{"t": round(now, 1), "k": "start", "last": self._read_alive()}])
        self._write_alive(now)

    def observe(self, online: dict, now=None):
        """`online` = {printer name: reachable bool}, the whole fleet, once per Hub tick. Writes only when a printer's
        state has changed AND stayed changed for `settle_s`; costs a dict lookup per printer otherwise."""
        with self._lock:
            now = self.clock() if now is None else now
            if not self._started:
                self._begin(now)
            recs = []
            seen = set()
            for name, up in online.items():
                key = str(name).lower()
                seen.add(key)
                st = self._state.setdefault(key, {"name": str(name), "up": None, "pending": None})
                st["name"] = str(name)
                up = bool(up)
                if st["up"] is not None and up == st["up"]:
                    st["pending"] = None
                    continue
                if st["pending"] is None or st["pending"][0] != up:
                    st["pending"] = (up, now)
                if now - st["pending"][1] >= self.settle_s:
                    st["up"] = up
                    recs.append({"t": round(st["pending"][1], 1), "k": "p", "n": st["name"], "up": 1 if up else 0})
                    st["pending"] = None
            for key in [k for k in self._state if k not in seen]:      # a printer the Hub no longer has
                st = self._state.pop(key)
                if st["up"] is not None:
                    recs.append({"t": round(now, 1), "k": "p", "n": st["name"], "up": None})
            self._append(recs)
            if now - self._alive_at >= self.alive_every_s:
                self._write_alive(now)

    def observe_fleet(self, fleet: dict, now=None):
        """observe() for the Hub's {name: snapshot} dict."""
        self.observe({n: is_online(s) for n, s in (fleet or {}).items()}, now)

    def stop(self):
        """At exit: note that the Hub was alive until now."""
        with self._lock:
            if self._started:
                self._write_alive(self.clock())

    # ---- reading ---------------------------------------------------------

    def report(self, since, until, names=None) -> dict:
        """Reachable and observed seconds per printer inside [since, until] (since None = from the first record):
            {"first_t": when the log began or None, "printers": {lower name: {"name", "online_s", "observed_s", "first_t"}}}
        `names` = set of lower-case names to include (None = all). Time the Hub was not running is not observed."""
        with self._lock:
            recs = self._read()
            if not recs:
                return {"first_t": None, "printers": {}}
            now = self.clock() if until is None else until
            alive = self._read_alive()
            sessions, cur = [], None
            for r in recs:
                if r["k"] == "start":
                    if cur is not None:
                        last = r.get("last")
                        cur["end"] = min(max(float(last), cur["start"]), r["t"]) if isinstance(last, (int, float)) else max(cur["last_rec"], cur["start"])
                    cur = {"start": r["t"], "end": None, "last_rec": r["t"], "p": {}}
                    sessions.append(cur)
                elif cur is None:                          # records without a start (a hand-edited file): treat as a session from the first record
                    cur = {"start": r["t"], "end": None, "last_rec": r["t"], "p": {}}
                    sessions.append(cur)
                if r["k"] == "p":
                    key = str(r.get("n")).lower()
                    cur["p"].setdefault(key, {"name": str(r.get("n")), "recs": []})["recs"].append((r["t"], r.get("up")))
                    cur["last_rec"] = max(cur["last_rec"], r["t"])
            if cur is not None and cur["end"] is None:
                running = self._started
                cur["end"] = now if running else max(cur["last_rec"], alive or 0.0, cur["start"])
            lo = since if since is not None else recs[0]["t"]
            out = {}
            for s in sessions:
                for key, info in s["p"].items():
                    if names is not None and key not in names:
                        continue
                    o = out.setdefault(key, {"name": info["name"], "online_s": 0.0, "observed_s": 0.0, "first_t": None})
                    o["name"] = info["name"]
                    rs = info["recs"]
                    for i, (t, up) in enumerate(rs):
                        end = rs[i + 1][0] if i + 1 < len(rs) else s["end"]
                        if up is None:
                            continue
                        a, b = max(t, lo), min(end, now)
                        if o["first_t"] is None or t < o["first_t"]:
                            o["first_t"] = t
                        if b > a:
                            o["observed_s"] += b - a
                            if up:
                                o["online_s"] += b - a
            return {"first_t": recs[0]["t"], "printers": out}
