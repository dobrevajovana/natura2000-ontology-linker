"""
Natura 2000 Ontology Linker — Visualization Server

A Flask app that serves the interactive graph visualization.
Reads pipeline output from DATA_DIR.
"""

import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

# `python viz/app.py` puts viz/ on the path, not the repo root, and the
# ontology manager reuses geoapp's calibrated OLS4 matcher.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("natura2000.viz")

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))

# Ontologies added from the viewer live in their own file, so a pipeline run
# never overwrites them and removing one never touches the pipeline's output.
ADDED_FILE = DATA_DIR / "mappings_added.json"

# Module-level cache, re-read whenever one of its files changes on disk:
# "Reload Data" and a fresh pipeline run then show up without a restart.
_terms: list[dict] | None = None
_mappings: list[dict] | None = None
_loaded_stamp: tuple | None = None
_data_lock = threading.Lock()


def _file_stamp() -> tuple:
    stamp = []
    for fname in ("terms.json", "mappings.json", "direct_mappings.json", ADDED_FILE.name):
        p = DATA_DIR / fname
        stamp.append(p.stat().st_mtime_ns if p.exists() else 0)
    return tuple(stamp)


def _read_added() -> dict:
    if not ADDED_FILE.exists():
        return {"ontologies": {}, "mappings": []}
    try:
        with ADDED_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {"ontologies": data.get("ontologies", {}), "mappings": data.get("mappings", [])}
    except Exception:
        logger.exception("Could not read %s; ignoring added ontologies", ADDED_FILE)
        return {"ontologies": {}, "mappings": []}


def _load_data() -> tuple[list[dict], list[dict]]:
    global _terms, _mappings, _loaded_stamp
    with _data_lock:
        stamp = _file_stamp()
        if stamp == _loaded_stamp and _terms is not None:
            return _terms, _mappings

        terms_path = DATA_DIR / "terms.json"
        if terms_path.exists():
            with terms_path.open("r", encoding="utf-8") as fh:
                _terms = json.load(fh)
            logger.info("Loaded %d terms", len(_terms))
        else:
            _terms = []

        # Prefer unified mappings, fall back to direct
        _mappings = []
        for fname in ("mappings.json", "direct_mappings.json"):
            p = DATA_DIR / fname
            if p.exists():
                with p.open("r", encoding="utf-8") as fh:
                    _mappings = json.load(fh)
                logger.info("Loaded %d mappings from %s", len(_mappings), fname)
                break
        added = _read_added()["mappings"]
        if added:
            _mappings = _mappings + added
            logger.info("Added %d mappings from %s", len(added), ADDED_FILE.name)

        _loaded_stamp = stamp
        return _terms, _mappings


def _float_arg(name: str, default: float) -> float:
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _display_ontology(m: dict) -> str:
    """Viewer category for a mapping's target: wikidata, dbpedia, gbif, eunis, ols4 (ENVO/NCBITaxon),
    or the id of an ontology added from the viewer."""
    if m.get("added_ontology"):
        return m["added_ontology"]
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
        min_confidence = _float_arg("min_confidence", 0.0)
        search_query = request.args.get("search", "").lower().strip()
        limit = max(1, min(_int_arg("limit", 500), 2000))

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

        terms, mappings = _load_data()
        category_filter = request.args.get("category", "")
        search_query = request.args.get("search", "").lower().strip()
        min_confidence = _float_arg("min_confidence", 0.0)

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

        # Link counts per term and target, over every mapping -- not only the
        # ones the graph's size limit happens to draw.
        counts: dict[str, dict[str, int]] = {}
        for m in mappings:
            if m.get("confidence", 0.0) >= min_confidence:
                per = counts.setdefault(m["source_id"], {})
                k = _display_ontology(m)
                per[k] = per.get(k, 0) + 1
        result = [{**t, "link_counts": counts.get(t["id"], {})} for t in result]

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


# ---------------------------------------------------------------------------
# Ontology manager: link the Natura 2000 terms to another OLS4 ontology
# ---------------------------------------------------------------------------

OLS4_API = "https://www.ebi.ac.uk/ols4/api"
_ONTOLOGY_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
LINKABLE_CATEGORIES = ("habitat", "species")
# Only the best hit per term is kept, and only when the ontology's own names back
# it (confidence 0.70 and up in geoapp.ontolink._CONFIDENCE). Spelling similarity
# (0.35) and head-noun guesses (0.15-0.30) are right a third of the time or less:
# habitats against UBERON gave "formations" -> "pontine reticular formation".
MIN_ADDED_CONFIDENCE = 0.70
# The lowest a person may choose, to see the guesses too ("Active raised bogs" ->
# GOLD "Bog" is a head-noun broadMatch at 0.30); those are flagged for review.
GUESS_CONFIDENCE = 0.30

