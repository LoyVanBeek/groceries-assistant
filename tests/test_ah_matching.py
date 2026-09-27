from groceries_assistant.ah.client import AHProduct
from groceries_assistant.ah.matching import (
    AHMapping,
    AHMappingStore,
    PackSize,
    fact_from_unit_size,
    normalize,
    parse_unit_size,
    similarity,
    suggest_match,
)


def test_normalize_stops_punctuation_and_stopwords():
    assert normalize("Pasta, penne (vers)!") == "pasta penne"
    assert normalize("AH huismerk melk 500 g") == "melk"
    assert normalize("Rijst 1000 gram") == "rijst"


def test_similarity_exact_and_unrelated():
    assert similarity("pesto", "AH Pesto rosso 190 g") > 0.4
    assert similarity("pesto", "Wasparientabletten") < 0.2


def test_suggest_match_picks_best_candidate():
    candidates = [
        AHProduct(1, "Wasparientabletten 40 stuks"),
        AHProduct(2, "AH Pesto alla Genovese 190 g"),
        AHProduct(3, "Pesto rosso 190 g"),
    ]
    best, score = suggest_match("pesto", candidates)
    assert best is not None
    assert best.webshop_id in (2, 3)
    assert score >= 0.45


def test_suggest_match_returns_none_below_threshold():
    candidates = [AHProduct(1, "Wasparientabletten 40 stuks")]
    best, _score = suggest_match("pesto", candidates, min_score=0.9)
    assert best is None


def test_mapping_store_round_trip(tmp_path):
    path = tmp_path / "mappings.yaml"
    store = AHMappingStore(path)
    store.put(
        AHMapping(
            product="pasta penne",
            ah_webshop_id=12345,
            ah_title="AH Penne 500 g",
            score=0.8,
            confirmed=True,
        )
    )
    store.save()

    reloaded = AHMappingStore(path)
    mapping = reloaded.get("PASTA PENNE")  # case-insensitive lookup
    assert mapping is not None
    assert mapping.ah_webshop_id == 12345
    assert mapping.ah_title == "AH Penne 500 g"
    assert mapping.confirmed is True


def test_mapping_store_skips_null_entries(tmp_path):
    path = tmp_path / "mappings.yaml"
    path.write_text(
        "mappings:\n  pasta penne: null\n  courgette: {ah_webshop_id: 9, ah_title: X}\n"
    )
    store = AHMappingStore(path)
    assert store.get("pasta penne") is None
    assert store.get("courgette") is not None


def test_mapping_store_keeps_ahs_pack_size(tmp_path):
    path = tmp_path / "mappings.yaml"
    store = AHMappingStore(path)
    store.put(
        AHMapping(
            product="broccoli",
            ah_webshop_id=7,
            ah_title="AH Broccoli 350 g",
            score=1.0,
            ah_unit_size="350 g",
        )
    )

    assert AHMappingStore(path).get("broccoli").ah_unit_size == "350 g"


def test_parse_unit_size_reads_the_plain_shapes():
    assert parse_unit_size("350 g") == PackSize(qty=350, unit="g", is_weight=True)
    assert parse_unit_size("1kg") == PackSize(qty=1, unit="kg", is_weight=True)
    assert parse_unit_size("2 x 100 g") == PackSize(qty=200, unit="g", is_weight=True)
    assert parse_unit_size("6 stuks") == PackSize(qty=6, unit="stuk", is_weight=False)


def test_parse_unit_size_refuses_what_it_cannot_read():
    # a wrong guess becomes a wrong shopping list, so these stay None
    for text in (None, "", "per 100 g", "1 fles 750ml", "2 x 3 stuks", "onbekend", "4x250ml!"):
        assert parse_unit_size(text) is None, text


def test_a_fact_from_a_pack_size_describes_one_purchase():
    fact = fact_from_unit_size("broccoli", "350 g")
    assert (fact.pack_qty, fact.pack_unit, fact.unit_qty) == (350, "g", None)
    assert fact.pack_contents == 350  # grams, not pieces: the weight is what we know


def test_a_fact_from_loose_pieces_claims_no_weight():
    fact = fact_from_unit_size("tomaat", "6 stuks")
    assert (fact.pack_qty, fact.pack_unit) == (6, "stuk")
    assert fact.unit_qty is None  # the user has to weigh one, not us


def test_an_unreadable_pack_size_suggests_nothing():
    assert fact_from_unit_size("courgette", "per 100 g") is None
