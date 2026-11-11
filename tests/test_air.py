"""Offline tests for the aerobiome KG: fake samples, a small GOLD, a fake OLS4 search. No network.

Run:  python -m pytest -q tests/test_air.py
"""

import csv
import io
import json

import pytest

from air import build, ena, vocab
from air import reviews as rv

ENVO = "http://purl.obolibrary.org/obo/ENVO_"
AIR, ORPHA = ENVO + "00002005", "http://www.orpha.net/ORDO/Orphanet_103918"

GOLD_OBO = """format-version: 1.2

[Term]
id: GOLDTERMS:3964
name: Environmental

[Term]
id: GOLDTERMS:3963
name: Environmental > Air
synonym: "Air, Environmental" EXACT []
is_a: GOLDTERMS:3964 ! Environmental
property_value: mixs:env_medium ENVO:00002005

[Term]
id: GOLDTERMS:3962
name: Environmental > Air > Outdoor Air
synonym: "Outdoor Air" EXACT []
is_a: GOLDTERMS:3963 ! Environmental > Air

[Term]
id: GOLDTERMS:9001
name: Engineered > Built environment > Air
is_a: GOLDTERMS:9000

[Typedef]
id: mixs:env_medium
"""
GOLD_SSSOM = "\n".join("\t".join(r) for r in [
    ["subject_id", "subject_label", "predicate_id", "object_id", "object_label"],
    ["GOLDVOCAB:Air", "Air", "skos:exactMatch", "ENVO:00002005", "air"],
    ["GOLDVOCAB:Outdoor-Air", "Outdoor Air", "skos:broadMatch", "ENVO:00002005", "air"],
    ["GOLDVOCAB:Wrong", "Wrong", "owl:differentFrom", "ENVO:00002005", "air"],
])


def rec(uri, source, field, value):
    return {"uri": uri, "curie": "", "source": source, "label": "", "confidence": 1.0, "match_type": "exactMatch",
            "origin": "biosamples", "matched_value": value, "matched_field": field}


SAMPLES = [
    {"accession": "SAMN1", "attributes": {"env_medium": "air", "env biome": "Tropical", "geo_loc_name": "Denmark",
                                          "carb_dioxide": "410 ppm"},
     "ontology_terms": [rec(AIR, "ENVO", "env_medium", "air"), rec(ORPHA, "Orphanet", "env biome", "Tropical")]},
    {"accession": "SAMN2", "attributes": {"environmental medium": "Air ", "isolation_source": "not collected",
                                          "env_local_scale": "campus[ENVO_00000485]", "empo_3": "Aerosol (non-saline)",
                                          "pollutants": "ozone; 2019-05-01"}},
    {"accession": "SAMN3", "attributes": {"isolation source": "outdoor air", "env_broad_scale": "sea spray aerosol"}},
]


class FakeSearch:
    """OLS4 stand-in: text -> one hit; records every lookup."""
    HITS = {("outdoor air", "envo"): ("ENVO:01000267", "atmosphere", "relatedMatch", 0.35),
            ("ozone", "chebi"): ("CHEBI:25812", "ozone", "exactMatch", 0.9),
            ("carbon dioxide", "chebi"): ("CHEBI:16526", "carbon dioxide", "exactMatch", 0.9),
            ("sea spray aerosol", "envo"): ("ENVO:00010505", "aerosol", "broadMatch", 0.9)}

    def __init__(self):
        self.calls = []

    def names(self, curies):
        return {c: {"ENVO:00000485": "campus", "ENVO:00000428": "biome"}.get(c, "") for c in curies}

    def __call__(self, text, ontology, branch=""):
        self.calls.append((text, ontology, branch))
        hit = self.HITS.get((text.lower(), ontology))
        if not hit:
            return []
        curie, label, match, conf = hit
        prefix, num = curie.split(":")
        return [{"uri": f"http://purl.obolibrary.org/obo/{prefix}_{num}", "curie": curie, "source": prefix,
                 "label": label, "match_type": match, "confidence": conf}]


@pytest.fixture
def search():
    return FakeSearch()


