"""Offline tests for the joined soil KG (MFD + NMDC).

Run:  python -m pytest -q tests/test_soil_kg.py
"""

import json

from soil import kg, nmdc
from tests.test_soil_nmdc import GOLD_OBO, _rec

MFD_GRAPH = {"nodes": [
    {"id": "mfd:Soil > Natural > Fields", "kind": "mfd", "label": "Fields", "ref": "Soil > Natural > Fields"},
    {"id": "http://purl.obolibrary.org/obo/ENVO_00000291", "kind": "ols4_envo", "label": "drainage basin",
     "url": "http://purl.obolibrary.org/obo/ENVO_00000291"},
    {"id": "mfd:Water > Lake", "kind": "mfd", "label": "Lake", "ref": "Water > Lake"},
    {"id": "http://purl.obolibrary.org/obo/ENVO_00000020", "kind": "ols4_envo", "label": "lake"},
], "edges": [
    {"source": "mfd:Soil > Natural > Fields", "target": "http://purl.obolibrary.org/obo/ENVO_00000291", "kind": "linker",
     "match": "skos:closeMatch", "confidence": 0.85, "direct": True},
    {"source": "mfd:Water > Lake", "target": "http://purl.obolibrary.org/obo/ENVO_00000020", "kind": "linker",
     "match": "skos:exactMatch", "confidence": 1.0, "direct": True},
]}
MFD_ONTO = {"nodes": [
    {"id": "Soil", "label": "Soil", "parent": None, "level_name": "Sample type", "natura2000": None},
    {"id": "Soil > Natural", "label": "Natural", "parent": "Soil", "level_name": "Area type", "natura2000": None},
    {"id": "Soil > Natural > Fields", "label": "Fields", "parent": "Soil > Natural", "level_name": "MFDO1", "natura2000": None},
]}


def _nmdc_graph(gold=None):
    return nmdc.build_graph([nmdc.read_sample(_rec(1))], gold)


def test_norm_id():
    assert kg.norm_id("http://purl.obolibrary.org/obo/ENVO_00000291") == "ENVO:00000291"
    assert kg.norm_id("http://www.wikidata.org/entity/Q1") == "http://www.wikidata.org/entity/Q1"
    assert kg.norm_id("ctx:ENVO:1") == "ctx:ENVO:1"


def test_shared_envo_term_is_one_node_reached_by_both():
    g = kg.merge(MFD_GRAPH, _nmdc_graph())
    nodes = {n["id"]: n for n in g["nodes"]}
    basin = nodes["ENVO:00000291"]
    assert basin["kind"] == "envo" and basin["datasets"] == ["mfd", "nmdc"]
    assert nodes["ENVO:00000020"]["datasets"] == ["mfd"]
    assert nodes["ENVO:00001998"]["datasets"] == ["nmdc"]
    assert nodes["mfd:Soil > Natural > Fields"]["meets_nmdc"] is True
    assert nodes["mfd:Water > Lake"]["meets_nmdc"] is False
    assert g["meta"]["shared_by_kind"] == {"envo": 1} and g["meta"]["mfd_classes_meeting_nmdc"] == 1
    assert not any(e["source"].startswith("http://purl") or e["target"].startswith("http://purl") for e in g["edges"])


def test_gold_bridge_lets_mfd_reach_gold():
    gold = nmdc.GoldIndex(nmdc.parse_gold_obo(GOLD_OBO))
    # NMDC without GOLD on its side: only the bridge connects ENVO to GOLD.
    g = kg.merge(MFD_GRAPH, _nmdc_graph(), gold)
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = {(e["source"], e["target"], e["kind"]) for e in g["edges"]}
    assert ("ENVO:00000291", "GOLDTERMS:3", "gold_env_local") in edges  # GOLDTERMS:3 has env_local ENVO:00000291
    assert ("GOLDTERMS:3", "GOLDTERMS:2", "is_a") in edges
    assert nodes["GOLDTERMS:3"]["datasets"] == ["mfd", "nmdc"] and nodes["GOLDTERMS:1"]["ancestor"]
    assert ("ENVO:00001998", "GOLDTERMS:3", "gold_env_local") not in edges  # only env_local bridges


def test_build_writes_kg_with_both_trees(tmp_path):
    (tmp_path / "mfd").mkdir()
    (tmp_path / "mfd" / "graph.json").write_text(json.dumps(MFD_GRAPH))
    (tmp_path / "mfd" / "ontology.json").write_text(json.dumps(MFD_ONTO))
    (tmp_path / "soil" / "nmdc").mkdir(parents=True)
    (tmp_path / "soil" / "nmdc" / "graph.json").write_text(json.dumps(_nmdc_graph()))
    out = kg.build(tmp_path, tmp_path, gold_url=None)
    saved = json.loads((tmp_path / "soil" / "kg.json").read_text())
    assert saved["meta"] == out["meta"] and out["meta"]["inputs"] == {"mfd": True, "nmdc": True, "gold": False}
    assert [t["parent"] for t in saved["trees"]["mfd"]] == [None, "mfd:Soil", "mfd:Soil > Natural"]
    assert saved["trees"]["nmdc"][0] == {"id": "ctx:ENVO:00000446", "label": "terrestrial biome", "parent": None,
                                         "level": "env_broad_scale", "samples": 1}


def test_app_serves_page_and_kg(tmp_path, monkeypatch):
    from soil import kg_app
    monkeypatch.setattr(kg_app, "DATA_DIR", tmp_path)
    client = kg_app.app.test_client()
    assert client.get("/").status_code == 200
    assert json.loads(client.get("/data/kg.json").data)["nodes"] == []  # nothing built yet
    (tmp_path / "soil").mkdir()
    (tmp_path / "soil" / "kg.json").write_text(json.dumps({"meta": {}, "nodes": [{"id": "x"}], "edges": []}))
    assert json.loads(client.get("/data/kg.json").data)["nodes"] == [{"id": "x"}]
