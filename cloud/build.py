"""Turn the cloud study's ENA runs into a reviewable subgraph.

Source: ENA project PRJEB54740, "Metatranscriptomic analysis of cloud and
aerosol samples at puy de Dôme, France" (CNRS): every run, its sample record
from BioSamples, and the sample's environment and organism text searched in
OLS4 (ENVO and NCBITaxon) with the calibrated matcher from ``geoapp``.

Each OLS4 hit is a predicted match an expert can review. A hit whose term is
already in our graphs is marked so: in the Natura 2000 linker's mappings it is
joined to the habitats or species that use it, in the BioSample graph
(``geo_kg.json``) it carries the number of samples there that have it. The
others are new terms for the graph.

Beyond OLS4: MGnify's biome and GOLD's ecosystem path for cloud and air
samples (with the gold-ontology project's mappings to ENVO, mostly lexical), CF Standard Names (and
ENVO) for the measurements in the project's metadata plan, and the NCBI
Taxonomy lineage of every predicted taxon. Which microbiome repositories hold
the study is recorded too.

Output (``<data>/cloud/``):

* ``samples.json`` -- the runs, one sample each, plus which of the cloud
  project's metadata the ENA records hold.
* ``graph.json`` -- study, site, samples, their attribute values, the
  predicted ENVO / NCBITaxon terms and the Natura 2000 terms they share.

Everything is looked up by function (cloud/auto.py); the only inputs are the
plan file (cloud/plan.json: project accession, metadata plan, themes) and,
optionally, a list of air metagenome samples (--air-list).

Run:  python -m cloud.build [--data-dir data] [--linker-dir data] [--refresh] [--plan FILE] [--air-list TSV]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
from collections import Counter
from pathlib import Path

import requests

logger = logging.getLogger("cloud.build")

ENA_FILEREPORT = "https://www.ebi.ac.uk/ena/portal/api/filereport"
ENA_VIEW = "https://www.ebi.ac.uk/ena/browser/view/"
PLAN_FILE = Path(__file__).with_name("plan.json")
RUN_FIELDS = [
    "run_accession", "sample_accession", "experiment_accession", "sample_alias", "sample_title", "study_title",
    "center_name", "library_source", "library_strategy", "library_selection", "instrument_model", "read_count",
    "base_count", "collection_date", "collection_date_start", "collection_date_end", "lat", "lon", "altitude",
    "country", "broad_scale_environmental_context", "local_environmental_context", "environmental_medium",
    "scientific_name", "tax_id", "temperature",
]

# The sample's own text, by the field it came from, and where to search it.
# branch: the ENVO subtree MIxS ties the field to (see geoapp.ontolink.ENVO_BRANCHES).
LOOKUP_FIELDS = [
    ("broad_scale_environmental_context", "Broad-scale environmental context", "envo", "biome"),
    ("local_environmental_context", "Local environmental context", "envo", ""),
    ("environmental_medium", "Environmental medium", "envo", "material"),
]
SAMPLE_TYPE_FIELD = "Sample type (from the sample name)"
ORGANISM_FIELD = "Organism"
LIBRARY_FIELD = "Library source"

ENVO_URI = ENVO_URI_ = "http://purl.obolibrary.org/obo/ENVO_"
OBI_URI = "http://purl.obolibrary.org/obo/OBI_"
NCBI_TAXON_URI = "http://purl.obolibrary.org/obo/NCBITaxon_"
NCBI_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
GOLD_VOCAB_URI = "https://w3id.org/gold.vocab/"
GOLD_ENVO_SSSOM = "https://raw.githubusercontent.com/cmungall/gold-ontology/main/mappings/gold-to-envo.curated.sssom.tsv"
CF_TABLE = "https://cfconventions.org/Data/cf-standard-names/current/src/cf-standard-name-table.xml"
TERM_KINDS = {"ENVO": "envo", "NCBITaxon": "ncbitaxon", "MGnify": "mgnify", "GOLD": "gold", "CF": "cf", "OBI": "obi",
              "PATO": "envo", "CHEBI": "envo"}
KEEP_HITS = 3
ROLES = ["material", "ecosystem", "organism", "assay"]


def load_plan(path: Path | None = None) -> dict:
    """The study's inputs (cloud/plan.json): project accession, metadata plan, themes."""
    path = Path(path or os.environ.get("CLOUD_PLAN") or PLAN_FILE)
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_runs(raw_dir: Path, project: str, refresh: bool = False) -> list[dict]:
    """Every read run of the project, from the ENA portal (cached as TSV)."""
    out = raw_dir / f"runs_{project}.tsv"
    if refresh or not out.exists():
        logger.info("Fetching runs of %s from ENA", project)
        resp = requests.get(ENA_FILEREPORT, params={"accession": project, "result": "read_run",
                                                    "fields": ",".join(RUN_FIELDS), "format": "tsv"}, timeout=60)
        resp.raise_for_status()
        raw_dir.mkdir(parents=True, exist_ok=True)
        # ENA sends no charset; the text is UTF-8 ("puy de Dôme").
        out.write_bytes(resp.content)
    return list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8")), delimiter="\t"))


