"""Printer controls shared by every driver: light, pre-heat, cool-down, unload / load filament.

The drivers (BambuPrinter, OctoPrintPrinter, PrusaLinkPrinter, MoonrakerPrinter) implement the
low-level primitives; this module owns everything that must behave the SAME for every family:

  * capability discovery   -> describe(prn) / summary(prn)       (what can this printer do right now?)
  * the safety gate        -> heaters / filament only while IDLE, FINISH or FAILED
  * temperature limits     -> nozzle 0-300 C, bed 0-120 C, anything else is rejected
  * material presets       -> PLA / PETG / ABS / TPU defaults, overridable from config
  * pre-heat auto-off      -> the Hub cools the printer down again after a timeout. It cannot be switched
                              off, it survives a restart (deadlines are persisted), and it only ever sends
                              "heaters off" to a printer that is CONNECTED and positively IDLE/FINISH/FAILED

Driver contract (all optional: a driver without them reports every control unsupported):

    control_caps() -> {"light":  {"supported": bool, "state": "on"|"off"|None, "reason"?, "note"?, "verified"?},
                       "heat":   {"supported": bool, ...},        # one entry covers pre-heat AND cool-down
                       "unload": {"supported": bool, ...},
                       "load":   {"supported": bool, ...}}
    set_light(on: bool)
    set_temps(nozzle: int|None, bed: int|None) -> dict|None      # None = leave that heater alone; 0 = off
    unload_filament()
    load_filament(tray: int|None = None)

Optional driver extras (all additive):
    snapshot()["start_pending"]   True from "a start command was accepted" until the print is seen (see
                                  mark_start_sent / with_start_pending); counts as busy for every control
    snapshot()["fault"]           truthy (a dict for Bambu) when the printer is in a hard fault
    set_temps() / set_light() may return {"confirmed": bool|None, "unconfirmed": "why"}: whether the
                                  printer's own reports showed the change. confirmed False = "sent but NOT
                                  confirmed": the auto-off retries it, the UI says so.

Controls sheet extras (all optional, all additive; a driver without them reports them unsupported):
    control_caps()["home"|"move"|"extrude"]     same shape as the other entries
    home_all()                                   G28 / Klipper homing
    move_relative(axis, mm)                      one relative jog, |mm| <= MOVE_MAX_MM (this module checks it first)
    extrude(mm)                                  signed, |mm| <= EXTRUDE_MAX_MM (checked here: hot enough first)
    unload_filament(nozzle=None) / load_filament(tray=None, nozzle=None)
                                                 `nozzle` is the temperature to heat to first (drivers that heat
                                                 by themselves, like the Bambu AMS, ignore it)
    FILAMENT_NOZZLE_TEMP = True                  class trait: load / unload need the Hub to say how hot (Klipper, OctoPrint)
    extrude_min_c()                              the printer's own minimum extrusion temperature, or None (default 170)
    snapshot()["homed"]                          True / False when the printer REPORTS its homed axes (Klipper); None =
                                                 unknown, then only a Home the Hub itself sent (and not too long ago) counts

`verified: False` on a capability means "written from the printer family's documentation, never
exercised on real hardware" (see docs/drivers.md).

This module imports nothing from the drivers, so drivers can import it.
"""

import functools
import json
import math
import os
import tempfile
import threading
import time

ACTIONS = ("light_on", "light_off", "preheat", "cooldown", "unload", "load", "home", "move", "extrude", "heater_off")

# The Controls sheet (jog, extrude, filament change). Every limit here is enforced on the server, not only in the page.
MOVE_MAX_MM = 10.0              # one tap moves one axis at most this far (relative moves only)
EXTRUDE_MAX_MM = 10.0           # one tap pushes / pulls at most this much filament
MIN_EXTRUDE_C = 170             # cold-extrusion guard when the printer does not say its own minimum
AXES = ("X", "Y", "Z")
HEATERS = ("nozzle", "bed", "both")
HUB_HOMED_TTL_S = 600.0         # printers that cannot REPORT being homed: a Home the Hub sent counts for this long after the last move

# Heaters and filament are only touched when the printer is parked. RUNNING / PREPARE / PAUSE
# are busy; UNKNOWN / DISCONNECTED mean we cannot reach the controller at all.
BUSY_STATES = ("RUNNING", "PREPARE", "PAUSE")
IDLE_STATES = ("IDLE", "FINISH", "FAILED")

NOZZLE_MAX = 300
BED_MAX = 120
DEFAULT_TIMEOUT_MIN = 15.0

# Pre-heat by preset heats the BED ONLY (owner decision, 10/17/26): the nozzle is heated by the printer itself for a filament change or a print,
# so a preset carries no nozzle temperature (nozzle None = that heater is left alone). The numbers are the Settings page's
# printers.preheat_bed_pla / _petg / _abs; these are only the fallbacks. An explicit `nozzle` argument (API / agents) still works.
DEFAULT_PRESETS = {
    "PLA":  {"nozzle": None, "bed": 60},
    "PETG": {"nozzle": None, "bed": 80},
    "ABS":  {"nozzle": None, "bed": 100},
}


# ------------------------------------------------------------------ start-pending window (review N-8)
#
# Between "the printer ACCEPTED a start command" and "the printer reports PREPARE / RUNNING" there is a gap
# of a few seconds in which the printer still reads IDLE / FINISH / FAILED. A pre-heat auto-off that
# lands in that gap would send "heaters off" into a print that is starting. So every driver calls
# mark_start_sent() once a start command was accepted, and its snapshot() carries `start_pending: True`
# until the print is OBSERVED (RUNNING / PREPARE / PAUSE), or a NEW failure is observed, or
# START_PENDING_S passes (a start the printer ignored). The auto-off and every heater / filament control
# treat start_pending as busy: they wait, they never cool.

START_PENDING_S = 120.0


def mark_start_sent(prn, from_state=None):
    """A start command was accepted by `prn`'s driver. `from_state` is the state the driver last saw (used
    so that a FAILED that was ALREADY showing is not mistaken for the new job failing)."""
    prn._start_pending_at = time.monotonic()
    prn._start_pending_from = from_state


def clear_start_pending(prn):
    prn._start_pending_at = None


def start_pending_for(prn, snap) -> bool:
    """Is a start still settling on this printer, given the snapshot just taken? Clears itself."""
    at = getattr(prn, "_start_pending_at", None)
    if at is None:
        return False
    if time.monotonic() - at >= START_PENDING_S:
        prn._start_pending_at = None
        return False
    state = snap.get("state")
    if snap.get("connected") and state in BUSY_STATES:
        prn._start_pending_at = None                  # it took: the busy state guards the printer from here
        return False
    if (snap.get("connected") and state == "FAILED"
            and getattr(prn, "_start_pending_from", None) not in (None, "FAILED")):
        prn._start_pending_at = None                  # the new job failed right away: nothing is starting
        return False
    return True


