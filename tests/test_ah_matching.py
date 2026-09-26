from groceries_assistant.ah.client import AHProduct
from groceries_assistant.ah.matching import (
    AHMapping,
    AHMappingStore,
    normalize,
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
