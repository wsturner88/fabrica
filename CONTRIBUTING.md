# Contributing to Fabrica

Thanks for helping. Fabrica is a self-hosted 3D-printer fleet manager and MCP server ("printer
management, agent-first."). Bug reports, printer-family support, docs and code are all welcome. Please
read the [Code of Conduct](CODE_OF_CONDUCT.md) first.

- **Small fix or doc change:** open a pull request directly.
- **New feature or printer family:** open an issue first (*Feature request* or *Printer support request*).
- **Security problem:** do **not** open a public issue. See [SECURITY.md](SECURITY.md).

**Never post** access codes, API keys, passwords, tokens, printer serial numbers, real IP addresses or host
names, or e-mail addresses in issues, PRs, logs or screenshots. Use placeholders such as `<ACCESS_CODE>` and
`192.168.1.x`. If you pasted one by mistake, rotate it and edit the post.

## Dev setup

Python 3.12 or newer.

```bash
git clone https://github.com/wsturner88/fabrica.git
cd fabrica
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt        # what the Docker image installs

# Keep all state in a throwaway folder, never next to a real install
export TMPDATA="$(mktemp -d)"
export BAMBU_MCP_CONFIG="$TMPDATA/none.yml"             # a file that does not exist = no legacy config
export BAMBU_MCP_CACHE="$TMPDATA/data/metacache.json"   # printers, keys and logs live beside this
export BAMBU_MCP_HOST=127.0.0.1                         # keep dev instances local
python app/server.py
```

Open <http://127.0.0.1:8271/dashboard>. Or use Docker: `docker compose up -d --build`. Details of the
drivers' contract are in [docs/drivers.md](docs/drivers.md).

## Tests

Tests use the standard-library `unittest`, fake printers and servers on `127.0.0.1`, and need no network
beyond loopback. **Never test against a printer you care about.**

**Run each test file in its own process** (several files set process-wide state, so one big run leaks
between them):

```bash
export BAMBU_MCP_CONFIG="$(mktemp -d)/none.yml"
export BAMBU_MCP_CACHE="$(mktemp -d)/metacache.json"
for f in tests/test_*.py; do python "$f" || break; done
```

Add or update tests with every behaviour change; use temp directories for all state.

## Style

- Match the surrounding code. No formatter is enforced.
- Standard library first. Runtime dependencies are deliberately few (`mcp`, `paho-mqtt`, `pyyaml`); discuss
  a new dependency in an issue before adding one.
- Network code needs timeouts and must never log secrets or return them in an API response, tool result or
  error. Secrets are write-only.
- Anything that moves hardware or starts/cancels a print needs the `control` scope and a safety gate. New
  REST routes and MCP tools must be listed in the scope tables in `app/security.py` (unlisted ones default
  to `control`).
- Dashboard: touch-friendly, usable at phone width and 1024x600, status always spelled out in words (never
  colour alone), no external scripts or fonts.
- Do not copy code from other projects unless its licence is MIT-compatible, and record it in
  [THIRD_PARTY.md](THIRD_PARTY.md). Never copy Obico (AGPL-3.0) code into the Hub or adapter.

## Pull requests

Branch from `main`, one logical change per PR, short imperative subject (for example
`fix(backup): skip empty folders`). Fill in the PR template. Checklist:

- [ ] Each test file passes when run on its own; new behaviour has tests
- [ ] Docs updated, and a line added under *Unreleased* in [CHANGELOG.md](CHANGELOG.md) for user-visible changes
- [ ] No personal info, real IP addresses, host names, serial numbers or secrets in code, tests, docs or screenshots
- [ ] UI changes include a phone-width and 1024x600 screenshot

By contributing you agree that your work is licensed under the project's [MIT licence](LICENSE).
