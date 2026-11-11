"""Cloud samples viewer and review tool (ENA PRJEB54740).

Serves the page, the samples and graph JSON, and the review API that stores
experts' decisions on the predicted ENVO / NCBITaxon terms (cloud/reviews.py).

Run:  python -m cloud.app      (builds the JSON on start; ENA and OLS4 answers are cached)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory

from cloud import build
from cloud.reviews import ReviewError, ReviewStore, export_csv, export_sssom, predicted_matches

logger = logging.getLogger("cloud.app")

# Files are always under <data dir>/cloud/.
DATA_DIR = Path(os.environ.get("CLOUD_DATA_DIR") or os.environ.get("DATA_DIR", "data"))
# The Natura 2000 linker's output and the BioSample graph; read-only here.
LINKER_DIR = Path(os.environ.get("LINKER_DIR", "data"))
REVIEWS_FILE = Path(os.environ.get("CLOUD_REVIEWS_FILE", DATA_DIR / "cloud" / "reviews.jsonl"))
SNAPSHOT_NAME = "cloud-match-reviews.csv"

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
        return predicted_matches(json.loads((DATA_DIR / "cloud" / "graph.json").read_text()))
    except (OSError, ValueError):
        return {}


def write_snapshot() -> Path | None:
    """cloud-match-reviews.csv next to the review log, rewritten after every decision."""
    out = REVIEWS_FILE.with_name(SNAPSHOT_NAME)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(export_csv(_predicted(), store().state()), encoding="utf-8")
        tmp.replace(out)
        return out
    except OSError:
        logger.exception("Could not write %s", out)
        return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/data/<name>.json")
def data(name: str):
    if name not in ("samples", "graph", "air"):
        abort(404)
    return send_from_directory(DATA_DIR / "cloud", f"{name}.json", max_age=0)


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
    write_snapshot()
    return jsonify({"event": event, "state": store().state().get(event["key"])})


@app.get("/api/reviews/export.csv")
def reviews_csv():
    return Response(export_csv(_predicted(), store().state()), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={SNAPSHOT_NAME}"})


@app.get("/api/reviews/export.sssom.tsv")
def reviews_sssom():
    return Response(export_sssom(_predicted(), store().state()), mimetype="text/tab-separated-values",
                    headers={"Content-Disposition": "attachment; filename=cloud-match-reviews.sssom.tsv"})


def prepare(strict: bool = True) -> None:
    """Build samples.json and graph.json; reviews are never touched. With
    strict=False a failure (no network on the first run) is logged instead."""
    try:
        build.build(DATA_DIR, LINKER_DIR)
        write_snapshot()
    except Exception:
        if strict:
            raise
        logger.exception("Could not build the cloud viewer's data; /cloud/ will show it as missing")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    prepare()
    port = int(os.environ.get("CLOUD_PORT", 8096))
    logger.info("Cloud viewer on port %d, data dir %s, reviews in %s", port, DATA_DIR, REVIEWS_FILE.parent)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