def with_start_pending(fn):
    """Decorator for a driver's snapshot(): adds `start_pending` (bool) to the returned dict."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        snap = fn(self, *a, **kw)
        if isinstance(snap, dict):
            snap["start_pending"] = start_pending_for(self, snap)
        return snap
    return wrapper


# ------------------------------------------------------------------ recent-control window
#
# Background chores (the file-metadata prewarm) leave a printer alone for a while after someone has just
# worked its controls: a bed pre-heat, a cool-down, a light, a filament move. Heating printers answer slowly
# and a human is watching them; the chores can wait.

CONTROL_QUIET_S = 120.0
_last_control = {}


def note_control(prn_or_name):
    """A control action is about to be sent to this printer (or the auto-off is cooling it)."""
    _last_control[_key(prn_or_name)] = time.monotonic()


def recent_control(prn_or_name, window=CONTROL_QUIET_S) -> bool:
    at = _last_control.get(_key(prn_or_name))
    return at is not None and time.monotonic() - at < window


class ControlError(Exception):
    """Base for control failures that map to an HTTP status."""
    status = 400
    code = "invalid"


class ControlInvalid(ControlError):
    """Bad input: unknown preset, temperature out of range, missing argument."""
    status = 400
    code = "invalid"


class ControlRefused(ControlError):
    """Not allowed right now (printer busy / not connected)."""
    status = 409
    code = "refused"


class ControlUnsupported(ControlError):
    """This printer (or its driver) cannot do that. The message is the reason."""
    status = 422
    code = "unsupported"


# ------------------------------------------------------------------ presets

_presets: dict = {k: dict(v) for k, v in DEFAULT_PRESETS.items()}
_resolver = None          # name(lower) -> live driver, set by the server so edits/removals are honoured


def check_temp(value, maximum, what):
    """int temperature in [0, maximum] or ControlInvalid. bool / NaN / text are rejected."""
    if isinstance(value, bool):
        raise ControlInvalid(f"{what} temperature must be a number.")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ControlInvalid(f"{what} temperature must be a number, got {value!r}.") from None
    if f != f or f in (float("inf"), float("-inf")):
        raise ControlInvalid(f"{what} temperature must be a finite number.")
    if f < 0 or f > maximum:
        raise ControlInvalid(f"{what} temperature {f:g} C is outside the allowed 0-{maximum} C.")
    return int(round(f))


def _clean_preset(name, spec):
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        spec = {"nozzle": spec[0], "bed": spec[1]}
    if not isinstance(spec, dict):
        raise ControlInvalid(f"preset {name!r} must be {{bed}}.")
    # a nozzle value in an older config.yml is read and dropped: a preset heats the bed only
    return {"nozzle": None, "bed": check_temp(spec.get("bed"), BED_MAX, f"preset {name} bed")}


def configure(presets=None, resolver=None, log=print, exclusive=False, data_dir=None, restore=True):
    """Install config-supplied presets on top of the defaults and the live-driver resolver.
    A bad preset entry is skipped with a log line (config is optional and must never stop the Hub).
    exclusive=True means `presets` IS the whole list (the Settings page owns it: a preset the user
    removed there must not come back from the built-in defaults).
    data_dir (default: the directory of BAMBU_MCP_CACHE, else /data when it exists) holds
    preheat_timers.json. The first call that gives a resolver also re-arms the pre-heat auto-offs that
    were pending when the Hub last stopped (restore=False skips that)."""
    global _presets, _resolver, _data_dir
    if data_dir is not None:
        _data_dir = data_dir
    merged = {} if exclusive else {k: dict(v) for k, v in DEFAULT_PRESETS.items()}
    if isinstance(presets, dict):
        for name, spec in presets.items():
            try:
                merged[str(name).strip().upper()] = _clean_preset(str(name), spec)
            except ControlInvalid as e:
                log(f"[controls] ignoring preheat_presets entry: {e}")
    _presets = merged
    if resolver is not None:
        _resolver = resolver
        if restore and not _restored[0]:
            _restored[0] = True
            try:
                restore_timers()
            except Exception as e:      # start-up must never be stopped by a bad timer file
                log(f"[controls] could not restore pre-heat timers: {e}")


def presets() -> dict:
    return {k: dict(v) for k, v in _presets.items()}


# Nozzle temperature per material for the Controls sheet (Nozzle buttons, Change filament, Load / Unload). The numbers are the
# Settings page's printers.filament_temp_pla / _petg / _abs; these are only the fallbacks.
DEFAULT_FILAMENT_TEMPS = {"PLA": 210, "PETG": 235, "ABS": 245}
_filament_temps: dict = dict(DEFAULT_FILAMENT_TEMPS)


def configure_filament_temps(temps=None, log=print):
    """Install the nozzle temperature of each material (name -> degrees C, 1..NOZZLE_MAX). A bad entry is skipped with a log
    line and keeps the built-in number; None / {} resets to the defaults."""
    global _filament_temps
    merged = dict(DEFAULT_FILAMENT_TEMPS)
    if isinstance(temps, dict):
        for name, t in temps.items():
            try:
                v = check_temp(t, NOZZLE_MAX, f"filament {name} nozzle")
                if v < MIN_EXTRUDE_C:
                    raise ControlInvalid(f"filament {name} nozzle temperature must be at least {MIN_EXTRUDE_C} C.")
                merged[str(name).strip().upper()] = v
            except ControlInvalid as e:
                log(f"[controls] ignoring filament temperature: {e}")
    _filament_temps = merged


def filament_temps() -> dict:
    return dict(_filament_temps)


def material_nozzle(material) -> int:
    """Nozzle temperature for a material name (PLA, PETG, ...) or ControlInvalid."""
    name = str(material).strip().upper()
    if name not in _filament_temps:
        raise ControlInvalid(f"Unknown material {material!r}. Known: {', '.join(_filament_temps)}.")
    return _filament_temps[name]


# Runtime hooks the server installs (kept here so the module still imports nothing from the app):
#   timeout_min        - the pre-heat auto-off saved on the Settings page, in minutes. None = nothing saved,
#                        use PREHEAT_TIMEOUT_MIN / the 15 min default. Never infinite: 0 or less = the default;
#                        anything above MAX_TIMEOUT_MIN is clamped to it.
#   on_auto_off        - callable(printer_name, minutes) after the auto-off really cooled a printer down.
#   on_auto_off_failed - callable(printer_name, reason) when the auto-off gave up (the heaters may still be on).
_UNSET = object()
_timeout_min = None
_on_auto_off = None
_on_auto_off_failed = None
MAX_TIMEOUT_MIN = 240.0


def _clean_minutes(v):
    """float minutes in (0, MAX_TIMEOUT_MIN], or None for anything unusable (None, text, NaN, inf,
    zero, negative). Large finite values are clamped, so no timer is ever armed for 1e12 minutes."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f <= 0:
        return None
    return min(f, MAX_TIMEOUT_MIN)


