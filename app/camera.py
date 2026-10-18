"""Chamber-camera reader for Bambu P1/A1-series printers.

Protocol (TLS on port 6000, self-signed certificate, no verification):
  1. Connect, then send an 80-byte auth packet:
       <IIII  0x40, 0x3000, 0, 0      (16-byte header)
       "bblp"        padded to 32 bytes
       access code   padded to 32 bytes
  2. The printer then streams frames forever. Each frame is a 16-byte header
     whose first 4 bytes are a little-endian uint32 payload size, followed by
     that many bytes of JPEG (starts FF D8).

OctoPrint printers with a webcam use the same shared-reader machinery
(PollingCameraStream): instead of a TLS socket, one background thread polls the
webcam's JPEG snapshot URL every ~1.5 s while there is demand.

Design rules (the printer's camera tolerates very few clients):
  * ONE connection per printer, shared by every viewer / request / MCP call.
  * Started lazily the first time somebody wants a frame (touch / get_frame).
  * Kept alive while there is demand: each request "touches" the stream, and
    each open MJPEG viewer counts as a viewer. With no viewers and no touch for
    `idle_timeout` seconds the reader thread closes the socket and exits.
  * Reconnects with exponential backoff on any error; nothing here raises into
    the caller, and nothing here touches the MQTT/FTPS paths.
"""

import asyncio
import base64
import collections
import logging
import os
import re
import select
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("bambu.camera")

CAMERA_PORT = 6000
AUTH_MAGIC = (0x40, 0x3000, 0, 0)
FRAME_HEADER_LEN = 16
MAX_FRAME_BYTES = 8 * 1024 * 1024      # sanity cap; real frames are 50-300 KB
IDLE_TIMEOUT = 30.0                    # seconds with no demand before disconnect
FRESH_SECONDS = 10.0                   # a cached frame older than this is "stale"
STALL_SECONDS = 15.0                   # no bytes at all for this long = dead link
BACKOFF_MIN, BACKOFF_MAX = 1.0, 30.0
POLL_INTERVAL = 1.5                    # OctoPrint webcam: seconds between snapshot fetches


def build_auth_packet(access_code: str) -> bytes:
    """The 80-byte login packet the camera expects right after the TLS handshake."""
    return (struct.pack("<IIII", *AUTH_MAGIC)
            + b"bblp".ljust(32, b"\0")
            + access_code.encode().ljust(32, b"\0"))


class _Done(Exception):
    """Raised inside the reader to leave a session because demand has stopped."""


