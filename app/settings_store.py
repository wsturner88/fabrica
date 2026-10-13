"""Hub settings: a typed, self-describing schema + a small persistent store.

Everything the Settings page (and an agent) can change lives here, in ONE place:

  SCHEMA          sections -> fields (label, type, default, help, options, min/max,
                  secret flag, ...). The dashboard renders the Settings page from
                  schema() generically and agents read the very same description.
  SettingsStore   /data/settings.json (atomic writes) for ordinary values; secrets (bot
                  tokens, SMTP password, webhook URLs...) go into the existing
                  secrets.json under the reserved namespace "_settings" so they get
                  the same 0600 treatment as printer access codes.

Rules (same spirit as fleet_store.py):
  * secrets are write-only: no public getter ever returns one - only "set" / "not set".
    The internal readers (get_secret / channel_config) are for the notification
    senders and nothing else; never route their output into an API response;
  * a saved setting wins over an environment variable, which wins over the built-in
    default (BACKUP_KEEP, PREHEAT_TIMEOUT_MIN, TZ are honoured as defaults);
  * validation is all-or-nothing: one bad field rejects the whole update and the
    error says which field and why, in words an agent can relay to a human;
  * export/import move the ordinary settings between Hubs - never secrets.

Standalone on purpose: stdlib only, no import of the driver modules. When the Hub
runs a FleetStore, pass it as fleet_store= so there is ONE in-memory copy (and one
writer) of secrets.json - otherwise a later printer save would overwrite the
"_settings" namespace with a stale copy.
"""

import copy
import json
import os
import re
import tempfile
import threading
import time

SETTINGS_FILE = "settings.json"
SECRETS_FILE = "secrets.json"
SECRETS_NAMESPACE = "_settings"
SCHEMA_VERSION = 1
SECRET_PLACEHOLDERS = ("set", "not set", "********", "••••••••")   # echoed back by a UI: ignore


# ---------------------------------------------------------------- errors

class SettingsError(Exception):
    """A settings request that cannot be honoured. `errors` maps field key -> message."""
    status = 400

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or {}

    def payload(self) -> dict:
        return {"error": str(self), "errors": self.errors}


# ---------------------------------------------------------------- events (shared vocabulary)

# kind -> (label, level, default on, default breaks through quiet hours)
# The settings toggle for an event is "notifications.<kind>". events.py / notify.py
# use the same names, so this table is the single source of truth.
EVENT_KINDS = {
    "print_started":      ("Print started",                          "info",    False, False),
    "print_finished":     ("Print finished",                         "info",    True,  False),
    "print_failed":       ("Print failed or cancelled",              "failure", True,  True),
    "print_paused":       ("Print paused (filament runout, error)",  "warning", True,  False),
    "printer_offline":    ("Printer went offline",                   "warning", True,  False),
    "printer_online":     ("Printer came back online",               "info",    True,  False),
    "backup_done":        ("Backup finished",                        "info",    False, False),
    "backup_failed":      ("Backup failed",                          "failure", True,  True),
    "spaghetti_suspected": ("Possible spaghetti / failed print",     "failure", True,  True),
    "preheat_auto_off":   ("Pre-heat auto-off fired",                "info",    True,  False),
    "preheat_auto_off_failed": ("Heaters could not be switched off", "failure", True, True),
    "detector_down":      ("Failure detector unreachable",           "warning", True,  False),
    "detector_up":        ("Failure detector back",                  "info",    True,  False),
}
EVENT_HELP = {
    "print_started": "A printer goes from idle to printing. Off by default - it is usually noise.",
    "print_finished": "A job completes. The message includes the job name and how long it took.",
    "print_failed": "A job fails, is cancelled or stopped before it finished.",
    "print_paused": "A job pauses - filament runout, a printer error, or someone pressed pause. The printer's error text is included when it gives one.",
    "printer_offline": "A printer stays unreachable for longer than the offline grace period.",
    "printer_online": "A printer you were told was offline is reachable again.",
    "backup_done": "A file backup finished successfully.",
    "backup_failed": "A file backup failed.",
    "spaghetti_suspected": "The failure detector (Settings > Detection) thinks a print has gone wrong. Includes a camera picture when enabled. Never sent while Detection is in learn-only mode.",
    "preheat_auto_off": "A pre-heated printer was switched off again because no print was started in time.",
    "preheat_auto_off_failed": "The pre-heat auto-off could not switch a printer's heaters off (printer unreachable, or still busy after many tries). Go and check it.",
    "detector_down": "The failure detector stopped answering, so running prints are NOT being watched for spaghetti.",
    "detector_up": "The failure detector answers again after an outage.",
}


def event_level(kind: str) -> str:
    return EVENT_KINDS.get(kind, ("", "info", True, False))[1]


# ---------------------------------------------------------------- schema

def _opts(*pairs):
    return [{"value": v, "label": l} for v, l in pairs]


def _field(name, label, type_, default, help_="", **kw):
    f = {"name": name, "label": label, "type": type_, "default": default, "help": help_}
    f.update(kw)
    return f


DATETIME_FORMATS = {      # key -> (strftime, example label)
    "iso": "%Y-%m-%d %H:%M",
    "us": "%m/%d/%Y %I:%M %p",
    "eu": "%d/%m/%Y %H:%M",
}

PREHEAT_DEFAULTS = {
    "PLA": {"nozzle_c": 215, "bed_c": 60},
    "PETG": {"nozzle_c": 240, "bed_c": 80},
    "ABS": {"nozzle_c": 255, "bed_c": 100},
    "TPU": {"nozzle_c": 225, "bed_c": 50},
}

_EVENT_FIELDS = [
    _field(kind, label, "bool", on, EVENT_HELP[kind])
    for kind, (label, _lvl, on, _brk) in EVENT_KINDS.items()
]

