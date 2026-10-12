# Security policy

Fabrica controls physical machines (heaters, motors, print jobs) and stores printer credentials, so
security reports are taken seriously.

## Supported versions

| Version | Supported |
|---|---|
| 1.x | Yes: security fixes go into the latest 1.x release |
| < 1.0 | No (pre-1.0 versions were never released) |

Please reproduce on the latest release before reporting.

## Reporting a vulnerability

**Please do not open a public issue.** Use GitHub private vulnerability reporting:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**
   (<https://github.com/wsturner88/fabrica/security/advisories/new>).

Include the Fabrica version (Settings > Agents / MCP > About), the edition (Lite / Full / Split), how you
installed it, what you did, what happened and the impact. Please **redact** real access codes, API keys,
serial numbers and IP addresses; use placeholders.

This is a small volunteer project: expect an acknowledgement within about a week and a fix or mitigation
as soon as it can be made safely. Reporters are credited in the advisory unless they prefer to stay
anonymous. If you test only on your own installation and printers and give reasonable time to fix the
issue before disclosing it, we will not pursue or support legal action over your research.

## Scope

**In scope**

- Authentication or authorisation bypass: API-key checks (`read` / `control` scopes), dashboard sign-in
  and sessions, the admin password.
- Weaknesses in API-key handling (generation, storage, comparison, logging).
- DNS-rebinding or Host-header guard bypass; cross-site request forgery against the dashboard.
- Server-side request forgery in printer discovery, spaghetti detection or "test connection".
- Path traversal in backups, downloads, file listings or uploads.
- Secret leakage in any `/api/*` output, MCP tool result, log line, export or error message.
- A way for a `read` key to change anything, or for an agent to create keys or change security settings
  through MCP.
- Needless privilege or exposure in the provided Dockerfiles and compose files.

**Out of scope**

- Attacks that need access to the Hub's network when the dashboard is deliberately left without
  sign-in. That is the documented trust model (below).
- Vulnerabilities in printer firmware or third-party software (Bambu Lab, Prusa, OctoPrint, Klipper,
  Moonraker): report them to their vendors.
- Issues in the Obico model container: report them to the
  [Obico project](https://github.com/TheSpaghettiDetective/obico-server). How Fabrica deploys it is in scope.
- Findings that need physical access to the host, a compromised host, or a valid `control` key.
- Denial of service by someone who already has network access; scanner output without a working proof
  of concept.

## Security model in brief

- **Local only, no telemetry.** Fabrica talks to your printers over your LAN and to the notification
  services you configure. It sends nothing to the project or to analytics services.
- **API keys.** The MCP endpoint and the REST API need a key on fresh installs. Keys are named, have a
  `read` or `control` scope, are shown once, and are stored only as a salted hash. Keys are never accepted
  in a URL query string. An agent cannot create or revoke keys or change security settings through MCP.
- **Dashboard sign-in (optional).** Turn on a dashboard password (and an optional view-only password) under
  Settings > Security. Sessions and trusted devices can be listed and revoked, and attempts are
  rate-limited. A brand-new install shows a first-run prompt offering to protect the dashboard. Key and
  security administration needs a separate admin password.
- **Secrets are write-only.** Printer access codes, API keys and passwords are never returned by any page,
  REST call or MCP tool.
- **Trust model.** Without a dashboard password, the dashboard is protected only by your network: anyone
  who can reach its port can operate the printers through it. Fabrica is built for a home, shop or VPN
  network you trust. **Do not expose it to the internet.**
- **Spaghetti detection (optional)** is alert-only and never pauses or stops a print. The model container
  has no published port and sits on an internal Docker network.

## Hardening checklist

- [ ] Set a **dashboard password** (Settings > Security), especially on shared networks.
- [ ] Keep Fabrica on your **LAN or a VPN**. Never port-forward it or put it on a public tunnel.
- [ ] Use **HTTPS** if the network is not fully trusted (for example `tailscale serve --bg 8271`, or a
      reverse proxy with a certificate); keys and cookies travel in clear over plain `http://`.
- [ ] Publish the port as `127.0.0.1:8271:8271` when a proxy or VPN is in front.
- [ ] Give each agent its **own key** with the lowest scope it needs (`read` for monitoring) and revoke
      keys you no longer recognise.
- [ ] Use a long random `DETECTOR_TOKEN` (`openssl rand -hex 32`) and never publish the model container's port.
- [ ] Keep the data folder (`/data`) private and back it up like a credential store.
- [ ] Update regularly: pull the latest image and read the [changelog](CHANGELOG.md).
