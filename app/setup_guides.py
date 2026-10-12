"""Step-by-step "get your printer ready" guides for the Add printer wizard.

One source of truth for three readers:
  * the dashboard wizard renders `GUIDES[family]` above the form (collapsible, numbered),
  * `list_printer_types` / GET /api/printer-types hand the same guide to AI agents
    (`setup_guide` and `troubleshooting` on each type), so an agent can walk a human through it,
  * `match_hint()` turns a failed "Test connection" into a plain-English tip that points
    back at the guide step that fixes it (the probe's own error text is not changed).

Guide shape: {"title", "intro", "steps": [{"id","title","text"}], "notes": [{"id","kind","title","text"}]}
  note kind: "important" (the dashboard prints the word IMPORTANT) or "info" (GOOD TO KNOW),
  so nothing is conveyed by colour alone.
Field help in each family's CONFIG_FIELDS carries a "guide_step" naming the step that shows
where to find that value.

Provenance (maintainers): steps marked VERIFIED were checked on real hardware by the owner
(Bambu P1S fw 01.07 and A1 fw 01.08.01 in LAN-only mode, Prusa Core One / MK4S via PrusaLink and
OctoPrint). Everything marked UNVERIFIED is written from the vendors' public documentation and
is deliberately worded version-tolerantly ("menu names vary by firmware"). Keep it that way:
firmware menus move between releases. Generic only - no serials, addresses or codes of real printers.
"""

# --------------------------------------------------------------------------- Bambu Lab

BAMBU_GUIDE = {
    "title": "Before you start: get your Bambu Lab printer ready",
    "intro": "About 5 minutes, at the printer's touch screen. Works with P1S, P1P, A1, A1 mini, X1 "
             "and H2 printers. No Bambu account or cloud is needed.",
    "steps": [
        # VERIFIED: ports 8883 / 990 / 6000 are what the Hub uses (MQTT, file transfer, camera).
        {"id": "network", "title": "Same network as the Hub",
         "text": "Connect the printer to the same router or Wi-Fi as the computer that runs the Hub. "
                 "Guest Wi-Fi and separate VLANs usually block the connection. The Hub talks to "
                 "the printer on ports 8883 (control), 990 (files) and 6000 (camera, P1 and A1)."},
        # VERIFIED (behaviour): cloud mode accepts status but silently drops start-print; LAN Only fixes it.
        # UNVERIFIED: exact menu path (varies by model and firmware).
        {"id": "lan", "title": "Switch on LAN Only mode",
         "text": "On the printer screen open Settings, then WLAN (on X1 and H2 printers it can be "
                 "under General or Network - menu names vary by firmware). Find LAN Only Mode and "
                 "switch it on. The Hub can watch a printer in normal (cloud) mode, but in cloud mode "
                 "the printer ignores start-print commands. LAN Only mode makes them work."},
        # VERIFIED: A1 fw 01.08.01 needs LAN-only + Developer Mode; P1S fw 01.07 has no Developer Mode and does not need it.
        # UNVERIFIED: where the toggle sits.
        {"id": "dev", "title": "Switch on Developer Mode (recent firmware)",
         "text": "Developer Mode lets software on your own network, like this Hub, send commands to "
                 "the printer. Newer firmware refuses those commands without it, so starting prints "
                 "from the Hub would fail. Look for Developer Mode on the same screen as LAN Only "
                 "Mode (or just below it) and switch it on. If your printer has no such switch, its "
                 "firmware is older and does not need it - skip this step."},
        {"id": "ip", "title": "Find the IP address",
         "text": "Settings > WLAN shows the printer's IP address (on X1 and H2: Settings > Network). "
                 "Your router's list of connected devices shows it too. Tip: give the printer a "
                 "reserved address in the router (a DHCP reservation). In LAN Only mode, a printer "
                 "that changes address simply drops off until you update it here."},
        {"id": "serial", "title": "Find the serial number",
         "text": "On the printer screen: Settings > Device (or About). It is also printed on the "
                 "sticker on the back or underside of the printer and on the box. It is 15 letters "
                 "and digits. The Hub reads the model from its first three characters."},
        {"id": "code", "title": "Read the access code",
         "text": "The access code is 8 characters and is shown on the printer screen next to the LAN "
                 "Only setting (Settings > WLAN, or Settings > Network on X1 and H2). Read it AFTER "
                 "you have switched LAN Only and Developer Mode on, because it can change when you "
                 "do."},
        {"id": "discover", "title": "Shortcut: let the Hub find it",
         "text": "Go back to the Printers page and tap Discover printers. The Hub scans your network "
                 "and fills in the IP address and serial number for you. You still type the access "
                 "code yourself."},
        {"id": "test", "title": "Fill in the form, then test",
         "text": "Type the details below and tap Test connection. When it says Connected, tap Save."},
    ],
    "notes": [
        # VERIFIED: P1S regenerated its code on 2 toggles, kept it on 2 others; A1 regenerated on one, kept on one.
        {"id": "code_changes", "kind": "important", "title": "The access code can change",
         "text": "Switching LAN Only or Developer Mode on or off can give the printer a new access "
                 "code. If the Hub later says 'wrong access code', read the code on the printer "
                 "again and enter it under Printers > Edit. Nothing else needs to be redone."},
        # VERIFIED: Handy / cloud gone in LAN-only mode; local companions kept working. Wording is neutral on purpose.
        {"id": "cloud", "kind": "info", "title": "What LAN Only mode changes",
         "text": "While LAN Only mode is on, Bambu's cloud features are not available. That includes "
                 "the Bambu Handy app and remote access from outside your home or workshop. Printing "
                 "and monitoring inside your own network keep working. You can switch it off again "
                 "at any time (the access code may change again)."},
        # VERIFIED: P1S note - Authorization Control arrived with 01.08.03 / 01.08.05 firmware.
        {"id": "firmware", "kind": "important", "title": "Think before updating the firmware",
         "text": "Newer Bambu firmware can restrict control from third-party software. Before you "
                 "update, read the release notes and check that Developer Mode is still offered. "
                 "A printer that works with the Hub today can stop working after an update."},
        # UNVERIFIED: "LAN mode liveview" setting name. VERIFIED: P1/A1 use :6000, X1/H2 RTSP (driver docstring).
        {"id": "camera", "kind": "info", "title": "Camera",
         "text": "P1 and A1 printers show their camera in the Hub automatically (no setup). If there "
                 "is no picture, look for a 'LAN mode liveview' option next to LAN Only Mode. X1 and "
                 "H2 printers use a different video format that the Hub does not support yet - "
                 "everything else works, just without a picture."},
    ],
}

