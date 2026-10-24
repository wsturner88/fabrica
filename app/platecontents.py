"""What is on a plate: how many of which object, read from the cancel-object labels a slicer writes.

A slicer that is asked to "label objects" writes the name of every object into the file so a printer can skip ONE of them
mid-print. The Hub reads those labels to answer "what is on this plate?", which lets a person (or an agent) name a file
by the quantity it holds ("4x Hose Clip + 2x Hook"). Every reader returns the same plain dict, and NEVER raises:

    {"labels":  "present" | "missing" | "unknown",
     "source":  "slice_info" | "m486" | "exclude_object" | "objects_info" | "comments" | None,
     "total":   int | None,                       # objects on the plate
     "groups":  [{"name": "Hose Clip", "count": 4}, ...],   # most first
     "summary": "4× Hose Clip + 2× Hook"}         # "" unless labels are present and counted

  present   labels were found and counted. `total` None with labels "present" means the file has labels but the part of
            it that was read cannot give the quantity (never guessed).
  missing   DEFINITIVE: the whole file, or the whole section where this slicer must write its labels, was read and there
            are none (or a Bambu slice_info lists no <object>). Only claimed for a slicer that identifies itself.
  unknown   the Hub could not tell: only a slice of the file was read and it did not reach the end of the section where
            labels live, the slicer is not one whose labels are understood (e.g. Cura), the format hides them (the
            compressed G-code blocks of a Prusa .bgcode), or the input was damaged. Nothing is shown for unknown.

Sources, by what the slicer wrote (found by slicing real samples; see tests/fixtures/plate/README.md):
  slice_info      Bambu Studio / OrcaSlicer .gcode.3mf: one <object> per object in Metadata/slice_info.config, per plate
  exclude_object  Klipper `EXCLUDE_OBJECT_DEFINE NAME=...` lines, written before the first layer (OrcaSlicer, PrusaSlicer
                  with the Klipper flavor). Copies are named `<Name>_id_<n>_copy_<k>`.
  m486            Marlin / Prusa firmware `M486 S<n>` + `M486 A<name>` definitions before the first layer (PrusaSlicer and
                  OrcaSlicer on Marlin-style printers)
  objects_info    PrusaSlicer's `; objects_info = {"objects":[...]}` line in the config block at the END of a plain .gcode
                  (an empty list = the file was sliced with labels off), or the same key in a .bgcode metadata block
  comments        `; printing object NAME id:<n> copy <k>` (OctoPrint CancelObject style) and Bambu's
                  `; start printing object, unique label id: <n>`

Counting rule: ONE labelled object counts ONE, whatever it is built from. An object assembled from several parts, or a
multi-part model the slicer treats as one object, is 1; two copies are 2. The count is "objects the printer can cancel
separately", which is also what the slicer's own "Label objects" setting means.

Names are normalised so copies group: the file extension (.stl .3mf .step .obj ...), the copy suffixes slicers add
(` id:0 copy 1`, `_id_0_copy_1`, ` (Instance 2)`, ` (3)`) and a trailing `_1` / `-2` index (only when the indices form a
1..n run, i.e. clearly copy numbers) are dropped; Klipper-style names, which cannot hold spaces, get theirs back from the
underscores. Grouping ignores case and the difference between space, `_` and `-`.

Stdlib only. Nothing here touches a printer or the network.
"""
import json
import os
import re
import unicodedata
import zipfile

import gcodemeta

LABELS = ("present", "missing", "unknown")
SOURCES = ("slice_info", "m486", "exclude_object", "objects_info", "comments")

MISSING_TEXT = ("No object labels \u2014 can't count parts or cancel one object. "
                "Turn on \"Label objects\" in the slicer.")
MAX_GROUPS_IN_SUMMARY = 6
MAX_NAME_CHARS = 60
HEAD_BYTES = 1024 * 1024                      # what the Hub reads of a printer's G-code head
STREAM_CAP = 48 * 1024 * 1024                 # the most a local file is read to find the end of its first layer
MAX_3MF_BYTES = 128 * 1024 * 1024               # a bigger .3mf is not read into memory (the library skips its details too)

