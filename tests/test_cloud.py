"""Offline tests for the cloud samples viewer: ENA runs and OLS4 hits are made up here, no network.

Run:  python -m pytest -q tests/test_cloud.py
"""

import importlib.util
import json
from pathlib import Path

import pytest

from cloud import build
from cloud import reviews as rv

ENVO = "http://purl.obolibrary.org/obo/ENVO_"
RUNS = [
    {"run_accession": "ERR1", "sample_accession": "SAMEA1", "sample_alias": "ADN20191002CLOUD", "library_source": "METAGENOMIC",
     "collection_date_start": "2019-10-01", "collection_date_end": "2019-10-02", "lat": "45.772", "lon": "2.9655",
     "altitude": "1465.0", "read_count": "100", "broad_scale_environmental_context": "air",
     "environmental_medium": "mountain", "scientific_name": "metagenome", "tax_id": "256318", "study_title": "Clouds"},
    {"run_accession": "ERR2", "sample_accession": "SAMEA2", "sample_alias": "ARN20191002CLOUD", "library_source": "METATRANSCRIPTOMIC",
     "collection_date_start": "2019-10-01", "lat": "45.772", "lon": "2.9655", "broad_scale_environmental_context": "air",
     "scientific_name": "metagenome", "tax_id": "256318"},
    {"run_accession": "ERR3", "sample_accession": "SAMEA3", "sample_alias": "ADN20200707AIR", "library_source": "METAGENOMIC",
     "collection_date_start": "2020-07-06", "broad_scale_environmental_context": "air", "scientific_name": "metagenome",
     "tax_id": "256318"},
]


def hit(uri, label, match="skos:exactMatch", conf=0.9, query="air", source="ENVO"):
    return {"uri": uri, "curie": uri.rsplit("/", 1)[-1].replace("_", ":"), "label": label, "source": source,
            "match": match, "confidence": conf, "method": "ols4", "query": query}


@pytest.fixture
def samples():
    return build.runs_to_samples(RUNS)


@pytest.fixture
def graph(samples):
    items = build.lookups(samples)
    hits = {("broad_scale_environmental_context", "air"): [hit(ENVO + "00002005", "air"),
                                                           hit(ENVO + "03501122", "airport", "skos:relatedMatch", 0.35)],
            ("sample_type", "cloud"): [hit(ENVO + "01000760", "cloud", query="cloud")]}
    for key, h in build.taxid_hits(samples).items():
        hits[key] = [h]
    in_graph = {ENVO + "00002005": [{"id": "habitat_8310_caves", "label": "8310 Caves", "category": "habitat",
                                     "match": "skos:relatedMatch", "confidence": 0.4, "url": None}]}
    return build.build_graph(samples, items, hits, in_graph, {ENVO + "03501122": 3})


def test_sample_type_and_library_come_from_the_record(samples):
    assert [(s["run"], s["type"], s["library"]) for s in samples] == [
        ("ERR1", "cloud", "metagenome"), ("ERR2", "cloud", "metatranscriptome"), ("ERR3", "air", "metagenome")]
    assert samples[0]["altitude"] == 1465 and samples[0]["lat"] == 45.772
    assert build.sample_type("weird") is None


def test_lookups_are_shared_across_runs_and_organisms_split_by_sample_type(samples):
    items = {(i["field"], i["text"]): i for i in build.lookups(samples)}
    assert items[("broad_scale_environmental_context", "air")]["runs"] == ["ERR1", "ERR2", "ERR3"]
    assert items[("broad_scale_environmental_context", "air")]["branch"] == "biome"
    assert items[("sample_type", "cloud")]["queries"] == ["cloud"]
    # "<type> metagenome" and "<broad-scale context> metagenome", from the record itself
    assert items[("organism_cloud", "metagenome")]["queries"] == ["cloud metagenome", "air metagenome"]
    assert items[("organism_air", "metagenome")]["queries"] == ["air metagenome"]
    assert items[("library", "metatranscriptome")]["runs"] == ["ERR2"]


def test_ena_tax_id_is_an_exact_ncbitaxon_match(samples):
    h = build.taxid_hits(samples)[("organism_cloud", "metagenome")]
    assert h["curie"] == "NCBITaxon:256318" and h["confidence"] == 1.0 and h["method"] == "ena_tax_id"


