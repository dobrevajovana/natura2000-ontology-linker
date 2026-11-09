"""Offline tests for hierarchy-based broad/narrow matches: OLS4 stubbed, no network.

Run:  python -m pytest -q tests/test_hierarchy.py
"""

import pytest

from geoapp import ontolink

OBO = "http://purl.obolibrary.org/obo/"
INTESTINE, LARGE_INTESTINE, GUT = "UBERON:0000160", "UBERON:0000059", "UBERON:0001007"
C = ontolink._CONFIDENCE


def _iri(curie):
    return OBO + curie.replace(":", "_")


def _doc(curie, label, **syn):
    return {"iri": _iri(curie), "label": label, "obo_id": curie, **syn}


@pytest.fixture(autouse=True)
def _tmp_cache(monkeypatch, tmp_path):
    # Ancestor sets are cached on disk; keep each test's cache to itself.
    monkeypatch.setattr(ontolink.config, "CACHE_DIR", tmp_path)


def _stub(monkeypatch, docs, ancestors):
    """Serve ``docs`` for searches and ``ancestors[curie]`` for class lookups.

    A curie missing from ``ancestors`` (or ``ancestors=None``) is a failed fetch.
    Returns the list of class-endpoint URLs requested, to count the extra cost.
    """
    class_calls = []

    def fake(session, url, *, params, **kw):
        if url == ontolink.config.OLS4_SEARCH:
            return {"response": {"docs": docs}}
        class_calls.append(url)
        for curie, above in (ancestors or {}).items():
            if url.endswith(curie.replace(":", "_")):
                return {"iri": _iri(curie), "hierarchicalAncestor": [_iri(a) for a in above]}
        return None

    monkeypatch.setattr(ontolink, "_request_json", fake)
    return class_calls


def _types(hits):
    return {h.curie: (h.match_type, h.confidence) for h in hits}


def test_an_ancestor_of_the_exact_hit_is_broader(monkeypatch):
    # a similar-looking ancestor would otherwise be a similarity relatedMatch
    _stub(monkeypatch, [_doc("UBERON:0001153", "caecum"), _doc("UBERON:0009999", "caecal")],
          {"UBERON:0001153": ["UBERON:0009999", INTESTINE]})
    assert ontolink.classify_hit("caecum", {"label": "caecal"})[0] == "relatedMatch"
    hits = _types(ontolink.ols4_search("caecum", "uberon", None))
    assert hits["UBERON:0001153"] == ("exactMatch", C["exact"])
    assert hits["UBERON:0009999"] == ("broadMatch", C["hierarchy"])


def test_a_dissimilar_ancestor_is_kept_as_broader(monkeypatch):
    # 'caecum' is part of the large intestine, which is part of the intestine;
    # by spelling alone 'intestine' would be dropped
    _stub(monkeypatch, [_doc("UBERON:0001153", "caecum"), _doc(INTESTINE, "intestine"), _doc(GUT, "gut")],
          {"UBERON:0001153": [LARGE_INTESTINE, INTESTINE]})
    assert ontolink.classify_hit("caecum", {"label": "intestine"}) is None
    hits = _types(ontolink.ols4_search("caecum", "uberon", None))
    assert hits == {"UBERON:0001153": ("exactMatch", C["exact"]), INTESTINE: ("broadMatch", C["hierarchy"])}


def test_a_descendant_of_the_exact_hit_is_narrower(monkeypatch):
    # 'serosa of intestine' escapes the head-noun rule and would be relatedMatch
    _stub(monkeypatch, [_doc(INTESTINE, "intestine"), _doc("UBERON:0001243", "serosa of intestine")],
          {INTESTINE: [GUT], "UBERON:0001243": [INTESTINE, GUT]})
    hits = _types(ontolink.ols4_search("intestine", "uberon", None))
    assert hits["UBERON:0001243"] == ("narrowMatch", C["hierarchy"])


def test_an_unrelated_similarity_hit_keeps_its_type(monkeypatch):
    _stub(monkeypatch, [_doc(INTESTINE, "intestine"), _doc("UBERON:0001242", "intestinal mucosa")],
          {INTESTINE: [GUT], "UBERON:0001242": [GUT]})
    before = ontolink.classify_hit("intestine", {"label": "intestinal mucosa"})
    hits = _types(ontolink.ols4_search("intestine", "uberon", None))
    assert hits["UBERON:0001242"] == before


def test_synonym_types_are_never_overruled_but_head_noun_guesses_are(monkeypatch):
    # 'gut' is relatedMatch by a related synonym, which the ontology asserts: kept
    # even against a contradicting ancestry. 'large intestine' is narrowMatch only
    # by the head-noun guess (12% precise), so the ancestry wins there.
    _stub(monkeypatch, [_doc(INTESTINE, "intestine"), _doc(LARGE_INTESTINE, "large intestine"),
                        _doc(GUT, "gut", related_synonyms=["intestine"])],
          {INTESTINE: [LARGE_INTESTINE, GUT]})
    hits = _types(ontolink.ols4_search("intestine", "uberon", None))
    assert hits == {INTESTINE: ("exactMatch", C["exact"]), GUT: ("relatedMatch", C["related_synonym"]),
                    LARGE_INTESTINE: ("broadMatch", C["hierarchy"])}


def test_a_failed_ancestor_fetch_changes_nothing_and_is_not_cached(monkeypatch):
    docs = [_doc("UBERON:0001153", "caecum"), _doc(INTESTINE, "caecal")]
    expected = ontolink.classify_hit("caecum", {"label": "caecal"})
    _stub(monkeypatch, docs, None)
    hits = _types(ontolink.ols4_search("caecum", "uberon", None))
    assert hits[INTESTINE] == expected and hits["UBERON:0001153"] == ("exactMatch", C["exact"])
    # the next run, with OLS4 back, gets the hierarchy: the failure was not cached
    _stub(monkeypatch, docs, {"UBERON:0001153": [INTESTINE]})
    assert _types(ontolink.ols4_search("caecum", "uberon", None))[INTESTINE][0] == "broadMatch"


def test_ancestor_sets_are_cached(monkeypatch):
    docs = [_doc("UBERON:0001153", "caecum"), _doc(INTESTINE, "intestine")]
    calls = _stub(monkeypatch, docs, {"UBERON:0001153": [INTESTINE]})
    ontolink.ols4_search("caecum", "uberon", None)
    ontolink.ols4_search("caecum", "uberon", None)
    assert len(calls) == 1


@pytest.mark.parametrize("docs", [
    # no exact hit: nothing to anchor on
    [_doc("UBERON:0001242", "intestinal mucosa"), _doc(LARGE_INTESTINE, "large intestinal mucosa")],
    # two terms share the name, so neither is the anchor (both become closeMatch)
    [_doc(INTESTINE, "intestine"), _doc(GUT, "gut", exact_synonyms=["intestine"]),
     _doc("UBERON:0001242", "intestinal mucosa")],
])
def test_no_anchor_means_no_hierarchy_requests(monkeypatch, docs):
    calls = _stub(monkeypatch, docs, {})
    ontolink.ols4_search("intestine", "uberon", None)
    assert calls == []
