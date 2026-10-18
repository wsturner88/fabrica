"""Turn periodic fleet snapshots into human events ("P1S: print finished").

EventWatcher never talks to a printer. The Hub's poll loop hands it the snapshots it
already has - {printer name: snapshot dict} as returned by driver.snapshot() - and the
watcher compares them with what it saw last time:

    started     idle/finished/failed -> RUNNING
    first_layer_done   once per run: layer >= 2, or Z risen and held (and a timer for printers with neither)
    finished    an active job -> FINISH (or back to IDLE having reached ~100 %)
    failed      an active job -> FAILED, or cancelled (back to IDLE well short of done)
    paused      RUNNING/PREPARE -> PAUSE, with the printer's error text when it gives one
    offline     unreachable for longer than a grace period (debounced)
    online      reachable again, but only if we said it was offline

plus hooks other modules call: backup_done / backup_failed, spaghetti_suspected,
preheat_auto_off. Each event goes to `sink(event)` - normally Notifier.submit.

Guard rails:
  * the first snapshot of a printer is a baseline: a Hub restart in the middle of a
    print (or with a printer switched off) produces no events;
  * a state change must be seen on consecutive snapshots for `confirm_s` seconds before it
    counts, so a flapping state (RUNNING/PAUSE/RUNNING) is not reported;
  * the offline notice waits `offline_grace_s`; "back online" is sent only if the offline
    notice was, and only after the printer has stayed up for `confirm_s`;
  * transitions are evaluated against the last state seen while the printer was reachable,
    so a print that finishes while the Wi-Fi is down is still reported once it returns.

Messages are plain words (FAILED, PAUSED, OFFLINE) - never colour or emoji alone.

The "finished" message also says when it finished, how long it took against the slicer's prediction,
what material it used and (if the Hub address is set) links back to the Hub. Where the prediction
comes from, per printer type:
  * file facts - `job_facts(printer, job)` (the Hub's job-log resolver, caches only): Bambu 3mf
    `prediction`, and the gcode's estimated print time that OctoPrint / PrusaLink / Moonraker report
    in their file listings;
  * otherwise the printer's own remaining-time reading in the first moments of the print
    (elapsed + remaining), taken once while the print is still under 10 %.
The run (start time, prediction, pauses, materials) is saved to `print_runs.json` in the data
directory, so a Hub restart in the middle of a print still reports the right numbers.
"""

import datetime
import html as _html
import json
import os
import re
import threading
import time
import urllib.parse

import settings_store as ss

ACTIVE = ("PREPARE", "RUNNING", "PAUSE")
OFFLINE_STATES = ("UNKNOWN", "DISCONNECTED")


class Event:
    """One thing worth telling a human. `image` is raw JPEG bytes if already in hand;
    `image_fn` fetches one lazily (the Notifier only calls it when it will actually send)."""

    def __init__(self, kind, printer, title, body, level=None, ts=None, data=None,
                 image=None, image_fn=None, dedupe_key="", html=None):
        self.kind = kind
        self.printer = printer or ""
        self.title = title
        self.body = body
        self.level = level or ss.event_level(kind)
        self.ts = ts if ts is not None else time.time()
        self.data = data or {}
        self.image = image
        self.image_fn = image_fn
        self.dedupe_key = dedupe_key
        self.html = html                  # optional Telegram-flavoured twin of title+body (channels that cannot show it use title/body)
        self._image_done = image is not None

    def get_image(self):
        if not self._image_done:
            self._image_done = True
            if self.image_fn is not None:
                try:
                    self.image = self.image_fn()
                except Exception:
                    self.image = None
        return self.image

    def to_dict(self) -> dict:
        return {"kind": self.kind, "printer": self.printer, "title": self.title, "body": self.body,
                "level": self.level, "ts": self.ts, "data": self.data}

    def __repr__(self):
        return f"Event({self.kind!r}, {self.printer!r}, {self.title!r})"


# ---------------------------------------------------------------- text helpers