class _SharedReader:
    """Shared, lazily started, auto-closing frame source for one printer.
    Subclasses implement `_session()` (run until error or until demand stops,
    calling `_store_frame` for every JPEG)."""

    def __init__(self, label="camera", idle_timeout=IDLE_TIMEOUT):
        self.label = label
        self.idle_timeout = idle_timeout
        self._lock = threading.Lock()
        self._thread = None
        self._stopped = False
        self._last_demand = 0.0
        self._viewers = 0
        self._frame = None          # latest JPEG bytes
        self._frame_ts = 0.0        # wall-clock time it arrived
        self._seq = 0               # increments with every frame
        self.connections_opened = 0  # diagnostics / tests
        self.connected = False
        self.last_error = None       # plain-language reason the camera has no picture (never holds credentials)

    # ---- demand ---------------------------------------------------------

    def touch(self):
        """Register demand for frames; start the reader thread if it isn't running."""
        with self._lock:
            self._last_demand = time.monotonic()
            if self._stopped:
                return
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name=f"camera-{self.label}", daemon=True)
                self._thread.start()

    def add_viewer(self):
        """A long-lived consumer (MJPEG stream) is attached; counts as demand."""
        with self._lock:
            self._viewers += 1
        self.touch()

    def remove_viewer(self):
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            self._last_demand = time.monotonic()

    def stop(self):
        """Permanently stop (server shutdown / tests)."""
        with self._lock:
            self._stopped = True

    def _idle(self) -> bool:
        with self._lock:
            return (self._viewers == 0
                    and time.monotonic() - self._last_demand > self.idle_timeout)

    # ---- consumers ------------------------------------------------------

    def latest(self):
        """(seq, jpeg, ts) of the newest cached frame, or (0, None, 0.0)."""
        with self._lock:
            return self._seq, self._frame, self._frame_ts

    def fresh_frame(self):
        """Newest frame if it is recent enough to show, else None."""
        _, jpeg, ts = self.latest()
        if jpeg is not None and time.time() - ts <= FRESH_SECONDS:
            return jpeg, ts
        return None

    def get_frame(self, timeout=8.0):
        """Blocking: (jpeg, ts) of a fresh frame, or None if none arrives in time."""
        self.touch()
        deadline = time.monotonic() + timeout
        while True:
            got = self.fresh_frame()
            if got:
                return got
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    async def aget_frame(self, timeout=8.0):
        """Async twin of get_frame — polls without parking an executor thread."""
        self.touch()
        deadline = time.monotonic() + timeout
        while True:
            got = self.fresh_frame()
            if got:
                return got
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.05)

    # ---- reader thread --------------------------------------------------

    def _run(self):
        backoff = BACKOFF_MIN
        while True:
            seq0 = self.latest()[0]
            try:
                self._session()
            except _Done:
                pass
            except Exception as e:  # never let a camera problem escape the thread
                log.info("camera %s: %s: %s", self.label, type(e).__name__, e)
            self.connected = False
            got_frames = self.latest()[0] != seq0
            if self._idle_or_stopped():
                if self._retire():
                    return
                continue
            backoff = BACKOFF_MIN if got_frames else min(backoff * 2, BACKOFF_MAX)
            end = time.monotonic() + backoff
            while time.monotonic() < end and not self._idle_or_stopped():
                time.sleep(0.25)
            if self._idle_or_stopped() and self._retire():
                return

    def _idle_or_stopped(self) -> bool:
        return self._stopped or self._idle()

    def _retire(self) -> bool:
        """Give up the thread slot unless demand arrived in the meantime."""
        with self._lock:
            still_idle = self._stopped or (
                self._viewers == 0
                and time.monotonic() - self._last_demand > self.idle_timeout)
            if still_idle:
                self._thread = None
                self._frame = None    # don't serve a frame from before the camera went idle
                return True
            return False

    def _store_frame(self, jpeg: bytes):
        with self._lock:
            self._frame, self._frame_ts = jpeg, time.time()
            self._seq += 1
        self.last_error = None

    def _session(self):
        raise NotImplementedError


