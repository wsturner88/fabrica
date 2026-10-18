"""Recorded-shape fixtures for a PrusaLink printer with firmware 6.x (a Core One class machine) - generic names only.

The shapes below were checked against a real printer's answers (read-only GETs), field names only:
  * GET /api/v1/files/<storage>/        -> {type, name, ro, children:[{name (8.3 SHORT name), display_name (long name), type,
                                           m_timestamp, ro, refs:{icon, thumbnail, download}}]}  - NO size, NO meta
                                           (a FOLDER child has no refs)
  * GET /api/v1/files/<storage>/<short> -> the same entry plus `size`  (no `meta` object on that firmware)
  * GET /thumb/l/<storage>/<short>      -> image/png, chunked (380x285 or 640x480);  /thumb/s/... (the icon) may be 404
  * DELETE /api/v1/files/<storage>/<short> -> 204
A `meta` object (estimated printing time, filament used [g], filament_type, layer_height, nozzle_diameter) is described by the
PrusaLink API for other firmware; `META_FILE` carries one so the driver's reading of it is tested - it was NOT seen on the device.
"""
import struct
import zlib


def png(w=64, h=48, rgb=(70, 130, 200)):
    """A small valid PNG (solid colour with a lighter block) - no image library needed."""
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            inside = w // 4 <= x < 3 * w // 4 and h // 4 <= y < 3 * h // 4
            r, g, b = (min(255, c + 70) for c in rgb) if inside else rgb
            rows += bytes((r, g, b))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


# short name -> what the printer knows about the file
CORE_FILES = {
    "PART_A~1.GCO": {"display_name": "part_a_0.4n_0.2mm_PETG_Printer-A HF_1h25m.gcode", "size": 3792208, "ts": 1775424909,
                     "png": png(160, 120, (200, 120, 60))},
    "BRACKE~1.BGC": {"display_name": "bracket v2_0.4n_0.2mm_PLA_PrinterA_32m.bgcode", "size": 763378, "ts": 1765753888,
                     "png": png(95, 71, (90, 160, 90))},
    "TWOCOL~1.BGC": {"display_name": "two colour tag_0.4n_0.15mm_PLA.PETG_PrinterA_2h11m.bgcode", "size": 1204551, "ts": 1766869109,
                     "png": png(160, 120, (160, 90, 190))},
    "NOPICT~1.GCO": {"display_name": "no picture here.gcode", "size": 91234, "ts": 1760000000, "png": None},
}

# the other shape: a file whose info carries a `meta` object (per the PrusaLink API description; not seen on the device)
META_FILE = {
    "METAFI~1.GCO": {"display_name": "meta file.gcode", "size": 555000, "ts": 1770000000, "png": png(64, 48, (60, 60, 200)),
                     "meta": {"estimated printing time (normal mode)": "1h 5m 30s", "filament used [g]": "12.34",
                              "filament used [mm]": "4120.5", "filament_type": "PLA;PETG", "layer_height": "0.2",
                              "nozzle_diameter": "0.4"}},
}


def listing_entry(storage, short, f):
    e = {"name": short, "ro": False, "type": "PRINT_FILE", "m_timestamp": f["ts"],
         "refs": {"icon": f"/thumb/s/{storage}/{short}", "thumbnail": f"/thumb/l/{storage}/{short}",
                  "download": f"/{storage}/{short}"},
         "display_name": f["display_name"]}
    return e