# Failures that happen BEFORE the failure hook exists (the pre-heat timers are restored while the server is
# still importing; the hook is installed a few lines later) are parked here and replayed to the hook the
# moment set_runtime(on_auto_off_failed=...) installs it, so a boot-time "could not restore the auto-off"
# is announced instead of only logged (review N-11).
_unannounced: list = []
_UNANNOUNCED_MAX = 100


def set_runtime(timeout_min=_UNSET, on_auto_off=_UNSET, on_auto_off_failed=_UNSET):
    global _timeout_min, _on_auto_off, _on_auto_off_failed
    if timeout_min is not _UNSET:
        _timeout_min = None if timeout_min is None else _clean_minutes(timeout_min)
    if on_auto_off is not _UNSET:
        _on_auto_off = on_auto_off
    if on_auto_off_failed is not _UNSET:
        _on_auto_off_failed = on_auto_off_failed
        if on_auto_off_failed is not None:
            _replay_unannounced()


def _replay_unannounced():
    hook = _on_auto_off_failed
    if hook is None:
        return
    with _tlock:
        parked, _unannounced[:] = list(_unannounced), []
    for name, reason in parked:
        try:
            hook(name, reason)
        except Exception as ex:
            _log(f"[controls] auto-off failure hook failed (replaying a boot-time failure): {ex}")


def resolve_temps(preset=None, nozzle=None, bed=None):
    """-> (nozzle|None, bed|None, preset_name|None), validated. Explicit temperatures override the
    preset's. At least one heater must end up above 0 (use cooldown for the rest)."""
    name = None
    if preset not in (None, ""):
        name = str(preset).strip().upper()
        if name not in _presets:
            raise ControlInvalid(f"Unknown preset {preset!r}. Known: {', '.join(_presets)}.")
        base = _presets[name]
        n, b = base["nozzle"], base["bed"]
    else:
        n = b = None
    if nozzle is not None:
        n = check_temp(nozzle, NOZZLE_MAX, "nozzle")
    if bed is not None:
        b = check_temp(bed, BED_MAX, "bed")
    if n is None and b is None:
        raise ControlInvalid("Give a preset (" + ", ".join(_presets) + ") or a bed (and/or nozzle) temperature.")
    if not (n or b):
        raise ControlInvalid("Both temperatures are 0 - use cooldown to switch the heaters off.")
    return n, b, name


# ------------------------------------------------------------------ pre-heat auto-off
#
# One supervisor thread owns every pending auto-off (no per-timer threading.Timer: an exception can
# never silently kill a timer, and a huge value can never overflow one). Each entry is
#   armed   -> waiting for its deadline; every OBSERVE_EVERY_S the printer is looked at, and ONLY an
#              observed print (connected + RUNNING / PREPARE / PAUSE) drops the entry
#   firing  -> deadline passed; "heaters off" is sent ONLY when the printer is connected AND positively
#              IDLE / FINISH / FAILED. Anything else (disconnected, UNKNOWN, a fault) is retried with
#              back-off for RETRY_WINDOW_S, then the failure is recorded and announced.
# Entries are persisted to <data dir>/preheat_timers.json, so a restart / crash / container update does
# not leave the heaters on: at start-up the entries are re-armed (a deadline that passed while the Hub
# was down is simply "firing").

RETRY_WINDOW_S = 600.0          # keep trying this long after the deadline, then give up and alert
RETRY_STEPS = (5.0, 10.0, 20.0, 30.0)   # back-off between attempts; the last step repeats
OBSERVE_EVERY_S = 5.0           # how often an armed entry's printer is checked for a started print
STATE_FILE = "preheat_timers.json"

_timers: dict = {}        # printer key -> entry dict (see _new_entry)
_last_auto_off: dict = {}  # printer key -> {"at", "result"}
_tlock = threading.Lock()
_cv = threading.Condition(_tlock)
_plock = threading.Lock()
_token = [0]
_thread = [None]
_restored = [False]
_data_dir = None
_log = print


def _key(prn_or_name) -> str:
    return str(getattr(prn_or_name, "name", prn_or_name)).lower()


def printer_identity(prn) -> str:
    """Which physical machine a driver talks to (NOT its name, which can be reused for another
    machine): Bambu -> its serial number, HTTP printers -> their address."""
    try:
        serial = getattr(prn, "serial", None)
        if serial:
            return "serial:" + str(serial).strip().upper()
        for attr in ("url", "ip"):
            v = getattr(prn, attr, None)
            if v:
                return f"{getattr(prn, 'kind', '?')}:" + str(v).strip().lower().rstrip("/")
    except Exception:
        pass
    return ""


def timeout_seconds() -> float:
    """Pre-heat timeout from env PREHEAT_TIMEOUT_MIN (minutes, default 15). Never infinite and never
    absurd: a missing, zero, negative, NaN or unparsable value means the default; anything above
    240 minutes is clamped to 240."""
    m = _clean_minutes(os.environ.get("PREHEAT_TIMEOUT_MIN", ""))
    if m is not None:
        return m * 60.0
    return DEFAULT_TIMEOUT_MIN * 60.0


def effective_timeout_seconds():
    """Seconds before the heaters are switched off again. Never None - the auto-off cannot be
    disabled. Saved setting (>0, clamped to 240 min) > PREHEAT_TIMEOUT_MIN > 15 min."""
    if _timeout_min is not None:
        return _timeout_min * 60.0
    return timeout_seconds()


def auto_off_remaining(prn_or_name):
    """Seconds until the Hub switches the heaters off again, or None."""
    with _tlock:
        e = _timers.get(_key(prn_or_name))
        return max(0, round(e["deadline"] - time.time())) if e else None


# ---- persistence

def _state_path():
    d = _data_dir
    if d is None:
        cache = os.environ.get("BAMBU_MCP_CACHE")
        d = os.path.dirname(cache) if cache else ("/data" if os.path.isdir("/data") else None)
    return os.path.join(d, STATE_FILE) if d else None


