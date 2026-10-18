"""Cost model for the job log: what did this print really cost?

Pure functions over job dicts (the rows joblog.py returns). No database, no printers, no
network - so it can be tested, and re-priced, on its own. Stdlib only.

Per attempt (one row of the job log):

    material    = sum over filaments of  grams_used_i / 1000 * price_per_kg(type_i)
    electricity = hours * watts / 1000 * kwh_price
    wear        = hours * wear_per_hour
    job cost    = material                          (the default: filament only)
    job cost    = material + electricity + wear     (when "include print time costs" is on)

  * The default total is the FILAMENT cost alone, because power and machine wear come from
    rates most people never set. Turn on `costs.include_machine_costs` (Rates.include_machine),
    or pass include_machine=True to a function here, to add them. `material`, `electricity` and
    `wear` are always computed and returned; `total_with_machine` is always the full sum.

  * grams_used is the slicer's estimate scaled by how far the print got: a print that failed at
    40 % used 40 % of the plate's filament (a finished print uses 100 %). Filament use is not
    perfectly linear in progress, but it is the best number available without a scale.
  * hours is the real wall-clock duration of the attempt (pauses included - the machine and
    the filament are tied up either way).
  * watts is looked up per printer (exact printer name, then model name), else the generic default.

"True" cost per part for a file (or printer, or period) charges the failures to the good parts:

    true cost per part  =  cost of ALL attempts  /  good parts produced
                        =  (sum of finished-job costs + sum of failed/cancelled-job costs)
                           / (sum of `parts` over finished jobs)

so one failed attempt among four makes every good part carry a quarter of its cost
(`waste per part`). A file with attempts but no finished job has no per-part figure (None) -
its sunk cost is still reported. Cancelled jobs count as failures here (the filament and the
hours are gone either way). Jobs still running or of unknown outcome are left out of cost
totals so a half-finished row never distorts a price.

Rates are applied when you ask, not when the job is logged: change a price and the whole
history re-prices.
"""

import datetime
import math

# ----------------------------------------------------------------------------- rates

DEFAULT_PRICE_PER_KG = {"PLA": 20.0, "PETG": 22.0, "ABS": 22.0, "ASA": 25.0, "TPU": 35.0}
DEFAULT_WATTS = {"P1S": 120.0, "A1": 90.0, "MK4": 100.0}
# Filament density, g/cm3 - only used to turn a filament LENGTH (OctoPrint / PrusaLink /
# Klipper report mm) into grams.
DENSITY = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "PC": 1.20,
           "PA": 1.14, "PVA": 1.23}
DEFAULT_DENSITY = 1.24

FAILED_RESULTS = ("failed", "cancelled")
CLOSED_RESULTS = ("finished", "failed", "cancelled", "unknown")


