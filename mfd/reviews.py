"""Expert review of the predicted matches shown in the MFD viewer.

The predicted matches are the Natura 2000 linker's links from a Natura 2000
habitat (one the MFD ontology points at) to Wikidata, EUNIS, ENVO and so on.
Reviewers mark each one correct, incorrect or unsure, may correct its match
type and may leave a comment.

Storage is an append-only JSON-lines log: every decision is kept, with who made
it and when, and nothing is ever rewritten. The current state of a match is the
latest decision per reviewer; when reviewers' latest decisions differ, the match
is "disputed".

Exports: CSV (every predicted match with its review state) and SSSOM (reviewed
matches only, as manual curation; an incorrect match carries predicate_modifier
"Not").
"""

from __future__ import annotations

import csv
import io
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

VERDICTS = ("correct", "incorrect", "unsure")
MATCH_TYPES = ("skos:exactMatch", "skos:closeMatch", "skos:broadMatch", "skos:narrowMatch", "skos:relatedMatch")
MAX_REVIEWER, MAX_COMMENT = 80, 2000


class ReviewError(ValueError):
    """A review that cannot be stored; the message is safe to show the reviewer."""


def mapping_key(source: str, target: str) -> str:
    return f"{source}|{target}"


def predicted_matches(graph: dict) -> dict[str, dict]:
    """The reviewable matches in graph.json, by key, with what was predicted."""
    labels = {n["id"]: n for n in graph.get("nodes", [])}
    out = {}
    for e in graph.get("edges", []):
        if e.get("kind") != "linker":
            continue
        src, tgt = labels.get(e["source"], {}), labels.get(e["target"], {})
        out[mapping_key(e["source"], e["target"])] = {
            "source": e["source"], "target": e["target"],
            "source_label": src.get("label"), "target_label": tgt.get("label"),
            "term": e.get("term"), "ontology": tgt.get("kind"),
            "match": e.get("match"), "confidence": e.get("confidence"), "method": e.get("method"),
            "flagged": bool(e.get("review")),
        }
    return out


class ReviewStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a half-written last line after a crash; the rest stands
        return out

    def add(self, body: dict, predicted: dict[str, dict]) -> dict:
        """Validate one decision against the predicted matches and append it."""
        key = str(body.get("key") or "")
        if key not in predicted:
            raise ReviewError("Unknown match; reload the page.")
        verdict = body.get("verdict")
        if verdict not in VERDICTS:
            raise ReviewError(f"Verdict must be one of: {', '.join(VERDICTS)}.")
        reviewer = " ".join(str(body.get("reviewer") or "").split())
        if not reviewer or len(reviewer) > MAX_REVIEWER:
            raise ReviewError(f"Enter your name (up to {MAX_REVIEWER} characters) before reviewing.")
        comment = str(body.get("comment") or "").strip()
        if len(comment) > MAX_COMMENT:
            raise ReviewError(f"Comment is longer than {MAX_COMMENT} characters.")
        match_type = body.get("match_type") or None
        if match_type is not None and match_type not in MATCH_TYPES:
            raise ReviewError("Unknown match type.")
        if verdict != "correct":
            match_type = None  # a corrected type only means something for a correct link
        event = {
            "key": key,
            "verdict": verdict,
            "match_type": match_type,
            "comment": comment,
            "reviewer": reviewer,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "predicted": predicted[key],
        }
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
        return event

    def state(self) -> dict[str, dict]:
        """Per match: latest decision per reviewer, overall latest, and whether reviewers disagree."""
        out: dict[str, dict] = {}
        for ev in self.events():
            s = out.setdefault(ev["key"], {"reviewers": {}, "history": []})
            s["reviewers"][ev["reviewer"]] = ev
            s["history"].append({k: ev.get(k) for k in ("verdict", "match_type", "comment", "reviewer", "time")})
        for s in out.values():
            latest = max(s["reviewers"].values(), key=lambda ev: ev["time"])
            opinions = {(ev["verdict"], ev["match_type"]) for ev in s["reviewers"].values()}
            s.update(verdict=latest["verdict"], match_type=latest["match_type"], comment=latest["comment"],
                     reviewer=latest["reviewer"], time=latest["time"], disputed=len(opinions) > 1)
            s["reviewers"] = {r: {"verdict": ev["verdict"], "match_type": ev["match_type"], "time": ev["time"]}
                              for r, ev in s["reviewers"].items()}
        return out


