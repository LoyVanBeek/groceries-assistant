"""Per-page help text for the '?' button in the corner.

`PAGE_HELP` maps a page key to a heading plus sections of
(element, explanation) pairs, written for the household itself.
`page_key` maps the request path to that key, with a `default` entry for
paths that were not enumerated — every page gets a panel.
"""

from __future__ import annotations

PAGE_HELP: dict[str, dict] = {
    "plan": {
        "title": "De week",
        "sections": [
            (
                "De weektabel",
                [
                    (
                        "Dag",
                        "Eén rij per kookavond. De vorige dagen van deze week staan er gedempt bij; de dag van vandaag is gemarkeerd.",
                    ),
                    (
                        "Gerecht",
                        "Het recept of gerecht voor die avond. Klik op de naam om het te openen; 'nog te kiezen' is de link naar de keuzepagina.",
                    ),
                    (
                        "Minuten",
                        "Zo lang deze avond mag gekookt worden (uit de weekinstellingen, hier per avond aan te passen).",
                    ),
                    (
                        "Kok",
                        "Wie deze avond kookt. Zonder keuze staat er 'auto': dan kiest de planner wie het langst niet gekookt heeft.",
                    ),
                    (
                        "eters-vinkjes",
                        "Wie die avond meeter. Haal iemand weg voor een avond weg; de porties en boodschappen schalen mee.",
                    ),
                    (
                        "goedkeuren",
                        "Zet een dag vast. Een goedgekeurde dag kan niet meer worden gewisseld voordat je de goedkeuring intrekt.",
                    ),
                    (
                        "besteld",
                        "De boodschappen van deze dag zijn bevestigd op de bestellijst; het gerecht is alleen te wisselen na de bevestiging terug te draaien op de bestelpagina.",
                    ),
                ],
            ),
            (
                "Acties boven en onder de tabel",
                [
                    (
                        "Vul de week in",
                        "Laat de planner een heel voorstel maken: gerechten passend bij tijd, smaak en herhaling. Alleen bij een lege week.",
                    ),
                    (
                        "Vul de lege dagen in",
                        "Vult alleen de avonden zonder gerecht; jouw keuzes en goedkeuringen blijven staan.",
                    ),
                    (
                        "scan — op de lijst",
                        "Scan een streepjescode: het product komt één keer op de boodschappenlijst (bijv. iets dat op is).",
                    ),
                    (
                        "Maak de boodschappenlijst",
                        "Bouwt de bestellijst voor de goedgekeurde dagen en gaat naar de bestelpagina.",
                    ),
                    (
                        "Plan de volgende week",
                        "Plakt een nieuwe, vrij gevulde week onder de huidige tabel; het voorstel blijft één doorlopend plan.",
                    ),
                    (
                        "Week(en) leegmaken",
                        "Wist deze week en alle latere weken (inclusief goedkeuringen en bestellingen); afgelopen weken blijven als geschiedenis.",
                    ),
                    (
                        "toon eerdere dagen",
                        "Bij een doorlopend plan dat al weken meeloopt: laat de verstopte afgelopen weken ook zien.",
                    ),
                ],
            ),
            (
                "Restjes en vriezer",
                [
                    (
                        "restjes",
                        "Een restjes-avond eet wat eerder die week over is gebleven; de bron is de kookavond erboven.",
                    ),
                    (
                        "❄ vriezer-chip",
                        "Sleep zo'n chip naar een lege dag om een portie uit de vriezer te plannen. Pas bij bevestiging op de vriezerpagina gaat het uit de voorraad.",
                    ),
                    (
                        "verplaats-pijlen",
                        "Wissel de avond met de avond erboven of eronder; gerechten, keuzes en vinkjes verhuizen mee.",
                    ),
                ],
            ),
        ],
    },
    "plan-select": {
        "title": "Gerecht kiezen",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "Voorstellen",
                        "Recepten en gerechten die op deze avond passen, gerangschikt op score: tijd, smaak, bonus en herhaling tellen mee.",
                    ),
                    (
                        "Bonusblok",
                        "Ingredienten die nu in de AH-aanbieding staan, worden gemarkeerd — handig om te kiezen wat voordelig is.",
                    ),
                    (
                        "Onderdelen uitklappen",
                        "Bij een samengesteld gerecht zie je welke onderdelen erin gaan en wat er nog open staat.",
                    ),
                    ("terug", "Terug naar de week zonder te kiezen."),
                ],
            ),
        ],
    },
    "recipes": {
        "title": "Recepten",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "importeren",
                        "Recepten binnenhalen van een website via de bookmarklet of een url-lijst.",
                    ),
                    ("recept-naam", "Opent het recept met ingrediënten en de bestelblokkade."),
                    ("bewerken", "Het recept aanpassen: ingrediënten, stappen, tijd en porties."),
                    (
                        "verwijderen",
                        "Verwijdert het recept permanent (er wordt om bevestiging gevraagd).",
                    ),
                    ("Nieuw recept", "Begin met een leeg recept om zelf in te vullen."),
                ],
            ),
        ],
    },
    "recipe": {
        "title": "Dit recept",
        "sections": [
            (
                "Boven het recept",
                [
                    ("wijzig", "Het recept bewerken."),
                    (
                        "Originele recept ↗",
                        "De website waar het recept vandaan komt (als die bekend is).",
                    ),
                    ("yaml ⤓", "Het recept downloaden als yaml-bestand."),
                    ("verwijder", "Verwijdert dit recept permanent."),
                    (
                        "personen",
                        "Pas het aantal personen aan: de ingrediënten en boodschappen schalen direct mee.",
                    ),
                ],
            ),
            (
                "De ingrediënten-/besteltabel",
                [
                    (
                        "in huis ☐",
                        "Vink aan wat je al thuis hebt. Het verdwijnt uit de AH-link en de QR-code, en de rij wordt gedempt. Geen verversen van de pagina nodig.",
                    ),
                    (
                        "recept",
                        "De hoeveelheid die het recept vraagt, geschaald naar het aantal personen.",
                    ),
                    ("product", "Het ingrediënt zoals het op de lijst komt."),
                    (
                        "bestellen — aantal",
                        "Hoeveel AH-verpakkingen je moet kopen (2 uien → 1 zak). Met (…) de rekenslag.",
                    ),
                    (
                        "bestellen — chip",
                        "Je koppeling: ✓ + artikelnaam met foto. 'wijzig' opent de koppelpagina om hem te controleren of te veranderen.",
                    ),
                    (
                        "koppel",
                        "Voor een nog niet gekoppeld product: ga naar de koppelpagina om het aan een AH-artikel te hangen.",
                    ),
                    (
                        "[… per pak, via AH]",
                        "AH vertelt zelf de verpakkingssorte; daarom rekent de planner hiermee. Handmatig in te stellen op de koppelpagina.",
                    ),
                    (
                        "−",
                        "Haalt een voorraad-product weer van de lijst (bij 'with' gekozen staples).",
                    ),
                ],
            ),
            (
                "Bestellen",
                [
                    (
                        "QR-code",
                        "Scan met je telefoon om dezelfde artikelen in de AH-app op je lijst te zetten (ingelogd in de AH-app).",
                    ),
                    ("AH-link", "Opent dezelfde add-multiple-lijst rechtstreeks in de browser."),
                    (
                        "voorraad vinkje",
                        "Bij 'voorraad'-producten (settings): vink aan om ze mee te bestellen deze keer.",
                    ),
                ],
            ),
        ],
    },
    "recipe-edit": {
        "title": "Recept aanpassen",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "Naam / porties / tijd",
                        "De basis: naam, voor hoeveel personen het recept is en hoe lang het duurt (voorbereiding apart te zetten).",
                    ),
                    (
                        "Ingrediënten",
                        "Product, hoeveelheid en eenheid per regel. De productenamen komen terug in de weektabel en op de bestellijst.",
                    ),
                    ("Stappen", "De bereiding, één stap per blok."),
                    ("tags / proteïne", "Vrije labels die de planner meeweegt bij het kiezen."),
                    (
                        "url",
                        "De bron van het recept; de 'Originele recept'-link komt hier vandaan.",
                    ),
                    ("opslaan", "Slaat het recept op en keert terug naar de receptpagina."),
                ],
            ),
        ],
    },
    "recipes-import": {
        "title": "Recepten importeren",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "Bookmarklet",
                        "Sleep de link naar je balkbladen; klik erop op een receptpagina en je browser opent hier het importformulier met de ingrediënten erin.",
                    ),
                    ("yml plakken", "Plak de yaml van een recept hieronder en importeer."),
                    (
                        "url-lijst",
                        "Batch importeren: plak recept-URL's, één per regel, en laat ze binnenhalen.",
                    ),
                ],
            ),
        ],
    },
    "recipes-photos": {
        "title": "Receptfotos",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "sleepzone",
                        "Sleep afbeeldingen (of een pdf) hierheen om een recept te koppelen aan een foto of om tekst te laten lezen (OCR).",
                    ),
                    (
                        "OCR",
                        "Tekst uit de afbeelding wordt een conceptrecept dat je daarna nabewerkt.",
                    ),
                    (
                        "toewijzen",
                        "Kies bij een foto welk recept erbij hoort; het wordt de afbeelding op de receptpagina en in de weektabel.",
                    ),
                ],
            ),
        ],
    },
    "dishes": {
        "title": "Gerechten",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "gerecht-naam",
                        "Een gerecht combineert onderdelen (aardappelen, groente, vlees) tot één avondmenu.",
                    ),
                    (
                        "onderdelen",
                        "Per label (Groente, Vlees, …) kies je welke recepten in het gerecht zitten; de planner kiest er automatisch uit tijdens het vullen.",
                    ),
                    ("nieuw gerecht", "Begin met een leeg gerecht."),
                    (
                        "QR / bestellen",
                        "Bij een gerecht kun je direct een boodschappenlijst maken voor alleen dit gerecht.",
                    ),
                ],
            ),
        ],
    },
    "components": {
        "title": "Onderdelen",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "onderdeel",
                        "Eén voedingsmiddel met hoeveelheid en kooktijd; onderdelen bouwen gerechten op.",
                    ),
                    ("afbeelding", "Sleep hier een foto; die verschijnt in de weektabel."),
                    ("nieuw onderdeel", "Begin met een leeg onderdeel."),
                ],
            ),
        ],
    },
    "vriezer": {
        "title": "Vriezer",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "voorraden",
                        "Per gerecht hoeveel porties in de vriezer liggen en hoe oud ze zijn.",
                    ),
                    (
                        "bevestigen",
                        "Porties die gepland zijn om gegeten te worden wachten hier op jouw bevestiging; pas dan gaan ze van de voorraad af.",
                    ),
                    (
                        "porties bijwerken",
                        "Voeg handmatig porties toe of weg (bijv. na het invriezen van restjes).",
                    ),
                    (
                        "suggestions",
                        "Suggesties: welk gerecht is het oudst en zou eerst gegeten moeten worden.",
                    ),
                ],
            ),
        ],
    },
    "matches": {
        "title": "Koppelingen",
        "sections": [
            (
                "Nog te koppelen",
                [
                    (
                        "wachtrij",
                        "Elke productnaam uit je recepten zonder AH-koppeling, bovenaan met de koppel-knop en van welke recepten hij komt.",
                    ),
                    (
                        "koppel",
                        "Opent de koppelpagina: kies uit AH-suggesties of plak zelf een AH-id of product-URL.",
                    ),
                ],
            ),
            (
                "Gekoppeld",
                [
                    (
                        "artikel-chip",
                        "De AH-koppeling met foto en artikelnaam; de link opent het product op ah.nl.",
                    ),
                    (
                        "wijzig",
                        "Verander of controleer de koppeling; verpakking zet je hier ook bij.",
                    ),
                    (
                        "ontkoppel",
                        "Haalt de koppeling weg. Het product komt terug in de wachtrij en wordt niet automatisch opnieuw gekoppeld.",
                    ),
                    ("filter", "Typ om de tabel live te filteren op product of artikelnaam."),
                    (
                        "pakgrootte",
                        "'onbekend' betekent: de planner koopt 1 pak per keer tot je hier de verpakkingssorte zet.",
                    ),
                ],
            ),
        ],
    },
    "match-one": {
        "title": "Koppelen",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "huidige koppeling",
                        "Het artikel waar dit product nu aan hangt, met ontkoppel erbij.",
                    ),
                    (
                        "verpakking",
                        "Hoeveel zit er in één pak (bijv. 500 g of 6 stuks)? Hiermee rekent de planner hele pakken uit in de bestelkolom. Een stukgewicht (per stuk) pas je op de instellingenpagina aan.",
                    ),
                    (
                        "of plak zelf een AH-id of product-URL",
                        "Heb je het artikel open in ah.nl? Plak de URL of het cijfermatige id; de foto en verpakking worden er meteen bijgehaald als AH het artikel kent.",
                    ),
                    (
                        "suggesties",
                        "AH's zoekresultaten voor dit product; klik koppel bij de juiste.",
                    ),
                    ("terug", "Terug naar waar je vandaan kwam (recept of koppel-wachtrij)."),
                ],
            ),
        ],
    },
    "order-match": {
        "title": "Koppelingen van deze week",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "Nog te koppelen",
                        "Alleen de producten die op deze weeklijst staan zonder koppeling.",
                    ),
                    (
                        "Gekoppeld",
                        "De koppelingen die deze week gebruikt worden, met foto, artikel en ontkoppel.",
                    ),
                    (
                        "verpakking",
                        "Zet hier de verpakkingssorte als de planner niet uit het aantal komt.",
                    ),
                ],
            ),
        ],
    },
    "order": {
        "title": "Boodschappenlijst",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "product",
                        "Wat er gekocht moet worden, met van welke recepten/gerechten het komt.",
                    ),
                    ("hoeveelheid", "Aantal verpakkingen (×) en wat er in een pak zit."),
                    (
                        "AH-link",
                        "Opent de AH-mijnlijst met alle gekoppelde artikelen in de juiste aantallen (ingelogd).",
                    ),
                    ("QR-code", "Zelfde lijst, te scannen met je telefoon."),
                    (
                        "voorraad",
                        "Vink aan wat je al in huis hebt (bijv. olie, zout): het gaat uit de link en de QR.",
                    ),
                    (
                        "bevestig",
                        "Zet de dagen van deze week op 'besteld'; daarna kan het gerecht niet meer zonder meer gewisseld worden.",
                    ),
                    (
                        "+ / −",
                        "Regel toevoegen aan of van de lijst (bijv. iets dat op is maar niet op het menu stond).",
                    ),
                ],
            ),
        ],
    },
    "order-link": {
        "title": "Naar AH",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "QR-code",
                        "Scan met de telefoon die ingelogd is op ah.nl; de artikelen komen in je AH-mijnlijst.",
                    ),
                    ("link", "Werkt ook in de browser op de telefoon zelf."),
                    ("bevestig", "Markeer de dagen als besteld zodat de weektabel het weet."),
                ],
            ),
        ],
    },
    "settings": {
        "title": "Instellingen",
        "sections": [
            (
                "Deze pagina",
                [
                    (
                        "personen",
                        "Wie er meedoen; de planner rekent porties per avond op basis van wie aanwezig is.",
                    ),
                    (
                        "week begint op",
                        "Op welke dag een nieuwe week start; het plan verankert hieraan.",
                    ),
                    (
                        "weeksjabloon",
                        "Per weekdag of er gekookt wordt, hoeveel minuten er zijn en of restjes mogen.",
                    ),
                    (
                        "productfeiten",
                        "Verpakkingssortes en stukgewichten per product (bijv. 500 g per pak, 60 g per ei). Hier regel je wat de koppelpagina niet doet.",
                    ),
                    (
                        "staples",
                        "Vaste producten die standaard niet meebesteld worden maar met een vinkje op de bestellijst kunnen.",
                    ),
                    ("thema", "Licht of donker; onthouden per browser."),
                    ("backup", "Download een zip van alle data of herstel er een."),
                ],
            ),
        ],
    },
    "default": {
        "title": "Deze pagina",
        "sections": [
            (
                "Navigatie",
                [
                    ("Week", "Het weekplan: dagen invullen, goedkeuren en bestellen."),
                    ("Recepten", "Alle recepten, importeren en bewerken."),
                    ("Gerechten", "Samengestelde avondmenus van onderdelen."),
                    ("Vriezer", "Ingevroren porties en wat gepland is."),
                    ("Koppelingen", "Producten aan AH-artikelen hangen."),
                    ("Instellingen", "Personen, weekinstellingen, verpakkingen, backup."),
                    ("thema", "Wissel tussen licht en donker."),
                ],
            ),
        ],
    },
}