def test_graph_marks_terms_already_in_our_graphs(graph):
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes[ENVO + "00002005"]["in_graph"] == ["natura2000"]
    assert nodes[ENVO + "03501122"]["in_graph"] == ["biosample"] and nodes[ENVO + "03501122"]["biosample_samples"] == 3
    assert nodes[ENVO + "01000760"]["in_graph"] == []                        # new term
    assert nodes["n2k:habitat_8310_caves"]["kind"] == "natura2000"
    kinds = {(e["source"], e["kind"]) for e in graph["edges"]}
    assert ("n2k:habitat_8310_caves", "linker") in kinds
    assert ("value:broad_scale_environmental_context=air", "predicted") in kinds
    assert sum(e["kind"] == "has_value" and e["target"] == "value:broad_scale_environmental_context=air" for e in graph["edges"]) == 3


def test_coverage_counts_runs_and_flags_items_ena_cannot_hold():
    cov = {i["item"]: i for g in build.metadata_coverage(RUNS, build.load_plan()) for i in g["items"]}
    assert (cov["Coordinates"]["runs"], cov["Coordinates"]["of"]) == (2, 3)
    assert cov["Dates"]["runs"] == 1                        # ERR1 has both start and end
    assert cov["pH"]["ena_fields"] == [] and cov["pH"]["runs"] == 0


def test_linker_and_biosample_indexes(tmp_path):
    (tmp_path / "terms.json").write_text(json.dumps([{"id": "h1", "label": "1110 Sandbanks", "category": "habitat"}]))
    (tmp_path / "mappings.json").write_text(json.dumps([{"source_id": "h1", "target_uri": ENVO + "1", "match_type": "skos:closeMatch"}]))
    (tmp_path / "geo_kg.json").write_text(json.dumps({
        "nodes": [{"data": {"id": "env:ENVO_2", "props": {"uri": ENVO + "2"}}}],
        "edges": [{"data": {"source": "sample:S1", "target": "env:ENVO_2"}}, {"data": {"source": "sample:S2", "target": "env:ENVO_2"}}]}))
    assert build.linker_index(tmp_path)[ENVO + "1"][0]["label"] == "1110 Sandbanks"
    assert build.biosample_index(tmp_path) == {ENVO + "2": 2}
    assert build.linker_index(tmp_path / "missing") == {} and build.biosample_index(tmp_path / "missing") == {}


def test_reviews_and_exports(tmp_path, graph):
    predicted = rv.predicted_matches(graph)
    key = rv.mapping_key("value:broad_scale_environmental_context=air", ENVO + "00002005")
    bad = rv.mapping_key("value:broad_scale_environmental_context=air", ENVO + "03501122")
    store = rv.ReviewStore(tmp_path / "cloud-reviews.jsonl")
    store.add({"key": key, "verdict": "correct", "reviewer": "Ana", "match_type": "skos:closeMatch"}, predicted)
    store.add({"key": bad, "verdict": "incorrect", "reviewer": "Ana", "comment": "an airport is not air"}, predicted)
    with pytest.raises(rv.ReviewError):
        store.add({"key": "nope", "verdict": "correct", "reviewer": "Ana"}, predicted)

    csv_text = rv.export_csv(predicted, store.state())
    assert csv_text.splitlines()[0].startswith("field,sample_text,runs")
    assert len(csv_text.strip().splitlines()) == 1 + len(predicted)        # every predicted term, reviewed or not
    sssom = [line for line in rv.export_sssom(predicted, store.state()).splitlines() if not line.startswith("#")]
    rows = [dict(zip(sssom[0].split("\t"), line.split("\t"))) for line in sssom[1:]]
    assert len(rows) == 2
    air = next(r for r in rows if r["object_label"] == "air")
    assert air["predicate_id"] == "skos:closeMatch" and air["subject_label"] == "air" and air["object_id"] == "ENVO:00002005"
    assert next(r for r in rows if r["object_label"] == "airport")["predicate_modifier"] == "Not"


def _write_data(root: Path, graph, samples):
    (root / "cloud").mkdir(parents=True, exist_ok=True)
    (root / "cloud" / "graph.json").write_text(json.dumps(graph))
    (root / "cloud" / "samples.json").write_text(json.dumps({"meta": {}, "samples": samples, "coverage": []}))


