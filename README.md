# Natura 2000 Ontology Linker

A Python pipeline that scrapes all terms from the Natura 2000 network, builds a formal OWL 2 ontology, links them to external knowledge bases (Wikidata, DBpedia, OLS4/ENVO/NCBITaxon) using both SPARQL and vector similarity, and serves an interactive graph visualization — all containerized with Docker Compose.

Built for the AAU Bio Collaboration team to enable semantic interoperability between Natura 2000 biodiversity data and international ontology standards.

---

## Architecture

```
[Natura 2000 / EEA Data Sources]
        |
        v
  Step 1: Scraper
  ─────────────────────────────────
  • Annex I habitats (EU habitat codes)
  • Annex II/IV/V species (Habitats Directive)
  • Annex I bird species (Birds Directive)
  • 9 biogeographical regions
  • IUCN conservation status categories
        |
        v  data/terms.json
  Step 2: OWL Ontology Builder
  ─────────────────────────────────
  • Full class hierarchy (Habitat → CoastalHabitat, ForestHabitat…)
  • Species subclasses (BirdSpecies, MammalSpecies, PlantSpecies…)
  • Object & data properties
  • OWL individuals for all terms
  • Namespace: https://ontology.natura2000.eu/
        |
        v  data/ontology.ttl  data/ontology.jsonld
  Step 3: External Linker
  ─────────────────────────────────
  • Wikidata SPARQL (P225 taxon name, label search)
  • DBpedia Lookup API + direct URI validation
  • OLS4/EBI API → ENVO (habitats) + NCBITaxon (species)
  • SKOS match types: exactMatch / closeMatch
        |
        v  data/direct_mappings.json
  Step 4: Vector Similarity Matcher
  ─────────────────────────────────
  • SentenceTransformers (all-MiniLM-L6-v2 or SapBERT)
  • FAISS inner-product index (cosine similarity on normalized vecs)
  • Top-5 nearest neighbours per term per ontology
  • Configurable thresholds → exactMatch / closeMatch / relatedMatch
  • Merges with direct mappings, deduplicates
        |
        v  data/mappings.json  data/mappings.ttl
  Step 5: Visualization
  ─────────────────────────────────
  • Flask backend serving REST API
  • Cytoscape.js interactive force-directed graph
  • Search, filter, confidence slider
  • Node click → full detail + all linked terms
  • Table view alternative
  http://localhost:8080
```

---

## Quick Start (Docker)

```bash
git clone https://github.com/dobrevajovana/natura2000-ontology-linker.git
cd natura2000-ontology-linker

# Optional: change thresholds and settings (everything has a default)
cp .env.example .env

# Run the full data pipeline (about 20 minutes: it is polite to public APIs)
docker compose --profile pipeline up

# Start the visualization once the pipeline has finished
docker compose --profile viz up
```

Then open **http://localhost:8080** in your browser.

---

## Running Locally (without Docker)

### Prerequisites
- Python 3.11+

### Pipeline

```bash
# Install dependencies
pip install -r requirements.pipeline.txt

# Run the full pipeline
python -m pipeline.main --data-dir ./data

# Dry-run mode (processes ~50 terms, fast for testing)
python -m pipeline.main --dry-run --data-dir ./data

# Run specific steps only (e.g. just scrape + build ontology)
python -m pipeline.main --steps 1,2 --data-dir ./data
```

### Visualization

```bash
# Install dependencies
pip install -r requirements.viz.txt

# Start the server (after pipeline has run)
DATA_DIR=./data python viz/app.py
```

Open **http://localhost:8080**.

---

## Configuration

All settings are in `.env`:

| Variable | Default | Description |
|---|---|---|
| `SIMILARITY_EXACT` | `0.85` | Cosine similarity threshold for `skos:exactMatch` |
| `SIMILARITY_CLOSE` | `0.70` | Threshold for `skos:closeMatch` |
| `SIMILARITY_RELATED` | `0.55` | Threshold for `skos:relatedMatch` |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | SentenceTransformers model |
| `API_RATE_LIMIT` | `1.0` | Seconds between calls to Wikidata, DBpedia and OLS4 |
| `GBIF_RATE_LIMIT` | `0.2` | Seconds between GBIF name-match calls |
| `VIZ_PORT` | `8080` | Visualization server port |
| `DRY_RUN_SIZE` | `50` | Number of terms to process in dry-run mode |

---

## Pipeline Steps

### Step 1 — Scrape Natura 2000 Terms

**File:** `pipeline/scraper.py`

Fetches biodiversity terms from EEA and Natura 2000 data sources:

