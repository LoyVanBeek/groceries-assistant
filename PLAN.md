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
- [x] Test on a real phone (Firefox): seven days fit one screen, expanders
      work, ↑/↓ moves correctly (user's check — bevestigd door het dagelijkse
      telefoongebruik tijdens de restjes- en compactrondes)

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
- [x] Vriesrestjes versleepbaar vanaf de weekpagina: de ❄-chip (in de
      'meer'-expander, per onderdeel bij dag-gerechten) sleept naar elke
      lege dag — de avond wordt een vriezer-restjes rij (`frozen`-vlag) en
      de porties gaan van de voorraad; bezette dagen worden geweigerd
      (`vriezer-bezet`), een lege vriezer met `restjes-geen`
- [x] Ook samengestelde gerechten kunnen invriezen: de ❄ vriezt het gekozen
      onderdeel met de meeste porties in (de component, niet het gerecht als
      geheel); de chip op een dag-gerecht telt dat surplus mee
- [x] Restjes-avonden blijven bij hun gerecht: de bron staat zolang het
      gerecht nog ergens eerder kookt (valt terug op de vorige kookavond als
      het helemaal verdwijnt); vriesrestjes dragen een `frozen`-vlag en
      schuiven nooit mee met ruilen

**Verify**: cook a dish with leftover portions → chip shows the count → ❄
freezes them → clear the week → the stock is still on the cook row of the
dish and on the selection page; auto-fill may plan an evening around it.

## 4. Week-integriteit en mobiele polish (tussendoor)

- [x] De week telt altijd zeven dagen: `plan_to_rows` krijgt de verwachte
      dagreeks en zet avonden zonder voorstel om in lege kookavonden
      ('Vul de week in' én 'Plan de volgende week'); /plan vult voorstellen
      die dagen missen bij het openen zelf weer aan (de verdwenen maandag
      kwam zo terug)
- [x] 'Nog te kiezen' op een lege dag is zelf de link naar de keuzepagina
- [x] Vriesrestjes heten 'Gerecht (uit vriezer)' met het sneeuwvlok-icoon;
      verse restjes blijven 'Restjes (gerecht)' met het recycle-icoon
- [x] 'Vandaag' als kader rond de dag-tegel in plaats van de ronde pil
- [x] One-line mobiele rijen: de naam knijpt (ellipsis), de restjes-chip en
      de pijlen staan op dezelfde lijn, de week past ruimschoots op één scherm
- [x] Receptpagina: beheer-werkbalk bovenaan (wijzig, origineel recept,
      yaml-download, verwijderen met bevestiging)

## 5. Gangen (courses) en weekweergave-links

- [x] Weekweergave linkt naar wat je kookt: het recept bij een gewone dag,
      het dag-gerecht naar /dishes met elk gekozen onderdeel gelinkt naar
      zijn receptpagina, restjes/vriezer naar hun bron
- [x] `course` op recept én dag-gerecht (COURSES: hoofdgerecht, soep,
      voorgerecht, nagerecht, bijgerecht; standaard hoofdgerecht, onbekende
      waarden vallen daar op terug) — het bewerken gebeurt in de editor met
      een Gang-select
- [x] De keuzepagina toont standaard alleen hoofdgerechten; een
      gangen-kiezer (alle gangen / soep / …) staat naast de tagfilter en de
      sorteerlinks houden de gang vast
- [ ] Meerdere gangen op een dag (soep + hoofdgerecht + nagerecht): een
      course-kolom op de planrijen, gegroepeerd in de weekweergave,
      boodschappen per dag sommeren — de planner vult voorlopig alleen
      hoofdgerechten
