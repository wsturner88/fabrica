# Spaghetti detection (optional add-on)

The Hub can watch every **running** print through its chamber camera and send you an alert
("P1S: possible print FAILURE") when it looks like spaghetti. It is **alert only**: the Hub never
pauses or stops a print on its own, and there is no setting that makes it.

> Status: the engine (`app/detection.py`), the detector service (`detector/`), the compose file and the
> Hub wiring (settings, routes, tools, dashboard) are all in place and tested against fakes. The Hub starts the
> loop only when it is run as a server (never on import) and it does nothing until **Settings -> Detection** is
> switched on. Section 9 is the plan for checking it on real hardware.

## 1. Editions

| Edition | Where things run | How |
|---|---|---|
| **Full** | Hub **and** detector on one machine (Mac, PC, NAS with Docker) | one compose command with `--profile detection` |
| **Split** | Hub on one machine (e.g. a Raspberry Pi), detector on another | detector box runs `docker-compose.detection.yml` alone; Hub is pointed at it (URL + token) |
| **Lite** | Hub only (e.g. Raspberry Pi 4, 2 GB) | nothing to install; detection is Off, or Split to another machine |

The model is far too heavy for a 2 GB Raspberry Pi (about 200 MB of weights, roughly 1 GB of RAM,
seconds per picture). Lite = Hub only; if you want detection with a Pi Hub, use Split.

## 2. How it works

```
Hub: every 10 s, for each RUNNING printer with a camera
  take the Hub's shared camera picture (no second camera connection)
  --POST /v1/score (JPEG, Bearer token)--> adapter --GET /p/?img=<one-time URL>--> Obico ml_api
  <-- {"score": 0.43, "detections": [...], "ms": 140} --
  smooth the score -> OK / WARNING / FAILURE -> alert
```

* The **detector** is "picture in, score out" and keeps no state. The **Hub** owns timing,
  smoothing, history and alerts.
* The model is Obico's `ml_api` CPU image, run **unmodified** from the upstream registry (AGPL-3.0,
  pinned by digest). Our adapter (MIT) sits in front of it because `ml_api` can only fetch pictures
  from a URL, not receive them. The two containers talk over a private Docker network with no
  route out; `ml_api` is never published on a port (it has no login of its own and fetches any URL it
  is given). See `detector/README.md` and `detector/adapter/README.md`.
* A score is the sum of the model's confidences for "failure" boxes in one picture. A single
  picture is noisy, so the Hub smooths it (section 5).

## 3. Full edition: one machine

Prerequisites: Docker with Compose >= 2.24 (Docker Desktop, or Colima on a Mac), about 1 GB of
disk for the image, about 1.5 GB of free RAM.

```bash
# 1. a token the Hub and the detector share (put it in .env next to docker-compose.yml)
echo "DETECTOR_TOKEN=$(openssl rand -hex 32)" >> .env

# 2. check the combined file parses
docker compose -f docker-compose.yml -f docker-compose.detection.yml --profile detection config -q

# 3. start the Hub + detector (first start pulls ~330 MB)
docker compose -f docker-compose.yml -f docker-compose.detection.yml --profile detection up -d          # pulls the published images; add --build to build the adapter from source
```

(Using the pre-built Hub image? Use `docker-compose.ghcr.yml` in place of `docker-compose.yml`.)

Notes:
* Leave out `--profile detection` and only the Hub starts: the extra file is harmless on its own.
  Lite installs simply never use it.
* The model needs up to two minutes to load; `docker compose ... ps` shows `healthy` when ready.
* The adapter is published on `127.0.0.1:8090` (this machine only) so you can test it with `curl`;
  the Hub reaches it inside Docker as `http://detector:8080`.
* The Hub container reads `DETECTOR_TOKEN` from the same `.env`, so "This machine" mode needs no
  token typed into the dashboard.
* Stop just the detector: `docker compose -f docker-compose.yml -f docker-compose.detection.yml --profile detection stop detector detector-ml`.

### The detector in a separate compose project (the Hub is already running)

You do not have to rebuild the Hub's compose file. Start the detector from its own folder / project, then point the Hub at it
with **Settings -> Detection -> Another machine** (address + token). Two ways to give the Hub an address that works from
*inside its container*:

