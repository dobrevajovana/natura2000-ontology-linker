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

# The review store is shared with every other validation (validation/store.py).
from verdant_validator.store import MATCH_TYPES, MAX_COMMENT, MAX_REVIEWER, VERDICTS, ReviewError, ReviewStore  # noqa: F401
from verdant_validator.mappings import mapping_key  # noqa: F401


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
