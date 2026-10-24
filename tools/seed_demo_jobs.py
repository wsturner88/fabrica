#!/usr/bin/env python3
"""Fill a THROW-AWAY data folder with made-up print history, so History and Analytics have something to show
(README screenshots, layout checks, demos). Everything is generic: four pretend printers in two groups, invented file
names, 60 days of prints with some failures, one print still running, a short printer-uptime history.

    python3 tools/seed_demo_jobs.py <empty-folder> [--days 60] [--seed 7] [--no-printers]

It writes into <empty-folder>:
    jobs.sqlite      the job log (same schema the Hub uses, created by joblog.py)
    uptime.jsonl     printer reachable / unreachable history (see app/uptime.py)
    printers.json    (unless --no-printers) four demo printers pointing at 127.0.0.1:9, where nothing listens, in the
                     groups "Work" and "Home", so the group filters have something to show. Nothing leaves this machine.

It refuses to run if the folder already holds a jobs.sqlite or printers.json: it can never overwrite a real install.
Start a local Hub on the result like this (see CONTRIBUTING.md; it never touches real printers):

    export BAMBU_MCP_CONFIG="<folder>/none.yml" BAMBU_MCP_CACHE="<folder>/metacache.json" BAMBU_MCP_HOST=127.0.0.1
    python3 app/server.py          # then open http://127.0.0.1:8271/dashboard   (add ?layout=5in for the 800x480 wall screen)

Stdlib only (printers.json needs the Hub's own packages); the same data every time for the same --seed.
"""
import argparse
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import joblog  # noqa: E402

PRINTERS = [   # name, group, model, how busy it is (0..1), reliability (chance a print finishes)
    ("Bench-A", "Work", "Demo Core", 0.95, 0.93),
    ("Bench-B", "Work", "Demo Core", 0.75, 0.88),
    ("Garage-1", "Home", "Demo Box", 0.50, 0.85),
    ("Shelf-Mini", "Home", "Demo Mini", 0.30, 0.95),
]
FILES = [   # name, parts per plate (None = unknown), grams, hours, material (or {type: grams} for a multi-colour print), popularity
    ("Phone Stand v3", 1, 38, 2.4, "PLA", 5), ("Cable Clip x12", 12, 26, 1.6, "PLA", 7), ("Headphone Hook", 1, 31, 1.9, "PETG", 3),
    ("Desk Organizer - Tall", 1, 142, 7.5, "PETG", 3), ("Plant Pot 120mm", 1, 96, 5.2, "PLA", 3), ("Gridfinity Bin 2x3", 4, 88, 4.1, "PLA", 8),
    ("Spool Holder Arm", 2, 54, 3.3, "PETG", 2), ("Drawer Pull x4", 4, 41, 2.6, "ASA", 3), ("Wall Hook - Heavy", 2, 63, 3.9, "ASA", 2),
    ("Keychain Tag x20", 20, 29, 2.2, "PLA", 4), ("Lamp Shade v2", 1, 118, 8.8, {"PLA": 96, "PETG": 22}, 1),
    ("Battery Holder AA x8", 8, 72, 4.7, "PLA", 3), ("Coaster Set x6", 6, 66, 3.1, {"PLA": 40, "TPU": 26}, 2),
    ("Raspberry Pi Case", 1, 47, 3.0, "PETG", 2), ("Calibration Cube", 1, 9, 0.5, "PLA", 4), ("Benchy", 1, 14, 1.0, "PLA", 4),
    ("Fan Duct v4", 1, 22, 1.7, "ASA", 2), ("Flexible Gasket x6", 6, 18, 1.4, "TPU", 2), ("Tablet Stand", None, 84, 5.4, "PLA", 7),
    ("Shelf Bracket x2", None, 58, 3.6, "PETG", 5),
]
REASONS = ["Filament ran out", "Nozzle clog suspected", "Print lifted off the bed", "Spaghetti detected - stopped",
           "Power loss during print", "Filament tangle at the spool"]


