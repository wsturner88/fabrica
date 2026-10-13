# Settings and notifications

The Hub's settings, and the notification engine that uses them, are three standalone modules
in `app/`. They import nothing from the drivers or `server.py`, so they can be wired in (or
tested) on their own:

| Module | Job |
|---|---|
| `settings_store.py` | Typed, self-describing settings schema + `SettingsStore` (`/data/settings.json`; secrets in `secrets.json`). |
| `notify.py` | Five channel senders (Telegram, ntfy, email, Discord, generic webhook) and the `Notifier` that applies the rules. |
| `events.py` | `EventWatcher`: turns fleet snapshots into events ("P1S: print FINISHED"). |

Stdlib only; no new dependencies. Tests: `tests/test_settings.py`, `tests/test_notify.py`,
`tests/test_events.py` (all loopback, no real service is ever contacted).

## How values are resolved

`saved setting` > `environment variable` > `built-in default`. The environment variables that
exist today act as defaults: `TZ` (time zone), `BACKUP_KEEP` (backups per printer),
`PREHEAT_TIMEOUT_MIN` (pre-heat auto-off), `BAMBU_MCP_API_AUTH` (API key check: on / warn / off). An invalid env value is ignored. `GET /api/settings`
reports where each value came from (`saved` / `env` / `default`).

Secrets (bot token, ntfy token, SMTP password, Discord webhook URL, webhook bearer) are
**write-only**: stored in `/data/secrets.json` (mode 0600) under the reserved key `_settings`,
never returned by any public function - only `"set"` / `"not set"`. Sending a blank value or the
placeholder `"set"` keeps the stored secret; to delete one pass its key in `clear`.
Export/import carries ordinary settings only, never secrets.

## Schema

`SettingsStore.schema()` returns the machine-readable version of this (label, type, help,
options, min/max, secret flag, default, `env`, `readonly`, `disabled`, per-section `help` and
`test` channel); `describe()` is the same with current `value` + `source` merged in. A UI can render
the whole Settings page from it. Field types: `bool`, `string`, `secret`, `url`, `int`, `enum`,
`multienum`, `time` (HH:MM), `timezone`, `emails`, `float` (finite decimals, min/max), `list_str` (with `pairs: true` every entry must look
like `NAME=number`), `presets`, `info` (read-only).
Keys are `<section>.<name>`.

Event toggles live in `notifications.<event>`; the event names are shared by all three modules:
`print_started`, `print_finished`, `print_failed` (failed or cancelled), `print_paused`,
`printer_offline`, `printer_online`, `backup_done`, `backup_failed`, `spaghetti_suspected`,
`preheat_auto_off`. Failures (`print_failed`, `backup_failed`, `spaghetti_suspected`) break through
quiet hours by default.

## Settings reference

### General (`general`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `general.hub_name` | string | `Print Hub` |  |
| `general.timezone` | timezone | `UTC` | env `TZ` is the default, then the machine's own zone (a container is usually UTC). Until it is set the dashboard shows times in the viewer's browser zone, and Settings offers a one-tap **Use <browser zone>** |
| `general.temp_unit` | enum | `C` | one of C, F |
| `general.datetime_format` | enum | `iso` | one of iso, us, eu. Until it is set the dashboard follows the browser's own 12/24-hour style |

### Notifications (`notifications`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `notifications.enabled` | bool | `True` |  |
| `notifications.quiet_enabled` | bool | `False` |  |
| `notifications.quiet_start` | time | `22:00` |  |
| `notifications.quiet_end` | time | `07:00` |  |
| `notifications.quiet_breakthrough` | multienum | `["print_failed","backup_failed","spaghetti_suspected"]` |  |
| `notifications.include_snapshot` | bool | `True` |  |
| `notifications.duplicate_window_min` | int | `5` | 0-240 |
| `notifications.offline_grace_s` | int | `90` | 10-3600 |
| `notifications.muted_printers` | list_str | `[]` |  |
| `notifications.print_started` | bool | `False` |  |
| `notifications.print_finished` | bool | `True` |  |
| `notifications.print_failed` | bool | `True` |  |
| `notifications.print_paused` | bool | `True` |  |
| `notifications.printer_offline` | bool | `True` |  |
| `notifications.printer_online` | bool | `True` |  |
| `notifications.backup_done` | bool | `False` |  |
| `notifications.backup_failed` | bool | `True` |  |
| `notifications.spaghetti_suspected` | bool | `True` |  |
| `notifications.preheat_auto_off` | bool | `True` |  |

