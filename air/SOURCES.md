# Aerobiome KG: data sources

Every external source the aerobiome KG (`air/`) reads, and where each mapping
choice comes from. Checked 2026-11-10.

## Samples

ENA Portal API: samples under taxon 655179 (air metagenome) and its subtree 
EBI BioSamples API: one JSON record per sample 
NCBI E-utilities: secondary accessions (SRS/ERS/DRS) to BioSample IDs | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/` | `geoapp/fetch.py` |

## Ontologies

| Ontology | Link | How it is used |
|---|---|---|
| ENVO | OLS4 search, `https://www.ebi.ac.uk/ols4/api` | Values without an ENVO term in the record, searched with geoapp's calibrated matcher (`geoapp/ontolink.py`) |
| CHEBI | OLS4 search, `https://www.ebi.ac.uk/ols4/api` | Chemical values and measured chemicals, same matcher |
| GOLD ecosystem classification | `https://raw.githubusercontent.com/cmungall/gold-ontology/main/gold.obo` (repo: https://github.com/cmungall/gold-ontology) | Downloaded once; values matched by name |
| GOLD to ENVO mappings (curated SSSOM) | `https://raw.githubusercontent.com/cmungall/gold-ontology/main/mappings/gold-to-envo.curated.sssom.tsv` | A value's ENVO term leads to GOLD by lookup; `owl:differentFrom` rows are never used |
| EMPO categories (levels 1-3, 27 classes) | https://qiita.ucsd.edu/static/doc/html/empo.html (also https://earthmicrobiome.org/protocols-and-standards/empo/) | Built into `air/vocab.py`; no download file exists |
| ENVO's EMPO subset (ENVO IDs for EMPO categories) | `https://raw.githubusercontent.com/EnvironmentOntology/envo/master/subsets/envoEmpo.owl` (`http://purl.obolibrary.org/obo/envo/subsets/envoEmpo.owl`) | Gives each built-in EMPO class an ENVO IRI. Pairing EMPO names with these classes by label is our choice, listed in `air/vocab.py` |
