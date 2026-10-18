"""Tests for the multi-filament spool picker and the per-file spool memory.

    python3 tests/test_multi_spool.py

Three layers:
  * spool_memory.py on its own (cap, LRU, atomic write, 0600, history);
  * the server: the /suggest payload (per-filament rows), a human remap through REST /start (allow_remap), and the
    memory (recorded only when a print really starts, re-resolved against the CURRENT trays, ignored when a spool is
    gone, the same note for agents through the MCP suggest_mapping);
  * the real dashboard.html in headless Chrome / Edge with a fake fetch (skipped when no browser is found).

Everything is local: the server module is imported with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointing into a
throw-away temp directory, printers are in-process fakes with synthetic files. Nothing here contacts a printer, the
LAN or a real Hub.
"""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="multispool-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ["BAMBU_MCP_API_AUTH"] = "off"

import spool_memory  # noqa: E402
from spool_memory import SpoolMemory  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover - the app's deps are not installed
    HAVE = False
    _ERR = e

import test_fix_dashboard as tfd  # noqa: E402   (the headless-browser harness: fake fetch + driver)


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


# ---------------------------------------------------------------- synthetic file + printer

def make_3mf(filaments, model_id="C12") -> bytes:
    """A minimal sliced .3mf. filaments: [(id, type, '#RRGGBB', grams)]."""
    fl = "".join(f'<filament id="{i}" tray_info_idx="GFA00" type="{t}" color="{c}" used_m="1.0" used_g="{g}"/>'
                 for i, t, c, g in filaments)
    info = ('<?xml version="1.0" encoding="UTF-8"?><config><plate><metadata key="index" value="1"/>'
            f'<metadata key="printer_model_id" value="{model_id}"/>'
            '<metadata key="prediction" value="3600"/><metadata key="weight" value="44"/>' + fl + '</plate></config>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", info)
        z.writestr("Metadata/project_settings.config", json.dumps({"printer_model": "Bambu Lab P1S"}))
        z.writestr("Metadata/plate_1.gcode", "G28\n")
    return buf.getvalue()


GREEN, BLACK, ORANGE = "#21FF06", "#000000", "#FD8008"      # the default palette's own hex values
OWNER_TRAYS = [("PLA", BLACK), ("PETG", GREEN), ("PLA", BLACK), ("PETG", ORANGE)]   # the owner's AMS: slots 1-4
TWO_COLOUR = [(1, "PETG", GREEN, 38.4), (2, "PLA", BLACK, 5.6)]                      # PETG body + PLA interface layer


class FakeBambu:
    kind = "bambu"
    has_camera = False
    uses_files_api = False

    def __init__(self, name, trays=None, model="Bambu Lab P1S"):
        self.name, self.model, self.serial = name, model, "01P00000000000A"
        self.files, self.started, self.fail = {}, [], False
        self.set_trays(trays or OWNER_TRAYS)

    def set_trays(self, spec):
        """spec: one (type, hex) per slot, or None for an empty slot."""
        self.trays = [{"tray": i, "slot_1based": i + 1, "type": s[0] if s else None, "color": s[1] if s else None}
                      for i, s in enumerate(spec)]

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": "IDLE", "job": "", "percent": 0, "remaining_min": None}

    def refresh(self, wait=0):
        pass

    def ams_trays(self):
        return [dict(t) for t in self.trays]

    def sd_list(self):
        return [{"name": n, "size": len(b), "modified": ""} for n, b in self.files.items()]

    def sd_download(self, filename):
        return self.files[filename]

    def start_print(self, filename, ams_mapping):
        if self.fail:
            raise RuntimeError("the printer did not answer")
        self.started.append((filename, list(ams_mapping)))


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class Base(unittest.TestCase):
    NAME = "hook.gcode.3mf"

    def setUp(self):
        server.PRINTERS.clear()
        server._START_PENDING.clear()
        server.START_TICKETS.clear()
        try:
            os.remove(server.CACHE_PATH)
        except OSError:
            pass
        self.memdir = tempfile.mkdtemp(prefix="spoolmem-")
        self.addCleanup(shutil.rmtree, self.memdir, True)
        self.mem = SpoolMemory(os.path.join(self.memdir, "spool_memory.json"))
        p = mock.patch.object(server, "SPOOL_MEMORY", self.mem)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(server.time, "sleep")        # start_print waits 8 s for the printer to react
        p.start()
        self.addCleanup(p.stop)
        self.a1 = FakeBambu("A1")
        server.PRINTERS["a1"] = self.a1
        self.browser = TestClient(server.mcp.streamable_http_app())
        self.assertEqual(self.browser.get("/dashboard").status_code, 200)    # the dashboard's cookie: the human at the screen
        self.agent = TestClient(server.mcp.streamable_http_app())            # no cookie: an API caller
        self.add_file(self.NAME, TWO_COLOUR)

    def add_file(self, name, filaments, printer=None):
        (printer or self.a1).files[name] = make_3mf(filaments)
        try:                                   # a re-sliced file may have the same size: never serve its old cached details
            os.remove(server.CACHE_PATH)
        except OSError:
            pass

    def suggest(self, name=None, printer="A1", client=None):
        r = (client or self.browser).post(f"/api/printer/{printer}/suggest", json={"filename": name or self.NAME})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def start(self, d, mapping=None, name=None, printer="A1", client=None, **extra):
        body = {"filename": name or self.NAME, "ams_mapping": d["ams_mapping"] if mapping is None else mapping,
                "confirm": d["confirm_string"], "plate_clear": True}
        body.update(extra)
        return (client or self.browser).post(f"/api/printer/{printer}/start", json=body)


# ================================================================ the suggest payload

class TestSuggestPayload(Base):
    def test_per_filament_rows(self):
        d = self.suggest()
        self.assertEqual(d["ams_mapping"], [1, 0])
        rows = d["filaments"]
        self.assertEqual([r["index"] for r in rows], [0, 1])
        self.assertEqual([r["id"] for r in rows], [1, 2])
        self.assertEqual([(r["type"], r["color_name"]) for r in rows], [("PETG", "GREEN"), ("PLA", "BLACK")])
        self.assertEqual([r["used_g"] for r in rows], [38.4, 5.6])
        self.assertEqual([r["tray"] for r in rows], [1, 0])
        self.assertEqual(rows[0]["role"], "main")           # the filament with the most grams: the only role the file proves
        self.assertNotIn("role", rows[1])
        for r in rows:                                       # colours leave the server as WORDS, never as hex
            self.assertNotIn("#", r["color_name"])

    def test_no_role_on_a_tie_or_a_single_filament(self):
        self.add_file("tie.gcode.3mf", [(1, "PETG", GREEN, 10.0), (2, "PLA", BLACK, 10.0)])
        self.assertFalse(any("role" in r for r in self.suggest("tie.gcode.3mf")["filaments"]))
        self.add_file("one.gcode.3mf", [(1, "PETG", GREEN, 10.0)])
        d = self.suggest("one.gcode.3mf")
        self.assertEqual(len(d["filaments"]), 1)
        self.assertNotIn("role", d["filaments"][0])

    def test_old_fields_are_untouched(self):
        d = self.suggest()
        for k in ("file", "printer", "ams_mapping", "mapping_described", "notes", "blocked", "confirm_string",
                  "next_step", "model_match", "print_time", "weight_g"):
            self.assertIn(k, d)
        self.assertIn("filament 1 PETG", d["mapping_described"])
        self.assertTrue(d["confirm_string"].startswith("ph-"))
        self.assertEqual(d["notes"], [])
        self.assertFalse(d["remembered"])

    def test_blocked_stays_blocked(self):
        self.a1.set_trays([("PLA", BLACK), ("PLA", BLACK), ("PLA", BLACK), ("PLA", ORANGE)])      # no PETG at all
        d = self.suggest()
        self.assertTrue(d["blocked"])
        self.assertEqual(d["confirm_string"], "")
        self.assertEqual(self.start(d).status_code, 409)
        self.assertEqual(self.a1.started, [])

    def test_blocked_stays_blocked_even_with_a_memory(self):
        self.mem.record("A1", "hook", [{"index": 0, "tray": 1, "type": "PETG", "color": GREEN, "color_name": "GREEN"},
                                       {"index": 1, "tray": 0, "type": "PLA", "color": BLACK, "color_name": "BLACK"}])
        self.a1.set_trays([("PLA", BLACK), ("PLA", BLACK), ("PLA", BLACK), ("PLA", ORANGE)])
        d = self.suggest()
        self.assertTrue(d["blocked"])
        self.assertFalse(d["remembered"])


# ================================================================ a human remap through REST /start

class TestRemap(Base):
    def test_the_other_petg_slot_is_accepted(self):
        d = self.suggest()
        r = self.start(d, [3, 0])                                     # filament 1 -> slot 4 (orange PETG), filament 2 unchanged
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.a1.started, [(self.NAME, [3, 0])])

    def test_swapping_the_two_filaments_is_accepted_and_materials_may_differ(self):
        d = self.suggest()
        r = self.start(d, [0, 1])                                     # PETG file filament on a PLA slot, on purpose
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.a1.started, [(self.NAME, [0, 1])])

    def test_the_same_tray_twice_is_refused(self):
        d = self.suggest()
        r = self.start(d, [1, 1])
        self.assertEqual(r.status_code, 409)
        self.assertIn("same tray twice", r.json()["error"])
        self.assertEqual(self.a1.started, [])

    def test_an_unloaded_tray_is_refused(self):
        self.a1.set_trays([("PLA", BLACK), ("PETG", GREEN), None, ("PETG", ORANGE)])
        d = self.suggest()
        r = self.start(d, [1, 2])
        self.assertEqual(r.status_code, 409)
        self.assertIn("does not hold any filament", r.json()["error"])
        self.assertEqual(self.start(d, [1, 7]).status_code, 409)       # an invented tray
        self.assertEqual(self.a1.started, [])

    def test_the_used_filament_slots_cannot_change(self):
        d = self.suggest()
        r = self.start(d, [1, -1])
        self.assertEqual(r.status_code, 409)
        self.assertIn("different filament slots", r.json()["error"])
        self.assertEqual(self.start(d, [1, 0, 3]).status_code, 409)
        self.assertEqual(self.a1.started, [])

    def test_a_refused_remap_keeps_the_ticket_so_the_human_can_correct_it(self):
        d = self.suggest()
        self.assertEqual(self.start(d, [1, 1]).status_code, 409)
        self.assertEqual(self.start(d, [3, 0]).status_code, 200)

    def test_a_ticket_is_still_single_use(self):
        d = self.suggest()
        self.assertEqual(self.start(d, [3, 0]).status_code, 200)
        server._START_PENDING.clear()
        r = self.start(d, [3, 0])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.a1.started), 1)

    def test_an_api_caller_without_the_dashboard_cookie_cannot_remap(self):
        d = self.suggest(client=self.agent)
        r = self.start(d, [3, 0], client=self.agent)
        self.assertEqual(r.status_code, 409)
        self.assertIn("differs", r.json()["error"])
        self.assertEqual(self.start(d, client=self.agent).status_code, 200)      # the suggested mapping still works
        self.assertEqual(self.a1.started, [(self.NAME, [1, 0])])

    def test_the_bed_clear_tick_is_still_required(self):
        d = self.suggest()
        self.assertEqual(self.start(d, [3, 0], plate_clear=False).status_code, 400)
        self.assertEqual(self.a1.started, [])

    def test_the_ams_changing_after_the_suggestion_still_refuses(self):
        d = self.suggest()
        self.a1.set_trays([("PLA", BLACK), ("PETG", ORANGE), ("PLA", BLACK), ("PETG", GREEN)])
        self.assertEqual(self.start(d, [3, 0]).status_code, 409)
        self.assertEqual(self.a1.started, [])


