"""Build the aerobiome KG: ENA air samples -> BioSamples JSON -> fields -> ENVO, GOLD, EMPO, CHEBI.

1. Fetch the BioSamples record of each accession from air.ena (geoapp/fetch.py, cached).
2. Read the fields in SLOTS and MEASURED; each distinct value becomes a term.
3. Match each term:
   ENVO   from the record (archive IDs, IDs typed in the text), else an OLS4 search
   GOLD   through its ENVO term (curated GOLD-ENVO SSSOM), and by name in gold.obo
   EMPO   from empo_* fields, else air/aerosol evidence -> Aerosol
   CHEBI  chemical values and measured chemicals, by OLS4 search
4. Hierarchy: each sample's broad > local > medium path makes a context tree; a
   whole context is matched to the GOLD class with the same ENVO triad; matched
   GOLD and EMPO classes come with their parents.
Sources are listed in air/SOURCES.md.

Output: <data>/air/samples.json (the fields read), <data>/air/graph.json.

Run:  python -m air.build [--accessions data/air/accessions.txt] [--no-search]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from geoapp.ontolink import _is_placeholder, _norm, _singular, classify_hit, should_look_up
from geoapp.schema import curie_from_uri

from air import vocab

logger = logging.getLogger("air.build")

# Field groups, under the names submitters use for them.
SLOTS = {
    "env_broad_scale": ("env_broad_scale", "broad-scale environmental context", "env biome", "biome",
                        "environment (biome)"),
    "env_local_scale": ("env_local_scale", "local environmental context", "env feature", "environment (feature)"),
    "env_medium": ("env_medium", "environmental medium", "env_material", "env material", "environment (material)"),
    "isolation_source": ("isolation_source", "isolation source"),
    "empo": ("empo_1", "empo_2", "empo_3", "empo 1", "empo 2", "empo 3"),
    "gold_ecosystem": ("ecosystem", "ecosystem_category", "ecosystem_type", "ecosystem_subtype", "specific_ecosystem",
                       "ecosystem category", "ecosystem type", "ecosystem subtype", "specific ecosystem"),
    "chemistry": ("pollutants", "volatile_org_comp", "volatile organic compounds", "chem_administration",
                  "chemical administration"),
}
SLOT_LABELS = {"env_broad_scale": "Broad-scale environment", "env_local_scale": "Local environment",
               "env_medium": "Environmental medium", "isolation_source": "Isolation source",
               "empo": "EMPO category", "gold_ecosystem": "GOLD ecosystem", "chemistry": "Chemicals"}
FIELD_SLOT = {f: slot for slot, fields in SLOTS.items() for f in fields}
ENV_SLOTS = ("env_broad_scale", "env_local_scale", "env_medium", "isolation_source")
# MIxS measurement fields that name a chemical; the chemical is the term, not the number.
MEASURED = {"carb_dioxide": "carbon dioxide", "carbon dioxide": "carbon dioxide", "carb_monoxide": "carbon monoxide",
            "carbon monoxide": "carbon monoxide", "oxygen": "oxygen", "methane": "methane", "ozone": "ozone",
            "nitrogen dioxide": "nitrogen dioxide", "no2": "nitrogen dioxide", "sulfur dioxide": "sulfur dioxide",
            "so2": "sulfur dioxide", "ammonia": "ammonia"}
# ENVO search branch per field, as in geoapp (MIxS: broad scale is a biome, medium a material).
BRANCH = {"env_broad_scale": "biome", "env_medium": "material"}
# Ontologies a slot is expected to match; anything else is flagged for review.
EXPECTED = {**{s: {"ENVO", "GOLD", "EMPO"} for s in ENV_SLOTS},
            "empo": {"EMPO"}, "gold_ecosystem": {"GOLD"}, "chemistry": {"CHEBI"}, "context": {"GOLD"}}
# The MIxS triad, broadest first: each sample's path through it makes the tree.
TRIAD = ("env_broad_scale", "env_local_scale", "env_medium")
GOLD_MIXS = {"env_broad_scale": "env_broad", "env_local_scale": "env_local", "env_medium": "env_medium"}
ENVO_BIOME = "ENVO:00000428"   # GOLD gives the root "biome" as broad scale: any biome fits
MIN_CONFIDENCE = 0.70   # same cut as the main viewer's ontology manager
TRUSTED = 0.85          # an ENVO match this good may lead on to GOLD and EMPO
REVIEW_BELOW = 0.85
MAX_ACCESSIONS = 25

# GOLD-ENVO SSSOM predicate -> confidence of the step from ENVO to GOLD.
VIA_ENVO = {"exactMatch": 0.90, "closeMatch": 0.85, "broadMatch": 0.75, "narrowMatch": 0.75}
AIR_ENVO = {"ENVO:00002005", "ENVO:00010505", "ENVO:01000267"}   # air, aerosol, atmosphere
AIR_WORDS = {"air", "aerosol", "bioaerosol", "atmosphere", "atmospheric", "airborne"}
SALINE_WORDS = {"marine", "ocean", "seawater", "saline", "sea"}
EMPO_RULE = 0.75
OLS4_API = "https://www.ebi.ac.uk/ols4/api"

# An ontology ID typed into the text: "ENVO:00002005", "campus[ENVO_00000485]", "CHEBI:16526".
_OBO_ID = re.compile(r"\b([A-Za-z]+)[:_](\d{4,8})\b")
_BARE_ID = re.compile(r"\s*([A-Za-z]+)[:_](\d{4,8})\s*")


def _curie(prefix: str, num: str) -> str:
    return f"{prefix.upper()}:{num}"
_SPLIT = re.compile(r"[;|,]")


def _key(value: str) -> str:
    return " ".join(str(value).split()).lower()


def _slot(field: str) -> str | None:
    return FIELD_SLOT.get(_key(field or ""))


def _words(text: str) -> set[str]:
    return {_singular(w) for w in _norm(text).split()}


def _parts(slot: str, value: str) -> list[str]:
    """Chemical fields list several chemicals; other fields hold one value."""
    if slot != "chemistry":
        return [value]
    out = []
    for p in _SPLIT.split(value):
        p = p.strip()
        if p and not re.fullmatch(r"[\d\s.:TZ+-]+", p):   # drop dates and amounts
            out.append(p)
    return out


# ─── 1-2. Read the fields ────────────────────────────────────────────────────

def read_terms(samples: list[dict]) -> dict[tuple, dict]:
    """Distinct (slot, value) terms with their samples, field names and the ontology IDs the record gives."""
    terms: dict[tuple, dict] = {}
    spellings: dict[tuple, Counter] = defaultdict(Counter)

    def add(slot, text, field, acc, measured=False):
        key = (slot, _key(text))
        t = terms.setdefault(key, {"slot": slot, "samples": set(), "fields": Counter(), "record": {},
                                   "measured": measured})
        t["samples"].add(acc)
        t["fields"][field] += 1
        spellings[key][" ".join(str(text).split())] += 1
        return key

    for s in samples:
        acc = s.get("accession")
        by_value: dict[tuple, list[tuple]] = defaultdict(list)
        for field, value in (s.get("attributes") or {}).items():
            value = str(value)
            if not value.strip() or _is_placeholder(value):
                continue
            chem = MEASURED.get(_key(field))
            if chem:
                add("chemistry", chem, field, acc, measured=True)
                continue
            slot = _slot(field)
            if not slot:
                continue
            for part in _parts(slot, value):
                by_value[(_key(field), _key(value))].append(add(slot, part, field, acc))
        # IDs the archive attached to a field value belong to that value's terms.
        for m in s.get("ontology_terms") or []:
            if m.get("origin") != "biosamples":
                continue
            for key in by_value.get((_key(m.get("matched_field") or ""), _key(m.get("matched_value") or "")), []):
                terms[key]["record"].setdefault(m["uri"], {**m, "origin": "biosamples"})

    for key, t in terms.items():
        t["label"] = spellings[key].most_common(1)[0][0]
    return terms


# ─── 3. Match ────────────────────────────────────────────────────────────────

class GoldIndex:
    """gold.obo by name: classify_hit against the GOLD classes that share a word with the text."""

    def __init__(self, terms: list[dict]):
        self.terms = terms
        self.by_word: dict[str, set[int]] = defaultdict(set)
        for i, t in enumerate(terms):
            for name in [t["label"], *t["exact_synonyms"], *t["narrow_synonyms"], *t["broad_synonyms"]]:
                for w in _words(name):
                    self.by_word[w].add(i)

    def match(self, text: str, min_conf: float) -> list[dict]:
        cands = set().union(*(self.by_word.get(w, set()) for w in _words(text))) if text else set()
        hits = []
        for i in cands:
            t = self.terms[i]
            verdict = classify_hit(text, {"label": t["label"], **{k: t[k] for k in (
                "exact_synonyms", "narrow_synonyms", "broad_synonyms", "related_synonyms")}})
            if verdict and verdict[1] >= min_conf:
                hits.append((verdict, t))
        if not hits:
            return []
        best = max(h[0][1] for h in hits)
        top = [h for h in hits if h[0][1] == best]
        # The same name at several depths ("Air" under Environmental and under Built environment):
        # the shortest path is the general class; several equally short ones are ambiguous.
        depth = min(h[1]["path"].count(">") for h in top)
        top = [h for h in top if h[1]["path"].count(">") == depth]
        ambiguous = len(top) > 1
        return [{"id": t["id"], "kind": "GOLD", "label": t["path"], "curie": t["id"], "url": vocab.gold_url(t["id"]),
                 "match_type": "closeMatch" if ambiguous and mtype == "exactMatch" else mtype,
                 "confidence": conf, "origin": "gold_name"} for (mtype, conf), t in top]


def _ontology_term(m: dict) -> dict:
    uri = m["uri"]
    curie, source = curie_from_uri(uri)
    curie = m.get("curie") or curie or uri
    kind = m.get("source") or source or "other"
    return {"id": uri, "kind": kind, "label": m.get("label") or curie, "curie": curie, "url": uri,
            "match_type": m.get("match_type", "exactMatch"), "confidence": m.get("confidence", 1.0),
            "origin": m.get("origin")}


def _ids_in_text(text: str) -> list[dict]:
    out = []
    for prefix, num in _OBO_ID.findall(text):
        p = prefix.upper()
        uri = f"http://purl.obolibrary.org/obo/{p}_{num}"
        out.append({"id": uri, "kind": p, "label": f"{p}:{num}", "curie": f"{p}:{num}", "url": uri,
                    "match_type": "exactMatch", "confidence": 1.0, "origin": "id_in_text"})
    return out


def _from_hits(hits: list, min_conf: float, origin: str) -> list[dict]:
    """The best search hit, if it clears ``min_conf``."""
    best = hits[0] if hits else None
    if best is None or best["confidence"] < min_conf:
        return []
    return [_ontology_term({**best, "origin": origin})]


def search_query(t: dict) -> tuple[str, str, str] | None:
    """(text, ontology, branch) when the record leaves the term without its main ontology."""
    slot, text = t["slot"], t["label"]
    if _OBO_ID.search(text):
        return None
    if slot in ENV_SLOTS:
        ontology = "envo"
    elif slot == "chemistry":
        ontology = "chebi"
    else:
        return None
    if any(m.get("source") == ontology.upper() for m in t["record"].values()) or not should_look_up(slot, text, ontology):
        return None
    return text, ontology, BRANCH.get(slot, "")


def match_term(t: dict, gold: GoldIndex | None, gold_sssom: dict, search: Callable | None, min_conf: float) -> list[dict]:
    slot, text = t["slot"], t["label"]
    found = _ids_in_text(text) or [_ontology_term(m) for m in t["record"].values()]

    query = search_query(t) if search else None
    if query:
        found += _from_hits(search(*query), min_conf, "ols4")

    envo = [m for m in found if m["kind"] == "ENVO" and m["confidence"] >= TRUSTED]
    if slot in ENV_SLOTS or slot == "gold_ecosystem":
        for e in envo:
            for g in gold_sssom.get(e["curie"], []):
                found.append({"id": g["id"], "kind": "GOLD", "label": g["label"] or g["id"], "curie": g["id"],
                              "url": vocab.gold_url(g["id"]), "match_type": g["match_type"],
                              "confidence": min(e["confidence"], VIA_ENVO[g["match_type"]]),
                              "origin": "via_envo", "via": e["curie"]})
        if gold:
            found += gold.match(text, min_conf)

    if slot == "empo":
        for e in vocab.empo_terms():
            verdict = classify_hit(text, {"label": e["label"]})
            if verdict and verdict[1] >= min_conf:
                found.append(_empo(e, *verdict, "empo_field"))
    elif slot in ENV_SLOTS:
        words = _words(text)
        if words & AIR_WORDS or any(m["curie"] in AIR_ENVO for m in envo):
            label = "Aerosol (saline)" if words & SALINE_WORDS else "Aerosol (non-saline)"
            e = next(x for x in vocab.empo_terms() if x["label"] == label)
            found.append(_empo(e, "broadMatch", EMPO_RULE, "empo_rule"))

    best: dict[str, dict] = {}
    for m in found:   # one match per target: the most confident
        if m["id"] not in best or m["confidence"] > best[m["id"]]["confidence"]:
            best[m["id"]] = m
    return list(best.values())


def _empo(e: dict, match_type: str, conf: float, origin: str) -> dict:
    return {"id": e["id"], "kind": "EMPO", "label": e["label"], "curie": e["envo"] or e["id"], "url": e["url"],
            "match_type": match_type, "confidence": conf, "origin": origin, "level": e["level"]}


# ─── Hierarchy ───────────────────────────────────────────────────────────────

def _tid(key: tuple) -> str:
    return f"air:{key[0]}:{key[1]}"


def sample_paths(terms: dict[tuple, dict]) -> dict[str, list[tuple]]:
    """Each sample's path through the triad: its broad, local and medium terms, those it gives.
    A sample with none of them is placed by its isolation source. A sample with two
    values in one field takes the one more samples use."""
    by_sample: dict[str, dict[str, list[tuple]]] = defaultdict(lambda: defaultdict(list))
    for key, t in terms.items():
        if key[0] in TRIAD or key[0] == "isolation_source":
            for acc in t["samples"]:
                by_sample[acc][key[0]].append(key)
    pick = lambda keys: max(keys, key=lambda k: (len(terms[k]["samples"]), k))  # noqa: E731
    out = {}
    for acc, slots in by_sample.items():
        path = [pick(slots[s]) for s in TRIAD if slots.get(s)]
        out[acc] = path or [pick(slots["isolation_source"])]
    return out


def build_tree(terms: dict[tuple, dict], paths: dict[str, list[tuple]]) -> dict[str, dict]:
    """Context tree: broad > local > medium, with the samples and terms below each node."""
    sample_terms: dict[str, set] = defaultdict(set)
    for key, t in terms.items():
        for acc in t["samples"]:
            sample_terms[acc].add(_tid(key))
    tree: dict[str, dict] = {}
    for acc, path in paths.items():
        for i in range(1, len(path) + 1):
            nid = "ctx:" + " > ".join(f"{s}={v}" for s, v in path[:i])
            n = tree.setdefault(nid, {"id": nid, "kind": "context", "label": terms[path[i - 1]]["label"],
                                      "path": [terms[k]["label"] for k in path[:i]], "slot": path[i - 1][0],
                                      "term": _tid(path[i - 1]), "level": i - 1, "samples": set(), "terms": set(),
                                      "parent": "ctx:" + " > ".join(f"{s}={v}" for s, v in path[:i - 1]) if i > 1 else None})
            n["samples"].add(acc)
            n["terms"] |= sample_terms[acc]
    return tree


def gold_for_context(path: list[tuple], envo: dict[tuple, set], gold_terms: list[dict]) -> list[dict]:
    """GOLD classes whose own broad / local / medium ENVO terms fit the context's.

    Every term the class names must be matched by the context (so Indoor Air
    needs a local term that says so), the medium among them, and at least two.
    The class naming the most wins; among those, the shallowest path.
    """
    given = {k[0]: envo.get(k, set()) for k in path}
    if not given.get("env_medium"):
        return []
    scored = []
    for g in gold_terms:
        named = {slot: g["mixs"][prop] for slot, prop in GOLD_MIXS.items() if g["mixs"].get(prop)}
        if "env_medium" not in named or len(named) < 2:
            continue
        if all(v in given.get(slot, ()) or (slot == "env_broad_scale" and v == ENVO_BIOME and slot in given)
               for slot, v in named.items()):
            scored.append((len(named), g["path"].count(">"), g))
    if not scored:
        return []
    best = max(s for s, _, _ in scored)
    depth = min(d for s, d, _ in scored if s == best)
    return [{"id": g["id"], "kind": "GOLD", "label": g["path"], "curie": g["id"], "url": vocab.gold_url(g["id"]),
             "match_type": "closeMatch", "confidence": 0.85 if best == 3 else 0.75, "origin": "gold_triad"}
            for s, d, g in scored if s == best and d == depth]


def add_ancestors(nodes: dict[str, dict], edges: list[dict], gold_terms: list[dict]) -> None:
    """is_a chains above every matched GOLD and EMPO class, from gold.obo and the EMPO table."""
    gold = {g["id"]: g for g in gold_terms}
    empo = {e["id"]: e for e in vocab.empo_terms()}
    todo = [n["id"] for n in nodes.values() if n["kind"] in ("GOLD", "EMPO")]
    seen = set()
    while todo:
        nid = todo.pop()
        if nid in seen:
            continue
        seen.add(nid)
        if nid in gold:
            parents = [(p, "GOLD", gold[p]["path"], vocab.gold_url(p)) for p in gold[nid]["parents"] if p in gold]
        elif nid in empo and empo[nid]["parent"]:
            p = empo[empo[nid]["parent"]]
            parents = [(p["id"], "EMPO", p["label"], p["url"])]
        else:
            parents = []
        for pid, kind, label, url in parents:
            nodes.setdefault(pid, {"id": pid, "kind": kind, "label": label, "curie": pid if kind == "GOLD" else (
                empo[pid]["envo"] or pid), "url": url, "ancestor": True})
            edges.append({"source": nid, "target": pid, "kind": "is_a"})
            todo.append(pid)


# ─── Graph ───────────────────────────────────────────────────────────────────

def build_graph(samples: list[dict], gold_terms: list[dict] | None = None, gold_sssom: dict | None = None,
                search: Callable | None = None, min_conf: float = MIN_CONFIDENCE) -> dict:
    """Term nodes in their broad > local > medium hierarchy, ontology nodes with their parents,
    and reviewable match edges from terms and from whole contexts."""
    gold_terms = gold_terms or []
    terms = read_terms(samples)
    gold = GoldIndex(gold_terms) if gold_terms else None
    if search and hasattr(search, "prefetch"):
        search.prefetch({q for q in map(search_query, terms.values()) if q})
    # Names for the IDs submitters typed ("ENVO:00000428" is "biome").
    typed = {f"{p.upper()}:{n}" for t in terms.values() for p, n in _OBO_ID.findall(t["label"])}
    names = search.names(typed) if search and hasattr(search, "names") else {}

    nodes: dict[str, dict] = {}
    edges = []
    envo: dict[tuple, set] = {}   # term -> its trusted ENVO CURIEs

    for key, t in sorted(terms.items(), key=lambda kv: -len(kv[1]["samples"])):
        slot = key[0]
        tid = _tid(key)
        nodes[tid] = {"id": tid, "kind": "air", "label": t["label"], "slot": slot, "samples": len(t["samples"]),
                      "accessions": sorted(a for a in t["samples"] if a)[:MAX_ACCESSIONS],
                      "fields": dict(t["fields"]), "measured": t["measured"]}
        found = match_term(t, gold, gold_sssom or {}, search, min_conf)
        envo[key] = {m["curie"] for m in found if m["kind"] == "ENVO" and m["confidence"] >= TRUSTED}
        bare = _BARE_ID.fullmatch(t["label"])
        if bare and names.get(_curie(*bare.groups())):
            t["label"] = nodes[tid]["label"] = f"{names[_curie(*bare.groups())]} ({_curie(*bare.groups())})"
        for m in found:
            if m["origin"] == "id_in_text" and names.get(m["curie"]):
                m["label"] = names[m["curie"]]
            node = nodes.setdefault(m["id"], {"id": m["id"], "kind": m["kind"], "label": m["label"],
                                              "curie": m["curie"], "url": m["url"]})
            if node["label"] == node["curie"] and m["label"] != m["curie"]:
                node["label"] = m["label"]   # an ID typed in the text gets a name once a search finds it
            edges.append({"source": tid, "target": m["id"], "kind": "match", "match": f"skos:{m['match_type']}",
                          "confidence": m["confidence"], "origin": m["origin"], "via": m.get("via"),
                          "review": m["confidence"] < REVIEW_BELOW or m["kind"] not in EXPECTED[slot]})

    # Hierarchy 1: the samples' own broad > local > medium paths.
    paths = sample_paths(terms)
    within = Counter((_tid(a), _tid(b)) for path in paths.values() for a, b in zip(path, path[1:]))
    edges += [{"source": a, "target": b, "kind": "within", "samples": n} for (a, b), n in within.items()]
    tree = build_tree(terms, paths)

    # A whole context (urban biome > atmosphere > air) can name a specific GOLD class.
    path_of = {"ctx:" + " > ".join(f"{s}={v}" for s, v in p[:i]): p[:i] for p in paths.values() for i in range(1, len(p) + 1)}
    for cid, c in tree.items():
        c["gold"] = gold_for_context(path_of[cid], envo, gold_terms) if c["slot"] == "env_medium" else []
        if not c["gold"]:
            continue
        nodes[cid] = {"id": cid, "kind": "context", "label": " › ".join(c["path"]), "slot": "context",
                      "samples": len(c["samples"]), "accessions": sorted(c["samples"])[:MAX_ACCESSIONS], "term": c["term"]}
        edges.append({"source": cid, "target": c["term"], "kind": "context_of"})
        for m in c["gold"]:
            nodes.setdefault(m["id"], {"id": m["id"], "kind": "GOLD", "label": m["label"], "curie": m["curie"], "url": m["url"]})
            edges.append({"source": cid, "target": m["id"], "kind": "match", "match": f"skos:{m['match_type']}",
                          "confidence": m["confidence"], "origin": m["origin"], "review": m["confidence"] < REVIEW_BELOW})

    # Hierarchy 2: the matched GOLD and EMPO classes inside their own trees.
    add_ancestors(nodes, edges, gold_terms)

    air = [n for n in nodes.values() if n["kind"] == "air"]
    matches = [e for e in edges if e["kind"] == "match" and nodes[e["source"]]["kind"] == "air"]
    matched_by = {k: len({e["source"] for e in matches if nodes[e["target"]]["kind"] == k})
                  for k in ("ENVO", "GOLD", "EMPO", "CHEBI")}
    meta = {"samples": len(samples), "fetch_errors": sum(1 for s in samples if s.get("fetch_error")),
            "terms": len(air), "matched_terms": len({e["source"] for e in matches}),
            "matches": sum(1 for e in edges if e["kind"] == "match"), "terms_matched_to": matched_by,
            "contexts": len(tree), "contexts_matched_to_gold": sum(1 for c in tree.values() if c["gold"]),
            "per_slot": {s: sum(1 for n in air if n["slot"] == s) for s in SLOTS}}
    tree_out = [{**{k: v for k, v in c.items() if k not in ("samples", "terms", "gold")},
                 "samples": len(c["samples"]), "accessions": sorted(c["samples"])[:MAX_ACCESSIONS],
                 "terms": sorted(c["terms"]), "gold": [m["id"] for m in c["gold"]]}
                for c in sorted(tree.values(), key=lambda c: (c["level"], -len(c["samples"]), c["label"]))]
    return {"meta": meta, "nodes": list(nodes.values()), "edges": edges, "tree": tree_out}


# ─── OLS4 search, cached ─────────────────────────────────────────────────────

class OLS4Search:
    """geoapp's calibrated OLS4 matcher with an on-disk cache; ``prefetch`` runs the lookups in parallel."""

    def __init__(self, cache_file: Path, workers: int = 4):
        import requests
        self.cache_file, self.workers = cache_file, workers
        try:
            self.cache = json.loads(cache_file.read_text())
        except (OSError, ValueError):
            self.cache = {}
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.lock = threading.Lock()

    def __call__(self, text: str, ontology: str, branch: str = "") -> list[dict]:
        key = f"{ontology}|{branch}|{text}"
        if key not in self.cache:
            from geoapp.ontolink import ols4_search
            hits = [h.to_dict() for h in ols4_search(text, ontology, self.session, branch)]
            with self.lock:
                self.cache[key] = hits
        return self.cache[key]

    def names(self, curies) -> dict[str, str]:
        """Labels for ontology IDs, from OLS4 (the defining ontology's entry); cached."""
        def look_up(curie):
            try:
                resp = self.session.get(f"{OLS4_API}/terms", params={"obo_id": curie}, timeout=30)
                resp.raise_for_status()
                docs = (resp.json().get("_embedded") or {}).get("terms") or []
            except Exception:
                logger.warning("OLS4 has no name for %s", curie)
                return
            own = [d for d in docs if d.get("ontology_name") == curie.split(":")[0].lower()] or docs
            with self.lock:
                self.cache["id|" + curie] = own[0].get("label", "") if own else ""

        todo = [c for c in curies if "id|" + c not in self.cache]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(look_up, todo))
        if todo:
            self.save()
        return {c: self.cache.get("id|" + c, "") for c in curies}

    def prefetch(self, queries) -> None:
        todo = [q for q in queries if f"{q[1]}|{q[2]}|{q[0]}" not in self.cache]
        logger.info("Searching OLS4: %d new lookups (%d cached)", len(todo), len(queries) - len(todo))
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(lambda q: self(*q), todo))
        self.save()

    def save(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(self.cache))


