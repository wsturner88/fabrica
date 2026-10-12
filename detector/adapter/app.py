"""Print Hub detector adapter (MIT).

A small front door for an unmodified Obico `ml_api` container. `ml_api` can only be asked to
fetch a picture from a URL; it cannot be handed bytes. This adapter turns that into a plain
"picture in, score out" service:

    POST /v1/score     body = a JPEG, header "Authorization: Bearer <DETECTOR_TOKEN>"
    GET  /healthz      no auth; 200 when ml_api answers, 503 when it does not

How a score is made (nothing here is Obico code; we only speak its HTTP API):

  1. check the token, the size and that the body really is a JPEG;
  2. keep the bytes in memory under a random, single-use path on a second, internal listener;
  3. ask ml_api `GET /p/?img=http://<this service>:<image port>/_f/<random id>`;
  4. ml_api fetches the picture from us (once), runs the model, answers with detections;
  5. forget the picture; answer `{"score": <sum of confidences>, "detections": [...], "ms": N}`.

Configuration (environment):

    DETECTOR_TOKEN           bearer token callers must send. REQUIRED: the adapter refuses to
                             start without one unless DETECTOR_ALLOW_NO_TOKEN=1 (tests / a
                             closed lab network only)
    ML_API_URL               default http://detector-ml:3333
    ML_API_TOKEN             sent to ml_api as a bearer token, if set (optional)
    MAX_BYTES                largest accepted picture, default 5242880 (5 MB)
    PORT                     public listener, default 8080
    IMAGE_PORT               internal picture listener, default 8081 (never publish it)
    IMAGE_HOST               name ml_api uses to reach the picture listener, default `detector`
                             (the compose service name; ml_api only waits 0.1 s to connect, so
                             this must be a name or IP on the shared private network)
    BIND                     address for both listeners, default 0.0.0.0
    FRAME_TTL                seconds a picture stays fetchable, default 30
    ML_TIMEOUT               seconds to wait for ml_api, default 30
    LOG_LEVEL                default INFO

Stdlib only. Pictures live in memory, are never written to disk and never logged.
"""

import hmac
import http.server
import json
import logging
import os
import platform
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = 1
log = logging.getLogger("detector.adapter")

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
HEADER_READ_TIMEOUT = 15.0           # a client that stalls mid-request is dropped
REQUEST_READ_DEADLINE = 20.0         # ...and one that drips bytes slowly enough to dodge that is cut off
                                     # after this much WALL-CLOCK time for the headers, and again for the body
MAX_CONNECTIONS = 32                 # handler threads at once; more are refused (a slow-loris cannot pile up threads)
BUSY_WAIT_SECONDS = 1.0              # ml_api runs one inference at a time


class Config:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.token = e.get("DETECTOR_TOKEN", "")
        self.allow_no_token = e.get("DETECTOR_ALLOW_NO_TOKEN", "") == "1"
        self.ml_api_url = e.get("ML_API_URL", "http://detector-ml:3333").rstrip("/")
        self.ml_api_token = e.get("ML_API_TOKEN", "")
        self.max_bytes = _int(e.get("MAX_BYTES"), DEFAULT_MAX_BYTES, 1024, 64 * 1024 * 1024)
        self.port = _int(e.get("PORT"), 8080, 0, 65535)
        self.image_port = _int(e.get("IMAGE_PORT"), 8081, 0, 65535)
        self.image_host = e.get("IMAGE_HOST", "detector")
        self.bind = e.get("BIND", "0.0.0.0")
        self.frame_ttl = float(_int(e.get("FRAME_TTL"), 30, 1, 600))
        self.ml_timeout = float(_int(e.get("ML_TIMEOUT"), 30, 1, 300))

    def problems(self):
        out = []
        if not self.token and not self.allow_no_token:
            out.append("DETECTOR_TOKEN is not set. Set a long random token, or "
                       "DETECTOR_ALLOW_NO_TOKEN=1 to run without one on a closed network.")
        return out


