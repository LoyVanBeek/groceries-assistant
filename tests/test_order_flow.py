"""From recipe to pack quantity: the whole order flow, one and combined.

The chain under test: recipe ingredients (in recipe units, scaled by the
people count) -> `build_order` -> `ah_pack_qty` (whole AH packs, from declared
pack arithmetic or AH's own size) -> the quantities that end up in the
`add-multiple` deep link. Staples ride the same chain only when asked for.
"""

from datetime import date
from urllib.parse import unquote

from groceries_assistant.ah.deeplink import build_add_multiple_url
from groceries_assistant.models import ProductFact, Recipe
from groceries_assistant.services.planner_service import (
    ah_pack_qty,
    build_order,
    rows_to_entries,
)
from groceries_assistant.store.db import PlanEntryRow, Store

D1 = date(2026, 11, 23)


def _recipe(slug, ingredients, servings=4):
    return Recipe.model_validate(
        {
            "slug": slug,
            "name": slug,
            "servings": servings,
            "time": {"cook": 30},
            "ingredients": ingredients,
        }
    )


def _order(*recipes, people=4, staples=(), facts=None):
    """One cook evening with the given recipes, scaled by `people`."""
    rows = [
        PlanEntryRow(day=D1, kind="cook", recipe_slug=recipe.slug, people=people)
        for recipe in recipes
    ]
    entries = rows_to_entries(rows, {r.slug: r for r in recipes})
    return build_order(entries, staples=list(staples), facts=facts or {})


# --- one recipe: scaling + pack conversion -------------------------------------


def test_a_recipe_scales_then_rounds_up_to_whole_packs():
    """500 g spaghetti for 4, cooked for 8: 1000 g -> two 500 g packs."""
    pasta = _recipe("pasta", [{"product": "spaghetti", "amount": 500, "unit": "g"}])
    order = _order(pasta, people=8)
    [line] = order.shopping
    assert line.amount == 1000.0 and line.unit == "g"
    facts = {"spaghetti": ProductFact(product="spaghetti", pack_qty=500, pack_unit="g")}
    pack = ah_pack_qty(line.product, line.amount, line.unit, line.pack_count, facts)
    assert pack.qty == 2 and pack.source == "pack"


def test_a_fact_lets_the_allocation_count_the_packs_itself():
    """With a declared fact, build_order already does the pack arithmetic: the
    line carries what the packs add up to, and the count in its note."""
    pasta = _recipe("pasta", [{"product": "spaghetti", "amount": 750, "unit": "g"}])
    facts = {"spaghetti": ProductFact(product="spaghetti", pack_qty=500, pack_unit="g")}
    order = _order(pasta, facts=facts)
    [line] = order.shopping
    assert line.amount == 1000.0 and line.note == "2 x 500 g"
    assert ah_pack_qty(line.product, line.amount, line.unit, line.pack_count, facts).qty == 2


def test_a_spoons_request_buys_one_pack_and_gets_flagged():
    """'2 el olijfolie' fits no pack measure: one bottle, for a human to check."""
    soep = _recipe("soep", [{"product": "olijfolie", "amount": 2, "unit": "el"}])
    order = _order(soep)
    [line] = order.shopping
    pack = ah_pack_qty(line.product, line.amount, line.unit, None, {}, {"olijfolie": "500 ml"})
    assert pack.qty == 1 and pack.source == "unknown"


# --- several recipes combined ---------------------------------------------------


def test_two_recipes_share_one_onion_bag():
    """2 ui + 2 ui = 4 stuks; a 6-piece net is one purchase, not four."""
    a = _recipe("a", [{"product": "ui", "amount": 2, "unit": "stuks"}])
    b = _recipe("b", [{"product": "ui", "amount": 2, "unit": "stuks"}])
    order = _order(a, b)
    [line] = order.shopping
    assert line.amount == 4.0
    pack = ah_pack_qty(line.product, line.amount, line.unit, None, {}, {"ui": "6 stuks"})
    assert pack.qty == 1 and pack.source == "derived"