@pytest.fixture
def graph(search):
    return build.build_graph(SAMPLES, vocab.parse_gold_obo(GOLD_OBO), vocab.parse_gold_sssom(GOLD_SSSOM), search)


def matches_of(graph, term_id):
    nodes = {n["id"]: n for n in graph["nodes"]}
    return {e["target"]: {**e, "kind": nodes[e["target"]]["kind"], "label": nodes[e["target"]]["label"]}
            for e in graph["edges"] if e["source"] == term_id and e["kind"] == "match"}


# ─── Reading the fields ──────────────────────────────────────────────────────

def test_fields_are_read_by_group_across_spellings():
    terms = build.read_terms(SAMPLES)
    medium = terms[("env_medium", "air")]   # "env_medium: air" and "environmental medium: Air "
    assert medium["samples"] == {"SAMN1", "SAMN2"} and set(medium["fields"]) == {"env_medium", "environmental medium"}
    assert AIR in medium["record"]                                  # the archive's ENVO ID goes with the value
    assert ("isolation_source", "not collected") not in terms       # placeholders are skipped
    assert not any(v == "denmark" for _, v in terms)                # not a field we read
    assert terms[("chemistry", "carbon dioxide")]["measured"]       # carb_dioxide measures carbon dioxide
    assert ("chemistry", "ozone") in terms and not any("2019" in v for _, v in terms)  # the date is dropped


# ─── Matching ────────────────────────────────────────────────────────────────

def test_envo_comes_from_the_record_and_search_fills_the_rest(graph, search):
    assert matches_of(graph, "air:env_medium:air")[AIR]["origin"] == "biosamples"
    searched = {c[:2] for c in search.calls}
    assert ("air", "envo") not in searched                          # the record already had ENVO
    assert ("outdoor air", "envo") in searched
    assert ("sea spray aerosol", "envo", "biome") in search.calls  # broad scale searched in the biome branch
    assert not any(c[0].startswith("campus") for c in search.calls)
    typed = matches_of(graph, "air:env_local_scale:campus[envo_00000485]")
    assert list(typed) == [ENVO + "00000485"] and typed[ENVO + "00000485"]["origin"] == "id_in_text"
    assert typed[ENVO + "00000485"]["label"] == "campus"             # the typed ID gets its name from OLS4
    assert not matches_of(graph, "air:isolation_source:outdoor air").get(ENVO + "01000267")  # 0.35 is under the cut


def test_gold_through_envo_and_by_name(graph):
    m = matches_of(graph, "air:env_medium:air")
    assert m["GOLDVOCAB:Air"]["match"] == "skos:exactMatch" and m["GOLDVOCAB:Air"]["via"] == "ENVO:00002005"
    assert m["GOLDVOCAB:Outdoor-Air"]["match"] == "skos:narrowMatch"   # GOLD broadMatch ENVO, seen from ENVO
    assert "GOLDVOCAB:Wrong" not in m                                  # differentFrom is never used
    assert m["GOLDTERMS:3963"]["origin"] == "gold_name"                # "Air": the shortest path wins
    assert "GOLDTERMS:9001" not in m
    outdoor = matches_of(graph, "air:isolation_source:outdoor air")
    assert outdoor["GOLDTERMS:3962"]["match"] == "skos:exactMatch"     # exact synonym "Outdoor Air"


def test_empo_from_the_field_and_from_air_evidence(graph):
    field = matches_of(graph, "air:empo:aerosol (non-saline)")
    assert field["EMPO:Aerosol (non-saline)"]["origin"] == "empo_field"
    rule = matches_of(graph, "air:env_medium:air")["EMPO:Aerosol (non-saline)"]
    assert (rule["origin"], rule["match"]) == ("empo_rule", "skos:broadMatch")
    assert "EMPO:Aerosol (saline)" in matches_of(graph, "air:env_broad_scale:sea spray aerosol")


def test_chebi_for_chemicals(graph):
    assert matches_of(graph, "air:chemistry:ozone")[build.vocab.ENVO_IRI.replace("ENVO_", "CHEBI_") + "25812"]["kind"] == "CHEBI"
    assert matches_of(graph, "air:chemistry:carbon dioxide")