# ================================================================ the memory, through the server

SPOOLS_PETG4_PLA1 = [{"index": 0, "tray": 3, "type": "PETG", "color": ORANGE, "color_name": "ORANGE"},
                     {"index": 1, "tray": 0, "type": "PLA", "color": BLACK, "color_name": "BLACK"}]


class TestMemoryRecording(Base):
    def test_recorded_on_a_successful_start_as_spool_identities(self):
        self.assertIsNone(self.mem.lookup("A1", "hook"))
        d = self.suggest()
        self.assertEqual(self.start(d, [3, 0]).status_code, 200)
        rem = self.mem.lookup("A1", "hook")
        self.assertIsNotNone(rem)
        self.assertEqual(rem["spools"], SPOOLS_PETG4_PLA1)
        self.assertEqual(rem["uses"], 1)
        self.assertGreater(rem["ts"], 0)

    def test_the_key_ignores_case_and_the_gcode_3mf_suffix(self):
        d = self.suggest()
        self.start(d)
        self.assertIsNotNone(self.mem.lookup("a1", "hook"))
        self.assertIsNotNone(self.mem.lookup("A1", server.costs.file_key("HOOK.gcode.3mf")))

    def test_not_recorded_when_the_start_is_refused(self):
        d = self.suggest()
        for bad in ([1, 1], [1, 9], [1, -1]):
            self.assertEqual(self.start(d, bad).status_code, 409)
        self.assertEqual(self.start(d, plate_clear=False).status_code, 400)
        self.assertEqual(self.start(d, confirm="ph-wrong").status_code, 409)
        self.assertIsNone(self.mem.lookup("A1", "hook"))
        self.assertEqual(len(self.mem), 0)

    def test_not_recorded_when_the_printer_fails_to_take_it(self):
        d = self.suggest()
        self.a1.fail = True
        r = self.start(d)
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(self.mem.lookup("A1", "hook"))

    def test_not_recorded_when_the_printer_is_busy(self):
        d = self.suggest()
        with mock.patch.object(self.a1, "snapshot", return_value={"printer": "A1", "connected": True, "state": "RUNNING",
                                                                  "job": "x", "percent": 5, "remaining_min": 3}):
            self.assertEqual(self.start(d).status_code, 409)
        self.assertIsNone(self.mem.lookup("A1", "hook"))

    def test_recorded_by_the_mcp_start_print_too(self):
        d = json.loads(server.suggest_mapping("A1", self.NAME))
        out = server.start_print("A1", self.NAME, d["ams_mapping"], d["confirm_string"], bed_clear_confirmed_by_human=True)
        self.assertIn('"sent": true', out)
        self.assertEqual([(s["index"], s["tray"]) for s in self.mem.lookup("A1", "hook")["spools"]], [(0, 1), (1, 0)])

    def test_mcp_refusal_records_nothing(self):
        d = json.loads(server.suggest_mapping("A1", self.NAME))
        out = server.start_print("A1", self.NAME, d["ams_mapping"], d["confirm_string"])      # no bed-clear statement
        self.assertTrue(out.startswith("REFUSED"))
        self.assertIsNone(self.mem.lookup("A1", "hook"))

    def test_a_memory_that_cannot_be_written_never_breaks_the_start(self):
        d = self.suggest()
        with mock.patch.object(self.mem, "record", side_effect=OSError("disk full")):
            self.assertEqual(self.start(d).status_code, 200)
        self.assertEqual(len(self.a1.started), 1)

    def test_same_choice_again_counts_up(self):
        for _ in range(2):
            server._START_PENDING.clear()
            self.start(self.suggest(), [3, 0])
        rem = self.mem.lookup("A1", "hook")
        self.assertEqual((rem["uses"], rem["count"]), (2, 2))
        self.assertEqual(len(self.mem.history("A1", "hook")), 1)

    def test_the_data_file_is_in_the_hardened_list(self):
        self.assertIn("spool_memory.json", server.PRIVATE_DATA_FILES)
        self.assertEqual(spool_memory.FILE_NAME, "spool_memory.json")


