# Troubleshooting

Find your symptom, apply the fix. If nothing helps, [read the logs](#reading-the-logs) and
[report a bug](#reporting-a-bug).

## Install and start

| Symptom | Cause | Fix |
|---|---|---|
| `docker compose up` fails with an error mentioning `env_file` | The compose files mark `.env` as optional, which needs Docker Compose **2.24 or newer** | `docker compose version`; update Docker |
| Cannot open `http://<host>:8271/dashboard` | Container not running, wrong host or port, firewall | `docker compose ps` should say `Up`. On the host, `curl http://localhost:8271/api/health` should print `{"ok": true}`. From another machine use the host's IP, not `localhost`. Open port 8271 in the host firewall. If you set `BAMBU_MCP_PORT`, change the `ports:` mapping to match |
| Port 8271 already in use | Another program has it | Change the left side of the mapping, e.g. `"8280:8271"` |
| Works on the host but not on a phone or laptop | Different network or VLAN, guest Wi-Fi, firewall | Use the same network, or a VPN such as Tailscale |
| Container restarts in a loop | Varies | `docker logs --tail 100 fabrica` shows why |

## Access and security

| Symptom | Cause | Fix |
|---|---|---|
| <a id="401-api-key-required"></a>**401** `{"error": "API key required"}` from `/mcp` or `/api/*` | The API key check is On (the default on a fresh install) and the request has no valid key | Create a key (**Settings > Security > Generate key**) and send it as `Authorization: Bearer <key>` or `X-Api-Key: <key>`. See [agents.md](agents.md). Keys do not work in a URL query string |
| `REFUSED: this API key has 'read' access` | The key is read-only and the tool changes something | Use a `control` key for that agent, or do the action yourself |
| <a id="host-name-rejected"></a>Dashboard loads but actions fail with 401 only when opened through a custom name | DNS-rebinding guard: it accepts IP addresses, `localhost`, single-label names and private-style suffixes (`.local`, `.lan`, `.home.arpa`, `.internal`, `.ts.net`, ...). A proxy name such as `fabrica.example.com` is not on the list | Add it to `.env`: `BAMBU_MCP_ALLOWED_HOSTS=fabrica.example.com`, then `docker compose up -d`. Or open the dashboard by IP address |
| `cross-site request refused` behind a proxy | Writes from the dashboard must come from its own origin; the proxy changed `Host` or `Origin` | Make the proxy pass the original `Host`, and open the dashboard through the name you configured |
| Signed out everywhere | Changing or clearing a password signs affected browsers out (by design) | Sign in again |
| An agent that worked before gets 401 after an update | An upgraded install starts in log-only `warn` mode once; the key check was later set to On | Give the agent a key. Settings > Security lists callers that came without one |

### Lost the dashboard or admin password

On the machine running Fabrica (anyone with shell access can do this, so keep that machine trusted):

```bash
docker exec fabrica python /app/security.py list-keys
docker exec -it fabrica python /app/security.py reset-dashboard-password
docker exec -it fabrica python /app/security.py reset-admin-password
docker exec fabrica python /app/security.py disable-dashboard-login   # turn sign-in off
docker exec fabrica python /app/security.py create-key "Recovery" --scope control
```

Revoke any recovery key afterwards under **Settings > Security**.

## Printers

| Symptom | Cause | Fix |
|---|---|---|
| **Bambu:** start says "sent" but nothing happens | The printer ignores local print-start unless it is in the right mode | Switch on **LAN Only mode** (and **Developer Mode** on recent firmware). The Add printer wizard shows the steps for your model |
| **Bambu:** "wrong access code", or it worked yesterday | Toggling LAN Only or Developer Mode can regenerate the access code | Read the code on the printer and enter it under **Printers > Edit** |
| **Bambu:** drops the connection | Wrong code, or another client holds the printer's local connection | Check the code; close slicers or a second Hub that may be connected; test again. Fabrica keeps one connection per printer |
| "Can't find ..." / "no answer" when testing | Wrong or changed IP, printer off, different network or VLAN | Use a current IP (set a DHCP reservation) and make sure Fabrica and the printer share a network. Prefer IP addresses over `.local` names: those resolve only on the same subnet |
| **OctoPrint:** key rejected | Key revoked or mistyped | Create a new key in OctoPrint under *Application Keys* and paste it again |
| **OctoPrint:** NOT CONNECTED | OctoPrint is up but its USB link to the printer is not | Power the printer on, or ask an agent to run `connect_printer` |
| **PrusaLink:** authentication failed | Wrong username or password | Re-read them in the printer's network settings |
| **Klipper:** "wants an API key" | Moonraker does not trust Fabrica's network | Add the network to `[authorization] trusted_clients` in `moonraker.conf` and restart Moonraker, or enter an API key |
| **Klipper:** NOT CONNECTED | Klipper itself reports `shutdown`, `error` or `disconnected` | Fix the cause in Mainsail/Fluidd, then tap the chip: Fabrica asks "Restart Klipper?" (a FIRMWARE_RESTART) and only restarts after your second tap. It never restarts a ready or printing printer |
| A printer shows the last state and "Reconnecting…" | The Hub missed a poll or two (Wi-Fi hiccup, a busy Pi). Nothing is offered to connect, nothing is sent | Wait; after about 30 seconds without an answer it shows OFFLINE |
| **Discover printers** finds nothing | Inside Docker, Fabrica probes only the networks you list | Type your network (for example `192.168.1.0/24`) and scan. Only private IPv4 ranges up to a /22 are allowed. See [discovery.md](discovery.md) |
| Controls (pre-heat, load, unload) missing or refused | The printer family cannot do it, or it is busy | The printer screen shows why; they are refused while printing or paused |

**Cameras.** Bambu P1 and A1 printers show their camera automatically; for X1 and H2 series (RTSP) the
camera is not supported, everything else works. OctoPrint needs a webcam set up in OctoPrint. PrusaLink and
Klipper can use a snapshot URL or the webcam from Mainsail/Fluidd; set it in the printer's settings.

## Detection (optional add-on)

See [detection.md](detection.md) for the full guide.

| Symptom | Cause | Fix |
|---|---|---|
| Detector "starting" or `unhealthy` for a while | The model takes up to two minutes to load; the first start also downloads it | Wait; `docker compose ps` should show `healthy`. Otherwise `docker compose logs detector-ml` |
| Detector exits at once | `DETECTOR_TOKEN` is empty | `echo "DETECTOR_TOKEN=$(openssl rand -hex 32)" >> .env`, then `up -d` again |
| Out-of-memory kills, or the Hub stalls (Mac) | Docker runs in a VM with a small default memory limit | Give Docker Desktop or Colima about 6 GB (`colima start --cpu 2 --memory 6`) |
| "DETECTOR UNREACHABLE" on the dashboard | Detector down, wrong address, or wrong token | **Settings > Detection > Test connection** says which |
| Hub in Docker cannot reach a detector on the host | `localhost` inside a container is the container | Use `http://host.docker.internal:8090`; see [detection.md](detection.md) |

## Data and updates

| Symptom | Cause | Fix |
|---|---|---|
| Printers or settings vanish after a restart | `/data` is not a persistent volume | Keep `- ./data:/data` under `volumes:` and run compose from the same folder every time |
| Errors writing files | `./data` is read-only, on a filesystem without Unix permissions (some network shares, exFAT), or blocked by SELinux | Keep `./data` on a normal local disk; on SELinux hosts append `:z` to the volume. `docker logs fabrica` has the exact error |
| An old page is shown after updating | Browser cache | Hard-refresh (Ctrl/Cmd+Shift+R) |
| `pull access denied` for the image | Wrong image name | Use `ghcr.io/wsturner88/fabrica` |

## Reading the logs

```bash
docker logs --tail 200 fabrica          # last 200 lines
docker logs -f fabrica                  # follow live
docker compose ps                       # state and health of each container
```

Check a log excerpt for secrets before you paste it.

## Reporting a bug

Open an issue: <https://github.com/wsturner88/fabrica/issues/new/choose>. Please include what you did and
expected, the Fabrica version (**Settings > Agents / MCP > About**), your edition (Lite, Full or Split), host
OS and architecture, Docker versions, printer family, model and firmware, and the last 100 to 200 log lines.

**Redact before pasting:** access codes, API keys, passwords, `DETECTOR_TOKEN`, notification tokens and
webhook URLs, serial numbers, real IP addresses and host names (use `192.168.1.x`). Never attach your
`data/` folder or `.env`.

Security problems: do not open a public issue; see [SECURITY.md](../SECURITY.md).
