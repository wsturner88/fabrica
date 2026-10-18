"""Tests for app/settings_store.py - schema, validation, secrets, env defaults, export/import.

    python3 tests/test_settings.py

Stdlib only (the FleetStore integration test also needs paho-mqtt and is skipped without it).
Everything happens in temp directories; nothing touches a network.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import settings_store as ss  # noqa: E402

def _load(path):
    with open(path) as fh:
        return json.load(fh)


TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghij"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def store(self, env=None, **kw):
        return ss.SettingsStore(self.dir, env={} if env is None else env, **kw)


class SchemaTests(Base):
    def test_schema_is_complete_and_json_safe(self):
        sch = self.store().schema()
        json.dumps(sch)
        ids = [s["id"] for s in sch["sections"]]
        for need in ("general", "notifications", "telegram", "ntfy", "email", "discord", "webhook",
                     "printers", "backups", "camera", "agents"):
            self.assertIn(need, ids)
        for sec in sch["sections"]:
            self.assertTrue(sec["label"] and sec["description"])
            for f in sec["fields"]:
                self.assertEqual(f["key"], f"{sec['id']}.{f['name']}")
                for k in ("label", "type", "help", "default", "secret"):
                    self.assertIn(k, f, f["key"])
                self.assertTrue(f["label"] and f["help"], f["key"])
                if f["type"] in ("enum", "multienum"):
                    self.assertTrue(f["options"], f["key"])
        for ch in ("telegram", "ntfy", "email", "discord", "webhook"):
            self.assertEqual(ss.SECTIONS[ch]["test"], ch)

    def test_telegram_help_walks_a_newcomer_through(self):
        h = ss.SECTIONS["telegram"]["help"]
        self.assertIn("@BotFather", h)
        self.assertIn("chat", h.lower())
        self.assertIn("getUpdates", h)

    def test_secret_fields_flagged(self):
        flagged = {k for k, f in ss.FIELDS.items() if f["secret"]}
        self.assertEqual(flagged, {"telegram.bot_token", "ntfy.token", "email.password",
                                   "discord.webhook_url", "webhook.bearer",
                                   "security.dashboard_password", "detection.token"})

    def test_defaults(self):
        s = self.store()
        self.assertEqual(s.get("general.hub_name"), "Fabrica")
        self.assertEqual(s.get("general.timezone"), "UTC")
        self.assertEqual(s.get("general.temp_unit"), "C")
        self.assertTrue(s.get("notifications.enabled"))
        self.assertFalse(s.get("notifications.quiet_enabled"))
        self.assertEqual(s.get("notifications.quiet_breakthrough"),
                         ["print_failed", "backup_failed", "spaghetti_suspected", "preheat_auto_off_failed"])
        self.assertFalse(s.get("notifications.print_started"))
        self.assertTrue(s.get("notifications.print_finished"))
        self.assertTrue(s.get("notifications.print_failed"))
        self.assertEqual(s.get("notifications.muted_printers"), [])
        self.assertEqual(s.get("ntfy.server_url"), "https://ntfy.sh")
        self.assertEqual([s.get("printers.preheat_bed_" + m) for m in ("pla", "petg", "abs")], [60, 80, 100])      # pre-heat is bed only
        self.assertEqual(s.get("printers.preheat_auto_off_min"), 15)
        self.assertTrue(s.get("printers.require_bed_clear"))
        self.assertEqual(s.get("backups.keep_per_printer"), 5)
        self.assertFalse(s.get("backups.schedule_enabled"))
        self.assertEqual(s.get("camera.thumb_refresh_s"), 5)
        self.assertEqual(s.get("camera.live_max_min"), 30)
        for sec in ("telegram", "ntfy", "email", "discord", "webhook"):
            self.assertFalse(s.get(f"{sec}.enabled"))

    def test_every_event_has_a_toggle_and_breakthrough_option(self):
        s = self.store()
        opts = {o["value"] for o in ss.FIELDS["notifications.quiet_breakthrough"]["options"]}
        for kind in ss.EVENT_KINDS:
            self.assertIn(f"notifications.{kind}", ss.FIELDS)
            self.assertIn(kind, opts)
        # the defaults are mutable objects: a caller changing one must not poison the schema
        v = s.get("backups.keep_per_printer"), s.get("notifications.muted_printers")
        s.get("notifications.muted_printers").append("x")
        self.assertEqual(s.get("notifications.muted_printers"), [])

    def test_agents_block_is_read_only_info_with_runtime_values(self):
        s = self.store()
        s.set_runtime_info(tool_count=42)
        self.assertEqual(s.get("agents.tool_count"), 42)
        self.assertEqual(s.get("agents.mcp_path"), "/mcp")
        with self.assertRaises(ss.SettingsError):
            s.update({"agents.tool_count": 3})
        blob = json.dumps(s.describe())
        self.assertNotIn("password", json.dumps(s.get_all()).lower().replace("email.password", "")
                         .replace("security.dashboard_password", ""))
        self.assertIn('"/mcp"', blob)


class ValidationTests(Base):
    def bad(self, key, value, fragment=""):
        s = self.store()
        with self.assertRaises(ss.SettingsError) as cm:
            s.update({key: value})
        self.assertIn(key, cm.exception.errors)
        self.assertIn(fragment, cm.exception.errors[key])
        return s

    def test_bad_values_rejected_with_reasons(self):
        self.bad("notifications.quiet_start", "25:00", "24-hour")
        self.bad("notifications.quiet_end", "7", "24-hour")
        self.bad("general.temp_unit", "K", "one of")
        self.bad("general.timezone", "Mars/Olympus", "time zone")
        self.bad("camera.thumb_refresh_s", 0, "at least 1")
        self.bad("camera.thumb_refresh_s", 99999, "at most")
        self.bad("camera.thumb_refresh_s", "abc", "number")
        self.bad("camera.thumb_refresh_s", 2.5, "whole")
        self.bad("camera.thumb_refresh_s", True, "number")
        self.bad("backups.keep_per_printer", 0, "at least")
        self.bad("ntfy.server_url", "ftp://x", "http")
        self.bad("webhook.url", "not a url", "http")
        self.bad("email.port", 70000, "at most")
        self.bad("email.tls", "magic", "one of")
        self.bad("email.to_addr", "nobody", "email address")
        self.bad("notifications.enabled", "maybe", "true or false")
        self.bad("notifications.quiet_breakthrough", ["print_exploded"], "unknown")
        self.bad("general.hub_name", "x" * 61, "at most")
        self.bad("general.hub_name", "line\nbreak", "line breaks")
        self.bad("nope.nothing", 1, "not a setting")

    def test_good_values_normalised(self):
        s = self.store()
        s.update({"notifications.quiet_start": "7:05", "general.temp_unit": "f", "camera.live_max_min": "45",
                  "email.to_addr": "a@example.com; Bob <b@example.com>", "notifications.enabled": "off",
                  "ntfy.server_url": "https://ntfy.example.com/", "general.timezone": "America/Chicago",
                  "notifications.muted_printers": "P1S, p1s, Garage"})
        self.assertEqual(s.get("notifications.quiet_start"), "07:05")
        self.assertEqual(s.get("general.temp_unit"), "F")
        self.assertEqual(s.get("camera.live_max_min"), 45)
        self.assertEqual(s.get("email.to_addr"), "a@example.com, Bob <b@example.com>")
        self.assertIs(s.get("notifications.enabled"), False)
        self.assertEqual(s.get("ntfy.server_url"), "https://ntfy.example.com")
        self.assertEqual(s.get("general.timezone"), "America/Chicago")
        self.assertEqual(s.get("notifications.muted_printers"), ["P1S", "Garage"])

    def test_all_or_nothing(self):
        s = self.store()
        with self.assertRaises(ss.SettingsError) as cm:
            s.update({"general.hub_name": "Shop", "camera.thumb_refresh_s": 0})
        self.assertEqual(list(cm.exception.errors), ["camera.thumb_refresh_s"])
        self.assertEqual(s.get("general.hub_name"), "Fabrica")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "settings.json")))

    def test_preheat_is_one_bed_temperature_per_material(self):
        s = self.store()
        self.assertNotIn("printers.preheat_presets", ss.FIELDS)          # one set of numbers: nothing else carries a pre-heat nozzle value
        self.assertEqual(sorted(k for k in ss.FIELDS if k.startswith("printers.preheat_bed_")),
                         ["printers.preheat_bed_abs", "printers.preheat_bed_petg", "printers.preheat_bed_pla"])
        s.update({"printers.preheat_bed_pla": 55, "printers.preheat_bed_abs": 90})
        self.assertEqual([s.get("printers.preheat_bed_" + m) for m in ("pla", "petg", "abs")], [55, 80, 90])
        self.bad("printers.preheat_bed_pla", 121, "120")
        self.bad("printers.preheat_bed_petg", -1, "0")
        self.bad("printers.preheat_bed_abs", "hot", "number")

    def test_readonly_and_disabled(self):
        s = self.store()
        self.assertEqual(s.update({"printers.require_bed_clear": True})["changed"], [])
        with self.assertRaises(ss.SettingsError) as cm:
            s.update({"printers.require_bed_clear": False})
        self.assertIn("require_bed_clear", cm.exception.errors and list(cm.exception.errors)[0])
        self.assertTrue(s.get("printers.require_bed_clear"))
        with self.assertRaises(ss.SettingsError) as cm:
            s.update({"backups.schedule_enabled": True})
        self.assertIn("not available yet", cm.exception.errors["backups.schedule_enabled"])

    def test_nested_form_accepted(self):
        s = self.store()
        s.update({"camera": {"thumb_refresh_s": 10}, "ntfy": {"topic": "abc"}})
        self.assertEqual(s.get("camera.thumb_refresh_s"), 10)
        self.assertEqual(s.get("ntfy.topic"), "abc")


class SecretTests(Base):
    def set_token(self, s):
        return s.update({"telegram.bot_token": TOKEN, "telegram.chat_id": "42", "telegram.enabled": True})

    def test_secret_never_in_any_public_view(self):
        s = self.store()
        self.set_token(s)
        everything = json.dumps([s.values(), s.get_all(), s.describe(), s.schema(), s.export_settings(),
                                 s.update({"general.hub_name": "x"})])
        self.assertNotIn(TOKEN, everything)
        self.assertNotIn("AAFakeToken", everything)
        self.assertEqual(s.values()["telegram.bot_token"], "set")
        self.assertEqual(s.values()["ntfy.token"], "not set")
        with open(os.path.join(self.dir, "settings.json")) as fh:
            self.assertNotIn("AAFakeToken", fh.read())
        with self.assertRaises(KeyError):
            s.get("telegram.bot_token")
        self.assertTrue(s.is_secret_set("telegram.bot_token"))

    def test_internal_getters_do_return_it(self):
        s = self.store()
        self.set_token(s)
        self.assertEqual(s.get_secret("telegram.bot_token"), TOKEN)
        cfg = s.channel_config("telegram")
        self.assertEqual(cfg, {"enabled": True, "bot_token": TOKEN, "chat_id": "42"})

    def test_secrets_file_mode_namespace_and_other_entries_kept(self):
        path = os.path.join(self.dir, "secrets.json")
        with open(path, "w") as fh:
            json.dump({"P1S": {"access_code": "12345678"}}, fh)
        s = self.store()
        self.set_token(s)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = _load(path)
        self.assertEqual(doc["P1S"], {"access_code": "12345678"})
        self.assertEqual(doc["_settings"], {"telegram.bot_token": TOKEN})
        s.update({}, clear=["telegram.bot_token"])
        doc = _load(path)
        self.assertNotIn("_settings", doc)
        self.assertEqual(doc["P1S"], {"access_code": "12345678"})
        self.assertFalse(s.is_secret_set("telegram.bot_token"))

    def test_blank_and_placeholder_keep_the_secret(self):
        s = self.store()
        self.set_token(s)
        for v in ("", "   ", None, "set", "not set", "********"):
            res = s.update({"telegram.bot_token": v, "telegram.chat_id": "43"})
            self.assertEqual(res["secrets_set"], [])
            self.assertEqual(s.get_secret("telegram.bot_token"), TOKEN)
        res = s.update({"telegram.bot_token": "999999:NewTokenValue_abcdefghijklmnop"})
        self.assertEqual(res["secrets_set"], ["telegram.bot_token"])

    def test_clear_only_applies_to_secrets(self):
        s = self.store()
        with self.assertRaises(ss.SettingsError):
            s.update({}, clear=["general.hub_name"])

    def test_survives_reload(self):
        s = self.store()
        self.set_token(s)
        s2 = self.store()
        self.assertEqual(s2.get_secret("telegram.bot_token"), TOKEN)
        self.assertEqual(s2.get("telegram.chat_id"), "42")

    def test_shares_secrets_with_fleet_store(self):
        try:
            import fleet_store
        except ImportError:
            self.skipTest("fleet_store needs paho-mqtt")
        fs = fleet_store.FleetStore(self.dir)
        s = ss.SettingsStore(self.dir, env={}, fleet_store=fs)
        fs.add({"name": "A1", "type": "octoprint", "url": "http://127.0.0.1:1"}, {"api_key": "KEYONE"})
        self.set_token(s)
        # a later printer save must not wipe the settings secrets, and vice versa
        fs.add({"name": "A2", "type": "octoprint", "url": "http://127.0.0.1:2"}, {"api_key": "KEYTWO"})
        s.update({"ntfy.token": "tk_abcdefghijklmnop"})
        fs2 = fleet_store.FleetStore(self.dir)
        self.assertEqual(fs2.secret("A1", "api_key"), "KEYONE")
        self.assertEqual(fs2.secret("A2", "api_key"), "KEYTWO")
        s3 = ss.SettingsStore(self.dir, env={})
        self.assertEqual(s3.get_secret("telegram.bot_token"), TOKEN)
        self.assertEqual(s3.get_secret("ntfy.token"), "tk_abcdefghijklmnop")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "secrets.json")).st_mode), 0o600)


class EnvAndPersistenceTests(Base):
    def test_env_defaults_and_saved_wins(self):
        env = {"BACKUP_KEEP": "9", "PREHEAT_TIMEOUT_MIN": "30", "TZ": "Europe/Berlin"}
        s = self.store(env)
        self.assertEqual(s.get("backups.keep_per_printer"), 9)
        self.assertEqual(s.get("printers.preheat_auto_off_min"), 30)
        self.assertEqual(s.get("general.timezone"), "Europe/Berlin")
        src = s.get_all()["sources"]
        self.assertEqual(src["backups.keep_per_printer"], "env")
        self.assertEqual(src["camera.live_max_min"], "default")
        s.update({"backups.keep_per_printer": 3})
        self.assertEqual(s.get("backups.keep_per_printer"), 3)
        self.assertEqual(s.get_all()["sources"]["backups.keep_per_printer"], "saved")
        s.reset(["backups.keep_per_printer"])
        self.assertEqual(s.get("backups.keep_per_printer"), 9)

    def test_invalid_env_value_ignored(self):
        s = self.store({"BACKUP_KEEP": "lots", "TZ": "Not/AZone"})
        self.assertEqual(s.get("backups.keep_per_printer"), 5)
        self.assertEqual(s.get("general.timezone"), "UTC")

    def test_persists_and_reloads(self):
        s = self.store()
        s.update({"general.hub_name": "Shop Hub", "notifications.print_started": True})
        s2 = self.store()
        self.assertEqual(s2.get("general.hub_name"), "Shop Hub")
        self.assertTrue(s2.get("notifications.print_started"))
        leftovers = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_corrupt_file_moved_aside(self):
        with open(os.path.join(self.dir, "settings.json"), "w") as fh:
            fh.write("{ not json")
        s = self.store()
        self.assertEqual(s.get("general.hub_name"), "Fabrica")
        self.assertTrue([f for f in os.listdir(self.dir) if f.startswith("settings.json.corrupt-")])

    def test_bad_saved_value_falls_back_to_default(self):
        with open(os.path.join(self.dir, "settings.json"), "w") as fh:
            json.dump({"values": {"camera.thumb_refresh_s": 0, "general.hub_name": "Ok",
                                  "telegram.bot_token": "smuggled", "bogus.key": 1}}, fh)
        s = self.store()
        self.assertEqual(s.get("camera.thumb_refresh_s"), 5)
        self.assertEqual(s.get("general.hub_name"), "Ok")
        self.assertFalse(s.is_secret_set("telegram.bot_token"))

    def test_reset_and_listener(self):
        s = self.store()
        seen = []
        s.subscribe(seen.append)
        s.update({"general.hub_name": "A"})
        s.update({"telegram.bot_token": TOKEN})
        self.assertEqual(seen[0], ["general.hub_name"])
        self.assertEqual(seen[1], ["telegram.bot_token"])
        self.assertEqual(s.reset(), ["general.hub_name"])
        self.assertEqual(s.get("general.hub_name"), "Fabrica")
        s.subscribe(lambda k: 1 / 0)       # a broken listener must not break a save
        s.update({"general.hub_name": "B"})

    def test_failed_write_rolls_back(self):
        s = self.store()
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        if os.access(self.dir, os.W_OK):
            self.skipTest("cannot make the directory read-only here (running as root?)")
        with self.assertRaises(OSError):
            s.update({"general.hub_name": "Nope"})
        self.assertEqual(s.get("general.hub_name"), "Fabrica")


class ExportImportTests(Base):
    def test_export_has_no_secrets_and_round_trips(self):
        a = self.store()
        a.update({"general.hub_name": "Shop", "notifications.quiet_enabled": True,
                  "telegram.bot_token": TOKEN, "telegram.chat_id": "7", "webhook.bearer": "bearer-secret-value",
                  "discord.webhook_url": "https://discord.com/api/webhooks/1234567890/secretpart_abc"})
        doc = a.export_settings()
        text = json.dumps(doc)
        for secret in (TOKEN, "bearer-secret-value", "secretpart_abc"):
            self.assertNotIn(secret, text)
        for k in doc["values"]:
            self.assertFalse(ss.FIELDS[k]["secret"])
        self.assertEqual(doc["values"]["telegram.chat_id"], "7")

        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        b = ss.SettingsStore(other, env={})
        res = b.import_settings(text)
        self.assertEqual(b.get("general.hub_name"), "Shop")
        self.assertTrue(b.get("notifications.quiet_enabled"))
        self.assertFalse(b.is_secret_set("telegram.bot_token"))
        self.assertIn("telegram.chat_id", res["applied"])

    def test_import_skips_secrets_and_unknowns_rejects_bad_values(self):
        s = self.store()
        res = s.import_settings({"values": {"telegram.bot_token": "stolen", "weird.key": 1,
                                            "general.hub_name": "Imported"}})
        self.assertEqual(res["applied"], ["general.hub_name"])
        self.assertEqual(res["skipped"]["telegram.bot_token"], "secrets are never imported")
        self.assertEqual(res["skipped"]["weird.key"], "unknown setting")
        self.assertFalse(s.is_secret_set("telegram.bot_token"))
        with self.assertRaises(ss.SettingsError):
            s.import_settings({"values": {"camera.live_max_min": 0, "general.hub_name": "Half"}})
        self.assertEqual(s.get("general.hub_name"), "Imported")
        with self.assertRaises(ss.SettingsError):
            s.import_settings("not json")
        with self.assertRaises(ss.SettingsError):
            s.import_settings({"format": "something-else", "values": {}})

    def test_dry_run_changes_nothing(self):
        s = self.store()
        res = s.import_settings({"values": {"general.hub_name": "Dry"}}, dry_run=True)
        self.assertEqual(res["applied"], ["general.hub_name"])
        self.assertEqual(s.get("general.hub_name"), "Fabrica")


class CostsMachineSettingTests(Base):
    """costs.include_machine_costs: off by default (cost = filament only); on adds power + machine wear."""

    def test_declared_in_the_costs_section(self):
        f = ss.FIELDS["costs.include_machine_costs"]
        self.assertEqual((f["type"], f["default"], f["secret"]), ("bool", False, False))
        self.assertIn("print time", f["label"].lower())
        self.assertIn("filament", f["help"].lower())
        sec = [x for x in self.store().schema()["sections"] if x["id"] == "costs"][0]
        self.assertIn("costs.include_machine_costs", [x["key"] for x in sec["fields"]])
        for rate in ("kwh_price", "wear_per_hour", "printer_watts", "default_watts"):      # the rate fields all remain
            self.assertIn("costs." + rate, ss.FIELDS)

    def test_default_off_validation_and_round_trip(self):
        s = self.store()
        self.assertIs(s.get("costs.include_machine_costs"), False)
        self.bad_value(s, "maybe")
        self.bad_value(s, 5)
        s.update({"costs.include_machine_costs": "on"})
        self.assertIs(s.get("costs.include_machine_costs"), True)
        self.assertIs(self.store().get("costs.include_machine_costs"), True)               # persisted
        doc = s.export_settings()
        self.assertIs(doc["values"]["costs.include_machine_costs"], True)
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        b = ss.SettingsStore(other, env={})
        b.import_settings(doc)
        self.assertIs(b.get("costs.include_machine_costs"), True)
        s.reset(["costs.include_machine_costs"])
        self.assertIs(s.get("costs.include_machine_costs"), False)

    def bad_value(self, s, value):
        with self.assertRaises(ss.SettingsError) as cm:
            s.update({"costs.include_machine_costs": value})
        self.assertIn("true or false", cm.exception.errors["costs.include_machine_costs"])

    def test_feeds_costs_rates(self):
        import costs
        s = self.store()
        self.assertFalse(costs.Rates.from_settings(s.get).include_machine)
        s.update({"costs.include_machine_costs": True})
        self.assertTrue(costs.Rates.from_settings(s.get).include_machine)


class FormatTests(unittest.TestCase):
    def test_temp(self):
        self.assertEqual(ss.format_temp(215, "C"), "215 °C")
        self.assertEqual(ss.format_temp(100, "F"), "212 °F")
        self.assertEqual(ss.format_temp(None), "-")

    def test_datetime(self):
        self.assertEqual(ss.format_datetime(0, "UTC", "iso"), "1970-01-01 00:00")
        self.assertEqual(ss.format_datetime(13 * 3600, "UTC", "us"), "01/01/1970 01:00 PM")
        self.assertEqual(ss.format_datetime(13 * 3600, "UTC", "eu"), "01/01/1970 13:00")


if __name__ == "__main__":
    unittest.main(verbosity=1)