class TestMemoryReuse(Base):
    def remember_orange_and_black(self):
        self.start(self.suggest(), [3, 0])
        server._START_PENDING.clear()
        server.START_TICKETS.clear()
        self.a1.started.clear()

    def test_the_next_suggestion_starts_from_the_same_spools(self):
        self.remember_orange_and_black()
        d = self.suggest()
        self.assertEqual(d["ams_mapping"], [3, 0])                    # not the GREEN the colour match would give
        self.assertTrue(d["remembered"])
        self.assertEqual([r["tray"] for r in d["filaments"]], [3, 0])
        self.assertTrue(d["notes"][0].startswith("Same spools as your last print of this file ("), d["notes"])
        self.assertRegex(d["notes"][0], r"\((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}\)$")
        self.assertIn("slot 4 (ORANGE PETG)", d["mapping_described"])
        self.assertIn("slot 1 (BLACK PLA)", d["mapping_described"])

    def test_the_remembered_proposal_is_a_normal_ticket_the_strict_path_accepts(self):
        self.remember_orange_and_black()
        d = self.suggest(client=self.agent)
        self.assertEqual(self.start(d, client=self.agent).status_code, 200)       # no remap needed: it IS the proposal
        self.assertEqual(self.a1.started, [(self.NAME, [3, 0])])

    def test_the_agent_gets_the_same_note_through_suggest_mapping(self):
        self.remember_orange_and_black()
        d = json.loads(server.suggest_mapping("A1", self.NAME))
        self.assertEqual(d["ams_mapping"], [3, 0])
        self.assertTrue(d["notes"][0].startswith("Same spools as your last print of this file ("))
        self.assertIn("slot 4 (ORANGE PETG)", d["next_step"])

    def test_it_is_only_a_proposal_nothing_starts_by_itself(self):
        self.remember_orange_and_black()
        self.suggest()
        self.suggest()
        self.assertEqual(self.a1.started, [])

    def test_the_most_recent_choice_wins(self):
        self.remember_orange_and_black()
        server._START_PENDING.clear()
        self.start(self.suggest(), [1, 2])                            # later: GREEN PETG + the other black PLA
        server.START_TICKETS.clear()
        d = self.suggest()
        self.assertEqual(d["ams_mapping"], [1, 2])
        self.assertEqual(len(self.mem.history("A1", "hook")), 2)

    def test_a_spool_that_moved_is_found_again(self):
        self.remember_orange_and_black()
        # the orange PETG spool now sits in slot 2, GREEN PETG went to slot 4, the black PLA is only in slot 3
        self.a1.set_trays([None, ("PETG", ORANGE), ("PLA", BLACK), ("PETG", GREEN)])
        d = self.suggest()
        self.assertEqual(d["ams_mapping"], [1, 2])
        self.assertTrue(d["remembered"])
        self.assertIn("slot 2 (ORANGE PETG)", d["mapping_described"])

    def test_a_spool_that_did_not_move_keeps_its_tray_when_a_twin_exists(self):
        self.remember_orange_and_black()                              # black PLA was slot 1; slot 3 holds an identical one
        self.a1.set_trays([("PLA", BLACK), ("PETG", GREEN), ("PLA", BLACK), ("PETG", ORANGE)])
        self.assertEqual(self.suggest()["ams_mapping"], [3, 0])
        self.a1.set_trays([("PETG", ORANGE), ("PETG", GREEN), ("PLA", BLACK), ("PLA", BLACK)])   # orange moved to slot 1
        self.assertEqual(self.suggest()["ams_mapping"], [0, 2])       # the first free twin, deterministic

    def test_two_filaments_never_land_on_one_tray(self):
        self.start(self.suggest(), [1, 0])
        server._START_PENDING.clear()
        server.START_TICKETS.clear()
        self.a1.set_trays([None, ("PETG", GREEN), None, ("PLA", BLACK)])
        d = self.suggest()
        self.assertEqual(sorted(d["ams_mapping"]), [1, 3])

    def test_a_missing_spool_falls_back_to_the_normal_proposal_for_the_whole_job(self):
        self.remember_orange_and_black()
        self.a1.set_trays([("PLA", BLACK), ("PETG", GREEN), ("PLA", BLACK), None])             # the orange PETG is gone
        d = self.suggest()
        self.assertFalse(d["remembered"])
        self.assertEqual(d["ams_mapping"], [1, 0])
        self.assertFalse(any("Same spools" in n for n in d["notes"]))
        self.a1.set_trays([("PLA", ORANGE), ("PETG", GREEN), None, ("PETG", ORANGE)])          # the black PLA is gone, not just one half
        self.assertFalse(self.suggest()["remembered"])

    def test_a_file_sliced_with_other_filaments_ignores_the_memory(self):
        self.remember_orange_and_black()
        self.add_file(self.NAME, [(1, "PETG", GREEN, 30.0), (3, "PLA", BLACK, 5.0)])           # now filaments 1 and 3
        d = self.suggest()
        self.assertFalse(d["remembered"])
        self.assertEqual(len(d["ams_mapping"]), 3)
        self.add_file(self.NAME, [(1, "PETG", GREEN, 30.0)])
        self.assertFalse(self.suggest()["remembered"])

    def test_it_is_per_printer_and_per_file(self):
        self.remember_orange_and_black()
        other = FakeBambu("B2")
        server.PRINTERS["b2"] = other
        self.add_file(self.NAME, TWO_COLOUR, other)
        self.assertFalse(self.suggest(printer="B2")["remembered"])
        self.add_file("other.gcode.3mf", TWO_COLOUR)
        self.assertFalse(self.suggest("other.gcode.3mf")["remembered"])

    def test_a_remembered_choice_with_a_different_material_gets_a_words_note_for_agents(self):
        self.start(self.suggest(), [0, 1])                            # PETG filament on the PLA slot, PLA filament on PETG
        server.START_TICKETS.clear()
        server._START_PENDING.clear()
        d = self.suggest(client=self.agent)
        self.assertEqual(d["ams_mapping"], [0, 1])
        self.assertTrue(any("filament 1 is sliced for PETG but slot 1 holds PLA" in n for n in d["notes"]), d["notes"])

    def test_the_memory_survives_a_restart(self):
        self.remember_orange_and_black()
        again = SpoolMemory(self.mem.path)
        self.assertEqual(again.lookup("A1", "hook")["spools"], SPOOLS_PETG4_PLA1)


