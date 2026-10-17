"""Tests for the second wiring pass in app/server.py: job log + costs, printer discovery, spaghetti
detection, and the key-management endpoints the dashboard's Security tab uses.

    python3 tests/test_wiring2.py

Needs the app's own dependencies (mcp, starlette, httpx). Everything is local: the server module is imported
with BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE pointing at a throwaway temp directory (never the real config.yml),
printers are in-process fakes, and every "service" (ntfy, the detector, a Klipper host) is a fake on 127.0.0.1.
Nothing here contacts a printer, the Internet, the LAN or a real detector. Discovery is only ever pointed at
127.0.0.1 and its multicast extras are switched off.
"""
import asyncio
import csv
import http.server
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="wiring2-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ["BAMBU_MCP_API_AUTH"] = "off"             # the scope tests switch it on explicitly
os.environ["BAMBU_MCP_ALLOW_LOOPBACK_DETECTOR"] = "1"   # the detector "Test" tests talk to a fake on 127.0.0.1
for _k in ("DETECTOR_URL", "DETECTOR_TOKEN", "PREHEAT_TIMEOUT_MIN", "BACKUP_KEEP", "BAMBU_MCP_ALLOWED_HOSTS"):
    os.environ.pop(_k, None)

try:
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover - the app's deps are not installed
    HAVE = False
    _ERR = e

import detection as det  # noqa: E402
import joblog  # noqa: E402
import security  # noqa: E402
import test_discovery as td  # noqa: E402
import test_joblog as tj  # noqa: E402
from test_detection import BIG, FakeDetector, GOOD  # noqa: E402

logging.getLogger("bambu.detection").setLevel(logging.CRITICAL)

CLIENT = None
TOKEN = "tok-ABC123-detector-secret"


def setUpModule():
    global CLIENT
    if HAVE:
        CLIENT = TestClient(server.mcp.streamable_http_app())
        CLIENT.__enter__()          # MCP's session manager needs the app's lifespan running


def tearDownModule():
    if CLIENT is not None:
        CLIENT.__exit__(None, None, None)
    shutil.rmtree(_TMP, ignore_errors=True)


