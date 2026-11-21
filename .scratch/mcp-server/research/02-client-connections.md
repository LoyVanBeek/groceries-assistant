# MCP client connection cheat sheet — OpenCode, Claude Code, remote MCP

Date checked: 2026-11-21. Sources are official docs (opencode.ai, code.claude.com, cursor.com, modelcontextprotocol.io); every claim below cites its source section.

Target clients for the groceries-assistant MCP server: **OpenCode** (local dev + this effort's home harness) and **Claude Code** (`claude` CLI). Cursor is sketched briefly for the auth-revisit path (ticket 07 context).

## TL;DR — decision-relevant facts

- Both OpenCode and Claude Code speak **streamable HTTP** for remote servers and **stdio** for local ones. OpenCode's remote type is literally `"remote"`; Claude Code's is `"http"` (accepts `streamable-http` as an alias).
- **Tool names in English snake_case (`[a-z][a-z0-9_]*`) are safe everywhere.** The MCP spec allows 1–128 chars of `A-Za-z0-9_.-` (SHOULD); OpenCode *normalizes* any char outside `A-Za-z0-9_-` to `_` (dots vanish); Claude Code restricts the *server* key to letters/numbers/hyphens/underscores and drops any tool whose full callable name `mcp__<server>__<tool>` exceeds **128 chars**. Short server name + short tool names = no problem.
- Claude Code imposes two input-schema rules that silently exclude a tool: **top-level property names must be 1–64 ASCII chars** (letters/digits/`_`/`.`/`-`), and the schema must validate against JSON Schema draft 2020-12. Root-level `anyOf`/`oneOf`/`allOf` get flattened (Claude) but don't rely on it.
- **OAuth will NOT work over LAN plain `http://`** for Claude Code: its v2 runtime refuses to send OAuth credentials to a token endpoint that is not HTTPS or localhost. OpenCode documents no such restriction, but the LAN-trust posture (ticket 07) can't be revisited *to OAuth* for a home-server `http://` endpoint without HTTPS. The realistic revisit path is **static headers** (`Authorization: Bearer …` via env-var substitution), which both clients support.
- No fixed tool-count cap in either client. Claude Code's default is **tool search** (tools deferred, discovered on demand); OpenCode defaults to **Code Mode**, where tools are grouped under `tools.<server>.<tool>` rather than listed as native tools.
- HTTP response size hard cap for Claude Code: **16 MiB** per response. Output-token default limit 25k (`MAX_MCP_OUTPUT_TOKENS`); >50k-char text results get persisted to a file and referenced.

---

## OpenCode (V2)

Source: https://opencode.ai/docs/mcp-servers/ and https://opencode.ai/docs/config/ (checked 2026-11-21).

### Config file locations and precedence

| Purpose | Location |
| --- | --- |
| Project | `opencode.json` / `opencode.jsonc` in project root, or `.opencode/opencode.json(c)` |
| Global (all projects) | `~/.config/opencode/opencode.json(c)` |
| System (Linux) | `/etc/opencode/opencode.json` |

Precedence (lowest→highest): `~/.config/opencode/` → project (farthest→closest dir, searched cwd→root) → `/etc/opencode`. A higher-precedence config **replaces the entire server object** with the same name (no field merge).

JSON only (or JSONC). Environment substitution is `{env:NAME}` inside JSON strings (→ empty string if unset); file substitution `{file:path}`. Shell-style `$NAME` is NOT expanded.

### Config shape (V2: servers under `mcp.servers`, NOT directly under `mcp`)

Local stdio server:

```jsonc
// opencode.jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "groceries": {
        "type": "local",
        "command": ["uv", "run", "--directory", "/path/to/groceries-assistant", "groceries-assistant", "mcp", "serve"],  // placeholder command
        "cwd": ".",                        // optional, default = workspace
        "environment": { "DATA_DIR": "{env:GROCERIES_DATA_DIR}" },
        "disabled": false,                 // optional; true = configured but not connected
        "codemode": true,                  // default; false = expose tools as native tools
        "timeout": { "startup": 45000, "catalog": 30000, "execution": 600000 },  // optional
        "protocol": "legacy"               // default | "auto" | "2026-07-28"
      }
    }
  }
}
```

Remote streamable-HTTP server (e.g. home server, web UI port 8901):

```jsonc
{
  "mcp": {
    "servers": {
      "groceries": {
        "type": "remote",
        "url": "http://home-server:8901/mcp",        // absolute URL; path TBD by spec
        "headers": { "Authorization": "Bearer {env:GROCERIES_MCP_TOKEN}" },  // optional
        "oauth": false,                              // disable OAuth if using header auth only
        "disabled": false
      }
    }
  }
}
```

Remote field reference: `type` ("remote", required), `url` (absolute Streamable HTTP endpoint, required), `headers` (string map), `oauth` (settings object or `false`), `disabled`, `codemode`, `timeout`, `protocol`, `pool`.

### CLI

```sh
opencode mcp add groceries --url http://home-server:8901/mcp          # remote (streamable HTTP)
opencode mcp add groceries --url http://home-server:8901/mcp \
  --header 'X-API-Key={env:GROCERIES_MCP_TOKEN}'                      # headers, {env:} quoted
opencode mcp add groceries -- npx -y @example/mcp-server              # local stdio ("--" then command)
opencode mcp add groceries --env LOG_LEVEL=info -- ./serve.py         # local + env vars
opencode mcp add --global ...                                          # write to global config
opencode mcp list                                                      # status: ✓ connected / needs authentication
opencode mcp auth groceries                                            # start OAuth flow from CLI
opencode mcp logout groceries
```

Manage/connect/disconnect in-app via the `/mcps` panel. Removing a server = delete its config entry.

### Auth options for remote servers

- **OAuth is ON by default** (`oauth` unset). OpenCode discovers the authorization server, signs in with PKCE, refreshes tokens, stores credentials outside project config. Client identification order: pre-registered `oauth.client_id` → CIMD (public client, sends `https://opencode.ai/oauth/opencode/client.json` as client_id) → DCR (RFC 7591). Pre-registered client fields (V2 snake_case): `client_id`, `client_secret`, `scope`, `callback_port`, `redirect_uri`, `auth_server_metadata_url`. SSH/headless flow: paste the callback URL back into the prompt.
- **Static headers**: set `oauth: false` and use `headers` (e.g. `Authorization: Bearer {env:TOKEN}`) when the server only takes an API key/bearer header.
- No documented bearer-token-only field besides headers; header auth is the mechanism.

### Gotchas that shape the server design

- **Tool naming**: OpenCode names a tool `<server>_<tool>`, replacing every char outside letters/numbers/`_`/`-` with `_` (`server: context 7` → `context_7`; tool `resolve.library/id` → `context_7_resolve_library_id`). Prompts become commands `<server>:<prompt>` with the same normalization. Choose a short server key that stays unique after normalization.
- **Code Mode is the default**: tools are grouped by server and called via Code Mode (`tools.context_7.resolve_library_id(...)`), not exposed on the provider's native tool list. Set `codemode: false` if tools must appear natively. Permissions match the *normalized* `<server>_<tool>` name (e.g. action `groceries_*`).
- **Timeouts**: startup 30 s, catalog (tools/prompts/resources list) 30 s, execution 12 h (defaults; per-server and `mcp.timeout` overrides). A server that lists its tools in <30 s is fine.
- **Protocol**: default handshake is classic `initialize` (revisions ≤ 2025-11-25). `protocol: "auto"` probes for 2026-07-28 and falls back. A default-configured server needs to support the legacy `initialize` handshake.
- **Pool**: `pool: true` shares one connection across directories (only on the 2026-07-28 revision; exact URL/headers/OAuth/command/env/timeout/protocol must match).
- **Resources**: agents read them via built-in tools `opencode_list_mcp_resources` / `opencode_read_mcp_resource`.
- **Server instructions** returned at initialize are injected into the system prompt (`<mcp_instructions>`), unless all the server's tools are denied.
- **Context metadata**: OpenCode sends the session ID as `CallToolRequest.params._meta["ai.opencode/sessionID"]` — treat as opaque correlation, not auth.
- Reconnect behavior for a dropped remote server is not documented; users reconnect via `/mcps`. Design for clients that reconnect cold (full re-initialize + re-list).

---

## Claude Code (`claude` CLI)

Source: https://code.claude.com/docs/en/mcp (checked 2026-11-21).

### Adding servers (CLI)

```sh
# Remote streamable HTTP (recommended transport)
claude mcp add --transport http groceries http://home-server:8901/mcp
# ...with a bearer header
claude mcp add --transport http groceries http://home-server:8901/mcp \
  --header "Authorization: Bearer your-token"

# Local stdio ("--" separates claude's flags from the server command; --env passes variables)
claude mcp add groceries --env DATA_DIR=/home/user/groceries/data -- \
  uv run --directory /path/to/groceries-assistant groceries-assistant mcp serve

# From a JSON block / other-client config
claude mcp add-json groceries '{"type":"http","url":"http://home-server:8901/mcp"}'

claude mcp list | get <name> | remove <name>          # list shows ✔ Connected / ✘ Failed / ! Needs authentication
claude mcp login <name> / logout <name>               # run OAuth flow from shell (--no-browser for SSH)
```

Scope flag `-s/--scope`: `local` (default, current project only, stored in `~/.claude.json` under that project), `project` (shared, written to `.mcp.json` at project root, commit it), `user` (all your projects, `~/.claude.json`).

### JSON shapes

`.mcp.json` (project scope, commit) / `~/.claude.json` (local+user scope) / `claude mcp add-json`:

```json
{
  "mcpServers": {
    "groceries": {
      "type": "http",                                  // "streamable-http" accepted as alias; "sse" (deprecated), "ws" exist
      "url": "http://home-server:8901/mcp",
      "headers": { "Authorization": "Bearer ${GROCERIES_MCP_TOKEN}" },
      "timeout": 600000,                               // optional per-server tool-call wall-clock limit (ms)
      "alwaysLoad": true                               // optional: load tools upfront instead of tool-search
    }
  }
}
```

```json
{
  "mcpServers": {
    "groceries": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/groceries-assistant", "groceries-assistant", "mcp", "serve"],
      "env": { "DATA_DIR": "${GROCERIES_DATA_DIR}" }
    }
  }
}
```

Hard rules: an entry with `url` but **no `type` is a config error** (read as stdio → skipped with a report). Server keys may contain only letters, numbers, hyphens, underscores. Reserved server names (skipped with a warning): `workspace`, `claude-in-chrome`, `computer-use`, `Claude Preview`, `Claude Browser` (also `anthropic-skills` suppresses prompts).

- **Env-var expansion** in `.mcp.json`: `${VAR}` and `${VAR:-default}` in `command`, `args`, `env`, `url`, `headers`. Unset var without default → server still loads with the literal text (warning). Claude Code's own credential vars (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `AWS_BEARER_TOKEN_BEDROCK`, `HTTPS_PROXY`, `NPM_TOKEN`, …) read as **empty** in remote `url`/`headers` — copy your token to a differently named var.
- `headersHelper`: command that emits a JSON object of headers at connect time (dynamic tokens; 10 s shell timeout; re-run on 401/403 + one retry).
- Pre-configured OAuth: `oauth` object in JSON (`clientId`, `callbackPort`, `authServerMetadataUrl`, `scopes`) or `--client-id`/`--client-secret`/`--callback-port` flags.

### Auth options for remote servers

1. **None** — fine for the LAN-trust start.
2. **Static headers** (`headers`, `--header`, `headersHelper`) — the realistic revisit path for a home-server `http://` endpoint.
3. **OAuth 2.0** — `/mcp` panel or `claude mcp login`. Triggered when the server answers 401/403 without a configured `Authorization` header. Supports RFC 9728 protected-resource metadata discovery, DCR, CIMD, fixed callback port, scope pinning. **Caveat for the revisit path: the v2 runtime refuses to send OAuth credentials to a token endpoint that is plain `http://` anywhere except `localhost`/`127.0.0.1`/`::1`** — i.e. OAuth against `http://home-server:8901` on the LAN will fail; it would need HTTPS or an SSH tunnel presenting as localhost. If `headers.Authorization` is configured and rejected, Claude Code reports a failed connection and does NOT fall back to OAuth.

### Reconnect / discovery behavior

- Mid-session remote drop: reconnect up to **5 attempts, exponential backoff from 1 s**; then marked failed, background retry of HTTP/SSE after transient errors for up to ~half a day. First connection transient failure (5xx/refused/timeout): **3 retries**.
- Stdio: process exit mid-session → marked failed; interactive `/mcp → Reconnect`; in `claude -p`/SDK it restarts on the next tool call.
- Discovery cache (v2.1.221+, `MCP_DISCOVERY_CACHE`): a previously connected remote server may show `cached 2h ago · connects on first use · 5 tools` and only actually connect when Claude first calls one of its tools — a LAN server that starts on demand must tolerate this lazy first contact.
- `notifications/tools/list_changed`: honored interactively (tool list refetched). The v2 runtime holds a notification stream open with limited reopen attempts — no requirement on the server, but list_changed support is cheap and keeps clients fresh.
- Idle timeout: **5 min** for HTTP servers (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`), 30 min stdio; a call with no response/progress for that window errors. Default tool-call wall clock ~28 h (`MCP_TOOL_TIMEOUT`), per-server `timeout` field overrides. Long ops should send progress notifications or run ≤ ~5 min per call.
- MCP calls running >2 min in the main conversation are backgrounded (task continues, result arrives as a notification).
- Tool annotations matter: `readOnlyHint`/`idempotentHint` enable parallel execution and one safe retry after a connection close; `anthropic/requiresUserInteraction` forces a prompt.
- Output caps: HTTP/SSE response **16 MiB**; `MAX_MCP_OUTPUT_TOKENS` default **25k** (warn at 10k); text >**50k chars** persisted to a file with a reference; server can raise its own tool's ceiling to ≤500k via `_meta["anthropic/maxResultSizeChars"]`.

### Tool naming & schema constraints (the sharp edges)

- Full callable name is `mcp__<server>__<tool>`; if that string is **>128 chars the tool is excluded** (it can't be sent to the Claude API). Keep the server key and tool names short.
- Server key charset: letters/numbers/hyphens/underscores only (also enforced by `claude mcp add`; names with other chars can't be imported from Claude Desktop either).
- Tool names are used as the server declares them for prompts; tool names per MCP spec SHOULD be 1–128 chars of `A-Za-z0-9_.-`, case-sensitive, unique per server (no spaces/commas). English snake_case is the intersection of everything.
- Input schema: **top-level property names must be 1–64 chars, ASCII letters/digits/`_`/`.`/`-`**; schema must be valid draft 2020-12 (or declare another `$schema`, which skips that check). Invalid → tool excluded (server's other tools unaffected). Root-level `anyOf`/`oneOf`/`allOf` are flattened into a single object with the required-lists explained in the description — avoid.
- Tool-count: **no fixed per-server cap** ("the practical limit is your context window budget"). Default behavior is tool search: only names + server instructions load at startup; descriptions are read on demand. Description caps: **4,096 chars** for instructions/upfront tools, **16,384 chars** for tools loaded via tool search — put searchable keywords first.
- Project-scope `.mcp.json` servers require interactive approval (per project, trust-dialog aware); in `claude -p`/SDK sessions they load without a prompt.

---

## Cursor (brief — context for the auth revisit path)

Source: https://cursor.com/docs/context/mcp (checked 2026-11-21).

- Config: `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project). Generic `mcpServers` shape: stdio entries use `command`/`args`/`env`; remote entries use `url` + `headers` (no `type` needed). Interpolation `${env:NAME}`, `${workspaceFolder}` in `command`/`args`/`env`/`url`/`headers`.
- Auth: manual env/header config for API keys; OAuth (PKCE, RFC 9728 discovery, DCR) for remote servers, plus a static-OAuth `auth: { CLIENT_ID, CLIENT_SECRET, scopes }` object for servers without DCR.
- Supports tools/prompts/resources/roots/elicitation; per-tool approval prompts; server failures isolated.

## MCP spec tool-name rules (baseline)

Source: https://modelcontextprotocol.io/specification/2025-11-25/server/tools (checked 2026-11-25 rev on 2026-11-21).

- Tool name SHOULD be 1–128 chars; allowed chars SHOULD be `A-Za-z0-9_-` and `.`; SHOULD NOT contain spaces/commas/specials; case-sensitive; unique within a server.
- `inputSchema`/`outputSchema` default to draft 2020-12; no-params tools should use `{ "type": "object", "additionalProperties": false }`.
- Clients SHOULD implement timeouts and treat annotations as untrusted; servers MUST validate inputs, implement access controls, rate-limit, sanitize outputs.

---

## What this means for the spec (ready-to-cite points for ticket 08)

1. **Endpoint**: expose streamable HTTP at a stable absolute URL (e.g. `http://<home-server>:8901/mcp`, path set by the spec) and stdio as a single command. Both target clients support both transports; no SSE needed (deprecated in Claude Code).
2. **Tool names**: English snake_case (`^[a-z][a-z0-9_]{0,30}$`-ish) — survives OpenCode normalization, Claude Code's 128-char callable-name limit, and the MCP spec charset. Avoid dots even though the spec permits them (OpenCode would rewrite them). Server key: short, lowercase, e.g. `groceries` (valid for Claude Code's charset and readable in OpenCode's `groceries_<tool>` names).
3. **Schemas**: plain draft-2020-12 object schemas, no root-level combinators, top-level property names ≤64 ASCII chars; `{ "type": "object", "additionalProperties": false }` for no-arg tools.
4. **Auth start (LAN-trust)**: no auth. Documented revisit path = static bearer header; both clients pass `Authorization` via `headers`/`--header` with env-var substitution (`{env:VAR}` OpenCode, `${VAR}` Claude Code). OAuth is only viable if the endpoint later gets HTTPS (Claude Code's plain-http token-endpoint restriction; OpenCode's browser-flow friction on a headless server).
5. **Config snippets for docs**: one `opencode.jsonc` block (`mcp.servers.groceries`, `type: "remote"`, `url`, optional headers + `oauth: false`) and one `claude mcp add --transport http groceries http://…:8901/mcp` line (plus `.mcp.json` shape). Local-dev stdio snippets: OpenCode `mcp.servers.groceries` with `command` array; Claude `claude mcp add groceries -- …` with `--`.
6. **Server behavior requirements**: start fast (<30 s listing = both clients' catalog/startup default); tolerate lazy first connection (Claude discovery cache) and cold reconnects (re-init + re-list); send `notifications/tools/list_changed` on tool changes; keep responses well under 16 MiB and ~25k tokens per call (or annotate `anthropic/maxResultSizeChars`); progress notifications for anything longer than a couple of minutes; mark read-only tools `readOnlyHint: true`.
7. **Timeouts to design against**: startup/list ≈30 s (both), execution ≈12 h OpenCode / idle-timeout 5 min HTTP + wall-clock ~28 h Claude Code.