def fmt_duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 60:
        return "under 1 min"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if m else f"{h} h"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_hm(seconds) -> str:
    """Compact duration for the "finished" message: '2h 41m', '3h', '41m', '<1m'."""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if s < 60:
        return "<1m"
    h, m = divmod(s // 60, 60)
    return (f"{h}h {m}m" if m else f"{h}h") if h else f"{m}m"


_JOB_EXT = re.compile(r"\.(gcode\.3mf|gcode|bgcode|3mf|ufp)$", re.I)


def friendly_job(job) -> str:
    """A job name as a person would say it: no .gcode / .3mf tail; '' for the 'no name' placeholder."""
    j = " ".join(str(job or "").split())
    if j.lower() in ("", "the current job"):
        return ""
    return _JOB_EXT.sub("", j) or j


def time_verdict(took_s, predicted_s) -> str:
    """'on time' | 'N min early' | 'N min late' (N becomes '1h 05m' past an hour). On time = within 2 min or 2 %."""
    delta = took_s - predicted_s
    if abs(delta) <= max(120.0, 0.02 * predicted_s):
        return "on time"
    mins = max(1, int(round(abs(delta) / 60.0)))
    amount = f"{mins} min" if mins < 60 else fmt_hm(mins * 60)
    return f"{amount} {'early' if delta < 0 else 'late'}"


def material_summary(filaments) -> list:
    """[(type, grams|None)] with the same material merged (two PLA spools = one PLA line), unused ones dropped."""
    out, order = {}, []
    for f in filaments or []:
        if not isinstance(f, dict):
            continue
        g = _num(f.get("used_g"))
        if g is not None and g <= 0:
            continue
        t = str(f.get("type") or "").strip().upper()
        if not t and g is None:
            continue
        t = t or "filament"
        if t not in out:
            out[t] = None
            order.append(t)
        if g is not None:
            out[t] = (out[t] or 0.0) + g
    return [(t, out[t]) for t in order]


def material_text(filaments) -> str:
    parts = []
    for t, g in material_summary(filaments):
        parts.append(f"{t} {max(1, int(round(g)))} g" if g is not None else t)
    return " + ".join(parts)


class _P:
    """What we remember about one printer."""

    def __init__(self):
        self.state = None            # last committed state seen while reachable (None = unknown)
        self.pending = None
        self.pending_since = 0.0
        self.in_job = False
        self.announced = False       # "started" already reported for this job
        self.run_started = None
        self.job = ""
        self.percent = None
        self.remaining = None
        self.offline_since = None
        self.offline_notified = False
        self.baseline_offline = False
        self.run_started_wall = None  # wall-clock start of the current job (only used to tell two prints of one file apart)
        self.last_recovered = None   # monotonic time of the last "back online" notice (flap hysteresis)
        self.up_since = None         # first reachable snapshot after an outage
        self.last_state_before_outage = None
        self.run_reset()
        self.fl_reset()

    def run_reset(self):
        """Per-print-run facts for the "finished" message (also saved to disk, see EventWatcher._save_run)."""
        self.pred_s = None           # the slicer's predicted total (seconds), once known
        self.pred_src = ""           # "file" (job facts) | "remaining" (printer's own estimate at the start)
        self.paused_s = 0.0          # seconds spent paused this run
        self.pause_since = None      # monotonic time the current pause began
        self.mats = []               # [{"type", "used_g"}] from the job facts
        self.facts_tries = 0
        self.facts_m = None
        self.saved_sig = None

    def fl_reset(self):
        """Per-print-run state of the "first layer done" detector."""
        self.fl_done = False         # already fired (or deliberately skipped) for this run
        self.fl_seen_low = False     # a layer number <= 1 was seen in this run (guards a stale layer from the last print)
        self.fl_base = None          # lowest Z (mm) seen this run = the first-layer height
        self.fl_since = None         # monotonic time Z first sat in the "second layer" window
        self.fl_polls = 0            # consecutive updates inside that window
        self.fl_run_s = 0.0          # seconds spent RUNNING this run (fallback timer)
        self.fl_last_m = None


class EventWatcher:
    def __init__(self, sink, settings=None, snapshot_fetcher=None, clock=time.time,
                 offline_grace_s=90, confirm_s=5, mono=None, baseline_offline_grace_s=900,
                 flap_hold_s=600, first_layer_hold_s=20, job_facts=None, state_path=None):
        self.sink = sink
        self.job_facts = job_facts               # callable(printer, job) -> {"seconds", "filaments": [{"type","used_g"}]} (caches only)
        if state_path is None and settings is not None and getattr(settings, "dir", None):
            state_path = os.path.join(settings.dir, "print_runs.json")
        self.state_path = state_path             # where the running prints are remembered across a Hub restart
        self._runs = None
        self.settings = settings
        self.fetch = snapshot_fetcher            # callable(printer_name) -> JPEG bytes | None
        self.clock = clock
        # Elapsed time (print duration, "offline for", debounce) uses a monotonic clock so a wall-clock
        # step (a Pi without an RTC gets its time from NTP after boot) cannot make a 2 h print "72 h".
        # With the real clock that is time.monotonic; an injected clock (tests) is its own timebase.
        self._mono_override = mono
        self._m = 0.0                            # monotonic "now" of the update in progress
        self._offline_grace = offline_grace_s
        self._baseline_grace = baseline_offline_grace_s   # a printer already off when the Hub started
        self._flap_hold = flap_hold_s            # after "back online", a new "offline" must last 3x longer
        self.confirm_s = confirm_s
        self.first_layer_hold_s = first_layer_hold_s
        self._p = {}
        self._lock = threading.RLock()

    def _mono(self):
        """Monotonic seconds, looked up at call time (`.clock` may be swapped after construction)."""
        if self._mono_override is not None:
            return self._mono_override()
        return time.monotonic() if self.clock is time.time else self.clock()

    # ---- config -----------------------------------------------------------

    def _grace(self) -> float:
        if self.settings is not None:
            try:
                return float(self.settings.get("notifications.offline_grace_s"))
            except Exception:
                pass
        return self._offline_grace

    def _unit(self) -> str:
        if self.settings is not None:
            try:
                return self.settings.get("general.temp_unit")
            except Exception:
                pass
        return "C"

    def _t(self, c) -> str:
        return ss.format_temp(c, self._unit())

    def _setting(self, key, default=""):
        if self.settings is not None:
            try:
                v = self.settings.get(key)
                if v is not None:
                    return v
            except Exception:
                pass
        return default

    # ---- the running print, remembered across a Hub restart ---------------

    def _load_runs(self) -> dict:
        if self._runs is None:
            self._runs = {}
            if self.state_path:
                try:
                    with open(self.state_path, encoding="utf-8") as fh:
                        d = json.load(fh)
                    if isinstance(d, dict):
                        self._runs = {k: v for k, v in d.items() if isinstance(v, dict)}
                except (OSError, ValueError):
                    pass
        return self._runs

    def _write_runs(self):
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._runs, fh)
            os.replace(tmp, self.state_path)
        except OSError as e:
            print(f"[events] could not save {self.state_path}: {e}", flush=True)

    @staticmethod
    def _run_record(p) -> dict:
        return {"job": p.job, "start_wall": p.run_started_wall, "predicted_s": p.pred_s, "pred_src": p.pred_src,
                "paused_s": round(p.paused_s, 1), "mats": p.mats}

    def _save_run(self, name, p):
        """Remember this printer's current run (only when something in it changed)."""
        if p.run_started_wall is None:
            return
        rec = self._run_record(p)
        sig = json.dumps(rec, sort_keys=True)
        if sig == p.saved_sig:
            return
        p.saved_sig = sig
        self._load_runs()[name] = rec
        self._write_runs()

    def _clear_run(self, name):
        if name in self._load_runs():
            del self._runs[name]
            self._write_runs()

    def _restore_run(self, name, p, snap, now):
        """First sight of a printer mid-print (Hub restarted): pick up the run we saved before, if it is
        the same job and plausibly the same run. Anything else is dropped."""
        rec = self._load_runs().get(name)
        if not rec:
            return
        start = _num(rec.get("start_wall"))
        age = None if start is None else now - start
        pred = _num(rec.get("predicted_s"))
        jn, jr = friendly_job(snap.get("job")).lower(), friendly_job(rec.get("job")).lower()
        if (not p.in_job or age is None or age < 0 or age > 7 * 86400 or (jn and jr and jn != jr)
                or (pred and age > pred * 3 + 3600)):
            self._clear_run(name)
            return
        p.run_started_wall, p.run_started = start, self._m - age
        p.pred_s, p.pred_src = (pred if pred and pred > 0 else None), str(rec.get("pred_src") or "")
        p.paused_s = _num(rec.get("paused_s")) or 0.0
        p.mats = [m for m in rec.get("mats") or [] if isinstance(m, dict)]
        p.job = p.job or str(rec.get("job") or "")
        p.saved_sig = json.dumps(self._run_record(p), sort_keys=True)

    # ---- facts about the run: the slicer's prediction, materials, pauses --

    FACTS_RETRY_S = 60               # the file facts may arrive after the print starts (cache fills): look again this often
    FACTS_MAX_TRIES = 20
    REMAINING_MAX_PERCENT = 10       # the printer's own estimate only counts as the "prediction" this early in the print

    def _try_facts(self, name, p):
        p.facts_m, p.facts_tries = self._m, p.facts_tries + 1
        if self.job_facts is None or not p.job:
            return
        try:
            res = self.job_facts(name, p.job)
        except Exception:
            return
        if not isinstance(res, dict):
            return
        secs = _num(res.get("seconds"))
        if secs and secs > 0:
            p.pred_s, p.pred_src = secs, "file"
        mats = [{"type": f.get("type"), "used_g": _num(f.get("used_g"))}
                for f in res.get("filaments") or [] if isinstance(f, dict)]
        if not mats and _num(res.get("grams")):
            mats = [{"type": res.get("material"), "used_g": _num(res.get("grams"))}]
        if mats and not p.mats:              # what was read at the start stays (the Hub's AMS note expires within minutes)
            p.mats = mats

    def _track_run(self, name, p, snap):
        """Per poll, while a print is under way: add up pause time, keep looking for the file facts, and
        take the printer's own remaining-time estimate as the prediction if nothing better turns up."""
        if not p.in_job:
            return
        if p.state == "PAUSE":
            if p.pause_since is None:
                p.pause_since = self._m
        elif p.pause_since is not None:
            p.paused_s += max(0.0, self._m - p.pause_since)
            p.pause_since = None
        if ((p.pred_s is None or p.pred_src != "file" or not p.mats) and p.facts_tries < self.FACTS_MAX_TRIES
                and (p.facts_m is None or self._m - p.facts_m >= self.FACTS_RETRY_S)):
            self._try_facts(name, p)
        if p.pred_s is None and p.state == "RUNNING" and self._norm(snap) == "RUNNING" and p.run_started is not None:
            rm, pct = _num(snap.get("remaining_min")), _num(snap.get("percent"))
            if rm and rm > 0 and (pct is None or pct <= self.REMAINING_MAX_PERCENT):
                p.pred_s = max(0.0, self._m - p.run_started - p.paused_s) + rm * 60.0
                p.pred_src = "remaining"
        self._save_run(name, p)

    # ---- emitting ---------------------------------------------------------

    def _emit(self, ev: Event, events: list = None) -> Event:
        if events is not None:
            events.append(ev)
        try:
            self.sink(ev)
        except Exception as e:           # a broken sink must never stop the poll loop
            print(f"[events] sink failed for {ev.kind}: {e}", flush=True)
        return ev

    def _img(self, name):
        if self.fetch is None:
            return None
        return lambda: self.fetch(name)

    # ---- the main entry point ---------------------------------------------

    def update(self, fleet: dict, now: float = None) -> list:
        """Feed one round of snapshots ({name: snapshot}). Returns the events emitted."""
        explicit = now is not None
        now = self.clock() if now is None else now
        events = []
        with self._lock:
            self._m = now if explicit else self._mono()
            for gone in [n for n in self._p if n not in fleet]:
                del self._p[gone]                  # removed printer: baseline again if re-added
            for name, snap in (fleet or {}).items():
                if not isinstance(snap, dict):
                    continue
                try:
                    self._one(name, snap, now, events)
                except Exception as e:
                    print(f"[events] {name}: {type(e).__name__}: {e}", flush=True)
        return events

    @staticmethod
    def _online(snap) -> bool:
        return bool(snap.get("connected")) and str(snap.get("state") or "UNKNOWN").upper() not in OFFLINE_STATES

    @staticmethod
    def _norm(snap) -> str:
        s = str(snap.get("state") or "UNKNOWN").upper()
        return "PAUSE" if s == "PAUSED" else s

    def _one(self, name, snap, now, events):
        p = self._p.get(name)
        online = self._online(snap)
        if p is None:                                   # first sight: baseline, no events
            p = self._p[name] = _P()
            if online:
                p.state = self._norm(snap)
                p.in_job = p.state in ACTIVE
                p.announced = p.in_job
                self._fl_baseline(p, snap)
                self._remember(p, snap)
                self._restore_run(name, p, snap, now)
            else:
                p.offline_since, p.baseline_offline = self._m, True
            return
        if not online:
            p.up_since = None
            if p.offline_since is None:
                p.offline_since = self._m
                p.last_state_before_outage = p.state
            if not p.offline_notified and self._m - p.offline_since >= self._offline_after(p):
                p.offline_notified = True
                self._emit(self._offline_event(name, p, snap, now), events)
            return
        if p.offline_since is not None:                 # reachable again after an outage
            if p.up_since is None:
                p.up_since = self._m
            if self._m - p.up_since < self.confirm_s:
                return
            if p.offline_notified:
                self._emit(self._online_event(name, p, snap, now), events)
            was_baseline = p.baseline_offline or p.state is None
            if not was_baseline:
                p.last_recovered = self._m              # any real recovery counts for the flap hold, noticed or not
            p.offline_since, p.offline_notified, p.baseline_offline, p.up_since = None, False, False, None
            if was_baseline:                            # never saw it up: adopt silently
                p.state = self._norm(snap)
                p.in_job = p.state in ACTIVE
                p.announced = p.in_job
                self._fl_baseline(p, snap)
                self._remember(p, snap)
                self._restore_run(name, p, snap, now)
                return
        self._transition(name, p, snap, now, events)
        self._first_layer(name, p, snap, now, events)
        self._track_run(name, p, snap)

    def _offline_after(self, p) -> float:
        """Seconds a printer must stay unreachable before "offline" is announced. A printer that was
        already off when the Hub started gets a longer wait (and is announced once, not never); one that
        came back from an outage within the last `flap_hold_s` needs 3x the normal wait, so a flapping
        Wi-Fi link does not produce an offline/online pair every few minutes."""
        grace = self._grace()
        if p.baseline_offline:
            return max(grace, self._baseline_grace)
        if p.last_recovered is not None and self._m - p.last_recovered < self._flap_hold:
            return grace * 3
        return grace

    def _remember(self, p, snap):
        if self._norm(snap) in ACTIVE or self._norm(snap) in ("FINISH", "FAILED"):
            job = (snap.get("job") or "").strip()
            if job:
                p.job = job
            pc = _num(snap.get("percent"))
            if pc is not None:
                p.percent = pc
            rm = _num(snap.get("remaining_min"))
            p.remaining = rm if self._norm(snap) in ACTIVE else None

    def _transition(self, name, p, snap, now, events):
        new = self._norm(snap)
        self._remember(p, snap)
        if new == p.state:
            p.pending = None
            return
        if p.pending != new:
            p.pending, p.pending_since = new, self._m
        if self._m - p.pending_since < self.confirm_s:
            return
        old, p.state, p.pending = p.state, new, None

        if new in ACTIVE and old not in ACTIVE:         # a job begins
            p.in_job, p.announced, p.run_started, p.run_started_wall = True, False, self._m, now
            p.fl_reset()
            p.run_reset()
            p.job = (snap.get("job") or "").strip() or p.job
            self._try_facts(name, p)
            self._save_run(name, p)
        if new == "RUNNING" and p.in_job and not p.announced:
            p.announced = True
            self._emit(self._started_event(name, p, snap, now), events)
        elif new == "PAUSE" and old in ("RUNNING", "PREPARE"):
            self._emit(self._paused_event(name, p, snap, now), events)
        elif new == "FINISH" and p.in_job:
            p.in_job = False
            self._emit(self._finished_event(name, p, snap, now), events)
        elif new == "FAILED" and p.in_job:
            p.in_job = False
            self._emit(self._failed_event(name, p, snap, now, cancelled=not self._error_text(snap)), events)
        elif new == "IDLE" and p.in_job:                # ended without a FINISH/FAILED state
            p.in_job = False
            if (p.percent or 0) >= 98:
                self._emit(self._finished_event(name, p, snap, now), events)
            else:
                self._emit(self._failed_event(name, p, snap, now, cancelled=True), events)
        elif new in ("FINISH", "FAILED", "IDLE"):
            p.in_job = False
        if not p.in_job:
            self._clear_run(name)

    # ---- "first layer done" -----------------------------------------------

    FIRST_LAYER_HOLD_S = 20          # Z must stay up this long (and for 2 polls): a z-hop is over in seconds
    FIRST_LAYER_MIN_RISE = 0.1       # mm above the first-layer height
    FIRST_LAYER_MAX_RISE = 1.0       # ...but not a purge/park move far above it

    @staticmethod
    def _layer_now(snap):
        """(current layer, total) from the snapshot's "n/total" string; None when not reported."""
        m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+|\?)\s*", str(snap.get("layer") or ""))
        return int(m.group(1)) if m else None

    @staticmethod
    def method_for(snap) -> str:
        """How the first layer is detected for this printer: "layer" (it reports layer numbers), "z" (it
        reports the nozzle height) or "timer" (neither: a fixed time after the print starts)."""
        if EventWatcher._layer_now(snap) is not None:
            return "layer"
        if _num(snap.get("z_mm")) is not None:
            return "z"
        return "timer"

    def _fl_baseline(self, p, snap):
        """First sight of a printer mid-print: only a print still on layer 1 (reported) can still alert."""
        cur = self._layer_now(snap)
        if p.in_job and cur is not None and cur <= 1:
            p.fl_seen_low = True
        else:
            p.fl_done = True

    def _fl_fallback_min(self) -> float:
        if self.settings is not None:
            try:
                return float(self.settings.get("notifications.first_layer_fallback_min"))
            except Exception:
                pass
        return 8.0

    def _first_layer(self, name, p, snap, now, events):
        last, p.fl_last_m = p.fl_last_m, self._m
        if not p.in_job or p.fl_done or p.state != "RUNNING" or self._norm(snap) != "RUNNING":
            p.fl_since, p.fl_polls = None, 0          # paused / preparing: a rising Z there means nothing
            return
        if last is not None:
            p.fl_run_s += min(max(self._m - last, 0.0), 60.0)
        method = self.method_for(snap)
        fire, detail = False, {}
        cur = self._layer_now(snap)
        if cur is not None and cur <= 1:
            p.fl_seen_low = True
        if method == "layer":
            fire = p.fl_seen_low and cur >= 2
        elif method == "z":
            z = _num(snap.get("z_mm"))
            if z > 0.05 and (p.fl_base is None or z < p.fl_base):
                p.fl_base = z
            rise = z - p.fl_base if p.fl_base is not None else 0.0
            if self.FIRST_LAYER_MIN_RISE <= rise <= self.FIRST_LAYER_MAX_RISE:
                if p.fl_since is None:
                    p.fl_since = self._m
                p.fl_polls += 1
                fire = p.fl_polls >= 2 and self._m - p.fl_since >= self._fl_hold()
            else:
                p.fl_since, p.fl_polls = None, 0
            detail = {"first_layer_z": p.fl_base, "z": z}
        else:
            mins = self._fl_fallback_min()
            fire = mins > 0 and p.fl_run_s >= mins * 60
            detail = {"fallback_min": mins}
            if mins <= 0:
                return
        if fire:
            p.fl_done = True
            self._emit(self._first_layer_event(name, p, snap, now, method, detail), events)

    def _fl_hold(self) -> float:
        return self.first_layer_hold_s

    def _first_layer_event(self, name, p, snap, now, method, detail):
        job = self._job_label(p, snap)
        pct = self._pct(p, snap)
        how = {"layer": "layer count", "z": "nozzle height",
               "timer": f"~{detail.get('fallback_min', 0):g} min in - this printer doesn't report layers"}[method]
        lead = f"First layer of {job} is done" + (f" ({how})" if method == "timer" else "") + "."
        prog = f"Now at {pct}." if pct else ""
        body = self._join(lead, prog, self._left(p, snap), self._temps(snap),
                          "Check the picture: is it sticking?")
        return Event("first_layer_done", name, f"First layer done \u2014 {name}", body, ts=now,
                     data={"job": job, "method": method, **detail}, image_fn=self._img(name),
                     dedupe_key=self._run_key(p, job))

    # ---- message builders -------------------------------------------------

    @staticmethod
    def _error_text(snap) -> str:
        err = snap.get("error")
        if isinstance(err, str) and err.strip():
            return " ".join(err.split())[:200]
        code = snap.get("print_error")
        if isinstance(code, int) and not isinstance(code, bool) and code > 1:
            return f"printer error code 0x{code:08X}"
        if code:
            return "the printer reported an error"
        return ""

    @staticmethod
    def _job_label(p, snap) -> str:
        return (snap.get("job") or "").strip() or p.job or "the current job"

    @staticmethod
    def _pct(p, snap):
        v = _num(snap.get("percent"))
        v = p.percent if v is None else v
        return None if v is None else f"{v:.0f}%"

    def _temps(self, snap, targets=False) -> str:
        n = snap.get("nozzle_target_c" if targets else "nozzle_c")
        b = snap.get("bed_target_c" if targets else "bed_c")
        if _num(n) is None and _num(b) is None:
            return ""
        label = "Target" if targets else "Now"
        return f"{label}: nozzle {self._t(n)}, bed {self._t(b)}."

    def _left(self, p, snap) -> str:
        rm = _num(snap.get("remaining_min"))
        rm = p.remaining if rm is None else rm
        return f"About {fmt_duration(rm * 60)} left." if rm and rm > 0 else ""

    @staticmethod
    def _join(*parts) -> str:
        return " ".join(x for x in parts if x)

    def _started_event(self, name, p, snap, now):
        job = self._job_label(p, snap)
        body = self._join(f"Started printing {job}.", self._left(p, snap), self._temps(snap, targets=True))
        return Event("print_started", name, f"{name}: print STARTED", body, ts=now,
                     data={"job": job}, image_fn=self._img(name), dedupe_key=self._run_key(p, job))

    @staticmethod
    def _run_key(p, job) -> str:
        """Dedupe key for a print's start/end notices: the job AND when this run began, so the second
        batch of the same file within the duplicate window is not swallowed as a repeat."""
        return f"{job}@{int(p.run_started_wall)}" if p.run_started_wall else job

    def _clock_text(self, ts) -> str:
        """Time of day in the Hub's time zone and clock style ('3:42 PM' for the US format, else '15:42')."""
        dt = datetime.datetime.fromtimestamp(ts, ss.tzinfo_for(self._setting("general.timezone", "UTC")))
        if self._setting("general.datetime_format", "iso") == "us":
            return dt.strftime("%I:%M %p").lstrip("0")
        return dt.strftime("%H:%M")

    def _hub_link(self, printer=None) -> str:
        """Where "Open in Fabrica" goes: the dashboard at notifications.hub_url ('' = no link). With a printer name the
        link is <hub_url>/dashboard?printer=<name>, which opens straight to that printer's screen."""
        base = str(self._setting("notifications.hub_url", "") or "").strip().rstrip("/")
        if not re.match(r"^https?://\S+$", base, re.I):
            return ""
        link = base if base.lower().endswith("/dashboard") else base + "/dashboard"
        if printer:
            link += ("&" if "?" in link else "?") + "printer=" + urllib.parse.quote(str(printer), safe="")
        return link

    def _finished_event(self, name, p, snap, now):
        job = self._job_label(p, snap)
        nice = friendly_job(job)
        secs = (self._m - p.run_started) if p.run_started is not None else None
        if p.pause_since is not None:                       # finished while "paused": that time was a pause too
            p.paused_s += max(0.0, self._m - p.pause_since)
            p.pause_since = None
        if p.pred_s is None or not p.mats:                  # one last look at the file facts
            self._try_facts(name, p)
        pred = p.pred_s if p.pred_s and p.pred_s > 0 else None
        paused = p.paused_s >= 60

        head = f"{name} finished" + (f" \u00b7 {nice}" if nice else "")
        done = f"Done {self._clock_text(now)}"
        if secs is not None:
            done += f" \u00b7 took {fmt_hm(secs)}"
            if pred:
                done += (f" (slicer said {fmt_hm(pred)}; print was paused)" if paused
                         else f" (slicer said {fmt_hm(pred)}: {time_verdict(secs, pred)})")
        mats = material_text(p.mats)
        used = f"Used {mats}" if mats else ""
        bed = _num(snap.get("bed_c"))
        warm = bed is not None and bed > 45
        clear = (f"Bed still {self._t(bed)}: let it cool before clearing it." if warm
                 else "Clear the bed before the next print.")
        link = self._hub_link(name)

        body = "\n".join(x for x in (done, used, clear, f"Open in Fabrica: {link}" if link else "") if x)
        esc = _html.escape
        h_lines = [f"\u2705 {esc(head, quote=False)}", f"\U0001F550 {esc(done, quote=False)}"]
        if used:
            h_lines.append(f"\U0001F9F5 {esc(used, quote=False)}")
        h_lines.append(f"\U0001F6CF {esc(clear, quote=False)}")
        if link:
            h_lines.append(f'\U0001F517 <a href="{esc(link, quote=True)}">Open in Fabrica</a>')
        return Event("print_finished", name, head, body, ts=now,
                     data={"job": job, "seconds": secs, "predicted_seconds": pred,
                           "predicted_from": p.pred_src if pred else None, "paused_seconds": round(p.paused_s),
                           "finished_at": now, "materials": [{"type": t, "grams": g} for t, g in material_summary(p.mats)],
                           "link": link or None},
                     image_fn=self._img(name), dedupe_key=self._run_key(p, job), html="\n".join(h_lines))

    def _failed_event(self, name, p, snap, now, cancelled):
        job = self._job_label(p, snap)
        pct = self._pct(p, snap)
        err = self._error_text(snap)
        head = f"{job} was cancelled or stopped" if cancelled else f"{job} FAILED"
        at = f" at {pct}" if pct else ""
        body = self._join(f"{head}{at}.", f"Reason: {err}." if err else "", self._temps(snap))
        title = f"{name}: print CANCELLED" if cancelled else f"{name}: print FAILED"
        return Event("print_failed", name, title, body, ts=now, data={"job": job, "percent": p.percent,
                     "cancelled": cancelled, "error": err}, image_fn=self._img(name),
                     dedupe_key=self._run_key(p, job))

    def _paused_event(self, name, p, snap, now):
        job = self._job_label(p, snap)
        pct = self._pct(p, snap)
        err = self._error_text(snap)
        body = self._join(f"{job} is paused" + (f" at {pct}" if pct else "") + ".",
                          f"Reason: {err}." if err else "No error reported - it may have been paused by hand or by a filament runout.",
                          self._left(p, snap), self._temps(snap))
        return Event("print_paused", name, f"{name}: print PAUSED", body, ts=now,
                     data={"job": job, "percent": p.percent, "error": err}, image_fn=self._img(name),
                     dedupe_key=job)

    def _offline_event(self, name, p, snap, now):
        down = fmt_duration(self._m - p.offline_since)
        last = ""
        if p.last_state_before_outage:
            last = f"Last state: {p.last_state_before_outage}" + (f" at {p.percent:.0f}%" if p.percent is not None
                                                                  and p.last_state_before_outage in ACTIVE else "") + "."
        err = snap.get("error")
        err = f"Detail: {' '.join(err.split())[:160]}." if isinstance(err, str) and err.strip() else ""
        since_start = "It has not been reachable since the Hub started." if p.baseline_offline else ""
        body = self._join(f"Not reachable for {down}.", since_start, last, err, "Check power and Wi-Fi.")
        return Event("printer_offline", name, f"{name}: OFFLINE", body, ts=now,
                     data={"last_state": p.last_state_before_outage})

    def _online_event(self, name, p, snap, now):
        down = fmt_duration(self._m - p.offline_since)
        state = self._norm(snap)
        pct = f" ({p.percent:.0f}%)" if state in ACTIVE and p.percent is not None else ""
        body = self._join(f"Reachable again after {down}.", f"State: {state}{pct}.")
        return Event("printer_online", name, f"{name}: back ONLINE", body, ts=now, data={"state": state})

    # ---- hooks for other modules ------------------------------------------

    def backup_done(self, printer, files=None, size_bytes=None, name=None) -> Event:
        bits = []
        if files is not None:
            bits.append(f"{files} file{'s' if files != 1 else ''}")
        if size_bytes:
            bits.append(f"{size_bytes / 1048576:.1f} MB")
        body = "Backup finished" + (f": {', '.join(bits)}" if bits else "") + (f" ({name})" if name else "") + "."
        return self._emit(Event("backup_done", printer, f"{printer}: backup finished", body,
                                data={"files": files, "size_bytes": size_bytes, "name": name}))

    def backup_failed(self, printer, error="") -> Event:
        err = " ".join(str(error).split())[:200]
        return self._emit(Event("backup_failed", printer, f"{printer}: backup FAILED",
                                "The backup did not complete." + (f" Reason: {err}." if err else ""),
                                data={"error": err}))

    def spaghetti_suspected(self, printer, score, image=None, level="failure") -> Event:
        """The failure detector's alert. level = "failure" (the default: likely spaghetti) or "warning"
        (possible spaghetti, a weaker signal). Each level has its own dedupe key, so a WARNING followed
        by a FAILURE in the same print is never swallowed as a repeat."""
        warning = str(level).lower() == "warning"
        try:
            s = float(score)
            pct = f"{(s * 100 if s <= 1 else s):.0f}%"
        except (TypeError, ValueError):
            pct = "unknown"
        with self._lock:
            p = self._p.get(printer)
            job = p.job if p else ""
        what = "possible print problem (WARNING)" if warning else "possible print FAILURE"
        body = self._join(f"The failure detector is {pct} sure this print has gone wrong"
                          + (f" ({job})" if job else "") + ".",
                          "Check the camera." if warning else "Check the camera and stop the print if needed.")
        return self._emit(Event("spaghetti_suspected", printer, f"{printer}: {what}", body,
                                level="warning" if warning else None,
                                data={"score": score, "job": job, "level": "warning" if warning else "failure"},
                                image=image, image_fn=None if image else self._img(printer),
                                dedupe_key=f"{job}|{'warning' if warning else 'failure'}"))

    def detector_down(self, error="", failures=None) -> Event:
        """The failure detector has stopped answering: prints are NOT being watched. (Hub-level event, no
        printer. The Notifier only delivers it once "detector_down" is in settings_store.EVENT_KINDS.)"""
        err = " ".join(str(error).split())[:200]
        return self._emit(Event("detector_down", "", "Failure detector UNREACHABLE",
                                "Running prints are NOT being watched for spaghetti"
                                + (f" ({err})" if err else "") + ". Check the detector service.",
                                level="warning", data={"error": err, "failures": failures},
                                dedupe_key="detector"))

    def detector_up(self) -> Event:
        return self._emit(Event("detector_up", "", "Failure detector is back",
                                "The detector is answering again; running prints are being watched.",
                                data={}, dedupe_key="detector"))

    def preheat_auto_off_failed(self, printer, reason="") -> Event:
        """The auto-off gave up: the heaters may STILL BE ON. Failure-class (breaks through quiet hours)."""
        why = " ".join(str(reason).split())[:200]
        return self._emit(Event("preheat_auto_off_failed", printer, f"{printer}: heaters may still be ON",
                                "The pre-heat auto-off could not switch the heaters off"
                                + (f" ({why})" if why else "") + ". Go and check the printer.",
                                data={"reason": why}))

    def preheat_auto_off(self, printer, minutes=None) -> Event:
        body = (f"No print started within {minutes} min, so pre-heating was switched off."
                if minutes else "No print was started in time, so pre-heating was switched off.")
        return self._emit(Event("preheat_auto_off", printer, f"{printer}: pre-heat switched OFF", body,
                                data={"minutes": minutes}))