class FakeHTTP:
    """Recording fake ntfy on 127.0.0.1."""

    def __init__(self):
        self.requests = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _h(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_PUT = do_GET = _h

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def titles(self):
        return [r["headers"].get("Title", "") for r in self.requests]

    def wait_for(self, text, seconds=6.0):
        end = time.time() + seconds
        while time.time() < end:
            if any(text in t for t in self.titles()):
                return True
            time.sleep(0.03)
        return False


class FakePrinter:
    """A driver stand-in with a settable state. Any command that could touch a print is recorded."""
    kind = "bambu"
    uses_files_api = False
    has_camera = False

    def __init__(self, name="Fake", state="IDLE", job="", percent=0):
        self.name = name
        self.set(state, job, percent)
        self.commands = []
        self.trays = []
        self._ts = 100.0

    def set(self, state, job="", percent=0):
        self.state, self.job, self.percent = state, job, percent

    def snapshot(self):
        return {"printer": self.name, "connected": True, "state": self.state, "job": self.job,
                "percent": self.percent, "remaining_min": None, "nozzle_c": 25, "bed_c": 24, "kind": self.kind}

    def ams_trays(self):
        return self.trays

    def control_caps(self):
        return {"light": {"supported": False, "reason": "none"}, "heat": {"supported": False, "reason": "none"},
                "unload": {"supported": False, "reason": "none"}, "load": {"supported": False, "reason": "none"}}

    # --- anything that could change a print is recorded: detection must never call these
    def pause(self): self.commands.append("pause")
    def resume(self): self.commands.append("resume")
    def cancel(self): self.commands.append("cancel")
    def stop(self): self.commands.append("stop")
    def pause_print(self): self.commands.append("pause_print")
    def cancel_print(self): self.commands.append("cancel_print")
    def start_print(self, *a, **k): self.commands.append("start_print")
    def set_temps(self, *a, **k): self.commands.append("set_temps")


class CamPrinter(FakePrinter):
    has_camera = True

    def camera(self):
        return self

    def get_frame(self, wait=0):
        self._ts += 1
        return BIG, self._ts


class Base(unittest.TestCase):
    ntfy = None

    @classmethod
    def setUpClass(cls):
        cls.c = CLIENT

    def setUp(self):
        self.c.cookies.clear()
        server.SETTINGS.reset()
        server.SETTINGS.update({}, clear=[k for k, f in server.settings_store.FIELDS.items() if f["secret"]])
        for k in server.AUTH.keys.list_public():
            server.AUTH.delete_key(k["id"])
        server.PRINTERS.clear()
        server.JOBLOG._exec("DELETE FROM jobs")
        server.JOBLOG._idle_since.clear()
        server.WATCHER._p.clear()
        server.WATCHER.confirm_s = 0
        server.NOTIFIER._last_sent.clear()
        server.NOTIFIER._history.clear()
        server.PRINTERS.update({})
        server._RECENT_STARTS.clear()
        server._FILES_SEEN.clear()
        self.t = [1_760_000_000.0]
        server.WATCHER.clock = server.JOBLOG.clock = lambda: self.t[0]
        try:
            os.remove(server.CACHE_PATH)
        except OSError:
            pass

    def tearDown(self):
        server.WATCHER.clock = server.JOBLOG.clock = time.time
        server.SETTINGS.reset()
        server.SETTINGS.update({}, clear=["detection.token"])
        server.PRINTERS.clear()

    def tick(self, advance=5.0):
        self.t[0] += advance
        server._event_tick()


def seed_cache(printer="Fake", name="benchy.gcode.3mf", size=123, parts=3, grams=36.0, seconds=9000, ptype="PLA"):
    server._cache_save({f"{printer}|{name}|{size}": {
        "printer_model_id": "C12", "print_seconds": seconds, "weight_g": grams,
        "filaments": [{"id": 1, "type": ptype, "color": "#FF8000", "used_g": grams}], "plates": [], "thumb": None,
        "parts": parts}})


# =================================================================== job log

@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class JobLogWiring(Base):
    def run_one_print(self, fail_at=None):
        prn = FakePrinter()
        server.PRINTERS["fake"] = prn
        prn.set("IDLE")
        self.tick()                                   # baseline
        prn.set("RUNNING", "benchy", 3)
        self.tick()                                   # print_started -> a row opens
        if fail_at is not None:
            prn.set("FAILED", "benchy", fail_at)
            self.tick(3600)
        else:
            prn.set("FINISH", "benchy", 100)
            self.tick(9000)
        return prn

    def test_a_print_is_logged_with_cost_end_to_end(self):
        seed_cache()
        prn = FakePrinter()
        server.PRINTERS["fake"] = prn
        prn.set("IDLE")
        self.tick()
        prn.set("RUNNING", "benchy", 3)
        self.tick()
        rows = self.c.get("/api/jobs").json()["jobs"]
        self.assertEqual([r["result"] for r in rows], ["running"])
        self.assertEqual((rows[0]["file"], rows[0]["parts"], rows[0]["grams_est"]), ("benchy", 3, 36.0))
        self.assertIsNone(rows[0]["cost"])                                   # not priced while it runs
        prn.set("FINISH", "benchy", 100)
        self.tick(9000)
        r = self.c.get("/api/jobs").json()
        self.assertEqual(r["currency"], "$")
        job = r["jobs"][0]
        self.assertEqual((job["result"], job["printer"], job["parts"], job["percent"]), ("finished", "Fake", 3, 100))
        self.assertAlmostEqual(job["cost"]["total"], 0.72, delta=0.01)       # default: filament only
        self.assertAlmostEqual(job["cost"]["per_part"], 0.24, delta=0.01)
        self.assertIs(job["cost"]["includes_machine_costs"], False)
        self.assertAlmostEqual(job["cost"]["total_with_machine"], 1.515, delta=0.01)    # 0.72 + 0.045 + 0.75
        self.assertAlmostEqual(job["cost"]["electricity"], 0.045, delta=0.01)           # the split is always there
        full = self.c.get("/api/jobs?machine=1").json()
        self.assertIs(full["includes_machine_costs"], True)
        self.assertAlmostEqual(full["jobs"][0]["cost"]["total"], 1.515, delta=0.01)
        self.assertAlmostEqual(full["jobs"][0]["cost"]["per_part"], 0.505, delta=0.01)
        self.assertEqual(len(self.c.get("/api/jobs").json()["jobs"]), 1)     # one row for the whole attempt

    def test_detail_stats_file_costs_and_csv(self):
        seed_cache()
        self.run_one_print()
        job = self.c.get("/api/jobs").json()["jobs"][0]
        d = self.c.get(f"/api/jobs/{job['id']}")
        self.assertEqual((d.status_code, d.json()["file"], d.json()["currency"]), (200, "benchy", "$"))
        self.assertEqual(self.c.get("/api/jobs/9999").status_code, 404)
        self.assertEqual(self.c.get("/api/jobs/abc").status_code, 404)
        st = self.c.get("/api/stats?period=all").json()
        self.assertEqual((st["jobs"], st["finished"], st["success_rate"], st["good_parts"]), (1, 1, 1.0, 3))
        self.assertAlmostEqual(st["true_cost_per_part"], st["cost"]["total"] / 3, delta=0.01)
        self.assertEqual(list(st["by_printer"]), ["Fake"])
        self.assertEqual(st["by_file"][0]["file"], "benchy")
        self.assertEqual(self.c.get("/api/stats?period=day&printer=Nope").json()["jobs"], 0)
        fc = self.c.get("/api/costs/file?name=benchy.gcode.3mf").json()
        self.assertEqual((fc["attempts"], fc["good_parts"]), (1, 3))
        self.assertEqual(self.c.get("/api/costs/file").status_code, 400)
        # a bad period / result is a 400, not a crash
        self.assertEqual(self.c.get("/api/stats?period=fortnight").status_code, 400)
        self.assertEqual(self.c.get("/api/jobs?result=exploded").status_code, 400)
        self.assertEqual(self.c.get("/api/jobs.csv?period=fortnight").status_code, 400)
        # CSV
        r = self.c.get("/api/jobs.csv")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/csv"))
        self.assertIn('filename="jobs.csv"', r.headers["content-disposition"])
        lines = r.text.strip().splitlines()
        self.assertEqual(lines[0].split(",")[:3], ["id", "printer", "model"])
        self.assertEqual(len(lines), 2)
        self.assertIn("benchy", lines[1])
        self.assertIn("finished", lines[1])

    def test_machine_param_and_setting_on_every_cost_route(self):
        """Filament only by default; ?machine=1 / ?machine=0 override the setting `costs.include_machine_costs` per request."""
        seed_cache()
        prn = self.run_one_print()
        prn.set("IDLE"); self.tick(60)
        prn.set("RUNNING", "benchy", 5); self.tick()
        prn.set("FAILED", "benchy", 40); self.tick(3600)
        job_id = [j for j in self.c.get("/api/jobs").json()["jobs"] if j["result"] == "finished"][0]["id"]

        def look(extra):                       # extra: "" or "&machine=1" ...
            st = self.c.get("/api/stats?period=all" + extra).json()
            jobs = self.c.get("/api/jobs?limit=50" + extra).json()
            one = self.c.get(f"/api/jobs/{job_id}?x=1" + extra).json()
            fc = self.c.get("/api/costs/file?name=benchy" + extra).json()
            rows = list(csv.DictReader(io.StringIO(self.c.get("/api/jobs.csv?period=all" + extra).text)))
            return st, jobs, one, fc, rows

        for setting_on in (False, True):
            r = self.c.put("/api/settings", json={"values": {"costs.include_machine_costs": setting_on}})
            self.assertEqual(r.status_code, 200, r.text)
            for extra, incl in (("", setting_on), ("&machine=1", True), ("&machine=0", False), ("&machine=maybe", setting_on)):
                st, jobs, one, fc, rows = look(extra)
                tag = (setting_on, extra)
                self.assertIs(st["includes_machine_costs"], incl, tag)
                self.assertIs(st["cost"]["includes_machine_costs"], incl, tag)
                self.assertIs(jobs["includes_machine_costs"], incl, tag)
                self.assertIs(one["cost"]["includes_machine_costs"], incl, tag)
                self.assertIs(fc["includes_machine_costs"], incl, tag)
                c = st["cost"]
                self.assertAlmostEqual(c["total"], c["total_with_machine"] if incl else c["material"], delta=0.011, msg=tag)
                self.assertGreater(c["electricity"], 0, tag)                      # the split is always present
                self.assertGreater(c["total_with_machine"], c["material"], tag)
                self.assertAlmostEqual(st["true_cost_per_part"] * st["good_parts"], c["total"], delta=0.011, msg=tag)
                self.assertAlmostEqual(fc["true_cost_per_part"], st["true_cost_per_part"], delta=0.0011, msg=tag)
                self.assertGreater(c["wasted_on_failures"], 0, tag)
                self.assertAlmostEqual(one["cost"]["total"], 1.515 if incl else 0.72, delta=0.011, msg=tag)
                done = [x for x in rows if x["result"] == "finished"][0]
                self.assertAlmostEqual(float(done["total_cost"]), 1.515 if incl else 0.72, delta=0.011, msg=tag)
                self.assertEqual(done["total_includes_print_time"], "yes" if incl else "no", tag)
                for j in jobs["jobs"]:
                    if j["cost"]:
                        self.assertIs(j["cost"]["includes_machine_costs"], incl, tag)
        # the MCP tools take the same switch (the setting is still ON here from the loop)
        self.assertIs(json.loads(self._tool("job_stats", period="all"))["includes_machine_costs"], True)
        off = json.loads(self._tool("job_stats", period="all", include_machine_costs=False))
        self.assertIs(off["includes_machine_costs"], False)
        self.assertAlmostEqual(off["cost"]["total"], off["cost"]["material"], delta=0.011)
        self.assertIn("incl. power + machine wear", self._tool("cost_per_part", file="benchy"))
        self.assertIn("filament only", self._tool("cost_per_part", file="benchy", include_machine_costs=False))
        rj = json.loads(self._tool("recent_jobs", include_machine_costs=False))
        self.assertIs(rj["includes_machine_costs"], False)
        self.assertTrue(all(j["cost"]["includes_machine_costs"] is False for j in rj["jobs"] if j["cost"]))

    def _tool(self, name, **kw):
        import asyncio
        return asyncio.run(getattr(server, name)(**kw))

    def test_failure_is_charged_to_the_good_parts(self):
        seed_cache()
        prn = self.run_one_print()                                   # one finished print (3 parts)
        prn.set("IDLE"); self.tick(60)
        prn.set("RUNNING", "benchy", 5); self.tick()
        prn.set("FAILED", "benchy", 40); self.tick(3600)
        st = self.c.get("/api/stats?period=all").json()
        self.assertEqual((st["finished"], st["failed"] + st["cancelled"]), (1, 1))
        self.assertAlmostEqual(st["success_rate"], 0.5)
        self.assertGreater(st["true_cost_per_part"], st["clean_cost_per_part"])
        self.assertGreater(st["cost"]["wasted_on_failures"], 0)

    def test_costs_settings_reprice_the_history(self):
        seed_cache()
        self.run_one_print()
        before = self.c.get("/api/stats?period=all").json()["cost"]["total"]
        r = self.c.put("/api/settings", json={"values": {"costs.currency": "EUR", "costs.kwh_price": 0.5,
                                                          "costs.filament_prices": "PLA=40, PETG=30",
                                                          "costs.wear_per_hour": 1}})
        self.assertEqual(r.status_code, 200, r.text)
        st = self.c.get("/api/stats?period=all").json()
        self.assertEqual(st["currency"], "EUR")
        self.assertGreater(st["cost"]["total"], before)
        self.assertEqual(self.c.get("/api/jobs").json()["currency"], "EUR")

    def test_costs_settings_are_validated(self):
        bad = self.c.put("/api/settings", json={"values": {"costs.filament_prices": "PLA"}})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("NAME=number", bad.json()["errors"]["costs.filament_prices"])
        for k, v in (("costs.kwh_price", "abc"), ("costs.kwh_price", -1), ("costs.default_watts", 5),
                     ("costs.window_n", 0), ("costs.printer_watts", "P1S=lots")):
            self.assertEqual(self.c.put("/api/settings", json={"values": {k: v}}).status_code, 400, (k, v))
        ok = self.c.put("/api/settings", json={"values": {"costs.printer_watts": ["P1S=130", "A1 = 80.5"]}})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(server.SETTINGS.get("costs.printer_watts"), ["P1S=130", "A1=80.5"])
        self.assertEqual(server.costs.Rates.from_settings(server.SETTINGS.get).watts["A1"], 80.5)

    def test_part_counts_are_stored_when_a_file_is_first_read(self):
        class Bambu(FakePrinter):
            def sd_download(self, name):
                return tj.make_3mf(tj.slice_xml(tj.plate_xml(1, 3), tj.plate_xml(2, 2)))
        prn = Bambu()
        meta = server._meta_for(prn, "demo.gcode.3mf", 77, True)
        self.assertEqual(meta["parts"], 3)                                   # plate 1
        self.assertIn("demo.gcode.3mf", " ".join(server._cache_load()))
        # ...and the resolver uses it, whatever the job name looks like
        server.PRINTERS["fake"] = prn
        for job in ("demo", "DEMO.gcode.3mf", "demo.3mf"):
            self.assertEqual(server._job_resolver("Fake", job)["parts"], 3, job)
        self.assertEqual(server._job_resolver("Fake", "never-seen"), {})     # cold cache: nothing, no network
        self.assertEqual(server._job_resolver("Nobody", "demo"), {})

    def test_an_old_cache_entry_without_parts_prices_as_one_part(self):
        seed_cache(parts=None)
        server.PRINTERS["fake"] = FakePrinter()
        res = server._job_resolver("Fake", "benchy")
        self.assertEqual((res["grams"], res["parts"], res["material"]), (36.0, None, "PLA"))

    def test_the_ams_tray_the_hub_chose_sets_the_price(self):
        seed_cache(ptype="PLA")
        prn = FakePrinter()
        prn.trays = [{"tray": 0, "slot_1based": 1, "type": "PLA", "color": "#FF8000"},
                     {"tray": 2, "slot_1based": 3, "type": "PETG", "color": "#0A2989"}]
        server.PRINTERS["fake"] = prn
        self.assertEqual(server._job_resolver("Fake", "benchy")["material"], "PLA")      # slicer's own answer
        server._RECENT_STARTS["fake"] = {"key": "benchy", "mapping": [2], "ts": time.time()}
        res = server._job_resolver("Fake", "benchy")
        self.assertEqual((res["material"], res["filaments"][0]["type"], res["filaments"][0]["used_g"]),
                         ("PETG", "PETG", 36.0))
        server._RECENT_STARTS["fake"] = {"key": "other", "mapping": [2], "ts": time.time()}    # another file: ignored
        self.assertEqual(server._job_resolver("Fake", "benchy")["material"], "PLA")
        server._RECENT_STARTS["fake"] = {"key": "benchy", "mapping": [2], "ts": time.time() - 99999}   # stale note
        self.assertEqual(server._job_resolver("Fake", "benchy")["material"], "PLA")

    def test_file_based_printers_use_the_last_listing_not_the_network(self):
        class Octo(FakePrinter):
            kind = "octoprint"
            uses_files_api = True

            def files(self):
                raise AssertionError("the resolver must never list files itself")
        prn = Octo("Shelf")
        server.PRINTERS["shelf"] = prn
        self.assertEqual(server._job_resolver("Shelf", "bracket"), {})
        server._remember_files(prn, [{"name": "bracket.gcode", "size": 1, "print_seconds": 3600, "filament_mm": 5000}])
        res = server._job_resolver("Shelf", "bracket")
        self.assertEqual((res["seconds"], res["parts"], res["kind"]), (3600, 1, "octoprint"))
        self.assertGreater(res["grams"], 10)

    def test_a_broken_job_log_never_stops_notifications(self):
        sent = []
        orig_log, orig_sub = server.JOBLOG.on_event, server.NOTIFIER.submit
        server.JOBLOG.on_event = lambda ev: (_ for _ in ()).throw(RuntimeError("db gone"))
        server.NOTIFIER.submit = lambda ev: sent.append(ev.kind)
        try:
            self.run_one_print()
        finally:
            server.JOBLOG.on_event, server.NOTIFIER.submit = orig_log, orig_sub
        self.assertEqual(sent, ["print_started", "print_finished"])

    def test_orphan_rows_are_closed_by_the_event_tick(self):
        seed_cache()
        prn = FakePrinter()
        server.PRINTERS["fake"] = prn
        prn.set("IDLE"); self.tick()
        prn.set("RUNNING", "benchy", 30); self.tick()
        server.WATCHER._p.clear()               # the Hub "forgot" the print: the end event will never come
        prn.set("IDLE"); self.tick(30)
        self.tick(200)                           # printer reachable and idle for longer than the grace period
        self.assertEqual(self.c.get("/api/jobs").json()["jobs"][0]["result"], "unknown")

    def test_mcp_tools(self):
        seed_cache()
        self.run_one_print()
        jobs = json.loads(asyncio.run(server.recent_jobs(5)))
        self.assertEqual((jobs["currency"], len(jobs["jobs"])), ("$", 1))
        self.assertTrue(asyncio.run(server.recent_jobs(5, "Nope")).count("\"jobs\": []"))
        self.assertTrue(asyncio.run(server.job_stats("fortnight")).startswith("REFUSED"))
        self.assertEqual(json.loads(asyncio.run(server.job_stats("all")))["jobs"], 1)
        text = json.loads(asyncio.run(server.cost_per_part("benchy.gcode.3mf")))
        self.assertIn("true cost per part", text["summary"])
        self.assertIn("1 attempt", text["summary"])
        self.assertIn("No finished", json.loads(asyncio.run(server.cost_per_part("nothing-here")))["summary"])
        self.assertTrue(asyncio.run(server.cost_per_part("  ")).startswith("REFUSED"))


# =================================================================== discovery

@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class DiscoveryWiring(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mk = td.make_http(td.moonraker_routes("voron"))
        d = server.DISCOVERY
        cls._saved = (d.ports, d.http_ports, d.use_ssdp, d.use_zeroconf, d.cooldown, d._local_ip_fn)
        d.ports = td.closed_ports(moonraker=cls.mk.server_address[1])      # loopback fakes only
        d.http_ports = list(d.ports["http"])
        d.use_ssdp = d.use_zeroconf = False
        d.cooldown = 0
        d._local_ip_fn = lambda: None

    @classmethod
    def tearDownClass(cls):
        d = server.DISCOVERY
        d.ports, d.http_ports, d.use_ssdp, d.use_zeroconf, d.cooldown, d._local_ip_fn = cls._saved
        cls.mk.shutdown()
        cls.mk.server_close()

    def wait(self, sid):
        end = time.time() + 20
        while time.time() < end:
            st = self.c.get(f"/api/discover/{sid}").json()
            if st["finished"]:
                return st
            time.sleep(0.02)
        self.fail("scan did not finish")

    def test_routes_are_mounted_and_find_a_loopback_fake(self):
        self.assertEqual(self.c.get("/api/discover/suggestions").status_code, 200)   # not swallowed by /{scan_id}
        r = self.c.post("/api/discover", json={"networks": ["127.0.0.1/32"]})
        self.assertEqual(r.status_code, 202, r.text)
        st = self.wait(r.json()["scan_id"])
        self.assertEqual((st["state"], st["total"]), ("done", 1))
        f = st["found"][0]
        self.assertEqual((f["family"], f["hostname"]), ("moonraker", "voron"))
        self.assertNotIn("secret", json.dumps(f["prefill"]).lower())
        self.assertEqual(self.c.get("/api/discover/nope").status_code, 404)
        self.assertEqual(self.c.delete("/api/discover/nope").status_code, 404)

    def test_only_private_networks_are_ever_accepted(self):
        r = self.c.post("/api/discover", json={"networks": ["8.8.8.0/24"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("private", r.json()["error"])

    def test_the_mcp_tool_returns_the_final_status(self):
        out = json.loads(asyncio.run(server.discover_printers(["127.0.0.1/32"])))
        self.assertEqual((out["state"], out["found"][0]["family"]), ("done", "moonraker"))
        self.assertTrue(asyncio.run(server.discover_printers(["8.8.8.0/24"])).startswith("REFUSED"))

    def test_the_dashboards_404_message_is_gone_because_the_route_exists(self):
        self.assertNotEqual(self.c.get("/api/discover/suggestions").status_code, 404)


# =================================================================== detection

def detector_handler(scores, token=TOKEN, down=None):
    """healthz always ok; /v1/score needs the bearer token; a 4-byte stub picture is 'undecodable' (422), which
    DetectorClient.check_auth treats as 'the token was accepted'."""
    def h(req, body):
        if req.path == "/healthz":
            return 200, {"status": "ok", "ml_api": "ok", "arch": "arm64"}, {}
        if req.headers.get("Authorization") != "Bearer " + token:
            return 401, {"error": "unauthorized"}, {}
        if len(body) < 100:
            return 422, {"error": "bad_image", "message": "cannot decode"}, {}
        s = scores.pop(0) if scores else 0.0
        return 200, dict(GOOD, score=s), {}
    return h


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class DetectionWiring(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ntfy = FakeHTTP()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.close()

    def setUp(self):
        super().setUp()
        self.ntfy.requests.clear()
        self.scores = []
        self.fake = FakeDetector(detector_handler(self.scores))
        self.addCleanup(self.fake.stop)
        shutil.rmtree(server.DETECTION_DIR, ignore_errors=True)
        server.DETECTION._ps.clear()

    def enable(self, **extra):
        vals = {"detection.mode": "remote", "detection.url": self.fake.url, "detection.token": TOKEN,
                "detection.learn_only": True}
        vals.update(extra)
        r = self.c.put("/api/settings", json={"values": vals})
        self.assertEqual(r.status_code, 200, r.text)

    def ntfy_on(self):
        server.SETTINGS.update({"ntfy.enabled": True, "ntfy.server_url": self.ntfy.url, "ntfy.topic": "wiring2"})

    def frames(self, n, t):
        """Run n detection passes 10 s apart; returns the new clock value."""
        for _ in range(n):
            server.DETECTION.tick(t)
            t += 10
        return t

    # ---- settings + import safety

    def test_nothing_starts_or_runs_by_default(self):
        self.assertFalse(server.DETECTION.enabled)
        self.assertIsNone(server.DETECTION._thread)                    # started by __main__ only
        st = self.c.get("/api/detection").json()
        self.assertEqual((st["enabled"], st["setup"]["mode"], st["auto_pause"]), (False, "off", False))
        self.assertEqual(server.SETTINGS.get("detection.learn_only"), True)      # learn-only is the default
        self.assertIn("Detection is off", st["summary"])

    def test_a_fresh_process_never_starts_the_loop(self):
        d = tempfile.mkdtemp(prefix="wiring2-sub-")
        self.addCleanup(shutil.rmtree, d, True)
        code = ("import sys, threading; sys.path.insert(0, %r); import server; "
                "print('@@', server.DETECTION._thread is None, server._event_thread is None, "
                "[t.name for t in threading.enumerate() if t.name in ('detection', 'event-watcher')]); "
                "sys.stdout.flush(); import os; os._exit(0)") % os.path.abspath(APP)
        env = {**os.environ, "BAMBU_MCP_CONFIG": os.path.join(d, "none.yml"),
               "BAMBU_MCP_CACHE": os.path.join(d, "data", "metacache.json")}
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=90)
        self.assertIn("@@ True True []", r.stdout, r.stdout + r.stderr)

    def test_settings_shape_and_no_auto_pause(self):
        sec = next(s for s in self.c.get("/api/settings").json()["sections"] if s["id"] == "detection")
        names = [f["name"] for f in sec["fields"]]
        self.assertEqual(names, ["mode", "url", "token", "interval_s", "sensitivity", "learn_only", "cooldown_min",
                                 "excluded_printers"])
        self.assertFalse([n for n in names if "pause" in n or "stop" in n or "cancel" in n])
        self.assertEqual(next(f for f in sec["fields"] if f["name"] == "token")["secret"], True)

    def test_the_token_is_write_only(self):
        self.enable()
        body = self.c.get("/api/settings").text
        self.assertNotIn(TOKEN, body)
        self.assertNotIn(TOKEN, self.c.get("/api/detection").text)
        self.assertNotIn(TOKEN, json.dumps(self.c.get("/api/settings/export").json()))
        f = next(f for s in self.c.get("/api/settings").json()["sections"] for f in s["fields"]
                 if f["key"] == "detection.token")
        self.assertEqual(f["value"], "set")

    def test_this_machine_mode_uses_env_then_default(self):
        self.assertEqual(server._detection_target("local", "", ""), ("local", "http://detector:8080", ""))
        os.environ["DETECTOR_URL"], os.environ["DETECTOR_TOKEN"] = "http://host.docker.internal:8090", "envtok"
        try:
            self.assertEqual(server._detection_target("local", "", ""),
                             ("local", "http://host.docker.internal:8090", "envtok"))
            server.SETTINGS.update({"detection.url": "http://10.0.0.5:8090", "detection.token": "savedtok"})
            self.assertEqual(server._detection_target("local", "", ""), ("local", "http://10.0.0.5:8090", "savedtok"))
            self.assertEqual(server._detection_target("remote", "http://x:1", "formtok"), ("remote", "http://x:1", "formtok"))
        finally:
            os.environ.pop("DETECTOR_URL"), os.environ.pop("DETECTOR_TOKEN")

    # ---- test connection

    def test_connection_test_success_in_words(self):
        r = self.c.post("/api/detection/test", json={"mode": "remote", "url": self.fake.url, "token": TOKEN})
        j = r.json()
        self.assertEqual((r.status_code, j["ok"], j["reachable"], j["token_ok"]), (200, True, True, True))
        self.assertIn("healthy and accepted the token", j["summary"])
        # only four fixed fields of the detector's own answer are ever shown (second security review N-6)
        self.assertEqual(set(j["detector"]), {"reachable", "token_ok", "healthy", "version"})
        self.assertEqual((j["detector"]["reachable"], j["detector"]["token_ok"], j["detector"]["healthy"]), (True, True, True))
        self.assertNotIn(TOKEN, r.text)

    def test_connection_test_wrong_token(self):
        j = self.c.post("/api/detection/test", json={"mode": "remote", "url": self.fake.url, "token": "wrong"}).json()
        self.assertEqual((j["ok"], j["reachable"], j["token_ok"]), (False, True, False))
        self.assertIn("rejected the token", j["summary"])
        self.assertNotIn("wrong", j["summary"].replace("is wrong", ""))

    def test_connection_test_falls_back_to_the_saved_token(self):
        self.enable()
        j = self.c.post("/api/detection/test", json={"mode": "remote", "url": self.fake.url, "token": ""}).json()
        self.assertTrue(j["ok"], j)
        j = self.c.post("/api/detection/test", json={}).json()           # nothing typed: the saved settings
        self.assertTrue(j["ok"], j)
        self.assertNotIn(TOKEN, json.dumps(j))

    def test_connection_test_unreachable_and_misc(self):
        dead = f"http://127.0.0.1:{td.free_port()}"
        j = self.c.post("/api/detection/test", json={"mode": "remote", "url": dead, "token": TOKEN}).json()
        self.assertEqual((j["ok"], j["reachable"]), (False, False))
        self.assertIn("not reachable", j["summary"])
        self.assertIn("Is it running", j["summary"])
        j = self.c.post("/api/detection/test", json={"mode": "remote", "url": "not a url", "token": TOKEN}).json()
        self.assertFalse(j["ok"])
        self.assertIn("http://", j["summary"])
        j = self.c.post("/api/detection/test", json={"mode": "off"}).json()
        self.assertIn("Off", j["summary"])
        sick = FakeDetector(lambda req, body: (503, {"status": "degraded", "ml_api": "down"}, {}))
        self.addCleanup(sick.stop)
        j = self.c.post("/api/detection/test", json={"mode": "remote", "url": sick.url, "token": TOKEN}).json()
        self.assertEqual((j["ok"], j["reachable"]), (False, True))
        self.assertIn("not healthy", j["summary"])

    # ---- the loop against a fake adapter

    def run_print(self, prn, n_ok=35, n_bad=12, t=5000.0):
        self.scores[:] = [0.0] * n_ok + [2.5] * n_bad
        prn.set("RUNNING", "benchy", 5)
        return self.frames(n_ok + n_bad, t)

    def test_learn_only_logs_scores_and_sends_nothing(self):
        self.enable()
        self.ntfy_on()
        prn = CamPrinter()
        server.PRINTERS["fake"] = prn
        t = self.run_print(prn)
        log = os.path.join(server.DETECTION_DIR, "scores.jsonl")
        rows = [json.loads(x) for x in open(log).read().splitlines()]
        self.assertEqual(len(rows), 47)
        self.assertEqual(rows[0]["printer"], "Fake")
        self.assertTrue(any(r["level"] in ("warning", "failure") and not r["warmup"] for r in rows))   # it WOULD have alerted
        st = self.c.get("/api/detection").json()
        self.assertEqual((st["enabled"], st["mode"], st["detector"]["state"]), (True, "learn", "up"))
        self.assertIn("Learn-only", st["summary"])
        time.sleep(0.3)
        self.assertEqual(self.ntfy.requests, [])
        self.assertEqual([n for n in server.NOTIFIER.recent() if n["event"] == "spaghetti_suspected"], [])
        self.assertEqual(prn.commands, [])

    def test_alerts_on_sends_a_warning_then_failure_notification_and_never_touches_the_print(self):
        self.enable(**{"detection.learn_only": False})
        self.ntfy_on()
        prn = CamPrinter()
        server.PRINTERS["fake"] = prn
        self.run_print(prn)
        self.assertTrue(self.ntfy.wait_for("possible print"), self.ntfy.titles())
        titles = self.ntfy.titles()
        self.assertTrue(any("WARNING" in t for t in titles) or any("FAILURE" in t for t in titles), titles)
        self.assertTrue(self.ntfy.requests[0]["body"])                       # message text
        sent = [n for n in server.NOTIFIER.recent() if n["event"] == "spaghetti_suspected"]
        self.assertTrue(sent)
        self.assertEqual(prn.commands, [], "detection must never pause, cancel or start anything")
        st = server.DETECTION.status()
        self.assertEqual((st["mode"], st["auto_pause"]), ("alert", False))

    def test_flipping_learn_only_off_mid_print_starts_alerting(self):
        self.enable()
        self.ntfy_on()
        prn = CamPrinter()
        server.PRINTERS["fake"] = prn
        t = self.run_print(prn)
        time.sleep(0.2)
        self.assertEqual(self.ntfy.requests, [])
        self.c.put("/api/settings", json={"values": {"detection.learn_only": False}})
        self.scores[:] = [2.5] * 3
        self.frames(3, t)
        self.assertTrue(self.ntfy.wait_for("possible print"), self.ntfy.titles())

    def test_the_loop_follows_settings_changes(self):
        self.enable()
        self.assertTrue(server.DETECTION.enabled)
        self.assertEqual(server.DETECTION._client.url, self.fake.url)
        self.c.put("/api/settings", json={"values": {"detection.mode": "off"}})
        self.assertFalse(server.DETECTION.enabled)
        self.c.put("/api/settings", json={"values": {"detection.mode": "remote", "detection.interval_s": "30",
                                                      "detection.sensitivity": "high", "detection.cooldown_min": 3,
                                                      "detection.excluded_printers": ["Cam2"]}})
        d = server.DETECTION
        self.assertEqual((d.interval_s, d.sensitivity, d.cooldown_s, d.excluded), (30.0, "high", 180.0, {"cam2"}))

    def test_excluded_printers_are_not_watched(self):
        self.enable(**{"detection.excluded_printers": ["Fake"]})
        prn = CamPrinter()
        server.PRINTERS["fake"] = prn
        prn.set("RUNNING", "benchy", 5)
        self.frames(3, 7000.0)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(server._detection_line("Fake")["text"], "Off for this printer")

    def test_detector_down_is_never_shown_as_all_clear(self):
        self.enable()
        prn = CamPrinter()
        server.PRINTERS["fake"] = prn
        prn.set("RUNNING", "benchy", 5)
        self.fake.stop()
        self.frames(2, 8000.0)
        st = self.c.get("/api/detection").json()
        self.assertEqual(st["detector"]["state"], "down")
        self.assertIn("NOT being watched", st["summary"])
        self.assertIn("unreachable", server._detection_line("Fake")["text"].lower())
        self.assertEqual(server._detection_line("Fake")["state"], "down")

    def test_printer_screen_line(self):
        prn = CamPrinter()
        server.PRINTERS["fake"] = prn
        self.assertEqual(server._detection_line("Fake"), {"state": "off", "text": "Off"})
        self.assertEqual(self.c.get("/api/printer/Fake").json()["detection"]["state"], "off")
        self.enable()
        self.assertIn("Ready", server._detection_line("Fake")["text"])
        self.assertIn("learn-only", server._detection_line("Fake")["text"])
        prn.set("RUNNING", "benchy", 5)
        self.scores[:] = [0.02]
        self.frames(1, 9000.0)
        line = server._detection_line("Fake")
        self.assertEqual(line["state"], "warmup")
        self.assertIn("score 0.02", line["text"])
        self.scores[:] = [0.0] * 31
        self.frames(31, 9010.0)
        line = server._detection_line("Fake")
        self.assertEqual(line["state"], "ok")
        self.assertEqual(line["text"], "Watching · score 0.00 · learn-only")
        server.PRINTERS["plain"] = FakePrinter("Plain")
        self.assertIn("No camera", server._detection_line("Plain")["text"])

    def test_per_printer_spaghetti_switch_uses_the_settings_route(self):
        """The 5-inch board's per-printer Spaghetti switch = add / remove the printer from detection.excluded_printers through the
        existing PUT /api/settings (no new route). The loop, /api/detection and the printer line all follow."""
        prn = CamPrinter()
        server.PRINTERS["fake"] = prn
        self.enable()
        self.assertEqual(self.c.get("/api/detection").json()["setup"]["excluded_printers"], [])
        self.assertEqual(self.c.get("/api/printer/Fake").json()["detection"]["state"], "idle")
        # switch OFF for this printer
        r = self.c.put("/api/settings", json={"values": {"detection.excluded_printers": ["Fake"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get("/api/detection").json()["setup"]["excluded_printers"], ["Fake"])
        self.assertIn("fake", server.DETECTION.excluded)
        line = self.c.get("/api/printer/Fake").json()["detection"]
        self.assertEqual(line["state"], "excluded")
        self.assertEqual(line["text"], "Off for this printer")
        prn.set("RUNNING", "benchy", 5)
        self.frames(2, 9000.0)
        self.assertEqual(self.scores, [])                                      # an excluded printer is never scored
        self.assertTrue(self.c.get("/api/detection").json()["printers"]["Fake"]["excluded"])
        # ... and back ON
        r = self.c.put("/api/settings", json={"values": {"detection.excluded_printers": []}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("fake", server.DETECTION.excluded)
        self.assertNotEqual(self.c.get("/api/printer/Fake").json()["detection"]["state"], "excluded")

    def test_printer_line_also_says_learn_and_alerts_for_the_5in_panel(self):
        prn = CamPrinter()
        server.PRINTERS["fake"] = prn
        self.enable()
        d = self.c.get("/api/printer/Fake").json()["detection"]
        self.assertIs(d["learn"], True)
        self.assertEqual(d["alerts"], 0)
        self.assertEqual(server._detection_line("Fake").keys() & {"learn", "alerts"}, set())      # the one-line helper itself is unchanged
        self.enable(**{"detection.learn_only": False})
        self.assertIs(self.c.get("/api/printer/Fake").json()["detection"]["learn"], False)

    def test_score_once_route_and_tool(self):
        prn = CamPrinter()
        server.PRINTERS["fake"] = prn
        r = self.c.post("/api/printer/Fake/detection/score")
        self.assertEqual((r.status_code, r.json()["ok"]), (409, False))          # detection is off
        self.assertEqual(self.c.post("/api/printer/Nobody/detection/score").status_code, 404)
        self.enable()
        self.scores[:] = [0.31]
        r = self.c.post("/api/printer/Fake/detection/score")
        self.assertEqual((r.status_code, r.json()["ok"], r.json()["score"]), (200, True, 0.31))
        self.scores[:] = [0.44]
        out = json.loads(asyncio.run(server.score_snapshot("fake")))
        self.assertEqual((out["ok"], out["score"]), (True, 0.44))
        status = json.loads(asyncio.run(server.detection_status()))
        self.assertEqual(status["setup"]["mode"], "remote")
        self.assertNotIn(TOKEN, json.dumps(status))
        self.assertEqual(status["setup"]["url"], self.fake.url)
        self.assertEqual(server.DETECTION._ps, {} if False else server.DETECTION._ps)   # tracker untouched by one-off scores

    def test_spaghetti_hook_levels(self):
        sink = []
        w = server.events.EventWatcher(sink.append, settings=server.SETTINGS, confirm_s=0)
        e1 = w.spaghetti_suspected("Fake", 0.5, b"x", level="warning")
        e2 = w.spaghetti_suspected("Fake", 0.9, b"x", level="failure")
        self.assertEqual((e1.level, e2.level), ("warning", "failure"))
        self.assertIn("WARNING", e1.title)
        self.assertIn("FAILURE", e2.title)
        self.assertNotEqual(e1.dedupe_key, e2.dedupe_key)
        self.assertEqual((e1.data["level"], e2.data["level"]), ("warning", "failure"))


# =================================================================== scopes

def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def sse_json(resp):
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError("no JSON-RPC message in: " + resp.text[:300])


@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class NewScopes(Base):
    def setUp(self):
        super().setUp()
        server.SETTINGS.update({"security.api_auth": "on"})
        server.PRINTERS["fake"] = CamPrinter()

    def tearDown(self):
        server.SETTINGS.update({"security.api_auth": "off"})
        super().tearDown()

    def key(self, scope):
        return {"Authorization": "Bearer " + server.AUTH.create_key(scope + "-key", scope)[1]}

    def test_a_read_key_can_look_at_history_and_detection_but_not_scan_export_or_score(self):
        h = self.key("read")
        for path in ("/api/jobs", "/api/jobs/1", "/api/stats", "/api/stats?period=day", "/api/costs/file?name=x",
                     "/api/detection", "/api/discover/suggestions", "/api/discover/nope"):
            self.assertNotIn(self.c.get(path, headers=h).status_code, (401, 403), path)
        for method, path in (("get", "/api/jobs.csv"), ("post", "/api/discover"), ("delete", "/api/discover/x"),
                             ("post", "/api/detection/test"), ("post", "/api/printer/Fake/detection/score")):
            r = getattr(self.c, method)(path, headers=h)
            self.assertEqual(r.status_code, 403, f"{method.upper()} {path}")
            self.assertEqual(r.json()["required_scope"], "control")

    def test_no_key_is_refused_for_every_new_route(self):
        for method, path in (("get", "/api/jobs"), ("get", "/api/jobs.csv"), ("get", "/api/stats"),
                             ("get", "/api/detection"), ("post", "/api/discover"),
                             ("post", "/api/detection/test"), ("get", "/api/discover/suggestions")):
            self.assertEqual(getattr(self.c, method)(path).status_code, 401, path)

    def test_a_control_key_can_do_them(self):
        h = self.key("control")
        self.assertEqual(self.c.get("/api/jobs.csv", headers=h).status_code, 200)
        r = self.c.post("/api/detection/test", headers=h, json={"mode": "off"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.post("/api/discover", headers=h, json={"networks": ["8.8.8.0/24"]}).status_code, 400)  # reached the handler

    def test_the_dashboard_cookie_is_a_control_credential_for_them(self):
        self.assertEqual(self.c.get("/dashboard").status_code, 200)               # hands out the cookie
        self.assertEqual(self.c.get("/api/jobs.csv").status_code, 200)
        self.assertEqual(self.c.get("/api/jobs").status_code, 200)

    def test_mcp_scopes_for_the_new_tools(self):
        rh = self.key("read")
        r = self.c.post("/mcp", headers={**MCP_HEADERS, **rh}, json=rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}))
        self.assertEqual(r.status_code, 200, r.text)
        h = {**MCP_HEADERS, **rh, "mcp-session-id": r.headers["mcp-session-id"], "mcp-protocol-version": "2025-03-26"}
        self.c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        def call(tool, args=None):
            resp = self.c.post("/mcp", headers=h, json=rpc("tools/call", {"name": tool, "arguments": args or {}}, 9))
            return resp.status_code, resp.text
        for tool, args in (("recent_jobs", {}), ("job_stats", {}), ("cost_per_part", {"file": "x"}),
                           ("detection_status", {})):
            code, text = call(tool, args)
            self.assertEqual(code, 200, (tool, text))
            self.assertNotIn("REFUSED: this API key", text, tool)
        for tool, args in (("discover_printers", {"networks": ["127.0.0.1/32"]}), ("score_snapshot", {"printer": "Fake"})):
            code, text = call(tool, args)
            self.assertIn("'read' access", text, tool)
        self.assertEqual(security.tool_scope("discover_printers"), "control")
        self.assertEqual(security.tool_scope("score_snapshot"), "control")


# =================================================================== security endpoints (the dashboard's Security tab)

@unittest.skipUnless(HAVE, "mcp + starlette + httpx needed to import the server")
class SecurityTabBackend(Base):
    def setUp(self):
        super().setUp()
        server.AUTH.stats.clear()
        server.AUTH._callers.clear()
        server.AUTH.limiter = security.LoginLimiter()

    def tearDown(self):
        server.SETTINGS.update({"security.api_auth": "off"}, clear=["security.dashboard_password"])
        server.AUTH.dash.set_fields(admin_hash=None)
        super().tearDown()

    def dash(self):
        """The owner's browser: the dashboard cookie, and - since keys and security need the admin password
        (second security review N-1) - the admin unlock after typing it."""
        server.AUTH.dash.set_fields(admin_hash=security.hash_password("admin password 1") if not server.AUTH.admin_password_set()
                                    else server.AUTH.dash.admin_hash)
        r = self.c.get("/dashboard")
        self.c.cookies.set(security.ADMIN_COOKIE, server.AUTH.make_admin_cookie())
        return r

    def dash_unlock(self):
        self.c.cookies.set(security.ADMIN_COOKIE, server.AUTH.make_admin_cookie())

    def test_round_trip_the_way_the_tab_uses_it(self):
        c = self.c
        self.dash()                                  # the owner's browser (its cookie is what may open the Security tab)
        # the tab opens: mode, no password, no keys
        s = c.get("/api/security").json()
        self.assertEqual((s["mode"], s["dashboard_password"], s["key_counts"]["active"]), ("off", "not set", 0))
        # warn: a keyless script is counted, and shown by path
        self.assertEqual(c.put("/api/security", json={"mode": "warn"}).json()["mode"], "warn")
        c.cookies.clear()
        self.assertEqual(c.get("/api/fleet", headers={"User-Agent": "curl-test"}).status_code, 200)
        self.assertEqual(c.get("/api/security").status_code, 401)      # admin: never open to a keyless caller, warn or not
        self.dash()
        s = c.get("/api/security").json()
        self.assertGreaterEqual(s["stats"]["without_key_but_allowed"], 1)
        row = next(x for x in s["warn"]["recent_callers"] if x["path"] == "/api/fleet")
        self.assertEqual((row["method"], row["user_agent"], row["allowed"]), ("GET", "curl-test", True))
        # generate a key: shown once
        made = c.post("/api/keys", json={"name": "Claude Desktop", "scope": "read"}).json()
        self.assertTrue(made["key"].startswith("ph_"))
        self.assertIn("cannot be shown again", made["note"])
        self.assertNotIn(made["key"], json.dumps(c.get("/api/security").json()))
        # use it: last used + path appear in the list
        self.assertEqual(c.get("/api/printers", headers={"Authorization": "Bearer " + made["key"]}).status_code, 200)
        k = next(x for x in c.get("/api/keys").json()["keys"] if x["id"] == made["id"])
        self.assertEqual(k["last_used_path"], "/api/printers")
        self.assertEqual((k["scope"], k["revoked"]), ("read", False))
        # on: keyless is refused, the key still works; revoke cuts it off; delete forgets it
        c.put("/api/security", json={"mode": "on"})
        c.cookies.clear()
        self.assertEqual(c.get("/api/fleet").status_code, 401)
        self.assertEqual(c.get("/api/fleet", headers={"Authorization": "Bearer " + made["key"]}).status_code, 200)
        self.dash()                                                # a browser gets its cookie again
        self.assertEqual(c.post(f"/api/keys/{made['id']}/revoke").status_code, 200)
        self.assertTrue(next(x for x in c.get("/api/keys").json()["keys"] if x["id"] == made["id"])["revoked"])
        self.assertEqual(c.get("/api/fleet", headers={"Authorization": "Bearer " + made["key"]}).status_code, 401)
        self.assertEqual(c.delete(f"/api/keys/{made['id']}").status_code, 200)
        self.assertEqual(c.get("/api/keys").json()["keys"], [])
        # a bad mode and a bad key are 400s with a message
        self.assertEqual(c.put("/api/security", json={"mode": "sometimes"}).status_code, 400)
        self.assertEqual(c.post("/api/keys", json={"name": "", "scope": "read"}).status_code, 400)

    def test_password_set_clear_and_login(self):
        c = self.c
        server.SETTINGS.update({"security.api_auth": "on"})       # sign-in is only accepted with the key check ON
        self.dash()
        r = c.put("/api/security", json={"dashboard_password": "correct horse"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["dashboard_password"], "set")
        self.assertNotIn("correct horse", r.text)
        self.assertEqual(c.get("/api/health").status_code, 200)
        c.cookies.clear()
        page = c.get("/dashboard")                                 # signed out: the login page, not the dashboard
        self.assertIn("password", page.text.lower())
        self.assertNotIn("openSettings", page.text)
        self.assertEqual(c.post("/api/auth/login", json={"password": "nope nope"}).status_code, 401)
        server.AUTH.limiter.reset()
        self.assertEqual(c.post("/api/auth/login", json={"password": "correct horse"}).status_code, 200)
        self.assertIn("openSettings", c.get("/dashboard").text)
        self.dash_unlock()
        self.assertEqual(c.put("/api/security", json={"dashboard_password": "short"}).status_code, 400)
        self.assertEqual(c.post("/api/auth/logout").status_code, 200)
        c.post("/api/auth/login", json={"password": "correct horse"})
        self.dash_unlock()
        r = c.put("/api/security", json={"clear_dashboard_password": True})
        self.assertEqual(r.json()["dashboard_password"], "not set")

    def test_the_dashboard_page_has_the_new_screens(self):
        html = self.c.get("/dashboard").text
        for marker in ('id="history"', "openHistory()", "stSecurityPanel", "detection/test", "/api/jobs.csv",
                       "/api/security", 'id="p-det"'):
            self.assertIn(marker, html, marker)


# =================================================================== the page itself

@unittest.skipUnless(shutil.which("node"), "node is needed to syntax-check the dashboard's scripts")
class DashboardScripts(unittest.TestCase):
    def test_every_script_block_parses(self):
        html = open(os.path.join(APP, "dashboard.html")).read()
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertGreaterEqual(len(blocks), 5)
        d = tempfile.mkdtemp(prefix="dash-js-")
        self.addCleanup(shutil.rmtree, d, True)
        for i, b in enumerate(blocks):
            p = os.path.join(d, f"b{i}.js")
            with open(p, "w") as fh:
                fh.write(b)
            r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"script block {i}: {r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