SCHEMA = [
    {"id": "general", "label": "General", "group": "general",
     "description": "How the Hub presents itself and shows times and temperatures.",
     "fields": [
         _field("hub_name", "Hub name", "string", "Fabrica",
                "Shown in the dashboard header and at the top of every notification.",
                max_len=60),
         _field("timezone", "Time zone", "timezone", "UTC",
                "IANA name, e.g. America/New_York or Europe/Berlin. Used for quiet hours and "
                "displayed times. Defaults to the TZ environment variable, else UTC.",
                env="TZ", placeholder="UTC"),
         _field("temp_unit", "Temperature unit", "enum", "C",
                "Display only - printers and the API always work in degrees Celsius.",
                options=_opts(("C", "Celsius (°C)"), ("F", "Fahrenheit (°F)"))),
         _field("datetime_format", "Date and time format", "enum", "iso",
                "How dates and times are written in the dashboard and in messages.",
                options=_opts(("iso", "2026-03-31 14:05"), ("us", "03/31/2026 2:05 PM"),
                              ("eu", "31/03/2026 14:05"))),
     ]},

    {"id": "notifications", "label": "Notifications", "group": "notifications",
     "description": "What the Hub tells you about, and when. Pick where messages go under Channels.",
     "fields": [
         _field("enabled", "Send notifications", "bool", True,
                "Master switch. Off = nothing is sent to any channel (the test buttons still work)."),
         _field("quiet_enabled", "Quiet hours", "bool", False,
                "During quiet hours only the events ticked below are sent; the rest are dropped."),
         _field("quiet_start", "Quiet hours start", "time", "22:00",
                "24-hour HH:MM in the Hub's time zone. A range that crosses midnight (22:00 to 07:00) is fine."),
         _field("quiet_end", "Quiet hours end", "time", "07:00", "24-hour HH:MM."),
         _field("quiet_breakthrough", "Still send during quiet hours", "multienum",
                [k for k, v in EVENT_KINDS.items() if v[3]],
                "These events ignore quiet hours. Failures break through by default.",
                options=_opts(*[(k, v[0]) for k, v in EVENT_KINDS.items()])),
         _field("include_snapshot", "Attach a camera picture", "bool", True,
                "Add the printer's current camera frame to alerts about a print (when the printer has a camera)."),
         _field("duplicate_window_min", "Suppress repeats for (minutes)", "int", 5,
                "The same event for the same printer (and job) is sent at most once in this window. "
                "0 = never suppress.", min=0, max=240, unit="min"),
         _field("offline_grace_s", "Offline grace period (seconds)", "int", 90,
                "A printer must be unreachable this long before it is reported offline, so a "
                "Wi-Fi blip or reboot does not ping you.", min=10, max=3600, unit="s"),
         _field("muted_printers", "Muted printers", "list_str", [],
                "No notifications at all for these printers (names exactly as in the fleet list).",
                item_source="printers"),
     ] + _EVENT_FIELDS},

    {"id": "telegram", "label": "Telegram", "group": "channels", "test": "telegram",
     "description": "Messages from your own Telegram bot.",
     "help": ("1. In Telegram open a chat with @BotFather, send /newbot, pick a name; BotFather replies "
              "with the bot token. 2. Open your new bot and press Start (or add it to a group). "
              "3. To find the chat id: send the bot any message, then open "
              "https://api.telegram.org/bot<TOKEN>/getUpdates in a browser and read "
              "result[0].message.chat.id (a number; groups are negative). Paste both below, save, press Test."),
     "fields": [
         _field("enabled", "Enabled", "bool", False, "Send notifications to this Telegram chat."),
         _field("bot_token", "Bot token", "secret", "",
                "From @BotFather, looks like 123456789:AA... Stored write-only.",
                secret=True, placeholder="123456789:AAxxxxxxxx"),
         _field("chat_id", "Chat id", "string", "",
                "Your user id or a group id (groups start with -). A public channel can use @name.",
                max_len=64, placeholder="123456789"),
     ]},

    {"id": "ntfy", "label": "ntfy", "group": "channels", "test": "ntfy",
     "description": "Push notifications through ntfy (the public ntfy.sh or your own server).",
     "help": ("Install the ntfy app, subscribe to a topic name nobody will guess "
              "(e.g. fabrica-7f3k2x9q), and type the same topic here. Anyone who knows the topic on "
              "a public server can read it, so use a long random name or an access token."),
     "fields": [
         _field("enabled", "Enabled", "bool", False, "Send notifications to this ntfy topic."),
         _field("server_url", "Server URL", "url", "https://ntfy.sh",
                "https://ntfy.sh, or your own server.", placeholder="https://ntfy.sh"),
         _field("topic", "Topic", "string", "", "The topic to publish to.", max_len=64,
                placeholder="fabrica-7f3k2x9q"),
         _field("token", "Access token (optional)", "secret", "",
                "Only for protected topics. Stored write-only.", secret=True),
     ]},

    {"id": "email", "label": "Email (SMTP)", "group": "channels", "test": "email",
     "description": "Email through any SMTP server.",
     "help": ("Use your mail provider's SMTP settings. Gmail/Outlook/iCloud need an 'app password', "
              "not your normal one. Port 587 + STARTTLS is the usual choice; 465 uses SSL; "
              "port 25 with no encryption is only for a trusted LAN relay."),
     "fields": [
         _field("enabled", "Enabled", "bool", False, "Send notifications by email."),
         _field("host", "SMTP server", "string", "", "e.g. smtp.example.com", max_len=253,
                placeholder="smtp.example.com"),
         _field("port", "Port", "int", 587, "587 (STARTTLS), 465 (SSL) or 25.", min=1, max=65535),
         _field("tls", "Encryption", "enum", "starttls", "How to secure the connection.",
                options=_opts(("starttls", "STARTTLS (port 587)"), ("ssl", "SSL/TLS (port 465)"),
                              ("none", "None (trusted LAN only)"))),
         _field("verify_cert", "Verify server certificate", "bool", True,
                "Turn off only for a LAN mail server with a self-signed certificate."),
         _field("username", "Username", "string", "", "Leave empty if the server needs no login.",
                max_len=200),
         _field("password", "Password", "secret", "", "SMTP password or app password. Stored write-only.",
                secret=True),
         _field("from_addr", "From address", "string", "", "e.g. hub@example.com or "
                "Fabrica <hub@example.com>", max_len=200),
         _field("to_addr", "To address(es)", "emails", "", "One or more addresses, comma separated.",
                max_len=500),
     ]},

    {"id": "discord", "label": "Discord", "group": "channels", "test": "discord",
     "description": "Post to a Discord channel through a webhook.",
     "help": ("In Discord: channel settings > Integrations > Webhooks > New Webhook > Copy Webhook URL. "
              "The URL is a secret - anyone with it can post to the channel."),
     "fields": [
         _field("enabled", "Enabled", "bool", False, "Post notifications to this Discord channel."),
         _field("webhook_url", "Webhook URL", "secret", "",
                "https://discord.com/api/webhooks/... Stored write-only.", secret=True),
     ]},

    {"id": "webhook", "label": "Generic webhook", "group": "channels", "test": "webhook",
     "description": "POST a JSON message to any URL (Home Assistant, n8n, your own service...).",
     "help": ('Request body (Content-Type: application/json): {"source": "print-hub", "version": 1, '
              '"title": "...", "body": "...", "level": "info|warning|failure", "event": "print_finished", '
              '"printer": "P1S", "timestamp": "2026-03-31T14:05:00Z", "image_jpeg_base64": "..." '
              '(only when a picture is attached)}. Any 2xx answer counts as delivered.'),
     "fields": [
         _field("enabled", "Enabled", "bool", False, "POST notifications to this URL."),
         _field("url", "URL", "url", "", "http:// or https:// endpoint.", placeholder="https://example.com/hook"),
         _field("bearer", "Bearer token (optional)", "secret", "",
                "Sent as 'Authorization: Bearer ...'. Stored write-only.", secret=True),
     ]},

    {"id": "printers", "label": "Printers & safety", "group": "hub",
     "description": "Pre-heat and start-print behaviour.",
     "fields": [
         _field("preheat_presets", "Pre-heat presets", "presets", copy.deepcopy(PREHEAT_DEFAULTS),
                "Material name -> nozzle (0-300) and bed (0-120) temperature in °C - the limits the pre-heat tool enforces. Edit, add or remove materials.",
                max_items=12),
         _field("preheat_auto_off_min", "Pre-heat auto-off (minutes)", "int", 15,
                "A pre-heated printer cools down again if no print starts within this time (1-240 min). It cannot be turned off - heaters are never left on indefinitely.",
                min=1, max=240, unit="min", env="PREHEAT_TIMEOUT_MIN"),
         _field("require_bed_clear", "Confirm the bed is clear before every start", "bool", True,
                "Always on. Starting a print on a plate that still holds the last part can wreck the "
                "printer, so the Hub (and any agent) must confirm the bed is clear every time.",
                readonly=True),
     ]},

    {"id": "backups", "label": "Backups", "group": "hub",
     "description": "File backups from the printers.",
     "fields": [
         _field("keep_per_printer", "Backups to keep per printer", "int", 5,
                "Older backups are deleted automatically.", min=1, max=100, env="BACKUP_KEEP"),
         _field("schedule_enabled", "Scheduled backups", "bool", False,
                "Coming soon: back up all printers automatically.", disabled=True),
         _field("schedule_time", "Scheduled backup time", "time", "03:00",
                "Coming soon.", disabled=True),
     ]},

    {"id": "camera", "label": "Camera", "group": "hub",
     "description": "Dashboard camera behaviour.",
     "fields": [
         _field("thumb_refresh_s", "Thumbnail refresh (seconds)", "int", 5,
                "How often the small camera pictures on the fleet page update.",
                min=1, max=120, unit="s"),
         _field("live_max_min", "Live view limit (minutes)", "int", 30,
                "A live camera view stops by itself after this long, to spare the printer.",
                min=1, max=480, unit="min"),
     ]},

    {"id": "costs", "label": "Costs", "group": "hub",
     "description": "What a print costs: filament, electricity and machine wear. Used by the History page and the cost-per-part figures.",
     "help": ("Every print is logged and priced when you look at it, so changing a price re-prices the whole history. "
              "Filament prices and printer power are lists of NAME=value, for example PLA=20 (per kg) or P1S=120 (watts). "
              "A printer's power is matched by its name, then by its model. Money is shown in the currency symbol you type; "
              "nothing is converted. Not counted: purge waste on multi-colour prints, supports the slicer did not report, "
              "packaging, fees and your own time."),
     "fields": [
         _field("currency", "Currency symbol", "string", "$",
                "Shown in front of every amount (for example $, EUR, GBP). No conversion is done.",
                max_len=8, placeholder="$"),
         _field("filament_prices", "Filament price per kg", "list_str",
                ["PLA=20", "PETG=22", "ABS=22", "ASA=25", "TPU=35"],
                "One NAME=price entry per material, price per kilogram, separated by commas. Entries you list replace the built-in ones.",
                pairs=True, placeholder="PLA=20, PETG=22"),
         _field("default_price_per_kg", "Price per kg for any other material", "float", 25.0,
                "Used when a material is not in the list above.", min=0, max=10000, unit="per kg"),
         _field("kwh_price", "Electricity price per kWh", "float", 0.15,
                "What one kilowatt-hour costs you.", min=0, max=100, unit="per kWh"),
         _field("printer_watts", "Printer power while printing", "list_str", ["P1S=120", "A1=90", "MK4=100"],
                "Average watts per printer: a printer's name (or part of its model name) = watts, for example P1S=120.",
                pairs=True, placeholder="P1S=120, A1=90"),
         _field("default_watts", "Power for any other printer", "int", 120,
                "Average watts while printing, for a printer not in the list above.", min=10, max=2000, unit="W"),
         _field("wear_per_hour", "Machine wear per hour", "float", 0.30,
                "Nozzle, belts, bed and depreciation, per hour of printing. 0 to ignore.", min=0, max=1000, unit="per hour"),
         _field("window_n", "Attempts per file in the average", "int", 20,
                "The cost per part of a file is averaged over its latest attempts, so an old bad week stops haunting a file that now prints reliably.",
                min=1, max=500, unit="attempts"),
     ]},

    {"id": "detection", "label": "Detection", "group": "hub",
     "description": "Spaghetti detection: watches the camera of every running print and alerts you when it looks like the print has gone wrong. It never pauses or stops a print on its own.",
     "help": ("Needs the detector add-on (see docs/detection.md). 'This machine' = the detector runs next to the Hub in Docker; "
              "'Another machine' = it runs elsewhere (or in its own compose project) and you give its address. "
              "Leave 'learn-only' on for your first prints: pictures are scored and logged (data/detection/scores.jsonl) "
              "but no alert is sent, so you can see how it behaves on your printers before it can bother you. "
              "The token is the DETECTOR_TOKEN from the detector's .env file."),
     "fields": [
         _field("mode", "Detection", "enum", "off", "Off, or where the detector runs.",
                options=_opts(("off", "Off"), ("local", "This machine"), ("remote", "Another machine"))),
         _field("url", "Detector address", "url", "",
                "Where the detector listens, for example http://192.168.1.20:8090 or http://host.docker.internal:8090. "
                "Leave empty in 'This machine' mode to use the DETECTOR_URL environment variable, else http://detector:8080.",
                placeholder="http://host.docker.internal:8090"),
         _field("token", "Detector token", "secret", "",
                "The detector's DETECTOR_TOKEN. Stored write-only. Leave empty in 'This machine' mode to use the DETECTOR_TOKEN environment variable.",
                secret=True),
         _field("interval_s", "Picture every", "enum", "10", "How often a running print is checked. 10 seconds is the tested setting.",
                options=_opts(("10", "10 seconds"), ("15", "15 seconds"), ("30", "30 seconds"))),
         _field("sensitivity", "Sensitivity", "enum", "medium",
                "Higher catches more but raises more false alarms. Each printer also learns what is normal for it.",
                options=_opts(("low", "Low"), ("medium", "Medium"), ("high", "High"))),
         _field("learn_only", "Learn-only (no alerts)", "bool", True,
                "Score and log pictures but send no alerts. Recommended for the first few prints; turn it off when the scores look sensible."),
         _field("cooldown_min", "Repeat an alert at most every", "int", 10,
                "Within one print, the same or a lower alert level is repeated only after this long. A worse level is sent at once.",
                min=1, max=120, unit="min"),
         _field("excluded_printers", "Do not watch these printers", "list_str", [],
                "For example a printer whose camera is poor. Names exactly as in the fleet list.",
                item_source="printers"),
     ]},

    {"id": "security", "label": "Security", "group": "security",
     "description": "Who may control the Hub: API keys for agents and scripts, and an optional dashboard sign-in (password, trusted devices, view-only password).",
     "help": ("Agents and scripts (MCP clients, the REST API) must present an API key. Create and revoke keys on "
              "the Security page - a key is shown once, when it is created. Managing keys and these settings needs "
              "the ADMIN PASSWORD (set it on the Security page). The dashboard itself is opened from a browser "
              "without a key; unless you turn on dashboard sign-in (Security page) it is protected only by your "
              "network - anyone who can reach it can operate the printers, though without the admin password they "
              "cannot create keys or turn the key check off."),
     "fields": [
         _field("api_auth", "API key check", "enum", "on",
                "On: every request needs a valid key (otherwise it is refused with 401). Warn: requests without "
                "a key still work, but are logged on the Security page so you can see what would stop working - "
                "this is what an upgraded Hub starts in. Off: anyone who can reach the Hub can control the "
                "printers. A new install starts On.",
                options=_opts(("on", "On - a key is required"),
                              ("warn", "Warn only - log requests without a key, but allow them"),
                              ("off", "Off - no key needed (anyone on the network can control the printers)")),
                env="BAMBU_MCP_API_AUTH", portable=False),
         _field("dashboard_password", "Dashboard password (optional)", "secret", "",
                "When set, opening the dashboard asks for this password first (8 or more characters). "
                "Leave it unset and anyone who can reach the Hub's address can open the dashboard and so "
                "control it. Stored write-only as a salted hash; setting, changing or clearing it signs every full-access browser and trusted device out. "
                "Managed on the Security page, together with the sign-in switch, trusted devices and the view-only password.",
                secret=True, hash=True, min_len=8, max_len=200, portable=False),
         _field("active_keys", "API keys in use", "info", 0,
                "Number of active (not revoked) keys. Manage them on the Security page.", readonly=True),
     ]},

    {"id": "agents", "label": "Agents / MCP", "group": "agents",
     "description": "How an AI agent connects to this Hub. Read-only.",
     "fields": [
         _field("mcp_path", "MCP endpoint path", "info", "/mcp",
                "Point an MCP client at http://<this-host>:<port>/mcp (Streamable HTTP).", readonly=True),
         _field("version", "Version", "info", None,
                "The build of this Hub: a short git revision, or \"dev\" for an unmarked build.", readonly=True),
         _field("tool_count", "MCP tools available", "info", None,
                "Everything on the dashboard can also be done by an agent through these tools.",
                readonly=True),
         _field("agent_kit", "Agent kit", "info", None,       # ==== agent kit ====
                "Agents connected to this Hub pick up guides and updates automatically.", readonly=True),
         _field("settings_api", "Settings API", "info", "/api/settings",
                "GET/PUT /api/settings, GET /api/settings/schema; MCP tools get_settings / update_settings.",
                readonly=True),
     ]},
]

