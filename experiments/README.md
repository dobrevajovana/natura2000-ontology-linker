# PANGAEA test: does VERDANT work on PANGAEA names?

VERDANT is used **as it is**. Only its input is replaced: instead of Natura 2000 terms, the pipeline reads
PANGAEA parameter names (15 organism names and 15 biome names). Nothing in `pipeline/` is changed.

## How to run

From the repo folder, with the repo's environment on (internet needed, a few minutes):

    source venv/bin/activate
    python experiments/pangaea_links.py

The script (1) writes the PANGAEA names into VERDANT's input file `data_pangaea/terms.json`, (2) runs
VERDANT's own pipeline on it (`python -m pipeline.main --steps 3,4 --data-dir ./data_pangaea`; steps 1 and 2,
the Natura scraper and ontology builder, are not needed) and (3) writes the Excel file.
Extra packages, if missing: `pip install pandas openpyxl`.
The results can also be viewed in VERDANT's viewer: `DATA_DIR=./data_pangaea python viz/app.py`
(tick "Species" in the category list).

## Files

- `sample_species_biome.csv`: the input, 30 PANGAEA names. Columns: `name`, `group` (species or biome),
  `pangaea_terms` (what PANGAEA already has for the parameter), `dataset`, `id_parameter`, `difficulty`.
  To test other names, edit this file.
- `pangaea_links.py`: the script (names -> VERDANT's input file -> VERDANT -> Excel).
- `output_species_biome/comparison_pangaea_vs_verdant.xlsx`: the result, with VERDANT's own match type for each link.

## What VERDANT does with the names

- **Species:** GBIF, Wikidata, NCBITaxon and DBpedia (`pipeline/linker.py`), using the Latin name.
- **Biomes (habitats):** ENVO, by OLS4 word search and embedding similarity (`pipeline/linker.py` and
  `pipeline/vector_match.py`). VERDANT's Wikidata habitat codes do not exist for PANGAEA names, so they give nothing.

## Limits

- 30 hand-picked names: a first test, not a measured success rate.
- Names are given to VERDANT unchanged, for example "Elphidium sp." and "Cold steppe". Cleaning them first
  ("Elphidium", "steppe") may find more links.
- ENVO matches are suggestions. A domain expert has to decide whether they are right.
- PANGAEA terms in the input file come from a snapshot of term names. The links behind the terms (for example
  the ENVO link of "tundra") are not in it.
