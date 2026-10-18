"""Printer discovery: find printers on the network so the Add-printer wizard can be pre-filled.

Why TCP scanning: the Hub usually runs in Docker, and on macOS (Colima / Docker Desktop) the
container sits behind a NAT - no multicast or broadcast from the home LAN ever reaches it, so
mDNS / SSDP discovery cannot be relied on. What always works is an outbound TCP connection to
an address the user names. So discovery is an ACTIVE SCAN of the networks the user lists
(CIDR ranges), and multicast listeners (SSDP, zeroconf) are only a bonus that is used when it
happens to work (Linux host networking) and is never required.

What it does, per host (bounded worker pool, short timeouts):
  * Bambu Lab   TCP 8883 (MQTT over TLS) is open and its TLS certificate subject CN is a
                serial number. No login is needed to read a certificate; the CN of a Bambu
                printer's certificate is its serial, from which the model follows (the prefix
                map in fleet_store). Corroboration required before a host is called a Bambu:
                FTPS 990 or camera 6000 open, OR the certificate issuer says "BBL", OR a known
                serial prefix. (Ports 990 / 6000 are also reported in `details`.)
  * Moonraker   GET /server/info answers JSON with `klippy_state` (Moonraker wraps replies in
                {"result": ...}; both shapes are accepted). Hostname from GET /printer/info.
                If the server wants an API key it answers 401 with a JSON {"error": ...} body
                from a Tornado server; that is reported as a Moonraker needing a key.
  * PrusaLink   GET /api/version is normally open and its JSON says "PrusaLink"; otherwise a
                401/403 with a Digest `WWW-Authenticate` challenge (PrusaLink uses realm
                "Printer API"; OctoPrint never uses Digest), or a root page titled PrusaLink.
                Hostname / printer type from GET /api/v1/info when that is open.
  * OctoPrint   GET /api/version JSON whose text says "OctoPrint", or (when /api/version is
                locked, which is the default) the root page <title> containing "OctoPrint".
Only the printer ports are touched (see DEFAULT_PORTS), no credentials are ever sent, and
nothing is ever written to a device. One scan at a time; a short cool-down between scans.

Near and far networks. A network the Hub reaches in a millisecond or two (its own LAN) is scanned
fast: a big pool of workers and a short connect timeout. A network that is routed - a home network
reached over a VPN subnet route, another site, anything with a round trip of more than FAR_RTT_MS -
is scanned gently: a small pool that ramps up, a connect timeout long enough for one lost SYN to be
re-sent, and a "canary" (a host that is known to answer) that is re-checked while the scan runs. If the
canary stops answering the route is being overloaded: new hosts are held back, the pool is halved, and
every host that came back silent in the meantime is looked at again. A burst of dozens of parallel
connects through a routed or NAT-ed path can silently drop SYNs; a dropped SYN looks exactly like
"nobody is home", which is how a real printer used to be reported as an empty address.

A host is called empty only when all three key ports (80, 7125, 8883) were silent. Such quiet hosts
get one more look at low concurrency and a long timeout at the end of the scan: always for addresses
of printers the Hub already knows, for hosts that were probed while the route was dropping, and for
every quiet host when a far network produced no answer at all and had no canary to vouch for it.

Optional extras (never required): an SSDP listener for Bambu NOTIFY packets (UDP 2021 / 1990)
for a few seconds, and the `zeroconf` library when it happens to be installed (it is NOT a
dependency) for host names. Both only fill in details for addresses inside the scanned networks.

This module is stdlib-only and knows nothing about server.py. Wire it with ~10 lines:

    import discovery
    DISCOVERY = discovery.DiscoveryManager(FLEET.list_public)
    for path, methods, fn in discovery.routes(DISCOVERY):          # order matters, keep it
        mcp.custom_route(path, methods=methods)(_safe_route(fn))
    @mcp.tool()
    async def discover_printers(networks: list[str] | None = None) -> str:      # control scope
        try:
            return json.dumps(await _run(DISCOVERY.scan_and_wait, networks), indent=2)
        except discovery.DiscoveryError as e:
            return f"REFUSED: {e}"

HTTP contract (documented in docs/discovery.md):
    POST   /api/discover              {"networks": ["192.168.1.0/24", ...]}  -> 202 {"scan_id", "total", "networks"}
    GET    /api/discover/suggestions  -> {"networks": [cidr...], "sources": {cidr: why}, "note": str}
    GET    /api/discover/{scan_id}    -> {"state", "done", "total", "found": [...], ...}
    DELETE /api/discover/{scan_id}    -> stops a running scan
"""

import concurrent.futures
import copy
import http.client
import ipaddress
import json
import re
import select
import socket
import ssl
import threading
import time
import urllib.parse
import uuid

# ---------------------------------------------------------------- constants

DEFAULT_PORTS = {
    "bambu_mqtt": 8883,      # MQTT over TLS: the certificate CN is the serial
    "bambu_ftps": 990,       # corroboration only
    "bambu_camera": 6000,    # corroboration only (P1 / A1 camera)
    "moonraker": 7125,
    "http": (80, 5000),      # OctoPrint / PrusaLink / Moonraker behind Mainsail or Fluidd
}
SSDP_PORTS = (2021, 1990)
SSDP_GROUP = "239.255.255.250"

MAX_PREFIX = 22              # biggest single network: /22 = 1024 addresses
MAX_NETWORKS = 8
MAX_TOTAL_HOSTS = 2048
MAX_WORKERS = 128

# Near / far (see the module doc). Numbers come from measurements of a 40 ms routed /24, docs/discovery.md.
FAR_RTT_MS = 10.0            # slower than this to the first hosts that answer = a far network
OVERLAY = ipaddress.ip_network("100.64.0.0/10")       # VPN overlay addresses are always routed

FAMILY_LABELS = {"bambu": "Bambu Lab", "octoprint": "OctoPrint", "prusalink": "PrusaLink",
                 "moonraker": "Klipper (Moonraker)"}

SHORT_NAMES = {"bambu": "Bambu", "octoprint": "OctoPrint", "prusalink": "PrusaLink", "moonraker": "Klipper"}

# Only private / link-local / loopback / CGNAT (VPN overlays such as Tailscale) ranges may be scanned.
_ALLOWED = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10")]

SUGGEST_NOTE = ("The Hub only looks at the address ranges listed here - when it runs inside Docker "
                "(for example on a Mac) it cannot see your network by itself. Networks already used "
                "by your printers are suggested first.")

