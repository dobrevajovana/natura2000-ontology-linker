"""The review tool: any mapping file becomes a page where experts accept or reject each link.

    from verdant_validator.web import create_app, serve
    serve("data_pangaea/mappings.json")                      # one file, http://localhost:8097
    app = create_app(folder="incoming", reviews_dir="reviews") # every file in a folder, plus uploads

Each dataset is a file (anything ``validation.load`` reads). Its automatic checks run
when it is first opened (the online ones in the background), and its decisions go to
``<reviews_dir>/<dataset>.jsonl`` with a CSV of the current state beside it
(``<dataset>-reviews.csv``), rewritten after every decision.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request

from verdant_validator.checks import check
from verdant_validator.export import export_csv, export_sssom
from verdant_validator.mappings import load
from verdant_validator.store import ReviewError, ReviewStore

logger = logging.getLogger("verdant_validator.web")
EXTENSIONS = {".json", ".tsv", ".csv", ".txt", ".xlsx"}
MAX_UPLOAD = 25 * 1024 * 1024


def dataset_name(path: Path) -> str:
    stem = path.name
    for ext in (".sssom.tsv", *EXTENSIONS):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    if stem in ("mappings", "graph"):          # name a linker run or a viewer graph by its folder
        stem = f"{path.parent.name}-{stem}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-") or "dataset"


class Dataset:
    def __init__(self, name: str, path: Path | None, reviews_dir: Path, online: bool, records=None):
        self.name, self.path, self.online, self.records = name, path, online, records
        self.store = ReviewStore(reviews_dir / f"{name}.jsonl")
        self.snapshot = reviews_dir / f"{name}-reviews.csv"
        self.maps: list[dict] = []
        self.summary: dict = {}
        self.error: str | None = None
        self.checking = False
        self._mtime = None
        self._lock = threading.Lock()

    def ensure(self) -> "Dataset":
        """Load (again, if the file changed) and run the checks."""
        mtime = self.path.stat().st_mtime if self.path else 0
        with self._lock:
            if self._mtime == mtime and (self.maps or self.error):
                return self
            self._mtime = mtime
            try:
                self.maps = load(self.records if self.records is not None else self.path)
                self.summary = check(self.maps, online=False)
                self.error = None
            except Exception as exc:          # a file we cannot read is shown as such, not a crash
                logger.exception("Could not read %s", self.path)
                self.maps, self.summary, self.error = [], {}, f"Could not read this file: {exc}"
                return self
        if self.online and self.maps:
            self.checking = True
            threading.Thread(target=self._online, daemon=True, name=f"check-{self.name}").start()
        return self

    def _online(self):
        try:
            summary = check(self.maps, online=True)
            self.summary = summary
        except Exception:
            logger.exception("Online checks failed for %s", self.name)
        finally:
            self.checking = False

    @property
    def by_key(self) -> dict[str, dict]:
        return {m["key"]: m for m in self.maps}

    def write_snapshot(self):
        try:
            self.snapshot.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.snapshot.with_name(self.snapshot.name + ".tmp")
            tmp.write_text(export_csv(self.maps, self.store.state()), encoding="utf-8")
            tmp.replace(self.snapshot)
        except OSError:
            logger.exception("Could not write %s", self.snapshot)


def create_app(source=None, *, folder=None, reviews_dir="reviews", online: bool = True,
               title: str = "Mapping validation", uploads: bool = True, name: str | None = None) -> Flask:
    """A review app for ``source`` (a file or a list of mapping dicts), every file in
    ``folder`` (new files appear without a restart), or both."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
    reviews_dir = Path(reviews_dir)
    folder = Path(folder) if folder else None
    fixed: dict[str, Dataset] = {}
    if source is not None:
        if isinstance(source, list):
            n = name or "dataset"
            fixed[n] = Dataset(n, None, reviews_dir, online, records=source)
        else:
            p = Path(source)
            n = name or dataset_name(p)
            fixed[n] = Dataset(n, p, reviews_dir, online)
    found: dict[str, Dataset] = {}

    def datasets() -> dict[str, Dataset]:
        out = dict(fixed)
        if folder and folder.exists():
            # Files at the top of the folder, and a linker run's mappings.json one level down;
            # not deeper (a run folder's caches are not datasets).
            candidates = [p for p in folder.glob("*") if p.is_file()] + list(folder.glob("*/mappings.json"))
            for p in sorted(candidates):
                if not p.name.startswith(".") and p.suffix.lower() in EXTENSIONS:
                    if p.name == "terms.json":
                        continue          # read beside a linker's mappings.json, not a dataset of its own
                    name = dataset_name(p)
                    if name not in found or found[name].path != p:
                        found[name] = Dataset(name, p, reviews_dir, online)
                    out.setdefault(name, found[name])
        return out

    def get(name: str) -> Dataset:
        ds = datasets().get(name)
        if not ds:
            abort(404)
        return ds.ensure()

    @app.route("/")
    def index():
        return render_template("validation_index.html", title=title, uploads=uploads and bool(folder),
                               single=next(iter(fixed)) if len(fixed) == 1 and not folder else None)

    @app.get("/api/datasets")
    def api_datasets():
        rows = []
        for name, ds in datasets().items():
            ds.ensure()
            if not ds.maps and not ds.error and ds.path is not None:
                continue          # a JSON file with no mappings in it (a cache, a settings file)
            state = ds.store.state()
            rows.append({"name": name, "file": ds.path.name if ds.path else None, "mappings": len(ds.maps),
                         "reviewed": sum(1 for m in ds.maps if m["key"] in state), "error": ds.error,
                         "with_errors": ds.summary.get("with_errors", 0), "with_warnings": ds.summary.get("with_warnings", 0)})
        return jsonify(rows)

    @app.post("/upload")
    def upload():
        if not (uploads and folder):
            abort(404)
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "Choose a file."}), 400
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(f.filename).name)
        if not any(name.lower().endswith(e) for e in EXTENSIONS):
            return jsonify({"error": f"Use one of: {', '.join(sorted(EXTENSIONS))}."}), 400
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / name
        f.save(dest)
        ds = Dataset(dataset_name(dest), dest, reviews_dir, online).ensure()
        if ds.error or not ds.maps:
            dest.unlink(missing_ok=True)
            return jsonify({"error": ds.error or "No mappings found: the file needs a subject and an object column "
                                                 "(for example subject_id and object_id, or source_id and target_uri)."}), 400
        return jsonify({"name": ds.name, "mappings": len(ds.maps)})

    @app.route("/d/<name>/")
    def page(name):
        get(name)
        return render_template("validation_review.html", title=title, name=name)

    @app.get("/d/<name>/api/mappings")
    def api_mappings(name):
        ds = get(name)
        return jsonify({"name": name, "file": ds.path.name if ds.path else None, "error": ds.error,
                        "checking": ds.checking, "summary": ds.summary, "mappings": ds.maps})

    @app.get("/d/<name>/api/reviews")
    def api_reviews(name):
        return jsonify({"state": get(name).store.state()})

    @app.post("/d/<name>/api/reviews")
    def api_review(name):
        ds = get(name)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Expected a JSON object."}), 400
        try:
            event = ds.store.add(body, {k: {x: m[x] for x in ("subject_id", "subject_label", "predicate_id", "object_id",
                                                               "object_label", "object_source", "confidence", "method")}
                                        for k, m in ds.by_key.items()})
        except ReviewError as exc:
            return jsonify({"error": str(exc)}), 400
        ds.write_snapshot()
        return jsonify({"event": event, "state": ds.store.state().get(event["key"])})

    @app.get("/d/<name>/export.csv")
    def csv_export(name):
        ds = get(name)
        return Response(export_csv(ds.maps, ds.store.state()), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}-reviews.csv"})

    @app.get("/d/<name>/export.sssom.tsv")
    def sssom_export(name):
        ds = get(name)
        return Response(export_sssom(ds.maps, ds.store.state(), name), mimetype="text/tab-separated-values",
                        headers={"Content-Disposition": f"attachment; filename={name}-reviews.sssom.tsv"})

    return app


def serve(source=None, *, folder=None, reviews="reviews", port: int = 8097, online: bool = True, title="Mapping validation"):
    """Start the review tool on ``port`` (blocks). ``reviews`` is a folder, or one
    ``.jsonl`` file for a single ``source`` (its name then names the dataset)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    reviews = Path(reviews)
    single = reviews.suffix == ".jsonl"
    reviews_dir = reviews.parent if single else reviews
    app = create_app(source, folder=folder, reviews_dir=reviews_dir, online=online, title=title,
                     name=reviews.stem if single and source is not None else None)
    logger.info("Review tool on http://localhost:%d, decisions in %s", port, reviews_dir.resolve())
    app.run(host="0.0.0.0", port=port, threaded=True)