def _persist():
    """Write every pending entry to disk (atomic). Never raises."""
    path = _state_path()
    if not path:
        return
    with _tlock:
        items = [{"key": k, "name": e.get("name"), "identity": e.get("identity"), "deadline": e["deadline"],
                  "nozzle": e.get("nozzle"), "bed": e.get("bed"), "seconds": e.get("seconds")}
                 for k, e in _timers.items()]
    with _plock:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=STATE_FILE + ".", suffix=".tmp", dir=os.path.dirname(path) or ".")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump({"version": 1, "timers": items}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as ex:
            _log(f"[controls] could not save {path}: {ex}")


def restore_timers() -> int:
    """Re-arm the auto-offs that were pending when the Hub last stopped (needs the live-driver resolver,
    so call it after the fleet is loaded; configure(resolver=...) does that once). A deadline that has
    passed is 'firing': the printer is cooled down as soon as it is confirmed idle. Returns how many
    entries were restored."""
    path = _state_path()
    if not path or not _resolver:
        return 0
    try:
        with open(path) as fh:
            doc = json.load(fh)
        items = doc.get("timers", []) if isinstance(doc, dict) else []
    except FileNotFoundError:
        return 0
    except Exception as ex:
        _log(f"[controls] {path} unreadable ({ex}); no pre-heat timers restored")
        return 0
    n, now, dropped = 0, time.time(), 0
    for it in items:
        try:
            key = str(it["key"]).lower()
            prn = _resolver(key)
            ident = it.get("identity") or ""
            if prn is None or (ident and printer_identity(prn) != ident):
                why = ("printer is no longer in the fleet" if prn is None
                       else "that name now belongs to a different machine")
                _record_failure(key, str(it.get("name") or key),
                                f"could not restore the pre-heat auto-off: {why}; its heaters may still be on")
                dropped += 1            # announced once; the dead entry must not be re-logged on every boot
                continue
            deadline = float(it["deadline"])
            if not math.isfinite(deadline):
                continue
            deadline = min(deadline, now + MAX_TIMEOUT_MIN * 60.0)     # a wild clock must not park it for days
            with _tlock:
                if key in _timers:
                    continue
                _timers[key] = _new_entry(key, prn, deadline, it.get("nozzle"), it.get("bed"),
                                          it.get("seconds") or 0, now)
            n += 1
        except Exception as ex:
            _log(f"[controls] skipped a bad pre-heat timer entry: {ex}")
    if n:
        _log(f"[controls] re-armed {n} pre-heat auto-off timer(s) from {path}")
        _ensure_supervisor()
    if n or dropped:
        _persist()                      # rewrites the file from the live entries: dead ones are gone
    return n


# ---- the entries

def _new_entry(key, prn, deadline, nozzle, bed, seconds, now):
    _token[0] += 1
    e = {"token": _token[0], "deadline": deadline, "prn": prn, "name": getattr(prn, "name", key),
         "identity": printer_identity(prn), "nozzle": nozzle, "bed": bed, "seconds": seconds,
         "phase": "armed", "next_observe": now + OBSERVE_EVERY_S, "attempts": 0,
         "first_attempt": None, "next_try": 0.0, "working": False}
    if deadline <= now:
        e.update(phase="firing", first_attempt=now, next_try=now)
    return e


def cancel_auto_off(prn_or_name) -> bool:
    with _tlock:
        e = _timers.pop(_key(prn_or_name), None)
        if e:
            _cv.notify_all()
    if e:
        _persist()
    return bool(e)


def note_print_start(prn_or_name) -> bool:
    """Kept for callers that still announce "a print is starting". It deliberately does NOTHING: a start
    that is refused, fails or is silently ignored by the printer must not cost the pre-heat its auto-off.
    The auto-off drops itself only when the printer is actually OBSERVED printing (RUNNING / PREPARE /
    PAUSE while connected)."""
    return False


def schedule_auto_off(prn, seconds=None, nozzle=None, bed=None):
    """Arm (or re-arm) the auto-off. Returns the delay in seconds. The auto-off cannot be disabled;
    `seconds` defaults to the configured timeout and is clamped to (0, 240 min]."""
    if seconds is None:
        seconds = effective_timeout_seconds()
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = float("nan")
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = effective_timeout_seconds()
    seconds = min(seconds, MAX_TIMEOUT_MIN * 60.0)
    key, now = _key(prn), time.time()
    with _tlock:
        _timers[key] = _new_entry(key, prn, now + seconds, nozzle, bed, seconds, now)
        _cv.notify_all()
    _ensure_supervisor()
    _persist()
    return seconds


def _ensure_supervisor():
    with _tlock:
        t = _thread[0]
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_supervise, name="preheat-auto-off", daemon=True)
        _thread[0] = t
        t.start()


def _supervise():
    """Never exits and never dies on an exception: whatever one tick throws, the next tick runs."""
    while True:
        try:
            wait = _tick()
        except BaseException as ex:                 # noqa: BLE001 - a supervisor must outlive anything
            _log(f"[controls] pre-heat supervisor error (continuing): {type(ex).__name__}: {ex}")
            wait = 2.0
        with _cv:
            _cv.wait(timeout=max(0.05, min(wait, OBSERVE_EVERY_S)))


def _tick() -> float:
    """Service every entry once; -> seconds until something is next due."""
    now = time.time()
    with _tlock:
        items = list(_timers.items())
    nxt = OBSERVE_EVERY_S
    for key, e in items:
        try:
            nxt = min(nxt, _service(key, e, now))
        except Exception as ex:
            _log(f"[controls] pre-heat auto-off tick for {key} failed (will retry): {type(ex).__name__}: {ex}")
            nxt = min(nxt, 2.0)
    return nxt


def _service(key, e, now) -> float:
    if e["phase"] == "armed":
        if now < e["deadline"]:
            if now >= e["next_observe"]:
                e["next_observe"] = now + OBSERVE_EVERY_S
                _observe(key, e)
            return min(e["deadline"] - now, max(0.05, e["next_observe"] - now))
        with _tlock:
            if _timers.get(key) is not e:
                return OBSERVE_EVERY_S
            e.update(phase="firing", first_attempt=now, next_try=now)
    if e["phase"] == "firing":
        if now >= e["next_try"]:
            _attempt(key, e["token"])
        return max(0.05, e["next_try"] - time.time())
    return OBSERVE_EVERY_S


def _resolve(key, e):
    """(driver to talk to, problem|None). The live driver for that name, but only if it is still the
    same MACHINE the pre-heat was sent to: a name re-used for another printer is never cooled."""
    prn = _resolver(key) if _resolver else None
    ident = e.get("identity") or ""
    if prn is not None and ident and printer_identity(prn) != ident:
        if printer_identity(e["prn"]) == ident:
            return e["prn"], None
        return None, "that printer name now belongs to a different machine; the pre-heated one was not cooled"
    return (prn if prn is not None else e["prn"]), None


def _fresh_snapshot(prn):
    fn = getattr(prn, "fresh_snapshot", None)
    return fn() if callable(fn) else prn.snapshot()


def _observe(key, e):
    """An armed entry: is a print running on that printer now? Only a positive sighting drops the entry."""
    try:
        prn, bad = _resolve(key, e)
        if prn is None:
            return
        snap = prn.snapshot()
    except Exception:
        return
    if snap.get("connected") and snap.get("state") in BUSY_STATES:
        with _tlock:
            if _timers.get(key) is not e:
                return
        _record(key, f"skipped: printer is {snap.get('state')} (a print owns the heaters)")
        _drop_entry(key, e)
        _persist()


def _record(key, result):
    rec = {"at": time.time(), "result": result}
    _last_auto_off[key] = rec
    _log(f"[controls] pre-heat auto-off on {key}: {result}")


def _record_failure(key, name, reason):
    _record(key, f"failed: {reason}")
    hook = _on_auto_off_failed
    if hook is None:                    # not installed yet (boot): keep it for set_runtime() to replay
        with _tlock:
            _unannounced.append((name, reason))
            del _unannounced[:-_UNANNOUNCED_MAX]
        return
    if hook is not None:
        try:
            hook(name, reason)
        except Exception as ex:
            _log(f"[controls] auto-off failure hook failed: {ex}")


