<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../app/static/brand/logo-lockup.svg">
    <img src="../app/static/brand/logo-lockup-light.svg" alt="Fabrica — Print Farm - printer management, agent-first." width="460">
  </picture>
</p>

# Fabrica — the full guide

> **Naming:** the product is **Fabrica** (full title "Fabrica — Print Farm"); the repository, Docker image and container are all `fabrica` now.
> Environment variables keep their `BAMBU_MCP_` prefix for compatibility.

**Give any AI agent hands on your Bambu Lab printers — over your own LAN, no cloud.**

An [MCP](https://modelcontextprotocol.io) server that lets Claude, or any
MCP-capable agent, browse the jobs already on your printer's SD card, read live
AMS spool state, and **start prints with explicit spool selection** — the thing
Bambu gates behind its cloud, and that most third-party screens (looking at you,
Panda Touch) can't do because they can't send an AMS mapping table.

```
You:    what can I print?
Agent:  9 jobs on the P1S. Heads up — "Wall Hook - Heavy" is sliced WHITE but your
        convention for hooks is GREEN.
You:    print it in green
Agent:  Tray 0 (GREEN PETG), 8h06m, 224g. Plate clear?
You:    yes
Agent:  ✅ RUNNING.
```

Runs anywhere Docker runs (Raspberry Pi included — multi-arch image), speaks
streamable-HTTP MCP on port 8271. Pair it with Tailscale and you can start
prints from anywhere on earth without exposing anything to the internet.

## Features

- 📋 **`list_sd_files`** — every `.3mf` on the card, enriched with print time,
  weight, and filament colors parsed from the file itself (cached after first read)
- 🧵 **`ams_state`** — live spool readout, **colors as names** ("Tray 0: GREEN PETG")
- 🎯 **`suggest_mapping`** — matches a job's filaments to your loaded spools,
  applies your part-color conventions, flags slicing errors
- 🖨️ **`start_print`** — from SD card, with `ams_mapping` spool override
- ⏯️ `pause_print` / `resume_print` / `cancel_print`
- 💡🔥 **Printer controls** — `set_light` (`chamber_light` still works), `preheat`, `cooldown`,
  `unload_filament`, `load_filament`, and `printer_controls` to see what a given printer supports
  ([details below](#printer-controls))
- 🔌 `connect_printer` — when the printer itself shows NOT CONNECTED: OctoPrint opens its USB/serial link (only if it is closed);
  Klipper restarts only after you confirm (`confirm_restart`), and never on a ready, printing or unreachable printer (Bambus connect on their own)
- 🖥️ Built-in touch **dashboard** at `/dashboard` (1024×600 kiosk) — same safety gates as the
  tools; every waiting screen has a Back button and a 20 s timeout, never a dead end
  - **The dashboard picks its layout for the screen.** A big window with a mouse (about 1100 px or wider) gets the **Desktop**
    layout that uses the whole window; an iPad or other touch tablet gets the 10-inch touch layout scaled to fill its screen; a phone gets
    the phone layout. **More > Switch view** overrides it (Desktop / 10-inch touch / Phone / 5-inch wall, or Automatic) and remembers the
    choice in that browser only. In the address, `?view=desktop|kiosk|touch|phone|5in` wins over detection; the wall screens keep
    `?kiosk=1` / `?layout=5in` and never change. Settings > Agents / MCP > About says which view a browser is using and why.
  - **Wall screens have no keyboard.** Open the 1024×600 screen at `/dashboard?kiosk=1` (the 5-inch screen's
    `?layout=5in` counts as a kiosk too): the file sheet then has no **Rename** button, because a rename field is
    useless without a keyboard. Desktop and phone keep it. The page never guesses a kiosk from the window size
    (landscape phones exist), so add `kiosk=1` to the kiosk's startup URL once.
  - **Open a printer directly.** `/dashboard?printer=<name>` (URL-encoded, not case sensitive) opens that printer's screen; it combines
    with `group=`, `layout=` and `kiosk=`. An unknown name shows the fleet with a "not found" notice. The "Print finished" message
    links here (Settings > Notifications > Hub address). The ← Fleet button drops the parameter again.
  - **Spaghetti on every screen.** Each fleet card says `Spaghetti: WATCHING / READY / OFF / NO CAMERA / DETECTOR DOWN`; a `WARNING` or
    `FAILURE` is an inverted line plus a heavy frame on the card and a banner above the cards. The printer screen has the on / off switch for
    that printer (it edits Settings > Detection > "Do not watch these printers").
- 🗂️ **File list and file sheet.** Each file shows its picture (or the words NO PICTURE), its full name, the
  material as words (PETG, PLA, PLA PETG ...) before the Print button, and Delete. Tap a file for the sheet: big
  picture, print time, filament, layer height, nozzle, size, date, storage, the estimated cost and when it was last
  printed, with Print / Delete / Rename / Back. A PrusaLink printer lists long names and `.bgcode` files; its pictures
  are read from the printer's own thumbnail service once and cached.
- 🖼️ **Plate thumbnails** — the dashboard file list shows each job's sliced plate preview.
  Bambu printers: pulled from the same `.3mf` download that already reads print time/weight/
  filaments. MK4/OctoPrint: pulled from the PrusaSlicer preview embedded in the gcode file's
  own header (just the header — not the whole file). Either way it's cached alongside the rest
  of that file's metadata (older cache entries backfill their thumbnail the next time the
  printer sits idle)
- 🗑️ **Delete from the dashboard** — two-tap confirm (file name + printer spelled out, no
  color-only cues); refuses while a job is active or if the file isn't on the printer's current
  list
- 🔄 **`set_tray`** — re-register a tray after a spool swap, by material AND color
  *name* (validates against Bambu's generic material profiles — PLA/PETG/ABS/ASA/TPU —
  and sets the right nozzle temp range; the dashboard's Change flow asks material
  first, then color)
- 📷 **Chamber camera** — `get_camera_snapshot` hands an agent the current JPEG so it can
  check on a print; the dashboard shows a small thumbnail per printer that has a camera, and
  a tap opens a live view. All viewers share ONE camera connection per printer
  (see [Camera](#camera-p1--a1-series))
- ➕ **Add / edit / remove printers from the dashboard — or by an agent.** Tools:
  `list_printer_types`, `test_printer_connection`, `add_printer`, `update_printer`, `remove_printer`.
  Connection is tested before saving; changes apply live (see [Quick start](#quick-start-for-a-new-install))
- 🗑️ `delete_sd_file`, `printer_status`, `list_printers`
- 🔐 **API keys** — named `read` / `control` keys for agents and scripts, on by default
  ([details below](#security-api-keys))
- 💾 **`backup_printer_files` / `backup_status`** — zip up every file on a printer into one
  archive on the Hub ([details below](#file-backups))
- 🧾 **History and what a print really costs** — every print attempt is logged (finished, failed,
  cancelled) and priced from your filament prices (the cost is the filament alone; switch on
  Settings → Costs → *Include print time costs* to add electricity and machine wear); the dashboard's
  **History** page shows success rate, material used, cost, and the *true* cost per part (failures
  charged to the good parts), with a CSV download. Tools: `recent_jobs`, `job_stats`, `cost_per_part`.
  Rates: Settings → Costs. How it works: [docs/joblog.md](joblog.md)
  The **Analytics** page next to it sums the same log over 7 days to a year (success rate, hours, filament, cost, a prints-per-day chart,
  per-printer utilization and % online, failures, the most printed files); see [Analytics](joblog.md#analytics-get-apianalytics-the-analytics-page).
- 🔎 **Find printers on your network** — **Printers → Discover printers** scans the networks you list
  (works from Docker) and pre-fills the Add form; tool `discover_printers`.
  [docs/discovery.md](discovery.md)
- 🍝 **Spaghetti detection (optional add-on)** — watches every running print through its camera and
  alerts you; starts in learn-only mode and never pauses or stops a print. Tools: `detection_status`,
  `score_snapshot`. Setup: Settings → Detection, [docs/detection.md](detection.md)
- 🗂️ **File library** — one library on the Hub for every print file across the fleet: folders, search,
  grid or list, plate previews and full details; send a file to any idle printer, then print it through the
  normal confirm. Tools: `library_list`, `library_file`, `library_send`, `library_save_from_printer`.
  [docs/library.md](library.md)
- ⚙️ Everything above is configurable on the dashboard; the settings reference is in
  [docs/settings.md](settings.md)

### Designed-in safety
- `start_print` **refuses** unless the agent passes the `confirm_string` that
  `suggest_mapping` just produced. It is a **single-use code that expires after 5 minutes**, kept on the Hub and
  tied to that printer, file, tray mapping and what is loaded in the AMS — it cannot be written by hand, replayed, or
  read out of an error message. `start_print` also needs `bed_clear_confirmed_by_human=true` (the human's own
  statement that the plate is clear), takes a per-printer lock (no double start) and refuses a printer that is
  not positively idle. Busy printers refuse starts outright.
- **Multi-filament jobs** (e.g. a support-interface filament in a different
  material) are mapped filament-by-filament, indexed by the slicer's own filament
  slot — never by list order — and no two filaments are ever assigned the same
  tray. If a needed material/color has no free spool, `suggest_mapping` **blocks**
  the job (empty confirm string) rather than guessing; `start_print` independently
  refuses an empty confirm or an all-unmapped `ams_mapping` for Bambu printers.
- **Colorblind-safe:** every color leaves the server as a NAME, never a bare hex
  or hue. `set_tray` takes names; the server owns the hex palette.
- One long-lived MQTT session per printer with gentle reconnects — the P1-series
  ESP32 broker destabilizes (and kicks other clients like OctoEverywhere
  companions) when clients churn connections.

## ⚠️ Firmware compatibility — read this first

Bambu has been progressively locking down local control ("Authorization Control").
What you need for **print-start** to work over local MQTT:

| Printer | Firmware | What's required |
|---|---|---|
| P1P/P1S | ≤ 01.07.x | **LAN-only mode** (no Developer Mode toggle exists — LAN mode alone = full control). In cloud mode these firmwares accept status/light/stop but **silently ignore print-start**. |
| P1P/P1S | ≥ 01.08.03 | LAN-only mode **+ Developer Mode** |
| A1 / A1 mini | ~01.08.x (pre-auth-control) | Works even in **cloud mode** (verified on 01.08.01.00) |
| X1 series / newer fw | with Authorization Control | LAN-only mode **+ Developer Mode** |

Monitoring/read tools generally work in any mode. If `start_print` reports
"sent" but the state never changes, your printer is gating the command — check
the table. After toggling LAN mode, give the printer a minute to settle (the
first command right after a toggle is often ignored). Toggling LAN/Developer
mode **may regenerate your access code** — update it under **Printers → Edit** (and
anywhere else you use it) if so.

Good news: OctoEverywhere companions, a Panda Touch, and this server can all
share a printer simultaneously — verified live.

## Quick start for a new install

No config file, no `.env`, no editing YAML — you add printers from the web page.

1. **Install Docker** (Docker Desktop on Mac/Windows, or `curl -fsSL https://get.docker.com | sh` on Linux / Raspberry Pi).
   You need **Docker Compose 2.24 or newer** (`docker compose version`) — the compose file marks `.env` as
   optional, which older versions reject.
2. **Create `docker-compose.yml`** in an empty folder (same as `docker-compose.ghcr.yml` in the repo):
   ```yaml
   services:
     fabrica:
       image: ghcr.io/wsturner88/fabrica:latest   # amd64 + arm64
       container_name: fabrica
       restart: unless-stopped
       ports:
         - "8271:8271"
       env_file:
         - path: .env          # optional: only for BACKUP_KEEP or a legacy printer import
           required: false
       volumes:
         - ./data:/data        # printers, secrets, metadata cache, backups
         # Upgrading from the old config.yml setup? Uncomment to import its printers once
         # (the colour palette / part-colour conventions in it are still read every start):
         # - ./config.yml:/app/config.yml:ro
   ```
   No `.env` and no `config.yml` are needed. (`.env.example` lists the few optional settings, such as `BACKUP_KEEP`.)
3. **Start it:** `docker compose up -d`
4. **Open the Dashboard:** `http://<host>:8271/dashboard` (use the machine's IP address or host name;
   `http://localhost:8271/dashboard` on the same machine).
5. **Add your printer:** the first screen says *No printers yet* — tap **Add your first printer**,
   pick the kind (Bambu Lab, OctoPrint, PrusaLink, Klipper/Moonraker), follow the numbered **Before you
   start** steps for that kind (Bambu: LAN Only mode and Developer Mode, IP, serial, access code; Prusa:
   enable PrusaLink; OctoPrint: create an API key; Klipper: trusted clients), fill in the form (each field
   explains where to find the value), tap **Test connection**, then **Save**. If the test fails, the
   message ends with a tip that links back to the step that fixes it.
   Later, the **Printers** button in the header lets you edit (new IP address, rotated access code…)
   or remove printers — no restart needed.

Building from source instead: clone the repo and `docker compose up -d --build` (the repo's
`docker-compose.yml` is the same file with `build: .` in place of `image:`).

**Where your data lives** — everything is in the `/data` volume (`./data` above):

| File | What |
|---|---|
| `printers.json` | the fleet: name, type, address, serial, model (no secrets) |
| `secrets.json` | access codes / API keys / passwords, keyed by printer name, file mode `0600`. **Write-only:** no web page, REST call or MCP tool ever shows one — only "saved" / "not set". Back it up like any credential file |
| `api_keys.json` | the API keys (a salted hash of each, never the key), the dashboard-session signing secret and the install record, file mode `0600` — see [Security](#security-api-keys) |
| `dashboard_auth.json` | the optional dashboard sign-in: on/off switch, the password hashes, trusted devices (a hash of each token, never the token) and signed-in sessions, file mode `0600` — see [Securing the dashboard](#securing-the-dashboard-optional) |
| `metacache.json`, `thumbs/`, `backups/` | file-metadata cache, plate previews, printer file backups |
| `jobs.sqlite` | the print log behind the History page (one row per print attempt; job names can carry order or customer info — treat it, and the CSV export, accordingly). Back it up with the rest of `/data` |
| `uptime.jsonl`, `uptime_alive.json` | when each printer became reachable / unreachable (one line per change, capped at 256 KB) and a small heartbeat file; they feed the Analytics page's % online |
| `detection/` | spaghetti detection: each printer's learned baseline and, in learn-only mode, `scores.jsonl` (rotated at 1 MB) |

**Access control.** Agents and scripts need an API key (a new install requires one from the start; an
upgraded Hub begins in a log-only mode — see [Security](#security-api-keys)). The dashboard itself opens
in a browser without one, so **unless you turn on dashboard sign-in it is protected only by your network**. Keep the
Hub on your home network or reach it through Tailscale / a VPN — don't expose it to the internet.

**Upgrading from the old `config.yml` + `.env` setup.** On the first start with an *empty* `/data`
and a `config.yml` that has a `printers:` list, the Hub imports those printers **once**: the
`access_code_env` / `api_key_env` variables are read from the container's environment and copied into
`secrets.json`, and the log says `[fleet] imported N printer(s)`. After that the store in `/data` is
the truth — later edits to `config.yml`'s `printers:` list (or to the env vars) are ignored, and
removing every printer never brings them back. Rotate a code from the **Printers** button. (If a
variable was missing at import time the printer is still imported, with the old variable name kept as a
fallback until you save a code.) `config.yml` is otherwise optional: if present it still supplies the
colour `palette` and part-colour `conventions` exactly as before. To import, uncomment the
`./config.yml:/app/config.yml:ro` line in the compose file (and keep your `.env` next to it so the
`*_env` variables resolve). A setup that already mounts `config.yml` and uses a `.env` keeps working
unchanged; remove them whenever you like once the import has happened.

### Finding your printer's details
- **Access code:** printer screen → Settings → WLAN.
- **Serial:** printer screen → Settings → Device, or — no login required —
  read it off the printer's TLS certificate:
  `echo | openssl s_client -connect <printer-ip>:8883 2>/dev/null | openssl x509 -noout -subject`
- **IP:** your router's client list. Set a DHCP reservation — Bambu printers
  don't do static IPs well.

### Use it on your phone
Open the dashboard in Safari on your iPhone, tap **Share → Add to Home Screen**, and it opens full-screen like an app:
one printer per row with its camera, finish time and progress, then the printer screen to start, pause or cancel, plus
History, Backups and Settings. It works in portrait and landscape and needs no pinch-zoom. The ↻ button in the header
reloads it (a Home Screen app has no browser reload). Push alerts to the phone are not built in yet: they need a secure
(HTTPS) address, which a plain `http://` LAN address is not. When you want them, put the Hub behind HTTPS with
`tailscale serve`, and a later release can add notifications on top of that.

### Connect an agent
First make an API key for it (dashboard **Settings → Security**, or
[`POST /api/keys`](#security-api-keys)), then give the client the key as a bearer header:
```bash
# Claude Code
claude mcp add --transport http fabrica http://<host>:8271/mcp \
  --header "Authorization: Bearer <your key>"
```
Any other MCP client: streamable-http transport, same URL, with the HTTP header
`Authorization: Bearer <your key>` — for example, in a JSON client config:
```json
{ "mcpServers": { "fabrica": {
    "type": "http", "url": "http://<host>:8271/mcp",
    "headers": { "Authorization": "Bearer <your key>" } } } }
```
(`X-Api-Key: <your key>` is accepted too, for tools that cannot set an `Authorization` header.)
Over Tailscale, use the host's tailnet address and print from anywhere. Give an agent a **read** key
unless it needs to act (see [scopes](#scopes)).

## Security (API keys)

Anything that can reach the Hub's port could otherwise drive the printers — malware, a guest laptop, or an
agent that has been tricked by text it read. So the REST API (`/api/*`) and the MCP endpoint (`/mcp`)
check an **API key**. Only `/dashboard`, `/`, `/api/ui-version`, `/api/health` (just `{"ok": true}`), the brand
files (`/favicon.ico`, `/favicon.svg`, `/apple-touch-icon.png`, `/manifest.webmanifest`, `/static/brand/*` — icons only, no data)
and the dashboard login routes are open.

**Modes** (Settings → Security, `PUT /api/security`, or the `BAMBU_MCP_API_AUTH` environment default):

| Mode | What happens to a request without a valid key |
|---|---|
| `on` | refused: `401 {"error": "API key required"}` (MCP: a real `401` with `WWW-Authenticate: Bearer`) |
| `warn` | still works, but is counted and logged — `GET /api/security` shows who called what (path, time, user agent, address) so you can see what would break |
| `off` | nothing is checked |

A **new install starts in `on`**. A Hub that **already has data** (it finds `printers.json`, `settings.json`, …
in `/data` from before this feature) starts in **`warn`**, once, so your running agents and screens keep
working; this is recorded in `api_keys.json` and never repeated. To roll out: create a key per agent/script,
give it to them, watch the *without a key* list on the Security page empty out, then switch to `on`.
`warn` logs are kept in memory (they start over when the Hub restarts).

**On the dashboard** it is all under **Settings → Security**: pick the mode (each choice says in plain words what it
does), **Generate key** (name + read-only or full control; the key is shown once, with a Copy button), a list of
keys with when and where each was last used, **Revoke** / **Delete** (both ask first), the list of callers that
came without a key (*what would break* if you switched to On — it refreshes while the tab is open) and the
dashboard sign-in (switch, password, trusted devices, view-only password). While the mode is not On, the fleet page carries a notice saying so.

**Keys.** Create one with a name and a scope; the key (`ph_…`) is **shown exactly once** — copy it then. Only
a salted hash is stored; the list shows the name, scope, the first 6 characters, and when/where it was last
used. Revoke a key to cut it off at once, delete it to forget it.
```bash
# keys are made on the dashboard (Settings > Security) - an API key can no longer mint keys (see Hardening).
# or on the Hub's machine (also the way back in when you are locked out):
docker exec fabrica python /app/security.py create-key "First key" --scope control
```
Send it as `Authorization: Bearer <key>` or `X-Api-Key: <key>`. Keys are never accepted in a URL query string.
**Keys travel in clear over plain `http://`** — anyone who can watch your network can read them. Put the
Hub behind HTTPS, e.g. on a tailnet: `tailscale serve --bg 8271` (then use `https://<machine>.<tailnet>.ts.net/mcp`;
publish the container port as `127.0.0.1:8271:8271` to close the plain-`http` port).

<a id="scopes"></a>**Scopes.** `read` can look but never change anything; `control` can do everything.

| Scope | REST routes | MCP tools |
|---|---|---|
| `read` | `GET` fleet, printers, printer-types, printer/{name}[/controls\|snapshot\|stream\|files], thumb, backups (the list), settings (+schema, export; secrets are redacted), notifications/recent, jobs, jobs/{id}, stats, costs/file, detection, discover/suggestions, discover/{scan}; `POST` printer/{name}/suggest | `list_printers`, `printer_status`, `ams_state`, `list_sd_files`, `suggest_mapping`, `printer_controls`, `get_camera_snapshot`, `backup_status`, `list_printer_types`, `get_settings`, `recent_jobs`, `job_stats`, `cost_per_part`, `detection_status` |
| `control` | everything else: start / action / control / tray / delete / backup, fleet add-edit-remove and test, settings write / import / test, backup downloads, `/api/security`, `/api/keys`, the job export `GET /api/jobs.csv`, starting or stopping a network scan (`POST` / `DELETE` discover), `POST /api/detection/test`, scoring a camera picture | `start_print`, `cancel_print`, `pause_print`, `resume_print`, `connect_printer`, `set_light`, `chamber_light`, `preheat`, `cooldown`, `unload_filament`, `load_filament`, `set_tray`, `delete_sd_file`, `backup_printer_files`, `test_printer_connection`, `add_printer`, `update_printer`, `remove_printer`, `set_detection_area`, `update_settings`, `send_test_notification`, `list_api_keys`, `discover_printers`, `score_snapshot` |

The scope is checked on **every MCP tool call**: a read key that calls `start_print` gets a clear
`REFUSED: this API key has 'read' access…` answer. Routes and tools not in the table need `control`.
A key's scope is enforced even in `warn` mode. There is deliberately **no MCP tool that creates, revokes or
deletes keys, or that changes the security settings** (`update_settings` refuses `security.*`): an agent must
not be able to mint itself more power or switch off the check. `list_api_keys` only lists.

**The dashboard.** The dashboard's own scripts call the same API, so opening `/dashboard` hands the browser an
`HttpOnly`, `SameSite=Strict` session cookie (`Secure` when served over HTTPS) that counts as a `control` credential for
that browser — the kiosk, a laptop and a phone keep working with no typing. Writes made with the cookie must
come from the dashboard's own origin (`Origin` / `Sec-Fetch-Site` are checked), and the cookie is not
handed out to unexpected host names (a guard against DNS rebinding; names like `printhub.local`, IP addresses
and `*.ts.net` are fine — list others in `BAMBU_MCP_ALLOWED_HOSTS=name1,name2`).
**Be honest about what this protects:** unless you turn on dashboard sign-in (next section), anyone who can
open the dashboard can control the Hub, so on a network you do not trust, turn it on.

### Admin password (keys and security settings)

Creating or revoking API keys, changing the key-check mode, sign-in, devices, pairing and any `security.*`
setting need an **admin password** — even when the dashboard itself opens without one. Set it in
Settings → Security (the first one can be set from the dashboard in a same-origin browser); unlocking lasts
30 minutes. API keys can never manage keys or security. Lost it? `python /app/security.py reset-admin-password`
(or `clear-admin-password`) inside the container. Turning on dashboard sign-in or a view-only password also
switches the key check to **On** — agents then need API keys.

Related settings: `BAMBU_MCP_TRUSTED_PROXIES` (only then is `X-Forwarded-For` / `X-Forwarded-Host` trusted, for
login rate limits behind a reverse proxy) · `BAMBU_MCP_ALLOW_LOOPBACK_DETECTOR=1` (allow a detector on
`localhost` when the Hub runs outside Docker).

### Securing the dashboard (optional)

Sign-in is a switch you control. An install that never turned it on behaves exactly as described above: the
page opens without a password and hands the browser the session cookie. A **brand-new install asks first** (below);
an upgraded Hub is never asked and nothing about it changes.

**First-run step: "Protect your printers".** The first time the dashboard opens on a new install it shows a full-screen
step before anything else: *Anyone on your network who opens this page could start, stop or heat your printers.* Type a
password twice and press **Set password** — sign-in is switched on, this browser stays signed in (it becomes a trusted
device) and the password also does the admin work until you choose a separate admin password. Wall screen? Pair it later
from Settings → Security (**Add a device**). **Skip — I understand anyone on my network can control my printers** needs a
second tap to confirm, is remembered (the step does not return) and leaves sign-in off; Settings → Security keeps saying
*SIGN-IN: NOT REQUIRED*. Until one of the two happened the Hub refuses changes from a browser (`403`, pointing to the
dashboard); reads, the dashboard itself and agents with API keys are unaffected. Not asked while the key check is Warn or
Off. To see the step again: `docker exec fabrica python /app/security.py reset-first-run`.

**Settings → Security → Dashboard sign-in.** *Require sign-in to open the dashboard* needs a password in the same
step (so you cannot lock yourself out). Turning it on signs every other browser and screen out — this one stays
signed in — and API keys and agents are not affected. Turning it off removes the passwords and forgets the devices.

| Piece | What it does |
|---|---|
| **Sign-in page** | `/dashboard` shows it instead of the dashboard. Password, a **Remember this device** box (and a name for it), and **Pair with a code from the Hub** (a big touch keypad, made for the 1024×600 wall screen). |
| **Sessions** | Without *remember*, a browser gets a server-side session: signed out after **12 h idle** (an open dashboard tab counts as active) or **7 days** in all, whichever comes first. Both are settings. Listed in Security (**Signed in now**), each can be ended; **Sign out everywhere** ends all of them and forgets every device (this one too) — API keys keep working. |
| **Trusted devices** | *Remember this device* issues a long-lived token in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` over HTTPS), kept **hashed** with a name (default from the browser, e.g. *iPad · Safari*; rename it any time). It **rotates** (a new token about every 24 h; the previous one works for 2 more minutes so parallel requests never fail) and expires after **90 days unused** (setting). The Security tab lists each device: name, first seen, last seen, last address, when it expires, **Revoke**, and **Revoke all other devices**. A password change ends sessions but not devices; use *Sign out everywhere* for both. |
| **Add a device with a one-time code** | For the screen with no keyboard: Security → **Add a device** shows a **6-digit code, valid 5 minutes, usable once**. On the screen: sign-in page → *Pair with a code from the Hub* → touch the digits → the screen is a trusted device (full access or view-only, your choice when you make the code). Five wrong guesses burn the code; wrong codes are also rate-limited per address and overall. |
| **Kiosk automation** | The wall screen's sleep-timer daemon (or any script) keeps using its **API key** — unchanged. |
| **View-only password** (optional) | A second password: the browser can see the fleet, cameras and history; every button that changes something is hidden (a *VIEW-ONLY* label is shown) and the Hub refuses it anyway (`403`, `read` scope). Needs sign-in on; must differ from the main password. Also available as a view-only pairing code. |

**Hardening that goes with it.** Wrong passwords: 5 per 5 minutes per address and 25 per 5 minutes overall (even the
right password is refused while locked); pairing codes: 5 per address, 10 overall. Passwords and codes are compared in
constant time. Writes made with a cookie must come from the dashboard's own origin (`Origin` / `Sec-Fetch-Site`
checked), cookies are `SameSite=Strict` and `Secure` when the request came over HTTPS (or `X-Forwarded-Proto: https`),
and the cookie is not handed to unexpected host names (DNS-rebinding guard; names like `printhub.local`, IP addresses
and `*.ts.net` are fine — list others in `BAMBU_MCP_ALLOWED_HOSTS=name1,name2`).

**Security headers** are sent on every response, and on HTML pages also `X-Frame-Options: DENY` and a
`Content-Security-Policy`: `default-src 'self'` with `frame-ancestors 'none'`, no plug-ins, no `<base>`, forms and
fetches to the Hub only. The dashboard is a single file with inline scripts, inline `onclick` handlers and inline
styles, so `script-src` and `style-src` must allow `'unsafe-inline'` — a stricter nonce policy would mean splitting the
page up. What it does forbid is anything loaded from outside: no foreign scripts, styles, images, frames or fetches.
If you deliberately embed the dashboard in another page (a frame, a home-automation panel), set
`BAMBU_MCP_ALLOW_FRAMING=1` to drop the frame protection.

**Use HTTPS.** *Passwords travel unencrypted over plain `http`* — anyone who can watch your network can read the
password and the sign-in cookie. Put the Hub behind HTTPS, e.g. on a tailnet: `tailscale serve --bg 8271`, then open
`https://<machine>.<tailnet>.ts.net/`. The Security tab and the sign-in page say in words whether the connection you
are using is **secure** or **not secure** (`GET /api/auth/me` and `GET /api/security` return a `connection` field).

**Locked out, or lost the password?** On the Hub's machine (works while the Hub runs; it notices on its next request):
```bash
docker exec fabrica python /app/security.py disable-dashboard-login        # sign-in off; dashboard opens again
docker exec -it fabrica python /app/security.py reset-dashboard-password   # asks for a new password; signs every browser out
docker exec fabrica python /app/security.py list-devices                   # id, name, role, first / last seen, address
docker exec fabrica python /app/security.py revoke-device d_1a2b3c4d       # or: revoke-device all
docker exec fabrica python /app/security.py create-key "First key" --scope control   # and list-keys
```
None of these ever prints a key, a password or a token.

REST (all `admin` = dashboard browser only, except `GET /api/auth/me` = `read`): `PUT /api/security` takes `dashboard_login`,
`dashboard_password`, `clear_dashboard_password`, `viewer_password`, `clear_viewer_password`, `session_idle_hours`,
`session_max_days`, `device_days`; `GET /api/devices`, `PATCH|DELETE /api/devices/{id}`, `POST /api/devices/revoke-others`,
`POST|DELETE /api/pairing`, `DELETE /api/sessions/{id}`, `POST /api/auth/logout-everywhere`. Public: `POST
/api/auth/login` (`{"password", "remember"?, "device_name"?}`), `POST /api/auth/pair` (`{"code"}`), `POST /api/auth/logout`.
There is no MCP tool for any of this: an agent cannot sign anyone in, mint a code or change the switch.

### Hardening (2026-10): what holds in every mode, `on`, `warn` and `off`

- **Browser boundary.** A state-changing request (anything but GET/HEAD) is refused when the browser says it is
  cross-site (`Origin` not the Hub's own, or `Sec-Fetch-Site` other than same-origin), when its `Host` is not a local name or
  IP (DNS rebinding; list extra names in `BAMBU_MCP_ALLOWED_HOSTS`) unless it carries a valid API key, and when its
  body is not `Content-Type: application/json` (`415`). In `warn`/`off` an unauthenticated *read* from a foreign Host
  is refused too. Agents and `curl` (no `Origin`) are unaffected.
- **Only the dashboard administers.** `/api/keys*`, `/api/security`, trusted devices, pairing codes and sessions need a
  signed-in dashboard browser (scope `admin`) — an API key, even a control key, gets `403`. So does changing any
  `security.*` setting through `PUT /api/settings`. In `warn` mode these routes are never open to a keyless caller.
  The Security page lists the keys that were created while the check was warn/off, and warns when no dashboard password is set.
- **MCP bodies are read strictly.** For a read key the body must be plain UTF-8 JSON (no BOM, UTF-16/32, duplicate keys);
  anything else is refused (`400`) instead of being waved through. Every tool also re-checks its scope from the request that carried it.
- **Saved secrets stay put.** A *Test* button (notification channels, detector, printer) and a *save* that points a channel at a new address
  never reuse the secret saved for the old one — type it again. The result says so (`note`).
- **Read-only callers** (read keys, view-only sign-ins) see `(hidden - needs full access)` for the webhook URL, ntfy topic, SMTP user and
  Telegram chat id, no `user:password@` in printer URLs, and cannot make the Hub download files.
- **Sync MCP tools run off the event loop**, so a slow tool no longer freezes the dashboard, cameras and other agents.

## Managing printers (REST + MCP)

| Endpoint | Purpose |
|---|---|
| `GET /api/printer-types` | The printer families this build can add and each one's form fields (what the dashboard wizard renders) |
| `GET /api/printers` | The saved printers, secrets redacted (`"access_code": "set"` / `"not set"`) |
| `POST /api/printers/test` | `{"type", "values", "name"?}` — try a connection without saving. `200 {"result": {ok, model, firmware, has_camera, message}}` (`ok:false` carries the reason in plain English). Pass an existing `name` to reuse its stored secret when `values` leaves it blank |
| `POST /api/printers` | `{"type", "values", "skip_test"?}` — add. Tests first unless `skip_test`; `201` on success, `422` + the reason if the test fails (nothing saved), `409` duplicate name, `400` bad input |
| `PUT /api/printers/{name}` | `{"values", "skip_test"?, "force"?}` — edit. A blank/missing secret keeps the stored one; the name can't change. `409 {"busy": true}` while the printer is printing/paused or being backed up, unless `force` |
| `DELETE /api/printers/{name}?force=1` | Remove (disconnects it first). Same busy rule |

Same capabilities as MCP tools: `list_printer_types`, `test_printer_connection`, `add_printer`,
`update_printer`, `remove_printer`. Names are 1–32 characters (letters, digits, `-` `_` `.`; no spaces)
and permanent. Edits swap the connection safely: the old MQTT session is fully closed before the new
one opens, so a Bambu printer never sees two clients from the Hub. The Bambu connection test opens one
short MQTT session (login + one status request), then closes it.

### Groups

A **group** is a label you put on printers, for example `Production` for the printers that earn money and
`Workshop` or `Prototypes` for the ones you experiment on. A printer can be in several groups, or none.
History and costs can then be filtered by a group, or by any hand-picked set of printers, and summed
(see [History and costs](joblog.md#filtering-by-group-or-printers)). A group exists while at least one
printer is in it; it has no settings of its own.

- **Dashboard:** *Printers → Edit* has a **Groups** field: type a name and press Enter (or tap an
  existing group under "Existing groups"), tap × to remove one. **Save groups now** applies the change at
  once - no connection test, and it works while the printer is printing, because a group never changes how the
  printer connects. Below the printer list, a **Groups** section lets you **Rename** a group on every
  printer at once (renaming onto another group merges the two) or **Delete** it (the label goes; the printers
  and their history stay). The fleet (home) screen is unchanged.
- **Rules:** at most 8 groups per printer, 32 characters each, any printable text except `/` `\` `,`.
  Names match without regard to case (`production` and `Production` are one group; the first spelling used wins).
  Groups are saved with the printer in `printers.json` (no secret is involved). A Hub from before this
  feature loads as if every printer had no groups.

| Endpoint | Scope | Purpose |
|---|---|---|
| `GET /api/groups` | read | `[{"name", "printers": [...]}]` for every group in use. Groups also appear as `groups` in `GET /api/printers` and `GET /api/fleet` |
| `PUT /api/printers/{name}/groups` | control | `{"groups": ["Production", ...]}` replaces the printer's groups (`[]` clears). `PUT /api/printers/{name}` with `{"values": {"groups": [...]}}` does the same, and also accepts `groups` on `POST /api/printers` |
| `POST /api/groups/rename` | control | `{"from": "Old", "to": "New"}` - on every printer; `404` if no such group |
| `DELETE /api/groups/{name}` | control | Takes the label off every printer |

Group changes are fleet changes, so they need the same `control` level as editing a printer; reading groups
needs only `read`. MCP: `list_groups` (read) and `set_printer_groups(printer, groups)` (control);
`list_printers` shows each printer's groups. Groups are not part of Settings export / import (those carry
settings only, never the fleet).

## Camera (Bambu P1 / A1 and OctoPrint webcams)

Bambu P1 and A1 printers expose their chamber camera as a JPEG stream on TLS port 6000
(self-signed cert, same `bblp` + access code login as MQTT). The hub reads it with no extra
config — if a printer is a P1/A1 it simply has a camera. X1- and H2-series models (RTSP
cameras) report `"camera": false`.

**OctoPrint printers** work too, with no config: the hub reads the webcam from OctoPrint's
`/api/settings` (the `classicwebcam` plugin block, falling back to the core `webcam` block),
resolves relative URLs against the printer's base URL, and — when the snapshot URL says
`localhost` (meaning the OctoPrint machine itself, unreachable from the hub) — derives the
snapshot URL from the stream URL (`action=stream` becomes `action=snapshot`). A printer only
reports `"camera": true` once a test snapshot actually returned a JPEG; the result is cached and
re-checked every few minutes. One shared poller fetches a snapshot about every 1.5 s while anyone
is watching and stops ~30 s after the last viewer; the same `/snapshot`, `/stream` (MJPEG built
from the polled snapshots) and `get_camera_snapshot` work. OctoPrint's mirror/flip settings are
passed as `camera_orientation` (`flip_h`, `flip_v`, `rotate90`); the dashboard applies the two
flips (not `rotate90`).

| Where | What |
|---|---|
| `GET /api/printer/<name>/snapshot` | Latest frame as `image/jpeg` (`Cache-Control: no-store`). Waits up to ~8 s for the first frame after the camera wakes; `503` JSON error if none arrives; `404 {"error": "no camera"}` for printers without one |
| `GET /api/printer/<name>/stream` | MJPEG (`multipart/x-mixed-replace; boundary=frame`), one part per new frame; ends after 30 min or if the printer goes quiet |
| `GET /api/fleet`, `GET /api/printer/<name>` | each printer now carries `"camera": true/false` |
| MCP tool `get_camera_snapshot(printer)` | the current frame as an MCP image |

**One connection, many viewers.** Printer cameras tolerate very few simultaneous clients (and
other tools such as OctoEverywhere may be connected too), so the hub never opens one connection
per viewer. A single background reader per printer starts the first time anyone asks for a
frame, caches the newest JPEG, and serves every snapshot request, stream and MCP call from that
cache. Each request (or open stream) counts as demand; about 30 s after the last one the reader
closes the camera connection. Errors reconnect with exponential backoff (1 s up to 30 s) and
never affect the MQTT or FTPS sessions.

The dashboard shows a small camera thumbnail on each printer's fleet card (printers without a
camera show a "No camera" box) and a tile on the printer screen. Thumbnails refresh a still every
5 s (staggered across printers) **only** while their screen is showing, the browser tab is visible
and the display has been touched in the last 10 minutes; tap one for the live MJPEG view, tap again
(or **Close camera**) to leave it.

## Printer controls

Light, pre-heat, cool-down, unload and load, for every printer family that can do them. Ask a
printer what it supports with the `printer_controls` tool or `GET /api/printer/<name>/controls`;
unsupported controls say why.

| | Bambu | OctoPrint | Klipper (Moonraker) | PrusaLink |
|---|---|---|---|---|
| Light | yes (chamber light) | no (no standard API) | yes, if an `output_pin` / `led` / `neopixel` / `dotstar` named like *light*, *led* or *lamp* exists | no |
| Pre-heat / cool-down | yes | yes | yes | no (API has no temperature endpoint) |
| Unload / load | AMS printers only | Prusa firmware (M702 / M701) | only if the `printer.cfg` has `UNLOAD_FILAMENT` / `LOAD_FILAMENT` macros | no |

Commands written from each family's documentation and not yet tried on real hardware are flagged
`"verified": false` in the controls output (see [docs/drivers.md](drivers.md)).

**Safety.** Pre-heat, cool-down, unload and load are **refused while the printer is printing,
preparing or paused** (and when it is not connected); the light is always allowed. Temperatures
above 300 C (nozzle) or 120 C (bed) are rejected. By default a pre-heat never stays on: the Hub switches the
heaters off after the auto-off time (**Settings > Printers & safety**, or `PREHEAT_TIMEOUT_MIN`; default 15
minutes) unless a print starts or you cool down / re-heat first. Setting it to 0 in Settings turns the
auto-off off. The controls live on each printer's screen in the dashboard; the fleet cards never show them.

**Pre-heat heats the bed only.** The nozzle is not touched: the printer heats it itself for a filament change or a print.
The presets are one bed temperature per material, PLA 60, PETG 80, ABS 100 C by default. Change them on the Settings page
(**Printers & safety**, "Pre-heat bed"); the wall screens and the dashboard buttons all read those numbers. Extra named presets can still
be added in `config.yml` for agents (only their `bed` value is used; a `nozzle` there is ignored):

```yaml
preheat_presets:
  NYLON: {bed: 90}
```
An agent or script can still pass an explicit `nozzle` temperature to the `preheat` tool; no button does.

### Controls (a pop-up)

Every printer screen has a **Controls** button (next to Light on the phone, the 10-inch touch layout and the desktop; in the action
strip on the 5-inch wall screen; first under **More** while a print runs). It opens a pop-up card over the dimmed printer screen (a bottom
sheet on a phone). Left, the **Move** panel; right, **Nozzle**, **Bed** and **Filament**; the header has the printer, its state in words
(for example `IDLE - HOMED`), the Light and a round close button.

| Part | What it does |
|---|---|
| Move | the **jog ring**: tap a wedge to move that way (Y+ back on top, X+ right, Y- front, X- left), **Home** in the middle (asks once); the **1 mm / 10 mm** switch sets the step (10 by default); **Z - Bed** Up / Down; **Filament** Pull / Push 10 mm, only when the nozzle is hot enough (they say "Heat the nozzle to 170 first") |
| Nozzle | the temperature now, the target in words, a meter, PLA / PETG / ABS (210 / 235 / 245 C by default; Settings > Printers & safety) and **Off** |
| Bed | the same with the Pre-heat bed presets |
| Filament | **Change filament...** (guided, below), **Unload**, **Load** (each asks which material first, so the nozzle heats to the right temperature) |

If the Y movement feels the wrong way round on one printer, turn on **Flip Y on the jog pad** (Edit printer > More options): the top wedge
becomes Y- (front) and sends Y-; saved without a connection test, also while the printer is printing.

Anything the printer cannot do is shown disabled with the reason in words, never hidden. **Change filament** is its own pop-up with a step
bar (Material, Heat, Home, Unload, Swap, Load; a tick marks the finished ones, the current one has a thick ring and a bold word) and a
footer with Cancel, Back and the one next action: pick the material (the nozzle heats, the bed is left alone), heating with a live ring,
Home (only when the printer says it is not homed - otherwise the bar has five steps), Unload, "Swap the spool" with a three-item checklist
then Load, a Purge button, and "Done - cool down now?". Cancel switches off the heater the flow switched on; **Keep hot** leaves it to the
auto-off.

| | Klipper | OctoPrint | PrusaLink | Bambu |
|---|---|---|---|---|
| Load / unload | your `LOAD_FILAMENT` / `UNLOAD_FILAMENT` (or `M701` / `M702`) macro after `M109`, passing `TEMP=`, and `G28` first if Klipper says it is not homed; with no macro a plain G-code sequence (Edit printer > More options: unload / load length, default 100 / 90 mm) | `M109`, then Prusa `M701` / `M702`; **MMU printers** (name or model says MMU): not offered, "load/unload from the printer's screen" | disabled, with the reason | AMS (existing path); you pick the slot |
| Home / move / extrude | `G28`, relative `G91` jogs, `M83` + `G1 E` | the same over `/api/printer/command` | disabled | `gcode_line`, **beta** |

**Safety, enforced by the Hub (not only the page).** All of these are refused while the printer is printing, preparing, paused, has a
print start pending, or is not ready; a view-only sign-in cannot reach them. Moves are relative only, at most 10 mm per tap, one axis,
and refused unless the printer is homed (Klipper reports it; OctoPrint and Bambu cannot, so a jog needs a Home all from the Hub in the
last 10 minutes). Extrude is at most 10 mm per tap and refused below the printer's minimum extrusion temperature (Klipper's
`min_extrude_temp`, else 170 C). Home needs `confirm: true`. Nozzle 300 C / bed 120 C limits as before, and a heater a control switches
on gets the normal auto-off.

REST additions to `POST /api/printer/<name>/control`: `home` (`"confirm": true`), `move` (`"axis": "X|Y|Z", "mm": -10..10`),
`extrude` (`"mm"`), `heater_off` (`"heater": "nozzle|bed|both"`), and `"material": "PLA|PETG|ABS"` on `preheat` / `load` / `unload`.
The jog-pad flip is `jog_flip_y` (true / false) in `values` of `PUT /api/printers/<name>`; `GET /api/printers` and `GET /api/printer/<name>` report it.

REST: `POST /api/printer/<name>/control` with `{"action": "light_on|light_off|preheat|cooldown|unload|load",
"preset": "PETG"}` (or explicit `nozzle` / `bed`; `tray` for a Bambu load) answers 200, 400 (bad input),
409 `REFUSED` (busy), 422 `UNSUPPORTED` (with `reason`) or 502 (the printer failed).

## File backups

Copies every file on a printer's file list into a single zip on the Hub, so a dead SD card
or a wiped OctoPrint doesn't take your sliced jobs with it. The copy runs on a background
thread — it never blocks MQTT, the dashboard, or other requests.

- **What gets copied:** the files the Hub's own file list shows — the `.3mf` jobs on a
  Bambu SD card (over FTPS), plus the `.3mf` files in the card's **`cache/` folder** (jobs sent
  straight from the slicer land there; they are stored as `cache/<name>` in the zip and manifest).
  No other card folder is touched (`ipcam/`, `logger/`, `recorder/`, `timelapse/`, `image/`,
  hidden and system folders). For OctoPrint, the `.gcode`/`.bgcode` files in local storage (over
  HTTP, `/downloads/files/local/<path>`, folders kept). For PrusaLink and Klipper/Moonraker, the
  G-code files on the printer / Pi (streamed over HTTP, folders kept).
- **Where it lands:** `/data/backups/<printer>/<printer>-YYYYMMDD-HHMMSS.zip` (the `/data`
  volume, next to the metadata cache). Each zip holds the files plus a `manifest.json`:
  printer, model, created time, per-file name/size/modified/**sha256**, totals, and any file
  that failed with the reason. The zip is written as `.part` and renamed only when complete,
  so a half-written archive is never listed or downloadable.
- **Retention:** the newest `BACKUP_KEEP` zips per printer are kept (default `5`; set `BACKUP_KEEP` in a `.env` file next to the compose file
  — see `.env.example`). Older ones are deleted after each successful backup.
- **Busy printers:** a Bambu printer is **refused** while `RUNNING`, `PREPARE` or `PAUSE`
  (and while its state is unknown, since idle can't be confirmed) — it has one FTPS session
  and a long download mid-print is exactly what the metadata refresh also avoids. The state is
  re-checked before every file; if a print starts mid-backup the job stops and writes nothing.
  **PrusaLink** is refused the same way (it reads the printer's USB stick). OctoPrint and
  **Klipper/Moonraker** backups are allowed mid-print — they read the OctoPrint / Pi storage, not
  the printer. Any other driver is refused while busy unless it says otherwise.
- **One job per printer at a time**; a second start is refused with 409.

| Endpoint | Purpose |
|---|---|
| `POST /api/printer/{name}/backup` | Start a backup (`{name}` may be `all` — printers run one after another). `202` + job info; `409` already running; `423` printer busy; `404` unknown printer |
| `GET /api/backups` | `running` jobs (files done/total, bytes, current file), `recent` finished jobs (`done` / `partial` / `failed` + error), and `backups` per printer (file, size, created, file count, download path) |
| `GET /api/backups/{printer}/{filename}` | Download a finished zip (`application/zip`, attachment). Both path parts are validated strictly; anything that isn't one of our own backup files is a 404 |

**MCP tools:** `backup_printer_files(printer)` starts a backup (`"all"` allowed) and returns
the job; `backup_status()` returns running jobs, recent results, and every stored backup with
its download path.

The dashboard has a **Backups** button on the fleet screen (per-printer status, progress,
Download latest, Back up files, Back up all) and a **Back up files** button on each printer's
screen. Status is always spelled out in words, never color alone.

## How it works

Pure local protocols, no Bambu account, no cloud:
- **MQTT (TLS :8883)** — `bblp` + access code; topics `device/<serial>/report|request`.
  Print start is the `project_file` command with `"url": "ftp:///<file>.gcode.3mf"`
  (three slashes = SD root) and an `ams_mapping` array of tray IDs.
- **Camera (TLS :6000)** — P1/A1 chamber camera: an 80-byte auth packet, then a continuous
  stream of `[16-byte header | JPEG]` frames. See [Camera](#camera-p1--a1-series).
- **FTPS (implicit TLS :990)** — lists/downloads the SD card; `.3mf` metadata
  (time, weight, filament colors) parsed from `Metadata/slice_info.config`.

## Disclaimer

Not affiliated with or endorsed by Bambu Lab. Uses the printers' local network
interfaces, which Bambu may change in any firmware update. **A remotely started
print on an unattended printer with something on the build plate can damage your
machine — the confirm-gate exists for a reason; keep a human in the loop.**
MIT licensed; you assume all risk.

---
*Built in an afternoon of live protocol archaeology on a real P1S (fw 01.07,
LAN mode) and A1 (fw 01.08.01, cloud mode) — every recipe in this repo was
verified on actual hardware before it was written down.*
