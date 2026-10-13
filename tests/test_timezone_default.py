"""A fresh install shows the viewer's own time zone and clock style; explicit settings are still honoured.

    python3 tests/test_timezone_default.py

Stdlib only. Reads app/dashboard.html as text; settings work happens in temp directories. No network.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import settings_store as ss  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def store(self, env=None, host_tz=None):
        return ss.SettingsStore(self.dir, env={} if env is None else env, host_tz=host_tz)


class ServerZoneTests(Base):
    def test_nothing_set_and_no_host_zone_is_utc(self):
        s = self.store()
        self.assertEqual(s.get("general.timezone"), "UTC")
        self.assertEqual(s.get_all()["sources"]["general.timezone"], "default")

    def test_host_zone_is_used_when_nothing_is_set(self):
        s = self.store(host_tz="Europe/Berlin")
        self.assertEqual(s.get("general.timezone"), "Europe/Berlin")
        self.assertEqual(s.get_all()["sources"]["general.timezone"], "default")     # still "not chosen by the owner"
        tz = [f for sec in s.describe()["sections"] for f in sec["fields"] if f["key"] == "general.timezone"][0]
        self.assertEqual((tz["value"], tz["source"]), ("Europe/Berlin", "default"))

    def test_env_beats_host_and_saved_beats_env(self):
        s = self.store({"TZ": "Asia/Tokyo"}, host_tz="Europe/Berlin")
        self.assertEqual(s.get("general.timezone"), "Asia/Tokyo")
        self.assertEqual(s.get_all()["sources"]["general.timezone"], "env")
        s.update({"general.timezone": "America/Chicago"})
        self.assertEqual(s.get("general.timezone"), "America/Chicago")
        self.assertEqual(s.get_all()["sources"]["general.timezone"], "saved")

    def test_bad_env_falls_through_to_host_then_utc(self):
        self.assertEqual(self.store({"TZ": "Not/AZone"}, host_tz="Europe/Paris").get("general.timezone"), "Europe/Paris")
        self.assertEqual(self.store({"TZ": "Not/AZone"}).get("general.timezone"), "UTC")

    def test_clearing_the_saved_zone_goes_back_to_the_fallback(self):
        s = self.store(host_tz="Europe/Berlin")
        s.update({"general.timezone": "America/Chicago"})
        s.reset(["general.timezone"])
        self.assertEqual(s.get("general.timezone"), "Europe/Berlin")

    def test_format_is_not_touched_and_stays_iso_by_default(self):
        s = self.store(host_tz="Europe/Berlin")
        self.assertEqual(s.get("general.datetime_format"), "iso")
        self.assertEqual(s.get_all()["sources"]["general.datetime_format"], "default")


class HostZoneDetectionTests(unittest.TestCase):
    def _detect(self, tzfile=None, link=None):
        def fake_open(path, *a, **k):
            if path == "/etc/timezone" and tzfile is not None:
                return mock.mock_open(read_data=tzfile)()
            raise FileNotFoundError(path)
        with mock.patch("builtins.open", fake_open), \
             mock.patch.object(ss.os.path, "realpath", return_value=link or "/etc/localtime"):
            return ss.host_timezone()

    def test_reads_etc_timezone(self):
        self.assertEqual(self._detect(tzfile="Europe/Paris\n"), "Europe/Paris")

    def test_reads_the_localtime_link(self):
        self.assertEqual(self._detect(link="/usr/share/zoneinfo/America/Chicago"), "America/Chicago")
        self.assertEqual(self._detect(link="/var/db/timezone/zoneinfo/Asia/Tokyo"), "Asia/Tokyo")

    def test_utc_or_nothing_means_no_host_zone(self):
        self.assertEqual(self._detect(tzfile="Etc/UTC\n"), "")
        self.assertEqual(self._detect(link="/usr/share/zoneinfo/Etc/UTC"), "")
        self.assertEqual(self._detect(), "")

    def test_garbage_is_ignored(self):
        self.assertEqual(self._detect(tzfile="Not/AZone\n"), "")

    def test_real_machine_never_raises(self):
        z = ss.host_timezone()
        self.assertIsInstance(z, str)

    def test_a_test_with_its_own_env_never_looks_at_the_host(self):
        with mock.patch.object(ss, "host_timezone", side_effect=AssertionError("must not be called")):
            d = tempfile.mkdtemp()
            try:
                self.assertEqual(ss.SettingsStore(d, env={}).get("general.timezone"), "UTC")
            finally:
                shutil.rmtree(d, True)


class DashboardTests(unittest.TestCase):
    def test_times_follow_the_browser_until_the_owner_chooses(self):
        self.assertIn("tzSet:false", HTML)
        self.assertIn("fmtSet:false", HTML)
        self.assertIn("if(!PREFS.tzSet || !PREFS.timezone) return {};", HTML)            # no timeZone passed = browser zone
        self.assertIn("const hour12 = () => PREFS.fmtSet ? PREFS.datetime_format === 'us' : undefined;", HTML)
        self.assertIn("const loc = !PREFS.fmtSet ? [] :", HTML)                           # browser locale for the date part
        self.assertIn("src['general.timezone'] !== 'default'", HTML)
        self.assertIn("src['general.datetime_format'] !== 'default'", HTML)

    def test_saved_prefs_are_honoured_exactly_as_before(self):
        # the explicit branches are unchanged: 'us' = 12 h + en-US dates, 'eu' = en-GB, otherwise en-CA (ISO-like)
        self.assertIn("PREFS.datetime_format === 'us' ? 'en-US' : PREFS.datetime_format === 'eu' ? 'en-GB' : 'en-CA'", HTML)
        self.assertIn("return {timeZone:PREFS.timezone}", HTML)

    def test_no_other_place_forces_a_zone_or_clock(self):
        import re
        for m in re.finditer(r"hour12\s*:\s*(true|false)", HTML):
            self.fail("hour12 must come from hour12(), not a constant: " + HTML[m.start() - 40:m.end()])

    def test_one_time_note_in_settings_general(self):
        self.assertIn("Times on this page use your browser's time zone (", HTML)
        self.assertIn("Set the Hub's time zone here so alerts and quiet hours match.", HTML)
        self.assertIn('data-act="stUseTz"', HTML)
        self.assertIn(">Use ${pmEsc(z)}</button>", HTML)
        self.assertIn("stUseTz:   b => stUseTz(b.dataset.tz),", HTML)
        self.assertIn("stTzDismiss: () => stTzDismiss(),", HTML)
        # shown only while the Hub's zone is the default and differs from the browser's, and never once dismissed
        self.assertIn("f.source !== 'default' || !z || z === f.value || stTzNoteHidden()", HTML)
        self.assertIn("localStorage.setItem(TZ_NOTE_KEY, '1')", HTML)
        # the zone is escaped, never spliced into a handler string
        self.assertNotIn("onclick=\"stUseTz(", HTML)

    def test_note_is_only_on_the_general_panel(self):
        i = HTML.index("if(sec.id === 'general') html += stTzNoteHtml();")
        self.assertIn("function stPanel(sec)", HTML[i - 900:i])


if __name__ == "__main__":
    unittest.main()