_EXT_RX = re.compile(r"(?:\.|[_ ])(stl|3mf|step|stp|obj|amf|ply)$", re.I)
_COPY_RXS = (
    re.compile(r"[\s_]*\(?\binstance[\s_]*\d+\)?$", re.I),                    # PrusaSlicer 3: "Name.stl (Instance 2)"
    re.compile(r"[\s_]+id[:_\s]\d+[\s_]+copy[\s_]*\d+$", re.I),               # " id:0 copy 1" / "_id_0_copy_1"
    re.compile(r"[\s_]+copy[\s_]*\d+$", re.I),                                # " copy 2"
    re.compile(r"\s*\(\d{1,3}\)$"),                                           # "Name (2)"
)
_INDEX_RX = re.compile(r"^(.*?\S)[_\-](\d{1,2})$")


# ============================================================================ result helpers

def unknown() -> dict:
    return {"labels": "unknown", "source": None, "total": None, "groups": [], "summary": ""}


def missing(source=None) -> dict:
    return {"labels": "missing", "source": source, "total": None, "groups": [], "summary": ""}


def _present(source, groups=None, total=None) -> dict:
    groups = groups or []
    return {"labels": "present", "source": source, "total": total, "groups": groups,
            "summary": summarize(groups) if groups else (f"{total} object{'s' if total != 1 else ''}" if total else "")}


def summarize(groups) -> str:
    """[{"name": "Hose Clip", "count": 4}, ...] -> "4× Hose Clip + 2× Hook" (at most six groups, then "+ N more")."""
    parts = [f"{g['count']}× {g['name']}" for g in groups[:MAX_GROUPS_IN_SUMMARY]]
    if len(groups) > MAX_GROUPS_IN_SUMMARY:
        parts.append(f"{len(groups) - MAX_GROUPS_IN_SUMMARY} more")
    return " + ".join(parts)


def is_missing(result) -> bool:
    return isinstance(result, dict) and result.get("labels") == "missing"


def plate_line(result) -> str:
    """The one line the screens and agents show: "4× Hose Clip + 2× Hook", the missing-labels sentence, or "" (unknown)."""
    if not isinstance(result, dict):
        return ""
    if result.get("labels") == "present":
        return str(result.get("summary") or "")
    if result.get("labels") == "missing":
        return MISSING_TEXT
    return ""


def clean_result(r) -> dict:
    """A result from a cache or an index file -> the canonical shape (anything odd becomes unknown). Never raises."""
    try:
        if not isinstance(r, dict) or r.get("labels") not in LABELS:
            return unknown()
        groups = []
        for g in r.get("groups") or []:
            if isinstance(g, dict) and isinstance(g.get("name"), str) and isinstance(g.get("count"), int) and g["count"] > 0:
                groups.append({"name": g["name"][:MAX_NAME_CHARS * 2], "count": g["count"]})
        total = r.get("total") if isinstance(r.get("total"), int) and not isinstance(r.get("total"), bool) else None
        src = r.get("source") if r.get("source") in SOURCES else None
        out = {"labels": r["labels"], "source": src, "total": total, "groups": groups,
               "summary": summarize(groups) if r["labels"] == "present" and groups
               else (str(r.get("summary") or "")[:200] if r["labels"] == "present" else "")}
        if isinstance(r.get("plate"), int):
            out["plate"] = r["plate"]
        if isinstance(r.get("plates"), list):
            out["plates"] = [clean_result(p) for p in r["plates"][:50] if isinstance(p, dict)]
        return out
    except Exception:
        return unknown()


# ============================================================================ names

def _tidy(label: str) -> str:
    s = unicodedata.normalize("NFC", str(label or "")).strip()
    s = s.strip("'\"").strip()
    return re.sub(r"\s+", " ", s)


