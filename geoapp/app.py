"""Flask web layer for the BioSample geo knowledge-graph explorer.

Serves a single-page app (map + knowledge graph) backed by the JSON artefacts
produced by the ingestion pipeline (``config.SAMPLES_JSON`` / ``config.KG_JSON``).
This module never assumes the pipeline has run: if the data files are missing
it still starts and serves an empty dataset with a "run the pipeline" banner,
and it never raises on a malformed / partial sibling module — those are
imported lazily, inside the handlers that need them, with a graceful fallback.
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request

from geoapp import config, schema


# --- In-memory data store ---------------------------------------------------


class Store:
    """Holds the currently loaded samples + knowledge graph, reloadable at runtime."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.samples: list[dict[str, Any]] = []
        self.graph_nodes: list[dict[str, Any]] = []
        self.graph_edges: list[dict[str, Any]] = []
        self.samples_loaded = False
        self.graph_loaded = False
        self.load_error: str = ""
        self.reload()

    def reload(self) -> dict[str, Any]:
        with self._lock:
            samples: list[dict[str, Any]] = []
            nodes: list[dict[str, Any]] = []
            edges: list[dict[str, Any]] = []
            samples_loaded = False
            graph_loaded = False
            error_parts: list[str] = []

            try:
                if config.SAMPLES_JSON.exists():
                    with open(config.SAMPLES_JSON, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    samples = _coerce_samples(raw)
                    samples_loaded = True
            except Exception as exc:  # noqa: BLE001 - never let a bad file crash the app
                error_parts.append(f"samples: {exc}")

            try:
                if config.KG_JSON.exists():
                    with open(config.KG_JSON, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    nodes, edges = _coerce_graph(raw)
                    graph_loaded = True
            except Exception as exc:  # noqa: BLE001
                error_parts.append(f"graph: {exc}")

            self.samples = samples
            self.graph_nodes = nodes
            self.graph_edges = edges
            self.samples_loaded = samples_loaded
            self.graph_loaded = graph_loaded
            self.load_error = "; ".join(error_parts)

            return {
                "samples_loaded": samples_loaded,
                "graph_loaded": graph_loaded,
                "samples": len(samples),
                "nodes": len(nodes),
                "edges": len(edges),
                "error": self.load_error,
            }


def _coerce_samples(raw: Any) -> list[dict[str, Any]]:
    """Accept either a bare list of sample dicts or {"samples": [...]}."""
    if isinstance(raw, dict):
        raw = raw.get("samples", [])
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            out.append(_normalise_sample(item))
    return out


def _normalise_sample(s: dict[str, Any]) -> dict[str, Any]:
    """Fill in defaults so downstream filter code never has to guard for missing keys."""
    s = dict(s)
    loc = dict(s.get("location") or {})
    loc.setdefault("lat", None)
    loc.setdefault("lon", None)
    loc.setdefault("precision", "none")
    loc.setdefault("country", "")
    loc.setdefault("country_code", "")
    loc.setdefault("region", "")
    if "has_coords" not in loc:
        loc["has_coords"] = loc.get("lat") is not None and loc.get("lon") is not None
    s["location"] = loc
    s.setdefault("ontology_terms", [])
    s.setdefault("archives", [])
    s.setdefault("attributes", {})
    s.setdefault("accession", s.get("accession", ""))
    s.setdefault("title", "")
    s.setdefault("organism", "")
    s.setdefault("collection_date", "")
    s.setdefault("source_url", "")
    return s


def _coerce_graph(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Accept {"nodes": [...], "edges": [...]} with either raw dicts or
    Cytoscape-style {"data": {...}} wrappers; normalise to plain dicts."""
    if not isinstance(raw, dict):
        return [], []
    nodes_raw = raw.get("nodes", [])
    edges_raw = raw.get("edges", [])

    def unwrap(item: Any) -> Optional[dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        if "data" in item and isinstance(item["data"], dict):
            return dict(item["data"])
        return dict(item)

    nodes = [n for n in (unwrap(i) for i in nodes_raw) if n is not None]
    edges = [e for e in (unwrap(i) for i in edges_raw) if e is not None]
    return nodes, edges


# --- Geo helpers with graceful fallback -------------------------------------


def _fallback_haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _fallback_in_bbox(lat, lon, min_lat, min_lon, max_lat, max_lon) -> bool:
    if lat is None or lon is None:
        return False
    lat_ok = min_lat <= lat <= max_lat
    if min_lon <= max_lon:
        lon_ok = min_lon <= lon <= max_lon
    else:
        # bbox crosses the antimeridian
        lon_ok = lon >= min_lon or lon <= max_lon
    return lat_ok and lon_ok


def _fallback_within_radius(lat, lon, center_lat, center_lon, radius_km) -> bool:
    if lat is None or lon is None:
        return False
    return _fallback_haversine_km(lat, lon, center_lat, center_lon) <= radius_km


def _geo_funcs():
    """Lazily import geoapp.geo; fall back to local implementations on failure."""
    try:
        from geoapp import geo as geo_mod  # type: ignore

        in_bbox = getattr(geo_mod, "in_bbox", None)
        within_radius = getattr(geo_mod, "within_radius", None)
        haversine_km = getattr(geo_mod, "haversine_km", None)
        if not callable(in_bbox):
            in_bbox = None
        if not callable(within_radius):
            within_radius = None
        if not callable(haversine_km):
            haversine_km = None
    except Exception:  # noqa: BLE001
        in_bbox = within_radius = haversine_km = None

    return (
        in_bbox or _fallback_in_bbox,
        within_radius or _fallback_within_radius,
        haversine_km or _fallback_haversine_km,
    )


# --- Shared filtering --------------------------------------------------------


def _parse_csv_param(name: str) -> list[str]:
    raw = request.args.get(name, "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_filters() -> dict[str, Any]:
    """Read all shared filter query params off the current Flask request."""
    args = request.args

    bbox_raw = args.get("bbox", "")
    bbox = None
    if bbox_raw:
        try:
            parts = [float(x.strip()) for x in bbox_raw.split(",")]
            if len(parts) == 4:
                bbox = tuple(parts)  # (min_lat, min_lon, max_lat, max_lon)
        except ValueError:
            bbox = None

    center_raw = args.get("center", "")
    center = None
    radius_km = None
    if center_raw:
        try:
            parts = [float(x.strip()) for x in center_raw.split(",")]
            if len(parts) == 2:
                center = tuple(parts)  # (lat, lon)
        except ValueError:
            center = None
    radius_raw = args.get("radius_km", "")
    if radius_raw:
        try:
            radius_km = float(radius_raw)
        except ValueError:
            radius_km = None

    min_confidence_raw = args.get("min_confidence", "")
    try:
        min_confidence = float(min_confidence_raw) if min_confidence_raw != "" else 0.0
    except ValueError:
        min_confidence = 0.0

    has_coords_raw = args.get("has_coords", "")
    has_coords: Optional[bool]
    if has_coords_raw in ("1", "true", "True"):
        has_coords = True
    elif has_coords_raw in ("0", "false", "False"):
        has_coords = False
    else:
        has_coords = None

    return {
        "search": (args.get("search", "") or "").strip().lower(),
        "country": (args.get("country", "") or "").strip(),
        "region": (args.get("region", "") or "").strip(),
        "precision": _parse_csv_param("precision"),
        "ontology": _parse_csv_param("ontology"),
        "archive": _parse_csv_param("archive"),
        "min_confidence": min_confidence,
        "show_archives": args.get("show_archives", "0") in ("1", "true", "yes"),
        "has_coords": has_coords,
        "bbox": bbox,
        "center": center,
        "radius_km": radius_km,
    }


def _sample_matches(sample: dict[str, Any], filters: dict[str, Any], geo_fns) -> bool:
    in_bbox, within_radius, _haversine = geo_fns
    loc = sample.get("location") or {}

    if filters["search"]:
        needle = filters["search"]
        haystack = " ".join(
            str(sample.get(f, "")) for f in ("accession", "title", "organism")
        ).lower()
        if needle not in haystack:
            return False

    if filters["country"] and loc.get("country", "") != filters["country"]:
        return False

    if filters["region"] and loc.get("region", "") != filters["region"]:
        return False

    if filters["precision"] and loc.get("precision", "none") not in filters["precision"]:
        return False

    if filters["has_coords"] is True and not loc.get("has_coords"):
        return False
    if filters["has_coords"] is False and loc.get("has_coords"):
        return False

    ontology_filter = set(filters["ontology"])
    archive_filter = set(filters["archive"])
    min_conf = filters["min_confidence"]

    terms = sample.get("ontology_terms") or []
    if ontology_filter or min_conf > 0:
        matched_any_term = False
        for t in terms:
            src_ok = (not ontology_filter) or (t.get("source", "") in ontology_filter)
            conf_ok = float(t.get("confidence", 1.0) or 0.0) >= min_conf
            if src_ok and conf_ok:
                matched_any_term = True
                break
        if not matched_any_term:
            return False

    if archive_filter:
        archives = sample.get("archives") or []
        if not any(a.get("source", "") in archive_filter for a in archives):
            return False

    if filters["bbox"] is not None:
        min_lat, min_lon, max_lat, max_lon = filters["bbox"]
        if not in_bbox(loc.get("lat"), loc.get("lon"), min_lat, min_lon, max_lat, max_lon):
            return False

    if filters["center"] is not None and filters["radius_km"] is not None:
        clat, clon = filters["center"]
        if not within_radius(loc.get("lat"), loc.get("lon"), clat, clon, filters["radius_km"]):
            return False

    return True


def _filter_samples(store: Store, filters: dict[str, Any]) -> list[dict[str, Any]]:
    geo_fns = _geo_funcs()
    return [s for s in store.samples if _sample_matches(s, filters, geo_fns)]


# Edge kinds whose confidence comes from an ontology match, so the confidence
# slider is meaningful for them. The rest are structural facts about the record.
_SCORED_EDGE_KINDS = {"has_taxon", "in_environment", "has_attribute", "ontology_match"}


def _present_graph(
    store: "Store",
    samples: list[dict[str, Any]],
    filters: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Shape the filtered subgraph for display.

    Applied here rather than in the endpoints so /api/graph and /api/stats can
    never disagree about how big the graph is.

    Two reductions:

    * Archive nodes are dropped unless explicitly requested. Every sample is
      archived in all three archives, so they contribute three universal hubs
      and ~40% of all edges while distinguishing nothing -- they pull the whole
      layout into a single ball. They remain in the detail panel and facets.
    * Edges from a weak ontology match are dropped, along with any non-sample
      node that is left with nothing to attach to. Filtering only whole samples
      made the confidence slider a no-op, because every sample carries at least
      one archive-supplied PURL at confidence 1.0.
    """
    sub = _graph_for_samples(store, samples)
    nodes, edges = sub["nodes"], sub["edges"]

    if not filters.get("show_archives"):
        drop = {n["id"] for n in nodes if n.get("kind") == "archive"}
        if drop:
            nodes = [n for n in nodes if n["id"] not in drop]
            edges = [e for e in edges if e["source"] not in drop and e["target"] not in drop]

    min_conf = float(filters.get("min_confidence") or 0.0)
    if min_conf > 0:
        edges = [
            e for e in edges
            if e.get("kind") not in _SCORED_EDGE_KINDS
            or float(e.get("confidence", 1.0) or 0.0) >= min_conf
        ]
        attached: set[str] = set()
        for e in edges:
            attached.add(e["source"])
            attached.add(e["target"])
        nodes = [n for n in nodes if n.get("kind") == "sample" or n["id"] in attached]
        live = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in live and e["target"] in live]

    return nodes, edges


def _graph_for_samples(
    store: Store,
    samples: list[dict[str, Any]],
    max_depth: Optional[int] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return the subset of the loaded graph reachable from the given samples.

    Reachability is BFS over edges starting from sample-node ids, so linked
    ontology/archive/taxon/place/attribute nodes come along with their sample,
    but nothing else does. Orphan edges (either endpoint missing from the
    resulting node set) are pruned - Cytoscape throws on those.

    ``max_depth`` bounds how many hops the BFS follows past the seed samples.
    Left unbounded (the default, used for the main filtered graph view) a
    single node shared by many samples - the same project, a common taxon,
    a country place node - would otherwise pull in a lot of nodes several
    hops removed once the dataset has any such hub. For a single sample's
    "immediate neighbourhood" (``/api/sample/<accession>``) a small bound
    keeps the result to what's actually adjacent instead of bleeding into
    a hub node's other, unrelated sibling nodes (e.g. every other region in
    the same country).
    """
    accessions = {s.get("accession", "") for s in samples if s.get("accession")}
    if not accessions:
        return {"nodes": [], "edges": []}

    nodes_by_id = {n.get("id"): n for n in store.graph_nodes if n.get("id") is not None}

    adjacency: dict[str, list[str]] = {}
    for e in store.graph_edges:
        src, tgt = e.get("source"), e.get("target")
        if src is None or tgt is None:
            continue
        adjacency.setdefault(src, []).append(tgt)
        adjacency.setdefault(tgt, []).append(src)

    # Seed with sample node ids that are present in the node table AND match
    # the filtered accessions. Sample node ids are expected to equal accession,
    # but fall back to matching by kind=="sample" + props.accession just in case.
    seed_ids = set()
    for n in store.graph_nodes:
        nid = n.get("id")
        if nid in accessions:
            seed_ids.add(nid)
            continue
        if n.get("kind") == "sample":
            props = n.get("props") or {}
            if props.get("accession") in accessions:
                seed_ids.add(nid)

    # BFS out from the seed samples, but never cross INTO another sample's
    # subgraph. Without this guard, a single shared hub node (the same
    # project, taxon, archive, ... referenced by many samples) would let the
    # traversal spill across the whole dataset the moment any two samples
    # share so much as one attribute, silently defeating the location filter.
    # A hub node itself is still included (it *is* directly linked to one of
    # our seed samples) - we just refuse to hop from it onward into a sample
    # that isn't part of the current filtered set.
    visited = set(seed_ids)
    frontier = list(seed_ids)
    depth = 0
    while frontier:
        if max_depth is not None and depth >= max_depth:
            break
        nxt = []
        for cur in frontier:
            for neighbour in adjacency.get(cur, []):
                if neighbour in visited or neighbour not in nodes_by_id:
                    continue
                neighbour_node = nodes_by_id[neighbour]
                if neighbour_node.get("kind") == "sample" and neighbour not in seed_ids:
                    continue
                visited.add(neighbour)
                nxt.append(neighbour)
        frontier = nxt
        depth += 1

    result_nodes = [nodes_by_id[nid] for nid in visited if nid in nodes_by_id]
    result_node_ids = {n.get("id") for n in result_nodes}

    result_edges = [
        e
        for e in store.graph_edges
        if e.get("source") in result_node_ids and e.get("target") in result_node_ids
    ]

    return {"nodes": result_nodes, "edges": result_edges}


# --- App factory -------------------------------------------------------------


def create_app() -> Flask:
    app = Flask(__name__)
    store = Store()
    app.extensions = getattr(app, "extensions", {})
    app.extensions["geo_store"] = store

    # -- pages --

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            samples_loaded=store.samples_loaded,
            graph_loaded=store.graph_loaded,
            load_error=store.load_error,
        )

    # -- api --

    @app.get("/api/samples")
    def api_samples():
        filters = _parse_filters()
        filtered = _filter_samples(store, filters)
        return jsonify(
            {
                "samples": filtered,
                "total": len(store.samples),
                "filtered": len(filtered),
            }
        )

    @app.get("/api/sample/<accession>")
    def api_sample_detail(accession: str):
        sample = next((s for s in store.samples if s.get("accession") == accession), None)
        if sample is None:
            return jsonify({"error": "not found", "accession": accession}), 404

        neighbourhood = _graph_for_samples(store, [sample], max_depth=2)
        return jsonify({"sample": sample, "graph": neighbourhood})

    @app.get("/api/geojson")
    def api_geojson():
        filters = _parse_filters()
        filtered = _filter_samples(store, filters)
        features = []
        for s in filtered:
            loc = s.get("location") or {}
            if not loc.get("has_coords"):
                continue
            lat, lon = loc.get("lat"), loc.get("lon")
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},  # GeoJSON: lon, lat
                    "properties": {
                        "accession": s.get("accession", ""),
                        "title": s.get("title", ""),
                        "organism": s.get("organism", ""),
                        "collection_date": s.get("collection_date", ""),
                        "precision": loc.get("precision", "none"),
                        "country": loc.get("country", ""),
                        "region": loc.get("region", ""),
                    },
                }
            )
        return jsonify({"type": "FeatureCollection", "features": features})

    @app.get("/api/graph")
    def api_graph():
        filters = _parse_filters()
        filtered = _filter_samples(store, filters)
        nodes, edges = _present_graph(store, filtered, filters)
        return jsonify({
            "nodes": [{"data": n} for n in nodes],
            "edges": [{"data": e} for e in edges],
        })

    @app.get("/api/stats")
    def api_stats():
        filters = _parse_filters()
        filtered = _filter_samples(store, filters)

        with_coords = sum(1 for s in filtered if (s.get("location") or {}).get("has_coords"))
        countries = {
            (s.get("location") or {}).get("country", "")
            for s in filtered
            if (s.get("location") or {}).get("country")
        }
        ontology_terms = set()
        for s in filtered:
            for t in s.get("ontology_terms") or []:
                key = t.get("curie") or t.get("uri")
                if key:
                    ontology_terms.add(key)

        nodes, edges = _present_graph(store, filtered, filters)

        return jsonify(
            {
                "samples": len(filtered),
                "samples_total": len(store.samples),
                "with_coords": with_coords,
                "countries": len(countries),
                "ontology_terms": len(ontology_terms),
                "graph_nodes": len(nodes),
                "graph_edges": len(edges),
                "samples_loaded": store.samples_loaded,
                "graph_loaded": store.graph_loaded,
            }
        )

    @app.get("/api/facets")
    def api_facets():
        countries = sorted({(s.get("location") or {}).get("country", "") for s in store.samples} - {""})
        regions = sorted({(s.get("location") or {}).get("region", "") for s in store.samples} - {""})
        precisions = sorted({(s.get("location") or {}).get("precision", "none") for s in store.samples})
        ontologies = sorted(
            {
                t.get("source", "")
                for s in store.samples
                for t in (s.get("ontology_terms") or [])
            }
            - {""}
        )
        archives = sorted(
            {
                a.get("source", "")
                for s in store.samples
                for a in (s.get("archives") or [])
            }
            - {""}
        )
        return jsonify(
            {
                "countries": countries,
                "regions": regions,
                "precisions": precisions,
                "ontologies": ontologies,
                "archives": archives,
            }
        )

    @app.get("/api/legend")
    def api_legend():
        return jsonify(
            {
                "node_kinds": list(schema.NODE_KINDS),
                "edge_kinds": list(schema.EDGE_KINDS),
                "ontology_sources": schema.ONTOLOGY_SOURCES,
                "archive_sources": schema.ARCHIVE_SOURCES,
            }
        )

    @app.post("/api/reload")
    def api_reload():
        result = store.reload()
        return jsonify(result)

    return app


def main() -> None:
    app = create_app()
    # load_dotenv=False: this repo lives on OneDrive with Files On-Demand, and
    # Flask's automatic .env/.flaskenv discovery can hang for tens of seconds
    # trying to read a dataless placeholder file elsewhere in the tree.
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, load_dotenv=False)


if __name__ == "__main__":
    main()
