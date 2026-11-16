import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import events


class _S:
    def __init__(self, d): self.d = d
    def get(self, k): return self.d.get(k)


def _snap(state, up=True):
    return {"printer": "X", "connected": up, "state": state, "job": "a.3mf" if state == "RUNNING" else "",
            "percent": 10, "remaining_min": 50}


class Split(unittest.TestCase):
    def run_outage(self, state, settings, minutes):
        t = [0.0]; out = []
        w = events.EventWatcher(out.append, settings=settings, clock=lambda: t[0], confirm_s=0)
        for _ in range(3):
            w.update({"X": _snap(state)}); t[0] += 5
        end = t[0] + minutes * 60
        while t[0] <= end:
            w.update({"X": _snap(state, up=False)}); t[0] += 10
        return [e for e in out if e.kind == "printer_offline"]

    def S(self, s=300, idle=60):
        return _S({"notifications.offline_grace_s": s, "notifications.offline_grace_idle_min": idle})

    def test_printing_waits_5_min(self):
        self.assertEqual(self.run_outage("RUNNING", self.S(), 4.5), [])
        self.assertEqual(len(self.run_outage("RUNNING", self.S(), 5.5)), 1)

    def test_idle_router_update_is_quiet(self):
        self.assertEqual(self.run_outage("IDLE", self.S(), 30), [])
        self.assertEqual(self.run_outage("FINISH", self.S(), 45), [])
        self.assertEqual(len(self.run_outage("IDLE", self.S(), 61)), 1)

    def test_idle_zero_never_warns(self):
        self.assertEqual(self.run_outage("IDLE", self.S(idle=0), 300), [])
        self.assertEqual(len(self.run_outage("RUNNING", self.S(idle=0), 6)), 1)


if __name__ == "__main__":
    unittest.main()
