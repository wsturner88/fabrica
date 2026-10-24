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
   The same answer carries `plate_contents`: how many of which object are on the plate (see "Plate contents" below).
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

## Plate contents: putting the quantity in a name

A file name that says `4x Hose Clip + 2x Hook` helps only if it is true. The Hub reads the cancel-object labels a slicer writes (the
"Label objects" / "Exclude objects" switch) and says how many of which object are on the plate.

`plate_contents(printer, filename)` (read key; `printer` is a printer name or `"library"`), `GET /api/files/plate?location=&name=`, and the
`plate_contents` field of `file_facts` / `GET /api/files/facts` (also `plate` in the file lists, the file detail and `suggest_mapping`):

```json
{"labels": "present", "source": "m486", "total": 6,
 "groups": [{"name": "Hose Clip", "count": 4}, {"name": "Hook", "count": 2}],
 "summary": "4× Hose Clip + 2× Hook"}
```

* `labels` is `present`, `missing` or `unknown`. **`missing` is said only when it is certain**: the whole file, or the whole section where
  that slicer writes its labels, was read and there are none (or a Bambu `slice_info.config` lists no object). A head-only read that stops
  before the end of the first layer, a slicer the Hub does not know (Cura) and a format that hides its G-code (Prusa `.bgcode`) are
  `unknown`, and `unknown` shows nothing on screen.
* `source`: `slice_info` (Bambu Studio / OrcaSlicer `.gcode.3mf`, per plate), `exclude_object` (Klipper `EXCLUDE_OBJECT_DEFINE`), `m486`
  (Marlin / Prusa `M486`), `objects_info` (PrusaSlicer's `; objects_info` line, or the same key in a `.bgcode` metadata block), `comments`
  (`; printing object ...`, Bambu's `; start printing object, unique label id`).
* **One labelled object counts one.** An object assembled from several parts, or a multi-part model the slicer treats as one object, is 1;
  two copies are 2 - it is "objects the printer can cancel separately".
* Names are normalised so copies group: extension (`.stl`, `.3mf`, `.step`, `.obj`), slicer copy suffixes (` id:0 copy 1`,
  `_id_0_copy_1`, ` (Instance 2)`, ` (3)`) and a trailing `_1` / `-2` only when the numbers run 1..n. Klipper names, which cannot hold
  spaces, get them back from underscores.
* Nothing is downloaded for it. The answer is worked out where the Hub already holds the bytes: the Bambu `.gcode.3mf` it fetches anyway,
  the 1 MB head and 64 KB tail of file-host G-code it already reads, and the library's own files on the Hub's disk. A file the Hub has not
  read yet - or read before this version (set `FABRICA_PLATE_BACKFILL=1` to let the background pass read those once more) - is `unknown`.
* The file list, grid and detail sheet show `Plate: 4× Hose Clip + 2× Hook`, or - for `missing` only - a framed `⚠️ No labels` and
  "No object labels - can't count parts or cancel one object. Turn on "Label objects" in the slicer." The start-print dialog shows one
  line. Nothing ever blocks a print.

A Klipper printer whose `printer.cfg` has no `[exclude_object]` section cannot cancel one object at all: `printer_status` and the printer
card say so in words (`exclude_object: false`, "Exclude objects not set up in printer.cfg"). The Hub looks once per connect with one small
read-only `GET /printer/objects/list`.
