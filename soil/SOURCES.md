# Soil KG: data sources

Every external source the soil KG (`soil/`) reads, and where each mapping
choice comes from. Checked 2026-11-11.

## MFD (`python -m soil.mfd`)

| Source | Link | How it is used |
|---|---|---|
| MFD habitat ontology (MFDO), GPL-3.0 | `https://raw.githubusercontent.com/cmc-aau/mfd_metadata/b1b17d4b4e8e289c4380d878c8bf1516eb107791/data/ontology/latest_mfd-habitat-ontology.xlsx` (repo: https://github.com/cmc-aau/mfd_metadata) | Read from the URL on every build, not saved. Pinned to one commit in `soil/mfd.py`; override with `MFD_ONTOLOGY_URL` or `--url`. Gives the five-level tree and MFD's own crosswalk to Natura 2000, EUNIS and EMPO |
| Natura 2000 linker output (this repo) | `data/terms.json`, `data/mappings.json`, `data/mappings_added.json` from `python -m pipeline.main` | Classes with a Natura 2000 code are joined through it to Wikidata, EUNIS, ENVO and any ontology added in the main viewer |
| ENVO via OLS4 search | `https://www.ebi.ac.uk/ols4/api` | Classes without a Natura 2000 code, matched by name with geoapp's calibrated matcher (`geoapp/ontolink.py`). Kept in `data/mfd/envo.json` |

## NMDC (`python -m soil.nmdc`)

| Source | Link | How it is used |
|---|---|---|
| NMDC runtime API | `https://api.microbiomedata.org/biosamples?filter=ecosystem_type:Soil` (docs: https://api.microbiomedata.org/docs) | All soil biosamples, paged with `cursor`. Fields read: `env_broad_scale`, `env_local_scale`, `env_medium`, the GOLD ecosystem path, `gold_biosample_identifiers`, `associated_studies`, `geo_loc_name`, `lat_lon` |
| ENVO | `http://purl.obolibrary.org/obo/ENVO_` | The triad terms come with ENVO IDs and names in the record; used as given, no search |
| GOLD ecosystem classification | `https://raw.githubusercontent.com/cmungall/gold-ontology/main/gold.obo` (repo: https://github.com/cmungall/gold-ontology) | Read from the URL, not saved. A sample's path `ecosystem > ecosystem_category > ecosystem_type > ecosystem_subtype > specific_ecosystem` is looked up by class name; `is_a` gives the parents; `mixs:env_*` values are compared with the sample's ENVO triad |

## Joined KG (`python -m soil.kg`)

| Source | Link | How it is used |
|---|---|---|
| MFD and NMDC graphs (this repo) | `data/mfd/graph.json`, `data/mfd/ontology.json`, `data/soil/nmdc/graph.json` | Merged; OBO PURLs become CURIEs so shared ENVO terms are one node |
| GOLD ecosystem classification | `https://raw.githubusercontent.com/cmungall/gold-ontology/main/gold.obo` | Read from the URL. Each GOLD class's `mixs:env_local` links it to that ENVO term |

## Choices made here

- Trailing "Unclassified" steps of a GOLD path are dropped before the lookup. If GOLD has no class for the full path, the deepest prefix it has is used, as `broadMatch`.
- MFD class names that say nothing on their own ("Other", "Natural", "Urban", ...) are not sent to ENVO; the list is `SKIP_LABELS` in `soil/mfd.py`.
- In the joined KG, only `env_local` links ENVO to GOLD (`env_medium` "soil" would tie every soil class together), at most 3 GOLD classes per ENVO term, shallowest first (`MAX_GOLD_PER_TERM` in `soil/kg.py`).
- ENVO matches by name below 0.70 confidence are dropped; below 0.85 they are flagged for review.
