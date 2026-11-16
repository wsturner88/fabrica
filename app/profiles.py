"""Printer profiles: what a printer MODEL has (a light, two nozzles, a chamber sensor ...), as data.

A profile is one JSON file in `profiles/` next to this module (shipped with Fabrica, never written to at run
time). `Registry` loads and validates them at start; a bad file is logged and skipped, it never stops the Hub.

Resolution for one printer (see `Registry.resolve`):
  1. the profile id saved on the printer (`profile_id`), if it still exists;
  2. else the built-in profile whose `types` contain the printer's type and one of whose `matches` is found in
     (model + " " + name).lower() - the LONGEST matching term wins (so "a1 mini" beats "a1"), ties by id;
  3. else the generic profile of that type (`generic-<type>`).
Then the printer's own `profile_overrides` (kept in printers.json, never here) are laid on top. Changing one
printer therefore never changes a profile file, another printer or another Hub.

Overrides are flat: any key of `has` ("light", "chamber", ...) plus "sensor_labels" ({"temperature_sensor X":
"Toolhead air"}) and "chamber_sensor" ("temperature_sensor X", or "" for none). `None` for a key means "back to
the profile's value".
"""
import json
import os
import re

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")

# the printer record's `type` (fleet_store.FAMILIES key) -> the profile type name used in the profile files
PROFILE_TYPE = {"moonraker": "klipper"}

CHAMBER_VALUES = ("none", "sensor", "heater")
FILAMENT_SYSTEMS = ("none", "ams", "ams_lite", "ams_2_pro", "mmu3", "toolchanger")
HAS_KEYS = ("light", "nozzles", "nozzle_max_c", "bed_mm", "bed_max_c", "chamber", "filament", "camera_builtin")
OVERRIDE_EXTRA_KEYS = ("sensor_labels", "chamber_sensor")
MAX_LABELS = 64
MAX_TEXT = 80


class ProfileError(ValueError):
    """A profile file or an override that is not valid. str(e) is plain English."""


def profile_type(record_type) -> str:
    t = str(record_type or "").strip().lower()
    return PROFILE_TYPE.get(t, t)


# ---------------------------------------------------------------- validation

def _num(key, v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= v <= hi:
        raise ProfileError(f"'{key}' must be a number from {lo:g} to {hi:g} (or empty for unknown).")
    return int(v) if float(v).is_integer() else float(v)


def clean_has_value(key, v):
    """One `has` value -> cleaned value (None = unknown). Raises ProfileError."""
    if key not in HAS_KEYS:
        raise ProfileError(f"'{key}' is not a printer feature Fabrica knows. Known: {', '.join(HAS_KEYS)}.")
    if v is None:
        return None
    if key in ("light", "camera_builtin"):
        if isinstance(v, bool):
            return v
        raise ProfileError(f"'{key}' must be true, false or empty (unknown).")
    if key == "nozzles":
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 16:
            raise ProfileError("'nozzles' must be a whole number from 1 to 16 (or empty for unknown).")
        return v
    if key == "nozzle_max_c":
        return _num(key, v, 50, 600)
    if key == "bed_max_c":
        return _num(key, v, 20, 300)
    if key == "bed_mm":
        if (not isinstance(v, (list, tuple)) or len(v) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in v)
                or any(not 10 <= x <= 3000 for x in v)):
            raise ProfileError("'bed_mm' must be two numbers, width and depth in mm, for example [250, 220].")
        return [int(x) if float(x).is_integer() else float(x) for x in v]
    if key == "chamber":
        if v in CHAMBER_VALUES:
            return v
        raise ProfileError("'chamber' must be \"none\", \"sensor\" (reads only) or \"heater\" (can be set).")
    if key == "filament":
        if not isinstance(v, dict) or set(v) - {"system", "slots"}:
            raise ProfileError("'filament' must look like {\"system\": \"ams\", \"slots\": 4}.")
        system = v.get("system", "none")
        slots = v.get("slots", 1)
        if system not in FILAMENT_SYSTEMS:
            raise ProfileError(f"'filament.system' must be one of: {', '.join(FILAMENT_SYSTEMS)}.")
        if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= 64:
            raise ProfileError("'filament.slots' must be a whole number from 1 to 64.")
        return {"system": system, "slots": slots}
    raise ProfileError(f"'{key}' cannot be set.")      # unreachable while HAS_KEYS and the branches agree


