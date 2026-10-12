# Lite edition on a Raspberry Pi

**Lite** is Fabrica's Hub on its own: dashboard, MCP server, print log, backups and alerts. It runs on a
small always-on box such as a **Raspberry Pi 4 (2 GB or more)**. Spaghetti detection is not part of Lite;
see [Detection options](#detection-options).

A **10" 1024x600 touchscreen** on the same Pi makes a good wall-mounted kiosk: open
`http://localhost:8271/dashboard` full-screen in a browser.

## Requirements

| | |
|---|---|
| Board | Raspberry Pi 4 (2 GB+) or newer |
| OS | A **64-bit** OS (Raspberry Pi OS 64-bit, or Ubuntu Server arm64). The published image is `linux/amd64` and `linux/arm64` only, so 32-bit (armv7) cannot run it |
| Docker | Docker Engine with **Compose 2.24 or newer** |
| Storage | Ideally an SSD or good USB drive for the data folder |
| Network | Same network as your printers; Ethernet is more reliable than Wi-Fi |

Check for a 64-bit system: `uname -m` should print `aarch64`.

## Install

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER            # then log out and back in
docker compose version                   # must be 2.24 or newer

mkdir -p ~/fabrica && cd ~/fabrica
curl -fsSLO https://raw.githubusercontent.com/wsturner88/fabrica/main/docker-compose.ghcr.yml
docker compose -f docker-compose.ghcr.yml up -d
```

The image is `ghcr.io/wsturner88/fabrica`; Docker picks the arm64 build automatically. Open
`http://<pi-address>:8271/dashboard` from another computer, add your printers, and set a dashboard
password when prompted. See also [INSTALL.md](INSTALL.md). No `.env` or config file is needed;
`restart: unless-stopped` brings Fabrica back after a reboot or power cut.

## SD-card care

SD cards wear out under frequent small writes. Fabrica is light, but it logs every print and caches
thumbnails, so keep its data off the card where you can.

1. **Put the data folder on an SSD or USB drive.** Mount it (for example at `/mnt/ssd`) and change the
   volume line in your compose file to `- /mnt/ssd/fabrica-data:/data`. Stop Fabrica first
   (`docker compose -f docker-compose.ghcr.yml down`), copy the old `./data` across
   (`sudo cp -a ./data/. /mnt/ssd/fabrica-data/`), then `up -d`.
2. **Better still, boot the Pi from the SSD**, so Docker's own files and logs leave the card too.
3. **Keep backups small:** lower `BACKUP_KEEP` in `.env` (default 5 zips per printer) and copy the zips elsewhere.
4. **Cap Docker's log size** by adding to the service in your compose file:
   ```yaml
   logging:
     driver: json-file
     options: { max-size: "10m", max-file: "3" }
   ```
5. **Back up `./data` regularly** (stop the container, copy the folder). It holds credentials, so treat the
   copy like a password file.

## Detection options

| Choice | How |
|---|---|
| **Off** (Lite default) | Do nothing: **Settings > Detection** stays Off |
| **Another machine** | Run the detector on a PC, Mac, NAS or stronger machine with Docker, then in **Settings > Detection > Another machine** enter its address and token. See [detection.md](detection.md) |

Do not run the detector on a small Pi: the model needs roughly 1 GB of RAM and seconds per picture.
Detection is alert-only and never stops a print.

## A stable address

Agents, bookmarks and the kiosk all point at the Pi, so its address must not change. Give the Pi (and each
printer) a **DHCP reservation** in your router. Prefer IP addresses over `.local` names: `.local` names
resolve only on the same subnet.

## HTTPS and remote access

Plain `http://` is fine on a trusted home LAN. Otherwise use **Tailscale** (`tailscale serve --bg 8271`, with
the port published as `127.0.0.1:8271:8271`) or a reverse proxy, and see [agents.md](agents.md#https).
Never expose port 8271 to the internet.

## Updates

```bash
cd ~/fabrica
docker compose -f docker-compose.ghcr.yml pull && docker compose -f docker-compose.ghcr.yml up -d
```

To pin a version use `image: ghcr.io/wsturner88/fabrica:1.0.0` instead of `:latest`. Your data stays in the
`/data` volume. Read the [CHANGELOG](../CHANGELOG.md) first. Problems: [troubleshooting.md](troubleshooting.md).
