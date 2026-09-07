"""Knowledge-graph construction and serialisation.

Builds a de-duplicated node/edge graph out of normalised :class:`~geoapp.schema.Sample`
records, then exports it as Cytoscape.js JSON, GeoJSON (map layer) and Turtle
(RDF). ``save_all`` writes every artefact under ``config.DATA_DIR``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from . import config
from .schema import ARCHIVE_SOURCES, Edge, Node, ONTOLOGY_SOURCES, Sample

logger = logging.getLogger(__name__)

try:
    from rdflib import Graph as _RDFGraph, Literal, Namespace, RDF, RDFS, URIRef, XSD
    _HAS_RDFLIB = True
except ImportError:
    _HAS_RDFLIB = False

_BASE = "https://example.org/geoapp/"
_GS_NS = f"{_BASE}ns#"

_SKOS_PREDICATES = {
    "exactMatch": "http://www.w3.org/2004/02/skos/core#exactMatch",
    "closeMatch": "http://www.w3.org/2004/02/skos/core#closeMatch",
    "relatedMatch": "http://www.w3.org/2004/02/skos/core#relatedMatch",
}

_EDGE_PREDICATE = {
    "collected_at": "collectedAt",
    "has_taxon": "hasTaxon",
    "in_environment": "inEnvironment",
    "archived_in": "archivedIn",
    "part_of": "partOf",
    "has_attribute": "hasAttribute",
    "within": "within",
}


def slugify(value: str) -> str:
    """Lowercase, ASCII-ish identifier fragment safe for node ids."""
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "unknown"


def _short_label(text: str, fallback: str, limit: int = 38) -> str:
    """Trim a node caption to something that fits beside a 34px circle.

    Sample titles run to 146 characters in real records; drawn in full they
    overlap their neighbours into an unreadable smear. The untruncated title
    stays in ``props`` for the detail panel.
    """
    text = (text or "").strip()
    if not text:
        return fallback
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut if len(cut) >= limit * 0.6 else text[:limit]).rstrip(" ,;:-") + "\u2026"


def _curie_to_id_fragment(curie: str) -> str:
    return curie.replace(":", "_") if curie else ""


class _GraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return node
        if existing.lat is None and node.lat is not None:
            existing.lat = node.lat
        if existing.lon is None and node.lon is not None:
            existing.lon = node.lon
        for k, v in node.props.items():
            existing.props.setdefault(k, v)
        return existing

    def add_edge(self, source: str, target: str, kind: str, *, label: str = "",
                 confidence: float = 1.0, props: Optional[dict[str, Any]] = None) -> None:
        key = (source, target, kind)
        if key in self.edges:
            return
        eid = f"{kind}:{source}->{target}"
        self.edges[key] = Edge(id=eid, source=source, target=target, kind=kind,
                                label=label, confidence=confidence, props=props or {})

    def result(self) -> tuple[list[Node], list[Edge]]:
        return list(self.nodes.values()), list(self.edges.values())


def build_graph(samples: list[Sample]) -> tuple[list[Node], list[Edge]]:
    """Construct the de-duplicated node/edge graph for ``samples``."""
    g = _GraphBuilder()

    for sample in samples:
        sample_id = f"sample:{sample.accession}"
        loc = sample.location
        g.add_node(Node(
            id=sample_id, label=_short_label(sample.title, sample.accession), kind="sample",
            lat=loc.lat, lon=loc.lon,
            props={
                "accession": sample.accession, "title": sample.title,
                "organism": sample.organism, "collection_date": sample.collection_date,
                "precision": loc.precision, "country": loc.country, "region": loc.region,
            },
        ))

        has_ncbitaxon = False
        for term in sample.ontology_terms:
            if term.source == "NCBITaxon":
                has_ncbitaxon = True
                frag = _curie_to_id_fragment(term.curie)
                taxon_id = f"taxon:{frag}"
                g.add_node(Node(id=taxon_id, label=term.label or term.curie, kind="taxon",
                                 props={"uri": term.uri, "curie": term.curie, "source": term.source}))
                g.add_edge(sample_id, taxon_id, "has_taxon", confidence=term.confidence,
                           label=term.match_type)
            elif term.source == "ENVO":
                frag = _curie_to_id_fragment(term.curie)
                env_id = f"env:{frag}"
                g.add_node(Node(id=env_id, label=term.label or term.curie, kind="environment",
                                 props={"uri": term.uri, "curie": term.curie, "source": term.source}))
                g.add_edge(sample_id, env_id, "in_environment", confidence=term.confidence,
                           label=term.match_type)
            elif term.origin == "ols4" and term.source not in ("NCBITaxon", "ENVO"):
                field = term.matched_field or "attribute"
                value = term.matched_value or term.label
                attr_id = f"attribute:{slugify(field)}:{slugify(value)}"
                g.add_node(Node(id=attr_id, label=_short_label(value, field), kind="attribute",
                                 props={"field": field, "value": value}))
                g.add_edge(sample_id, attr_id, "has_attribute")

                frag = _curie_to_id_fragment(term.curie)
                term_id = f"attribute:term:{frag}"
                g.add_node(Node(id=term_id, label=term.label or term.curie, kind="attribute",
                                 props={"uri": term.uri, "curie": term.curie, "source": term.source,
                                        "is_ontology_term": True}))
                g.add_edge(attr_id, term_id, "ontology_match", confidence=term.confidence,
                           label=term.match_type, props={"match_type": term.match_type})

        if not has_ncbitaxon and sample.organism:
            taxon_id = f"taxon:name:{slugify(sample.organism)}"
            g.add_node(Node(id=taxon_id, label=sample.organism, kind="taxon",
                             props={"name": sample.organism}))
            g.add_edge(sample_id, taxon_id, "has_taxon")

        country_id = None
        if loc.country_code:
            country_id = f"place:country:{loc.country_code.strip().upper()}"
        elif loc.country:
            country_id = f"place:country:{slugify(loc.country).upper()}"
        if country_id:
            props = {"place_type": "country", "name": loc.country, "country_code": loc.country_code}
            if loc.has_coords:
                props["lat"], props["lon"] = loc.lat, loc.lon
            g.add_node(Node(id=country_id, label=loc.country or loc.country_code, kind="place", props=props))

        region_id = None
        if loc.region:
            region_id = f"place:region:{slugify(loc.region)}"
            props = {"place_type": "region", "name": loc.region}
            if loc.has_coords:
                props["lat"], props["lon"] = loc.lat, loc.lon
            g.add_node(Node(id=region_id, label=loc.region, kind="place", props=props))
            if country_id:
                g.add_edge(region_id, country_id, "within")

        most_specific_place = region_id or country_id
        if most_specific_place:
            g.add_edge(sample_id, most_specific_place, "collected_at")

        # One node per archive, not per (archive, sample). Keying these by
        # accession gave every sample three private leaf nodes that connected to
        # nothing else -- 246 of 430 nodes in a typical run, all noise that
        # buried the parts of the graph that actually relate samples to each
        # other. The specific accession belongs on the edge, and is still shown
        # in full in the detail panel.
        for ref in sample.archives:
            archive_id = f"archive:{ref.source}"
            meta = ARCHIVE_SOURCES.get(ref.source, {})
            g.add_node(Node(id=archive_id, label=meta.get("label", ref.source), kind="archive",
                             props={"source": ref.source}))
            g.add_edge(sample_id, archive_id, "archived_in", label=ref.accession,
                       props={"accession": ref.accession, "url": ref.url})

    return g.result()


def to_cytoscape(nodes: list[Node], edges: list[Edge]) -> dict[str, Any]:
    """Render nodes/edges as ``{"nodes": [...], "edges": [...]}`` Cytoscape.js elements."""
    return {
        "nodes": [{"data": n.to_dict()} for n in nodes],
        "edges": [{"data": e.to_dict()} for e in edges],
    }


def to_geojson(samples: list[Sample]) -> dict[str, Any]:
    """FeatureCollection of Point features, one per sample with coordinates (lon-first)."""
    features = []
    for s in samples:
        loc = s.location
        if not loc.has_coords:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [loc.lon, loc.lat]},
            "properties": {
                "accession": s.accession, "title": s.title, "organism": s.organism,
                "country": loc.country, "region": loc.region,
                "collection_date": s.collection_date, "precision": loc.precision,
                "n_ontology_terms": len(s.ontology_terms), "n_archives": len(s.archives),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _turtle_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _node_uri(node: Node) -> str:
    real = node.props.get("uri") if isinstance(node.props, dict) else None
    if real:
        return real
    return _BASE + quote(node.id, safe="")


def to_rdf(samples: list[Sample], nodes: list[Node], edges: list[Edge]) -> str:
    """Turtle serialisation of the graph. Uses rdflib when available, else hand-written."""
    if _HAS_RDFLIB:
        return _to_rdf_rdflib(nodes, edges)
    return _to_rdf_manual(nodes, edges)


def _to_rdf_rdflib(nodes: list[Node], edges: list[Edge]) -> str:
    g = _RDFGraph()
    GS = Namespace(_GS_NS)
    GEO = Namespace("http://www.w3.org/2003/01/geo/wgs84_pos#")
    SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
    DCTERMS = Namespace("http://purl.org/dc/terms/")
    g.bind("gs", GS)
    g.bind("geo", GEO)
    g.bind("skos", SKOS)
    g.bind("dcterms", DCTERMS)
    g.bind("rdfs", RDFS)

    uri_by_id = {n.id: _node_uri(n) for n in nodes}
    for n in nodes:
        subj = URIRef(uri_by_id[n.id])
        g.add((subj, RDF.type, GS[n.kind.capitalize()]))
        if n.label:
            g.add((subj, RDFS.label, Literal(n.label)))
        lat = n.lat if n.lat is not None else n.props.get("lat")
        lon = n.lon if n.lon is not None else n.props.get("lon")
        if lat is not None and lon is not None:
            g.add((subj, GEO.lat, Literal(lat, datatype=XSD.double)))
            g.add((subj, GEO.long, Literal(lon, datatype=XSD.double)))

    for e in edges:
        subj = URIRef(uri_by_id.get(e.source, _BASE + quote(e.source, safe="")))
        obj = URIRef(uri_by_id.get(e.target, _BASE + quote(e.target, safe="")))
        if e.kind == "ontology_match":
            pred = SKOS[e.props.get("match_type", "relatedMatch")]
        else:
            pred = GS[_EDGE_PREDICATE.get(e.kind, e.kind)]
        g.add((subj, pred, obj))

    out = g.serialize(format="turtle")
    return out if isinstance(out, str) else out.decode("utf-8")


def _to_rdf_manual(nodes: list[Node], edges: list[Edge]) -> str:
    lines = [
        f"@prefix gs: <{_GS_NS}> .",
        "@prefix geo: <http://www.w3.org/2003/01/geo/wgs84_pos#> .",
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .",
        "@prefix dcterms: <http://purl.org/dc/terms/> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
    ]
    uri_by_id = {n.id: _node_uri(n) for n in nodes}

    for n in nodes:
        subj = f"<{uri_by_id[n.id]}>"
        stmts = [f"a gs:{n.kind.capitalize()}"]
        if n.label:
            stmts.append(f'rdfs:label "{_turtle_escape(n.label)}"')
        lat = n.lat if n.lat is not None else n.props.get("lat")
        lon = n.lon if n.lon is not None else n.props.get("lon")
        if lat is not None and lon is not None:
            stmts.append(f'geo:lat "{lat}"^^xsd:double')
            stmts.append(f'geo:long "{lon}"^^xsd:double')
        lines.append(f"{subj} " + " ;\n    ".join(stmts) + " .")

    for e in edges:
        subj = f"<{uri_by_id.get(e.source, _BASE + quote(e.source, safe=''))}>"
        obj = f"<{uri_by_id.get(e.target, _BASE + quote(e.target, safe=''))}>"
        if e.kind == "ontology_match":
            pred = f"skos:{e.props.get('match_type', 'relatedMatch')}"
        else:
            pred = f"gs:{_EDGE_PREDICATE.get(e.kind, e.kind)}"
        lines.append(f"{subj} {pred} {obj} .")

    return "\n".join(lines) + "\n"


def stats(samples: list[Sample], nodes: list[Node], edges: list[Edge]) -> dict[str, Any]:
    """Summary counters over samples and the built graph."""
    with_coords = sum(1 for s in samples if s.location.has_coords)
    by_precision: dict[str, int] = {}
    by_country: dict[str, int] = {}
    by_ontology_source: dict[str, int] = {}
    by_archive: dict[str, int] = {}
    taxa: set[str] = set()
    environments: set[str] = set()

    for s in samples:
        by_precision[s.location.precision] = by_precision.get(s.location.precision, 0) + 1
        if s.location.country:
            by_country[s.location.country] = by_country.get(s.location.country, 0) + 1
        for t in s.ontology_terms:
            by_ontology_source[t.source] = by_ontology_source.get(t.source, 0) + 1
            if t.source == "NCBITaxon":
                taxa.add(t.curie)
            if t.source == "ENVO":
                environments.add(t.curie)
        for a in s.archives:
            by_archive[a.source] = by_archive.get(a.source, 0) + 1

    by_node_kind: dict[str, int] = {}
    for n in nodes:
        by_node_kind[n.kind] = by_node_kind.get(n.kind, 0) + 1
    by_edge_kind: dict[str, int] = {}
    for e in edges:
        by_edge_kind[e.kind] = by_edge_kind.get(e.kind, 0) + 1

    return {
        "total_samples": len(samples),
        "samples_with_coordinates": with_coords,
        "samples_without_coordinates": len(samples) - with_coords,
        "by_precision": by_precision,
        "by_country": dict(sorted(by_country.items(), key=lambda kv: kv[1], reverse=True)),
        "by_ontology_source": by_ontology_source,
        "by_archive": by_archive,
        "nodes_by_kind": by_node_kind,
        "edges_by_kind": by_edge_kind,
        "distinct_taxa": len(taxa),
        "distinct_environments": len(environments),
    }


def save_all(samples: list[Sample], nodes: list[Node], edges: list[Edge],
             data_dir: Optional[Path] = None) -> dict[str, Path]:
    """Write samples, KG (JSON + Turtle) and GeoJSON artefacts; return their paths."""
    data_dir = Path(data_dir) if data_dir else config.DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)

    samples_path = config.SAMPLES_JSON if data_dir == config.DATA_DIR else data_dir / config.SAMPLES_JSON.name
    kg_json_path = config.KG_JSON if data_dir == config.DATA_DIR else data_dir / config.KG_JSON.name
    kg_ttl_path = config.KG_TURTLE if data_dir == config.DATA_DIR else data_dir / config.KG_TURTLE.name
    geojson_path = config.GEOJSON if data_dir == config.DATA_DIR else data_dir / config.GEOJSON.name

    samples_path.write_text(json.dumps([s.to_dict() for s in samples], indent=2), encoding="utf-8")
    kg_json_path.write_text(json.dumps(to_cytoscape(nodes, edges), indent=2), encoding="utf-8")
    kg_ttl_path.write_text(to_rdf(samples, nodes, edges), encoding="utf-8")
    geojson_path.write_text(json.dumps(to_geojson(samples), indent=2), encoding="utf-8")

    logger.info("saved %d samples, %d nodes, %d edges to %s", len(samples), len(nodes), len(edges), data_dir)
    return {
        "samples": samples_path, "kg_json": kg_json_path,
        "kg_turtle": kg_ttl_path, "geojson": geojson_path,
    }


def load_samples(path: Optional[Path] = None) -> list[Sample]:
    """Read back the samples written by :func:`save_all`.

    Lets later pipeline stages run without re-fetching, e.g.
    ``python -m geoapp.cli --steps 3,4``. A missing file yields an empty list
    rather than an error, so a first run degrades to "nothing cached yet".
    """
    path = Path(path) if path else config.SAMPLES_JSON
    if not path.exists():
        logger.warning("no cached samples at %s", path)
        return []

    from .schema import ArchiveRef, Location, OntologyTerm

    raw = json.loads(path.read_text(encoding="utf-8"))
    samples: list[Sample] = []
    for item in raw:
        loc = dict(item.get("location") or {})
        loc.pop("has_coords", None)  # derived property, not a constructor field
        samples.append(
            Sample(
                accession=item.get("accession", ""),
                query_accession=item.get("query_accession", ""),
                name=item.get("name", ""),
                title=item.get("title", ""),
                organism=item.get("organism", ""),
                tax_id=item.get("tax_id"),
                collection_date=item.get("collection_date", ""),
                release_date=item.get("release_date", ""),
                center_name=item.get("center_name", ""),
                checklist=item.get("checklist", ""),
                location=Location(**loc),
                ontology_terms=[OntologyTerm(**t) for t in item.get("ontology_terms", [])],
                archives=[ArchiveRef(**a) for a in item.get("archives", [])],
                attributes=item.get("attributes", {}),
                source_url=item.get("source_url", ""),
                fetch_error=item.get("fetch_error", ""),
            )
        )
    logger.info("loaded %d samples from %s", len(samples), path)
    return samples


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from .schema import ArchiveRef, Location, OntologyTerm

    demo = [
        Sample(
            accession="SAMEA1", title="Baltic seawater sample", organism="Escherichia coli",
            collection_date="2021-06-01",
            location=Location(lat=55.7, lon=12.5, precision="coordinates", country="Denmark",
                               country_code="DK", region="Kattegat"),
            ontology_terms=[
                OntologyTerm(uri="http://purl.obolibrary.org/obo/NCBITaxon_562", curie="NCBITaxon:562",
                             source="NCBITaxon", label="Escherichia coli", origin="biosamples"),
                OntologyTerm(uri="http://purl.obolibrary.org/obo/ENVO_00002149", curie="ENVO:00002149",
                             source="ENVO", label="brackish water", origin="ols4",
                             match_type="closeMatch", confidence=0.82,
                             matched_value="brackish water", matched_field="env_medium"),
            ],
            archives=[ArchiveRef(source="ena", accession="ERS0001", url="https://example.org/ERS0001")],
            attributes={"tissue": "whole organism"},
        ),
        Sample(
            accession="SAMEA2", title="Kattegat sediment sample", organism="Escherichia coli",
            collection_date="2021-06-02",
            location=Location(lat=55.9, lon=12.7, precision="coordinates", country="Denmark",
                               country_code="DK", region="Kattegat"),
            ontology_terms=[
                OntologyTerm(uri="http://purl.obolibrary.org/obo/NCBITaxon_562", curie="NCBITaxon:562",
                             source="NCBITaxon", label="Escherichia coli", origin="biosamples"),
                OntologyTerm(uri="http://purl.obolibrary.org/obo/UBERON_0002097", curie="UBERON:0002097",
                             source="UBERON", label="skin epidermis", origin="ols4",
                             match_type="relatedMatch", confidence=0.65,
                             matched_value="skin", matched_field="tissue"),
            ],
            archives=[ArchiveRef(source="ena", accession="ERS0002", url="https://example.org/ERS0002")],
            attributes={"tissue": "skin"},
        ),
        Sample(
            accession="SAMEA3", title="Unlocated soil sample", organism="Pseudomonas fluorescens",
            collection_date="2020-01-01",
            location=Location(precision="none"),
        ),
        Sample(
            accession="SAMEA4", title="German sample, region-only precision",
            organism="Escherichia coli", collection_date="2021-07-01",
            location=Location(precision="country", country="Germany", country_code="DE"),
            archives=[ArchiveRef(source="sra", accession="SRS0004", url="https://example.org/SRS0004")],
        ),
    ]

    nodes, edges = build_graph(demo)
    print(f"nodes={len(nodes)} edges={len(edges)}")

    taxon_ids = {n.id for n in nodes if n.kind == "taxon" and "562" in n.id}
    assert len(taxon_ids) == 1, f"expected one shared E. coli taxon node, got {taxon_ids}"
    ecoli_edges = [e for e in edges if e.kind == "has_taxon" and e.target in taxon_ids]
    assert len(ecoli_edges) == 2, f"expected 2 samples sharing the taxon node, got {len(ecoli_edges)}"
    print("OK: de-duplication -- two samples share one E. coli taxon node")

    country_ids = {n.id for n in nodes if n.kind == "place" and n.props.get("place_type") == "country"
                   and n.props.get("country_code") == "DK"}
    assert len(country_ids) == 1, f"expected one DK place node, got {country_ids}"
    print("OK: de-duplication -- DK country node shared across samples")

    gj = to_geojson(demo)
    assert len(gj["features"]) == 2, f"expected 2 geolocated features, got {len(gj['features'])}"
    lon, lat = gj["features"][0]["geometry"]["coordinates"]
    assert (lon, lat) == (12.5, 55.7), f"lon-first check failed: got {(lon, lat)}"
    print("OK: GeoJSON coordinates are lon-first")

    cy = to_cytoscape(nodes, edges)
    assert set(cy.keys()) == {"nodes", "edges"}
    assert all("data" in n for n in cy["nodes"])
    print("OK: to_cytoscape shape")

    ttl = to_rdf(demo, nodes, edges)
    print(f"Turtle length: {len(ttl)} chars, rdflib available: {_HAS_RDFLIB}")
    try:
        from rdflib import Graph as _G
        parsed = _G()
        parsed.parse(data=ttl, format="turtle")
        print(f"OK: Turtle parses with rdflib ({len(parsed)} triples)")
    except ImportError:
        print("rdflib not installed -- skipping parse check")

    st = stats(demo, nodes, edges)
    print(json.dumps(st, indent=2))

    paths = save_all(demo, nodes, edges, data_dir=config.DATA_DIR / "graph_demo")
    for k, p in paths.items():
        print(f"{k}: {p} ({p.stat().st_size} bytes)")

    print("\nALL CHECKS PASSED", file=sys.stderr)
