"""Offline tests for the MFD viewer's build step and server: a small in-memory ontology, no network.

Run:  python -m pytest -q tests/test_mfd.py
"""

import io
import json

import pandas as pd
import pytest

from mfd import build

ONTO = pd.DataFrame([
    # sampletype, areatype, hab1 code, hab1, hab2 code, hab2, hab3 code, hab3, N2K, EUNIS, EMPO
    ["Soil", "Natural", 4000, "Temperate heath and scrub", None, None, None, None, 4000, "S", "Free-living; Non-saline;  Soil (non-saline)"],
    ["Soil", "Natural", 4000, "Temperate heath and scrub", 4010, "Wet heath", None, None, 4010, None, None],
    ["Soil", "Natural", 4000, "Temperate heath and scrub", 4030, "Dry heath", None, None, 4030, None, None],
    ["Water", "Urban", 2000, "Wastewater", None, None, None, None, None, None, None],
], columns=["mfd_sampletype", "mfd_areatype", "mfd_hab1_code", "mfd_hab1", "mfd_hab2_code", "mfd_hab2",
            "mfd_hab3_code", "mfd_hab3", "Natura2000", "EUNIS", "EMPO"])

TERMS = [
    {"id": "habitat_4010_wet_heaths", "label": "4010 Northern Atlantic wet heaths", "category": "habitat", "codes": ["4010"],
     "source_url": "https://eunis.eea.europa.eu/habitats/4010"},
    {"id": "species_x", "label": "4010 not a habitat", "category": "species", "codes": ["4010"]},
]
MAPPINGS = [
    {"source_id": "habitat_4010_wet_heaths", "target_uri": "http://www.wikidata.org/entity/Q1", "target_label": "wet heath",
     "match_type": "skos:exactMatch", "confidence": 1.0, "source_ontology": "wikidata", "method": "wikidata_p6436_code"},
    {"source_id": "habitat_4010_wet_heaths", "target_uri": "http://purl.obolibrary.org/obo/ENVO_1", "target_label": "heath",
     "match_type": "skos:relatedMatch", "confidence": 0.6, "source_ontology": "ols4_envo"},
    {"source_id": "species_x", "target_uri": "http://example.org/never", "match_type": "skos:exactMatch"},
]


@pytest.fixture
def nodes():
    return {n["id"]: n for n in build.build_ontology(ONTO)}


def test_tree_has_every_level_with_parents(nodes):
    heath = nodes["Soil > Natural > Temperate heath and scrub"]
    assert heath["level"] == 2 and heath["level_name"] == "MFDO1"
    assert heath["parent"] == "Soil > Natural"
    assert nodes["Soil"]["parent"] is None
    assert len(nodes) == 8  # 2 sample types, 2 area types, 2 MFDO1, 2 MFDO2


def test_ancestors_come_before_descendants():
    order = [n["id"] for n in build.build_ontology(ONTO)]
    for n in build.build_ontology(ONTO):
        if n["parent"]:
            assert order.index(n["parent"]) < order.index(n["id"])


def test_crosswalk_sits_on_the_rows_own_level(nodes):
    heath = nodes["Soil > Natural > Temperate heath and scrub"]
    assert (heath["code"], heath["natura2000"], heath["eunis"]) == ("4000", "4000", "S")
    assert heath["empo"] == "Free-living; Non-saline; Soil (non-saline)"  # whitespace collapsed
    wet = nodes["Soil > Natural > Temperate heath and scrub > Wet heath"]
    assert (wet["code"], wet["natura2000"], wet["eunis"]) == ("4010", "4010", None)
    assert nodes["Soil > Natural"]["natura2000"] is None


def test_graph_joins_mfd_to_linker_through_natura2000_code():
    g = build.build_graph(build.build_ontology(ONTO), TERMS, MAPPINGS)
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = {(e["source"], e["target"]): e for e in g["edges"]}

    wet = "mfd:Soil > Natural > Temperate heath and scrub > Wet heath"
    assert nodes[wet]["ref"] == "Soil > Natural > Temperate heath and scrub > Wet heath"
    assert edges[(wet, "n2k:4010")]["kind"] == "mfd_crosswalk"
    assert nodes["n2k:4010"]["label"] == "4010 Northern Atlantic wet heaths" and nodes["n2k:4010"]["linked"]
    assert edges[("n2k:4010", "http://www.wikidata.org/entity/Q1")]["match"] == "skos:exactMatch"
    assert edges[("n2k:4010", "http://purl.obolibrary.org/obo/ENVO_1")]["confidence"] == 0.6
    assert "http://example.org/never" not in nodes  # species terms do not join on habitat codes


