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
# Clone the repository
git clone <repo-url>
cd natura2000-ontology-linker

# Optional: edit thresholds and settings
cp .env .env.local

# Run the full data pipeline
docker-compose --profile pipeline up

# Start the visualization (after pipeline completes)
docker-compose --profile viz up

# Or run both in sequence
docker-compose --profile pipeline --profile viz up
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
| `OLS4_ONTOLOGIES` | `envo,ncbitaxon,chebi` | Ontologies to query via OLS4 API |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | SentenceTransformers model |
| `API_RATE_LIMIT` | `1.0` | Seconds between external API calls |
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

Queries three external knowledge bases for matching terms.

**Wikidata**
- Species: SPARQL query on property P225 (taxon name) → high-confidence exact matches
- Habitats: Label-based SPARQL search
- Rate-limited, 30s timeout

**DBpedia**
- DBpedia Lookup API for all terms (label search, top 3 results)
- Direct URI construction for species: `dbr:Canis_lupus` — validated via ASK query
- Scores adjusted by string similarity (SequenceMatcher)

**OLS4 (EBI)**
- `https://www.ebi.ac.uk/ols4/api/search` REST API
- ENVO (Environment Ontology) for habitats
- NCBITaxon for species
- Top 3 results per ontology

All matches are classified as `skos:exactMatch` or `skos:closeMatch` based on string similarity and result rank.

**Output:** `data/direct_mappings.json`

---

### Step 4 — Vector Similarity Matching

**File:** `pipeline/vector_match.py`

Finds semantic matches between Natura 2000 terms and external ontology terms using dense vector similarity.

**Process:**
1. Encode all Natura 2000 terms using SentenceTransformers (text = `label | scientific_name | definition[:200]`)
2. Build FAISS `IndexFlatIP` (inner-product index on L2-normalized vectors = cosine similarity)
3. For each Natura 2000 term, retrieve top-5 nearest neighbours from each target ontology corpus
4. Apply similarity thresholds to assign match type
5. Merge with Step 3 direct mappings (deduplicate by source+target URI, boost confidence for combined matches)

**Thresholds:**
| Score | Match type |
|---|---|
| ≥ 0.85 | `skos:exactMatch` |
| 0.70–0.85 | `skos:closeMatch` |
| 0.55–0.70 | `skos:relatedMatch` |
| < 0.55 | discarded |

**Outputs:** `data/mappings.json` (unified JSON), `data/mappings.ttl` (SKOS Turtle)

---

## Data Files

| File | Description |
|---|---|
| `data/terms.json` | Natura 2000 term register (~150 terms) |
| `data/ontology.ttl` | OWL 2 ontology in Turtle format |
| `data/ontology.jsonld` | OWL 2 ontology in JSON-LD format |
| `data/direct_mappings.json` | Mappings from SPARQL/API queries (Step 3) |
| `data/mappings.json` | Unified final mappings (Step 3 + Step 4) |
| `data/mappings.ttl` | SKOS mappings in Turtle format |
| `data/pipeline.log` | Full pipeline run log |

---

## Visualization

The web app at http://localhost:8080 provides an interactive view of the linked ontology.

**Graph view:**
- Force-directed layout showing Natura 2000 terms as larger colored nodes
- External ontology matches as smaller shape-coded nodes (diamond=Wikidata, rectangle=DBpedia, circle=OLS4)
- Edge color indicates source: blue (Wikidata), orange (DBpedia), green (OLS4)
- Edge style indicates match type: solid (exactMatch), dashed (closeMatch), dotted (relatedMatch)
- Edge thickness scales with confidence score

**Controls:**
- **Search bar** — real-time filtering by label or scientific name
- **Category checkboxes** — show/hide habitats, species, regions, statuses
- **Ontology checkboxes** — show/hide Wikidata, DBpedia, OLS4 connections
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
- `ontology=wikidata,ols4` — filter by ontology
- `min_confidence=0.7` — minimum confidence threshold
- `search=wolf` — text search

---

## Notes for the Bio Team

- The visualization works fully **offline** once the pipeline has run — no live API calls from the browser
- Use `--dry-run` flag for quick testing (processes ~50 terms, completes in minutes)
- All external API calls are rate-limited to 1 request/second per the terms of service of each provider
- If any external service is unavailable, the pipeline logs a warning and continues — it will never crash on API failures
- Check `data/pipeline.log` for detailed progress and any warnings during the pipeline run
- To use the heavier SapBERT model for better biomedical term matching, set `EMBEDDING_MODEL=cambridgeltl/SapBERT-from-PubMedBERT` in `.env` (requires ~2GB more RAM)
- The ontology uses namespace `https://ontology.natura2000.eu/` and is compatible with standard OWL 2 reasoners (HermiT, Pellet, etc.)

---

## Troubleshooting

**"Pipeline data not found" in browser**
Run the pipeline first: `docker-compose --profile pipeline up`

**External API timeouts during pipeline**
Normal — public endpoints can be slow. The pipeline logs these and continues. Increase `API_RATE_LIMIT` if needed.

**Docker memory issues (OOM during Step 4)**
SentenceTransformers loads a model into memory (~400MB for MiniLM, ~1.5GB for SapBERT). Increase Docker memory limit to at least 4GB in Docker Desktop → Settings → Resources.

**Port already in use**
Change `VIZ_PORT` in `.env` to another port (e.g., 8081).

**SPARQL queries returning no results**
Wikidata and DBpedia SPARQL endpoints occasionally have downtime. Run Step 3 again: `python -m pipeline.main --steps 3 --data-dir ./data`

---

## License

MIT
