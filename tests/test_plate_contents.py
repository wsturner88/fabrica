"""Plate contents: how many of which object is on a plate, read from the slicer's cancel-object labels.

    python3 tests/test_plate_contents.py

Pure-module tests of app/platecontents.py (no server, no printer, no network). The fixtures in tests/fixtures/plate/ are REAL slicer
output (PrusaSlicer 3.0 alpha, OrcaSlicer 2.4.2, Bambu Studio 2.7) of one plate with 4x "Hose Clip" + 2x "Hook", trimmed to the
sections a reader looks at (see tests/fixtures/plate/README.md); the two .bgcode files and the label-less slice_info are hand-built.
The wiring (cache, REST, MCP, dashboard) is in tests/test_plate_wiring.py.
"""
import io
import os
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import gcodemeta  # noqa: E402
import platecontents as pc  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "plate")
WANT = [{"name": "Hose Clip", "count": 4}, {"name": "Hook", "count": 2}]
SUMMARY = "4× Hose Clip + 2× Hook"


def fx(name):
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read()


def gcode(name, head_only=False):
    """head_only: the file as the first 1 MB of a much bigger one (no tail); otherwise it is the whole file (head and tail)."""
    b = fx(name)
    return pc.from_gcode(b, None, 10 ** 8) if head_only else pc.from_gcode(b, b, len(b))


def shape_ok(t, r):
    t.assertEqual(sorted(r), ["groups", "labels", "source", "summary", "total"] if "plate" not in r and "plates" not in r
                  else sorted(r))
    t.assertIn(r["labels"], pc.LABELS)


class RealSlicerFiles(unittest.TestCase):
    """What each slicer actually wrote, read back."""

    def check(self, r, source):
        shape_ok(self, r)
        self.assertEqual((r["labels"], r["source"], r["total"]), ("present", source, 6))
        self.assertEqual(r["groups"], WANT)
        self.assertEqual(r["summary"], SUMMARY)

    def test_prusaslicer_marlin_prusa_firmware_m486(self):
        self.check(gcode("prusa_mk4s_m486.gcode", head_only=True), "m486")

    def test_prusaslicer_octoprint_comments_before_the_first_layer(self):
        self.check(gcode("prusa_mk4s_octoprint.gcode", head_only=True), "comments")

    def test_prusaslicer_octoprint_objects_info_in_the_config_block(self):
        b = fx("prusa_mk4s_octoprint.gcode")
        tail = b[b.index(b"; objects_info"):]
        head = b[: b.index(b"; printing object")]                      # a head that stops before any label: only the config block has them
        self.check(pc.from_gcode(head, tail, 10 ** 7), "objects_info")

    def test_prusaslicer_klipper_flavor_exclude_object_define(self):
        self.check(gcode("prusa_klipper_exclude.gcode", head_only=True), "exclude_object")

    def test_orcaslicer_klipper_exclude_object_define_with_copy_suffixes(self):
        self.check(gcode("orca_voron_exclude.gcode", head_only=True), "exclude_object")

    def test_orcaslicer_marlin_m486_names_are_sanitised(self):
        self.check(gcode("orca_prusa_m486.gcode", head_only=True), "m486")

    def test_bambu_studio_slice_info(self):
        r = pc.from_slice_info(fx("bambu_p1s_slice_info.config"))
        shape_ok(self, r)
        self.assertEqual((r["labels"], r["source"], r["total"], r["summary"]), ("present", "slice_info", 6, SUMMARY))
        self.assertEqual(r["groups"], WANT)
        self.assertEqual(r["plate"], 1)

    def test_prusa_binary_gcode_objects_info_in_the_metadata(self):
        r = pc.from_bgcode(fx("prusa_mk4s_synthetic.bgcode"))
        self.assertEqual((r["labels"], r["source"], r["total"], r["groups"]), ("present", "objects_info", 6, WANT))

    # ---- sliced with labels off
    def test_prusaslicer_labels_disabled_is_missing(self):
        r = gcode("prusa_mk4s_nolabels.gcode")
        shape_ok(self, r)
        self.assertEqual((r["labels"], r["total"], r["groups"], r["summary"]), ("missing", None, [], ""))

    def test_orcaslicer_labels_off_is_missing(self):
        self.assertEqual(gcode("orca_voron_nolabels.gcode")["labels"], "missing")
        self.assertEqual(gcode("orca_voron_nolabels.gcode", head_only=True)["labels"], "missing")   # the first layer was read

    def test_bambu_slice_info_without_objects_is_missing(self):
        r = pc.from_slice_info(fx("bambu_p1s_nolabels_slice_info.config"))
        self.assertEqual((r["labels"], r["source"], r["total"]), ("missing", "slice_info", None))

    def test_prusa_binary_gcode_with_labels_off_is_missing(self):
        self.assertEqual(pc.from_bgcode(fx("prusa_mk4s_nolabels_synthetic.bgcode"))["labels"], "missing")

    def test_the_dispatcher_picks_the_reader_by_extension(self):
        b = fx("prusa_mk4s_m486.gcode")
        self.assertEqual(pc.from_slice("part.gcode", b, None, len(b))["summary"], SUMMARY)
        self.assertEqual(pc.from_slice("part.bgcode", fx("prusa_mk4s_synthetic.bgcode"))["summary"], SUMMARY)
        self.assertEqual(pc.from_slice("part.stl", b)["labels"], "unknown")
        self.assertEqual(pc.from_slice("part.gcode", None)["labels"], "unknown")
        self.assertEqual(pc.from_slice(None, b)["labels"], "unknown")


