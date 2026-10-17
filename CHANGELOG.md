# Changelog

All notable changes to Fabrica are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Printer deep link: `/dashboard?printer=<name>`** opens straight to that printer's screen (the name is not case sensitive). It combines with
  `group=`, `layout=` and `kiosk=`; an unknown name leaves the fleet on screen with a short "not found" notice. The "Print finished" message's
  "Open in Fabrica" link now uses it (`<Hub address>/dashboard?printer=<name>`), so a tap lands on the printer that finished.
- **The 10-inch kiosk, the desktop page and the phone layout got the 5-inch screen's features.** Every fleet card says `Spaghetti: WATCHING / READY /
  OFF / NO CAMERA / DETECTOR DOWN`, and a `WARNING` or `FAILURE` is an inverted block with a warning sign, a heavy frame on the card and a banner above
  the grid (words and shape, never colour alone; the 5-inch card could not show it before: it never read the detector's verdict). Each card shows
  the same per-state words (Paused with the time left, Ready to print / Last job, Tap to connect, No answer from the printer) and a picture slot
  that says NO CAMERA or CAMERA OFFLINE. The printer screen has the big percent, time left, `Done ~clock`, layer, a bar (also while paused), the nozzle
  and bed tiles, the per-printer Spaghetti switch (ON / OFF in words), the live camera (the snapshot is the fallback) with LIVE / SNAPSHOT badges
  and the Spaghetti word, a status panel in place of a missing camera (with "Add camera" off the kiosk), and AMS slots as 48 px buttons. The files list has
  sort buttons (Date / Name / Time) in the toolbar row, the date modified, and a "Printing" tag on the file that is running.
- **History on the 5-inch screen** is rebuilt for 800x480: one toolbar row (period, printers, Filament only / + Print time, More, Close), four compact
  tiles in one row, tabs (Recent prints | By printer | By file) and a list of jobs that scrolls inside its own box (date, printer, file, result, grams,
  cost; tap a job for its details). The page no longer scrolls; the first job row starts at y=212 instead of 368.

### Changed (History, every layout)

- The "By file" table shows the file name as it was written (`display`, new in `/api/stats` `by_file`; `file` stays the lower-case matching key).
  The cost tile says "filament only" once, the tiles are tighter (the tables start about 70 px higher at 1024x600), and the "Print-time costs: switch
  on in Settings" link moved into **More**.
- Library "save from printer" keeps a PrusaLink file under its long name instead of failing on `NAME~1.GCO`.

- **"First layer done" alert** with a camera picture ("Check the picture: is it sticking?"), once per print. Detected from the layer count (Bambu, Klipper when the slicer reports layers), else from nozzle height held up for 20 s (Klipper, PrusaLink; z-hops ignored), else a timer for OctoPrint (`notifications.first_layer_fallback_min`, default 8, 0 = off). Never sent for a print already under way when the Hub starts. Toggle: `notifications.first_layer_done`.
- **Camera address for any printer.** Every printer type has an optional "Camera address": an `rtsp://` / `rtsps://` stream
  (read with ffmpeg, one shared process per printer, only while someone is watching) or an `http(s)://` JPEG picture URL. It replaces
  the printer's own camera (e.g. a Prusa Buddy3D camera `rtsp://<ip>/live` on a Core One). A `user:password@` in the address is kept
  private (shown as `rtsp://***@host/...`, never logged). Test connection grabs one frame. The Docker image installs ffmpeg by default
  (`--build-arg WITH_FFMPEG=0` for a lite image; RTSP cameras then report "need the full image").

- **File detail sheet.** Tap a file in the list (any printer, any layout) for a big picture, the full long name, print time, filament weight and
  material(s), layer height and nozzle where the file says them, size, date, storage, the estimated cost (filament only, or with power + wear)
  and when the file was last printed, with Print / Delete / Rename / Back. The details load when the sheet opens.
