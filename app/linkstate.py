"""Hold the last known printer state across a short network blip (display only).

A printer that misses one or two polls is almost always a Wi-Fi hiccup or a busy Pi, not a printer that went
away. The dashboard and the fleet API therefore show the LAST GOOD reading with `reconnecting: true` until the
printer has been unreachable for GRACE_S seconds AND has failed at least MIN_MISSES polls; only then does the
raw "no answer" reading go through (OFFLINE). Time is the main rule because the poll interval differs per
screen (5 s dashboard, slower for agents); the miss count stops a single poll after a long quiet spell from
counting as "30 s of outage".

Scope, deliberately narrow:
  * Display only. Drivers' snapshot() stays raw and honest: the safety paths (pre-heat auto-off, controls,
    the notification watcher with its own 90 s offline grace) never see a held reading.
  * Only "unreachable" (connected: false) is held. A printer that ANSWERS and says it is disconnected
    (OctoPrint serial closed, Klipper shutdown/error) goes through at once: that is the printer talking.
  * Nothing to hold (the Hub just started and the printer never answered) -> the raw reading goes through.
"""
import threading
import time

GRACE_S = 30.0
MIN_MISSES = 2


class LinkHold:
    def __init__(self, grace_s=GRACE_S, min_misses=MIN_MISSES, clock=time.monotonic):
        self.grace_s, self.min_misses, self._clock = grace_s, min_misses, clock
        self._lock = threading.Lock()
        self._st = {}                      # printer name (lower) -> {"good", "good_at", "first_miss", "misses"}

    def forget(self, name):
        with self._lock:
            self._st.pop(str(name).lower(), None)

    def apply(self, name, snap):
        """-> the snapshot to SHOW. Adds `reconnecting` (bool) to dict snapshots; may return a held copy."""
        if not isinstance(snap, dict):
            return snap
        key, now = str(name).lower(), self._clock()
        with self._lock:
            st = self._st.setdefault(key, {"good": None, "good_at": None, "first_miss": None, "misses": 0})
            if snap.get("connected"):
                st.update(good=dict(snap), good_at=now, first_miss=None, misses=0)
                return {**snap, "reconnecting": False}
            st["misses"] += 1
            if st["first_miss"] is None:
                st["first_miss"] = now
            down = now - st["first_miss"]
            good = st["good"]
            if good is not None and (down < self.grace_s or st["misses"] < self.min_misses):
                age = (good.get("age_seconds") or 0) + (now - st["good_at"])
                return {**good, "connected": True, "reconnecting": True,
                        "unreachable_seconds": round(down), "age_seconds": round(age)}
            return {**snap, "reconnecting": False, "unreachable_seconds": round(down)}


LINKS = LinkHold()