CHANNELS = ("telegram", "ntfy", "email", "discord", "webhook")


def _build_index():
    idx = {}
    for sec in SCHEMA:
        for f in sec["fields"]:
            f["key"] = f"{sec['id']}.{f['name']}"
            f["section"] = sec["id"]
            f.setdefault("secret", f["type"] == "secret")
            if f["type"] == "secret":
                f["secret"] = True
            idx[f["key"]] = f
    return idx


FIELDS = _build_index()
SECTIONS = {s["id"]: s for s in SCHEMA}


# ---------------------------------------------------------------- validation

_TRUE = {"true", "1", "yes", "on", "y"}
_FALSE = {"false", "0", "no", "off", "n"}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$")
_PAIR_RE = re.compile(r"^\s*([^=,;\s][^=,;]{0,31}?)\s*=\s*(\d+(?:\.\d+)?)\s*$")
_PRESET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 +._-]{0,15}$")


def _tz_ok(name: str) -> bool:
    """True if `name` is a usable IANA zone. If the system has no tz database at all
    (some slim images) fall back to a format check so a valid name is not refused."""
    try:
        import zoneinfo
        try:
            zoneinfo.ZoneInfo(name)
            return True
        except zoneinfo.ZoneInfoNotFoundError:
            pass
        except Exception:
            return False
        try:
            zoneinfo.ZoneInfo("UTC")
        except Exception:
            return bool(_TZ_RE.match(name))      # no tz database installed at all
        return False
    except ImportError:
        return bool(_TZ_RE.match(name))


