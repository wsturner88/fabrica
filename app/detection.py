"""Spaghetti / print-failure detection - the Hub's side of it (engine only).

Three pieces, none of which imports server.py or any driver (stdlib only):

    DetectorClient   pushes one JPEG to a detector service (POST /v1/score, bearer token) and
                     returns a ScoreResult. It never raises.
    FailureTracker   one per printer. Turns the stream of per-picture scores into a level -
                     OK / WARNING / FAILURE - with smoothing, a warm-up, and a long per-printer
                     baseline that is kept on disk, so "my bed clip always looks like spaghetti"
                     stops mattering after a few prints. Written from the published description
                     of how Obico smooths its scores (an exponentially weighted mean checked
                     against a short and a long rolling mean); no Obico code is used.
    DetectionLoop    the scheduler. Every `interval_s` it asks for the fleet, picks the printers
                     that are RUNNING and have a camera, fetches the Hub's shared camera frame,
                     turns it upright and cuts it to the printer's watch area (framefix.py), scores
                     it, feeds the tracker and - unless learn-only - calls the alert hook.

Alert only. Nothing in here pauses or stops a print, by design; the owner decides.

The detector is "image in, score out"; the Hub owns timing, smoothing, state and alerts.
A dead detector is a state of its own ("down"), never shown as "all clear".

Wiring into server.py is documented in docs/detection.md.
"""

import collections
import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request

import framefix

log = logging.getLogger("bambu.detection")

API_VERSION = 1                      # detector API this client speaks

LEVEL_OK, LEVEL_WARNING, LEVEL_FAILURE = "ok", "warning", "failure"
_RANK = {LEVEL_OK: 0, LEVEL_WARNING: 1, LEVEL_FAILURE: 2}
LABELS = {LEVEL_OK: "OK", LEVEL_WARNING: "WARNING - possible spaghetti",
          LEVEL_FAILURE: "FAILURE - likely spaghetti"}

SENSITIVITY = {"low": 0.5, "medium": 1.0, "normal": 1.0, "high": 2.0}


