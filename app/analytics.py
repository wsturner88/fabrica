"""Analytics: one summary of the job log across time (success, hours, filament, cost, failures, trend, uptime).

History lists prints; this answers "how is the fleet doing?" for a period. It reads the SAME table as History
(`jobs`, see joblog.py) with the SAME rules, but sums in SQL so it stays cheap on a Raspberry Pi with a big log:

  * window     a print is in `7d` / `30d` / `90d` / `1y` when COALESCE(ended_at, started_at) >= now - period
               (History's window); `all` has no lower bound. Dates and the trend buckets use the Hub's time zone
               (`JobLog.tz`, Settings > General), like History's material series.
  * outcomes   running (not ended) and unknown (the end was never seen) are counted but leave the success rate,
               hours, grams and cost alone; success rate = finished / (finished + failed + cancelled).
  * grams      the slicer estimate x how far the print got (costs.grams_used); the per-filament split is
               scaled to that total (costs.material_grams). Needs SQLite's JSON functions; without them a print's
               grams all go to its main material.
  * hours      costs.machine_hours (the real duration unless it cannot be right, else estimate x progress).
  * cost       FILAMENT cost at the current Costs prices (costs.price_per_kg). Power / wear (the "+ Print time" switch)
               are not part of Analytics. Cost per good part = every attempt's cost / good parts (as History's
               "true cost per part"); a finished print with no part count counts as 1 part, as History does.
  * parts      "parts made" per file adds only prints whose plate had a known part count; prints without one are
               counted separately ("parts unknown") rather than guessed.
  * utilization  printing hours / hours in the period, for the part of the period the printer existed
               (its first print or first uptime record onwards), capped at 100 %.
  * uptime     % of observed time a printer was reachable, from uptime.py (None until it has data).

Pure reads. Never raises for an empty log. Stdlib only.
"""

import datetime
import time

import costs as _costs
import joblog as _joblog

DECIDED = ("finished", "failed", "cancelled")
_FAIL = ("failed", "cancelled")
TOP_FILES = 8
TOP_REASONS = 8
NO_REASON = "no reason recorded"
MIN_OBSERVED_S = 300            # less observed time than this is not turned into a percentage
DAY_SLICE_S = 900               # the trend is grouped in slices in SQL and the slices are folded into local days / weeks / months in Python:
WEEK_SLICE_S = 3600             # 15 minutes for a day chart (exact for every real UTC offset, DST-correct because the fold uses the zone),
                                # an hour for weeks and months (few rows even for years of prints; a print within an hour of a
                                # week / month boundary in a half-hour zone may land in the neighbouring bar)

PERIODS = ("7d", "30d", "90d", "1y", "all")


def period_seconds(period):
    """'7d' / '30d' / '90d' / '1y' / 'all' (or anything joblog accepts: day, week, month, year, '<N>d') -> seconds or None."""
    p = str(period or "30d").strip().lower()
    if p in ("1y", "1yr", "12m"):
        p = "year"
    return _joblog._period_seconds(p)


# ============================================================================ SQL

def _window_sql(since, printer):
    where, args = [], []
    if since is not None:             # = History's COALESCE(ended_at, started_at) >= ?, written so SQLite can use the ended_at index
        where.append("(ended_at >= ? OR (ended_at IS NULL AND started_at >= ?))")
        args.extend([since, since])
    pc = _joblog._printer_clause(printer)
    if pc:
        where.append(pc[0])
        args.extend(pc[1])
    return (" WHERE " + " AND ".join(where)) if where else "", args


def _with_j(window_sql):
    """The CTE every query starts from: the window's rows with the derived columns (outcome `st`, progress `fr`,
    grams `g`, hours `h`) computed once, by the same rules as costs.py."""
    return (
        "WITH j AS (SELECT id, printer, LOWER(printer) AS pk, file, file_key, material, filaments, error, parts, "
        "  COALESCE(ended_at, started_at) AS ts, "
        "  CASE WHEN result='running' OR ended_at IS NULL THEN 'running' "
        "       WHEN result IN ('finished','failed','cancelled') THEN result ELSE 'unknown' END AS st, "
        "  fr, "
        "  CASE WHEN grams_est IS NULL OR fr IS NULL THEN NULL ELSE grams_est * fr END AS g, "
        "  CASE WHEN duration_s IS NOT NULL AND duration_s >= 0 AND duration_s <= %d "
        "            AND NOT (est_seconds IS NOT NULL AND est_seconds > 0 AND duration_s > MAX(est_seconds * 10, est_seconds + 86400)) "
        "       THEN duration_s / 3600.0 "
        "       WHEN est_seconds IS NOT NULL AND fr IS NOT NULL THEN est_seconds * fr / 3600.0 END AS h "
        " FROM (SELECT *, CASE WHEN result='finished' THEN 1.0 WHEN percent IS NULL THEN NULL "
        "                      ELSE MIN(1.0, MAX(0.0, percent / 100.0)) END AS fr FROM jobs" + window_sql + ")) "
    ) % _costs.MAX_PLAUSIBLE_JOB_S