def _num(field, v, cast):
    if isinstance(v, bool):
        raise ValueError("must be a number")
    try:
        if isinstance(v, str):
            v = v.strip()
        n = cast(v) if cast is float else int(float(v)) if not isinstance(v, int) else v
        if cast is int and isinstance(v, float) and v != int(v):
            raise ValueError
    except (TypeError, ValueError, OverflowError):        # "inf" / "1e999" turn into an OverflowError for an int
        raise ValueError("must be a whole number" if cast is int else "must be a number") from None
    if isinstance(n, float) and (n != n or n in (float("inf"), float("-inf"))):
        raise ValueError("must be a number")
    lo, hi = field.get("min"), field.get("max")
    if lo is not None and n < lo:
        raise ValueError(f"must be at least {lo}")
    if hi is not None and n > hi:
        raise ValueError(f"must be at most {hi}")
    return n


def _split_list(v):
    if isinstance(v, str):
        return [p.strip() for p in re.split(r"[,;\n]", v) if p.strip()]
    if isinstance(v, (list, tuple)):
        return [str(p).strip() for p in v if str(p).strip()]
    raise ValueError("must be a list")


def coerce(field: dict, v):
    """Validate/normalise one value for `field`; raises ValueError(message)."""
    t = field["type"]
    if t == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        if isinstance(v, str) and v.strip().lower() in _TRUE | _FALSE:
            return v.strip().lower() in _TRUE
        raise ValueError("must be true or false")
    if t in ("string", "secret", "url", "time", "timezone", "emails", "enum"):
        if v is None:
            v = ""
        if not isinstance(v, str):
            raise ValueError("must be text")
        v = v.strip()
        if re.search(r"[\x00-\x08\x0a-\x1f\x7f]", v):
            raise ValueError("must not contain line breaks or control characters")
        limit = field.get("max_len", 500 if t == "secret" else 200)
        if len(v) > limit:
            raise ValueError(f"must be at most {limit} characters")
        if v and len(v) < field.get("min_len", 0):
            raise ValueError(f"must be at least {field['min_len']} characters")
        if t == "url":
            if v and not re.match(r"^https?://[^\s/]+[^\s]*$", v, re.I):
                raise ValueError("must start with http:// or https:// followed by a host name")
            return v.rstrip("/") if v else v
        if t == "time":
            m = _TIME_RE.match(v)
            if not m:
                raise ValueError("must be a 24-hour time like 22:30")
            return f"{int(m.group(1)):02d}:{m.group(2)}"
        if t == "timezone":
            if not v:
                return field["default"]
            if not _tz_ok(v):
                raise ValueError("is not a known time zone name (try e.g. America/Chicago or Europe/Paris)")
            return v
        if t == "emails":
            parts = _split_list(v)
            for p in parts:
                addr = re.sub(r"^.*<(.*)>\s*$", r"\1", p)
                if not _EMAIL_RE.match(addr):
                    raise ValueError(f"'{p}' is not an email address")
            return ", ".join(parts)
        if t == "enum":
            for o in field["options"]:
                if v == o["value"] or v.lower() == str(o["value"]).lower():
                    return o["value"]
            raise ValueError("must be one of: " + ", ".join(str(o["value"]) for o in field["options"]))
        return v
    if t == "int":
        return _num(field, v, int)
    if t == "float":
        return _num(field, v, float)
    if t == "multienum":
        allowed = [o["value"] for o in field["options"]]
        items = _split_list(v)
        bad = [i for i in items if i not in allowed]
        if bad:
            raise ValueError("unknown value(s): " + ", ".join(bad) + "; allowed: " + ", ".join(allowed))
        return [a for a in allowed if a in items]
    if t == "list_str":
        items = _split_list(v)
        if len(items) > 200:
            raise ValueError("at most 200 entries")
        if any(len(i) > 64 for i in items):
            raise ValueError("entries must be at most 64 characters")
        seen, out = set(), []
        for i in items:
            if field.get("pairs"):
                m = _PAIR_RE.match(i)
                if not m:
                    raise ValueError(f"'{i}' must look like NAME=number, for example PLA=20")
                i = f"{m.group(1).strip()}={m.group(2)}"
            if i.lower() not in seen:
                seen.add(i.lower())
                out.append(i)
        return out
    if t == "presets":
        if not isinstance(v, dict):
            raise ValueError("must be an object like {\"PLA\": {\"nozzle_c\": 215, \"bed_c\": 60}}")
        if len(v) > field.get("max_items", 12):
            raise ValueError(f"at most {field.get('max_items', 12)} presets")
        out = {}
        for name, p in v.items():
            name = str(name).strip()
            if not _PRESET_NAME_RE.match(name):
                raise ValueError(f"preset name '{name}' must be 1-16 letters, digits, space, + . _ -")
            if name.lower() in {k.lower() for k in out}:
                raise ValueError(f"duplicate preset name '{name}'")
            if not isinstance(p, dict):
                raise ValueError(f"preset {name} must have nozzle_c and bed_c")
            try:
                nozzle = _num({"min": 0, "max": 300}, p.get("nozzle_c"), int)
                bed = _num({"min": 0, "max": 120}, p.get("bed_c"), int)
            except ValueError as e:
                raise ValueError(f"preset {name}: nozzle_c 0-300 and bed_c 0-120 are required ({e})") from None
            out[name] = {"nozzle_c": nozzle, "bed_c": bed}
        if not out:
            raise ValueError("keep at least one preset")
        return out
    if t == "info":
        raise ValueError("is read-only")
    raise ValueError(f"unsupported field type {t}")


