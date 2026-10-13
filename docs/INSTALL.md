# Install Fabrica

About 10 minutes. Pick your edition first:

| | 🪶 **Lite** | 💪 **Full** |
|---|---|---|
| Runs on | Raspberry Pi 4 (2 GB+), or any machine with Docker | Mac, PC or NAS with Docker (≈ 1.5 GB free RAM) |
| Everything in Fabrica | ✅ | ✅ |
| Spaghetti detection | — (or borrow a Full machine's detector) | ✅ built in |

## 1. Install Docker
- **Mac / Windows:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- **Linux / Raspberry Pi:** `curl -fsSL https://get.docker.com | sh`

Check you have Compose 2.24 or newer: `docker compose version`

## 2. Get Fabrica
```bash
git clone https://github.com/wsturner88/fabrica.git
cd fabrica
```

## 3. Start it

**🪶 Lite**
```bash
docker compose -f docker-compose.ghcr.yml up -d
```

**💪 Full** (Fabrica + spaghetti detection)
```bash
echo "DETECTOR_TOKEN=$(openssl rand -hex 32)" >> .env
docker compose -f docker-compose.ghcr.yml -f docker-compose.detection.yml --profile detection up -d
```
The detector takes about two minutes to load the first time.
Then turn it on: **Settings → Detection → This machine**.

## 4. Open the dashboard
`http://<your-machine>:8271/dashboard` (on the same machine: `http://localhost:8271/dashboard`)

Open the dashboard right after starting it — the first person to open it sets the password.

## 5. Add your printers
Tap **Add your first printer** (or **Discover printers** to scan your network), pick the kind
(Bambu Lab · Prusa · OctoPrint · Klipper), fill in the form, **Test connection**, **Save**.
Each kind shows its own numbered "before you start" steps, so you know exactly where to find the
access code or API key.

## 6. Connect your AI agent (optional)
1. **Settings → Security → New key** — give it a name, copy the key.
2. Add the server to your agent. Claude Code:
   ```bash
   claude mcp add --transport http fabrica http://<your-machine>:8271/mcp --header "Authorization: Bearer <your key>"
   ```
   Any other MCP client: streamable HTTP, same address, same header.

## 7. Put it on the wall (optional)
The dashboard is made for a **10″ 1024×600 touchscreen**. Open it full-screen in a browser
(kiosk mode) on a Raspberry Pi with a touchscreen, an old tablet, or a spare monitor by the printers.
On **Lite**, the Pi running Fabrica can drive its own touchscreen.

---
✅ Done. Keep it on your own network, or reach it from anywhere with Tailscale — never open it to the internet.

**Bambu printers:** they need **LAN Only mode** (and **Developer Mode** on newer firmware) before
Fabrica can start prints — see [Firmware compatibility](GUIDE.md#️-firmware-compatibility--read-this-first).

**More:** [Full guide](GUIDE.md) · [Spaghetti detection](detection.md) · [Settings](settings.md) ·
[Printer types](drivers.md) · [Print history & costs](joblog.md)
