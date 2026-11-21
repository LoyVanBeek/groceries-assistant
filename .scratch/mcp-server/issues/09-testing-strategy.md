# Testing strategy for the MCP surface

Label: wayfinder:grilling
Type: grilling
Status: open
Blocked by: 01, 03
Map: .scratch/mcp-server/map.md

## Question

How do we test the MCP server to the repo's standard (pytest, in-process,
no network)?

- Reuse the existing test helpers/fixtures in `tests/`; what new fixtures the
  MCP server needs (same data dir as the web tests use).
- SDK test utilities from [01 — MCP SDK choice](01-mcp-sdk-choice.md): the
  in-process client pattern, testing both transports (stdio via subprocess?
  HTTP via ASGI transport?).
- Whether end-to-end against a real client (MCP inspector, or OpenCode
  pointing at it) is in scope for CI or manual-only.
- CI additions: GitHub Actions currently runs lint/format/types/tests — what
  changes.

Output: a test plan for the spec, naming the concrete patterns.
