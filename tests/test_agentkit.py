"""Tests for the built-in agent kit (app/agentkit.py + the fabrica_guide tool, resource, prompts, /api/agent-kit).

    python3 tests/test_agentkit.py

Everything is local: BAMBU_MCP_CONFIG / BAMBU_MCP_CACHE point at a throwaway temp directory, the key check is off,
there are no printers and nothing touches the network.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

_TMP = tempfile.mkdtemp(prefix="agentkit-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")
os.environ["BAMBU_MCP_API_AUTH"] = "off"

try:
    import agentkit  # noqa: E402
    import security  # noqa: E402
    import server  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402
    HAVE = True
except Exception as e:      # pragma: no cover - the app's deps are not installed
    HAVE = False
    print("SKIP: app dependencies missing:", e)

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def msg(resp):
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(resp.text[:300])


def make_guides(d, **texts):
    os.makedirs(d, exist_ok=True)
    base = {"start": "S", "printing": "P", "library": "L", "history": "H", "renaming": "R", "changelog": "- one"}
    base.update(texts)
    for k, v in base.items():
        with open(os.path.join(d, k + ".md"), "w") as f:
            f.write(v)


TOOLS = {"alpha": "does a", "beta": "does b"}
PROMPTS = ["p1", "p2"]


@unittest.skipUnless(HAVE, "app dependencies needed")
class KitUnits(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="kit-unit-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.g = os.path.join(self.d, "guides")
        make_guides(self.g)

    def kit(self, tools=None, prompts=None, build="b1"):
        k = agentkit.Kit(self.d, build, self.g)
        k.finish(dict(tools or TOOLS), prompts or PROMPTS)
        return k

    def records(self):
        with open(os.path.join(self.d, "agent_kit.json")) as f:
            return json.load(f)["records"]

    def test_version_format_and_stability(self):
        v = self.kit().version
        self.assertRegex(v, r"^k-[0-9a-f]{8}$")
        self.assertEqual(self.kit().version, v)
        self.assertEqual(self.kit(build="other-build").version, v)       # the build id is not part of the version

    def test_version_changes_with_guide_tool_description_tool_set_and_prompts(self):
        v = self.kit().version
        make_guides(self.g, printing="P changed")
        v2 = self.kit().version
        self.assertNotEqual(v2, v)
        self.assertNotEqual(self.kit({**TOOLS, "alpha": "does a better"}).version, v2)
        self.assertNotEqual(self.kit({**TOOLS, "gamma": "new"}).version, v2)
        self.assertNotEqual(self.kit(prompts=PROMPTS + ["p3"]).version, v2)

    def test_record_only_on_change_capped_atomic_and_private(self):
        self.kit()
        self.kit()
        recs = self.records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(set(r), {"version", "date", "build", "tools", "tool_desc", "prompts", "guides"})
        self.assertEqual(r["tools"], ["alpha", "beta"])
        self.assertEqual(r["prompts"], ["p1", "p2"])
        self.assertIn("printing", r["guides"])
        mode = stat.S_IMODE(os.stat(os.path.join(self.d, "agent_kit.json")).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual([f for f in os.listdir(self.d) if f.endswith(".tmp")], [])
        for i in range(agentkit.KEEP + 5):
            make_guides(self.g, start=f"S{i}")
            self.kit()
        recs = self.records()
        self.assertEqual(len(recs), agentkit.KEEP)
        self.assertEqual(recs[-1]["version"], self.kit().version)

    def test_unreadable_record_file_is_replaced_not_fatal(self):
        with open(os.path.join(self.d, "agent_kit.json"), "w") as f:
            f.write("{not json")
        k = self.kit()
        self.assertEqual(self.records()[-1]["version"], k.version)

    def test_guide_topics_have_header_and_unknown_topic(self):
        k = self.kit()
        for t in ("start", "printing", "library", "history", "renaming"):
            out = k.guide(t)
            self.assertTrue(out.startswith(f"Fabrica kit {k.version} · build b1\n"), t)
        self.assertIn("Unknown topic", k.guide("../etc/passwd"))
        self.assertIn("Topics:", k.guide("nope"))

    def test_changes_diff(self):
        old = self.kit()
        make_guides(self.g, printing="P new", changelog="- one\n- two")
        new = self.kit({"alpha": "does a MORE", "gamma": "new tool"}, ["p1", "p3"])
        out = new.guide("changes", old.version)
        self.assertTrue(out.startswith(f"Fabrica kit {new.version}"))
        self.assertIn("Tools added: gamma", out)
        self.assertIn("Tools removed: beta", out)
        self.assertIn("changed description: alpha", out)
        self.assertIn("Guides changed: printing", out)
        self.assertIn("prompts) added: p3", out)
        self.assertIn("prompts) removed: p2", out)
        self.assertIn("- two", out)                                       # the release notes
        self.assertNotIn("Guides changed: changelog", out)

    def test_changes_same_unknown_and_empty_since(self):
        k = self.kit()
        self.assertIn("No changes", k.guide("changes", k.version))
        out = k.guide("changes", "k-deadbeef")
        self.assertIn("do not know", out)
        self.assertTrue(out.rstrip().endswith("S"))                           # falls back to the full start guide
        self.assertIn("Recent release notes", k.guide("changes"))

    def test_instructions_short_and_complete(self):
        k = self.kit()
        text = k.instructions()
        self.assertLessEqual(len(text.splitlines()), 15)
        for needle in ("fabrica_guide", "plate is clear", "suggest_mapping", "start_print", "cancel", "idle",
                       "REFUSED", k.version, "since="):
            self.assertIn(needle, text)


@unittest.skipUnless(HAVE, "app dependencies needed")
class KitInServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(server.mcp.streamable_http_app())
        cls.c.__enter__()               # MCP's session manager needs the app's lifespan running

    @classmethod
    def tearDownClass(cls):
        cls.c.__exit__(None, None, None)
        shutil.rmtree(_TMP, True)

    def session(self):
        r = self.c.post("/mcp", headers=MCP_HEADERS, json=rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}))
        self.assertEqual(r.status_code, 200, r.text)
        h = {**MCP_HEADERS, "mcp-session-id": r.headers["mcp-session-id"], "mcp-protocol-version": "2025-03-26"}
        self.c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        return msg(r), h

    def test_instructions_arrive_on_connect(self):
        init, _ = self.session()
        text = init["result"]["instructions"]
        self.assertLessEqual(len(text.splitlines()), 15)
        self.assertIn(server.KIT.version, text)
        self.assertIn("fabrica_guide", text)

    def test_every_topic_over_mcp_has_the_version_header(self):
        _, h = self.session()
        for t in ("start", "printing", "library", "history", "renaming", "changes"):
            r = self.c.post("/mcp", headers=h, json=rpc("tools/call", {"name": "fabrica_guide", "arguments": {"topic": t}}))
            text = msg(r)["result"]["content"][0]["text"]
            self.assertTrue(text.startswith(f"Fabrica kit {server.KIT.version} · build {server.BUILD_REV}"), t)
        self.assertEqual(len(server.KIT.guide("start").splitlines()) < 60, True)

    def test_shipped_guides_are_short_and_name_only_real_tools(self):
        import re
        tools = {t.name for t in server.mcp._tool_manager.list_tools()}
        pending = {"file_facts", "rename_preview", "rename_apply"}          # built on another branch
        pending |= {"print_a_file", "fleet_status", "print_history_report"}  # workflows (prompts), not tools
        for topic in agentkit.TOPICS[:-1]:
            text = server.KIT.guide(topic)
            self.assertLessEqual(len(text.splitlines()), 45, topic)
            calls = re.findall(r"`([a-z_]+)\(", text)                    # `tool_name(` = a call
            if topic == "start":
                calls += re.findall(r"`([a-z_]+)`", text.split("## What each tool family")[1].split("## Keys")[0])
            for name in calls:
                if name not in pending and name not in agentkit.TOPICS:
                    self.assertIn(name, tools, f"{topic}: `{name}` is not a tool")

    def test_scope_and_rest(self):
        self.assertEqual(security.TOOL_SCOPES["fabrica_guide"], "read")
        self.assertEqual(security.rest_scope("GET", "/api/agent-kit"), "read")
        j = self.c.get("/api/agent-kit").json()
        self.assertEqual(j["version"], server.KIT.version)
        self.assertEqual(j["build"], server.BUILD_REV)
        self.assertEqual(j["topics"], list(agentkit.TOPICS))
        self.assertEqual(sorted(j["prompts"]), ["fleet_status", "print_a_file", "print_history_report", "rename_files"])
        self.assertEqual(server.SETTINGS.get("agents.agent_kit"), server.KIT.version)

    def test_list_printers_carries_the_version_line(self):
        out = server.list_printers()
        self.assertTrue(out.startswith(f"Fabrica kit {server.KIT.version}\n"))

    def test_resource_and_prompts_registered(self):
        _, h = self.session()
        r = msg(self.c.post("/mcp", headers=h, json=rpc("resources/templates/list")))
        self.assertIn("fabrica://guide/{topic}", [t["uriTemplate"] for t in r["result"]["resourceTemplates"]])
        r = msg(self.c.post("/mcp", headers=h, json=rpc("resources/read", {"uri": "fabrica://guide/printing"})))
        self.assertEqual(r["result"]["contents"][0]["text"], server.KIT.guide("printing"))
        r = msg(self.c.post("/mcp", headers=h, json=rpc("prompts/list")))
        self.assertEqual(sorted(p["name"] for p in r["result"]["prompts"]),
                         ["fleet_status", "print_a_file", "print_history_report", "rename_files"])
        r = msg(self.c.post("/mcp", headers=h, json=rpc("prompts/get", {"name": "print_a_file",
                                                                       "arguments": {"file": "x.3mf"}})))
        self.assertIn("x.3mf", r["result"]["messages"][0]["content"]["text"])
        self.assertIn("plate", r["result"]["messages"][0]["content"]["text"])

    def test_server_recorded_this_version_in_the_data_dir(self):
        p = os.path.join(os.path.dirname(os.environ["BAMBU_MCP_CACHE"]), "agent_kit.json")
        with open(p) as f:
            recs = json.load(f)["records"]
        self.assertEqual(recs[-1]["version"], server.KIT.version)
        self.assertIn("fabrica_guide", recs[-1]["tools"])
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=1)
