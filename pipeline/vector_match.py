"""
Step 4 — Vector Similarity Matching

Generates embeddings for Natura 2000 terms and external ontology terms,
builds FAISS indices, and finds semantic matches via cosine similarity.
Merges results with direct mappings from Step 3.

Input:  {data_dir}/terms.json
        {data_dir}/direct_mappings.json
Output: {data_dir}/mappings.json
        {data_dir}/mappings.ttl
"""

import json
import logging
import os
from pathlib import Path

import numpy as np
from tqdm import tqdm
from sentence_transformers import SentenceTransformer
import faiss
from rdflib import Graph, Namespace, RDF, Literal, URIRef
from rdflib.namespace import SKOS, XSD

logger = logging.getLogger("natura2000.vector_match")

N2K = Namespace("https://ontology.natura2000.eu/")

# Fallback corpus terms if linker found too few external terms
FALLBACK_TERMS = [
    {"uri": "http://purl.obolibrary.org/obo/ENVO_00000447", "label": "coastal lagoon", "ontology": "ols4_envo"},
    {"uri": "http://purl.obolibrary.org/obo/ENVO_00000054", "label": "freshwater lake", "ontology": "ols4_envo"},
    {"uri": "http://purl.obolibrary.org/obo/ENVO_00000027", "label": "sand dune", "ontology": "ols4_envo"},
    {"uri": "http://purl.obolibrary.org/obo/ENVO_01000174", "label": "forest biome", "ontology": "ols4_envo"},
    {"uri": "http://purl.obolibrary.org/obo/ENVO_00000043", "label": "grassland biome", "ontology": "ols4_envo"},
    {"uri": "http://purl.obolibrary.org/obo/ENVO_00001999", "label": "coastal wetland", "ontology": "ols4_envo"},
    {"uri": "http://purl.obolibrary.org/obo/ENVO_00000086", "label": "heath", "ontology": "ols4_envo"},
    {"uri": "http://purl.obolibrary.org/obo/ENVO_00000172", "label": "raised bog", "ontology": "ols4_envo"},
    {"uri": "http://purl.obolibrary.org/obo/ENVO_00000255", "label": "rocky shore", "ontology": "ols4_envo"},
    {"uri": "http://purl.obolibrary.org/obo/ENVO_00000144", "label": "reef", "ontology": "ols4_envo"},
    {"uri": "http://purl.ncbi.nlm.nih.gov/taxonomy/9612", "label": "Canis lupus", "ontology": "ols4_ncbitaxon"},
    {"uri": "http://purl.ncbi.nlm.nih.gov/taxonomy/61381", "label": "Lynx lynx", "ontology": "ols4_ncbitaxon"},
    {"uri": "http://purl.ncbi.nlm.nih.gov/taxonomy/9644", "label": "Ursus arctos", "ontology": "ols4_ncbitaxon"},
    {"uri": "http://purl.ncbi.nlm.nih.gov/taxonomy/9733", "label": "Lutra lutra", "ontology": "ols4_ncbitaxon"},
    {"uri": "http://purl.ncbi.nlm.nih.gov/taxonomy/8027", "label": "Salmo salar", "ontology": "ols4_ncbitaxon"},
    {"uri": "http://purl.ncbi.nlm.nih.gov/taxonomy/7994", "label": "Acipenser sturio", "ontology": "ols4_ncbitaxon"},
    {"uri": "http://purl.ncbi.nlm.nih.gov/taxonomy/8358", "label": "Triturus cristatus", "ontology": "ols4_ncbitaxon"},
    {"uri": "http://purl.ncbi.nlm.nih.gov/taxonomy/8404", "label": "Bombina bombina", "ontology": "ols4_ncbitaxon"},
    {"uri": "http://www.wikidata.org/entity/Q18594", "label": "Natura 2000", "ontology": "wikidata"},
    {"uri": "http://www.wikidata.org/entity/Q192408", "label": "continental shelf", "ontology": "wikidata"},
]


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

def prepare_text(term: dict) -> str:
    parts = [term.get("label", "")]
    if term.get("scientific_name"):
        parts.append(term["scientific_name"])
    if term.get("definition"):
        parts.append(term["definition"][:200])
    return " | ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# VectorMatcher
# ---------------------------------------------------------------------------

