"""Facts a slicer writes INTO a G-code file: which material(s), layer height, nozzle, estimated weight.

Two sources, both read from a small slice of the file (never the whole file):

* plain `.gcode` from PrusaSlicer / OrcaSlicer / SuperSlicer: the config block is at the END of the file
  (`; filament_type = PETG;PLA`, `; layer_height = 0.2`, ...). `from_gcode_tail(text)` reads the last ~64 KB.
* binary `.bgcode` (Prusa "binary G-code"): the metadata blocks sit at the START, uncompressed or deflated.
  `from_bgcode_head(data)` walks them (libbgcode layout: header "GCDE", then blocks of type/compression/sizes).
  NOTE: the block layout here follows the published specification and is covered by a synthetic fixture only -
  no real .bgcode file was available to test against.

Everything returns plain dicts and never raises: unreadable input gives {}.
"""
import re
import struct
import zlib

_TYPE_RE = re.compile(r"^;\s*filament_type\s*=\s*(.+?)\s*$", re.M)
_NUM = r"([0-9]+(?:\.[0-9]+)?)"


def _materials(value):
    """'PETG;PLA' -> ['PETG', 'PLA'] (upper case, empty items dropped, at most 8)."""
    out = []
    for part in re.split(r"[;,]", str(value or "")):
        m = re.sub(r"[^A-Za-z0-9+\-]", "", part).upper()
        if m and m != "NIL":
            out.append(m)
    return out[:8]


def _first_num(value):
    m = re.match(r"\s*" + _NUM, str(value or ""))
    return float(m.group(1)) if m else None


def from_gcode_tail(text) -> dict:
    """Facts from the config block at the end of a plain G-code file (pass the last ~64 KB)."""
    try:
        text = text.decode("utf-8", "ignore") if isinstance(text, (bytes, bytearray)) else str(text or "")
        out = {}
        found = _TYPE_RE.findall(text)
        if found:
            mats = _materials(found[-1])
            if mats:
                out["materials"] = mats
        m = re.search(r"^;\s*layer_height\s*=\s*" + _NUM, text, re.M)
        if m and float(m.group(1)):
            out["layer_mm"] = float(m.group(1))
        m = re.search(r"^;\s*nozzle_diameter\s*=\s*" + _NUM, text, re.M)
        if m and float(m.group(1)):
            out["nozzle_mm"] = float(m.group(1))
        m = re.search(r"^;\s*total filament used \[g\]\s*=\s*" + _NUM, text, re.M)
        if m and float(m.group(1)):
            out["weight_g"] = float(m.group(1))
        return out
    except Exception:
        return {}


def _kv(text):
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def bgcode_kv(data):
    """(key/value pairs of every metadata block, reached_gcode) from the start of a binary .bgcode (pass the first ~1 MB).
    `reached_gcode` is True when the walk got to the first G-code block, i.e. every metadata block was read in full;
    False when the data ended (or a block could not be decoded) first, so a key that is missing may simply be unread.
    Never raises: unreadable input gives ({}, False)."""
    try:
        data = bytes(data or b"")
        if data[:4] != b"GCDE" or len(data) < 10:
            return {}, False
        checksum_type = struct.unpack_from("<H", data, 8)[0]
        crc = 4 if checksum_type == 1 else 0
        pos, kv, reached = 10, {}, False
        while pos + 8 <= len(data):
            btype, comp, usize = struct.unpack_from("<HHI", data, pos)
            pos += 8
            csize = usize
            if comp != 0:
                if pos + 4 > len(data):
                    break
                csize = struct.unpack_from("<I", data, pos)[0]
                pos += 4
            params = 6 if btype == 5 else 2          # thumbnail blocks carry format/width/height
            end = pos + params + csize
            if end > len(data):
                break
            if btype in (0, 2, 3, 4):                # file / slicer / printer / print metadata: key=value lines
                body = data[pos + params:end]
                if comp == 1:
                    body = zlib.decompress(body)
                elif comp != 0:
                    body = b""                       # heatshrink: not decoded here
                kv.update(_kv(body.decode("utf-8", "ignore")))
            elif btype == 1:
                reached = True                       # the G-code itself starts: the metadata is behind us
                break
            pos = end + crc
        return kv, reached
    except Exception:
        return {}, False


def from_bgcode_head(data) -> dict:
    """Facts from the metadata blocks at the start of a binary .bgcode (pass the first ~64 KB)."""
    try:
        kv, _reached = bgcode_kv(data)
        if not kv:
            return {}
        out = {}
        mats = _materials(kv.get("filament_type"))
        if mats:
            out["materials"] = mats
        lh = _first_num(kv.get("layer_height"))
        if lh:
            out["layer_mm"] = lh
        nz = _first_num(kv.get("nozzle_diameter"))
        if nz:
            out["nozzle_mm"] = nz
        g = _first_num(kv.get("filament used [g]"))
        if g:
            out["weight_g"] = g
        return out
    except Exception:
        return {}


def from_file_slice(name, head=None, tail=None) -> dict:
    """Pick the reader for a file name: `.bgcode` -> its head, anything else -> its tail."""
    if str(name or "").lower().endswith(".bgcode"):
        return from_bgcode_head(head) if head else {}
    return from_gcode_tail(tail) if tail else {}