def clean_has(raw, partial=False) -> dict:
    """A whole `has` block (every key present, None = unknown) or, with partial=True, just the keys given."""
    if raw is None and partial:
        return {}
    if not isinstance(raw, dict):
        raise ProfileError("'has' must be an object such as {\"light\": true}.")
    out = {}
    for k, v in raw.items():
        out[k] = clean_has_value(k, v)
    if not partial:
        for k in HAS_KEYS:
            out.setdefault(k, None)
    return out


def _clean_text_list(key, v, lower=False):
    if not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() for x in v):
        raise ProfileError(f"'{key}' must be a list of words.")
    return [x.strip().lower() if lower else x.strip() for x in v]


def clean_profile(raw) -> dict:
    """A parsed profile file -> the cleaned profile. Raises ProfileError."""
    if not isinstance(raw, dict):
        raise ProfileError("the file must contain one JSON object.")
    pid = raw.get("id")
    if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", pid):
        raise ProfileError("'id' must be lower-case letters, digits, dot, dash or underscore (like \"prusa-core-one\").")
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ProfileError("'label' is missing.")
    types = _clean_text_list("types", raw.get("types"), lower=True)
    if not types:
        raise ProfileError("'types' needs at least one printer type.")
    matches = _clean_text_list("matches", raw.get("matches", []), lower=True)
    return {"id": pid, "label": label.strip(), "maker": str(raw.get("maker") or "Other").strip() or "Other",
            "types": types, "matches": matches, "has": clean_has(raw.get("has", {})),
            "notes": str(raw.get("notes") or "").strip(), "verified": raw.get("verified") is True}


