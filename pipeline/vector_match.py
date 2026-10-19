"""
Step 4 — Vector Similarity Matching

Scores Natura 2000 habitats against the ENVO classes OLS4 returned in step 3, by meaning:
habitat name + definition against class label + synonyms + definition, embedded with
SentenceTransformers and compared in a FAISS inner-product index.

Only habitats are matched this way. Species are identified by their binomial, which step 3
already resolved exactly; embedding Latin names would only pair unrelated taxa that sound alike.

Input:  {data_dir}/terms.json
        {data_dir}/direct_mappings.json
        {data_dir}/envo_candidates.json
Output: {data_dir}/mappings.json         unified mappings (viewer input)
        {data_dir}/mappings.ttl          SKOS links + one owl:Axiom per mapping with its confidence
        {data_dir}/mappings.sssom.tsv    the same mappings in SSSOM, the OBO standard for mappings
"""

import os

# FAISS and PyTorch each bring an OpenMP runtime; on macOS the two crash together in
# multi-threaded search. One thread is plenty for a few thousand vectors.
os.environ.setdefault("OMP_NUM_THREADS", "1")

import csv
import json
import logging
import re
from pathlib import Path

import numpy as np
import faiss
from sentence_transformers import SentenceTransformer
from rdflib import BNode, Graph, Literal, Namespace, OWL, RDF, URIRef
from rdflib.namespace import SKOS, XSD

faiss.omp_set_num_threads(1)

logger = logging.getLogger("natura2000.vector_match")

N2K = Namespace("https://ontology.natura2000.eu/")

TYPE_RANK = {"skos:exactMatch": 3, "skos:closeMatch": 2, "skos:relatedMatch": 1}

# how each method justifies its mapping, in SSSOM's vocabulary (semapv)
JUSTIFICATION = {
    "wikidata_p6436_code": "semapv:DatabaseCrossReference",
    "wikidata_p6681_eunis": "semapv:DatabaseCrossReference",
    "wikidata_p225_taxon_name": "semapv:LexicalMatching",
    "wikidata_p685_ncbi": "semapv:DatabaseCrossReference",
    "wikidata_p846_gbif": "semapv:DatabaseCrossReference",
    "wikidata_iucn_category": "semapv:LexicalMatching",
    "ols4_exact_name": "semapv:LexicalMatching",
    "ols4_lexical": "semapv:LexicalMatching",
    "ols4_synonym": "semapv:LexicalMatching",
    "dbpedia_direct_uri": "semapv:LexicalMatching",
    "vector_similarity": "semapv:SemanticSimilarityThresholdMatching",
    "combined": "semapv:CompositeMatching",
}


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

def _clean(label: str) -> str:
    return re.sub(r"^\d[0-9A-Z]{3}\s*\*?\s*", "", label or "").strip()   # Annex I codes include 91E0, 91D0


def habitat_text(term: dict) -> str:
    parts = [_clean(term.get("label", ""))]
    if term.get("definition"):
        parts.append(term["definition"][:300])
    return ". ".join(p for p in parts if p)


def candidate_text(c: dict) -> str:
    parts = [c.get("label", "")]
    if c.get("synonyms"):
        parts.append("also called " + ", ".join(c["synonyms"][:5]))
    if c.get("definition"):
        parts.append(c["definition"][:300])
    return ". ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# VectorMatcher
# ---------------------------------------------------------------------------

class VectorMatcher:
    def __init__(self, model_name: str):
        logger.info("Loading embedding model: %s", model_name)
        self.model = SentenceTransformer(model_name)

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        return self.model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                 show_progress_bar=False).astype(np.float32)

    @staticmethod
    def build_index(embeddings: np.ndarray) -> faiss.IndexFlatIP:
        index = faiss.IndexFlatIP(embeddings.shape[1])
        index.add(embeddings)
        return index


def score_to_match_type(score: float, thresholds: dict) -> str | None:
    if score >= thresholds["exact"]:
        return "skos:exactMatch"
    if score >= thresholds["close"]:
        return "skos:closeMatch"
    if score >= thresholds["related"]:
        return "skos:relatedMatch"
    return None


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_mappings(direct: list[dict], vector: list[dict]) -> list[dict]:
    combined: dict[tuple, dict] = {(m["source_id"], m["target_uri"]): dict(m) for m in direct}
    for m in vector:
        key = (m["source_id"], m["target_uri"])
        if key not in combined:
            combined[key] = dict(m)
            continue
        existing = combined[key]
        # agreement between a lexical hit and the embedding raises confidence a little
        existing["confidence"] = round(min(1.0, max(existing["confidence"], m["confidence"]) + 0.05), 4)
        existing["method"] = "combined"
        existing["vector_score"] = m["confidence"]
        if TYPE_RANK.get(m["match_type"], 0) > TYPE_RANK.get(existing["match_type"], 0):
            existing["match_type"] = m["match_type"]
        if existing.get("review") and m["match_type"] != "skos:relatedMatch":
            existing.pop("review")     # the embedding confirms what lexical search only guessed
    return list(combined.values())


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _source_uri(m: dict) -> URIRef:
    return N2K[re.sub(r"[^a-zA-Z0-9_]", "_", m["source_id"]).strip("_")[:80]]


