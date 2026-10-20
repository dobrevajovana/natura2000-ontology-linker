"""
Natura 2000 Ontology Linker — Visualization Server

A Flask app that serves the interactive graph visualization.
Reads pipeline output from DATA_DIR.
"""

import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("natura2000.viz")

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))

# Module-level cache
_terms: list[dict] | None = None
_mappings: list[dict] | None = None


def _load_data() -> tuple[list[dict], list[dict]]:
    global _terms, _mappings
    if _terms is None:
        terms_path = DATA_DIR / "terms.json"
        if terms_path.exists():
            with terms_path.open("r", encoding="utf-8") as fh:
                _terms = json.load(fh)
            logger.info("Loaded %d terms", len(_terms))
        else:
            _terms = []

    if _mappings is None:
        # Prefer unified mappings, fall back to direct
        for fname in ("mappings.json", "direct_mappings.json"):
            p = DATA_DIR / fname
            if p.exists():
                with p.open("r", encoding="utf-8") as fh:
                    _mappings = json.load(fh)
                logger.info("Loaded %d mappings from %s", len(_mappings), fname)
                break
        if _mappings is None:
            _mappings = []

    return _terms, _mappings


def _display_ontology(m: dict) -> str:
    """Viewer category for a mapping's target: wikidata, dbpedia, gbif, eunis or ols4 (ENVO/NCBITaxon)."""
    ont = m.get("source_ontology", "")
    for k in ("wikidata", "dbpedia", "gbif", "eunis"):
        if k in ont:
            return k
    return "ols4"


def _data_missing() -> bool:
    return not (DATA_DIR / "terms.json").exists()


def _uri_to_node_id(uri: str) -> str:
    return "ext_" + re.sub(r"[^a-zA-Z0-9]", "_", uri)


def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/graph")
def api_graph():
    try:
        if _data_missing():
            return _add_cors(jsonify({
                "error": "Pipeline has not been run yet. Please run the pipeline first.",
                "data_missing": True,
            })), 503

        terms, mappings = _load_data()

        # Query parameters
        category_filter = request.args.get("category", "")
        ontology_filter = request.args.get("ontology", "")
        min_confidence = float(request.args.get("min_confidence", "0.0"))
        search_query = request.args.get("search", "").lower().strip()
        limit = min(int(request.args.get("limit", "500")), 2000)

        allowed_categories = set(category_filter.split(",")) if category_filter else None
        allowed_ontologies = set(ontology_filter.split(",")) if ontology_filter else None

        # Filter terms
        filtered_terms = terms
        if allowed_categories:
            filtered_terms = [t for t in filtered_terms if t.get("category") in allowed_categories]
        if search_query:
            filtered_terms = [
                t for t in filtered_terms
                if search_query in t.get("label", "").lower()
                or search_query in (t.get("scientific_name") or "").lower()
            ]

        # Apply limit — prioritise habitats/regions/statuses over species
        if len(filtered_terms) > limit:
            priority_order = {"habitat": 0, "region": 1, "status": 2, "species": 3}
            filtered_terms = sorted(filtered_terms, key=lambda t: priority_order.get(t.get("category", ""), 9))
            filtered_terms = filtered_terms[:limit]

        included_ids = {t["id"] for t in filtered_terms}

        # Filter mappings
        filtered_mappings = [
            m for m in mappings
            if m["source_id"] in included_ids
            and m.get("confidence", 0.0) >= min_confidence
            and (allowed_ontologies is None
                 or _display_ontology(m) in allowed_ontologies)
        ]

        # Build Cytoscape nodes
        nodes = []
        for t in filtered_terms:
            nodes.append({
                "data": {
                    "id": f"n2k_{t['id']}",
                    "label": t.get("label", t["id"]),
                    "category": t.get("category", ""),
                    "definition": t.get("definition", ""),
                    "codes": t.get("codes", []),
                    "scientific_name": t.get("scientific_name"),
                    "source_url": t.get("source_url", ""),
                    "node_type": "natura2000",
                }
            })

        # Collect external nodes referenced by edges
        ext_nodes: dict[str, dict] = {}
        for m in filtered_mappings:
            uri = m["target_uri"]
            node_id = _uri_to_node_id(uri)
            if node_id not in ext_nodes:
                ont_display = _display_ontology(m)
                ext_nodes[node_id] = {
                    "data": {
                        "id": node_id,
                        "label": m.get("target_label", uri.split("/")[-1]),
                        "uri": uri,
                        "source_ontology": ont_display,
                        "node_type": "external",
                    }
                }
        nodes.extend(ext_nodes.values())

        # Build edges
        edges = []
        for idx, m in enumerate(filtered_mappings):
            source_node = f"n2k_{m['source_id']}"
            target_node = _uri_to_node_id(m["target_uri"])
            ont_display = _display_ontology(m)
            edges.append({
                "data": {
                    "id": f"edge_{idx}",
                    "source": source_node,
                    "target": target_node,
                    "match_type": m.get("match_type", "skos:closeMatch"),
                    "confidence": m.get("confidence", 0.5),
                    "source_ontology": ont_display,
                }
            })

        return _add_cors(jsonify({"nodes": nodes, "edges": edges}))

    except Exception as exc:
        logger.exception("Error in /api/graph")
        return _add_cors(jsonify({"error": str(exc)})), 500


