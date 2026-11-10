"""
PANGAEA names -> VERDANT's own linking code -> one Excel file.

Input is always experiments/sample_species_biome.csv (columns: name, group = species or biome;
optional: pangaea_terms, id_parameter, difficulty). To test other names, edit that file.

The linking is done by the repo's functions:
    species : GBIF, Wikidata, NCBITaxon, DBpedia       (pipeline/linker.py)
    biomes  : the 3 closest ENVO classes by embedding   (pipeline/vector_match.py, needs envo.json)
The column 'pangaea_terms' of the input file (what PANGAEA already annotated) is shown next to the result.

Run from the repo folder, with the repo's environment on (the species part needs internet):
    python experiments/pangaea_links.py
Output: experiments/output_species_biome/comparison_pangaea_vs_verdant.xlsx
Extra packages, if missing:  pip install pandas openpyxl
"""

import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")   # same macOS FAISS/PyTorch workaround as the pipeline

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from pipeline import linker  # noqa: E402  (the repo's own linking code)
from pipeline.vector_match import VectorMatcher, candidate_text  # noqa: E402

SAMPLE = HERE / "sample_species_biome.csv"
ENVO = ROOT.parent / "VERDANT" / "envo.json"       # public ENVO (http://purl.obolibrary.org/obo/envo.json)
OUT = HERE / "output_species_biome" / "comparison_pangaea_vs_verdant.xlsx"
# climate words that are dropped from a biome name before matching ("Cold steppe" -> "steppe")
CLIMATE_WORDS = {"cold", "cool", "warm", "hot", "temperate"}
# the repo labels each link with its source; this maps those labels to our column names
RESOURCE = {"gbif": "gbif", "wikidata": "wikidata", "ols4_ncbitaxon": "ncbitaxon", "dbpedia": "dbpedia"}


# --- cleaning ----------------------------------------------------------------

def clean_taxon(name: str) -> str:
    """Reduce a PANGAEA organism name to the Latin name that GBIF, Wikidata etc. understand.
    'Elphidium sp.' -> 'Elphidium';  'Neogloboquadrina atlantica dextral' -> 'Neogloboquadrina atlantica'.
    Rule 1: drop a trailing ' sp.'   Rule 2: for 3+ words ('Genus species extra') keep the first two."""
    # Rule 1: remove " sp." at the end
    n = re.sub(r"\s+sp\.$", "", name.strip())
    words = n.split()
    # Rule 2: capitalised genus + lowercase species + more words -> keep genus + species only
    if len(words) >= 3 and words[0][:1].isupper() and words[1].islower():
        n = " ".join(words[:2])
    return n


def habitat_core(name: str) -> str:
    """Keep the habitat word of a biome name, so ENVO is searched with the habitat and not the climate.
    'Cold steppe' -> 'steppe'. Climate words (cold, cool, warm, hot, temperate) are dropped.
    A name made only of climate words is returned unchanged."""
    words = [w for w in name.split() if w.lower() not in CLIMATE_WORDS]
    return " ".join(words) or name


# --- species: the repo's own chain -------------------------------------------

def link_species(names: list[str]) -> dict[str, dict]:
    """Find external links for a list of Latin names, using the repo's own species functions in the repo's order.
    Returns {name: {"gbif": url, "wikidata": url, "ncbitaxon": url, "dbpedia": url}}, only the links that were found.
    Needs internet (GBIF, Wikidata, EBI OLS4, DBpedia); answers are cached in experiments/.cache."""
    # the repo expects a list of term records
    terms = [{"id": f"s{i}", "label": n, "scientific_name": n, "category": "species"} for i, n in enumerate(names)]
    gbif, accepted = linker.match_species_gbif(terms)             # 1. GBIF species match (also gives the accepted name)
    # 2. Wikidata page by scientific name (P225); it also carries the NCBITaxon id when Wikidata has one
    wikidata, with_ncbi = linker.link_species_wikidata(terms, accepted, {m["source_id"] for m in gbif})
    # 3. NCBITaxon looked up directly, only for names where Wikidata gave none
    ncbi = linker.link_species_ols4_exact([t for t in terms if t["id"] not in with_ncbi])
    dbpedia = linker.link_species_dbpedia(terms)                   # 4. DBpedia page with the same name
    links = {n: {} for n in names}
    # put all links together, drop repeats, and file each one under its name
    for m in linker.deduplicate(gbif + wikidata + ncbi + dbpedia):
        if m["source_ontology"] in RESOURCE:
            links[names[int(m["source_id"][1:])]][RESOURCE[m["source_ontology"]]] = m["target_uri"]
    return links


# --- biomes: closest ENVO classes --------------------------------------------

def load_envo(path: Path) -> list[dict]:
    """Read envo.json and return the current ENVO classes that have a label (about 4,000).
    Each entry: uri, label, exact synonyms, definition. Same reading rules as the VERDANT mini prototype notebook.
    Deprecated (retired) classes are skipped."""
    graph = json.load(path.open(encoding="utf-8"))["graphs"][0]
    out = []
    for node in graph["nodes"]:
        meta = node.get("meta", {})
        if node.get("type") != "CLASS" or "lbl" not in node or "/ENVO_" not in node["id"] or meta.get("deprecated"):
            continue
        syns, seen = [], set()
        for s in meta.get("synonyms", []):
            if s["pred"] == "hasExactSynonym" and s["val"].lower() not in seen:
                seen.add(s["val"].lower()); syns.append(s["val"])
        out.append({"uri": node["id"], "label": node["lbl"], "synonyms": syns,
                    "definition": (meta.get("definition") or {}).get("val", "")})
    return out


