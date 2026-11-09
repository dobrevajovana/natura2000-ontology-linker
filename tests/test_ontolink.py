"""Offline tests for geoapp's OLS4 hit classification: no network.

Run:  python -m pytest -q
"""

import pytest

from geoapp.ontolink import classify_hit

FECES = {
    "label": "feces", "obo_id": "UBERON:0001988", "short_form": "UBERON_0001988",
    "exact_synonyms": ["faeces", "stool", "fecal matter"],
    "narrow_synonyms": ["scat", "guano"],
    "broad_synonyms": ["excreta"],
    "related_synonyms": ["droppings"],
}
LAGOON = {"label": "lagoon", "exact_synonyms": []}
LARGE_INTESTINE = {"label": "large intestine", "exact_synonyms": ["intestinum crassum"]}
INTESTINE_SEROSA = {"label": "serosa of intestine", "exact_synonyms": []}


@pytest.mark.parametrize("value, doc, expected", [
    ("feces", FECES, "exactMatch"),
    ("Stool", FECES, "exactMatch"),            # exact synonym, any case
    ("fecal_matter", FECES, "exactMatch"),     # MIxS-style underscores
    ("UBERON:0001988", FECES, "exactMatch"),   # shared ID
    ("scat", FECES, "broadMatch"),             # narrow synonym -> the term is broader
    ("excreta", FECES, "narrowMatch"),         # broad synonym -> the term is narrower
    ("droppings", FECES, "relatedMatch"),
    ("coastal lagoon", LAGOON, "broadMatch"),  # head noun: lagoon is broader
    ("intestine", LARGE_INTESTINE, "narrowMatch"),
])
def test_structural_match_types(value, doc, expected):
    mtype, _ = classify_hit(value, doc)
    assert mtype == expected


def test_near_identical_spelling_is_close_not_exact():
    mtype, score = classify_hit("faecess", FECES)
    assert mtype == "closeMatch" and score < 1.0


def test_trailing_words_after_a_preposition_are_not_the_head_noun():
    # 'serosa of intestine' is a serosa, not a kind of intestine
    assert classify_hit("intestine", INTESTINE_SEROSA) is None or \
        classify_hit("intestine", INTESTINE_SEROSA)[0] not in ("narrowMatch", "broadMatch")


def test_unrelated_hit_is_dropped():
    assert classify_hit("liver", FECES) is None


# --- query-level behaviour, with OLS4 stubbed ---------------------------------

from geoapp import ontolink


def _doc(curie, label, **syn):
    ns, num = curie.split(":")
    return {"iri": f"http://purl.obolibrary.org/obo/{ns}_{num}", "label": label, "obo_id": curie, **syn}


def _stub_ols4(monkeypatch, docs):
    monkeypatch.setattr(ontolink, "_request_json", lambda *a, **k: None if docs is None else {"response": {"docs": docs}})


def test_hits_are_ranked_by_match_type_not_ols4_order(monkeypatch):
    _stub_ols4(monkeypatch, [_doc("ENVO:00002150", "coastal sea water"), _doc("ENVO:00002149", "sea water")])
    hits = ontolink.ols4_search("sea water", "envo", None)
    assert [h.curie for h in hits][:2] == ["ENVO:00002149", "ENVO:00002150"]
    assert hits[0].match_type == "exactMatch"


def test_a_name_shared_by_two_terms_is_not_an_exact_match(monkeypatch):
    _stub_ols4(monkeypatch, [_doc("ENVO:01001243", "forest"), _doc("ENVO:00000111", "forested area", exact_synonyms=["forest"])])
    hits = ontolink.ols4_search("forest", "envo", None)
    assert {h.match_type for h in hits} == {"closeMatch"}


def test_a_failed_lookup_is_not_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(ontolink.config, "CACHE_DIR", tmp_path)
    _stub_ols4(monkeypatch, None)
    assert ontolink._cached_lookup("ols4", "soil", "envo", None, True) == []
    _stub_ols4(monkeypatch, [_doc("ENVO:00001998", "soil")])
    [hit] = ontolink._cached_lookup("ols4", "soil", "envo", None, True)
    assert hit.curie == "ENVO:00001998"


# --- validation harness scoring ------------------------------------------------

