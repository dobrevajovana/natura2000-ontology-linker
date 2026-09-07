# BioSample Geo Knowledge Graph (`geoapp/`)

A single application that takes a list of INSDC accessions, pulls the records from
**NCBI E-utilities** and the **EBI BioSamples API**, works out **where each sample was
collected**, links its attributes to **external ontologies** (ENVO, NCBITaxon, UBERON,
PO, Wikidata) and **sequence archives** (ENA, SRA, BioSample), and serves an
interactive **map + knowledge graph** where selecting an area on the map filters the
graph to the samples in it.

Geolocation is the spine of the app: it is what the map filter acts on, what the
`place` nodes in the graph are built from, and what every other view is sliced by.

This lives alongside the existing Natura 2000 pipeline (`pipeline/`, `viz/`) and does
not modify it — separate package, separate data files, separate port, separate
compose file.

---

## Quick start

```bash
pip install -r requirements.geoapp.txt

# 1. fetch -> geolocate -> link ontologies -> build the graph
python -m geoapp.cli --accessions accessions.txt

# 2. explore it
python -m geoapp.app          # http://localhost:8090
```

Docker:

```bash
docker compose -f docker-compose.geoapp.yml run --rm geoapp-pipeline
docker compose -f docker-compose.geoapp.yml up geoapp
```

`accessions.txt` ships with 83 real accessions assembled from live BioSamples
queries, deliberately mixed so every geolocation path is exercised: samples with
explicit `lat lon`, samples with only a country or sea, and two secondary accessions
that force the `SRS -> SAM` resolution path.

Use your own list with `--accessions my-file.txt`. One accession per line;
`#` starts a comment, inline or on its own line.

---

## The four stages

Run any subset with `--steps`, e.g. `--steps 1,2` to fetch and geolocate only.

### 1. Fetch — `geoapp/fetch.py`

Accessions starting with `SRS`/`ERS`/`DRS` are secondary; they are resolved to a
primary BioSample accession through NCBI E-utilities
(`esearch` -> `idlist` -> `esummary` -> `accession`) before anything else. `SAM*`
accessions skip straight through.

The record itself comes from `https://www.ebi.ac.uk/biosamples/samples/{accession}`.
While parsing it the fetcher already harvests the ontology PURLs the archive supplies
inline — BioSamples records carry `ontologyTerms` on individual characteristics, e.g.
`organism` -> `NCBITaxon_410658`, `title` -> `ENVO_00005802` — and builds the archive
cross-references from `externalReferences` and `sraAccession`.

Concurrent (`ThreadPoolExecutor`), rate-limited per host (3 req/s to NCBI, 10 with an
API key; 8 req/s to EBI), retried with exponential backoff on timeouts and 429/5xx,
and cached on disk under `data/cache/`. A failed accession still produces a `Sample`
with `fetch_error` set — inputs are never silently dropped.

### 2. Geolocate — `geoapp/geo.py`, `geoapp/gazetteer.py`

The most important stage, and the messiest, because there is no single way archives
record position. `locate()` tries four strategies in order and records which one won
in `Location.precision`, so the UI can be honest about how much to trust each pin:

| precision | source | map marker |
|---|---|---|
| `coordinates` | `lat lon` / `lat_lon` / separate `geographic location (latitude\|longitude)` | solid circle |
| `locality` | `geographic location (region and locality)`, the part after the colon in `geo_loc_name` | hollow ring |
| `country` | `geographic location (country and/or sea)`, `country`, part before the colon in `geo_loc_name` | faded, dashed |
| `none` | nothing usable | not on the map, still in the graph and tables |

Coordinate parsing handles both real-world formats — ENA decimal
(`"57.25689, 11.14193"`) and NCBI hemisphere (`"57.25 N 11.14 E"`, `"36.9 S 174.7 W"`) —
plus unit suffixes, unspaced variants, and the nine "no value" placeholders INSDC
uses (`not collected`, `not applicable`, `missing`, `restricted access`, …). Values
outside `|lat|<=90` / `|lon|<=180` are rejected rather than plotted wrongly.

The gazetteer is **entirely offline** — 202 countries and territories, 54 INSDC
aliases (`USA`, `UK`, `Czechia`, `Viet Nam`, the Congo split…), 42 seas and oceans,
and 47 regions weighted towards the European basins this project cares about
(Kattegat, Skagerrak, Wadden Sea, Limfjord, Øresund, Doggerbank). Seas matter because
`geographic location (country and/or sea)` is very often a body of water, not a
country. No geocoding API, so the pipeline still runs air-gapped.

`country`, `country_code` and `region` are filled whenever they can be derived, even
when coordinates came from somewhere else — the app filters by name as well as by map
area.

### 3. Link ontologies — `geoapp/ontolink.py`