# ---------------------------------------------------------------- file helpers

def _atomic_write(path: str, text: str, mode: int):
    """temp file in the same directory + fsync + rename (same pattern as fleet_store)."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as e:
        bad = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, bad)
        except OSError:
            bad = "(could not move it aside)"
        print(f"[settings] {path} was unreadable ({e}); moved to {bad}, starting empty", flush=True)
        return default


class _FileSecrets:
    """Secrets namespace inside <data_dir>/secrets.json, read-modify-write each time so
    the printer entries another writer put there are preserved."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, SECRETS_FILE)
        self.lock = threading.RLock()

    def get(self) -> dict:
        with self.lock:
            doc = _read_json(self.path, {})
        ns = doc.get(SECRETS_NAMESPACE) if isinstance(doc, dict) else None
        return dict(ns) if isinstance(ns, dict) else {}

    def put(self, ns: dict):
        with self.lock:
            doc = _read_json(self.path, {})
            if not isinstance(doc, dict):
                doc = {}
            if ns:
                doc[SECRETS_NAMESPACE] = ns
            else:
                doc.pop(SECRETS_NAMESPACE, None)
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            _atomic_write(self.path, json.dumps(doc, indent=2, sort_keys=True), 0o600)


class _FleetSecrets:
    """Same namespace, but through a live FleetStore's in-memory secrets dict + lock, so the
    two never overwrite each other (FleetStore._save rewrites the whole file)."""

    def __init__(self, store):
        self.store = store

    def get(self) -> dict:
        with self.store._lock:
            ns = self.store._secrets.get(SECRETS_NAMESPACE)
            return dict(ns) if isinstance(ns, dict) else {}

    def put(self, ns: dict):
        with self.store._lock:
            old = self.store._secrets.get(SECRETS_NAMESPACE)
            if ns:
                self.store._secrets[SECRETS_NAMESPACE] = ns
            else:
                self.store._secrets.pop(SECRETS_NAMESPACE, None)
            try:
                os.makedirs(os.path.dirname(self.store.secrets_path) or ".", exist_ok=True)
                _atomic_write(self.store.secrets_path,
                              json.dumps(self.store._secrets, indent=2, sort_keys=True), 0o600)
            except BaseException:
                if old is None:
                    self.store._secrets.pop(SECRETS_NAMESPACE, None)
                else:
                    self.store._secrets[SECRETS_NAMESPACE] = old
                raise


