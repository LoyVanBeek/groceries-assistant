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

- [ ] Per evening block on mobile: day + dish name (+ time/bonus chip) only;
      kok, attendance, goedkeuren/verplaatsen and dish parts move into a
      details-expander per row, so the week fits one screen
- [ ] Dual rendering with per-breakpoint form ids (Jinja macro): desktop keeps
      its columns, mobile gets the expander — no double-submit conflicts
- [ ] Reordering on mobile without HTML5-drag: ↑/↓ buttons inside the
      expander, posting the same /plan/move-day rotation — also sidesteps the
      pull-to-refresh clash
- [ ] Test on a real phone (Firefox): seven days fit one screen, expanders
      work, ↑/↓ moves correctly

**Verify**: phone, Firefox, no scrolling needed for the whole week; details
per row available; reorder via arrows.