| Way | Address to type | Notes |
|---|---|---|
| Through the host | `http://host.docker.internal:8090` | The adapter publishes `127.0.0.1:8090` on the host; from a container, `host.docker.internal` is the host. Docker Desktop and recent Colima resolve that name on their own. On plain Docker Engine (Linux) add `extra_hosts: ["host.docker.internal:host-gateway"]` to the Hub's service. **Do not add that line under Colima** - there `host-gateway` points at the VM, not at your Mac, and would break the name Colima already provides. Check from the Hub's container: `docker exec fabrica python -c "import socket;print(socket.gethostbyname('host.docker.internal'))"`; then press **Test connection** on the Detection page. |
| Same Docker network | `http://detector:8080` | `docker network connect <detector-project>_default fabrica` puts the Hub on the detector's network, where the service name `detector` resolves. Survives container restarts but not a re-create of the Hub unless the network is declared in its compose file. |

The token is the `DETECTOR_TOKEN` line in the detector project's `.env`; copy it into **Detector token** (stored write-only,
never shown again). **Test connection** checks both the address and the token and says in words which one is wrong.

### Resources (important on a Mac)
Docker on a Mac runs inside a VM with its own memory limit.

* **Colima**: check with `colima status`. The default (2 CPU / 2 GB) is too small. Recommended
  2 CPU / 6 GB: `colima stop && colima start --cpu 2 --memory 6`. Apple-silicon Macs should run
  Colima natively (aarch64), not `--arch x86_64`: do not set `platform: linux/amd64` anywhere.
* **Docker Desktop**: Settings -> Resources -> Memory: 6 GB.
* The model container is capped at `mem_limit: 1536m` so a leak cannot starve the Hub. Measure
  with `docker stats` (section 9) and adjust.
* CPU only (no GPU / Metal inside the VM). At one picture per 10 s per printer the load is a few
  percent of one core; the cost is RAM, not CPU.

## 4. Split edition: detector on another machine

**Detector machine** (any machine with Docker; amd64 or arm64). Copy `docker-compose.detection.yml`
and the `detector/adapter/` folder (or clone this repo), then:

```bash
echo "DETECTOR_TOKEN=$(openssl rand -hex 32)" > .env      # note this token: the Hub needs it
echo "DETECTOR_BIND=0.0.0.0" >> .env                      # listen on the network, not just localhost
docker compose -f docker-compose.detection.yml --profile detection up -d
curl http://localhost:8090/healthz                        # {"status":"ok","ml_api":"ok",...}
```

**Hub**: Settings -> Detection -> **Another machine**, URL `http://<detector-machine>:8090`, paste
the token, **Test connection**. The test answers in words whether the address or the token is the problem.

### The token flow
1. `DETECTOR_TOKEN` is chosen once and lives in two places: the detector box's `.env` and the
   Hub's settings (stored write-only, never shown again).
2. For every picture the Hub sends `Authorization: Bearer <token>`. The adapter checks it (constant
   time) **before reading the body**; wrong or missing token = `401`, no picture is read, nothing
   reaches the model.
3. The adapter uses a *different*, internal token (`DETECTOR_INTERNAL_TOKEN`, default = the same
   value) only on the private hop to the model container. The caller's token is never forwarded.
4. `/healthz` needs no token (so a monitor can use it), and reveals nothing but status.
5. The Hub's "Test connection" does both: `/healthz` (is it up?) and a stub picture (is the token
   right?): `DetectorClient.health()` and `.check_auth()`.

Security: plain HTTP is fine on a trusted home LAN, where the token and pictures are visible to
anyone on the network. Beyond one LAN put TLS in front (Tailscale, or a reverse proxy such as
Caddy) and **never expose the port to the internet**. Pictures are held in memory for the length of
one request and are not stored or logged by the detector.

## 5. The Hub engine (`app/detection.py`)

Stdlib only; imports nothing from `server.py`. Three parts:

| Class | Job |
|---|---|
| `DetectorClient(url, token)` | `score(jpeg, printer) -> ScoreResult`, `health()`, `check_auth()`. 10 s timeout, one request in flight, no proxy, no redirects, never raises. A failure is a `ScoreResult(ok=False, code=..., error=...)` with a token-free sentence. |
| `FailureTracker(printer, params, store_dir)` | One per printer: per-picture score in, `Verdict` (level OK / WARNING / FAILURE, plus the numbers) out. Baseline persisted in `<store_dir>/<printer>.json` (i.e. `/data/detection/<printer>.json`). |
| `DetectionLoop(fleet_fetcher, jpeg_fetcher, on_alert, client, ...)` | Polls only RUNNING printers that have a camera; backs off when the detector is down; calls `on_alert(printer, level, score, jpeg)`; `status()` and `score_once(printer)` for the dashboard / MCP. |

### Failure logic
Written from the published description of Obico's approach, independently (no Obico code). Per
picture with score `p`:

