# Runtime architecture: same process or separate?

Label: wayfinder:grilling
Type: grilling
Status: open
Blocked by: 01
Map: .scratch/mcp-server/map.md

## Question

How does the MCP server run relative to the web app, and how does it share
state safely?

Options to weigh (use the SDK facts from [01 — MCP SDK choice](01-mcp-sdk-choice.md)):

- Mounted into the same FastAPI process: one uvicorn app serves the web UI and
  the MCP endpoint (likely `/mcp`) — see `create_app(data_dir)` in
  `src/groceries_assistant/web/app.py`.
- A separate process: a `groceries-assistant mcp` typer subcommand next to
  `serve` in `src/groceries_assistant/main.py`, sharing the same `data/` dir.
- Both run modes from one shared codebase (services/planner/store layers).

Decide and record, with trade-offs:

- SQLite (WAL) concurrency and YAML-cache coherence between processes.
- Whether OCR (tesseract), playwright, and camera-dependent flows constrain
  the choice.
- Implications for Docker/systemd (deployment ticket 08 builds on this).

Output: an architecture recommendation with rationale, ready to paste into the
spec.