def _unconfirmed(res):
    """'' when a driver's set_temps() result does not say it failed to confirm, else the human sentence.
    Drivers that cannot verify (HTTP ones) return no `confirmed` key and are taken at their word."""
    if isinstance(res, dict) and "confirmed" in res and res["confirmed"] is not True:
        return str(res.get("unconfirmed") or "the cool-down was sent but the printer's reports do not "
                   "confirm the heaters are off")
    return ""


def _drop_entry(key, e):
    """Remove entry `e` (if it is still the live one) and run its cleanup (a detached supervision driver
    is disconnected once its timer is finished)."""
    with _tlock:
        if _timers.get(key) is e:
            _timers.pop(key, None)
        _cv.notify_all()
    cleanup = e.pop("cleanup", None)
    if cleanup:
        try:
            cleanup()
        except Exception as ex:
            _log(f"[controls] detached-driver cleanup failed: {ex}")


def _try_cool(key, e):
    """One cool-down attempt -> (outcome, detail); outcome is 'cooled' | 'skipped' | 'retry'."""
    prn, problem = _resolve(key, e)
    if prn is None:
        return "skipped", problem
    snap = _fresh_snapshot(prn)
    state, connected = snap.get("state"), bool(snap.get("connected"))
    if connected and state in BUSY_STATES:
        return "skipped", f"printer is {state} (a print owns the heaters)"
    if not connected or state not in IDLE_STATES:
        return "retry", (f"printer is not confirmed idle (state {state}, "
                         f"{'connected' if connected else 'not connected'})")
    if snap.get("start_pending"):
        # N-8: a start was just accepted and the printer has not reported PREPARE / RUNNING yet. It still
        # reads idle, but cooling now would hit a print that is starting. Wait.
        return "retry", "a print start was just sent and has not shown up yet (waiting, not cooling)"
    with _tlock:
        if _timers.get(key) is not e:       # cancelled while we were looking
            return "skipped", "cancelled"
    note_control(prn)
    res = prn.set_temps(0, 0)
    bad = _unconfirmed(res)
    if bad:
        # N-13: the command was published but the printer's own reports still show heater targets. Not
        # "cooled down" - try again (and eventually alert) instead of dropping the timer.
        return "retry", bad
    return "cooled", "cooled down"


def _attempt(key, token):
    with _tlock:
        e = _timers.get(key)
        if not e or e["token"] != token or e["working"]:
            return
        e["working"] = True
        if e["first_attempt"] is None:
            e.update(phase="firing", first_attempt=time.time())
    try:
        try:
            outcome, detail = _try_cool(key, e)
        except Exception as ex:
            outcome, detail = "retry", f"{type(ex).__name__}: {ex}"
    finally:
        e["working"] = False
    now = time.time()
    e["attempts"] += 1
    if outcome == "retry":
        if now - e["first_attempt"] < RETRY_WINDOW_S:
            e["next_try"] = now + RETRY_STEPS[min(e["attempts"] - 1, len(RETRY_STEPS) - 1)]
            if e["attempts"] == 1:
                _log(f"[controls] pre-heat auto-off on {key}: {detail}; retrying for "
                     f"{round(RETRY_WINDOW_S / 60)} min")
            return
        _record_failure(key, e["name"], f"gave up after {round(RETRY_WINDOW_S / 60)} min: {detail}; "
                                        "the heaters may still be on - check the printer")
        _drop_entry(key, e)
        _persist()
        return
    if detail != "cancelled":
        _record(key, "cooled down" if outcome == "cooled" else f"skipped: {detail}")
    _drop_entry(key, e)
    _persist()
    if outcome == "cooled" and _on_auto_off is not None:
        mins = round((e.get("seconds") or 0) / 60.0, 1)
        try:
            _on_auto_off(e["name"], int(mins) if mins == int(mins) else mins)
        except Exception as ex:         # a notification problem must not matter here
            _log(f"[controls] auto-off hook failed: {ex}")


def _fire(key, token):
    """Run the cool-down attempt for this entry now (the supervisor calls it at the deadline; tests and
    tools may call it directly)."""
    _attempt(key, token)


def release_printer(prn) -> str:
    """The printer is about to be removed or re-pointed at another machine: cool it down FIRST if a
    pre-heat auto-off is pending for it and it is connected and idle (a printing one keeps its heaters).
    If that cannot be confirmed right now the entry stays, bound to the machine (not the name), and the
    normal retry / alert runs. Returns a short human result ('' when nothing was pending). Never raises."""
    key = _key(prn)
    with _tlock:
        e = _timers.get(key)
    if not e:
        return ""
    ident = printer_identity(prn)
    if e.get("identity") and ident and ident != e["identity"]:
        return ""
    try:
        snap = _fresh_snapshot(prn)
        state, connected = snap.get("state"), bool(snap.get("connected"))
        if connected and state in BUSY_STATES:
            res = f"printer is {state} (a print owns the heaters)"
            outcome = "skipped"
        elif connected and state in IDLE_STATES and snap.get("start_pending"):
            res, outcome = "a print start was just sent and has not shown up yet", "retry"
        elif connected and state in IDLE_STATES:
            bad = _unconfirmed(prn.set_temps(0, 0))
            res, outcome = (bad, "retry") if bad else ("cooled down", "cooled")
        else:
            res, outcome = f"printer is not confirmed idle (state {state})", "retry"
    except Exception as ex:
        res, outcome = f"{type(ex).__name__}: {ex}", "retry"
    if outcome == "retry":
        with _tlock:
            if _timers.get(key) is e:
                e["prn"] = prn                      # keep the (about to be stopped) driver bound to its machine
                e.update(phase="firing", first_attempt=e["first_attempt"] or time.time(), next_try=time.time() + 5)
                _cv.notify_all()
        _ensure_supervisor()
        return f"pre-heat auto-off kept ({res})"
    _drop_entry(key, e)
    _persist()
    _record(key, "cooled down (printer released)" if outcome == "cooled" else f"skipped: {res}")
    return f"pre-heat auto-off: {res}"


def has_pending(prn) -> bool:
    """Is a pre-heat auto-off still pending for THIS machine (same name AND same identity)?"""
    with _tlock:
        e = _timers.get(_key(prn))
    if not e:
        return False
    ident = printer_identity(prn)
    return not (e.get("identity") and ident and ident != e["identity"])


