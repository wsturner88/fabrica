"""Fleet store + runtime fleet manager — printers are added from the dashboard (or by
an agent), not from a config file.

Three layers, all in this file so bambu.py stays a pure driver module:

  FleetStore    printers.json (non-secret fields) + secrets.json (0600) in the data
                volume. Atomic writes. One-time import of a legacy config.yml.
  FAMILIES      the printer-type registry: key -> label, description, CONFIG_FIELDS
                (what the "Add printer" form is rendered from), probe() ("Test
                connection") and a builder for the driver class. Bambu and OctoPrint
                are described here; PrusaLink / Moonraker are picked up when their
                modules import (each driver ships its own CONFIG_FIELDS + probe()).
  FleetManager  add / edit / remove printers while the Hub is running, keeping the
                shared PRINTERS dict (also used by backup.py) in step with the store.

Rules the manager enforces:
  * a printer never has two live drivers: on edit the old driver is fully
    disconnected (MQTT loop joined, camera stopped) BEFORE the new one connects;
  * remove / edit are refused while the printer is printing unless force=True;
  * secrets (access codes, API keys, passwords) are write-only: no function that
    feeds an API, page or tool ever returns one — only "set" / "not set".
"""

import hashlib
import inspect
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import controls
import setup_guides
from bambu import BambuPrinter, OctoPrintPrinter, make_opener, is_redirect, REDIRECT_MSG

try:  # drivers written by other agents: offered only when they import
    from prusalink import PrusaLinkPrinter
except ImportError:
    PrusaLinkPrinter = None
try:
    from moonraker import MoonrakerPrinter
except ImportError:
    MoonrakerPrinter = None

BUSY_STATES = ("RUNNING", "PREPARE", "PAUSE", "PAUSED")
PROBE_TIMEOUT = 5.0
PROBE_CACHE_SECONDS = 120


# ---------------------------------------------------------------- errors

class FleetError(Exception):
    """A request the fleet cannot honour. `status` is the HTTP code REST answers with;
    str(e) is written for a human (or an agent relaying it to one)."""
    status = 400
    extra: dict = {}

    def payload(self) -> dict:
        return {"error": str(self), **self.extra}


class NotFound(FleetError):
    status = 404


class Conflict(FleetError):
    status = 409


class Busy(Conflict):
    """The printer is mid-job (or has a backup running); retry with force=True to override."""
    def __init__(self, msg):
        super().__init__(msg)
        self.extra = {"busy": True}


class ProbeFailed(FleetError):
    status = 422

    def __init__(self, result):
        msg = result.get("message") or result.get("error") or "Could not connect."
        if result.get("hint"):
            msg = f"{msg} Tip: {result['hint']}"
        super().__init__(msg)
        self.result = result
        self.extra = {"test": result}


# ---------------------------------------------------------------- name / value validation

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_LEGACY_NAME_RE = re.compile(r"""^[^\x00-\x1f"'`<>&\\/]{1,64}$""")
RESERVED_NAMES = {"all"}        # "all" means "every printer" in backup_printer_files


def validate_name(name, legacy=False) -> str:
    n = (name or "").strip() if isinstance(name, str) else ""
    if not n:
        raise FleetError("Give the printer a name.")
    if n.lower() in RESERVED_NAMES:
        raise FleetError(f"'{n}' is reserved - pick another name.")
    if not (_LEGACY_NAME_RE if legacy else NAME_RE).match(n):
        raise FleetError("Printer names are 1-32 characters: letters, digits, dash, underscore "
                         "or dot, starting with a letter or digit (no spaces). Example: P1S or Garage-MK4.")
    return n


def norm_host(h) -> str:
    """One spelling per machine address, so '127.1', 'LOCALHOST', '::1' and '0:0:0:0:0:0:0:1' compare equal to
    what they mean: IPv4 shorthand is expanded, IPv6 is compressed, any loopback address (and 'localhost')
    becomes 127.0.0.1, host names are lower-cased."""
    h = (h or "").strip().lower().strip("[]").rstrip(".")
    if h in ("localhost", "localhost.localdomain"):
        return "127.0.0.1"
    try:
        ip = ipaddress.ip_address(h)
        return "127.0.0.1" if str(ip) == "::1" else str(ip)
    except ValueError:
        pass
    try:
        return norm_host(socket.inet_ntoa(socket.inet_aton(h))) if h.replace(".", "").isdigit() or h.startswith("0x") else h
    except OSError:
        return h


_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$|^[0-9A-Fa-f:]+$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9]{8,24}$")