def normalize_name(label, humanize=False) -> str:
    """One label as the slicer wrote it -> the readable part name copies share. `humanize` turns `_` into a space
    (Klipper / M486 names cannot hold spaces, so a slicer writes them as underscores)."""
    s = _tidy(label)
    for _ in range(4):
        before = s
        for rx in _COPY_RXS:
            s = rx.sub("", s).rstrip()
        s = _EXT_RX.sub("", s).rstrip()
        if s == before:
            break
    if humanize and " " not in s and "_" in s:
        s = re.sub(r"_+", " ", s).strip()
    s = s.strip(" ._-")
    s = s[:MAX_NAME_CHARS].rstrip()
    return s or "Object"


def _key(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", name).strip().casefold()


def group_names(labels, humanize=False):
    """Labels -> ([{"name", "count"}, ...] most first, total). Copies of one part share a group."""
    names = [normalize_name(x, humanize) for x in labels if str(x or "").strip()]
    # a trailing _1 / -2 is a copy number only when the numbers run 1..n (or 0..n-1) for one base name
    fam = {}
    for n in names:
        m = _INDEX_RX.match(n)
        if m:
            fam.setdefault(_key(m.group(1)), []).append(int(m.group(2)))
    bare = {_key(n) for n in names if not _INDEX_RX.match(n)}
    strip = set()
    for base, idx in fam.items():
        have = sorted(set(idx) | ({1} if base in bare else set()))
        run = have == list(range(have[0], have[0] + len(have))) and have[0] in (0, 1)
        if len(idx) == len(set(idx)) and len(have) >= 2 and run:
            strip.add(base)
    order, counts, shown = [], {}, {}
    for n in names:
        m = _INDEX_RX.match(n)
        if m and _key(m.group(1)) in strip:
            n = m.group(1).rstrip(" ._-")
        k = _key(n)
        if k not in counts:
            order.append(k)
            counts[k] = 0
            shown[k] = n
        counts[k] += 1
    groups = sorted(({"name": shown[k], "count": counts[k]} for k in order), key=lambda g: -g["count"])
    return groups, sum(counts.values())


# ============================================================================ Bambu slice_info

def _plate_result(p: dict, parse_error) -> dict:
    names = [n for n in (p.get("objects") or []) if isinstance(n, str)]
    if names:
        groups, total = group_names(names)
        return _present("slice_info", groups, total)
    if p.get("object_entries"):                      # objects exist but every one was skipped: nothing left to count
        return unknown()
    if parse_error:                                  # a damaged slice_info: an absent <object> proves nothing
        return unknown()
    return missing("slice_info")


def from_slice_info(info) -> dict:
    """Bambu / Orca `slice_info.config` (its text, or what `joblog.parse_slice_info` returned) -> the result of the first
    plate; a file with several plates also carries `plates`: [result + "plate": n, ...]. Labels are "missing" when a plate
    lists no <object> at all. Never raises."""
    try:
        if not isinstance(info, dict):
            import joblog
            info = joblog.parse_slice_info(info)
        plates = info.get("plates") or []
        if not plates:
            return unknown()
        err = info.get("error")
        per = []
        for p in plates:
            r = _plate_result(p, err)
            r["plate"] = p.get("index") if isinstance(p.get("index"), int) else len(per) + 1
            per.append(r)
        out = dict(per[0])
        out.pop("plate", None)
        out["plate"] = per[0]["plate"]
        if len(per) > 1:
            out["plates"] = per
        return out
    except Exception:
        return unknown()


def from_3mf(data) -> dict:
    """The bytes of a .gcode.3mf -> from_slice_info. A .3mf without a slice_info (an unsliced project) is unknown."""
    try:
        import joblog
        info = joblog.slice_info_from_3mf(data)
        if not info.get("plates"):
            return unknown()
        return from_slice_info(info)
    except Exception:
        return unknown()


def for_plate(result, plate=None) -> dict:
    """The result of one plate of a multi-plate file (default: the first)."""
    try:
        if plate is not None and isinstance(result, dict) and result.get("plates"):
            for p in result["plates"]:
                if p.get("plate") == plate:
                    return p
        return result if isinstance(result, dict) and result.get("labels") in LABELS else unknown()
    except Exception:
        return unknown()


# ============================================================================ plain G-code

_LAYER_RX = re.compile(r"^;[ \t]*(?:LAYER_CHANGE\b|LAYER:[ \t]*\d+|CHANGE_LAYER\b)", re.M)
_EXD_RX = re.compile(r"^EXCLUDE_OBJECT_DEFINE\b([^\r\n]*)", re.M)
_EXD_NAME_RX = re.compile(r"""\bNAME=(?:'([^']*)'|"([^"]*)"|(\S+))""")
_M486_RX = re.compile(r"^M486[ \t]+([^\r\n]*)", re.M)
_M486_S_RX = re.compile(r"^S(-?\d+)")
_M486_A_RX = re.compile(r"""(?:^|\s)A[ \t]*"?([^\r\n";]*)"?""")
_M486_T_RX = re.compile(r"^T(\d+)")
_PRINTING_RX = re.compile(r"^;[ \t]*printing object[ \t]+([^\r\n]+?)[ \t]*$", re.M)
_BAMBU_ID_RX = re.compile(r"^;[ \t]*start printing object, unique label id:[ \t]*(\d+)", re.M)
_OBJINFO_RX = re.compile(r"^;[ \t]*objects_info[ \t]*=[ \t]*(\{[^\r\n]*\})[ \t]*$", re.M)
_GEN_RX = re.compile(r"^;[ \t]*(?:generated by|BambuStudio|Bambu Studio)\b", re.M | re.I)
_CFG_OFF_RX = re.compile(r"^;[ \t]*gcode_label_objects[ \t]*=[ \t]*(disabled|0)[ \t]*$", re.M | re.I)
_CFG_ON_RX = re.compile(r"^;[ \t]*gcode_label_objects[ \t]*=[ \t]*(?!disabled\b|0\b)\S", re.M | re.I)
_EXCL_CFG_RX = re.compile(r"^;[ \t]*exclude_object[ \t]*=[ \t]*(\d)", re.M | re.I)


def _text(b) -> str:
    if isinstance(b, (bytes, bytearray)):
        return bytes(b).decode("utf-8", "ignore")
    return str(b or "")


def _objects_info_names(text):
    """-> list of object names from the LAST `; objects_info = {...}` line, or None when there is no readable line."""
    found = _OBJINFO_RX.findall(text)
    if not found:
        return None
    try:
        doc = json.loads(found[-1])
    except ValueError:
        return None
    objs = doc.get("objects") if isinstance(doc, dict) else None
    if not isinstance(objs, list):
        return None
    return [str(o.get("name")) if isinstance(o, dict) and o.get("name") is not None else "" for o in objs]


def _m486_objects(text):
    """-> ({index: name or None}, declared_total or None) from the M486 lines, in file order."""
    names, last, total = {}, None, None
    for m in _M486_RX.finditer(text):
        rest = m.group(1).strip()
        t = _M486_T_RX.match(rest)
        if t:
            total = int(t.group(1))
            continue
        s = _M486_S_RX.match(rest)
        if s:
            idx = int(s.group(1))
            last = idx if idx >= 0 else None
            if idx >= 0:
                names.setdefault(idx, None)
            rest = rest[s.end():].strip()
        a = _M486_A_RX.search(rest)
        if a and last is not None and a.group(1).strip():
            if names.get(last) is None:
                names[last] = a.group(1).strip()
    return names, total


def _scan_gcode(head_text, tail_text, whole) -> dict:
    """The decision. `head_text` starts at the beginning of the file, `tail_text` is its last bytes (may be ''), `whole` is
    True when head_text is the entire file."""
    layers = [m.start() for m in _LAYER_RX.finditer(head_text)]
    first_layer_done = whole or len(layers) >= 2         # the first layer was read from its first line to its last
    pre_end = layers[0] if layers else (len(head_text) if whole else None)   # where the pre-layer section ends
    cfg_text = tail_text if tail_text else head_text
    ex = _EXCL_CFG_RX.findall(cfg_text)
    # the file's own settings say "no labels": PrusaSlicer `gcode_label_objects = disabled`, OrcaSlicer `= 0` together with
    # `exclude_object = 0` (OrcaSlicer's Klipper labels are a separate switch)
    cfg_off = bool(_CFG_OFF_RX.search(cfg_text)) and not _CFG_ON_RX.search(cfg_text) and not (ex and ex[-1] == "1")
    # -- Klipper EXCLUDE_OBJECT_DEFINE
    defs = []
    for m in _EXD_RX.finditer(head_text):
        n = _EXD_NAME_RX.search(m.group(1))
        if n:
            defs.append((m.start(), m.group(1).strip(), n.group(1) or n.group(2) or n.group(3) or ""))
    if defs:
        seen, labels = set(), []
        for _pos, line, name in defs:                      # byte-identical repeats are one object; two copies differ in position
            if line in seen:
                continue
            seen.add(line)
            labels.append(name)
        complete = whole or any(p > defs[-1][0] for p in layers)
        if not complete:
            return _present("exclude_object")
        groups, total = group_names(labels, humanize=True)
        return _present("exclude_object", groups, total)
    # -- Marlin / Prusa M486
    names, declared = _m486_objects(head_text)
    if names:
        named = {i: n for i, n in names.items() if n}
        if named:
            last_def = max(m.start() for m in _M486_RX.finditer(head_text) if _M486_A_RX.search(m.group(1)))
            complete = whole or any(p > last_def for p in layers)
            if not complete:
                return _present("m486")
            groups, total = group_names([named[i] for i in sorted(named)], humanize=True)
            return _present("m486", groups, total)
        # indices but no names: countable only when the whole first layer was read
        if declared:
            return _present("m486", [], declared)
        return _present("m486", [], len(names)) if first_layer_done else _present("m486")
    # -- PrusaSlicer objects_info (config block at the end)
    info = _objects_info_names(cfg_text if tail_text else head_text) if (tail_text or whole) else None
    if info:
        groups, total = group_names(info)
        return _present("objects_info", groups, total)
    # -- comments
    marks = [(m.start(), m.group(1)) for m in _PRINTING_RX.finditer(head_text) if not m.group(1).lower().startswith("stop")]
    ids = list(dict.fromkeys(_BAMBU_ID_RX.findall(head_text)))
    if marks:
        pre = [lab for pos, lab in marks if pre_end is not None and pos < pre_end]
        if pre:                                           # PrusaSlicer 2.6+: every object once, before the first layer
            groups, total = group_names(pre)
            return _present("comments", groups, total)
        if first_layer_done:                              # older slicers / OrcaSlicer: each object by its unique id:n copy:k label
            groups, total = group_names(list(dict.fromkeys(lab for _pos, lab in marks)))
            return _present("comments", groups, total)
        return _present("comments")
    if ids:
        return _present("comments", [], len(ids)) if first_layer_done else _present("comments")
    # -- nothing found: say "missing" only for a slicer that labels when asked, and only when the section was read
    ours = bool(_GEN_RX.search(head_text[:16384]))
    if ours and (first_layer_done or cfg_off):
        return missing()
    if ours and info is not None and not info:            # `; objects_info = {"objects":[]}` in the tail
        return missing()
    return unknown()


def from_gcode(head, tail=None, size=None) -> dict:
    """Plain G-code from the pieces the Hub reads: `head` = the first bytes of the file (the Hub reads 1 MB), `tail` = its
    last bytes (or None), `size` = the file size in bytes (None = unknown). "missing" needs the first layer to have been read
    from start to finish, or the file's own config block saying labels were off; a head that stops earlier is "unknown"."""
    try:
        hb = bytes(head) if isinstance(head, (bytes, bytearray)) else str(head or "").encode("utf-8", "ignore")
        tb = bytes(tail) if isinstance(tail, (bytes, bytearray)) else (str(tail).encode("utf-8", "ignore") if tail else b"")
        if not hb.strip():
            return unknown()
        known = isinstance(size, int) and not isinstance(size, bool) and size > 0     # 0 = a listing that does not say
        whole = known and size <= len(hb)
        if not whole and known and tb and size <= len(hb) + len(tb):     # head + tail together are the whole file
            hb, tb, whole = hb + tb[-(size - len(hb)):], b"", True
        return _scan_gcode(hb.decode("utf-8", "ignore"), "" if whole else tb.decode("utf-8", "ignore"), whole)
    except Exception:
        return unknown()


def from_gcode_file(path) -> dict:
    """A plain G-code file on the Hub's own disk: read from the start until the first layer is complete (or the end, or 48 MB),
    plus the last 256 KB for the config block. Definitive. Never raises."""
    try:
        size = os.path.getsize(path)
        buf, count, prev = bytearray(), 0, b""
        with open(path, "rb") as fh:
            while len(buf) < min(size, STREAM_CAP):
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                buf += chunk
                window = (prev + chunk).decode("utf-8", "ignore")
                count += sum(1 for m in _LAYER_RX.finditer(window) if m.end() > len(prev))
                prev = chunk[-64:]
                if count >= 2:
                    break
            tail = b""
            if size > len(buf):
                fh.seek(max(len(buf), size - 256 * 1024))
                tail = fh.read(256 * 1024)
        return from_gcode(bytes(buf), tail, size)
    except Exception:
        return unknown()


# ============================================================================ Prusa binary G-code

def from_bgcode(head) -> dict:
    """A Prusa .bgcode from its first bytes (the metadata blocks live there). The G-code itself is compressed (heatshrink +
    MeatPack) and is not decoded, so only what the metadata states can be used: PrusaSlicer's `objects_info` key lists the
    objects, and an empty list or `gcode_label_objects = disabled` is a definitive "missing". Anything else is unknown."""
    try:
        kv, reached = gcodemeta.bgcode_kv(head)
        if not kv:
            return unknown()
        raw = kv.get("objects_info")
        names = None
        if raw:
            try:
                doc = json.loads(raw)
                objs = doc.get("objects") if isinstance(doc, dict) else None
                if isinstance(objs, list):
                    names = [str(o.get("name")) if isinstance(o, dict) and o.get("name") is not None else "" for o in objs]
            except ValueError:
                names = None
        if names:
            groups, total = group_names(names)
            return _present("objects_info", groups, total)
        off = str(kv.get("gcode_label_objects", "")).strip().lower() in ("disabled", "0")
        if (names == [] or off) and reached:                 # every metadata block was read: labels were off
            return missing("objects_info")
        return unknown()
    except Exception:
        return unknown()


# ============================================================================ entry points

def from_slice(name, head=None, tail=None, size=None) -> dict:
    """Pick the reader by file name: `.bgcode` -> from_bgcode(head); `.gcode` / `.gco` / `.g` -> from_gcode. (A
    `.gcode.3mf` is read with from_3mf on its bytes.) Never raises."""
    try:
        low = str(name or "").lower()
        if low.endswith(".bgcode"):
            return from_bgcode(head) if head else unknown()
        if low.endswith((".gcode", ".gco", ".g")):
            return from_gcode(head, tail, size) if head else unknown()
        return unknown()
    except Exception:
        return unknown()


def from_path(path, kind=None) -> dict:
    """A file on the Hub's own disk (the library). `kind` is the library's own label ("3mf", "gcode" or "bgcode"); None goes by
    the file name. Definitive where the format allows it. Never raises."""
    try:
        low = str(path).lower()
        if kind not in ("3mf", "gcode", "bgcode"):
            kind = "3mf" if low.endswith((".gcode.3mf", ".3mf")) else "bgcode" if low.endswith(".bgcode") else "gcode"
        if kind == "3mf":
            if os.path.getsize(path) > MAX_3MF_BYTES:
                return unknown()
            with open(path, "rb") as fh:
                return from_3mf(fh.read())
        if kind == "bgcode":
            with open(path, "rb") as fh:
                return from_bgcode(fh.read(HEAD_BYTES))
        return from_gcode_file(path)
    except Exception:
        return unknown()
