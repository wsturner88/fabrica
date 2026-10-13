# Changelog

All notable changes to Fabrica are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

## [1.0.0] - 2026-10-12

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