def test_two_courses_on_one_evening_each_buy_their_own_count():
    """A soep for 2 and a hoofdgerecht for 4 on the same evening: the soep is
    bought for 2, not for whoever's row happened to come last."""
    soep = _recipe("soep", [{"product": "soepgroente", "amount": 200, "unit": "g"}], servings=2)
    hoofd = _recipe("hoofd", [{"product": "spaghetti", "amount": 500, "unit": "g"}], servings=4)
    rows = [
        PlanEntryRow(day=D1, kind="cook", recipe_slug="soep", people=2, course="soep"),
        PlanEntryRow(day=D1, kind="cook", recipe_slug="hoofd", people=4),
    ]
    entries = rows_to_entries(rows, {r.slug: r for r in (soep, hoofd)})
    order = build_order(entries, staples=[])
    amounts = {line.product: line.amount for line in order.shopping}
    assert amounts["soepgroente"] == 200.0  # 2 persons, untouched by the other course
    assert amounts["spaghetti"] == 500.0


def test_combining_scales_with_each_recipe_s_own_people_count():
    """Two recipes, each cooked for its own count, sum their demands."""
    a = _recipe("a", [{"product": "spaghetti", "amount": 500, "unit": "g"}])
    b = _recipe("b", [{"product": "spaghetti", "amount": 500, "unit": "g"}], servings=2)
    rows = [
        PlanEntryRow(day=D1, kind="cook", recipe_slug="a", people=4),
        PlanEntryRow(day=D1, kind="cook", recipe_slug="b", people=6),
    ]
    entries = rows_to_entries(rows, {r.slug: r for r in (a, b)})
    order = build_order(entries, staples=[])
    [line] = order.shopping
    # a for 4 (500 g) + b for 6 (6/2 * 500 = 1500 g)
    assert line.amount == 2000.0
    facts = {"spaghetti": ProductFact(product="spaghetti", pack_qty=500, pack_unit="g")}
    assert ah_pack_qty(line.product, line.amount, line.unit, line.pack_count, facts).qty == 4


def test_one_product_in_two_units_stays_two_lines():
    """ui in stuks and ui in grams cannot share a line; each converts alone."""
    a = _recipe("a", [{"product": "ui", "amount": 2, "unit": "stuks"}])
    b = _recipe("b", [{"product": "ui", "amount": 300, "unit": "g"}])
    order = _order(a, b)
    by_unit = {line.unit: line for line in order.shopping}
    assert set(by_unit) == {"stuks", "g"}
    pieces = ah_pack_qty("ui", by_unit["stuks"].amount, "stuks", None, {}, {"ui": "6 stuks"})
    grams = ah_pack_qty("ui", by_unit["g"].amount, "g", None, {}, {"ui": "6 stuks"})
    # 2 stuks fits the 6-piece net; 300 g of an unreadable-weight net is unknown
    assert (pieces.qty, pieces.source) == (1, "derived")
    assert grams.source == "unknown"


def test_the_deep_link_carries_the_pack_quantities():
    """The URL the QR code encodes: one p=<id>:<packs> per matched product."""
    a = _recipe("a", [{"product": "spaghetti", "amount": 750, "unit": "g"}])
    b = _recipe("b", [{"product": "ui", "amount": 8, "unit": "stuks"}])
    order = _order(a, b)
    matched = {"spaghetti": 4321, "ui": 4322}
    sizes = {"spaghetti": "500 g", "ui": "6 stuks"}
    pairs = [
        (
            matched[line.product],
            ah_pack_qty(line.product, line.amount, line.unit, line.pack_count, {}, sizes).qty,
        )
        for line in order.shopping
        if line.product in matched
    ]
    assert pairs == [(4321, 2), (4322, 2)]  # 750 g / 500 g, 8 stuks / 6 stuks
    assert "p=4321%3A2" in build_add_multiple_url(pairs)
    assert "p=4322%3A2" in build_add_multiple_url(pairs)


# --- staples ---------------------------------------------------------------------


def test_staples_stay_off_the_list_until_asked_for():
    """The default: a staple is split off, so it lands on no order at all."""
    soep = _recipe(
        "soep",
        [
            {"product": "olijfolie", "amount": 2, "unit": "el"},
            {"product": "ui", "amount": 2, "unit": "stuks"},
        ],
    )
    order = _order(soep, staples=["olijfolie"])
    assert [line.product for line in order.shopping] == ["ui"]
    assert [line.product for line in order.staples] == ["olijfolie"]


def test_a_wanted_staple_is_ordered_like_any_product():
    """Asked for, the staple flows through with its own pack conversion."""
    soep = _recipe("soep", [{"product": "olijfolie", "amount": 2, "unit": "el"}])
    # the caller 'un-staples' it (the with=/plan_staples mechanism)
    order = _order(soep, staples=[])
    [line] = order.shopping
    assert line.product == "olijfolie"
    pack = ah_pack_qty(line.product, line.amount, line.unit, None, {}, {"olijfolie": "500 ml"})
    assert pack.qty == 1  # two spoons, one bottle -- never two


