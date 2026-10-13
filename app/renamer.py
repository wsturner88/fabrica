"""File renaming MECHANICS for agents and people (the pure part: names, tickets, no printer, no network).

The Hub does NOT know or enforce any naming convention - different projects name their files differently, and
those rules live in the owner's own agents. What the Hub provides is
  * the facts an agent needs to choose a name (`file_facts`, in server.py),
  * a check that a proposed name is something a file system and a printer can actually hold,
  * a SAFE, approved way to apply it: a preview, a single-use ticket bound to the exact validated list, and an apply
    step that needs the human's yes (`approved_by_human`).

This module holds the name check and the ticket store. Everything that touches a printer or the library is in the
`# ==== rename ====` block of server.py (so the busy guards stay in the one place the other file operations use).
Nothing here, or there, can start a print.
"""
import hashlib
import json
import re
import secrets
import threading
import time
import unicodedata

MAX_ITEMS = 100                  # renames in one preview
MAX_NAME_CHARS = 200
MAX_NAME_BYTES = 255             # what ext4 / exFAT / FAT long names hold
TICKET_TTL_S = 600               # long enough to show the table to the human and get a yes
MAX_TICKETS = 50
FORBIDDEN = '/\\:*?"<>|'         # the characters a file system or a printer's file protocol cannot hold in a name
ORIGINALS_FOLDER = "Renamed originals"      # library folder that receives the copy of a file renamed on a printer

METHOD_PRINTER = "download -> keep a copy in the Library -> upload under the new name -> verify -> delete the old name"
METHOD_LIBRARY = "rename inside the Library (file, preview and index change together)"


class RenameError(Exception):
    """A request the rename tools refuse as a whole (bad shape, too many items). str(e) is written for a person."""
    status = 400


def file_ext(name) -> str:
    """The extension a rename must keep, in the name's own capitals: `.gcode.3mf` is ONE extension, otherwise the last
    `.xxx` (letters/digits, up to 10). '' when there is none."""
    name = str(name or "")
    if name.lower().endswith(".gcode.3mf"):
        return name[-len(".gcode.3mf"):]
    m = re.search(r"\.[A-Za-z0-9]{1,10}$", name)
    return m.group(0) if m else ""


def basename(path) -> str:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1]


def folder_of(path) -> str:
    p = str(path or "").replace("\\", "/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def target_path(old_name, new_name) -> str:
    """Where the renamed file will be: the old file's folder (kept) + the new bare name."""
    f = folder_of(old_name)
    return (f + "/" if f else "") + new_name


def name_problems(old_name, new_name) -> list:
    """Why `new_name` cannot replace `old_name`'s name (plain sentences; [] = fine). Only what a file system or a printer
    needs: no naming convention is checked here, ever."""
    out = []
    if not isinstance(new_name, str) or not new_name:
        return ["The new name is empty."]
    if len(new_name) > MAX_NAME_CHARS or len(new_name.encode("utf-8")) > MAX_NAME_BYTES:
        out.append(f"The new name is too long (at most {MAX_NAME_CHARS} characters).")
    bad = sorted({c for c in new_name if c in FORBIDDEN})
    if bad:
        out.append("The new name has a character a file name cannot hold: " + " ".join(bad)
                   + ". Give just the file name (the folder stays the same).")
    if any(unicodedata.category(c)[0] == "C" for c in new_name):
        out.append("The new name has a control or invisible character.")
    if new_name != new_name.strip(" ") or new_name.startswith(".") or new_name.endswith("."):
        out.append("The new name cannot start or end with a space or a dot, or start with a dot.")
    old_ext, new_ext = file_ext(basename(old_name)), file_ext(new_name)
    if old_ext != new_ext:
        out.append(f"The new name must keep the extension {old_ext or '(none)'}"
                   + (f" (it ends in {new_ext})." if new_ext else " (it has none)."))
    else:
        stem = new_name[:len(new_name) - len(new_ext)] if new_ext else new_name
        if not stem:
            out.append("The new name has nothing before the extension.")
        elif stem != stem.rstrip(" .") or stem != stem.lstrip(" "):
            out.append("The part before the extension cannot end with a space or a dot.")
    if new_name == basename(old_name):
        out.append("The new name is the same as the old one.")
    return out


def parse_items(renames) -> list:
    """The `renames` argument -> [{"location","old_name","new_name"}] (NFC-normalised, all text), or RenameError."""
    if not isinstance(renames, list) or not renames:
        raise RenameError("Give a list of renames: [{\"location\": printer name or \"library\", \"old_name\": ..., \"new_name\": ...}].")
    if len(renames) > MAX_ITEMS:
        raise RenameError(f"Too many renames in one preview (at most {MAX_ITEMS}). Split them into several previews.")
    items = []
    for i, r in enumerate(renames, 1):
        if not isinstance(r, dict):
            raise RenameError(f"Rename {i} must be an object with location, old_name and new_name.")
        loc, old, new = r.get("location"), r.get("old_name"), r.get("new_name")
        if not all(isinstance(x, str) and x.strip() for x in (loc, old, new)):
            raise RenameError(f"Rename {i} needs text for location, old_name and new_name.")
        items.append({"location": loc.strip(), "old_name": unicodedata.normalize("NFC", old.strip("\r\n")),
                      "new_name": unicodedata.normalize("NFC", new)})
    return items


def digest(items) -> str:
    """A fingerprint of the exact validated list (what a ticket is bound to)."""
    blob = json.dumps([[i["location"], i["old_name"], i["new_name"], i.get("size")] for i in items], ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class RenameTickets:
    """Single-use, short-lived tickets: rename_preview issues one for the exact list it validated, rename_apply needs it back.

    Same pattern as the start-print tickets in server.py (StartTickets): random, kept on the server, consumed once, expires,
    and nothing about the expected value is ever echoed back. A ticket also remembers the live driver object of each printer
    in the list, so a printer that was edited or re-added since the preview invalidates it."""

    def __init__(self, ttl=TICKET_TTL_S, clock=time.time):
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._t = {}

    def _prune(self, now):
        for k in [k for k, t in self._t.items() if t["expires"] <= now]:
            del self._t[k]
        while len(self._t) > MAX_TICKETS:
            del self._t[min(self._t, key=lambda k: self._t[k]["created"])]

    def issue(self, items, printers) -> str:
        """`items` = the validated, ticketed renames; `printers` = {location lower: driver object}."""
        nonce = "rn-" + secrets.token_urlsafe(18)
        now = self.clock()
        with self._lock:
            self._prune(now)
            self._t[nonce] = {"items": [dict(i) for i in items], "printers": dict(printers), "digest": digest(items),
                              "created": now, "expires": now + self.ttl}
        return nonce

    def peek(self, nonce):
        """-> (ticket, None) or (None, why). Does not consume. `why` never contains the expected value."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            t = self._t.get(nonce.strip()) if isinstance(nonce, str) else None
        if t is None:
            return None, ("it is not a current ticket - it is wrong, was already used, or has expired "
                          f"(they last {self.ttl // 60} minutes and work once)")
        if digest(t["items"]) != t["digest"]:
            return None, "the list it was issued for has changed"
        return t, None

    def consume(self, nonce) -> bool:
        with self._lock:
            return self._t.pop(nonce.strip() if isinstance(nonce, str) else nonce, None) is not None

    def clear(self):
        with self._lock:
            self._t.clear()