def save_mappings_ttl(mappings: list[dict], output_path: Path) -> None:
    """SKOS triples, plus an owl:Axiom per mapping so each confidence belongs to one mapping."""
    g = Graph()
    g.bind("n2k", N2K); g.bind("skos", SKOS); g.bind("owl", OWL); g.bind("xsd", XSD)
    pred = {"skos:exactMatch": SKOS.exactMatch, "skos:closeMatch": SKOS.closeMatch, "skos:relatedMatch": SKOS.relatedMatch}
    for m in mappings:
        s, p, o = _source_uri(m), pred.get(m["match_type"], SKOS.relatedMatch), URIRef(m["target_uri"])
        g.add((s, p, o))
        ax = BNode()
        g.add((ax, RDF.type, OWL.Axiom))
        g.add((ax, OWL.annotatedSource, s))
        g.add((ax, OWL.annotatedProperty, p))
        g.add((ax, OWL.annotatedTarget, o))
        g.add((ax, N2K.confidence, Literal(round(float(m["confidence"]), 4), datatype=XSD.decimal)))
        g.add((ax, N2K.method, Literal(m.get("method", ""))))
        if m.get("review"):
            g.add((ax, N2K.needsReview, Literal(True)))
    g.serialize(destination=str(output_path), format="turtle")
    logger.info("Saved SKOS mappings to %s (%d triples)", output_path, len(g))


def save_mappings_sssom(mappings: list[dict], output_path: Path) -> None:
    """Simple Standard for Sharing Ontological Mappings (https://w3id.org/sssom)."""
    header = [
        "# curie_map:",
        "#   n2k: https://ontology.natura2000.eu/",
        "#   skos: http://www.w3.org/2004/02/skos/core#",
        "#   semapv: https://w3id.org/semapv/vocab/",
        "# mapping_set_id: https://ontology.natura2000.eu/mappings.sssom.tsv",
        "# license: https://opensource.org/licenses/MIT",
    ]
    cols = ["subject_id", "subject_label", "predicate_id", "object_id", "object_label",
            "mapping_justification", "confidence", "object_source", "mapping_tool", "comment"]
    with output_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(header) + "\n")
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(cols)
        for m in sorted(mappings, key=lambda x: (x["source_id"], -x["confidence"])):
            w.writerow([
                "n2k:" + str(_source_uri(m)).rsplit("/", 1)[-1], m.get("source_label", ""), m["match_type"],
                m["target_uri"], m.get("target_label", ""), JUSTIFICATION.get(m.get("method", ""), "semapv:UnspecifiedMatching"),
                m["confidence"], m.get("source_ontology", ""), "natura2000-ontology-linker/" + m.get("method", ""),
                "needs expert review" if m.get("review") else "",
            ])
    logger.info("Saved SSSOM mappings to %s", output_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_vector_matching(data_dir: str, dry_run: bool = False) -> list[dict]:
    data_path = Path(data_dir)
    terms: list[dict] = json.loads((data_path / "terms.json").read_text(encoding="utf-8"))

    direct: list[dict] = []
    if (data_path / "direct_mappings.json").exists():
        direct = json.loads((data_path / "direct_mappings.json").read_text(encoding="utf-8"))
        logger.info("Loaded %d direct mappings", len(direct))
    else:
        logger.warning("direct_mappings.json not found; proceeding without direct mappings")

    candidates: dict[str, list[dict]] = {}
    if (data_path / "envo_candidates.json").exists():
        candidates = json.loads((data_path / "envo_candidates.json").read_text(encoding="utf-8"))

    thresholds = {
        "exact": float(os.environ.get("SIMILARITY_EXACT", "0.85")),
        "close": float(os.environ.get("SIMILARITY_CLOSE", "0.70")),
        "related": float(os.environ.get("SIMILARITY_RELATED", "0.55")),
    }

    habitats = [t for t in terms if t.get("category") == "habitat" and candidates.get(t["id"])]
    # one pooled corpus of every ENVO class seen, so a habitat can also meet classes found for its neighbours
    pool: dict[str, dict] = {}
    for cands in candidates.values():
        for c in cands:
            pool.setdefault(c["uri"], c)
    corpus = list(pool.values())

    vector: list[dict] = []
    if habitats and corpus:
        matcher = VectorMatcher(os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2"))
        logger.info("Encoding %d habitats against %d ENVO classes", len(habitats), len(corpus))
        h_emb = matcher.encode([habitat_text(h) for h in habitats])
        index = matcher.build_index(matcher.encode([candidate_text(c) for c in corpus]))
        scores, idx = index.search(h_emb, min(5, len(corpus)))
        for i, h in enumerate(habitats):
            for score, j in zip(scores[i], idx[i]):
                if j < 0:
                    continue
                mtype = score_to_match_type(float(score), thresholds)
                if mtype is None:
                    continue
                c = corpus[j]
                vector.append({
                    "source_id": h["id"], "source_label": h.get("label", ""),
                    "target_uri": c["uri"], "target_label": c.get("label", ""),
                    "target_definition": c.get("definition", ""),
                    "match_type": mtype, "confidence": round(float(score), 4),
                    "source_ontology": "ols4_envo", "method": "vector_similarity",
                    **({"review": True} if mtype == "skos:relatedMatch" else {}),
                })
    else:
        logger.info("No ENVO candidates to score (no habitats in this run or OLS4 returned nothing)")
    logger.info("Vector matches found: %d", len(vector))

    unified = merge_mappings(direct, vector)
    logger.info("Unified mappings (direct + vector, deduped): %d", len(unified))

    (data_path / "mappings.json").write_text(json.dumps(unified, indent=2, ensure_ascii=False), encoding="utf-8")
    save_mappings_ttl(unified, data_path / "mappings.ttl")
    save_mappings_sssom(unified, data_path / "mappings.sssom.tsv")

    by_type: dict[str, int] = {}
    by_ont: dict[str, int] = {}
    for m in unified:
        by_type[m["match_type"]] = by_type.get(m["match_type"], 0) + 1
        by_ont[m["source_ontology"]] = by_ont.get(m["source_ontology"], 0) + 1
    logger.info("Match types: %s", by_type)
    logger.info("By target: %s", by_ont)
    logger.info("Flagged for expert review: %d", sum(1 for m in unified if m.get("review")))
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
