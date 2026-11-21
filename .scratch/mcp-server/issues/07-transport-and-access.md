# Transports: stdio + streamable HTTP, access posture

Label: wayfinder:grilling
Type: grilling
Status: open
Blocked by: 01, 02
Map: .scratch/mcp-server/map.md

## Question

Lock the transport story. Charting decision: both transports from day one.

- stdio: how a local client launches it (typer subcommand vs SDK runner
  pattern from [01 — MCP SDK choice](01-mcp-sdk-choice.md)); which data dir it
  binds.
- Streamable HTTP: path and port relative to the web UI (8901; same process
  per [03 — runtime architecture](03-runtime-architecture.md)); how LAN clients
  point at it (client facts from [02 — client connections](02-client-connections.md)).
- Confirm the charting posture: match the web UI (LAN-trust, no new auth).
  Record the revisit path concretely: which token/header mechanism the chosen
  SDK offers if that posture is ever reversed.
- CORS/headers and anything the web-UI coexistence requires.

Output: transport + access decisions with rationale, ready to paste into the
spec.
