"""
PANGAEA names as input for VERDANT, and one Excel file with the result.

VERDANT is used as it is; only its input is replaced. VERDANT's pipeline reads its input from
<data-dir>/terms.json (normally Natura 2000 terms). This script does three things:

  1. writes the PANGAEA names from sample_species_biome.csv into that file
       species names -> category "species"  (VERDANT links them to GBIF, Wikidata, NCBITaxon, DBpedia)
       biome names   -> category "habitat"  (VERDANT links them to ENVO)
  2. runs VERDANT's own pipeline on it:  python -m pipeline.main --steps 3,4 --data-dir ./data_pangaea
     (steps 1 and 2, the Natura scraper and ontology builder, are not needed)
  3. reads VERDANT's mappings.json and writes an Excel file with PANGAEA's own terms next to every link,
     each link with the match type VERDANT gave it (exactMatch, closeMatch, relatedMatch)

Run from the repo folder, with the repo's environment on (internet needed, a few minutes):
    python experiments/pangaea_links.py
Output: experiments/output_species_biome/comparison_pangaea_vs_verdant.xlsx
Extra packages, if missing:  pip install pandas openpyxl
"""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SAMPLE = HERE / "sample_species_biome.csv"      # columns: name, group (species or biome), pangaea_terms, ...
DATA_DIR = ROOT / "data_pangaea"                # the folder VERDANT reads and writes
OUT = HERE / "output_species_biome" / "comparison_pangaea_vs_verdant.xlsx"

CATEGORY = {"species": "species", "biome": "habitat"}
# VERDANT's label for the source of a link -> our Excel column
SPECIES_COLUMNS = {"gbif": "VERDANT_GBIF", "wikidata": "VERDANT_Wikidata",
                   "ols4_ncbitaxon": "VERDANT_NCBITaxon", "dbpedia": "VERDANT_DBpedia"}


# --- 1. PANGAEA names -> VERDANT's input file ---------------------------------

def write_terms(rows: list[dict]) -> None:
    """Write the names as terms.json, the same kind of records VERDANT reads for Natura terms."""
    terms = []
    for i, r in enumerate(rows):
        category = CATEGORY[r["group"]]
        terms.append({
            "id": f"pangaea_{category}_{i:02d}",
            "label": r["name"].strip(),                  # the PANGAEA parameter name, unchanged
            "category": category,
            "definition": f"PANGAEA parameter (dataset {r.get('dataset', '')})",
            "codes": [],                                 # no Natura/EUNIS codes for PANGAEA names
            "scientific_name": r["name"].strip() if category == "species" else None,
            "common_names": [],
            "source_url": "",
        })
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "terms.json").write_text(json.dumps(terms, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(terms)} terms to {DATA_DIR / 'terms.json'}")


# --- 2. VERDANT itself --------------------------------------------------------

def run_verdant() -> None:
    """Run the repo's own pipeline, steps 3 (linking) and 4 (embedding matching). Nothing is reimplemented."""
    cmd = [sys.executable, "-m", "pipeline.main", "--steps", "3,4", "--data-dir", str(DATA_DIR)]
    print("Running VERDANT:", " ".join(cmd[1:]))
    if subprocess.run(cmd, cwd=ROOT).returncode != 0:
        sys.exit("VERDANT's pipeline reported an error (see the messages above).")


# --- 3. VERDANT's result -> Excel ---------------------------------------------

def link_text(m: dict) -> str:
    """'https://www.gbif.org/species/123 (exactMatch)'  or  'tundra biome (closeMatch, 0.72)'."""
    kind = m["match_type"].replace("skos:", "")
    if m["source_ontology"] in SPECIES_COLUMNS:
        return f"{m['target_uri']} ({kind})"
    return f"{m['target_label']} ({kind}, {m['confidence']:.2f})"


def has_specific_term(terms: str) -> str:
    """PANGAEA's 'Abundance or Count' only says what was measured. 'yes' if another term is present."""
    parts = [p.strip() for p in str(terms).split(";") if p.strip() and p.strip() != "Abundance or Count"]
    return "yes" if parts else "no"


def write_excel(sample: pd.DataFrame) -> None:
    terms = json.loads((DATA_DIR / "terms.json").read_text(encoding="utf-8"))
    mappings = json.loads((DATA_DIR / "mappings.json").read_text(encoding="utf-8"))
    by_term: dict[str, list[dict]] = {}
    for m in mappings:
        by_term.setdefault(m["source_id"], []).append(m)

    rows = []
    for term, (_, s) in zip(terms, sample.iterrows()):          # same order as write_terms wrote them
        found = by_term.get(term["id"], [])
        row = {"name": s["name"], "group": s["group"]}
        if "pangaea_terms" in sample.columns:
            row.update({"PANGAEA_terms": s["pangaea_terms"],
                        "PANGAEA_has_specific_term": has_specific_term(s["pangaea_terms"])})
        for col in SPECIES_COLUMNS.values():
            row[col] = ""
        row["VERDANT_ENVO"] = ""
        for m in found:
            col = SPECIES_COLUMNS.get(m["source_ontology"], "VERDANT_ENVO")
            row[col] = (row[col] + " ; " if row[col] else "") + link_text(m)
        row["VERDANT_number_of_links"] = len(found)
        row["VERDANT_has_exactMatch"] = "yes" if any(m["match_type"] == "skos:exactMatch" for m in found) else "no"
        rows.append(row)

    df = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(OUT, index=False)
    for group in ("species", "biome"):
        sub = df[df.group == group]
        print(f"{group}: {len(sub)} names, {(sub.VERDANT_number_of_links > 0).sum()} with at least one link, "
              f"{(sub.VERDANT_has_exactMatch == 'yes').sum()} with an exactMatch")
    print(f"Saved: {OUT}")


def main() -> None:
    with SAMPLE.open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["name"].strip()]
    write_terms(rows)
    run_verdant()
    write_excel(pd.read_csv(SAMPLE, dtype=str, keep_default_na=False).query("name != ''").reset_index(drop=True))


if __name__ == "__main__":
    main()
