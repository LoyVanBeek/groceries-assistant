# Tickets

Work split from the composed-dish / pantry / planner design. One commit per ticket,
in the order listed. Each ticket lists what it blocks and what blocks it.

| # | Ticket | Blocked by |
|---|--------|-----------|
| 1 | [Dishes: models + loader](01-dishes-models-loader.md) | - |
| 2 | [Dishes: plan picks + grocery plumbing](02-dishes-picks-and-groceries.md) | 1 |
| 3 | [Dishes: definition pages at /dishes](03-dishes-web-pages.md) | 1 |
| 4 | [Dishes: week view with per-part picks](04-dishes-week-view.md) | 2, 3 |
| 5 | [Pantry: product facts + AH unit size](05-pantry-product-facts.md) | - |
| 6 | [Pantry: day-aware demand + allocation](06-pantry-allocation.md) | 5 |
| 7 | [Pantry: order page, settings, prefill](07-pantry-web.md) | 6 |
| 8 | [Planner: composed dishes in plan_week](08-planner-dishes.md) | 2, 3 |
| 9 | [Planner: persist picks + reasons + verdicts](09-planner-picks-and-reasons.md) | 8 |

## Design in one page

**Composed dish** (`data/dishes/<slug>.yaml`): a dish assembled from parts, each
part an explicit list of recipe slugs that can fill it. Hand-picked in the week
view: one `<select>` per part, posted to `/plan/picks`. `plan_entries.recipe_slug`
holds the dish slug, so swap/leftovers/approve all keep working. Groceries are the
sum of the chosen parts, each scaled by its own `servings`. Cooking time is the
longest part, with the total shown as a warning.

**Pantry** (no leftovers bookkeeping in the plan): a product may carry a *fact* --
how one purchase looks and how long it keeps (`broccoli: 1 stuk = 350 g, 3 dagen`).
The order is then built day by day instead of as a week total: each day eats
in-date stock (oldest first) and buys the shortfall in whole units; whatever a
unit has left over joins the stock with an expiry. Carried-in stock is derived
from the previous approved plan (`order_items` minus its demand), so there is no
state to maintain and the order page stays read-only. Products without a fact are
unaffected.

**Planner**: `plan_week` scores a dish as the mean of its parts' scores (a verdict
on the dish's own slug replaces that mean), `never` on a dish slug excludes the
whole dish, and each part's option is filled with the engine's existing per-option
penalties. Picks are persisted with the proposal.
