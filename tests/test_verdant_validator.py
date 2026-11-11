"""Offline tests for the validation tool (validation/): any input, the checks, the store, the web app.

Run:  python -m pytest -q tests/test_validation_tool.py
"""

import importlib.util
import io
import json
from pathlib import Path

import pytest

from verdant_validator import ReviewStore, check, export_csv, export_sssom, load
from verdant_validator import checks as chk
from verdant_validator.mappings import from_wide_table, normalise_predicate

ENVO = "http://purl.obolibrary.org/obo/ENVO_"
GBIF = "https://www.gbif.org/species/"
TERMS = [{"id": "p_species_00", "label": "Melonis barleeanus", "category": "species"},
         {"id": "p_habitat_01", "label": "Cold desert", "category": "habitat"}]
LINKER = [
    {"source_id": "p_species_00", "source_label": "Melonis barleeanus", "target_uri": GBIF + "4381944",
     "match_type": "skos:exactMatch", "confidence": 1.0, "source_ontology": "gbif", "method": "gbif_match"},
    {"source_id": "p_habitat_01", "source_label": "Cold desert", "target_uri": ENVO + "01000382", "target_label": "cold desert",
     "match_type": "skos:exactMatch", "confidence": 1.0, "source_ontology": "ols4_envo", "review": True,
     "target_definition": "x" * 1000},
    {"source_id": "p_habitat_01", "source_label": "Cold desert", "target_uri": ENVO + "01000179", "target_label": "desert",
     "match_type": "skos:exactMatch", "confidence": 0.6, "source_ontology": "ols4_envo"},
]


@pytest.fixture
def linker_dir(tmp_path):
    (tmp_path / "terms.json").write_text(json.dumps(TERMS))
    (tmp_path / "mappings.json").write_text(json.dumps(LINKER))
    return tmp_path


def test_linker_output_loads_with_categories(linker_dir):
    maps = load(linker_dir / "mappings.json")
    assert len(maps) == 3
    m = maps[1]
    assert (m["subject_label"], m["subject_category"], m["predicate_id"], m["flagged"]) == ("Cold desert", "habitat", "skos:exactMatch", True)
    assert len(m["context"]["target_definition"]) < 300                         # long text is cut
    assert load(linker_dir)[0]["key"] == f"p_species_00|{GBIF}4381944"         # a folder means its mappings.json


def test_sssom_csv_and_aliases(tmp_path):
    (tmp_path / "x.sssom.tsv").write_text("# curie_map:\n#   ENVO: http://purl.obolibrary.org/obo/ENVO_\n"
                                          "subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\tpredicate_modifier\n"
                                          "a\tair\tskos:exactMatch\tENVO:00002005\tair\tNot\n")
    s = load(tmp_path / "x.sssom.tsv")
    assert s[0]["object_id"] == "ENVO:00002005" and s[0]["context"]["predicate_modifier"] == "Not"
    (tmp_path / "x.csv").write_text("name,target_uri,match_type,score,extra\nTundra,http://purl.obolibrary.org/obo/ENVO_01000180,closeMatch,0.7,hello\n")
    c = load(tmp_path / "x.csv")[0]
    assert (c["subject_id"], c["predicate_id"], c["confidence"], c["context"]) == ("Tundra", "skos:closeMatch", 0.7, {"extra": "hello"})
    assert normalise_predicate("http://www.w3.org/2004/02/skos/core#broadMatch") == "skos:broadMatch"


def test_pangaea_wide_sheet():
    rows = [{"name": "Tundra", "group": "biome", "VERDANT_ENVO": "tundra (closeMatch, 0.72) ; tundra biome (relatedMatch, 0.40)",
             "VERDANT_GBIF": "", "VERDANT_number_of_links": "2"},
            {"name": "Bulimina marginata", "group": "species", "VERDANT_GBIF": GBIF + "7737079 (exactMatch)"}]
    maps = from_wide_table(rows)
    assert [(m["subject_id"], m["object_source"], m["predicate_id"]) for m in maps] == [
        ("Tundra", "ENVO", "skos:closeMatch"), ("Tundra", "ENVO", "skos:relatedMatch"), ("Bulimina marginata", "GBIF", "skos:exactMatch")]
    assert maps[0]["confidence"] == 0.72 and maps[0]["object_label"] == "tundra"


def test_viewer_graph_loads(tmp_path):
    g = {"nodes": [{"id": "value:x=air", "label": "air", "field_label": "medium"}, {"id": ENVO + "00002005", "label": "air", "kind": "envo"}],
         "edges": [{"source": "value:x=air", "target": ENVO + "00002005", "kind": "predicted", "match": "skos:exactMatch", "confidence": 0.9},
                   {"source": "run:1", "target": "value:x=air", "kind": "has_value"}]}
    (tmp_path / "graph.json").write_text(json.dumps(g))
    maps = load(tmp_path / "graph.json")
    assert len(maps) == 1 and maps[0]["subject_category"] == "medium"


