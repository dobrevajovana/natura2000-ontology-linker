"""Offline tests for the pipeline's pure logic: no network, no embedding model.

Run:  python -m pytest -q
"""

import json

import pytest
from rdflib import Graph, OWL, RDF

from pipeline import linker, ontology, scraper
from pipeline.vector_match import merge_mappings, save_mappings_sssom, score_to_match_type


# --- scraper ---------------------------------------------------------------

def test_stratified_sample_covers_every_category():
    terms = ([{"id": f"h{i}", "category": "habitat"} for i in range(30)]
             + [{"id": f"s{i}", "category": "species", "directive": "habitats"} for i in range(30)]
             + [{"id": f"b{i}", "category": "species", "directive": "birds"} for i in range(30)]
             + [{"id": f"r{i}", "category": "region"} for i in range(9)])
    sample = scraper.stratified_sample(terms, 8)
    assert len(sample) == 8
    # the first slice the linker takes in dry-run mode must already be mixed
    assert {t["id"][0] for t in sample[:4]} == {"h", "s", "b", "r"}


def test_stratified_sample_never_exceeds_the_input():
    assert len(scraper.stratified_sample([{"id": "a", "category": "x"}], 50)) == 1


# --- linker ----------------------------------------------------------------

@pytest.mark.parametrize("plural, single", [
    ("Estuaries", "Estuary"), ("Coastal lagoons", "Coastal lagoon"), ("Marshes", "Marsh"),
    ("Reefs", "Reef"), ("Grass", "Grass"), ("Bog woodland", "Bog woodland"), ("Alnus", "Alnus"),
])
def test_singular(plural, single):
    assert linker._singular(plural) == single


def test_key_words_drop_legal_filler():
    assert linker._key_words("Sandbanks which are slightly covered by sea water all the time") == ["sandbank", "sea", "water"]
    assert "alnus" in linker._key_words("Alluvial forests with Alnus glutinosa and Fraxinus excelsior")


@pytest.mark.parametrize("term, expected", [
    ({"label": "1150 * Coastal lagoons"}, "Coastal lagoons"),
    ({"label": "91E0 * Alluvial forests"}, "Alluvial forests"),
    ({"label": "Otter (Lutra lutra)"}, "Otter"),
    ({"label": "Crex crex (Corncrake)", "scientific_name": "Crex crex"}, "Crex crex"),
])
def test_clean_label(term, expected):
    assert linker._clean_label(term) == expected


def _cand(label, synonyms=()):
    return {"uri": f"http://purl.obolibrary.org/obo/ENVO_{abs(hash(label)) % 10**8:08d}", "label": label,
            "synonyms": list(synonyms), "definition": ""}


def test_envo_scoring_uses_key_words_not_letters():
    bog_woodland = {"id": "habitat_91d0", "label": "91D0 * Bog woodland"}
    assert linker.score_envo_candidates(bog_woodland, [_cand("oak woodland")]) == []

    estuaries = {"id": "habitat_1130", "label": "1130 Estuaries"}
    [m] = linker.score_envo_candidates(estuaries, [_cand("estuary")])
    assert m["match_type"] == "skos:exactMatch"


def test_envo_synonym_only_match_goes_to_review():
    reefs = {"id": "habitat_1170", "label": "1170 Reefs"}
    [m] = linker.score_envo_candidates(reefs, [_cand("rocky reef", synonyms=["reef"])])
    assert m["match_type"] == "skos:closeMatch" and m["review"]


def test_deduplicate_keeps_the_most_confident_mapping():
    a = {"source_id": "x", "target_uri": "u", "confidence": 0.5}
    b = {"source_id": "x", "target_uri": "u", "confidence": 0.9}
    assert linker.deduplicate([a, b]) == [b]


def test_habitats_link_by_annex_code(monkeypatch):
    rows = [{"item": {"value": "http://www.wikidata.org/entity/Q61484480"}, "code": {"value": "1150"},
             "eunis": {"value": "10007"}, "itemLabel": {"value": "Coastal lagoons"}}]
    monkeypatch.setattr(linker, "_sparql", lambda endpoint, query: rows)
    out = linker.link_habitats_wikidata([{"id": "habitat_1150", "label": "1150 * Coastal lagoons", "codes": ["1150"]}])
    assert {m["target_uri"] for m in out} == {"http://www.wikidata.org/entity/Q61484480",
                                              "https://eunis.eea.europa.eu/habitats/10007"}
    assert all(m["match_type"] == "skos:exactMatch" and m["confidence"] == 1.0 for m in out)


