"""Temperature cards for the fleet view: ONE place that turns what a driver reports into the `temps` and
`sensors` lists of GET /api/fleet.

A driver may offer `temp_readings()` (no network: it reads what its last poll already fetched) returning

    {"nozzles": [{"c", "target"}, ...],        # one per independently heated nozzle, first = the main one
     "bed": {"c", "target"},
     "chamber": {"c", "target"},               # a driver-reported chamber (Bambu, OctoPrint)
     "sensors": [{"id", "kind", "name", "c", "target"}]}   # Klipper: every discovered heater_generic / sensor

Every key is optional. Without `temp_readings()` the nozzle and bed come from the snapshot, as before.
"""
import re

ROLES = ("chamber", "toolhead", "board", "host", "probe", "other")
DEFAULT_LABELS = {"chamber": "Chamber", "toolhead": "Toolhead board", "board": "Main board", "host": "Pi", "probe": "Probe"}

# (role, substrings anywhere in the name, whole words). The first role with a hit wins; "probe" is looked at before
# "board" so a probe's own mcu ("btt_eddy_mcu") is a probe, not the main board.
_ROLE_RULES = (
    ("chamber", ("chamber", "enclosure"), ()),
    ("toolhead", ("ebb", "sb22", "toolhead", "nhk"), ("can",)),
    ("probe", ("eddy", "probe", "beacon", "cartographer"), ()),
    ("board", ("mcu", "octopus", "board", "spider", "manta", "skr"), ()),
    ("host", ("host", "rpi", "cpu"), ("pi",)),
)
_KIND_ROLE = {"temperature_host": "host", "temperature_probe": "probe"}   # used only when the name says nothing


def _words(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return set(re.sub(r"\d+$", "", w) or w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w)     # "can0" -> "can"


def classify(kind, name) -> str:
    """Role of a Klipper sensor from its object kind and name (case-insensitive)."""
    low, words = str(name).lower(), _words(name)
    for role, subs, whole in _ROLE_RULES:
        if any(s in low for s in subs) or any(w in words for w in whole):
            return role
    return _KIND_ROLE.get(kind, "other")


def _num(v):
    return round(float(v), 1) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _target(v):
    return _num(v)


def _clean_sensor(s):
    if not isinstance(s, dict) or not s.get("id") or _num(s.get("c")) is None:
        return None
    kind = str(s.get("kind") or str(s["id"]).partition(" ")[0])
    name = str(s.get("name") or str(s["id"]).partition(" ")[2] or s["id"])
    return {"id": str(s["id"]), "kind": kind, "name": name, "c": _num(s["c"]), "target": _target(s.get("target"))}


def _unique_labels(rows, labels):
    """Same default label twice (two sensors both called Pi): keep both, tell them apart. A label the owner chose is
    left exactly as typed."""
    groups = {}
    for r in rows:
        if r["id"] not in labels:
            groups.setdefault(r["label"], []).append(r)
    for label, same in groups.items():
        if len(same) > 1:
            distinct_names = len({x["name"] for x in same}) > 1
            for r in same:
                r["label"] = f"{label} ({r['name'] if distinct_names else r['kind']})"


def build(raw, snap, has=None, overrides=None, record_type="") -> tuple:
    """-> (temps, sensors). `raw` = the driver's temp_readings() (or None), `snap` = the snapshot being shown,
    `has` = the effective profile `has`, `overrides` = the printer's own profile_overrides. Never raises."""
    try:
        return _build(raw, snap, has or {}, overrides or {}, str(record_type or "").lower())
    except Exception:
        return [], []


def _build(raw, snap, has, overrides, rtype):
    raw = raw if isinstance(raw, dict) else {}
    snap = snap if isinstance(snap, dict) else {}
    labels = overrides.get("sensor_labels") if isinstance(overrides.get("sensor_labels"), dict) else {}

    # ---- nozzles
    noz = raw.get("nozzles")
    if not isinstance(noz, list) or not noz:
        noz = [{"c": snap.get("nozzle_c"), "target": snap.get("nozzle_target_c")}]
    temps = []
    for i, n in enumerate(noz, 1):
        n = n if isinstance(n, dict) else {}
        c = _num(n.get("c"))
        if c is None:
            continue
        temps.append({"id": "nozzle" if i == 1 else f"nozzle{i}", "label": "Nozzle" if len(noz) == 1 else f"Nozzle {i}",
                      "kind": "nozzle", "c": c, "target_c": _target(n.get("target"))})

    # ---- bed
    bed = raw.get("bed") if isinstance(raw.get("bed"), dict) else {"c": snap.get("bed_c"), "target": snap.get("bed_target_c")}
    if _num(bed.get("c")) is not None:
        temps.append({"id": "bed", "label": "Bed", "kind": "bed", "c": _num(bed.get("c")), "target_c": _target(bed.get("target"))})

    # ---- discovered sensors (Klipper), role by name
    rows = []
    for s in raw.get("sensors") or []:
        s = _clean_sensor(s)
        if s:
            s["role"] = classify(s["kind"], s["name"])
            rows.append(s)

    # ---- chamber
    chamber_has = has.get("chamber")
    chamber = None
    if "chamber_sensor" in overrides:                      # the owner's choice wins; "" = this printer has none
        want = overrides["chamber_sensor"]
        chamber = next((r for r in rows if r["id"] == want), None) if want else None
    elif chamber_has != "none":
        cands = [r for r in rows if r["role"] == "chamber"]
        cands.sort(key=lambda r: (r["kind"] != "heater_generic", 0))        # a real heater first (it has a target)
        chamber = cands[0] if cands else None
        if chamber is None and isinstance(raw.get("chamber"), dict):
            # a printer-reported chamber: Bambu reports a number for every model, so it is believed only when the
            # profile says there is a sensor; other drivers report it only when the printer really has one
            if rtype == "bambu":
                ok = chamber_has in ("sensor", "heater")
            else:
                ok = True
            chamber = raw["chamber"] if ok else None
    if chamber is not None and _num(chamber.get("c")) is not None:
        cid = chamber.get("id") if chamber.get("id") in labels else None
        temps.append({"id": "chamber", "label": labels.get(cid) or "Chamber", "kind": "chamber", "c": _num(chamber["c"]),
                      "target_c": _target(chamber.get("target")) if chamber.get("kind") == "heater_generic" else None})
        chamber_id = chamber.get("id")
        rows = [r for r in rows if r["id"] != chamber_id]
    # any other sensor that merely sounds like a chamber is an ordinary reading ("chamber" is taken)
    sensors = []
    for r in rows:
        role = "other" if r["role"] == "chamber" else r["role"]
        sensors.append({"id": r["id"], "label": labels.get(r["id"]) or DEFAULT_LABELS.get(role) or r["name"], "role": role,
                        "c": r["c"], "kind": r["kind"], "name": r["name"]})
    _unique_labels(sensors, labels)
    for s in sensors:
        s.pop("kind", None)
        s.pop("name", None)
    return temps, sensors
