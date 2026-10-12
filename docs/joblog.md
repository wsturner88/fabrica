# Job log and cost per part

Every print attempt is logged, and priced. Two standalone modules in `app/` (stdlib only, no
printer or network access) that `server.py` wires into the Hub - see [How it is wired](#how-it-is-wired):

| Module | Job |
|---|---|
| `joblog.py` | `JobLog`: the SQLite job log (`/data/jobs.sqlite`), the `EventWatcher` sink, orphan handling, the query API, CSV export, and the `slice_info.config` part-count parser. |
| `costs.py` | `Rates` (prices, power, wear) and the pure cost / aggregation functions. No database. |

Tests: `tests/test_joblog.py`, `tests/test_costs.py` (temp directories, fake clock, nothing leaves the machine).

## What gets logged

One row per **attempt**, not per file: a print that fails and is re-run is two rows. A row is
opened by `print_started`, updated by `print_paused`, and closed by `print_finished` /
`print_failed` (cancelled is a flavour of failed). The Hub never writes to the log from anywhere
else, and the log never contacts a printer: file facts come from a `resolver` callback (below).

### Data model (`jobs` table)

| Column | Meaning |
|---|---|
| `id` | Integer primary key (the job id used by the API). |
| `printer`, `kind`, `model` | Printer name as in the fleet; driver kind (`bambu` / `octoprint` / `prusalink` / `moonraker`); model name or the slicer's `printer_model_id`. |
| `file`, `file_key`, `plate` | Job name as the printer reported it; lower-cased name with `.gcode.3mf` / `.3mf` / `.gcode` / `.bgcode` stripped (what "same file" means); plate number when known. |
| `started_at`, `ended_at` | Unix seconds. `ended_at IS NULL` = still open. `started_at` is NULL only for "late" rows (below) with no duration. |
| `duration_s`, `est_seconds` | Actual wall-clock seconds (pauses included) and the slicer's estimate. |
| `result` | `running` / `finished` / `failed` / `cancelled` / `unknown`. |
| `percent` | Progress reached (100 for finished). Last value seen if the end event carries none. |
| `grams_est` | Filament the **whole plate** needs, per the slicer. |
| `grams_used` | `grams_est` scaled by progress: 100 % if finished, `percent` % otherwise; NULL if progress is unknown (never guessed). |
| `material`, `colour`, `filaments` | Dominant material / colour, plus JSON `[{type, color, used_g}]` for every filament on the plate. |
| `parts`, `parts_source` | Objects on the plate (`slice_info` = counted; `assumed` = 1 for non-Bambu files; NULL = unknown, priced as 1). |
| `pauses`, `error`, `ext_job_id` | Number of pause events; the printer's error text (or the note on an auto-closed row); the printer's own job id when an event carries one. |
| `source`, `last_seen_at` | `event` (normal) or `late` (end seen with no start on record); last time the job was seen alive (used for orphan handling). |

Indexes: `(printer, started_at)`, `file_key`, `ended_at`. SQLite in WAL mode. Schema changes are
numbered migrations in `joblog.MIGRATIONS`, recorded in `schema_migrations`; opening the file
applies whatever is missing, inside one transaction each, and is safe to repeat.

### Lifecycle rules (restarts, duplicates, orphans)

| Situation | What happens |
|---|---|
| `print_started` | Opens a row; the resolver fills grams / time / parts / material. |
| `print_started` again for the same file (replay) | Ignored - the original row stays. |
| `print_started` while another job is open on that printer | The old row is closed `unknown` ("superseded"), then the new one opens. |
| Hub restarts mid-print | `EventWatcher` baselines silently (no second start). The open row survives in the database and the eventual `print_finished` / `print_failed` closes it. One row total. |
| End event with no open row (DB is new, or the Hub was down at the start) | A `late` row is written: end time from the event, start back-dated by the reported duration when there is one. |
| Same end event delivered twice | Recognised (same printer + file within 5 min) and ignored. |
| End event names a different file than the open row | The open row is **not** closed by it; the end is logged as its own late row, and `observe()` sweeps the stale one. |
| End event never arrives | `observe(fleet)`: the printer is reachable but not printing for `orphan_grace_s` (120 s) -> the row is closed `unknown`, ended at the last time it was seen active, so its duration is honest. |
| ...then the end event does arrive (race) | The swept row is upgraded to the real result instead of creating a duplicate. |
| Printer reports a different job than the open row | Old row closed `unknown`. |
| Printer removed / unreachable for `stale_after_s` (48 h) | Row closed `unknown`. |

`unknown` rows are shown but left out of cost totals and success rates - they would distort both.

### Part counting

`joblog.parse_slice_info(text)` reads a Bambu `Metadata/slice_info.config`: for each `<plate>`
it returns `parts` (number of `<object>` entries, ignoring `skipped="true"`), weight, estimated
seconds, `printer_model_id` and the filament list. Verified shape on real printer-side files:
3, 2 and 2 objects. It never raises: bad XML falls back to a lenient scan (the result's `error`
says so), unreadable input gives `{"plates": [], "error": ...}`, and a DOCTYPE / ENTITY
declaration is never handed to the XML parser. A plate with no `<object>` entries has
`parts = None` ("unknown"), not 0. `slice_info_from_3mf(bytes)` does the same from a `.gcode.3mf`;
`parts_for_plate(info, plate=None)` picks a plate (default: the first). Printer-side files carry no
geometry, so this counts objects, not triangles - exactly what a sale is counted in.

### The resolver

`JobLog(path, resolver=fn)` where `fn(printer, job_name) -> dict | None`. It is called inside the
event sink, so it must answer from caches only and never touch the network. Return any of
`grams`, `seconds`, `parts`, `material`, `colour`, `filaments`, `model`, `plate`, `kind`.
Helpers to build that dict from what the Hub already holds:

| Helper | From |
|---|---|
| `resolved_from_3mf(bytes, plate=None)` | A downloaded `.gcode.3mf` (per-plate grams, time, parts, filaments). |
| `resolved_from_cached_meta(meta, parts=None)` | The dict `bambu.parse_3mf_meta` produced (the metadata cache entry). It has no part count - pass `parts` from `parse_slice_info`. |
| `resolved_from_file_entry(entry, material=None, kind=None)` | A driver `files()` entry (`print_seconds`, `filament_mm`); mm -> grams by 1.75 mm and material density; 1 part assumed. |

If the resolver had nothing at start (cold cache), it is asked again when the job closes.

## Costs

Rates (`costs.Rates`, defaults in brackets): filament price per kg per material (PLA 20,
PETG 22, ABS 22, ASA 25, TPU 35; others 25), electricity price per kWh (0.15), average watts per
printer (P1S 120, A1 90, MK4 100; generic 120), machine wear per hour (0.30), and the file window N
(20 attempts). All editable; rates are applied **when you ask**, so changing a price re-prices the
whole history. Material names match exactly, then by longest prefix (`PLA+` and `PLA-CF` use the
PLA price). Printer watts match the exact printer name, then a key inside the model name, then one
inside the printer name, then the generic default.

### Formulas

```
fraction     = 1.0 if finished, else percent / 100         (unknown percent -> unknown, not 0)
grams_used   = grams_est x fraction
material     = sum over filaments:  grams_used_i / 1000 x price_per_kg(type_i)
hours        = real duration / 3600   (estimate x fraction if the duration was not captured)
electricity  = hours x watts / 1000 x kwh_price
wear         = hours x wear_per_hour
job cost     = material + electricity + wear
cost per part (one finished job) = job cost / parts
```

**True cost per part** (per file, per printer, or over a period) charges failures to the good parts:

```
true cost per part = cost of ALL attempts / good parts produced
                   = (sum of finished job costs + sum of failed/cancelled job costs)
                     / (sum of `parts` over finished jobs)
waste per part     = failed/cancelled job costs / good parts      (true = clean + waste)
clean cost per part = finished job costs / good parts
```

Per file the "all attempts" are the last N (default 20) closed attempts of that file, finished or
failed, newest first - so an old bad week stops haunting a file that now prints reliably. A file with
failures but no finished job has no per-part figure (None); its sunk cost still shows as
`wasted_on_failures`. **Success rate** = finished / (finished + failed + cancelled); cancelled counts
as a failure (the filament and hours are gone either way), `unknown` and `running` are excluded.

### Worked example

Rates: PLA 20/kg, 0.15 per kWh, P1S 120 W, wear 0.30/h. File `stand`: a plate of 3 parts, 36 g PLA,
estimated 2 h 30 min. Four attempts on the P1S: three finish in 2 h 30 min; one fails at 40 % after 1 h.

| Attempt | Material | Electricity | Wear | Job cost |
|---|---|---|---|---|
| Finished (each) | 36 g -> 0.72 | 2.5 h x 0.120 kW x 0.15 = 0.045 | 2.5 x 0.30 = 0.75 | **1.515** (0.505 per part) |
| Failed at 40 % | 14.4 g -> 0.288 | 1 h -> 0.018 | 0.30 | **0.606** |

```
all attempts      = 3 x 1.515 + 0.606 = 5.151
good parts        = 3 x 3 = 9
true cost / part  = 5.151 / 9 = 0.5723      (clean 0.5050 + waste 0.0673)
success rate      = 3 / 4 = 75 %
```

The sale price for a `stand` has to clear 0.57, not 0.51. (Not modelled: purge / flush waste on
multi-colour prints, supports the slicer did not count, packaging, fees, labour.)

## Query API (functions on `JobLog`)

| Call | Returns |
|---|---|
| `recent_jobs(limit=50, printer=None, result=None)` | Newest first; each job with its `cost` block (None while open / unknown), ISO times in the Hub timezone, `time_vs_estimate_pct`. |
| `job(id)` | One job or None. |
| `stats(period="month", printer=None)` | Period = `day` (24 h) / `week` (7 d) / `month` (30 d) / `year` / `all` / `<N>d`. Counts, `success_rate`, `hours`, `grams_used`, `cost{material,electricity,wear,total,wasted_on_failures}`, `clean_cost_per_part`, `true_cost_per_part`, `waste_per_part`, plus `by_printer`, `by_file` (top 25 by spend), `by_material`, and `material_series` (grams per day, or per month for year / all). |
| `material_usage(bucket="day"/"week"/"month", period, printer)` | Grams by material per bucket. |
| `file_costs(name, last_n=None, printer=None)` | The per-file figures over the last N attempts, plus `avg_parts_per_print`, `grams_per_part`, `hours_per_part`. `name` may be written with or without extension. |
| `export_csv(printer=None, period="all", fp=None)` | CSV text of the jobs (oldest first) with costs at current rates; spreadsheet-formula-looking names are neutralised with a leading `'`. |
| `on_event(event)` / `observe(fleet)` | The inputs (see wiring). Neither raises. |

## How it is wired

Everything below is in `app/server.py` unless it says otherwise.

1. **Construction.** `JOBLOG = joblog.JobLog(DATA_DIR/jobs.sqlite, resolver=_job_resolver, rates=<Costs settings>,
   tz=<general.timezone>)`, created next to `WATCHER`. Rates are read on every query, so a Settings edit re-prices the
   whole history at once; a time-zone change applies at once too.
2. **Events.** `EventWatcher` has one sink, `_event_sink(ev)`: it hands the event to `JOBLOG.on_event` first (so the row
   exists before a notification could link to it) and then to `NOTIFIER.submit`. Each is in its own `try`, so a broken
   database never silences a notification and vice versa.
3. **Orphans.** `_event_tick` (the 5 s loop that already feeds the watcher) calls `JOBLOG.observe(fleet)` with the same
   snapshots: no extra printer traffic.
4. **Resolver `_job_resolver(printer, job)` - caches only, never the network.**
   * Bambu: the metadata cache entry for that job name, matched the way the log matches files (case-insensitive, with or
     without `.gcode.3mf` / `.3mf`). `_meta_for` now stores **`meta["parts"]`** (objects on plate 1, from
     `slice_info.config`) next to the rest, so counts arrive with the cache; entries cached before this have no count until
     they are refreshed and are priced as 1 part. (The key is `parts`, not `plates`: `meta["plates"]` already holds the list of
     plate G-code names.)
   * AMS: when **this Hub** started the print (`start_print` remembers the file and its `ams_mapping` for 30 minutes) the
     filament type and colour come from the AMS tray that was actually chosen, so the price follows what was loaded, not what
     the slicer assumed. A print started at the printer itself is priced from the slicer's filament.
   * OctoPrint / PrusaLink / Moonraker: the last `files()` listing the Hub already made (`_FILES_SEEN`, refreshed by the file
     list and the pre-warm thread), via `resolved_from_file_entry`.
   * Cold cache: `{}`; the log asks again when the job closes, so a file that was read mid-print still gets its facts.
5. **Routes** (`read` scope, except the export): `GET /api/jobs?limit=&printer=&result=` -> `{"jobs": [...], "currency"}`;
   `GET /api/jobs/{job_id}` (404 for an unknown or non-numeric id); `GET /api/stats?period=&printer=`;
   `GET /api/costs/file?name=&last_n=&printer=`; `GET /api/jobs.csv?printer=&period=` (**`control`** - file names can carry order
   information; `text/csv`, `Content-Disposition: attachment; filename="jobs.csv"`). A bad `period` / `result` / missing name is a 400.
6. **MCP tools** (`read`): `recent_jobs(limit=20, printer="")`, `job_stats(period="month", printer="")`, `cost_per_part(file)` -
   the last one answers with a sentence ("true cost per part $0.572 (clean $0.505 + waste $0.067), 75% of attempts finished,
   based on the latest 4 attempt(s)") as well as the figures.
7. **Settings -> Costs** (`costs.*`): `currency` (string), `filament_prices` and `printer_watts` (lists of `NAME=number`, validated),
   `default_price_per_kg`, `kwh_price`, `wear_per_hour` (decimals), `default_watts`, `window_n` (whole numbers). `Rates.from_settings`
   reads exactly these.
8. **Dashboard -> History** (header button, full screen): a printer picker and a period picker (24 hours / 7 days / 30 days / all
   time), four tiles (prints, success rate, material used, cost with what failures wasted and the true / clean cost per part), a
   per-printer table, a per-file table (attempts, success, parts, true and clean cost per part), the recent prints (result as a
   word with a symbol, parts, grams, time, cost; tap one for the full detail incl. the error text and the cost breakdown) and a
   **Download CSV** button. It refreshes by itself every 20 seconds. The currency is the Costs setting.
9. **Housekeeping.** `jobs.sqlite` lives on the `/data` volume with everything else (add it to anything that backs up `/data`).
   Job names can contain customer or order info; the CSV is a plain export (spreadsheet formulas are neutralised) - treat it
   accordingly. No retention limit yet (a year of prints is a few MB).

Tests: `tests/test_wiring2.py` drives a fake printer through idle -> printing -> finished and checks the row, the cost, the
routes, the CSV, the resolver (cache, AMS choice, file listings) and the tools.