def sample_type(alias: str) -> str | None:
    """The word a sample name ends in after its date or number: ADN20191002CLOUD is
    "cloud", ARN20200707AIR "air". None when the name does not end that way."""
    m = re.search(r"\d[_-]?([A-Za-z]{2,})$", alias or "")
    return m.group(1).lower() if m else None


def library_word(source: str | None) -> str:
    """ENA's library source as a noun: METAGENOMIC -> metagenome, METATRANSCRIPTOMIC -> metatranscriptome."""
    return re.sub(r"omic$", "ome", (source or "").lower())


def runs_to_samples(runs: list[dict]) -> list[dict]:
    out = []
    for r in sorted(runs, key=lambda r: (r.get("collection_date_start") or "", r["run_accession"])):
        out.append({
            "run": r["run_accession"], "sample": r["sample_accession"], "experiment": r.get("experiment_accession"),
            "name": r.get("sample_alias") or r.get("sample_title"),
            "type": sample_type(r.get("sample_alias") or r.get("sample_title")),
            "library": library_word(r.get("library_source")),
            "start": r.get("collection_date_start") or r.get("collection_date"),
            "end": r.get("collection_date_end") or r.get("collection_date"),
            "lat": _num(r.get("lat")), "lon": _num(r.get("lon")), "altitude": _num(r.get("altitude")),
            "reads": _num(r.get("read_count")), "bases": _num(r.get("base_count")),
            "instrument": r.get("instrument_model"),
            "organism": r.get("scientific_name"), "tax_id": r.get("tax_id"),
            "attributes": {f: r.get(f) for f, *_ in LOOKUP_FIELDS if r.get(f)},
            "url": ENA_VIEW + r["run_accession"],
        })
        out[-1]["date_conflict"] = date_conflict(out[-1])
    return out


def date_conflict(s: dict) -> str | None:
    """The date in the sample name (ADN20201118AIR) when ENA's collection dates disagree with it."""
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", s.get("name") or "")
    if not m or not s.get("start"):
        return None
    named = f"{m[1]}-{m[2]}-{m[3]}"
    return None if named in (s["start"], s["end"]) else named


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def metadata_coverage(runs: list[dict], plan: dict) -> list[dict]:
    """For each item of the project's metadata plan: how many runs record it in ENA."""
    total = len(runs)
    out = []
    for g in plan["metadata_plan"]:
        rows = []
        for it in g["items"]:
            fields = it.get("ena_fields") or []
            have = sum(1 for r in runs if fields and all((r.get(f) or "").strip() for f in fields))
            rows.append({"item": it["item"], "ena_fields": fields, "runs": have, "of": total})
        out.append({"group": g["group"], "items": rows})
    return out