# ---------------------------------------------------------------- formatting helpers

def tzinfo_for(name: str):
    """A tzinfo for `name`, UTC when unknown / unavailable."""
    import datetime
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name or "UTC")
    except Exception:
        return datetime.timezone.utc


def format_datetime(ts: float, tz_name: str = "UTC", fmt_key: str = "iso") -> str:
    import datetime
    dt = datetime.datetime.fromtimestamp(ts, tzinfo_for(tz_name))
    return dt.strftime(DATETIME_FORMATS.get(fmt_key, DATETIME_FORMATS["iso"]))


def format_temp(celsius, unit: str = "C", digits: int = 0) -> str:
    """'215 °C' / '419 °F'; '-' for a missing reading. Display only."""
    if celsius is None:
        return "-"
    try:
        c = float(celsius)
    except (TypeError, ValueError):
        return "-"
    if str(unit).upper().startswith("F"):
        return f"{c * 9 / 5 + 32:.{digits}f} °F"
    return f"{c:.{digits}f} °C"


# ---------------------------------------------------------------- the store

class SettingsStore:
    """settings.json + the "_settings" secrets namespace. Thread-safe."""

    def __init__(self, data_dir: str, env=None, fleet_store=None):
        self.dir = data_dir
        self.env = os.environ if env is None else env
        self.path = os.path.join(data_dir, SETTINGS_FILE)
        self._lock = threading.RLock()
        self._secrets = _FleetSecrets(fleet_store) if fleet_store is not None else _FileSecrets(data_dir)
        self._listeners = []
        self._runtime = {}
        doc = _read_json(self.path, {})
        saved = doc.get("values") if isinstance(doc, dict) else None
        self._saved = {}
        for k, v in (saved if isinstance(saved, dict) else {}).items():
            f = FIELDS.get(k)
            if not f or f["type"] in ("secret", "info") or f.get("readonly") or f.get("disabled"):
                continue
            try:
                self._saved[k] = coerce(f, v)
            except ValueError as e:
                print(f"[settings] ignoring saved {k}: {e}", flush=True)

    # ---- description ------------------------------------------------------

    def set_runtime_info(self, **info):
        """Read-only facts for the Agents section, e.g. tool_count=42, mcp_path='/mcp'."""
        with self._lock:
            self._runtime.update({k: v for k, v in info.items() if v is not None})

    def subscribe(self, callback):
        """callback(changed_keys: list) after every successful update/reset/import."""
        self._listeners.append(callback)

    def schema(self) -> dict:
        """Static description of every setting (JSON-safe, no values, no secrets)."""
        sections = []
        for sec in SCHEMA:
            s = {k: v for k, v in sec.items() if k != "fields"}
            s["fields"] = [self._describe_field(f) for f in sec["fields"]]
            sections.append(s)
        return {"version": SCHEMA_VERSION, "sections": sections, "channels": list(CHANNELS),
                "events": [{"kind": k, "label": v[0], "level": v[1], "default": v[2],
                            "breaks_quiet_hours": v[3]} for k, v in EVENT_KINDS.items()]}

    def _describe_field(self, f):
        d = {k: copy.deepcopy(v) for k, v in f.items()}
        d["default"] = self.default(f["key"])
        return d

    def default(self, key: str):
        """Environment default if the field has one and it is valid, else the built-in."""
        f = FIELDS[key]
        env_name = f.get("env")
        if env_name and self.env.get(env_name):
            try:
                return coerce(f, self.env.get(env_name))
            except ValueError:
                pass
        return copy.deepcopy(f["default"])

    def _source(self, key):
        if key in self._saved or (FIELDS[key]["secret"] and self._secrets.get().get(key)):
            return "saved"
        f = FIELDS[key]
        env_name = f.get("env")
        if env_name and self.env.get(env_name):
            try:
                coerce(f, self.env.get(env_name))
                return "env"
            except ValueError:
                pass
        return "default"

    # ---- reads ------------------------------------------------------------

    def get(self, key: str):
        """Effective value of a non-secret setting (saved > env > default)."""
        f = FIELDS.get(key)
        if f is None:
            raise KeyError(key)
        if f["secret"]:
            raise KeyError(f"{key} is a secret; use is_secret_set()")
        with self._lock:
            if f["type"] == "info":
                name = f["name"]
                return self._runtime.get(name, f["default"])
            if key in self._saved:
                return copy.deepcopy(self._saved[key])
        return self.default(key)

    def is_secret_set(self, key: str) -> bool:
        if not FIELDS.get(key, {}).get("secret"):
            raise KeyError(key)
        return bool(self._secrets.get().get(key))

    def get_secret(self, key: str) -> str:
        """INTERNAL: the stored secret, for the notification senders. Never put the
        result in an API response, log line or tool result."""
        if not FIELDS.get(key, {}).get("secret"):
            raise KeyError(key)
        return self._secrets.get().get(key, "")

    def channel_config(self, name: str) -> dict:
        """INTERNAL: every field of a channel section by short name, secrets included."""
        sec = SECTIONS.get(name)
        if sec is None or name not in CHANNELS:
            raise KeyError(name)
        cfg = {}
        ns = self._secrets.get()
        for f in sec["fields"]:
            cfg[f["name"]] = ns.get(f["key"], "") if f["secret"] else self.get(f["key"])
        return cfg

    def values(self) -> dict:
        """Flat key -> value for the whole Hub, secrets shown only as 'set' / 'not set'."""
        out = {}
        ns = self._secrets.get()
        for key, f in FIELDS.items():
            if f["secret"]:
                out[key] = "set" if ns.get(key) else "not set"
            else:
                out[key] = self.get(key)
        return out

    def get_all(self) -> dict:
        """What GET /api/settings returns: values + where each came from."""
        with self._lock:
            return {"values": self.values(),
                    "sources": {k: self._source(k) for k in FIELDS if FIELDS[k]["type"] != "info"}}

    def describe(self) -> dict:
        """Schema with the current value (and source) merged into each field - one call
        gives a UI or an agent everything."""
        sch = self.schema()
        vals = self.get_all()
        for sec in sch["sections"]:
            for f in sec["fields"]:
                f["value"] = vals["values"][f["key"]]
                if f["key"] in vals["sources"]:
                    f["source"] = vals["sources"][f["key"]]
        return sch

    # ---- writes -----------------------------------------------------------

    @staticmethod
    def _flatten(changes) -> dict:
        if not isinstance(changes, dict):
            raise SettingsError("Send an object of {setting: value}.")
        flat = {}
        for k, v in changes.items():
            if "." not in str(k) and k in SECTIONS and isinstance(v, dict):
                for kk, vv in v.items():          # {"telegram": {"chat_id": "1"}} form
                    flat[f"{k}.{kk}"] = vv
            else:
                flat[str(k)] = v
        return flat

    def update(self, changes: dict, clear=()) -> dict:
        """Apply {key: value}. All-or-nothing. Secret fields: a non-empty value replaces the
        stored one; empty / missing / the 'set' placeholder keeps it; list a key in `clear`
        to delete it. Returns {"changed": [...], "secrets_set": [...], "secrets_cleared": [...]}."""
        flat = self._flatten(changes)
        clear = [c for c in (clear or [])]
        errors, new_vals, new_secrets, to_clear = {}, {}, {}, []
        for key, v in flat.items():
            f = FIELDS.get(key)
            if f is None:
                errors[key] = "is not a setting (see GET /api/settings/schema)"
                continue
            if f["type"] == "info":
                errors[key] = "is read-only information"
                continue
            if f["secret"]:
                if v is None or (isinstance(v, str) and (not v.strip() or v.strip() in SECRET_PLACEHOLDERS)):
                    continue
                try:
                    new_secrets[key] = coerce(f, v)
                    if f.get("hash"):          # e.g. the dashboard password: only a salted hash is stored
                        from security import hash_password
                        new_secrets[key] = hash_password(new_secrets[key])
                except ValueError as e:
                    errors[key] = str(e)
                continue
            try:
                val = coerce(f, v)
            except ValueError as e:
                errors[key] = str(e)
                continue
            if f.get("readonly"):
                if val != self.get(key):
                    errors[key] = "cannot be changed: " + (f["help"] or "read-only")
                continue
            if f.get("disabled"):
                if val != f["default"]:
                    errors[key] = "is not available yet"
                continue
            new_vals[key] = val
        for key in clear:
            if not FIELDS.get(key, {}).get("secret"):
                errors[key] = "is not a secret setting, so there is nothing to clear"
            else:
                to_clear.append(key)
        if errors:
            first = next(iter(errors.items()))
            raise SettingsError(f"Settings not saved - {first[0]} {first[1]}"
                                + (f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""), errors)
        with self._lock:
            changed = [k for k, v in new_vals.items() if self.get(k) != v or k not in self._saved]
            old_saved = dict(self._saved)
            self._saved.update(new_vals)
            old_ns = self._secrets.get()
            ns = dict(old_ns)
            ns.update(new_secrets)
            for k in to_clear:
                ns.pop(k, None)
            try:
                if new_vals:
                    self._write_values()
                if ns != old_ns:
                    self._secrets.put(ns)
            except BaseException:
                self._saved = old_saved
                raise
        touched = changed + sorted(new_secrets) + to_clear
        if touched:
            self._notify(touched)
        return {"changed": changed, "secrets_set": sorted(new_secrets), "secrets_cleared": to_clear}

    def reset(self, keys=None) -> list:
        """Forget saved values (back to env/default). keys=None resets every ordinary
        setting; secrets are never touched here (use update(clear=...))."""
        with self._lock:
            targets = list(self._saved) if keys is None else [k for k in keys if k in self._saved]
            for k in keys or []:
                if k not in FIELDS:
                    raise SettingsError(f"{k} is not a setting.", {k: "is not a setting"})
            old = dict(self._saved)
            for k in targets:
                self._saved.pop(k, None)
            try:
                self._write_values()
            except BaseException:
                self._saved = old
                raise
        if targets:
            self._notify(targets)
        return targets

    def _write_values(self):
        os.makedirs(self.dir, exist_ok=True)
        doc = {"version": SCHEMA_VERSION, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "values": dict(sorted(self._saved.items()))}
        _atomic_write(self.path, json.dumps(doc, indent=2), 0o600)      # the webhook URL and ntfy topic act like passwords

    def _notify(self, keys):
        for cb in list(self._listeners):
            try:
                cb(list(keys))
            except Exception as e:           # a listener must never break a save
                print(f"[settings] listener failed: {e}", flush=True)

    # ---- export / import --------------------------------------------------

    def export_settings(self) -> dict:
        """Saved ordinary settings as a portable document. Never contains a secret -
        not even a 'set' marker - so it is safe to attach to a bug report or share."""
        with self._lock:
            return {"format": "print-hub-settings", "version": SCHEMA_VERSION,
                    "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "note": "Secrets (tokens, passwords, webhook URLs) are not included; re-enter them on the new Hub.",
                    "values": copy.deepcopy({k: v for k, v in self._saved.items()
                                             if not FIELDS[k]["secret"] and FIELDS[k].get("portable", True)})}

    def import_settings(self, doc, dry_run=False) -> dict:
        """Apply an export. Unknown keys and any secret keys are skipped (and reported),
        invalid values reject the whole import. Returns {"applied","skipped","changed"}."""
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except ValueError as e:
                raise SettingsError(f"That is not valid JSON ({e}).") from None
        if not isinstance(doc, dict):
            raise SettingsError("Expected a settings export (a JSON object).")
        vals = doc.get("values") if isinstance(doc.get("values"), dict) else doc
        if doc.get("format") not in (None, "print-hub-settings"):
            raise SettingsError("This file is not a Fabrica settings export.")
        apply, skipped = {}, {}
        for k, v in self._flatten(vals).items():
            f = FIELDS.get(k)
            if f is None:
                skipped[k] = "unknown setting"
            elif f["secret"]:
                skipped[k] = "secrets are never imported"
            elif not f.get("portable", True):
                skipped[k] = "security settings are never imported"
            elif f["type"] == "info" or f.get("readonly") or f.get("disabled"):
                skipped[k] = "not changeable"
            else:
                apply[k] = v
        if dry_run:
            errors = {}
            for k, v in apply.items():
                try:
                    coerce(FIELDS[k], v)
                except ValueError as e:
                    errors[k] = str(e)
            if errors:
                raise SettingsError("Import would be rejected.", errors)
            return {"applied": sorted(apply), "skipped": skipped, "changed": []}
        res = self.update(apply)
        return {"applied": sorted(apply), "skipped": skipped, "changed": res["changed"]}
