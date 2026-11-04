# Plan: remaining TODO items

Base everything on `master` (32391fc). One feature branch per item, branched
off master. Each item ends with its TODO.md line ticked. Check the boxes as
you review each item's result.

- [ ] 0. Housekeeping: merge `b6bcca0` (deploy unit runs as root; docker
       always needs sudo on the server) into master — **in progress on a
       separate worktree**. Then re-run `sudo ./deploy/setup.sh` on the
       server to pick it up.

## 1. Restjes thumbnail + icon on ordered evenings (small)

Already built in `43c5a79` for not-yet-ordered evenings (`plan.html:50-56`,
CSS `.restjes-thumb`/`.restjes-icon` in `static/style.css:532-545`).

Gap: the `e.is_ordered` branch of `plan.html:44-49` shows the repeated dish's
thumbnail without the recycle icon.

- [ ] Wrap the thumb in the ordered branch in the same
      `<span class="restjes-thumb">` + `&#9851;` icon markup
- [ ] Tick TODO.md line 82; adjust/extend the plan-page render test

**Verify**: /plan on a week with a restjes evening, both before and after
ordering it — icon shows in both; `uv run pytest -q` green.

## 2. Tags for recipes + filter in search (medium)

`tags: list[str]` already exists on the model (`models.py:58`) but is
unplumbed — and `/recipes/save` currently *silently wipes* tags on every edit.

- [ ] Fix data loss: `/recipes/save` (`web/app.py:2284-2297`) round-trips tags
- [ ] Editor: comma-separated tags input in `recipe_edit.html`, defaults via
      `editor_ctx` (`web/app.py:2053-2072`)
- [ ] List: `?tag=` param in `recipes_page` (`web/app.py:1926-1962`), tag
      chips column + tag filter dropdown in `recipes.html` (dropdown fed from
      tags in use)
- [ ] Plan picker: same tag filter in `select_options`
      (`web/views.py:309-311`) and the `/plan/select` page
- [ ] Tests: save round-trip, list filter, select filter

**Verify**: edit a tagged recipe -> tags survive; `/recipes?tag=x` and the
dropdown filter; `/plan/select` respects the filter; pytest green.

## 3. Only auto-fill the week on button press (small)

- [ ] /plan with no proposal shows an empty week: all seven days, nothing
      selected ("Nog te kiezen" + gerecht kiezen per day)
- [ ] "Vul de week in" replaces the untouched week with the planner's
      proposal once; hidden once the week has selections
- [ ] "Week(en) leegmaken" clears this week + planned-ahead weeks so the
      empty week (and its button) returns
- [ ] Tests: fresh store -> /plan shows the skeleton; fill only on button

**Verify**: fresh data dir -> /plan shows 7 empty days; fill plans once;
manual picks are never overwritten; leegmaken brings the empty week back.

## 4. Drag & drop re-order week menu (medium)

`POST /plan/swap-days` + `Store.swap_days` (`store/db.py:615-667`) already
swap two evenings and move `plan_picks`/`plan_approvals`/`plan_ordered`
groceries along — pairwise only. Drag & drop wants a positional move.

- [ ] `Store.move_day(day, to)`: splice the 7 evenings in one transaction,
      picks/approvals/ordered items/groceries travel with the dish (reuse
      swap_days internals)
- [ ] `POST /plan/move-day` (`web/app.py`, next to swap-days) with the same
      guardrails as `plan_swap` for approved days
- [ ] Frontend: vanilla JS HTML5 drag on the ordered-week rows in
      `plan.html` (drop -> POST -> reload); keep the "ruil dag" details as
      no-JS fallback
- [ ] Store unit tests: picks + ordered items travel with the moved dish

**Verify**: drag a row -> order persists after reload; groceries and
goedgekeurd state follow the dish; pytest green.

## 5. Bonus preference (medium; research spike first)

- [ ] **Spike**: does the anonymous mobile API expose promotions/bonus? If
      yes use it; otherwise scrape `ah.nl/bonus` via `fetch_html_browser`
      (playwright path already beats Akamai)
- [ ] Cache: `data/bonus.yaml` (product names/ids), refreshed on demand,
      hand-editable
- [ ] Matching: recipe ingredients -> bonus products via
      `normalize`/`similarity` (`ah/matching.py`) + pinned `ah_mappings` ids
- [ ] Scoring: `BONUS_SCORE` in `planner/engine.py`; count of in-bonus
      ingredients adds a small bonus in `score_option`/`_pick`; engine
      receives the bonus set from `services/planner_service.py`
- [ ] UI: proposal annotation on /plan ("2 ingrediënten in de bonus")

**Verify**: unit test with a bonus fixture — a recipe with bonus ingredients
beats an equal alternative; /plan shows the annotation; missing/stale cache
never crashes planning.

## 6. Bookmarklet POSTs recipes directly (medium)

Builds on `5b22269` (bookmarklet currently copies recipe JSON for pasting
into `/recipes/import-urls`).

- [ ] `POST /recipes/import-paste` accepting `{url, jsonld|html}` -> same
      pipeline as the pasted-HTML path of `extract_from_url` -> redirect to
      the prefilled editor (human review stays in the loop)
- [ ] Cross-origin: permissive CORS on that route only + accept `text/plain`
      to avoid the preflight; simple token query param baked into the
      bookmarklet (app has no auth)
- [ ] Update the bookmarklet (served snippet/README) to POST instead of copy
- [ ] Tests: route parse-failure, token rejection

**Verify**: on an ah.nl recipe page the bookmarklet opens the editor on the
server, prefilled; curl without token -> 403.

## 7. Barcode scanner for grocery items (medium-large; spike first)

`Recipe.barcode` already exists; groceries/voorraad live in settings + order.

- [ ] **Spike**: barcode lookup in the AH anonymous API? If not, local map
- [ ] Store: `data/barcodes.yaml` (barcode -> product name)
- [ ] UI: "scan" button on the item forms (settings + order) using the
      `BarcodeDetector` API (Chrome/Android), camera picker like the recipe
      photo flow; manual barcode entry as fallback; a scan fills/creates the
      item and remembers the mapping
- [ ] Tests for the mapping store

**Verify**: on the phone, scanning a package selects/creates the item;
manual entry path works; mapping persists across restarts.

## 8. Make it look nice on mobile (medium, ongoing)

- [ ] Audit `base.html` + plan/order/recipes/select templates at phone width
- [ ] `style.css`: breakpoints; wide tables (select, order) become cards or
      scroll horizontally; larger touch targets on kies/vink controls
- [ ] Real-phone pass over /plan, /plan/select, /order, /recipes

**Verify**: all pages usable at 390px width; nothing clipped or
double-horizontal-scrolling.

---

Suggested order: 0 -> 1 -> 2 -> 3 -> 4 -> 6 -> 5 -> 7 -> 8 (2 fixes a real
data-loss bug; 5 and 7 start with a research spike).
