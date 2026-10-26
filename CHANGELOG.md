# Changelog

All notable changes to Fabrica are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A "Controls" button that is always on the printer screen.** The one button in the top toolbar row, idle **and** while printing, on the desktop, the 10-inch
  touch / kiosk layout, the phone and the 5-inch screen (quiet theme: in the header right of the title; Bridge theme: a block in the left rail), opens the
  Controls pop-up. While a print runs the pop-up opens with its parts locked and one line in words, "Locked while printing" (the Hub refuses those actions
  as before; the light still works). Controls is also still the first entry under More. To keep the toolbar ONE row (also with the wide fallback font of
  Linux machines) the four sort buttons and the Grid / List pair became ONE "Sort: Recent" button on the 10-inch layout and in narrow windows and on the phone;
  its chooser holds the sort choices and, below them, Grid / List. The phone rows are now: title, Pause / Cancel (printing), Controls + Light, Sort + Refresh + More.
- **Slimmer fleet cards.** The "Nozzle 220 / Bed 58" line and the "Spaghetti: WATCHING" line are gone (about 60-75 px shorter). A small Spaghetti badge sits
  at the right end of the "Done ~6:59 PM" row: a shape and a word, never colour alone - eye + OK (watching), eye + READY / LEARN, eye with a slash + OFF,
  camera with a slash + NO CAM, triangle + WARN / FAIL (bold frame), triangle + DOWN (detector down); its tooltip keeps the full words ("Spaghetti: WATCHING").
  The camera's "Updated 9:19:03 AM" is now a small "9:19 AM" overlay on the picture's corner (the seconds are in the tooltip). The info line keeps
  "7% - 9h 40m left - Layer 13 / 1150"; a printer that reports no layers shows the job's material instead ("19% - 4h 57m left - PETG") when its file says one.
  A Spaghetti WARNING / FAILURE card keeps its big inverted block, heavy frame, the banner and the Pause / Cancel row. The Hub's `/api/fleet` and
  `/api/printer/<name>` snapshots gained `job_materials` (only while a print runs, read from caches - never a request to a printer).

- **Controls and Change filament are now pop-up cards.** Controls opens as a centred card over the dimmed printer screen (a bottom sheet with a grab
  handle on a phone, a near-full-screen card on the 5-inch screen, both themes): a **Move** panel (a round **jog ring** - Y+ back, X+ right, Y- front,
  X- left around **Home** - a **1 mm | 10 mm** switch, a **Z** column and a **Filament** Pull / Push column that says "Heat the nozzle to 170 first"),
  **Nozzle** and **Bed** cards (the big temperature, the target in words - "-> 235 PETG" or "heater off" - a meter, and PLA / PETG / ABS / Off choices;
  the chosen one has a tick and a frame) and a **Filament** card (Change filament..., Unload, Load). **Change filament** is a pop-up with a step bar
  (Material, Heat, Home, Unload, Swap, Load: done = a tick, the current one has a thick ring and a bold word), a picture beside the words (a heating
  ring with "182 of 235 PETG" and Material / Target / Left chips; the spool and a three-item checklist for the swap), and a footer with Cancel, Back
  and the one next action. Home is left out of the bar when the printer is already homed ("Step 3 of 5"). New per-printer setting **Flip Y on the
  jog pad** (Edit printer > More options, off by default): on, the top wedge is Y- (front). It is a display setting - saved without a connection
  test, also while printing. Every safety rule and every Hub-side limit is unchanged.
- **A Controls sheet on every printer screen, and a guided "Change filament...".** A **Controls** button (next to Light on the phone, the 10-inch
  touch layout and the desktop; in the action strip on the 5-inch screen with either theme; first under More while a print runs) opens one sheet with
  one compact row per section: **Filament** (Change filament..., Load, Unload), **Nozzle** (PLA 210 / PETG 235 / ABS 245 C from the new
  Settings > Printers & safety numbers `printers.filament_temp_*`, and Off), **Bed** (the Pre-heat bed presets and Off), **Move** (Home all, which asks
  once, and X / Y / Z -10 -1 +1 +10 mm) and **Extrude** (+10 / -10 mm, only when the nozzle is hot; the buttons say "Heat the nozzle first"), with
  current and target temperatures in words and the Light in the header. Anything a printer cannot do is shown disabled with the reason in words. The
  guided change has six steps, each with Back and Cancel (Cancel switches off the heater it switched on): material, heating ("Heating 142 -> 235 C"),
  home (only when the printer says it is not homed), unload, "Swap the spool, then tap Load", load + purge, "Done - cool down now?". Klipper: the
  printer's own `LOAD_FILAMENT` / `UNLOAD_FILAMENT` (or `M701` / `M702`) macro now runs after the Hub heated the nozzle (`M109`, `TEMP=` passed) and
  after `G28` if Klipper says it is not homed - the fix for "Must home axis first" and for macros that heat to their own 205 C; with no macro a plain
  G-code sequence with per-printer lengths (Edit printer > More options, default 100 / 90 mm). OctoPrint: `M109` then `M701` / `M702`; an MMU printer is
  never loaded or unloaded by the Hub ("load/unload from the printer's screen"). PrusaLink shows everything disabled with the reason; Bambu uses the AMS
  and `gcode_line` for move / extrude, marked beta. **Safety is enforced on the Hub:** every new action is refused while printing / paused / starting;
  moves are relative, one axis, at most 10 mm and refused unless homed (printers that cannot report it need a Home all from the Hub in the last 10
  minutes); extrude is at most 10 mm and refused below the printer's minimum extrude temperature (Klipper's `min_extrude_temp`, else 170 C); Home
  needs `confirm`; a view-only sign-in never sees or reaches any of it; heaters switched on get the normal auto-off. New REST actions `home`, `move`,
  `extrude`, `heater_off` and `material` on `POST /api/printer/<name>/control`.
