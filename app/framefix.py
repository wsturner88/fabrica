"""Get a camera picture ready for the spaghetti detector: turn it the way the dashboard shows it,
then cut out the printer's watch area. Stdlib only; the pixels are handled by ffmpeg.

Why ffmpeg and nothing else: it is already in the full Hub image (RTSP cameras need it), so this
adds no dependency and no image size. A Lite image (built without ffmpeg) cannot do it - there the
picture goes to the detector as it came and ONE note is logged (see `available()` / `note()`); the
printer screen says so in words. One frame every ~10 s per printer makes one short ffmpeg run per
frame (about 50-100 ms), cheap enough that a resident process is not worth its upkeep.

    View        what to do: flip_h / flip_v / rotate90 (the same flags the dashboard gets as
                `camera_orientation`) and the optional watch `area` (percent of the TURNED picture).
    prepare()   jpeg + View -> Prepared(jpeg, status): "same" (nothing to do), "done", "unavailable"
                (no ffmpeg: the original picture), "failed" (do not score this one).

Order: rotate (90 deg counter-clockwise, as OctoPrint's `rotate90` means) first, then the two flips,
then the crop. The crop box is therefore always drawn on the picture as a person sees it upright.
"""

import dataclasses
import logging
import math
import os
import shutil
import subprocess
import threading

log = logging.getLogger("bambu.framefix")

MAX_IN_BYTES = 8 * 1024 * 1024        # the camera readers' own cap
MAX_OUT_BYTES = 4 * 1024 * 1024       # the detector adapter refuses > 5 MB; stay clear of it
TIMEOUT_S = 6.0                       # one picture; the detection loop is one thread for every printer
MIN_SIDE_PCT = 10.0                   # a watch area smaller than this is a mis-click, not a choice

STATUS_SAME, STATUS_DONE, STATUS_UNAVAILABLE, STATUS_FAILED = "same", "done", "unavailable", "failed"


# =================================================================== the watch area

def clean_area(value):
    """A watch area -> {"x","y","w","h"} in percent of the (turned) picture, rounded to 0.1, or None for
    the whole picture. Accepts that dict, a list [x, y, w, h] or the text "x,y,w,h"; None / "" / {} /
    a box covering everything mean "the whole picture". Raises ValueError with a plain sentence when it
    does not make sense."""
    if value is None or value == "" or value == {} or value == []:
        return None
    try:
        if isinstance(value, dict):
            nums = [value[k] for k in ("x", "y", "w", "h")]
        elif isinstance(value, str):
            nums = [p for p in value.replace(";", ",").replace(" ", ",").split(",") if p != ""]
        elif isinstance(value, (list, tuple)) and len(value) == 4:
            nums = list(value)
        else:
            raise TypeError
        if any(isinstance(n, bool) for n in nums) or len(nums) != 4:
            raise TypeError
        x, y, w, h = [float(n) for n in nums]
    except (KeyError, TypeError, ValueError):
        raise ValueError('The watch area needs four numbers in percent: {"x": 10, "y": 20, "w": 60, "h": 50} '
                         "(left, top, width, height of the picture as it is shown upright).") from None
    if not all(math.isfinite(n) for n in (x, y, w, h)):
        raise ValueError("The watch area numbers must be real numbers.")
    x, y, w, h = (round(n, 1) for n in (x, y, w, h))
    if x < 0 or y < 0 or x > 100 or y > 100:
        raise ValueError("The watch area must lie inside the picture (left and top between 0 and 100 percent).")
    if w < MIN_SIDE_PCT or h < MIN_SIDE_PCT:
        raise ValueError(f"The watch area is too small: make it at least {MIN_SIDE_PCT:g} percent wide and high.")
    if x + w > 100.05 or y + h > 100.05:
        raise ValueError("The watch area runs past the edge of the picture.")
    w, h = min(w, round(100 - x, 1)), min(h, round(100 - y, 1))
    if x == 0 and y == 0 and w >= 100 and h >= 100:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


