"""
Step 3 — Link to External Ontologies

Links Natura 2000 terms to Wikidata, NCBITaxon, GBIF, EUNIS, DBpedia and ENVO.

Exact identifiers first, search second:
  * Habitats   — Wikidata items carry the Annex I code (P6436) and their EUNIS ID (P6681),
                 so habitats are matched by code, not by name.
  * Species    — Wikidata taxon name (P225) gives the item plus its NCBI taxonomy ID (P685)
                 and GBIF ID (P846) in one bulk query. OLS4 is only asked, with an exact
                 search, for species Wikidata does not know.
  * DBpedia    — direct resource URIs for species, checked in bulk.
  * ENVO       — OLS4 search for habitats. Hits are scored on label and synonyms; weak
                 hits are kept as candidates flagged for expert review, never as matches.
  * Statuses   — the IUCN category items on Wikidata.

Input:  {data_dir}/terms.json
Output: {data_dir}/direct_mappings.json   one dict per mapping
        {data_dir}/envo_candidates.json   ENVO classes seen for each habitat (used by step 4)
"""

import hashlib
import json
import logging
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests

logger = logging.getLogger("natura2000.linker")

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
DBPEDIA_SPARQL = "https://dbpedia.org/sparql"
OLS4_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"
GBIF_MATCH = "https://api.gbif.org/v1/species/match"