def test_review_api_writes_log_and_snapshot(tmp_path, monkeypatch, graph, samples):
    from cloud import app as cloud_app
    _write_data(tmp_path, graph, samples)
    monkeypatch.setattr(cloud_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cloud_app, "REVIEWS_FILE", tmp_path / "reviews" / "cloud-reviews.jsonl")
    client = cloud_app.app.test_client()
    assert client.get("/data/graph.json").status_code == 200
    assert client.get("/data/other.json").status_code == 404
    (tmp_path / "cloud" / "air.json").write_text("{}")
    assert client.get("/data/air.json").status_code == 200
    key = rv.mapping_key("value:sample_type=cloud", ENVO + "01000760")
    assert client.post("/api/reviews", json={"key": key, "verdict": "correct"}).status_code == 400   # no name
    ok = client.post("/api/reviews", json={"key": key, "verdict": "correct", "reviewer": "Ana"})
    assert ok.status_code == 200 and ok.get_json()["state"]["verdict"] == "correct"
    assert "correct" in (tmp_path / "reviews" / "cloud-match-reviews.csv").read_text()
    assert client.get("/api/reviews").get_json()["state"][key]["reviewer"] == "Ana"


def test_main_viewer_serves_the_cloud_viewer_under_cloud(tmp_path, monkeypatch, graph, samples):
    from cloud import app as cloud_app
    _write_data(tmp_path, graph, samples)
    monkeypatch.setattr(cloud_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cloud_app, "REVIEWS_FILE", tmp_path / "cloud" / "cloud-reviews.jsonl")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = Path(__file__).resolve().parent.parent / "viz" / "app.py"
    spec = importlib.util.spec_from_file_location("viz_app_with_cloud", path)
    viz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(viz)
    viz.app.root_path = str(path.parent)
    client = viz.app.test_client()

    assert b'href="cloud/"' in client.get("/").data
    assert client.get("/cloud").headers["Location"].endswith("/cloud/")
    page = client.get("/cloud/")
    assert page.status_code == 200 and b"Cloud Samples" in page.data
    assert len(client.get("/cloud/data/samples.json").get_json()["samples"]) == 3
    key = rv.mapping_key("value:sample_type=cloud", ENVO + "01000760")
    assert client.post("/cloud/api/reviews", json={"key": key, "verdict": "unsure", "reviewer": "Ana"}).status_code == 200
    assert client.get("/mfd/").status_code == 200                      # the MFD viewer is still there


def test_ncbi_lineage_joins_taxa_up_to_metagenomes(samples):
    items = build.lookups(samples)
    tax = "http://purl.obolibrary.org/obo/NCBITaxon_"
    hits = {("organism_cloud", "metagenome"): [hit(tax + "1643811", "cloud metagenome", source="NCBITaxon")]}
    lineages = {"1643811": {"name": "cloud metagenome", "rank": "species", "division": "Environmental samples",
                            "lineage": [{"id": "2787823", "name": "unclassified entries"}, {"id": "12908", "name": "unclassified sequences"},
                                        {"id": "408169", "name": "metagenomes"}, {"id": "410657", "name": "ecological metagenomes"}]}}
    g = build.build_graph(samples, items, hits, {}, {}, lineages)
    chain = [(e["source"], e["target"]) for e in g["edges"] if e["kind"] == "lineage"]
    assert chain == [(tax + "1643811", tax + "410657"), (tax + "410657", tax + "408169")]   # stops below the generic roots
    node = next(n for n in g["nodes"] if n["id"] == tax + "1643811")
    assert node["rank"] == "species" and node["lineage"].endswith("ecological metagenomes")


def test_name_date_that_disagrees_with_ena_is_flagged():
    assert build.date_conflict({"name": "ADN20201118AIR", "start": "2020-01-11", "end": "2020-01-11"}) == "2020-11-18"
    assert build.date_conflict({"name": "ADN20191002CLOUD", "start": "2019-10-01", "end": "2019-10-02"}) is None
    assert build.date_conflict({"name": "nodate", "start": "2019-10-01", "end": None}) is None


