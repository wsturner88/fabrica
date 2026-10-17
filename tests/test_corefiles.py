"""File lists, pictures, material badges, the file detail sheet and delete - for a PrusaLink "Core One" class printer and the others.

    python3 tests/test_corefiles.py          (needs the app requirements; a node binary for the page-function tests)

Everything is local: a fake PrusaLink (tests/test_prusalink.py + tests/prusalink_fixtures.py, shaped like the answers a real
firmware-6 printer gave to read-only GETs) and in-process fake drivers. No printer, no host, no network. The server module is
imported with throwaway config / cache directories.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="corefiles-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)

import gcodemeta  # noqa: E402
import prusalink_fixtures as FX  # noqa: E402
from test_prusalink import FakePrusaLink, CREDS  # noqa: E402
from prusalink import PrusaLinkPrinter  # noqa: E402
from bambu import PrinterError  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover
    HAVE = False
    _ERR = e

with open(os.path.join(APP, "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()


def core_files(extra=None):
    """A fresh copy (the fake deletes from it)."""
    out = {k: dict(v) for k, v in FX.CORE_FILES.items()}
    out.update({k: dict(v) for k, v in (extra or {}).items()})
    return out


# ============================================================== the driver

class TestDriverListing(unittest.TestCase):
    def setUp(self):
        self.fake = FakePrusaLink(core=core_files(FX.META_FILE))
        self.p = PrusaLinkPrinter("Printer-A", self.fake.url, CREDS)
        self.addCleanup(self.fake.close)
        self.rows = {r["name"]: r for r in self.p.files()}

    def test_long_names_are_kept_and_the_short_name_stays_the_path(self):
        r = self.rows["PART_A~1.GCO"]
        self.assertEqual(r["display_name"], "part_a_0.4n_0.2mm_PETG_Printer-A HF_1h25m.gcode")
        self.assertEqual(r["name"], "PART_A~1.GCO")

    def test_bgcode_files_are_listed_by_type_not_by_their_8_3_extension(self):
        # a real printer calls a .bgcode "NAME~1.BGC": the old extension filter dropped every one of them
        self.assertIn("BRACKE~1.BGC", self.rows)
        self.assertIn("TWOCOL~1.BGC", self.rows)
        self.assertEqual(len(self.rows), 5)

    def test_the_listing_has_no_size_until_the_details_were_read(self):
        self.assertEqual(self.rows["PART_A~1.GCO"]["size"], 0)
        self.assertEqual(self.p.file_details("PART_A~1.GCO", 1775424909), {"size": 3792208})
        self.assertEqual({r["name"]: r for r in self.p.files()}["PART_A~1.GCO"]["size"], 3792208)

    def test_a_changed_file_is_read_again(self):
        self.p.file_details("PART_A~1.GCO", 1775424909)
        row = {r["name"]: r for r in self.p.files()}["PART_A~1.GCO"]
        self.assertEqual(row["size"], 3792208)
        self.fake.core["PART_A~1.GCO"]["ts"] += 5                 # re-saved: a new timestamp, a new rev
        row = {r["name"]: r for r in self.p.files()}["PART_A~1.GCO"]
        self.assertEqual(row["size"], 0)

    def test_print_time_material_and_layer_come_from_the_long_name(self):
        r = self.rows["PART_A~1.GCO"]
        self.assertEqual((r["print_seconds"], r["print_time"], r["filament_type"], r["materials"]), (5100, "1h25m", "PETG", ["PETG"]))
        self.assertEqual((r["nozzle_mm"], r["layer_mm"]), (0.4, 0.2))
        self.assertEqual(self.rows["BRACKE~1.BGC"]["print_time"], "0h32m")
        two = self.rows["TWOCOL~1.BGC"]
        self.assertEqual((two["materials"], two["print_seconds"], two["layer_mm"]), (["PLA", "PETG"], 7860, 0.15))

    def test_a_name_without_the_slicer_words_says_nothing_instead_of_guessing(self):
        r = self.rows["NOPICT~1.GCO"]
        self.assertEqual((r["print_seconds"], r["print_time"], r["filament_type"], r["materials"]), (None, None, None, []))

    def test_the_metadata_object_is_read_when_the_firmware_has_one(self):
        d = self.p.file_details("METAFI~1.GCO", 1770000000)
        self.assertEqual((d["size"], d["print_seconds"], d["weight_g"], d["filament_mm"]), (555000, 3930, 12.34, 4120.5))
        self.assertEqual((d["materials"], d["layer_mm"], d["nozzle_mm"]), (["PLA", "PETG"], 0.2, 0.4))
        row = {r["name"]: r for r in self.p.files()}["METAFI~1.GCO"]
        self.assertEqual((row["print_time"], row["weight_g"], row["materials"]), ("1h05m", 12.34, ["PLA", "PETG"]))

    def test_the_picture_ref_is_the_large_thumbnail(self):
        self.assertEqual(self.rows["PART_A~1.GCO"]["thumb_ref"], "/thumb/l/usb/PART_A~1.GCO")

    def test_nothing_but_gets_is_sent_for_a_listing_details_and_pictures(self):
        self.p.files()
        self.p.file_details("PART_A~1.GCO", 1)
        self.p.thumbnail_png("PART_A~1.GCO", "/thumb/l/usb/PART_A~1.GCO")
        self.assertEqual({m for m, _p in self.fake.log if not _p.startswith("/api/version")}, {"GET"})


class TestDriverPictures(unittest.TestCase):
    def setUp(self):
        self.fake = FakePrusaLink(core=core_files())
        self.p = PrusaLinkPrinter("Printer-A", self.fake.url, CREDS)
        self.addCleanup(self.fake.close)

    def test_a_picture_comes_back_as_png_through_digest_auth(self):
        data = self.p.thumbnail_png("PART_A~1.GCO", "/thumb/l/usb/PART_A~1.GCO")
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(data, FX.CORE_FILES["PART_A~1.GCO"]["png"])
        self.assertTrue(any(a for a, _k in self.fake.auth_headers))

    def test_api_key_firmware_works_too(self):
        fake = FakePrusaLink(auth="key", core=core_files())
        self.addCleanup(fake.close)
        from test_prusalink import KEY
        p = PrusaLinkPrinter("Printer-A", fake.url, KEY)
        self.assertEqual(p.thumbnail_png("BRACKE~1.BGC")[:4], b"\x89PNG")

    def test_no_picture_is_none_not_an_error(self):
        self.assertIsNone(self.p.thumbnail_png("NOPICT~1.GCO", "/thumb/l/usb/NOPICT~1.GCO"))

    def test_the_ref_is_built_from_the_name_when_the_listing_gave_none(self):
        self.assertEqual(self.p.thumbnail_png("TWOCOL~1.BGC")[:4], b"\x89PNG")
        self.assertIn(("GET", "/thumb/l/usb/TWOCOL~1.BGC"), self.fake.log)

    def test_a_ref_that_is_not_a_thumbnail_path_is_never_followed(self):
        for bad in ("/api/v1/files/usb/PART_A~1.GCO", "http://evil.example/x.png", "/thumb/../api/v1/status", "/thumb/l/usb/x?y=1"):
            self.p.thumbnail_png("BRACKE~1.BGC", bad)
        paths = [pp for _m, pp in self.fake.log]
        self.assertTrue(all(pp.startswith("/thumb/l/usb/BRACKE~1.BGC") for pp in paths if pp.startswith("/thumb")), paths)
        self.assertNotIn("/api/v1/status", paths)

    def test_something_that_is_not_a_png_is_not_a_picture(self):
        self.fake.core["BRACKE~1.BGC"]["png"] = b"<html>not a picture</html>"
        self.assertIsNone(self.p.thumbnail_png("BRACKE~1.BGC"))

    def test_an_unreachable_printer_is_a_printererror(self):
        self.fake.close()
        with self.assertRaises(PrinterError):
            self.p.thumbnail_png("BRACKE~1.BGC")


class TestDriverDelete(unittest.TestCase):
    def setUp(self):
        self.fake = FakePrusaLink(core=core_files())
        self.p = PrusaLinkPrinter("Printer-A", self.fake.url, CREDS)
        self.addCleanup(self.fake.close)

    def test_delete_uses_the_short_name_as_the_path(self):
        self.p.sd_delete("PART_A~1.GCO")
        self.assertIn(("DELETE", "/api/v1/files/usb/PART_A~1.GCO"), self.fake.log)
        self.assertNotIn("PART_A~1.GCO", self.fake.core)

    def test_a_file_in_a_folder_keeps_its_folder_in_the_path(self):
        with mock.patch.object(self.p, "_call") as call, mock.patch("prusalink._require_idle_for_delete"):
            self.p.sd_delete("SUBDIR/Z_FILE~1.GCO")
        self.assertEqual(call.call_args[0][:2], ("DELETE", "/api/v1/files/usb/SUBDIR/Z_FILE~1.GCO"))

    def test_the_long_name_is_not_a_path(self):
        with self.assertRaises(PrinterError):
            self.p.sd_delete("part_a_0.4n_0.2mm_PETG_Printer-A HF_1h25m.gcode")
        self.assertEqual(len(self.fake.core), 4)

    def test_a_printing_printer_deletes_nothing(self):
        self.fake.state = "PRINTING"
        self.fake.job = {"id": 1, "name": "x", "progress": 5, "time_remaining": 100}
        with self.assertRaises(PrinterError):
            self.p.sd_delete("PART_A~1.GCO")
        self.assertNotIn("DELETE", [m for m, _p in self.fake.log])


# ============================================================== material from the file itself

def bgcode(blocks, checksum=1):
    """A synthetic binary G-code head: header + metadata blocks [(type, compression, text)] + a G-code block."""
    out = b"GCDE" + struct.pack("<IH", 1, checksum)
    for btype, comp, text in blocks:
        raw = text.encode()
        body = zlib.compress(raw) if comp == 1 else raw
        hdr = struct.pack("<HHI", btype, comp, len(raw)) + (struct.pack("<I", len(body)) if comp else b"")
        out += hdr + struct.pack("<H", 0) + body + (b"\0\0\0\0" if checksum == 1 else b"")
    out += struct.pack("<HHI", 1, 0, 4) + struct.pack("<H", 0) + b"G28\n"
    return out


class TestGcodeMeta(unittest.TestCase):
    def test_a_plain_gcode_tail_with_one_material(self):
        tail = "G1 X1\n; prusaslicer_config = begin\n; filament_type = PETG\n; layer_height = 0.2\n; nozzle_diameter = 0.4\n"
        self.assertEqual(gcodemeta.from_gcode_tail(tail), {"materials": ["PETG"], "layer_mm": 0.2, "nozzle_mm": 0.4})

    def test_a_plain_gcode_tail_with_two_materials(self):
        tail = b"; total filament used [g] = 12.5\n; filament_type = PLA;PETG\n; filament_colour = #FF0000;#00FF00\n"
        self.assertEqual(gcodemeta.from_gcode_tail(tail), {"materials": ["PLA", "PETG"], "weight_g": 12.5})

    def test_garbage_gives_nothing(self):
        for junk in (b"", b"\x00\xff\x10", "no config here", None):
            self.assertEqual(gcodemeta.from_gcode_tail(junk), {})
        self.assertEqual(gcodemeta.from_bgcode_head(b"\x00" * 50), {})
        self.assertEqual(gcodemeta.from_bgcode_head(None), {})

    def test_bgcode_single_and_double_material_plain_and_deflated(self):
        one = bgcode([(0, 0, "Producer=Slicer\n"), (3, 0, "printer_model=PRINTERA\nfilament_type=PETG\nlayer_height=0.2\nnozzle_diameter=0.4\nfilament used [g]=7.5\n")])
        self.assertEqual(gcodemeta.from_bgcode_head(one), {"materials": ["PETG"], "layer_mm": 0.2, "nozzle_mm": 0.4, "weight_g": 7.5})
        two = bgcode([(3, 1, "filament_type=PLA;PETG\n")], checksum=0)
        self.assertEqual(gcodemeta.from_bgcode_head(two)["materials"], ["PLA", "PETG"])

    def test_bgcode_stops_at_the_gcode_block_and_survives_a_cut(self):
        data = bgcode([(3, 0, "filament_type=ABS\n")])
        self.assertEqual(gcodemeta.from_bgcode_head(data[:-3])["materials"], ["ABS"])
        self.assertEqual(gcodemeta.from_bgcode_head(data[:20]), {})

    def test_the_reader_is_chosen_by_the_file_name(self):
        self.assertEqual(gcodemeta.from_file_slice("a.bgcode", head=bgcode([(3, 0, "filament_type=TPU\n")]))["materials"], ["TPU"])
        self.assertEqual(gcodemeta.from_file_slice("a.gcode", tail="; filament_type = ASA\n")["materials"], ["ASA"])
        self.assertEqual(gcodemeta.from_file_slice("a.gcode", head=b"GCDE"), {})


# ============================================================== the server

def _png_count(d):
    return len([n for n in os.listdir(d) if n.endswith(".png")]) if os.path.isdir(d) else 0


@unittest.skipUnless(HAVE, "needs the app requirements")
class ServerCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakePrusaLink(core=core_files(FX.META_FILE))
        self.addCleanup(self.fake.close)
        self.prn = PrusaLinkPrinter("Printer-A", self.fake.url, CREDS, "Core One")
        server.PRINTERS["printer-a"] = self.prn
        self.addCleanup(server.PRINTERS.pop, "printer-a", None)
        with server._cache_lock:
            server._cache_save({})
        server._FILES_SEEN.pop("printer-a", None)
        server.SETTINGS.update({"security.api_auth": "off"})
        self.client = TestClient(server.mcp.streamable_http_app())
        p = mock.patch.object(server.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def listing(self):
        return {f["name"]: f for f in json.loads(server.list_sd_files("Printer-A"))}

    def warm(self):
        server._prewarm_prusalink(self.prn)


@unittest.skipUnless(HAVE, "needs the app requirements")
class TestServerListing(ServerCase):
    def test_the_first_listing_is_fast_and_honest(self):
        rows = self.listing()
        r = rows["PART_A~1.GCO"]
        self.assertEqual((r["display_name"], r["size"], r["thumb"], r["thumb_lazy"]),
                         ("part_a_0.4n_0.2mm_PETG_Printer-A HF_1h25m.gcode", 0, None, True))
        self.assertNotIn("thumb_ref", r)
        self.assertEqual([p for _m, p in self.fake.log if p.startswith("/thumb") or p.count("/") > 4 and "~" in p], [])

    def test_prewarm_fills_sizes_and_pictures_and_the_next_listing_has_them(self):
        self.listing()
        self.warm()
        rows = self.listing()
        r = rows["PART_A~1.GCO"]
        self.assertEqual((r["size"], r["thumb_lazy"]), (3792208, False))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}\.png", r["thumb"]))
        with open(server._thumb_path(r["thumb"]), "rb") as fh:
            self.assertEqual(fh.read(), FX.CORE_FILES["PART_A~1.GCO"]["png"])
        self.assertEqual((rows["NOPICT~1.GCO"]["thumb"], rows["NOPICT~1.GCO"]["thumb_lazy"]), (None, False))   # looked, none: not asked again
        self.assertEqual(rows["METAFI~1.GCO"]["weight_g"], 12.34)
        self.assertEqual(rows["METAFI~1.GCO"]["materials"], ["PLA", "PETG"])

    def test_a_second_pass_asks_the_printer_for_nothing(self):
        self.listing()
        self.warm()
        n = len(self.fake.log)
        self.warm()
        gets = [p for _m, p in self.fake.log[n:] if p.startswith("/thumb") or ("~" in p and p.count("/") > 4)]
        self.assertEqual(gets, [])

    def test_only_gets_ever_reach_the_printer(self):
        self.listing()
        self.warm()
        self.assertEqual({m for m, _p in self.fake.log}, {"GET"})

    def test_the_sizes_survive_a_hub_restart_through_the_cache(self):
        self.listing()
        self.warm()
        fresh = PrusaLinkPrinter("Printer-A", self.fake.url, CREDS)
        server.PRINTERS["printer-a"] = fresh
        r = self.listing()["PART_A~1.GCO"]
        self.assertEqual(r["size"], 3792208)
        self.assertEqual({x["name"]: x for x in fresh.files()}["PART_A~1.GCO"]["size"], 3792208)

    def test_a_busy_printer_gets_at_most_ten_files_per_pass(self):
        many = {f"F{i:03d}~1.GCO": {"display_name": f"f{i}.gcode", "size": 1000 + i, "ts": 1700000000 + i, "png": FX.png(8, 8)} for i in range(25)}
        self.fake.core.update(many)
        self.fake.state = "PRINTING"
        self.fake.job = {"id": 3, "name": "other", "progress": 10, "time_remaining": 600}
        self.listing()
        self.warm()
        rows = self.listing()
        self.assertEqual(sum(1 for r in rows.values() if not r["thumb_lazy"]), 10)          # looked at: 10 files, not 30
        self.assertEqual(sum(1 for r in rows.values() if r["thumb_lazy"]), len(rows) - 10)
        self.warm()
        self.assertEqual(sum(1 for r in self.listing().values() if not r["thumb_lazy"]), 20)      # the next pass continues

    def test_the_start_ticket_size_is_the_listing_size(self):
        d = json.loads(server.suggest_mapping("Printer-A", "PART_A~1.GCO"))
        self.assertEqual(d["file"], "PART_A~1.GCO")
        self.assertEqual(d["display_name"], "part_a_0.4n_0.2mm_PETG_Printer-A HF_1h25m.gcode")
        listing = {f["name"]: f for f in self.prn.files()}
        self.assertEqual(listing["PART_A~1.GCO"]["size"], 3792208)         # the number the ticket was issued for

    def test_job_names_find_the_files_costs_by_the_long_name(self):
        self.listing()
        key = server.costs.file_key("part_a_0.4n_0.2mm_PETG_Printer-A HF_1h25m.gcode")
        self.assertIn(key, server._FILES_SEEN["printer-a"])
        self.assertIn(server.costs.file_key("PART_A~1.GCO"), server._FILES_SEEN["printer-a"])


@unittest.skipUnless(HAVE, "needs the app requirements")
class TestServerPictures(ServerCase):
    def test_the_picture_route_serves_a_png_on_demand_and_caches_it(self):
        self.listing()
        r = self.client.get("/api/printer/Printer-A/filethumb", params={"file": "BRACKE~1.BGC"})
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/png"))
        self.assertEqual(r.content, FX.CORE_FILES["BRACKE~1.BGC"]["png"])
        n = len([1 for _m, p in self.fake.log if p == "/thumb/l/usb/BRACKE~1.BGC"])
        self.assertEqual(self.client.get("/api/printer/Printer-A/filethumb", params={"file": "BRACKE~1.BGC"}).status_code, 200)
        self.assertEqual(len([1 for _m, p in self.fake.log if p == "/thumb/l/usb/BRACKE~1.BGC"]), n)

    def test_no_picture_is_a_404_the_page_turns_into_words(self):
        r = self.client.get("/api/printer/Printer-A/filethumb", params={"file": "NOPICT~1.GCO"})
        self.assertEqual(r.status_code, 404)

    def test_a_file_that_is_not_listed_is_a_404_and_asks_the_printer_for_nothing(self):
        r = self.client.get("/api/printer/Printer-A/filethumb", params={"file": "../../etc/passwd"})
        self.assertEqual(r.status_code, 404)
        self.assertFalse([p for _m, p in self.fake.log if "passwd" in p or "etc" in p])

    def test_another_printer_type_has_no_such_route_data(self):
        class Octo:
            name, kind, uses_files_api = "Octo-A", "octoprint", True
            def files(self): return []
        server.PRINTERS["octo-a"] = Octo()
        self.addCleanup(server.PRINTERS.pop, "octo-a", None)
        self.assertEqual(self.client.get("/api/printer/Octo-A/filethumb", params={"file": "a.gcode"}).status_code, 404)


@unittest.skipUnless(HAVE, "needs the app requirements")
class TestServerDelete(ServerCase):
    def test_the_two_tap_flow_ends_in_a_delete_of_the_short_name(self):
        self.listing()
        self.warm()
        thumb = self.listing()["PART_A~1.GCO"]["thumb"]
        r = self.client.post("/api/printer/Printer-A/delete", json={"filename": "PART_A~1.GCO", "confirm": "PART_A~1.GCO"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(("DELETE", "/api/v1/files/usb/PART_A~1.GCO"), self.fake.log)
        self.assertFalse(os.path.exists(server._thumb_path(thumb)))        # its picture and cache entry went with it
        self.assertNotIn("PART_A~1.GCO", self.listing())

    def test_refused_while_that_printer_is_printing(self):
        self.fake.state = "PRINTING"
        self.fake.job = {"id": 3, "name": "other", "progress": 10, "time_remaining": 600}
        r = self.client.post("/api/printer/Printer-A/delete", json={"filename": "PART_A~1.GCO", "confirm": "PART_A~1.GCO"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("REFUSED", r.json()["error"])
        self.assertNotIn("DELETE", [m for m, _p in self.fake.log])

    def test_refused_when_the_file_is_not_on_the_current_list(self):
        for name in ("NOTHERE~1.GCO", "part_a_0.4n_0.2mm_PETG_Printer-A HF_1h25m.gcode", "../PART_A~1.GCO"):
            r = self.client.post("/api/printer/Printer-A/delete", json={"filename": name, "confirm": name})
            self.assertEqual(r.status_code, 409, name)
        self.assertNotIn("DELETE", [m for m, _p in self.fake.log])

    def test_the_confirmation_must_match(self):
        r = self.client.post("/api/printer/Printer-A/delete", json={"filename": "PART_A~1.GCO", "confirm": "other"})
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("DELETE", [m for m, _p in self.fake.log])


@unittest.skipUnless(HAVE, "needs the app requirements")
class TestServerDetail(ServerCase):
    def detail(self, name, **params):
        r = self.client.get("/api/printer/Printer-A/filedetail", params={"file": name, **params})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_everything_the_sheet_shows(self):
        d = self.detail("PART_A~1.GCO")
        self.assertEqual(d["display_name"], "part_a_0.4n_0.2mm_PETG_Printer-A HF_1h25m.gcode")
        self.assertEqual((d["print_seconds"], d["print_time"], d["size"], d["storage"]), (5100, "1h25m", 3792208, "USB stick"))
        self.assertEqual((d["filament_type"], d["materials"], d["layer_mm"], d["nozzle_mm"]), ("PETG", ["PETG"], 0.2, 0.4))
        self.assertEqual(d["modified"], 1775424909)
        self.assertTrue(d["thumb"])
        self.assertTrue(d["can_delete"] and d["can_rename"])

    def test_the_sheet_reads_only_what_it_needs_with_gets(self):
        self.detail("PART_A~1.GCO")
        self.assertEqual({m for m, _p in self.fake.log}, {"GET"})
        n = len(self.fake.log)
        self.detail("PART_A~1.GCO")
        self.assertEqual([p for _m, p in self.fake.log[n:] if p.startswith("/thumb") or "~" in p], [])    # cached: no more traffic

    def test_a_file_that_is_not_listed_is_a_404(self):
        r = self.client.get("/api/printer/Printer-A/filedetail", params={"file": "NOTHERE~1.GCO"})
        self.assertEqual(r.status_code, 404)

    def test_the_cost_is_estimated_from_the_file_numbers_at_todays_rates(self):
        d = self.detail("METAFI~1.GCO")
        c = d["cost"]
        self.assertTrue(c["available"])
        self.assertAlmostEqual(c["material"], 12.34 / 1000 * 20.0, places=3)         # PLA, default $20/kg
        self.assertAlmostEqual(c["hours"], 3930 / 3600, places=3)
        self.assertEqual(set(("material", "electricity", "wear", "total", "includes_machine_costs", "total_with_machine", "currency")) - set(c), set())
        self.assertAlmostEqual(c["total_with_machine"], c["material"] + c["electricity"] + c["wear"], places=3)

    def test_cost_default_is_filament_only_and_the_request_can_flip_it(self):
        base = self.detail("METAFI~1.GCO")["cost"]
        self.assertFalse(base["includes_machine_costs"])
        self.assertAlmostEqual(base["total"], base["material"], places=4)
        on = self.detail("METAFI~1.GCO", machine=1)["cost"]
        self.assertTrue(on["includes_machine_costs"])
        self.assertAlmostEqual(on["total"], on["material"] + on["electricity"] + on["wear"], places=3)
        off = self.detail("METAFI~1.GCO", machine=0)["cost"]
        self.assertFalse(off["includes_machine_costs"])
        self.assertAlmostEqual(off["total"], off["material"], places=4)

    def test_a_missing_filament_weight_is_said_in_words(self):
        c = self.detail("PART_A~1.GCO")["cost"]                  # the long name gives time and material, not grams
        self.assertTrue(c["available"])
        self.assertEqual(c["material"], 0)
        self.assertIn("Filament cost unknown", c["note"])
        c = self.detail("NOPICT~1.GCO")["cost"]
        self.assertFalse(c["available"])
        self.assertIn("does not say", c["reason"])

    def test_last_printed_comes_from_the_history_under_the_long_name(self):
        self.assertIsNone(self.detail("PART_A~1.GCO")["last_printed"])
        long_name = "part_a_0.4n_0.2mm_PETG_Printer-A HF_1h25m"
        server.JOBLOG._insert(printer="Printer-A", file=long_name, file_key=server.costs.file_key(long_name),
                              started_at=1780000000.0, ended_at=1780005000.0, result="finished", percent=100.0)
        lp = self.detail("PART_A~1.GCO")["last_printed"]
        self.assertEqual((lp["printer"], lp["result"], lp["ts"], lp["attempts"]), ("Printer-A", "finished", 1780005000.0, 1))


@unittest.skipUnless(HAVE, "needs the app requirements")
class TestOtherPrinterTypes(unittest.TestCase):
    """The detail sheet and the badge for Bambu, OctoPrint and Moonraker rows (fake drivers: no network)."""

    def setUp(self):
        with server._cache_lock:
            server._cache_save({})
        server._FILES_SEEN.clear()

    def test_octoprint_uses_the_cached_header_and_tail_facts(self):
        class Octo:
            name, kind, uses_files_api, model = "Octo-A", "octoprint", True, ""
            def files(self):
                return [{"name": "cube.gcode", "size": 1234, "modified": 1700000000, "print_seconds": 600, "print_time": "0h10m",
                         "filament_mm": 3000.0, "origin": "local"}]
            def upload(self, *a): pass
            def download(self, *a): pass
        prn = Octo()
        with server._cache_lock:
            server._cache_save({"Octo-A|cube.gcode|1234": {"thumb": None, "slicer_model": None, "materials": ["PETG", "PLA"]}})
        d = server._file_detail(prn, "cube.gcode")
        self.assertEqual((d["materials"], d["storage"], d["print_time"], d["size"]), (["PETG", "PLA"], "OctoPrint storage", "0h10m", 1234))
        self.assertTrue(d["cost"]["available"])
        self.assertAlmostEqual(d["cost"]["grams"], server.costs.grams_from_mm(3000.0, None), places=1)         # length -> grams

    def test_bambu_shows_filaments_with_colour_words(self):
        class Bambu:
            name, kind, model = "Bambu-A", "bambu", "Bambu Lab P1S"
            def sd_listing_cached(self):
                return [{"name": "tag.gcode.3mf", "size": 5000, "modified": 1700000000}]
        meta = {"print_seconds": 3600, "print_time": "1h00m", "weight_g": 20.0, "thumb": None,
                "filaments": [{"id": 1, "type": "PETG", "color": "#000000", "used_g": 12.0},
                              {"id": 2, "type": "PLA", "color": "#FFFFFF", "used_g": 8.0}]}
        with server._cache_lock:
            server._cache_save({"Bambu-A|tag.gcode.3mf|5000": meta})
        d = server._file_detail(Bambu(), "tag.gcode.3mf")
        self.assertEqual([(f["type"], f["used_g"]) for f in d["filaments"]], [("PETG", 12.0), ("PLA", 8.0)])
        self.assertTrue(all(f["color_name"] for f in d["filaments"]))
        self.assertEqual((d["materials"], d["storage"], d["weight_g"]), (["PETG", "PLA"], "SD card", 20.0))
        self.assertAlmostEqual(d["cost"]["material"], (12 * 22 + 8 * 20) / 1000, places=3)

    def test_bambu_without_cached_metadata_says_so_instead_of_guessing(self):
        class Bambu:
            name, kind, model = "Bambu-B", "bambu", ""
            def sd_listing_cached(self):
                return [{"name": "a.gcode.3mf", "size": 10, "modified": 1}]
        d = server._file_detail(Bambu(), "a.gcode.3mf")
        self.assertEqual((d["materials"], d["filaments"], d["print_time"]), ([], [], None))
        self.assertFalse(d["cost"]["available"])
        self.assertFalse(d["metadata_loaded"])

    def test_moonraker_rows_carry_material_layer_and_weight(self):
        from test_moonraker import FakeMoonraker
        from moonraker import MoonrakerPrinter
        fake = FakeMoonraker()
        self.addCleanup(fake.close)
        fake.meta["benchy.gcode"].update({"filament_type": "PETG;PLA", "filament_weight_total": 11.5,
                                          "layer_height": 0.24, "nozzle_diameter": 0.4})
        row = {r["name"]: r for r in MoonrakerPrinter("Klipper-A", fake.url).files()}["benchy.gcode"]
        self.assertEqual((row["materials"], row["weight_g"], row["layer_mm"], row["nozzle_mm"]), (["PETG", "PLA"], 11.5, 0.24, 0.4))
        self.assertEqual(row["filament_type"], "PETG")

    def test_octoprint_tail_read_uses_a_range_and_never_a_whole_file(self):
        import http.server
        body = (b"G1 X1\n" * 20000) + b"; filament_type = PLA;PETG\n; layer_height = 0.2\n"
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                seen.append(self.headers.get("Range"))
                m = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range") or "")
                if m and not IGNORE[0]:
                    a, b = int(m.group(1)), int(m.group(2))
                    chunk = body[a:b + 1]
                    self.send_response(206)
                else:
                    chunk = body
                    self.send_response(200)
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
        IGNORE = [False]
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        from bambu import OctoPrintPrinter
        prn = OctoPrintPrinter("Octo-A", f"http://127.0.0.1:{httpd.server_address[1]}", "KEY")
        tail = prn.download_tail("cube.gcode", len(body))
        self.assertLessEqual(len(tail), 65536)
        self.assertEqual(gcodemeta.from_gcode_tail(tail)["materials"], ["PLA", "PETG"])
        self.assertEqual(seen[0], f"bytes={len(body) - 65536}-{len(body) - 1}")
        IGNORE[0] = True
        self.assertIsNone(prn.download_tail("cube.gcode", len(body)))        # the server ignored Range: the file is not read
        self.assertEqual(prn.download_tail("empty.gcode", 0), b"")

    def test_weight_layer_and_nozzle_the_file_itself_states_reach_the_list_and_the_sheet(self):
        class Core:
            name, kind, uses_files_api, model = "Core-X", "prusalink", True, ""
            def download_head(self, name, n=1048576):
                return bgcode([(3, 0, "filament_type=PLA;PETG\nfilament used [g]=9.5\nlayer_height=0.2\nnozzle_diameter=0.4\n")])
        e = server._octo_meta_for(Core(), "x.bgcode", 777)
        self.assertEqual((e["materials"], e["weight_g"], e["layer_mm"], e["nozzle_mm"]), (["PLA", "PETG"], 9.5, 0.2, 0.4))

    def test_the_octoprint_prewarm_stores_materials_and_asks_for_the_tail_only_once(self):
        class Octo:
            name, kind, uses_files_api = "Octo-B", "octoprint", True
            calls = []
            def download_head(self, name, n=1048576):
                self.calls.append("head"); return b"; thumbnail\n"
            def download_tail(self, name, size, n=65536):
                self.calls.append("tail"); return b"; filament_type = ABS\n"
        prn = Octo()
        e = server._octo_meta_for(prn, "a.gcode", 100)
        self.assertEqual(e["materials"], ["ABS"])
        server._octo_meta_for(prn, "a.gcode", 100)
        self.assertEqual(prn.calls, ["head", "tail"])
        e = server._octo_meta_for(prn, "b.bgcode", 100)                     # bgcode: its head, no tail
        self.assertEqual((e["materials"], prn.calls), ([], ["head", "tail", "head"]))


# ============================================================== the page

NODE = shutil.which("node")


class TestPage(unittest.TestCase):
    """Static checks on app/dashboard.html (no browser)."""

    def test_rows_show_the_long_name_and_call_the_api_with_the_short_one(self):
        row = HTML[HTML.index("function fileRowHtml(f, i){"):HTML.index("// ---- the file detail sheet")]
        self.assertIn("const shown = fileShow(f);", row)
        self.assertIn("esc(filesBase(shown))", row)
        self.assertIn('data-f="${esc(name)}"', row)                  # name = the path every API call uses
        self.assertNotIn("esc(filesBase(name))", row)
        self.assertIn("function fileShow(f)" if "function fileShow(f)" in HTML else "const fileShow = f =>", HTML)

    def test_every_row_has_a_picture_or_the_words_no_picture(self):
        row = HTML[HTML.index("function fileRowHtml(f, i){"):HTML.index("// ---- the file detail sheet")]
        self.assertIn("NO<br>PICTURE", row)
        self.assertIn('onerror="thumbFail(this)"', row)
        self.assertIn("function thumbFail(img)", HTML)
        self.assertIn("/filethumb", HTML)

    def test_the_badge_is_words_just_before_print(self):
        row = HTML[HTML.index("function fileRowHtml(f, i){"):HTML.index("// ---- the file detail sheet")]
        self.assertLess(row.index("${badgeHtml(f)}"), row.index('class="primary" onclick="askPrint'))
        css = re.search(r"\.fbadge \{[^}]*\}", HTML).group(0)
        size = int(re.search(r"font-size:(\d+)px", css).group(1))
        body = int(re.search(r"\.frow \.fname \{ font-size:(\d+)px", HTML).group(1))
        self.assertGreater(size, body)                              # larger than the row's body text
        self.assertIn("text-transform:uppercase", css)
        badge = HTML[HTML.index("function badgeHtml(f){"):HTML.index("function thumbFail")]
        self.assertIn('aria-label="Material: ', badge)             # the colour word, if any, is in the label too

    def test_tapping_a_row_opens_the_detail_sheet_on_every_layout(self):
        self.assertIn("fileMenu:  b => fileDetail(b.dataset.f),", HTML)       # no `if(L5)` any more
        self.assertGreaterEqual(HTML.count('data-act="fileMenu"'), 3)       # name block, picture, placeholder
        self.assertIn('.frow .grow[data-act="fileMenu"]', HTML)         # Enter on the row opens it too

    def test_the_sheet_has_the_buttons_and_the_words_only_details(self):
        sheet = HTML[HTML.index("function fdHtml("):HTML.index("async function fileDetail(")]
        everything = HTML[HTML.index("function fdPicture"):HTML.index("async function fileDetail(")]
        for word in ("Print time", "Filament", "Layer height", "Nozzle", "File size", "Modified", "Storage", "Estimated cost",
                     "Last printed", "Not in the print history yet", "NO PICTURE"):
            self.assertIn(word, everything)
        for act in ("fdPrint", "fdDelete", "fdRename", "fdBack"):
            self.assertIn(f'data-act="{act}"', sheet)
            self.assertIn(f"{act}:", HTML)
        self.assertIn("Loading details", sheet)                     # lazy: the sheet opens at once
        self.assertIn("data-act=\"fdCost\" data-m=\"0\"", HTML)
        self.assertIn("Filament only", HTML)
        self.assertIn("+ Print time", HTML)

    def test_sheet_buttons_are_finger_sized_and_the_picture_is_big(self):
        self.assertRegex(HTML, r"\.fd-act button \{[^}]*min-height:48px")
        self.assertRegex(HTML, r"\.fd-chips button \{[^}]*min-height:44px")
        self.assertRegex(HTML, r"#mbody:has\(\.fdw\) \{ width:880px")
        self.assertRegex(HTML, r"\.fd-pic \{ flex:0 0 min\(44%, 380px\)")

    def test_delete_still_asks_twice_and_names_the_long_name(self):
        ask = HTML[HTML.index("function askDelete(filename){"):HTML.index("async function doDelete(){")]
        self.assertIn("Delete this file?", ask)
        self.assertIn("Back", ask)
        self.assertIn("Yes, delete", ask)
        self.assertIn("showName(filename)", ask)
        self.assertIn("body: JSON.stringify({filename, confirm: filename})", HTML)      # the short name goes to the API

    def test_the_start_confirm_uses_the_long_name(self):
        self.assertIn("String(d.display_name || d.file)", HTML)

    def test_the_row_has_no_rename_button_and_the_sheet_hides_it_on_a_kiosk(self):
        row = HTML[HTML.index("function fileRowHtml(f, i){"):HTML.index("// ---- the file detail sheet")]
        self.assertNotIn("frename", row)
        self.assertNotIn("rnOpenFile", row)
        self.assertIn("const canRename = !KIOSK && (d ? d.can_rename !== false : true);", HTML)

    def test_the_old_material_chip_is_gone_and_5in_keeps_the_badge(self):
        self.assertNotIn("class=\"mat m-", HTML[HTML.index("function fileRowHtml(f, i){"):HTML.index("// ---- the file detail sheet")])
        self.assertRegex(HTML, r"html\.lay-5in \.frow \.fbadge \{")

    def test_the_files_page_spends_its_rows_on_files(self):
        """Screen-design rule (vertical space is precious): rarely used actions sit behind ONE More button in the title bar,
        the file list's own header is a plain one-line title, and no empty progress-bar band shows while nothing prints."""
        self.assertIn('class="fmore-tb" data-act="filesMore"', HTML)
        head = HTML[HTML.index("const filesHead = extra => L5"):HTML.index("// ---- files: names, pictures")]
        self.assertNotIn("<button", head.split(": '<div class=\"fhead\">Jobs on SD card'")[1].split("function filesMore")[0])
        self.assertIn("function filesMore(){", HTML)
        self.assertIn("Back up files", HTML[HTML.index("function filesMore(){"):HTML.index("function filesMore(){") + 900])
        self.assertIn('html:not(.lay-5in) #printer .tbar > button[onclick^="bkStart"]', HTML)      # the two old title-bar buttons
        self.assertNotIn("$('p-bar').parentNode.style.display", HTML)                                # the bar lives in the progress card now: no empty band is spent on it
        self.assertIn('<span id="p-sort" class="fsort"', HTML)                                     # the sort buttons share this one toolbar row
        self.assertIn("html.lay-5in #printer .tbar .fmore-tb { display:none; }", HTML)             # the 5-inch keeps its own More sheet

    def test_the_history_toolbar_is_one_row_with_the_cost_switch_in_it(self):
        head = HTML[HTML.index("function hiHead(){"):HTML.index("function hiMore(){")]
        for part in ('class="hf-btn"', 'class="seg hi-per"', 'class="hi-mach"', 'class="hi-more"', ">Close</button>"):
            self.assertIn(part, head)
        self.assertNotIn("bkdl", head)                      # CSV is behind More
        self.assertIn("Download CSV", HTML[HTML.index("function hiMore(){"):HTML.index("function hiMore(){") + 900])
        self.assertIn('data-act="hiMore"', head)
        kpi = HTML[HTML.index("function hiRender(){"):HTML.index("function hiRender(){") + 4000]
        self.assertNotIn("${costTog}", kpi)                 # the switch is no longer a block inside the cost card

    def test_the_list_keeps_its_scroll_place(self):
        self.assertIn("FS.shown === CUR ? $('files').scrollTop : 0", HTML)


