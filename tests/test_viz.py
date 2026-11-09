"""Offline tests for the viewer's API and its ontology manager: OLS4 stubbed, no network.

Run:  python -m pytest -q
"""

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

from geoapp import ontolink
from geoapp.schema import OntologyTerm

TERMS = [
    {"id": "habitat_7110_active_raised_bogs", "label": "7110 * Active raised bogs", "category": "habitat", "codes": ["7110"]},
    {"id": "habitat_1130_estuaries", "label": "1130 Estuaries", "category": "habitat", "codes": ["1130"]},
    {"id": "species_lucanus_cervus", "label": "Lucanus cervus", "scientific_name": "Lucanus cervus", "category": "species"},
]
MAPPINGS = [{"source_id": "species_lucanus_cervus", "target_uri": "http://www.wikidata.org/entity/Q190491",
             "target_label": "Lucanus cervus", "match_type": "skos:exactMatch", "confidence": 1.0,
             "source_ontology": "wikidata"}]


@pytest.fixture
def viz(tmp_path, monkeypatch):
    (tmp_path / "terms.json").write_text(json.dumps(TERMS))
    (tmp_path / "mappings.json").write_text(json.dumps(MAPPINGS))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = Path(__file__).resolve().parent.parent / "viz" / "app.py"
    spec = importlib.util.spec_from_file_location("viz_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_ontology_title", lambda ont: ont.upper())
    return mod


def _hit(label, mtype, conf):
    return OntologyTerm(uri=f"https://w3id.org/gold.vocab/{label}", curie=f"GOLD:{label}", source="GOLD",
                        label=label, match_type=mtype, confidence=conf, origin="ols4")


def _wait_for_job(client, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = client.get("/api/ontologies").get_json()["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_bad_query_parameters_fall_back_instead_of_failing(viz):
    c = viz.app.test_client()
    assert c.get("/api/graph?limit=abc&min_confidence=x").status_code == 200


def test_term_link_counts_cover_every_mapping(viz):
    [beetle] = viz.app.test_client().get("/api/terms?category=species").get_json()
    assert beetle["link_counts"] == {"wikidata": 1}


def test_linking_an_ontology_keeps_well_supported_links_and_can_be_removed(viz, monkeypatch):
    answers = {"Estuaries": [_hit("Estuary", "closeMatch", 0.90)], "Active raised bogs": [_hit("Bog", "broadMatch", 0.30)]}
    monkeypatch.setattr(ontolink, "ols4_search", lambda text, ont, session, branch="": answers.get(text, []))
    c = viz.app.test_client()
    assert c.post("/api/ontologies", json={"id": "gold", "categories": ["habitat"]}).status_code == 202
    job = _wait_for_job(c)
    assert (job["state"], job["total"], job["linked"]) == ("done", 2, 1)   # the 0.30 guess is left out
    stats = c.get("/api/stats").get_json()
    assert stats["mappings_by_ontology"]["gold"] == 1 and stats["total_mappings"] == 2
    [m] = c.get("/api/term/habitat_1130_estuaries").get_json()["mappings"]
    assert (m["target_label"], m["match_type"], m["added_ontology"]) == ("Estuary", "skos:closeMatch", "gold")

    assert c.delete("/api/ontologies/gold").status_code == 200
    assert c.get("/api/stats").get_json()["total_mappings"] == 1
    assert c.get("/api/ontologies").get_json()["added"] == []


def test_guesses_are_kept_when_asked_for_and_flagged(viz, monkeypatch):
    monkeypatch.setattr(ontolink, "ols4_search",
                        lambda text, ont, session, branch="": [_hit("Bog", "broadMatch", 0.30)] if "bogs" in text else [])
    c = viz.app.test_client()
    c.post("/api/ontologies", json={"id": "gold", "categories": ["habitat"], "min_confidence": 0.30})
    assert _wait_for_job(c)["linked"] == 1
    [m] = c.get("/api/term/habitat_7110_active_raised_bogs").get_json()["mappings"]
    assert m["review"] is True


def test_a_hung_request_does_not_hold_the_job_forever(viz, monkeypatch):
    release = threading.Event()

    def search(text, ont, session, branch=""):
        if text == "Estuaries":
            release.wait(5)          # never answers within the stall window
            return []
        return [_hit("Bog", "broadMatch", 0.90)]

    monkeypatch.setattr(ontolink, "ols4_search", search)
    monkeypatch.setattr(viz, "STALL_SECONDS", 0.3)
    c = viz.app.test_client()
    c.post("/api/ontologies", json={"id": "gold", "categories": ["habitat"]})
    job = _wait_for_job(c, timeout=3)
    release.set()
    assert (job["state"], job["linked"], job["skipped"], job["skipped_terms"]) == ("done", 1, 1, ["Estuaries"])
    [added] = c.get("/api/ontologies").get_json()["added"]
    assert (added["count"], added["skipped"]) == (1, 1)


@pytest.mark.parametrize("body, status", [
    ({"id": "Not An Id!"}, 400), ({"id": "gold", "categories": []}, 400), ({"id": "gold", "categories": ["region"]}, 400),
])
def test_bad_requests_are_refused_with_a_reason(viz, body, status):
    r = viz.app.test_client().post("/api/ontologies", json=body)
    assert r.status_code == status and r.get_json()["error"]


def test_a_second_job_waits_for_the_first(viz, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(ontolink, "ols4_search", lambda *a, **k: gate.wait(5) and [])
    c = viz.app.test_client()
    assert c.post("/api/ontologies", json={"id": "gold"}).status_code == 202
    assert c.post("/api/ontologies", json={"id": "uberon"}).status_code == 409
    gate.set()
    _wait_for_job(c)


def test_catalog_lists_ols4_ontologies_and_is_cached(viz, monkeypatch):
    import requests

    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"_embedded": {"ontologies": [
                {"ontologyId": "uberon", "numberOfTerms": 15000, "config": {"title": "Uber-anatomy ontology", "description": "x " * 200}},
                {"ontologyId": "aro", "numberOfTerms": 8000, "config": {"title": "Antibiotic Resistance Ontology"}},
                {"ontologyId": "Bad Id", "config": {}},
            ]}}

    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(1) or Resp())
    c = viz.app.test_client()
    items = c.get("/api/ontologies/catalog").get_json()
    assert [i["id"] for i in items] == ["aro", "uberon"]           # sorted, bad ids dropped
    assert items[1]["terms"] == 15000 and len(items[1]["description"]) <= 161
    c.get("/api/ontologies/catalog")
    assert len(calls) == 1


def test_catalog_failure_says_so(viz, monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", boom)
    r = viz.app.test_client().get("/api/ontologies/catalog")
    assert r.status_code == 502 and "unavailable" in r.get_json()["error"]