def _clean_url(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    if "://" not in v:
        v = "http://" + v
    u = urllib.parse.urlparse(v)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FleetError("The address must look like http://192.168.1.50 (http or https, then the "
                         "printer's IP address or host name).")
    # NOTE (N-7): a `user:pass@` typed here is kept in the stored address (the edit form shows it back to the
    # owner; read-scope callers get it scrubbed by security.py). The DRIVERS drop it from the address they
    # use, so it can never reach an error message, a snapshot field or an alert.
    return v.rstrip("/")


# ---------------------------------------------------------------- data store

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _atomic_write(path: str, text: str, mode: int):
    """temp file in the same directory + fsync + rename: a crash leaves the old
    file or the new one, never half of either. The temp file is created 0600 and
    only widened (if `mode` says so) once complete."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as e:
        # Never silently lose a fleet: keep the unreadable file for a human, start fresh.
        bad = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, bad)
        except OSError:
            bad = "(could not move it aside)"
        print(f"[fleet] {path} was unreadable ({e}); moved to {bad}, starting empty", flush=True)
        return default


class FleetStore:
    """printers.json + secrets.json. Thread-safe. Printer records are flat dicts:
    name, type, added_at, updated_at, source, plus that family's non-secret fields
    (ip, serial, url, model, ...). Secrets live only in secrets.json, keyed by
    printer name; a record may carry `legacy_env` ({field: ENV_VAR_NAME}) as a
    read-only fallback for printers imported from an old config.yml."""

    def __init__(self, data_dir: str, env=None):
        self.dir = data_dir
        self.env = os.environ if env is None else env
        self.printers_path = os.path.join(data_dir, "printers.json")
        self.secrets_path = os.path.join(data_dir, "secrets.json")
        self._lock = threading.RLock()
        doc = _read_json(self.printers_path, {})
        if not isinstance(doc, dict):
            doc = {}
        self._records = [r for r in doc.get("printers", []) if isinstance(r, dict) and r.get("name")]
        self._legacy = doc.get("legacy_import") if isinstance(doc.get("legacy_import"), dict) else {}
        sec = _read_json(self.secrets_path, {})
        self._secrets = sec if isinstance(sec, dict) else {}

    # ---- persistence ------------------------------------------------------

    def _save(self, secrets_first=True):
        os.makedirs(self.dir, exist_ok=True)
        self._legacy = {**self._legacy, "checked": True}   # once saved, never import again
        doc = {"version": 1, "legacy_import": self._legacy, "printers": self._records}
        sec_text = json.dumps(self._secrets, indent=2, sort_keys=True)
        prn_text = json.dumps(doc, indent=2)
        if secrets_first:      # a printer must never exist without its secrets
            _atomic_write(self.secrets_path, sec_text, 0o600)
            _atomic_write(self.printers_path, prn_text, 0o600)
        else:                  # removal: forget the printer first, then its secrets
            _atomic_write(self.printers_path, prn_text, 0o600)
            _atomic_write(self.secrets_path, sec_text, 0o600)

    # ---- reads ------------------------------------------------------------

    def records(self) -> list:
        with self._lock:
            return [dict(r) for r in self._records]

    def get(self, name: str):
        n = (name or "").lower()
        with self._lock:
            for r in self._records:
                if r["name"].lower() == n:
                    return dict(r)
        return None

    def secret(self, name: str, field: str) -> str:
        """The stored secret (or its legacy env-var fallback). INTERNAL USE ONLY -
        never route this into an API response."""
        with self._lock:
            rec = self.get(name)
            if rec is None:
                return ""
            v = (self._secrets.get(rec["name"]) or {}).get(field, "")
            if v:
                return v
            envname = (rec.get("legacy_env") or {}).get(field)
            return self.env.get(envname, "") if envname else ""

    def secret_is_set(self, name: str, field: str) -> bool:
        return bool(self.secret(name, field))

    @property
    def legacy_checked(self) -> bool:
        with self._lock:
            return bool(self._legacy.get("checked"))

    # ---- writes -----------------------------------------------------------

    def add(self, record: dict, secrets: dict = None):
        with self._lock:
            if self.get(record["name"]):
                raise Conflict(f"A printer named '{record['name']}' already exists.")
            rec = {**record, "added_at": record.get("added_at") or _now()}
            self._records.append(rec)
            self._secrets[rec["name"]] = {k: v for k, v in (secrets or {}).items() if v}
            if not self._secrets[rec["name"]]:
                del self._secrets[rec["name"]]
            try:
                self._save(True)
            except BaseException:
                self._records.remove(rec)
                self._secrets.pop(rec["name"], None)
                raise
            return dict(rec)

    def update(self, name: str, fields: dict, secrets: dict = None, drop_secrets=()):
        """Merge `fields` into the record (keys with None are dropped); non-empty
        `secrets` replace stored ones, absent/empty ones are kept - except the names in
        `drop_secrets`, which are forgotten (the printer moved to another address, so the old
        secret must not follow it)."""
        with self._lock:
            n = (name or "").lower()
            for i, r in enumerate(self._records):
                if r["name"].lower() == n:
                    break
            else:
                raise NotFound(f"No printer named '{name}'.")
            old_rec, old_sec = dict(r), dict(self._secrets.get(r["name"]) or {})
            new = {**r, **fields, "updated_at": _now()}
            new = {k: v for k, v in new.items() if v is not None}
            new["name"] = r["name"]
            self._records[i] = new
            sec = dict(old_sec)
            for k, v in (secrets or {}).items():
                if v:
                    sec[k] = v
            for k in drop_secrets or ():
                if k not in (secrets or {}) or not (secrets or {}).get(k):
                    sec.pop(k, None)
                    if isinstance(new.get("legacy_env"), dict):
                        new["legacy_env"] = {a: b for a, b in new["legacy_env"].items() if a != k} or None
            new = {k: v for k, v in new.items() if v is not None}
            self._records[i] = new
            if sec:
                self._secrets[r["name"]] = sec
            else:
                self._secrets.pop(r["name"], None)
            try:
                self._save(True)
            except BaseException:
                self._records[i] = old_rec
                if old_sec:
                    self._secrets[r["name"]] = old_sec
                else:
                    self._secrets.pop(r["name"], None)
                raise
            return dict(new)

    def remove(self, name: str):
        with self._lock:
            n = (name or "").lower()
            for i, r in enumerate(self._records):
                if r["name"].lower() == n:
                    break
            else:
                raise NotFound(f"No printer named '{name}'.")
            rec = self._records.pop(i)
            sec = self._secrets.pop(rec["name"], None)
            try:
                self._save(False)
            except BaseException:
                self._records.insert(i, rec)
                if sec is not None:
                    self._secrets[rec["name"]] = sec
                raise
            return rec

    def import_legacy(self, printers_cfg, source="config.yml", env=None) -> list:
        """One-time import of the old config.yml `printers:` list. Runs only while the
        store is empty AND has never been saved (so removing every printer later does
        not resurrect them). Secrets named by access_code_env / api_key_env are read
        from the environment now and copied into secrets.json; the env-var NAME is
        also kept as a fallback if the copy came up empty. Returns imported names."""
        env = self.env if env is None else env
        with self._lock:
            if self._records or self._legacy.get("checked"):
                return []
            imported = []
            for p in printers_cfg or []:
                if not isinstance(p, dict):
                    continue
                try:
                    name = validate_name(str(p.get("name", "")), legacy=True)
                except FleetError as e:
                    print(f"[fleet] import: skipping printer {p.get('name')!r}: {e}", flush=True)
                    continue
                if any(r["name"].lower() == name.lower() for r in self._records):
                    continue
                kind = str(p.get("type") or "bambu").lower()
                if kind == "octoprint":
                    rec = {"name": name, "type": "octoprint", "url": str(p.get("url", "")).rstrip("/"),
                           "model": str(p.get("model", "") or "")}
                    env_map = {"api_key": p.get("api_key_env")}
                elif kind == "bambu":
                    rec = {"name": name, "type": "bambu", "ip": str(p.get("ip", "")),
                           "serial": str(p.get("serial", "")), "model": str(p.get("model", "") or "")}
                    env_map = {"access_code": p.get("access_code_env")}
                else:
                    print(f"[fleet] import: skipping '{name}': unknown type '{kind}'", flush=True)
                    continue
                secrets, legacy_env = {}, {}
                for field, envname in env_map.items():
                    if envname:
                        legacy_env[field] = str(envname)
                        if env.get(str(envname)):
                            secrets[field] = env[str(envname)]
                rec.update(source="legacy-config", added_at=_now())
                if legacy_env:
                    rec["legacy_env"] = legacy_env
                self._records.append(rec)
                if secrets:
                    self._secrets[name] = secrets
                imported.append(name)
            self._legacy = {"checked": True, "imported": imported, "source": source, "at": _now()}
            self._save(True)
            return imported


# ---------------------------------------------------------------- probes

def _result(**kw) -> dict:
    out = {"ok": False, "model": None, "firmware": None, "has_camera": False,
           "error": None, "code": None, "message": None, "hint": None, "guide_step": None}
    out.update(kw)
    return out


# Bambu serial-number prefixes (public: printed on the box). Used to show a model
# name in the test result and to pick the right camera behaviour when the user
# leaves the model blank.
SERIAL_MODELS = {
    "01S": "Bambu Lab P1P", "01P": "Bambu Lab P1S", "030": "Bambu Lab A1 mini",
    "039": "Bambu Lab A1", "00M": "Bambu Lab X1 Carbon", "00W": "Bambu Lab X1",
    "03W": "Bambu Lab X1E", "094": "Bambu Lab H2D", "093": "Bambu Lab H2S",
}


def bambu_model_from_serial(serial: str) -> str:
    return SERIAL_MODELS.get((serial or "").strip().upper()[:3], "")


def _bambu_has_camera(model: str) -> bool:
    return BambuPrinter("probe", "", "", "", model or "").has_camera


def probe_bambu(values: dict, timeout: float = PROBE_TIMEOUT, port: int = None) -> dict:
    """TLS + MQTT CONNECT to the printer (user bblp + access code), subscribe to its
    report topic, ask for one status push and wait briefly for it. One short-lived
    client, disconnected before returning. Never raises."""
    ip = (values.get("ip") or "").strip()
    serial = (values.get("serial") or "").strip()
    code = values.get("access_code") or ""
    port = port or BambuPrinter.MQTT_PORT
    if not ip or not serial or not code:
        return _result(error="Fill in the IP address, serial number and access code first.", code="bad_input")
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return _result(error="The MQTT library (paho-mqtt) is not installed in this Hub.", code="internal")

    cid = f"fabrica-probe-{os.urandom(3).hex()}"
    try:  # paho 2.x
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid, protocol=mqtt.MQTTv311)
    except AttributeError:  # paho 1.x
        c = mqtt.Client(client_id=cid, protocol=mqtt.MQTTv311)
    c.username_pw_set("bblp", code)
    c.tls_set(cert_reqs=ssl.CERT_NONE)
    c.tls_insecure_set(True)
    st = {"rc": None, "dropped": False, "payload": None}
    answered, got_msg = threading.Event(), threading.Event()

    def on_connect(client, userdata, flags, rc):
        st["rc"] = rc
        if rc == 0:
            client.subscribe(f"device/{serial}/report")
            client.publish(f"device/{serial}/request", json.dumps(
                {"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}}))
        answered.set()

    def on_disconnect(client, userdata, rc):
        if st["rc"] is None:
            st["dropped"] = True
        answered.set()

    def on_message(client, userdata, msg):
        try:
            st["payload"] = json.loads(msg.payload)
        except Exception:
            st["payload"] = {}
        got_msg.set()

    c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, on_message
    started = False
    try:
        try:
            c.connect(ip, port, keepalive=30)
        except socket.gaierror:
            return _result(error=f"Can't find '{ip}'. Check the IP address.", code="unreachable")
        except ConnectionRefusedError:
            return _result(error=f"The device at {ip} refused the connection on port {port}. Is that a "
                                 "Bambu Lab printer, and is it switched on?", code="refused")
        except (socket.timeout, TimeoutError):
            return _result(error=f"Can't reach {ip} - no answer. Check the IP address, that the printer is "
                                 "on, and that it is on the same network as this Hub.", code="unreachable")
        except ssl.SSLError:
            return _result(error=f"{ip} answered but does not speak Bambu's secure MQTT. Is the address right?",
                           code="tls")
        except OSError as e:
            return _result(error=f"Can't reach {ip}: {e.strerror or e}. Check the IP address and that the "
                                 "printer is on the same network as this Hub.", code="unreachable")
        c.loop_start()
        started = True
        if not answered.wait(max(0.5, timeout)):
            return _result(error=f"{ip} accepted the connection but did not answer in {timeout:.0f} seconds. "
                                 "Check the address and that the printer is on.", code="timeout")
        if st["rc"] in (4, 5):
            return _result(error="Wrong access code - the printer rejected it. Re-read it on the printer "
                                 "screen (it can change when you switch LAN mode on or off).", code="bad_code")
        if st["rc"] not in (0, None):
            return _result(error=f"The printer refused the login (MQTT code {st['rc']}).", code="refused")
        if st["rc"] is None:
            return _result(error="The printer dropped the connection before answering. Check the access "
                                 "code, and that no other app is hogging the printer's connection.", code="dropped")
        if not got_msg.wait(timeout):
            return _result(error=f"Connected to the printer, but it sent no data for serial number {serial}. "
                                 "Double-check the serial number (Settings > Device on the printer).",
                           code="serial_mismatch")
        payload = st["payload"] or {}
        pr = payload.get("print") if isinstance(payload.get("print"), dict) else {}
        model = bambu_model_from_serial(serial) or (values.get("model") or "").strip() or "Bambu Lab printer"
        fw = pr.get("sw_ver") or pr.get("firmware_version") or None
        return _result(ok=True, model=model, firmware=fw, has_camera=_bambu_has_camera(model))
    finally:
        try:
            c.disconnect()
        except Exception:
            pass
        if started:
            try:
                c.loop_stop()
            except Exception:
                pass


def probe_octoprint(values: dict, timeout: float = PROBE_TIMEOUT) -> dict:
    """GET /api/version (checks address + API key) then the webcam discovery the
    driver already uses (/api/settings + a test snapshot). Never raises."""
    try:
        url = _clean_url(values.get("url"))
    except FleetError as e:
        return _result(error=str(e), code="bad_input")
    key = values.get("api_key") or ""
    if not url or not key:
        return _result(error="Fill in the OctoPrint address and API key first.", code="bad_input")
    opener = make_opener()       # no env proxy, and a redirect is an error: the API key goes to THIS host only
    req = urllib.request.Request(url + "/api/version", headers={"X-Api-Key": key})
    try:
        with opener.open(req, timeout=timeout) as r:
            ver = json.loads(r.read(65536) or b"{}")
    except urllib.error.HTTPError as e:
        e.close()
        if is_redirect(e):
            return _result(error=f"{url} answered {e.code}: {REDIRECT_MSG}. Use the exact address OctoPrint "
                                 "is served on (for example the https:// one if it redirects there).",
                           code="redirect")
        if e.code in (401, 403):
            return _result(error="OctoPrint rejected the API key. In OctoPrint: Settings > API (or "
                                 "Application Keys) - copy the key again.", code="bad_key")
        if e.code == 404:
            return _result(error=f"{url} answered, but it does not look like OctoPrint "
                                 "(no /api/version). Check the address and port.", code="not_octoprint")
        return _result(error=f"OctoPrint answered with an error ({e.code} {e.reason}).", code="http")
    except urllib.error.URLError as e:
        why = e.reason if isinstance(e.reason, str) else getattr(e.reason, "strerror", None) or e.reason
        return _result(error=f"Can't reach {url}: {why}. Check the address and that OctoPrint is running "
                             "on the same network as this Hub.", code="unreachable")
    except (socket.timeout, TimeoutError):
        return _result(error=f"Can't reach {url} - no answer in {timeout:.0f} seconds.", code="unreachable")
    except ValueError:
        return _result(error=f"{url} answered, but not with OctoPrint's JSON. Check the address and port.",
                       code="not_octoprint")
    except Exception as e:
        return _result(error=f"Could not talk to {url}: {e}", code="unreachable")
    if not isinstance(ver, dict) or not ("server" in ver or "text" in ver):
        return _result(error=f"{url} answered, but it does not look like OctoPrint.", code="not_octoprint")
    cam = False
    try:
        status, _info = OctoPrintPrinter("probe", url, key)._probe_webcam()
        cam = status == "ok"
    except Exception:
        cam = False
    fw = str(ver.get("server") or "") or None
    return _result(ok=True, model=(values.get("model") or "").strip() or "OctoPrint",
                   firmware=fw, has_camera=cam)


# ---------------------------------------------------------------- field descriptors

BAMBU_MODELS = ["Bambu Lab P1S", "Bambu Lab P1P", "Bambu Lab A1", "Bambu Lab A1 mini",
                "Bambu Lab X1 Carbon", "Bambu Lab X1E", "Bambu Lab H2D", "Bambu Lab H2S"]

NAME_HELP = ("A short name for this printer, used on the dashboard and by AI agents. "
             "Letters, digits, dash, underscore or dot - no spaces. Example: P1S or Garage-MK4. "
             "It cannot be changed later.")

BAMBU_FIELDS = [
    {"name": "name", "label": "Printer name", "type": "text", "required": True,
     "placeholder": "P1S", "help": NAME_HELP},
    {"name": "ip", "label": "IP address", "type": "text", "required": True,
     "placeholder": "192.168.1.100", "guide_step": "ip",
     "help": "On the printer screen: Settings > WLAN (P1/A1) or Settings > Network (X1/H2) shows its IP "
             "address; your router's list of connected devices shows it too. Tip: give it a DHCP "
             "reservation in the router so the address never changes. 'Discover printers' fills this "
             "in for you."},
    {"name": "serial", "label": "Serial number", "type": "text", "required": True,
     "placeholder": "15 letters and digits", "guide_step": "serial",
     "help": "On the printer screen: Settings > Device (or About). It is also printed on the sticker on "
             "the printer's back or underside and on the box. 15 letters and digits."},
    {"name": "access_code", "label": "Access code", "type": "password", "required": True, "secret": True,
     "guide_step": "code",
     "help": "8 characters, on the printer screen: Settings > WLAN (P1/A1) or Settings > Network (X1/H2), "
             "next to LAN Only Mode. Starting prints from the Hub needs the printer in LAN Only mode - "
             "and Developer Mode on recent firmware (see the setup steps). Switching either mode can "
             "change this code: read it AFTER you switch, and if the Hub later says 'wrong access "
             "code', read it again and re-enter it via Edit."},
    {"name": "model", "label": "Model", "type": "select", "required": False,
     "options": [{"value": "", "label": "Detect from the serial number"}]
                + [{"value": m, "label": m} for m in BAMBU_MODELS],
     "help": "Leave on 'Detect' unless the test shows the wrong model. P1 and A1 printers show their "
             "camera in the Hub automatically; X1 and H2 printers use RTSP video, which the Hub does "
             "not support yet (no camera view)."},
]

OCTOPRINT_FIELDS = [
    {"name": "name", "label": "Printer name", "type": "text", "required": True,
     "placeholder": "MK4", "help": NAME_HELP},
    {"name": "url", "label": "OctoPrint address", "type": "url", "required": True,
     "placeholder": "http://192.168.1.50", "guide_step": "url",
     "help": "The address you type in a browser to open OctoPrint - for example http://octopi.local or "
             "http://192.168.1.50. If you leave off http:// the Hub adds it. The printer must be "
             "connected inside OctoPrint (its Connect button) before the Hub can print."},
    {"name": "api_key", "label": "API key", "type": "password", "required": True, "secret": True,
     "guide_step": "apikey",
     "help": "In OctoPrint: Settings > Application Keys - type 'Print Hub' as the name, tap Generate and "
             "copy the long key (recommended: it can be revoked on its own). Older versions: Settings > "
             "API shows the global key."},
    {"name": "model", "label": "Model", "type": "text", "required": False,
     "placeholder": "Prusa MK4S", "help": "Display only - helps you tell printers apart. The webcam is "
                                          "picked up automatically if OctoPrint shows one."},
]


# ---------------------------------------------------------------- registry

def is_secret_field(f: dict) -> bool:
    return bool(f.get("secret")) or f.get("type") == "password"


class Family:
    def __init__(self, key, label, description, fields, probe, build, require_one_of=()):
        # guide / troubleshooting come from setup_guides.py, keyed by the family key
        self.key, self.label, self.description = key, label, description
        self.fields, self._probe, self._build = fields, probe, build
        self.require_one_of = [tuple(g) for g in require_one_of]

    @property
    def field_names(self):
        return [f["name"] for f in self.fields]

    @property
    def secret_fields(self):
        return [f["name"] for f in self.fields if is_secret_field(f)]

    def describe(self) -> dict:
        return {"type": self.key, "label": self.label, "description": self.description,
                "fields": [dict(f) for f in self.fields],
                "require_one_of": [list(g) for g in self.require_one_of],
                "setup_guide": setup_guides.guide_for(self.key),
                "troubleshooting": setup_guides.troubleshooting_for(self.key)}

    def probe(self, values: dict, timeout: float = PROBE_TIMEOUT) -> dict:
        try:
            r = self._probe(values, timeout)
        except Exception as e:      # a probe must never take the request down
            r = _result(error=f"The connection test failed unexpectedly: {type(e).__name__}: {e}", code="internal")
        r = {**_result(), **r}
        r["ok"] = bool(r.get("ok"))
        if r["ok"]:
            bits = []
            if r.get("firmware"):
                bits.append(f"version {r['firmware']}")
            bits.append("camera available" if r.get("has_camera") else "no camera")
            r["message"] = f"Connected — {r.get('model') or self.label} ({', '.join(bits)})"
        else:
            r["message"] = r.get("error") or "Could not connect."
            hint = setup_guides.match_hint(self.key, r)
            if hint:
                r["hint"], r["guide_step"] = hint["text"], hint["step"]
        return r

    def build(self, values: dict):
        return self._build(values)

    def probe_result_ok(self, model, firmware, has_camera) -> dict:
        r = {**_result(ok=True, model=model, firmware=firmware or None, has_camera=has_camera)}
        bits = ([f"version {firmware}"] if firmware else []) + ["camera available" if has_camera else "no camera"]
        r["message"] = f"Connected - {model or self.label} ({', '.join(bits)}); already connected to the Hub"
        return r


def _build_bambu(v):
    return BambuPrinter(v["name"], v["ip"], v["serial"], v.get("access_code", ""), v.get("model", ""))


def _build_octoprint(v):
    return OctoPrintPrinter(v["name"], v["url"], v.get("api_key", ""), v.get("model", ""))


def _generic_family(key, label, description, cls) -> Family:
    """Adapter for the drivers other agents ship: class attributes CONFIG_FIELDS
    (+ optional CONFIG_REQUIRE_ONE_OF) and `@classmethod probe(url, creds, timeout, **opts)`.
    Constructor convention: cls(name, url, creds_dict, model, **extra). Any form field that
    is a named constructor parameter (e.g. storage, snapshot_url) is passed as that keyword;
    every other field except name/url/model goes into creds_dict."""
    fields = [dict(f) for f in cls.CONFIG_FIELDS]
    names = {f["name"] for f in fields}
    ctor = set(inspect.signature(cls.__init__).parameters) - {"self", "name", "url", "creds", "model"}
    pk = set(inspect.signature(cls.probe).parameters) - {"cls", "url", "creds", "timeout"}

    def split(v):
        kw = {k: v[k] for k in names & ctor if v.get(k)}
        creds = {k: v.get(k, "") for k in names - {"name", "url", "model"} - ctor}
        return kw, creds

    def probe(v, timeout):
        kw, creds = split(v)
        opts = {k: val for k, val in kw.items() if k in pk}
        return cls.probe(v.get("url", ""), creds, timeout=timeout, **opts)

    def build(v):
        kw, creds = split(v)
        return cls(v["name"], v["url"], creds, v.get("model", ""), **kw)

    return Family(key, label, description, fields, probe, build,
                  getattr(cls, "CONFIG_REQUIRE_ONE_OF", ()))


def _make_families() -> dict:
    fam = {}
    fam["bambu"] = Family(
        "bambu", "Bambu Lab",
        "Bambu Lab P1, A1, X1 or H2 printers on your network (no Bambu account or cloud needed).",
        BAMBU_FIELDS, lambda v, t: probe_bambu(v, t), _build_bambu)
    fam["octoprint"] = Family(
        "octoprint", "OctoPrint",
        "Any printer run by OctoPrint - for example a Prusa on a Raspberry Pi.",
        OCTOPRINT_FIELDS, lambda v, t: probe_octoprint(v, t), _build_octoprint)
    if PrusaLinkPrinter is not None:
        fam["prusalink"] = _generic_family(
            "prusalink", "PrusaLink",
            "Prusa printers with built-in PrusaLink (MK4, MK3.9, Core One, XL, MINI).", PrusaLinkPrinter)
    if MoonrakerPrinter is not None:
        fam["moonraker"] = _generic_family(
            "moonraker", "Klipper (Moonraker)",
            "Klipper printers running Moonraker, with Fluidd or Mainsail.", MoonrakerPrinter)
    return fam


FAMILIES = _make_families()


def family_for(key: str, families=None) -> Family:
    fams = FAMILIES if families is None else families
    k = (key or "").strip().lower()
    if k not in fams:
        raise FleetError(f"Unknown printer type '{key}'. Available: {', '.join(fams)}.")
    return fams[k]


# ---------------------------------------------------------------- values handling

def normalize_values(fam: Family, values: dict, existing: dict = None, existing_secret=None,
                     editing=False) -> dict:
    """Validate and clean a submitted form. `existing` = the stored record (edit), whose
    non-secret values are the defaults; blank secrets fall back to `existing_secret(field)`.
    Returns the full cleaned dict (secrets included - never log or return it)."""
    values = values if isinstance(values, dict) else {}
    out = {}
    for f in fam.fields:
        n = f["name"]
        raw = values.get(n)
        if isinstance(raw, (int, float)):
            raw = str(raw)
        raw = raw.strip() if isinstance(raw, str) else ""
        if is_secret_field(f):
            if not raw and existing_secret is not None:
                raw = existing_secret(n) or ""
        elif n not in values and existing is not None and n in existing:
            raw = str(existing.get(n) or "")
        if not raw and f.get("default") and not editing:
            raw = str(f["default"])
        out[n] = raw
    out["name"] = validate_name(out.get("name"))
    for f in fam.fields:
        n, v = f["name"], out[f["name"]]
        if f.get("required") and not v:
            raise FleetError(f"'{f['label']}' is required.")
        if not v:
            continue
        if f.get("type") == "url":
            out[n] = _clean_url(v)
        elif f.get("type") == "select":
            if v not in [o["value"] if isinstance(o, dict) else o for o in f.get("options", [])]:
                raise FleetError(f"'{f['label']}' must be one of the listed choices.")
    for group in fam.require_one_of:
        if not any(out.get(g) for g in group):
            labels = " or ".join(next((f["label"] for f in fam.fields if f["name"] == g), g) for g in group)
            raise FleetError(f"Fill in at least one of: {labels}.")
    if fam.key == "bambu":
        ip, serial, code = out["ip"], out["serial"], out["access_code"]
        if "://" in ip or "/" in ip or not _HOST_RE.match(ip):
            raise FleetError("The IP address should be just the address, like 192.168.1.100.")
        if not _SERIAL_RE.match(serial):
            raise FleetError("The serial number should be 8-24 letters and digits (Settings > Device on the printer).")
        out["serial"] = serial.upper()
        if re.search(r"\s", code) or len(code) < 4:
            raise FleetError("The access code is usually 8 characters with no spaces (Settings > WLAN on the printer).")
    return out


def split_values(fam: Family, values: dict):
    """(non-secret fields, secrets) from cleaned values."""
    sec = {n: values[n] for n in fam.secret_fields if values.get(n)}
    plain = {f["name"]: values[f["name"]] for f in fam.fields
             if not is_secret_field(f) and f["name"] != "name"}
    return plain, sec


# ---------------------------------------------------------------- where a printer is (for secret reuse)

def _address_key(fam, get) -> tuple:
    """The destination a stored secret would be sent to: Bambu -> (IP, serial); URL families ->
    (scheme, host, port). `get(field)` returns the effective value of a form field."""
    if fam.key == "bambu":
        return ("bambu", (get("ip") or "").strip().lower(), (get("serial") or "").strip().upper())
    raw = (get("url") or "").strip()
    try:
        u = urllib.parse.urlsplit(raw if "://" in raw else "http://" + raw)
        port = u.port or {"http": 80, "https": 443}.get(u.scheme, 0)
        return (u.scheme, (u.hostname or "").lower(), port)
    except ValueError:
        return ("raw", raw.lower(), 0)


def _given(values: dict, name: str) -> str:
    v = values.get(name)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    return v.strip() if isinstance(v, str) else ""


def address_changed(fam, existing: dict, values: dict) -> bool:
    """True when `values` (a partial edit form) point the printer at a different destination than the
    stored record. A stored secret is only ever reused for the SAME destination."""
    values = values if isinstance(values, dict) else {}

    def new(n):
        return _given(values, n) if n in values else str(existing.get(n) or "")

    return _address_key(fam, new) != _address_key(fam, lambda n: str(existing.get(n) or ""))


def _need_secrets_message(fam, values) -> str:
    """'' when every REQUIRED secret was (re-)entered, else the message asking for them."""
    missing = [f.get("label") or f["name"] for f in fam.fields
               if is_secret_field(f) and f.get("required") and not _given(values, f["name"])]
    if not missing:
        return ""
    return ("The printer's address changed, so its saved " + " and ".join(missing).lower()
            + " is not reused (it would be sent to the new address). Enter it again.")


# ---------------------------------------------------------------- runtime fleet

def stop_driver(prn):
    """Fully stop a driver: its own disconnect() when it has one (Bambu: MQTT loop joined,
    camera stopped; OctoPrint / PrusaLink / Moonraker: camera poller stopped). A driver
    written without one still gets its shared camera reader stopped so no thread outlives
    the printer."""
    fn = getattr(prn, "disconnect", None)
    if callable(fn):
        fn()
        return
    cam = getattr(prn, "_camera", None)
    if cam is not None and hasattr(cam, "stop"):
        cam.stop()


class FleetManager:
    """Add / edit / remove printers on a running Hub. `printers` is the shared
    name.lower() -> driver dict that server.py and backup.py read."""

    def __init__(self, store: FleetStore, printers: dict, families=None, guards=None, log=print):
        self.store = store
        self.printers = printers
        self.families = FAMILIES if families is None else families
        self.guards = guards if guards is not None else []   # callables name -> reason|None
        self.log = log
        self.lock = threading.RLock()
        self._probe_cache = {}

    # ---- views (secrets redacted) ----------------------------------------

    def types(self) -> list:
        return [f.describe() for f in self.families.values()]

    @staticmethod
    def _address(rec) -> str:
        return rec.get("ip") or rec.get("url") or ""

    def public(self, rec: dict) -> dict:
        fam = self.families.get(rec.get("type"))
        secrets = {}
        if fam:
            secrets = {n: ("set" if self.store.secret_is_set(rec["name"], n) else "not set")
                       for n in fam.secret_fields}
        elif rec.get("type") == "bambu":
            secrets = {"access_code": "set" if self.store.secret_is_set(rec["name"], "access_code") else "not set"}
        plain = {k: v for k, v in rec.items() if k not in ("legacy_env",)}
        return {"name": rec["name"], "type": rec.get("type"),
                "type_label": fam.label if fam else str(rec.get("type")),
                "address": self._address(rec), "model": rec.get("model", ""),
                "added_at": rec.get("added_at"), "source": rec.get("source", "dashboard"),
                "values": {k: v for k, v in plain.items()
                           if k not in ("name", "type", "added_at", "updated_at", "source")},
                "secrets": secrets,
                "loaded": rec["name"].lower() in self.printers,
                "driver_available": fam is not None}

    def list_public(self) -> list:
        return [self.public(r) for r in self.store.records()]

    # ---- start-up ----------------------------------------------------------

    def _values_for(self, rec, fam) -> dict:
        v = {k: str(rec.get(k, "") or "") for k in fam.field_names}
        for n in fam.secret_fields:
            v[n] = self.store.secret(rec["name"], n)
        return v

    def load_all(self):
        """Construct + connect a driver for every stored printer. A printer that cannot
        be built (unknown/unavailable type, bad record) is logged and skipped - the
        record stays in the store so it is not lost."""
        for rec in self.store.records():
            fam = self.families.get(rec.get("type"))
            if fam is None:
                self.log(f"[fleet] '{rec['name']}': printer type '{rec.get('type')}' is not available in "
                         "this build - skipped (kept in the store)")
                continue
            try:
                prn = fam.build(self._values_for(rec, fam))
                prn.connect()
            except Exception as e:
                self.log(f"[fleet] '{rec['name']}': could not start: {type(e).__name__}: {e}")
                continue
            self.printers[rec["name"].lower()] = prn
        self.log(f"[fleet] {len(self.printers)} printer(s) loaded")

    # ---- probing -----------------------------------------------------------

    @staticmethod
    def _probe_key(fam, values) -> str:
        blob = json.dumps([fam.key, sorted(values.items())], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _probe(self, fam, values, use_cache=False) -> dict:
        key = self._probe_key(fam, values)
        if use_cache:
            hit = self._probe_cache.get(key)
            if hit and time.monotonic() - hit[0] < PROBE_CACHE_SECONDS:
                return hit[1]
        res = fam.probe(values, PROBE_TIMEOUT)
        with self.lock:
            now = time.monotonic()
            self._probe_cache = {k: v for k, v in self._probe_cache.items() if now - v[0] < PROBE_CACHE_SECONDS}
            if res["ok"]:
                self._probe_cache[key] = (now, res)
        return res

    def test(self, type_key, values, existing_name=None) -> dict:
        """'Test connection': probe without saving anything. With existing_name, blank
        secret fields borrow that printer's stored ones (editing a printer) - but ONLY while the
        address is unchanged: a stored secret is never sent to a new destination."""
        fam = family_for(type_key, self.families)
        existing, esec = None, None
        if existing_name:
            existing = self.store.get(existing_name)
            if existing is None:
                raise NotFound(f"No printer named '{existing_name}'.")
            if address_changed(fam, existing, values):
                why = _need_secrets_message(fam, values)
                if why:
                    raise FleetError(why)
                esec = lambda n: ""                                          # noqa: E731
            else:
                esec = lambda n: self.store.secret(existing["name"], n)      # noqa: E731
        vals = normalize_values(fam, values, existing, esec, editing=bool(existing))
        if existing is None and self.store.get(vals["name"]):
            raise Conflict(f"A printer named '{vals['name']}' already exists.")
        # N-16 / P1-9: a Bambu's broker tolerates very few clients. Never open a second MQTT session to a
        # printer the Hub is already connected to - whatever name or spelling of the address was typed.
        other = self._duplicate_of(fam, vals, skip=existing["name"] if existing else None)
        if other:
            raise Conflict(f"'{other}' is already this printer (same serial number or IP address) and the "
                           "Hub is connected to it - edit that entry instead of testing it again.")
        live = self._live_result(fam, existing, vals)
        if live is not None:
            return live
        return self._probe(fam, vals)

    def _live_bambu_for(self, vals):
        """The CONNECTED Bambu driver that is the machine `vals` names (same serial, or same address however
        it is spelled), else None. A driver that is down is not returned: probing is then the only way
        to find out why (for example the access code changed)."""
        serial, ip = (vals.get("serial") or "").strip().upper(), norm_host(vals.get("ip"))
        for prn in list(self.printers.values()):
            if getattr(prn, "kind", "") != "bambu":
                continue
            if not ((serial and str(getattr(prn, "serial", "")).strip().upper() == serial)
                    or (ip and norm_host(getattr(prn, "ip", "")) == ip)):
                continue
            try:
                if prn.snapshot().get("connected"):
                    return prn
            except Exception:
                continue
        return None

    def _live_result(self, fam, rec, vals):
        """A Bambu the Hub is already connected to is NEVER probed with a second MQTT client on its (fragile)
        broker. Tested with its saved settings: answered from the live session. Tested with a different
        access code (or any other value for the same machine): refused with the reason. None = no live
        session for this machine, so a probe is fine."""
        if fam.key != "bambu":
            return None
        prn = self._live_bambu_for(vals)
        if prn is None:
            return None
        saved = self.store.secret(prn.name, "access_code") if self.store.get(prn.name) else prn.access_code
        same_addr = norm_host(vals.get("ip")) == norm_host(getattr(prn, "ip", ""))
        if vals.get("access_code") != saved or not same_addr:
            raise Conflict(f"The Hub is already connected to this printer as '{prn.name}', and a second test "
                           "connection could disturb it. To try a different access code or address, save the change "
                           "with Edit: the Hub steps aside, tests the new code, and puts the old one back "
                           "if it does not work.")
        model = bambu_model_from_serial(vals["serial"]) or (vals.get("model") or "").strip() or "Bambu Lab printer"
        return fam.probe_result_ok(model, "", _bambu_has_camera(model))

    # ---- busy checks -------------------------------------------------------

    def _busy_reason(self, name: str, verb: str):
        prn = self.printers.get(name.lower())
        if prn is not None:
            try:
                s = prn.snapshot()
            except Exception:
                s = {}
            if s.get("state") in BUSY_STATES:
                job = s.get("job") or "a job"
                return (f"{prn.name} is {s['state']} on '{job}'. {verb} it now would cut the Hub's "
                        "connection to a running job (the print itself keeps going on the printer). "
                        "Wait until it finishes, or confirm to do it anyway.")
        for g in self.guards:
            try:
                why = g(name)
            except Exception:
                why = None
            if why:
                return why
        return None

    # ---- mutations ---------------------------------------------------------

    def _duplicate_of(self, fam, vals, skip=None):
        """Name of another stored Bambu that already is this serial / IP, else None. Two entries for one
        printer would hold two permanent MQTT sessions on a broker that tolerates very few."""
        if fam.key != "bambu":
            return None
        ip, serial = norm_host(vals.get("ip")), (vals.get("serial") or "").strip().upper()
        for r in self.store.records():
            if r.get("type") != "bambu" or (skip and r["name"].lower() == skip.lower()):
                continue
            if (serial and str(r.get("serial") or "").strip().upper() == serial) or \
                    (ip and norm_host(r.get("ip")) == ip):
                return r["name"]
        return None

    def add(self, type_key, values, skip_test=False) -> dict:
        fam = family_for(type_key, self.families)
        vals = normalize_values(fam, values)
        if self.store.get(vals["name"]):
            raise Conflict(f"A printer named '{vals['name']}' already exists.")
        other = self._duplicate_of(fam, vals)
        if other:
            raise Conflict(f"'{other}' is already this printer (same serial number or IP address). "
                           "Edit or remove it instead of adding it twice.")
        result = None
        if not skip_test:
            result = self._probe(fam, vals, use_cache=True)
            if not result["ok"]:
                raise ProbeFailed(result)
        plain, secrets = split_values(fam, vals)
        if fam.key == "bambu" and not plain.get("model"):   # remember what the serial told us
            plain["model"] = (result or {}).get("model") or bambu_model_from_serial(vals["serial"])
        rec = {"name": vals["name"], "type": fam.key, **plain, "source": "dashboard"}
        prn = fam.build({**vals, **{k: v for k, v in plain.items()}})
        with self.lock:
            if vals["name"].lower() in self.printers or self.store.get(vals["name"]):
                raise Conflict(f"A printer named '{vals['name']}' already exists.")
            other = self._duplicate_of(fam, vals)
            if other:
                raise Conflict(f"'{other}' is already this printer (same serial number or IP address).")
            self.store.add(rec, secrets)
            try:
                prn.connect()
            except Exception:
                self.store.remove(vals["name"])
                try:
                    stop_driver(prn)
                except Exception:
                    pass
                raise
            self.printers[vals["name"].lower()] = prn
        self.log(f"[fleet] added '{vals['name']}' ({fam.key})")
        return {"printer": self.public(self.store.get(vals["name"])), "test": result}

    def update(self, name, values, skip_test=False, force=False) -> dict:
        rec = self.store.get(name)
        if rec is None:
            raise NotFound(f"No printer named '{name}'.")
        fam = self.families.get(rec.get("type"))
        if fam is None:
            raise FleetError(f"'{rec['name']}' is a {rec.get('type')} printer, which this build cannot edit.")
        values = dict(values or {})
        if values.get("name") and str(values["name"]).strip().lower() != rec["name"].lower():
            raise FleetError("A printer's name cannot be changed. Remove it and add it again under the new name.")
        values["name"] = rec["name"]
        esec = lambda n: self.store.secret(rec["name"], n)   # noqa: E731
        # Saved secrets are reused only for the SAME address; a moved printer needs them entered again
        # (they would otherwise be sent to the new destination by the connection test / the new driver).
        moved = address_changed(fam, rec, values)
        if moved:
            why = _need_secrets_message(fam, values)
            if why:
                raise FleetError(why)
        vals = normalize_values(fam, values, rec, (lambda n: "") if moved else esec, editing=True)
        other = self._duplicate_of(fam, vals, skip=rec["name"])
        if other:
            raise Conflict(f"'{other}' is already a printer with that serial number or IP address.")
        plain, secrets = split_values(fam, vals)
        # the secrets as they are NOW, captured before anything is written, so a failed edit can put
        # back exactly these (never the rejected new ones)
        old_secrets = {k: esec(k) for k in fam.secret_fields}
        drop = [k for k in fam.secret_fields if moved and old_secrets[k] and not secrets.get(k)]
        # which parts changed? (a blank secret = keep, so it never counts as a change)
        changed = {k for k, v in plain.items() if str(rec.get(k, "") or "") != v}
        changed |= {k for k, v in secrets.items() if v != old_secrets[k]}
        changed |= set(drop)
        if not changed:
            return {"printer": self.public(rec), "test": None, "changed": []}
        if not force:
            why = self._busy_reason(rec["name"], "Changing")
            if why:
                raise Busy(why)
        result = None
        needs_probe = not skip_test and bool(changed - {"model"})
        bambu_live = fam.key == "bambu" and self.printers.get(rec["name"].lower()) is not None
        if needs_probe and not bambu_live:
            result = self._probe(fam, vals, use_cache=True)
            if not result["ok"]:
                raise ProbeFailed(result)
        new_prn = fam.build(vals)
        with self.lock:
            old = self.printers.get(rec["name"].lower())
            # Does this edit re-point the name at a DIFFERENT machine? Then a pending pre-heat auto-off belongs
            # to the machine being left behind and must be settled while its driver can still talk to it.
            ident_changes = old is not None and controls.printer_identity(old) != controls.printer_identity(new_prn)
            released = False
            if needs_probe and bambu_live and old is not None:
                # A Bambu's broker tolerates very few clients: free the live session BEFORE the test opens
                # another, and bring it back if the test fails.
                if ident_changes:       # N-9: cool the OLD machine first - after stop_driver() it cannot be reached
                    note = controls.release_printer(old)
                    released = True
                    if note:
                        self.log(f"[fleet] '{rec['name']}': {note}")
                try:
                    stop_driver(old)
                except Exception as e:
                    self.log(f"[fleet] '{rec['name']}': old driver did not stop cleanly: {e}")
                result = self._probe_or_restore(fam, vals, old)
            self.store.update(rec["name"], {k: (v if v != "" else None) for k, v in plain.items()},
                              {k: v for k, v in secrets.items() if v != old_secrets[k]}, drop_secrets=drop)
            if old is not None:
                if ident_changes and not released:
                    note = controls.release_printer(old)     # cool a pre-heated machine BEFORE it is re-pointed
                    if note:
                        self.log(f"[fleet] '{rec['name']}': {note}")
                try:
                    stop_driver(old)      # fully gone BEFORE the new driver connects
                except Exception as e:
                    self.log(f"[fleet] '{rec['name']}': old driver did not stop cleanly: {e}")
            try:
                new_prn.connect()
            except Exception:
                # restore the previous printer so the fleet is never left without it: the OLD fields and
                # the OLD secrets (captured above), never the ones that just failed
                self.store.update(rec["name"], {k: (rec.get(k) or None) for k in plain},
                                  {k: v for k, v in old_secrets.items() if v},
                                  drop_secrets=[k for k, v in old_secrets.items() if not v])
                try:
                    stop_driver(new_prn)
                except Exception:
                    pass
                if old is not None:
                    try:
                        old.connect()
                    except Exception:
                        pass
                raise
            self.printers[rec["name"].lower()] = new_prn
            if old is not None and ident_changes:
                self._keep_supervised(old)
        self.log(f"[fleet] updated '{rec['name']}' ({', '.join(sorted(changed))})")
        return {"printer": self.public(self.store.get(rec["name"])), "test": result,
                "changed": sorted(changed)}

    def _keep_supervised(self, old):
        """N-9: `old` (a stopped driver) was left behind with a pre-heat auto-off that could not be settled
        (the machine was not confirmed idle when it went). A heated machine must never fall out of
        supervision: give the pending timer a fresh, detached driver for the SAME machine (same address,
        serial and access code) that lives only until the timer finishes (cooled, skipped because a print
        owns the heaters, or given up with an alert). Non-Bambu drivers need nothing: stopping them only
        stops their camera, the HTTP calls still work."""
        if not isinstance(old, BambuPrinter):
            return
        try:
            if not controls.has_pending(old):
                return
            clone = BambuPrinter(old.name, old.ip, old.serial, old.access_code, old.model)
            clone.connect()
        except Exception as e:
            self.log(f"[fleet] '{getattr(old, 'name', '?')}': could not keep the pre-heat auto-off supervised: {e}")
            return
        if controls.adopt_detached(old, clone, cleanup=clone.disconnect):
            self.log(f"[fleet] '{old.name}': its pre-heat auto-off keeps running on a detached connection "
                     "until the old machine is confirmed cooled")
        else:
            clone.disconnect()

    def _probe_or_restore(self, fam, vals, old):
        """Probe with the old Bambu session already stopped; on failure bring that session back."""
        try:
            res = self._probe(fam, vals, use_cache=True)
        except BaseException:
            try:
                old.connect()
            except Exception:
                pass
            raise
        if not res["ok"]:
            try:
                old.connect()
            except Exception:
                pass
            raise ProbeFailed(res)
        return res

    def remove(self, name, force=False) -> dict:
        rec = self.store.get(name)
        if rec is None:
            raise NotFound(f"No printer named '{name}'.")
        if not force:
            why = self._busy_reason(rec["name"], "Removing")
            if why:
                raise Busy(why)
        with self.lock:
            live = self.printers.get(rec["name"].lower())
            if live is not None:
                note = controls.release_printer(live)    # a pre-heated, idle printer is cooled BEFORE it goes
                if note:
                    self.log(f"[fleet] '{rec['name']}': {note}")
            self.store.remove(rec["name"])
            prn = self.printers.pop(rec["name"].lower(), None)
            if prn is not None:
                try:
                    stop_driver(prn)
                except Exception as e:
                    self.log(f"[fleet] '{rec['name']}': driver did not stop cleanly: {e}")
                self._keep_supervised(prn)
        self.log(f"[fleet] removed '{rec['name']}'")
        return {"removed": rec["name"]}