### Telegram (`telegram`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `telegram.enabled` | bool | `False` |  |
| `telegram.bot_token` | secret | *(secret)* | write-only |
| `telegram.chat_id` | string | empty |  |

### ntfy (`ntfy`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `ntfy.enabled` | bool | `False` |  |
| `ntfy.server_url` | url | `https://ntfy.sh` |  |
| `ntfy.topic` | string | empty |  |
| `ntfy.token` | secret | *(secret)* | write-only |

### Email (SMTP) (`email`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `email.enabled` | bool | `False` |  |
| `email.host` | string | empty |  |
| `email.port` | int | `587` | 1-65535 |
| `email.tls` | enum | `starttls` | one of starttls, ssl, none |
| `email.verify_cert` | bool | `True` |  |
| `email.username` | string | empty |  |
| `email.password` | secret | *(secret)* | write-only |
| `email.from_addr` | string | empty |  |
| `email.to_addr` | emails | empty |  |

### Discord (`discord`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `discord.enabled` | bool | `False` |  |
| `discord.webhook_url` | secret | *(secret)* | write-only |

### Generic webhook (`webhook`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `webhook.enabled` | bool | `False` |  |
| `webhook.url` | url | empty |  |
| `webhook.bearer` | secret | *(secret)* | write-only |

### Printers & safety (`printers`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `printers.preheat_presets` | presets | `{"PLA":{"nozzle_c":215,"bed_c":60},"PETG":{"nozzle_c":240,"bed_c":80},"ABS":{"nozzle_c":255,"bed_c":100},"TPU":{"nozzle_c":225,"bed_c":50}}` |  |
| `printers.preheat_auto_off_min` | int | `15` | env `PREHEAT_TIMEOUT_MIN` is the default; 0-240 |
| `printers.require_bed_clear` | bool | `True` | read-only |

### Backups (`backups`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `backups.keep_per_printer` | int | `5` | env `BACKUP_KEEP` is the default; 1-100 |
| `backups.schedule_enabled` | bool | `False` | placeholder, disabled |
| `backups.schedule_time` | time | `03:00` | placeholder, disabled |

### Camera (`camera`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `camera.thumb_refresh_s` | int | `5` | 1-120 |
| `camera.live_max_min` | int | `30` | 1-480 |

### Costs (`costs`)

Used by the History page, `recent_jobs`, `job_stats` and `cost_per_part` (see [joblog.md](joblog.md)). Changing a price re-prices the whole history.

| Key | Type | Default | Notes |
|---|---|---|---|
| `costs.currency` | string | `$` | a symbol, up to 8 characters; nothing is converted |
| `costs.filament_prices` | list_str (`NAME=price`) | `PLA=20, PETG=22, ABS=22, ASA=25, TPU=35` | price per kg; entries override the built-in ones |
| `costs.default_price_per_kg` | float | `25` | any other material |
| `costs.kwh_price` | float | `0.15` | per kWh |
| `costs.printer_watts` | list_str (`NAME=watts`) | `P1S=120, A1=90, MK4=100` | name or part of the model name |
| `costs.default_watts` | int | `120` | 10-2000 |
| `costs.wear_per_hour` | float | `0.30` | nozzle, belts, bed, depreciation |
| `costs.window_n` | int | `20` | attempts per file in the per-file average (1-500) |

### Detection (`detection`)

Spaghetti detection (see [detection.md](detection.md)). There is no auto-pause setting, on purpose.

| Key | Type | Default | Notes |
|---|---|---|---|
| `detection.mode` | enum | `off` | `off`, `local` (this machine), `remote` (another machine) |
| `detection.url` | url | *(empty)* | detector address; `local` falls back to env `DETECTOR_URL`, then `http://detector:8080` |
| `detection.token` | secret | *(secret)* | write-only; `local` falls back to env `DETECTOR_TOKEN` |
| `detection.interval_s` | enum | `10` | `10`, `15`, `30` |
| `detection.sensitivity` | enum | `medium` | `low`, `medium`, `high` |
| `detection.learn_only` | bool | `True` | score and log, send no alert |
| `detection.cooldown_min` | int | `10` | 1-120 |
| `detection.excluded_printers` | list | `[]` | printer names |