CSV_FIELDS = ["natura2000_code", "natura2000_label", "predicted_match", "target_id", "target_label", "target_ontology",
              "confidence", "method", "flagged_by_linker", "status", "reviewed_match", "disputed", "reviewers",
              "last_reviewer", "last_reviewed", "comment"]


def export_csv(predicted: dict[str, dict], state: dict[str, dict]) -> str:
    """Every predicted match, reviewed or not, for a spreadsheet."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for key, p in sorted(predicted.items()):
        s = state.get(key, {})
        w.writerow({
            "natura2000_code": p["source"].removeprefix("n2k:"), "natura2000_label": p["source_label"],
            "predicted_match": p["match"], "target_id": p["target"], "target_label": p["target_label"],
            "target_ontology": p["ontology"], "confidence": p["confidence"], "method": p["method"],
            "flagged_by_linker": "yes" if p["flagged"] else "", "status": s.get("verdict", "unreviewed"),
            "reviewed_match": s.get("match_type") or "", "disputed": "yes" if s.get("disputed") else "",
            "reviewers": "; ".join(f"{r}: {v['verdict']}" for r, v in s.get("reviewers", {}).items()),
            "last_reviewer": s.get("reviewer", ""), "last_reviewed": s.get("time", ""), "comment": s.get("comment", ""),
        })
    return buf.getvalue()


SSSOM_HEADER = """# curie_map:
#   n2k: https://ontology.natura2000.eu/
#   skos: http://www.w3.org/2004/02/skos/core#
#   semapv: https://w3id.org/semapv/vocab/
# mapping_set_id: https://ontology.natura2000.eu/mappings-reviewed.sssom.tsv
# mapping_set_description: Natura 2000 linker matches used by the MFD habitat ontology, reviewed by experts in the MFD viewer.
# license: https://opensource.org/licenses/MIT
"""
SSSOM_FIELDS = ["subject_id", "subject_label", "predicate_id", "predicate_modifier", "object_id", "object_label",
                "mapping_justification", "author_label", "mapping_date", "object_source", "comment"]


def export_sssom(predicted: dict[str, dict], state: dict[str, dict]) -> str:
    """Matches whose latest decision is correct or incorrect; unsure ones stay out."""
    buf = io.StringIO()
    buf.write(SSSOM_HEADER)
    w = csv.DictWriter(buf, fieldnames=SSSOM_FIELDS, delimiter="\t", lineterminator="\n")
    w.writeheader()
    for key, p in sorted(predicted.items()):
        s = state.get(key)
        if not s or s["verdict"] not in ("correct", "incorrect"):
            continue
        notes = []
        if s.get("match_type") and s["match_type"] != p["match"]:
            notes.append(f"predicted {p['match']}")
        if s.get("disputed"):
            notes.append("reviewers disagree: " + "; ".join(f"{r} {v['verdict']}" for r, v in s["reviewers"].items()))
        if s.get("comment"):
            notes.append(s["comment"])
        w.writerow({
            "subject_id": f"n2k:{p['term']}" if p.get("term") else p["source"],
            "subject_label": p["source_label"],
            "predicate_id": s.get("match_type") or p["match"],
            "predicate_modifier": "Not" if s["verdict"] == "incorrect" else "",
            "object_id": p["target"], "object_label": p["target_label"],
            "mapping_justification": "semapv:ManualMappingCuration",
            "author_label": "|".join(sorted(s["reviewers"])),
            "mapping_date": s["time"][:10], "object_source": p["ontology"],
            "comment": " | ".join(notes).replace("\t", " ").replace("\n", " "),
        })
    return buf.getvalue()
