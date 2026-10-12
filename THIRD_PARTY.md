# Third-party software and notices

Fabrica's own code (the Hub in `app/`, the detector adapter in `detector/adapter/`, the brand assets in
`app/static/brand/`, and the documentation) is original to this project and released under the
[MIT licence](LICENSE). This page lists what it depends on, what it merely talks to, and the trademarks
of other parties it names. Versions are resolved when the image is built; check the installed package
metadata for exact versions. This page is not legal advice.

## 1. Python dependencies of the Hub (installed from PyPI)

The Hub image installs three direct dependencies: `pip install "mcp>=1.9,<2" paho-mqtt pyyaml`.

| Package | Licence | How it is used |
|---|---|---|
| [mcp](https://github.com/modelcontextprotocol/python-sdk) (Model Context Protocol Python SDK) | MIT | MCP server (streamable HTTP) |
| [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) | EPL-2.0 OR BSD-3-Clause (dual) | MQTT client for Bambu Lab printers in LAN mode |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | Reads the optional legacy `config.yml` |

`mcp` brings in further packages (for example starlette, uvicorn, pydantic, anyio, httpx), all under
permissive licences (MIT, BSD-3-Clause, Apache-2.0); `pip show <name>` lists them. Nothing is modified or
vendored in this repository. The detector adapter uses only the Python standard library.

## 2. Base image

The images are built `FROM python:3.12-slim`: Debian GNU/Linux (software under many licences, see
<https://www.debian.org/legal/licenses/>) with CPython under the
[Python Software Foundation licence](https://docs.python.org/3/license.html).

## 3. Spaghetti detector: Obico model container (AGPL-3.0)

The optional spaghetti detector uses a model container made by the Obico project.

- It is the **unmodified upstream image** `ghcr.io/thespaghettidetective/obico-ml-api-cpu`, pinned by
  digest in `docker-compose.detection.yml`.
- It is **pulled by your own Docker daemon from the upstream registry** when you start the detection
  profile. **This project does not build, bundle, copy, re-host or redistribute it**, and it is not part
  of the `ghcr.io/wsturner88/fabrica` or `ghcr.io/wsturner88/fabrica-detector` images.
- Fabrica talks to it **over HTTP only**, through its own adapter, on a private Docker network. No Obico
  code is imported, linked, copied or modified.
- Upstream source: <https://github.com/TheSpaghettiDetective/obico-server>, licensed under the
  [GNU AGPL-3.0](https://github.com/TheSpaghettiDetective/obico-server/blob/release/LICENSE).
- **If you modify the model image or redistribute it (or a derivative) to others, the AGPL-3.0
  obligations apply to you.** Running the unmodified image for yourself, as the compose file does, does
  not change Fabrica's licence.
- Obico's optional error reporting is left unset, so nothing is sent to a third party.
- The upstream image contains model weights; check the upstream project for the terms that apply to them.

## 4. Fonts

The dashboard and logo SVGs name **IBM Plex Mono** (with system monospace fallbacks) but **do not bundle or
download it**: it is used only if already installed on the viewer's device. If it were ever bundled, it is
licensed under the [SIL Open Font Licence 1.1](https://github.com/IBM/plex/blob/master/LICENSE.txt).

## 5. Brand assets

The Fabrica name, logo, icons and the SVG/PNG files in `app/static/brand/` are original to this project
and released under the MIT licence with the rest of the repository.

## 6. Trademarks

Bambu Lab, Prusa (Prusa Research), OctoPrint, Klipper, Moonraker, Obico, Tailscale, Telegram, Discord,
ntfy, MQTT, Docker, Raspberry Pi and all other product and company names are trademarks or registered
trademarks of their respective owners. They are used only to describe compatibility (nominative use).
Fabrica is an independent project and is **not affiliated with, endorsed by, or sponsored by** Bambu Lab,
Prusa Research, OctoPrint, Klipper or Obico or any other party named here.
