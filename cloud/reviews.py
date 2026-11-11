"""Expert review of the cloud samples' predicted ENVO and NCBITaxon terms.

Storage and review rules are the MFD viewer's (mfd/reviews.py): an append-only
JSON-lines log, latest decision per reviewer, "disputed" when they differ. Only
what is reviewed and how it is exported differ.
"""

from __future__ import annotations

import csv
import io

from verdant_validator.mappings import mapping_key
from verdant_validator.store import MATCH_TYPES, VERDICTS, ReviewError, ReviewStore  # noqa: F401  (re-exported)


def predicted_matches(graph: dict) -> dict[str, dict]:
    """The reviewable matches in graph.json (sample text -> ontology term), by key."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    out = {}
    for e in graph.get("edges", []):
        if e.get("kind") != "predicted":
            continue
        src, tgt = nodes.get(e["source"], {}), nodes.get(e["target"], {})
        out[mapping_key(e["source"], e["target"])] = {
            "source": e["source"], "target": e["target"],
            "field": src.get("field_label"), "text": src.get("label"),
            "target_label": tgt.get("label"), "curie": tgt.get("curie"), "ontology": tgt.get("kind"),
            "match": e.get("match"), "confidence": e.get("confidence"), "method": e.get("method"),
            "query": e.get("query"), "runs": e.get("runs"), "in_graph": e.get("in_graph") or [],
        }
    return out


CSV_FIELDS = ["field", "sample_text", "runs", "predicted_match", "term_id", "term_label", "ontology", "confidence",
              "method", "searched_for", "already_in_graph", "status", "reviewed_match", "disputed", "reviewers",
              "last_reviewer", "last_reviewed", "comment"]


def export_csv(predicted: dict[str, dict], state: dict[str, dict]) -> str:
    """Every predicted match, reviewed or not, for a spreadsheet."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for key, p in sorted(predicted.items(), key=lambda kv: (kv[1]["field"] or "", kv[1]["text"] or "", -(kv[1]["confidence"] or 0))):
        s = state.get(key, {})
        w.writerow({
            "field": p["field"], "sample_text": p["text"], "runs": p["runs"], "predicted_match": p["match"],
            "term_id": p["curie"], "term_label": p["target_label"], "ontology": p["ontology"],
            "confidence": p["confidence"], "method": p["method"], "searched_for": p["query"],
            "already_in_graph": "; ".join(p["in_graph"]), "status": s.get("verdict", "unreviewed"),
            "reviewed_match": s.get("match_type") or "", "disputed": "yes" if s.get("disputed") else "",
            "reviewers": "; ".join(f"{r}: {v['verdict']}" for r, v in s.get("reviewers", {}).items()),
            "last_reviewer": s.get("reviewer", ""), "last_reviewed": s.get("time", ""), "comment": s.get("comment", ""),
        })
    return buf.getvalue()


SSSOM_HEADER = """# curie_map:
#   ENVO: http://purl.obolibrary.org/obo/ENVO_
#   NCBITaxon: http://purl.obolibrary.org/obo/NCBITaxon_
#   CF: http://vocab.nerc.ac.uk/standard_name/
#   MGnify: https://www.ebi.ac.uk/metagenomics/biomes/
#   GOLD: https://w3id.org/gold.path/
#   OBI: http://purl.obolibrary.org/obo/OBI_
#   skos: http://www.w3.org/2004/02/skos/core#
#   semapv: https://w3id.org/semapv/vocab/
# mapping_set_id: https://ontology.natura2000.eu/cloud-PRJEB54740-reviewed.sssom.tsv
# mapping_set_description: Sample text of the cloud and aerosol runs in ENA PRJEB54740 and the project's metadata plan, mapped to ENVO, NCBITaxon, MGnify biomes, GOLD ecosystems and CF Standard Names, reviewed by experts.
# license: https://opensource.org/licenses/MIT
"""
OBJECT_SOURCES = {"envo": "obo:envo", "ncbitaxon": "obo:ncbitaxon", "mgnify": "https://www.ebi.ac.uk/metagenomics/biomes", "gold": "obo:gold",
                  "cf": "https://cfconventions.org/Data/cf-standard-names/", "obi": "obo:obi"}
SSSOM_FIELDS = ["subject_label", "subject_category", "predicate_id", "predicate_modifier", "object_id", "object_label",
                "mapping_justification", "author_label", "mapping_date", "object_source", "comment"]


def export_sssom(predicted: dict[str, dict], state: dict[str, dict]) -> str:
    """Matches whose latest decision is correct or incorrect; unsure ones stay out.

    The subject is free text from the ENA record, so it goes in subject_label
    with its field as subject_category, as SSSOM does for literal mappings.
    """
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
            "subject_label": p["text"], "subject_category": p["field"],
            "predicate_id": s.get("match_type") or p["match"],
            "predicate_modifier": "Not" if s["verdict"] == "incorrect" else "",
            "object_id": p["curie"], "object_label": p["target_label"],
            "mapping_justification": "semapv:ManualMappingCuration",
            "author_label": "|".join(sorted(s["reviewers"])),
            "mapping_date": s["time"][:10], "object_source": OBJECT_SOURCES.get(p["ontology"], p["ontology"] or ""),
            "comment": " | ".join(notes).replace("\t", " ").replace("\n", " "),
        })
    return buf.getvalue()
