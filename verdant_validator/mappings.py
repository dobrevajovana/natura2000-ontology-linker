"""Predicted mappings from any source, in one shape the checks and the review tool understand.

A mapping is a plain dict, close to an SSSOM row:

    key               "<subject_id>|<object_id>", stable across runs (reviews are stored by it)
    subject_id        what is being linked (a Natura 2000 term, a PANGAEA name, a sample's text)
    subject_label     its readable name
    subject_category  species, habitat, biome, ... (optional)
    predicate_id      skos:exactMatch / closeMatch / broadMatch / narrowMatch / relatedMatch
    object_id         the term linked to (a URI, or a CURIE)
    object_label      its label (optional)
    object_source     ENVO, NCBITaxon, GBIF, Wikidata, ... (optional)
    confidence        0..1 (optional)
    method            how the link was made (optional)
    flagged           True when the producer itself asked for a review
    context           anything else the source carried, shown to the reviewer

``load(path)`` reads, by its shape:

* the Natura 2000 linker's ``mappings.json`` (and ``terms.json`` beside it for categories),
  which is also what the PANGAEA experiment produces;
* SSSOM TSV;
* tables (CSV, TSV, Excel) with SSSOM or linker column names, or the PANGAEA experiment's
  wide comparison sheet (``VERDANT_*`` columns holding "uri (exactMatch)");
* the MFD and cloud viewers' ``graph.json``;
* a list of dicts already in memory (``from_records``).
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

MATCH_TYPES = ("skos:exactMatch", "skos:closeMatch", "skos:broadMatch", "skos:narrowMatch", "skos:relatedMatch")

# Column names seen in the wild for each field, first match wins.
ALIASES = {
    "subject_id": ["subject_id", "source_id", "term_id", "subject", "name"],
    "subject_label": ["subject_label", "source_label", "label", "name", "text"],
    "subject_category": ["subject_category", "category", "group", "field"],
    "predicate_id": ["predicate_id", "match_type", "predicate", "match"],
    "object_id": ["object_id", "target_uri", "target_id", "object", "uri", "term"],
    "object_label": ["object_label", "target_label", "term_label"],
    "object_source": ["object_source", "source_ontology", "ontology", "target_ontology"],
    "confidence": ["confidence", "score"],
    "method": ["mapping_justification", "method", "mapping_tool"],
}
FIELDS = list(ALIASES) + ["flagged", "context"]


def mapping_key(subject_id: str, object_id: str) -> str:
    return f"{subject_id}|{object_id}"


def normalise_predicate(p) -> str | None:
    """'exactMatch', 'skos:exactMatch', 'http://www.w3.org/2004/02/skos/core#exactMatch' -> 'skos:exactMatch'."""
    if not p:
        return None
    p = str(p).strip()
    p = p.rsplit("#", 1)[-1] if p.startswith("http") else p
    p = p if ":" in p else "skos:" + p
    for t in MATCH_TYPES:
        if t.lower() == p.lower():
            return t
    return p


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f    # NaN from spreadsheets


def make(subject_id, object_id, *, subject_label=None, subject_category=None, predicate_id=None, object_label=None,
         object_source=None, confidence=None, method=None, flagged=False, context=None) -> dict:
    subject_id, object_id = str(subject_id).strip(), str(object_id).strip()
    # Long text (definitions) is cut: the reviewer needs a hint, not the whole paragraph.
    context = {k: (v[:280] + "…" if isinstance(v, str) and len(v) > 300 else v) for k, v in (context or {}).items()
               if v not in (None, "", [], {})}
    return {
        "key": mapping_key(subject_id, object_id), "subject_id": subject_id,
        "subject_label": (str(subject_label).strip() if subject_label not in (None, "") else subject_id),
        "subject_category": subject_category or None, "predicate_id": normalise_predicate(predicate_id),
        "object_id": object_id, "object_label": object_label or None, "object_source": object_source or None,
        "confidence": _num(confidence), "method": method or None, "flagged": bool(flagged), "context": context or {},
    }


def from_records(records: list[dict]) -> list[dict]:
    """Rows with any of the ALIASES as keys; the rest of each row is kept as context."""
    out = []
    for r in records:
        r = {str(k).strip(): v for k, v in r.items() if v is not None and str(v).strip() not in ("", "nan")}
        got = {}
        for field, names in ALIASES.items():
            for n in names:
                if n in r and n not in got.values():
                    got[field] = n
                    break
        if "subject_id" not in got or "object_id" not in got:
            continue
        used = set(got.values())
        out.append(make(r[got["subject_id"]], r[got["object_id"]],
                        **{f: r.get(n) for f, n in got.items() if f not in ("subject_id", "object_id")},
                        flagged=str(r.get("review", "")).lower() in ("true", "1", "yes"),
                        context={k: v for k, v in r.items() if k not in used and k != "review"}))
    return _dedupe(out)


def _dedupe(maps: list[dict]) -> list[dict]:
    """One mapping per key; a repeat with another predicate is kept in context for the checks."""
    by_key: dict[str, dict] = {}
    for m in maps:
        first = by_key.get(m["key"])
        if first is None:
            by_key[m["key"]] = m
        elif m["predicate_id"] != first["predicate_id"]:
            first["context"].setdefault("other_predicates", []).append(m["predicate_id"])
    return list(by_key.values())


def from_linker(mappings: list[dict], terms: list[dict] | None = None) -> list[dict]:
    """The Natura 2000 linker's mappings.json (PANGAEA's experiment writes the same)."""
    cats = {t["id"]: t.get("category") for t in terms or []}
    return _dedupe([make(m["source_id"], m["target_uri"], subject_label=m.get("source_label"),
                         subject_category=cats.get(m["source_id"]), predicate_id=m.get("match_type"),
                         object_label=m.get("target_label"), object_source=m.get("source_ontology"),
                         confidence=m.get("confidence"), method=m.get("method"), flagged=m.get("review"),
                         context={k: v for k, v in m.items() if k not in
                                  ("source_id", "source_label", "target_uri", "target_label", "match_type",
                                   "source_ontology", "confidence", "method", "review")})
                    for m in mappings if m.get("source_id") and m.get("target_uri")])


def from_graph(graph: dict) -> list[dict]:
    """The MFD / cloud viewers' graph.json: their predicted (or linker) edges."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    out = []
    for e in graph.get("edges", []):
        if e.get("kind") not in ("predicted", "linker"):
            continue
        s, t = nodes.get(e["source"], {}), nodes.get(e["target"], {})
        out.append(make(e["source"], e["target"], subject_label=s.get("label"),
                        subject_category=s.get("field_label") or s.get("kind"), predicate_id=e.get("match"),
                        object_label=t.get("label"), object_source=t.get("kind"), confidence=e.get("confidence"),
                        method=e.get("method"), flagged=e.get("review")))
    return _dedupe(out)