```
ewm      = p * a + ewm * (1 - a)            a = 2 / (ewm_span + 1); first picture: ewm = p
short    = mean of the last 310 scores of THIS print
long     = mean of the last 7200 scores of THIS PRINTER across prints (persisted)
adjusted(f) = (ewm - long) * sensitivity / f
failing(f)  = adjusted(f) > 0.78
              or (adjusted(f) >= 0.38 and adjusted(f) > (short - long) * 3.8)
level    = FAILURE if failing(1.75) else WARNING if failing(1.0) else OK
```
* The first **30 pictures** of every print (about 5 minutes) never alert, but still feed the baseline.
* The **long baseline** is what makes a printer that always scores a little high (a bed clip, a
  reflection) settle to "normal" over a few prints. It is saved at the end of each print, at most once
  a minute while printing, and reloaded after a Hub restart.
* A new print starts when a printer goes to RUNNING from anything but RUNNING / PAUSE, when the job
  name changes, or when progress drops by more than 10 points. A Hub restart in the middle of a print
  counts as a new print for warm-up purposes (the baseline is kept).
* PAUSE stops polling without resetting anything. Offline blips are ignored.
* Pictures that are tiny (< 2.5 KB, almost certainly a black frame because the chamber light is off)
  are skipped and reported as `camera: "blind"` rather than scored as zero. A camera that returns the
  same picture twice is `stale` and is scored once. (Heuristic; tune `blind_bytes`.)

### Parameters
`Params` defaults (tuned by the upstream project for one picture every 10 s; all configurable):

| Parameter | Default | Meaning |
|---|---|---|
| `ewm_span` | 12 | smoothing span |
| `short_window` / `long_window` | 310 / 7200 | rolling windows, in pictures |
| `warmup_frames` | 30 | pictures at the start of a print that never alert |
| `threshold_low` / `threshold_high` | 0.38 / 0.78 | adjusted score thresholds |
| `short_factor` | 3.8 | the "between thresholds" rule |
| `failure_factor` | 1.75 | how much stronger the signal must be for FAILURE than for WARNING |
| `sensitivity` | 1.0 | multiplies the adjusted score: **low 0.5, medium 1.0, high 2.0** |

Interval: 10 s by default; 15 s / 30 s allowed. `Params.for_interval()` scales `ewm_span`,
`short_window` and `warmup_frames` so they cover the same stretch of time (30 s -> span 4, warm-up
10). Unproven at other rates: treat 10 s as the supported setting until tested.

### Alert policy
* WARNING -> "possible spaghetti" with the latest picture. FAILURE -> the high-priority version.
* Per print: the first alert of a level is sent at once; the same or a lower level is repeated only
  after `cooldown_s` (default 600 s); a **higher** level (WARNING then FAILURE) is sent immediately.
* **Learn-only mode**: pictures are scored, smoothed and logged (`<store_dir>/scores.jsonl`, rotated at
  1 MB) but **no alert is sent**. Run it for the first prints, look at the numbers, then switch it
  off. The baseline builds up either way.
