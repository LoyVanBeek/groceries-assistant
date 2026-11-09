# Plan: remaining TODO items

Master has everything from the previous round, verified: deploy tooling
(setup/update), restjes icon, tags + filters, empty week + fill button +
week(en) leegmaken, drag & drop with live preview, bonus preference (search
API's isBonus flags, select-page only), bookmarklet-POST, barcode scanner
(learning registry), mobile-fits-390px.

## 1. Fill only the remaining days (TODO: "Add button to add 'remaining days'")

- [x] A second button next to "Vul de week in" on /plan: **"Vul de lege dagen
      in"** — visible as soon as the week still has days without a dish but
      isn't empty anymore
- [x] `POST /plan/fill-remaining`: the planner runs over the empty evenings
      only, with the manual picks passed as 'planned' (no repeats, no reuse)
      and merged back into the proposal; picks/goedkeuring stay untouched
- [x] Restjes-chain: the dish of the evening before the first empty day is
      passed as 'previous', so the tail can open with restjes; a manual dish
      on a day *between* two empty days is not linked (rare, hand-fillable)
- [x] Tests: pick 2 days by hand → button → the other days fill, the 2 stay;
      pressing again does nothing; engine tests for planned/previous seeding

**Verify**: choose a few days yourself, press the button, your picks survive.

## 2. Compact mobile week rows (TODO: "On mobile, just have the recipe names…")

- [x] Per evening block on mobile: day + dish name (+ time/bonus chip) only;
      kok, attendance, goedkeuren/verplaatsen and dish parts move into a
      details-expander per row, so the week fits one screen
- [x] Dual rendering with per-breakpoint form ids (Jinja macro): desktop keeps
      its columns, mobile gets the expander — no double-submit conflicts
- [x] Reordering on mobile without HTML5-drag: ↑/↓ buttons inside the
      expander, posting the same /plan/swap-days exchange — also sidesteps the
      pull-to-refresh clash
- [ ] Test on a real phone (Firefox): seven days fit one screen, expanders
      work, ↑/↓ moves correctly (user's check)

**Verify**: phone, Firefox, no scrolling needed for the whole week; details
per row available; reorder via arrows.

## 3. Restjes: portions, freezer, selection page, auto-planner

- [x] Drag = swap everywhere (restjes included): dropping a dish past its
      restjes erases the usage, the chip only offers *later* days
      (`restjes-vroeg` / `restjes-bezet` refusals)
- [x] The ⇛ restjes chip counts portions: `servings − eters − geplande
      restjes − vriezer` — no chip when nothing is left over; the editor
      field is relabeled "Porties (het gerecht oplevert)"
- [x] ❄ in de vriezer: button next to the chip stores the surplus in
      `data/vriezer.yaml` (VriezerStore, hand-editable, re-read per request);
      without surplus the endpoint refuses with `restjes-geen`
- [x] Frozen restjes survive week(en) leegmaken (the file is never touched by
      clearing) and show as "❄ in de vriezer: N porties" on the cook row
- [x] Selection page (/plan/select): the restjes button names the dish and its
      surplus; each frozen stock appears as "❄ restjes uit de vriezer: name
      (N porties)" — eating it swaps the evening to a leftovers row and takes
      the portions out of the freezer (`__vriezer__:<slug>` choice)
- [x] Auto-planner: `plan_week(..., vriezer=...)` may open the freezer as the
      fallback after fresh leftovers (needs `allow_leftovers` + 15 min, and
      nothing scoring better); wired through propose() and fill-remaining()
- [x] Tests: VriezerStore round-trip, freeze endpoint (create + `restjes-geen`),
      chip portion arithmetic (shows and disappears), select-page frozen
      option + swap, engine frozen-fallback rules
- [ ] Later: dragging frozen portions onto a day straight from the week page
      (for now: selection page + auto-planner cover it)

**Verify**: cook a dish with leftover portions → chip shows the count → ❄
freezes them → clear the week → the stock is still on the cook row of the
dish and on the selection page; auto-fill may plan an evening around it.