class CameraStream(_SharedReader):
    """Bambu P1/A1 chamber camera: TLS :6000 JPEG stream (see module docstring)."""

    def __init__(self, host, access_code, port=CAMERA_PORT, label="camera",
                 idle_timeout=IDLE_TIMEOUT, connect_timeout=8.0):
        super().__init__(label, idle_timeout)
        self.host, self.port = host, port
        self._access_code = access_code
        self.connect_timeout = connect_timeout

    def _session(self):
        """One connection; runs until an error or until demand stops."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # printer cert is self-signed; LAN + access code is the auth
        raw = socket.create_connection((self.host, self.port), self.connect_timeout)
        sock = None
        try:
            sock = ctx.wrap_socket(raw, server_hostname=self.host)
            self.connections_opened += 1
            sock.sendall(build_auth_packet(self._access_code))
            sock.settimeout(1.0)          # short reads so idle/stop is noticed promptly
            self.connected = True
            while True:
                hdr = self._recv_exact(sock, FRAME_HEADER_LEN)
                size = struct.unpack_from("<I", hdr, 0)[0]
                if size <= 0 or size > MAX_FRAME_BYTES:
                    raise ValueError(f"implausible frame size {size}")
                jpeg = self._recv_exact(sock, size)
                if jpeg[:2] != b"\xff\xd8":
                    raise ValueError("frame is not a JPEG")
                self._store_frame(jpeg)
        finally:
            for s in (sock, raw):
                try:
                    if s is not None:
                        s.close()
                except Exception:
                    pass
            self.connected = False

    def _recv_exact(self, sock, n) -> bytes:
        buf = bytearray()
        last_rx = time.monotonic()
        while len(buf) < n:
            if self._idle_or_stopped():
                raise _Done()
            try:
                chunk = sock.recv(min(65536, n - len(buf)))
            except (socket.timeout, ssl.SSLWantReadError):
                if time.monotonic() - last_rx > STALL_SECONDS:
                    raise ConnectionError("camera stalled")
                continue
            if not chunk:
                raise ConnectionError("camera closed the connection")
            buf += chunk
            last_rx = time.monotonic()
        return bytes(buf)


class PollingCameraStream(_SharedReader):
    """OctoPrint webcam: poll a JPEG snapshot URL while somebody is watching.

    `url_fn` returns the current snapshot URL (it may change if the printer's
    webcam settings are re-discovered). One poller per printer serves every
    viewer; with no demand for `idle_timeout` seconds it stops polling."""

    def __init__(self, url_fn, label="camera", interval=POLL_INTERVAL,
                 idle_timeout=IDLE_TIMEOUT, fetch_timeout=8.0):
        super().__init__(label, idle_timeout)
        self._url_fn = url_fn
        self.interval = interval
        self.fetch_timeout = fetch_timeout
        self.fetches = 0   # diagnostics / tests

    def _session(self):
        while True:
            if self._idle_or_stopped():
                raise _Done()
            url = self._url_fn()
            if not url:
                raise ConnectionError("no webcam snapshot URL")
            self._store_frame(fetch_jpeg(url, self.fetch_timeout))
            self.fetches += 1
            self.connected = True
            end = time.monotonic() + self.interval
            while time.monotonic() < end:
                if self._idle_or_stopped():
                    raise _Done()
                time.sleep(0.1)


def fetch_jpeg(url: str, timeout: float = 8.0) -> bytes:
    """GET `url` and return its body, which must be a JPEG (raises otherwise)."""
    req = urllib.request.Request(url, headers={"User-Agent": "fabrica-camera"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_FRAME_BYTES + 1)
    if len(data) > MAX_FRAME_BYTES or data[:2] != b"\xff\xd8":
        raise ValueError("webcam snapshot is not a JPEG")
    return data


# ====================================================================== per-printer camera address
# Any printer can carry an optional `camera_url`: rtsp:// / rtsps:// (read with ffmpeg) or an
# http(s):// JPEG snapshot URL. When set it replaces the driver's own camera. The URL may embed
# `user:pass@` - that is a credential: it is shown only as `rtsp://***@host/path`, and never reaches
# a log line, an error message or an API answer (see redact_url / plain_url / redact_text).

CAMERA_SCHEMES = ("rtsp", "rtsps", "http", "https")
NO_FFMPEG_MSG = "RTSP cameras need the full image (ffmpeg)"
_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f\"'`<>\\^{}|]")
_USERINFO_ANY = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s]*@")


class CameraUrlError(ValueError):
    """A camera address that is not acceptable (the message is safe to show)."""


def validate_camera_url(value) -> str:
    """Clean + validate a camera address; '' stays ''. Raises CameraUrlError. The result is only ever
    passed to ffmpeg / urllib as ONE argument (never through a shell)."""
    v = (value or "").strip() if isinstance(value, str) else ""
    if not v:
        return ""
    if len(v) > 600:
        raise CameraUrlError("The camera address is too long.")
    if _BAD_CHARS.search(v):
        raise CameraUrlError("The camera address contains characters that are not allowed (spaces, quotes, "
                             "brackets or backslashes). Percent-encode them, e.g. an @ in a password as %40.")
    try:
        u = urllib.parse.urlsplit(v)
        host, _port = u.hostname, u.port
    except ValueError:
        raise CameraUrlError("That camera address is not valid.") from None
    if u.scheme.lower() not in CAMERA_SCHEMES or not host:
        raise CameraUrlError("The camera address must look like rtsp://192.168.1.60/live (rtsp, rtsps, "
                             "http or https, then the camera's address).")
    if not re.fullmatch(r"[A-Za-z0-9._:\-\[\]%]+", host):
        raise CameraUrlError("The camera's host name has characters that are not allowed.")
    if u.username == "***":
        raise CameraUrlError("Enter the whole camera address again, including the user name and password.")
    return v


def _split(url):
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{u.port}" if u.port else "")
    return u, netloc


def plain_url(url) -> str:
    """The address without user:pass - for messages (`rtsp://192.168.1.60/live`)."""
    try:
        u, netloc = _split(url)
    except Exception:
        return redact_text(str(url))
    return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, ""))


def redact_url(url) -> str:
    """For API output / forms: `rtsp://***@host/path` when the address carries a login, else unchanged."""
    if not url:
        return ""
    try:
        u, netloc = _split(url)
        if "@" in u.netloc:
            netloc = "***@" + netloc
        return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, ""))
    except Exception:
        return redact_text(str(url))


def redact_text(text, url=None) -> str:
    """Free text (an ffmpeg complaint, an exception) with every login removed, including the exact
    user / password of `url` even when ffmpeg printed them without the scheme."""
    t = _USERINFO_ANY.sub(r"\1", str(text))
    if url:
        try:
            u = urllib.parse.urlsplit(url)
            for bit in (u.password, u.username, urllib.parse.unquote(u.password or ""),
                        urllib.parse.unquote(u.username or "")):
                if bit and len(bit) >= 3:
                    t = t.replace(bit, "***")
        except ValueError:
            pass
    return t


_FFMPEG_CACHE = {}


def find_ffmpeg():
    """Path of the ffmpeg binary or None. Looked up every time (cheap) so installing it needs no restart."""
    return shutil.which(os.environ.get("FABRICA_FFMPEG") or "ffmpeg")


def _ffmpeg_cmd(exe, url, one_frame=False):
    cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if url.lower().startswith(("rtsp://", "rtsps://")):
        cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000"]     # socket I/O timeout, microseconds
    cmd += ["-analyzeduration", "2000000", "-probesize", "1000000", "-i", url, "-an", "-sn"]
    if one_frame:
        cmd += ["-frames:v", "1", "-vf", "scale='min(1280,iw)':-2"]
    else:
        cmd += ["-vf", "fps=2,scale='min(1280,iw)':-2"]
    return cmd + ["-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "-"]


def _ffmpeg_reason(stderr_text, url) -> str:
    """Plain-language reason from ffmpeg's stderr (login removed)."""
    low = (stderr_text or "").lower()
    where = plain_url(url)
    if "401" in low or "unauthorized" in low or "authorization" in low:
        return f"camera rejected the user name or password at {where}"
    return f"camera not reachable at {where}"


