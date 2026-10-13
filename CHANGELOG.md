# Changelog

All notable changes to Fabrica are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