def lookups(samples: list[dict]) -> list[dict]:
    """The distinct (field, text) pairs to search, each with the samples that carry it."""
    found: dict[tuple, dict] = {}

    def add(field, label, text, ontology, branch, run, queries=None):
        key = (field, text.lower())  # field is unique per lookup, organism_cloud and so on
        item = found.setdefault(key, {"field": field, "field_label": label, "text": text, "ontology": ontology,
                                      "branch": branch, "queries": queries or [text], "runs": []})
        item["runs"].append(run)

    for s in samples:
        for field, label, ontology, branch in LOOKUP_FIELDS:
            if s["attributes"].get(field):
                add(field, label, s["attributes"][field], ontology, branch, s["run"])
        if s["type"]:
            add("sample_type", SAMPLE_TYPE_FIELD, s["type"], "envo", "", s["run"])
        if s["organism"]:
            # ENA's own organism (its tax_id is the exact match) is often the generic
            # "metagenome"; NCBITaxon names metagenomes by environment, so each sample
            # type is searched as "<type> metagenome" and "<broad context> metagenome".
            context = [s["type"], (s["attributes"].get("broad_scale_environmental_context") or "").lower()]
            qs = list(dict.fromkeys(f"{w} metagenome" for w in context if w)) or [s["organism"]]
            label = f"{ORGANISM_FIELD} ({s['type']} samples)" if s["type"] else ORGANISM_FIELD
            add(f"organism_{s['type']}" if s["type"] else "organism", label, s["organism"], "ncbitaxon", "", s["run"], qs)
        if s["library"]:
            add("library", LIBRARY_FIELD, s["library"], "", "", s["run"], [])
    return list(found.values())


def plan_items(plan: dict) -> list[dict]:
    """The metadata plan's measurements as lookups with no runs: the vocabulary
    their values will be recorded in, once there are values. Searched in ENVO
    (which imports PATO and ChEBI) and matched to CF Standard Names."""
    return [{"field": "plan", "field_label": f"Metadata plan: {g['group']}", "text": it["item"], "ontology": "envo",
             "branch": "", "queries": [it["item"]], "runs": [], "plan": True}
            for g in plan["metadata_plan"] for it in g["items"] if it.get("measure")]


def cf_names(raw_dir: Path, refresh: bool = False) -> dict[str, dict]:
    """CF standard names with their units and description (the table is cached).
    Empty when the table cannot be had: the curated names are then used unchecked."""
    import xml.etree.ElementTree as ET
    path = raw_dir / "cf-standard-name-table.xml"
    try:
        if refresh or not path.exists():
            resp = requests.get(CF_TABLE, timeout=60)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        root = ET.parse(path).getroot()
    except (requests.RequestException, ET.ParseError, OSError) as exc:
        logger.warning("CF standard name table unavailable (%s); CF names not checked", exc)
        return {}
    version = root.findtext("version_number")
    return {e.get("id"): {"units": e.findtext("canonical_units"), "description": (e.findtext("description") or "").strip(),
                          "version": version} for e in root.iter("entry")}


def library_hits(items: list[dict], raw_dir: Path) -> dict[tuple, list[dict]]:
    """OBI assays for each library source, from OLS4."""
    from cloud import auto
    return {("library", it["text"].lower()): auto.assay_hits(it["text"], raw_dir)
            for it in items if it["field"] == "library"}


def sample_type_hits(items: list[dict], samples: list[dict], title: str, raw_dir: Path) -> dict[tuple, list[dict]]:
    """For each sample type: ENVO materials, its GOLD ecosystem path and MGnify biome."""
    from cloud import air, auto
    out: dict[tuple, list[dict]] = {}
    for it in items:
        if it["field"] != "sample_type":
            continue
        mine = [s for s in samples if s["type"] == it["text"]]
        broad = sorted({(s["attributes"].get("broad_scale_environmental_context") or "").lower() for s in mine} - {""})
        text = " ".join([it["text"], title or "", *(v for s in mine for v in s["attributes"].values())])
        hits = auto.material_hits(it["text"], raw_dir)
        gold = auto.gold_path_hit([it["text"], *broad], air.setting(text), raw_dir)
        if gold:
            hits.append(gold)
            biome = auto.mgnify_biome_hit(gold, raw_dir)
            if biome:
                hits.append(biome)
        out[("sample_type", it["text"].lower())] = hits
    return out