def test_species_retry_under_accepted_name(monkeypatch):
    seen_queries = []

    def fake_taxa(names):
        seen_queries.append(list(names))
        if "Lyciasalamandra luschani" in names:
            return {"Lyciasalamandra luschani": [{"item": {"value": "http://www.wikidata.org/entity/Q1"},
                                                  "ncbi": {"value": "123"}}]}
        return {}

    monkeypatch.setattr(linker, "_wikidata_taxa", fake_taxa)
    sp = {"id": "species_mertensiella_luschani", "label": "Mertensiella luschani", "scientific_name": "Mertensiella luschani"}
    out, with_ncbi = linker.link_species_wikidata([sp], accepted={sp["id"]: "Lyciasalamandra luschani"})
    assert seen_queries == [["Mertensiella luschani"], ["Lyciasalamandra luschani"]]
    assert with_ncbi == {sp["id"]}
    assert any(m["target_uri"].endswith("NCBITaxon_123") and "accepted name" in m["note"] for m in out)


def test_homonyms_are_flagged_not_trusted(monkeypatch):
    rows = [{"item": {"value": "http://www.wikidata.org/entity/Q1"}, "ncbi": {"value": "1"}},
            {"item": {"value": "http://www.wikidata.org/entity/Q2"}, "ncbi": {"value": "2"}}]
    monkeypatch.setattr(linker, "_wikidata_taxa", lambda names: {"Morus alba": rows} if "Morus alba" in names else {})
    out, _ = linker.link_species_wikidata([{"id": "s", "label": "Morus alba", "scientific_name": "Morus alba"}])
    assert out and all(m["match_type"] == "skos:closeMatch" and m["review"] for m in out)


# --- vector matching ---------------------------------------------------------

@pytest.mark.parametrize("score, expected", [
    (0.9, "skos:exactMatch"), (0.75, "skos:closeMatch"), (0.6, "skos:relatedMatch"), (0.3, None)])
def test_score_to_match_type(score, expected):
    assert score_to_match_type(score, {"exact": 0.85, "close": 0.70, "related": 0.55}) == expected


def test_vector_agreement_confirms_a_review_candidate():
    direct = [{"source_id": "h", "target_uri": "e", "match_type": "skos:relatedMatch", "confidence": 0.5, "review": True}]
    vector = [{"source_id": "h", "target_uri": "e", "match_type": "skos:closeMatch", "confidence": 0.8}]
    [m] = merge_mappings(direct, vector)
    assert m["match_type"] == "skos:closeMatch" and m["method"] == "combined"
    assert m["confidence"] == pytest.approx(0.85) and "review" not in m


def test_sssom_output(tmp_path):
    path = tmp_path / "m.sssom.tsv"
    save_mappings_sssom([{"source_id": "species_crex_crex", "source_label": "Crex crex", "target_uri": "http://x/1",
                          "target_label": "Crex crex", "match_type": "skos:exactMatch", "confidence": 1.0,
                          "source_ontology": "wikidata", "method": "wikidata_p225_taxon_name"}], path)
    lines = path.read_text().splitlines()
    header = next(l for l in lines if not l.startswith("#")).split("\t")
    row = dict(zip(header, lines[-1].split("\t")))
    assert row["subject_id"] == "n2k:species_crex_crex"
    assert row["mapping_justification"] == "semapv:LexicalMatching"


# --- ontology ----------------------------------------------------------------

def test_term_ids_match_the_scraper():
    assert ontology._term_id("region", "Black Sea") == scraper.make_id("region", "Black Sea")
    assert ontology._term_id("status", "CR") == scraper.make_id("status", "CR")


def test_regions_are_single_named_individuals(tmp_path):
    terms = scraper.build_regions() + scraper.build_statuses()
    ontology.build_ontology(terms, str(tmp_path))
    g = Graph().parse(tmp_path / "ontology.ttl")
    regions = set(g.subjects(RDF.type, ontology.N2K.BiogeographicalRegion))
    assert len(regions) == 9                       # one individual per region, not two
    assert all((r, RDF.type, OWL.NamedIndividual) in g for r in regions)


# --- geoapp --------------------------------------------------------------------

def test_null_island_is_not_a_location():
    from geoapp.geo import parse_lat_lon
    assert parse_lat_lon("0 N 0 E") is None
    assert parse_lat_lon("55.67 N 12.57 E") == pytest.approx((55.67, 12.57))