from geoapp.schema import OntologyTerm
from validation.benchmark import Case, evaluate, load_gold


def _term(curie, mtype="exactMatch", conf=1.0):
    return OntologyTerm(uri="", curie=curie, source="ENVO", match_type=mtype, confidence=conf)


def test_evaluate_scores_recall_top1_bands_and_negatives():
    cases = [
        Case("t", "soil", "envo", {"ENVO:1"}, "exactMatch"),
        Case("t", "bog", "envo", {"ENVO:2"}, "exactMatch", wrong_ids={"ENVO:9"}),
        Case("t", "nothing", "envo", {"ENVO:3"}),
    ]
    hits = {"soil": [_term("ENVO:1")], "bog": [_term("ENVO:9", "narrowMatch", 0.85), _term("ENVO:2", "closeMatch", 0.9)],
            "nothing": []}
    summary, rows = evaluate(cases, lambda text, ont, branch, also: hits[text])
    assert summary["recall_at_3"] == pytest.approx(2 / 3)
    assert summary["top1_correct"] == pytest.approx(1 / 3)
    assert summary["no_hit"] == pytest.approx(1 / 3)
    assert summary["known_wrong_returned"] == 1
    assert summary["bands"]["1.00"] == (1, 1) and summary["bands"]["0.85"] == (0, 1)
    assert summary["types"] == {("exactMatch", "exactMatch"): 1, ("exactMatch", "closeMatch"): 1}
    assert rows[1]["known_wrong_returned"] == "ENVO:9"


def test_load_gold_uses_the_path_leaf_and_keeps_negatives(tmp_path):
    p = tmp_path / "g.tsv"
    p.write_text("# curie_map: {}\n"
                 "subject_id\tsubject_label\tpredicate_id\tobject_id\n"
                 "G:1\tEnvironmental > Aquatic > Whale fall\tskos:exactMatch\tENVO:01000140\n"
                 "G:2\tEnvironmental > Aquatic > Thalassic\towl:differentFrom\tENVO:01001667\n", encoding="utf-8")
    whale, thal = load_gold(p)
    assert (whale.text, whale.gold_ids, whale.gold_predicate) == ("Whale fall", {"ENVO:01000140"}, "exactMatch")
    assert thal.gold_ids == set() and thal.wrong_ids == {"ENVO:01001667"}


@pytest.mark.parametrize("value, doc, heads, expected", [
    ("sea water", {"label": "ocean water", "exact_synonyms": ["seawater"]}, frozenset(), "exactMatch"),
    ("wetland", {"label": "wetland area"}, frozenset({"biome", "area"}), "closeMatch"),
    ("grassland", {"label": "grassland biome"}, frozenset({"biome", "area"}), "closeMatch"),
])
def test_spacing_variants_and_field_head_words(value, doc, heads, expected):
    assert classify_hit(value, doc, heads)[0] == expected


def test_field_head_words_only_apply_inside_a_branch():
    assert classify_hit("wetland", {"label": "wetland area"})[0] != "closeMatch"


def test_env_broad_scale_searches_the_biome_branch_then_falls_back(monkeypatch):
    calls = []

    def fake(session, url, *, params, **kw):
        calls.append(params.get("allChildrenOf", ""))
        return {"response": {"docs": [] if "allChildrenOf" in params else [_doc("ENVO:00000111", "forest")]}}

    monkeypatch.setattr(ontolink, "_request_json", fake)
    [hit] = ontolink.ols4_search("forest", "envo", None, branch="biome")
    assert calls[0] == ontolink.ENVO_BRANCHES["biome"][0] and calls[-1] == ""
    assert hit.curie == "ENVO:00000111"


@pytest.mark.parametrize("value, label, expected", [
    ("soils", "soil", "closeMatch"),             # plural only
    ("marine sediments", "marine sediment", "closeMatch"),
    ("desert", "dessert", "relatedMatch"),       # similar spelling, different thing
    ("greenhouse", "greenhouse soil", "relatedMatch"),   # a soil, not a kind of greenhouse
])
def test_similarity_only_reaches_closematch_for_plurals(value, label, expected):
    assert classify_hit(value, {"label": label})[0] == expected