def test_offline_checks():
    maps = load([
        {"subject_id": "a", "subject_label": "Cold desert", "object_id": ENVO + "1", "object_label": "cold desert", "predicate_id": "exactMatch"},
        {"subject_id": "a", "subject_label": "Cold desert", "object_id": ENVO + "2", "object_label": "kelp forest", "predicate_id": "exactMatch"},
        {"subject_id": "b", "object_id": "tundra", "predicate_id": "sameAs", "confidence": 3},
        {"subject_id": "c", "object_id": ENVO + "3", "predicate_id": "closeMatch"},
        {"subject_id": "c", "object_id": ENVO + "3", "predicate_id": "broadMatch"},
    ])
    summary = check(maps)
    codes = {(m["subject_id"], m["object_id"]): {f["code"] for f in m["checks"]} for m in maps}
    assert codes[("a", ENVO + "2")] >= {"label", "many_exact"} and "label" not in codes[("a", ENVO + "1")]
    assert codes[("b", "tundra")] >= {"predicate", "confidence", "identifier"}
    assert "conflict" in codes[("c", ENVO + "3")]
    assert summary["with_errors"] == 2 and summary["online"] is False


def test_online_checks_with_stubbed_services(monkeypatch, tmp_path):
    monkeypatch.setattr(chk, "CACHE", tmp_path)
    monkeypatch.setattr(chk, "_ols_term", lambda iri: {"found": True, "label": "cold desert biome", "obsolete": iri.endswith("9"),
                                                       "replaced_by": None, "synonyms": ["cold desert"]})
    monkeypatch.setattr(chk, "_gbif", lambda uri: {"found": True, "name": "Melonis barleeanus", "rank": "SPECIES",
                                                   "status": "SYNONYM", "accepted": "Melonis affinis (Reuss, 1851)"})
    monkeypatch.setattr(chk, "_wikidata", lambda uri: {"found": True, "label": "Bolivina", "taxon_name": "Bolivina"})
    maps = load([
        {"subject_id": "s1", "subject_label": "Melonis barleeanus", "object_id": GBIF + "1", "predicate_id": "exactMatch"},
        {"subject_id": "s2", "subject_label": "Bolivina sp.", "object_id": "http://www.wikidata.org/entity/Q1", "predicate_id": "exactMatch"},
        {"subject_id": "h1", "subject_label": "Cold desert", "object_id": ENVO + "1", "object_label": "cold desert", "predicate_id": "exactMatch"},
        {"subject_id": "h2", "subject_label": "Old", "object_id": ENVO + "9", "object_label": "old", "predicate_id": "closeMatch"},
    ])
    check(maps, online=True)
    msgs = {m["subject_id"]: " ".join(f["message"] for f in m["checks"]) for m in maps}
    assert "synonym" in msgs["s1"] and "Melonis affinis" in msgs["s1"]
    assert msgs["s2"] == ""                                           # "Bolivina sp." is the genus Bolivina
    assert msgs["h1"] == ""                                           # a synonym of the OLS label is fine
    assert "obsolete" in msgs["h2"]
    assert maps[0]["verified"]["label"] == "Melonis barleeanus"


def test_reviews_and_exports(tmp_path, linker_dir):
    maps = load(linker_dir / "mappings.json")
    check(maps)
    store = ReviewStore(tmp_path / "r.jsonl")
    by_key = {m["key"]: m for m in maps}
    store.add({"key": maps[1]["key"], "verdict": "correct", "reviewer": "Ana"}, by_key)
    store.add({"key": maps[2]["key"], "verdict": "incorrect", "reviewer": "Ana", "comment": "too broad"}, by_key)
    state = store.state()
    rows = export_csv(maps, state).strip().splitlines()
    assert rows[0].startswith("subject_id,subject_label") and len(rows) == 4
    sssom = export_sssom(maps, state, "pangaea")
    assert "#   ENVO: http://purl.obolibrary.org/obo/ENVO_" in sssom and "ENVO:01000179" in sssom
    body = [line for line in sssom.splitlines() if not line.startswith("#")]
    assert len(body) == 3 and "\tNot\t" in sssom                       # header + 2 decided