def test_air_metagenomes_mapping(tmp_path, monkeypatch):
    from cloud import air
    rows = [
        {"sample_accession": "S1", "lat": "45.7", "lon": "2.9", "country": "France", "altitude": "1465", "collection_date": "2020-07-07",
         "broad_scale_environmental_context": "atmosphere [ENVO:01000267]", "environmental_medium": "air", "project_name": "P1"},
        {"sample_accession": "S2", "lat": "45.7", "lon": "2.9", "country": "France", "environment_material": "air",
         "isolation_source": "indoor air of an office", "project_name": "P1"},
        {"sample_accession": "S3", "lat": "missing", "country": "USA", "environmental_medium": "air", "description": "pig farm"},
        {"sample_accession": "S4", "environmental_medium": "missing", "description": "nothing"},
    ]
    monkeypatch.setattr(air, "fetch", lambda raw, refresh=False: rows)
    monkeypatch.setattr(air, "envo_labels", lambda ids, raw: {"01000267": {"label": "atmosphere", "synonyms": []}})
    monkeypatch.setattr(air, "search_texts", lambda texts, use_cache=True: {
        ("medium", "air"): {"uri": ENVO + "00002005", "curie": "ENVO:00002005", "label": "air", "source": "ENVO",
                            "match": "skos:exactMatch", "confidence": 0.9, "method": "ols4", "query": "air"}})
    monkeypatch.setattr(air, "REVIEW_MIN", 3)
    summary, items, hits = air.build_air(tmp_path)
    m = summary["meta"]
    assert (m["samples"], m["with_coordinates"], m["sites"], m["mapped"]) == (4, 2, 1, 3)
    assert m["settings"] == {"outdoor": 1, "indoor": 1, "agricultural": 1, "not stated": 1}
    medium = {t["curie"]: t for t in summary["terms"]["medium"]}
    assert medium["ENVO:00002005"]["samples"] == 3                         # the older field name counts too
    assert summary["terms"]["broad"][0]["methods"] == {"envo_id_in_record": 1}
    assert [s["accession"] for s in summary["similar"]] == ["S1"]          # 1465 m
    assert [i["text"] for i in items] == ["air"] and hits[("air_medium", "air")][0]["curie"] == "ENVO:00002005"
    assert air.clean("  Not Collected ") == "" and air.setting("Swine barn") == "agricultural"


def test_envo_id_that_contradicts_its_words_is_a_conflict():
    from cloud import air
    t = lambda label, *syn: {"label": label, "synonyms": list(syn)}
    assert air.id_conflict("atmosphere [ENVO:01000004]", t("concentration basin mediterranean sea biome")) == "atmosphere"
    assert air.id_conflict("atmosphere [ENVO:01000267]", t("atmosphere")) is None
    assert air.id_conflict("ENVO:00000428", t("biome")) is None
    assert air.id_conflict("urban [ENVO:01000249]", t("urban biome")) is None
    assert air.id_conflict("particulate matter [ENVO:01000060]", t("particulate environmental material", "particulate matter")) is None


def test_sample_type_and_library_words_are_read_from_any_naming():
    assert build.sample_type("ADN20191002CLOUD") == "cloud" and build.sample_type("S12_soil") == "soil"
    assert build.library_word("METATRANSCRIPTOMIC") == "metatranscriptome" and build.library_word("GENOMIC") == "genome"


def test_cf_names_are_found_by_the_items_words():
    from cloud import auto
    cf = {n: {"units": "u", "version": "95"} for n in ["air_temperature", "air_temperature_at_cloud_top", "altitude",
                                                        "cloud_top_altitude", "wind_speed", "relative_humidity",
                                                        "atmosphere_boundary_layer_thickness", "sea_water_temperature"]}
    ctx = {"cloud", "air"}
    assert auto.cf_hits("Temperature", cf, ctx)[0]["label"] == "air_temperature"           # the study's medium first
    assert auto.cf_hits("Temperature", cf, ctx)[0]["match"] == "skos:exactMatch"
    assert auto.cf_hits("Altitude", cf, ctx)[0]["label"] == "altitude"                     # fewest extra words
    assert auto.cf_hits("Boundary layer height", cf, ctx)[0]["label"] == "atmosphere_boundary_layer_thickness"
    assert auto.cf_hits("Cell counts", cf, ctx) == []


def test_gold_path_follows_the_sample_setting(tmp_path, monkeypatch):
    from cloud import auto
    docs = [{"iri": "https://w3id.org/gold.vocab/Air", "label": "Air"},
            {"iri": "https://w3id.org/gold.path/3961", "label": "Environmental > Air > Indoor Air"},
            {"iri": "https://w3id.org/gold.path/3962", "label": "Environmental > Air > Outdoor Air"},
            {"iri": "https://w3id.org/gold.path/3963", "label": "Environmental > Air"}]
    monkeypatch.setattr(auto, "ranked", lambda q, o, raw, branch="", rows=10: docs if q == "air" else [])
    outdoor = auto.gold_path_hit(["cloud", "air"], "outdoor", tmp_path)
    assert outdoor["lineage"] == "Environmental > Air > Outdoor Air" and outdoor["match"] == "skos:broadMatch"  # via the context
    assert auto.gold_path_hit(["air"], "indoor", tmp_path)["lineage"].endswith("Indoor Air")
    assert auto.gold_path_hit(["air"], "not stated", tmp_path)["lineage"] == "Environmental > Air"           # shortest
    assert auto.gold_path_hit(["soil"], "outdoor", tmp_path) is None


