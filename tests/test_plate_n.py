"""A .gcode.3mf holds ONE plate of its project (OrcaSlicer exports one file per plate): the plate-4 file has
Metadata/plate_4.gcode and plate_4.png (and still the project's plate_1.png). The Hub must use the plate the file really
holds - for the picture, for what it tells the printer to print, for the layer count and the part count.

    python3 tests/test_plate_n.py        (needs the app requirements)

Synthetic zips and in-process fake printers only; the server module is imported with throwaway config / cache directories
and socket connects are blocked, so nothing here can reach a printer or the LAN.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

_TMP = tempfile.mkdtemp(prefix="plate-n-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ["BAMBU_MCP_API_AUTH"] = "off"
os.environ.pop("FABRICA_PLATE_BACKFILL", None)

import bambu  # noqa: E402
import joblog  # noqa: E402
import library  # noqa: E402
from bambu import BambuPrinter, extract_plate_png, parse_3mf_meta, plate_number, plate_png_name  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover
    HAVE = False
    _ERR = e

PNG = {n: b"\x89PNG-" + n.encode() for n in ("p1", "p1s", "p4", "p4s", "p5")}     # distinguishable fake pictures


def setUpModule():
    global _NET
    _NET = mock.patch("socket.socket.connect", side_effect=AssertionError("a test tried to open a network connection"))
    _NET.start()


def tearDownModule():
    _NET.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def slice_info(*plates):
    """plates = [(index, [object names])]"""
    body = ""
    for idx, objs in plates:
        body += (f'<plate><metadata key="index" value="{idx}"/><metadata key="printer_model_id" value="C12"/>'
                 '<metadata key="prediction" value="3600"/><metadata key="weight" value="12.5"/>'
                 + "".join(f'<object identify_id="{i}" name="{o}" skipped="false"/>' for i, o in enumerate(objs, 10))
                 + '<filament id="1" type="PLA" color="#FF0000" used_m="1.0" used_g="3.0"/></plate>')
    return '<?xml version="1.0" encoding="UTF-8"?><config>' + body + '</config>'


def make_3mf(gcodes=(4,), pngs=("plate_4.png",), plates=((4, ["Clip"]),), layers=None) -> bytes:
    """gcodes = plate numbers that have Metadata/plate_N.gcode; pngs = Metadata/<name> pictures (content from PNG_BY_NAME)."""
    layers = layers or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info(*plates))
        z.writestr("Metadata/project_settings.config", json.dumps({"printer_model": "Bambu Lab P1S", "layer_height": "0.2"}))
        z.writestr("3D/3dmodel.model", '<?xml version="1.0"?><model/>')
        for n in gcodes:
            z.writestr(f"Metadata/plate_{n}.gcode", f"; total layer number: {layers.get(n, 10 * n)}\nG28\n")
        for name in pngs:
            z.writestr(f"Metadata/{name}", PNG_BY_NAME[name])
    return buf.getvalue()


PNG_BY_NAME = {"plate_1.png": PNG["p1"], "plate_1_small.png": PNG["p1s"], "plate_4.png": PNG["p4"],
               "plate_4_small.png": PNG["p4s"], "plate_5.png": PNG["p5"]}


class TestPlateDetection(unittest.TestCase):
    def test_single_gcode_is_the_plate(self):
        self.assertEqual(plate_number(["Metadata/plate_4.gcode", "Metadata/plate_1.png"]), 4)
        self.assertEqual(plate_number(["Metadata/plate_1.gcode"]), 1)

    def test_several_gcodes_prefer_the_one_slice_info_names_else_lowest(self):
        names = ["Metadata/plate_2.gcode", "Metadata/plate_5.gcode"]
        self.assertEqual(plate_number(names, slice_info((5, ["a"])).encode()), 5)
        self.assertEqual(plate_number(names, slice_info((9, ["a"]))), 2)           # names a plate that is not here
        self.assertEqual(plate_number(names), 2)

    def test_no_gcode_uses_slice_info_else_1(self):
        self.assertEqual(plate_number(["Metadata/plate_1.png"]), 1)
        self.assertEqual(plate_number(["Metadata/plate_1.png"], slice_info((3, ["a"]))), 3)
        self.assertEqual(plate_number([]), 1)

    def test_parse_3mf_meta_reports_it(self):
        m = parse_3mf_meta(make_3mf())
        self.assertEqual(m["plate_n"], 4)
        self.assertEqual(m["plates"], ["Metadata/plate_4.gcode"])
        self.assertEqual(m["meta_v"], bambu.META_VERSION)
        self.assertEqual(parse_3mf_meta(make_3mf(gcodes=(1,), pngs=("plate_1.png",), plates=((1, ["a"]),)))["plate_n"], 1)

    def test_plate_gcode_from_cached_meta(self):
        f = bambu.plate_gcode_from_meta
        self.assertEqual(f({"plates": ["Metadata/plate_4.gcode"]}), "Metadata/plate_4.gcode")      # an entry from before plate_n
        self.assertEqual(f({"plates": ["Metadata/plate_2.gcode", "Metadata/plate_5.gcode"], "plate_n": 5}), "Metadata/plate_5.gcode")
        self.assertEqual(f({"plates": ["Metadata/plate_12.gcode", "Metadata/plate_3.gcode"]}), "Metadata/plate_3.gcode")   # numeric, not text, order
        self.assertIsNone(f({"plates": []}))
        self.assertIsNone(f({"error": "no slice_info.config"}))
        self.assertIsNone(f(None))


class TestPicture(unittest.TestCase):
    def test_plate_4_file_gets_its_own_picture_not_plate_1(self):
        data = make_3mf(pngs=("plate_1.png", "plate_4.png"))
        self.assertEqual(extract_plate_png(data), PNG["p4"])

    def test_plate_4_only(self):
        self.assertEqual(extract_plate_png(make_3mf(pngs=("plate_4.png",))), PNG["p4"])

    def test_small_picture_when_there_is_no_full_size_one(self):
        self.assertEqual(extract_plate_png(make_3mf(pngs=("plate_1.png", "plate_4_small.png"))), PNG["p4s"])

    def test_two_files_of_one_project_show_different_pictures(self):
        a = make_3mf(gcodes=(4,), pngs=("plate_1.png", "plate_4.png"))
        b = make_3mf(gcodes=(5,), pngs=("plate_1.png", "plate_5.png"), plates=((5, ["Clip"]),))
        self.assertNotEqual(extract_plate_png(a), extract_plate_png(b))

    def test_plate_1_only(self):
        data = make_3mf(gcodes=(1,), pngs=("plate_1.png",), plates=((1, ["a"]),))
        self.assertEqual(extract_plate_png(data), PNG["p1"])

    def test_own_picture_missing_falls_back_to_plate_1_then_any(self):
        self.assertEqual(extract_plate_png(make_3mf(pngs=("plate_1.png",))), PNG["p1"])
        self.assertEqual(extract_plate_png(make_3mf(pngs=("plate_1_small.png",))), PNG["p1s"])
        self.assertEqual(extract_plate_png(make_3mf(pngs=("plate_5.png",))), PNG["p5"])     # any plate picture at all

    def test_no_picture_is_none_not_an_error(self):
        self.assertIsNone(extract_plate_png(make_3mf(pngs=())))
        self.assertIsNone(plate_png_name([], 4))


class TestStartPrintCommand(unittest.TestCase):
    def printer(self):
        prn = BambuPrinter("T", "127.0.0.1", "SERIAL", "code", "Bambu Lab P1S")
        prn.request = mock.Mock()
        return prn

    def test_sends_the_plate_it_is_given(self):
        prn = self.printer()
        with mock.patch.object(bambu._controls, "mark_start_sent"):
            prn.start_print("job.gcode.3mf", [0], plate="Metadata/plate_4.gcode")
        sent = prn.request.call_args[0][0]["print"]
        self.assertEqual(sent["command"], "project_file")
        self.assertEqual(sent["param"], "Metadata/plate_4.gcode")

    def test_default_stays_plate_1_as_the_last_resort(self):
        prn = self.printer()
        with mock.patch.object(bambu._controls, "mark_start_sent"):
            prn.start_print("job.gcode.3mf", [0])
        self.assertEqual(prn.request.call_args[0][0]["print"]["param"], "Metadata/plate_1.gcode")


class FakeBambu:
    kind = "bambu"
    has_camera = False
    uses_files_api = False

    def __init__(self, name, model="Bambu Lab P1S", serial="01P00A000000001"):
        self.name, self.model, self.serial = name, model, serial
        self.files, self.downloads, self.started, self.state = {}, 0, [], "IDLE"

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": self.state, "job": "", "percent": 0, "remaining_min": None}

    def refresh(self, wait=0):
        pass

    def ams_trays(self):
        return [{"tray": 0, "slot_1based": 1, "type": "PLA", "color": "#FF0000"}]

    def sd_list(self):
        return [{"name": n, "size": len(b), "modified": ""} for n, b in self.files.items()]

    def sd_download(self, filename):
        self.downloads += 1
        return self.files[filename]

    def start_print(self, filename, ams_mapping, plate="Metadata/plate_1.gcode"):
        self.started.append((filename, plate))


@unittest.skipUnless(HAVE, "the app's dependencies are not installed")
class ServerBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.mcp.streamable_http_app())

    def setUp(self):
        server.PRINTERS.clear()
        server._START_PENDING.clear()
        server.START_TICKETS.clear()
        server.SPOOL_MEMORY.clear()
        try:
            os.remove(server.CACHE_PATH)
        except OSError:
            pass
        p = mock.patch.object(server.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        self.p = FakeBambu("P1")
        server.PRINTERS["p1"] = self.p

    def suggest(self, name):
        r = self.client.post("/api/printer/P1/suggest", json={"filename": name})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def start(self, name, d):
        return self.client.post("/api/printer/P1/start", json={
            "filename": name, "ams_mapping": d["ams_mapping"], "confirm": d["confirm_string"], "plate_clear": True})

    def cache_entry(self, name):
        with server._cache_lock:
            return server._cache_load()[f"P1|{name}|{len(self.p.files[name])}"]


class TestServerStart(ServerBase):
    def test_a_plate_4_file_is_started_as_plate_4(self):
        self.p.files["p4.gcode.3mf"] = make_3mf(pngs=("plate_1.png", "plate_4.png"))
        d = self.suggest("p4.gcode.3mf")
        r = self.start("p4.gcode.3mf", d)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.p.started, [("p4.gcode.3mf", "Metadata/plate_4.gcode")])
        self.assertEqual(self.p.downloads, 1)                       # the cache was enough: no second download to start

    def test_a_plate_1_file_is_still_plate_1(self):
        self.p.files["p1.gcode.3mf"] = make_3mf(gcodes=(1,), pngs=("plate_1.png",), plates=((1, ["a"]),))
        d = self.suggest("p1.gcode.3mf")
        self.assertEqual(self.start("p1.gcode.3mf", d).status_code, 200)
        self.assertEqual(self.p.started, [("p1.gcode.3mf", "Metadata/plate_1.gcode")])

    def test_a_cache_entry_from_before_plate_n_still_gives_the_right_plate(self):
        self.p.files["p4.gcode.3mf"] = make_3mf()
        d = self.suggest("p4.gcode.3mf")
        entry = self.cache_entry("p4.gcode.3mf")
        entry.pop("plate_n"), entry.pop("meta_v")                     # what a Hub from before this fix cached
        with server._cache_lock:
            c = server._cache_load()
            c[f"P1|p4.gcode.3mf|{len(self.p.files['p4.gcode.3mf'])}"] = entry
            server._cache_save(c)
        self.assertEqual(self.start("p4.gcode.3mf", d).status_code, 200)
        self.assertEqual(self.p.started[-1][1], "Metadata/plate_4.gcode")

    def test_no_cached_details_keeps_the_plate_1_last_resort(self):
        self.p.files["x.gcode.3mf"] = make_3mf(gcodes=(1,), pngs=(), plates=((1, ["a"]),))
        d = self.suggest("x.gcode.3mf")
        os.remove(server.CACHE_PATH)
        self.assertIsNone(server._plate_param_for(self.p, "x.gcode.3mf", len(self.p.files["x.gcode.3mf"])))
        self.assertEqual(self.start("x.gcode.3mf", d).status_code, 200)
        self.assertEqual(self.p.started, [("x.gcode.3mf", "Metadata/plate_1.gcode")])


class TestServerCache(ServerBase):
    def test_meta_for_caches_the_files_own_picture_plate_and_parts(self):
        data = make_3mf(pngs=("plate_1.png", "plate_4.png"), plates=((1, ["a", "b", "c"]), (4, ["Clip"])))
        self.p.files["p4.gcode.3mf"] = data
        meta = server._meta_for(self.p, "p4.gcode.3mf", len(data), True)
        self.assertEqual(meta["plate_n"], 4)
        self.assertEqual(meta["meta_v"], bambu.META_VERSION)
        self.assertEqual(meta["parts"], 1)                            # plate 4's objects, not plate 1's three
        with open(server._thumb_path(meta["thumb"]), "rb") as fh:
            self.assertEqual(fh.read(), PNG["p4"])

    def test_an_old_entry_is_read_again_once_and_gets_the_right_picture(self):
        data = make_3mf(pngs=("plate_1.png", "plate_4.png"))
        self.p.files["p4.gcode.3mf"] = data
        key = f"P1|p4.gcode.3mf|{len(data)}"
        old = {"printer_model_id": "C12", "plates": ["Metadata/plate_4.gcode"], "thumb": server._save_thumb(key, PNG["p1"])}
        self.assertTrue(server._meta_stale(old))
        with server._cache_lock:
            server._cache_save({key: old})
        with mock.patch.object(server.time, "sleep"):
            server._prewarm_bambu(self.p)
        self.assertEqual(self.p.downloads, 1)
        new = self.cache_entry("p4.gcode.3mf")
        self.assertFalse(server._meta_stale(new))
        with open(server._thumb_path(new["thumb"]), "rb") as fh:
            self.assertEqual(fh.read(), PNG["p4"])
        server._prewarm_bambu(self.p)                                 # and only once
        self.assertEqual(self.p.downloads, 1)

    def test_an_old_plain_plate_1_entry_is_not_downloaded_again(self):
        """Its picture was already right; re-reading every file would tie up the printer's one FTPS session."""
        old = {"printer_model_id": "C12", "plates": ["Metadata/plate_1.gcode"], "thumb": "x.png"}
        self.assertFalse(server._meta_stale(old))
        self.assertTrue(server._meta_stale({"printer_model_id": "C12", "thumb": "x.png"}))     # plates unknown: re-read

    def test_a_current_entry_is_left_alone(self):
        data = make_3mf()
        self.p.files["p4.gcode.3mf"] = data
        server._meta_for(self.p, "p4.gcode.3mf", len(data), True)
        n = self.p.downloads
        server._prewarm_bambu(self.p)
        self.assertEqual(self.p.downloads, n)

    def test_a_busy_printer_is_never_asked_for_the_old_file(self):
        data = make_3mf()
        self.p.files["p4.gcode.3mf"] = data
        key = f"P1|p4.gcode.3mf|{len(data)}"
        old = {"plates": ["Metadata/plate_4.gcode"], "thumb": None}
        with server._cache_lock:
            server._cache_save({key: old})
        self.p.state = "RUNNING"
        server._prewarm_bambu(self.p)
        self.assertEqual(server._meta_for(self.p, "p4.gcode.3mf", len(data), True, force=True), old)
        self.assertEqual(self.p.downloads, 0)

    def test_a_library_seeded_entry_carries_the_marker_only_when_the_library_read_it_with_this_version(self):
        lib = library.Library(os.path.join(_TMP, "lib-seed"))
        with mock.patch.object(server, "LIB", lib):
            entry = lib.add_bytes("p4.gcode.3mf", make_3mf(pngs=("plate_1.png", "plate_4.png")))
            self.p.files["p4.gcode.3mf"] = make_3mf(pngs=("plate_1.png", "plate_4.png"))
            server._library_seed_meta(self.p, lib.get("p4.gcode.3mf"))
            seeded = self.cache_entry_for(entry)
            self.assertEqual(seeded["meta_v"], bambu.META_VERSION)
            self.assertEqual(seeded["plate_n"], 4)

    def cache_entry_for(self, entry):
        with server._cache_lock:
            return server._cache_load()[f"P1|{entry['name']}|{entry['size']}"]


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.lib = library.Library(tempfile.mkdtemp(prefix="lib-", dir=_TMP))

    def thumb_bytes(self, entry):
        with open(self.lib.thumb_path(entry["thumb"]), "rb") as fh:
            return fh.read()

    def test_layers_come_from_the_files_plate_not_plate_1(self):
        data = make_3mf(gcodes=(4,), layers={4: 123})
        meta, png = library.extract_meta(self._write(data), "3mf")
        self.assertEqual(meta["layers"], 123)
        self.assertEqual(png, PNG["p4"])
        self.assertEqual(meta["plate_n"], 4)
        self.assertEqual(meta["meta_v"], bambu.META_VERSION)

    def test_layers_of_plate_1_files_unchanged(self):
        meta, _ = library.extract_meta(self._write(make_3mf(gcodes=(1,), pngs=("plate_1.png",), plates=((1, ["a"]),), layers={1: 75})), "3mf")
        self.assertEqual(meta["layers"], 75)

    def test_no_gcode_no_layers_no_crash(self):
        meta, _ = library.extract_meta(self._write(make_3mf(gcodes=(), pngs=())), "3mf")
        self.assertNotIn("layers", meta)
        self.assertNotIn("error", meta)

    def test_parts_are_those_of_the_files_plate(self):
        data = make_3mf(plates=((1, ["a", "b", "c"]), (4, ["Clip"])))
        meta, _ = library.extract_meta(self._write(data), "3mf")
        self.assertEqual(meta["parts"], 1)

    @unittest.skipUnless(HAVE, "the app's dependencies are not installed")
    def test_old_library_entries_get_their_picture_read_again_from_disk(self):
        data = make_3mf(pngs=("plate_1.png", "plate_4.png"))
        e = self.lib.add_bytes("p4.gcode.3mf", data)
        # pretend the entry was made by the old code: plate 1's picture, no marker
        self.lib.refresh_picture("p4.gcode.3mf", PNG["p1"], meta_v=0, plate_n=None)
        self.assertEqual(self.thumb_bytes(self.lib.get("p4.gcode.3mf")), PNG["p1"])
        with mock.patch.object(server, "LIB", self.lib), mock.patch.object(server.time, "sleep"):
            server._picture_backfill_library()
        e = self.lib.get("p4.gcode.3mf")
        self.assertEqual(self.thumb_bytes(e), PNG["p4"])
        self.assertEqual(e["meta"]["meta_v"], bambu.META_VERSION)
        self.assertEqual(e["meta"]["plate_n"], 4)

    def _write(self, data):
        p = os.path.join(tempfile.mkdtemp(prefix="f-", dir=_TMP), "x.gcode.3mf")
        with open(p, "wb") as fh:
            fh.write(data)
        return p


if __name__ == "__main__":
    unittest.main()
