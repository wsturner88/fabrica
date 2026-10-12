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
import logging
import socket
import ssl
import struct
import threading
import time
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
