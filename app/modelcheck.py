"""'Was this file sliced for THIS printer?' - the pure part (no network, no printer, no server import).

A job sliced for another printer model can crash the toolhead, scrape the bed or run past the build
volume, so the Hub checks the model the slicer recorded in the file against the model of the printer
the job is about to start on. The check is advisory-by-evidence: when either side is not known the
answer is "unknown" and nothing is blocked.

Sources for the file side
  * Bambu .3mf: Metadata/slice_info.config carries <metadata key="printer_model_id" value="C12"/>
    (written by Bambu Studio's bbs_3mf.cpp, PRINTER_MODEL_ID_ATTR). The codes are the "model_id" of
    each Bambu Studio machine profile - see BAMBU_MODEL_IDS below. Metadata/project_settings.config
    also carries "printer_model": "Bambu Lab P1S" and is used when the code is not in the table.
  * gcode (PrusaLink / OctoPrint / Klipper): the slicer's own comments in the file head -
    `; printer_model = MK4S`, `; printer_settings_id = ...`, Prusa's `M862.3 P "MK4S"` printer check,
    and the same keys inside a .bgcode header. The head is only the first ~1 MB, so a slicer that
    writes its config block at the END of a big file gives "unknown" unless it also has the
    M862.3 line (PrusaSlicer's default start gcode does).
Target side: the printer's model from the fleet store, or for Bambu the serial-number prefix.
"""
import re

# Bambu Studio `printer_model_id` -> (short model name, full name).
# Source: https://github.com/bambulab/BambuStudio/tree/master/resources/profiles/BBL/machine
# ("model_id" in each `Bambu Lab <model>.json`), read 2026-10-11. Verified = value seen in that file.
BAMBU_MODEL_IDS = {
    "BL-P001": ("X1 Carbon", "Bambu Lab X1 Carbon"),   # verified
    "BL-P002": ("X1", "Bambu Lab X1"),                 # verified
    "C13": ("X1E", "Bambu Lab X1E"),                   # verified
    "C11": ("P1P", "Bambu Lab P1P"),                   # verified
    "C12": ("P1S", "Bambu Lab P1S"),                   # verified
    "N1": ("A1 mini", "Bambu Lab A1 mini"),            # verified
    "N2S": ("A1", "Bambu Lab A1"),                     # verified
    "O1D": ("H2D", "Bambu Lab H2D"),                   # verified
    "O1E": ("H2D Pro", "Bambu Lab H2D Pro"),           # verified
    "O1S": ("H2S", "Bambu Lab H2S"),                   # verified
    "O1C2": ("H2C", "Bambu Lab H2C"),                  # verified (an older "O1C" may exist in early files: unverified)
    "O1C": ("H2C", "Bambu Lab H2C"),                   # UNVERIFIED alias
    "N7": ("P2S", "Bambu Lab P2S"),                    # verified
    "N6": ("X2D", "Bambu Lab X2D"),                    # verified
    "N9": ("A2L", "Bambu Lab A2L"),                    # verified
}

# Words that carry no model information.
_NOISE = ("ORIGINAL PRUSA", "BAMBU LAB", "BAMBULAB", "PRUSA RESEARCH", "PRUSA", "CREALITY", "ELEGOO", "ANYCUBIC")
# "Bambu Lab P1S 0.4 nozzle", "Original Prusa MK4S HF0.4 nozzle", "... 0.4mm nozzle"
_NOZZLE_RE = re.compile(r"\b(?:HF|HT|NS)?\s*\d(?:\.\d+)?\s*(?:MM)?\s*NOZZLE\b")
# Same machine, different spelling / hardware revision that runs the same gcode.
_ALIASES = {
    "X1CARBON": "X1C", "X1CARB": "X1C", "A1MINI": "A1MINI",
    "MK4": "MK4", "MK4S": "MK4", "MK4IS": "MK4",
    "MK3": "MK3", "MK3S": "MK3", "MK3S+": "MK3", "MK3SPLUS": "MK3", "MK3SMMU3": "MK3", "MK3MMU3": "MK3",
    "MINI": "MINI", "MINI+": "MINI", "MINIPLUS": "MINI",
}
# Canonical keys of the Bambu models (X1 Carbon is "X1C"); anything else is a free-text model.
_BAMBU_KEYS = {"X1C", "X1", "X1E", "P1P", "P1S", "A1MINI", "A1", "H2D", "H2DPRO", "H2S", "H2C", "P2S", "X2D", "A2L"}

MODEL_OVERRIDE_TAG = "model_override"       # appended to the confirm string when the human overrides


def model_for_code(code):
    """(short, full) for a Bambu printer_model_id, or None when the code is not in the table."""
    return BAMBU_MODEL_IDS.get(str(code or "").strip().upper())


