# Printer drivers

The Print Hub talks to every printer through a small driver class with the same
public surface. Today: `BambuPrinter`, `OctoPrintPrinter` (both in `app/bambu.py`),
`PrusaLinkPrinter` (`app/prusalink.py`) and `MoonrakerPrinter` (`app/moonraker.py`).
This page is the contract new drivers (and the code that wires them in) follow.

## The contract (mirrors `OctoPrintPrinter`)

| Member | Notes |
|---|---|
| `kind` | `"prusalink"` / `"moonraker"`. Goes into `snapshot()["kind"]` and `/api/printer/<n>`. |
| `__init__(name, url, creds, model="", ...)` | `creds`: PrusaLink = `{"username","password","api_key"}` or an API-key string; Moonraker = API-key string / `{"api_key"}` / `None`. |
| `from_config(cfg, env=os.environ)` (classmethod) | Builds from one `printers:` entry of `config.yml`. Secrets come from env vars named by `password_env` / `api_key_env`; inline `password` / `api_key` also accepted (for UI-created printers). Raises `PrinterError` if `url` is missing. |
| `CONFIG_FIELDS` | List of `{name, label, type: text\|password\|url, required, help, default?, placeholder?, secret?}`; `name` is the config key. A UI renders the add-printer form from it. `CONFIG_REQUIRE_ONE_OF` lists groups where at least one field is needed (PrusaLink: password or api_key). |
| `probe(url, creds, timeout=5)` (classmethod) | Never raises. `{"ok","model","firmware","has_camera","error"}` (Moonraker adds `"warning"` when Moonraker is up but Klipper is not ready). Powers "Test connection". PrusaLink also accepts `snapshot_url=`, `storage=`. |
| `connect()` | No connection to hold; just warms the camera probe in a background thread. |
| `disconnect()` | Stops the shared camera poller (the only long-lived thing the driver owns). Idempotent, safe before `connect()`, never raises. `FleetManager` (via `fleet_store.stop_driver`) calls it when a printer is edited or removed. |
| `refresh(wait=0)` | Drops the 3 s status cache. |
| `snapshot() -> dict` | Exactly OctoPrint's keys (`printer connected state job percent remaining_min layer nozzle_c nozzle_target_c bed_c bed_target_c print_error sdcard age_seconds kind`) **plus** `raw_state` (the printer's native state string) and `error` (why it is disconnected / the printer's error message, else `None`). Cached 3 s; never raises. |
| `ams_trays() -> []` | No AMS. |
| `files() -> list[dict]` | `{name, size, modified, print_seconds, print_time, filament_mm, origin}`. `name` is the path to pass back to every other method. `origin` is always `"local"` (= downloadable by `open_file`). Moonraker `files(with_meta=False)` skips the per-file metadata lookups. |
| `download_head(filename, nbytes=1048576) -> bytes` | First bytes of a file (slicer thumbnail lives in the G-code header). Sends `Range`; works if the server ignores it. |
| **`open_file(filename, timeout=30, headers=None)`** | **The full-download method for backup.** Returns the HTTP response (context manager, `.read(n)` in chunks) so 100 MB files stream to disk instead of into RAM. Raises `PrinterError`. |
| `download(filename, max_bytes=500 MB) -> bytes` | The whole file, capped (used by renaming). Optional: a driver without it cannot be renamed on. |
| `upload(filename, data)` | **Beta - not yet verified on real hardware.** Stores a file (renaming and the Library's Send to...). Must refuse unless the printer is positively idle (`_require_idle_for_delete(self, "uploaded")`), must never select or start the file, never follow a redirect. Optional. |
| `start_print(filename, ams_mapping=None, origin="local")` | Starts an existing file (no upload). `ams_mapping` ignored. |
| `stop()` `pause()` `resume()` | Cancel / pause / resume the current job. |
| `sd_delete(filename, origin="local")` | Delete a file. |
| `has_camera` (property) | True only when a snapshot source was found AND a test frame was fetched. Cached, re-checked in the background (same TTLs as OctoPrint). |
| `camera_info()` | `{"flip_h","flip_v","rotate90"}` or `None` — same shape the dashboard already gets for OctoPrint. |
| `camera()` | The shared `PollingCameraStream` (camera.py) for that printer. |
| `connect_serial(confirm_restart=False)` | Safe reconnect, never a blind restart. It looks first: unreachable -> `PrinterUnreachable`, nothing sent; printing / paused -> `PrinterBusy`, always refused; Klipper `ready` / `startup` -> nothing sent. Only when Klipper reports `shutdown` / `error` / `disconnected` can it send `POST /printer/firmware_restart` (FIRMWARE_RESTART, not `/printer/restart`: per the Klipper docs a plain RESTART does not clear the micro-controller's error state), and only with `confirm_restart=True`; without it `ConnectNeedsConfirm` ("This restarts Klipper (FIRMWARE_RESTART). Only do this if the printer shows an error."). OctoPrint: reads `GET /api/connection` first and posts "connect" only when the serial link is closed. PrusaLink has no equivalent. |
| class traits | `uses_files_api = True` (list with `files()`, not `sd_list()`), `backup_mid_print_ok` (PrusaLink `False`: it reads the printer's USB stick; Moonraker `True`: reads the Pi's disk). |

Failures raise `bambu.PrinterError` with a message fit to show a human. Moonraker
errors also carry `.status` (HTTP code) for callers that care.

| `control_caps()` | What this printer can do **right now** for the controls below: `{"light": {supported, state, reason?, note?, verified?}, "heat": {...}, "unload": {...}, "load": {...}}` (`heat` covers pre-heat and cool-down). Never raises for "unsupported": it reports it with a `reason`. |
| `set_light(on)` `set_temps(nozzle, bed)` `unload_filament()` `load_filament(tray=None)` | Primitives behind the controls. `set_temps` takes `None` = leave that heater alone, `0` = off, and may return what it actually applied. Raise `controls.ControlUnsupported` when the printer cannot. No state checks here: `controls.perform` does the gate. |
| `controls()` | `controls.describe(self)`: the capability dict plus presets, the busy gate and the auto-off countdown. |

### State values

The server/dashboard only act on: `RUNNING PREPARE PAUSE` (busy: refuse start/backup),
`UNKNOWN` (= host unreachable; dashboard shows OFFLINE when `connected` is false),
`DISCONNECTED` (host reachable, printer firmware not; dashboard shows NOT CONNECTED),
`FAILED` (last job cancelled/failed; shown as idle), `FINISH`, `IDLE`.
There is no separate `OFFLINE` state: it is `connected: false` + `state: "UNKNOWN"`, as for OctoPrint.

**PrusaLink `printer.state` -> hub**

| PrusaLink | Hub | Why |
|---|---|---|
| IDLE, READY | IDLE | |
| BUSY | PREPARE | homing / loading / calibrating: treat as busy |
| PRINTING | RUNNING | |
| PAUSED | PAUSE | |
| ATTENTION | PAUSE (IDLE if no job) | needs a human, e.g. filament runout; `print_error=1` |
| FINISHED | FINISH | `percent` forced to 100, job name kept |
| STOPPED | FAILED | user cancel (same as Bambu) |
| ERROR | FAILED | `print_error=1` |
| anything else | UNKNOWN | |
| host unreachable / auth failure | UNKNOWN, `connected: false`, `error` says why | |

**Moonraker -> hub**

| Moonraker | Hub | Why |
|---|---|---|
| `print_stats.state` standby | IDLE | job name blanked |
| printing | RUNNING | |
| paused | PAUSE | |
| complete | FINISH | `percent` 100 |
| cancelled | FAILED | user cancel |
| error | FAILED | `print_error=1`, `error` = `print_stats.message` |
| `webhooks.state` startup | DISCONNECTED | Klipper still starting |
| shutdown / error / disconnected (Moonraker's `klippy_state`) | DISCONNECTED | `print_error=1`; `connect_serial(confirm_restart=True)` after a human OK |
| objects query 5xx but `klippy_state` ready | `connected: false` (no reading) | a hiccup, not a disconnect: no connect is offered |
| Moonraker unreachable / 401 | UNKNOWN, `connected: false` | |

Percent is `virtual_sdcard.progress` (file position; falls back to `display_status.progress`).
Remaining time is extrapolated from `print_duration` once progress >= 1 %.

## Wiring a driver into `server.py` / `backup.py`

server.py printer construction (replaces the `if/else` that builds `PRINTERS`):

```python
from prusalink import PrusaLinkPrinter
from moonraker import MoonrakerPrinter
DRIVERS = {"prusalink": PrusaLinkPrinter, "moonraker": MoonrakerPrinter}

for p in CFG["printers"]:
    if p.get("type") in DRIVERS:
        prn = DRIVERS[p["type"]].from_config(p, os.environ)
    elif p.get("type") == "octoprint":
        ...   # unchanged
```

Every `_is_octo(prn)` / `kind == "octoprint"` check that means "file-based HTTP printer, no AMS"
(start_print confirm rule, `prn.files()` vs `prn.sd_list()`, prewarm, delete, thumbnails) becomes
`_is_octo(prn) or getattr(prn, "uses_files_api", False)`. `connect_printer` should call
`prn.connect_serial(confirm_restart=...)` when `hasattr(prn, "connect_serial")` (OctoPrint, Moonraker). A driver's `connect_serial` must look before it sends and
must never act on a printer that is printing or unreachable.
`_camera_fields`, `/api/camera/*` already work unchanged (`has_camera`, `camera_info()`, `camera()`).

backup.py (done): `_busy_reason` skips the mid-print refusal when
`getattr(prn, "backup_mid_print_ok", False)` (a driver with no such trait is refused while busy
or unknown, like Bambu); `_list` uses `files()` for any driver with `open_file`
(`with_meta=False` when the driver accepts it), and `_fetch` streams `prn.open_file(name)` to a
temp file with the same chunk + sha256 loop as the OctoPrint branch. Backup tests with a PrusaLink
and a Moonraker fake are in `tests/test_backup.py`.

Config (`config.yml`):

```yaml
  - name: MK4
    type: prusalink
    model: "Prusa MK4S"
    url: http://192.168.1.50
    username: maker              # default
    password_env: MK4_PASSWORD   # or api_key_env: MK4_API_KEY
    # storage: usb               # snapshot_url: http://<camera-host>/snapshot.jpg

  - name: Voron
    type: moonraker
    model: "Voron 2.4"
    url: http://192.168.1.60     # port 80 (Mainsail/Fluidd) or :7125 both work
    # api_key_env: VORON_API_KEY # only if Moonraker needs one
    # camera_base_url: http://192.168.1.60   # only if webcam links aren't on port 80
```

Add-printer wizard: `Driver.CONFIG_FIELDS` renders the form; the "Test connection" button
calls `Driver.probe(url, creds)` (blocking, <= ~3 requests, so run it off the event loop).

## Printer controls (`app/controls.py`)

Light, pre-heat, cool-down, unload, load. `controls.py` owns everything that must behave the same
for every family; a driver only supplies `control_caps()` and the primitives in the table above.
It imports nothing from the drivers (drivers import it), so a new driver needs no server change.

- **Gate** (`controls.perform`): check order is known action -> supported (422 `ControlUnsupported`) ->
  valid arguments (400 `ControlInvalid`) -> safe now (409 `ControlRefused`) -> send. Heater and
  filament actions run only in `IDLE` / `FINISH` / `FAILED` (never `RUNNING` / `PREPARE` / `PAUSE`,
  never `UNKNOWN` / `DISCONNECTED`). Light is never gated.
- **Limits**: nozzle 0-300 C, bed 0-120 C; outside that is rejected, not clamped. Both zero = use cooldown.
- **Presets**: bed only - `PLA 60, PETG 80, ABS 100` C, set in Settings > Printers & safety (`printers.preheat_bed_*`); a preset sends
  `set_temps(None, bed)`, so every driver (Bambu `M140`, OctoPrint bed target, Klipper `M140`) sends a bed command and no nozzle command.
  `preheat_presets:` in `config.yml` may add named presets (their `bed` value only). PrusaLink has no heater control at all.
- **Auto-off**: every pre-heat arms a timer (Settings > Printers & safety, else `PREHEAT_TIMEOUT_MIN`, default 15; an
  invalid or <= 0 env value means 15; a saved 0 in Settings means no timer - the status line then says so and
  Cool down is the only way off). When it fires it cools down through the *live* driver (looked up by name, so an
  edited printer is fine) unless the printer is by then printing; a hub-started print
  (`controls.note_print_start`), a cool-down or a new pre-heat cancels/replaces it, and
  `describe()` drops it if it sees the printer busy. `auto_off_in_s` and `last_auto_off` are in the
  `preheat` entry.
- **Server**: `GET /api/printer/<n>/controls` (full), `POST /api/printer/<n>/control` (200 / 400 / 404 /
  409 / 422 / 502), `controls` summary (`actions`, `allowed_now`, `light`, `presets`, `auto_off_in_s`) inside
  `GET /api/printer/<n>` (the same summary also carries the full description under `detail`), plus the MCP tools `printer_controls`, `set_light` (`chamber_light` alias),
  `preheat`, `cooldown`, `unload_filament`, `load_filament`. The old `/action` `light_on` / `light_off` still work.

| Family | Light | Pre-heat / cool-down | Unload / load |
|---|---|---|---|
| Bambu (MQTT) | `ledctrl` `chamber_light` (proven); falls back to `work_light` when that is all the printer reports (**unverified**); state from `lights_report` | `gcode_line` `M104 S<n>` / `M140 S<n>` (**unverified**) | AMS only: `ams_change_filament`, `target` 255 = unload, `target` = slot for load (**unverified**) |
| OctoPrint | unsupported (no standard API) | `POST /api/printer/tool` `{"command":"target","targets":{"tool0":T}}` + `POST /api/printer/bed` `{"command":"target","target":B}` (documented API; not tried on hardware) | `POST /api/printer/command` `M702` / `M701` (Prusa firmware; **unverified**) |
| Moonraker | discovered from `/printer/objects/list`: `output_pin` -> `SET_PIN PIN=<n> VALUE=1\|0`; `led` / `neopixel` / `dotstar` -> `SET_LED LED=<n> RED= GREEN= BLUE= WHITE=1\|0`; state from `objects/query` (**unverified**) | `POST /printer/gcode/script` `M104` / `M140` (no heated bed = bed skipped) | only when `gcode_macro UNLOAD_FILAMENT` / `LOAD_FILAMENT` exist (case-insensitive); the call may outlive a normal timeout and then counts as started (**unverified**) |
| PrusaLink | unsupported | unsupported | unsupported: the v1 API has no light, temperature, G-code or filament endpoint (from the API description; not checked on hardware) |

Bambu unload/load notes: needs `print.ams.ams` in the printer's report; the temperature fields
(`curr_temp` = current nozzle, `tar_temp` = the filament's `nozzle_temp_min` but at least 220 C) and the
`ams_id` / `slot_id` fields follow public protocol notes and may differ per firmware. Newer Bambu
firmware with authorization control can reject `gcode_line` and `ams_change_filament` outside LAN-only /
developer mode.

## Things to confirm on real hardware

Written from API documentation and knowledge, tested only against fake servers.

**PrusaLink**
- Download URL: the driver tries `GET /api/v1/files/{storage}/{path}/raw` and falls back to the
  plain path, deciding by response `Content-Type` (JSON = file info, not the file). Confirm which one
  Buddy (MK4/Core One/XL) firmware serves; the result is cached per printer.
- File listing: assumes `GET /api/v1/files/{storage}/` returns `children` with `type`
  (`PRINT_FILE`/`FOLDER`/`FIRMWARE`), `name`, `size`, `m_timestamp`, and folders unexpanded (the driver
  recurses, max 60 folder reads). On FAT USB the API may expose short 8.3 `name`s next to a long
  `display_name`; the driver uses `name` for paths (it is what the API accepts) and reports
  `display_name` as an extra key. Print time / filament usually are NOT in the listing, so
  `print_seconds` / `filament_mm` are mostly `None` (parsed only if present).
- Auth: Digest (MD5, qop=auth) is exercised against a standards-checked fake; confirm firmware 5.x and
  6.x both accept it, and whether your unit wants `X-Api-Key` instead. `/api/version` is assumed open.
- `time_remaining` seconds, `progress` percent 0-100 in `/api/v1/status` `job` — confirm progress is not 0-1.
- Model: `probe()` reads `printer_type`/`model` from `/api/v1/info`; many firmwares do not expose a model
  there, so `model` may be `None` (the user-entered model is used for display).
- Local cameras: `GET /api/v1/cameras` (`camera_list[].camera_id`) and `/api/v1/cameras/{id}/snap` exist on
  PrusaLink builds with USB-camera support; Buddy (MK4) usually has none. Unverified field names.
- `PUT /api/v1/job/{id}/resume` and `DELETE /api/v1/job/{id}` as documented; a job in ATTENTION may need
  `resume` from the printer screen.
- Reading a file over USB while printing could stutter the print: `backup_mid_print_ok = False` guards that.
- HTTPS / self-signed certificates are not handled (PrusaLink is plain HTTP on the LAN).

**Moonraker**
- Camera URLs: relative links resolve against the host with Moonraker's port 7125 dropped (nginx on 80).
  A non-7125 port is kept (reverse-proxy setups); override with `camera_base_url`. Orientation mapping
  assumes the dashboard's `rotate90` is counter-clockwise (OctoPrint semantics).
- `/server/webcams/list` needs Moonraker >= ~v0.8; older ones use the `webcams` database namespace
  (supported as fallback, field names `urlSnapshot`/`flipX` from Mainsail/Fluidd).
- `printer/objects/query` while Klipper is down: assumed HTTP 503 (driver treats 500/503 as "Klipper not
  ready" and checks `/server/info`). Confirm on a powered-off MCU.
- Authorization: only `X-Api-Key` is supported, not JWT/Bearer or oneshot tokens.
- `modified` is float epoch seconds (OctoPrint's is int); `backup.py`'s `_modified` handles epoch.
- `.bgcode` is listed if present, but stock Klipper cannot print it.
- Print-time estimate uses file-position progress; with M73 slicers `display_status.progress` is used only
  when file progress is 0.

## Tests

```
python3 tests/test_prusalink.py
python3 tests/test_moonraker.py
python3 tests/test_backup.py       # also runs the PrusaLink / Moonraker fakes above
python3 tests/test_controls.py     # needs the app's deps (mcp, starlette, httpx) for the server part
```
Stdlib only; fake servers on 127.0.0.1 (the PrusaLink fake implements real Digest auth and is
cross-checked against urllib's stock Digest client). No printer or network host is contacted.


## Setup guides and test-connection tips (Add printer wizard)

`app/setup_guides.py` holds the step-by-step "Before you start" guide for each printer family
(Bambu, PrusaLink, OctoPrint, Klipper/Moonraker) and the tips shown when "Test connection"
fails. One source, three readers: the dashboard renders it above the form, `list_printer_types`
and `GET /api/printer-types` return it to agents (`setup_guide`, `troubleshooting`), and
`Family.probe()` attaches `hint` + `guide_step` to a failed result (the probe's own `error` /
`message` are unchanged; the hint is also appended as "Tip: ..." to the 422 error of
`add_printer` / `update_printer`).

* A new driver adds an entry to `GUIDES` and `HINTS` keyed by its family key. A field's
  `guide_step` in `CONFIG_FIELDS` names the step that shows where to find that value.
* Hints match the probe `code` (Bambu, OctoPrint) or a fragment of the error text (PrusaLink,
  Moonraker, whose probes carry no code). A test fails if a driver rewords an error the hints rely on.
* Keep the text generic (no real serials, addresses or codes) and version-tolerant: firmware menus move.
