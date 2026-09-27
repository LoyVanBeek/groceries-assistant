# groceries-assistant-2

Weekplan voor 7 diners -> boodschappenlijst -> AH-link. De opvolger van
`groceries-assistant` (v1): zonder Grocy, zonder appie-CLI, zonder TLS.
Eén commando, lokaal, SQLite + recepten als YAML.

## Gebruik

```bash
docker compose up -d          # web UI op http://<host>:8901
```

Of lokaal: `uv run groceries-assistant serve`.

Flow:

1. **Instellingen** — weekschema (per avond: kok, minuten, restjes oké?),
   familieleden, basisvoorraad (nooit bestellen), verpakkingen (zie stap 5).
2. **Recepten importeren** — foto van print (OCR), geplakte tekst of URL;
   controleren, opslaan. Vink per recept per persoon love/ok/dislike/never aan.
3. **Gerechten** — een gerecht dat je zelf samenstelt (`aardappel-groente-vlees`):
   per onderdeel een lijstje recepten waaruit dat onderdeel gekozen wordt. Ook
   over het gerecht zelf kun je per persoon love/ok/dislike/never zeggen; dat
   weegt zwaarder dan de onderdelen samen.
4. **Week** — planner stelt 7 diners voor (tijd past bij de avond, geen
   herhaling binnen ~3 weken, geen zelfde proteïne op rij, verdicts geteld).
   Gerechten doen mee als gewoon voorstel, met de onderdelen meteen gekozen.
   Wisselen via dropdown (ook 'restjes van de avond ervoor'), dan goedkeuren.
5. **Verpakkingen** — per product hoe één aankoop eruitziet en hoe lang het
   resteert (`broccoli: 1 stuk = 350 g, 3 dagen`). De lijst rekent dan met hele
   verpakkingen, koopt per dag wat nodig is, en houdt het restant als voorraad
   met datum; een regel wordt dan 'broccoli 2 stuk (700 g)' en de lijst laat zien
   wat er al in de koelkast ligt. Zonder verpakking blijft het een optelsom per
   product.
6. **Lijst** — vink af wat je al hebt -> genereer de AH-link
   (`ah.nl/mijnlijst/add-multiple?p=id:qty`): tik op telefoon of scan QR,
   bestel af in de AH-app.

Zondags voorbereiden: recept met `prep_ahead: <minuten>` in de YAML; de planner
boekt die minuten op een avond eerder met genoeg ruimte.

## Data

- `data/recipes/*.yaml` — recepten (bewerken mag, is de bron van waarheid)
- `data/dishes/*.yaml` — gerechten uit onderdelen (idem, ook bewerkbaar)
- `data/state.db` — SQLite: mensen, verdicts, weekschema, voorstellen/goedgekeurde
  weken, basisvoorraad, verpakkingen, AH-matches
- `data/ah_mappings.yaml` — product -> AH webshop-id cache (handmatig te pinnen)

## Ontwikkelen

```bash
uv sync
uv run pytest -q
uv run ruff check .
```

AH-matching gebruikt de anonieme mobiele API (alleen lezen); de bestelstap is
gewoon een URL, er is geen login in deze app.