# requests' timeouts do not cover every way a call can stall (a hung name lookup,
# for one), and a single request that never returns would hold the job, and the
# Link button, forever. If no term finishes for this long, the rest are skipped.
STALL_SECONDS = 180

_job: dict = {"state": "idle"}
_job_lock = threading.Lock()


def _term_text(term: dict) -> str:
    """What to search for: a habitat's name without its Annex I code, a species' scientific name."""
    if term.get("category") == "species":
        return (term.get("scientific_name") or term.get("label") or "").strip()
    return re.sub(r"^\d{4}\s*\*?\s*", "", term.get("label") or "").strip()


def _ontology_title(ontology: str) -> str | None:
    """The ontology's title if OLS4 knows it, else None."""
    import requests
    try:
        resp = requests.get(f"{OLS4_API}/ontologies/{ontology}", timeout=30,
                            headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return None
        cfg = resp.json().get("config") or {}
        return cfg.get("title") or ontology.upper()
    except Exception:
        logger.exception("OLS4 lookup of ontology %s failed", ontology)
        return None


def _write_added(data: dict) -> None:
    tmp = ADDED_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ADDED_FILE)


def _link_ontology(ontology: str, title: str, categories: list[str], min_conf: float) -> None:
    """Background job: best OLS4 hit per term, classified by geoapp's calibrated rules."""
    try:
        _run_link_job(ontology, title, categories, min_conf)
    except Exception as exc:
        logger.exception("Linking to %s failed", ontology)
        with _job_lock:
            _job.update(state="error", error=str(exc))


