"""MFD habitat ontology viewer and review tool.

Serves the page, the ontology and graph JSON, and the review API that stores
experts' decisions on the predicted matches (see mfd/reviews.py).

Run:  python -m mfd.app      (builds the JSON on start; the ontology download is cached)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory

from mfd import build
from mfd.reviews import ReviewError, ReviewStore, export_csv, export_sssom, predicted_matches

logger = logging.getLogger("mfd.app")

# MFD_DATA_DIR lets the Natura 2000 viewer host this one with its own data folder;
# the files (and reviews) are always under <data dir>/mfd/.
DATA_DIR = Path(os.environ.get("MFD_DATA_DIR") or os.environ.get("DATA_DIR", "data"))
# The Natura 2000 linker's output; read-only here.
LINKER_DIR = Path(os.environ.get("LINKER_DIR", "data"))
# Reviews live next to the viewer's data unless pointed elsewhere.
REVIEWS_FILE = Path(os.environ.get("MFD_REVIEWS_FILE", DATA_DIR / "mfd" / "reviews.jsonl"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
_store: ReviewStore | None = None


def store() -> ReviewStore:
    global _store
    if _store is None or _store.path != REVIEWS_FILE:
        _store = ReviewStore(REVIEWS_FILE)
    return _store


def _predicted() -> dict:
    try:
        return predicted_matches(json.loads((DATA_DIR / "mfd" / "graph.json").read_text()))
    except (OSError, ValueError):
        return {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/data/<name>.json")
def data(name: str):
    if name not in ("ontology", "graph"):
        abort(404)
    # Rebuilt on every start, so the browser revalidates rather than keeping an old copy.
    return send_from_directory(DATA_DIR / "mfd", f"{name}.json", max_age=0)


@app.get("/api/reviews")
def get_reviews():
    return jsonify({"state": store().state()})


@app.post("/api/reviews")
def post_review():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Expected a JSON object."}), 400
    try:
        event = store().add(body, _predicted())
    except ReviewError as exc:
        return jsonify({"error": str(exc)}), 400
    logger.info("Review: %s marked %s as %s", event["reviewer"], event["key"], event["verdict"])
    return jsonify({"event": event, "state": store().state().get(event["key"])})


@app.get("/api/reviews/export.csv")
def reviews_csv():
    return Response(export_csv(_predicted(), store().state()), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=mfd-match-reviews.csv"})


@app.get("/api/reviews/export.sssom.tsv")
def reviews_sssom():
    return Response(export_sssom(_predicted(), store().state()), mimetype="text/tab-separated-values",
                    headers={"Content-Disposition": "attachment; filename=mfd-match-reviews.sssom.tsv"})


def prepare(strict: bool = True) -> None:
    """Build the ontology and graph JSON. Both steps are cheap, so they run on every
    start: a new ontology pin or a new linker run is picked up by a restart.
    Reviews are never touched. With strict=False a failure (no network for the
    first download, say) is logged instead of stopping the server that hosts us."""
    try:
        build.build(DATA_DIR)
        build.write_graph(DATA_DIR, LINKER_DIR)
    except Exception:
        if strict:
            raise
        logger.exception("Could not build the MFD viewer's data; /mfd/ will show it as missing")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    prepare()
    port = int(os.environ.get("MFD_PORT", 8095))
    logger.info("MFD viewer on port %d, data dir %s, reviews in %s", port, DATA_DIR, REVIEWS_FILE)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