- **Annex I habitats**: EU habitat codes (1110 Sandbanks, 1150 Coastal lagoons, 9110 Beech forests, etc.) scraped from the EUNIS CSV export. Falls back to a curated reference list of ~28 representative habitats.
- **Habitats Directive species (Annex II/IV/V)**: Mammals, amphibians, reptiles, fish, invertebrates, and plants from the EUNIS species database. Falls back to ~23 key species.
- **Birds Directive species (Annex I)**: ~20 key bird species from the Birds Directive.
- **Biogeographical regions**: The 9 official regions (Alpine, Atlantic, Black Sea, Boreal, Continental, Macaronesian, Mediterranean, Pannonian, Steppic).
- **Conservation status categories**: CR, EN, VU, NT, LC, DD (IUCN categories).

All HTTP calls are rate-limited and resilient — if a source fails, it falls back gracefully.

**Output:** `data/terms.json`

Each term object:
```json
{
  "id": "habitat_1150_coastal_lagoons",
  "label": "1150 * Coastal lagoons",
  "category": "habitat",
  "definition": "...",
  "codes": ["1150"],
  "scientific_name": null,
  "common_names": [],
  "source_url": "https://eunis.eea.europa.eu/habitats/1150",
  "directive": "habitats",
  "annex": "I",
  "priority": true
}
```

---

### Step 2 — Build OWL 2 Ontology

**File:** `pipeline/ontology.py`

Constructs a formal OWL 2 ontology from the term register using `rdflib`.

**Namespace:** `https://ontology.natura2000.eu/`

**Class hierarchy:**
```
owl:Thing
└── N2K:Term
    ├── N2K:Habitat
    │   ├── N2K:CoastalHabitat     (codes 1xxx)
    │   ├── N2K:DuneHabitat        (codes 2xxx)
    │   ├── N2K:FreshwaterHabitat  (codes 3xxx)
    │   ├── N2K:HeathHabitat       (codes 4xxx)
    │   ├── N2K:GrasslandHabitat   (codes 6xxx)
    │   ├── N2K:BogHabitat         (codes 7xxx)
    │   ├── N2K:RockyHabitat       (codes 8xxx)
    │   └── N2K:ForestHabitat      (codes 9xxx)
    ├── N2K:Species
    │   ├── N2K:BirdSpecies
    │   ├── N2K:MammalSpecies
    │   ├── N2K:AmphibianSpecies
    │   ├── N2K:ReptileSpecies
    │   ├── N2K:FishSpecies
    │   ├── N2K:InvertebrateSpecies
    │   └── N2K:PlantSpecies
    ├── N2K:BiogeographicalRegion
    ├── N2K:ConservationStatus
    └── N2K:Directive
```

**Object properties:** `foundInHabitat`, `occursInRegion`, `hasConservationStatus`, `protectedBy`

**Data properties:** `euCode`, `scientificName`, `commonName`, `annexNumber`, `sourceURL`

**Outputs:** `data/ontology.ttl` (Turtle), `data/ontology.jsonld` (JSON-LD)

---

### Step 3 — Link to External Ontologies

**File:** `pipeline/linker.py`

Exact identifiers first, search second. Most links come from identifiers that curated databases already share, so they are exact rather than guessed.

| Term type | Source | How | Match |
|---|---|---|---|
| Habitats | Wikidata | Annex I code on the item (**P6436**), one bulk SPARQL query | `exactMatch` |
| Habitats | EUNIS | EUNIS habitat ID on the same Wikidata items (**P6681**) — the Annex I ↔ EUNIS crosswalk | `exactMatch` |
| Habitats | ENVO (OLS4) | search on the name and its singular form; scored on label **and synonyms** | exact / close; weak top hits become `relatedMatch` flagged for expert review |
| Species | GBIF | backbone name match; synonyms report their accepted name | `exactMatch` |
| Species | Wikidata | taxon name (**P225**) in bulk, retried under GBIF's accepted name for renamed species | `exactMatch` (homonyms → `closeMatch` + review) |
| Species | NCBITaxon | NCBI taxonomy ID on the Wikidata item (**P685**); otherwise an *exact* OLS4 name search — never fuzzy | `exactMatch` |
| Species | DBpedia | `dbr:Genus_species`, existence checked in bulk | `exactMatch` |
| Statuses | Wikidata | the IUCN category items | `exactMatch` |

Biogeographical regions have no curated counterpart in these sources yet and are left for experts.

**Outputs:** `data/direct_mappings.json`, `data/envo_candidates.json` (every ENVO class seen per habitat, input to step 4)

---

### Step 4 — Vector Similarity Matching

**File:** `pipeline/vector_match.py`

Scores each **habitat** against the ENVO classes OLS4 returned, by meaning: the habitat's name and definition against each class's label, synonyms and definition (SentenceTransformers embeddings, FAISS inner-product index). When the embedding agrees with a lexical hit, confidence goes up and a review flag is cleared.

Species are not embedded: they are identified by their binomial, which step 3 already resolved exactly. Embedding Latin names would pair unrelated taxa that merely sound alike.

| Score | Match type |
|---|---|
| ≥ 0.85 | `skos:exactMatch` |
| 0.70–0.85 | `skos:closeMatch` |
| 0.55–0.70 | `skos:relatedMatch` (flagged for expert review) |
| < 0.55 | discarded |

