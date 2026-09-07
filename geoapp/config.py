"""Configuration for the BioSample geo knowledge-graph app.

Every setting can be overridden with an environment variable, so the same code
runs from the CLI, from docker-compose, and from the Flask dev server.
"""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Paths -----------------------------------------------------------------

DATA_DIR = _env_path("GEO_DATA_DIR", _REPO_ROOT / "data")
CACHE_DIR = _env_path("GEO_CACHE_DIR", DATA_DIR / "cache")

# Input: one accession per line (SRS/ERS/DRS secondary, or SAMN/SAMEA/SAMD).
ACCESSIONS_FILE = _env_path("GEO_ACCESSIONS", _REPO_ROOT / "accessions.txt")

# Pipeline artefacts.
SAMPLES_JSON = DATA_DIR / "geo_samples.json"     # normalised sample records
KG_JSON = DATA_DIR / "geo_kg.json"               # knowledge graph nodes/edges
KG_TURTLE = DATA_DIR / "geo_kg.ttl"              # same graph as RDF
GEOJSON = DATA_DIR / "geo_samples.geojson"       # map layer
RUN_LOG = DATA_DIR / "geoapp.log"

# --- External services -----------------------------------------------------

BIOSAMPLES_API = "https://www.ebi.ac.uk/biosamples/samples"
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OLS4_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

ENA_BROWSER = "https://www.ebi.ac.uk/ena/browser/view/"
NCBI_BIOSAMPLE = "https://www.ncbi.nlm.nih.gov/biosample/"
NCBI_SRA = "https://www.ncbi.nlm.nih.gov/sra/"

# NCBI allows 3 req/s anonymously, 10 req/s with an API key.
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()
NCBI_EMAIL = os.environ.get("NCBI_EMAIL", "").strip()
NCBI_RATE = _env_float("NCBI_RATE", 10.0 if NCBI_API_KEY else 3.0)
EBI_RATE = _env_float("EBI_RATE", 8.0)

USER_AGENT = os.environ.get(
    "GEO_USER_AGENT",
    "natura2000-ontology-linker/0.1 (AAU Bio Collaboration)",
)

# --- Fetching --------------------------------------------------------------

HTTP_TIMEOUT = _env_float("GEO_HTTP_TIMEOUT", 30.0)
HTTP_RETRIES = _env_int("GEO_HTTP_RETRIES", 3)
FETCH_WORKERS = _env_int("GEO_FETCH_WORKERS", 8)
CACHE_TTL_DAYS = _env_int("GEO_CACHE_TTL_DAYS", 30)

# Ontologies queried through OLS4 when a value carries no inline PURL.
OLS4_ONTOLOGIES = [
    o.strip()
    for o in os.environ.get("GEO_OLS4_ONTOLOGIES", "envo,ncbitaxon,uberon,po").split(",")
    if o.strip()
]
OLS4_MIN_SCORE = _env_float("GEO_OLS4_MIN_SCORE", 0.60)
ENRICH_ONTOLOGY = os.environ.get("GEO_ENRICH_ONTOLOGY", "1") not in ("0", "false", "no")
ENRICH_WIKIDATA = os.environ.get("GEO_ENRICH_WIKIDATA", "1") not in ("0", "false", "no")

# --- Server ----------------------------------------------------------------

HOST = os.environ.get("GEO_HOST", "0.0.0.0")
PORT = _env_int("GEO_PORT", 8090)
DEBUG = os.environ.get("GEO_DEBUG", "0") not in ("0", "false", "no")


def ensure_dirs() -> None:
    """Create the data and cache directories if they do not exist yet."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
