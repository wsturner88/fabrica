"""The built-in agent kit: what an agent needs to use Fabrica, served by Fabrica itself.

  * short markdown guides in agent_guides/<topic>.md (shipped in the image with app/)
  * a KIT VERSION ("k-" + 8 hex) = hash of every guide's text + every MCP tool name and description + the prompt names.
    It changes by itself whenever any of those change: nobody bumps a number.
  * agent_kit.json in the data dir: a short history of versions, so `fabrica_guide('changes', since=<old version>)`
    can say exactly what is new.
  * server instructions (sent to every MCP client on connect), one resource template and three workflow prompts.

Nothing here touches a printer or the network.
"""
import datetime
import hashlib
import json
import os
import re
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDES_DIR = os.path.join(HERE, "agent_guides")
TOPICS = ("start", "printing", "library", "history", "renaming", "changes")
KEEP = 50                                    # versions remembered
_TOPIC_RX = re.compile(r"^[a-z]+$")


def _h(text: str, n: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def read_guides(guides_dir: str = GUIDES_DIR) -> dict:
    """{name: text} for every *.md in the folder (the topics plus changelog)."""
    out = {}
    try:
        for fn in sorted(os.listdir(guides_dir)):
            if fn.endswith(".md"):
                with open(os.path.join(guides_dir, fn), encoding="utf-8") as f:
                    out[fn[:-3]] = f.read()
    except OSError:
        pass
    return out


def compute_version(guides: dict, tools: dict, prompts) -> str:
    """tools = {name: description}; prompts = names. Stable for the same input, different for any change."""
    blob = json.dumps({"guides": guides, "tools": tools, "prompts": sorted(prompts)}, sort_keys=True)
    return "k-" + _h(blob)


def _atomic_write(path: str, text: str):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Kit:
    def __init__(self, data_dir: str, build: str = "dev", guides_dir: str = GUIDES_DIR):
        self.path = os.path.join(data_dir, "agent_kit.json")
        self.build = build
        self.guides_dir = guides_dir
        self.version = ""
        self.record = None
        self.prompts = []
        self._lock = threading.Lock()

    # ---- startup -----------------------------------------------------------
    def finish(self, tools: dict, prompts) -> str:
        """Call once, after every tool and prompt is registered. Computes the version and, if it is new, records it."""
        guides = read_guides(self.guides_dir)
        self.prompts = sorted(prompts)
        self.version = compute_version(guides, tools, self.prompts)
        self.record = {"version": self.version,
                       "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "build": self.build, "tools": sorted(tools),
                       "tool_desc": {n: _h(d or "") for n, d in tools.items()},
                       "prompts": self.prompts,
                       "guides": {k: _h(v) for k, v in guides.items()}}
        with self._lock:
            recs = self._load()
            if recs and recs[-1].get("version") == self.version:
                self.record = recs[-1]
            else:
                recs = (recs + [self.record])[-KEEP:]
                try:
                    _atomic_write(self.path, json.dumps({"records": recs}, indent=1))
                except OSError as e:
                    print(f"[agentkit] could not save {self.path}: {e}", flush=True)
        return self.version

    def _load(self) -> list:
        try:
            with open(self.path, encoding="utf-8") as f:
                recs = json.load(f).get("records")
            return [r for r in recs if isinstance(r, dict) and r.get("version")] if isinstance(recs, list) else []
        except (OSError, ValueError, AttributeError):
            return []

    # ---- what agents receive -----------------------------------------------
    def header(self) -> str:
        return f"Fabrica kit {self.version} · build {self.build}"

    def instructions(self) -> str:
        return "\n".join([
            "Fabrica manages a fleet of 3D printers. These tools can start, stop and change real machines.",
            "Call fabrica_guide() first: it holds everything you need (topics: " + ", ".join(TOPICS) + ").",
            "Rules that always apply:",
            "- A human confirms the build plate is clear before EVERY print start.",
            "- Start prints only through suggest_mapping (it returns a ticket) and then start_print.",
            "- Never pause or cancel a print unless the human asked you to.",
            "- Upload, delete or rename files only on idle printers.",
            "- Add, change or remove printers and settings only when the human asks.",
            "- A reply starting REFUSED: is a safety stop. Tell the human; do not work around it.",
            f"Kit version: {self.version}.",
            "If any Fabrica reply shows a different kit version than the one you know, call",
            "fabrica_guide('changes', since='<the version you know>') and re-read what it lists.",
        ])

    def guide(self, topic: str = "start", since: str = "") -> str:
        topic = (topic or "start").strip().lower()
        if topic not in TOPICS or not _TOPIC_RX.match(topic):
            return f"{self.header()}\nUnknown topic '{topic[:40]}'. Topics: {', '.join(TOPICS)}."
        if topic == "changes":
            return self.changes(since)
        return self.header() + "\n\n" + read_guides(self.guides_dir).get(topic, "(this guide is missing)").rstrip() + "\n"

    def changes(self, since: str = "") -> str:
        since = (since or "").strip()
        head = self.header()
        notes = self._notes()
        if not since:
            return (f"{head}\n\nNo `since` given. Pass the kit version you know, e.g. "
                    f"fabrica_guide('changes', since='k-1a2b3c4d').\n\nRecent release notes:\n{notes}\n")
        if since == self.version:
            return f"{head}\n\nNo changes: you already have the current kit ({since}).\n"
        old = next((r for r in reversed(self._load()) if r.get("version") == since), None)
        if old is None:
            return (f"{head}\n\nI do not know the version '{since[:40]}' (too old, or from another Hub). "
                    f"Here is the full start guide:\n\n" + self.guide("start"))
        cur = self.record
        ot, ct = set(old.get("tools", [])), set(cur.get("tools", []))
        changed = sorted(n for n in ot & ct
                         if (old.get("tool_desc") or {}).get(n) != (cur.get("tool_desc") or {}).get(n))
        og, cg = old.get("guides", {}), cur.get("guides", {})
        op, cp = set(old.get("prompts", [])), set(cur.get("prompts", []))
        rows = [("Tools added", sorted(ct - ot)), ("Tools removed", sorted(ot - ct)),
                ("Tools with a changed description", changed),
                ("Guides changed", sorted(k for k in set(og) | set(cg) if k != "changelog" and og.get(k) != cg.get(k))),
                ("Workflows (prompts) added", sorted(cp - op)), ("Workflows (prompts) removed", sorted(op - cp))]
        lines = [f"{head}", "", f"Changes since {since} ({str(old.get('date', ''))[:10]}):"]
        lines += [f"- {label}: {', '.join(v)}" for label, v in rows if v]
        if len(lines) == 3:
            lines.append("- Only wording changed (release notes below).")
        lines += ["", "Release notes:", notes, "",
                  "Re-read the changed guides with fabrica_guide(<topic>) and use the tool list from your client."]
        return "\n".join(lines) + "\n"

    def _notes(self) -> str:
        text = read_guides(self.guides_dir).get("changelog", "").strip()
        return "\n".join(text.splitlines()[-10:]) or "(none)"

    def summary(self) -> dict:
        return {"version": self.version, "build": self.build, "topics": list(TOPICS), "prompts": list(self.prompts)}


# ---- workflows (MCP prompts) and the resource ---------------------------------

PROMPT_TEXT = {
    "print_a_file": (
        "Print a file on a Fabrica printer. Follow fabrica_guide('printing').\n"
        "1. Find the file: {file}. Use library_list or list_sd_files; if it is only in the library and the printer is "
        "idle, library_send puts it on the printer.\n"
        "2. Choose an idle printer ({printer}); list_printers shows which are idle.\n"
        "3. Call suggest_mapping and tell me the plan in plain words: spools by colour name, time, weight, any warning.\n"
        "4. Ask me whether the build plate is clear and whether to go ahead. Wait for my yes.\n"
        "5. Only then call start_print with the ticket and bed_clear_confirmed_by_human=true. Report the result."),
    "fleet_status": (
        "Give me the status of my printers. Call list_printers, then printer_status for any that are printing, "
        "paused, failed or disconnected.\n"
        "Report in a short table: printer, state, file, progress, time left. Then list anything that needs me: "
        "a failed print, a printer waiting for a plate to be cleared, a disconnected printer, a spool problem. "
        "Change nothing."),
    "print_history_report": (
        "Report what was printed in this period: {period} (for example day, week, month, 30d). "
        "Printer or group filter (optional): {scope}.\n"
        "Use job_stats for the totals (jobs, success rate, hours, grams, cost) and recent_jobs for the details. "
        "For each file give attempts, parts made (see fabrica_guide('history')) and cost; if I name one file, use "
        "cost_per_part. State the period and filter you used. Do not guess missing numbers; say unknown."),
}


def register(mcp, kit: "Kit"):
    """Add the guide resource and the three workflow prompts to a FastMCP server."""

    @mcp.resource("fabrica://guide/{topic}", mime_type="text/markdown")
    def guide_resource(topic: str) -> str:
        """A Fabrica agent guide (same text as fabrica_guide)."""
        return kit.guide(topic)

    @mcp.prompt()
    def print_a_file(file: str = "", printer: str = "") -> str:
        """Print a file step by step: find it, plan the AMS mapping, ask if the bed is clear, then start."""
        return PROMPT_TEXT["print_a_file"].format(file=file or "ask me which file", printer=printer or "ask me if unclear")

    @mcp.prompt()
    def fleet_status() -> str:
        """What is printing, when it will be done, and anything that failed or is waiting."""
        return PROMPT_TEXT["fleet_status"]

    @mcp.prompt()
    def print_history_report(period: str = "month", printer_or_group: str = "") -> str:
        """What was printed in a period: pieces per file, success and cost (optionally one printer or group)."""
        return PROMPT_TEXT["print_history_report"].format(period=period or "month",
                                                          scope=printer_or_group or "all printers")