- **A Spaghetti WARNING now has Pause and Cancel print right in it.** On the printer screen (phone, 10-inch / kiosk, desktop and the 5-inch board
  with either theme) the WARNING block grows one row holding two big buttons, "Pause" and "Cancel print": the same Pause (one tap) and the same
  Cancel (the one "Keep printing / CANCEL PRINT" confirm) as the toolbar. Pause shows only while the print is running, Cancel while a print exists;
  nothing is shown for an idle printer or to a view-only sign-in, and nothing is ever paused or cancelled automatically. The alert message
  (Telegram, ntfy, e-mail...) now ends with "Open in Fabrica: <link>" like the finished message; the link opens that printer, and on the phone the
  WARNING block sits right under the toolbar. On the fleet page the warning banner and the word on a warned card open that printer; the card shows
  a compact "Pause / Cancel" row in place of its Nozzle / Bed line (5-inch: in place of the file and layer lines). On the phone a slim Pause /
  Cancel bar sticks to the top while a print runs and the toolbar row is scrolled out of sight. The 5-inch strip's Cancel gets a heavy frame.
- **What is on a plate: "4× Hose Clip + 2× Hook" from the slicer's object labels, and a warning when there are none.** The Hub reads the
  cancel-object labels a slicer writes - Bambu Studio / OrcaSlicer `slice_info.config` (per plate), Klipper `EXCLUDE_OBJECT_DEFINE`, Marlin /
  Prusa `M486`, PrusaSlicer's `; objects_info` line and `; printing object` comments, and the same key in a Prusa `.bgcode` - and groups the
  objects by name (extensions, copy suffixes like ` id:0 copy 1` / `_id_0_copy_1` / ` (Instance 2)` and `_1`, `_2` copy numbers are dropped; an
  object built from several parts counts as one). Each file is `present`, `missing` or `unknown`; **`missing` is said only when it is certain**
  (the whole file, or the whole section where that slicer writes labels, was read and has none), and `unknown` shows nothing. The file list /
  grid / detail sheet show `Plate: 4× Hose Clip + 2× Hook`; a file sliced without labels gets a framed `⚠️ No labels` (words, inline, no new
  row) and the sheet says what to turn on; the start-print dialog shows one line or the warning and never blocks a print. Agents get
  `plate_contents(printer, filename)` (read key, cache only), `GET /api/files/plate`, and `plate_contents` in `file_facts` /
  `suggest_mapping` / the file lists; the agent guide says to put the quantity in a name only when labels are present and never to guess
  when they are missing or unknown. Nothing new is fetched from a printer: it is worked out from the Bambu file the Hub downloads anyway,
  the head / tail of file-host G-code it already reads, and the library's own files. Entries cached before this version stay `unknown`
  until read again (`FABRICA_PLATE_BACKFILL=1` lets the background pass do that once). See docs/renaming.md.
- **A Klipper printer without `[exclude_object]` says so.** One read-only `GET /printer/objects/list` per connect (kept; a Klipper restart
  forgets it; "not set up" is looked at again every 30 minutes). `printer_status` / `list_printers` carry `exclude_object` and the words
  "Exclude objects not set up in printer.cfg", and the idle printer's card and screen show the same line (no row is added; a narrow card
  uses a shorter wording and the whole sentence as its tooltip). Only Klipper / Moonraker printers.