class DefinitiveOrUnknown(unittest.TestCase):
    """'missing' only when the section where labels must be was read. Never a false 'missing'."""

    def head_until(self, name, marker, count):
        """The file text up to (not including) the count-th `marker`."""
        text = fx(name).decode()
        pos = -1
        for _ in range(count):
            pos = text.index(marker, pos + 1)
        return text[:pos].encode()

    def test_a_head_that_stops_before_the_first_layer_ends_is_unknown(self):
        head = self.head_until("orca_voron_nolabels.gcode", ";LAYER_CHANGE", 1)
        self.assertEqual(pc.from_gcode(head, None, 80 * 1024 * 1024)["labels"], "unknown")

    def test_a_head_with_one_layer_marker_is_still_unknown(self):
        text = fx("orca_voron_nolabels.gcode").decode()
        head = text[: text.index(";LAYER_CHANGE") + 400].encode()
        self.assertEqual(pc.from_gcode(head, None, 80 * 1024 * 1024)["labels"], "unknown")

    def test_a_head_that_read_the_whole_first_layer_is_missing(self):
        head = self.head_until("orca_voron_nolabels.gcode", ";LAYER_CHANGE", 2) + b";LAYER_CHANGE\n"
        self.assertEqual(pc.from_gcode(head, None, 80 * 1024 * 1024)["labels"], "missing")

    def test_a_config_block_that_says_labels_off_is_missing_even_with_a_short_head(self):
        b = fx("orca_voron_nolabels.gcode")
        head = b[: b.index(b";LAYER_CHANGE")]
        tail = b[b.index(b"; exclude_object"):]
        self.assertEqual(pc.from_gcode(head, tail, 80 * 1024 * 1024)["labels"], "missing")
        self.assertEqual(pc.from_gcode(head, b"; some other tail\n", 80 * 1024 * 1024)["labels"], "unknown")

    def test_prusaslicer_empty_objects_info_in_the_tail_is_missing(self):
        b = fx("prusa_mk4s_nolabels.gcode")
        head = b[: b.index(b";LAYER_CHANGE")]
        self.assertEqual(pc.from_gcode(head, b'; objects_info = {"objects":[]}\n', 10 ** 8)["labels"], "missing")

    def test_labels_on_in_the_config_block_never_make_missing(self):
        head = b"; generated by PrusaSlicer 2.7.0\nG28\n"
        tail = b"; gcode_label_objects = octoprint\n"
        self.assertEqual(pc.from_gcode(head, tail, 10 ** 8)["labels"], "unknown")

    def test_a_slicer_that_does_not_identify_itself_is_unknown_even_when_the_whole_file_is_read(self):
        cura = b";FLAVOR:Marlin\n;Generated with Cura_SteamEngine 5.7\n;LAYER_COUNT:2\n;LAYER:0\nG1 X1\n;LAYER:1\nG1 X2\n;LAYER:2\n"
        self.assertEqual(pc.from_gcode(cura, cura, len(cura))["labels"], "unknown")
        bare = b"G28\nG1 X10 Y10 E1\nG1 X20\n"
        self.assertEqual(pc.from_gcode(bare, None, len(bare))["labels"], "unknown")

    def test_a_small_known_slicer_file_read_to_its_end_without_labels_is_missing(self):
        t = b"; generated by SuperSlicer 2.5\nG28\nG1 X1 E1\n"
        self.assertEqual(pc.from_gcode(t, None, len(t))["labels"], "missing")
        self.assertEqual(pc.from_gcode(t, None, None)["labels"], "unknown")        # size not known: could be a cut head
        self.assertEqual(pc.from_gcode(t, None, 10 ** 9)["labels"], "unknown")     # head of a huge file

    def test_a_cut_label_block_gives_labels_but_no_quantity(self):
        b = fx("orca_voron_exclude.gcode").decode()
        cut = b.index("EXCLUDE_OBJECT_DEFINE", b.index("EXCLUDE_OBJECT_DEFINE") + 50)     # stop after the first define
        r = pc.from_gcode(b[:cut + 30].encode(), None, 10 ** 8)
        self.assertEqual((r["labels"], r["source"], r["total"], r["groups"], r["summary"]), ("present", "exclude_object", None, [], ""))

    def test_a_head_and_tail_that_together_cover_the_file_count_as_the_whole_file(self):
        t = b"; generated by PrusaSlicer 2.7.0\nG28\nG1 X1 E1\n"
        r = pc.from_gcode(t[:20], t[10:], len(t))
        self.assertEqual(r["labels"], "missing")

    def test_old_prusaslicer_labels_appear_inside_the_first_layer(self):
        t = ("; generated by PrusaSlicer 2.4.0\n;LAYER_CHANGE\n;Z:0.2\n; printing object Clip id:0 copy 0\nG1 X1 E1\n"
             "; stop printing object Clip id:0 copy 0\n; printing object Clip id:0 copy 1\nG1 X5 E1\n"
             "; stop printing object Clip id:0 copy 1\n; printing object Hook id:1 copy 0\nG1 X9 E1\n"
             "; stop printing object Hook id:1 copy 0\n;LAYER_CHANGE\n;Z:0.4\n; printing object Clip id:0 copy 0\n").encode()
        r = pc.from_gcode(t, None, 10 ** 8)
        self.assertEqual((r["labels"], r["source"], r["total"], r["summary"]), ("present", "comments", 3, "2× Clip + 1× Hook"))
        short = t[: t.index(b"; printing object Hook")]            # the first layer was not finished: do not state a quantity
        r = pc.from_gcode(short, None, 10 ** 8)
        self.assertEqual((r["labels"], r["total"], r["summary"]), ("present", None, ""))

    def test_bambu_plain_gcode_label_ids(self):
        t = ("; HEADER_BLOCK_START\n; BambuStudio 02.00.00.00\n; CHANGE_LAYER\n; start printing object, unique label id: 7\nG1 X1\n"
             "; stop printing object, unique label id: 7\n; start printing object, unique label id: 9\nG1 X2\n"
             "; CHANGE_LAYER\n; start printing object, unique label id: 7\n").encode()
        r = pc.from_gcode(t, None, 10 ** 8)
        self.assertEqual((r["labels"], r["source"], r["total"], r["groups"], r["summary"]), ("present", "comments", 2, [], "2 objects"))

    def test_m486_with_a_declared_total_and_no_names(self):
        t = b"; generated by SuperSlicer 2.5\nM486 T3\nM486 S0\nM486 S-1\n;LAYER_CHANGE\n"
        r = pc.from_gcode(t, None, 10 ** 8)
        self.assertEqual((r["labels"], r["source"], r["total"], r["groups"], r["summary"]), ("present", "m486", 3, [], "3 objects"))

    def test_klipper_defines_that_repeat_one_line_count_once_and_distinct_copies_count_each(self):
        t = (b"EXCLUDE_OBJECT_DEFINE NAME='Clip' CENTER=1,1 POLYGON=[[0,0]]\nEXCLUDE_OBJECT_DEFINE NAME='Clip' CENTER=5,5 POLYGON=[[4,4]]\n"
             b"EXCLUDE_OBJECT_DEFINE NAME='Clip' CENTER=1,1 POLYGON=[[0,0]]\nEXCLUDE_OBJECT_DEFINE RESET=1\n;LAYER_CHANGE\n")
        r = pc.from_gcode(t, None, len(t))
        self.assertEqual((r["total"], r["groups"]), (2, [{"name": "Clip", "count": 2}]))

    def test_klipper_name_quoting_variants(self):
        t = (b'EXCLUDE_OBJECT_DEFINE NAME="Left Arm" CENTER=1,1\nEXCLUDE_OBJECT_DEFINE NAME=Right_Arm CENTER=2,2\n'
             b"EXCLUDE_OBJECT_DEFINE NAME='Plate' CENTER=3,3\n;LAYER_CHANGE\n")
        r = pc.from_gcode(t, None, len(t))
        self.assertEqual(sorted(g["name"] for g in r["groups"]), ["Left Arm", "Plate", "Right Arm"])

    def test_m486_one_line_forms(self):
        t = b'M486 S0 A"Clip"\nM486 S1 A"Clip"\nM486 S2 A Hook\nM486 S-1\n;LAYER_CHANGE\n'
        r = pc.from_gcode(t, None, len(t))
        self.assertEqual(r["groups"], [{"name": "Clip", "count": 2}, {"name": "Hook", "count": 1}])