# --- web: the staples checklist and the inline block ----------------------------


class FakeProduct:
    def __init__(self, webshop_id, title, unit_size="500 ml"):
        self.webshop_id = webshop_id
        self.title = title
        self.unit_size = unit_size


def _search(query):
    sizes = {"tomaten": "500 g", "olijfolie": "500 ml", "spaghetti": "500 g"}
    return [FakeProduct(555, "AH " + query, sizes.get(query, "1 stuks"))]


def test_the_order_staples_checklist_puts_a_staple_on_the_list(tmp_path):
    """Ticking olijfolie on /order puts it on the saved list, matched and
    riding the AH link; unticking takes it off again."""
    from tests.test_web import make_client, seed_plan, seed_recipe

    seed_recipe(tmp_path)
    with make_client(tmp_path, search_fn=_search) as client:
        seed_plan(client)
        store = Store(tmp_path / "state.db")
        plan_id = store.latest_plan()[0]
        cook = next(r for r in store.plan_entries(plan_id) if r.kind == "cook")
        client.post("/plan/approve", data={"day": cook.day.isoformat()}, follow_redirects=False)
        client.post("/plan/order", follow_redirects=False)
        store.set_staple("olijfolie", True)

        # default: the checklist shows it unticked and the list lacks it
        page = client.get("/order").text
        assert 'name="product" value="olijfolie"' in page
        assert 'value="olijfolie" checked' not in page
        assert ("olijfolie", "el") not in {
            (i["product"], i["unit"]) for i in store.order_items(plan_id)
        }

        # tick it: it joins the list (restock: pasta uses no olijfolie),
        # matched, and rides the link
        client.post("/order/staples", data={"product": "olijfolie"}, follow_redirects=False)
        items = {i["product"].lower(): i for i in store.order_items(plan_id)}
        assert "olijfolie" in items
        assert items["olijfolie"]["webshop_id"] == 555
        assert store.plan_staples(plan_id) == {"olijfolie"}
        import re
        from urllib.parse import unquote

        url = unquote(
            re.search(r'href="([^"]*add-multiple[^"]*)"', client.get("/order/link").text).group(1)
        )
        assert "555:1" in url  # one pack

        # untick: off the list and out of the links again
        client.post("/order/staples", data={}, follow_redirects=False)
        assert store.plan_staples(plan_id) == set()
        assert "olijfolie" not in {i["product"].lower() for i in store.order_items(plan_id)}


def test_a_ticked_staple_survives_a_list_rebuild(tmp_path):
    """Approve/order again: the ticked staple is bought, not silently dropped."""
    from tests.test_web import make_client, seed_plan, seed_recipe

    seed_recipe(tmp_path)
    with make_client(tmp_path, search_fn=_search) as client:
        seed_plan(client)
        store = Store(tmp_path / "state.db")
        plan_id = store.latest_plan()[0]
        cook = next(r for r in store.plan_entries(plan_id) if r.kind == "cook")
        store.set_staple("olijfolie", True)
        store.add_plan_staple(plan_id, "olijfolie")
        client.post("/plan/approve", data={"day": cook.day.isoformat()}, follow_redirects=False)
        client.post("/plan/order", follow_redirects=False)
        assert "olijfolie" in {i["product"].lower() for i in store.order_items(plan_id)}
        # and a second rebuild keeps it
        client.post("/plan/order", follow_redirects=False)
        assert "olijfolie" in {i["product"].lower() for i in store.order_items(plan_id)}


def test_a_ticked_staple_not_cooked_this_week_is_a_restock(tmp_path):
    """A staple with no demand in the plan still lands on the list as one pack
    -- that is the irregular restock the checklist exists for."""
    from tests.test_web import make_client, seed_plan, seed_recipe

    seed_recipe(tmp_path)
    with make_client(tmp_path, search_fn=_search) as client:
        seed_plan(client)
        store = Store(tmp_path / "state.db")
        plan_id = store.latest_plan()[0]
        cook = next(r for r in store.plan_entries(plan_id) if r.kind == "cook")
        client.post("/plan/approve", data={"day": cook.day.isoformat()}, follow_redirects=False)
        client.post("/plan/order", follow_redirects=False)
        store.set_staple("rijst", True)  # nothing this week cooks rijst
        client.post("/order/staples", data={"product": "rijst"}, follow_redirects=False)
        items = {i["product"].lower(): i for i in store.order_items(plan_id)}
        assert items["rijst"]["webshop_id"] == 555