- **An Analytics page: how the fleet is doing over time.** Next to History in the nav (More > Analytics on a phone and the 5-inch screen):
  pick 7 days / 30 days / 90 days / 1 year / All time and, if you use printer groups, one group, in one toolbar row (CSV is under More).
  Six tiles (prints, success rate, hours printing, filament used, filament cost, cost per good part), a prints-per-day / week / month bar chart
  (finished = solid bars, failed or cancelled = hatched bars, numbers in words under it), a per-printer table (prints, success, hours,
  utilization, failures, filament, % online), per-material figures, the failures by outcome, reason and file, and the most printed files with
  the good parts they made (a file without a part count says "parts unknown"). The 5-inch screen shows the same behind five tabs, and nothing
  on it scrolls but the panel. The figures are the History figures (same window, same cost rules), summed in SQL by the new
  `GET /api/analytics?period=30d&group=Work` (read scope). **Uptime is new data:** the Hub now notes when a printer becomes unreachable and
  reachable again (`uptime.jsonl` in the data folder: one line per change that lasts 45 s, a 256 KB cap, plus one small heartbeat file rewritten every
  10 minutes so the Hub's own downtime is not counted), and the page says "collecting since <date>" until that history covers the period.
  `tools/seed_demo_jobs.py` fills a throw-away folder with generic demo history for screenshots.
- **A real desktop layout, and the dashboard now picks its layout for the screen.** In a big window with a mouse the dashboard uses the
  whole window instead of the 1024x600 wall-screen box: more cards per row on the fleet page, a large camera beside the progress and
  controls on the printer screen with the picture grid across the full width, History with Recent prints / By printer / By file side by
  side, readable widths on Settings, Library and Printers, hover states and tooltips on cut-off names. An iPad (or other touch tablet)
  gets the 10-inch touch layout scaled to fill its screen. More > Switch view overrides the choice per browser (Desktop / 10-inch touch /
  Phone / 5-inch wall / Automatic); `?view=` in the address wins over detection. Wall screens (`?kiosk=1`, `?layout=5in`) are unchanged.

### Fixed

- **A material was guessed from the temperature.** In the Controls pop-up an MK4 printing PETG at 245 C showed "-> 245 ABS" because the ABS preset is 245 C.
  The target line now names a material only from the running job's own file (`job_materials`, or the file's row on the printer screen); otherwise it says just
  "-> 245". A preset chip may still carry a tick when its temperature matches, but the words never claim a material the file did not name.
- **A huge tooltip on the desktop view.** Hovering the History page (and Analytics) could pop up a native tooltip holding the whole list's text: the "full name when
  cut short" helper treated any scrolling pane as cut-short text. Now only real truncated text (an ellipsis or a line clamp, overflow hidden, a text leaf,
  at most 160 characters) gets a tooltip; a scroll container, a row or a list never does.
- **A multi-plate project's per-plate exports (OrcaSlicer: one `.gcode.3mf` per plate) are treated as the plate they hold, not plate 1.** The
  plate-4 file has `Metadata/plate_4.gcode` and `plate_4.png`; the Hub used to read `plate_1.png` (two different plates showed the same
  picture), count layers and parts from plate 1, and - safety-critical - tell the printer to print `Metadata/plate_1.gcode`, which is not in
  that file. Now: the picture, layers, part count and the plate sent to the printer all come from the file's real plate (taken from the cached
  details of that exact file, never a second download; plate 1 stays the last resort only when the Hub holds no details at all). Cached entries
  carry a `meta_v` marker; older ones are read again once by the normal idle-printer backfill (library files: re-read from the Hub's own disk at
  start-up), so wrong pictures fix themselves - no cache wipe needed.

- **The printer screen's toolbar stays on one row while printing, even in a wider font.** On the 10-inch layout a print under way now shows ONE
  "Sort: Recent ▾" button (it opens a chooser in words, with a tick on the active choice) instead of the four sort buttons, so Pause / Resume
  and Cancel never wrap onto a second row on a Linux screen whose fallback font is wider than the Mac's. An idle printer, the desktop view and
  the phone keep the four buttons.
- **Spaghetti detection no longer sees an upside-down camera upside down.** The detector got the raw camera picture while the dashboard
  turned it with CSS, so a printer whose webcam is set to flip (a Klipper printer with `flip_vertical`) was scored upside down and its
  scores drifted to 0.2 - 0.9 on a healthy print (a false alarm). Every picture is now turned the way the dashboard shows it (rotate, then
  flips, for every driver that reports `camera_orientation`) before it is scored. It uses the ffmpeg that is already in the full image; a
  Lite image sends the picture as it is, logs one note and says so on the printer screen. A picture that cannot be prepared is skipped,
  never scored raw. See docs/detection.md section 5a.
- **A baseline is only valid for the view it was learned from.** `detection/<printer>.json` now records the flips / rotation / watch area
  of its baseline. When they change the baseline is reset and the printer screen says "LEARNING - camera view changed" until it has
  learned again. An old baseline file counts as "the untouched picture", so an upside-down camera relearns once after this update.