def adopt_detached(old_prn, clone, cleanup=None) -> bool:
    """N-9: `old_prn` (a Bambu) is being torn down / re-pointed while its pre-heat auto-off could not be
    settled (the machine was not confirmed idle). Move that entry onto `clone` - a fresh driver for the
    SAME machine (same address, serial and access code) - under its own key, so:
      * the old machine stays supervised (retry / alert) even though its driver is gone,
      * a new printer re-using the NAME can neither inherit nor overwrite it.
    `cleanup` (e.g. clone.disconnect) runs when the entry is finished. Returns whether it was adopted."""
    key = _key(old_prn)
    with _tlock:
        e = _timers.get(key)
        if not e:
            return False
        ident = printer_identity(old_prn)
        if e.get("identity") and ident and ident != e["identity"]:
            return False
        _timers.pop(key, None)
        newkey = f"{key}#detached:{e.get('identity') or id(e)}"
        old = _timers.get(newkey)
        e["prn"] = clone
        e["cleanup"] = cleanup
        e["detached"] = True
        e["phase"] = "firing"
        e["first_attempt"] = e["first_attempt"] or time.time()
        e["next_try"] = time.time() + 5
        _timers[newkey] = e
        _cv.notify_all()
    if old is not None and old is not e:
        c = old.pop("cleanup", None)
        if c:
            try:
                c()
            except Exception:
                pass
    _ensure_supervisor()
    _persist()
    return True


def reset_for_tests():
    """Drop every pending entry (and the persisted file contents). Test helper."""
    with _tlock:
        old = list(_timers.values())
        _timers.clear()
        _unannounced.clear()
        _last_control.clear()
        _cv.notify_all()
    for e in old:
        c = e.get("cleanup")
        if c:
            try:
                c()
            except Exception:
                pass
    _persist()


# ------------------------------------------------------------------ capability + gate

_ALL_UNSUPPORTED_REASON = "This printer's driver has no printer controls."


def all_unsupported(reason):
    caps = {k: {"supported": False, "reason": reason} for k in ("heat", "unload", "load", "home", "move", "extrude")}
    caps["light"] = {"supported": False, "state": None, "reason": reason}
    return caps


def _caps(prn) -> dict:
    fn = getattr(prn, "control_caps", None)
    caps = fn() if callable(fn) else all_unsupported(_ALL_UNSUPPORTED_REASON)
    out = all_unsupported("Not reported by the driver.")
    for k, v in (caps or {}).items():
        if k in out and isinstance(v, dict):
            out[k] = v
    return out


def _gate(snap, action):
    """None when `action` may run now, else the human-readable refusal. Light is never gated.
    Heaters and filament: only while connected and positively IDLE / FINISH / FAILED, and not while the
    printer reports a hard fault. Cool-down is the safe direction, so it only needs 'connected and not
    printing' (an odd or unknown state does not stop you switching the heaters OFF)."""
    if action in ("light_on", "light_off"):
        return None
    state = snap.get("state")
    connected = bool(snap.get("connected"))
    if connected and state in BUSY_STATES:
        return f"printer is {state}: heaters, filament and movement are not touched during a print."
    if snap.get("start_pending"):
        return ("a print start was just sent to this printer and has not shown up yet: heaters, filament and "
                "movement are not touched until it does (or about 2 minutes pass).")
    if action in ("cooldown", "heater_off"):
        if not connected:
            return f"printer is not connected (state {state}" + (f": {snap.get('error')}" if snap.get("error") else "") + ")."
        return None
    if state in BUSY_STATES:
        return f"printer is {state}: heaters, filament and movement are not touched during a print."
    if not connected or state not in IDLE_STATES:
        return (f"printer is not ready (state {state}"
                + (f": {snap.get('error')}" if snap.get("error") else "") + ").")
    if snap.get("fault"):
        return "printer reports a fault" + _fault_text(snap.get("fault")) + ": clear the error on the printer first."
    return None


def _fault_text(fault) -> str:
    """' (<message>)' for a driver's fault dict, '' for a plain True."""
    if isinstance(fault, dict):
        msg = fault.get("message") or fault.get("code")
        if msg:
            return f" ({msg})"
    return ""


def _cap_entry(cap, gate_reason):
    e = {"supported": bool(cap.get("supported"))}
    if not e["supported"]:
        e["reason"] = cap.get("reason") or "Not supported by this printer."
    else:
        e["allowed_now"] = gate_reason is None
        if gate_reason:
            e["blocked"] = gate_reason
    for k in ("note", "verified"):
        if k in cap:
            e[k] = cap[k]
    return e


# ------------------------------------------------------------------ homed / hot enough (server-side rules for move + extrude)

def _hub_homed(prn, snap) -> bool:
    """A printer that cannot REPORT its homed axes (OctoPrint, Bambu): did the Hub home it, recently, and has it
    stayed connected and out of a print since? Anything else is 'not homed' - motors may have been switched off."""
    at = getattr(prn, "_hub_homed_at", None)
    if at is None:
        return False
    busy = bool(snap.get("connected")) and snap.get("state") in BUSY_STATES
    if not snap.get("connected") or busy or time.monotonic() - at > HUB_HOMED_TTL_S:
        prn._hub_homed_at = None
        return False
    return True


def note_homed(prn):
    prn._hub_homed_at = time.monotonic()


def homed_state(prn, snap):
    """(homed: bool, how: 'reported' | 'hub'). A driver that reports `homed` in its snapshot is believed
    (False means NOT homed); otherwise only the Hub's own recent Home counts."""
    if snap.get("homed") in (True, False):
        return bool(snap["homed"]), "reported"
    return _hub_homed(prn, snap), "hub"


def _extrude_min(prn, snap) -> int:
    fn = getattr(prn, "extrude_min_c", None)
    v = None
    if callable(fn):
        try:
            v = fn()
        except Exception:
            v = None
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= NOZZLE_MAX:
        return int(round(v))
    return MIN_EXTRUDE_C


def _too_cold(prn, snap):
    """None when the nozzle is hot enough to push filament, else the sentence that says why."""
    need = _extrude_min(prn, snap)
    cur = snap.get("nozzle_c")
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return f"The nozzle temperature is unknown. Heat the nozzle first (it must be at least {need} C)."
    if cur < need:
        return f"Heat the nozzle first: it is {cur:g} C and extruding needs at least {need} C."
    if snap.get("can_extrude") is False:
        return "Heat the nozzle first: the printer says it is too cold to extrude."
    return None


def _not_homed(prn, snap):
    ok, how = homed_state(prn, snap)
    if ok:
        return None
    return ("The axes are not homed. Home first."
            if how == "reported" else
            "The Hub has not homed this printer lately (it cannot read the printer's homed state). Home first.")


def clean_axis(axis) -> str:
    a = str(axis).strip().upper() if isinstance(axis, str) else ""
    if a not in AXES:
        raise ControlInvalid(f"axis must be one of {', '.join(AXES)}.")
    return a


def clean_mm(value, limit, what) -> float:
    """Signed millimetres, finite, not zero, at most `limit` either way. bool / text / NaN are rejected."""
    if isinstance(value, bool) or value is None:
        raise ControlInvalid(f"{what}: give a distance in mm.")
    if not isinstance(value, (int, float)):            # a JSON number only: no "5", no text
        raise ControlInvalid(f"{what}: the distance must be a number, got {value!r}.")
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        raise ControlInvalid(f"{what}: the distance must be a finite number.")
    if abs(f) > limit:
        raise ControlInvalid(f"{what}: {f:g} mm is more than the {limit:g} mm allowed per tap.")
    f = round(f, 2)
    if f == 0:
        raise ControlInvalid(f"{what}: the distance cannot be 0.")
    return f


