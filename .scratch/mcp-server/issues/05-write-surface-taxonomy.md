# Write surface: mutations as tools, with guardrails

Label: wayfinder:grilling
Type: grilling
Status: open
Blocked by: 04
Map: .scratch/mcp-server/map.md

## Question

Which mutations become tools, and with what semantics? (Full UI parity is the
ambition; the charting decision left the grouping open.)

Walk the POST routes in `src/groceries_assistant/web/app.py` — picks,
approve/unapprove, swap-days, restjes/vriezer endpoints, order/have/confirm,
matches pin/unpin, recipe/component/dish save+delete, settings, import flows —
and decide, using the taxonomy method from [04 — read surface](04-read-surface-taxonomy.md):

- Which map 1:1 to tools, which merge into one tool, which stay UI-only and
  why.
- Guardrails for destructive or locking actions (besteld vergrendelt de dag,
  week(en) leegmaken, overwriting import) — confirmation semantics, dry-run
  flags, or refusal-by-default?
- How binary inputs enter via MCP: photo/OCR import (image files), recipe
  images, barcode scans (EAN strings), pasted JSON-LD/page-source.

Output: a write-surface catalogue with per-tool risk notes, consistent with
04's method and naming style.