_HAS_JSON = None


def has_json(jl) -> bool:
    """Does this SQLite build have the JSON functions (built in since 3.38, optional before)?"""
    global _HAS_JSON
    if _HAS_JSON is None:
        try:
            jl._q("SELECT json_valid('[]') AS v, json_extract('{\"a\":1}', '$.a') AS a, json_type('[]') AS t")
            _HAS_JSON = True
        except Exception:
            _HAS_JSON = False
    return _HAS_JSON


# grams per (printer, outcome, material): a print with a complete per-filament list is split by it, any other print
# puts everything on its main material - the rule of costs.material_grams, in SQL
_MIX_JSON = (
    ", d AS (SELECT * FROM j WHERE st IN ('finished','failed','cancelled') AND g IS NOT NULL), "
    "f AS (SELECT d.id AS id, UPPER(COALESCE(NULLIF(json_extract(e.value,'$.type'),''), NULLIF(d.material,''), '?')) AS t, "
    "             json_extract(e.value,'$.used_g') AS u "
    "      FROM d, json_each(CASE WHEN json_valid(d.filaments) AND json_type(d.filaments)='array' THEN d.filaments ELSE '[]' END) e), "
    "c AS (SELECT id, COUNT(*) AS n, COUNT(u) AS nu, SUM(u) AS su FROM f GROUP BY id), "
    "mix AS (SELECT d.pk AS pk, d.st AS st, f.t AS t, d.g * f.u / c.su AS gg FROM d JOIN f ON f.id = d.id JOIN c ON c.id = d.id "
    "          WHERE c.n > 0 AND c.n = c.nu AND c.su > 0 "
    "        UNION ALL "
    "        SELECT d.pk, d.st, UPPER(COALESCE(NULLIF(d.material,''), '?')), d.g FROM d LEFT JOIN c ON c.id = d.id "
    "          WHERE c.id IS NULL OR NOT (c.n > 0 AND c.n = c.nu AND c.su > 0)) "
    "SELECT pk, st, t, SUM(gg) AS g FROM mix GROUP BY pk, st, t")
_MIX_PLAIN = (
    ", d AS (SELECT * FROM j WHERE st IN ('finished','failed','cancelled') AND g IS NOT NULL) "
    "SELECT pk, st, UPPER(COALESCE(NULLIF(material,''), '?')) AS t, SUM(g) AS g FROM d GROUP BY pk, st, t")

_COUNTS = ("SUM(st='finished') AS fin, SUM(st='failed') AS fail, SUM(st='cancelled') AS canc, "
           "SUM(st='unknown') AS unk, SUM(st='running') AS run, COUNT(*) AS n")


def _i(v):
    return int(v or 0)


def _f(v):
    return float(v or 0.0)


def _rate(fin, decided):
    return round(fin / decided, 4) if decided else None


# ============================================================================ pieces

def _by_printer_rows(jl, base, args):
    return jl._q(base + "SELECT pk, printer, MAX(ts) AS last_ts, " + _COUNTS + ", "
                 "SUM(CASE WHEN st IN ('finished','failed','cancelled') THEN COALESCE(h, 0) END) AS hours, "
                 "SUM(CASE WHEN st IN ('finished','failed','cancelled') THEN COALESCE(g, 0) END) AS grams, "
                 "SUM(CASE WHEN st = 'finished' THEN COALESCE(NULLIF(parts, 0), 1) END) AS good_parts "
                 "FROM j GROUP BY pk", tuple(args))


def _first_seen(jl, names=None):
    """{printer lower: first start time ever logged}: a printer's age, for utilization (index-only scan)."""
    rows = jl._q("SELECT LOWER(printer) AS pk, MIN(started_at) AS f FROM jobs GROUP BY LOWER(printer)")
    return {r["pk"]: r["f"] for r in rows if r["f"] is not None}


def _material_rows(jl, base, args):
    return jl._q(base + "SELECT UPPER(COALESCE(NULLIF(material,''), '?')) AS m, " + _COUNTS + " FROM j GROUP BY m", tuple(args))


