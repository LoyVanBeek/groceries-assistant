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
- [ ] Paste a AH.nl product URL to match the products
- [x] Include thumbnail of the AH-item. Just crosslinking to the image on AH.nl should be fine right?
      (yes - AH's static.ah.nl CDN is public; suggestions and pinned items show a
      lazy-loaded thumb, image url remembered in `ah_mappings.yaml`).