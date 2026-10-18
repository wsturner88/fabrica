# Printer discovery

The **Printers** page has a **Discover printers** button. It scans the networks you list, shows the
printers that answer, and **Add this printer** opens the Add wizard with the family, address, serial
number, model and a suggested name already filled in - you only type the secret (Bambu access code,
OctoPrint API key, PrusaLink password).

Code: `app/discovery.py` (stdlib only, no server imports). Tests: `tests/test_discovery.py`.

## How it finds printers (and what it cannot see)

Discovery is an **active TCP scan** of CIDR ranges the user gives. It does not depend on multicast or
broadcast, because the Hub normally runs in Docker and on macOS (Colima / Docker Desktop) the container sits
behind a NAT: nothing multicast or broadcast from the home LAN ever reaches it. What does work from inside
the container is an outbound connection to an address you name, so that is what is used.

- **Networks**: `192.168.1.0/24`, `10.0.0.0/24`, a single address, ... Only private ranges (192.168/16,
  10/8, 172.16/12, 169.254/16, 100.64/10 for VPN overlays, 127/8) may be scanned, never the internet.
  The biggest network is a /22 (1024 addresses), at most 8 networks and 2048 addresses in all.
- **Suggestions**: the /24 of every configured printer's IP (de-duplicated), then the Hub's own /24 when it
  looks like a real LAN (never a 172.16/12 Docker bridge). With no printers yet and Docker, nothing can be
  suggested - the user types the network. The UI says that the Hub only sees the ranges listed.
- **Another site** (a home network reached over a VPN subnet route): add that network to the list. It works if the
  Hub can route to it. The Hub notices that the network is far away and scans it gently - see "Near and far" below.
- **Ports touched**: 8883, 990, 6000 (Bambu), 7125 (Moonraker), 80 and 5000 (OctoPrint / PrusaLink / Mainsail).
  Nothing else. No credentials are ever sent, nothing is written to any device, requests are plain `GET`s.
- **Limits**: a local network is scanned with 64 workers and a ~0.6 s connect timeout; a far network with a
  pool of at most 16 and a 1.5 s timeout (below). A host is skipped only after ports 80, 7125 and 8883 all timed
  out. One scan at a time, 3 s cool-down between scans, and a hard stop after 240 s.

## Near and far networks

A burst of dozens of parallel connects can overload a routed path (a VPN subnet route, a NAT such as the one in
front of a Docker container on a Mac): the excess SYNs are dropped, and a dropped SYN looks exactly like "nobody
is home". A short connect timeout (under the 1 s a TCP stack waits before re-sending a SYN) makes it worse. So the
scan first asks how far each network is, and treats the two cases differently.