class VectorMatcher:
    def __init__(self, model_name: str):
        logger.info("Loading embedding model: %s", model_name)
        self.model = SentenceTransformer(model_name)

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        embeddings = self.model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.astype(np.float32)

    def build_index(self, embeddings: np.ndarray) -> faiss.IndexFlatIP:
        dim = embeddings.shape[1]
        index = faiss.IndexFlatIP(dim)
        index.add(embeddings)
        return index

    def search(self, query_embeddings: np.ndarray, index: faiss.IndexFlatIP, k: int = 5):
        k = min(k, index.ntotal)
        if k == 0:
            return np.array([]), np.array([])
        distances, indices = index.search(query_embeddings, k)
        return distances, indices


# ---------------------------------------------------------------------------
# Corpus construction
# ---------------------------------------------------------------------------

def build_corpora_from_mappings(direct_mappings: list[dict]) -> dict[str, list[dict]]:
    """Extract per-ontology corpora from direct mapping results."""
    corpora: dict[str, list[dict]] = {}
    seen: dict[str, set] = {}

    for m in direct_mappings:
        ont = m.get("source_ontology", "unknown")
        if ont not in corpora:
            corpora[ont] = []
            seen[ont] = set()
        uri = m.get("target_uri", "")
        if uri and uri not in seen[ont]:
            seen[ont].add(uri)
            corpora[ont].append({
                "uri": uri,
                "label": m.get("target_label", ""),
                "ontology": ont,
            })

    # Ensure we have at least one non-trivial corpus
    if not corpora or all(len(v) < 5 for v in corpora.values()):
        logger.info("Insufficient direct mappings corpus; using fallback terms")
        for ft in FALLBACK_TERMS:
            ont = ft["ontology"]
            if ont not in corpora:
                corpora[ont] = []
                seen[ont] = set()
            if ft["uri"] not in seen[ont]:
                seen[ont].add(ft["uri"])
                corpora[ont].append(ft)

    for ont, terms in corpora.items():
        logger.info("Corpus '%s': %d terms", ont, len(terms))

    return corpora


# ---------------------------------------------------------------------------
# Thresholds → match type
# ---------------------------------------------------------------------------

def score_to_match_type(score: float, thresholds: dict) -> str | None:
    if score >= thresholds["exact"]:
        return "skos:exactMatch"
    elif score >= thresholds["close"]:
        return "skos:closeMatch"
    elif score >= thresholds["related"]:
        return "skos:relatedMatch"
    return None


# ---------------------------------------------------------------------------
# SKOS Turtle output
# ---------------------------------------------------------------------------

def save_mappings_ttl(mappings: list[dict], output_path: Path) -> None:
    g = Graph()
    g.bind("n2k", N2K)
    g.bind("skos", SKOS)
    g.bind("xsd", XSD)

    skos_map = {
        "skos:exactMatch": SKOS.exactMatch,
        "skos:closeMatch": SKOS.closeMatch,
        "skos:relatedMatch": SKOS.relatedMatch,
    }

    for m in mappings:
        source_frag = m["source_id"].replace("/", "_")
        source_uri = N2K[source_frag]
        target_uri = URIRef(m["target_uri"])
        match_pred = skos_map.get(m["match_type"], SKOS.closeMatch)
        g.add((source_uri, match_pred, target_uri))
        # Annotate with confidence
        conf_node = Literal(round(float(m["confidence"]), 4), datatype=XSD.decimal)
        g.add((source_uri, N2K.confidence, conf_node))

    g.serialize(destination=str(output_path), format="turtle")
    logger.info("Saved SKOS mappings to %s (%d triples)", output_path, len(g))


# ---------------------------------------------------------------------------
# Merge with direct mappings
# ---------------------------------------------------------------------------

