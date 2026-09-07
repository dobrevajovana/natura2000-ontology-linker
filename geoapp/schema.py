"""Shared data contracts.

These dataclasses are the single source of truth for what flows between the
fetch stage, the geolocation stage, the ontology linker, the graph builder and
the Flask API. Everything serialises to plain JSON via ``to_dict``.
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# --- Controlled vocabularies used across the app ---------------------------

NODE_KINDS = ("sample", "taxon", "environment", "place", "archive", "project", "attribute")
EDGE_KINDS = (
    "collected_at",     # sample  -> place
    "has_taxon",        # sample  -> taxon
    "in_environment",   # sample  -> environment
    "archived_in",      # sample  -> archive
    "part_of",          # sample  -> project
    "has_attribute",    # sample  -> attribute
    "within",           # place   -> place  (locality -> country)
    "ontology_match",   # attribute/taxon/environment -> external ontology term
)

# Ontology prefixes we recognise in a PURL and how to present them.
ONTOLOGY_SOURCES = {
    "NCBITaxon": {"label": "NCBI Taxonomy", "color": "#2e7d32"},
    "ENVO": {"label": "Environment Ontology", "color": "#00796b"},
    "UBERON": {"label": "Uberon", "color": "#8e24aa"},
    "PO": {"label": "Plant Ontology", "color": "#558b2f"},
    "CHEBI": {"label": "ChEBI", "color": "#c62828"},
    "OBI": {"label": "OBI", "color": "#5d4037"},
    "PATO": {"label": "PATO", "color": "#6a1b9a"},
    "GAZ": {"label": "Gazetteer", "color": "#ef6c00"},
    # OLS4 routinely answers with terms from ontologies neighbouring the one we
    # asked for; these show up in real runs and would otherwise render unstyled.
    "EFO": {"label": "Experimental Factor Ontology", "color": "#00695c"},
    "BTO": {"label": "BRENDA Tissue Ontology", "color": "#4527a0"},
    "CL": {"label": "Cell Ontology", "color": "#ad1457"},
    "UO": {"label": "Units of Measurement", "color": "#616161"},
    "wikidata": {"label": "Wikidata", "color": "#1565c0"},
    "dbpedia": {"label": "DBpedia", "color": "#ef6c00"},
}

# Archives a sample can be cross-referenced into.
ARCHIVE_SOURCES = {
    "biosample": {"label": "BioSample", "color": "#455a64"},
    "ena": {"label": "ENA", "color": "#0277bd"},
    "sra": {"label": "SRA", "color": "#00838f"},
    "bioproject": {"label": "BioProject", "color": "#37474f"},
}


@dataclass
class OntologyTerm:
    """A single external ontology term a sample attribute resolves to."""

    uri: str                       # full PURL, e.g. http://purl.obolibrary.org/obo/ENVO_00002030
    curie: str                     # e.g. ENVO:00002030
    source: str                    # key into ONTOLOGY_SOURCES, e.g. "ENVO"
    label: str = ""                # human-readable term label
    confidence: float = 1.0        # 1.0 for PURLs supplied by the archive
    match_type: str = "exactMatch"  # skos exactMatch / closeMatch / relatedMatch
    origin: str = "biosamples"     # biosamples | ols4 | wikidata
    matched_value: str = ""        # the raw attribute text that produced it
    matched_field: str = ""        # the attribute name it came from

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Location:
    """Normalised geolocation for one sample.

    ``lat``/``lon`` are WGS84 decimal degrees. ``precision`` records how the
    coordinates were obtained, which the map uses to style the marker:
      * ``coordinates`` - explicit lat/lon in the record (trustworthy)
      * ``locality``    - centroid of a named region/locality
      * ``country``     - country centroid fallback (low precision)
      * ``none``        - no location could be derived
    """

    lat: Optional[float] = None
    lon: Optional[float] = None
    precision: str = "none"
    country: str = ""
    country_code: str = ""          # ISO 3166-1 alpha-2 where known
    region: str = ""                # region / locality / sea
    raw_geo_text: str = ""          # the untouched source string
    raw_latlon_text: str = ""
    source_field: str = ""          # which attribute produced the coordinates

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lon is not None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["has_coords"] = self.has_coords
        return d


@dataclass
class ArchiveRef:
    """A cross-reference from a sample into a sequence archive."""

    source: str          # key into ARCHIVE_SOURCES
    accession: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Sample:
    """One normalised BioSample record."""

    accession: str                      # primary BioSample accession (SAMN/SAMEA/SAMD)
    query_accession: str = ""           # what the user asked for (may be SRS/ERS/DRS)
    name: str = ""
    title: str = ""
    organism: str = ""
    tax_id: Optional[int] = None
    collection_date: str = ""
    release_date: str = ""
    center_name: str = ""
    checklist: str = ""                 # ENA checklist, e.g. ERC000011
    location: Location = field(default_factory=Location)
    ontology_terms: list[OntologyTerm] = field(default_factory=list)
    archives: list[ArchiveRef] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)   # flattened characteristics
    source_url: str = ""
    fetch_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession": self.accession,
            "query_accession": self.query_accession,
            "name": self.name,
            "title": self.title,
            "organism": self.organism,
            "tax_id": self.tax_id,
            "collection_date": self.collection_date,
            "release_date": self.release_date,
            "center_name": self.center_name,
            "checklist": self.checklist,
            "location": self.location.to_dict(),
            "ontology_terms": [t.to_dict() for t in self.ontology_terms],
            "archives": [a.to_dict() for a in self.archives],
            "attributes": self.attributes,
            "source_url": self.source_url,
            "fetch_error": self.fetch_error,
        }


@dataclass
class Node:
    """A knowledge-graph node, shaped for Cytoscape.js consumption."""

    id: str
    label: str
    kind: str                                   # one of NODE_KINDS
    props: dict[str, Any] = field(default_factory=dict)
    lat: Optional[float] = None                 # set on sample/place nodes
    lon: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Edge:
    """A knowledge-graph edge."""

    id: str
    source: str
    target: str
    kind: str                                   # one of EDGE_KINDS
    label: str = ""
    confidence: float = 1.0
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def curie_from_uri(uri: str) -> tuple[str, str]:
    """Split an ontology PURL into ``(curie, source)``.

    Handles OBO PURLs, OLS4 IRIs and Wikidata/DBpedia entity URIs. Returns
    ``("", "")`` for anything unrecognised.
    """
    if not uri:
        return "", ""
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    if "wikidata.org" in uri:
        return tail, "wikidata"
    if "dbpedia.org" in uri:
        return tail, "dbpedia"
    # identifiers.org names the source in the path, not in the local id, so the
    # generic prefix split below would leave these with no source at all.
    if "identifiers.org/" in uri:
        parts = uri.split("identifiers.org/", 1)[1].strip("/").split("/")
        if len(parts) >= 2:
            prefix = parts[0]
            known = next((k for k in ONTOLOGY_SOURCES if k.lower() == prefix.lower()), prefix)
            return f"{known}:{parts[-1]}", known
    if "#" in tail:
        tail = tail.rsplit("#", 1)[-1]
    for sep in ("_", ":"):
        if sep in tail:
            prefix, local = tail.split(sep, 1)
            for known in ONTOLOGY_SOURCES:
                if prefix.upper() == known.upper():
                    return f"{known}:{local}", known
            return f"{prefix}:{local}", prefix
    return tail, ""