def _int(raw, default, lo, hi):
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


class ApiError(Exception):
    """An error with an HTTP status and a stable machine-readable code."""

    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


# ---------------------------------------------------------------- single-use picture store

class FrameStore:
    """Pictures waiting to be fetched by ml_api. Each id works once and then expires."""

    def __init__(self, ttl=30.0, clock=time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._frames = {}                       # id -> (bytes, expires_at)

    def put(self, data: bytes) -> str:
        fid = secrets.token_urlsafe(24)         # 192 bits: not guessable
        now = self._clock()
        with self._lock:
            self._purge(now)
            self._frames[fid] = (data, now + self.ttl)
        return fid

    def take(self, fid: str):
        """The bytes for `fid`, once; None if unknown, used or expired."""
        now = self._clock()
        with self._lock:
            self._purge(now)
            item = self._frames.pop(fid, None)
        return item[0] if item else None

    def discard(self, fid: str):
        with self._lock:
            self._frames.pop(fid, None)

    def __len__(self):
        with self._lock:
            self._purge(self._clock())
            return len(self._frames)

    def _purge(self, now):
        for k in [k for k, (_, exp) in self._frames.items() if exp <= now]:
            del self._frames[k]


# ---------------------------------------------------------------- talking to ml_api

class MlApi:
    def __init__(self, base_url, token="", timeout=30.0):
        self.base, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        # Never follow redirects and never use a proxy: this is a private, local hop.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, path, timeout):
        req = urllib.request.Request(self.base + path)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        return self._opener.open(req, timeout=timeout)

    def healthy(self) -> bool:
        try:
            with self._request("/hc/", 3.0) as r:
                return r.status == 200 and r.read(16).strip() == b"ok"
        except urllib.error.HTTPError as e:
            e.close()
            return False
        except Exception:
            return False

    def detect(self, image_url: str):
        """Ask ml_api to fetch `image_url`. Returns the list of [label, conf, box] it found.
        Raises ApiError(502/504) on any ml_api problem."""
        path = "/p/?img=" + urllib.parse.quote(image_url, safe="")
        try:
            with self._request(path, self.timeout) as r:
                body = r.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 400:
                raise ApiError(502, "detector_fetch_failed",
                               "ml_api could not fetch or decode the picture") from None
            if e.code in (401, 403):
                raise ApiError(502, "detector_auth", "ml_api rejected the adapter's token") from None
            raise ApiError(502, "detector_error", f"ml_api answered HTTP {e.code}") from None
        except (TimeoutError, OSError) as e:
            if _is_timeout(e):
                raise ApiError(504, "detector_timeout", "ml_api did not answer in time") from None
            raise ApiError(502, "detector_unreachable", "ml_api is not reachable") from None
        try:
            dets = json.loads(body)["detections"]
            if not isinstance(dets, list):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ApiError(502, "detector_bad_response", "ml_api returned something unexpected") from None
        return dets


def _is_timeout(e):
    import socket
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True
    return isinstance(e, urllib.error.URLError) and isinstance(getattr(e, "reason", None),
                                                               (TimeoutError, socket.timeout))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def normalise(dets):
    """ml_api's [[label, conf, [cx, cy, w, h]], ...] -> (score, [{"label","conf","box"}])."""
    out, total = [], 0.0
    for d in dets:
        try:
            label, conf, box = d[0], float(d[1]), [float(v) for v in d[2]][:4]
        except (TypeError, ValueError, IndexError):
            continue
        if conf != conf or conf < 0:           # NaN / negative: ignore
            continue
        total += conf
        out.append({"label": str(label), "conf": round(conf, 4), "box": [round(v, 1) for v in box]})
    return round(total, 6), out


# ---------------------------------------------------------------- the service

class Adapter:
    """Owns the two listeners. `start()` returns once both are accepting; `url`/`image_url_base`
    carry the real bound ports (so tests can use port 0)."""

    def __init__(self, config: Config):
        self.cfg = config
        self.frames = FrameStore(config.frame_ttl)
        self.ml = MlApi(config.ml_api_url, config.ml_api_token, config.ml_timeout)
        self._gate = threading.Semaphore(1)
        self._public = self._image = None
        self._threads = []
        self.image_host = config.image_host

    # -- lifecycle --
    def start(self):
        self._public = _Server((self.cfg.bind, self.cfg.port), _PublicHandler, self)
        self._image = _Server((self.cfg.bind, self.cfg.image_port), _ImageHandler, self)
        for srv in (self._public, self._image):
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        for srv in (self._public, self._image):
            if srv is not None:
                srv.shutdown()
                srv.server_close()

    @property
    def port(self):
        return self._public.server_address[1]

    @property
    def image_port(self):
        return self._image.server_address[1]

    def image_url(self, fid):
        return f"http://{self.image_host}:{self.image_port}/_f/{fid}"

    # -- auth --
    def authorised(self, header: str) -> bool:
        if not self.cfg.token:
            return self.cfg.allow_no_token
        scheme, _, given = (header or "").partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(given.strip().encode(), self.cfg.token.encode())

    # -- scoring --
    def score(self, jpeg: bytes) -> dict:
        if not self._gate.acquire(timeout=BUSY_WAIT_SECONDS):
            raise ApiError(503, "busy", "another picture is being scored", {"Retry-After": "1"})
        t0 = time.monotonic()
        fid = None
        try:
            attempts = 0
            while True:
                attempts += 1
                fid = self.frames.put(jpeg)
                try:
                    dets = self.ml.detect(self.image_url(fid))
                    break
                except ApiError as e:
                    # One retry when ml_api could not fetch (its connect budget is 0.1 s).
                    if e.code == "detector_fetch_failed" and attempts < 2:
                        self.frames.discard(fid)
                        continue
                    raise
        finally:
            if fid:
                self.frames.discard(fid)
            self._gate.release()
        score, detections = normalise(dets)
        return {"api": API_VERSION, "score": score, "detections": detections,
                "ms": int((time.monotonic() - t0) * 1000)}

    def health(self):
        ok = self.ml.healthy()
        body = {"api": API_VERSION, "status": "ok" if ok else "degraded",
                "ml_api": "ok" if ok else "unreachable",
                "auth": "token" if self.cfg.token else "none",
                "arch": platform.machine(), "max_bytes": self.cfg.max_bytes}
        return (200 if ok else 503), body


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, addr, handler, adapter):
        self.adapter = adapter
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)             # too many open connections: refuse, do not queue
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _BaseHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "detector-adapter"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(HEADER_READ_TIMEOUT)
        self._deadline_timer = None

    # -- wall-clock limit on reading a request. The socket timeout is per recv(), so a client that
    #    sends one byte every few seconds would otherwise hold a handler thread (and up to 5 MB of
    #    body) for as long as it likes.
    def _arm(self, seconds=None):
        self._disarm()
        t = threading.Timer(REQUEST_READ_DEADLINE if seconds is None else seconds, self._cut_off)
        t.daemon = True
        self._deadline_timer = t
        t.start()

    def _disarm(self):
        t, self._deadline_timer = getattr(self, "_deadline_timer", None), None
        if t is not None:
            t.cancel()

    def _cut_off(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def handle_one_request(self):
        self._arm()
        try:
            super().handle_one_request()
        finally:
            self._disarm()

    def parse_request(self):
        ok = super().parse_request()
        self._disarm()                       # headers are in; the body gets its own limit in _read_body
        return ok

    def log_message(self, fmt, *args):          # no default access log: paths carry frame ids
        pass

    def _send(self, status, body: bytes, ctype="application/json", headers=None, close=False):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:                      # the client went away (or was cut off): nothing to tell it
            self.close_connection = True

    def _json(self, status, obj, headers=None, close=False):
        self._send(status, json.dumps(obj).encode(), headers=headers, close=close)

    def _error(self, e: ApiError, close=False):
        self._json(e.status, {"api": API_VERSION, "error": e.code, "message": e.message},
                   headers=e.headers, close=close)

    def _not_allowed(self):
        self._error(ApiError(405, "method_not_allowed", "method not allowed"), close=True)

    do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed


class _PublicHandler(_BaseHandler):
    def do_GET(self):
        adapter = self.server.adapter
        if self.path.split("?", 1)[0] == "/healthz":
            status, body = adapter.health()
            return self._json(status, body)
        self._error(ApiError(404, "not_found", "unknown path"), close=True)

    do_HEAD = do_GET

    def do_POST(self):
        adapter = self.server.adapter
        try:
            if self.path.split("?", 1)[0] != "/v1/score":
                raise ApiError(404, "not_found", "unknown path")
            # Order matters: refuse before reading a single body byte.
            if not adapter.authorised(self.headers.get("Authorization", "")):
                raise ApiError(401, "unauthorized", "missing or wrong bearer token",
                               {"WWW-Authenticate": "Bearer"})
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                raise ApiError(411, "length_required", "send a Content-Length (no chunked bodies)")
            raw_len = self.headers.get("Content-Length")
            try:
                length = int(raw_len)
                if length < 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ApiError(411, "length_required", "Content-Length is required") from None
            if length == 0:
                raise ApiError(400, "empty_body", "the body is empty")
            if length > adapter.cfg.max_bytes:
                raise ApiError(413, "too_large", f"larger than the {adapter.cfg.max_bytes} byte limit")
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype not in ("", "image/jpeg", "image/jpg", "application/octet-stream"):
                raise ApiError(415, "unsupported_type", "Content-Type must be image/jpeg")
            data = self._read_body(length)
            if data[:2] != b"\xff\xd8":
                raise ApiError(415, "not_jpeg", "the body is not a JPEG (must start FF D8)")
        except ApiError as e:
            return self._error(e, close=True)       # body (if any) unread: drop the connection
        try:
            result = adapter.score(data)
        except ApiError as e:
            return self._error(e)
        except Exception:                           # never leak internals
            log.exception("scoring failed")
            return self._error(ApiError(500, "internal", "internal error"))
        log.info("scored %d bytes -> score=%.3f in %d ms (%d boxes)", len(data), result["score"],
                 result["ms"], len(result["detections"]))
        self._json(200, result)

    def _read_body(self, length):
        chunks, got = [], 0
        self._arm()
        try:
            while got < length:
                part = self.rfile.read(min(65536, length - got))
                if not part:
                    break
                chunks.append(part)
                got += len(part)
        except OSError:
            pass
        finally:
            self._disarm()
        if got != length:
            raise ApiError(400, "short_body", "the body ended before Content-Length bytes")
        return b"".join(chunks)


class _ImageHandler(_BaseHandler):
    """Serves each stored picture exactly once, to ml_api."""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        fid = path[len("/_f/"):] if path.startswith("/_f/") else ""
        data = self.server.adapter.frames.take(fid) if fid else None
        if data is None:
            return self._send(404, b"gone", "text/plain", close=True)
        self._send(200, data, "image/jpeg", close=True)

    # HEAD is refused (base class): it would burn the single use.


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    problems = cfg.problems()
    if problems:
        for p in problems:
            print("detector-adapter: " + p, file=sys.stderr, flush=True)
        return 2
    if not cfg.token:
        log.warning("running WITHOUT a token (DETECTOR_ALLOW_NO_TOKEN=1): anyone who can reach "
                    "this port can use the detector")
    adapter = Adapter(cfg).start()
    log.info("listening on %s:%d (pictures on :%d as http://%s:%d/_f/<id>), ml_api at %s, limit %d bytes",
             cfg.bind, adapter.port, adapter.image_port, cfg.image_host, adapter.image_port,
             cfg.ml_api_url, cfg.max_bytes)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        adapter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