# --------------------------------------------------------------------------- PrusaLink

PRUSALINK_GUIDE = {
    "title": "Before you start: get your Prusa printer ready (PrusaLink)",
    "intro": "For Prusa printers with PrusaLink built in: MK4 and MK4S, MK3.9 and MK3.5, Core One, "
             "XL and MINI. About 3 minutes at the printer. (A MK3 or older with no network port "
             "is run by OctoPrint instead - pick OctoPrint.)",
    "steps": [
        # VERIFIED (Core One, buddy firmware): Settings > Network > PrusaLink; disabled there = no web UI on port 80.
        {"id": "enable", "title": "Switch PrusaLink on",
         "text": "On the printer screen open Settings > Network and make sure the printer is "
                 "connected to your network (Wi-Fi or Ethernet). Then open the PrusaLink entry in "
                 "the same menu and make sure it is enabled."},
        {"id": "ip", "title": "Find the address",
         "text": "Settings > Network shows the printer's IP address. The address to enter below is "
                 "http:// followed by that number, for example http://192.168.1.50. To check it, "
                 "type it into a web browser on any computer on the same network: you should see "
                 "the PrusaLink login page. Tip: give the printer a reserved address in the router."},
        # VERIFIED: user maker + password shown on the PrusaLink screen; 'Generate Password' rotates it.
        {"id": "creds", "title": "Read the username and password",
         "text": "The PrusaLink screen (Settings > Network > PrusaLink) shows the username, usually "
                 "maker, and the password. Type them in exactly, capital letters included. If the "
                 "password is unclear, use the screen's Generate Password button to make a new one "
                 "(the old one stops working everywhere)."},
        # VERIFIED: buddy firmware has no legacy API key; its password also works in the API key slot.
        # UNVERIFIED: PrusaLink on older MK3/MINI firmware shows an API key in its web page.
        {"id": "apikey", "title": "If your firmware uses an API key",
         "text": "Some older firmware shows an API key (in the PrusaLink web page under Settings) "
                 "instead of a password. If so, paste it into the API key box and leave the "
                 "password empty. Most current printers use the username and password."},
        {"id": "usb", "title": "Put in the USB stick",
         "text": "Your G-code files live on the printer's USB stick, so keep a stick inserted. "
                 "Leave Storage on usb unless you know your files are somewhere else."},
        {"id": "test", "title": "Fill in the form, then test",
         "text": "Type the details below and tap Test connection. When it says Connected, tap Save."},
    ],
    "notes": [
        {"id": "works", "kind": "info", "title": "What the Hub can do with PrusaLink",
         "text": "Works: live status, temperatures, progress, the file list, and start, pause, resume "
                 "and cancel. Not available through PrusaLink: pre-heat, cool-down, light, and "
                 "filament load or unload. The Hub hides those buttons for this printer. File "
                 "backup does not run while a job is printing, because it reads the same USB stick."},
        # VERIFIED: Core One / PrusaLink has no local camera; Prusa Connect cameras are cloud-only.
        {"id": "camera", "kind": "info", "title": "Camera",
         "text": "PrusaLink has no local camera, and Prusa's own camera view is cloud-only, so the "
                 "Hub shows no picture by default. If you have another webcam that serves a JPEG "
                 "still over your network, put its address in 'Camera snapshot URL'."},
    ],
}