def run_defaults(samples: list[dict], hits: dict[tuple, list[dict]]) -> dict[tuple, tuple[str, str]]:
    """Each run's default term per role, chosen from the hits by rule:
    material: the best ENVO material for the sample type; ecosystem: its GOLD path;
    organism: an exact "<type> metagenome", else the first exact one, else ENA's taxon;
    assay: the first OBI assay for the library. Returns (role, key) -> (term URI, lookup field)."""
    def best(hs):
        return max(hs, key=lambda h: h["confidence"]) if hs else None

    out = {}
    for s in samples:
        t = (s["type"] or "").lower()
        st = hits.get(("sample_type", t), [])
        m = best([h for h in st if h.get("material")])
        if m:
            out[("material", t)] = (m["uri"], "sample_type")
        g = next((h for h in st if h["source"] == "GOLD"), None)
        if g:
            out[("ecosystem", t)] = (g["uri"], "sample_type")
        org = hits.get((f"organism_{t}", (s["organism"] or "").lower()), [])
        o = (next((h for h in org if h["query"] == f"{t} metagenome" and h["match"] == "skos:exactMatch"), None)
             or next((h for h in org if h["method"] == "ols4" and h["match"] == "skos:exactMatch"), None)
             or next((h for h in org if h["method"] == "ena_tax_id"), None))
        if o:
            out[("organism", t)] = (o["uri"], f"organism_{t}")
        a = hits.get(("library", s["library"]), [])
        if a:
            out[("assay", s["library"])] = (a[0]["uri"], "library")
    return out


def run_links(samples: list[dict], graph_nodes: dict, defaults: dict[tuple, tuple[str, str]]) -> list[dict]:
    """Each run's default term per role (see run_defaults), and DNA/RNA runs from the same sampling."""
    edges = []
    for s in samples:
        for role in ROLES:
            key = s["library"] if role == "assay" else (s["type"] or "")
            if (role, key) not in defaults:
                continue
            target, field = defaults[(role, key)]
            text = s["organism"] if field.startswith("organism") else s["library"] if field == "library" else s["type"]
            vid = f"value:{field}={(text or '').lower()}"
            if target in graph_nodes and vid in graph_nodes:
                edges.append({"source": "run:" + s["run"], "target": target, "kind": "run_term", "role": role, "value": vid})
    pairs: dict[tuple, list[dict]] = {}
    for s in samples:
        pairs.setdefault((s["type"], s["start"]), []).append(s)
    for group in pairs.values():
        dna = [s for s in group if s["library"] == "metagenome"]
        rna = [s for s in group if s["library"] == "metatranscriptome"]
        for a, b in zip(dna, rna):
            edges.append({"source": "run:" + a["run"], "target": "run:" + b["run"], "kind": "paired_with"})
    return edges


def plan_hits(items: list[dict], cf: dict[str, dict], context: set[str]) -> dict[tuple, list[dict]]:
    """CF Standard Names for each measurement in the plan, by its words."""
    from cloud import auto
    return {("plan", it["text"].lower()): auto.cf_hits(it["text"], cf, context) for it in items if it.get("plan")}


def gold_envo(raw_dir: Path, paths: list[str], refresh: bool = False) -> list[dict]:
    """Mappings from the words in the linked GOLD ``paths`` to ENVO (cached).

    From the gold-ontology project (cmungall/gold-ontology), not from JGI. The
    file is named "curated", but nearly every row is a lexical match made by a
    program (semapv:LexicalMatching, oaklib); each row's justification is kept.
    """
    path = raw_dir / "gold-to-envo.sssom.tsv"
    try:
        if refresh or not path.exists():
            resp = requests.get(GOLD_ENVO_SSSOM, timeout=60)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        text = path.read_text(encoding="utf-8")
    except (requests.RequestException, OSError) as exc:
        logger.warning("GOLD to ENVO mappings unavailable (%s)", exc)
        return []
    # The first word is the top of GOLD's tree (Environmental, Engineered, ...): too broad to link.
    words = {w.replace(" ", "-") for p in paths for w in p.split(" > ")[1:]}
    rows = csv.DictReader((line for line in io.StringIO(text) if not line.startswith("#")), delimiter="\t")
    out = []
    for r in rows:
        word = (r.get("subject_id") or "").removeprefix("GOLDVOCAB:")
        if word in words and (r.get("object_id") or "").startswith("ENVO:"):
            out.append({"gold": word, "gold_label": r.get("subject_label"), "envo": r["object_id"],
                        "envo_label": r.get("object_label"), "match": r.get("predicate_id"),
                        "confidence": round(float(r.get("confidence") or 0), 2),
                        "justification": (r.get("mapping_justification") or "").removeprefix("semapv:")})
    return out