def top3(sim: np.ndarray, corpus: list[dict]) -> str:
    """Turn a list of similarity scores (one per ENVO class) into a text with the 3 best:
    'tundra biome (0.67) ; area of tundra (0.62) ; ...'.
    The number is cosine similarity between the two embeddings (0 to 1). It measures similar
    wording and meaning, it is not proof that two terms mean the same."""
    best = np.argsort(-sim)[:3]
    return " ; ".join(f"{corpus[j]['label']} ({sim[j]:.2f})" for j in best)


def envo_embeddings(matcher, corpus: list[dict], envo_path: Path, model: str) -> np.ndarray:
    """One vector per ENVO class. Computing 4,000 vectors is the slowest step, so the result is saved
    in experiments/.cache and reused until envo.json or the model changes."""
    stat = envo_path.stat()
    safe_model = re.sub(r"[^A-Za-z0-9._-]", "_", model)
    cache = HERE / ".cache" / f"envo_{safe_model}_{stat.st_size}_{stat.st_mtime_ns}.npy"
    if cache.exists():
        return np.load(cache)
    emb = matcher.encode([candidate_text(c) for c in corpus])
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.save(cache, emb)
    return emb


def match_biomes(names: list[str], envo_path: Path) -> dict[str, dict]:
    """For each biome name, find the closest ENVO classes with the repo's embedding matcher.
    The name is tried twice: as written ("whole name") and without the climate word ("core word").
    Returns {name: {"core": the core word, "envo": text with both top-3 lists}}.
    No internet needed; the first run loads the embedding model, which takes a moment."""
    corpus = load_envo(envo_path)
    model = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    matcher = VectorMatcher(model)
    # turn text into vectors (lists of numbers); similar meaning = vectors that point the same way
    envo_emb = envo_embeddings(matcher, corpus, envo_path, model)
    cores = [habitat_core(n) for n in names]
    # one matrix product gives the similarity of every name to every ENVO class (column i = name i)
    whole_sim = envo_emb @ matcher.encode([n.lower() for n in names]).T
    core_sim = envo_emb @ matcher.encode([c.lower() for c in cores]).T
    return {n: {"core": cores[i],
                "envo": f"whole name: {top3(whole_sim[:, i], corpus)}  ||  core word: {top3(core_sim[:, i], corpus)}"}
            for i, n in enumerate(names)}


# --- what PANGAEA already has (column 'pangaea_terms' of the input file) -------

def has_specific_term(terms: str) -> str:
    """Look at PANGAEA's own terms for a parameter, for example 'Abundance or Count; tundra'.
    'Abundance or Count' only says what was measured, so it does not count.
    Returns 'yes' if any other term is left (like 'tundra'), otherwise 'no'."""
    parts = [p.strip() for p in str(terms).split(";") if p.strip() and p.strip() != "Abundance or Count"]
    return "yes" if parts else "no"


# --- main --------------------------------------------------------------------

NO_LINKS = {"VERDANT_GBIF": "", "VERDANT_Wikidata": "", "VERDANT_NCBITaxon": "", "VERDANT_DBpedia": ""}


def main() -> None:
    """Steps: read the names -> clean and link the species -> match the biomes to ENVO ->
    put PANGAEA's terms and VERDANT's results side by side -> save one Excel file."""
    sample = pd.read_csv(SAMPLE, dtype=str, keep_default_na=False)
    sample = sample[sample["name"] != ""]
    is_species = sample["group"] == "species"
    cleaned = {n: clean_taxon(n) for n in sample.loc[is_species, "name"]}     # clean each species name once
    biomes = list(sample.loc[~is_species, "name"])
    print(f"{len(sample)} names: {len(cleaned)} species, {len(biomes)} biomes")

    if biomes and not ENVO.exists():
        sys.exit(f"ENVO file not found: {ENVO}\nDownload http://purl.obolibrary.org/obo/envo.json and put it there.")

    linker.CACHE_DIR = HERE / ".cache"        # answers from the web services are cached here
    species_links = link_species(sorted(set(cleaned.values()))) if cleaned else {}
    biome_hits = match_biomes(biomes, ENVO) if biomes else {}

    rows = []
    for r in sample.to_dict("records"):
        row = {"name": r["name"], "group": r["group"], "difficulty": r.get("difficulty", "")}
        if "pangaea_terms" in r:
            row.update({"PANGAEA_terms": r["pangaea_terms"],
                        "PANGAEA_has_specific_term": has_specific_term(r["pangaea_terms"])})
        if r["group"] == "species":
            found = species_links[cleaned[r["name"]]]
            row.update({"VERDANT_cleaned_name": cleaned[r["name"]],
                        "VERDANT_GBIF": found.get("gbif", ""), "VERDANT_Wikidata": found.get("wikidata", ""),
                        "VERDANT_NCBITaxon": found.get("ncbitaxon", ""), "VERDANT_DBpedia": found.get("dbpedia", ""),
                        "VERDANT_ENVO_top3": "", "VERDANT_found_something": "yes" if found else "no"})
        else:
            hit = biome_hits[r["name"]]
            row.update({"VERDANT_cleaned_name": hit["core"], **NO_LINKS, "VERDANT_ENVO_top3": hit["envo"],
                        "VERDANT_found_something": "suggestion only (check by a person)"})
        rows.append(row)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_excel(OUT, index=False)
    print(f"Saved: {OUT}")


if __name__ == "__main__":
    main()
