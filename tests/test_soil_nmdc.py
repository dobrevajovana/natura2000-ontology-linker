"""Offline tests for the NMDC soil build: fixture records and a small GOLD obo, no network.

Run:  python -m pytest -q tests/test_soil_nmdc.py
"""

from soil import nmdc


def _term(curie, name):
    return {"term": {"id": curie, "name": name, "type": "nmdc:OntologyClass"}, "type": "nmdc:ControlledIdentifiedTermValue"}


def _rec(i, local=("ENVO:00000291", "drainage basin"), path=("Environmental", "Terrestrial", "Soil", "Drainage basin", "Bulk soil")):
    return {
        "id": f"nmdc:bsm-{i}", "name": f"sample {i}",
        "env_broad_scale": _term("ENVO:00000446", "terrestrial biome"),
        "env_local_scale": _term(*local),
        "env_medium": _term("ENVO:00001998", "soil"),
        **dict(zip(nmdc.GOLD_PATH, path)),
        "gold_biosample_identifiers": [f"gold:Gb{i}"],
        "associated_studies": ["nmdc:sty-1"],
    }


GOLD_OBO = """format-version: 1.2

[Term]
id: GOLDTERMS:1
name: Environmental > Terrestrial > Soil

[Term]
id: GOLDTERMS:2
name: Environmental > Terrestrial > Soil > Drainage basin
is_a: GOLDTERMS:1 ! Soil

[Term]
id: GOLDTERMS:3
name: Environmental > Terrestrial > Soil > Drainage basin > Bulk soil
is_a: GOLDTERMS:2 ! Drainage basin
property_value: mixs:env_broad ENVO:00000446
property_value: mixs:env_local ENVO:00000291
property_value: mixs:env_medium ENVO:00001998

[Typedef]
id: part_of
name: part of
"""


def test_reads_triad_gold_path_and_ids():
    s = nmdc.read_sample(_rec(1))
    assert s["env_medium"] == {"id": "ENVO:00001998", "name": "soil"}
    assert s["gold_path"] == ["Environmental", "Terrestrial", "Soil", "Drainage basin", "Bulk soil"]
    assert s["gold_biosamples"] == ["Gb1"] and s["studies"] == ["nmdc:sty-1"]
    assert nmdc.read_sample({"id": "x", "env_medium": {"has_raw_value": "loam"}})["env_medium"] == {"id": None, "name": "loam"}


def test_gold_path_lookup_trims_unclassified_and_falls_back():
    gold = nmdc.GoldIndex(nmdc.parse_gold_obo(GOLD_OBO))
    assert len(gold.by_id) == 3  # Typedef skipped
    assert gold.lookup(["Environmental", "Terrestrial", "Soil", "Drainage basin", "Bulk soil"])[1] == "exactMatch"
    t, match = gold.lookup(["Environmental", "Terrestrial", "Soil", "Unclassified", "Unclassified"])
    assert (t["id"], match) == ("GOLDTERMS:1", "exactMatch")
    t, match = gold.lookup(["environmental", "terrestrial", "soil", "Wetlands", "Peat"])
    assert (t["id"], match) == ("GOLDTERMS:1", "broadMatch")
    assert gold.lookup(["Host-associated"]) is None
    assert gold.ancestors("GOLDTERMS:3") == ["GOLDTERMS:2", "GOLDTERMS:1"]


def test_graph_has_context_tree_gold_and_parents():
    gold = nmdc.GoldIndex(nmdc.parse_gold_obo(GOLD_OBO))
    samples = [nmdc.read_sample(r) for r in [_rec(1), _rec(2), _rec(3, local=("ENVO:01000861", "area of dwarf scrub"))]]
    g = nmdc.build_graph(samples, gold)
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = {(e["source"], e["target"], e["kind"]): e for e in g["edges"]}

    root = "ctx:ENVO:00000446"
    leaf = "ctx:ENVO:00000446 > ENVO:00000291 > ENVO:00001998"
    assert nodes[root]["samples"] == 3 and nodes[leaf]["samples"] == 2
    assert nodes[leaf]["parent"] == "ctx:ENVO:00000446 > ENVO:00000291"
    assert (root, "ctx:ENVO:00000446 > ENVO:00000291", "within") in edges
    assert edges[(leaf, "ENVO:00001998", "envo")]["origin"] == "nmdc_record"
    assert nodes["ENVO:00001998"]["url"] == "http://purl.obolibrary.org/obo/ENVO_00001998"

    e = edges[(leaf, "GOLDTERMS:3", "gold")]
    assert (e["match"], e["samples"], e["agrees"]) == ("skos:exactMatch", 2, True)
    other = edges[("ctx:ENVO:00000446 > ENVO:01000861 > ENVO:00001998", "GOLDTERMS:3", "gold")]
    assert other["agrees"] is False  # GOLD says drainage basin, the sample says dwarf scrub
    assert nodes["GOLDTERMS:1"]["ancestor"] and ("GOLDTERMS:3", "GOLDTERMS:2", "is_a") in edges
    m = g["meta"]
    assert (m["samples"], m["matched_to_gold"], m["gold_classes"], m["gold_disagrees"]) == (3, 3, 1, 1)


def test_download_follows_cursor():
    pages = {"*": {"results": [{"id": "a"}], "meta": {"count": 2, "next_cursor": "c2"}},
             "c2": {"results": [{"id": "b"}], "meta": {"count": 2, "next_cursor": None}}}
    seen = []

    class Resp:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    class Session:
        def get(self, url, params, timeout):
            seen.append(params)
            assert url == "https://api.microbiomedata.org/biosamples" and params["filter"] == "ecosystem_type:Soil"
            return Resp(pages[params["cursor"]])

    assert [r["id"] for r in nmdc.download(session=Session())] == ["a", "b"]
    assert [r["id"] for r in nmdc.download(limit=1, session=Session())] == ["a"]
