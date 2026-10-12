"""Turn periodic fleet snapshots into human events ("P1S: print finished").

EventWatcher never talks to a printer. The Hub's poll loop hands it the snapshots it
already has - {printer name: snapshot dict} as returned by driver.snapshot() - and the
watcher compares them with what it saw last time:

    started     idle/finished/failed -> RUNNING
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
"""

import threading
import time

import settings_store as ss

ACTIVE = ("PREPARE", "RUNNING", "PAUSE")
OFFLINE_STATES = ("UNKNOWN", "DISCONNECTED")


class Event:
    """One thing worth telling a human. `image` is raw JPEG bytes if already in hand;
    `image_fn` fetches one lazily (the Notifier only calls it when it will actually send)."""

    def __init__(self, kind, printer, title, body, level=None, ts=None, data=None,
                 image=None, image_fn=None, dedupe_key=""):
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


class EventWatcher:
    def __init__(self, sink, settings=None, snapshot_fetcher=None, clock=time.time,
                 offline_grace_s=90, confirm_s=5, mono=None, baseline_offline_grace_s=900,
                 flap_hold_s=600):
        self.sink = sink
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
                self._remember(p, snap)
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
                self._remember(p, snap)
                return
        self._transition(name, p, snap, now, events)

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
            p.job = (snap.get("job") or "").strip() or p.job
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

    def _finished_event(self, name, p, snap, now):
        job = self._job_label(p, snap)
        secs = (self._m - p.run_started) if p.run_started is not None else None
        took = f"Took {fmt_duration(secs)}." if secs is not None else ""
        bed = _num(snap.get("bed_c"))
        warm = f"Bed is still {self._t(bed)} - let it cool before removing the part." if bed and bed > 45 else ""
        body = self._join(f"{job} finished.", took, warm, "Clear the bed before the next print.")
        return Event("print_finished", name, f"{name}: print FINISHED", body, ts=now,
                     data={"job": job, "seconds": secs},
                     image_fn=self._img(name), dedupe_key=self._run_key(p, job))

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