class LocalFiles(unittest.TestCase):
    """The library reads whole local files: definitive, and the first layer may start far into the file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_labels_behind_a_two_megabyte_thumbnail_are_found(self):
        body = fx("orca_voron_exclude.gcode")
        thumb = b"; THUMBNAIL_BLOCK_START\n" + b";" + b"A" * 80 + b"\n" * 1 + (b"; " + b"B" * 120 + b"\n") * 16000 + b"; THUMBNAIL_BLOCK_END\n"
        self.assertGreater(len(thumb), 1900000)
        marker = body.index(b"\n", body.index(b"; HEADER_BLOCK_END")) + 1
        data = body[:marker] + thumb + body[marker:]
        r = pc.from_path(self.write("big.gcode", data), "gcode")
        self.assertEqual((r["labels"], r["total"], r["summary"]), ("present", 6, SUMMARY))

    def test_missing_is_definitive_for_a_local_file_with_a_long_first_layer(self):
        body = fx("orca_voron_nolabels.gcode").decode()
        pad = "G1 X10 Y10 E0.1\n" * 150000                                  # a 2.4 MB first layer
        first = body.index(";LAYER_CHANGE")
        second = body.index(";LAYER_CHANGE", first + 5)
        data = (body[:second] + pad + body[second:]).encode()
        self.assertGreater(second + len(pad), 2 * 1024 * 1024)
        self.assertEqual(pc.from_path(self.write("long.gcode", data), "gcode")["labels"], "missing")

    def test_each_kind_by_path(self):
        self.assertEqual(pc.from_path(os.path.join(FIX, "prusa_mk4s_m486.gcode"))["summary"], SUMMARY)
        self.assertEqual(pc.from_path(os.path.join(FIX, "prusa_mk4s_synthetic.bgcode"))["summary"], SUMMARY)
        self.assertEqual(pc.from_path(os.path.join(FIX, "prusa_mk4s_nolabels.gcode"))["labels"], "missing")

    def test_a_gcode_3mf_on_disk(self):
        p = self.write("plate.gcode.3mf", make_3mf(fx("bambu_p1s_slice_info.config").decode()))
        r = pc.from_path(p, "3mf")
        self.assertEqual((r["labels"], r["summary"]), ("present", SUMMARY))

    def test_a_huge_3mf_is_not_read_into_memory(self):
        p = self.write("huge.gcode.3mf", make_3mf(fx("bambu_p1s_slice_info.config").decode()))
        self.assertEqual(pc.from_path(p, "3mf")["labels"], "present")
        old = pc.MAX_3MF_BYTES
        pc.MAX_3MF_BYTES = 100
        try:
            self.assertEqual(pc.from_path(p, "3mf")["labels"], "unknown")
        finally:
            pc.MAX_3MF_BYTES = old

    def test_missing_files_and_garbage_are_unknown(self):
        self.assertEqual(pc.from_path(os.path.join(self.tmp.name, "nope.gcode"))["labels"], "unknown")
        self.assertEqual(pc.from_path(self.write("junk.3mf", b"not a zip"), "3mf")["labels"], "unknown")
        self.assertEqual(pc.from_path(self.write("empty.gcode", b""))["labels"], "unknown")


def make_3mf(slice_info: str, name="Metadata/slice_info.config") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(name, slice_info)
    return buf.getvalue()


def plate_xml(index, objects, skipped=()):
    objs = "".join(f'<object identify_id="{i}" name="{n}" skipped="{"true" if i in skipped else "false"}" />'
                   for i, n in enumerate(objects, 100))
    return f'<plate><metadata key="index" value="{index}"/>{objs}<filament id="1" type="PLA" used_g="5"/></plate>'


class Bambu(unittest.TestCase):
    def test_a_gcode_3mf(self):
        r = pc.from_3mf(make_3mf(fx("bambu_p1s_slice_info.config").decode()))
        self.assertEqual((r["labels"], r["source"], r["summary"]), ("present", "slice_info", SUMMARY))

    def test_a_3mf_without_slice_info_is_unknown(self):
        self.assertEqual(pc.from_3mf(make_3mf("x", name="Metadata/other.config"))["labels"], "unknown")

    def test_per_plate_results(self):
        xml = f"<config>{plate_xml(1, ['Clip.stl'] * 3 + ['Hook.stl'])}{plate_xml(2, [])}{plate_xml(3, ['Lid.3mf'])}</config>"
        r = pc.from_slice_info(xml)
        self.assertEqual((r["labels"], r["summary"], r["plate"]), ("present", "3× Clip + 1× Hook", 1))
        self.assertEqual([(p["plate"], p["labels"]) for p in r["plates"]], [(1, "present"), (2, "missing"), (3, "present")])
        self.assertEqual(pc.for_plate(r, 3)["summary"], "1× Lid")
        self.assertEqual(pc.for_plate(r, 2)["labels"], "missing")
        self.assertEqual(pc.for_plate(r, 9)["summary"], r["summary"])              # an unknown plate falls back to the first
        self.assertEqual(pc.for_plate(r)["plate"], 1)

    def test_a_single_plate_has_no_plates_list(self):
        self.assertNotIn("plates", pc.from_slice_info(f"<config>{plate_xml(1, ['A'])}</config>"))

    def test_skipped_objects_are_not_counted(self):
        r = pc.from_slice_info(f"<config>{plate_xml(1, ['A', 'A', 'B'], skipped=(101,))}</config>")
        self.assertEqual((r["total"], r["groups"]), (2, [{"name": "A", "count": 1}, {"name": "B", "count": 1}]))

    def test_a_plate_whose_every_object_was_skipped_is_not_missing(self):
        r = pc.from_slice_info(f"<config>{plate_xml(1, ['A'], skipped=(100,))}</config>")
        self.assertEqual(r["labels"], "unknown")

    def test_a_damaged_slice_info_without_objects_is_not_called_missing(self):
        r = pc.from_slice_info('<config><plate><metadata key="index" value="1"/><filament id="1"')
        self.assertEqual(r["labels"], "unknown")

    def test_an_assembled_object_counts_one(self):
        # Bambu lists one <object> per object whatever its parts are
        r = pc.from_slice_info(f"<config>{plate_xml(1, ['Gearbox.3mf'])}</config>")
        self.assertEqual((r["total"], r["summary"]), (1, "1× Gearbox"))

    def test_input_forms(self):
        import joblog
        xml = f"<config>{plate_xml(1, ['A', 'A'])}</config>"
        self.assertEqual(pc.from_slice_info(xml.encode())["total"], 2)
        self.assertEqual(pc.from_slice_info(joblog.parse_slice_info(xml))["total"], 2)
        for bad in (None, "", b"", 12, [], {}, {"plates": []}, "<config/>"):
            self.assertEqual(pc.from_slice_info(bad)["labels"], "unknown", repr(bad))


def bgcode_with(blocks, checksum=0, tail_block=True):
    out = b"GCDE" + struct.pack("<IH", 1, checksum)
    for btype, comp, text in blocks:
        raw = text.encode()
        body = zlib.compress(raw) if comp == 1 else raw
        hdr = struct.pack("<HHI", btype, comp, len(raw)) + (struct.pack("<I", len(body)) if comp else b"")
        out += hdr + struct.pack("<H", 0) + body + (b"\0\0\0\0" if checksum == 1 else b"")
    if tail_block:
        out += struct.pack("<HHI", 1, 0, 4) + struct.pack("<H", 0) + b"G28\n"
    return out


class BinaryGcode(unittest.TestCase):
    OI = '{"objects":[{"name":"Clip.stl id:0 copy 0","polygon":[]},{"name":"Clip.stl id:0 copy 1","polygon":[]}]}'

    def test_objects_info_in_any_metadata_block_and_deflated_blocks(self):
        for btype in (0, 3, 4):
            r = pc.from_bgcode(bgcode_with([(btype, 0, "objects_info=" + self.OI + "\n")]))
            self.assertEqual((r["labels"], r["total"], r["summary"]), ("present", 2, "2× Clip"), btype)
        r = pc.from_bgcode(bgcode_with([(2, 1, "objects_info=" + self.OI + "\n")], checksum=1))
        self.assertEqual(r["summary"], "2× Clip")

    def test_label_setting_disabled_is_missing_only_when_all_metadata_was_read(self):
        full = bgcode_with([(2, 1, "gcode_label_objects=disabled\n")])
        self.assertEqual(pc.from_bgcode(full)["labels"], "missing")
        cut = full[:-14]                                                  # the G-code block is cut: could the metadata be incomplete?
        self.assertEqual(pc.from_bgcode(cut)["labels"], "unknown")

    def test_labels_inside_compressed_gcode_cannot_be_seen_so_unknown(self):
        r = pc.from_bgcode(bgcode_with([(3, 0, "printer_model=MK4S\nfilament_type=PLA\n")]))
        self.assertEqual((r["labels"], r["total"]), ("unknown", None))

    def test_not_a_bgcode(self):
        for bad in (None, b"", b"GCDE", b"; plain gcode\n", b"\x00" * 100, "text"):
            self.assertEqual(pc.from_bgcode(bad)["labels"], "unknown", repr(bad))

    def test_broken_json_is_unknown(self):
        r = pc.from_bgcode(bgcode_with([(3, 0, "objects_info={not json\n")]))
        self.assertEqual(r["labels"], "unknown")

    def test_the_existing_material_reader_is_unchanged_by_the_shared_walker(self):
        data = bgcode_with([(3, 0, "printer_model=MK4S\nfilament_type=PETG\nlayer_height=0.2\nnozzle_diameter=0.4\n")])
        self.assertEqual(gcodemeta.from_bgcode_head(data), {"materials": ["PETG"], "layer_mm": 0.2, "nozzle_mm": 0.4})
        kv, reached = gcodemeta.bgcode_kv(data)
        self.assertTrue(reached)
        self.assertEqual(kv["printer_model"], "MK4S")


class Names(unittest.TestCase):
    def n(self, s, h=False):
        return pc.normalize_name(s, h)

    def test_extensions(self):
        for raw in ("Hose Clip.stl", "Hose Clip.STL", "Hose Clip.3mf", "Hose Clip.step", "Hose Clip.stp", "Hose Clip.obj"):
            self.assertEqual(self.n(raw), "Hose Clip", raw)
        self.assertEqual(self.n("v1.2 bracket.stl"), "v1.2 bracket")              # only a model extension goes
        self.assertEqual(self.n("Notes.txt"), "Notes.txt")

    def test_copy_suffixes_of_each_slicer(self):
        for raw in ("Hose Clip.stl id:0 copy 1", "Hose_Clip.stl_id_0_copy_1", "Hose_Clip_stl_id_12_copy_0", "Hose Clip.stl (Instance 3)",
                    "Hose Clip.stl (3)", "Hose Clip (2)", "Hose Clip copy 4", "'Hose Clip'", "  Hose   Clip  "):
            self.assertEqual(self.n(raw, True), "Hose Clip", raw)

    def test_humanize_only_when_there_is_no_space(self):
        self.assertEqual(self.n("Hose_Clip", True), "Hose Clip")
        self.assertEqual(self.n("Hose_Clip", False), "Hose_Clip")
        self.assertEqual(self.n("Hose Clip_v2", True), "Hose Clip_v2")

    def test_degenerate_labels(self):
        for raw in ("", None, ".stl", "   ", "(1)"):
            self.assertTrue(self.n(raw))                                           # never empty: falls back to "Object"

    def test_long_names_are_cut(self):
        self.assertLessEqual(len(self.n("x" * 500)), pc.MAX_NAME_CHARS)

    def test_trailing_indices_are_copy_numbers_only_for_a_1_to_n_run(self):
        g, total = pc.group_names(["Hook_1", "Hook_2", "Hook_3"])
        self.assertEqual((g, total), ([{"name": "Hook", "count": 3}], 3))
        g, _ = pc.group_names(["Hook-1.stl", "Hook-2.stl"])
        self.assertEqual(g, [{"name": "Hook", "count": 2}])
        g, _ = pc.group_names(["Hook", "Hook_2", "Hook_3"])                        # the bare name is copy 1
        self.assertEqual(g, [{"name": "Hook", "count": 3}])
        g, _ = pc.group_names(["M3_20", "M3_30"])                                  # lengths, not copies
        self.assertEqual(sorted(x["name"] for x in g), ["M3_20", "M3_30"])
        g, _ = pc.group_names(["Clip_2"])                                          # one alone: no way to know
        self.assertEqual(g, [{"name": "Clip_2", "count": 1}])
        g, _ = pc.group_names(["Clip_1", "Clip_1"])                                # the same number twice is not a run
        self.assertEqual(g, [{"name": "Clip_1", "count": 2}])

    def test_grouping_ignores_case_and_separators_and_orders_by_count(self):
        g, total = pc.group_names(["hose clip", "Hose_Clip", "Hose-Clip", "Hook", "Hook", "Lid"])
        self.assertEqual(total, 6)
        self.assertEqual([x["count"] for x in g], [3, 2, 1])
        self.assertEqual(g[0]["name"], "hose clip")

    def test_blank_labels_are_ignored(self):
        self.assertEqual(pc.group_names(["", None, "  "]), ([], 0))

    def test_summary_text(self):
        self.assertEqual(pc.summarize([{"name": "A", "count": 1}]), "1× A")
        many = [{"name": f"P{i}", "count": 1} for i in range(9)]
        self.assertEqual(pc.summarize(many), " + ".join(f"1× P{i}" for i in range(6)) + " + 3 more")
        self.assertEqual(pc.summarize([]), "")

    def test_plate_line_and_flags(self):
        self.assertEqual(pc.plate_line(pc.unknown()), "")
        self.assertEqual(pc.plate_line(pc.missing()), pc.MISSING_TEXT)
        self.assertIn("Label objects", pc.MISSING_TEXT)
        self.assertEqual(pc.plate_line({"labels": "present", "summary": "2× A"}), "2× A")
        self.assertTrue(pc.is_missing(pc.missing()))
        self.assertFalse(pc.is_missing(None))
        self.assertEqual(pc.plate_line(None), "")


class NeverRaises(unittest.TestCase):
    JUNK = [None, b"", "", b"\x00\xff" * 50, "é中" * 30, b"\xff\xfe\xfd", 123, [], {}, object()]

    def test_every_entry_point_survives_junk(self):
        for j in self.JUNK:
            for fn in (pc.from_gcode, pc.from_bgcode, pc.from_slice_info, pc.from_3mf, pc.clean_result, pc.for_plate):
                r = fn(j)
                self.assertIn(r["labels"], pc.LABELS)
            for size in (None, -5, 0, "x", 10 ** 12):
                self.assertIn(pc.from_gcode(j, j, size)["labels"], pc.LABELS)
            self.assertIn(pc.from_slice("a.gcode", j, j, 5)["labels"], pc.LABELS)
            self.assertIn(pc.from_slice("a.bgcode", j)["labels"], pc.LABELS)
            self.assertIn(pc.from_path(j)["labels"], pc.LABELS)

    def test_damaged_labels(self):
        for t in (b"EXCLUDE_OBJECT_DEFINE NAME=\n", b"EXCLUDE_OBJECT_DEFINE\n", b"M486 S\nM486 A\n", b"M486 Sx Ay\n",
                  b'; objects_info = {"objects": 5}\n', b"; objects_info = {\n", b"; printing object\n", b"M486 T999999999999999999999\n"):
            self.assertIn(pc.from_gcode(t, t, len(t))["labels"], pc.LABELS, t)

    def test_clean_result_repairs_a_cache_entry(self):
        good = pc.from_gcode(fx("prusa_mk4s_m486.gcode"), None, 10 ** 8)
        self.assertEqual(pc.clean_result(good), good)
        self.assertEqual(pc.clean_result({"labels": "present", "groups": [{"name": "A", "count": 2}, {"name": 5}, "x"], "total": "6",
                                          "source": "bogus"}),
                         {"labels": "present", "source": None, "total": None, "groups": [{"name": "A", "count": 2}], "summary": "2× A"})
        for bad in (None, "x", {"labels": "maybe"}, {"labels": None}, 5):
            self.assertEqual(pc.clean_result(bad), pc.unknown())
        multi = {"labels": "present", "source": "slice_info", "total": 1, "groups": [{"name": "A", "count": 1}], "summary": "1× A",
                 "plate": 1, "plates": [{"labels": "missing", "plate": 2}, 7]}
        out = pc.clean_result(multi)
        self.assertEqual([p["labels"] for p in out["plates"]], ["missing"])
        self.assertEqual(out["plates"][0]["plate"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
