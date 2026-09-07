# Natura 2000 Ontology Linker — Partner Pitch

## The Problem

Europe's biodiversity data is fragmented.

The Natura 2000 network protects over **27,000 sites** across the EU — home to 231 habitat types and more than 1,700 protected species. But this rich dataset lives in isolation. Researchers, environmental agencies, and conservation planners cannot easily connect Natura 2000 data to the broader world of biological knowledge: species taxonomy databases, environmental ontologies, open linked data, or AI-ready knowledge graphs.

The result? Duplicated work, incompatible datasets, and missed insights — at a time when biodiversity loss demands faster, smarter responses.

---

## What We Built

**The Natura 2000 Ontology Linker** is an open-source pipeline that automatically bridges this gap.

It takes the entire Natura 2000 term register — every protected habitat, every directive species, every biogeographical region — and links it to established international knowledge bases:

- **Wikidata** — the world's largest open knowledge graph
- **DBpedia** — structured data extracted from Wikipedia
- **ENVO** — the Environment Ontology, used in ecological research globally
- **NCBITaxon** — the authoritative taxonomy database from the National Center for Biotechnology Information

Every link is scored with a **semantic confidence value** and classified using SKOS standards (`exactMatch`, `closeMatch`, `relatedMatch`), making the output directly usable in semantic web applications, AI pipelines, and data integration workflows.

---

## How It Works

```
Natura 2000 Sources (GBIF, EEA, EU Directives)
        ↓  1,955 terms scraped automatically
  OWL 2 Ontology (formal class hierarchy)
        ↓  published as Turtle + JSON-LD
  External Linking (SPARQL + REST APIs)
        ↓  14,000+ mappings generated
  Vector Similarity Matching (SentenceTransformers + FAISS)
        ↓  semantic nearest-neighbour search
  Interactive Visualization (Cytoscape.js)
        ↓
  http://localhost:8080  —  explore, filter, click
```

The full pipeline runs in a single Docker Compose command. No manual data entry. No hand-coded mappings. Everything is reproducible and extensible.

---

## Why This Matters

### For Researchers
Connect your biodiversity field data to global ontologies without manual curation. Run semantic queries across Natura 2000 and ENVO in a single SPARQL endpoint.

### For Environmental Agencies
Build interoperable reporting systems. The OWL 2 ontology is compatible with HermiT, Pellet, and other standard OWL reasoners — enabling automated consistency checks and inference.

### For AI & Data Science Teams
1,700+ species and 231 habitats, pre-linked to structured knowledge graphs, ready for use as training data, retrieval-augmented generation (RAG) corpora, or knowledge graph embeddings.

### For the Open Science Community
All outputs — ontology, mappings, visualisation — are MIT-licensed and fully reproducible from public data sources.

---

## Current State

| Component | Status |
|---|---|
| Automated scraper (GBIF live data) | ✅ Production-ready |
| OWL 2 ontology builder | ✅ Production-ready |
| External linker (Wikidata, DBpedia, OLS4) | ✅ Production-ready |
| Vector similarity matcher (MiniLM / SapBERT) | ✅ Production-ready |
| Interactive web visualization | ✅ Production-ready |
| Docker Compose deployment | ✅ One-command setup |

**Coverage:** 1,955 terms · 14,500+ mappings · 229 Annex I habitats · 1,450 Habitats Directive species · 261 Birds Directive species

---

## What We Are Looking For

We are seeking partners who can contribute to or benefit from this infrastructure:

### Data Partners
- Environmental agencies with Natura 2000 site-level data
- Research institutions with species occurrence datasets
- NGOs maintaining regional biodiversity inventories

### Technical Partners
- Groups working on biodiversity informatics or semantic web
- Teams building AI applications for conservation or land management
- Developers of GBIF, EUNIS, or related platforms

### Funding & Application Partners
- EU Horizon Europe consortia addressing biodiversity and digital twins
- Interreg or LIFE programme projects requiring semantic data infrastructure
- Universities seeking to embed this tool in ecological informatics curricula

---

## What Partnership Looks Like

We are flexible. Depending on your context, collaboration could mean:

- **Co-development** — extend the pipeline with new data sources or ontologies
- **Integration** — embed the linked ontology into your existing data infrastructure
- **Application** — use the knowledge graph in a specific conservation or research use case
- **Co-authorship** — joint publication on the methodology and results
- **Grant application** — build a joint proposal around semantic biodiversity data

---

## Get Started in 5 Minutes

```bash
git clone <repo-url>
cd natura2000-ontology-linker
docker-compose --profile pipeline up
# Then open http://localhost:8080
```

The visualization lets you explore every term, filter by habitat type or species group, adjust confidence thresholds, and click any node to see its full set of linked external concepts.

---

## Contact

Built at **Aalborg University** as part of the AAU Bio Collaboration initiative.

For partnership enquiries, technical questions, or to schedule a demo, please reach out to the project team.

---

*"Biodiversity data should work as hard as the ecosystems it describes."*
