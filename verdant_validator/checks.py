"""Automatic checks on predicted mappings, run before any expert looks at them.

Each check adds findings to a mapping's ``checks`` list: ``{"code", "level", "message"}``
with level ``error`` (the mapping cannot be right as stated), ``warning`` (look first)
or ``info``. Experts see them on the review page, and mappings with errors or warnings
come first in "Next to review".

Offline checks (always):
  predicate       the match type is one of the five SKOS mapping types
  confidence      a number between 0 and 1
  identifier      the object is a URI or a CURIE, not a bare label
  conflict        the same pair was given two different match types
  many_exact      one subject has several exactMatch links into the same source
  label           an exactMatch whose subject and object labels share no word

Online checks (``online=True``; answers cached on disk):
  ols             OBO terms (ENVO, NCBITaxon, OBI, ...): the term exists in OLS4, is not
                  obsolete, and its label is the one the mapping shows
  gbif            GBIF species IDs: the canonical name GBIF gives, against the subject's name
  wikidata        Wikidata items: the English label and taxon name, against the subject's name
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import requests

from verdant_validator.mappings import MATCH_TYPES

logger = logging.getLogger("verdant_validator.checks")

CACHE = Path(os.environ.get("VERDANT_VALIDATOR_CACHE", Path.home() / ".cache" / "verdant-validator"))
UA = {"User-Agent": "verdant-validator/0.1 (AAU Bio Collaboration)"}
OBO = "http://purl.obolibrary.org/obo/"


def _finding(m: dict, code: str, level: str, message: str) -> None:
    m.setdefault("checks", []).append({"code": code, "level": level, "message": message})


def _words(text) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(w) > 2 and w not in {"the", "and", "sp", "spp"}}


def _similar(a, b) -> bool:
    if not a or not b:
        return True       # nothing to compare
    a, b = str(a).lower().strip(), str(b).lower().strip()
    return bool(_words(a) & _words(b)) or SequenceMatcher(None, a, b).ratio() >= 0.75


def check_offline(maps: list[dict]) -> None:
    exact_by_source: dict[tuple, list[dict]] = defaultdict(list)
    for m in maps:
        p = m.get("predicate_id")
        if p not in MATCH_TYPES:
            _finding(m, "predicate", "error", f"“{p}” is not a SKOS mapping type ({', '.join(t[5:] for t in MATCH_TYPES)}).")
        c = m.get("confidence")
        if c is not None and not 0 <= c <= 1:
            _finding(m, "confidence", "error", f"Confidence {c} is outside 0–1.")
        o = m["object_id"]
        if not (o.startswith(("http://", "https://")) or re.match(r"^[A-Za-z][\w.-]*:\S+$", o)):
            _finding(m, "identifier", "warning", "The object is a label, not an identifier; it cannot be looked up.")
        others = m.get("context", {}).get("other_predicates")
        if others:
            _finding(m, "conflict", "error", f"The same pair is also given as {', '.join(others)}.")
        if p == "skos:exactMatch":
            exact_by_source[(m["subject_id"], m.get("object_source") or _source_of(o))].append(m)
            if m.get("object_label") and not _similar(m["subject_label"], m["object_label"]):
                _finding(m, "label", "warning",
                         f"Exact match, but “{m['subject_label']}” and “{m['object_label']}” share no word.")
    for (subject, source), group in exact_by_source.items():
        if len(group) > 1:
            for m in group:
                _finding(m, "many_exact", "warning",
                         f"{len(group)} exact matches for “{m['subject_label']}” in {source}; an exact match should be unique.")


def _source_of(uri: str) -> str:
    if uri.startswith(OBO):
        return uri[len(OBO):].split("_")[0]
    if "gbif.org" in uri:
        return "GBIF"
    if "wikidata.org" in uri:
        return "Wikidata"
    return uri.split("/")[2] if uri.startswith("http") else uri.split(":")[0]


class _Cache:
    def __init__(self, name: str):
        self.path = CACHE / f"{name}.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, key, fetch):
        if key not in self.data:
            value = fetch()
            if value is None:
                return None          # failed: not cached, tried again next time
            self.data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))
        return self.data[key]


def _json(url, **params):
    try:
        r = requests.get(url, params=params, headers=UA, timeout=30)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Lookup failed for %s: %s", url, exc)
        return None


def _ols_term(iri: str) -> dict | None:
    data = _json("https://www.ebi.ac.uk/ols4/api/terms", iri=iri, size=20)
    if data is None:
        return None
    terms = (data.get("_embedded") or {}).get("terms") or []
    own = [t for t in terms if t.get("is_defining_ontology")] or terms
    if not own:
        return {"found": False}
    t = own[0]
    return {"found": True, "label": t.get("label"), "obsolete": bool(t.get("is_obsolete")),
            "replaced_by": t.get("term_replaced_by"), "synonyms": t.get("synonyms") or []}


def _gbif(uri: str) -> dict | None:
    m = re.search(r"gbif\.org/species/(\d+)", uri)
    if not m:
        return {}
    data = _json(f"https://api.gbif.org/v1/species/{m.group(1)}")
    if data is None:
        return None
    return {"found": bool(data), "name": data.get("canonicalName") or data.get("scientificName"),
            "rank": data.get("rank"), "status": data.get("taxonomicStatus"), "accepted": data.get("accepted")}


def _wikidata(uri: str) -> dict | None:
    m = re.search(r"(Q\d+)$", uri)
    if not m:
        return {}
    data = _json("https://www.wikidata.org/wiki/Special:EntityData/" + m.group(1) + ".json")
    if data is None:
        return None
    e = (data.get("entities") or {}).get(m.group(1)) or {}
    taxon = [c["mainsnak"].get("datavalue", {}).get("value") for c in (e.get("claims") or {}).get("P225", [])]
    return {"found": bool(e), "label": ((e.get("labels") or {}).get("en") or {}).get("value"),
            "taxon_name": next((t for t in taxon if t), None)}


def check_online(maps: list[dict]) -> None:
    ols, gbif, wd = _Cache("ols_terms"), _Cache("gbif_species"), _Cache("wikidata_items")
    for m in maps:
        o = m["object_id"]
        if o.startswith(OBO):
            t = ols.get(o, lambda: _ols_term(o))
            if t is None:
                continue
            if not t["found"]:
                _finding(m, "ols", "error", "OLS4 does not know this term.")
                continue
            if t["obsolete"]:
                _finding(m, "ols", "error", "The term is obsolete" + (f"; replaced by {t['replaced_by']}" if t.get("replaced_by") else "") + ".")
            names = [t["label"], *t.get("synonyms", [])]
            if m.get("object_label") and not any(_similar(m["object_label"], n) for n in names if n):
                _finding(m, "ols", "warning", f"The mapping says “{m['object_label']}”, OLS4 calls the term “{t['label']}”.")
            m.setdefault("verified", {})["label"] = t["label"]
        elif "gbif.org/species/" in o:
            g = gbif.get(o, lambda: _gbif(o))
            if not g:
                continue
            if not g.get("found"):
                _finding(m, "gbif", "error", "GBIF does not know this species ID.")
                continue
            m.setdefault("verified", {})["label"] = g["name"]
            if g.get("status") and g["status"] != "ACCEPTED":
                _finding(m, "gbif", "warning", f"GBIF lists “{g['name']}” as {g['status'].lower()}"
                         + (f", accepted name {g['accepted']}" if g.get("accepted") else "") + ".")
            if m.get("predicate_id") == "skos:exactMatch" and not _same_taxon(m["subject_label"], g["name"]):
                _finding(m, "gbif", "warning", f"Exact match, but GBIF's name is “{g['name']}” ({(g.get('rank') or '').lower()}).")
        elif "wikidata.org/entity/" in o:
            w = wd.get(o, lambda: _wikidata(o))
            if not w:
                continue
            if not w.get("found"):
                _finding(m, "wikidata", "error", "Wikidata does not have this item.")
                continue
            name = w.get("taxon_name") or w.get("label")
            m.setdefault("verified", {})["label"] = name
            if m.get("predicate_id") == "skos:exactMatch" and name and not (_same_taxon(m["subject_label"], name)
                                                                             or _similar(m["subject_label"], w.get("label"))):
                _finding(m, "wikidata", "warning", f"Exact match, but Wikidata calls it “{name}”.")


def _same_taxon(a, b) -> bool:
    """Latin names equal once 'sp.' and form words are set aside: 'Elphidium sp.' ~ 'Elphidium'."""
    clean = lambda s: " ".join(w for w in re.findall(r"[a-z]+", str(s or "").lower()) if w not in {"sp", "spp", "cf", "aff"})
    a, b = clean(a), clean(b)
    return bool(a) and bool(b) and (a == b or a.startswith(b + " ") or b.startswith(a + " "))


def check(maps: list[dict], online: bool = False) -> dict:
    """Run the checks on ``maps`` (findings are added to each mapping) and return a summary."""
    for m in maps:
        m["checks"] = []
    check_offline(maps)
    if online:
        check_online(maps)
    levels = Counter(f["level"] for m in maps for f in m["checks"])
    codes = Counter(f["code"] for m in maps for f in m["checks"] if f["level"] != "info")
    return {"mappings": len(maps), "with_errors": sum(any(f["level"] == "error" for f in m["checks"]) for m in maps),
            "with_warnings": sum(any(f["level"] == "warning" for f in m["checks"]) for m in maps),
            "findings": dict(levels), "by_check": dict(codes), "online": online}