@unittest.skipUnless(NODE, "needs node")
class TestPageFunctions(unittest.TestCase):
    """Run the page's own file-row / badge / sheet functions in node with stubs (no DOM)."""

    @classmethod
    def setUpClass(cls):
        a = HTML.index("const FS = {files:[]")
        b = HTML.index("function filesRender(){")
        src = HTML[a:b]
        c = HTML.index("async function fileDetail(")
        pre = ("const L5 = false, CUR = 'Printer-A', BUSY_STATES = ['RUNNING','PREPARE','PAUSE'];\nlet CTL = {d:null}, KIOSK = false; const setKiosk = v => { KIOSK = v; };\n"
               "const esc = s => String(s == null ? '' : s).replace(/[&<>\"'`]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;','`':'&#96;'}[c]));\n"
               "const pu = s => encodeURIComponent(String(s == null ? '' : s));\nconst papi = (n, t) => '/api/printer/' + pu(n) + (t || '');\n"
               "const IC = n => ''; const clampPct = n => n; const $ = () => null;\n")
        cls.js = pre + src + "\nmodule.exports = {fileMats, badgeHtml, fileRowHtml, fdHtml, FS, fileShow, showName, setKiosk};"

    def run_js_plain(self, code):
        r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def run_js(self, body):
        code = self.js + "\n" + body
        r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_badge_single_double_many_and_unknown(self):
        out = self.run_js("""
const t = (f) => { const h = badgeHtml(f); return h.replace(/<[^>]+>/g, '|').replace(/\\|+/g, '|'); };
console.log(JSON.stringify({
  one: t({materials:['petg']}), two: t({materials:['PETG','PLA']}), many: t({materials:['PETG','PLA','ABS','TPU']}),
  none: badgeHtml({}), empty: badgeHtml({materials:[]}), single: t({filament_type:'pla'}),
  bambu: badgeHtml({filaments:[{type:'PETG',color_name:'Black'},{type:'PLA',color_name:'White'}]}),
  dup: t({materials:['PLA','pla']})
}));
""")
        self.assertEqual(out["one"], "|PETG|")
        self.assertEqual(out["two"], "|PETG|PLA|")
        self.assertEqual(out["many"], "|PETG|+3|")
        self.assertEqual((out["none"], out["empty"]), ("", ""))
        self.assertEqual((out["single"], out["dup"]), ("|PLA|", "|PLA|"))
        self.assertIn('aria-label="Material: PETG (Black) + PLA (White)"', out["bambu"])
        self.assertIn('class="fbadge two"', out["bambu"])

    def test_row_shows_the_long_name_keeps_the_short_one_in_data_f_and_a_picture_or_words(self):
        out = self.run_js("""
const f1 = {name:'PART_A~1.GCO', display_name:'part_a_0.4n_0.2mm_PETG_PrinterA_1h25m.gcode', thumb:'abc.png', materials:['PETG'], print_time:'1h25m', origin:'local', size:5};
const f2 = {name:'NOPICT~1.GCO', display_name:'no picture here.gcode', thumb:null, thumb_lazy:false, origin:'local'};
const f3 = {name:'LAZY~1.GCO', display_name:'lazy one.gcode', thumb:null, thumb_lazy:true, origin:'local'};
console.log(JSON.stringify([fileRowHtml(f1,0), fileRowHtml(f2,1), fileRowHtml(f3,2)]));
""")
        a, b, c = out
        self.assertIn('<div class="fname">part_a_0.4n_0.2mm_PETG_PrinterA_1h25m</div>', a)
        self.assertNotIn('<div class="fname">PART_A~1.GCO', a)
        self.assertIn('onclick="askPrint(this.dataset.f)" data-f="PART_A~1.GCO"', a)
        self.assertIn('onclick="askDelete(this.dataset.f)" data-f="PART_A~1.GCO"', a)
        self.assertIn('src="/api/thumb/abc.png"', a)
        self.assertLess(a.index('class="fbadge'), a.index('class="primary"'))
        self.assertIn("NO<br>PICTURE", b)
        self.assertNotIn("<img", b)
        self.assertNotIn("fbadge", b)                                        # unknown material: no badge, no guess
        self.assertIn('src="/api/printer/Printer-A/filethumb?file=LAZY~1.GCO"', c)
        for row in out:
            self.assertNotIn("Rename", row)                                  # no Rename on the rows, only in the detail sheet
            self.assertNotIn("frename", row)

    def test_rename_is_in_the_sheet_on_desktop_and_phone_and_never_on_a_wall_screen(self):
        out = self.run_js("""
const row = {name:'A~1.GCO', display_name:'a.gcode'};
const d = {name:'A~1.GCO', display_name:'a.gcode', can_rename:true, cost:{available:false, reason:'x'}};
const normal = [fdHtml(row, null), fdHtml(row, d)];
setKiosk(true);
const kiosk = [fdHtml(row, null), fdHtml(row, d)];
console.log(JSON.stringify({normal, kiosk}));
""")
        for h in out["normal"]:
            self.assertIn('data-act="fdRename"', h)
        for h in out["kiosk"]:
            self.assertNotIn("Rename", h)
            self.assertIn('data-act="fdPrint"', h)
            self.assertIn('data-act="fdDelete"', h)
            self.assertIn('data-act="fdBack"', h)

    def test_the_kiosk_flag_is_an_explicit_param_never_guessed_from_the_size(self):
        fn = HTML[HTML.index("function kioskParse(search){"):HTML.index("const L5 = l5Parse(location.search);")]
        self.assertNotRegex(fn, r"innerWidth|innerHeight|matchMedia|screen\\.")
        out = self.run_js_plain(fn + """
const t = s => kioskParse(s);
console.log(JSON.stringify([t('?kiosk=1'), t('?group=Home&kiosk=true'), t('?kiosk=YES'), t('?kiosk=on'), t(''), t('?kiosk=0'), t('?kiosk=no'), t('?kiosk='), t('?layout=5in'), t(null)]));
""")
        self.assertEqual(out, [True, True, True, True, False, False, False, False, False, False])
        self.assertIn("const KIOSK = L5 || kioskParse(location.search);", HTML)      # the 5-inch layout is a kiosk too

    def test_the_sheet_in_words(self):
        out = self.run_js("""
const row = {name:'PART_A~1.GCO', display_name:'part a.gcode'};
FS.files = [row];
const loading = fdHtml(row, null);
const d = {name:'PART_A~1.GCO', display_name:'part a.gcode', thumb:'abc.png', print_seconds:5100, weight_g:12.3, filament_type:'PETG',
  materials:['PETG'], filaments:[{type:'PETG', color_name:null, used_g:12.3}], layer_mm:0.2, nozzle_mm:0.4, size:3792208, modified:1775424909,
  storage:'USB stick', can_rename:true,
  cost:{available:true, grams:12.3, currency:'$', material:0.27, electricity:0.01, wear:0.43, total:0.27, includes_machine_costs:false, note:null},
  last_printed:{printer:'Printer-B', result:'finished', ts:1780005000, attempts:2}};
const off = fdHtml(row, d);
d.cost.includes_machine_costs = true; d.cost.total = 0.71;
const on = fdHtml(row, d);
d.last_printed = null; d.cost = {available:false, reason:'The file does not say how much filament it uses.'}; d.print_seconds = null; d.weight_g = null; d.size = null;
const bare = fdHtml(row, d);
console.log(JSON.stringify({loading, off, on, bare, err: fdHtml(row, null, 'boom')}));
""")
        self.assertIn("Loading details", out["loading"])
        off = out["off"]
        for s in ("1 h 25 min", "estimated by the slicer", "12.3 g · PETG", "0.2 mm", "0.4 mm", "3.6 MB", "USB stick", "Filament only",
                  "Filament only (no power or wear)", "finished on Printer-B", "2 attempts in the history",
                  'src="/api/thumb/abc.png"', "On the printer as PART_A~1.GCO"):
            self.assertIn(s, off)
        self.assertIn('data-m="0" aria-pressed="true"', off)
        self.assertIn('data-m="1" aria-pressed="false"', off)
        on = out["on"]
        self.assertIn("Filament $0.27 · Power $0.01 · Wear $0.43", on)
        self.assertIn('data-m="1" aria-pressed="true"', on)
        bare = out["bare"]
        self.assertIn("Not in the print history yet", bare)
        self.assertIn("unknown", bare)
        self.assertIn("The file does not say", bare)
        for act in ("fdPrint", "fdDelete", "fdRename", "fdBack"):
            self.assertIn(f'data-act="{act}"', off)
        self.assertIn("Could not load the details: boom", out["err"])
        # an unknown cost is ONE short line and carries no switch (nothing to switch)
        self.assertNotIn("fdCost", bare)
        self.assertRegex(bare, r"Estimated cost</dt><dd>unknown <small[^>]*>\(The file does not say how much filament it uses\)</small></dd>")


if __name__ == "__main__":
    unittest.main()
