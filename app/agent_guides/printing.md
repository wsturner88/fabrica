# Printing

Starting a print always follows this order. Skipping a step makes `start_print` refuse.

1. Pick the printer: `list_printers` (state must be idle) and the file: `list_sd_files(printer)` or `library_list`.
   A file that is only in the library must first be uploaded with `library_send` (see topic `library`).
2. `suggest_mapping(printer, filename)` plans the job. It returns `ams_mapping`, `mapping_described`
   (spools in colour words), `print_time`, `weight_g`, `notes`, `blocked`, `model_match` and a `confirm_string`.
3. Tell the human the plan in plain words: file, printer, which spool for which part, time, weight, any notes.
4. Ask: "Is the build plate clear?" and "Shall I print this?" Wait for a clear yes to both.
5. `start_print(printer, filename, ams_mapping, confirm, bed_clear_confirmed_by_human=True)`.
   Use the `ams_mapping` and `confirm_string` exactly as `suggest_mapping` returned them.

## Good to know
- `confirm_string` is single use, valid 5 minutes, tied to that printer, file and mapping. A used, expired or
  changed one is refused: call `suggest_mapping` again (and ask the human again if the plan changed).
- `bed_clear_confirmed_by_human` is the human's statement. Set it only after they said so in this conversation,
  never by default and never because a previous print finished.
- `blocked` is not empty (no free spool of the needed material or colour): `confirm_string` is empty. Tell the human
  what is missing; do not call `start_print`.
- Sliced-for check: if `model_match` is false the file was sliced for a different printer model and the start is
  refused. Only if the human has read that warning and says it is safe, pass `model_override=True` and add
  `|model_override` to the end of the confirm string. Never on your own.
- Printers without an AMS print whatever filament is loaded: remind the human to check it.
- A read key cannot make the Hub download metadata; if `suggest_mapping` says metadata is unavailable, say so.

## Pause, resume, cancel
`pause_print`, `resume_print`, `cancel_print` only when the human asked, naming the printer. A cancelled job
shows as FAILED afterwards: that is normal. If a print looks wrong, tell the human (a camera picture from
`get_camera_snapshot` helps); do not act on your own.