| | near (the Hub's own LAN) | far (routed, VPN, slow) |
|---|---|---|
| How it is recognised | the Hub sits on it, or the first hosts that answer (known printers first, then .1 / .2 / the top of the range, on ports 80, 7125, 8883) answer in 10 ms or less | the round trip is over 10 ms; or nothing answered the check; or it is a 100.64/10 VPN overlay range |
| Pool | 64 workers at once | starts at 4, +2 every half second, at most 16 (a slow start) |
| Connect timeout | 0.6 s | 1.5 s (one SYN re-send fits) |
| Watching the route | no | a *canary* (a printer the Hub already has, or a host that answered the check) is re-connected to every half second. If it stops answering the route is overloaded: new hosts are held back, the pool is halved and never grows past the last good size, and the scan waits (up to 15 s) for the canary to answer again |
| Time for an empty /24 | about 6 s | about a minute (measured 62 s) |

**The second look.** A host counts as empty only when all three key ports (80, 7125, 8883) stayed silent and nothing
else answered. Hosts that came back silent, or only partly answered, get one more pass at the end - 4 workers, 2 s
timeout - when (a) the Hub already has a printer at that address, (b) they were probed while the canary was not
answering, or (c) a far network found nothing at all and had no canary to vouch for its silence (they are then
tried in order of closeness to any printer found or known). The second look is time-boxed to 60 s and says so when
it did not get through them all.

**Progress.** `GET /api/discover/{id}` also returns `phase` (`checking | scanning | rechecking | finishing | finished`),
`remote` (a far network is being scanned), `remote_networks`, `profiles` (per network: `kind` near / far,
`rtt_ms`, `why`), `eta_s` (seconds to go; a guess from the per-address cost until real progress is known),
`slowed` (the pool was cut back because the route was overloaded) and `recheck {total, done}`. The Discover screen
turns these into words ("Scanning a remote network - about a minute left", "Double-checking 5 quiet addresses").
If a scan reaches the 240 s limit it stops, keeps what it found and says so in `note`.

Numbers were chosen from measurements of a real routed /24 (about 35 ms round trip, four printers on it, scanned
from a Mac on another LAN). The time of a scan with nothing on it is `addresses x 0.8 x 3 x timeout / pool`, so the
pool and the timeout trade straight against each other:

| pool | connect timeout | time | found |
|---|---|---|---|
| 8 | 1.5 s | 117 s | 4 of 4 |
| 12 | 1.5 s | 79 s | 4 of 4 |
| **16 (chosen)** | **1.5 s** | **62 s** | 4 of 4 |
| 16 | 1.0 s | 42 s | 4 of 4 |
| 24 | 1.5 s | 43 s | 4 of 4 |

Connect latency also grows with the burst: median 43 ms with 64 connects in flight, 91 ms with 254, and the slowest
connect went from 0.16 s to 0.55 s. A path with one more NAT hop has less room than the 0.6 s timeout leaves.
Every one of the numbers is a `DiscoveryManager` argument (`far_workers`, `far_connect_timeout`, `far_rtt_ms`, `retry_*`, `scan_timeout`).

## What it recognises

| Family | What identifies it |
|---|---|
| Bambu Lab | TLS on 8883 whose certificate subject CN is a serial number (no login needed; read from the DER certificate with a tiny built-in parser). Needs corroboration: FTPS 990 or camera 6000 open, a `BBL` issuer, or a known serial prefix. Model from the serial prefix (`fleet_store.SERIAL_MODELS`). |
| Moonraker (Klipper) | `GET /server/info` returns JSON with `klippy_state` (wrapped in `result` or not); hostname from `GET /printer/info`. A 401 JSON error from a Tornado server is reported as "needs an API key" (medium confidence). |
| PrusaLink | `GET /api/version` JSON saying PrusaLink; or a 401/403 with a Digest `WWW-Authenticate` challenge (realm "Printer API"); or a page titled PrusaLink. Hostname / serial from `GET /api/v1/info` when open. |
| OctoPrint | `GET /api/version` JSON saying OctoPrint; or, when the API is locked (the default), a root page whose `<title>` contains OctoPrint. |

Optional extras, used only when they happen to work and never required: an SSDP listener (UDP 2021 and 1990)
for Bambu `NOTIFY` packets for ~3 s, and the `zeroconf` library if it is importable (it is not a
dependency). Both only add names or details for addresses inside the scanned networks.

A printer already in the fleet is marked `already_added` (matched by serial for Bambu, otherwise by
host + family), and the UI shows **Already added**.

## HTTP contract

```
GET    /api/discover/suggestions
  -> {"networks": ["192.168.1.0/24"], "sources": {"192.168.1.0/24": "used by P1S"}, "note": "..."}

POST   /api/discover           {"networks": ["192.168.1.0/24", "10.0.5.0/24"]}   (omit = use suggestions)
  -> 202 {"scan_id": "ab12cd34ef56", "total": 508, "networks": ["10.0.5.0/24", "192.168.1.0/24"]}
  -> 400 {"error": "..."} bad network   409 {"error", "busy": true, "scan_id"} a scan is running
  -> 429 {"error", "retry_after"} too soon after the last scan

GET    /api/discover/{scan_id}
  -> {"scan_id", "state": "running|done|cancelled|error", "finished": bool, "done": 140, "total": 254,
      "networks": [...], "elapsed_s": 6.1, "error": null, "note": null, "extras": {"ssdp_heard": 0, "zeroconf_names": 0},
      "phase": "scanning", "remote": false, "remote_networks": [], "eta_s": 4, "slowed": false,
      "profiles": [{"network": "192.168.1.0/24", "kind": "near", "rtt_ms": 1.2, "why": "round trip 1.2 ms"}],
      "recheck": {"total": 0, "done": 0},
      "found": [{
        "id": "bambu@192.168.1.50:8883", "family": "bambu|octoprint|prusalink|moonraker", "label": "Bambu Lab",
        "address": "192.168.1.50", "port": 8883, "url": null,            // url for the HTTP families
        "model": "Bambu Lab P1S", "serial": "01P00A...", "hostname": null,
        "already_added": false, "already_added_as": null,
        "confidence": "high|medium|low", "source": "tcp|ssdp", "details": {...},
        "suggested_name": "P1S",
        "prefill": {"type": "bambu", "values": {"name": "P1S", "ip": "192.168.1.50", "serial": "01P00A..."}}
      }]}
     `found` grows while the scan runs.  -> 404 for an unknown / expired scan.

DELETE /api/discover/{scan_id}   -> {"scan_id", "cancelling": true}
```

`prefill.values` use the same field names as `GET /api/printer-types`, so the dashboard just hands them
to the Add form. They never contain a secret.

MCP tool: `discover_printers(networks=None)` (control scope) = `DiscoveryManager.scan_and_wait(networks)`,
which returns the same final status object.

## How it is wired

In `server.py`: `DISCOVERY = discovery.DiscoveryManager(FLEET.list_public)`, the four routes from `discovery.routes(DISCOVERY)`
(registered in that order, so `/suggestions` is not taken for a scan id), and the MCP tool `discover_printers(networks=None)`.

Scopes: `GET /api/discover/suggestions` and `GET /api/discover/{scan_id}` are **read** (they only report); `POST /api/discover`
(starts a scan) and `DELETE /api/discover/{scan_id}` are **control**, and so is the MCP tool. A `DiscoveryError` is mapped to its own
HTTP status (400 / 404 / 409 / 429) or to `REFUSED: ...` for the tool.

The dashboard needed no change: **Printers -> Discover printers** used to say "does not have printer discovery yet" while the
suggestions route was missing and now works end to end (suggested networks, scan with progress, found printers, **Add this printer**
pre-fills the Add form).

Tests: `tests/test_wiring2.py` mounts the routes on the real server and scans `127.0.0.1` only, against fake Klipper / OctoPrint
hosts; its multicast extras (SSDP, zeroconf) are switched off and the "own network" lookup is stubbed, so nothing leaves the machine.
