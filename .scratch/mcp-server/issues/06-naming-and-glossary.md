# Naming conventions + GLOSSARY.md

Label: wayfinder:grilling
Type: grilling
Status: open
Blocked by: 04
Map: .scratch/mcp-server/map.md

## Question

Fix the naming scheme and write the glossary.

Charting decision: tool names in English, Dutch domain terms preserved in
descriptions/parameters. Against the catalogues from [04 — read surface](04-read-surface-taxonomy.md)
and [05 — write surface](05-write-surface-taxonomy.md):

- Naming conventions: verb choices, prefixes, resource URI schemes, plural/
  singular, how course/gang and week-start appear.
- Where Dutch terms stay (descriptions, parameter docs, error strings) so an
  agent still maps "restjes"/"vriezer"/"gerecht" correctly.

Also create `GLOSSARY.md` at the repo root capturing the domain vocabulary
(gerecht, onderdeel/component, recept/recipe, restjes, vriezer, week,
goedkeuring, besteld, bonus, koppeling) so the tool docs and the spec share
one vocabulary. Follow the repo's language reality: UI is Dutch, code and this
effort are English.

Output: naming conventions (one page) + `GLOSSARY.md` committed.