def _finite(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def sensitivity_factor(value) -> float:
    """'low' / 'medium' / 'high' (or a number) -> multiplier. Unknown values fall back to 1.0."""
    if isinstance(value, str) and value.strip().lower() in SENSITIVITY:
        return SENSITIVITY[value.strip().lower()]
    f = _finite(value, 1.0)
    return min(4.0, max(0.25, f)) if f > 0 else 1.0


# =================================================================== parameters

@dataclasses.dataclass(frozen=True)
class Params:
    """Smoothing and threshold parameters. The defaults are tuned for one picture every 10 s."""
    ewm_span: int = 12            # EWM smoothing span (alpha = 2 / (span + 1))
    short_window: int = 310       # frames in the per-print rolling mean
    long_window: int = 7200       # frames in the per-printer baseline (~20 h at 10 s), persisted
    warmup_frames: int = 30       # frames at the start of a print that can never alert
    threshold_low: float = 0.38   # adjusted score below this: always OK
    threshold_high: float = 0.78  # adjusted score above this: failing
    short_factor: float = 3.8     # between the two: failing if adjusted > (short - long) * this
    failure_factor: float = 1.75  # FAILURE needs the signal this much stronger than WARNING
    sensitivity: float = 1.0      # multiplies the adjusted score (low 0.5 / medium 1 / high 2)

    @classmethod
    def for_interval(cls, interval_s=10, sensitivity="medium", scale=True, **overrides):
        """Defaults for a given polling interval. The numbers above are tuned at 10 s; at a
        slower rate the windows are scaled so they cover the same stretch of time (unproven
        for other rates - treat as a starting point, see docs/detection.md)."""
        k = 10.0 / max(1.0, _finite(interval_s, 10.0)) if scale else 1.0
        base = dict(ewm_span=max(2, round(12 * k)), short_window=max(30, round(310 * k)),
                    warmup_frames=max(5, round(30 * k)), sensitivity=sensitivity_factor(sensitivity))
        base.update(overrides)
        return cls(**base)


# =================================================================== verdict

@dataclasses.dataclass
class Verdict:
    level: str            # "ok" | "warning" | "failure"
    warmup: bool          # still inside the warm-up: the level is forced to "ok"
    frame: int            # 1-based frame number within this print
    score: float          # this picture's raw score (sum of detection confidences)
    ewm: float            # smoothed score
    short_mean: float
    long_mean: float
    adjusted: float       # (ewm - long_mean) * sensitivity
    gauge: float          # 0..1 for a UI meter: <1/3 normal, 1/3..2/3 warning, >2/3 failure

    @property
    def label(self) -> str:
        return "WARMING UP" if self.warmup else LABELS[self.level]

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["label"] = self.label
        for k in ("score", "ewm", "short_mean", "long_mean", "adjusted", "gauge"):
            d[k] = round(d[k], 4)
        return d


# =================================================================== per-printer tracker

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _store_name(printer: str) -> str:
    """A file name for a printer: safe characters only, plus a short hash if anything changed."""
    p = str(printer or "printer")
    safe = _SAFE.sub("_", p).lstrip(".")[:60] or "printer"
    if safe != p:
        safe += "-" + hashlib.sha1(p.encode("utf-8", "replace")).hexdigest()[:6]
    return safe


class FailureTracker:
    """Scores in, level out, for ONE printer.

    Per picture (score p):
        ewm   = p * a + ewm * (1 - a),  a = 2 / (ewm_span + 1)        (first picture: ewm = p)
        short = mean of the last short_window scores of THIS print
        long  = mean of the last long_window scores of this printer, across prints (persisted)
        adjusted(f) = (ewm - long) * sensitivity / f
        failing(f)  = adjusted(f) > threshold_high
                      or (adjusted(f) >= threshold_low and adjusted(f) > (short - long) * short_factor)
        level       = FAILURE if failing(failure_factor) else WARNING if failing(1) else OK
    The first `warmup_frames` pictures of a print never alert (but still feed the baseline).
    """

    # Pictures that look like a failure are only rarely let into the long baseline: otherwise a few
    # hours of one bad print teach the baseline that "spaghetti" is normal, and the NEXT bad print
    # gets no alert for an hour. One in ADMIT_FLAGGED_EVERY still goes in, so a camera that always
    # looks like spaghetti (a bed clip in view) is eventually learned instead of alerting forever.
    ADMIT_FLAGGED_EVERY = 10

    def __init__(self, printer, params=None, store_dir=None, clock=time.time, save_every_s=300.0):
        self.printer = printer
        self.params = params or Params()
        self.store_dir = store_dir
        self._clock = clock
        self._save_every = save_every_s
        self._lock = threading.RLock()
        self._short = collections.deque(maxlen=self.params.short_window)
        self._long = collections.deque(maxlen=self.params.long_window)
        self._ewm = None
        self.frame = 0
        self._dirty = False
        self._flagged = 0
        self._last_save = clock()
        self._warned_io = False
        self.last = None                 # last Verdict
        # The view (turned / cropped how) the baseline was learned from, as View.signature(). None = this
        # printer has no baseline file yet; a file from before this was recorded counts as the untouched picture.
        self._view = None
        self.learning = ""               # why the baseline is being re-learned ("" = it is not)
        self._load()

    # ---- parameters ----------------------------------------------------

    def set_params(self, params: Params):
        """Apply new parameters mid-print (e.g. the owner changed sensitivity); keeps history."""
        with self._lock:
            self.params = params
            if params.short_window != self._short.maxlen:
                self._short = collections.deque(self._short, maxlen=params.short_window)
            if params.long_window != self._long.maxlen:
                self._long = collections.deque(self._long, maxlen=params.long_window)

    # ---- the view the baseline belongs to ----------------------------------

    # After a reset, a baseline needs this many pictures (about an hour at one per 10 s) before the
    # printer screen stops saying LEARNING.
    LEARN_FRAMES = 360

    def set_view(self, signature: str) -> bool:
        """Tell the tracker which view (View.signature()) the pictures now come in. The baseline only means
        something for the view it was learned from: when the view changed (camera turned, watch area
        moved) the baseline is thrown away and learned again. Returns True when it was reset."""
        with self._lock:
            if signature == self._view:
                return False
            if self._view is None:                      # nothing learned yet: just remember the view
                self._view = signature
                self._dirty = True
                return False
            self.reset_baseline("camera view changed", signature)
            return True

    def reset_baseline(self, reason="camera view changed", signature=None):
        """Forget the learned baseline AND this print's smoothing (the next pictures go through the warm-up
        again), and say why in words (`learning`). Written to disk straight away."""
        with self._lock:
            self._long.clear()
            self._short.clear()
            self._ewm = None
            self.frame = 0
            self._flagged = 0
            self.last = None
            self.learning = reason or ""
            if signature is not None:
                self._view = signature
            self._dirty = True
            self._save(force=True)
        log.info("detection baseline for %s reset: %s", self.printer, reason)

    # ---- feeding -------------------------------------------------------

    def start_print(self):
        """A new print: forget the per-print state (smoothing, short window, warm-up counter).
        The long baseline stays - that is the point of it."""
        with self._lock:
            self._save(force=True)
            self._short.clear()
            self._ewm = None
            self.frame = 0
            self.last = None

    def observe(self, score) -> Verdict:
        p = max(0.0, _finite(score))
        prm = self.params
        with self._lock:
            self.frame += 1
            a = 2.0 / (prm.ewm_span + 1)
            self._ewm = p if self._ewm is None else p * a + self._ewm * (1.0 - a)
            self._short.append(p)
            self._long.append(p)
            self._dirty = True
            if self.learning and len(self._long) >= self.LEARN_FRAMES:
                self.learning = ""
            short_mean = sum(self._short) / len(self._short)
            long_mean = sum(self._long) / len(self._long)
            adjusted = (self._ewm - long_mean) * prm.sensitivity
            warm = self.frame <= prm.warmup_frames
            if warm:
                level = LEVEL_OK
            elif self._failing(self._ewm, short_mean, long_mean, 1.0 * prm.failure_factor):
                level = LEVEL_FAILURE
            elif self._failing(self._ewm, short_mean, long_mean, 1.0):
                level = LEVEL_WARNING
            else:
                level = LEVEL_OK
            self.last = Verdict(level, warm, self.frame, p, self._ewm, short_mean, long_mean,
                                adjusted, self._gauge(level, warm, adjusted))
            if level != LEVEL_OK and not warm:               # keep flagged pictures out of the baseline
                self._flagged += 1
                if self._flagged % self.ADMIT_FLAGGED_EVERY:
                    self._long.pop()
            self._save(force=False)
            return self.last

    def _failing(self, ewm, short_mean, long_mean, factor) -> bool:
        prm = self.params
        adj = (ewm - long_mean) * prm.sensitivity / factor
        if adj < prm.threshold_low:
            return False
        if adj > prm.threshold_high:
            return True
        return adj > (short_mean - long_mean) * prm.short_factor

    def _gauge(self, level, warm, adjusted) -> float:
        if warm:
            return 0.0
        if level == LEVEL_FAILURE:
            return 0.85
        if level == LEVEL_WARNING:
            return 0.5
        return min(0.33, max(0.0, adjusted) / self.params.threshold_low / 3.0)

    # ---- baseline persistence -------------------------------------------

    @property
    def baseline_frames(self) -> int:
        return len(self._long)

    @property
    def path(self):
        return os.path.join(self.store_dir, _store_name(self.printer) + ".json") if self.store_dir else None

    def flush(self):
        """Write the baseline now (print finished, shutdown)."""
        with self._lock:
            self._save(force=True)

    def _save(self, force):
        if not self.path or not self._dirty:
            return
        if not force and self._clock() - self._last_save < self._save_every:
            return
        try:
            os.makedirs(self.store_dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"v": 1, "printer": self.printer, "saved": self._clock(),
                           "view": self._view, "learning": self.learning,
                           "long": [round(x, 3) for x in self._long]}, f, separators=(",", ":"))
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_save = self._clock()
        except OSError as e:
            if not self._warned_io:
                self._warned_io = True
                log.warning("could not save the detection baseline for %s: %s", self.printer, e)

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path) as f:
                doc = json.load(f)
            vals = doc["long"]
            clean = [x for x in (_finite(v, None) for v in vals) if x is not None and x >= 0]
            self._long.extend(clean[-self.params.long_window:])
            v = doc.get("view")
            self._view = v if isinstance(v, str) and v else framefix.IDENTITY_SIGNATURE   # older file: untouched pictures
            lr = doc.get("learning")
            self.learning = lr[:80] if isinstance(lr, str) else ""
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("ignoring unreadable detection baseline %s: %s", self.path, e)


