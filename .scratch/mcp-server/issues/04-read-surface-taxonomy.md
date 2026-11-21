# Read surface: routes → tools, resources, prompts

Label: wayfinder:grilling
Type: grilling
Status: open
Blocked by: —
Map: .scratch/mcp-server/map.md

## Question

How should the app's read capabilities map onto MCP tools, resources, and
prompts — at full-parity ambition?

Start from the route inventory in `src/groceries_assistant/web/app.py` (~92
routes, GETs serve HTML) and the layers underneath
(`services/`, `planner/`, `store/`, `recipes/`, `dishes/`, `ah/`).
Group the capabilities: week plan (proposal/approval state), recipes /
components / dishes, freezer (vriezer), order list + AH matching + bonus,
barcodes, settings/export.

Decide:

- What is a tool vs a resource (e.g. `week/current`, `recipes/{slug}`) vs a
  plain prompt.
- The stance for binary/image outputs (recipe thumbnails, OCR photos): file
  paths, embedded base64, or omitted.
- Param shapes: how Dutch domain concepts (course/gang, tag filter,
  week-start weekday) appear as parameters.

Output: a draft read-surface catalogue (tool names in English, Dutch terms in
descriptions, params, return shapes) that ticket 05 (write surface) and
ticket 06 (naming & glossary) build on. Keep it one session's work: catalogue
level, not implementation detail.