def canon(name) -> str:
    """Canonical comparison key for a model name. '' when there is nothing usable."""
    s = str(name or "").upper().replace("_", " ").strip()
    if not s:
        return ""
    s = _NOZZLE_RE.sub(" ", s)
    for w in _NOISE:
        s = s.replace(w, " ")
    s = re.sub(r"[^A-Z0-9+. ]", " ", s)
    s = re.sub(r"\s+", "", s).strip(".")
    s = s.replace("MK3.9", "MK39").replace("MK3.5", "MK35")
    return _ALIASES.get(s, s)


def _clean_display(text, limit: int = 40) -> str:
    """Printer-model text from a FILE is untrusted: keep only boring characters and cap the length."""
    s = re.sub(r"[^A-Za-z0-9 .+\-]", "", str(text or "")).strip()
    return s[:limit].strip()


def _short(name: str) -> str:
    """'Bambu Lab A1' -> 'A1'; 'Original Prusa MK4S' -> 'MK4S'; unknown brands stay as typed."""
    s = _clean_display(name)
    for w in ("Original Prusa", "Bambu Lab", "BambuLab", "Prusa Research", "Prusa"):
        s = re.sub(re.escape(w), "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s or _clean_display(name)


def same_model(a: str, b: str):
    """True / False when both are usable, None when either is unknown."""
    ka, kb = canon(a), canon(b)
    if not ka or not kb:
        return None
    if ka == kb:
        return True
    if ka in _BAMBU_KEYS or kb in _BAMBU_KEYS:
        return False        # a Bambu model is only ever itself - no fuzzy matching (A1 vs A1 mini)
    # free-text models: 'Voron 2.4' vs 'Voron 2.4 300mm' are the same machine
    return (ka in kb and len(ka) >= 4) or (kb in ka and len(kb) >= 4)


# ----------------------------------------------------------------- the file side

def file_model_bambu(meta) -> str:
    """The model name a Bambu .3mf says it was sliced for ('' when it does not say).
    `meta` is the parse_3mf_meta dict (cached): printer_model_id, optionally printer_model."""
    if not isinstance(meta, dict):
        return ""
    hit = model_for_code(meta.get("printer_model_id"))
    if hit:
        return hit[1]
    return _clean_display(meta.get("printer_model")) if canon(meta.get("printer_model")) else ""


_HEAD_PATTERNS = (
    re.compile(rb"^[; \t]*printer_model[ \t]*=[ \t]*([^\r\n\x00;]+)", re.M),               # ; printer_model = MK4S
    re.compile(rb"printer_model[ \t]*=[ \t]*([^\r\n\x00;]+)"),                           # .bgcode metadata block
    re.compile(rb'^M862\.3[ \t]+P[ \t]*"([^"\r\n]+)"', re.M),                              # printer model check
    re.compile(rb"^[; \t]*printer_settings_id[ \t]*=[ \t]*([^\r\n\x00;]+)", re.M),          # ; printer_settings_id = ...
)


def gcode_head_model(data) -> str:
    """The printer model named in the first part of a gcode / bgcode file ('' when none)."""
    if not data:
        return ""
    head = bytes(data[:1048576])
    for rx in _HEAD_PATTERNS:
        for m in rx.finditer(head):
            val = m.group(1).decode("utf8", "ignore").strip()
            if canon(val) and not val.startswith("["):          # '[printer_model]' = unexpanded template
                return _clean_display(val)
    return ""


# ----------------------------------------------------------------- the verdict

def target_model_for(prn) -> str:
    """The model of a fleet printer: its stored model, or for Bambu the serial-number prefix."""
    model = str(getattr(prn, "model", "") or "").strip()
    if canon(model):
        return model
    if getattr(prn, "kind", "") == "bambu":
        try:
            from fleet_store import bambu_model_from_serial
            return bambu_model_from_serial(getattr(prn, "serial", "") or "")
        except Exception:
            return ""
    return ""


def check(sliced_for: str, target_model: str) -> dict:
    """The fields /suggest returns. model_match is True, False or the string 'unknown'."""
    sliced, target = _clean_display(sliced_for), _clean_display(target_model)
    verdict = same_model(sliced, target)
    s_short, t_short = _short(sliced), _short(target)
    out = {"sliced_for": s_short or None, "target_model": t_short or None,
           "target_model_name": target or None,
           "model_match": "unknown" if verdict is None else bool(verdict), "model_warning": None}
    if verdict is False:
        out["model_warning"] = (f"THIS FILE WAS SLICED FOR {s_short.upper()} — YOU ARE ON {t_short.upper()}. "
                                "Printing it could damage the printer.")
    elif verdict is None:
        out["model_warning"] = "Couldn't tell which printer this was sliced for."
    return out