# =================================================================== detector client

@dataclasses.dataclass
class ScoreResult:
    ok: bool
    score: float = 0.0
    detections: list = dataclasses.field(default_factory=list)   # [{"conf": f, "box": [cx, cy, w, h]}]
    ms: int = None               # inference time the detector reports
    rtt_ms: int = None           # round trip as the Hub saw it
    code: str = ""               # "" when ok; else unreachable / timeout / unauthorized / busy / ...
    error: str = ""              # a sentence for a human, token-free
    soft: bool = False           # a "try again next tick" failure (busy), not an outage

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):          # a redirect would re-send the bearer token
        return None


def _is_timeout(e) -> bool:
    import socket
    r = getattr(e, "reason", e)
    return isinstance(r, (TimeoutError, socket.timeout))


class DetectorClient:
    """Talks to a detector adapter (detector/adapter/app.py) - or anything with the same
    contract. One request at a time; a second concurrent call answers `busy` immediately.

        POST {url}/v1/score   Content-Type: image/jpeg   Authorization: Bearer <token>
        GET  {url}/healthz
    """

    def __init__(self, url, token="", timeout_s=10.0):
        self.url = (url or "").strip().rstrip("/")
        self.token = token or ""
        self.timeout_s = float(timeout_s)
        self._busy = threading.Lock()
        # Detectors live on the LAN: never go through an HTTP proxy, never follow a redirect.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @property
    def configured(self) -> bool:
        return self.url.startswith(("http://", "https://"))

    def _scrub(self, text) -> str:
        t = " ".join(str(text).split())[:200]
        return t.replace(self.token, "***") if self.token else t

    # ---- scoring --------------------------------------------------------

    def score(self, jpeg: bytes, printer: str = "") -> ScoreResult:
        try:
            if not self.configured:
                return ScoreResult(False, code="bad_config", error="the detector URL is not set (needs http:// or https://)")
            if not jpeg:
                return ScoreResult(False, code="empty_frame", error="no picture to score")
            if not self._busy.acquire(blocking=False):
                return ScoreResult(False, code="busy", soft=True, error="the previous picture is still being scored")
            try:
                return self._post(jpeg, printer)
            finally:
                self._busy.release()
        except Exception as e:           # the contract: never raise
            return ScoreResult(False, code="client_error", error=self._scrub(f"{type(e).__name__}: {e}"))

    def _post(self, jpeg, printer):
        headers = {"Content-Type": "image/jpeg", "User-Agent": "fabrica-hub"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        label = re.sub(r"[^\w .-]", "", str(printer))[:40]
        if label:
            headers["X-Printer"] = label
        req = urllib.request.Request(self.url + "/v1/score", data=bytes(jpeg), headers=headers, method="POST")
        t0 = time.monotonic()
        try:
            with self._opener.open(req, timeout=self.timeout_s) as r:
                body = r.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            return self._http_error(e, t0)
        except (TimeoutError, OSError) as e:
            if _is_timeout(e):
                return ScoreResult(False, code="timeout", error=f"no answer from the detector within {self.timeout_s:.0f} s")
            return ScoreResult(False, code="unreachable", error=self._scrub(f"the detector is not reachable ({getattr(e, 'reason', e)})"))
        rtt = int((time.monotonic() - t0) * 1000)
        return self._parse(body, rtt)

    def _http_error(self, e, t0):
        try:
            data = json.loads(e.read(65536) or b"{}")
        except (ValueError, OSError):
            data = {}
        finally:
            e.close()
        code = str(data.get("error") or f"http_{e.code}") if isinstance(data, dict) else f"http_{e.code}"
        msg = str(data.get("message") or "") if isinstance(data, dict) else ""
        if e.code == 401:
            return ScoreResult(False, code="unauthorized", error="the detector rejected the token")
        if e.code == 503 and code == "busy":
            return ScoreResult(False, code="busy", soft=True, error="the detector is busy")
        return ScoreResult(False, code=code, error=self._scrub(f"the detector answered HTTP {e.code}: {msg or code}"))

    def _parse(self, body, rtt):
        try:
            d = json.loads(body)
            api = d.get("api", API_VERSION)
            score = _finite(d["score"], None)
            if score is None or score < 0:
                raise ValueError("score")
            dets = []
            for x in d.get("detections") or []:
                dets.append({"conf": round(_finite(x.get("conf", x.get("confidence"))), 4),
                             "box": [_finite(v) for v in (x.get("box") or [])][:4]})
            ms = d.get("ms", d.get("inference_ms"))
            ms = int(ms) if isinstance(ms, (int, float)) and math.isfinite(ms) else None
        except (ValueError, KeyError, TypeError, AttributeError):
            return ScoreResult(False, code="bad_response", error="the detector sent an answer this Hub does not understand")
        if isinstance(api, int) and api > API_VERSION:
            return ScoreResult(False, code="api_mismatch", error=f"the detector speaks API {api}; this Hub speaks {API_VERSION} - update the Hub")
        return ScoreResult(True, score=score, detections=dets, ms=ms, rtt_ms=rtt)

    # ---- health ---------------------------------------------------------

    def health(self, timeout_s=5.0) -> dict:
        """{"ok": bool, "status": "ok"|"degraded"|"unreachable"|"bad_config", "info": {...}, "error": str}.
        Never raises. /healthz needs no token, so this does NOT prove the token is right
        (see check_auth)."""
        if not self.configured:
            return {"ok": False, "status": "bad_config", "info": {}, "error": "the detector URL is not set"}
        try:
            req = urllib.request.Request(self.url + "/healthz", headers={"User-Agent": "fabrica-hub"})
            try:
                with self._opener.open(req, timeout=timeout_s) as r:
                    info = json.loads(r.read(65536) or b"{}")
                    return {"ok": True, "status": str(info.get("status", "ok")), "info": info, "error": ""}
            except urllib.error.HTTPError as e:
                try:
                    info = json.loads(e.read(65536) or b"{}")
                except (ValueError, OSError):
                    info = {}
                finally:
                    e.close()
                return {"ok": False, "status": str(info.get("status", "degraded")), "info": info,
                        "error": f"the detector is up but not healthy (HTTP {e.code}, ml_api {info.get('ml_api', '?')})"}
        except (TimeoutError, OSError) as e:
            return {"ok": False, "status": "unreachable", "info": {},
                    "error": self._scrub(f"the detector is not reachable ({getattr(e, 'reason', e)})")}
        except Exception as e:
            return {"ok": False, "status": "unreachable", "info": {}, "error": self._scrub(f"{type(e).__name__}: {e}")}

    def check_auth(self) -> ScoreResult:
        """For a 'Test connection' button: send a 4-byte stub JPEG. A right token is answered
        with anything but 401 (the detector then fails to decode the stub - that is fine)."""
        res = self.score(b"\xff\xd8\xff\xd9", "test")
        if res.code == "unauthorized":
            return res
        if res.ok or res.code not in ("unreachable", "timeout", "bad_config", "client_error"):
            return ScoreResult(True)
        return res


# =================================================================== the loop

class _PS:
    """What the loop remembers about one printer."""

    def __init__(self, tracker):
        self.tracker = tracker
        self.state = None            # last committed state seen while reachable
        self.job = ""
        self.percent = None
        self.next_due = 0.0
        self.last_ts = 0.0           # camera timestamp of the last scored picture
        self.camera = "unknown"      # ok | no_frame | stale | blind | unknown
        self.camera_misses = 0
        self.last_alert = None       # (rank, ts) within this print
        self.alerts = collections.deque(maxlen=10)
        self.verdict = None          # last Verdict.to_dict() with "at"
        self.scored = 0
        self.print_started = None
        self.view = framefix.IDENTITY   # how this printer's pictures are turned / cropped before scoring
        self.prep = "off"            # off (nothing to do) | ok (turned / cropped)
        self.unprepared = False      # the picture should be turned / cropped but this build has no ffmpeg


class DetectionLoop:
    """Poll RUNNING printers that have a camera; score; track; alert.

    Callbacks (the loop imports nothing from the server):
        fleet_fetcher()            -> {printer name: snapshot dict}   (snapshot["state"], ["job"], ["percent"], ["connected"])
        jpeg_fetcher(name)         -> (jpeg_bytes, timestamp) | jpeg_bytes | None
                                      (the Hub: prn.camera().get_frame(8.0), the shared reader)
        on_alert(printer, level, score, jpeg)   level is "warning" or "failure"; score is the
                                      smoothed score clipped to 0..1. Never called in learn-only mode.
        has_camera(name)           -> bool  (default: every printer)
        view_fetcher(name)         -> framefix.View | None   how this printer's camera picture is turned and
                                      cut before it is scored (None / absent: it is scored as it is)

    `tick()` does one pass and never blocks on more than the detector's timeout per picture;
    `start()` runs it on its own daemon thread. All settings can change at runtime via
    `configure(...)`.
    """

    def __init__(self, fleet_fetcher, jpeg_fetcher, on_alert, client=None, *, has_camera=None,
                 store_dir=None, interval_s=10, sensitivity="medium", learn_only=False,
                 cooldown_s=600, max_backoff_s=300, excluded=(), blind_bytes=2500,
                 scale_to_interval=True, clock=time.time, tick_s=2.0, on_detector=None,
                 outage_alert_after=3, view_fetcher=None):
        self._fleet = fleet_fetcher
        self._jpeg = jpeg_fetcher
        self._view_fetcher = view_fetcher
        self._on_alert = on_alert
        # on_detector("down", {"error","code","failures"}) once the detector has failed `outage_alert_after`
        # times in a row (a blip is not an outage), and on_detector("up", {}) when it answers again.
        # Without it a dead detector is only visible in status(): nobody is told prints are unwatched.
        self._on_detector = on_detector
        self._outage_after = max(1, int(outage_alert_after))
        self._outage_told = False
        self._has_camera = has_camera or (lambda name: True)
        self._store_dir = store_dir
        self._clock = clock
        self.tick_s = tick_s
        self.blind_bytes = blind_bytes
        self.scale_to_interval = scale_to_interval
        self.max_backoff_s = max_backoff_s
        self._lock = threading.RLock()
        self._ps = {}
        self._client = client
        self.interval_s = max(1.0, float(interval_s))
        self.sensitivity = sensitivity
        self.learn_only = bool(learn_only)
        self.cooldown_s = float(cooldown_s)
        self.excluded = {str(x).lower() for x in excluded}
        self.params = self._make_params()
        # detector health
        self._fails = 0
        self._down_until = 0.0
        self._det = {"state": "unknown", "error": "", "code": "", "since": None, "last_ok": None,
                     "last_ms": None, "last_rtt_ms": None, "failures": 0, "info": {}}
        self._thread = None
        self._stop = threading.Event()
        self._last_tick = None

    # ---- configuration -------------------------------------------------

    def _make_params(self):
        return Params.for_interval(self.interval_s, self.sensitivity, scale=self.scale_to_interval)

    def configure(self, **kw):
        """Change settings at runtime. Keys: client (None switches detection off), interval_s,
        sensitivity, learn_only, cooldown_s, excluded. Trackers keep their history."""
        with self._lock:
            if "client" in kw:
                self._client = kw["client"]
                self._fails, self._down_until = 0, 0.0
                self._outage_told = False
                self._det.update(state="unknown", error="", code="", failures=0, info={})
            if "interval_s" in kw:
                self.interval_s = max(1.0, _finite(kw["interval_s"], self.interval_s))
            if "sensitivity" in kw:
                self.sensitivity = kw["sensitivity"]
            if "learn_only" in kw:
                self.learn_only = bool(kw["learn_only"])
            if "cooldown_s" in kw:
                self.cooldown_s = max(0.0, _finite(kw["cooldown_s"], self.cooldown_s))
            if "excluded" in kw:
                self.excluded = {str(x).lower() for x in kw["excluded"]}
            self.params = self._make_params()
            for ps in self._ps.values():
                ps.tracker.set_params(self.params)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ---- lifecycle -----------------------------------------------------

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="detection", daemon=True)
            self._thread.start()
            return self._thread

    def stop(self, join=2.0):
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(join)
        with self._lock:
            for ps in self._ps.values():
                ps.tracker.flush()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:                    # the loop must outlive any bug
                log.warning("detection tick failed: %s: %s", type(e).__name__, e)
            self._stop.wait(self.tick_s)

    # ---- one pass ------------------------------------------------------

    def tick(self, now=None):
        now = self._clock() if now is None else now
        self._last_tick = now
        with self._lock:
            client = self._client
            interval = self.interval_s
        try:
            fleet = self._fleet() or {}
        except Exception as e:
            log.warning("could not read the fleet: %s", e)
            return
        for name, snap in fleet.items():
            self._observe_state(name, snap or {}, now)
        with self._lock:
            for gone in [n for n in self._ps if n not in fleet]:
                self._ps.pop(gone).tracker.flush()
        if client is None:
            return
        if self._down_until > now:                    # backing off: do nothing at all
            return
        if self._fails and not self._probe(client, now):
            return
        for name, snap in fleet.items():
            if self._stop.is_set():
                return
            if not self._eligible(name, snap or {}):
                continue
            ps = self._ps[name]
            if now < ps.next_due:
                continue
            ps.next_due = now + interval
            try:
                self._poll_one(client, name, ps, now)
            except Exception as e:                    # one printer's bug must not stop the others
                log.warning("detection for %s failed: %s: %s", name, type(e).__name__, e)
            if self._down_until > now:                # the detector just failed: stop this pass
                return

    def _eligible(self, name, snap) -> bool:
        if str(snap.get("state", "")).upper() != "RUNNING" or snap.get("connected") is False:
            return False
        if name.lower() in self.excluded:
            return False
        try:
            return bool(self._has_camera(name))
        except Exception:
            return False

    # ---- print boundaries ------------------------------------------------

    def _ps_for(self, name) -> _PS:
        with self._lock:
            ps = self._ps.get(name)
            if ps is None:
                ps = self._ps[name] = _PS(FailureTracker(name, self.params, self._store_dir, self._clock))
            return ps

    def _observe_state(self, name, snap, now):
        state = str(snap.get("state", "")).upper()
        if state in ("", "UNKNOWN", "DISCONNECTED") or snap.get("connected") is False:
            self._ps_for(name)                          # make sure it appears in status()
            return                                      # no information: keep what we believed
        ps = self._ps_for(name)
        job = str(snap.get("job") or "")
        pct = snap.get("percent")
        pct = float(pct) if isinstance(pct, (int, float)) else None
        prev = ps.state
        if state == "RUNNING":
            new_print = (prev not in ("RUNNING", "PAUSE", "PAUSED")
                         or (job and ps.job and job != ps.job)
                         or (pct is not None and ps.percent is not None and pct < ps.percent - 10))
            if new_print:
                self._new_print(name, ps, now)
            ps.job = job or ps.job
        elif prev in ("RUNNING", "PAUSE", "PAUSED") and state not in ("PAUSE", "PAUSED"):
            ps.tracker.flush()                           # the print ended
        if state in ("PAUSE", "PAUSED") and job:
            ps.job = job
        ps.percent = pct if pct is not None else ps.percent
        ps.state = state
        self._sync_view(name, ps)

    def _view_for(self, name):
        """framefix.View in effect for a printer, or None when unknown (no camera, no answer)."""
        if self._view_fetcher is None:
            return None
        try:
            v = self._view_fetcher(name)
        except Exception as e:
            log.info("camera view for %s unavailable: %s", name, e)
            return None
        return v if isinstance(v, framefix.View) else None

    def _sync_view(self, name, ps):
        """Keep the printer's view up to date. When it differs from the one the learned baseline was made
        from (camera turned, watch area moved) the baseline is reset - it would be compared against
        pictures that look different - and the printer screen says LEARNING."""
        wanted = self._view_for(name)
        if wanted is None:
            return
        view = framefix.effective(wanted)          # without ffmpeg nothing is turned: the baseline stays as it is
        ps.view = view
        ps.unprepared = view is not wanted and not wanted.identity
        if ps.tracker.set_view(view.signature()):
            ps.verdict, ps.scored, ps.last_alert = None, 0, None

    def _new_print(self, name, ps, now):
        ps.tracker.start_print()
        ps.last_alert = None
        ps.last_ts = 0.0
        ps.camera, ps.camera_misses = "unknown", 0
        ps.verdict = None
        ps.scored = 0
        ps.percent = None
        ps.print_started = now
        ps.next_due = now                                # first picture right away

    # ---- polling one printer ------------------------------------------------

    def _fetch_jpeg(self, name):
        got = self._jpeg(name)
        if isinstance(got, (tuple, list)):
            return (got[0], float(got[1]) if len(got) > 1 and got[1] else None) if got else (None, None)
        return got, None

    def _poll_one(self, client, name, ps, now):
        try:
            jpeg, ts = self._fetch_jpeg(name)
        except Exception as e:
            log.info("camera for %s failed: %s", name, e)
            jpeg, ts = None, None
        if not jpeg:
            ps.camera, ps.camera_misses = "no_frame", ps.camera_misses + 1
            return
        if ts is not None and ts <= ps.last_ts:
            ps.camera, ps.camera_misses = "stale", ps.camera_misses + 1     # same picture again
            return
        if self.blind_bytes and len(jpeg) < self.blind_bytes:
            # A tiny JPEG is almost always a black or blank frame (chamber light off). Scoring it
            # would only drag the baseline toward zero, so it is skipped and reported as "can't see".
            ps.camera, ps.camera_misses = "blind", ps.camera_misses + 1
            if ts is not None:
                ps.last_ts = ts
            return
        self._sync_view(name, ps)
        prepared = framefix.prepare(jpeg, ps.view)
        ps.prep = {framefix.STATUS_SAME: "off", framefix.STATUS_DONE: "ok"}.get(prepared.status, prepared.status)
        if prepared.status == framefix.STATUS_FAILED:
            # Scoring the untouched picture would feed the baseline a view it was not learned from.
            ps.camera, ps.camera_misses = "unprepared", ps.camera_misses + 1
            if ts is not None:
                ps.last_ts = ts
            return
        res = client.score(prepared.jpeg, name)
        if not res.ok:
            if res.soft:
                ps.next_due = now + 1.0
            else:
                self._detector_failed(res, now)
            return
        self._detector_ok(res, now)
        ps.camera, ps.camera_misses = "ok", 0
        if ts is not None:
            ps.last_ts = ts
        v = ps.tracker.observe(res.score)
        ps.scored += 1
        ps.verdict = dict(v.to_dict(), at=now, detections=len(res.detections))
        if self.learn_only:
            self._learn_log(name, v, res, now)
            return
        self._maybe_alert(name, ps, v, jpeg, now)

    # ---- detector health -----------------------------------------------------

    def _detector_ok(self, res, now):
        with self._lock:
            if self._det["state"] != "up":
                self._det.update(state="up", since=now)
            self._det.update(error="", code="", last_ok=now, last_ms=res.ms, last_rtt_ms=res.rtt_ms)
            self._fails = 0
            self._down_until = 0.0
            self._det["failures"] = 0
            was_told, self._outage_told = self._outage_told, False
        if was_told:
            log.warning("detector is answering again")
            self._tell_detector("up", {})

    def _detector_failed(self, res, now):
        with self._lock:
            self._fails += 1
            delay = min(self.max_backoff_s, self.interval_s * (2 ** (self._fails - 1)))
            self._down_until = now + delay
            was = self._det["state"]
            self._det.update(state="down", error=res.error, code=res.code, failures=self._fails,
                             since=self._det["since"] if was == "down" else now)
        if was != "down":
            log.warning("detector unreachable: %s", res.error)
        tell = False
        with self._lock:
            if self._fails >= self._outage_after and not self._outage_told:
                self._outage_told = tell = True
        if tell:
            self._tell_detector("down", {"error": res.error, "code": res.code, "failures": self._fails})

    def _tell_detector(self, state, info):
        if self._on_detector is None:
            return
        try:
            self._on_detector(state, info)
        except Exception as e:                       # a notification problem must never stop scoring
            log.warning("detector-status hook failed: %s", e)

    def _probe(self, client, now) -> bool:
        """After a failure: ask /healthz before spending a camera frame. True = go ahead."""
        h = client.health()
        if h["ok"]:
            with self._lock:
                self._det["info"] = h.get("info", {})
            return True
        self._detector_failed(ScoreResult(False, code=h["status"], error=h["error"]), now)
        return False

    # ---- alerts -----------------------------------------------------------------

    def _maybe_alert(self, name, ps, v, jpeg, now):
        if v.warmup or v.level == LEVEL_OK:
            return
        rank = _RANK[v.level]
        last = ps.last_alert
        if last is not None and rank <= last[0] and now - last[1] < self.cooldown_s:
            return                         # same or lower level inside the cooldown: stay quiet
        ps.last_alert = (rank, now)
        ps.alerts.append({"at": now, "level": v.level, "ewm": round(v.ewm, 3)})
        score = min(1.0, round(v.ewm, 3))
        try:
            # the owner gets the picture the way the dashboard shows it (upright, whole), not the raw camera frame
            shown = framefix.prepare(jpeg, ps.view.without_area())
            self._on_alert(name, v.level, score, shown.jpeg if shown.status == framefix.STATUS_DONE else jpeg)
        except Exception as e:
            log.warning("alert hook failed for %s: %s", name, e)

    def _learn_log(self, name, v, res, now):
        log.info("[learn] %s frame %d score=%.3f ewm=%.3f long=%.3f would-be=%s",
                 name, v.frame, v.score, v.ewm, v.long_mean, "warmup" if v.warmup else v.level)
        if not self._store_dir:
            return
        try:
            os.makedirs(self._store_dir, exist_ok=True)
            path = os.path.join(self._store_dir, "scores.jsonl")
            if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
                os.replace(path, path + ".1")
            with open(path, "a") as f:
                f.write(json.dumps({"t": round(now, 1), "printer": name, "frame": v.frame,
                                    "score": round(v.score, 4), "ewm": round(v.ewm, 4),
                                    "long": round(v.long_mean, 4), "level": v.level,
                                    "warmup": v.warmup}) + "\n")
        except OSError:
            pass

    # ---- on-demand + status ------------------------------------------------------

    def score_once(self, printer) -> dict:
        """One-off score of the printer's current camera picture (the `score_snapshot` MCP tool).
        Does NOT touch the tracker or raise alerts. Never raises."""
        client = self._client
        if client is None:
            return {"ok": False, "printer": printer, "error": "detection is off"}
        if not self._has_camera(printer):
            return {"ok": False, "printer": printer, "error": "this printer has no camera"}
        try:
            jpeg, _ts = self._fetch_jpeg(printer)
        except Exception as e:
            return {"ok": False, "printer": printer, "error": f"camera error: {type(e).__name__}"}
        if not jpeg:
            return {"ok": False, "printer": printer, "error": "no camera picture available"}
        wanted = self._view_for(printer) or framefix.IDENTITY
        view = framefix.effective(wanted)
        prepared = framefix.prepare(jpeg, view)
        if prepared.status == framefix.STATUS_FAILED:
            return {"ok": False, "printer": printer, "error": "the camera picture could not be prepared: " + prepared.error}
        res = client.score(prepared.jpeg, printer)
        if not res.ok:
            return {"ok": False, "printer": printer, "error": res.error, "code": res.code}
        out = {"ok": True, "printer": printer, "score": round(res.score, 4), "detections": res.detections,
               "inference_ms": res.ms, "rtt_ms": res.rtt_ms,
               "note": "raw score of this one picture; alerts use the smoothed per-print score"}
        if view is not wanted and not wanted.identity:
            out["picture_note"] = framefix.note()
        return out

    def status(self) -> dict:
        """Everything the dashboard / the `detection_status` MCP tool needs, JSON-safe.
        `summary` is a plain sentence; a down detector is never reported as clear."""
        now = self._clock()
        with self._lock:
            det = dict(self._det)
            if det["state"] == "down":
                det["retry_in_s"] = max(0, round(self._down_until - now))
            printers = {}
            for name, ps in self._ps.items():
                watching = ps.state == "RUNNING" and name.lower() not in self.excluded
                printers[name] = {
                    "watching": bool(watching and self._client is not None),
                    "excluded": name.lower() in self.excluded,
                    "state": ps.state, "camera": ps.camera, "frames_scored": ps.scored,
                    "baseline_frames": ps.tracker.baseline_frames,
                    "learning": ps.tracker.learning,
                    "picture": dict(ps.view.as_dict(), prep=ps.prep,
                                    note=framefix.note() if ps.unprepared else ""),
                    "verdict": ps.verdict, "alerts": list(ps.alerts),
                    "detector_down": det["state"] == "down",
                }
            enabled = self._client is not None
            mode = "off" if not enabled else ("learn" if self.learn_only else "alert")
        return {"enabled": enabled, "mode": mode, "interval_s": self.interval_s,
                "sensitivity": self.sensitivity, "cooldown_s": self.cooldown_s,
                "auto_pause": False, "detector": det, "printers": printers,
                "summary": self._summary(enabled, mode, det, printers)}

    @staticmethod
    def _summary(enabled, mode, det, printers) -> str:
        if not enabled:
            return "Detection is off."
        if det["state"] == "down":
            return f"DETECTOR UNREACHABLE - prints are NOT being watched ({det['error']})."
        bad = [n for n, p in printers.items() if p["verdict"] and p["watching"] and p["verdict"]["level"] != LEVEL_OK
               and not p["verdict"]["warmup"]]
        if bad:
            return ("Possible failure on: " + ", ".join(sorted(bad)) + "."
                    + (" (Learn-only mode: no alert was sent.)" if mode == "learn" else ""))
        n = sum(1 for p in printers.values() if p["watching"])
        base = f"Watching {n} running print{'s' if n != 1 else ''}." if n else "No print is running."
        return base + (" Learn-only mode: scoring and logging, no alerts." if mode == "learn" else "")