def ncbi_lineages(taxids: set[str], raw_dir: Path) -> dict[str, dict]:
    """Rank, division and lineage of each taxon from NCBI Taxonomy (cached)."""
    import xml.etree.ElementTree as ET
    path = raw_dir / "ncbi_taxonomy.json"
    cached = _load_json(path, {})
    missing = sorted(t for t in taxids if t not in cached)
    if missing:
        try:
            resp = requests.get(NCBI_EFETCH, params={"db": "taxonomy", "id": ",".join(missing), "retmode": "xml"}, timeout=60)
            resp.raise_for_status()
            for t in ET.fromstring(resp.content).findall("Taxon"):
                cached[t.findtext("TaxId")] = {
                    "name": t.findtext("ScientificName"), "rank": t.findtext("Rank"), "division": t.findtext("Division"),
                    "lineage": [{"id": x.findtext("TaxId"), "name": x.findtext("ScientificName")}
                                for x in t.findall("LineageEx/Taxon")]}
            path.write_text(json.dumps(cached))
        except (requests.RequestException, ET.ParseError) as exc:
            logger.warning("NCBI Taxonomy lookup failed (%s); no lineages this time", exc)
    return {t: cached[t] for t in taxids if t in cached}


def search(items: list[dict], use_cache: bool = True) -> dict[tuple, list[dict]]:
    """OLS4 hits per lookup, best first; the archive's own taxon id counts as exact."""
    from geoapp import config as geo_config
    from geoapp import ontolink

    session = requests.Session()
    session.headers.update({"User-Agent": geo_config.USER_AGENT, "Accept": "application/json"})
    out = {}
    for it in items:
        hits: dict[str, dict] = {}
        for q in it["queries"]:
            for t in ontolink.search_text(q, it["ontology"], session, it["branch"], use_cache):
                # A hit for a helper query ("cloud water" for a cloud sample) is judged
                # against that query; the reviewer sees which one it was.
                prev = hits.get(t.uri)
                if prev is None or t.confidence > prev["confidence"]:
                    hits[t.uri] = {"uri": t.uri, "curie": t.curie, "label": t.label, "source": t.source,
                                   "match": "skos:" + t.match_type, "confidence": round(t.confidence, 2),
                                   "method": "ols4", "query": q}
        ranked = sorted(hits.values(), key=lambda h: -h["confidence"])
        # Several helper queries can each bring an exact hit; keep a few per query.
        keep, per_query = [], {}
        for h in ranked:
            if per_query.get(h["query"], 0) < KEEP_HITS:
                keep.append(h)
                per_query[h["query"]] = per_query.get(h["query"], 0) + 1
        out[(it["field"], it["text"].lower())] = keep
    return out


def taxid_hits(samples: list[dict]) -> dict[tuple, dict]:
    """The NCBITaxon term ENA itself assigns (tax_id), per organism lookup."""
    out = {}
    for s in samples:
        if s["organism"] and s["tax_id"]:
            field = f"organism_{s['type']}" if s["type"] else "organism"
            out[(field, s["organism"].lower())] = {
                "uri": f"http://purl.obolibrary.org/obo/NCBITaxon_{s['tax_id']}", "curie": f"NCBITaxon:{s['tax_id']}",
                "label": s["organism"], "source": "NCBITaxon", "match": "skos:exactMatch", "confidence": 1.0,
                "method": "ena_tax_id", "query": f"tax_id {s['tax_id']}"}
    return out


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def biosample_index(linker_dir: Path) -> dict[str, int]:
    """Term URI -> how many samples of the BioSample graph (geo_kg.json) carry it."""
    graph = _load_json(linker_dir / "geo_kg.json", {})
    uris = {}
    for n in graph.get("nodes", []):
        d = n.get("data", n)
        uri = (d.get("props") or {}).get("uri")
        if uri:
            uris[d["id"]] = uri
    out: dict[str, int] = {}
    for e in graph.get("edges", []):
        d = e.get("data", e)
        if d.get("target") in uris and str(d.get("source", "")).startswith("sample:"):
            out[uris[d["target"]]] = out.get(uris[d["target"]], 0) + 1
    for uri in uris.values():
        out.setdefault(uri, 0)
    return out