Attributes without an inline PURL are looked up in **OLS4**, each routed to the
ontology likely to hold it: organism/host/serovar to `ncbitaxon`; isolation source,
`env_broad_scale`/`env_local_scale`/`env_medium`, habitat, biome, title and
description to `envo`; tissue and body site to `uberon`; plant parts to `po`.
Scientific names additionally go to **Wikidata** via a SPARQL query on P225.

Hits are scored by string similarity against the term label and classified
`skos:exactMatch` (>=0.95), `skos:closeMatch` (>=0.80) or `skos:relatedMatch`, with
anything under `GEO_OLS4_MIN_SCORE` discarded. Lookups are de-duplicated across the
whole run — the same organism appears in many samples and is only ever queried once —
cached on disk, and never allowed to raise: a dead endpoint degrades to "no extra
matches", it does not fail the pipeline.

### 4. Build the graph — `geoapp/graph.py`

| node kind | comes from | edge to sample |
|---|---|---|
| `sample` | one per record, carries lat/lon | — |
| `taxon` | NCBITaxon terms, or the organism name | `has_taxon` |
| `environment` | ENVO terms | `in_environment` |
| `place` | country and region centroids | `collected_at`, region `within` country |
| `archive` | ENA / SRA / BioSample cross-refs | `archived_in` |
| `attribute` | attributes matched to other ontologies | `has_attribute` + `ontology_match` |

Nodes are shared across samples and de-duplicated by stable id — that sharing *is* the
knowledge graph: two samples with the same organism converge on one `taxon` node, and
everything collected in Denmark hangs off one `place` node.

Outputs, all into `data/`:

| file | what for |
|---|---|
| `geo_samples.json` | normalised sample records |
| `geo_kg.json` | Cytoscape-ready nodes and edges |
| `geo_samples.geojson` | map layer (coordinates lon-first, per the GeoJSON spec) |
| `geo_kg.ttl` | RDF/Turtle — `wgs84:lat`/`long`, `dcterms:`, `skos:*Match` for the ontology mappings |

---

## The explorer — `geoapp/app.py`

Map (Leaflet, clustered) above, knowledge graph (Cytoscape.js) below. **Selecting a
rectangle or radius on the map filters the graph** to the samples inside it and the
ontology, archive and place nodes they reach; clicking a sample node in the graph pans
the map to its marker. The left panel filters by text, country, region, precision,
ontology source, archive and match confidence.

| endpoint | returns |
|---|---|
| `GET /api/samples` | filtered sample records + total/filtered counts |
| `GET /api/sample/<accession>` | one sample plus its graph neighbourhood |
| `GET /api/geojson` | FeatureCollection for the map |
| `GET /api/graph` | Cytoscape nodes/edges for the current filter |
| `GET /api/stats` | counts for the stats strip |
| `GET /api/facets` | dropdown values (countries, regions, ontologies, archives) |
| `GET /api/legend` | node/ontology/archive vocabularies from `schema.py` |
| `POST /api/reload` | re-read the data files from disk |

Every endpoint accepts the same filter parameters: `search`, `country`, `region`,
`precision`, `ontology`, `archive`, `min_confidence`, `has_coords`, `bbox`,
`center`+`radius_km`.

---

## Configuration

Environment variables, all optional — see `geoapp/config.py`.

| variable | default | meaning |
|---|---|---|
| `GEO_DATA_DIR` | `./data` | where artefacts and the cache are written |
| `GEO_ACCESSIONS` | `./accessions.txt` | default input list |
| `NCBI_API_KEY` / `NCBI_EMAIL` | – | raises the NCBI rate limit from 3 to 10 req/s |
| `GEO_FETCH_WORKERS` | `8` | parallel fetches |
| `GEO_CACHE_TTL_DAYS` | `30` | on-disk cache lifetime |
| `GEO_OLS4_ONTOLOGIES` | `envo,ncbitaxon,uberon,po` | ontologies queried via OLS4 |
| `GEO_OLS4_MIN_SCORE` | `0.60` | below this a match is discarded |
| `GEO_ENRICH_ONTOLOGY` / `GEO_ENRICH_WIKIDATA` | `1` | set `0` to skip stage 3 lookups |
| `GEO_PORT` | `8090` | explorer port (the Natura 2000 viz keeps 8080) |

---

## Layout

```
geoapp/
  config.py       settings, all env-overridable
  schema.py       dataclasses + controlled vocabularies - the contract between stages
  fetch.py        E-utilities resolution, BioSamples fetch, cache, rate limiting
  geo.py          coordinate parsing and the locate() strategy ladder
  gazetteer.py    offline country / sea / region centroids
  ontolink.py     OLS4 + Wikidata enrichment
  graph.py        knowledge graph, GeoJSON, RDF, stats
  app.py          Flask API + explorer
  cli.py          the four-stage pipeline runner
  templates/index.html
  static/app.js  static/style.css
```
