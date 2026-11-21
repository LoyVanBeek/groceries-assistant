# MCP SDK choice: official `mcp` vs FastMCP

Label: wayfinder:research
Type: research
Status: open
Blocked by: —
Map: .scratch/mcp-server/map.md

## Question

Which SDK should the spec plan around: the official `mcp` Python SDK
(modelcontextprotocol/python-sdk) or FastMCP (jlowin/fastmcp)?

Facts to gather (Python >=3.11 project, FastAPI web app, typer CLI):

- Maintenance status and release velocity of each.
- stdio AND streamable-HTTP transport support in each.
- Coexistence with FastAPI: mounting an MCP server inside an existing FastAPI
  app (one uvicorn process serves UI + MCP) and/or running standalone.
- Authoring ergonomics for tools, resources, and prompts.
- Auth primitives for streamable HTTP (token/header middleware), even though
  v1 leans LAN-trust — the revisit path should be known.
- Test utilities: an in-process client usable from pytest.
- License and dependency weight.

Constraint: exactly one SDK becomes a runtime dependency in `pyproject.toml`.

Deliverable: a comparison table plus a clear recommendation that ticket 03
(runtime architecture) and ticket 09 (testing strategy) can cite.
