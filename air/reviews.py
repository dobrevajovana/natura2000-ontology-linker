"""Expert review of the aerobiome matches, stored like the MFD viewer's (mfd/reviews.py)."""

from __future__ import annotations

import csv
import io
from urllib.parse import quote

from mfd.reviews import MATCH_TYPES, VERDICTS, ReviewError, ReviewStore, mapping_key  # noqa: F401

from air.build import SLOT_LABELS


def predicted_matches(graph: dict) -> dict[str, dict]:
    """The reviewable matches in graph.json, by key."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    out = {}
    for e in graph.get("edges", []):
        if e.get("kind") != "match":
            continue
        src, tgt = nodes.get(e["source"], {}), nodes.get(e["target"], {})
        out[mapping_key(e["source"], e["target"])] = {
            "source": e["source"], "target": e["target"],
            "source_label": src.get("label"), "target_label": tgt.get("label"),
            "slot": src.get("slot"), "samples": src.get("samples"), "ontology": tgt.get("kind"),
            "curie": tgt.get("curie"), "match": e.get("match"), "confidence": e.get("confidence"),
            "method": e.get("origin"), "flagged": bool(e.get("review")),
        }
    return out


CSV_FIELDS = ["field", "value", "samples", "predicted_match", "target_id", "target_label", "target_ontology",
              "confidence", "origin", "flagged", "status", "reviewed_match", "disputed", "reviewers",
              "last_reviewer", "last_reviewed", "comment"]


def export_csv(predicted: dict[str, dict], state: dict[str, dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for key, p in sorted(predicted.items()):
        s = state.get(key, {})
        w.writerow({
            "field": SLOT_LABELS.get(p["slot"], p["slot"]), "value": p["source_label"], "samples": p["samples"],
            "predicted_match": p["match"], "target_id": p["curie"] or p["target"], "target_label": p["target_label"],
            "target_ontology": p["ontology"], "confidence": p["confidence"], "origin": p["method"],
            "flagged": "yes" if p["flagged"] else "", "status": s.get("verdict", "unreviewed"),
            "reviewed_match": s.get("match_type") or "", "disputed": "yes" if s.get("disputed") else "",
            "reviewers": "; ".join(f"{r}: {v['verdict']}" for r, v in s.get("reviewers", {}).items()),
            "last_reviewer": s.get("reviewer", ""), "last_reviewed": s.get("time", ""), "comment": s.get("comment", ""),
        })
    return buf.getvalue()


SSSOM_HEADER = """# curie_map:
#   air: urn:verdant:air:
#   skos: http://www.w3.org/2004/02/skos/core#
#   semapv: https://w3id.org/semapv/vocab/
# mapping_set_id: urn:verdant:air:mappings-reviewed
# mapping_set_description: Environment terms from air samples (MIxS fields), matched to ENVO and other ontologies and reviewed by experts.
# license: https://opensource.org/licenses/MIT
"""
SSSOM_FIELDS = ["subject_id", "subject_label", "subject_category", "predicate_id", "predicate_modifier", "object_id",
                "object_label", "mapping_justification", "author_label", "mapping_date", "object_source", "comment"]


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
            "subject_id": "air:" + quote(p["source"].removeprefix("air:"), safe=":"),
            "subject_label": p["source_label"], "subject_category": p["slot"],
            "predicate_id": s.get("match_type") or p["match"],
            "predicate_modifier": "Not" if s["verdict"] == "incorrect" else "",
            "object_id": p["curie"] or p["target"], "object_label": p["target_label"],
            "mapping_justification": "semapv:ManualMappingCuration",
            "author_label": "|".join(sorted(s["reviewers"])),
            "mapping_date": s["time"][:10], "object_source": p["ontology"],
            "comment": " | ".join(notes).replace("\t", " ").replace("\n", " "),
        })
    return buf.getvalue()
