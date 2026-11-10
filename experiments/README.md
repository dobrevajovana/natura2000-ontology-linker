# PANGAEA test: does VERDANT's linking code work on PANGAEA names?

`pangaea_links.py` takes PANGAEA parameter names instead of Natura 2000 terms and runs VERDANT's own
code on them. The result is one Excel file that shows PANGAEA's existing terms next to what VERDANT finds.

## How to run

From the repo folder, with the repo's environment on (internet needed):

    source venv/bin/activate
    python experiments/pangaea_links.py

Needs `pandas` and `openpyxl` (`pip install pandas openpyxl`) and `envo.json` at `../VERDANT/envo.json`
(download: http://purl.obolibrary.org/obo/envo.json).

## Files

- `sample_species_biome.csv`: the input, 30 PANGAEA names (15 species, 15 biomes). Columns: `name`, `group`
  (species or biome), `pangaea_terms` (what PANGAEA already has), optional `id_parameter`, `difficulty`.
  To test other names, edit this file.
- `pangaea_links.py`: the script.
- `output_species_biome/comparison_pangaea_vs_verdant.xlsx`: the result.

## Where PANGAEA replaces Natura

- **Species:** each name is put into the same record a Natura species has (`id`, `label`, `scientific_name`,
  `category`), then the repo's functions in `pipeline/linker.py` run unchanged: GBIF, Wikidata, NCBITaxon, DBpedia.
- **Biomes:** Natura habitats are linked with Wikidata codes, which PANGAEA names do not have. So only
  VERDANT's embedding matcher (`pipeline/vector_match.py`) is reused: the closest 3 ENVO classes are listed
  with a similarity score.

## Small additions (not in VERDANT)

- Name cleaning: `Elphidium sp.` -> `Elphidium`, `Neogloboquadrina atlantica dextral` -> `Neogloboquadrina atlantica`,
  `Cold steppe` -> `steppe` for the ENVO search.