def _run_link_job(ontology: str, title: str, categories: list[str], min_conf: float) -> None:
    import requests
    from geoapp import ontolink

    terms, _ = _load_data()
    todo = [t for t in terms if t.get("category") in categories and len(_term_text(t)) >= 3]
    with _job_lock:
        _job.update(total=len(todo), done=0, linked=0)
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    def link(term: dict) -> dict | None:
        hits = ontolink.ols4_search(_term_text(term), ontology, session)
        best = hits[0] if hits else None
        with _job_lock:
            _job["done"] += 1
        if best is None or best.confidence < min_conf:
            return None
        with _job_lock:
            _job["linked"] += 1
        return {
            "source_id": term["id"], "source_label": term.get("label", ""),
            "target_uri": best.uri, "target_label": best.label,
            "match_type": f"skos:{best.match_type}", "confidence": best.confidence,
            "source_ontology": f"ols4_{ontology}", "method": "ols4_search",
            "added_ontology": ontology,
            **({"review": True} if best.confidence < 0.85 else {}),
        }

    found: list[dict] = []
    pool = ThreadPoolExecutor(max_workers=4)
    futures = {pool.submit(link, t): t for t in todo}
    skipped = 0
    skipped_terms: list[str] = []
    try:
        for fut in as_completed(futures, timeout=STALL_SECONDS):
            m = fut.result()
            if m:
                found.append(m)
    except FuturesTimeout:
        skipped_terms = [_term_text(t) for f, t in futures.items() if not f.done()]
        skipped = len(skipped_terms)
        logger.warning("Linking to %s stalled; skipping %d term(s) that did not answer: %s",
                       ontology, skipped, skipped_terms)
    finally:
        # never wait on a hung request; its thread is abandoned
        pool.shutdown(wait=False, cancel_futures=True)
    with _data_lock:
        data = _read_added()
        data["mappings"] = [m for m in data["mappings"] if m.get("added_ontology") != ontology] + found
        data["ontologies"][ontology] = {
            "title": title, "categories": categories, "count": len(found), "min_confidence": min_conf,
            "terms_searched": len(todo) - skipped, "skipped": skipped, "skipped_terms": skipped_terms,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _write_added(data)
    with _job_lock:
        _job.update(state="done", finished=time.time(), skipped=skipped, skipped_terms=skipped_terms)
    logger.info("Linked %d of %d terms to %s", len(found), len(todo), ontology)


# OLS4's catalogue (about 290 ontologies) for the suggestions under the ontology
# box; fetched once and kept for a day, it is small enough to filter in the page.
CATALOG_TTL = 24 * 3600
_catalog: dict = {"at": 0.0, "items": []}
_catalog_lock = threading.Lock()


def _ols4_catalog() -> list[dict]:
    import requests
    with _catalog_lock:
        if _catalog["items"] and time.time() - _catalog["at"] < CATALOG_TTL:
            return _catalog["items"]
        resp = requests.get(f"{OLS4_API}/ontologies", params={"size": 1000}, timeout=60,
                            headers={"Accept": "application/json"})
        resp.raise_for_status()
        items = []
        for o in (resp.json().get("_embedded") or {}).get("ontologies", []):
            cfg = o.get("config") or {}
            oid = o.get("ontologyId") or cfg.get("id")
            if not oid or not _ONTOLOGY_ID.match(oid):
                continue
            desc = " ".join((cfg.get("description") or "").split())
            items.append({"id": oid, "title": cfg.get("title") or oid.upper(),
                          "terms": o.get("numberOfTerms") or 0,
                          "description": desc[:160] + ("…" if len(desc) > 160 else "")})
        items.sort(key=lambda i: i["id"])
        _catalog.update(at=time.time(), items=items)
        return items


@app.route("/api/ontologies/catalog")
def api_ontology_catalog():
    try:
        return _add_cors(jsonify(_ols4_catalog()))
    except Exception as exc:
        logger.exception("OLS4 catalogue unavailable")
        return _add_cors(jsonify({"error": f"OLS4 catalogue unavailable: {exc}"})), 502


@app.route("/api/ontologies", methods=["GET"])
def api_ontologies():
    data = _read_added()
    with _job_lock:
        job = dict(_job)
    added = [{"id": k, **v} for k, v in sorted(data["ontologies"].items())]
    return _add_cors(jsonify({"added": added, "job": job}))


@app.route("/api/ontologies", methods=["POST"])
def api_add_ontology():
    body = request.get_json(silent=True) or {}
    ontology = str(body.get("id", "")).strip().lower()
    categories = [c for c in body.get("categories", ["habitat"]) if c in LINKABLE_CATEGORIES]
    try:
        min_conf = min(1.0, max(GUESS_CONFIDENCE, float(body.get("min_confidence", MIN_ADDED_CONFIDENCE))))
    except (TypeError, ValueError):
        min_conf = MIN_ADDED_CONFIDENCE
    if not _ONTOLOGY_ID.match(ontology):
        return _add_cors(jsonify({"error": "Use the ontology's OLS4 id, such as uberon or gold."})), 400
    if not categories:
        return _add_cors(jsonify({"error": "Pick habitats, species or both."})), 400
    if _data_missing():
        return _add_cors(jsonify({"error": "Run the pipeline first: there are no Natura 2000 terms to link."})), 409
    with _job_lock:
        if _job.get("state") == "running":
            return _add_cors(jsonify({"error": f"Already linking {_job.get('ontology')}. Wait for it to finish."})), 409
    title = _ontology_title(ontology)
    if title is None:
        return _add_cors(jsonify({"error": f"OLS4 has no ontology called '{ontology}'."})), 404
    with _job_lock:
        _job.clear()
        _job.update(state="running", ontology=ontology, title=title, categories=categories, min_confidence=min_conf,
                    total=0, done=0, linked=0, started=time.time())
    threading.Thread(target=_link_ontology, args=(ontology, title, categories, min_conf), daemon=True).start()
    return _add_cors(jsonify({"job": dict(_job)})), 202


@app.route("/api/ontologies/<ontology>", methods=["DELETE"])
def api_remove_ontology(ontology: str):
    with _job_lock:
        if _job.get("state") == "running" and _job.get("ontology") == ontology:
            return _add_cors(jsonify({"error": "It is still being linked."})), 409
    with _data_lock:
        data = _read_added()
        if ontology not in data["ontologies"]:
            return _add_cors(jsonify({"error": f"'{ontology}' was not added."})), 404
        data["ontologies"].pop(ontology)
        data["mappings"] = [m for m in data["mappings"] if m.get("added_ontology") != ontology]
        _write_added(data)
    return _add_cors(jsonify({"removed": ontology}))


if __name__ == "__main__":
    port = int(os.environ.get("VIZ_PORT", 8080))
    logger.info("Starting visualization server on port %d, data dir: %s", port, DATA_DIR)
    app.run(host="0.0.0.0", port=port, debug=False)