def test_web_tool_upload_review_export(tmp_path, linker_dir):
    from verdant_validator.web import create_app
    incoming, reviews = tmp_path / "incoming", tmp_path / "reviews"
    incoming.mkdir()
    (incoming / "pangaea.json").write_text(json.dumps(LINKER))
    (incoming / "settings.json").write_text(json.dumps({"a": 1}))         # not mappings: not listed
    (incoming / "run1").mkdir()
    (incoming / "run1" / "mappings.json").write_text(json.dumps(LINKER[:1]))
    (incoming / "run1" / "cache").mkdir()
    (incoming / "run1" / "cache" / "abc.json").write_text("[]")           # deeper: ignored
    app = create_app(folder=incoming, reviews_dir=reviews, online=False)
    client = app.test_client()
    names = [d["name"] for d in client.get("/api/datasets").get_json()]
    assert names == ["pangaea", "run1-mappings"]
    up = client.post("/upload", data={"file": (io.BytesIO(b"name,target_uri\nTundra,http://purl.obolibrary.org/obo/ENVO_01000180\n"), "tundra.csv")},
                     content_type="multipart/form-data")
    assert up.status_code == 200 and up.get_json()["name"] == "tundra"
    bad = client.post("/upload", data={"file": (io.BytesIO(b"a,b\n1,2\n"), "nothing.csv")}, content_type="multipart/form-data")
    assert bad.status_code == 400 and not (incoming / "nothing.csv").exists()
    assert client.get("/d/pangaea/").status_code == 200 and client.get("/d/missing/").status_code == 404
    maps = client.get("/d/pangaea/api/mappings").get_json()["mappings"]
    ok = client.post("/d/pangaea/api/reviews", json={"key": maps[0]["key"], "verdict": "correct", "reviewer": "Ana"})
    assert ok.status_code == 200
    assert (reviews / "pangaea.jsonl").exists() and "correct" in (reviews / "pangaea-reviews.csv").read_text()
    assert client.post("/d/pangaea/api/reviews", json={"key": "nope", "verdict": "correct", "reviewer": "Ana"}).status_code == 400
    assert "ManualMappingCuration" in client.get("/d/pangaea/export.sssom.tsv").get_data(as_text=True)


def test_main_viewer_serves_the_tool_under_validate(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "x.json").write_text(json.dumps(LINKER))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VALIDATE_DIR", str(incoming))
    monkeypatch.setenv("VALIDATE_REVIEWS_DIR", str(tmp_path / "reviews"))
    path = Path(__file__).resolve().parent.parent / "viz" / "app.py"
    spec = importlib.util.spec_from_file_location("viz_app_with_validate", path)
    viz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(viz)
    viz.app.root_path = str(path.parent)
    client = viz.app.test_client()
    assert b'href="validate/"' in client.get("/").data
    assert client.get("/validate").headers["Location"].endswith("/validate/")
    assert [d["name"] for d in client.get("/validate/api/datasets").get_json()] == ["x"]
    assert client.get("/validate/d/x/").status_code == 200


def test_validator_in_python(tmp_path, linker_dir):
    from verdant_validator import Validator
    v = Validator(linker_dir / "mappings.json", reviews=tmp_path / "r.jsonl")
    assert len(v) == 3 and v.name.endswith("-mappings")
    report = v.check()
    assert report["mappings"] == 3 and v.summary()["checks"]["online"] is False
    warn = v.findings(level="warning")
    assert {f["check"] for f in warn} == {"many_exact"}                        # two exact links for "Cold desert"
    assert v.findings(level="error") == []
    first = v.pending()[:2]
    assert {m["key"] for m in first} == {m["key"] for m in v.maps if v.level(m) == "warning"}       # flagged first
    assert first[0]["confidence"] <= first[1]["confidence"]                                           # least sure first
    k = v.find("desert")[1]["key"]
    v.reject(k, reviewer="Ana", comment="too broad")
    v.accept(v.find("cold desert")[0]["key"], reviewer="Ana", match_type="closeMatch")
    assert v.status(k) == "incorrect" and len(v.pending()) == 1
    assert v.summary()["decisions"] == {"correct": 1, "incorrect": 1, "unsure": 0, "disputed": 0, "unreviewed": 1}
    rows = v.to_records()
    assert {r["status"] for r in rows} == {"correct", "incorrect", "unreviewed"}
    sssom = v.to_sssom(tmp_path / "out.sssom.tsv")
    assert "skos:closeMatch" in sssom and "\tNot\t" in sssom and (tmp_path / "out.sssom.tsv").exists()
    with pytest.raises(Exception):
        v.decide(k, "maybe", reviewer="Ana")
    # decisions made in Python and on the page share one file
    assert len((tmp_path / "r.jsonl").read_text().splitlines()) == 2


def test_example_data_loads():
    from verdant_validator import Validator
    root = Path(__file__).resolve().parent.parent
    v = Validator(root / "examples" / "pangaea" / "mappings.json", reviews="/dev/null", name="pangaea")
    v.check()
    assert len(v) == 38 and v.summary()["subjects"] == 17