# ================================================================ spool_memory.py on its own

def _read(path):
    with open(path) as fh:
        return fh.read()


def _read_json(path):
    return json.loads(_read(path))


def spools(*trays):
    return [{"index": i, "tray": t, "type": "PLA", "color": BLACK, "color_name": "BLACK"} for i, t in enumerate(trays)]


class TestSpoolMemoryUnit(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="spoolmem-unit-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = os.path.join(self.d, "spool_memory.json")
        self.t = [1000.0]
        self.mem = SpoolMemory(self.path, max_files=3, clock=lambda: self.t[0])

    def rec(self, name, *trays):
        self.t[0] += 1
        return self.mem.record("P", name, spools(*trays))

    def test_the_file_is_owner_only_and_atomic_and_leaves_no_temp_files(self):
        self.rec("a", 0, 1)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(os.listdir(self.d), ["spool_memory.json"])
        data = _read_json(self.path)
        self.assertEqual(data["version"], 1)
        self.assertIn("p|a", data["files"])

    def test_a_crash_during_the_write_keeps_the_old_file_whole(self):
        self.rec("a", 0)
        before = _read(self.path)
        with mock.patch.object(spool_memory.os, "replace", side_effect=OSError("boom")):
            self.rec("b", 1)
        self.assertEqual(_read(self.path), before)
        self.assertEqual(os.listdir(self.d), ["spool_memory.json"])         # the temp file was cleaned up

    def test_the_cap_forgets_the_least_recently_used_file(self):
        for n in "abc":
            self.rec(n, 0)
        self.rec("a", 1)                                                    # a is used again: now the newest
        self.rec("d", 0)                                                    # over the cap: b (the oldest) goes
        self.assertEqual(len(self.mem), 3)
        self.assertIsNone(self.mem.lookup("P", "b"))
        for n in "acd":
            self.assertIsNotNone(self.mem.lookup("P", n), n)
        self.assertEqual(len(_read_json(self.path)["files"]), 3)

    def test_the_default_cap_is_500(self):
        self.assertEqual(spool_memory.MAX_FILES, 500)
        self.assertEqual(SpoolMemory(self.path).max_files, 500)

    def test_history_keeps_five_newest_first_and_counts_repeats(self):
        for tray in range(7):
            self.rec("a", tray)
        h = self.mem.history("P", "a")
        self.assertEqual(len(h), spool_memory.HISTORY_MAX)
        self.assertEqual([x["spools"][0]["tray"] for x in h], [6, 5, 4, 3, 2])
        self.rec("a", 4)                                                    # an old choice again: back to the front, count 2
        h = self.mem.history("P", "a")
        self.assertEqual((h[0]["spools"][0]["tray"], h[0]["count"]), (4, 2))
        self.assertEqual(self.mem.lookup("P", "a")["uses"], 8)
        self.assertEqual(self.mem.lookup("P", "a")["spools"][0]["tray"], 4)   # most recent only

    def test_garbage_is_refused_and_a_broken_file_reads_as_empty(self):
        self.assertFalse(self.mem.record("P", "a", []))
        self.assertFalse(self.mem.record("P", "", spools(0)))
        self.assertFalse(self.mem.record("P", "a", [{"index": "x", "tray": 1, "type": "PLA"}]))
        self.assertFalse(self.mem.record("P", "a", [{"index": 0, "tray": True, "type": "PLA"}]))
        self.assertFalse(self.mem.record("P", "a", [{"index": 0, "tray": 1, "type": ""}]))
        self.assertEqual(len(self.mem), 0)
        for junk in ("", "not json", "[1,2]", '{"files": 5}', '{"files": {"p|a": {"history": [{"spools": "x"}]}}}'):
            with open(self.path, "w") as fh:
                fh.write(junk)
            self.assertIsNone(SpoolMemory(self.path).lookup("P", "a"), junk)

    def test_clear(self):
        self.rec("a", 0)
        self.mem.clear()
        self.assertEqual(len(self.mem), 0)
        self.assertEqual(_read_json(self.path)["files"], {})

    def test_threads_do_not_corrupt_it(self):
        def work(k):
            for n in range(20):
                self.mem.record("P", f"f{k}", spools(n % 4))
        ts = [threading.Thread(target=work, args=(k,)) for k in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        _read_json(self.path)
        self.assertEqual(len(self.mem), 3)


# ================================================================ the dashboard (headless browser)

class TestDashboardStatic(unittest.TestCase):
    HTML = tfd.HTML

    def test_the_picker_pieces_are_there(self):
        for needle in ('data-act="pickMulti"', "pickMulti: b =>", "function msPick(", "function msRender(", "function msBuild(",
                       "&#9733; suggested", "File wants ${esc(r.kind)}", "Swapped: Filament", "d.filaments",
                       "mapping[r.idx] = MS.pick[r.idx]", 'class="cfdlg${multi'):
            self.assertIn(needle, self.HTML, needle)

    def test_single_filament_grid_and_ticket_wiring_are_unchanged(self):
        for needle in ('data-act="pickSpool"', "Tap the spool to print with:", "suggestedConfirm.indexOf('ph-') === 0",
                       "const confirm = ticket ? suggestedConfirm :", "function highlightSpool()"):
            self.assertIn(needle, self.HTML, needle)

    def test_the_kiosk_and_phone_rules_exist(self):
        # the confirm dialog's own rules (see test_confirm_compact.py for the measured layout): a short-screen tier and a phone tier
        self.assertIn("  @media (max-height:520px) {\n    .cfscroll {", self.HTML)
        self.assertIn(".msgrid button { min-height:50px; font-size:14px; }", self.HTML)
        self.assertIn("    .msgrid { grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }", self.HTML)
        self.assertIn(".cfactions .toggle { flex:1 1 330px;", self.HTML)           # the bed-clear tick rides in the footer with START


DRIVER = r"""
const trays = [{tray: 0, slot_1based: 1, type: 'PLA', color_name: 'BLACK'}, {tray: 1, slot_1based: 2, type: 'PETG', color_name: 'GREEN'},
               {tray: 2, slot_1based: 3, type: 'PLA', color_name: 'BLACK'}, {tray: 3, slot_1based: 4, type: 'PETG', color_name: 'ORANGE'}];
const files = [{name: 'Hook.gcode.3mf', print_time: '2h', weight_g: 44, filaments: [{color_name: 'GREEN', type: 'PETG'}, {color_name: 'BLACK', type: 'PLA'}]},
               {name: 'Single.gcode.3mf', print_time: '1h', weight_g: 5, filaments: [{color_name: 'GREEN', type: 'PETG'}]}];
const snap = {snapshot: {printer: 'A1', connected: true, state: 'IDLE', job: '', percent: 0, layer: 0, nozzle_c: 25, bed_c: 24, nozzle_target_c: 0, bed_target_c: 0},
  trays, controls: {actions: []}, kind: 'bambu', materials: ['PLA', 'PETG'], palette: ['GREEN', 'BLACK']};
const base = {print_time: '2h', weight_g: 44, notes: [], blocked: null, model_match: true, target_model_name: 'P1S', printer_kind: 'bambu', confirm_string: 'ph-TICKET'};
const two = {...base, file: 'Hook.gcode.3mf', ams_mapping: [1, 0], mapping_described: 'filament 1 PETG -> slot 2\nfilament 2 PLA -> slot 1',
  filaments: [{index: 0, id: 1, type: 'PETG', color_name: 'GREEN', used_g: 38.4, role: 'main', tray: 1}, {index: 1, id: 2, type: 'PLA', color_name: 'BLACK', used_g: 5.6, tray: 0}]};
const one = {...base, file: 'Single.gcode.3mf', ams_mapping: [1], mapping_described: 'filament 1 PETG -> slot 2',
  filaments: [{index: 0, id: 1, type: 'PETG', color_name: 'GREEN', used_g: 5, tray: 1}]};
window.__starts = [];
window.__stubs.push((u, m, init) => {
  if(u === '/api/fleet') return J([{printer: 'A1', connected: true, state: 'IDLE', percent: 0, remaining_min: 0, job: '', nozzle_c: 25, bed_c: 24, camera: false}]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: []});
  if(/^\/api\/printer\/[^/]+\/files/.test(u)) return J(files);
  if(/\/suggest$/.test(u)) return J(JSON.parse(init.body).filename === 'Single.gcode.3mf' ? one : two);
  if(/\/start$/.test(u) && m === 'POST'){ window.__starts.push(JSON.parse(init.body)); return J({state_after_8s: 'PREPARE', job: 'Hook'}); }
  if(/^\/api\/printer\/[^/]+$/.test(u)) return J(snap);
  return null;
});
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
const rows = () => [...document.querySelectorAll('#msrows .msrow')].map(r => ({
  head: r.querySelector('.mshead').textContent,
  pressed: [...r.querySelectorAll('button[aria-pressed=true]')].map(b => b.dataset.tray),
  starred: [...r.querySelectorAll('button')].filter(b => b.textContent.includes('suggested')).map(b => b.dataset.tray),
  labels: [...r.querySelectorAll('button')].map(b => b.textContent),
  warn: (r.querySelector('.mswarn') || {}).textContent || null, colour: (r.querySelector('.mscol') || {}).textContent || null}));
const tap = (idx, tray) => { document.querySelector('#msrows [data-idx="' + idx + '"][data-tray="' + tray + '"]').click(); };
const openFile = async name => {
  await wait(() => document.querySelector('.frow'));
  [...document.querySelectorAll('.frow')].find(r => r.textContent.includes(name)).querySelector('button.primary').click();
  await wait(() => $('startbtn'));
};
const startNow = async () => { $('plateok').click(); await sleep(50); R.startEnabled = !$('startbtn').disabled; $('startbtn').click(); await sleep(300); };

await wait(() => document.querySelector('.pcard'));
document.querySelector('.pcard').click();

// A: a two-filament job: one row each, the suggested spools starred, then the OTHER petg slot for filament 1
await openFile('Hook');
R.hasGrid = !!$('spoolgrid'); R.intro = document.querySelector('.msintro').textContent;
R.rowsA = rows();
R.footerHasPlate = !!document.querySelector('.cfactions #plateok');
R.cfmulti = !!document.querySelector('.cfdlg.cfmulti');
tap(0, 3); await sleep(50);
R.rowsA2 = rows();
await startNow();
R.startA = window.__starts.slice();
closeModal();

// B: swap - tap the slot the other filament holds; the two trade, and the material mismatch is said in words
await openFile('Hook');
tap(1, 1); await sleep(50);
R.rowsB = rows(); R.msg = $('msmsg').textContent;
tap(1, 3); await sleep(50);
R.rowsB2 = rows();
await startNow();
R.startB = window.__starts.slice(1);
closeModal();

// C: a single-filament job keeps today's grid
await openFile('Single');
R.single = {grid: !!$('spoolgrid'), multi: !!$('msrows'), buttons: [...document.querySelectorAll('#spoolgrid [data-tray]')].length, cfmulti: !!document.querySelector('.cfmulti')};
document.querySelector('#spoolgrid [data-tray="3"]').click(); await sleep(50);
await startNow();
R.startC = window.__starts.slice(2);
"""


@unittest.skipUnless(tfd.CHROME, "no Chrome / Edge found for the browser tests")
class TestDashboardBrowser(unittest.TestCase):
    R = None

    def run_it(self):
        if TestDashboardBrowser.R is None:
            TestDashboardBrowser.R = tfd.run_page(self, DRIVER, 90000)
        return TestDashboardBrowser.R

    def test_finished_without_errors(self):
        R = self.run_it()
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])

    def test_one_row_per_filament_with_words_and_the_suggested_spool_starred(self):
        R = self.run_it()
        self.assertFalse(R["hasGrid"])                                # no single-spool grid for a two-filament job
        self.assertTrue(R["cfmulti"])
        self.assertIn("2 filaments", R["intro"])
        a = R["rowsA"]
        self.assertEqual(len(a), 2)
        self.assertEqual(a[0]["head"], "Filament 1 · PETG · file colour GREEN · 38 g · main")
        self.assertEqual(a[1]["head"], "Filament 2 · PLA · file colour BLACK · 5.6 g")
        self.assertEqual(a[0]["labels"][1], "✔ Slot 2GREEN PETG★ suggested")      # chosen mark + star: both in words/symbols
        self.assertEqual([lbl.split("★")[0] for lbl in a[1]["labels"]],
                         ["✔ Slot 1BLACK PLA", "Slot 2GREEN PETG", "Slot 3BLACK PLA", "Slot 4ORANGE PETG"])
        self.assertEqual((a[0]["pressed"], a[0]["starred"]), (["1"], ["1"]))
        self.assertEqual((a[1]["pressed"], a[1]["starred"]), (["0"], ["0"]))
        self.assertIsNone(a[0]["warn"])
        self.assertIsNone(a[1]["warn"])
        self.assertTrue(R["footerHasPlate"])                          # the bed-clear box rides with START in the sticky footer

    def test_tapping_the_other_petg_slot_sends_that_mapping_with_the_existing_ticket(self):
        R = self.run_it()
        a2 = R["rowsA2"]
        self.assertEqual(a2[0]["pressed"], ["3"])
        self.assertEqual(a2[0]["starred"], ["1"])                     # the star stays on what was suggested
        self.assertEqual(a2[1]["pressed"], ["0"])
        self.assertIsNone(a2[0]["warn"])                              # PETG on PETG: nothing to say about material
        self.assertIn("Colour: file says GREEN, Slot 4 is ORANGE.", a2[0]["colour"])      # a softer note, words only
        self.assertTrue(R["startEnabled"])
        self.assertEqual(len(R["startA"]), 1)
        body = R["startA"][0]
        self.assertEqual(body["ams_mapping"], [3, 0])
        self.assertEqual(body["confirm"], "ph-TICKET")                # the single-use ticket, unchanged
        self.assertIs(body["plate_clear"], True)
        self.assertEqual(body["filename"], "Hook.gcode.3mf")

    def test_tapping_a_spool_the_other_filament_holds_swaps_them_and_warns_in_words(self):
        R = self.run_it()
        self.assertEqual(R["msg"], "Swapped: Filament 1 now uses Slot 1.")
        b = R["rowsB"]
        self.assertEqual((b[0]["pressed"], b[1]["pressed"]), (["0"], ["1"]))               # never two filaments on one tray
        self.assertEqual(b[0]["warn"], "⚠ File wants PETG — Slot 1 holds PLA. Allowed — check it is on purpose.")
        self.assertEqual(b[1]["warn"], "⚠ File wants PLA — Slot 2 holds PETG. Allowed — check it is on purpose.")
        b2 = R["rowsB2"]
        self.assertEqual((b2[0]["pressed"], b2[1]["pressed"]), (["0"], ["3"]))
        self.assertEqual(len(R["startB"]), 1)
        self.assertEqual(R["startB"][0]["ams_mapping"], [0, 3])
        self.assertEqual(len(set(R["startB"][0]["ams_mapping"])), 2)

    def test_a_single_filament_job_keeps_todays_grid(self):
        R = self.run_it()
        self.assertEqual(R["single"], {"grid": True, "multi": False, "buttons": 4, "cfmulti": False})
        self.assertEqual(R["startC"][0]["ams_mapping"], [3])
        self.assertEqual(R["startC"][0]["confirm"], "ph-TICKET")


if __name__ == "__main__":
    unittest.main(verbosity=1)
