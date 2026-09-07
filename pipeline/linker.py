"""
Step 3 — Link to External Ontologies

Links Natura 2000 terms to Wikidata, DBpedia, and OLS4 (EBI) using
SPARQL queries and REST APIs.

Input:  {data_dir}/terms.json
Output: {data_dir}/direct_mappings.json
"""

import json
import logging
import os
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
from SPARQLWrapper import SPARQLWrapper, JSON as SPARQL_JSON

logger = logging.getLogger("natura2000.linker")

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
DBPEDIA_SPARQL = "http://dbpedia.org/sparql"
OLS4_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"

USER_AGENT = "Natura2000OntologyLinker/1.0 (research project)"
API_RATE_LIMIT = float(os.environ.get("API_RATE_LIMIT", "1.0"))

import re as _re

def _clean_label(term: dict) -> str:
    """Return the best search label for a term, stripping EU codes and parentheticals."""
    # For species, prefer scientific name alone — external ontologies use binomial nomenclature
    if term.get("scientific_name"):
        return term["scientific_name"]
    label = term.get("label", "")
    # Strip leading EU habitat code: "1150 * Coastal lagoons" → "Coastal lagoons"
    label = _re.sub(r"^\d{4,5}[a-zA-Z]?\s*\*?\s*", "", label).strip()
    # Strip trailing parentheticals: "Otter (Lutra lutra)" → "Otter"
    label = _re.sub(r"\s*\(.*?\)\s*$", "", label).strip()
    return label or term.get("label", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def string_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _sparql_query(endpoint: str, query: str, timeout: int = 30) -> list[dict]:
    wrapper = SPARQLWrapper(endpoint)
    wrapper.addCustomHttpHeader("User-Agent", USER_AGENT)
    wrapper.setTimeout(timeout)
    wrapper.setQuery(query)
    wrapper.setReturnFormat(SPARQL_JSON)
    results = wrapper.query().convert()
    return results.get("results", {}).get("bindings", [])


# ---------------------------------------------------------------------------
# Wikidata
# ---------------------------------------------------------------------------

def link_wikidata(term: dict) -> list[dict]:
    mappings: list[dict] = []
    source_id = term["id"]
    source_label = term.get("label", "")
    search_label = _clean_label(term)
    category = term.get("category", "")

    for attempt in range(2):  # 1 retry on failure
        try:
            if category == "species" and term.get("scientific_name"):
                sci = term["scientific_name"].replace('"', '\\"')
                query = f"""
SELECT ?item ?itemLabel WHERE {{
  ?item wdt:P225 "{sci}" .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
}}
LIMIT 1
"""
                bindings = _sparql_query(WIKIDATA_SPARQL, query)
                for b in bindings:
                    uri = b["item"]["value"]
                    lbl = b.get("itemLabel", {}).get("value", sci)
                    mappings.append({
                        "source_id": source_id,
                        "source_label": source_label,
                        "target_uri": uri,
                        "target_label": lbl,
                        "match_type": "skos:exactMatch",
                        "confidence": 1.0,
                        "source_ontology": "wikidata",
                        "method": "sparql_p225",
                    })
            else:
                # Use Wikidata Search API — far more reliable than exact rdfs:label SPARQL
                resp = requests.get(
                    "https://www.wikidata.org/w/api.php",
                    params={
                        "action": "wbsearchentities",
                        "search": search_label,
                        "language": "en",
                        "limit": 3,
                        "format": "json",
                    },
                    headers={"User-Agent": USER_AGENT},
                    timeout=20,
                )
                resp.raise_for_status()
                for hit in resp.json().get("search", []):
                    uri = f"http://www.wikidata.org/entity/{hit['id']}"
                    lbl = hit.get("label", search_label)
                    sim = string_similarity(search_label, lbl)
                    mappings.append({
                        "source_id": source_id,
                        "source_label": source_label,
                        "target_uri": uri,
                        "target_label": lbl,
                        "match_type": "skos:exactMatch" if sim > 0.9 else "skos:closeMatch",
                        "confidence": 0.9 if sim > 0.9 else 0.75,
                        "source_ontology": "wikidata",
                        "method": "wikidata_search_api",
                    })
            break  # success — no retry needed

        except Exception as exc:
            if attempt == 0:
                logger.warning("Wikidata failed for '%s' (retrying): %s", source_id, exc)
                time.sleep(3)
            else:
                logger.warning("Wikidata failed for '%s' (giving up): %s", source_id, exc)

    return mappings


# ---------------------------------------------------------------------------
# DBpedia
# ---------------------------------------------------------------------------


def _dbpedia_ask_exists(uri: str) -> bool:
    try:
        wrapper = SPARQLWrapper(DBPEDIA_SPARQL)
        wrapper.addCustomHttpHeader("User-Agent", USER_AGENT)
        wrapper.setTimeout(20)
        wrapper.setQuery(f"ASK {{ <{uri}> ?p ?o }}")
        wrapper.setReturnFormat(SPARQL_JSON)
        return bool(wrapper.query().convert().get("boolean", False))
    except Exception:
        return False


def link_dbpedia(term: dict) -> list[dict]:
    """DBpedia linking via direct URI only (lookup.dbpedia.org is unreliable).
    Only applies to species — constructs dbr:Scientific_Name and validates via SPARQL."""
    mappings: list[dict] = []
    source_id = term["id"]
    source_label = term.get("label", "")
    category = term.get("category", "")

    if category != "species" or not term.get("scientific_name"):
        return []

    sci = term["scientific_name"]
    uri = f"http://dbpedia.org/resource/{sci.replace(' ', '_')}"
    if _dbpedia_ask_exists(uri):
        mappings.append({
            "source_id": source_id,
            "source_label": source_label,
            "target_uri": uri,
            "target_label": sci,
            "match_type": "skos:exactMatch",
            "confidence": 0.90,
            "source_ontology": "dbpedia",
            "method": "dbpedia_direct_uri",
        })
        logger.debug("DBpedia match for '%s': %s", source_id, uri)

    return mappings


# ---------------------------------------------------------------------------
# OLS4 (EBI)
# ---------------------------------------------------------------------------

def link_ols4(term: dict) -> list[dict]:
    mappings: list[dict] = []
    source_id = term["id"]
    source_label = term.get("label", "")
    search_label = _clean_label(term)
    category = term.get("category", "")

    # Choose ontologies based on category
    if category == "habitat":
        ontologies = ["envo"]
    elif category == "species":
        ontologies = ["ncbitaxon"]
    else:
        ontologies = ["envo", "ncbitaxon"]

    headers = {"User-Agent": USER_AGENT}

    for ontology in ontologies:
        try:
            resp = requests.get(
                OLS4_SEARCH,
                params={"q": search_label, "ontology": ontology, "rows": 3, "type": "class"},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            docs = resp.json().get("response", {}).get("docs", [])

            for idx, doc in enumerate(docs):
                uri = doc.get("iri", "")
                if not uri:
                    continue
                result_label = doc.get("label", source_label)
                definition = ""
                desc = doc.get("description", [])
                if isinstance(desc, list) and desc:
                    definition = desc[0]
                elif isinstance(desc, str):
                    definition = desc

                sim = string_similarity(source_label, result_label)
                confidence = (0.90 if sim >= 0.8 else 0.75) if idx == 0 else 0.70
                match_type = "skos:exactMatch" if sim >= 0.9 else "skos:closeMatch"

                mappings.append({
                    "source_id": source_id,
                    "source_label": source_label,
                    "target_uri": uri,
                    "target_label": result_label,
                    "target_definition": definition,
                    "match_type": match_type,
                    "confidence": confidence,
                    "source_ontology": f"ols4_{ontology}",
                    "method": "ols4_api",
                })

            time.sleep(API_RATE_LIMIT)

        except Exception as exc:
            logger.warning("OLS4 failed for '%s' (ontology=%s): %s", source_id, ontology, exc)

    return mappings


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
    """Run all external linking. Returns list of mapping dicts."""
    data_path = Path(data_dir)
    with (data_path / "terms.json").open("r", encoding="utf-8") as fh:
        terms: list[dict] = json.load(fh)

    logger.info("Loaded %d terms from %s/terms.json", len(terms), data_dir)

    if dry_run:
        terms = terms[:20]
        logger.info("Dry-run mode: limiting to first 20 terms")

    all_mappings: list[dict] = []
    counts = {"wikidata": 0, "dbpedia": 0, "ols4": 0}

    for idx, term in enumerate(terms, 1):
        logger.info("[%d/%d] Linking: %s", idx, len(terms), term.get("id"))

        wd = link_wikidata(term)
        counts["wikidata"] += len(wd)
        all_mappings.extend(wd)
        time.sleep(API_RATE_LIMIT)

        db = link_dbpedia(term)
        counts["dbpedia"] += len(db)
        all_mappings.extend(db)
        time.sleep(API_RATE_LIMIT)

        ols = link_ols4(term)
        counts["ols4"] += len(ols)
        all_mappings.extend(ols)
        time.sleep(API_RATE_LIMIT)

    all_mappings = deduplicate(all_mappings)

    output_file = data_path / "direct_mappings.json"
    with output_file.open("w", encoding="utf-8") as fh:
        json.dump(all_mappings, fh, indent=2, ensure_ascii=False)

    logger.info(
        "Linking complete: %d terms | Wikidata: %d | DBpedia: %d | OLS4: %d | Total (deduped): %d",
        len(terms), counts["wikidata"], counts["dbpedia"], counts["ols4"], len(all_mappings),
    )
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