def clean_overrides(raw, strict=True) -> dict:
    """Per-printer overrides -> cleaned dict holding only real overrides. strict=True (a user's edit): anything wrong
    raises ProfileError. strict=False (reading a stored record): wrong parts are dropped, never raised.
    `None` for a key means "no override" and is dropped."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        if strict:
            raise ProfileError("'profile_overrides' must be an object such as {\"light\": true}.")
        return {}
    out = {}
    for k, v in raw.items():
        try:
            if k in HAS_KEYS:
                val = clean_has_value(k, v)
                if val is not None:
                    out[k] = val
            elif k == "sensor_labels":
                if v is None:
                    continue
                if not isinstance(v, dict):
                    raise ProfileError("'sensor_labels' must look like {\"temperature_sensor X\": \"Toolhead air\"}.")
                if len(v) > MAX_LABELS:
                    raise ProfileError(f"'sensor_labels' can hold at most {MAX_LABELS} names.")
                labels = {}
                for sk, sv in v.items():
                    if not isinstance(sk, str) or not sk.strip() or len(sk) > MAX_TEXT:
                        raise ProfileError("A sensor name in 'sensor_labels' is empty or too long.")
                    if sv is None or (isinstance(sv, str) and not sv.strip()):
                        continue                                  # an empty label = back to the automatic name
                    if not isinstance(sv, str) or len(sv.strip()) > 40 or not sv.strip().isprintable():
                        raise ProfileError(f"The label for '{sk}' must be plain text of at most 40 characters.")
                    labels[sk.strip()] = sv.strip()
                if labels:
                    out["sensor_labels"] = labels
            elif k == "chamber_sensor":
                if v is None:
                    continue
                if not isinstance(v, str) or len(v.strip()) > MAX_TEXT or not v.strip().isprintable() and v.strip():
                    raise ProfileError("'chamber_sensor' must be the sensor's name (like \"temperature_sensor chamber\"), or empty for none.")
                out["chamber_sensor"] = v.strip()                # "" is kept: it means "this printer has no chamber sensor"
            else:
                raise ProfileError(f"'{k}' is not something a printer can override. Allowed: "
                                   f"{', '.join(HAS_KEYS + OVERRIDE_EXTRA_KEYS)}.")
        except ProfileError:
            if strict:
                raise
    return out


# ---------------------------------------------------------------- registry

def _generic(ptype) -> dict:
    """Built-in last resort when even the generic file is missing or broken."""
    return {"id": f"generic-{ptype}", "label": f"Generic {ptype} printer", "maker": "Generic", "types": [ptype], "matches": [],
            "has": {**{k: None for k in HAS_KEYS}, "nozzles": 1}, "notes": "Nothing is assumed.", "verified": False}


class Registry:
    def __init__(self, directory=None, log=print):
        self.directory = directory or PROFILE_DIR
        self.log = log
        self.profiles = {}          # id -> cleaned profile
        self.errors = []            # [(file, why)]
        self.load()

    def load(self):
        self.profiles, self.errors = {}, []
        try:
            names = sorted(n for n in os.listdir(self.directory) if n.lower().endswith(".json"))
        except OSError as e:
            self.errors.append((self.directory, str(e)))
            self.log(f"[profiles] cannot read {self.directory}: {e}")
            return
        for n in names:
            try:
                with open(os.path.join(self.directory, n), "r", encoding="utf-8") as f:
                    prof = clean_profile(json.load(f))
                if prof["id"] in self.profiles:
                    raise ProfileError(f"the id '{prof['id']}' is already used by another profile file.")
                self.profiles[prof["id"]] = prof
            except (OSError, ValueError, ProfileError) as e:      # json.JSONDecodeError is a ValueError
                self.errors.append((n, str(e)))
                self.log(f"[profiles] skipping {n}: {e}")

    # ---- reading

    def get(self, pid):
        return self.profiles.get(pid) if isinstance(pid, str) else None

    def public_list(self) -> list:
        rows = sorted(self.profiles.values(), key=lambda p: (p["maker"].casefold(), p["label"].casefold()))
        out = []
        for p in rows:
            printer_types = sorted({k for k, v in PROFILE_TYPE.items() if v in p["types"]} | {t for t in p["types"] if t not in PROFILE_TYPE.values()})
            out.append({"id": p["id"], "label": p["label"], "maker": p["maker"], "types": list(p["types"]),
                        "printer_types": printer_types, "has": json.loads(json.dumps(p["has"])),
                        "verified": p["verified"], "notes": p["notes"]})
        return out

    def fits(self, profile, record_type) -> bool:
        return profile_type(record_type) in profile["types"]

    # ---- matching

    def match(self, record_type, model="", name="") -> dict:
        """The profile picked by matching alone (no saved choice, no overrides)."""
        ptype = profile_type(record_type)
        text = f"{model or ''} {name or ''}".lower()
        best = None
        for p in self.profiles.values():
            if ptype not in p["types"]:
                continue
            for term in p["matches"]:
                if term and term in text:
                    key = (len(term), p["id"])
                    if best is None or key[0] > best[0][0] or (key[0] == best[0][0] and key[1] < best[0][1]):
                        best = (key, p)
        if best:
            return best[1]
        return self.profiles.get(f"generic-{ptype}") or _generic(ptype)

    def resolve(self, rec) -> dict:
        """Everything about one printer record's profile (see CONTRACT section 1/2). `rec` is a stored printer
        record (or anything with type / model / name / profile_id / profile_overrides). Never raises."""
        rec = rec if isinstance(rec, dict) else {}
        saved = self.get(rec.get("profile_id"))
        if saved is not None and not self.fits(saved, rec.get("type")):
            saved = None                                       # a hand-edited file naming a profile for another kind of printer
        prof = saved or self.match(rec.get("type"), rec.get("model"), rec.get("name"))
        overrides = clean_overrides(rec.get("profile_overrides"), strict=False)
        has = json.loads(json.dumps(prof["has"]))
        overridden = []
        for k in HAS_KEYS:
            if k in overrides:
                if overrides[k] != has.get(k):
                    overridden.append(k)
                has[k] = overrides[k]
        return {"id": prof["id"], "label": prof["label"], "auto": saved is None, "has": has,
                "overridden": overridden, "verified": prof["verified"] and not overridden,
                "overrides": overrides, "notes": prof["notes"]}


_DEFAULT = None


def default_registry() -> Registry:
    """The shared registry, loaded on first use from the shipped profiles/ folder."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Registry()
    return _DEFAULT