def merge_mappings(direct: list[dict], vector: list[dict]) -> list[dict]:
    combined: dict[tuple, dict] = {}

    for m in direct:
        key = (m["source_id"], m["target_uri"])
        combined[key] = dict(m)
        combined[key]["method"] = combined[key].get("method", "direct")

    for m in vector:
        key = (m["source_id"], m["target_uri"])
        if key in combined:
            existing = combined[key]
            new_conf = min(1.0, max(existing["confidence"], m["confidence"]) + 0.05)
            existing["confidence"] = new_conf
            existing["method"] = "combined"
            # Use the higher-confidence match type
            type_rank = {"skos:exactMatch": 3, "skos:closeMatch": 2, "skos:relatedMatch": 1}
            if type_rank.get(m["match_type"], 0) > type_rank.get(existing["match_type"], 0):
                existing["match_type"] = m["match_type"]
        else:
            combined[key] = dict(m)

    return list(combined.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_vector_matching(data_dir: str, dry_run: bool = False) -> list[dict]:
    data_path = Path(data_dir)

    with (data_path / "terms.json").open("r", encoding="utf-8") as fh:
        terms: list[dict] = json.load(fh)

    direct_mappings: list[dict] = []
    dm_path = data_path / "direct_mappings.json"
    if dm_path.exists():
        with dm_path.open("r", encoding="utf-8") as fh:
            direct_mappings = json.load(fh)
        logger.info("Loaded %d direct mappings", len(direct_mappings))
    else:
        logger.warning("direct_mappings.json not found; proceeding without direct mappings")

    if dry_run:
        terms = terms[:30]
        logger.info("Dry-run mode: limiting to 30 terms")

    thresholds = {
        "exact": float(os.environ.get("SIMILARITY_EXACT", "0.85")),
        "close": float(os.environ.get("SIMILARITY_CLOSE", "0.70")),
        "related": float(os.environ.get("SIMILARITY_RELATED", "0.55")),
    }
    model_name = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

    matcher = VectorMatcher(model_name)

    # Encode Natura 2000 terms
    logger.info("Encoding %d Natura 2000 terms", len(terms))
    n2k_texts = [prepare_text(t) for t in terms]
    n2k_embeddings = matcher.encode(n2k_texts)

    # Build corpora and indices
    corpora = build_corpora_from_mappings(direct_mappings)

    vector_mappings: list[dict] = []

    for ontology_name, corpus in corpora.items():
        if not corpus:
            continue

        logger.info("Building FAISS index for '%s' (%d terms)", ontology_name, len(corpus))
        corpus_texts = [c.get("label", "") for c in corpus]
        corpus_embeddings = matcher.encode(corpus_texts)
        index = matcher.build_index(corpus_embeddings)

        logger.info("Running similarity search against '%s'", ontology_name)
        distances, indices = matcher.search(n2k_embeddings, index, k=5)

        if distances.size == 0:
            continue

        for i, term in enumerate(tqdm(terms, desc=f"Matching vs {ontology_name}", leave=False)):
            for rank in range(distances.shape[1]):
                score = float(distances[i, rank])
                corpus_idx = int(indices[i, rank])
                if corpus_idx < 0 or corpus_idx >= len(corpus):
                    continue
                match_type = score_to_match_type(score, thresholds)
                if match_type is None:
                    continue
                target = corpus[corpus_idx]
                vector_mappings.append({
                    "source_id": term["id"],
                    "source_label": term.get("label", ""),
                    "target_uri": target["uri"],
                    "target_label": target.get("label", ""),
                    "match_type": match_type,
                    "confidence": round(score, 4),
                    "source_ontology": ontology_name,
                    "method": "vector_similarity",
                })

    logger.info("Vector matches found: %d", len(vector_mappings))

    unified = merge_mappings(direct_mappings, vector_mappings)
    logger.info("Unified mappings (direct + vector, deduped): %d", len(unified))

    # Save JSON
    json_path = data_path / "mappings.json"
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(unified, fh, indent=2, ensure_ascii=False)
    logger.info("Saved unified mappings to %s", json_path)

    # Save Turtle
    ttl_path = data_path / "mappings.ttl"
    save_mappings_ttl(unified, ttl_path)

    # Summary stats
    by_type: dict[str, int] = {}
    by_ont: dict[str, int] = {}
    for m in unified:
        by_type[m["match_type"]] = by_type.get(m["match_type"], 0) + 1
        by_ont[m["source_ontology"]] = by_ont.get(m["source_ontology"], 0) + 1
    logger.info("Match types: %s", by_type)
    logger.info("By ontology: %s", by_ont)

    return unified


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Step 4: Vector similarity matching.")
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_vector_matching(args.data_dir, args.dry_run)
