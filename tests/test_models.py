"""The rules the product-fact model enforces before a pack size reaches the list."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from groceries_assistant.models import ProductFact


def test_a_pack_of_weight_is_its_own_quantity():
    fact = ProductFact(product="rijst", pack_qty=500, pack_unit="g")
    assert fact.pack_measure == "g"
    assert fact.pack_contents == 500
    assert fact.unit_qty is None


def test_a_pack_of_pieces_knows_the_weight_of_one():
    fact = ProductFact(product="broccoli", pack_qty=2, unit_qty=350, unit_measure="g")
    assert fact.pack_measure == "g"
    assert fact.pack_contents == 700  # two pieces of 350 g


def test_a_piece_weight_needs_both_halves():
    with pytest.raises(ValidationError, match="go together"):
        ProductFact(product="broccoli", pack_qty=1, unit_qty=350)
    with pytest.raises(ValidationError, match="go together"):
        ProductFact(product="broccoli", pack_qty=1, unit_measure="g")


def test_a_pack_must_hold_something():
    with pytest.raises(ValidationError):
        ProductFact(product="broccoli", pack_qty=0)


def test_a_shelf_life_is_a_number_of_days():
    fact = ProductFact(product="broccoli", pack_qty=1)
    assert fact.shelf_life_days == 3
    assert fact.shelf_life == timedelta(days=3)
    with pytest.raises(ValidationError):
        ProductFact(product="broccoli", pack_qty=1, shelf_life_days=-1)
