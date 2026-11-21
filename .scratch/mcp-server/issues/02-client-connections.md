# Client connection facts: OpenCode, Claude Code, and remote MCP

Label: wayfinder:research
Type: research
Status: open
Blocked by: —
Map: .scratch/mcp-server/map.md

## Question

How do the target MCP clients connect, concretely?

Facts to gather:

- OpenCode: where MCP servers are configured (file + shape), how local stdio
  servers are declared, how remote/streamable-HTTP servers are declared, and
  what header/auth options remote servers support.
- Claude Code / claude CLI: `claude mcp add`, `.mcp.json` / `mcpServers` JSON
  shapes for stdio and HTTP servers, and header/auth options.
- What remote-connection options (headers, OAuth, bearer tokens) common MCP
  clients support — relevant if the LAN-trust posture (see ticket 07) is ever
  revisited.
- Gotchas: config discovery, reconnect behavior, tool-count/tool-name limits,
  anything that constrains tool naming (English names are the charting
  decision).

Deliverable: a client-connection cheat sheet with concrete config snippets
that ticket 08 (deployment & docs) can cite directly.