def test_off_ontology_record_ids_are_flagged(graph):
    tropical = matches_of(graph, "air:env_broad_scale:tropical")
    assert tropical[ORPHA]["review"] and not matches_of(graph, "air:env_medium:air")[AIR]["review"]
    assert graph["meta"]["terms_matched_to"]["GOLD"] >= 2


# ─── Hierarchy ───────────────────────────────────────────────────────────────

GOLD_TRIAD_OBO = GOLD_OBO + """
[Term]
id: GOLDTERMS:3970
name: Environmental > Air > Outdoor Air > Urban
is_a: GOLDTERMS:3962 ! Outdoor Air
property_value: mixs:env_broad ENVO:00000428
property_value: mixs:env_local ENVO:01000267
property_value: mixs:env_medium ENVO:00002005

[Term]
id: GOLDTERMS:3971
name: Environmental > Air > Indoor Air
is_a: GOLDTERMS:3963 ! Environmental > Air
property_value: mixs:env_broad ENVO:00000428
property_value: mixs:env_local ENVO:01000426
property_value: mixs:env_medium ENVO:00002005
"""
CONTEXT = [  # two samples in the same context: urban biome > atmosphere > air
    {"accession": f"SAMN{i}", "attributes": {"env_broad_scale": "urban biome", "env_local_scale": "atmosphere [ENVO:01000267]",
                                             "env_medium": "air [ENVO:00002005]"}} for i in (7, 8)
] + [{"accession": "SAMN9", "attributes": {"env_broad_scale": "urban biome", "env_medium": "air [ENVO:00002005]"}}]


@pytest.fixture
def hgraph():
    return build.build_graph(CONTEXT, vocab.parse_gold_obo(GOLD_TRIAD_OBO), {})


def test_samples_make_a_broad_local_medium_tree(hgraph):
    tree = {c["id"]: c for c in hgraph["tree"]}
    root = tree["ctx:env_broad_scale=urban biome"]
    assert root["samples"] == 3 and root["parent"] is None and root["level"] == 0
    leaf = next(c for c in tree.values() if c["path"] == ["urban biome", "atmosphere [ENVO:01000267]", "air [ENVO:00002005]"])
    assert leaf["samples"] == 2 and tree[leaf["parent"]]["slot"] == "env_local_scale"
    # SAMN9 gives no local value: its medium sits straight under the biome.
    assert tree["ctx:env_broad_scale=urban biome > env_medium=air [envo:00002005]"]["samples"] == 1
    within = {(e["source"], e["target"]): e["samples"] for e in hgraph["edges"] if e["kind"] == "within"}
    assert within[("air:env_broad_scale:urban biome", "air:env_local_scale:atmosphere [envo:01000267]")] == 2


def test_bare_ids_are_named_and_samples_without_the_triad_use_isolation_source(search):
    g = build.build_graph([{"accession": "SAMN1", "attributes": {"env_broad_scale": "ENVO:00000428"}},
                           {"accession": "SAMN2", "attributes": {"isolation_source": "residential air"}}], search=search)
    tree = {c["label"]: c for c in g["tree"]}
    assert "biome (ENVO:00000428)" in tree
    assert tree["residential air"]["slot"] == "isolation_source" and tree["residential air"]["level"] == 0


def test_a_whole_context_matches_the_shallowest_gold_class_that_fits(hgraph):
    ctx = next(c for c in hgraph["tree"] if len(c["path"]) == 3)
    # Outdoor Air > Urban has the same three ENVO terms, Indoor Air another local one.
    assert ctx["gold"] == ["GOLDTERMS:3970"]
    edge = next(e for e in hgraph["edges"] if e["source"] == ctx["id"] and e["kind"] == "match")
    assert (edge["origin"], edge["confidence"], edge["match"]) == ("gold_triad", 0.85, "skos:closeMatch")
    two = next(c for c in hgraph["tree"] if c["path"] == ["urban biome", "air [ENVO:00002005]"])
    assert two["gold"] == []   # broad and medium only: Air classes all fit, none more than the others