_FALLBACK = PAGE_HELP["default"]


def page_key(path: str) -> str:
    """Map a request path to the help text that belongs to it."""
    if path == "/" or path == "/plan" or path.startswith("/plan/"):
        return "plan-select" if path.startswith("/plan/select") else "plan"
    if path.startswith("/recipes/photos") or path.startswith("/recipes/ocr"):
        return "recipes-photos"
    if path.startswith("/recipes/import"):
        return "recipes-import"
    if path == "/recipes" or path.startswith("/recipes/"):
        if path.endswith("/edit") or path == "/recipes/new":
            return "recipe-edit"
        if path.endswith("/export") or "/img/" in path:
            return "recipe"
        if path.count("/") == 2:  # /recipes/{slug}
            return "recipe"
        return "recipes"
    if path == "/dishes" or path == "/components":
        return path[1:]
    if path.startswith("/dishes/") or path.startswith("/components/"):
        return "dishes" if path.startswith("/dishes/") else "components"
    if path.startswith("/vriezer"):
        return "vriezer"
    if path == "/matches" or path.startswith("/matches/"):
        return "matches"
    if path.startswith("/order/match/one"):
        return "match-one"
    if path.startswith("/order/match"):
        return "order-match"
    if path == "/order" or path.startswith("/order/link"):
        return "order-link" if path.startswith("/order/link") else "order"
    if path.startswith("/settings"):
        return "settings"
    return "default"


def help_for(path: str) -> dict:
    """The help block for this path; the default for unlisted pages."""
    return PAGE_HELP.get(page_key(path), _FALLBACK)