* A detector that is down is its own state (`detector.state == "down"`, summary "DETECTOR UNREACHABLE -
  prints are NOT being watched"). It is never shown as "all clear". Back-off is 10 s, 20 s, 40 s ...
  capped at 300 s; after each wait the loop asks `/healthz` before spending a camera picture.
* Status words are text (`OK`, `WARNING - possible spaghetti`, `FAILURE - likely spaghetti`,
  `WARMING UP`), never colour alone.

## 6. Settings

Section `detection` (group "Printers & Hub"), keys `detection.<name>`:

| Key | Type | Default | Notes |
|---|---|---|---|
| `mode` | enum `off` / `local` / `remote` | `off` | Off / This machine / Another machine |
| `url` | url | `""` | The detector's address. **Both modes**: in `local` mode an empty value means env `DETECTOR_URL`, else `http://detector:8080`; `remote` needs it |
| `token` | secret (write-only) | `""` | The detector's `DETECTOR_TOKEN`. In `local` mode an empty value means env `DETECTOR_TOKEN` |
| `interval_s` | enum `10` / `15` / `30` | `10` | only while a printer is RUNNING |
| `sensitivity` | enum `low` / `medium` / `high` | `medium` | |
| `learn_only` | bool | `true` | score and log, send no alert; turn off after a few prints |
| `cooldown_min` | int 1-120 | `10` | between repeated alerts in one print |
| `excluded_printers` | list | `[]` | e.g. a printer with a bad camera |

No `auto_pause` field exists or will: alert only.

Dashboard (Settings -> Detection): the three-way choice; address, token, interval, sensitivity, learn-only, cooldown and
per-printer exclusion appear once it is not Off; a status line for what the Hub is doing right now (connected / unreachable /
waiting); **Test connection**, which uses the values on the page, saved or not, and answers in words: connected, the detector
cannot be reached (address / port), it is up but not ready (the model is still loading), or it rejected the token. A running
print shows one line on its printer screen: `Watching - score 0.02 - learn-only`, `WARNING - possible spaghetti ...`,
`Camera sees almost nothing - turn the chamber light on`, `Detector unreachable - this print is NOT being watched`, `Off`,
`Off for this printer` or `No camera`. The fleet cards carry nothing extra (their layout is deliberately fixed).

## 7. How it is wired

* `settings_store.py`: the section above; `spaghetti_suspected`'s help text is updated.
* `events.py`: `EventWatcher.spaghetti_suspected(printer, score, image=None, level="failure")`. `level="warning"` gives the title
  `"<printer>: possible print problem (WARNING)"` and a warning-level message; `"failure"` keeps `"possible print FAILURE"`. The two levels
  have separate dedupe keys, so a WARNING followed by a FAILURE in one print is never swallowed as a repeat.
* `server.py`: `DETECTION = detection.DetectionLoop(_fleet_snapshots, _detection_frame, _detection_alert, ...)` is built on import (idle,
  no client) and **started only from `__main__`** (`DETECTION.start()`, and `DETECTION.stop` at exit so each printer's baseline is saved).
  `_apply_detection_settings` pushes mode, address, token, interval, sensitivity, learn-only, cooldown and exclusions into the loop on start-up and
  after every settings change; a new client (address / token changed) resets the back-off. Pictures come from the printer's
  **shared** camera reader (no second camera connection). `spaghetti_detected(printer, score, jpeg, level)` is the hook; the loop never
  calls it in learn-only mode.
* Routes: `GET /api/detection` (read); `POST /api/detection/test` and `POST /api/printer/{name}/detection/score` (control: they make the Hub
  contact an address / send a picture). The test builds its client from the form values in the request and never echoes the token.
  `GET /api/printer/{name}` carries `detection: {state, text}` for the printer screen.
* MCP: `detection_status` (read), `score_snapshot(printer)` (control).
* Learn-only scores are appended to `/data/detection/scores.jsonl` (rotated at 1 MB); baselines are `/data/detection/<printer>.json`.

Tests: `tests/test_wiring2.py` runs the loop against a fake adapter on `127.0.0.1` (learn-only logs scores and sends nothing; learn-only
off sends a notification to a fake ntfy; a down detector is never "all clear"; the token never appears in any answer; nothing ever pauses
or cancels a fake printer).

## 8. Upgrading the model image / licence

The compose file pins `obico-ml-api-cpu:2026.10.2@sha256:e1b1c5b4...cb86ee`. The digest was
re-checked against the upstream registry on 2026-10-11 and matches the research report. To move to a
newer release: pick the new tag on <https://github.com/TheSpaghettiDetective/obico-server/pkgs/container/obico-ml-api-cpu>, read its digest
(`docker buildx imagetools inspect <image>:<tag>`), and change both in `docker-compose.detection.yml`.
If upstream ever removes the image, it can be built from `ml_api/Containerfile.cpu` in the upstream
repository (`docker build -f ml_api/Containerfile.cpu -t obico-ml-api:cpu ml_api`).

Licence position (not legal advice): the Hub and adapter are MIT with no Obico code; the model runs as
an unmodified upstream container pulled from upstream's registry at install time and is reached only
over HTTP. Do not copy `ml_api` or Obico's `prediction.py` into this repository, and do not publish a
modified image. A short legal review before promoting the add-on to strangers is cheap insurance.
Leave `SENTRY_DSN` unset (it would report errors to a third party).

## 9. Real-hardware test plan

Run these on the real Mac (replace `<repo>` and the printer name).

```bash
cd "<repo>"
T=$(openssl rand -hex 32); echo "DETECTOR_TOKEN=$T" >> .env      # skip if already set
C="docker compose -f docker-compose.yml -f docker-compose.detection.yml --profile detection"

colima status                         # expect arch aarch64; memory 6 GiB (see section 3)
$C config -q && echo parses
$C up -d --build
$C ps                                 # detector-ml: starting -> healthy (up to ~2 min), detector: healthy
```

| # | Check | How | Pass |
|---|---|---|---|
| 1 | Native arm64, no emulation | `docker image inspect --format '{{.Architecture}}' ghcr.io/thespaghettidetective/obico-ml-api-cpu:2026.10.2` | `arm64` |
| 2 | The `python`-based healthcheck works in the upstream image | `$C ps`; `docker inspect --format '{{json .State.Health}}' $($C ps -q detector-ml)` | `healthy` (if it never goes healthy the healthcheck, not the model, is the suspect) |
| 3 | Adapter health | `curl -s http://127.0.0.1:8090/healthz` | `"status":"ok","ml_api":"ok"` |
| 4 | Token required | `curl -s -o /dev/null -w '%{http_code}' -X POST --data-binary @frame.jpg http://127.0.0.1:8090/v1/score` and again with `-H "Authorization: Bearer wrong"` | `401` both |
| 5 | A real picture scores | `curl -s http://127.0.0.1:8271/api/printer/<name>/snapshot -o frame.jpg` (add the Hub API key header if the key check is On), then `curl -s -X POST -H "Authorization: Bearer $T" -H "Content-Type: image/jpeg" --data-binary @frame.jpg http://127.0.0.1:8090/v1/score` | JSON with `score`, `ms` |
| 6 | Latency | repeat #5 about 100 times (`for i in $(seq 100); do curl ... -w '%{time_total}\n' -o /dev/null; done`) | record p50 / p95; expect well under 10 s |
| 7 | Limits | POST a non-JPEG; POST a 6 MB file | `415` / `413` |
| 8 | Model is NOT reachable except through the adapter | `curl -m 3 http://127.0.0.1:3333/hc/` (host) and `docker compose exec fabrica python -c "import urllib.request;urllib.request.urlopen('http://detector-ml:3333/hc/',timeout=3)"` | both fail |
| 9 | No leftover pictures, one-time URLs | `$C logs detector` after #5/#6: only score lines, no errors; there is nothing on disk (`$C exec detector ls /tmp /app`) | clean |
| 10 | Failure handling | `$C stop detector-ml`, then repeat #5 | `502 detector_unreachable`; `/healthz` 503 `degraded`; restart it and it recovers |
| 11 | RAM | `docker stats --no-stream` while looping #6, with Colima at 4 GB and at 6 GB | `detector-ml` under its 1.5 GB cap, no OOM kills (`docker inspect ... .State.OOMKilled` false), Hub unaffected |
| 12 | Without the profile | `docker compose -f docker-compose.yml -f docker-compose.detection.yml up -d` | only the Hub starts, no warnings |
| 13 | Learn-only on a real print (after wiring) | Settings: This machine, learn-only on; run a normal print | `data/detection/scores.jsonl` fills with scores; typical healthy score near 0; `data/detection/<printer>.json` appears at print end; no alerts |
| 14 | Known-bad pictures | feed pictures of a failed print (your own, or from the Obico project) to #5 | clearly higher scores than healthy ones; use them to pick sensitivity |
| 15 | Alert path | learn-only off, with a deliberately bad picture or a lowered threshold | one WARNING/FAILURE alert with a picture; cooldown respected; no printer command is ever sent |
| 16 | Split | detector on a second machine / subnet with `DETECTOR_BIND=0.0.0.0`, Hub on the Pi: Test connection, then a print | works with no `.local` DNS dependence; Hub CPU / RAM steady at 10 s polling |
| 17 | Long run | a 12 hour print | flat RSS in `docker stats`; a Hub restart mid-print resumes (new warm-up, baseline kept) |
| 18 | Other cameras | OctoPrint webcams (full-resolution snapshots) | pictures under 5 MB score; larger ones need the downscale path (below) |

Tear down: `$C down` (add `--rmi local` to drop the adapter image).

## 10. Known limits

* One class only ("failure"): a part that detaches and disappears, a clog or a stall is invisible.
  Use the printer's own telemetry (layer progress, errors) alongside it.
* Chamber light off = black picture; handled as `blind`, not as "all clear".
* The Hub sends the camera picture as it is. Bambu pictures (about 1280x720) are comfortably inside
  the 5 MB limit; downscaling to about 640 px would cut bandwidth and is a possible later addition (the
  Hub has no imaging library today, and the model squashes everything to 416x416 anyway).
* Accuracy on a P1S / A1 camera is not yet measured: that is what learn-only mode and tests 13-14 are for.
* CPU only; an Obico model quirk scales box heights by picture width, which is irrelevant for scores
  and alerts but means boxes should not be drawn on pictures without checking.
* Plain HTTP between Hub and a remote detector: see section 4.
* PrintGuard (GPL-2.0, separate project) is a possible alternative backend behind the same adapter
  contract; it is not implemented.