@dataclasses.dataclass(frozen=True)
class View:
    flip_h: bool = False
    flip_v: bool = False
    rotate90: bool = False
    area: tuple = None                 # (x, y, w, h) percent, or None = the whole picture

    @classmethod
    def of(cls, orientation=None, area=None) -> "View":
        """From a driver's camera_info() dict (or None) and a stored area (dict / None)."""
        o = orientation if isinstance(orientation, dict) else {}
        try:
            a = clean_area(area)
        except ValueError:
            a = None                    # a bad stored value must never stop detection: watch everything
        return cls(bool(o.get("flip_h")), bool(o.get("flip_v")), bool(o.get("rotate90")),
                   (a["x"], a["y"], a["w"], a["h"]) if a else None)

    @property
    def identity(self) -> bool:
        return not (self.flip_h or self.flip_v or self.rotate90 or self.area)

    def signature(self) -> str:
        """Stable text for 'is this the same view as before' (what the learned baseline was made from)."""
        a = ",".join(f"{v:g}" for v in self.area) if self.area else "full"
        return f"h{int(self.flip_h)}v{int(self.flip_v)}r{int(self.rotate90)}|{a}"

    def without_area(self) -> "View":
        return dataclasses.replace(self, area=None)

    def as_dict(self) -> dict:
        return {"flip_h": self.flip_h, "flip_v": self.flip_v, "rotate90": self.rotate90,
                "area": ({"x": self.area[0], "y": self.area[1], "w": self.area[2], "h": self.area[3]}
                         if self.area else None)}


IDENTITY = View()
IDENTITY_SIGNATURE = IDENTITY.signature()


# =================================================================== ffmpeg

def find_ffmpeg():
    """Path of the ffmpeg binary or None. Looked up every time (cheap), like the camera code does."""
    return shutil.which(os.environ.get("FABRICA_FFMPEG") or "ffmpeg")


def available() -> bool:
    return find_ffmpeg() is not None


_told = set()
_told_lock = threading.Lock()


def _once(key, message, level=logging.WARNING):
    with _told_lock:
        if key in _told:
            return
        _told.add(key)
    log.log(level, message)


def note() -> str:
    """A sentence for the printer screen when pictures cannot be turned / cropped here."""
    return "this Hub build has no ffmpeg, so the camera picture is sent to the detector as it is"


def filter_chain(view: View) -> str:
    """The ffmpeg -vf text for a View ('' = nothing to do). Numbers only, no shell: it is one argv item."""
    parts = []
    if view.rotate90:
        parts.append("transpose=2")                     # 90 degrees counter-clockwise
    if view.flip_h:
        parts.append("hflip")
    if view.flip_v:
        parts.append("vflip")
    if view.area:
        x, y, w, h = (v / 100.0 for v in view.area)
        parts.append(f"crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f}")
    return ",".join(parts)


def _command(exe, vf):
    return [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-noautorotate",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-i", "pipe:0",
            "-vf", vf, "-frames:v", "1", "-pix_fmt", "yuvj420p", "-q:v", "3",
            "-fs", str(MAX_OUT_BYTES), "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]


@dataclasses.dataclass
class Prepared:
    jpeg: bytes
    status: str                    # same | done | unavailable | failed
    error: str = ""


def prepare(jpeg: bytes, view: View, timeout=TIMEOUT_S) -> Prepared:
    """Turn and crop one JPEG. Never raises. See the module text for the four statuses."""
    if view is None or view.identity:
        return Prepared(jpeg, STATUS_SAME)
    if not jpeg or jpeg[:2] != b"\xff\xd8":
        return Prepared(jpeg, STATUS_FAILED, "not a JPEG picture")
    if len(jpeg) > MAX_IN_BYTES:
        return Prepared(jpeg, STATUS_FAILED, "the picture is too large to prepare")
    exe = find_ffmpeg()
    if exe is None:
        _once("no-ffmpeg", "no ffmpeg in this image: camera pictures go to the detector as they come "
                           "(not turned upright, no watch area). Use the full Hub image for that.")
        return Prepared(jpeg, STATUS_UNAVAILABLE, note())
    try:
        r = subprocess.run(_command(exe, filter_chain(view)), input=bytes(jpeg), capture_output=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        _once("timeout", "ffmpeg took too long to prepare a camera picture; pictures are skipped until it recovers")
        return Prepared(jpeg, STATUS_FAILED, "preparing the picture took too long")
    except OSError as e:
        _once("oserror", f"could not run ffmpeg to prepare camera pictures: {type(e).__name__}")
        return Prepared(jpeg, STATUS_FAILED, "ffmpeg could not be started")
    out = r.stdout or b""
    if r.returncode != 0 or out[:2] != b"\xff\xd8" or out[-2:] != b"\xff\xd9" or len(out) >= MAX_OUT_BYTES:
        msg = " ".join((r.stderr or b"").decode("utf-8", "replace").split())[:160]
        _once("rc:" + msg[:40], f"ffmpeg could not prepare a camera picture: {msg or 'no usable output'}")
        return Prepared(jpeg, STATUS_FAILED, "the picture could not be prepared")
    return Prepared(out, STATUS_DONE)


def effective(view: View) -> View:
    """The View that will really be applied here: without ffmpeg nothing is (so the learned baseline,
    which remembers the view it was made from, is not reset for a transform that never happened)."""
    return view if (view is None or view.identity or available()) else IDENTITY