def seed(folder, days=60, seed_value=7, printers=True, now=None):
    rnd = random.Random(seed_value)
    now = float(now if now is not None else time.time())
    os.makedirs(folder, exist_ok=True)
    for name in ("jobs.sqlite", "printers.json"):
        if os.path.exists(os.path.join(folder, name)):
            raise SystemExit(f"{folder} already has a {name}: this tool only fills an EMPTY folder (it never overwrites a real install).")
    jl = joblog.JobLog(os.path.join(folder, "jobs.sqlite"), tz="UTC")
    jl.migrate()
    weights = [f[5] for f in FILES]
    n_rows = 0
    for pname, _grp, model, busy, reliab in PRINTERS:
        t = now - days * 86400 + rnd.uniform(0, 6 * 3600)
        while t < now - 3 * 3600:
            hour = time.gmtime(t).tm_hour
            if rnd.random() > busy or hour < 6:                       # idle gaps: nights, quiet days, a printer that is less used
                t += rnd.uniform(1, 6) * 3600 * (1.0 if hour >= 6 else 2.0)
                continue
            name, parts, grams, hours, mat, _w = rnd.choices(FILES, weights)[0]
            est = hours * 3600
            fils = [{"type": m, "color": "#808080", "used_g": g} for m, g in mat.items()] if isinstance(mat, dict) else [{"type": mat, "color": "#808080", "used_g": float(grams)}]
            main = max(fils, key=lambda f: f["used_g"])["type"]
            roll = rnd.random()
            if roll < reliab:
                result, pct, dur, err = "finished", 100.0, est * rnd.uniform(0.96, 1.08), None
            elif roll < reliab + (1 - reliab) * 0.6:
                result, pct = "failed", round(rnd.uniform(8, 92), 1)
                dur, err = est * pct / 100.0 * rnd.uniform(0.97, 1.03), rnd.choice(REASONS)
            else:
                result, pct = "cancelled", round(rnd.uniform(3, 70), 1)
                dur, err = est * pct / 100.0, None
            if t + dur > now - 1800:
                break
            if rnd.random() < 0.012:                                   # an end that was never reported
                result, err, dur, pct = "unknown", "closed: printer unreachable, end never reported", est * 0.5, None
            jl._insert(printer=pname, kind="demo", model=model, file=name + ".gcode.3mf", file_key=name.lower(), plate=1,
                       started_at=round(t, 1), ended_at=round(t + dur, 1), duration_s=round(dur, 1), result=result,
                       percent=pct, est_seconds=round(est), grams_est=float(grams),
                       grams_used=joblog.JobLog._used(grams, result, pct) if result != "unknown" else None,
                       material=main, colour="#808080", filaments=json.dumps(fils), parts=parts,
                       parts_source=("slice_info" if parts else None), pauses=0, error=err, source="event",
                       last_seen_at=round(t + dur, 1))
            n_rows += 1
            t += dur + rnd.uniform(0.2, 3.0) * 3600
    # one print still running right now
    name, parts, grams, hours, mat, _w = FILES[1]
    jl._insert(printer=PRINTERS[0][0], kind="demo", model=PRINTERS[0][2], file=name + ".gcode.3mf", file_key=name.lower(), plate=1,
               started_at=round(now - 1500, 1), result="running", percent=24.0, est_seconds=round(hours * 3600), grams_est=float(grams),
               material="PLA", filaments=json.dumps([{"type": "PLA", "color": "#808080", "used_g": float(grams)}]), parts=parts,
               parts_source="slice_info", source="event", last_seen_at=round(now - 30, 1))
    n_rows += 1
    jl.close()

    # printer uptime: every printer mostly reachable, a few outages of different lengths (see app/uptime.py for the format)
    lines = [{"t": round(now - days * 86400, 1), "k": "start", "last": None}]
    for pname in [p[0] for p in PRINTERS]:
        up = 1
        t = now - days * 86400 + 60
        lines.append({"t": round(t, 1), "k": "p", "n": pname, "up": 1})
        while True:
            t += rnd.uniform(3, 20) * 86400
            if t > now - 2 * 86400:
                break
            lines.append({"t": round(t, 1), "k": "p", "n": pname, "up": 0})
            t += rnd.choice([600, 1800, 7200, 20000])
            lines.append({"t": round(t, 1), "k": "p", "n": pname, "up": 1})
    lines.sort(key=lambda r: r["t"])
    with open(os.path.join(folder, "uptime.jsonl"), "w", encoding="utf8") as fh:
        for r in lines:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    with open(os.path.join(folder, "uptime_alive.json"), "w", encoding="utf8") as fh:
        json.dump({"t": round(now - 5, 1)}, fh)

    if printers:
        import fleet_store            # (needs the Hub's packages; --no-printers does not)
        store = fleet_store.FleetStore(folder, env={})
        for pname, grp, model, *_rest in PRINTERS:
            store.add({"name": pname, "type": "octoprint", "url": "http://127.0.0.1:9", "model": model}, secrets={"api_key": "demo-not-a-key"})
            store.set_groups(pname, [grp])
    return n_rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", help="an EMPTY folder to fill (it is created if missing)")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-printers", action="store_true", help="skip printers.json (no groups, no pretend printers)")
    a = ap.parse_args(argv)
    n = seed(a.folder, a.days, a.seed, printers=not a.no_printers)
    print(f"wrote {n} demo prints over {a.days} days into {os.path.abspath(a.folder)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