# --------------------------------------------------------------------------- OctoPrint

OCTOPRINT_GUIDE = {
    "title": "Before you start: get OctoPrint ready",
    "intro": "For any printer run by OctoPrint, for example a Prusa MK3 or MK4 connected by USB to a "
             "Raspberry Pi. About 3 minutes in the OctoPrint web page.",
    "steps": [
        {"id": "url", "title": "Find the OctoPrint address",
         "text": "It is what you type into a browser to open OctoPrint, like http://octopi.local or "
                 "http://192.168.1.50. Use the same address here. If you leave off the http:// the "
                 "Hub adds it."},
        # VERIFIED: a printer with OctoPrint's auto-connect off stayed "not connected" after a power cycle.
        {"id": "connect", "title": "Connect OctoPrint to the printer",
         "text": "In OctoPrint, check the Connection panel says the printer is connected; if not, "
                 "choose the serial port and tap Connect. To make this happen by itself after a power "
                 "cut, open Settings > Serial Connection and tick the option to connect "
                 "automatically when OctoPrint starts (menu names vary by version)."},
        # VERIFIED (behaviour): the old global key still works but logs deprecation warnings.
        # UNVERIFIED: exact menu wording of Application Keys on every OctoPrint version.
        {"id": "apikey", "title": "Create an API key for the Hub",
         "text": "In OctoPrint open Settings (spanner icon) > Application Keys. Type Fabrica as "
                 "the application name and tap Generate. Copy the long key it shows and paste it "
                 "below. Older OctoPrint versions have only Settings > API: copy the key from there. "
                 "A separate Fabrica key is better than the global key: you can revoke it "
                 "without touching anything else."},
        # VERIFIED: classic webcam snapshot at /webcam/?action=snapshot works from outside the Pi.
        {"id": "webcam", "title": "Webcam (optional)",
         "text": "If OctoPrint already shows a webcam picture under Settings > Webcam & Timelapse, "
                 "the Hub finds it by itself; there is nothing to enter. No picture in OctoPrint "
                 "means no picture in the Hub."},
        {"id": "test", "title": "Fill in the form, then test",
         "text": "Type the details below and tap Test connection. When it says Connected, tap Save."},
    ],
    "notes": [
        # VERIFIED: M601 in afterPrintDone paused the MK4S after every OctoPrint job.
        {"id": "prusa_scripts", "kind": "info", "title": "Prusa printers: check OctoPrint's G-code scripts",
         "text": "Under Settings > GCODE Scripts, leave 'Before print job starts' and 'After print job "
                 "completes' empty on a Prusa MK4. A pause command (M601) in the after-print script "
                 "pauses the printer at the end of every job."},
    ],
}