def test_matched_gold_and_empo_classes_bring_their_parents(hgraph):
    is_a = {(e["source"], e["target"]) for e in hgraph["edges"] if e["kind"] == "is_a"}
    assert {("GOLDTERMS:3970", "GOLDTERMS:3962"), ("GOLDTERMS:3962", "GOLDTERMS:3963"),
            ("GOLDTERMS:3963", "GOLDTERMS:3964")} <= is_a
    assert ("EMPO:Aerosol (non-saline)", "EMPO:Non-saline") in is_a and ("EMPO:Non-saline", "EMPO:Free-living") in is_a
    nodes = {n["id"]: n for n in hgraph["nodes"]}
    assert nodes["GOLDTERMS:3964"]["ancestor"] and nodes["EMPO:Free-living"]["ancestor"]


def test_builds_without_gold_or_search():
    g = build.build_graph(SAMPLES)
    assert g["meta"]["terms"] and all(n["kind"] != "GOLD" for n in g["nodes"])


# ─── Vocabularies and ENA ────────────────────────────────────────────────────

def test_vocabularies_parse():
    gold = {t["id"]: t for t in vocab.parse_gold_obo(GOLD_OBO)}
    assert gold["GOLDTERMS:3962"]["label"] == "Outdoor Air" and gold["GOLDTERMS:3962"]["parents"] == ["GOLDTERMS:3963"]
    assert gold["GOLDTERMS:3963"]["mixs"] == {"env_medium": "ENVO:00002005"}
    empo = {e["label"]: e for e in vocab.empo_terms()}
    assert sum(e["level"] == 3 for e in empo.values()) == 27
    assert empo["Aerosol (non-saline)"]["envo"] == "ENVO:01001054" and empo["Aerosol (non-saline)"]["parent"] == "EMPO:Non-saline"


def test_ena_accessions_every_nth():
    tsv = "sample_accession\tsample_description\ttax_id\n" + "".join(f"SAMN{i}\tair\t655179\n" for i in range(10))
    assert ena.accessions(tsv) == [f"SAMN{i}" for i in range(10)]
    assert ena.accessions(tsv, every=4) == ["SAMN0", "SAMN4", "SAMN8"]


# ─── Reviews and the app ─────────────────────────────────────────────────────

def test_exports(tmp_path, graph):
    predicted = rv.predicted_matches(graph)
    key = rv.mapping_key("air:env_medium:air", AIR)
    store = rv.ReviewStore(tmp_path / "reviews.jsonl")
    store.add({"key": key, "verdict": "correct", "reviewer": "Ana"}, predicted)
    rows = [r for r in csv.DictReader(io.StringIO(rv.export_csv(predicted, store.state()))) if r["value"] == "air"]
    assert {r["target_ontology"] for r in rows} >= {"ENVO", "GOLD", "EMPO"}
    assert next(r for r in rows if r["target_ontology"] == "ENVO")["status"] == "correct"
    body = [l.split("\t") for l in rv.export_sssom(predicted, store.state()).splitlines() if not l.startswith("#")]
    assert len(body) == 2 and dict(zip(body[0], body[1]))["subject_id"] == "air:env_medium:air"


def test_app_serves_the_graph_and_reviews(tmp_path, monkeypatch, graph):
    from air import app as air_app
    (tmp_path / "air").mkdir()
    (tmp_path / "air" / "graph.json").write_text(json.dumps(graph))
    monkeypatch.setattr(air_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(air_app, "REVIEWS_FILE", tmp_path / "air" / "reviews.jsonl")
    air_app.prepare()
    client = air_app.app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/data/graph.json").get_json()["meta"]["samples"] == 3
    key = rv.mapping_key("air:env_medium:air", AIR)
    assert client.post("/api/reviews", json={"key": key, "verdict": "correct"}).status_code == 400  # no name
    ok = client.post("/api/reviews", json={"key": key, "verdict": "correct", "reviewer": "Ana"})
    assert ok.status_code == 200 and "Ana" in (tmp_path / "air" / "air-match-reviews.csv").read_text()


def test_app_without_a_graph_shows_nothing_rather_than_failing(tmp_path, monkeypatch):
    from air import app as air_app
    monkeypatch.setattr(air_app, "DATA_DIR", tmp_path)
    air_app.prepare()
    assert air_app.app.test_client().get("/data/graph.json").get_json()["nodes"] == []