- **Material badge** on every file row, just before Print, as words (`PETG`, `PLA`, `PLA PETG`, ...): Bambu from the cached `.3mf`
  metadata, PrusaLink from the slicer's file name (and the file head of a `.bgcode` when the printer is idle), Klipper from its
  file metadata, OctoPrint from the end of the `.gcode` (one small ranged read). Unknown = no badge.
- **PrusaLink (Core One class) files**: long names, sizes, print time, material and pictures; the list now includes the `.bgcode`
  files (a printer shows them as `NAME~1.BGC`, which the old filter dropped); Delete works with the same two-tap flow.
  Pictures are read from the printer's own thumbnail service on first use and cached. Every file without a picture says NO PICTURE.

### Changed

- **Pre-heat heats the bed only, with one set of numbers.** The quick Pre-heat buttons (5-inch wall screen, 10-inch kiosk, dashboard) and
  the preset names of the `preheat` tool send a bed temperature and no nozzle temperature (the printer heats the nozzle itself for a
  filament change or a print). The temperatures are three Settings (Printers & safety, "Pre-heat bed"): PLA 60, PETG 80, ABS 100 C, and
  every screen reads them. The old nozzle/bed preset list is gone (an agent can still pass an explicit `nozzle`). Buttons say it in words
  ("PLA bed 60°"); the auto-off still switches the heaters off.

- **Costs are filament only by default.** The History "Cost" tile, per-job costs, true cost per part, wasted-on-failures, the CSV
  `total_cost`, the REST API (`/api/stats`, `/api/jobs`, `/api/jobs/{id}`, `/api/costs/file`) and the MCP tools (`recent_jobs`,
  `job_stats`, `cost_per_part`) now report the FILAMENT cost; power and machine wear (which came from rates most people never set)
  are opt-in via **Settings → Costs → Include print time costs** (`costs.include_machine_costs`, off by default). `material`,
  `electricity` and `wear` are still in every response, plus `total_with_machine` and `includes_machine_costs`; `?machine=1` /
  `?machine=0` (MCP: `include_machine_costs`) override the setting per request. The History page puts the period, the switch and More (CSV) in one toolbar row, and says "Filament only" or lists
  Filament / Power / Wear in words, and has a **Filament only / + Print time** switch for that view (not saved).