_CELL = re.compile(r"^(?P<target>.+?)\s*\((?P<match>\w+Match)(?:,\s*(?P<conf>[\d.]+))?\)$")


def from_wide_table(rows: list[dict]) -> list[dict]:
    """The PANGAEA experiment's comparison sheet: one row per name, a column per source
    (VERDANT_GBIF, VERDANT_ENVO, ...) holding 'target (matchType[, confidence])' joined by ' ; '."""
    out = []
    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        for col, cell in r.items():
            if not str(col).startswith("VERDANT_") or col in ("VERDANT_number_of_links", "VERDANT_has_exactMatch"):
                continue
            for part in [p.strip() for p in str(cell or "").split(" ; ") if p.strip() and str(cell) != "nan"]:
                m = _CELL.match(part)
                if not m:
                    continue
                target = m["target"].strip()
                is_uri = target.startswith("http")
                out.append(make(name, target, subject_category=r.get("group"), predicate_id=m["match"],
                                object_label=None if is_uri else target, object_source=col.removeprefix("VERDANT_"),
                                confidence=m["conf"],
                                context={k: r[k] for k in ("PANGAEA_terms",) if r.get(k)} |
                                        ({} if is_uri else {"note": "the sheet gives a label, not an identifier"})))
    return _dedupe(out)


def from_sssom(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    rows = list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="\t"))
    maps = from_records(rows)
    for m, r in zip(maps, rows):
        if (r.get("predicate_modifier") or "").lower() == "not":
            m["context"]["predicate_modifier"] = "Not"
    return maps


def _table_rows(path: Path) -> list[dict]:
    if path.suffix.lower() in (".xlsx", ".xls"):
        import pandas as pd
        return pd.read_excel(path, dtype=str).fillna("").to_dict("records")
    text = path.read_text(encoding="utf-8")
    delim = "\t" if path.suffix.lower() in (".tsv", ".txt") or text.count("\t") > text.count(",") else ","
    return list(csv.DictReader(io.StringIO("\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))),
                               delimiter=delim))


def load(source, terms=None) -> list[dict]:
    """Mappings from a file (format told by its shape) or from a list of dicts."""
    if isinstance(source, list):
        return from_records(source)
    path = Path(source)
    if path.is_dir():
        path = path / "mappings.json"
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "edges" in data:
            return from_graph(data)
        if isinstance(data, dict) and "mappings" in data:       # mappings_added.json
            data = data["mappings"]
        if isinstance(data, list) and data and "target_uri" in data[0]:
            terms_file = Path(terms) if terms else path.with_name("terms.json")
            terms_data = json.loads(terms_file.read_text(encoding="utf-8")) if terms_file.exists() else None
            return from_linker(data, terms_data)
        return from_records(data if isinstance(data, list) else [])
    if path.name.endswith(".sssom.tsv") or path.read_text(encoding="utf-8", errors="ignore").startswith("# curie_map"):
        return from_sssom(path.read_text(encoding="utf-8"))
    rows = _table_rows(path)
    if rows and any(str(c).startswith("VERDANT_") for c in rows[0]):
        return from_wide_table(rows)
    return from_records(rows)
