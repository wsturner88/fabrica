"""5-inch wall screen: after CAM_IDLE_MS with no touch, a screen left anywhere but the fleet goes back to the fleet and its
camera stills run again (2026-11-09: the home 5-inch sat on a page with its pictures paused and showed no video).

    python3 tests/test_5in_idle_home.py

Reads app/dashboard.html as text and runs l5HomeCheck under node with stubs (skipped without node). Opens no sockets.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()


def js_function(name):
    m = re.search(r"function " + name + r"\(", HTML)
    assert m, name
    i = HTML.index("{", m.end())
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[m.start():j + 1]
    raise AssertionError(name)


HARNESS = r"""
const S = %(state)s;
const L5 = S.l5, CAM_IDLE_MS = 600000;
const CAM = {last: Date.now() - S.idle_ms, full: S.full, paused: S.paused};
const PT = {stat: {textContent: 'x'}};
const FC = {items: {a: {stat: {textContent: 'Paused'}}}};
const on = new Set(S.on);
const calls = [];
const $ = id => ({classList: {contains: c => c === 'on' && on.has(id)}});
const visible = () => S.visible;
function camCloseFull(){ calls.push('camCloseFull'); CAM.full = false; }
function pmoreClose(){ calls.push('pmoreClose'); }
function moreClose(){ calls.push('moreClose'); }
function filesClose(){ calls.push('filesClose'); }
function camSync(){ calls.push('camSync'); }
function fcSync(){ calls.push('fcSync'); }
function show(id){ calls.push('show:' + id); }
%(fn)s
const r = l5HomeCheck();
console.log(JSON.stringify({r, calls, paused: CAM.paused, fc: FC.items.a.stat.textContent}));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class IdleHome(unittest.TestCase):
    def run_case(self, **kw):
        state = dict(l5=True, idle_ms=11 * 60 * 1000, full=False, paused=False, visible=True, on=["printer"])
        state.update(kw)
        src = HARNESS % {"state": json.dumps(state), "fn": js_function("l5HomeCheck")}
        out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_idle_printer_screen_goes_home(self):
        r = self.run_case(paused=True)
        self.assertTrue(r["r"])
        self.assertIn("show:home", r["calls"])
        self.assertFalse(r["paused"])
        self.assertEqual(r["fc"], "")

    def test_recent_touch_stays(self):
        r = self.run_case(idle_ms=60 * 1000)
        self.assertFalse(r["r"])
        self.assertEqual(r["calls"], [])

    def test_not_5inch_stays(self):
        self.assertFalse(self.run_case(l5=False)["r"])

    def test_hidden_tab_stays(self):
        self.assertFalse(self.run_case(visible=False)["r"])

    def test_popup_open_stays(self):
        for pop in ("modal", "firstrun"):
            r = self.run_case(on=["printer", pop])
            self.assertFalse(r["r"], pop)
            self.assertEqual(r["calls"], [], pop)

    def test_fleet_running_is_left_alone(self):
        r = self.run_case(on=["home"])
        self.assertFalse(r["r"])
        self.assertEqual(r["calls"], [])

    def test_fleet_with_enlarged_view_or_paused_restarts_stills(self):
        for kw in ({"full": True}, {"paused": True}):
            r = self.run_case(on=["home"], **kw)
            self.assertTrue(r["r"], kw)
            self.assertNotIn("show:home", r["calls"], kw)
            self.assertIn("fcSync", r["calls"], kw)

    def test_wired_on_a_timer(self):
        self.assertIn("setInterval(l5HomeCheck,", HTML)


if __name__ == "__main__":
    unittest.main()