class JpegSplitter:
    """Cuts a byte stream of back-to-back JPEGs into frames (SOI ff d8 ... EOI ff d9)."""

    def __init__(self, max_frame=MAX_FRAME_BYTES):
        self.buf = bytearray()
        self.max_frame = max_frame

    def feed(self, data: bytes):
        self.buf += data
        out = []
        while True:
            start = self.buf.find(b"\xff\xd8")
            if start < 0:
                self.buf.clear()          # nothing resembling a frame start: drop the noise
                break
            if start:
                del self.buf[:start]
            end = self.buf.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buf) > self.max_frame:
                    self.buf.clear()       # runaway "frame": resync on the next SOI
                break
            out.append(bytes(self.buf[:end + 2]))
            del self.buf[:end + 2]
        return out


def _basic_header(url):
    u = urllib.parse.urlsplit(url)
    if u.username is None:
        return {}
    tok = f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}".encode()
    return {"Authorization": "Basic " + base64.b64encode(tok).decode()}


def fetch_snapshot_url(url: str, timeout: float = 8.0) -> bytes:
    """GET an http(s) JPEG snapshot URL; a user:pass@ in it becomes a Basic header (never a log line)."""
    req = urllib.request.Request(plain_url(url), headers={"User-Agent": "fabrica-camera", **_basic_header(url)})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            data = r.read(MAX_FRAME_BYTES + 1)
    except Exception as e:
        raise ConnectionError(f"camera not reachable at {plain_url(url)} ({type(e).__name__})") from None
    if len(data) > MAX_FRAME_BYTES or data[:2] != b"\xff\xd8":
        raise ValueError(f"camera at {plain_url(url)} did not return a JPEG picture")
    return data


class RtspCameraStream(_SharedReader):
    """rtsp(s):// camera: ONE long-lived ffmpeg per printer while somebody is watching, JPEG frames
    (about 2 per second) cut from its stdout. Killed when demand ends; restarted with backoff."""

    def __init__(self, url, label="camera", idle_timeout=IDLE_TIMEOUT):
        super().__init__(label, idle_timeout)
        self._url = url
        self.where = plain_url(url)
        self.proc = None
        self.spawns = 0   # diagnostics / tests

    def _session(self):
        exe = find_ffmpeg()
        if not exe:
            self.last_error = NO_FFMPEG_MSG
            raise ConnectionError(NO_FFMPEG_MSG)
        err_tail = collections.deque(maxlen=8)
        proc = subprocess.Popen(self._cmd(exe), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0, close_fds=True)
        self.proc = proc
        self.spawns += 1
        self.connections_opened += 1

        def drain():
            try:
                for line in iter(proc.stderr.readline, b""):
                    err_tail.append(line.decode("utf-8", "replace").strip())
            except Exception:
                pass
        threading.Thread(target=drain, daemon=True, name=f"ffmpeg-err-{self.label}").start()
        split, last_rx = JpegSplitter(), time.monotonic()
        try:
            fd = proc.stdout.fileno()
            while True:
                if self._idle_or_stopped():
                    raise _Done()
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    if time.monotonic() - last_rx > STALL_SECONDS:
                        raise ConnectionError(f"camera at {self.where} stopped sending pictures")
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    time.sleep(0.2)          # let the stderr drainer finish
                    self.last_error = _ffmpeg_reason(redact_text(" ".join(err_tail), self._url), self._url)
                    raise ConnectionError(self.last_error)
                last_rx = time.monotonic()
                for jpeg in split.feed(chunk):
                    self._store_frame(jpeg)
                    self.connected = True
        except _Done:
            raise
        except Exception as e:
            if not self.last_error:
                self.last_error = f"camera not reachable at {self.where}"
            raise ConnectionError(redact_text(e, self._url)) from None
        finally:
            self.connected = False
            self._kill(proc)

    def _cmd(self, exe):
        return _ffmpeg_cmd(exe, self._url)

    @staticmethod
    def _kill(proc):
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(2)
            for f in (proc.stdout, proc.stderr):
                try:
                    f.close()
                except Exception:
                    pass
        except Exception:
            pass

    def stop(self):
        super().stop()
        proc = self.proc
        if proc is not None:
            self._kill(proc)