USER_AGENT = "Natura2000OntologyLinker/1.1 (https://github.com/dobrevajovana/natura2000-ontology-linker)"
API_RATE_LIMIT = float(os.environ.get("API_RATE_LIMIT", "1.0"))
GBIF_RATE_LIMIT = float(os.environ.get("GBIF_RATE_LIMIT", "0.2"))   # GBIF's API is built for bulk use
BATCH = 50

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# On-disk cache of API answers, so a re-run only asks what it has not asked before.
# Set by run_linker() to {data_dir}/cache/linker; entries older than LINKER_CACHE_DAYS are refetched.
CACHE_DIR: Path | None = None
CACHE_DAYS = float(os.environ.get("LINKER_CACHE_DAYS", "30"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_label(term: dict) -> str:
    """Best search label: the binomial for species, the name without its EU code otherwise."""
    if term.get("scientific_name"):
        return term["scientific_name"]
    label = term.get("label", "")
    label = re.sub(r"^\d[0-9A-Z]{3}\s*\*?\s*", "", label).strip()     # "1150 * Coastal lagoons", "91E0 * Alluvial forests"
    label = re.sub(r"\s*\(.*?\)\s*$", "", label).strip()               # "Otter (Lutra lutra)" -> "Otter"
    return label or term.get("label", "")


def _singular(text: str) -> str:
    """Naive English singular of the last word: 'Estuaries' -> 'Estuary', 'Reefs' -> 'Reef'."""
    head, _, last = text.rpartition(" ")
    low = last.lower()
    if not low.endswith(("ss", "us", "is")):        # "grass", and Latin like "Alnus", stay as they are
        for plural, single in (("ies", "y"), ("ches", "ch"), ("shes", "sh"), ("s", "")):
            if low.endswith(plural):
                last = last[: -len(plural)] + single
                break
    return f"{head} {last}".strip()


def string_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _mapping(term: dict, uri: str, label: str, match_type: str, confidence: float,
             ontology: str, method: str, **extra) -> dict:
    m = {
        "source_id": term["id"],
        "source_label": term.get("label", ""),
        "target_uri": uri,
        "target_label": label,
        "match_type": match_type,
        "confidence": round(confidence, 4),
        "source_ontology": ontology,
        "method": method,
    }
    m.update({k: v for k, v in extra.items() if v not in (None, "")})
    return m


def _get_json(url: str, *, params: dict | None = None, data: dict | None = None,
              headers: dict | None = None, retries: int = 3, pause: float = API_RATE_LIMIT) -> dict | None:
    """GET (or POST when `data` is given) with cache, retries and backoff; None after the last failure.

    `pause` is the politeness delay after a real network call; cache hits return immediately.
    """
    cache_file = None
    if CACHE_DIR is not None:
        key = hashlib.sha1(json.dumps([url, params, data], sort_keys=True).encode()).hexdigest()
        cache_file = CACHE_DIR / key[:2] / f"{key}.json"
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CACHE_DAYS * 86400:
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except ValueError:
                pass
    for attempt in range(retries):
        try:
            if data is not None:
                resp = SESSION.post(url, data=data, headers=headers, timeout=60)
            else:
                resp = SESSION.get(url, params=params, headers=headers, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            result = resp.json()
            if cache_file is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(result), encoding="utf-8")
            time.sleep(pause)
            return result
        except Exception as exc:
            wait = 2 ** attempt * 3
            logger.warning("Request to %s failed (%s); attempt %d/%d, waiting %ds",
                           url.split("?")[0], exc, attempt + 1, retries, wait)
            time.sleep(wait)
    return None


def _sparql(endpoint: str, query: str) -> list[dict]:
    res = _get_json(endpoint, data={"query": query},
                    headers={"Accept": "application/sparql-results+json"})
    return res.get("results", {}).get("bindings", []) if res else []


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Habitats: Wikidata by Annex I code, EUNIS from the same items
# ---------------------------------------------------------------------------

def link_habitats_wikidata(habitats: list[dict]) -> list[dict]:
    rows = _sparql(WIKIDATA_SPARQL, """
SELECT ?item ?code ?eunis ?itemLabel WHERE {
  ?item wdt:P6436 ?code .
  OPTIONAL { ?item wdt:P6681 ?eunis }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}""")
    by_code: dict[str, list[dict]] = {}
    for r in rows:
        by_code.setdefault(r["code"]["value"].strip().rstrip("*").strip(), []).append(r)
    logger.info("Wikidata: %d habitat items carry an Annex I code (P6436)", len(rows))

    out = []
    for h in habitats:
        for code in h.get("codes") or []:
            for r in by_code.get(str(code), []):
                out.append(_mapping(h, r["item"]["value"], r["itemLabel"]["value"], "skos:exactMatch", 1.0,
                                    "wikidata", "wikidata_p6436_code"))
                if r.get("eunis"):
                    eunis = r["eunis"]["value"]
                    out.append(_mapping(h, f"https://eunis.eea.europa.eu/habitats/{eunis}", f"EUNIS habitat {eunis}",
                                        "skos:exactMatch", 1.0, "eunis", "wikidata_p6681_eunis"))
    return out


# ---------------------------------------------------------------------------
# Species: Wikidata by taxon name, with NCBI and GBIF identifiers
# ---------------------------------------------------------------------------

def match_species_gbif(species: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """GBIF backbone match per species: a GBIF link, plus the accepted name when the listed one is a synonym."""
    out, accepted = [], {}
    for s in species:
        name = s.get("scientific_name")
        if not name:
            continue
        d = _get_json(GBIF_MATCH, params={"name": name, "strict": "true"}, pause=GBIF_RATE_LIMIT) or {}
        if d.get("matchType") not in ("EXACT", "HIGHERRANK") or not d.get("usageKey"):
            continue
        exact = d["matchType"] == "EXACT"
        note = None
        if d.get("status") == "SYNONYM" and d.get("species") and d["species"] != name:
            accepted[s["id"]] = d["species"]
            note = f"synonym of {d['species']}"
        out.append(_mapping(s, f"https://www.gbif.org/species/{d['usageKey']}", d.get("canonicalName", name),
                            "skos:exactMatch" if exact else "skos:closeMatch", 1.0 if exact else 0.7,
                            "gbif", "gbif_backbone_match", note=note, review=None if exact else True))
    logger.info("GBIF: %d of %d species matched in the backbone; %d listed under an older name",
                len(out), len(species), len(accepted))
    return out, accepted


def _wikidata_taxa(names: list[str]) -> dict[str, list[dict]]:
    hits: dict[str, list[dict]] = {}
    for batch in _chunks(names, BATCH):
        values = " ".join(_literal(n) for n in batch)
        for r in _sparql(WIKIDATA_SPARQL, f"""
SELECT ?name ?item ?ncbi ?gbif WHERE {{
  VALUES ?name {{ {values} }}
  ?item wdt:P225 ?name .
  OPTIONAL {{ ?item wdt:P685 ?ncbi }}
  OPTIONAL {{ ?item wdt:P846 ?gbif }}
}}"""):
            hits.setdefault(r["name"]["value"], []).append(r)
    return hits


def link_species_wikidata(species: list[dict], accepted: dict[str, str] | None = None,
                          have_gbif: set[str] | None = None) -> tuple[list[dict], set[str]]:
    """Wikidata item, NCBI taxon and (if GBIF matching missed it) GBIF ID by taxon name.

    Species not found under their listed name are retried under GBIF's accepted name.
    Returns mappings and the ids of species that received an NCBI taxon this way.
    """
    accepted, have_gbif = accepted or {}, have_gbif or set()
    by_name = {s["scientific_name"]: s for s in species if s.get("scientific_name")}
    hits = _wikidata_taxa(list(by_name))
    via_accepted = {accepted[s["id"]]: s for n, s in by_name.items() if n not in hits and s["id"] in accepted}
    for name, rows in _wikidata_taxa(list(via_accepted)).items():
        hits[name] = rows
        by_name[name] = via_accepted[name]

    out, with_ncbi = [], set()
    for name, rows in hits.items():
        term = by_name[name]
        note = f"via accepted name {name}" if name in via_accepted else None
        # one item per taxon name is the rule; homonyms across kingdoms are flagged, preferring items with an NCBI id
        items = {r["item"]["value"]: r for r in rows}
        best = [r for r in items.values() if r.get("ncbi")] or list(items.values())
        ambiguous = len(best) > 1
        for r in best:
            conf, mtype = (0.8, "skos:closeMatch") if ambiguous else (1.0, "skos:exactMatch")
            out.append(_mapping(term, r["item"]["value"], name, mtype, conf, "wikidata", "wikidata_p225_taxon_name",
                                review=ambiguous or None, note=note))
            if r.get("ncbi"):
                ncbi = r["ncbi"]["value"]
                out.append(_mapping(term, f"http://purl.obolibrary.org/obo/NCBITaxon_{ncbi}", name, mtype, conf,
                                    "ols4_ncbitaxon", "wikidata_p685_ncbi", review=ambiguous or None, note=note))
                with_ncbi.add(term["id"])
            if r.get("gbif") and term["id"] not in have_gbif:   # GBIF's own match is newer than Wikidata's pre-2026 IDs
                gbif = r["gbif"]["value"]
                out.append(_mapping(term, f"https://www.gbif.org/species/{gbif}", name, mtype, conf,
                                    "gbif", "wikidata_p846_gbif", review=ambiguous or None))
    logger.info("Wikidata: %d of %d species found by taxon name (P225, %d via accepted name); %d with an NCBI taxon",
                len(hits), len(species), len(set(via_accepted) & set(hits)), len(with_ncbi))
    return out, with_ncbi


def link_species_ols4_exact(species: list[dict]) -> list[dict]:
    """NCBITaxon by exact name, only for species Wikidata could not resolve. Never fuzzy."""
    out = []
    for s in species:
        name = s.get("scientific_name")
        if not name:
            continue
        res = _get_json(OLS4_SEARCH, params={"q": name, "ontology": "ncbitaxon", "exact": "true",
                                             "type": "class", "rows": 5, "fieldList": "iri,label,obo_id"})
        for doc in (res or {}).get("response", {}).get("docs", []):
            if doc.get("label", "").lower() == name.lower():
                out.append(_mapping(s, doc["iri"], doc["label"], "skos:exactMatch", 0.95, "ols4_ncbitaxon", "ols4_exact_name"))
                break
    return out


def link_species_dbpedia(species: list[dict]) -> list[dict]:
    """dbr:Genus_species resources, checked in bulk; keeps only URIs DBpedia actually has."""
    by_uri = {f"http://dbpedia.org/resource/{s['scientific_name'].replace(' ', '_')}": s
              for s in species if s.get("scientific_name")}
    found: set[str] = set()
    for batch in _chunks(list(by_uri), BATCH):
        values = " ".join(f"<{u}>" for u in batch if re.fullmatch(r"[\w:/.()\-]+", u))
        for r in _sparql(DBPEDIA_SPARQL, f"SELECT DISTINCT ?s WHERE {{ VALUES ?s {{ {values} }} ?s ?p ?o }}"):
            found.add(r["s"]["value"])
    logger.info("DBpedia: %d of %d species have a resource page", len(found), len(by_uri))
    return [_mapping(by_uri[u], u, by_uri[u]["scientific_name"], "skos:exactMatch", 0.9, "dbpedia", "dbpedia_direct_uri")
            for u in sorted(found)]


# ---------------------------------------------------------------------------
# Habitats: ENVO candidates from OLS4
# ---------------------------------------------------------------------------

# words that carry no habitat meaning in Annex I names ("... which are slightly covered by ...")
_STOPWORDS = {"with", "and", "of", "the", "which", "are", "on", "in", "by", "to", "at", "all", "time", "other",
              "not", "or", "a", "an", "slightly", "covered", "type", "types", "its", "their", "from", "low", "high",
              "species", "vegetation", "communities", "community", "habitat", "habitats", "European"}


def _key_words(name: str, limit: int = 4) -> list[str]:
    """Content words of a habitat name, singular, in order: 'Active raised bogs' -> ['active', 'raised', 'bog']."""
    words = []
    for w in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", name):
        w = _singular(w).lower()
        if w not in {x.lower() for x in _STOPWORDS} and w not in words:
            words.append(w)
    return words[:limit]


def _word_set(text: str) -> frozenset[str]:
    return frozenset(_key_words(text, limit=99))


def score_envo_candidates(habitat: dict, cands: list[dict]) -> list[dict]:
    """Lexical ENVO matches for one habitat, judged on key words rather than characters.

    'Bog woodland' and 'oak woodland' look alike letter by letter but share one of three key
    words, so they do not match. Exact needs the class *label* to carry the same key words as
    the habitat name; agreement through a synonym alone is close at most, and goes to review,
    because ENVO synonyms are often broader ('reef' on 'rocky reef').
    """
    name = _clean_label(habitat)
    words = _word_set(name)
    out = []
    if not words:
        return out
    for c in cands:
        label_words = _word_set(c["label"])
        overlap = len(words & label_words) / len(words | label_words) if label_words else 0.0
        if label_words == words:
            out.append(_mapping(habitat, c["uri"], c["label"], "skos:exactMatch", 1.0, "ols4_envo", "ols4_lexical",
                                target_definition=c["definition"]))
        elif overlap >= 0.6:
            out.append(_mapping(habitat, c["uri"], c["label"], "skos:closeMatch", round(0.6 + 0.3 * overlap, 4),
                                "ols4_envo", "ols4_lexical", target_definition=c["definition"]))
        elif any(_word_set(syn) == words for syn in c["synonyms"]):
            out.append(_mapping(habitat, c["uri"], c["label"], "skos:closeMatch", 0.75, "ols4_envo", "ols4_synonym",
                                target_definition=c["definition"], review=True,
                                note="name matches an ENVO synonym, which may be broader or narrower"))
    return out


def link_habitats_envo(habitats: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """ENVO classes for each habitat.

    OLS4 only returns classes matching every word of a query, and Annex I names are long legal
    descriptions, so the full name often finds nothing. Each habitat is searched by its name, its
    singular form and its key words; the pooled hits are candidates for step 4, which ranks them by
    meaning. Only close lexical matches on the name itself become mappings here.
    """
    cache: dict[str, list[dict]] = {}

    def search(query: str) -> list[dict]:
        if query not in cache:
            res = _get_json(OLS4_SEARCH, params={"q": query, "ontology": "envo", "type": "class", "rows": 10,
                                                 "fieldList": "iri,label,obo_id,synonym,description"})
            cache[query] = [d for d in (res or {}).get("response", {}).get("docs", [])
                            if d.get("iri", "").startswith("http://purl.obolibrary.org/obo/ENVO_")]
        return cache[query]

    out, candidates = [], {}
    for h in habitats:
        q = _clean_label(h)
        names = [q] + ([_singular(q)] if _singular(q) != q else [])
        docs, seen = [], set()
        for query in names + _key_words(q):
            for d in search(query):
                if d["iri"] not in seen and len(docs) < 30:
                    seen.add(d["iri"]); docs.append(d)
        cands = []
        for d in docs:
            desc = d.get("description") or []
            cands.append({"uri": d["iri"], "label": d.get("label", ""), "synonyms": d.get("synonym") or [],
                          "definition": desc[0] if isinstance(desc, list) and desc else (desc if isinstance(desc, str) else "")})
        candidates[h["id"]] = cands
        out.extend(score_envo_candidates(h, cands))
    logger.info("ENVO: candidates for %d of %d habitats (%d distinct searches)",
                sum(1 for c in candidates.values() if c), len(habitats), len(cache))
    return out, candidates


# ---------------------------------------------------------------------------
# Conservation statuses: IUCN category items
# ---------------------------------------------------------------------------

def link_statuses_wikidata(statuses: list[dict]) -> list[dict]:
    out = []
    for s in statuses:
        res = _get_json(WIKIDATA_API, params={"action": "wbsearchentities", "search": s.get("label", ""),
                                              "language": "en", "limit": 7, "format": "json"})
        for hit in (res or {}).get("search", []):
            if "iucn" in hit.get("description", "").lower() and string_similarity(s["label"], hit.get("label", "")) > 0.9:
                out.append(_mapping(s, f"http://www.wikidata.org/entity/{hit['id']}", hit["label"], "skos:exactMatch", 1.0,
                                    "wikidata", "wikidata_iucn_category"))
                break
    return out


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(mappings: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for m in mappings:
        key = (m["source_id"], m["target_uri"])
        if key not in seen or m["confidence"] > seen[key]["confidence"]:
            seen[key] = m
    return list(seen.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_linker(data_dir: str, dry_run: bool = False) -> list[dict]:
    """Run all external linking. Returns the list of mapping dicts."""
    global CACHE_DIR
    data_path = Path(data_dir)
    CACHE_DIR = data_path / "cache" / "linker"
    with (data_path / "terms.json").open("r", encoding="utf-8") as fh:
        terms: list[dict] = json.load(fh)
    logger.info("Loaded %d terms from %s/terms.json", len(terms), data_dir)

    if dry_run:
        terms = terms[:20]
        logger.info("Dry-run mode: limiting to first 20 terms")

    habitats = [t for t in terms if t.get("category") == "habitat"]
    species = [t for t in terms if t.get("category") == "species"]
    statuses = [t for t in terms if t.get("category") == "status"]
    logger.info("Linking %d habitats, %d species, %d statuses", len(habitats), len(species), len(statuses))

    all_mappings: list[dict] = []
    counts: dict[str, int] = {}

    def add(name: str, ms: list[dict]) -> None:
        counts[name] = counts.get(name, 0) + len(ms)
        all_mappings.extend(ms)
        logger.info("  %-28s %d mappings", name, len(ms))

    if habitats:
        add("habitats → Wikidata/EUNIS", link_habitats_wikidata(habitats))
        envo, candidates = link_habitats_envo(habitats)
        add("habitats → ENVO", envo)
    else:
        candidates = {}
    if species:
        gbif, accepted = match_species_gbif(species)
        add("species → GBIF backbone", gbif)
        wd, with_ncbi = link_species_wikidata(species, accepted, {m["source_id"] for m in gbif})
        add("species → Wikidata/NCBI", wd)
        add("species → NCBITaxon (exact)", link_species_ols4_exact([s for s in species if s["id"] not in with_ncbi]))
        add("species → DBpedia", link_species_dbpedia(species))
    if statuses:
        add("statuses → Wikidata (IUCN)", link_statuses_wikidata(statuses))
    # Biogeographical regions have no curated counterpart in these sources yet; left for experts.

    all_mappings = deduplicate(all_mappings)

    with (data_path / "direct_mappings.json").open("w", encoding="utf-8") as fh:
        json.dump(all_mappings, fh, indent=2, ensure_ascii=False)
    with (data_path / "envo_candidates.json").open("w", encoding="utf-8") as fh:
        json.dump(candidates, fh, indent=2, ensure_ascii=False)

    review = sum(1 for m in all_mappings if m.get("review"))
    logger.info("Linking complete: %d terms → %d mappings (%d flagged for expert review)",
                len(terms), len(all_mappings), review)
    return all_mappings


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Step 3: Link to external ontologies.")
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_linker(args.data_dir, args.dry_run)
