"""Aerobiome vocabulary viewer and review tool.

Serves the page, graph.json (from python -m air.build) and the review API
(see air/reviews.py).

Run:  python -m air.app
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory

from air.reviews import ReviewError, ReviewStore, export_csv, export_sssom, predicted_matches

logger = logging.getLogger("air.app")

DATA_DIR = Path(os.environ.get("AIR_DATA_DIR") or os.environ.get("DATA_DIR", "data"))
REVIEWS_FILE = Path(os.environ.get("AIR_REVIEWS_FILE", DATA_DIR / "air" / "reviews.jsonl"))

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
        return predicted_matches(json.loads((DATA_DIR / "air" / "graph.json").read_text()))
    except (OSError, ValueError):
        return {}


def write_snapshot() -> None:
    """air-match-reviews.csv next to the review log, kept current."""
    out = REVIEWS_FILE.with_name("air-match-reviews.csv")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(export_csv(_predicted(), store().state()), encoding="utf-8")
        tmp.replace(out)
    except OSError:
        logger.exception("Could not write %s", out)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/data/<name>.json")
def data(name: str):
    if name != "graph":
        abort(404)
    if not (DATA_DIR / "air" / "graph.json").exists():
        return jsonify({"meta": {}, "nodes": [], "edges": []})
    return send_from_directory(DATA_DIR / "air", "graph.json", max_age=0)


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
                    headers={"Content-Disposition": "attachment; filename=air-match-reviews.csv"})


@app.get("/api/reviews/export.sssom.tsv")
def reviews_sssom():
    return Response(export_sssom(_predicted(), store().state()), mimetype="text/tab-separated-values",
                    headers={"Content-Disposition": "attachment; filename=air-match-reviews.sssom.tsv"})


def prepare(strict: bool = True) -> None:
    """Refresh the review snapshot. graph.json comes from python -m air.build, which needs the network."""
    if not (DATA_DIR / "air" / "graph.json").exists():
        logger.info("No aerobiome graph yet: run python -m air.ena, then python -m air.build")
        return
    try:
        write_snapshot()
    except Exception:
        if strict:
            raise
        logger.exception("Could not write the aerobiome review snapshot")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    prepare()
    port = int(os.environ.get("AIR_PORT", 8096))
    logger.info("Aerobiome viewer on port %d, data in %s, reviews in %s", port, DATA_DIR / "air", REVIEWS_FILE.parent)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