# --------------------------------------------------------------------------- Klipper / Moonraker

MOONRAKER_GUIDE = {
    "title": "Before you start: get your Klipper printer ready (Moonraker)",
    "intro": "For Klipper printers with Moonraker, usually with the Mainsail or Fluidd web page on a "
             "Raspberry Pi. About 3 minutes.",
    "steps": [
        # UNVERIFIED (written from the Moonraker / Mainsail / Fluidd documentation).
        {"id": "address", "title": "Find the address",
         "text": "Use the address you type into a browser to open Mainsail or Fluidd, like "
                 "http://192.168.1.60 or http://voron.local. The Hub reaches Moonraker on its usual "
                 "port, 7125, by itself. Only add :7125 yourself if your web page is on a different "
                 "port."},
        {"id": "trusted", "title": "Let the Hub in",
         "text": "Most home setups need no key, because Moonraker trusts your local network. If "
                 "the test says Moonraker wants a key, open moonraker.conf (Mainsail and Fluidd have "
                 "an editor for it) and make sure the [authorization] section lists your network "
                 "under trusted_clients, for example 192.168.1.0/24. Save, then restart Moonraker."},
        {"id": "apikey", "title": "Or use an API key",
         "text": "Instead of trusting the network you can give the Hub a key. From a computer that "
                 "is allowed in, open http://<address>:7125/access/api_key to read it (newer "
                 "Mainsail and Fluidd also show it in their settings). Paste it into the API key box. "
                 "Leave the box empty if you did not need a key."},
        {"id": "ready", "title": "Check Klipper is running",
         "text": "In Mainsail or Fluidd the printer should say Ready. If it says Error or Shutdown, "
                 "fix that and use Firmware Restart there. Until then the Hub shows the printer as "
                 "not connected."},
        {"id": "test", "title": "Fill in the form, then test",
         "text": "Type the details below and tap Test connection. When it says Connected, tap Save."},
    ],
    "notes": [
        {"id": "controls", "kind": "info", "title": "Light, load and unload are detected from your config",
         "text": "The Hub switches a light if printer.cfg has an output_pin, led, neopixel or dotstar "
                 "whose name contains light, led or lamp (for example 'caselight'). It offers "
                 "filament unload and load only if you have macros named UNLOAD_FILAMENT and "
                 "LOAD_FILAMENT. Otherwise those buttons stay hidden for this printer."},
        {"id": "camera", "kind": "info", "title": "Camera",
         "text": "The Hub uses the webcam you set up in Mainsail or Fluidd (stored in Moonraker). "
                 "Web links like /webcam/?action=snapshot are looked up on the printer's address "
                 "without port 7125. If your camera is served elsewhere, fill in 'Web UI / camera "
                 "base address'."},
    ],
}

GUIDES = {
    "bambu": BAMBU_GUIDE,
    "prusalink": PRUSALINK_GUIDE,
    "octoprint": OCTOPRINT_GUIDE,
    "moonraker": MOONRAKER_GUIDE,
}

# --------------------------------------------------------------------------- test-connection tips
# One entry per known way "Test connection" fails. Matching only READS the probe result
# ({"code", "error"}); probe behaviour is untouched. `codes` match probe_bambu / probe_octoprint
# result codes, `contains` match (case-insensitive) fragments of the error text for the
# drivers whose probes carry no code (PrusaLink, Moonraker). First match wins.

_BAMBU_NET = ("Make sure the printer is switched on and on the same network as this Hub (guest Wi-Fi "
              "and separate VLANs often block it), and that the IP address is current: read it again "
              "on the printer (Settings > WLAN). In LAN Only mode a printer that moved to a new "
              "address cannot be found.")

