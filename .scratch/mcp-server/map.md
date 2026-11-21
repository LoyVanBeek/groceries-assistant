# Wayfinder map: MCP server for groceries-assistant

Label: wayfinder:map
Status: open

## Destination

An implementation-ready spec at `.scratch/mcp-server/spec.md` for adding an MCP
server to groceries-assistant: every decision needed to build it locked, with
full UI parity as the tool-scope ambition and both stdio and streamable-HTTP
transports from day one. No code is written by this effort; reaching the end
means the spec is complete, no tickets remain, and a follow-on implementation
effort can start on this worktree without further decisions.

## Notes

- Domain: Dutch-language household app (weekplan → boodschappenlijst →
  AH-link). Key terms: gerecht, onderdeel (component), restjes (leftovers),
  vriezer (freezer), goedkeuring (approval), besteld (ordered). Charting
  decision: tool names in English, Dutch domain terms preserved in
  descriptions/parameters.
- Skills every session should consult: research tickets → call Skill
  "research"; grilling tickets → call Skill "grilling" and Skill
  "domain-modeling"; questions about how a surface should look/behave may call
  Skill "prototype".
- Locked in charting (standing preferences for this effort):
  - Destination is the spec only; implementation is a follow-on effort.
  - An MCP SDK dependency in pyproject is acceptable (official `mcp` or
    FastMCP — ticket 01 decides which).
  - Both transports (stdio + streamable HTTP) in v1.
  - Full UI parity is the tool-scope target.
  - The spec is unordered: no phasing imposed on the implementer.
  - The streamable-HTTP endpoint starts with the web UI's posture
    (LAN-trust, no new auth); ticket 07 confirms and records the revisit path.
- Codebase facts: FastAPI app via `create_app(data_dir)` in
  `src/groceries_assistant/web/app.py` (~92 routes, almost all HTML); typer
  CLI in `src/groceries_assistant/main.py` (`serve`); SQLite `data/state.db`
  plus the YAML recipe library under `data/`; uv + pytest + ruff; deployed in
  Docker/systemd on a server (web UI on port 8901).
- This effort's tracker files live in-repo under `.scratch/mcp-server/` on the
  `mcp-server` worktree branch. The eventual spec artifact is
  `.scratch/mcp-server/spec.md` (written by ticket 10).

## Decisions so far

<!-- the index: one line per closed ticket, enough to judge relevance, then zoom the link for the detail the ticket holds -->

## Not yet specified

<!-- in-scope fog you can't ticket yet; graduates as the frontier advances -->

- MCP prompts / guided flows ("plan next week", "use up the freezer") — hangs
  on the read (04) and write (05) taxonomies.
- Agent-facing usage guidance: how tool descriptions encode the Dutch domain
  semantics, and the ah.nl import workaround guidance (bookmarklet/paste) —
  hangs on 04/05.
- Multi-step OCR/photo import flows over MCP (queue semantics, image inputs) —
  hangs on the binary-flow stances in 04/05.
- Whether agent-facing AH search/bonus tools need caching or rate-limiting to
  protect the anonymous API — hangs on 04.
- Family phone assistants (shortcuts etc.) over the HTTP transport — hangs on
  the client facts (02) and the transport decision (07).
- Tool versioning/compatibility policy once more than one client depends on
  the surface.

## Out of scope

<!-- work ruled beyond the destination; closed, never graduates -->

- Auto-placing AH orders — there is no ordering API; the deeplink/QR stays
  manual and unchanged.
- Postgres migration — the store layer stays SQLite + the YAML recipe library.
- A general REST/JSON API — MCP is the machine boundary.
- Web UI redesign — MCP is additive; the UI stays as-is.
- Building the MCP server itself — this effort ends at the spec; the
  implementation is the follow-on effort on this worktree.