@app.route("/api/term/<path:term_id>")
def api_term(term_id: str):
    try:
        if _data_missing():
            return _add_cors(jsonify({"error": "Pipeline not run", "data_missing": True})), 503

        terms, mappings = _load_data()
        term = next((t for t in terms if t["id"] == term_id), None)
        if term is None:
            return _add_cors(jsonify({"error": f"Term '{term_id}' not found"})), 404

        term_mappings = [m for m in mappings if m["source_id"] == term_id]
        return _add_cors(jsonify({"term": term, "mappings": term_mappings}))

    except Exception as exc:
        logger.exception("Error in /api/term")
        return _add_cors(jsonify({"error": str(exc)})), 500


@app.route("/api/stats")
def api_stats():
    try:
        pipeline_ran = (DATA_DIR / "terms.json").exists()
        if not pipeline_ran:
            return _add_cors(jsonify({
                "pipeline_ran": False,
                "total_terms": 0,
                "terms_by_category": {},
                "total_mappings": 0,
                "mappings_by_ontology": {},
                "mappings_by_type": {},
                "avg_confidence": 0.0,
            }))

        terms, mappings = _load_data()

        terms_by_cat: dict[str, int] = {}
        for t in terms:
            cat = t.get("category", "unknown")
            terms_by_cat[cat] = terms_by_cat.get(cat, 0) + 1

        mappings_by_ont: dict[str, int] = {}
        mappings_by_type: dict[str, int] = {}
        total_confidence = 0.0

        for m in mappings:
            k = _display_ontology(m)
            mappings_by_ont[k] = mappings_by_ont.get(k, 0) + 1

            mt = m.get("match_type", "skos:closeMatch")
            mappings_by_type[mt] = mappings_by_type.get(mt, 0) + 1
            total_confidence += m.get("confidence", 0.0)

        avg_conf = round(total_confidence / len(mappings), 4) if mappings else 0.0

        return _add_cors(jsonify({
            "pipeline_ran": True,
            "total_terms": len(terms),
            "terms_by_category": terms_by_cat,
            "total_mappings": len(mappings),
            "mappings_by_ontology": mappings_by_ont,
            "mappings_by_type": mappings_by_type,
            "avg_confidence": avg_conf,
        }))

    except Exception as exc:
        logger.exception("Error in /api/stats")
        return _add_cors(jsonify({"error": str(exc)})), 500


@app.route("/api/terms")
def api_terms():
    try:
        if _data_missing():
            return _add_cors(jsonify({"error": "Pipeline not run", "data_missing": True})), 503

        terms, _ = _load_data()
        category_filter = request.args.get("category", "")
        search_query = request.args.get("search", "").lower().strip()

        result = terms
        if category_filter:
            allowed = set(category_filter.split(","))
            result = [t for t in result if t.get("category") in allowed]
        if search_query:
            result = [
                t for t in result
                if search_query in t.get("label", "").lower()
                or search_query in (t.get("scientific_name") or "").lower()
            ]

        return _add_cors(jsonify(result))

    except Exception as exc:
        logger.exception("Error in /api/terms")
        return _add_cors(jsonify({"error": str(exc)})), 500


@app.route("/api/geo/samples")
def api_geo_samples():
    """Environmental samples with coordinates, written by the geoapp (geo_samples.geojson)."""
    path = DATA_DIR / "geo_samples.geojson"
    if not path.exists():
        return _add_cors(jsonify({"type": "FeatureCollection", "features": []}))
    with path.open("r", encoding="utf-8") as fh:
        return _add_cors(jsonify(json.load(fh)))


if __name__ == "__main__":
    port = int(os.environ.get("VIZ_PORT", 8080))
    logger.info("Starting visualization server on port %d, data dir: %s", port, DATA_DIR)
    app.run(host="0.0.0.0", port=port, debug=False)