def _nozzle_for_filament(prn, snap, nozzle, material):
    """The nozzle temperature a load / unload heats to first: an explicit one, else the material's, else what the
    nozzle is already set to, else the first material (PLA). Only for printers that need the Hub to say (Klipper, OctoPrint)."""
    named = material_nozzle(material) if material not in (None, "") else None        # an unknown material is refused on every printer
    if not getattr(prn, "FILAMENT_NOZZLE_TEMP", False):
        return None
    if nozzle is not None:
        n = check_temp(nozzle, NOZZLE_MAX, "nozzle")
    elif named is not None:
        n = named
    else:
        t = snap.get("nozzle_target_c")
        n = int(round(t)) if isinstance(t, (int, float)) and not isinstance(t, bool) and t >= MIN_EXTRUDE_C else next(iter(_filament_temps.values()))
    if n < MIN_EXTRUDE_C:
        raise ControlInvalid(f"The nozzle must be at least {MIN_EXTRUDE_C} C to load or unload filament.")
    return n


def _call_driver(fn, *args, **kw):
    """Call a driver primitive, passing only the keyword arguments its signature has (older / test drivers stay valid)."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            kw = {k: v for k, v in kw.items() if k in params}
    except (TypeError, ValueError):
        pass
    return fn(*args, **kw)


def describe(prn) -> dict:
    """Everything an agent or UI needs to know about this printer's controls right now."""
    snap = prn.snapshot()
    caps = _caps(prn)
    key = _key(prn)
    remaining = auto_off_remaining(key)
    if remaining is not None and snap.get("connected") and snap.get("state") in BUSY_STATES:
        cancel_auto_off(key)            # a print is OBSERVED running (started outside the Hub): it owns the heaters
        remaining = None
    gate = _gate(snap, "preheat")
    cool_gate = _gate(snap, "cooldown")
    light = _cap_entry(caps["light"], None)
    light["state"] = caps["light"].get("state") if light["supported"] else None
    heat = caps["heat"]
    preheat = _cap_entry(heat, gate)
    cooldown = _cap_entry(heat, cool_gate)
    preheat["presets"] = presets()                  # also for a printer that cannot heat: the sheet shows the same buttons, disabled, with the reason
    preheat["limits"] = {"nozzle_max_c": NOZZLE_MAX, "bed_max_c": BED_MAX}
    if preheat["supported"]:
        eff = effective_timeout_seconds()
        preheat["auto_off_timeout_min"] = round(eff / 60.0, 2) if eff else 0
        preheat["auto_off_in_s"] = remaining
        last = _last_auto_off.get(key)
        if last:
            preheat["last_auto_off"] = {"result": last["result"], "ago_s": round(time.time() - last["at"])}
    unload = _cap_entry(caps["unload"], gate)
    load = _cap_entry(caps["load"], gate)
    # ---- the Controls sheet: Home / Move / Extrude / one heater off / Change filament
    homed, how = homed_state(prn, snap)
    home = _cap_entry(caps["home"], gate)
    move = _cap_entry(caps["move"], gate or _not_homed(prn, snap))
    cold = _too_cold(prn, snap)
    extrude = _cap_entry(caps["extrude"], gate or cold)
    heater_off = _cap_entry(heat, cool_gate)
    # Change filament needs heat + unload + load; MMU / PrusaLink / no-macro printers say which part is missing
    parts = [("heating the nozzle", heat), ("unloading", caps["unload"]), ("loading", caps["load"])]
    missing = next(((w, c) for w, c in parts if not c.get("supported")), None)
    change = _cap_entry({"supported": missing is None,
                         "reason": (missing[1].get("reason") or f"This printer cannot do {missing[0]}.") if missing else "",
                         "verified": False if (caps["unload"].get("verified") is False or caps["load"].get("verified") is False) else True},
                        gate)
    if change.get("verified") is True:
        change.pop("verified", None)
    out = {"printer": getattr(prn, "name", None), "kind": getattr(prn, "kind", None),
           "state": snap.get("state"), "connected": bool(snap.get("connected")),
           "light": light, "preheat": preheat, "cooldown": cooldown, "unload": unload, "load": load,
           "home": home, "move": move, "extrude": extrude, "heater_off": heater_off, "change_filament": change}
    out["motion"] = {"homed": homed, "homed_how": how, "min_extrude_c": _extrude_min(prn, snap),
                     "nozzle_c": snap.get("nozzle_c"), "nozzle_target_c": snap.get("nozzle_target_c"),
                     "bed_c": snap.get("bed_c"), "bed_target_c": snap.get("bed_target_c"),
                     "max_move_mm": MOVE_MAX_MM, "max_extrude_mm": EXTRUDE_MAX_MM, "axes": list(AXES)}
    out["filament"] = {"temps": filament_temps(), "needs_nozzle_temp": bool(getattr(prn, "FILAMENT_NOZZLE_TEMP", False)),
                       "unload_mm": getattr(prn, "unload_mm", None), "load_mm": getattr(prn, "load_mm", None)}
    actions = []
    if light["supported"]:
        actions += ["light_on", "light_off"]
    if preheat["supported"]:
        actions += ["preheat", "cooldown", "heater_off"]
    if unload["supported"]:
        actions.append("unload")
    if load["supported"]:
        actions.append("load")
    for name in ("home", "move", "extrude"):
        if out[name]["supported"]:
            actions.append(name)
    out["actions"] = actions
    return out


def summary(prn) -> dict:
    """The compact form carried in /api/printer/<name>: enough to render buttons, plus the full
    description under `detail` (blocked reasons, preset temperatures, verified flags, notes).
    Never raises (a flaky printer must not break the detail view)."""
    try:
        d = describe(prn)
    except Exception as e:
        return {"actions": [], "allowed_now": [], "light": None, "presets": [], "auto_off_in_s": None,
                "error": str(e)}
    allowed = []
    if d["light"]["supported"]:
        allowed += ["light_on", "light_off"]
    for name in ("preheat", "cooldown", "unload", "load", "home", "move", "extrude", "heater_off", "change_filament"):
        if d[name].get("allowed_now"):
            allowed.append(name)
    return {"actions": d["actions"], "allowed_now": allowed,
            "light": d["light"]["state"] if d["light"]["supported"] else None,
            "presets": list(d["preheat"].get("presets", {})) if d["preheat"]["supported"] else [],
            "auto_off_in_s": d["preheat"].get("auto_off_in_s") if d["preheat"]["supported"] else None,
            "detail": d}


# ------------------------------------------------------------------ perform

_NEED = {"light_on": "light", "light_off": "light", "preheat": "heat", "cooldown": "heat", "heater_off": "heat",
         "unload": "unload", "load": "load", "home": "home", "move": "move", "extrude": "extrude"}


