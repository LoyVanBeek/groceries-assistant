# TODO (OpenCode)

- [x] Black formatting (ruff format), ruff lint and ty type checks
- [x] Basic CI with the checks above (GitHub Actions: lint, format, types, tests)
- [x] Pre-commit hooks with the checks above (pytest on push)
- [x] Nicer theme (sans typography, green accent, softer surfaces, aligned
      controls; both light and dark).
- [x] Pictures for recipes (upload in the editor, shown on the list + view
      pages, stored under `data/recipes/img/<slug>.<ext>`, moves on rename).
- [x] Allow to adjust the time available for cooking from the webpage
- [x] Add an image picker for the recipes (multiple photos, pick the featured one)
- [x] Show thumbnail of recipe image on the week overview webpage
- [x] Match up all the items with AH article numbers (pin/unpin UI on
      `/order/match`, confirmed pins persist in `data/ah_mappings.yaml`)
- [x] On the 'koppel' page, add links to the actual article on ah.nl so I can check.
- [x] Include the recipe image in the dropdown (dish picker with thumbs).
- [x] Combine the dropdown with the selected menu item to take up less screen real estate
      (details/summary: the summary is the current dish, alternatives open below).
- [x] On the koppel page, add a custom entry option as well (manual AH-id + title).
- [x] Recipes need an optional URL field where I can paste an URL to the original recipe
      (optional `url` in the YAML, edit-form field, 'Originele recept' link on the
      recipe page, auto-filled when importing by URL).
- [x] I also need a way to indicate we don'ty have to cook at all due to eating elsewhere etc.
      ('niet koken (eten elders)' per evening: no dish, no groceries, kok/attendance
      hidden for that evening).
- [x] Paste a AH.nl product URL to match the products
      (custom field takes a bare id, 'wi<id>' or a full ah.nl product link; the slug
      fills in the description).
- [x] Include thumbnail of the AH-item. Just crosslinking to the image on AH.nl should be fine right?
      (yes - AH's static.ah.nl CDN is public; suggestions and pinned items show a
      lazy-loaded thumb, image url remembered in `ah_mappings.yaml`).
- [x] Camera picker for the web UI when adding recipes
      ('Maak foto' button next to the file picker; opens the rear camera on
      phone, falls back to the normal picker on desktop)
- [x] Bulk photo import
      (`/recipes/photos`: drop/pick a pile of photos, then assign them one by
      one to a recipe; leftovers stay staged, unwanted ones are discarded)
- [x] Drag and drop photo import
      (drop zone on the recipe editor and the bulk page, plus a drag-over
      highlight; dropped files land in the same input as a hand-picked set)
- [x] Import one recipe from several photos (the import form takes a list: front and back of one card are read as a
      single recipe, ingredients and steps merged; picking/camera/drop as before)