def _grams_rows(jl, base, args):
    sql = _MIX_JSON if has_json(jl) else _MIX_PLAIN
    return jl._q(base + sql, tuple(args))


def _file_rows(jl, base, args):
    return jl._q(base + "SELECT pk, printer, COALESCE(NULLIF(file_key,''), LOWER(TRIM(COALESCE(file,'')))) AS fk, file, MAX(ts) AS last_ts, "
                 "COUNT(*) AS n, SUM(st='finished') AS fin, SUM(st='failed') AS fail, SUM(st='cancelled') AS canc, "
                 "SUM(st='unknown') AS unk, SUM(st='running') AS run, "
                 "SUM(CASE WHEN st='finished' AND parts > 0 THEN parts END) AS parts_known, "
                 "SUM(st='finished' AND (parts IS NULL OR parts <= 0)) AS parts_unknown "
                 "FROM j GROUP BY pk, fk", tuple(args))


def _reason_rows(jl, base, args):
    return jl._q(base + "SELECT st, COALESCE(NULLIF(TRIM(error),''), '') AS reason, COUNT(*) AS n FROM j "
                 "WHERE st IN ('failed','cancelled') GROUP BY st, reason ORDER BY n DESC, reason LIMIT ?", tuple(args) + (TOP_REASONS * 2,))


def _slice_rows(jl, base, args, width):
    return jl._q(base + "SELECT CAST(ts / %d AS INTEGER) AS b, SUM(st='finished') AS fin, SUM(st IN ('failed','cancelled')) AS bad, "
                 "SUM(st IN ('unknown','running')) AS oth FROM j WHERE ts IS NOT NULL GROUP BY b" % width, tuple(args))


def _fold_files(jl, rows):
    """Group by file across printers, following renames (a renamed file is one file, shown under its newest name)."""
    rmap = jl._renames_map()
    out = {}
    for r in rows:
        key, new_name = (r["fk"], None)
        if rmap:
            key, new_name = jl._canonical(r["printer"], r["fk"], rmap)
        e = out.setdefault(key or "(unnamed)", {"key": key or "(unnamed)", "name": None, "name_ts": -1.0, "prints": 0,
                                                "finished": 0, "failed": 0, "cancelled": 0, "unknown": 0, "running": 0,
                                                "parts": 0, "parts_unknown_prints": 0})
        e["prints"] += _i(r["n"])
        e["finished"] += _i(r["fin"])
        e["failed"] += _i(r["fail"])
        e["cancelled"] += _i(r["canc"])
        e["unknown"] += _i(r["unk"])
        e["running"] += _i(r["run"])
        e["parts"] += _i(r["parts_known"])
        e["parts_unknown_prints"] += _i(r["parts_unknown"])
        ts = _f(r["last_ts"])
        if new_name:                              # the newest spelling a rename gave it wins
            e["name"], e["name_ts"] = new_name, float("inf")
        elif ts > e["name_ts"]:
            e["name"], e["name_ts"] = r["file"], ts
    files = []
    for e in out.values():
        decided = e["finished"] + e["failed"] + e["cancelled"]
        files.append({"file": _costs.file_label(e["name"]) or e["key"], "prints": e["prints"], "finished": e["finished"],
                      "failed": e["failed"] + e["cancelled"], "success_rate": _rate(e["finished"], decided),
                      "parts": e["parts"], "parts_unknown_prints": e["parts_unknown_prints"]})
    return files


def _trend(jl, base, args, since, until, first_ts):
    secs_span = (until - since) if since is not None else ((until - first_ts) if first_ts else 0)
    day = 86400
    bucket = "day" if secs_span <= 45 * day else "week" if secs_span <= 800 * day else "month"
    tz = jl.tz
    start_ts = since if since is not None else first_ts

    def key_of(ts):
        d = datetime.datetime.fromtimestamp(ts, tz).date()
        if bucket == "month":
            return datetime.date(d.year, d.month, 1)
        if bucket == "week":
            return d - datetime.timedelta(days=d.weekday())
        return d

    counts = {}
    width = DAY_SLICE_S if bucket == "day" else WEEK_SLICE_S
    for r in _slice_rows(jl, base, args, width):
        k = key_of(r["b"] * width + width / 2)
        c = counts.setdefault(k, [0, 0, 0])
        c[0] += _i(r["fin"])
        c[1] += _i(r["bad"])
        c[2] += _i(r["oth"])
    if start_ts is None:
        return {"bucket": bucket, "buckets": [], "partial_first": False}
    first, last = key_of(start_ts), key_of(until)
    buckets, k = [], first
    while k <= last and len(buckets) < 1200:
        c = counts.get(k, [0, 0, 0])
        buckets.append({"start": k.isoformat(), "finished": c[0], "failed": c[1], "other": c[2]})
        if bucket == "month":
            k = datetime.date(k.year + (k.month == 12), k.month % 12 + 1, 1)
        else:
            k += datetime.timedelta(days=7 if bucket == "week" else 1)
    first_open = datetime.datetime.combine(first, datetime.time.min, tzinfo=tz).timestamp()
    return {"bucket": bucket, "buckets": buckets, "partial_first": since is not None and since - first_open > 60}


