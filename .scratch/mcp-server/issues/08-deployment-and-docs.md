# Deployment & docs for the MCP server

Label: wayfinder:grilling
Type: grilling
Status: open
Blocked by: 03, 07
Map: .scratch/mcp-server/map.md

## Question

What must change in deployment and docs for the spec to be complete?

- Docker: the image already carries tesseract/xvfb/chromium; does the MCP
  HTTP endpoint ride the same container/port or a second service in
  `docker-compose.yml`? Per [03 — runtime architecture](03-runtime-architecture.md)
  and [07 — transports](07-transport-and-access.md).
- systemd/compose units: what changes in `deploy/`.
- Client configuration snippets for OpenCode and Claude (from
  [02 — client connections](02-client-connections.md)): where they live in the
  spec and/or README.
- README section (Dutch, matching the repo's voice) and what the follow-on
  implementation effort must ship as "done".

Output: the deployment/docs outline for the spec, with concrete file-level
changes listed.
