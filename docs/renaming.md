# Renaming files

Fabrica gives an agent (or you) the **mechanics** of renaming print files, safely. It does **not** know or enforce any naming
convention: different projects name their files differently, and those rules live in your agent. The Hub supplies the facts
to decide with, checks that a name is something a file system and a printer can hold, and applies the change only after a
human has seen the before -> after table and said yes.

## The flow an agent follows

1. `file_facts(location, filename)` (read key). `location` is a printer name or `"library"`. Returns the current name, the
   extension to keep (`.gcode.3mf` counts as one), size, **how many objects are on the plate** (`parts`, with `parts_source`:
   `slice_info` = counted in the file, `assumed` = a gcode file cannot say so one is assumed, `unknown`), the printer model
   it was sliced for, print time, grams, the filaments as material + colour **words**, where else the same file exists, and
   how often it was printed. It reads cached information; a busy printer is never contacted (the answer says "facts from cache").
2. The agent picks names with **its own** rules.
3. `rename_preview(renames)` (control key): `[{location, old_name, new_name}, ...]`, up to 100. Returns the before -> after table,
   per-row problems and a **single-use ticket** (`rn-...`, valid 10 minutes, bound to exactly that list). Nothing changes.
4. The agent shows the table to the human and waits for a clear yes.
5. `rename_apply(ticket, approved_by_human=true)` (control key). Refused unless the flag is literally `true` and the ticket is
   current. The flag is the human's yes, never the agent's own.

An MCP prompt, `rename_files`, carries the same five steps for any agent.

## What the preview checks

Only what a file system and a printer need: the source exists; the new name **keeps the same extension**; no `/ \ : * ? " < > |`,
no control or invisible characters, no leading or trailing space or dot, at most 200 characters; no other file already has that
name (nothing is ever overwritten; a change of capitals only is not allowed on a printer); the file is not the one being
printed; no swaps or chains inside one batch. A printer that is busy is a **warning** at preview and a **refusal** at apply.
The new name is the bare file name: a file in a printer's folder stays in that folder.

## What apply does

Item by item (one failure never stops the others). Each result is `renamed`, `refused (busy)`, `failed at <step>: <why>` or `skipped`.

| Where | Steps |
|---|---|
| Library | one atomic rename inside the library store: file, plate preview and index entry together; the folder stays |
| Printer | re-check idle (under the printer's file lock) -> download -> **keep a copy of the original in the Library** (folder `Renamed originals`; skipped when a file with identical content is already there) -> re-check idle -> upload under the new name -> **verify** the new name is listed with the same size -> only then delete the old name |

If the upload or the verify fails, the old name is **not** deleted and a half-written new file is removed. A printer that is not
positively idle gets no session at all. Nothing in this feature can start a print.

## History stays linked

A rename leaves a row in the job log (`old file key -> new file key`, printer or library, when). Old job rows are never rewritten.
`recent_jobs`, `job_stats`, `cost_per_part` and the History "By file" table treat the old and the new name as one file and show
the newest name (`renamed_from` on the job). A rename on one printer links only that printer's history; a Library rename links all.

## In the dashboard

A **Rename...** button sits in the Library detail panel and on each printer file row. The dialog shows the name without its
extension (the extension is fixed), checks it, shows the before -> after, and **Apply** is the human's approval.

## Support by printer type

| Printer | Rename | Status |
|---|---|---|
| Bambu (LAN) | download / upload / delete over the printer's one FTPS session | uses the existing, field-tested upload |
| OctoPrint, PrusaLink, Klipper (Moonraker) | the same flow over each one's HTTP API | **beta - not yet verified on real hardware** |

The HTTP upload calls (also used by the Library's **Send to...** for these printers, also beta): OctoPrint `POST /api/files/local`
(`select=false`, `print=false`), PrusaLink `PUT /api/v1/files/<storage>/<path>` (`Overwrite: ?0`, `Print-After-Upload: ?0`),
Moonraker `POST /server/files/upload` (`root=gcodes`, no `print`). Each refuses unless the printer is positively idle, never
follows a redirect, and never starts a print. Downloads are capped at 500 MB.

## API

| Route | Scope | |
|---|---|---|
| `GET /api/files/facts?location=&name=` | read | same as `file_facts` |
| `POST /api/rename/preview` | control | `{renames: [...]}` -> table + ticket |
| `POST /api/rename/apply` | control | `{ticket, approved_by_human: true}` |

Code: `app/renamer.py` (name check, tickets), the `# ==== rename ====` block in `app/server.py` (every printer touch), the driver
`upload` / `download` calls, `Library.rename`, `JobLog.record_rename`. Tests: `tests/test_rename.py`.
