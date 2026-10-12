# Connect an AI agent

Fabrica speaks **MCP over streamable HTTP**. Any MCP client that can send an HTTP header can use it.

| You need | Value |
|---|---|
| URL | `http://<host>:8271/mcp` (port set by `BAMBU_MCP_PORT`, default `8271`; for HTTPS see [below](#https)) |
| Header | `Authorization: Bearer <your key>` (`X-Api-Key: <your key>` also works for tools that cannot set `Authorization`) |
| Key | Dashboard **Settings > Security > Generate key** (shown once), or `docker exec fabrica python /app/security.py create-key "Agent" --scope read` |

Replace `<host>` with the machine's address (`localhost` on the same machine). Keys are never accepted in a
URL query string. After connecting, ask the agent "list my printers". A 401 means the key is missing or
wrong: see [Troubleshooting](troubleshooting.md#401-api-key-required).

## Which key scope to give

| Scope | The agent can | Use it for |
|---|---|---|
| **read** | look: status, files, AMS spools, camera pictures, history, costs, settings (secrets redacted), plan a print with `suggest_mapping` | Monitoring and cost questions. **Start here.** |
| **control** | everything, including starting, pausing and cancelling prints, heaters, deleting files, adding or removing printers, changing settings | An agent you trust to act, with you watching |

A `read` key that calls a control tool gets a plain `REFUSED: this API key has 'read' access ...`. Make one
key per agent so you can revoke it alone. No MCP tool can create or revoke keys or change security
settings, by design.

## Tools

The scope of every tool is defined in `TOOL_SCOPES` in `app/security.py`.

| Group | Scope | Tools |
|---|---|---|
| Look | read | `list_printers`, `printer_status`, `ams_state`, `list_sd_files`, `get_camera_snapshot`, `printer_controls`, `list_printer_types`, `get_settings` |
| Plan | read | `suggest_mapping` |
| History and cost | read | `recent_jobs`, `job_stats`, `cost_per_part` |
| Backups and detection (status) | read | `backup_status`, `detection_status` |
| Print | control | `start_print`, `pause_print`, `resume_print`, `cancel_print`, `connect_printer` |
| Printer controls | control | `set_light` (alias `chamber_light`), `preheat`, `cooldown`, `unload_filament`, `load_filament`, `set_tray` |
| Files and backups | control | `delete_sd_file`, `backup_printer_files` |
| Fleet | control | `test_printer_connection`, `add_printer`, `update_printer`, `remove_printer`, `discover_printers` |
| Settings and alerts | control | `update_settings`, `send_test_notification`, `list_api_keys` (list only), `score_snapshot` |

## Example prompts

- "List my printers and tell me which are idle." (read)
- "What is loaded in the AMS on printer-1, by colour name?" (read)
- "Show me a camera picture of the print on printer-1. Does it look healthy?" (read)
- "What did I print this month, and what did it cost?" (read)
- "Print `Bracket-v3.3mf` on printer-1. Tell me the spools and the time first and wait for my yes." (control)
- "Back up the files on every printer." (control)

## The confirm flow

The agent cannot start a job it has not described to you first:

1. `suggest_mapping(printer, filename)` works out the spool plan and any warnings, and returns a
   `confirm_string`: a single-use code, valid for 5 minutes, tied to that printer, file and tray mapping.
2. The agent shows you the plan and asks whether the build plate is clear.
3. `start_print` is **refused** without that confirm string and without
   `bed_clear_confirmed_by_human=true`, which is *your* statement that the plate is clear. It is also refused
   if the file was sliced for a different printer model (unless you explicitly override), and while the
   printer is busy or not connected.
4. `delete_sd_file` needs `confirm` equal to the file name. `update_printer` and `remove_printer` refuse
   while a job is running unless the agent passes `force`, which it should only do after you say yes.

Tell your agent: "Never call `start_print` without my explicit yes in this conversation, and always ask
whether the plate is clear."

## Client setup

Syntax follows each client's own documentation at the time of writing; clients change quickly, so if a
snippet no longer works the client's docs are the authority. All examples use the name `fabrica`.

**Claude Code**

```bash
claude mcp add --transport http fabrica http://<host>:8271/mcp \
  --header "Authorization: Bearer <your key>"
```

**Cursor** (`~/.cursor/mcp.json` or `.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "fabrica": {
      "url": "http://<host>:8271/mcp",
      "headers": { "Authorization": "Bearer ${env:FABRICA_KEY}" }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`; the `inputs` block asks for the key once so it is not in the file)

```json
{
  "inputs": [
    { "type": "promptString", "id": "fabrica-key", "description": "Fabrica API key", "password": true }
  ],
  "servers": {
    "fabrica": {
      "type": "http",
      "url": "http://<host>:8271/mcp",
      "headers": { "Authorization": "Bearer ${input:fabrica-key}" }
    }
  }
}
```

**Any other client:** choose *streamable HTTP* (sometimes just "http" or "remote"), set the URL to
`http://<host>:8271/mcp` and add the `Authorization` header. A stdio-only client can use a bridge such as
the community [`mcp-remote`](https://github.com/geelen/mcp-remote) (third-party; untested with Fabrica).
Clients that support only the older SSE transport are not supported.

<a id="https"></a>
## HTTPS

Keys travel in clear over plain `http://`. That is fine on a trusted home LAN; otherwise put Fabrica
behind HTTPS.

- **Tailscale:** `tailscale serve --bg 8271`, then use `https://<machine>.<tailnet>.ts.net/mcp`. Publish the
  port as `127.0.0.1:8271:8271` in your compose file to close the plain-`http` port.
- **Reverse proxy** (Caddy, nginx): terminate TLS there and add its host name to
  `BAMBU_MCP_ALLOWED_HOSTS=fabrica.example.com` in `.env` (see
  [Troubleshooting](troubleshooting.md#host-name-rejected)).

Never expose Fabrica's port to the internet.