def test_graph_without_linker_keeps_the_mfd_crosswalk():
    g = build.build_graph(build.build_ontology(ONTO), [], [])
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes["n2k:4000"]["label"] == "Natura 2000 group 4000" and not nodes["n2k:4000"]["linked"]
    assert nodes["eunis:S"]["kind"] == "eunis_mfd"
    assert nodes["empo:Free-living; Non-saline; Soil (non-saline)"]["label"] == "Soil (non-saline)"
    assert not any(n["id"].startswith("mfd:Water") for n in g["nodes"])  # no crosswalk, no node
    assert all(e["kind"] == "mfd_crosswalk" for e in g["edges"])


def test_build_reads_only_the_ontology_and_app_serves_it(tmp_path, monkeypatch):
    src = tmp_path / "latest_mfd-habitat-ontology.xlsx"
    ONTO.to_excel(src, index=False)
    old = tmp_path / "mfd" / "raw"
    old.mkdir(parents=True)
    (tmp_path / "mfd" / "samples.json").write_text("{}")  # from the sample-based version
    (old / "latest_mfd_db.xlsx").write_text("old")

    meta = build.build(tmp_path, src)
    assert meta["classes"] == 8 and meta["natura2000_codes"] == 3
    assert not (tmp_path / "mfd" / "samples.json").exists()
    assert not (old / "latest_mfd_db.xlsx").exists()

    linker = tmp_path / "linker"
    linker.mkdir()
    (linker / "terms.json").write_text(json.dumps(TERMS))
    (linker / "mappings.json").write_text(json.dumps(MAPPINGS[:1]))
    (linker / "mappings_added.json").write_text(json.dumps({"ontologies": {}, "mappings": MAPPINGS[1:2]}))
    g = build.write_graph(tmp_path, linker)
    assert g["meta"] == {"linker_terms": 2, "linker_mappings": 2}

    from mfd import app as mfd_app
    monkeypatch.setattr(mfd_app, "DATA_DIR", tmp_path)
    client = mfd_app.app.test_client()
    assert client.get("/").status_code == 200
    assert len(json.loads(client.get("/data/ontology.json").data)["nodes"]) == 8
    assert json.loads(client.get("/data/graph.json").data)["edges"] == g["edges"]
    assert client.get("/data/samples.json").status_code == 404


def test_write_graph_tolerates_missing_linker_output(tmp_path):
    (tmp_path / "mfd").mkdir()
    (tmp_path / "mfd" / "ontology.json").write_text(json.dumps({"nodes": build.build_ontology(ONTO)}))
    g = build.write_graph(tmp_path, tmp_path / "nowhere")
    assert g["meta"] == {"linker_terms": 0, "linker_mappings": 0}
    assert g["edges"] and all(e["kind"] == "mfd_crosswalk" for e in g["edges"])


# ─── Reviews ──────────────────────────────────────────────────────────────────

from mfd import reviews as rv  # noqa: E402

WIKI = "http://www.wikidata.org/entity/Q1"
ENVO = "http://purl.obolibrary.org/obo/ENVO_1"
KEY_WIKI, KEY_ENVO = rv.mapping_key("n2k:4010", WIKI), rv.mapping_key("n2k:4010", ENVO)


@pytest.fixture
def predicted():
    return rv.predicted_matches(build.build_graph(build.build_ontology(ONTO), TERMS, MAPPINGS))


def test_predicted_matches_are_the_linker_edges_only(predicted):
    assert set(predicted) == {KEY_WIKI, KEY_ENVO}  # MFD's own crosswalk is not up for review
    p = predicted[KEY_WIKI]
    assert (p["source_label"], p["target_label"], p["ontology"], p["match"]) == (
        "4010 Northern Atlantic wet heaths", "wet heath", "wikidata", "skos:exactMatch")
    assert p["term"] == "habitat_4010_wet_heaths" and p["flagged"] is False


def test_review_is_validated_and_appended(tmp_path, predicted):
    store = rv.ReviewStore(tmp_path / "reviews.jsonl")
    for bad, msg in [({"key": "nope", "verdict": "correct", "reviewer": "A"}, "Unknown match"),
                     ({"key": KEY_WIKI, "verdict": "maybe", "reviewer": "A"}, "Verdict"),
                     ({"key": KEY_WIKI, "verdict": "correct", "reviewer": "  "}, "name"),
                     ({"key": KEY_WIKI, "verdict": "correct", "reviewer": "A", "match_type": "skos:sameAs"}, "match type"),
                     ({"key": KEY_WIKI, "verdict": "correct", "reviewer": "A", "comment": "x" * 2001}, "Comment")]:
        with pytest.raises(rv.ReviewError, match=msg):
            store.add(bad, predicted)
    assert store.events() == []

    ev = store.add({"key": KEY_ENVO, "verdict": "incorrect", "reviewer": "  Ana   B ", "comment": "too broad",
                    "match_type": "skos:broadMatch"}, predicted)
    assert ev["reviewer"] == "Ana B" and ev["match_type"] is None  # a type only goes with "correct"
    assert ev["predicted"]["match"] == "skos:relatedMatch" and ev["time"].endswith("Z")
    assert len((tmp_path / "reviews.jsonl").read_text().splitlines()) == 1


