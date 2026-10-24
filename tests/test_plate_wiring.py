"""Plate contents in the Hub: the metadata cache, the file lists, file facts, suggest_mapping, the library, the MCP tool and REST route.

    python3 tests/test_plate_wiring.py        (needs the app requirements)

Every printer is an in-process fake; the server module is imported with throwaway config / cache directories and socket connects are
blocked, so nothing here can reach a printer or the LAN. The label-reading itself is tests/test_plate_contents.py.
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

_TMP = tempfile.mkdtemp(prefix="plate-wiring-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ.pop("BAMBU_MCP_API_AUTH", None)
os.environ.pop("BAMBU_MCP_ALLOWED_HOSTS", None)
os.environ.pop("FABRICA_PLATE_BACKFILL", None)

import bambu  # noqa: E402
import joblog  # noqa: E402
import library  # noqa: E402
import platecontents as pc  # noqa: E402
import security  # noqa: E402
from bambu import PrinterBusy  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover
    HAVE = False
    _ERR = e

FIX = os.path.join(HERE, "fixtures", "plate")
SUMMARY = "4× Hose Clip + 2× Hook"
CLIENT = None
_NET = None


def fx(name):
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read()


def make_3mf(slice_info: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info)
        z.writestr("Metadata/project_settings.config", json.dumps({"printer_model": "Bambu Lab P1S", "layer_height": "0.2"}))
        z.writestr("3D/3dmodel.model", '<?xml version="1.0"?><model/>')
        z.writestr("Metadata/plate_1.gcode", "; total layer number: 75\nG28\n")
    return buf.getvalue()


def setUpModule():
    global CLIENT, _NET
    _NET = mock.patch("socket.socket.connect", side_effect=AssertionError("a test tried to open a network connection"))
    _NET.start()
    if HAVE:
        CLIENT = TestClient(server.mcp.streamable_http_app())
        CLIENT.__enter__()


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    if _NET is not None:
        _NET.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


class FakeBambu:
    kind = "bambu"
    has_camera = False
    uses_files_api = False

    def __init__(self, name, state="IDLE"):
        self.name, self.model, self.serial, self.state, self.job = name, "Bambu Lab P1S", "", state, ""
        self.files, self.cached, self.downloads = {}, [], 0

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": self.state, "job": self.job, "percent": 0, "remaining_min": None}

    def refresh(self, wait=0):
        pass

    def ams_trays(self):
        return []

    def sd_list(self):
        if self.state not in ("IDLE", "FINISH", "FAILED"):
            if self.cached:
                return [dict(r) for r in self.cached]
            raise PrinterBusy("The file list will appear when it finishes printing")
        self.cached = [{"name": n, "size": len(b), "modified": ""} for n, b in self.files.items()]
        return [dict(r) for r in self.cached]

    def sd_listing_cached(self):
        return [dict(r) for r in self.cached]

    def sd_download(self, filename):
        self.downloads += 1
        return self.files[filename]


class FakeFileHost:
    """A Klipper / OctoPrint / PrusaLink style host: files(), download_head(), optionally download_tail()."""
    kind = "moonraker"
    has_camera = False
    uses_files_api = True
    backup_mid_print_ok = True

    def __init__(self, name, tail=True, state="IDLE"):
        self.name, self.state, self.model, self.job = name, state, "Voron 2.4", ""
        self.blobs, self.heads, self.tails = {}, 0, 0
        if tail:
            self.download_tail = self._download_tail

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": self.state, "job": self.job, "percent": 0, "remaining_min": None}

    def refresh(self, wait=0):
        pass

    def ams_trays(self):
        return []

    def files(self, with_meta=True):
        return [{"name": n, "size": len(b), "origin": "local", "print_seconds": 3900, "print_time": "1h05m", "filament_mm": 1000.0}
                for n, b in self.blobs.items()]

    def download_head(self, filename, nbytes=1048576):
        self.heads += 1
        return self.blobs[filename][:nbytes]

    def _download_tail(self, filename, size, nbytes=65536):
        self.tails += 1
        return self.blobs[filename][-nbytes:]


class Base(unittest.TestCase):
    def setUp(self):
        if not HAVE:
            self.skipTest(f"server not importable: {_ERR}")
        self.dir = tempfile.mkdtemp(prefix="plate-hub-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for attr, new in (("LIB", library.Library(os.path.join(self.dir, "library"))),
                          ("JOBLOG", joblog.JobLog(os.path.join(self.dir, "jobs.sqlite")))):
            old = getattr(server, attr)
            setattr(server, attr, new)
            self.addCleanup(setattr, server, attr, old)
        server.JOBLOG.migrate()
        server.SETTINGS.update({"security.api_auth": "off"}, clear=["security.dashboard_password"])
        server.AUTH.env = dict(os.environ)
        server.PRINTERS.clear()
        self.addCleanup(server.PRINTERS.clear)
        server.START_TICKETS.clear()
        with server._cache_lock:
            server._cache_save({})
        server._FILES_SEEN.clear()
        self.p1s = FakeBambu("P1S")
        self.busy = FakeBambu("Busy", state="RUNNING")
        self.klip = FakeFileHost("Klip")                 # with a tail read (OctoPrint-like)
        self.mr = FakeFileHost("Moon", tail=False)       # head only (Moonraker / PrusaLink-like)
        for p in (self.p1s, self.busy, self.klip, self.mr):
            server.PRINTERS[p.name.lower()] = p
        self.good3mf = make_3mf(fx("bambu_p1s_slice_info.config").decode())
        self.bare3mf = make_3mf(fx("bambu_p1s_nolabels_slice_info.config").decode())

    def cache(self):
        with server._cache_lock:
            return server._cache_load()

    def tool(self, name, *args):
        raw = getattr(server, name)(*args)
        try:
            return json.loads(raw)
        except ValueError:
            return raw


class TestBambu(Base):
    def test_the_download_the_hub_makes_anyway_stores_the_plate(self):
        self.p1s.files["Clips.gcode.3mf"] = self.good3mf
        meta = server._meta_for(self.p1s, "Clips.gcode.3mf", len(self.good3mf), True)
        self.assertEqual((meta["plate"]["labels"], meta["plate"]["summary"], meta["parts"]), ("present", SUMMARY, 6))
        self.assertEqual(self.p1s.downloads, 1)
        again = server._meta_for(self.p1s, "Clips.gcode.3mf", len(self.good3mf), True)             # cached: no second download
        self.assertEqual((again["plate"]["summary"], self.p1s.downloads), (SUMMARY, 1))

    def test_a_file_without_objects_is_missing_and_the_lists_say_so(self):
        self.p1s.files["Bare.gcode.3mf"] = self.bare3mf
        self.p1s.files["Clips.gcode.3mf"] = self.good3mf
        rows = {r["name"]: r for r in json.loads(server.list_sd_files("P1S", True))}
        self.assertEqual(rows["Bare.gcode.3mf"]["plate"]["labels"], "missing")
        self.assertEqual(rows["Clips.gcode.3mf"]["plate"]["summary"], SUMMARY)
        self.assertEqual(set(rows["Clips.gcode.3mf"]["plate"]), {"labels", "source", "total", "groups", "summary", "plate"})

    def test_an_uncached_file_is_unknown_and_nothing_is_downloaded_for_a_read_only_listing(self):
        self.p1s.files["Clips.gcode.3mf"] = self.good3mf
        rows = json.loads(server.list_sd_files("P1S", False))
        self.assertEqual(rows[0]["plate"]["labels"], "unknown")
        self.assertEqual(self.p1s.downloads, 0)

    def test_a_cache_entry_from_before_this_version_is_unknown_not_missing(self):
        self.p1s.files["Old.gcode.3mf"] = self.good3mf
        self.p1s.cached = [{"name": "Old.gcode.3mf", "size": len(self.good3mf), "modified": ""}]
        with server._cache_lock:
            server._cache_save({f"P1S|Old.gcode.3mf|{len(self.good3mf)}": {"print_time": "1h", "parts": 6, "filaments": [], "thumb": None}})
        rows = json.loads(server.list_sd_files("P1S", False))
        self.assertEqual(rows[0]["plate"], pc.unknown())
        self.assertEqual(self.tool("plate_contents", "P1S", "Old.gcode.3mf")["labels"], "unknown")

    def test_a_busy_printer_is_never_contacted(self):
        self.busy.files["Job.gcode.3mf"] = self.good3mf
        self.busy.cached = [{"name": "Job.gcode.3mf", "size": len(self.good3mf), "modified": ""}]
        meta = bambu.parse_3mf_meta(self.good3mf)
        meta["plate"] = pc.from_3mf(self.good3mf)
        with server._cache_lock:
            server._cache_save({f"Busy|Job.gcode.3mf|{len(self.good3mf)}": meta})
        d = self.tool("plate_contents", "Busy", "Job.gcode.3mf")
        self.assertEqual((d["labels"], d["summary"], d["location"]), ("present", SUMMARY, "Busy"))
        facts = self.tool("file_facts", "Busy", "Job.gcode.3mf")
        self.assertEqual(facts["plate_contents"]["summary"], SUMMARY)
        self.assertEqual(self.busy.downloads, 0)

    def test_suggest_mapping_carries_the_plate_for_the_confirm_dialog(self):
        self.p1s.files["Clips.gcode.3mf"] = self.good3mf
        d = json.loads(server.suggest_mapping("P1S", "Clips.gcode.3mf"))
        self.assertEqual((d["plate"]["labels"], d["plate"]["summary"]), ("present", SUMMARY))
        self.p1s.files["Bare.gcode.3mf"] = self.bare3mf
        d = json.loads(server.suggest_mapping("P1S", "Bare.gcode.3mf"))
        self.assertEqual(d["plate"]["labels"], "missing")

    def test_the_file_detail_sheet_data_has_the_plate(self):
        self.p1s.files["Clips.gcode.3mf"] = self.good3mf
        server._meta_for(self.p1s, "Clips.gcode.3mf", len(self.good3mf), True)
        self.p1s.cached = [{"name": "Clips.gcode.3mf", "size": len(self.good3mf), "modified": 1}]
        d = server._file_detail(self.p1s, "Clips.gcode.3mf")
        self.assertEqual(d["plate"]["summary"], SUMMARY)
        r = CLIENT.get("/api/printer/P1S/filedetail", params={"file": "Clips.gcode.3mf"})
        self.assertEqual(r.json()["plate"]["summary"], SUMMARY)

    def test_the_parts_of_a_3mf_still_come_from_slice_info(self):
        self.p1s.files["Clips.gcode.3mf"] = self.good3mf
        d = self.tool("file_facts", "P1S", "Clips.gcode.3mf")
        self.assertEqual((d["parts"], d["parts_source"]), (6, "slice_info"))


class TestFileHosts(Base):
    LABELLED = fx("orca_voron_exclude.gcode")
    BARE = fx("orca_voron_nolabels.gcode")

    def read(self, prn, name):
        size = len(prn.blobs[name])
        return server._octo_meta_for(prn, name, size)

    def test_head_and_tail_are_read_once_and_the_plate_is_cached(self):
        self.klip.blobs["clips.gcode"] = self.LABELLED
        e = self.read(self.klip, "clips.gcode")
        self.assertEqual((e["plate"]["labels"], e["plate"]["source"], e["plate"]["summary"]), ("present", "exclude_object", SUMMARY))
        self.assertEqual((self.klip.heads, self.klip.tails), (1, 1))                         # the reads the Hub made before this feature
        self.read(self.klip, "clips.gcode")
        self.assertEqual((self.klip.heads, self.klip.tails), (1, 1))

    def test_a_host_without_a_tail_read_still_finds_labels_in_the_head(self):
        self.mr.blobs["clips.gcode"] = self.LABELLED
        e = self.read(self.mr, "clips.gcode")
        self.assertEqual(e["plate"]["summary"], SUMMARY)

    def test_missing_is_only_said_when_the_head_reached_the_end_of_the_first_layer(self):
        big = len(self.BARE) + 50 * 1024 * 1024
        for prn, tail in ((self.klip, True), (self.mr, False)):
            prn.blobs["bare.gcode"] = self.BARE
            # the host says the file is 50 MB bigger than what the head holds (a long print): the first layer is in the head
            e = server._octo_gcode_facts(prn, "bare.gcode", big, self.BARE)
            self.assertEqual(e["plate"]["labels"], "missing", tail)
        cut = self.BARE[: self.BARE.index(b";LAYER_CHANGE") + 20]
        e = server._octo_gcode_facts(self.mr, "bare.gcode", big, cut)
        self.assertEqual(e["plate"]["labels"], "unknown")

    def test_an_unknown_size_never_means_the_head_is_the_whole_file(self):
        t = b"; generated by PrusaSlicer 2.7.0\nG28\nG1 X1 E1\n"
        self.assertEqual(server._octo_gcode_facts(self.mr, "a.gcode", 0, t)["plate"]["labels"], "unknown")
        self.assertEqual(server._octo_gcode_facts(self.mr, "a.gcode", len(t), t)["plate"]["labels"], "missing")

    def test_bgcode_uses_the_metadata_in_its_head(self):
        self.mr.blobs["clips.bgcode"] = fx("prusa_mk4s_synthetic.bgcode")
        e = self.read(self.mr, "clips.bgcode")
        self.assertEqual(e["plate"]["summary"], SUMMARY)
        self.assertEqual(e["materials"], ["PLA"])

    def test_lists_facts_and_suggest_show_the_cached_plate(self):
        self.klip.blobs["clips.gcode"] = self.LABELLED
        self.klip.blobs["bare.gcode"] = self.BARE
        self.klip.blobs["new.gcode"] = self.LABELLED
        self.read(self.klip, "clips.gcode")
        self.read(self.klip, "bare.gcode")
        rows = {r["name"]: r for r in json.loads(server.list_sd_files("Klip"))}
        self.assertEqual(rows["clips.gcode"]["plate"]["summary"], SUMMARY)
        self.assertEqual(rows["bare.gcode"]["plate"]["labels"], "missing")
        self.assertEqual(rows["new.gcode"]["plate"]["labels"], "unknown")              # not read yet: nothing is claimed
        f = self.tool("file_facts", "Klip", "clips.gcode")
        self.assertEqual(f["plate_contents"]["summary"], SUMMARY)
        self.assertEqual((f["parts"], f["parts_source"]), (6, "labels"))                # better than "assumed 1"
        f = self.tool("file_facts", "Klip", "bare.gcode")
        self.assertEqual((f["parts"], f["parts_source"]), (1, "assumed"))
        self.assertIn("Do not guess", f["plate_contents"]["meaning"])
        s = json.loads(server.suggest_mapping("Klip", "clips.gcode"))
        self.assertEqual(s["plate"]["summary"], SUMMARY)

    def test_the_tool_and_the_rest_route_read_only_the_cache(self):
        self.klip.blobs["clips.gcode"] = self.LABELLED
        self.read(self.klip, "clips.gcode")
        server._FILES_SEEN.clear()
        self.assertIn("not on Klip's last saved file list", self.tool("plate_contents", "Klip", "clips.gcode"))     # no listing was seen yet
        json.loads(server.list_sd_files("Klip"))
        heads = self.klip.heads
        d = self.tool("plate_contents", "Klip", "clips.gcode")
        self.assertEqual((d["labels"], d["total"], d["location"], d["name"]), ("present", 6, "Klip", "clips.gcode"))
        self.assertIn("4x", d["meaning"])
        self.assertEqual(self.klip.heads, heads)
        r = CLIENT.get("/api/files/plate", params={"location": "Klip", "name": "clips.gcode"})
        self.assertEqual((r.status_code, r.json()["summary"]), (200, SUMMARY))
        self.assertEqual(CLIENT.get("/api/files/plate", params={"location": "Klip", "name": "nope.gcode"}).status_code, 404)
        self.assertEqual(CLIENT.get("/api/files/plate", params={"location": "Ghost", "name": "a"}).status_code, 404)

    def test_backfill_is_off_by_default_and_reads_an_older_entry_once_when_asked(self):
        self.klip.blobs["old.gcode"] = self.LABELLED
        size = len(self.LABELLED)
        old = {"thumb": None, "slicer_model": None, "materials": ["PLA"]}
        with server._cache_lock:
            server._cache_save({f"Klip|old.gcode|{size}": old})
        e = server._octo_meta_for(self.klip, "old.gcode", size)
        self.assertNotIn("plate", e)
        self.assertEqual(self.klip.heads, 0)                                           # nothing was read: no extra download by default
        with mock.patch.dict(os.environ, {"FABRICA_PLATE_BACKFILL": "1"}):
            e = server._octo_meta_for(self.klip, "old.gcode", size)
            self.assertEqual(e["plate"]["summary"], SUMMARY)
            self.assertEqual(e["materials"], ["PLA"])                                  # the rest of the entry is untouched
            self.assertEqual(self.klip.heads, 1)
            server._octo_meta_for(self.klip, "old.gcode", size)
            self.assertEqual(self.klip.heads, 1)                                       # once


class TestLibrary(Base):
    def test_files_are_read_on_upload_and_listed_with_their_plate(self):
        server.LIB.add_bytes("clips.gcode", fx("prusa_mk4s_m486.gcode"))
        server.LIB.add_bytes("clips.bgcode", fx("prusa_mk4s_synthetic.bgcode"))
        server.LIB.add_bytes("clips.gcode.3mf", self.good3mf)
        server.LIB.add_bytes("bare.gcode", fx("prusa_mk4s_nolabels.gcode"))
        server.LIB.add_bytes("bare.gcode.3mf", self.bare3mf)
        rows = {r["name"]: r for r in server._library_rows()}
        for n in ("clips.gcode", "clips.bgcode", "clips.gcode.3mf"):
            self.assertEqual(rows[n]["plate"]["summary"], SUMMARY, n)
        for n in ("bare.gcode", "bare.gcode.3mf"):
            self.assertEqual(rows[n]["plate"]["labels"], "missing", n)
        snap = CLIENT.get("/api/library").json()
        self.assertEqual({r["name"]: r["plate"]["labels"] for r in snap["rows"]}["clips.gcode"], "present")
        d = self.tool("plate_contents", "library", "clips.gcode.3mf")
        self.assertEqual((d["labels"], d["location"], d["source"]), ("present", "library", "slice_info"))
        f = self.tool("file_facts", "library", "clips.gcode")
        self.assertEqual((f["plate_contents"]["summary"], f["parts"], f["parts_source"]), (SUMMARY, 6, "labels"))

    def test_a_library_file_stored_before_this_version_is_read_from_disk_and_kept(self):
        server.LIB.add_bytes("old.gcode", fx("prusa_mk4s_m486.gcode"))
        server.LIB.add_bytes("old2.gcode", fx("orca_voron_nolabels.gcode"))
        for n in ("old.gcode", "old2.gcode"):                      # as if the index was written by an older version
            e = server.LIB._find(n)[1]
            e["meta"].pop("plate", None)
        self.assertIsNone(server._library_rows()[0]["plate"])      # the listing itself does no reading
        d = self.tool("plate_contents", "library", "old.gcode")
        self.assertEqual(d["summary"], SUMMARY)
        self.assertEqual(server.LIB.get("old.gcode")["meta"]["plate"]["labels"], "present")        # kept
        server._plate_backfill_library()                           # the start-up pass reads the rest
        self.assertEqual(server.LIB.get("old2.gcode")["meta"]["plate"]["labels"], "missing")
        with open(server.LIB.index_path) as fh:
            again = json.load(fh)["files"]["old2.gcode"]["meta"]["plate"]
        self.assertEqual(again["labels"], "missing")               # saved to the index

    def test_a_file_the_library_could_not_read_stays_unknown(self):
        server.LIB.add_bytes("junk.3mf", b"not a zip")
        self.assertEqual(self.tool("plate_contents", "library", "junk.3mf")["labels"], "unknown")
        self.assertTrue(self.tool("plate_contents", "library", "nope.3mf").startswith("REFUSED"))

    def test_a_printer_only_row_gets_the_plate_from_the_cache(self):
        meta = {"print_time": "1h", "plate": pc.from_3mf(self.good3mf)}
        self.p1s.cached = [{"name": "Only.gcode.3mf", "size": 10, "modified": ""}]
        with server._cache_lock:
            server._cache_save({"P1S|Only.gcode.3mf|10": meta})
        row = next(r for r in server._library_rows() if r["name"] == "Only.gcode.3mf")
        self.assertEqual(row["plate"]["summary"], SUMMARY)


class TestKlipperFlag(Base):
    """A Klipper printer without [exclude_object] says so in the MCP status and on the fleet card data; no other family does."""

    def setUp(self):
        super().setUp()
        base = self.mr.snapshot
        self.mr.snapshot = lambda: {**base(), "kind": "moonraker", "exclude_object": False,
                                    "exclude_object_note": "Exclude objects not set up in printer.cfg"}
        self.mr.refresh = lambda wait=0: None

    def test_list_printers_and_printer_status_say_it_in_words(self):
        text = server.list_printers()
        self.assertIn("Moon: IDLE", text)
        self.assertIn("[WARNING: Exclude objects not set up in printer.cfg]", text)
        self.assertEqual(text.count("WARNING"), 1)                                      # only the Klipper printer
        st = json.loads(server.printer_status("Moon"))
        self.assertIs(st["exclude_object"], False)
        self.assertEqual(st["exclude_object_note"], "Exclude objects not set up in printer.cfg")
        self.assertIn("exclude_object", server.printer_status.__doc__)

    def test_the_fleet_cards_get_the_fields_and_other_families_do_not(self):
        fleet = {p["printer"]: p for p in CLIENT.get("/api/fleet").json()}
        self.assertIs(fleet["Moon"]["exclude_object"], False)
        self.assertEqual(fleet["Moon"]["exclude_object_note"], "Exclude objects not set up in printer.cfg")
        self.assertNotIn("exclude_object", fleet["P1S"])
        self.assertNotIn("exclude_object", fleet["Klip"])                               # (this stub is not a MoonrakerPrinter snapshot)

    def test_a_printer_that_has_it_or_is_unchecked_gets_no_warning(self):
        for val in (True, None):
            self.mr.snapshot = lambda v=val: {"printer": "Moon", "connected": True, "state": "IDLE", "job": "", "percent": 0,
                                              "remaining_min": None, "kind": "moonraker", "exclude_object": v, "exclude_object_note": None}
            self.assertNotIn("WARNING", server.list_printers())


class TestScopeAndTool(Base):
    def test_the_tool_and_the_route_are_read_scope(self):
        self.assertEqual(security.TOOL_SCOPES["plate_contents"], "read")
        self.assertIn(("GET", "/api/files/plate", "read"), security.REST_SCOPES)
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        self.assertIn("plate_contents", names)

    def test_the_tool_text_tells_an_agent_what_not_to_do(self):
        doc = server.plate_contents.__doc__
        for word in ("never guess", "missing", "unknown", "ONE"):
            self.assertIn(word, doc)

    def test_the_agent_guides_say_to_use_the_quantity_only_when_labels_are_present(self):
        guide = server.KIT.guide("renaming")
        for needle in ("plate_contents(printer, filename)", '`labels: "present"`', '`labels: "missing"`', '`labels: "unknown"`', "Do NOT guess",
                       "4x Hose Clip + 2x Hook", "ONE", "parts_source"):
            self.assertIn(needle, guide, needle)
        self.assertLessEqual(len(guide.splitlines()), 45)                         # the kit's own cap
        self.assertIn("`plate_contents`", server.KIT.guide("start"))
        self.assertIn("`plate`", server.KIT.guide("printing"))
        self.assertIn("2026-10-18", server.KIT.guide("changes"))
        self.assertIn("plate_contents", server.KIT.record["tools"])
        text = server.rename_files()
        self.assertIn("plate_contents", text)
        self.assertIn("do NOT guess a quantity", text)

    def test_the_kit_version_follows_the_new_guide_text_and_tool_by_itself(self):
        import agentkit
        guides = agentkit.read_guides()
        tools = {t.name: t.description for t in server.mcp._tool_manager.list_tools()}
        prompts = [p.name for p in server.mcp._prompt_manager.list_prompts()]
        self.assertEqual(agentkit.compute_version(guides, tools, prompts), server.KIT.version)
        changed = dict(guides, renaming=guides["renaming"] + " ")
        self.assertNotEqual(agentkit.compute_version(changed, tools, prompts), server.KIT.version)

    def test_bad_input_is_refused_in_words(self):
        for args in (("", ""), ("Ghost", "a.3mf"), ("library", ""), ("P1S", "nope.3mf"), ("library", "../etc/passwd")):
            self.assertTrue(str(self.tool("plate_contents", *args)).startswith("REFUSED"), args)


if __name__ == "__main__":
    unittest.main(verbosity=1)