def perform(prn, action, preset=None, nozzle=None, bed=None, tray=None, axis=None, mm=None, material=None,
            heater=None, confirm=None) -> dict:
    """Run one control. Raises ControlInvalid (400) / ControlUnsupported (422) / ControlRefused (409);
    a printer-side failure propagates as the driver's own error. Order of checks: known action ->
    supported by this printer -> valid arguments -> safe right now -> send."""
    if action not in ACTIONS:
        raise ControlInvalid(f"Unknown action {action!r}. Known: {', '.join(ACTIONS)}.")
    snap = prn.snapshot()               # first: drivers that discover on the wire need a fresh poll
    caps = _caps(prn)
    cap = caps[_NEED[action]]
    if not cap.get("supported"):
        raise ControlUnsupported(cap.get("reason") or "Not supported by this printer.")
    temps = None
    if action == "preheat":
        if material not in (None, "") and nozzle is None:
            nozzle = material_nozzle(material)       # a material button heats the NOZZLE; the bed is left alone
        temps = resolve_temps(preset, nozzle, bed)
    if action == "load" and tray is not None:
        if isinstance(tray, bool) or not isinstance(tray, int) or tray < 1:
            raise ControlInvalid("tray must be a slot number starting at 1.")
    jog = None
    if action == "move":
        jog = (clean_axis(axis), clean_mm(mm, MOVE_MAX_MM, "move"))
    elif action == "extrude":
        jog = clean_mm(mm, EXTRUDE_MAX_MM, "extrude")
    elif action == "heater_off":
        heater = str(heater or "").strip().lower()
        if heater not in HEATERS:
            raise ControlInvalid(f"heater must be one of {', '.join(HEATERS)}.")
    elif action == "home" and confirm is not True:
        raise ControlInvalid("Home all moves the print head and the bed: send confirm=true to say you are sure.")
    reason = _gate(snap, action)
    if reason:
        raise ControlRefused(reason)
    if action == "move":
        reason = _not_homed(prn, snap)
        if reason:
            raise ControlRefused(reason)
    elif action == "extrude":
        reason = _too_cold(prn, snap)
        if reason:
            raise ControlRefused(reason)
    fil_temp = None
    if action in ("load", "unload"):
        fil_temp = _nozzle_for_filament(prn, snap, nozzle, material)

    note_control(prn)                   # background chores (prewarm) now leave this printer alone for a while
    res = {"ok": True, "action": action, "printer": getattr(prn, "name", None)}
    if action in ("light_on", "light_off"):
        lres = prn.set_light(action == "light_on")
        res["message"] = f"light {'on' if action == 'light_on' else 'off'} sent"
        if isinstance(lres, dict) and lres.get("confirmed") is False:
            res["confirmed"] = False
            res["message"] += " but NOT confirmed by the printer's reports - check the light"
        elif isinstance(lres, dict) and lres.get("confirmed") is True:
            res["confirmed"] = True
    elif action == "preheat":
        n, b, pname = temps
        # Arm the auto-off FIRST: if the command half-lands (nozzle set, bed not) or the link dies right
        # after, the heaters still get switched off. If nothing was sent, the cool-down is harmless.
        secs = schedule_auto_off(prn, nozzle=n, bed=b)
        applied = prn.set_temps(n, b) or {"nozzle": n, "bed": b}
        res.update(preset=pname, nozzle=applied.get("nozzle"), bed=applied.get("bed"),
                   auto_off_in_s=round(secs))
        what = ", ".join(([f"nozzle {applied.get('nozzle')}"] if applied.get("nozzle") is not None else [])
                         + ([f"bed {applied.get('bed')}"] if applied.get("bed") is not None else []))
        res["message"] = (f"pre-heat sent ({what} C" + (f", {pname}" if pname else "") + ")"
                          + f"; heaters switch off in {round(secs / 60, 1)} min unless a print starts")
        if _unconfirmed(applied):
            res["confirmed"] = False
            res["message"] = ("pre-heat sent but NOT confirmed by the printer's reports (" + _unconfirmed(applied)
                              + "); the auto-off is armed anyway and will switch the heaters off in "
                              + f"{round(secs / 60, 1)} min")
    elif action == "cooldown":
        cres = prn.set_temps(0, 0)
        bad = _unconfirmed(cres)
        if bad:
            # N-13: sent, not confirmed. The auto-off (if armed) stays: it will keep trying.
            res["confirmed"] = False
            res["message"] = ("cool-down sent but NOT confirmed (" + bad + "); the heaters may still be on - "
                              "check the printer" + ("; the auto-off stays armed and will retry"
                                                     if auto_off_remaining(prn) is not None else ""))
        else:
            cancel_auto_off(prn)
            res["message"] = "cool-down sent (nozzle 0, bed 0)"
    elif action == "heater_off":
        n_off, b_off = (0 if heater in ("nozzle", "both") else None), (0 if heater in ("bed", "both") else None)
        hres = prn.set_temps(n_off, b_off)
        bad = _unconfirmed(hres)
        other_on = (heater == "nozzle" and (snap.get("bed_target_c") or 0) > 0) or \
                   (heater == "bed" and (snap.get("nozzle_target_c") or 0) > 0)
        if bad:
            res["confirmed"] = False
            res["message"] = f"{heater} off sent but NOT confirmed ({bad}); check the printer"
        else:
            if not other_on:
                cancel_auto_off(prn)         # nothing left that the auto-off would have to switch off
            res["message"] = f"{heater} heater off sent" + ("; the other heater still switches off by itself" if other_on else "")
    elif action == "home":
        _call_driver(prn.home_all)
        note_homed(prn)
        res["message"] = "home all sent - the printer is homing; keep hands clear"
    elif action == "move":
        ax, dist = jog
        _call_driver(prn.move_relative, ax, dist)
        if getattr(prn, "_hub_homed_at", None) is not None:
            note_homed(prn)                           # a move keeps a Hub-tracked 'homed' fresh
        res.update(axis=ax, mm=dist)
        res["message"] = f"moved {ax} {dist:+g} mm"
    elif action == "extrude":
        _call_driver(prn.extrude, jog)
        res.update(mm=jog)
        res["message"] = f"{'extruded' if jog > 0 else 'retracted'} {abs(jog):g} mm of filament"
    elif action in ("unload", "load"):
        if fil_temp is not None:
            schedule_auto_off(prn, nozzle=fil_temp, bed=None)       # the nozzle this heats gets the same auto-off as a pre-heat
            res["nozzle"] = fil_temp
        if action == "unload":
            _call_driver(prn.unload_filament, nozzle=fil_temp)
            res["message"] = ("unload sent" + (f" (nozzle {fil_temp} C)" if fil_temp else "")
                              + " - watch the printer" + ("" if fil_temp else "; it heats the nozzle first"))
        else:
            _call_driver(prn.load_filament, tray, nozzle=fil_temp)
            res["message"] = ("load sent" + (f" (nozzle {fil_temp} C)" if fil_temp else "")
                              + " - watch the printer" + ("" if fil_temp else "; it heats the nozzle first"))
    return res