def linker_index(linker_dir: Path) -> dict[str, list[dict]]:
    """Target URI -> the Natura 2000 terms our graph already links to it."""
    terms = {t["id"]: t for t in _load_json(linker_dir / "terms.json", [])}
    mappings = _load_json(linker_dir / "mappings.json", [])
    mappings += _load_json(linker_dir / "mappings_added.json", {}).get("mappings", [])
    out: dict[str, list[dict]] = {}
    for m in mappings:
        uri = m.get("target_uri")
        t = terms.get(m.get("source_id"))
        if uri and t:
            out.setdefault(uri, []).append({
                "id": t["id"], "label": t.get("label"), "category": t.get("category"),
                "match": m.get("match_type"), "confidence": m.get("confidence"), "url": t.get("source_url")})
    return out


def build_graph(samples: list[dict], items: list[dict], hits: dict, in_graph: dict[str, list[dict]],
                in_biosamples: dict[str, int] | None = None, lineages: dict[str, dict] | None = None,
                gold_envo_maps: list[dict] | None = None, project: str = "", place: dict | None = None) -> dict:
    in_biosamples, lineages, gold_envo_maps = in_biosamples or {}, lineages or {}, gold_envo_maps or []
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def node(nid, kind, label, **extra):
        nodes.setdefault(nid, {"id": nid, "kind": kind, "label": label, **extra})

    def edge(src, dst, kind, **extra):
        edges.setdefault((src, dst, kind), {"source": src, "target": dst, "kind": kind, **extra})

    study = f"study:{project}"
    node(study, "study", project, url=ENA_VIEW + project)
    site = next(((s["lat"], s["lon"], s["altitude"]) for s in samples if s["lat"] is not None), (None, None, None))
    place = place or {}
    site_id = "site:" + (re.sub(r"[^a-z0-9]+", "-", (place.get("label") or "site").lower()).strip("-") or "site")
    node(site_id, "site", place.get("label") or "Sampling site", lat=site[0], lon=site[1], altitude=site[2],
         wikidata=place.get("wikidata"), place_type=place.get("type"), distance_km=place.get("distance_km"))
    for s in samples:
        sid = "run:" + s["run"]
        node(sid, "sample", s["name"] or s["run"], run=s["run"], sample=s["sample"], type=s["type"], reads=s["reads"],
             library=s["library"], start=s["start"], end=s["end"], url=s["url"])
        edge(study, sid, "has_run")
        edge(sid, site_id, "collected_at")

    for it in items:
        vid = f"value:{it['field']}={it['text'].lower()}"
        node(vid, "value", it["text"], field=it["field"], field_label=it["field_label"], runs=len(it["runs"]),
             plan=bool(it.get("plan")), air=bool(it.get("air")), samples=it.get("samples"))
        for run in it["runs"]:
            edge("run:" + run, vid, "has_value", field=it["field_label"])
        for rank, h in enumerate(hits.get((it["field"], it["text"].lower()), [])):
            linked = in_graph.get(h["uri"], [])
            where = (["natura2000"] if linked else []) + (["biosample"] if h["uri"] in in_biosamples else [])
            extra = {k: h[k] for k in ("units", "description", "cf_version", "lineage", "verified") if h.get(k) is not None}
            node(h["uri"], TERM_KINDS.get(h["source"], "term"), h["label"], curie=h["curie"], url=h["uri"],
                 in_graph=where, biosample_samples=in_biosamples.get(h["uri"]), **extra)
            edge(vid, h["uri"], "predicted", match=h["match"], confidence=h["confidence"], method=h["method"],
                 query=h["query"], rank=rank, field=it["field_label"], runs=len(it["runs"]), in_graph=where)
            for t in linked:
                nid = "n2k:" + t["id"]
                node(nid, "natura2000" if t["category"] == "habitat" else "n2k_" + (t["category"] or "term"),
                     t["label"], url=t["url"])
                edge(nid, h["uri"], "linker", match=t["match"], confidence=t["confidence"])

    # GOLD: the path's words and GOLD's own mappings of them to ENVO; MGnify's
    # biome is the same path.
    for n in list(nodes.values()):
        if n["kind"] != "gold":
            continue
        for m in [m for m in gold_envo_maps if m["gold"].replace("-", " ") in n.get("lineage", "")]:
            wid = GOLD_VOCAB_URI + m["gold"]
            node(wid, "gold", m["gold_label"], curie=f"GOLDVOCAB:{m['gold']}", url=wid, in_graph=[], lineage_only=True)
            edge(n["id"], wid, "path_word")
            eid = ENVO_URI + m["envo"].removeprefix("ENVO:")
            node(eid, "envo", m["envo_label"], curie=m["envo"], url=eid, in_graph=[])
            edge(wid, eid, "gold_envo", match=m["match"], confidence=m["confidence"], justification=m.get("justification"))
        for b in nodes.values():
            if b["kind"] == "mgnify" and b.get("lineage", "").split(":")[1:] == n.get("lineage", "").split(" > "):
                edge(b["id"], n["id"], "same_path")

    # NCBI Taxonomy: each predicted taxon up its lineage to "metagenomes", so
    # cloud and air metagenome meet the generic metagenome in the graph.
    for n in list(nodes.values()):
        tid = n["id"].removeprefix(NCBI_TAXON_URI) if n["kind"] == "ncbitaxon" else None
        info = lineages.get(tid) if tid else None
        if not info:
            continue
        n.update(rank=info["rank"], division=info["division"], lineage=" > ".join(x["name"] for x in info["lineage"]))
        child = n["id"]
        for anc in reversed(info["lineage"]):
            if anc["name"] in ("unclassified entries", "unclassified sequences"):
                break  # the root of every metagenome; adds nothing
            aid = NCBI_TAXON_URI + anc["id"]
            node(aid, "ncbitaxon", anc["name"], curie=f"NCBITaxon:{anc['id']}", url=aid, lineage_only=True, in_graph=[])
            edge(child, aid, "lineage")
            child = aid

    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def build(data_dir: Path, linker_dir: Path, refresh: bool = False, use_cache: bool = True,
          plan: dict | None = None) -> dict:
    """Everything is looked up: ENA for the runs, OLS4, GOLD, MGnify, CF, NCBI and
    Wikidata for the terms (cloud/auto.py). The plan file only says which study,
    and what the project means to record."""
    from cloud import auto
    plan = plan or load_plan()
    project = plan["project"]
    out_dir = data_dir / "cloud"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "raw"
    # Left by the earlier, hand-set version of this build.
    for name in ("runs.tsv", "mgnify_status.json", "envo_labels.json"):
        (raw / name).unlink(missing_ok=True)
    runs = fetch_runs(raw, project, refresh)
    samples = runs_to_samples(runs)
    info = auto.study(project, raw)
    first = runs[0] if runs else {}
    title = info.get("study_title") or first.get("study_title")

    items = lookups(samples)
    items += plan_items(plan)
    hits = search(items, use_cache)
    for key, h in taxid_hits(samples).items():
        hits[key] = [h] + [x for x in hits.get(key, []) if x["uri"] != h["uri"]]
    for key, hs in sample_type_hits(items, samples, title, raw).items():
        have = {h["uri"]: h for h in hits.get(key, [])}
        for h in hs:
            if h["uri"] in have:
                # Already found by the plain search: it is a material too.
                have[h["uri"]]["material"] = have[h["uri"]].get("material") or bool(h.get("material"))
            else:
                hits.setdefault(key, []).append(h)
    hits.update(library_hits(items, raw))
    # CF names: the study's own words (sample types, broad contexts) count as its medium.
    context = {s["type"] for s in samples if s["type"]} | {
        w for s in samples for w in re.findall(r"[a-z]+", (s["attributes"].get("broad_scale_environmental_context") or "").lower())}
    for key, hs in plan_hits(items, cf_names(raw, refresh), context).items():
        hits[key] = hits.get(key, []) + hs

    # Every air metagenome sample in the archives (cloud/air.py), for comparison;
    # a failure there must not stop the cloud study's own build.
    air_meta = None
    try:
        from cloud import air
        listed = air.read_list(plan.get("air_list") or os.environ.get("CLOUD_AIR_LIST"))
        summary, air_items, air_hits = air.build_air(raw, refresh, use_cache, listed=listed,
                                                     study_samples={s["sample"] for s in samples})
        items += air_items
        hits.update(air_hits)
        (out_dir / "air.json").write_text(json.dumps(summary, ensure_ascii=False))
        air_meta = summary["meta"]
    except Exception:
        logger.exception("Could not map the air metagenome samples; the Air metagenomes tab stays empty")

    taxids = {h["uri"].removeprefix(NCBI_TAXON_URI) for hs in hits.values() for h in hs if h["source"] == "NCBITaxon"}
    gold_paths = sorted({h["lineage"] for hs in hits.values() for h in hs if h["source"] == "GOLD"})
    lat_lon = next(((s["lat"], s["lon"]) for s in samples if s["lat"] is not None), None)
    place = auto.site(*lat_lon, raw) if lat_lon else None
    graph = build_graph(samples, items, hits, linker_index(linker_dir), biosample_index(linker_dir),
                        ncbi_lineages(taxids, raw), gold_envo(raw, gold_paths, refresh), project, place)
    graph["edges"] += run_links(samples, {n["id"]: n for n in graph["nodes"]}, run_defaults(samples, hits))
    repositories = auto.repositories(project, info.get("secondary_study_accession"), title, len(samples),
                                     samples[0]["run"] if samples else None, raw)

    predicted = [e for e in graph["edges"] if e["kind"] == "predicted"]
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    types = [t for t, _ in sorted(Counter(s["type"] for s in samples if s["type"]).items(), key=lambda kv: -kv[1])]
    meta = {
        "project": project, "url": ENA_VIEW + project, "title": title, "secondary": info.get("secondary_study_accession"),
        "center": info.get("center_name") or first.get("center_name"), "runs": len(samples),
        "types": types, "type_counts": {t: sum(s["type"] == t for s in samples) for t in types},
        "dates": len({s["start"] for s in samples}),
        "site": place, "plan_title": plan.get("title"), "themes": plan.get("themes", []),
        "predicted": len(predicted),
        "in_natura2000_graph": sum("natura2000" in e["in_graph"] for e in predicted),
        "in_biosample_graph": sum("biosample" in e["in_graph"] for e in predicted),
        "new_terms": len({e["target"] for e in predicted if not e["in_graph"]}),
        "terms": len({e["target"] for e in predicted}),
        "by_vocabulary": {k: len({e["target"] for e in predicted if kinds.get(e["target"]) == k})
                          for k in set(TERM_KINDS.values())},
        "record_text": {f: sorted({s["attributes"][f] for s in samples if s["attributes"].get(f)}) for f, *_ in LOOKUP_FIELDS},
        "organisms": sorted({s["organism"] for s in samples if s["organism"]}),
        "repositories": repositories,
        "air": air_meta,
    }
    (out_dir / "samples.json").write_text(json.dumps(
        {"meta": meta, "samples": samples, "coverage": metadata_coverage(runs, plan)}, ensure_ascii=False))
    graph["meta"] = meta
    (out_dir / "graph.json").write_text(json.dumps(graph, ensure_ascii=False))
    logger.info("Wrote %d runs, %d predicted matches (%d new terms of %d) to %s",
                len(samples), len(predicted), meta["new_terms"], meta["terms"], out_dir)
    return meta


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("CLOUD_DATA_DIR") or os.environ.get("DATA_DIR", "data")))
    ap.add_argument("--linker-dir", type=Path, default=Path(os.environ.get("LINKER_DIR", "data")))
    ap.add_argument("--refresh", action="store_true", help="fetch the run list from ENA again")
    ap.add_argument("--no-cache", action="store_true", help="search OLS4 again instead of using the cache")
    ap.add_argument("--plan", type=Path, help="the study's plan file (default: cloud/plan.json, or CLOUD_PLAN)")
    ap.add_argument("--air-list", type=Path, help="a TSV of sample accessions (such as air_sample.tsv) to limit the air map to")
    args = ap.parse_args()
    plan = load_plan(args.plan)
    if args.air_list:
        plan["air_list"] = str(args.air_list)
    build(args.data_dir, args.linker_dir, args.refresh, not args.no_cache, plan)


if __name__ == "__main__":
    main()