### Security (`security`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `security.api_auth` | enum | `on` | one of `on`, `warn`, `off`; env `BAMBU_MCP_API_AUTH` is the default. A **new** install is `on`; an install that already had data (`printers.json`, `settings.json`, ...) when this feature first ran is saved as `warn` once. Never exported or imported |
| `security.dashboard_password` | secret | *(secret)* | write-only, 8+ characters, stored as a salted PBKDF2 hash (not the password); setting or clearing it signs every session out (trusted devices stay). The password now lives in `dashboard_auth.json` (the offline `security.py` commands can change it); this setting is kept as an input for older scripts. Never exported or imported |

The rest of the dashboard sign-in (the on/off **switch**, the **view-only password**, idle / maximum session length,
trusted-device lifetime, the devices and sessions themselves) is not a setting: it lives in `dashboard_auth.json`
and is managed on the Security page or with `PUT /api/security` — see the README, *Securing the dashboard*.
| `security.active_keys` | info | runtime | read-only count of active API keys; the keys themselves are managed with `/api/keys` (below) |

### Agents / MCP (`agents`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `agents.mcp_path` | info | `/mcp` | read-only |
| `agents.version` | info | runtime | read-only; the build id (short git revision, `dev` if unmarked; Docker `--build-arg BUILD_REV=...`) |
| `agents.tool_count` | info | runtime | read-only |
| `agents.settings_api` | info | `/api/settings` | read-only |

## Channels

All senders share `send(title, body, image_jpeg=None, level="info"|"warning"|"failure") -> (ok, error)`.
They never raise, time out after 10 s, and scrub secrets (tokens, passwords, webhook URLs and
their path pieces) from `error`.

| Channel | What it sends | Verified |
|---|---|---|
| Telegram | `sendMessage` (JSON); with a picture `sendPhoto` multipart (caption 1024 chars, full text follows as a message if longer) | fake Bot API on localhost |
| ntfy | `POST {server}/{topic}` with `Title`, `Priority` (3/4/5), `Tags`, optional bearer; with a picture `PUT` + `Filename`/`Message` headers, falling back to a plain POST if the server refuses attachments; non-ASCII titles RFC 2047 encoded | fake ntfy on localhost |
| Email | `smtplib`, STARTTLS / SSL / none, optional login, picture as a JPEG attachment, `verify_cert` off for self-signed LAN servers | fake SMTP server on localhost (plain, STARTTLS, implicit SSL, AUTH) |
| Discord | webhook JSON; with a picture multipart (`payload_json` + `files[0]`). Sends a custom User-Agent (Discord blocks urllib's default) | fake webhook on localhost |
| Generic webhook | JSON POST, optional `Authorization: Bearer` | fake endpoint on localhost |

Generic webhook payload:

```json
{"source": "print-hub", "version": 1, "title": "P1S: print FINISHED",
 "body": "benchy finished. Took 2 h 14 min.", "level": "info",
 "event": "print_finished", "printer": "P1S",
 "timestamp": "2026-03-31T14:05:00Z", "image_jpeg_base64": "..."}
```
(`image_jpeg_base64` only when a picture is attached; `level` is `info`, `warning` or `failure`.)

Not exercised against the real services (by design): live Telegram, ntfy.sh, Discord, SMTP
providers. The request shapes follow their documented APIs; do one real `Test` per channel after
installing.

## Notifier rules (in order)

1. unknown event, master switch off, event toggle off -> dropped;
2. printer in `notifications.muted_printers` -> dropped;
3. quiet hours (`quiet_start` -> `quiet_end` in the Hub time zone, overnight ranges fine,
   start == end means none): dropped unless the event is in `quiet_breakthrough`;
4. no enabled and complete channel -> dropped;
5. duplicate within `duplicate_window_min` for the same printer + event + job -> dropped;
6. otherwise sent to all enabled channels in parallel; a camera picture is fetched only now, and
   only if `include_snapshot` is on.

`Notifier.notify(event)` is synchronous and returns `{"sent", "skipped", "channels"}`;
`Notifier.submit(event)` queues for a background thread (use this from the poll loop);
`Notifier.recent()` is a delivery log for the UI/agents; `Notifier.send_test(channel, values=None)`
ignores rules 1-5 and the channel's own `enabled` flag, and may take unsaved form values (a blank
secret falls back to the saved one) so the Settings page can offer "Test" before "Save".