HINTS = {
    "bambu": [
        {"codes": ("bad_code",), "step": "code",
         "when": "Wrong access code",
         "tip": "The access code changes when LAN Only or Developer Mode is switched on or off. Read "
                "it again on the printer screen (Settings > WLAN) and type the new one."},
        {"codes": ("unreachable", "timeout", "refused", "tls"), "step": "network",
         "when": "Can't reach the printer / no answer",
         "tip": _BAMBU_NET},
        {"codes": ("dropped",), "step": "code",
         "when": "The printer dropped the connection",
         "tip": "Check the access code first (it changes when LAN Only or Developer Mode is toggled). "
                "Then close other apps that hold the printer's local connection, such as Bambu Studio "
                "or a second Hub, and test again."},
        {"codes": ("serial_mismatch",), "step": "serial",
         "when": "Connected, but no data for that serial number",
         "tip": "The serial number does not match this printer. Re-read it on the printer "
                "(Settings > Device) or from its sticker. Discover printers can fill it in for you."},
    ],
    "octoprint": [
        {"codes": ("bad_key",), "step": "apikey",
         "when": "OctoPrint rejected the API key",
         "tip": "Create a new key under OctoPrint Settings > Application Keys (name it Fabrica) and "
                "paste it again without spaces. A key that was revoked or deleted in OctoPrint stops "
                "working."},
        {"codes": ("unreachable",), "step": "url",
         "when": "Can't reach OctoPrint",
         "tip": "Check the address opens OctoPrint in a browser on another computer, and that the Hub "
                "and OctoPrint are on the same network."},
        {"codes": ("not_octoprint",), "step": "url",
         "when": "The address answers, but it is not OctoPrint",
         "tip": "Use the address and port you use to open the OctoPrint page (for example "
                "http://octopi.local). A Klipper or Prusa printer needs a different printer type."},
    ],
    "prusalink": [
        {"contains": ("authentication failed", "rejected the credentials"), "step": "creds",
         "when": "Wrong username or password (401)",
         "tip": "Read the username and password again on the printer (Settings > Network > PrusaLink). "
                "The username is usually maker, the password is case-sensitive, and a newly generated "
                "password replaces the old one. On firmware that shows an API key instead, use the "
                "API key box."},
        {"contains": ("could not connect",), "step": "enable",
         "when": "Can't reach the printer",
         "tip": "Check PrusaLink is switched on (Settings > Network > PrusaLink), the printer is on "
                "the same network as this Hub, and the address is the printer's current IP. Test it "
                "in a browser: you should see the PrusaLink login page."},
        {"contains": ("does not look like prusalink", "not like prusalink"), "step": "ip",
         "when": "The address answers, but it is not PrusaLink",
         "tip": "Use the printer's own address (http://<ip>, no port). A printer run by OctoPrint or "
                "Klipper needs that printer type instead."},
    ],
    "moonraker": [
        {"contains": ("wants an api key", "trusted_clients"), "step": "trusted",
         "when": "Moonraker refuses the Hub (401 / 403)",
         "tip": "Add the Hub's network to [authorization] trusted_clients in moonraker.conf (for "
                "example 192.168.1.0/24) and restart Moonraker, or paste an API key into the key box."},
        {"contains": ("could not connect",), "step": "address",
         "when": "Can't reach the Klipper host",
         "tip": "Check the address opens Mainsail or Fluidd in a browser on another computer, and that "
                "the Hub is on the same network. Moonraker listens on port 7125."},
        {"contains": ("does not look like moonraker", "not like moonraker"), "step": "address",
         "when": "The address answers, but it is not Moonraker",
         "tip": "Use the address of your Mainsail or Fluidd page, or http://<address>:7125."},
    ],
}


def match_hint(family_key: str, result: dict):
    """-> {"text", "step"} for a failed probe result, or None. Never raises."""
    try:
        if not result or result.get("ok"):
            return None
        code = (result.get("code") or "").lower()
        err = (result.get("error") or result.get("message") or "").lower()
        for h in HINTS.get(family_key, ()):
            if code and code in h.get("codes", ()):
                return {"text": h["tip"], "step": h["step"]}
            if err and any(s in err for s in h.get("contains", ())):
                return {"text": h["tip"], "step": h["step"]}
    except Exception:
        pass
    return None


def guide_for(family_key: str):
    g = GUIDES.get(family_key)
    return None if g is None else {
        "title": g["title"], "intro": g["intro"],
        "steps": [dict(s) for s in g["steps"]], "notes": [dict(n) for n in g["notes"]]}


def troubleshooting_for(family_key: str) -> list:
    return [{"when": h["when"], "tip": h["tip"], "step": h["step"]} for h in HINTS.get(family_key, ())]
