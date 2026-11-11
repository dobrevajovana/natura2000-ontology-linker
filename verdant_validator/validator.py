"""Validation of one set of predicted mappings, done in Python.

    from verdant_validator import Validator

    v = Validator("data_pangaea/mappings.json", reviews="reviews/pangaea.jsonl")
    v.check(online=True)                  # automatic checks
    v.findings(level="warning")           # what they found
    v.decide(key, "incorrect", reviewer="Jovana", comment="GBIF: a synonym")
    v.pending()                           # what still needs an expert
    v.summary()
    v.to_sssom("pangaea.reviewed.sssom.tsv")

Decisions made here and on the review page (``v.serve()``) go to the same file,
so a reviewer can work in a notebook, on the page, or both.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from verdant_validator.checks import check as run_checks
from verdant_validator.export import export_csv, export_sssom
from verdant_validator.mappings import load
from verdant_validator.store import ReviewError, ReviewStore

LEVELS = {"error": 0, "warning": 1, "info": 2, "clean": 3}


class Validator:
    """Predicted mappings, their automatic checks, and the experts' decisions on them."""

    def __init__(self, source, reviews: str | Path | None = None, name: str | None = None):
        """``source``: anything ``load`` reads (a file, a folder with mappings.json, or a list of
        dicts). ``reviews``: the decisions file (.jsonl); default ``reviews/<name>.jsonl``."""
        self.source = source
        self.maps: list[dict] = load(source)
        if name is None:
            name = Path(source).stem if not isinstance(source, list) else "mappings"
            if name in ("mappings", "graph") and not isinstance(source, list):
                name = f"{Path(source).resolve().parent.name}-{name}"
        self.name = name
        self.store = ReviewStore(Path(reviews) if reviews else Path("reviews") / f"{name}.jsonl")
        self.report: dict = {}

    # --- the mappings -------------------------------------------------------

    def __len__(self) -> int:
        return len(self.maps)

    def __iter__(self):
        return iter(self.maps)

    def __getitem__(self, key: str) -> dict:
        for m in self.maps:
            if m["key"] == key:
                return m
        raise KeyError(key)

    def find(self, text: str) -> list[dict]:
        """Mappings whose subject or object mentions ``text`` (any case)."""
        t = text.lower()
        return [m for m in self.maps if any(t in str(m.get(f) or "").lower()
                                            for f in ("subject_id", "subject_label", "object_id", "object_label"))]

    # --- automatic checks ---------------------------------------------------

    def check(self, online: bool = False) -> dict:
        """Run the automatic checks; findings go into each mapping's ``checks`` list."""
        self.report = run_checks(self.maps, online=online)
        return self.report

    @staticmethod
    def level(m: dict) -> str:
        levels = {f["level"] for f in m.get("checks") or []}
        return next((lv for lv in ("error", "warning", "info") if lv in levels), "clean")

    def findings(self, level: str | None = None) -> list[dict]:
        """One row per finding: the mapping, the check and its message. ``level`` keeps
        only errors, or errors and warnings (``"warning"``)."""
        keep = None if level is None else {lv for lv, rank in LEVELS.items() if rank <= LEVELS[level]}
        return [{"key": m["key"], "subject": m["subject_label"], "object": m.get("object_label") or m["object_id"],
                 "predicate": m.get("predicate_id"), "level": f["level"], "check": f["code"], "message": f["message"]}
                for m in self.maps for f in m.get("checks") or [] if keep is None or f["level"] in keep]

    # --- expert decisions ---------------------------------------------------

    def state(self) -> dict[str, dict]:
        return self.store.state()

    def status(self, key: str) -> str:
        s = self.state().get(key)
        return "unreviewed" if not s else "disputed" if s["disputed"] else s["verdict"]

    def decide(self, key: str, verdict: str, reviewer: str, comment: str = "", match_type: str | None = None) -> dict:
        """Record one decision (correct / incorrect / unsure), as the review page does."""
        if not match_type or verdict != "correct":
            match_type = None
        elif ":" not in match_type:
            match_type = "skos:" + match_type
        return self.store.add({"key": key, "verdict": verdict, "reviewer": reviewer, "comment": comment,
                               "match_type": match_type}, {m["key"]: m for m in self.maps})

    def accept(self, key: str, reviewer: str, comment: str = "", match_type: str | None = None) -> dict:
        return self.decide(key, "correct", reviewer, comment, match_type)

    def reject(self, key: str, reviewer: str, comment: str = "") -> dict:
        return self.decide(key, "incorrect", reviewer, comment)

    def pending(self) -> list[dict]:
        """Mappings no one has decided on yet, those the checks flag first."""
        state = self.state()
        open_ = [m for m in self.maps if m["key"] not in state]
        return sorted(open_, key=lambda m: (LEVELS[self.level(m)], m.get("confidence") if m.get("confidence") is not None else 1))

    def summary(self) -> dict:
        state = self.state()
        statuses = Counter("unreviewed" if m["key"] not in state else "disputed" if state[m["key"]]["disputed"]
                           else state[m["key"]]["verdict"] for m in self.maps)
        return {"name": self.name, "mappings": len(self.maps), "subjects": len({m["subject_id"] for m in self.maps}),
                "by_source": dict(Counter(m.get("object_source") or "?" for m in self.maps)),
                "by_match_type": dict(Counter(m.get("predicate_id") or "?" for m in self.maps)),
                "checks": {k: v for k, v in self.report.items() if k != "mappings"} or "not run",
                "decisions": {s: statuses.get(s, 0) for s in ("correct", "incorrect", "unsure", "disputed", "unreviewed")},
                "reviews_file": str(self.store.path)}

    # --- out ----------------------------------------------------------------

    def to_records(self) -> list[dict]:
        """Every mapping with its findings and its current decision, flat (for a DataFrame)."""
        state = self.state()
        rows = []
        for m in self.maps:
            s = state.get(m["key"], {})
            rows.append({**{k: m.get(k) for k in ("key", "subject_id", "subject_label", "subject_category", "predicate_id",
                                                  "object_id", "object_label", "object_source", "confidence", "method")},
                         "check_level": self.level(m), "checks": "; ".join(f["message"] for f in m.get("checks") or []),
                         "status": "unreviewed" if not s else "disputed" if s["disputed"] else s["verdict"],
                         "reviewed_match": s.get("match_type"), "reviewer": s.get("reviewer"), "comment": s.get("comment")})
        return rows

    def to_dataframe(self):
        import pandas as pd
        return pd.DataFrame(self.to_records())

    def to_csv(self, path: str | Path | None = None) -> str:
        text = export_csv(self.maps, self.state())
        if path:
            Path(path).write_text(text, encoding="utf-8")
        return text

    def to_sssom(self, path: str | Path | None = None) -> str:
        """Decided mappings (correct, and incorrect with predicate_modifier Not) as SSSOM."""
        text = export_sssom(self.maps, self.state(), self.name)
        if path:
            Path(path).write_text(text, encoding="utf-8")
        return text

    def serve(self, port: int = 8097, online: bool = True):
        """The review page for these mappings; decisions go to the same reviews file."""
        from verdant_validator.web import serve
        serve(self.maps, reviews=self.store.path, port=port, online=online, title=f"Validation · {self.name}")

    def __repr__(self) -> str:
        return f"<Validator {self.name}: {len(self.maps)} mappings, reviews in {self.store.path}>"


__all__ = ["Validator", "ReviewError"]