# ============================================================================ the summary

def _names_filter(printer):
    """The printer filter as a set of lower-case names, or None for "no filter" (what uptime.report wants)."""
    if printer is None:
        return None
    if isinstance(printer, str):
        return {printer.lower()} if printer else None
    return {str(p).lower() for p in printer if p}


def summary(jl, uptime=None, period="30d", printer=None, now=None, known_printers=None) -> dict:
    """Everything the Analytics page shows, for the trailing `period` and an optional printer filter (as JobLog:
    a name, a list of names, or None). `known_printers` = every printer the Hub has, so one that has not printed
    in the period still gets a row. See the module docstring for the rules."""
    secs = period_seconds(period)
    now = jl.clock() if now is None else now
    since = None if secs is None else now - secs
    rates = jl.rates()
    where, wargs = _window_sql(since, printer)
    base = _with_j(where)

    prn = _by_printer_rows(jl, base, wargs)
    firsts = _first_seen(jl)
    grams_rows = _grams_rows(jl, base, wargs)
    mat_rows = _material_rows(jl, base, wargs)
    file_rows = _file_rows(jl, base, wargs)
    reason_rows = _reason_rows(jl, base, wargs)

    # ---- cost: grams by (printer, outcome, material) x price
    cost_by_printer, cost_by_mat, grams_by_mat = {}, {}, {}
    good_cost = fail_cost = 0.0
    for r in grams_rows:
        g = _f(r["g"])
        c = g / 1000.0 * _costs.price_per_kg(rates, r["t"])
        cost_by_printer[r["pk"]] = cost_by_printer.get(r["pk"], 0.0) + c
        cost_by_mat[r["t"]] = cost_by_mat.get(r["t"], 0.0) + c
        grams_by_mat[r["t"]] = grams_by_mat.get(r["t"], 0.0) + g
        if r["st"] == "finished":
            good_cost += c
        else:
            fail_cost += c
    total_cost = good_cost + fail_cost

    # ---- uptime
    names_f = _names_filter(printer)
    up = uptime.report(since, now, names_f) if uptime is not None else {"first_t": None, "printers": {}}

    # ---- per printer
    rows, tot = {}, {"fin": 0, "fail": 0, "canc": 0, "unk": 0, "run": 0, "n": 0, "hours": 0.0, "grams": 0.0, "good_parts": 0}
    display = {}
    for r in prn:
        pk = r["pk"]
        display[pk] = r["printer"]
        rows[pk] = r
        for k in ("fin", "fail", "canc", "unk", "run", "n", "good_parts"):
            tot[k] += _i(r[k])
        tot["hours"] += _f(r["hours"])
        tot["grams"] += _f(r["grams"])
    for n in known_printers or []:
        if names_f is None or str(n).lower() in names_f:
            display.setdefault(str(n).lower(), str(n))
    for pk, u in up["printers"].items():
        if names_f is None or pk in names_f:
            display.setdefault(pk, u.get("name") or pk)

    by_printer = []
    for pk in sorted(display, key=lambda k: display[k].lower()):
        r = rows.get(pk)
        fin, fail, canc = (_i(r["fin"]), _i(r["fail"]), _i(r["canc"])) if r else (0, 0, 0)
        decided = fin + fail + canc
        hours = _f(r["hours"]) if r else 0.0
        first = firsts.get(pk)
        u = up["printers"].get(pk)
        if u and u.get("first_t") is not None:
            first = u["first_t"] if first is None else min(first, u["first_t"])
        begin = since if since is not None else first
        if begin is not None and first is not None:
            begin = max(begin, first)
        span_h = (now - begin) / 3600.0 if begin is not None else 0.0
        online_pct = None
        if u and u["observed_s"] >= MIN_OBSERVED_S:
            online_pct = round(100.0 * u["online_s"] / u["observed_s"], 1)
        by_printer.append({
            "printer": display[pk], "prints": _i(r["n"]) if r else 0, "finished": fin, "failed": fail + canc,
            "success_rate": _rate(fin, decided), "hours": round(hours, 2),
            "utilization": round(min(1.0, hours / span_h), 4) if span_h >= 1.0 else None,
            "grams": round(_f(r["grams"]), 1) if r else 0.0, "cost": round(cost_by_printer.get(pk, 0.0), 2),
            "online_pct": online_pct, "observed_h": round(u["observed_s"] / 3600.0, 1) if u else 0.0})

    # ---- per material
    mats = {}
    for r in mat_rows:
        mats[r["m"]] = r
    by_material = []
    for m in sorted(set(mats) | set(grams_by_mat), key=lambda k: (-grams_by_mat.get(k, 0.0), k)):
        r = mats.get(m)
        fin, fail, canc = (_i(r["fin"]), _i(r["fail"]), _i(r["canc"])) if r else (0, 0, 0)
        by_material.append({"material": m, "prints": _i(r["n"]) if r else 0, "finished": fin, "failed": fail + canc,
                            "success_rate": _rate(fin, fin + fail + canc), "grams": round(grams_by_mat.get(m, 0.0), 1),
                            "cost": round(cost_by_mat.get(m, 0.0), 2)})

    # ---- files
    files = _fold_files(jl, file_rows)
    top = sorted((f for f in files if f["prints"]), key=lambda f: (-f["prints"], -f["finished"], f["file"].lower()))[:TOP_FILES]
    worst = sorted((f for f in files if f["failed"]), key=lambda f: (-f["failed"], f["file"].lower()))[:TOP_FILES]

    # ---- failures
    reasons, shown = [], 0
    for r in reason_rows[:TOP_REASONS]:
        txt = " ".join(str(r["reason"]).split())[:90] or NO_REASON
        reasons.append({"reason": txt, "outcome": r["st"], "count": _i(r["n"])})
        shown += _i(r["n"])
    n_bad = tot["fail"] + tot["canc"]
    decided = tot["fin"] + tot["fail"] + tot["canc"]
    good_parts = tot["good_parts"]
    parts_unknown = sum(f["parts_unknown_prints"] for f in files)

    first_ts = None
    if since is None and tot["n"]:         # "all": the trend starts at the oldest print in the log
        first_ts = jl._q("SELECT MIN(COALESCE(started_at, ended_at)) AS f FROM jobs" + where, tuple(wargs))[0]["f"]
    trend = _trend(jl, base, wargs, since, now, first_ts)

    return {
        "period": str(period or "30d").strip().lower(), "since": since, "until": now,
        "since_iso": jl._iso(since), "until_iso": jl._iso(now), "tz": str(getattr(jl.tz, "key", None) or jl.tz),
        "currency": rates.currency, "cost_basis": "filament",
        "empty": tot["n"] == 0,
        "totals": {
            "prints": tot["n"], "finished": tot["fin"], "failed": tot["fail"], "cancelled": tot["canc"],
            "unknown": tot["unk"], "running": tot["run"], "decided": decided, "success_rate": _rate(tot["fin"], decided),
            "hours": round(tot["hours"], 2), "grams": round(tot["grams"], 1), "filament_kg": round(tot["grams"] / 1000.0, 3),
            "filament_cost": round(total_cost, 2), "wasted_cost": round(fail_cost, 2), "good_parts": good_parts,
            "parts_unknown_prints": parts_unknown,
            "cost_per_good_part": round(total_cost / good_parts, 4) if good_parts else None},
        "by_printer": by_printer,
        "by_material": by_material,
        "failures": {
            "by_outcome": [{"outcome": "failed", "count": tot["fail"]}, {"outcome": "cancelled", "count": tot["canc"]},
                           {"outcome": "unknown", "count": tot["unk"]}],
            "failed_or_cancelled": n_bad, "wasted_cost": round(fail_cost, 2),
            "by_reason": reasons, "reasons_other": max(0, n_bad - shown) if reason_rows else 0,
            "top_files": worst},
        "top_files": top,
        "trend": trend,
        "uptime": {"available": up["first_t"] is not None, "since": up["first_t"], "since_iso": jl._iso(up["first_t"]),
                   "partial": up["first_t"] is None or (since is not None and up["first_t"] > since + 3600)
                   or (since is None and first_ts is not None and up["first_t"] > first_ts + 3600)},
    }