- **5-inch wall screen (`/dashboard?layout=5in`) redesigned.** Same-size cards for every printer (name + state, picture or a "NO CAMERA /
  CAMERA OFFLINE" panel, big number or word + ETA + file, bar, temperatures, `Spaghetti: WATCHING / LEARNING / READY / OFF / NO CAMERA`).
  The printer screen shows the live camera (the same MJPEG stream as the enlarged view; snapshots if it fails), a control row (Left / Layer /
  Done while printing, the AMS slots on an AMS printer, PLA / PETG / ABS pre-heat when idle), a per-printer Spaghetti on/off switch (adds /
  removes the printer from Settings > Detection > "Do not watch these printers"), a status panel instead of the picture when there is no
  camera, and a 56 px action strip with Files as the primary button. A second look, `?layout=5in&theme=bridge`, re-skins the same screens
  (remembered per screen in the browser; `theme=quiet` switches back). The fonts (Inter, JetBrains Mono, Antonio; SIL OFL) ship with the Hub.
  The 1024x600, desktop and phone layouts are unchanged.
- `/api/printer/<name>` `detection` now also says `learn` (learn-only mode) and `alerts`; `/api/detection` `setup` lists `excluded_printers`.
  A printer excluded from detection reads "Off for this printer" before the detector has looked at it for the first time.

- **PrusaLink setup has one secret: Password.** An API key pasted there works too (the Hub tries digest login first, then the key).
  Username, storage, model and camera address are "advanced" fields. Saved printers are migrated (old `snapshot_url` becomes the
  camera address, an old API key becomes the password). Address fields say that `http://` is added for you.

## [1.0.0] - 2026-10-13

### Fixed

- **Two-filament prints: you can now choose the spools.** The start-print confirm used to offer the spool grid only for single-filament
  jobs; a job with a PETG body and a PLA interface layer just showed text, with no way to pick the PETG slot you wanted. It now shows one
  row per filament ("Filament 1 - PETG - file colour GREEN - 38 g") with every loaded slot as a tap target and the suggested one starred.
  Tapping a slot another filament holds swaps the two; a different material (file wants PETG, slot holds PLA) is said in words and allowed.
  The ticket and bed-clear tick are unchanged. `suggest_mapping` / `/suggest` gain `filaments` (type, colour word, grams).

### Added

- **Remembered spools per file.** When a print starts, the Hub notes which spool (slot, material, colour) fed each filament, in
  `spool_memory.json` in the data folder (owner-only, last 500 files). The next proposal for the same file on the same printer starts from
  those spools, even if they moved to another slot, and says so ("Same spools as your last print of this file (Oct 13)"); if one is no
  longer loaded the normal proposal is used. Agents see the same note. It only changes the proposal: nothing starts without the human.

- **Filter the main page by printer group.** `/dashboard?group=Work` shows only that group's printers (several `group=` = union, case-insensitive;
  an unknown or deleted group shows everyone plus a "not found" notice). A chip row "All / Work / Home" (only when groups exist) switches it and
  rewrites the URL, so a reload, bookmark or wall kiosk keeps the choice; the active chip is inverted + bold + underlined + ticked, never colour alone.
  Client side only (`/api/fleet` already carries each printer's groups).

- **5-inch wall-screen layout.** `/dashboard?layout=5in` (combine with `group=`, e.g. `?group=Home&layout=5in`) is a compact Home screen for an
  800x480 touchscreen (a Raspberry Pi 5-inch panel): a 48 px header with the pages behind **More**, one row of group chips, and the printers as
  tiles (1-2 = one big row, 3-4 = 2x2, 5-6 = 3 across, more scroll inside the grid) with name, a state word, job, progress bar + %, "Done ~5:08 PM"
  and temperatures on one line, no camera on the tiles, no page scroll; text is 14 px or more and every target 44 px or more. It is opt-in only (never
  detected from the screen size, so landscape phones keep their layout), is read once at load, and survives the group chips. The "HUB NOT RESPONDING"
  banner stays on screen. Every other layout is unchanged.

- **Name: Fabrica** (title "Fabrica — Print Farm"). README, About screen and the Home Screen app name; repository, image and container stay `fabrica`.

### Changed

- **New installs show your own time.** Until a time zone / date style is chosen in Settings, the dashboard uses the browser's time zone
  and 12/24-hour habit instead of UTC / 24 h. The Hub itself (quiet hours, notification times, CSV dates) uses `TZ`, else the machine's own
  zone, else UTC, and Settings > General offers a one-time, dismissible **Use <your zone>** note when they differ.
- **Phone header.** On phones the second row is Printers, Library and a **More** menu (History, Backups, Settings, Sign out). The wall
  screen and desktop are unchanged.
### Security

- **Send to printer never overwrites.** File names are compared ignoring upper/lower case on every printer type (SD cards and USB sticks
  ignore case); OctoPrint / PrusaLink / Klipper sends need a live file list and are refused if it cannot be read. "Save from printer"
  now stops reading at the size limit while downloading (HTTP and Bambu). Archives with thousands of members and oversized
  `slice_info.config` members are refused. Library renames check the previewed size. OctoPrint deletes re-check that the printer is idle.
  View-only devices no longer see Download buttons. First-run password: open the dashboard right after starting it.

### Added

- **Agent-facing file renaming.** The Hub provides the mechanics, not a naming convention: `file_facts` (parts on the plate, sliced-for
  model, time, grams, colour words, where else the file is, how often it was printed), `rename_preview` (before -> after table, per-row
  problems, single-use 10-minute ticket) and `rename_apply` (needs the ticket and the human's yes, `approved_by_human=true`), plus the
  MCP prompt `rename_files`. On a printer: download, keep a copy of the original in the Library (`Renamed originals`), upload under the
  new name, verify, and only then delete the old name; busy printers are refused, nothing is overwritten. Library files are renamed
  atomically. Job history, costs and the History "By file" table treat a renamed file as one file. Dashboard: a **Rename...** button
  in the Library and on each printer file row. REST `GET /api/files/facts`, `POST /api/rename/preview`, `POST /api/rename/apply`.
  See [docs/renaming.md](docs/renaming.md).
- **Upload and download for OctoPrint, PrusaLink and Klipper (beta - not yet verified on real hardware).** Needed by renaming; also lets
  the Library's **Send to...** put `.gcode` / `.bgcode` files on these printers. Idle printers only, never starts a print.
- **Agent kit.** An agent needs only the MCP address: server instructions with the safety rules arrive on connect,
  `fabrica_guide` returns short guides (start, printing, library, history, renaming, changes), and three workflows
  (`print_a_file`, `fleet_status`, `print_history_report`) are available as MCP prompts. A kit version (`k-xxxxxxxx`)
  changes by itself whenever a guide, tool or prompt changes; `list_printers` shows it, and
  `fabrica_guide('changes', since=...)` says what is new. `GET /api/agent-kit`; shown in Settings > Agents / MCP.
- **Printer groups.** Label printers (`Production`, `Workshop`, ...), several per printer. History, costs, stats and
  the CSV export filter by a group or any hand-picked set of printers and sum them, with a **By group** table
  (a printer in two groups counts in both). Groups are edited on the Printers page; REST `GET /api/groups`,
  `PUT /api/printers/{name}/groups`, `POST /api/groups/rename`, `DELETE /api/groups/{name}`; MCP `list_groups`,
  `set_printer_groups`, and `group` / `printers` filters on `job_stats`, `recent_jobs`, `cost_per_part`.

First public release.

### Added

- **Multi-family dashboard.** One Hub shows your whole fleet: **Bambu Lab** (LAN mode), **Prusa**
  (PrusaLink and OctoPrint), **OctoPrint** and **Klipper** (Moonraker). Local protocols only, no cloud
  account. Made for phones and a 10" 1024x600 wall touchscreen.
- **Cameras and finish times.** Live camera thumbnails on the fleet screen, a per-printer camera view, and
  an estimated finish time on every running print.
- **Start from the SD card.** Start a print from the printer's own files, with AMS spool mapping, a
  wrong-printer guard (the file must match the printer it was sliced for) and a "bed is clear" confirmation.
- **Spaghetti detection (Full edition).** Alert-only failure detection from the chamber camera, using an
  unmodified, separately licensed Obico model container behind Fabrica's own adapter. It never pauses or
  stops a print. See [THIRD_PARTY.md](THIRD_PARTY.md).
- **Alerts** through Telegram, Discord, ntfy, e-mail or a generic webhook: print finished, failed, paused,
  printer offline and more.
- **History and true cost per part.** Every print attempt is logged locally, with totals per period,
  printer and file, and a CSV download. An optional cost model covers material, energy and wear, including
  a failure-adjusted true cost per part.
- **Backups.** Per-printer file backups to zip archives, with a configurable number of copies kept.
- **Discovery and add-printer wizard.** Scan your networks for printers, then add them with step-by-step
  setup guides and a plain-words "test connection".
- **Printer controls** where the printer supports them: light, pre-heat (with a mandatory auto-off),
  cool-down, filament load and unload, pause, resume, cancel, and verified spool changes.
- **Settings import and export** for the whole Hub.
- **Security.** API keys (`read` or `control` scope) for the MCP endpoint and REST API, optional dashboard
  sign-in with a view-only password, sessions and trusted devices, and a first-run password prompt on new
  installs.
- **MCP server with about 40 tools** over streamable HTTP, so AI agents can monitor and, with a
  `control` key, operate the fleet.
- **Two editions:** **Lite** (Hub only, runs on a Raspberry Pi 4 or any Docker machine) and **Full**
  (Hub plus the spaghetti detector). Docker images for amd64 and arm64.

[Unreleased]: https://github.com/wsturner88/fabrica/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/wsturner88/fabrica/releases/tag/v1.0.0