def test_confidence_follows_measured_precision():
    c = ontolink._CONFIDENCE
    assert c["exact"] > c["synonym"] > c["similarity"] > c["head_broad"] > c["head_narrow"]
    assert classify_hit("UBERON:0001988", FECES) == ("exactMatch", c["id"])


@pytest.mark.parametrize("raw, core", [
    ("fecal sample from infant #7 DOL 29", "fecal"),
    ("microbial mat from Paralvinella palmiformis worms", "microbial mat"),
    ("Soil CK replicate 1", "Soil CK"),
    ("aquatic biome - 12 m", "aquatic biome"),
    ("serosa of intestine", "serosa of intestine"),
    ("soil metagenome", "soil"),
])
def test_core_text_keeps_the_words_an_ontology_can_hold(raw, core):
    assert ontolink.core_text(raw) == core


@pytest.mark.parametrize("value, ontology, expected", [
    ("p17.W1", "envo", False), ("V000307258.fastq.gz", "envo", False), ("29707", "envo", False),
    ("SAR11", "ncbitaxon", True), ("marine sediment", "envo", True), ("missing", "envo", False),
])
def test_identifiers_are_screened_in_every_field_but_taxon_names(value, ontology, expected):
    assert ontolink.should_look_up("isolation source", value, ontology) is expected


def test_hits_keep_the_submitters_text_and_anatomy_falls_back_to_uberon(monkeypatch):
    def fake(session, url, *, params, **kw):
        if params.get("ontology") == "envo":
            return {"response": {"docs": [_doc("ENVO:00000001", "colonial nesting area")]}}
        return {"response": {"docs": [_doc("UBERON:0001155", "colon")]}}
    monkeypatch.setattr(ontolink, "_request_json", fake)
    [top, *_] = ontolink._ols4_search_or_raise("colon sample 3", "envo", None, "material", "uberon")
    assert top.curie == "UBERON:0001155" and top.match_type == "exactMatch"
    assert top.matched_value == "colon sample 3"


def test_hits_from_non_obo_ontologies_are_kept(monkeypatch):
    # GOLD's IRIs do not name their ontology; they used to be dropped silently
    monkeypatch.setattr(ontolink, "_request_json", lambda *a, **k: {"response": {"docs": [
        {"iri": "https://w3id.org/gold.vocab/Estuary", "label": "Estuary", "short_form": "Estuary"}]}})
    [hit] = ontolink.ols4_search("estuary", "gold", None)
    assert (hit.curie, hit.source, hit.match_type) == ("GOLD:Estuary", "GOLD", "exactMatch")


@pytest.mark.parametrize("word, singular", [
    ("estuaries", "estuary"), ("lagoons", "lagoon"), ("marshes", "marsh"), ("grass", "grass"),
    ("humus", "humus"), ("bogs", "bog"), ("gas", "gas"),
])
def test_singular_is_cautious(word, singular):
    assert ontolink._singular(word) == singular


def test_plural_habitat_names_find_singular_terms(monkeypatch):
    queries = []

    def fake(session, url, *, params, **kw):
        queries.append(params["q"])
        docs = [_doc("ENVO:00000038", "coastal lagoon")] if params["q"] == "Coastal lagoon" else []
        return {"response": {"docs": docs}}

    monkeypatch.setattr(ontolink, "_request_json", fake)
    [hit] = ontolink.ols4_search("Coastal lagoons", "envo", None)
    assert queries[:2] == ["Coastal lagoons", "Coastal lagoon"]
    assert (hit.match_type, hit.confidence) == ("closeMatch", ontolink._CONFIDENCE["plural"])
    assert classify_hit("Estuaries", {"label": "Estuary"})[0] == "closeMatch"


@pytest.mark.parametrize("raw, core", [
    ("Rivers with muddy banks with Chenopodion rubri p.p. and Bidention p.p. vegetation",
     "Rivers with muddy banks with Chenopodion rubri and Bidention vegetation"),
    ("Dehesas with evergreen Quercus spp.", "Dehesas with evergreen Quercus"),
    ("Sub-Arctic Salix spp. scrub", "Sub-Arctic Salix scrub"),
])
def test_botanical_abbreviations_are_dropped(raw, core):
    # OLS4 times out on "p.p"
    assert ontolink.core_text(raw) == core