## EventWatcher

`EventWatcher(sink, settings=None, snapshot_fetcher=None, offline_grace_s=90, confirm_s=5)`;
`update(fleet)` takes `{printer name: snapshot}`.

| Transition (confirmed for `confirm_s`) | Event |
|---|---|
| IDLE / FINISH / FAILED -> PREPARE -> RUNNING (not a resume from PAUSE) | `print_started` |
| active job -> FINISH, or -> IDLE having reached 98 %+ | `print_finished` (with duration) |
| active job -> FAILED, or -> IDLE short of done | `print_failed` ("FAILED", or "CANCELLED" when the printer gave no error) |
| RUNNING/PREPARE -> PAUSE | `print_paused` (error text / Bambu code `0x...` when present) |
| unreachable (`connected` false, `UNKNOWN` or `DISCONNECTED`) for `notifications.offline_grace_s` | `printer_offline` |
| reachable again, only if offline was announced | `printer_online` |

The first snapshot of every printer is a baseline (a Hub restart mid-print, or with a printer
switched off, produces no events); a printer that was off at baseline never produces "back online";
RUNNING/PAUSE flapping is swallowed by the confirm window; a print that finishes while the printer
is unreachable is reported when it returns. Hooks for other modules: `backup_done(printer, files,
size_bytes, name)`, `backup_failed(printer, error)`, `spaghetti_suspected(printer, score, image)`,
`preheat_auto_off(printer, minutes)`.

## How it is wired into the Hub

Done in `server.py` (tests: `tests/test_wiring.py`):

- **Objects**: `SETTINGS` (built with `fleet_store=STORE`, so there is one writer of `secrets.json`),
  `NOTIFIER`, `WATCHER`. The camera picture for a notification comes from the printer's shared camera
  reader (`prn.camera().get_frame`), never a second connection, and only when a message is really sent.
