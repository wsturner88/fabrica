# History and cost

Every print the Hub sees is logged: printer, file, result, parts, grams, time and cost.

- `recent_jobs(limit=20, printer="", printers=None, group="")`: newest first. Result is FINISHED, FAILED,
  CANCELLED, UNKNOWN or RUNNING. Each row has parts, grams, duration and cost.
- `job_stats(period="month", printer="", printers=None, group="")`: totals for a period: jobs, success rate,
  hours, grams, cost, and breakdowns by printer, material, file and printer group (`by_group`).
  `period` is day, week, month, year, all, or `<N>d` such as `14d`.
- `cost_per_part(file, printers=None, group="")`: the true cost of one part of a file (failed and cancelled
  attempts charged to the good parts) next to the clean cost (finished prints only), and the success rate.
  The file name may be given with or without `.gcode.3mf`.

## Filters
`printer` (one name), `printers` (a list) and `group` (one name, or several separated by commas; see `list_groups`)
add up: a print counts if it ran on any named printer or in any named group. An unknown group is refused and the
reply lists the groups that exist.

## How pieces are counted
A Bambu `.gcode.3mf` records the objects on the plate; that count is `parts` for the job. Files without that
information count as 1 part, and a job whose parts are unknown is priced as 1. Good parts = parts of FINISHED jobs.
Cost per part = cost / parts. Report both numbers when asked about pieces made: parts made, and attempts.

## Tips
- Say the period and filter you used in your answer.
- Costs use the owner's rates from `get_settings`; do not invent prices.
- If a job has no material or weight, say "unknown" rather than guessing.
