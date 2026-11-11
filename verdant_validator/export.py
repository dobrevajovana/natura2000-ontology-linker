"""Reviewed mappings out again: CSV (every mapping with its state) and SSSOM (decided ones)."""

from __future__ import annotations

import csv
import io
import re

CSV_FIELDS = ["subject_id", "subject_label", "subject_category", "predicted_match", "object_id", "object_label",
              "object_source", "confidence", "method", "automatic_checks", "status", "reviewed_match", "disputed",
              "reviewers", "last_reviewer", "last_reviewed", "comment"]

# Prefixes for SSSOM CURIEs; others are written as full URIs.
PREFIXES = {
    "http://purl.obolibrary.org/obo/ENVO_": "ENVO:", "http://purl.obolibrary.org/obo/NCBITaxon_": "NCBITaxon:",
    "http://purl.obolibrary.org/obo/OBI_": "OBI:", "http://purl.obolibrary.org/obo/PATO_": "PATO:",
    "http://purl.obolibrary.org/obo/CHEBI_": "CHEBI:", "http://purl.obolibrary.org/obo/UBERON_": "UBERON:",
    "http://www.wikidata.org/entity/": "wikidata:", "https://www.gbif.org/species/": "gbif:",
    "http://dbpedia.org/resource/": "dbpedia:", "https://w3id.org/gold.path/": "GOLD:",
    "http://vocab.nerc.ac.uk/standard_name/": "CF:",
}


def curie(uri: str) -> str:
    for base, prefix in PREFIXES.items():
        if uri.startswith(base):
            return prefix + uri[len(base):].rstrip("/")
    return uri


def _checks_text(m: dict) -> str:
    return "; ".join(f"{f['level']}: {f['message']}" for f in m.get("checks") or [] if f["level"] != "info")


def export_csv(maps: list[dict], state: dict[str, dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for m in sorted(maps, key=lambda m: (m["subject_label"] or "", -(m.get("confidence") or 0))):
        s = state.get(m["key"], {})
        w.writerow({
            "subject_id": m["subject_id"], "subject_label": m["subject_label"], "subject_category": m.get("subject_category") or "",
            "predicted_match": m.get("predicate_id") or "", "object_id": m["object_id"], "object_label": m.get("object_label") or "",
            "object_source": m.get("object_source") or "", "confidence": "" if m.get("confidence") is None else m["confidence"],
            "method": m.get("method") or "", "automatic_checks": _checks_text(m), "status": s.get("verdict", "unreviewed"),
            "reviewed_match": s.get("match_type") or "", "disputed": "yes" if s.get("disputed") else "",
            "reviewers": "; ".join(f"{r}: {v['verdict']}" for r, v in s.get("reviewers", {}).items()),
            "last_reviewer": s.get("reviewer", ""), "last_reviewed": s.get("time", ""), "comment": s.get("comment", ""),
        })
    return buf.getvalue()


SSSOM_FIELDS = ["subject_id", "subject_label", "subject_category", "predicate_id", "predicate_modifier", "object_id",
                "object_label", "mapping_justification", "author_label", "mapping_date", "object_source", "comment"]


def export_sssom(maps: list[dict], state: dict[str, dict], name: str = "mappings") -> str:
    """Mappings whose latest decision is correct or incorrect (unsure ones stay out),
    as manual curation; an incorrect one carries predicate_modifier "Not"."""
    used = {p for m in maps for b, p in PREFIXES.items() if m["object_id"].startswith(b)}
    header = ["# curie_map:"] + [f"#   {p[:-1]}: {b}" for b, p in PREFIXES.items() if p in used] + [
        "#   skos: http://www.w3.org/2004/02/skos/core#", "#   semapv: https://w3id.org/semapv/vocab/",
        f"# mapping_set_id: https://w3id.org/verdant/validation/{re.sub(r'[^A-Za-z0-9_.-]+', '-', name)}.sssom.tsv",
        "# mapping_set_description: Predicted mappings reviewed by experts with the VERDANT validation tool.",
        "# license: https://opensource.org/licenses/MIT"]
    buf = io.StringIO()
    buf.write("\n".join(header) + "\n")
    w = csv.DictWriter(buf, fieldnames=SSSOM_FIELDS, delimiter="\t", lineterminator="\n")
    w.writeheader()
    for m in sorted(maps, key=lambda m: m["key"]):
        s = state.get(m["key"])
        if not s or s["verdict"] not in ("correct", "incorrect"):
            continue
        notes = []
        if s.get("match_type") and s["match_type"] != m.get("predicate_id"):
            notes.append(f"predicted {m.get('predicate_id')}")
        if s.get("disputed"):
            notes.append("reviewers disagree: " + "; ".join(f"{r} {v['verdict']}" for r, v in s["reviewers"].items()))
        if s.get("comment"):
            notes.append(s["comment"])
        w.writerow({
            "subject_id": m["subject_id"], "subject_label": m["subject_label"], "subject_category": m.get("subject_category") or "",
            "predicate_id": s.get("match_type") or m.get("predicate_id"),
            "predicate_modifier": "Not" if s["verdict"] == "incorrect" else "",
            "object_id": curie(m["object_id"]), "object_label": m.get("object_label") or "",
            "mapping_justification": "semapv:ManualMappingCuration", "author_label": "|".join(sorted(s["reviewers"])),
            "mapping_date": s["time"][:10], "object_source": m.get("object_source") or "",
            "comment": " | ".join(notes).replace("\t", " ").replace("\n", " "),
        })
    return buf.getvalue()
