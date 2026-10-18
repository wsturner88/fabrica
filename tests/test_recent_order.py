"""Newest first: the file list's "Recent" order (a file's last print on that printer lifts it), History's By file (most recently
printed first, spend still available) and the job lists (API and tool order).

    python3 tests/test_recent_order.py

SQLite in a throwaway temp dir, a fake clock, in-process fakes. No printer or network. Names are generic.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, HERE)
sys.path.insert(0, APP)
_TMP = tempfile.mkdtemp(prefix="recentorder-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ["BAMBU_MCP_API_AUTH"] = "off"

from test_joblog import Base, ev, T0  # noqa: E402

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception:      # pragma: no cover
    HAVE = False


class Log(Base):
    def print_file(self, jl, name, start, printer="P1S", result="print_finished", seconds=600):
        self.files.setdefault(__import__("costs").file_key(name), {"grams": 10.0, "seconds": 600, "parts": 1, "material": "PLA", "kind": "bambu"})
        jl.on_event(ev("print_started", printer=printer, ts=start, job=name))
        if result:
            jl.on_event(ev(result, printer=printer, ts=start + seconds, job=name, seconds=seconds))


class LastPrintedTests(Log):
    def test_the_newest_print_of_each_file_on_that_printer_only(self):
        jl = self.make()
        self.print_file(jl, "alpha.gcode.3mf", T0)
        self.print_file(jl, "alpha.gcode.3mf", T0 + 5000)
        self.print_file(jl, "beta.gcode.3mf", T0 + 1000)
        self.print_file(jl, "alpha.gcode.3mf", T0 + 90000, printer="A1")            # another printer's print does not count
        got = jl.last_printed_for("P1S", [("alpha.gcode.3mf",), ("beta.gcode.3mf",), ("gamma.gcode.3mf",)])
        self.assertEqual(got[0], T0 + 5000 + 600)
        self.assertEqual(got[1], T0 + 1000 + 600)
        self.assertIsNone(got[2])                                                      # never printed here
        self.assertEqual(jl.last_printed_for("A1", [("alpha.gcode.3mf",)]), [T0 + 90000 + 600])

    def test_the_short_name_and_the_long_name_of_a_file_both_find_it(self):
        jl = self.make()
        self.print_file(jl, "Phone Stand v3.gcode.3mf", T0)
        got = jl.last_printed_for("P1S", [("SCRAPE~1.GCO", "phone stand v3.gcode"), ("SCRAPE~1.GCO", None)])
        self.assertEqual(got, [T0 + 600, None])

    def test_a_print_still_running_counts_from_its_start(self):
        jl = self.make()
        self.print_file(jl, "alpha.gcode.3mf", T0, result=None)
        self.assertEqual(jl.last_printed_for("P1S", [("alpha.gcode.3mf",)]), [T0])

    def test_a_renamed_file_keeps_its_history(self):
        jl = self.make()
        self.print_file(jl, "old name.gcode.3mf", T0)
        jl.record_rename("printer", "P1S", "old name.gcode.3mf", "new name.gcode.3mf")
        self.assertEqual(jl.last_printed_for("P1S", [("new name.gcode.3mf",)]), [T0 + 600])

    def test_one_query_for_the_whole_list_however_long(self):
        jl = self.make()
        self.print_file(jl, "alpha.gcode.3mf", T0)
        seen = []
        orig = jl._q
        jl._q = lambda sql, args=(): (seen.append(sql), orig(sql, args))[1]
        jl.last_printed_for("P1S", [("f%d.gcode" % i,) for i in range(3)])
        few = len([s for s in seen if "FROM jobs" in s])
        seen.clear()
        jl.last_printed_for("P1S", [("f%d.gcode" % i,) for i in range(300)])
        many = len([s for s in seen if "FROM jobs" in s])
        self.assertEqual((few, many), (1, 1))

    def test_no_printer_or_no_files(self):
        jl = self.make()
        self.assertEqual(jl.last_printed_for("", [("a",)]), [None])
        self.assertEqual(jl.last_printed_for("P1S", []), [])


class ByFileOrderTests(Log):
    def seed(self, jl):
        for k, g in (("cheap old", 1.0), ("dear middle", 500.0), ("cheap new", 2.0)):
            self.files[k] = {"grams": g, "seconds": 600, "parts": 1, "material": "PLA", "kind": "bambu"}
        self.print_file(jl, "cheap old.gcode.3mf", T0)
        self.print_file(jl, "dear middle.gcode.3mf", T0 + 10000)
        self.print_file(jl, "cheap new.gcode.3mf", T0 + 20000)

    def test_by_file_is_most_recently_printed_first_by_default(self):
        jl = self.make()
        self.seed(jl)
        f = jl.stats("all")["by_file"]
        self.assertEqual([x["file"] for x in f], ["cheap new", "dear middle", "cheap old"])
        self.assertEqual(f[0]["last_ts"], T0 + 20000 + 600)

    def test_spend_order_is_still_available(self):
        jl = self.make()
        self.seed(jl)
        f = jl.stats("all", file_order="spend")["by_file"]
        self.assertEqual(f[0]["file"], "dear middle")

    def test_the_recent_twenty_five_are_the_recent_ones(self):
        jl = self.make()
        for i in range(30):
            self.print_file(jl, "file %02d.gcode.3mf" % i, T0 + i * 1000)
        f = jl.stats("all")["by_file"]
        self.assertEqual(len(f), 25)
        self.assertEqual(f[0]["file"], "file 29")
        self.assertEqual(f[-1]["file"], "file 05")


class JobListOrderTests(Log):
    def test_recent_jobs_are_newest_first(self):
        jl = self.make()
        for i, n in enumerate(["a", "b", "c"]):
            self.print_file(jl, n + ".gcode.3mf", T0 + i * 1000)
        self.print_file(jl, "z.gcode.3mf", T0 + 500, printer="A1")
        rows = jl.recent_jobs()
        starts = [r["started_at"] for r in rows]
        self.assertEqual(starts, sorted(starts, reverse=True))
        self.assertEqual(rows[0]["file"], "c.gcode.3mf")
        self.assertEqual([r["file"] for r in jl.recent_jobs(printer="P1S")][:1], ["c.gcode.3mf"])


@unittest.skipUnless(HAVE, "needs the app requirements")
class FilesRouteTests(Log):
    class Prn:
        name = "Shop P1S"

    def test_each_listed_file_gets_its_last_printed_time(self):
        jl = self.make()
        self.print_file(jl, "alpha.gcode.3mf", T0, printer="Shop P1S")
        rows = [{"name": "alpha.gcode.3mf", "modified": 5}, {"name": "beta.gcode.3mf", "modified": 6}, {"name": "gamma", "display_name": "Alpha", "modified": 7}]
        with mock.patch.object(server, "JOBLOG", jl), mock.patch.dict(server.PRINTERS, {"shop p1s": self.Prn()}, clear=False):
            server._mark_last_printed("shop p1s", rows)
        self.assertEqual(rows[0]["last_printed"], T0 + 600)
        self.assertNotIn("last_printed", rows[1])
        self.assertEqual(rows[2]["last_printed"], T0 + 600)             # found by its long name

    def test_a_broken_log_never_breaks_the_list(self):
        rows = [{"name": "a"}]
        boom = mock.Mock()
        boom.last_printed_for.side_effect = RuntimeError("disk")
        with mock.patch.object(server, "JOBLOG", boom):
            server._mark_last_printed("x", rows)
        self.assertEqual(rows, [{"name": "a"}])


if __name__ == "__main__":
    unittest.main()