_SERIAL_RE = re.compile(r"^[A-Za-z0-9]{8,24}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


# ---------------------------------------------------------------- errors

class DiscoveryError(Exception):
    """A request discovery cannot honour. `status` is the HTTP code; str(e) is for a human."""
    status = 400
    extra: dict = {}

    def payload(self) -> dict:
        return {"error": str(self), **self.extra}


class NotFound(DiscoveryError):
    status = 404


class Busy(DiscoveryError):
    status = 409

    def __init__(self, msg, scan_id=None):
        super().__init__(msg)
        self.extra = {"busy": True, "scan_id": scan_id}


class RateLimited(DiscoveryError):
    status = 429

    def __init__(self, msg, retry_after):
        super().__init__(msg)
        self.extra = {"retry_after": round(retry_after, 1)}


# ---------------------------------------------------------------- network parsing

def parse_networks(networks, max_prefix=MAX_PREFIX, max_total=MAX_TOTAL_HOSTS):
    """Validate user input -> sorted, de-duplicated list of IPv4Network.

    Accepts a list (or one string with commas / spaces) of "192.168.1.0/24", "192.168.1.0/255.255.255.0"
    or a single address "192.168.1.50". Host bits are tolerated ("192.168.1.5/24" -> 192.168.1.0/24).
    Refused: IPv6, public ranges, anything bigger than a /22, more than MAX_NETWORKS networks,
    more than MAX_TOTAL_HOSTS addresses in all."""
    if isinstance(networks, str):
        networks = re.split(r"[,\s;]+", networks.strip())
    if not isinstance(networks, (list, tuple)):
        raise DiscoveryError("Give the networks as a list, like [\"192.168.1.0/24\"].")
    items = [str(x).strip() for x in networks if str(x).strip()]
    if not items:
        raise DiscoveryError("Enter at least one network to scan, for example 192.168.1.0/24.")
    nets = []
    for it in items:
        try:
            net = ipaddress.ip_network(it, strict=False)
        except ValueError:
            raise DiscoveryError(f"'{it}' is not a network address. Use a form like 192.168.1.0/24 "
                                 "(that is 192.168.1.1 to 192.168.1.254).") from None
        if net.version != 4:
            raise DiscoveryError(f"'{it}' is an IPv6 network; only IPv4 networks (like 192.168.1.0/24) can be scanned.")
        if net.prefixlen < max_prefix:
            raise DiscoveryError(f"'{it}' is too big to scan ({net.num_addresses} addresses). The biggest "
                                 f"network allowed is a /{max_prefix} ({2 ** (32 - max_prefix)} addresses) - "
                                 "list a smaller one, like 192.168.1.0/24.")
        if not any(net.subnet_of(a) for a in _ALLOWED):
            raise DiscoveryError(f"'{it}' is not a private network. Only home / office ranges "
                                 "(192.168.x.x, 10.x.x.x, 172.16-31.x.x) can be scanned, never the internet.")
        nets.append(net)
    nets = sorted(set(nets), key=lambda n: (int(n.network_address), n.prefixlen))
    kept = []                                    # drop networks wholly inside another one in the list
    for n in nets:
        if not any(n.subnet_of(k) for k in kept):
            kept.append(n)
    if len(kept) > MAX_NETWORKS:
        raise DiscoveryError(f"Too many networks ({len(kept)}); scan at most {MAX_NETWORKS} at a time.")
    total = sum(_host_count(n) for n in kept)
    if total > max_total:
        raise DiscoveryError(f"That is {total} addresses; at most {max_total} can be scanned at once. "
                             "List fewer or smaller networks.")
    return kept


def _host_count(net) -> int:
    return net.num_addresses if net.prefixlen >= 31 else net.num_addresses - 2


def hosts_of(networks) -> list:
    out, seen = [], set()
    for net in networks:
        for h in (list(net) if net.prefixlen >= 31 else net.hosts()):
            if h not in seen:
                seen.add(h)
                out.append(str(h))
    return out


# ---------------------------------------------------------------- minimal DER / X.509 name reader

_OIDS = {b"\x55\x04\x03": "CN", b"\x55\x04\x0a": "O", b"\x55\x04\x0b": "OU", b"\x55\x04\x06": "C"}


def _tlv(buf, pos):
    """-> (tag, content_start, content_end) of the DER element at `pos`."""
    tag = buf[pos]
    n = buf[pos + 1]
    pos += 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0 or k > 4:
            raise ValueError("bad DER length")
        n = int.from_bytes(buf[pos:pos + k], "big")
        pos += k
    end = pos + n
    if end > len(buf):
        raise ValueError("DER element runs past the end")
    return tag, pos, end


def _children(buf, start, end):
    while start < end:
        tag, cs, ce = _tlv(buf, start)
        yield tag, cs, ce
        start = ce


def _decode_str(tag, raw):
    if tag == 0x1E:                        # BMPString
        return raw.decode("utf-16-be", "replace")
    if tag == 0x0C:                        # UTF8String
        return raw.decode("utf-8", "replace")
    return raw.decode("latin-1")           # Printable / IA5 / T61 / Teletex


def _parse_name(buf, start, end) -> dict:
    out = {}
    for tag, rs, re_ in _children(buf, start, end):          # RDN: SET
        if tag != 0x31:
            continue
        for atag, as_, ae in _children(buf, rs, re_):        # AttributeTypeAndValue: SEQUENCE
            if atag != 0x30:
                continue
            kids = list(_children(buf, as_, ae))
            if len(kids) >= 2 and kids[0][0] == 0x06:
                key = _OIDS.get(bytes(buf[kids[0][1]:kids[0][2]]))
                if key and key not in out:
                    out[key] = _decode_str(kids[1][0], bytes(buf[kids[1][1]:kids[1][2]]))
    return out


def parse_cert_names(der: bytes) -> dict:
    """{"subject": {"CN":..,"O":..}, "issuer": {...}} from a DER X.509 certificate. Stdlib only
    (no `cryptography`); {} for anything it cannot read. Walks Certificate -> TBSCertificate and
    reads the Name structures; it does not validate anything (the point is to READ an unverified
    certificate)."""
    try:
        buf = bytes(der)
        tag, cs, ce = _tlv(buf, 0)
        if tag != 0x30:
            return {}
        tag, ts, te = _tlv(buf, cs)                          # tbsCertificate
        if tag != 0x30:
            return {}
        kids = list(_children(buf, ts, te))
        i = 1 if kids and kids[0][0] == 0xA0 else 0          # optional [0] version
        # serialNumber, signature, issuer, validity, subject
        issuer, subject = kids[i + 2], kids[i + 4]
        return {"subject": _parse_name(buf, subject[1], subject[2]),
                "issuer": _parse_name(buf, issuer[1], issuer[2])}
    except (ValueError, IndexError):
        return {}


# ---------------------------------------------------------------- SSDP (Bambu NOTIFY) parsing

def parse_ssdp(data: bytes, addr=None):
    """A Bambu SSDP NOTIFY -> {"ip","serial","name","model_code","connect"} or None.
    Bambu printers announce themselves with `DevName.bambu.com`, `DevModel.bambu.com`, `USN: <serial>`
    and `Location: <ip>` headers."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None
    lines = text.replace("\r", "").split("\n")
    if not lines or not lines[0].upper().startswith(("NOTIFY", "HTTP/1.1 200", "M-SEARCH")):
        return None
    hdr = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdr[k.strip().lower()] = v.strip()
    if not (any(k.endswith(".bambu.com") for k in hdr) or "bambulab" in text.lower()):
        return None
    ip = hdr.get("location", "")
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        ip = addr[0] if addr else ""
    if not ip:
        return None
    serial = hdr.get("usn", "")
    return {"ip": ip, "serial": serial if _SERIAL_RE.match(serial) else None,
            "name": hdr.get("devname.bambu.com") or None,
            "model_code": hdr.get("devmodel.bambu.com") or None,
            "connect": hdr.get("devconnect.bambu.com") or None}


# ---------------------------------------------------------------- helpers

def _model_from_serial(serial: str) -> str:
    """Model name from the serial prefix, using fleet_store's map when it can be imported."""
    try:
        from fleet_store import bambu_model_from_serial
        return bambu_model_from_serial(serial)
    except Exception:
        return ""


def _norm_host(h: str) -> str:
    h = (h or "").strip().lower().rstrip(".")
    return h[:-6] if h.endswith(".local") else h


def _host_of(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if "://" in v:
        try:
            return _norm_host(urllib.parse.urlparse(v).hostname or "")
        except ValueError:
            return ""
    return _norm_host(v.split("/")[0].split(":")[0] if v.count(":") <= 1 else v)


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None


def _title(body: bytes) -> str:
    m = re.search(rb"<title[^>]*>(.*?)</title>", body[:65536], re.I | re.S)
    return m.group(1).decode("utf-8", "replace").strip() if m else ""


def suggest_name(item: dict, taken) -> str:
    """A valid printer name (fleet NAME_RE) not in `taken` (case-insensitive)."""
    host = (item.get("hostname") or "").strip().rstrip(".")
    if host.lower().endswith(".local"):
        host = host[:-6]
    base = host.split(".")[0]
    if not base and item.get("model"):
        base = re.sub(r"^bambu lab\s+", "", item["model"], flags=re.I)
    if not base:
        tail = (item.get("serial") or "")[-4:] or str(item.get("address", "")).split(".")[-1]
        base = f"{SHORT_NAMES.get(item['family'], 'Printer')}-{tail}"
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-_.")[:28] or "Printer"
    if not base[0].isalnum():
        base = "P" + base
    low = {t.lower() for t in taken}
    name, n = base, 2
    while name.lower() in low or name.lower() == "all" or not _NAME_RE.match(name):
        name = f"{base}-{n}"[:32]
        n += 1
    return name


# ---------------------------------------------------------------- wall-clock limits

class _Watchdog:
    """Closes a socket from a timer thread once `seconds` of WALL-CLOCK time have passed.

    Socket timeouts are per recv(): a host that drips one byte a second (TLS handshake, HTTP headers,
    a never-ending body) never trips them and holds the worker forever. Shutting the socket down
    from outside wakes the blocked call with an error, whatever phase it is in.

        with _Watchdog(sock, 2.0) as wd:
            ...blocking socket work...
        if wd.fired: ...   # it was cut off
    """

    def __init__(self, sock, seconds):
        self.sock = sock
        self.fired = False
        self._timer = threading.Timer(max(0.01, seconds), self._fire)
        self._timer.daemon = True

    def _fire(self):
        self.fired = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        return False


# ---------------------------------------------------------------- the scan itself

class _Profile:
    """How one network is scanned: pool size, timeouts and whether the pool ramps and is watched."""
    __slots__ = ("kind", "workers", "connect_timeout", "http_timeout", "gentle")

    def __init__(self, kind, workers, connect_timeout, http_timeout, gentle=False):
        self.kind, self.workers = kind, int(workers)
        self.connect_timeout, self.http_timeout, self.gentle = connect_timeout, http_timeout, gentle


class _Gate:
    """How many hosts may be in flight right now. Starts small, grows a step at a time (slow start),
    is halved by shrink() and held shut by pause() while a routed path is overloaded."""

    def __init__(self, cap, start=4, step=2):
        self.cap = max(1, int(cap))
        self.window = min(self.cap, max(1, int(start)))
        self.step = step
        self.floor = min(2, self.cap)
        self.active = 0
        self.paused = False
        self.cond = threading.Condition()

    def enter(self, cancel) -> bool:
        with self.cond:
            while not cancel.is_set() and (self.paused or self.active >= self.window):
                self.cond.wait(0.1)
            if cancel.is_set():
                return False
            self.active += 1
            return True

    def leave(self):
        with self.cond:
            self.active -= 1
            self.cond.notify_all()

    def grow(self):
        with self.cond:
            self.window = min(self.cap, self.window + self.step)
            self.cond.notify_all()

    def shrink(self):
        """Overload: halve the pool, and do not grow back past the last size that was fine."""
        with self.cond:
            self.cap = max(self.floor, min(self.cap, self.window - self.step))
            self.window = max(self.floor, min(self.cap, self.window // 2))

    def pause(self, on):
        with self.cond:
            self.paused = on
            self.cond.notify_all()


class _Scan:
    def __init__(self, scan_id, networks, hosts):
        self.id = scan_id
        self.networks = networks
        self.hosts = hosts
        self.total = len(hosts)
        self.done = 0
        self.found = []                 # internal finding dicts, kept in discovery order
        self.state = "running"
        self.error = None
        self.cancel = threading.Event()
        self.started = time.time()
        self.finished = None
        self.extras = {}
        self.note = None                # why a scan ended early (time limit)
        self.names = set()              # suggested names handed out in this scan
        self.hard_deadline = None       # monotonic: past this the scan is considered wedged
        self.cancelled_at = None        # monotonic time cancel() was called
        self.phase = "starting"         # starting | checking | scanning | rechecking | finishing
        self.groups = []                # one dict per near / far group of networks (see _plan)
        self.cur = None                 # the group being scanned: {"kind","started","base","total"}
        self.quiet = {}                 # ip -> monotonic time it ended with no clear answer (silent, or some key ports silent)
        self.tainted = set()            # quiet hosts probed while the route was dropping packets
        self.retry_total = 0            # quiet hosts being looked at again
        self.retry_done = 0
        self.retry_skipped = 0          # quiet hosts the retry did not get to (time box)
        self.backoffs = 0               # times the route was found overloaded and the pool halved
        self.responders = 0             # hosts that sent any reply at all
        self.ctx = (None, None, False)  # (profile, gate, is_retry) for the host tasks now queued
        self.retry_closed = False       # the second look is over; late results are dropped
        self.profiles = []              # per network: near / far, round trip, why
        self.limit_at = None            # monotonic: the hard stop for this scan


class DiscoveryManager:
    """Owns scans. All public methods are thread-safe and quick (a scan runs on its own thread).

    fleet_records   callable -> list of FleetManager.public() dicts (used to mark printers that are
                    already added and to suggest networks). None = empty fleet.
    ports           overrides for DEFAULT_PORTS (tests point these at loopback fakes).
    """

    def __init__(self, fleet_records=None, ports=None, workers=64, connect_timeout=0.6, http_timeout=1.5,
                 cooldown=3.0, ssdp=True, ssdp_seconds=3.0, ssdp_ports=SSDP_PORTS, ssdp_bind="0.0.0.0",
                 ssdp_join=True, zeroconf=True, zeroconf_seconds=2.0, zeroconf_module=None,
                 local_ip=None, keep=5, host_timeout=15.0, scan_timeout=240.0, cancel_grace=5.0,
                 far_workers=16, far_connect_timeout=1.5, far_http_timeout=3.0, far_rtt_ms=FAR_RTT_MS,
                 far_start=4, far_step=2, far_tick=0.5, retry=True, retry_workers=4,
                 retry_connect_timeout=2.0, retry_seconds=60.0, probe_timeout=1.0, probe_seconds=3.0,
                 pause_seconds=15.0, connector=None):
        self.fleet_records = fleet_records or (lambda: [])
        self.ports = {**DEFAULT_PORTS, **(ports or {})}
        http = self.ports["http"]
        self.http_ports = [http] if isinstance(http, int) else list(http)
        self.workers = max(1, min(int(workers), MAX_WORKERS))
        self.connect_timeout = connect_timeout
        self.http_timeout = http_timeout
        # far (routed / slow) networks: small pool that ramps, long connect timeout, a canary watching the route
        self.far_workers = max(1, min(int(far_workers), MAX_WORKERS))
        self.far_connect_timeout, self.far_http_timeout = far_connect_timeout, far_http_timeout
        self.far_rtt_ms = float(far_rtt_ms)
        self.far_start, self.far_step, self.far_tick = far_start, far_step, float(far_tick)
        # the second look at quiet hosts, at low concurrency and with a long timeout
        self.retry = bool(retry)
        self.retry_workers = max(1, int(retry_workers))
        self.retry_connect_timeout = retry_connect_timeout
        self.retry_seconds = float(retry_seconds)
        # the quick round-trip check that decides near or far
        self.probe_timeout, self.probe_seconds = probe_timeout, float(probe_seconds)
        self.pause_seconds = float(pause_seconds)   # longest a far scan waits for its canary to answer again
        self._connector = connector            # tests: a function (ip, port, timeout) -> socket; None = real connects
        self.cooldown = cooldown
        self.use_ssdp, self.ssdp_seconds = ssdp, ssdp_seconds
        self.ssdp_ports, self.ssdp_bind, self.ssdp_join = tuple(ssdp_ports), ssdp_bind, ssdp_join
        self.use_zeroconf, self.zeroconf_seconds, self._zc_module = zeroconf, zeroconf_seconds, zeroconf_module
        self._local_ip_fn = local_ip or self._default_local_ip
        self.keep = keep
        self.host_timeout = float(host_timeout)    # wall-clock limit for ONE host, all probes together
        self.scan_timeout = float(scan_timeout)    # hard wall-clock limit for a whole scan
        self.cancel_grace = float(cancel_grace)    # after cancel, how long to wait for stragglers
        self._tl = threading.local()               # per-worker: this host's deadline, profile and the scan's cancel flag
        self._lock = threading.RLock()
        self._scans = {}
        self._running = None
        self._last_end = 0.0

    # ---- public API --------------------------------------------------------

    def suggestions(self) -> dict:
        """Default networks to offer: the /24 of every already-configured printer's IP address
        (the Hub cannot see the LAN by itself in Docker), de-duplicated, then - when it looks like
        a real LAN rather than a Docker bridge - the Hub's own /24."""
        nets, sources = [], {}

        def add(cidr, why):
            if cidr not in sources:
                nets.append(cidr)
                sources[cidr] = why
        for rec in self._records():
            for host in self._record_hosts(rec):
                try:
                    ip = ipaddress.IPv4Address(host)
                except ValueError:
                    continue
                net = ipaddress.ip_network(f"{ip}/24", strict=False)
                if any(net.subnet_of(a) for a in _ALLOWED):
                    add(str(net), f"used by {rec.get('name') or 'a printer'}")
        try:
            own = self._local_ip_fn()
            if own:
                ip = ipaddress.IPv4Address(own)
                if ip.is_private and not ip.is_loopback and ip not in ipaddress.ip_network("172.16.0.0/12"):
                    add(str(ipaddress.ip_network(f"{ip}/24", strict=False)), "this Hub's own network")
        except Exception:
            pass
        return {"networks": nets, "sources": sources, "note": SUGGEST_NOTE}

    def start(self, networks=None) -> dict:
        """Begin a scan on a background thread -> {"scan_id","total","networks"}. `networks` None or
        empty = the suggestions. Raises DiscoveryError (400 bad input, 409 a scan is running,
        429 too soon after the last one)."""
        if networks is None or (isinstance(networks, (list, tuple)) and not [x for x in networks if str(x).strip()]):
            networks = self.suggestions()["networks"]
            if not networks:
                raise DiscoveryError("No network to scan yet - type one, for example 192.168.1.0/24.")
        nets = parse_networks(networks)
        hosts = hosts_of(nets)
        with self._lock:
            self._release_if_wedged()
            if self._running is not None:
                raise Busy("A scan is already running. Wait for it to finish (or stop it).", self._running)
            wait = self._last_end + self.cooldown - time.monotonic()
            if wait > 0:
                raise RateLimited(f"Please wait {wait:.0f} more second(s) before scanning again.", wait)
            scan = _Scan(uuid.uuid4().hex[:12], [str(n) for n in nets], hosts)
            scan.hard_deadline = time.monotonic() + self.scan_timeout + self.cancel_grace + 30.0 \
                + self.ssdp_seconds + self.zeroconf_seconds
            scan.known = self._known()
            self._scans[scan.id] = scan
            while len(self._scans) > self.keep:
                oldest = next((i for i, s in self._scans.items() if s.state != "running"), None)
                if oldest is None:
                    break
                del self._scans[oldest]
            self._running = scan.id
        threading.Thread(target=self._run, args=(scan, nets), name=f"discover-{scan.id}", daemon=True).start()
        return {"scan_id": scan.id, "total": scan.total, "networks": scan.networks}

    def status(self, scan_id) -> dict:
        with self._lock:
            scan = self._scans.get(scan_id)
            if scan is None:
                raise NotFound("No such scan (it may be old - start a new one).")
            found = [self._public(copy.deepcopy(f)) for f in self._sorted(scan.found)]
            end = scan.finished or time.time()
            far = [g for g in scan.groups if g["kind"] == "far"]
            return {"scan_id": scan.id, "state": scan.state, "done": scan.done, "total": scan.total,
                    "finished": scan.state != "running", "networks": list(scan.networks),
                    "found": found, "error": scan.error, "note": scan.note, "extras": dict(scan.extras),
                    "elapsed_s": round(end - scan.started, 1),
                    "phase": scan.phase if scan.state == "running" else "finished",
                    "remote": bool(far), "remote_networks": [n for g in far for n in g["networks"]],
                    "profiles": [dict(x) for x in scan.profiles], "eta_s": self._eta(scan),
                    "slowed": scan.backoffs > 0,
                    "recheck": {"total": scan.retry_total, "done": scan.retry_done}}

    def _eta(self, scan):
        """Seconds still to go (a plain estimate, None when not known yet or finished). Real progress
        replaces the per-address guess as soon as a few addresses are done."""
        if scan.state != "running" or not scan.groups:
            return None
        now, left, seen = time.monotonic(), 0.0, False
        for g in scan.groups:
            if g is scan.cur and g["started"] is not None:
                seen = True
                done = max(0, min(g["total"], scan.done - g["base"]))
                per = (now - g["started"]) / done if done >= max(8, g["total"] // 20) else g["est"]
                left += (g["total"] - done) * per
            elif seen or g["started"] is None:
                left += g["total"] * g["est"]
        if scan.phase == "rechecking":
            left = max(0, scan.retry_total - scan.retry_done) * 3.0 / max(1, self.retry_workers)
        if scan.limit_at is not None:
            left = min(left, max(0.0, scan.limit_at - now))
        return int(round(left))

    def cancel(self, scan_id) -> dict:
        with self._lock:
            scan = self._scans.get(scan_id)
            if scan is None:
                raise NotFound("No such scan.")
            scan.cancel.set()
            if scan.cancelled_at is None:
                scan.cancelled_at = time.monotonic()
        return {"scan_id": scan_id, "cancelling": True}

    def _release_if_wedged(self):
        """Watchdog (caller holds the lock): a scan that overran its hard limit, or was cancelled and
        still has not stopped, must not lock every later scan out with 'Busy'."""
        sid = self._running
        if sid is None:
            return
        scan = self._scans.get(sid)
        now = time.monotonic()
        wedged = scan is None or (scan.hard_deadline is not None and now > scan.hard_deadline) or \
            (scan.cancelled_at is not None and now - scan.cancelled_at > self.cancel_grace + 5.0)
        if not wedged:
            return
        if scan is not None and scan.state == "running":
            scan.state = "error" if not scan.cancel.is_set() else "cancelled"
            scan.error = scan.error or "The scan stopped answering and was abandoned."
            scan.finished = scan.finished or time.time()
            scan.cancel.set()
        print(f"[discovery] scan {sid} was wedged; released so new scans can start", flush=True)
        self._running = None
        self._last_end = now

    def scan_and_wait(self, networks=None, timeout=300.0) -> dict:
        """Blocking helper for the MCP tool: start (or refuse), wait, return the final status."""
        sid = self.start(networks)["scan_id"]
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            st = self.status(sid)
            if st["finished"]:
                return st
            time.sleep(0.2)
        self.cancel(sid)
        time.sleep(0.5)
        return self.status(sid)

    # ---- fleet awareness ---------------------------------------------------

    def _records(self) -> list:
        try:
            recs = self.fleet_records() or []
        except Exception:
            recs = []
        return [r for r in recs if isinstance(r, dict)]

    @staticmethod
    def _record_hosts(rec) -> list:
        vals = rec.get("values") if isinstance(rec.get("values"), dict) else {}
        out = []
        for v in (vals.get("ip"), vals.get("url"), rec.get("address")):
            h = _host_of(v or "")
            if h and h not in out:
                out.append(h)
        return out

    def _known(self) -> dict:
        names, hosts, serials = set(), {}, {}
        for rec in self._records():
            name = rec.get("name") or ""
            if name:
                names.add(name.lower())
            fam = rec.get("type")
            for h in self._record_hosts(rec):
                hosts[(h, fam)] = name
            vals = rec.get("values") if isinstance(rec.get("values"), dict) else {}
            if vals.get("serial"):
                serials[str(vals["serial"]).upper()] = name
        return {"names": names, "hosts": hosts, "serials": serials}

    @staticmethod
    def _default_local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))        # UDP connect sends nothing; it just picks the route
            return s.getsockname()[0]
        finally:
            s.close()

    # ---- near or far -------------------------------------------------------

    def _connect(self, ip, port, timeout):
        """One TCP connect (tests swap in a simulated network through `connector`)."""
        fn = self._connector
        return fn(ip, port, timeout) if fn else socket.create_connection((ip, port), timeout=timeout)

    def _ct(self) -> float:
        p = getattr(self._tl, "profile", None)
        return p.connect_timeout if p else self.connect_timeout

    def _ht(self) -> float:
        p = getattr(self._tl, "profile", None)
        return p.http_timeout if p else self.http_timeout

    def _attached(self, net) -> bool:
        """True when the Hub itself sits on `net` (so it is one hop away): loopback, or the /24 of its own address."""
        if net.network_address.is_loopback:
            return True
        try:
            own = ipaddress.IPv4Address(self._local_ip_fn())
            return net.overlaps(ipaddress.ip_network(f"{own}/24", strict=False))
        except Exception:
            return False

    @staticmethod
    def _url_port(url, default=80):
        try:
            u = urllib.parse.urlparse(url if "://" in (url or "") else f"http://{url or ''}")
            return u.port or (443 if u.scheme == "https" else default)
        except ValueError:
            return default

    def _known_endpoints(self, net) -> list:
        """[(ip, port)] of printers the Hub already has inside `net` - they are known to answer, so they make
        the best canaries and the best round-trip samples."""
        out = []
        for rec in self._records():
            vals = rec.get("values") if isinstance(rec.get("values"), dict) else {}
            port = self.ports["bambu_mqtt"] if rec.get("type") == "bambu" \
                else self._url_port(vals.get("url") or rec.get("address") or "")
            for h in self._record_hosts(rec):
                try:
                    ip = ipaddress.IPv4Address(h)
                except ValueError:
                    continue
                if ip in net and (str(ip), port) not in out:
                    out.append((str(ip), port))
        return out

    def _timed_probe(self, ip, port):
        """(ip, port, seconds) when the host answered at all (open OR refused), else None."""
        t0 = time.monotonic()
        try:
            self._connect(ip, port, self.probe_timeout).close()
        except ConnectionRefusedError:
            pass
        except OSError:
            return None
        return ip, port, time.monotonic() - t0

    def _sample(self, net, known, seconds) -> list:
        """Round trips to the first hosts that answer: known printers first, then the gateway-ish addresses
        (.1 .2 and the top of the range) on the printer ports. Stops after three answers or `seconds`."""
        hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
        picks = [hosts[i] for i in (0, 1, -1, -2, len(hosts) // 2) if -len(hosts) <= i < len(hosts)] if hosts else []
        ports = list(dict.fromkeys([self.http_ports[0], self.ports["moonraker"], self.ports["bambu_mqtt"]]))
        targets = list(known[:3])
        for h in dict.fromkeys(str(x) for x in picks):
            targets += [(h, pt) for pt in ports if (h, pt) not in targets]
        targets = targets[:24]
        found, pend = [], set()
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="disc-probe")
        try:
            futs = [ex.submit(self._timed_probe, ip, pt) for ip, pt in targets]
            vouched = set(futs[:min(3, len(known))])        # the known printers must all be heard before stopping
            pend = set(futs)
            end = time.monotonic() + seconds
            while pend and time.monotonic() < end and (len(found) < 3 or vouched & pend):
                done, pend = concurrent.futures.wait(pend, timeout=0.1,
                                                     return_when=concurrent.futures.FIRST_COMPLETED)
                found += [r for r in (f.result() for f in done) if r]
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        return found

    def _classify(self, scan, net, end) -> dict:
        """near or far, how we know, and the canaries (hosts that answer) for a far network."""
        overlay = net.subnet_of(OVERLAY)
        if self._attached(net) and not overlay:
            return {"kind": "near", "rtt_ms": None, "why": "directly attached", "canaries": []}
        left = end - time.monotonic()
        known_eps = self._known_endpoints(net)
        samples = self._sample(net, known_eps, min(self.probe_seconds, left)) if left > 0.3 else []
        want = set(known_eps)                  # known printers first: they are certain to be there
        canaries = [(ip, pt) for ip, pt, _ in sorted(samples, key=lambda x: (x[:2] not in want, x[2]))][:3]
        if samples:                            # the first connect over a cold path is often slow: time the best again
            best = min(samples, key=lambda x: x[2])
            again = self._timed_probe(best[0], best[1])
            rtt = round(min(best[2], again[2] if again else best[2]) * 1000, 1)
        else:
            rtt = None
        if overlay:
            kind, why = "far", "VPN overlay address range"
        elif not samples:
            kind, why = "far", "no host answered the quick round-trip check"
        elif rtt > self.far_rtt_ms:
            kind, why = "far", f"round trip {rtt:g} ms"
        else:
            kind, why = "near", f"round trip {rtt:g} ms"
        return {"kind": kind, "rtt_ms": rtt, "why": why, "canaries": canaries if kind == "far" else []}

    def _est_host(self, prof) -> float:
        """Seconds of scan time one address costs on a mostly empty network (a quiet address is two to three
        timeouts, shared across the pool) - used for the time estimate before real progress is known."""
        return 0.8 * 3 * prof.connect_timeout / prof.workers

    def _plan(self, scan, nets, limit):
        """Decide near / far for every network and group the addresses: near groups first (fast), far last."""
        end = time.monotonic() + max(6.0, 2 * self.probe_seconds)
        info = []
        by_kind = {"near": [], "far": []}
        for net in nets:
            if scan.cancel.is_set():
                break
            c = self._classify(scan, net, min(end, limit))
            info.append({"network": str(net), "kind": c["kind"], "rtt_ms": c["rtt_ms"], "why": c["why"]})
            by_kind[c["kind"]].append((net, c))
        scan.profiles = info
        for kind in ("near", "far"):
            items = by_kind[kind]
            if not items:
                continue
            hosts = hosts_of([n for n, _ in items])
            prof = self._far_profile() if kind == "far" else self._near_profile()
            scan.groups.append({"kind": kind, "profile": prof, "hosts": hosts, "total": len(hosts),
                                "networks": [str(n) for n, _ in items], "base": 0, "started": None,
                                "canaries": [cn for _, c in items for cn in c["canaries"]],
                                "est": self._est_host(prof)})

    def _near_profile(self):
        return _Profile("near", self.workers, self.connect_timeout, self.http_timeout)

    def _far_profile(self):
        return _Profile("far", self.far_workers, self.far_connect_timeout, self.far_http_timeout, gentle=True)

    def _retry_profile(self):
        return _Profile("retry", self.retry_workers, max(self.retry_connect_timeout, self.far_connect_timeout),
                        max(self.far_http_timeout, self.http_timeout))

    # ---- the scan thread ---------------------------------------------------

    def _run(self, scan, nets):
        extras = {}
        try:
            if self.use_ssdp:
                extras["ssdp"] = self._start_extra(self._ssdp_listen, scan)
            if self.use_zeroconf:
                extras["zeroconf"] = self._start_extra(self._zeroconf_browse, scan)
            limit = time.monotonic() + self.scan_timeout
            scan.limit_at = limit
            scan.phase = "checking"
            self._plan(scan, nets, limit)
            scan.phase = "scanning"
            ok = True
            for g in scan.groups:
                if not self._scan_group(scan, g, limit):
                    ok = False
                    break
            if ok and not scan.cancel.is_set():
                self._recheck(scan, limit)
            scan.phase = "finishing"
            for key, (th, box) in extras.items():
                th.join(timeout=self.ssdp_seconds + self.zeroconf_seconds + 2)
            self._merge_extras(scan, nets, {k: box for k, (th, box) in extras.items()})
            with self._lock:
                timed_out = scan.note is not None and scan.note.startswith("Stopped at the time limit")
                scan.state = "cancelled" if (scan.cancel.is_set() and not timed_out) else "done"
        except Exception as e:                     # a scan must never take the Hub down
            with self._lock:
                scan.state, scan.error = "error", f"The scan failed unexpectedly: {type(e).__name__}: {e}"
        finally:
            with self._lock:
                if scan.finished is None:
                    scan.finished = time.time()
                if self._running == scan.id:       # a wedged scan may already have been released
                    self._last_end = time.monotonic()
                    self._running = None

    def _scan_group(self, scan, g, limit) -> bool:
        """Scan one group of addresses with its profile. False = the scan was stopped (cancel / time limit)."""
        prof = g["profile"]
        gate = _Gate(prof.workers, self.far_start, self.far_step) if prof.gentle else None
        g["base"], g["started"] = scan.done, time.monotonic()
        scan.cur = g
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=prof.workers, thread_name_prefix=f"disc-{prof.kind}")
        try:
            scan.ctx = (prof, gate, False)
            pending = {ex.submit(self._host_task, scan, ip) for ip in g["hosts"]}
            return self._drain(scan, pending, limit, gate=gate, canaries=list(g["canaries"]))
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    def _drain(self, scan, pending, limit, gate=None, canaries=(), soft_end=None) -> bool:
        """Wait for `pending` host tasks. Applies the cancel / time-limit rules, and for a far group keeps
        the canary watch running. `soft_end` = stop waiting quietly then (the second look is time-boxed).
        Returns False when the scan was stopped."""
        abandon_at = None
        st = {"good": time.monotonic(), "canaries": list(canaries), "limit": limit}
        next_tick = time.monotonic() + self.far_tick
        while pending:
            done, pending = concurrent.futures.wait(pending, timeout=0.2,
                                                    return_when=concurrent.futures.FIRST_COMPLETED)
            now = time.monotonic()
            if soft_end is not None and now > soft_end and not scan.cancel.is_set():
                for f in pending:
                    f.cancel()
                scan.retry_skipped = scan.retry_total - scan.retry_done
                return True
            if scan.cancel.is_set() and abandon_at is None:
                abandon_at = now + self.cancel_grace
            if now > limit and abandon_at is None:
                scan.note = ("Stopped at the time limit; "
                             f"{len(pending)} address(es) were not finished.")
                scan.cancel.set()
                abandon_at = now + self.cancel_grace
            if abandon_at is not None and now > abandon_at:
                if not scan.note and pending:
                    scan.note = f"Stopped; {len(pending)} address(es) did not answer in time and were skipped."
                return False                   # stragglers die on their own wall-clock limits; do not wait
            if gate is not None and now >= next_tick and not scan.cancel.is_set():
                self._watch(scan, gate, st)
                next_tick = time.monotonic() + self.far_tick
        return abandon_at is None

    def _canary_alive(self, canaries) -> bool:
        for ip, port in canaries[:2]:
            if self._timed_probe(ip, port):
                return True
        return False

    def _watch(self, scan, gate, st):
        """One tick of the route watch for a far group: widen the pool a step while the canary answers;
        when it stops answering the route is overloaded - hold new hosts back, halve the pool, wait for the
        canary to come back, and mark every quiet host probed meanwhile for a second look."""
        if not st["canaries"] or self._canary_alive(st["canaries"]):
            st["good"] = time.monotonic()
            gate.grow()
            return
        with self._lock:
            scan.backoffs += 1
        gate.pause(True)
        gate.shrink()
        end = time.monotonic() + self.pause_seconds
        alive = False
        while time.monotonic() < min(end, st["limit"]) and not scan.cancel.is_set():
            time.sleep(0.4)
            if self._canary_alive(st["canaries"]):
                alive = True
                break
        gate.pause(False)
        with self._lock:
            for ip, t in scan.quiet.items():
                if t >= st["good"]:
                    scan.tainted.add(ip)
        if not alive:
            st["canaries"] = []                # it never came back: the host itself is probably off, stop trusting it
        st["good"] = time.monotonic()

    def _recheck(self, scan, limit):
        """The second look: hosts that gave no reply at all get one more pass at low concurrency and a long timeout.
        Always for printers the Hub already knows and for hosts probed while the route was dropping packets; for
        every quiet host of a far network that found nothing and had no canary to vouch for it. Time-boxed."""
        if not self.retry:
            return
        with self._lock:
            quiet = dict(scan.quiet)
            found = {f["address"] for f in scan.found}
        known = {h for (h, _fam) in scan.known["hosts"]}
        todo = [ip for ip in quiet if ip not in found]
        first = [ip for ip in todo if ip in known or ip in scan.tainted]
        rest = [ip for ip in todo if ip not in set(first)]
        bulk = []
        for g in scan.groups:
            if g["kind"] != "far" or g["canaries"]:
                continue
            members = set(g["hosts"])
            if not any(f["address"] in members for f in scan.found):
                bulk += [ip for ip in rest if ip in members]
        if bulk:
            anchors = []
            for h in [*found, *known]:
                try:
                    anchors.append(int(ipaddress.IPv4Address(h)))
                except ValueError:
                    pass
            bulk.sort(key=lambda ip: (min((abs(int(ipaddress.IPv4Address(ip)) - a) for a in anchors), default=0),
                                      int(ipaddress.IPv4Address(ip))))
        cand = first + bulk
        if not cand or scan.cancel.is_set():
            return
        scan.phase = "rechecking"
        scan.retry_total, scan.retry_done = len(cand), 0
        prof = self._retry_profile()
        soft_end = min(limit, time.monotonic() + self.retry_seconds)
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=prof.workers, thread_name_prefix="disc-retry")
        try:
            scan.ctx = (prof, None, True)
            pending = {ex.submit(self._host_task, scan, ip) for ip in cand}
            self._drain(scan, pending, limit, soft_end=soft_end)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            scan.retry_closed = True
            if scan.retry_skipped and not scan.note:
                scan.note = (f"Looked again at {scan.retry_done} of {scan.retry_total} quiet addresses "
                             "before the time box for that ended.")

    @staticmethod
    def _start_extra(fn, scan):
        box = {}
        th = threading.Thread(target=lambda: fn(scan, box), daemon=True)
        th.start()
        return th, box

    def _host_task(self, scan, ip):
        prof, gate, retry = scan.ctx            # set by _scan_group / _recheck before the tasks are queued
        items, verdict, entered = [], None, True
        if gate is not None:
            entered = gate.enter(scan.cancel)
        try:
            if entered and not scan.cancel.is_set():
                self._tl.cancel, self._tl.profile, self._tl.verdict = scan.cancel, prof, None
                try:
                    items = self._scan_host(ip)
                except Exception:
                    items = []
                finally:
                    verdict = self._tl.verdict
                    self._tl.cancel = self._tl.profile = self._tl.verdict = None
        finally:
            if gate is not None and entered:
                gate.leave()
        with self._lock:
            if scan.cancel.is_set() and scan.state != "running":
                return                              # a straggler finishing after the scan was closed
            if retry and scan.retry_closed:
                return
            for it in items:
                self._add_found(scan, it)
            if verdict is not None:
                if verdict.get("replied"):
                    scan.responders += 1
                if items or (verdict.get("replied") and not verdict.get("unsure")):
                    scan.quiet.pop(ip, None)           # found, or every key port gave a clear yes / no
                elif not retry:
                    scan.quiet[ip] = time.monotonic()  # silent, or only partly answered: a second look may be worth it
                    if verdict.get("shaky"):
                        scan.tainted.add(ip)           # open but unreadable: always worth another try
            if retry:
                scan.retry_done += 1
            else:
                scan.done += 1

    def _add_found(self, scan, item):          # caller holds the lock
        item["source"] = item.get("source", "tcp")
        known = scan.known
        host = item["address"]
        item["already_added"], item["already_added_as"] = False, None
        if item["family"] == "bambu" and item.get("serial") and item["serial"].upper() in known["serials"]:
            item["already_added"], item["already_added_as"] = True, known["serials"][item["serial"].upper()]
        else:
            for h in (host, _norm_host(item.get("hostname") or "")):
                if h and (h, item["family"]) in known["hosts"]:
                    item["already_added"], item["already_added_as"] = True, known["hosts"][(h, item["family"])]
                    break
        item["suggested_name"] = suggest_name(item, known["names"] | scan.names)
        scan.names.add(item["suggested_name"].lower())
        scan.found.append(item)

    @staticmethod
    def _sorted(found):
        return sorted(found, key=lambda f: (int(ipaddress.ip_address(f["address"])), f["family"]))

    def _public(self, f) -> dict:
        fam, ip, port = f["family"], f["address"], f["port"]
        url = None
        if fam != "bambu":
            url = f"http://{ip}" if port == 80 else f"http://{ip}:{port}"
        if fam == "bambu":
            values = {"name": f["suggested_name"], "ip": ip, "serial": f.get("serial") or ""}
        else:
            values = {"name": f["suggested_name"], "url": url}
            if fam == "prusalink" and f.get("model"):
                values["model"] = f["model"]
        return {"id": f"{fam}@{ip}:{port}", "family": fam, "label": FAMILY_LABELS.get(fam, fam),
                "address": ip, "port": port, "url": url, "model": f.get("model"),
                "serial": f.get("serial"), "hostname": f.get("hostname"),
                "already_added": f["already_added"], "already_added_as": f["already_added_as"],
                "confidence": f.get("confidence", "high"), "source": f["source"],
                "details": f.get("details") or {}, "suggested_name": f["suggested_name"],
                "prefill": {"type": fam, "values": values}}

    # ---- per-host fingerprinting ------------------------------------------

    def _remaining(self):
        dl = getattr(self._tl, "deadline", None)
        return None if dl is None else dl - time.monotonic()

    def _budget(self, want) -> float:
        """Seconds the next phase may take: `want`, never more than what is left of this host's wall-clock
        limit, and 0 once the host is out of time or the scan was cancelled."""
        cancel = getattr(self._tl, "cancel", None)
        if cancel is not None and cancel.is_set():
            return 0.0
        r = self._remaining()
        return float(want) if r is None else max(0.0, min(float(want), r))

    def _tcp_probe(self, ip, port) -> str:
        """'open' | 'closed' (host answered with a reset) | 'silent' (timeout / no route)."""
        t = self._budget(self._ct())
        if t <= 0:
            return "silent"
        try:
            self._connect(ip, port, t).close()
            return "open"
        except ConnectionRefusedError:
            return "closed"
        except OSError:
            return "silent"

    def _scan_host(self, ip) -> list:
        """Fingerprint one host within `host_timeout` seconds of wall-clock time, however it behaves."""
        self._tl.deadline = time.monotonic() + self.host_timeout
        try:
            return self._scan_host_inner(ip)
        finally:
            self._tl.deadline = None

    def _scan_host_inner(self, ip) -> list:
        p = self.ports
        # The three key ports first: 80 (Mainsail, Fluidd, OctoPi, PrusaLink), 7125 (Moonraker), 8883 (Bambu).
        # A host is called empty only when ALL THREE stayed silent and nothing else answered: one or two
        # timeouts can be a firewall that drops some ports (a Pi that only opens 80) or one lost SYN.
        key = list(dict.fromkeys([*self.http_ports[:1], p["moonraker"], p["bambu_mqtt"]]))
        order = list(dict.fromkeys([*key, *self.http_ports, p["bambu_ftps"], p["bambu_camera"]]))
        state = {}
        for port in order:
            if self._budget(1) <= 0:
                break                          # out of time: judge by what has been seen
            state[port] = self._tcp_probe(ip, port)
            if (state[port] == "silent" and all(state.get(k) == "silent" for k in key)
                    and all(v == "silent" for v in state.values())):
                break                          # all key ports timed out and no reply at all: nobody is home
        # replied: something answered. unsure: a key port stayed silent - a lost SYN or a firewall, can't tell which
        self._tl.verdict = {"replied": any(v != "silent" for v in state.values()),
                            "unsure": any(state.get(k) == "silent" for k in key)}
        is_open = lambda port: state.get(port) == "open"   # noqa: E731
        self._tl.io_failed = False
        found = []
        if is_open(p["bambu_mqtt"]) and self._budget(1) > 0:
            b = self._fingerprint_bambu(ip, p["bambu_mqtt"], is_open(p["bambu_ftps"]), is_open(p["bambu_camera"]))
            if b:
                found.append(b)
        http_found = {}
        for port in dict.fromkeys([p["moonraker"], *self.http_ports]):
            if not is_open(port) or self._budget(1) <= 0:
                continue
            it = self._fingerprint_http(ip, port)
            if not it:
                continue
            old = http_found.get(it["family"])
            if old is None or self._prefer(it, old):
                http_found[it["family"]] = it
        found.extend(http_found.values())
        if not found and self._tl.io_failed and any(
                is_open(pt) for pt in (p["bambu_mqtt"], p["moonraker"], *self.http_ports)):
            # a port was open but the follow-up request got no answer: very likely a dropped connect
            self._tl.verdict["unsure"] = self._tl.verdict["shaky"] = True
        return found

    def _prefer(self, new, old) -> bool:
        """Same host, same family on two ports (Mainsail on :80 in front of Moonraker :7125, OctoPi's
        proxy on :80 in front of :5000): keep Moonraker's own port, otherwise the first listed port."""
        if new["family"] == "moonraker":
            return new["port"] == self.ports["moonraker"] and old["port"] != self.ports["moonraker"]
        order = self.http_ports
        pos = lambda it: order.index(it["port"]) if it["port"] in order else len(order)   # noqa: E731
        return pos(new) < pos(old)

    # -- Bambu

    def _tls_cert(self, ip, port):
        der = self._tls_cert_raw(ip, port)
        if der is None:
            self._tl.io_failed = True              # nothing came back: a dropped connect looks like this
        return der

    def _tls_cert_raw(self, ip, port):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        total = self._budget(self._ct() + self._ht())
        if total <= 0:
            return None
        t0 = time.monotonic()
        raw = tls = None
        try:
            raw = self._connect(ip, port, min(self._ct(), total))
            left = total - (time.monotonic() - t0)
            if left <= 0:
                return None
            raw.settimeout(min(self._ht(), left))
            tls = ctx.wrap_socket(raw, server_hostname=None, do_handshake_on_connect=False)
            with _Watchdog(tls, left) as wd:          # the handshake can drip one byte a second forever
                tls.do_handshake()
                der = tls.getpeercert(binary_form=True)
            return None if wd.fired else der
        except (OSError, ssl.SSLError, ValueError):
            return None
        finally:
            for sk in (tls, raw):
                if sk is not None:
                    try:
                        sk.close()
                    except Exception:
                        pass

    def _fingerprint_bambu(self, ip, port, ftps_open, cam_open):
        der = self._tls_cert(ip, port)
        if not der:
            return None
        names = parse_cert_names(der)
        cn = (names.get("subject") or {}).get("CN") or ""
        issuer = (names.get("issuer") or {}).get("CN") or ""
        serial = cn if _SERIAL_RE.match(cn) else None
        model = _model_from_serial(serial) if serial else ""
        details = {"mqtt_port": port, "ftps_open": ftps_open, "camera_port_open": cam_open,
                   "cert_issuer": issuer or None}
        if serial:
            corroborated = ftps_open or cam_open or "bbl" in issuer.lower() or bool(model)
            if not corroborated:
                return None                    # some other MQTT-over-TLS broker that happens to use a serial-like CN
            conf = "high" if (ftps_open or "bbl" in issuer.lower()) else "medium"
            return {"family": "bambu", "address": ip, "port": port, "model": model or None,
                    "serial": serial.upper(), "hostname": None, "details": details, "confidence": conf}
        if ftps_open and cam_open:             # looks like a Bambu but the certificate gave no serial
            return {"family": "bambu", "address": ip, "port": port, "model": None, "serial": None,
                    "hostname": None, "details": details, "confidence": "low"}
        return None

    # -- HTTP families

    def _http_get(self, ip, port, path):
        r = self._http_get_raw(ip, port, path)
        if r is None:
            self._tl.io_failed = True
        return r

    def _http_get_raw(self, ip, port, path):
        total = self._budget(self._ht())
        if total <= 0:
            return None
        t0 = time.monotonic()
        conn = http.client.HTTPConnection(ip, port, timeout=total)
        if self._connector:
            conn._create_connection = lambda addr, timeout=None, source_address=None: \
                self._connector(addr[0], addr[1], timeout)
        try:
            conn.connect()
            left = total - (time.monotonic() - t0)
            if left <= 0:
                return None
            conn.sock.settimeout(left)
            with _Watchdog(conn.sock, left) as wd:    # headers / body may drip forever: wall-clock cut-off
                conn.request("GET", path, headers={"User-Agent": "fabrica-discovery",
                                                   "Accept": "application/json, text/html;q=0.8",
                                                   "Connection": "close"})
                r = conn.getresponse()
                body = r.read(65536)
                result = (r.status, {k.lower(): v for k, v in r.getheaders()}, body)
            return None if wd.fired else result
        except (OSError, http.client.HTTPException, ValueError):
            return None
        finally:
            conn.close()

    def _fingerprint_http(self, ip, port):
        def item(family, **kw):
            d = {"family": family, "address": ip, "port": port, "model": None, "serial": None,
                 "hostname": None, "details": {}, "confidence": "high"}
            d.update(kw)
            return d

        # 1. Moonraker
        r = self._http_get(ip, port, "/server/info")
        if r:
            st, h, body = r
            j = _json(body)
            res = j.get("result") if isinstance(j, dict) and isinstance(j.get("result"), dict) else j
            if st == 200 and isinstance(res, dict) and "klippy_state" in res:
                details = {"klippy_state": res.get("klippy_state"),
                           "moonraker_version": res.get("moonraker_version")}
                host = None
                pi = self._http_get(ip, port, "/printer/info")
                if pi and pi[0] == 200:
                    pj = _json(pi[2])
                    pr = pj.get("result") if isinstance(pj, dict) and isinstance(pj.get("result"), dict) else pj
                    if isinstance(pr, dict):
                        host = pr.get("hostname") or None
                        details["klipper_version"] = pr.get("software_version")
                return item("moonraker", hostname=host, details=details)
            if (st == 401 and isinstance(j, dict) and isinstance(j.get("error"), dict)
                    and "tornado" in h.get("server", "").lower()):
                return item("moonraker", confidence="medium", details={"auth_required": True})
        # 2. /api/version: PrusaLink and OctoPrint both implement it
        r = self._http_get(ip, port, "/api/version")
        if r:
            st, h, body = r
            j = _json(body)
            if st == 200 and isinstance(j, dict):
                blob = " ".join(str(j.get(k) or "") for k in ("text", "original", "server")).lower()
                if "prusalink" in blob or "prusa-link" in blob:
                    return self._prusalink(ip, port, item, version=j.get("original") or j.get("text"))
                if "octoprint" in blob:
                    return item("octoprint", details={"version": j.get("server") or j.get("text")})
            chal = h.get("www-authenticate", "")
            if st in (401, 403) and chal.lower().startswith("digest"):
                m = re.search(r'realm="([^"]*)"', chal)
                return self._prusalink(ip, port, item, auth="digest", realm=m.group(1) if m else None,
                                       confidence="high" if m and m.group(1) == "Printer API" else "medium")
        # 3. the root page: OctoPrint's login page / PrusaLink's web app have telling <title>s
        r = self._http_get(ip, port, "/")
        if r and r[0] in (200, 401, 403):
            t = _title(r[2]).lower()
            if "octoprint" in t:
                return item("octoprint", details={"page_title": _title(r[2]), "api_locked": True})
            if "prusalink" in t or "prusa link" in t:
                return self._prusalink(ip, port, item, confidence="medium")
        return None

    def _prusalink(self, ip, port, item, version=None, auth=None, realm=None, confidence="high"):
        details = {k: v for k, v in (("version", version), ("auth", auth), ("realm", realm)) if v}
        host = model = serial = None
        info = self._http_get(ip, port, "/api/v1/info")        # often open; if locked we just skip it
        if info and info[0] == 200:
            j = _json(info[2])
            if isinstance(j, dict):
                host = j.get("hostname") or None
                model = j.get("printer_type") or j.get("model") or None
                serial = j.get("serial") if isinstance(j.get("serial"), str) else None
                if j.get("nozzle_diameter") is not None:
                    details["nozzle_diameter"] = j["nozzle_diameter"]
        return item("prusalink", hostname=host, model=model, serial=serial, details=details, confidence=confidence)

    # ---- optional multicast extras ----------------------------------------

    def _ssdp_listen(self, scan, box):
        """Collect Bambu NOTIFY packets for ssdp_seconds. Best effort: any failure -> nothing."""
        socks = []
        for port in self.ssdp_ports:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    try:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except OSError:
                        pass
                s.bind((self.ssdp_bind, port))
                if self.ssdp_join:
                    try:
                        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                     socket.inet_aton(SSDP_GROUP) + socket.inet_aton("0.0.0.0"))
                    except OSError:
                        pass
                s.setblocking(False)
                socks.append(s)
            except OSError:
                if s is not None:
                    s.close()
                continue
        hits = box.setdefault("hits", {})
        end = time.monotonic() + self.ssdp_seconds
        try:
            while socks and time.monotonic() < end and not scan.cancel.is_set():
                ready, _, _ = select.select(socks, [], [], 0.2)
                for s in ready:
                    try:
                        data, addr = s.recvfrom(4096)
                    except OSError:
                        continue
                    info = parse_ssdp(data, addr)
                    if info:
                        hits[info["ip"]] = info
        finally:
            for s in socks:
                s.close()

    def _zeroconf_browse(self, scan, box):
        """Host names via zeroconf, ONLY if the library is installed (it is not a dependency)."""
        mod = self._zc_module
        if mod is None:
            try:
                import zeroconf as mod
            except ImportError:
                return
        names = box.setdefault("names", {})
        zc = None
        try:
            zc = mod.Zeroconf()
            types = ["_octoprint._tcp.local.", "_http._tcp.local.", "_moonraker._tcp.local."]
            seen = []

            def on_change(zeroconf, service_type, name, state_change):
                if state_change == mod.ServiceStateChange.Added:
                    seen.append((service_type, name))
            browser = mod.ServiceBrowser(zc, types, handlers=[on_change])
            time.sleep(self.zeroconf_seconds)
            for stype, name in list(seen):
                info = zc.get_service_info(stype, name, timeout=500)
                if not info:
                    continue
                host = (getattr(info, "server", "") or "").rstrip(".")
                for addr in info.parsed_addresses():
                    if host and addr not in names:
                        names[addr] = host
            try:
                browser.cancel()
            except Exception:
                pass
        except Exception:
            pass
        finally:
            if zc is not None:
                try:
                    zc.close()
                except Exception:
                    pass

    def _merge_extras(self, scan, nets, boxes):
        def inside(ip):
            try:
                a = ipaddress.IPv4Address(ip)
            except ValueError:
                return False
            return any(a in n for n in nets)
        with self._lock:
            ssdp = (boxes.get("ssdp") or {}).get("hits", {})
            zc = (boxes.get("zeroconf") or {}).get("names", {})
            scan.extras = {"ssdp_heard": len(ssdp), "zeroconf_names": len(zc)}
            for ip, info in ssdp.items():
                if not inside(ip):
                    continue
                mine = [f for f in scan.found if f["family"] == "bambu" and f["address"] == ip]
                if mine:
                    f = mine[0]
                    f["hostname"] = f.get("hostname") or info["name"]
                    f["serial"] = f.get("serial") or (info["serial"].upper() if info["serial"] else None)
                    f["details"]["ssdp_model_code"] = info["model_code"]
                    if f["serial"] and not f.get("model"):
                        f["model"] = _model_from_serial(f["serial"]) or None
                else:
                    serial = info["serial"].upper() if info["serial"] else None
                    self._add_found(scan, {
                        "family": "bambu", "address": ip, "port": self.ports["bambu_mqtt"],
                        "model": (_model_from_serial(serial) or None) if serial else None,
                        "serial": serial, "hostname": info["name"], "source": "ssdp",
                        "confidence": "medium", "details": {"ssdp_model_code": info["model_code"],
                                                            "connect": info["connect"]}})
            for f in scan.found:
                if not f.get("hostname") and f["address"] in zc and inside(f["address"]):
                    f["hostname"] = zc[f["address"]]
            # a hostname learned late may change the suggested name / already-added match
            for f in scan.found:
                if f["source"] == "tcp" or f.get("hostname"):
                    scan.names.discard(f["suggested_name"].lower())
                    f["suggested_name"] = suggest_name(f, scan.known["names"] | scan.names)
                    scan.names.add(f["suggested_name"].lower())


# ---------------------------------------------------------------- HTTP routes (Starlette)

def routes(manager):
    """[(path, methods, async handler)] for the four endpoints. Register them in THIS order
    (/suggestions must come before /{scan_id}). Starlette is imported here, not at module level."""
    from starlette.responses import JSONResponse

    def fail(e):
        return JSONResponse(e.payload(), status_code=e.status)

    async def start(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        nets = body.get("networks") if isinstance(body, dict) else None
        try:
            return JSONResponse(manager.start(nets), status_code=202)
        except DiscoveryError as e:
            return fail(e)

    async def suggestions(request):
        return JSONResponse(manager.suggestions())

    async def status(request):
        try:
            return JSONResponse(manager.status(request.path_params["scan_id"]))
        except DiscoveryError as e:
            return fail(e)

    async def cancel(request):
        try:
            return JSONResponse(manager.cancel(request.path_params["scan_id"]))
        except DiscoveryError as e:
            return fail(e)

    return [("/api/discover", ["POST"], start),
            ("/api/discover/suggestions", ["GET"], suggestions),
            ("/api/discover/{scan_id}", ["GET"], status),
            ("/api/discover/{scan_id}", ["DELETE"], cancel)]