**Outputs:** `data/mappings.json`, `data/mappings.ttl` (SKOS, with one `owl:Axiom` per mapping carrying its confidence and method), `data/mappings.sssom.tsv` ([SSSOM](https://w3id.org/sssom), the OBO standard for sharing mappings)

---

## Data Files

| File | Description |
|---|---|
| `data/terms.json` | Natura 2000 term register (habitats, species, regions, statuses) |
| `data/ontology.ttl` / `.jsonld` | OWL 2 ontology (Turtle / JSON-LD) |
| `data/direct_mappings.json` | Identifier and lexical mappings (step 3) |
| `data/envo_candidates.json` | ENVO classes considered for each habitat (step 3 → 4) |
| `data/mappings.json` | Unified final mappings, read by the visualization |
| `data/mappings.ttl` | SKOS mappings with per-mapping confidence |
| `data/mappings.sssom.tsv` | The same mappings in SSSOM |
| `data/pipeline.log` | Full pipeline run log |

Mappings with `"review": true` are candidates for expert curation, not settled links.

---

## Visualization

The web app at http://localhost:8080 provides an interactive view of the linked ontology.

**Graph view:**
- Force-directed layout showing Natura 2000 terms as larger colored nodes
- External matches as smaller shape-coded nodes (diamond=Wikidata, rectangle=DBpedia, circle=OLS4/ENVO/NCBITaxon, triangle=GBIF, hexagon=EUNIS)
- Edge color indicates source: blue (Wikidata), orange (DBpedia), green (OLS4), purple (GBIF), gold (EUNIS)
- Edge style indicates match type: solid (exactMatch), dashed (closeMatch), dotted (relatedMatch)
- Edge thickness scales with confidence score

**Controls:**
- **Search bar** — real-time filtering by label or scientific name
- **Category checkboxes** — show/hide habitats, species, regions, statuses
- **Ontology checkboxes** — show/hide Wikidata, DBpedia, OLS4, GBIF and EUNIS connections
- **Confidence slider** — hide matches below threshold (default 0.50)
- **Table view** — sortable tabular alternative showing match counts per ontology

**Node detail panel:**
Click any node to see full term information: label, category, EU code, scientific name, definition, directive/annex, source URL, and all linked external terms with confidence scores and match types.

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/graph` | Cytoscape.js-ready nodes and edges (filterable) |
| `GET /api/term/<id>` | Single term with all its mappings |
| `GET /api/stats` | Summary statistics |
| `GET /api/terms` | Filterable term list (for table view) |

Query parameters for `/api/graph`:
- `category=habitat,species` — filter by category
- `ontology=wikidata,ols4,gbif,eunis,dbpedia` — filter by target
- `min_confidence=0.7` — minimum confidence threshold
- `search=wolf` — text search

---

## Notes for the Bio Team

- The visualization works fully **offline** once the pipeline has run — no live API calls from the browser
- Use `--dry-run` for quick testing: 50 terms sampled across every category (habitats, both directives' species, regions, statuses); finishes in under a minute
- Calls to Wikidata, DBpedia and OLS4 are rate-limited (1/s) and batched where the endpoint allows; GBIF's name matcher, built for bulk use, gets 5/s
- If any external service is unavailable, the pipeline logs a warning and continues — it will never crash on API failures
- Check `data/pipeline.log` for detailed progress and any warnings during the pipeline run
- To use the heavier SapBERT model for better biomedical term matching, set `EMBEDDING_MODEL=cambridgeltl/SapBERT-from-PubMedBERT` in `.env` (requires ~2GB more RAM)
- The ontology uses namespace `https://ontology.natura2000.eu/` and is compatible with standard OWL 2 reasoners (HermiT, Pellet, etc.)

---

## Troubleshooting

**"Pipeline data not found" in browser**
Run the pipeline first: `docker compose --profile pipeline up`

**External API timeouts during pipeline**
Normal — public endpoints can be slow. The pipeline logs these and continues. Increase `API_RATE_LIMIT` if needed.

**Docker memory issues (OOM during Step 4)**
SentenceTransformers loads a model into memory (~400MB for MiniLM, ~1.5GB for SapBERT). Increase Docker memory limit to at least 4GB in Docker Desktop → Settings → Resources.

**Port already in use**
Change `VIZ_PORT` in `.env` to another port (e.g., 8081).

**SPARQL queries returning no results**
Wikidata and DBpedia SPARQL endpoints occasionally have downtime. Run Step 3 again: `python -m pipeline.main --steps 3 --data-dir ./data`

---

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests are offline (no network, no model download) and cover sampling, label cleaning, code- and name-based linking, synonym handling, homonym flagging, match thresholds, merging, SSSOM output, ontology individuals and the geo app's coordinate parsing.

---

## License

MIT
