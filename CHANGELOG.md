# Changelog

All notable changes to Fabrica are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