class SnapshotUrlCameraStream(PollingCameraStream):
    """http(s) JPEG snapshot camera (the address may carry user:pass@)."""

    def __init__(self, url, label="camera", **kw):
        super().__init__(lambda: plain_url(url), label=label, **kw)
        self._url = url

    def _session(self):
        while True:
            if self._idle_or_stopped():
                raise _Done()
            try:
                self._store_frame(fetch_snapshot_url(self._url, self.fetch_timeout))
            except Exception as e:
                self.last_error = redact_text(e, self._url)
                raise
            self.fetches += 1
            self.connected = True
            end = time.monotonic() + self.interval
            while time.monotonic() < end:
                if self._idle_or_stopped():
                    raise _Done()
                time.sleep(0.1)


def make_camera_stream(url, label="camera"):
    return RtspCameraStream(url, label) if url.lower().startswith(("rtsp://", "rtsps://")) \
        else SnapshotUrlCameraStream(url, label)


def check_camera(url, timeout=8.0):
    """'Test connection' for a camera address: grab ONE frame. -> (ok, plain-language message)."""
    where = plain_url(url)
    try:
        if not url.lower().startswith(("rtsp://", "rtsps://")):
            fetch_snapshot_url(url, timeout)
            return True, f"Camera picture received from {where}"
        exe = find_ffmpeg()
        if not exe:
            return False, NO_FFMPEG_MSG
        try:
            r = subprocess.run(_ffmpeg_cmd(exe, url, one_frame=True), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"camera not reachable at {where} (no picture within {timeout:.0f} s)"
        if r.stdout[:2] == b"\xff\xd8":
            return True, f"Camera picture received from {where}"
        return False, _ffmpeg_reason(redact_text(r.stderr.decode("utf-8", "replace"), url), url)
    except Exception as e:
        return False, redact_text(e, url)


class CameraOverrideMixin:
    """Mixed into a printer driver (see apply_camera_url) so the printer's camera is the configured
    address instead of the driver's own. Properties of the driver are overridden, nothing else changes."""
    _override_url = None
    _override_stream = None
    _override_guard = threading.Lock()

    @property
    def has_camera(self):
        return True

    def camera(self):
        with CameraOverrideMixin._override_guard:
            if self._override_stream is None:
                self._override_stream = make_camera_stream(self._override_url, label=str(self.name))
            return self._override_stream

    def camera_info(self):
        return {"flip_h": False, "flip_v": False, "rotate90": False}

    def disconnect(self):
        with CameraOverrideMixin._override_guard:
            cam, self._override_stream = self._override_stream, None
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass
        sup = getattr(super(), "disconnect", None)
        if callable(sup):
            return sup()


_OVERRIDE_CLASSES = {}


def apply_camera_url(prn, url):
    """Make `prn` use `url` as its camera. Returns prn. A blank url changes nothing."""
    if not url:
        return prn
    cls = type(prn)
    sub = _OVERRIDE_CLASSES.get(cls)
    if sub is None:
        sub = _OVERRIDE_CLASSES[cls] = type(cls.__name__, (CameraOverrideMixin, cls), {"__module__": cls.__module__})
    prn.__class__ = sub
    prn._override_url = url
    prn._override_stream = None
    return prn