def _f(v, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _truthy(v, default=False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    try:
        return bool(v)
    except Exception:
        return default


def parse_pairs(value) -> dict:
    """Turn ["PLA=20", "PETG=22"] (the Settings list_str shape), "PLA=20, PETG=22", or a dict into
    {"PLA": 20.0, "PETG": 22.0}. Names are upper-cased; bad entries are skipped."""
    out = {}
    if value is None:
        return out
    if isinstance(value, dict):
        items = list(value.items())
    else:
        if isinstance(value, str):
            value = [p for p in value.replace(";", ",").split(",")]
        items = []
        for entry in value:
            if isinstance(entry, str) and "=" in entry:
                k, v = entry.split("=", 1)
                items.append((k, v))
    for k, v in items:
        key = str(k).strip().upper()
        num = _f(v)
        if key and num is not None and num >= 0:
            out[key] = num
    return out


class Rates:
    """Prices and power figures. `Rates()` is usable as-is; override anything by keyword,
    or build one from the Hub settings with `Rates.from_settings(get)`."""

    def __init__(self, currency="$", per_kg=None, default_per_kg=25.0, kwh_price=0.15,
                 watts=None, default_watts=120.0, wear_per_hour=0.30, window_n=20,
                 include_machine=False):
        self.include_machine = _truthy(include_machine)      # add power + machine wear to totals
        self.currency = str(currency or "$")
        self.per_kg = dict(DEFAULT_PRICE_PER_KG)
        self.per_kg.update(parse_pairs(per_kg))
        self.default_per_kg = max(0.0, _f(default_per_kg, 25.0))
        self.kwh_price = max(0.0, _f(kwh_price, 0.15))
        self.watts = dict(DEFAULT_WATTS)
        self.watts.update(parse_pairs(watts))
        self.default_watts = max(0.0, _f(default_watts, 120.0))
        self.wear_per_hour = max(0.0, _f(wear_per_hour, 0.30))
        try:
            self.window_n = max(1, int(window_n))
        except (TypeError, ValueError):
            self.window_n = 20

    @classmethod
    def from_settings(cls, get):
        """`get(key)` -> value, e.g. SettingsStore.get. Missing keys fall back to defaults."""
        def g(key, default=None):
            try:
                v = get(key)
            except Exception:
                return default
            return default if v is None or v == "" else v
        return cls(currency=g("costs.currency", "$"),
                   per_kg=g("costs.filament_prices"), default_per_kg=g("costs.default_price_per_kg", 25.0),
                   kwh_price=g("costs.kwh_price", 0.15),
                   watts=g("costs.printer_watts"), default_watts=g("costs.default_watts", 120.0),
                   wear_per_hour=g("costs.wear_per_hour", 0.30), window_n=g("costs.window_n", 20),
                   include_machine=g("costs.include_machine_costs", False))

    def to_dict(self) -> dict:
        return {"currency": self.currency, "per_kg": dict(self.per_kg),
                "default_per_kg": self.default_per_kg, "kwh_price": self.kwh_price,
                "watts": dict(self.watts), "default_watts": self.default_watts,
                "wear_per_hour": self.wear_per_hour, "window_n": self.window_n,
                "include_machine": self.include_machine}


def includes_machine(rates: Rates, include_machine=None) -> bool:
    """Does a total include power + machine wear? An explicit True / False wins (a per-request
    override); None means "use the setting" (rates.include_machine, default off)."""
    if include_machine is None:
        return bool(getattr(rates, "include_machine", False))
    return _truthy(include_machine)


def price_per_kg(rates: Rates, material) -> float:
    """Price for a filament type. Exact key first, then the longest key that is a prefix
    ("PLA+" / "PLA-CF" -> PLA, "PETG-CF" -> PETG), else the default price."""
    m = str(material or "").strip().upper()
    if not m:
        return rates.default_per_kg
    if m in rates.per_kg:
        return rates.per_kg[m]
    best = None
    for k in rates.per_kg:
        if m.startswith(k) and (best is None or len(k) > len(best)):
            best = k
    return rates.per_kg[best] if best else rates.default_per_kg


def watts_for(rates: Rates, printer, model=None) -> float:
    """Average draw while printing. Exact printer name wins, then the longest key found in the
    model name, then in the printer name, else the generic default."""
    name = str(printer or "").strip().upper()
    mod = str(model or "").strip().upper()
    if name in rates.watts:
        return rates.watts[name]
    for hay in (mod, name):
        if not hay:
            continue
        for k in sorted(rates.watts, key=len, reverse=True):
            if k in hay:
                return rates.watts[k]
    return rates.default_watts


def grams_from_mm(mm, material=None, diameter_mm=1.75):
    """Filament length -> grams (OctoPrint / PrusaLink / Klipper report length)."""
    mm = _f(mm)
    if mm is None or mm <= 0:
        return None
    d = DENSITY.get(str(material or "").strip().upper(), DEFAULT_DENSITY)
    r_cm = (diameter_mm / 10.0) / 2.0
    return round(math.pi * r_cm * r_cm * (mm / 10.0) * d, 2)


# ----------------------------------------------------------------------------- one job

def usage_fraction(job):
    """How much of the plate's filament this attempt used (0..1), or None if unknowable."""
    if job.get("result") == "finished":
        return 1.0
    pc = _f(job.get("percent"))
    if pc is None:
        return None
    return min(1.0, max(0.0, pc / 100.0))


def grams_used(job):
    """Grams actually consumed: estimate x fraction. None when the estimate or the progress
    is missing (a failed job with no recorded percent is not guessed at)."""
    est = _f(job.get("grams_est"))
    fr = usage_fraction(job)
    if est is None or fr is None:
        return None
    return round(est * fr, 3)


def material_grams(job) -> dict:
    """{material type: grams used}. Uses the per-filament list when it is complete, else puts
    everything on the job's main material."""
    g = grams_used(job)
    if g is None:
        return {}
    fr = usage_fraction(job)
    fils = job.get("filaments") or []
    parts = {}
    complete = bool(fils) and all(_f(f.get("used_g")) is not None for f in fils)
    if complete:
        for f in fils:
            t = (f.get("type") or job.get("material") or "?").upper()
            parts[t] = parts.get(t, 0.0) + _f(f["used_g"]) * fr
        total = sum(parts.values())
        # slicer per-filament grams occasionally disagree slightly with the plate weight;
        # keep the plate weight authoritative and rescale
        if total > 0 and abs(total - g) > 0.01:
            k = g / total
            parts = {t: v * k for t, v in parts.items()}
        return {t: round(v, 3) for t, v in parts.items()}
    return {(job.get("material") or "?").upper(): g}


MAX_PLAUSIBLE_JOB_S = 14 * 86400      # no single print runs longer than this


def duration_plausible(duration_s, est_seconds=None) -> bool:
    """False for a duration that can only come from a wall-clock step (a Pi without an RTC that
    gets its time from NTP mid-print): over two weeks, or more than ten times the slicer's estimate
    (and more than a day over it)."""
    d, est = _f(duration_s), _f(est_seconds)
    if d is None or d < 0:
        return False
    if d > MAX_PLAUSIBLE_JOB_S:
        return False
    if est and est > 0 and d > max(est * 10, est + 86400):
        return False
    return True


def machine_hours(job):
    """Wall-clock hours of the attempt; falls back to estimate x progress when the duration
    was not captured or cannot be right (see duration_plausible)."""
    d = _f(job.get("duration_s"))
    est, fr = _f(job.get("est_seconds")), usage_fraction(job)
    if d is not None and duration_plausible(d, est):
        return d / 3600.0
    if est is not None and fr is not None:
        return est * fr / 3600.0
    return None


def job_cost(job: dict, rates: Rates, include_machine=None) -> dict:
    """Cost of one attempt. `total` (and `per_part`) is the filament cost unless machine costs are
    on (include_machine=True, or None + the setting): then it is material + electricity + wear.
    `material` / `electricity` / `wear` / `total_with_machine` are always filled in.
    `partial` is True when an input was missing (no grams or no duration) and that component counts as 0."""
    mg = material_grams(job)
    material = sum(g / 1000.0 * price_per_kg(rates, t) for t, g in mg.items())
    hours = machine_hours(job)
    watts = watts_for(rates, job.get("printer"), job.get("model"))
    kwh = (hours or 0.0) * watts / 1000.0
    electricity = kwh * rates.kwh_price
    wear = (hours or 0.0) * rates.wear_per_hour
    with_machine = material + electricity + wear
    incl = includes_machine(rates, include_machine)
    total = with_machine if incl else material
    parts = job.get("parts") or 1
    good = job.get("result") == "finished"
    return {
        "material": round(material, 4), "electricity": round(electricity, 4), "wear": round(wear, 4),
        "total": round(total, 4), "total_with_machine": round(with_machine, 4),
        "includes_machine_costs": incl, "grams_used": grams_used(job), "hours": None if hours is None else round(hours, 4),
        "kwh": round(kwh, 4), "watts": watts,
        "per_part": round(total / parts, 4) if good and parts else None,
        "per_part_with_machine": round(with_machine / parts, 4) if good and parts else None,
        "partial": (not mg) or hours is None,
    }


# ----------------------------------------------------------------------------- many jobs

def _closed(job) -> bool:
    return job.get("ended_at") is not None and job.get("result") in CLOSED_RESULTS


def aggregate(jobs, rates: Rates, include_machine=None) -> dict:
    """Roll a list of jobs up: counts, success rate, hours, grams, cost split, and the true
    cost per part (see the module docstring). Running jobs are counted but not costed;
    `unknown` jobs are counted but excluded from cost and success rate.
    Totals, wasted_on_failures and the per-part figures are filament-only unless machine costs are
    on (see job_cost); `total_with_machine` / `wasted_with_machine` / `*_with_machine` always carry the full sum."""
    incl = includes_machine(rates, include_machine)
    c = {"jobs": 0, "finished": 0, "failed": 0, "cancelled": 0, "unknown": 0, "running": 0}
    mat = elec = wear = 0.0
    good_cost = fail_cost = 0.0
    good_full = fail_full = 0.0
    hours = grams = 0.0
    good_parts = 0
    partial = 0
    for j in jobs:
        c["jobs"] += 1
        r = j.get("result")
        if r == "running" or j.get("ended_at") is None:
            c["running"] += 1
            continue
        if r not in c:
            r = "unknown"
        c[r] += 1
        if r == "unknown":
            continue
        jc = job_cost(j, rates, incl)
        mat += jc["material"]
        elec += jc["electricity"]
        wear += jc["wear"]
        hours += jc["hours"] or 0.0
        grams += jc["grams_used"] or 0.0
        partial += 1 if jc["partial"] else 0
        if r == "finished":
            good_cost += jc["total"]
            good_full += jc["total_with_machine"]
            good_parts += int(j.get("parts") or 1)
        else:
            fail_cost += jc["total"]
            fail_full += jc["total_with_machine"]
    decided = c["finished"] + c["failed"] + c["cancelled"]
    total = good_cost + fail_cost
    total_full = good_full + fail_full
    out = dict(c)
    out.update({
        "success_rate": round(c["finished"] / decided, 4) if decided else None,
        "hours": round(hours, 3), "grams_used": round(grams, 2), "good_parts": good_parts,
        "cost": {"material": round(mat, 2), "electricity": round(elec, 2), "wear": round(wear, 2),
                 "total": round(total, 2), "total_with_machine": round(total_full, 2),
                 "wasted_on_failures": round(fail_cost, 2), "wasted_with_machine": round(fail_full, 2),
                 "includes_machine_costs": incl},
        "includes_machine_costs": incl,
        "clean_cost_per_part": round(good_cost / good_parts, 4) if good_parts else None,
        "true_cost_per_part": round(total / good_parts, 4) if good_parts else None,
        "waste_per_part": round(fail_cost / good_parts, 4) if good_parts else None,
        "true_cost_per_part_with_machine": round(total_full / good_parts, 4) if good_parts else None,
        "clean_cost_per_part_with_machine": round(good_full / good_parts, 4) if good_parts else None,
        "waste_per_part_with_machine": round(fail_full / good_parts, 4) if good_parts else None,
        "incomplete_cost_rows": partial,
    })
    return out


def file_key(name) -> str:
    """Normalise a job / file name so `Benchy`, `benchy.gcode.3mf` and `BENCHY.3mf` match."""
    n = str(name or "").strip().lower()
    for suf in (".gcode.3mf", ".3mf", ".bgcode", ".gcode"):
        if n.endswith(suf):
            n = n[: -len(suf)]
            break
    return n


def file_label(name) -> str:
    """The name to SHOW for a file: what the slicer / printer called it, case kept, without the print-file ending
    (`file_key` is the lower-cased matching key and is never shown)."""
    n = str(name or "").strip()
    low = n.lower()
    for suf in (".gcode.3mf", ".3mf", ".bgcode", ".gcode"):
        if low.endswith(suf):
            return n[: -len(suf)]
    return n


def file_costs(jobs, rates: Rates, last_n=None, include_machine=None) -> dict:
    """Per-file figures over the last N attempts (newest first by start/end time).
    Costs follow include_machine as in aggregate()."""
    n = last_n or rates.window_n
    closed = [j for j in jobs if _closed(j) and j.get("result") != "unknown"]
    closed.sort(key=lambda j: (j.get("ended_at") or j.get("started_at") or 0), reverse=True)
    window = closed[:n]
    agg = aggregate(window, rates, include_machine)
    fin = [j for j in window if j.get("result") == "finished"]
    parts_total = agg["good_parts"]
    agg.update({
        "attempts": len(window), "window_n": n,
        "avg_parts_per_print": round(parts_total / len(fin), 2) if fin else None,
        "grams_per_part": round(sum((grams_used(j) or 0) for j in fin) / parts_total, 3) if parts_total else None,
        "hours_per_part": round(sum((machine_hours(j) or 0) for j in fin) / parts_total, 4) if parts_total else None,
    })
    return agg


def material_series(jobs, bucket="day", tz=None) -> list:
    """Grams used per day / week (ISO, Monday) / month, by material, oldest first.
    Buckets with no print are omitted. Dates are in `tz` (a tzinfo; default UTC)."""
    tz = tz or datetime.timezone.utc
    buckets = {}
    for j in jobs:
        if not _closed(j) or j.get("result") == "unknown":
            continue
        ts = j.get("ended_at") or j.get("started_at")
        mg = material_grams(j)
        if ts is None or not mg:
            continue
        d = datetime.datetime.fromtimestamp(ts, tz).date()
        if bucket == "month":
            key = f"{d.year:04d}-{d.month:02d}"
        elif bucket == "week":
            monday = d - datetime.timedelta(days=d.weekday())
            key = monday.isoformat()
        else:
            key = d.isoformat()
        b = buckets.setdefault(key, {"bucket": key, "grams": 0.0, "by_material": {}})
        for t, g in mg.items():
            b["grams"] += g
            b["by_material"][t] = round(b["by_material"].get(t, 0.0) + g, 3)
    out = [buckets[k] for k in sorted(buckets)]
    for b in out:
        b["grams"] = round(b["grams"], 2)
    return out