def test_each_run_gets_its_own_terms_by_rule_and_dna_rna_pairs(samples):
    items = build.lookups(samples)
    tax = "http://purl.obolibrary.org/obo/NCBITaxon_"
    hits = {("sample_type", "cloud"): [{**hit(ENVO + "01000760", "cloud", query="cloud"), "material": True},
                                       {**hit(ENVO + "03600081", "cloud water", "skos:relatedMatch", 0.35, "cloud"), "material": True},
                                       hit(ENVO + "01000912", "airport", conf=0.95, query="cloud"),   # not a material
                                       {**hit("https://w3id.org/gold.path/3962", "Outdoor Air", source="GOLD"), "lineage": "Environmental > Air > Outdoor Air"}],
            ("organism_cloud", "metagenome"): [hit(tax + "256318", "metagenome", conf=1.0, source="NCBITaxon") | {"method": "ena_tax_id"},
                                               hit(tax + "1643811", "cloud metagenome", query="cloud metagenome", source="NCBITaxon") | {"method": "ols4"}],
            ("library", "metagenome"): [hit(build.OBI_URI + "0002623", "whole metagenome sequencing assay", source="OBI")],
            ("library", "metatranscriptome"): [hit(build.OBI_URI + "0002760", "metatranscriptome profiling assay", source="OBI")]}
    defaults = build.run_defaults(samples, hits)
    assert defaults[("material", "cloud")][0] == ENVO + "01000760"             # the best material hit
    assert defaults[("organism", "cloud")][0] == tax + "1643811"              # "<type> metagenome" beats ENA's generic one
    assert defaults[("ecosystem", "cloud")][0].endswith("/3962")
    g = build.build_graph(samples, items, hits, {}, {}, project="PRJ1", place={"label": "Puy de Dôme"})
    links = build.run_links(samples, {n["id"]: n for n in g["nodes"]}, defaults)
    err1 = {(e["role"], e["target"]) for e in links if e["source"] == "run:ERR1" and e["kind"] == "run_term"}
    assert ("material", ENVO + "01000760") in err1 and ("assay", build.OBI_URI + "0002623") in err1
    assert [(e["source"], e["target"]) for e in links if e["kind"] == "paired_with"] == [("run:ERR1", "run:ERR2")]
    assert any(n["id"] == "site:puy-de-d-me" or n["label"] == "Puy de Dôme" for n in g["nodes"] if n["kind"] == "site")


def test_repository_answers_are_remembered_when_a_check_fails(tmp_path, monkeypatch):
    from cloud import auto
    monkeypatch.setattr(auto, "check_mgnify", lambda sec: "not analysed yet (no MGnify study for it)")
    monkeypatch.setattr(auto, "check_microbiomedb", lambda p, t: None)      # no answer, nothing remembered yet
    first = {r["name"]: r for r in auto.repositories("PRJ1", "ERP1", "Clouds", 3, "ERR1", tmp_path)}
    assert first["MGnify"]["status"].startswith("not analysed") and first["MicrobiomeDB"]["status"].startswith("unknown")
    assert "cannot be checked by a program" in first["MicrobeAtlas"]["status"] and first["ENA"]["status"] == "holds the 3 runs"
    monkeypatch.setattr(auto, "check_mgnify", lambda sec: None)            # MGnify down: the last answer, dated
    assert "as of" in {r["name"]: r for r in auto.repositories("PRJ1", "ERP1", "Clouds", 3, "ERR1", tmp_path)}["MGnify"]["status"]


def test_air_list_is_read_and_compared(tmp_path):
    from cloud import air
    f = tmp_path / "air_sample.tsv"
    f.write_text("sample_accession\tsample_description\ttax_id\nSAMEA1\tair\t655179\nSAMN2\tx\t655179\n")
    assert air.read_list(f) == {"SAMEA1", "SAMN2"} and air.read_list(None) is None
