# Fabrica: start here

Fabrica manages a fleet of 3D printers (Bambu Lab, Prusa, OctoPrint, Klipper) and tells you about it through
MCP tools. Everything you need is in these guides; there is no other file to fetch.

## Rules (always)
1. A human confirms the build plate is clear before EVERY print start. Never decide that yourself.
2. Prints start only through the ticket flow: `suggest_mapping` -> show the human -> `start_print`.
3. Never pause, resume or cancel a print unless the human asked for it in this conversation.
4. File actions (upload, delete, rename, backup) happen only on idle printers, and deleting needs a clear yes.
5. Change the fleet (add, update, remove printers, groups, settings) only when the human asks.
6. Colours are always names ("Black"), never hex or hue alone.
7. A reply starting `REFUSED:` is a safety stop. Read it, tell the human, do not look for a way around it.

## What each tool family is for
- Look: `list_printers`, `printer_status`, `ams_state`, `list_sd_files`, `printer_controls`, `get_camera_snapshot`, `list_groups`.
- Print: `suggest_mapping`, `start_print`, `pause_print`, `resume_print`, `cancel_print`. See topic `printing`.
- Library (files kept on the Hub): `library_list`, `library_file`, `library_send`, `library_save_from_printer`. See `library`.
- History and cost: `recent_jobs`, `job_stats`, `cost_per_part`. See `history`.
- Renaming files on printers: `file_facts`, `rename_preview`, `rename_apply`. See `renaming`.
- Printer controls: `set_light`, `preheat`, `cooldown`, `load_filament`, `unload_filament`, `set_tray`.
- Files and backups: `delete_sd_file`, `backup_printer_files`, `backup_status`.
- Fleet and settings: `list_printer_types`, `add_printer`, `update_printer`, `remove_printer`, `set_printer_groups`, `get_settings`, `update_settings`.
- Failure watch: `detection_status` (alerts only; it never stops a print).

## Keys
A `read` key can look and plan but is refused control tools. A `control` key can act. Use the least that does the job.

## Topics
`fabrica_guide(topic)`: start, printing, library, history, renaming, changes.
Workflows to start by name (prompts): `print_a_file`, `fleet_status`, `print_history_report`.

## Staying current
Remember the kit version in the first line of every guide. If any Fabrica reply shows a different one, call
`fabrica_guide('changes', since=<the version you know>)` and re-read what it lists.