def test_the_recipe_block_orders_a_staple_on_request(tmp_path):
    """&with=olijfolie orders it as an item (voorraad), the rest get a + link,
    and the regenerate form keeps the choice."""
    from tests.test_web import make_client

    soep = (
        "name: Tomatensoep\nservings: 4\ntime: {cook: 30}\ningredients:\n"
        "  - {product: tomaten, amount: 800, unit: g}\n"
        "  - {product: olijfolie, amount: 2, unit: el}\nsteps:\n  - Kook.\n"
    )
    (tmp_path / "recipes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "recipes" / "tomatensoep.yaml").write_text(soep, encoding="utf-8")

    with make_client(tmp_path, search_fn=_search) as client:
        # without with=: olijfolie is a staple on this recipe (if marked) and
        # shows with a + link; tomaten is a normal item
        store = Store(tmp_path / "state.db")
        store.set_staple("olijfolie", True)
        page = client.get("/recipes/tomatensoep", params={"people": 4}).text
        # 800 g tomaten against AH's 500 g pack: two packs, in the table
        assert "order-table" in page
        assert "2 &times;" in page
        assert "tomaten" in page
        assert "+ op de lijst" in page and "olijfolie" in page
        assert "(voorraad)" not in page  # not ordered, so not marked

        # with=olijfolie: it becomes an ordered item, marked voorraad, with a
        # minus to take it off, and the AH link includes it
        page = client.get("/recipes/tomatensoep", params={"people": 4, "with": "olijfolie"}).text
        assert "(voorraad)" in page
        assert "&minus;" in page
        assert "people=4" in page and 'name="with" value="olijfolie"' in page


def test_the_recipe_table_scales_and_ticking_in_house_updates_the_link(tmp_path):
    """?people=8 doubles the ingredient amounts and the table shows recipe
    quantity next to the order quantity; ticking 'in huis' drops that product
    from the AH link and QR, and keeps the tick when regenerating."""
    from tests.test_web import make_client

    pasta = (
        "name: Pasta\nservings: 4\ntime: {cook: 30}\ningredients:\n"
        "  - {product: spaghetti, amount: 500, unit: g}\n"
        "  - {product: ui, amount: 2, unit: stuks}\nsteps:\n  - Kook.\n"
    )
    (tmp_path / "recipes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "recipes" / "pasta.yaml").write_text(pasta, encoding="utf-8")

    class FakeProduct:
        def __init__(self, webshop_id, title, unit_size):
            self.webshop_id = webshop_id
            self.title = title
            self.unit_size = unit_size

    def search(query):
        sizes = {"spaghetti": "500 g", "ui": "6 stuks"}
        return [FakeProduct(1000 + len(query), "AH " + query, sizes.get(query, "1 stuks"))]

    with make_client(tmp_path, search_fn=search) as client:
        # the combo is always there, at the recipe's own servings by default
        plain = client.get("/recipes/pasta").text
        assert "<td>500 g</td>" in plain  # unscaled recipe column
        assert "order-table" in plain
        assert 'id="order-combo"' in plain

        page = client.get("/recipes/pasta", params={"people": 8}).text
        assert "voor 8 personen" in page
        assert "1000 g" in page  # the recipe column scales with the eaters
        assert "order-table" in page
        assert ">bestellen<" in page

        def link(page_text):
            for chunk in page_text.split('href="')[1:]:
                if "add-multiple" in chunk:
                    return unquote(chunk.split('"')[0])

        # spaghetti for 8 = 1000 g -> two 500 g packs; ui for 8 = 4 stuks -> 1 net
        assert "1009:2" in link(page)
        assert "1002:1" in link(page)

        # in huis: the spaghetti pair disappears from the link, the row dims
        ticked = client.get("/recipes/pasta", params={"people": 8, "have": "spaghetti"}).text
        assert "1009:2" not in link(ticked)
        assert "1002:1" in link(ticked)
        assert "in huis" in ticked
        # the regenerate form keeps the tick (and it round-trips)
        assert 'name="have" value="spaghetti"' in ticked

        # without the tick the pair is back
        assert "1009:2" in link(client.get("/recipes/pasta", params={"people": 8}).text)