- **Poll loop**: `start_event_loop()` (called only from `__main__`, so importing the server starts nothing)
  runs a daemon thread that feeds `WATCHER.update()` (and the job log's `observe()`) the cached `snapshot()` of the whole fleet every 5 s.
  It reads the live printer registry through a copy, so printers added, edited or removed at runtime are
  fine, and a driver that raises is reported as unreachable rather than dropped.
- **Hooks**: a finished / failed backup job -> `backup_done` / `backup_failed` (a partly successful one
  counts as failed, with the reason); the pre-heat auto-off timer -> `preheat_auto_off` (only when it really
  cooled a printer down); `server.spaghetti_detected(printer, score, jpeg, level)` is the entry point the failure
  detector's alerts go through (see [detection.md](detection.md)).
- **Settings in use**: `backups.keep_per_printer` (BackupManager `keep=`), `printers.preheat_auto_off_min`
  (saved value wins; 1–240 min, **it can never be turned off**; with nothing saved the old `PREHEAT_TIMEOUT_MIN` / 15 min behaviour
  is unchanged), `printers.preheat_presets` (a saved list replaces the built-in and `config.yml` ones; with
  nothing saved those still apply), `camera.live_max_min` (live view length) and, in the dashboard,
  `general.hub_name`, `temp_unit`, `timezone`, `datetime_format`, `camera.thumb_refresh_s`.
  `agents.tool_count` is the real number of registered MCP tools.
- **REST**: `GET/PUT /api/settings` (GET = schema + values + sources; PUT takes `{"values": {...},
  "clear": [...]}` or a flat object and answers with the fresh description), `GET /api/settings/schema`,
  `POST /api/settings/test/{channel}` (optional `{"values": {...}}` = unsaved form fields by short name,
  runs off the event loop), `GET /api/settings/export`, `POST /api/settings/import[?dry_run=1]`,
  `GET /api/notifications/recent` (`{"notifications": [...], "channels": [enabled and complete]}`).
  Errors: `SettingsError` -> 400 `{error, errors: {key: why}}`.
- **MCP tools**: `get_settings(section="")`, `update_settings(values, clear=[])`,
  `send_test_notification(channel)`. `update_settings` refuses `security.*` keys (owner-only).
- **Dashboard**: header **Settings** button -> a full-screen page generated from `GET /api/settings`
  (tabs per group, field widgets per type, secrets as Set / Not set with Clear, a Send test button per
  channel that uses the unsaved form values, validation errors beside the fields, recent notifications,
  export / import).

## Security: API keys (`app/security.py`)

Implementation notes for the key check (the user-facing description is in the README, *Security*). Standalone
module, stdlib only; `server.py` wraps the app returned by `streamable_http_app()` in `AuthMiddleware`.

- **Install mode.** `detect_existing_install()` runs before the fleet store exists; the decision (`fresh` ->
  `on`, `upgrade` -> saved `warn`) is recorded in `api_keys.json` (`meta.install`, `meta.initial_mode`) and made once.
  `BAMBU_MCP_API_AUTH` set in the environment suppresses the upgrade default.
- **Storage.** `/data/api_keys.json`, mode 0600, atomic writes: `meta`, `session_secret` (signs dashboard cookies),
  `keys[]` = `{id, name, scope, prefix, salt, hash = sha256(salt + key), created, last_used, last_path, revoked,
  revoked_at}`. `last_used` is updated in memory per request and written at most once a minute. The file is
  re-read when another process (`python security.py create-key`) changes it.
- **Routes.** `GET/PUT /api/security` (mode, `dashboard_login`, `dashboard_password`, `clear_dashboard_password`,
  `viewer_password`, `clear_viewer_password`, `session_idle_hours`, `session_max_days`, `device_days`,
  `dismiss_login_offer`; GET adds the key list, counts, stats, the warn-log tail and the `connection`), `GET/POST
  /api/keys`, `POST /api/keys/{id}/revoke`, `DELETE /api/keys/{id}`, `GET /api/auth/me` (`read`), `GET /api/devices`,
  `PATCH|DELETE /api/devices/{id}`, `POST /api/devices/revoke-others`, `POST|DELETE /api/pairing`,
  `DELETE /api/sessions/{id}`, `POST /api/auth/logout-everywhere`, and the public `POST /api/auth/login`,
  `POST /api/auth/pair`, `POST /api/auth/logout`, `GET /api/health`. Also public (icons and the web-app manifest, no data):
  `GET /favicon.ico`, `/favicon.svg`, `/apple-touch-icon.png`, `/manifest.webmanifest`, `/static/brand/<file>`.
  `POST /api/keys` returns the key once, with `Cache-Control: no-store`.
- **Dashboard sign-in** (`DashboardAuthStore`, `/data/dashboard_auth.json`, mode 0600, re-read when another process
  changes it). `login_required()` = an owner password exists and (the switch is on, or was never set - an install that
  had a dashboard password before the switch existed keeps asking for it). Off = the old stateless `v1` cookie, no
  server state. On = `v2.<session id>.<expiry>.<hmac>` session cookies backed by a server-side record (idle / absolute
  limits) or a `printhub_device` cookie (`<device id>.<token>`, record holds salt + sha256, previous hash for the
  2-minute rotation grace). Identity kind `cookie` carries scope `control` or `read` (the view-only role); the
  middleware enforces it with the same scope tables as for API keys, also when the key check is Off. Pairing codes
  are memory-only, one at a time. `AuthMiddleware` adds the security headers (`CSP`, `_Out`) to every response.
- **MCP.** `list_api_keys` only; `update_settings` refuses `security.*` keys. The middleware looks inside
  `POST /mcp` bodies (up to 256 KiB) for `tools/call` and compares the tool with `security.TOOL_SCOPES`.
- **Scope tables.** `security.REST_SCOPES` and `security.TOOL_SCOPES` list every route and tool; anything not
  listed needs `control`. `tests/test_auth.py` fails when a registered route/tool has no row, so a new one gets
  a deliberate decision.
- **Dashboard.** Settings -> Security is a custom page over these routes (it is its own top-level settings tab). It does not use the
  generic Save button: each action (mode, key, password) is applied at once, after a confirmation where it can lock something out.
  A `401` on any `/api/*` call sends the page back to `/dashboard` (the sign-in page when sign-in is on). A view-only sign-in
  (`GET /api/auth/me` -> `view_only`) hides every element marked `data-ctl`.
- **Settings in the other sections** (`/api/settings`) are `read` to view and `control` to change; an agent holding
  a `read` key therefore sees non-secret values such as the ntfy topic, chat id and mail addresses.