# ─── Run ─────────────────────────────────────────────────────────────────────

def fetch_samples(accessions_file: Path) -> list[dict]:
    from geoapp import fetch
    accs = fetch.load_accessions(accessions_file)
    logger.info("Fetching %d BioSamples records (cached after the first run)", len(accs))
    step = max(1, len(accs) // 20)
    progress = lambda done, total, acc: done % step == 0 and logger.info("  %d / %d", done, total)  # noqa: E731
    return [s.to_dict() for s in fetch.fetch_all(accs, progress=progress)]


def run(data_dir: Path, accessions_file: Path, search: bool = True, min_conf: float = MIN_CONFIDENCE) -> dict:
    out = data_dir / "air"
    samples = fetch_samples(accessions_file)
    try:
        gold_terms, gold_sssom = vocab.load_gold(out / "vocab")
    except Exception:
        logger.exception("Could not load GOLD; building without it")
        gold_terms, gold_sssom = [], {}
    searcher = OLS4Search(out / "cache" / "ols4.json") if search else None
    graph = build_graph(samples, gold_terms, gold_sssom, searcher, min_conf)
    graph["meta"].update({"built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "accessions": str(accessions_file), "min_confidence": min_conf, "searched": search,
                          "gold_classes": len(gold_terms)})
    read = [{"accession": s.get("accession"), "error": s.get("fetch_error") or None,
             "fields": {k: v for k, v in (s.get("attributes") or {}).items() if _slot(k) or _key(k) in MEASURED}}
            for s in samples]
    out.mkdir(parents=True, exist_ok=True)
    (out / "samples.json").write_text(json.dumps(read, ensure_ascii=False, indent=1))
    (out / "graph.json").write_text(json.dumps(graph, ensure_ascii=False))
    m = graph["meta"]
    logger.info("Wrote %d terms from %d samples (%d fetch errors); terms matched to %s", m["terms"], m["samples"],
                m["fetch_errors"], m["terms_matched_to"])
    return graph


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    data = Path(os.environ.get("AIR_DATA_DIR") or os.environ.get("DATA_DIR", "data"))
    ap.add_argument("--data-dir", type=Path, default=data)
    ap.add_argument("--accessions", type=Path, help="default: <data>/air/accessions.txt from python -m air.ena")
    ap.add_argument("--no-search", action="store_true", help="skip the OLS4 searches for ENVO and CHEBI")
    ap.add_argument("--min-confidence", type=float, default=MIN_CONFIDENCE)
    args = ap.parse_args()
    run(args.data_dir, args.accessions or args.data_dir / "air" / "accessions.txt", not args.no_search, args.min_confidence)


if __name__ == "__main__":
    main()
