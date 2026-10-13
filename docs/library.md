# File library

One place on the Hub for the print files of the whole fleet. Open it with the **Library** button in the dashboard header.

## What you can do

- **Keep files on the Hub**: upload from the browser (`.3mf`, `.gcode.3mf`, `.gcode`, `.bgcode`), or **Save to library** from a file that is on a printer. Files live in `/data/library/` (private to the Hub, mode 0600). Include that folder in your own volume backups; the printer zips under `/data/backups/` do not contain it.
- **See everything in one list**: files in the library and the files the printers are known to hold, merged by name and size into one row ("on: P1S, A1 - in library"). **Hide printer-only files** shows only what is stored on the Hub.
- **Organise and find**: one level of folders (chips along the top), search (names, printers, "sliced for" model, filament colours), grid or list.
- **Details panel**: plate preview, print time, weight, layer height, layers, nozzle and temperatures, infill, supports, slicer, the printer model the file was **sliced for**, and every filament as a **colour word** with its grams.
- **Select several files** to download, send, save or delete them together. Deleting asks twice (tap, then tap again) and only ever removes the library copy; a printer's own files are deleted on that printer's page.

## Sending and printing

**Send to...** uploads a library file to the printers you tick. It never starts anything.

| Rule | Why |
|---|---|
| Only a printer that is positively idle (connected and idle, finished or failed) gets a file; a printing, paused, preparing, offline or unknown one is refused, and the Hub opens no session on it. | One file transfer is enough to disturb a print on these printers. |
| Bambu LAN printers receive `.3mf` files. OctoPrint, PrusaLink and Klipper printers receive `.gcode` / `.bgcode` files (**beta - not yet verified on real hardware**). | Each driver has its own upload call; the HTTP ones are new. |
| A file sliced for a different printer model is refused, with the same warning the start-print confirm shows. You can send it anyway by ticking "I know this file is safe on ...". | The same crash risk the confirm guards against. |
| A different file with the same name already on the card is never overwritten. | Your own files stay yours. |

After a successful send, **Open the start-print confirm** hands over to the normal start flow on that printer: spool mapping, the "bed is clear" tick, the single-use ticket. A file that is already on a printer also gets a **Print on ...** button in the details panel.

Saving a file *from* a printer follows the backup rules: a Bambu is only asked when idle (its last idle file list is shown otherwise); OctoPrint and Klipper read the host's own storage and are fine mid-print; PrusaLink reads the printer's USB stick, so only when idle. **Refresh printers** re-reads the lists the same careful way.

**Rename...** in the details panel renames a library file (or, for a printer-only file, the file on that printer) after a check and your tap on Apply; see [Renaming files](renaming.md).

## Limits and safety

- File names are checked strictly: no folders or `..` in a name, no leading dot, no control characters, one of the four extensions. The Hub refuses a bad name; it does not rewrite it.
- Largest file: 500 MB (set `FABRICA_LIBRARY_MAX_MB` to change it, 1 to 4096). The size is enforced while the file arrives; a partly received file is never listed.
- Details of files over 128 MB are not read (the file is kept).

## API

| Route | Scope | |
|---|---|---|
| `GET /api/library?q=&folder=&hide_printer_only=1` | read | rows (library + cached printer listings), folders, printers and whether each is idle |
| `GET /api/library/files/{name}` | read | one library file |
| `GET /api/library/files/{name}/download` | read | the file |
| `GET /api/library/thumb/{basename}` | read | plate preview |
| `PUT /api/library/files/{name}?folder=&replace=1` | control | upload: the file's bytes as the body, `Content-Type: application/octet-stream` **and** header `X-Fabrica-Upload: 1` (the Hub refuses other non-JSON writes; this header cannot be set by a cross-site form) |
| `POST /api/library/delete` | control | `{name, confirm: name}` |
| `POST /api/library/save` | control | `{printer, filename, folder?}` |
| `POST /api/library/send` | control | `{names|name, printers, model_override?}` -> one result per file and printer |
| `POST /api/library/folder` | control | `{action: create|delete|move, folder, name?}` |
| `POST /api/library/scan` | control | re-read the printers' file lists |

## For agents

`library_list(query?, folder?)` and `library_file(name)` (read key) look; `library_send(name, printers, model_override=False)` and `library_save_from_printer(printer, filename)` (control key) act. `library_send` uploads only: to print, the agent still goes through `suggest_mapping`, the human's go-ahead and bed-clear statement, then `start_print`. `model_override` is for the human's decision, never the agent's.

Code: `app/library.py` (store, details, merge), the `# ==== library ====` block in `app/server.py` (routes, tools and every printer touch), the Library screen in `app/dashboard.html`. Tests: `tests/test_library.py`.