- **The start-print confirm dialog (the spool picker) fits the screen.** It used to run off the bottom on the 10-inch kiosk, the 5-inch wall
  screen and phones: a tall header, a full-width line each for the printer family, the file, the notes and the picture, four-line spool buttons,
  and a bed-clear tick that scrolled away from START. Now the dialog is a header, one scrolling middle and a footer that is always on screen
  (bed-clear tick + Back + START on one row; a phone wraps the tick onto its own row). On the kiosk, the wall screen and a desktop window the live
  picture and the file name sit in a left column beside the spool rows, so nothing scrolls; a phone stacks them and scrolls only the middle. The
  file name is one line (tap for all of it), the notes are short lines under the spool rows, and a chosen spool now carries a tick as well as the
  border (single-filament jobs too). Nothing was dropped and the start request is the same. Measured by `tests/test_confirm_compact.py`.
- **Discovery on a routed network.** "Discover printers" on a network reached through a VPN subnet route (about 40 ms away) used to
  find nothing in a few seconds, while a /29 or a single address worked: 64 parallel connects with a 0.6 s timeout overloaded the route,
  dropped SYNs read as "nobody is home", and the "two silent ports = skip" shortcut turned them into empty addresses. Now the scan
  measures how far each network is. A far network gets a small pool that ramps up (at most 16), a 1.5 s connect timeout and a canary that is
  checked while the scan runs: if the route starts dropping, new hosts are held back, the pool is halved and the dropped hosts are looked at
  again. A host is empty only when ports 80, 7125 and 8883 all stayed silent (a Pi that only opens 80 is no longer skipped), and quiet hosts
  get a second look at low concurrency for known printers, for hosts probed during a drop, and when a far network found nothing at all. A
  local network is as fast as before. A far /24 takes about a minute and the screen says so ("Scanning a remote network - about a minute
  left"), with a live count, a "Double-checking N quiet addresses" step and a note when the 240 s limit or the second-look time box stopped
  it. New in the scan status: `phase`, `remote`, `profiles`, `eta_s`, `slowed`, `recheck`. See docs/discovery.md.

### Added

- **Spaghetti watch area, per printer.** A camera that also sees the room can be told where the printer is: Edit printer > More options >
  *Spaghetti watch area* - drag a box over the upright picture (or type Left / Top / Width / Height), *Reset* = the whole picture. Words and a
  dashed frame instead of colour, 44 px targets, one compact row, desktop and phone (not on kiosks). Stored as `detect_area` next to the
  printer's other settings; changing it needs no connection test and works mid-print. `PUT /api/printers/{name}/detection-area`,
  `detect_area` in `PUT /api/printers/{name}`, MCP `set_detection_area(printer, area)`; `GET /api/printers` shows `detect_area` and
  `camera_orientation`. The detector is shown only that part of the picture.
- **A visible Refresh button on the printer's files list, on every layout.** `↻ Refresh` (a word label, 44 px) sits in the existing one toolbar row (the 5-inch
  files sheet has it in its header); it says "Refreshing…" while it runs, then updates the list in place, keeping the chosen sort and newest first. The
  slow "Refresh details" entry is gone from More. Opening the files shows the last list at once and looks again in the background when it is older than
  30 s; an open, idle files screen re-checks every 60 s (not for Bambu, not while a job runs). The automatic looks (`?refresh=1&bg=1`) follow the prewarm's
  politeness rules: the Hub answers `{"skipped": true}` while the printer is heating, was just controlled or is unreachable. The button always refreshes.

### Fixed

- **"Connect" can no longer restart a healthy or printing Klipper printer.** It used to send a blind FIRMWARE_RESTART, so a few seconds of network trouble
  followed by a tap on the NOT CONNECTED chip could abort a print. It now looks first: unreachable -> nothing sent ("can't reach the printer, nothing sent",
  HTTP 503); printing or paused -> refused, always; Klipper ready -> nothing sent ("already connected"). Only when Klipper itself reports
  shutdown / error / disconnected is a restart possible, and only after a second, explicit tap: "This restarts Klipper (FIRMWARE_RESTART). Only do this
  if the printer shows an error." (agents: `connect_printer(confirm_restart=true)` after a human OK). OctoPrint's connect now reads its connection state first
  and never reopens a port that is already open or printing.
- **A network blip no longer flips a printer to OFFLINE / NOT CONNECTED.** The fleet and printer screens keep the last known state with the word
  "Reconnecting…" until the printer has been unreachable for 30 s (and missed 2 polls). Connect is only offered when the printer itself reports
  disconnected, never for an unreachable one. Safety paths (pre-heat auto-off, controls, the offline notice) still see the raw state.
- **The job list answers 503 in words, not a 500 crash,** when the printer cannot be reached over the network (502 when it answers with a refusal).
- **The background file prewarm is polite:** it skips and stops while any heater target is above 0 (printer not printing), for 2 minutes after a control
  action (pre-heat, cool-down, light, filament) and while the printer is unreachable, for every printer type.

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