def test_state_keeps_every_decision_and_flags_disagreement(tmp_path, predicted):
    store = rv.ReviewStore(tmp_path / "reviews.jsonl")
    store.add({"key": KEY_WIKI, "verdict": "unsure", "reviewer": "Ana"}, predicted)
    store.add({"key": KEY_WIKI, "verdict": "correct", "reviewer": "Ana"}, predicted)
    s = store.state()[KEY_WIKI]
    assert s["verdict"] == "correct" and not s["disputed"] and len(s["history"]) == 2

    store.add({"key": KEY_WIKI, "verdict": "incorrect", "reviewer": "Bo"}, predicted)
    s = store.state()[KEY_WIKI]
    assert s["disputed"] and set(s["reviewers"]) == {"Ana", "Bo"} and len(s["history"]) == 3

    (tmp_path / "reviews.jsonl").open("a").write('{"half a line')  # a crash mid-write loses only that line
    assert len(store.events()) == 3


def test_exports(tmp_path, predicted):
    store = rv.ReviewStore(tmp_path / "reviews.jsonl")
    store.add({"key": KEY_WIKI, "verdict": "correct", "reviewer": "Ana", "match_type": "skos:closeMatch",
               "comment": "close, not the same\tthing"}, predicted)
    state = store.state()

    import csv as _csv
    rows = list(_csv.DictReader(io.StringIO(rv.export_csv(predicted, state))))
    assert len(rows) == 2  # every predicted match, reviewed or not
    by = {r["target_id"]: r for r in rows}
    assert by[WIKI]["status"] == "correct" and by[WIKI]["reviewed_match"] == "skos:closeMatch"
    assert by[ENVO]["status"] == "unreviewed" and by[ENVO]["natura2000_code"] == "4010"

    sssom = rv.export_sssom(predicted, state)
    assert sssom.startswith("# curie_map:")
    body = [l.split("\t") for l in sssom.splitlines() if not l.startswith("#")]
    head, rows = body[0], [dict(zip(body[0], r)) for r in body[1:]]
    assert len(rows) == 1  # unreviewed matches stay out
    r = rows[0]
    assert (r["subject_id"], r["predicate_id"], r["predicate_modifier"]) == ("n2k:habitat_4010_wet_heaths", "skos:closeMatch", "")
    assert r["mapping_justification"] == "semapv:ManualMappingCuration" and r["author_label"] == "Ana"
    assert "predicted skos:exactMatch" in r["comment"] and "\t" not in r["comment"]

    store.add({"key": KEY_ENVO, "verdict": "incorrect", "reviewer": "Bo"}, predicted)
    rows = [l.split("\t") for l in rv.export_sssom(predicted, store.state()).splitlines() if not l.startswith("#")]
    envo = dict(zip(rows[0], next(r for r in rows[1:] if r[4] == ENVO)))
    assert (envo["predicate_id"], envo["predicate_modifier"]) == ("skos:relatedMatch", "Not")


def test_review_api(tmp_path, monkeypatch):
    (tmp_path / "mfd").mkdir()
    (tmp_path / "mfd" / "ontology.json").write_text(json.dumps({"nodes": build.build_ontology(ONTO)}))
    linker = tmp_path / "linker"
    linker.mkdir()
    (linker / "terms.json").write_text(json.dumps(TERMS))
    (linker / "mappings.json").write_text(json.dumps(MAPPINGS))
    build.write_graph(tmp_path, linker)

    from mfd import app as mfd_app
    monkeypatch.setattr(mfd_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mfd_app, "REVIEWS_FILE", tmp_path / "mfd" / "reviews.jsonl")
    client = mfd_app.app.test_client()

    assert client.get("/api/reviews").get_json() == {"state": {}}
    bad = client.post("/api/reviews", json={"key": KEY_WIKI, "verdict": "correct"})
    assert bad.status_code == 400 and "name" in bad.get_json()["error"]
    assert client.post("/api/reviews", data="not json").status_code == 400

    ok = client.post("/api/reviews", json={"key": KEY_WIKI, "verdict": "correct", "reviewer": "Ana"})
    assert ok.status_code == 200 and ok.get_json()["state"]["verdict"] == "correct"
    assert client.get("/api/reviews").get_json()["state"][KEY_WIKI]["reviewer"] == "Ana"

    csv_resp = client.get("/api/reviews/export.csv")
    assert csv_resp.mimetype == "text/csv" and "attachment" in csv_resp.headers["Content-Disposition"]
    assert "semapv:ManualMappingCuration" in client.get("/api/reviews/export.sssom.tsv").get_data(as_text=True)