- [x] Bulk OCR import: one recipe per photo
      (`/recipes/ocr`: queue a pile of photos, click through them one by one,
      each opens pre-filled in the editor with '1 van N' and a 'volgende' link;
      saving retires that card and returns to the queue, keeping the photo as
      the recipe's picture unless unticked)
- [x] Show thumbnails on the '/order' page
      (each list item shows a lazy-loaded thumb of the matched AH article)
- [x] A dish made of parts you pick yourself ('aardappel-groente-vlees')
      (`data/dishes/<slug>.yaml`: each part lists the recipes that can fill it;
      `/dishes` edits them; in the week plan a select per part, the row shows
      the parts joined with ' + ' under the dish's name, and the longest part
      decides the time you need -- with the total shown next to it.
      The planner fills the parts itself too: dishes compete with every recipe,
      'never' on a dish slug skips the whole dish, a verdict on the dish counts
      more than the mean of its parts, and without a verdict the dish is worth
      the mean plus a small bonus -- so a dish you wrote down gets proposed
      about once a week, but still loses to a recipe someone loves. A proposal
      lands filled: the
      planner's own picks show up as chosen in the week plan. On `/dishes` you
      can say something about a dish per person, just like about a recipe)
- [x] Leftovers as arithmetic instead of a planner feature
      (declare how one purchase looks and how long it keeps per product: you buy
      whole things, what a purchase leaves over is stock with a shelf life, usable
      in the same week and in any other dish; the list then says '2 stuk (700 g)'
- [x] A component: a recipe that is a part of a dish instead of a meal
      (`component: true` on a recipe, so it keeps its own time, ingredients and
      verdicts but is never planned as an evening; `/components` lists them
      individually, the editor has a checkbox, and the dish form offers them as
      checkboxes with their minutes on the label)
- [x] A dish you cannot plan says why instead of vanishing
      (a part option that names no recipe, a part everyone marks 'never', a part
      that does not fit the evening: the dish stays in the week view, greyed out,
      with the reason underneath)
- [x] Copy the 13 prepared components for 'aardappel-groente-vlees' into
      `data/components/` (they landed there rather than in `data/recipes/` as
      first planned: wokjes, krieltjes, rosti, aardappelfrietjes, brocolli,
      sperzieboontjes, bloemkool, boontjes, worstjes, kipfilet, speklapje,
      kipschnitzel, vegaschitzel -- 17 components in total, the dish
      references them and appears in the planner)
- [x] When there are 'restjes', use the thumbnail of the repeated dish but with an icon overlayed on it
- [x] Have a preference for recipes when ingredients of it are in Bonus
      (bonus products are fetched per household ingredient via the search API's
      own `isBonus` flags -- the bonuspage endpoints refuse anonymous tokens
      with HTTP 500 -- and cached in `data/bonus.yaml`, hand-editable; each
      bonus ingredient is worth 0.5 planner points, capped at 1.5, so it nudges
      instead of overriding verdicts. Visible only on the gerecht-kiespagina:
      a chip on the option plus an expandable ingredient list with the bonus
      items highlighted)
- [x] Configure week-start. Groceries are delivered on Tuesdays, for a week ahead
      ('De week begint op' select in the settings, stored as week_start_weekday,
      default Monday; the plan week rotates around it)
- [x] Color-coded like-dislike rating when picking a dish
      (the old plan-page dropdown is gone; picking now happens on /plan/select,
      where the Waardering column shows a coloured badge -- geliefd/mixed/ok/
      niet geliefd -- plus a per-person dot per eater)
- [x] Recency of use when picking a dish
      (on the same surface a 'Laatst gepland' badge colours each recipe:
      deze week red, 2-3 weken orange, langer geleden green, nog nooit grey)
- [x] Import recipes from Albert Heijn's Allerhande.
      (ah.nl sits behind Akamai bot protection; recipe URLs fetch the ah.be twin
      first via curl_cffi with a browser TLS fingerprint, then a real headed
      browser. Failing that: paste the page source or JSON-LD in the import form,
      or use the bookmarklet that copies the recipe JSON from the AH page)
- [x] Bulk import from a list of URLs (`/recipes/import-urls`: one URL per line,
      parsed and saved directly, each outcome in a table; already-imported URLs are
      skipped, same-name recipes get -2. Also accepts pasted JSON-LD/page-source
      blocks -- optionally each preceded by its URL -- for the AH recipes where
      fetching is hopeless and the bookmarklet provides the JSON)
- [x] Drag and drop to re-order week menu
      (⋮⋮ handle per day on /plan: a ghost card follows the cursor and the row
      moves live to the slot where it will land; drop posts /plan/move-day and
      the server rotates the evenings so picks, goedkeuring and besteld-status
      travel along. Goedgekeurde/bestelde dagen in the dragged segment refuse
      the move; touch keeps using 'ruil dag')
- [x] Tags for recipes, filter on tags in search
      (tags: field in the editor -- comma-separated, normalized, and no longer
      wiped by a save; chips + dropdown filter on /recipes and the same filter
      on /plan/select, where composed dishes match via their parts' tags)
- [x] Only auto-fill in week with a button press, leave empty otherwise
- [x] Barcode scanner to add new grocery items
      (there is no barcode API -- AH's gtin lookup answers 404 -- so
      `data/barcodes.yaml` learns: a scan decodes the EAN in the browser
      (BarcodeDetector on Chromium, vendored ZXing elsewhere: live camera on
      localhost, photo-of-the-barcode on plain-http phones, typed EAN last)
      and an unknown barcode asks for the name once. Scan buttons on the
      settings forms and on /plan + /order, where a scanned package joins the
      week's list as 1 stuk; list rebuilds reset scanned extras)
- [x] Bookmarklet should POST new recipes to server directly
      ("AH-recept direct naar de server" on the import page: grabs the
      canonical URL + recipe JSON-LD on the page and submits a hidden form to
      `/recipes/import-paste` with a stable per-install token -- form
      navigation, so no CORS; lands in the prefilled review editor. The
      clipboard bookmarklet stays for the bulk page)
- [x] Make look nice on mobile as well
      (all pages fit a 390px phone: nav wraps, the plan/select tables already
      render as day-cards, the wide recipes/settings tables scroll sideways
      with the name column pinned. Bonus: the order page's week title is
      filled and 'voor <recept>' has its space back)
- [x] Add button to add 'remaining days' on week menu
      ("Vul de lege dagen in" next to "Vul de week in" on /plan: the planner
      runs over the empty evenings only, manual picks survive as 'planned',
      goedkeuring stays; pressing again does nothing)
- [x] On mobile, just have the recipe names and only show details in a dropdown on each row, so that the whole page fits a single screen., That makes drag and drop nicer on mobile, which also has pull to refresh which gets confused with drag and drop.
      (compact per-evening rows: thumb + name on one line, kok/attendance/
      goedkeuren/parts move into a 'meer' expander, reorder via up/down
      arrows that post the same day exchange -- no HTML5-drag, so pull to
      refresh never fights it. One line per row: name ellipsizes, restjes
      chip and arrows share the line)
- [ ] Keep all data in Postgress or sqlite, not yaml files.
    - [ ] But allow recipe export end import of recipe yaml files, via a standardized format/schema
