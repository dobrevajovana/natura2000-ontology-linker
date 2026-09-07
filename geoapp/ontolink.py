"""Ontology enrichment for BioSample records.

``Sample.ontology_terms`` already carries the PURLs the archive supplied
inline (``origin="biosamples"``). This module fills the gaps: attribute
values that carry no inline PURL are looked up against OLS4 (and, for
scientific names, Wikidata) and the resulting :class:`~geoapp.schema.OntologyTerm`
objects are appended to the matching samples in place.

Lookups are deduplicated across the whole batch (the same organism string
appears in hundreds of samples), cached on disk, rate-limited and retried
with backoff. Nothing in here is allowed to raise out of :func:`enrich` --
a dead endpoint just means fewer matches.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

import requests

from . import config, schema
from .schema import OntologyTerm, Sample

logger = logging.getLogger(__name__)

# --- field -> ontology routing ----------------------------------------------

_TAXON_FIELDS = {"organism", "scientific_name", "host scientific name", "host", "serovar"}
_ENVO_FIELDS = {
    "isolation source", "environmental sample", "env_broad_scale", "env_local_scale",
    "env_medium", "environment (biome)", "environment (feature)", "environment (material)",
    "habitat", "biome", "title", "description",
}
_UBERON_FIELDS = {"tissue", "organism part", "body site", "host tissue sampled"}
_PO_FIELDS = {"plant structure", "plant part"}

FIELD_ONTOLOGY_MAP: dict[str, str] = {}
for _f in _TAXON_FIELDS:
    FIELD_ONTOLOGY_MAP[_f] = "ncbitaxon"
for _f in _ENVO_FIELDS:
    FIELD_ONTOLOGY_MAP[_f] = "envo"
for _f in _UBERON_FIELDS:
    FIELD_ONTOLOGY_MAP[_f] = "uberon"
for _f in _PO_FIELDS:
    FIELD_ONTOLOGY_MAP[_f] = "po"

# Wikidata (P225 taxon name) only makes sense for genuine scientific names.
SCIENTIFIC_NAME_FIELDS = {"organism", "scientific_name", "host scientific name"}

ONTOLOGY_ID_TO_SOURCE = {
    "ncbitaxon": "NCBITaxon",
    "envo": "ENVO",
    "uberon": "UBERON",
    "po": "PO",
}

# Free-text fields worth mining for environment terms, but also the fields where
# submitter-internal sample codes leak in; screened by _looks_like_identifier.
FREE_TEXT_FIELDS = {"title", "description", "sample comment", "sample_comment"}

_PLACEHOLDERS = {
    "not collected", "not applicable", "not provided", "missing", "unknown",
    "n/a", "na", "none", "-", "restricted access",
}


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDERS


def _looks_like_identifier(value: str) -> bool:
    """True for lab codes and sample names, which no ontology will ever hold.

    Free-text fields such as ``title`` are a genuine source of environment
    terms ("Bulk soil in the rice paddy field"), but they just as often carry a
    submitter's internal label -- ``ARMS-Laesoe1-190819-200811-MF100-A-18S``,
    ``BLxDC7``, ``HRECC+10S/15``. Sending those to OLS4 burns requests and can
    only return noise, so they are screened out before the lookup.
    """
    v = value.strip()
    if not v:
        return True

    words = v.split()
    digits = sum(c.isdigit() for c in v)
    if digits / len(v) > 0.30:
        return True
    # A single token that mixes case or punctuation with digits is a code,
    # not a term -- but plain hyphenated words ("sub-surface") are fine.
    if len(words) == 1 and digits and any(c in v for c in "-_+/.:" ) :
        return True
    if len(words) == 1 and digits and not v.islower() and not v.istitle():
        return True
    return False


def _iter_candidate_fields(sample: Sample):
    if sample.organism:
        yield "organism", sample.organism
    if sample.title:
        yield "title", sample.title
    for k, v in sample.attributes.items():
        if v:
            yield k, v


# --- HTTP plumbing: rate limiting, retries, cache ---------------------------


class _RateLimiter:
    """Thread-safe token-bucket-of-one rate limiter."""

    def __init__(self, rate_per_sec: float):
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next = now + self._min_interval


_ols4_limiter = _RateLimiter(config.EBI_RATE)
_wikidata_limiter = _RateLimiter(1.0)

_CACHE_SUBDIR = "ontology"


def _cache_dir() -> Path:
    d = config.CACHE_DIR / _CACHE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(value: str, ontology: str, origin: str) -> str:
    raw = f"{origin}|{ontology}|{value.strip().lower()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cache_read(key: str) -> Optional[list[dict]]:
    path = _cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    ts = payload.get("ts", 0)
    if time.time() - ts > config.CACHE_TTL_DAYS * 86400:
        return None
    return payload.get("results")


def _cache_write(key: str, results: list[dict]) -> None:
    path = _cache_dir() / f"{key}.json"
    try:
        path.write_text(json.dumps({"ts": time.time(), "results": results}), encoding="utf-8")
    except Exception:
        logger.debug("failed to write ontology cache entry %s", path)


def _request_json(
    session: requests.Session, url: str, *, params: dict, headers: dict,
    limiter: _RateLimiter, timeout: float | None = None, retries: int | None = None,
) -> Optional[dict]:
    timeout = timeout if timeout is not None else config.HTTP_TIMEOUT
    retries = retries if retries is not None else config.HTTP_RETRIES
    last_exc: Exception | None = None
    for attempt in range(retries):
        limiter.acquire()
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
        else:
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_exc = requests.HTTPError(f"HTTP {resp.status_code} from {url}")
            else:
                try:
                    resp.raise_for_status()
                    return resp.json()
                except Exception as exc:
                    last_exc = exc
        if attempt < retries - 1:
            time.sleep(min(2 ** attempt, 20) + random.uniform(0, 0.5))
    logger.warning("request to %s failed after %d attempt(s): %s", url, retries, last_exc)
    return None


# --- lookups -----------------------------------------------------------------


def _ols4_query(query: str, ontology: str, value: str,
                session: requests.Session) -> list[OntologyTerm]:
    """Run one OLS4 search, scoring every hit against ``value``, not ``query``.

    Keeping the score anchored to the original attribute text is what stops a
    broadened fallback query from being reported as an exact match.
    """
    params = {
        "q": query, "ontology": ontology, "rows": 3, "exact": "false",
        "fieldList": "iri,label,ontology_prefix,short_form,description",
    }
    headers = {"Accept": "application/json", "User-Agent": config.USER_AGENT}
    data = _request_json(session, config.OLS4_SEARCH, params=params, headers=headers, limiter=_ols4_limiter)
    if not data:
        return []

    docs = ((data.get("response") or {}).get("docs")) or []
    out: list[OntologyTerm] = []
    vlow = value.strip().lower()
    for doc in docs:
        iri = doc.get("iri") or ""
        label = doc.get("label") or doc.get("short_form") or ""
        if isinstance(label, list):
            label = label[0] if label else ""
        if not iri or not label:
            continue
        score = difflib.SequenceMatcher(None, vlow, str(label).lower()).ratio()
        if score < config.OLS4_MIN_SCORE:
            continue
        match_type = "exactMatch" if score >= 0.95 else "closeMatch" if score >= 0.80 else "relatedMatch"
        curie, source = schema.curie_from_uri(iri)
        if not source:
            continue
        out.append(OntologyTerm(
            uri=iri, curie=curie, source=source, label=str(label),
            confidence=round(score, 4), match_type=match_type, origin="ols4",
            matched_value=value, matched_field="",
        ))
    return out


def ols4_search(value: str, ontology: str, session: requests.Session) -> list[OntologyTerm]:
    """Search OLS4 for ``value`` restricted to ``ontology``, scored by label similarity.

    OLS4 effectively ANDs the query tokens against term labels, so a descriptive
    phrase such as "coastal lagoon" returns nothing even though ENVO holds
    "lagoon". When the full phrase finds no acceptable hit we retry with
    progressively shorter tails, which walks a modifier-plus-head-noun phrase
    down to its head noun. Those broader hits still score against the original
    text, so they land as relatedMatch rather than masquerading as exact.
    """
    try:
        hits = _ols4_query(value, ontology, value, session)
        if hits:
            return hits

        tokens = value.strip().split()
        for start in range(1, min(len(tokens), 4)):
            tail = " ".join(tokens[start:])
            if len(tail) < 3:
                break
            hits = _ols4_query(tail, ontology, value, session)
            if hits:
                logger.debug("ols4 fallback %r -> %r matched %s", value, tail, hits[0].curie)
                return hits
        return []
    except Exception:
        logger.exception("ols4_search failed for value=%r ontology=%r", value, ontology)
        return []


def wikidata_taxon(scientific_name: str, session: requests.Session) -> list[OntologyTerm]:
    """Look up a scientific name against Wikidata's taxon-name property (P225)."""
    try:
        if not config.ENRICH_WIKIDATA or not scientific_name:
            return []
        name = scientific_name.strip()
        if not name or '"' in name or "\\" in name:
            return []
        query = (
            'SELECT ?item ?itemLabel WHERE { ?item wdt:P225 "%s". '
            'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } } LIMIT 3'
        ) % name
        headers = {"Accept": "application/sparql-results+json", "User-Agent": config.USER_AGENT}
        data = _request_json(
            session, config.WIKIDATA_SPARQL, params={"query": query, "format": "json"},
            headers=headers, limiter=_wikidata_limiter,
        )
        if not data:
            return []
        out: list[OntologyTerm] = []
        for b in ((data.get("results") or {}).get("bindings")) or []:
            item = (b.get("item") or {}).get("value", "")
            label = (b.get("itemLabel") or {}).get("value", name)
            if not item:
                continue
            curie, source = schema.curie_from_uri(item)
            out.append(OntologyTerm(
                uri=item, curie=curie, source=source or "wikidata", label=label,
                confidence=0.95, match_type="exactMatch", origin="wikidata",
                matched_value=scientific_name, matched_field="",
            ))
        return out
    except Exception:
        logger.exception("wikidata_taxon failed for name=%r", scientific_name)
        return []


def _cached_lookup(kind: str, value: str, ontology: str, session: requests.Session, use_cache: bool) -> list[OntologyTerm]:
    origin = "ols4" if kind == "ols4" else "wikidata"
    key = _cache_key(value, ontology, origin)
    if use_cache:
        cached = _cache_read(key)
        if cached is not None:
            try:
                return [OntologyTerm(**d) for d in cached]
            except Exception:
                pass
    terms = ols4_search(value, ontology, session) if kind == "ols4" else wikidata_taxon(value, session)
    if use_cache:
        _cache_write(key, [t.to_dict() for t in terms])
    return terms


def _add_term(sample: Sample, term: OntologyTerm) -> None:
    for existing in sample.ontology_terms:
        if existing.uri == term.uri and existing.origin == term.origin and existing.matched_field == term.matched_field:
            return
    sample.ontology_terms.append(term)


def enrich(
    samples: list[Sample], use_cache: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> None:
    """Append OLS4/Wikidata ontology matches to samples lacking an inline PURL.

    Mutates ``samples`` in place. Never raises -- a broken endpoint simply
    results in no additional matches.
    """
    if not config.ENRICH_ONTOLOGY or not samples:
        return
    try:
        _enrich_impl(samples, use_cache, progress)
    except Exception:
        logger.exception("ontology enrichment failed; continuing without extra matches")


def _enrich_impl(samples: list[Sample], use_cache: bool, progress: Callable[[int, int, str], None] | None) -> None:
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})

    ols4_tasks: dict[tuple[str, str], list[tuple[int, str]]] = {}
    wikidata_tasks: dict[str, list[tuple[int, str]]] = {}

    for i, sample in enumerate(samples):
        covered_sources = {t.source for t in sample.ontology_terms if t.origin == "biosamples"}
        for field, raw_value in _iter_candidate_fields(sample):
            value = (raw_value or "").strip()
            if not value or _is_placeholder(value) or not (3 <= len(value) <= 120):
                continue
            fkey = field.strip().lower()
            if fkey in FREE_TEXT_FIELDS and _looks_like_identifier(value):
                continue
            ontology = FIELD_ONTOLOGY_MAP.get(fkey)
            if not ontology or ontology not in config.OLS4_ONTOLOGIES:
                continue
            source_key = ONTOLOGY_ID_TO_SOURCE.get(ontology)
            if source_key and source_key in covered_sources:
                continue
            ols4_tasks.setdefault((value, ontology), []).append((i, field))
            if config.ENRICH_WIKIDATA and fkey in SCIENTIFIC_NAME_FIELDS:
                wikidata_tasks.setdefault(value, []).append((i, field))

    total = len(ols4_tasks) + len(wikidata_tasks)
    if total == 0:
        return
    done = 0
    ols4_results: dict[tuple[str, str], list[OntologyTerm]] = {}
    wikidata_results: dict[str, list[OntologyTerm]] = {}

    with ThreadPoolExecutor(max_workers=max(1, config.FETCH_WORKERS)) as pool:
        futures = {}
        for (value, ontology) in ols4_tasks:
            fut = pool.submit(_cached_lookup, "ols4", value, ontology, session, use_cache)
            futures[fut] = ("ols4", (value, ontology))
        for value in wikidata_tasks:
            fut = pool.submit(_cached_lookup, "wikidata", value, "", session, use_cache)
            futures[fut] = ("wikidata", value)

        for fut in as_completed(futures):
            kind, key = futures[fut]
            try:
                terms = fut.result()
            except Exception:
                logger.exception("lookup task failed for %s/%r", kind, key)
                terms = []
            if kind == "ols4":
                ols4_results[key] = terms
            else:
                wikidata_results[key] = terms
            done += 1
            if progress is not None:
                try:
                    progress(done, total, f"{kind}:{key}")
                except Exception:
                    logger.debug("progress callback raised", exc_info=True)

    for (value, ontology), occurrences in ols4_tasks.items():
        for term in ols4_results.get((value, ontology), []):
            for (i, field) in occurrences:
                _add_term(samples[i], replace(term, matched_field=field))

    for value, occurrences in wikidata_tasks.items():
        for term in wikidata_results.get(value, []):
            for (i, field) in occurrences:
                _add_term(samples[i], replace(term, matched_field=field))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    print("--- live smoke test: ols4_search ---")
    with requests.Session() as s:
        s.headers.update({"User-Agent": config.USER_AGENT})
        for value, ontology in [("marine metagenome", "ncbitaxon"), ("soil", "envo")]:
            hits = ols4_search(value, ontology, s)
            print(f"{value!r} @ {ontology}: {len(hits)} hit(s)")
            for h in hits:
                print(f"   {h.curie:20s} {h.match_type:14s} conf={h.confidence:.2f}  {h.label!r}  {h.uri}")

    print("\n--- enrich() over synthetic samples ---")
    demo_samples = [
        Sample(
            accession="SAMDEMO1", organism="Escherichia coli", title="gut sample",
            attributes={"isolation source": "soil", "tissue": "liver"},
        ),
        Sample(
            accession="SAMDEMO2", organism="Escherichia coli", title="another sample",
            attributes={"habitat": "marine sediment"},
        ),
    ]
    enrich(demo_samples, use_cache=True, progress=lambda d, t, m: print(f"  [{d}/{t}] {m}"))
    for sm in demo_samples:
        print(f"{sm.accession}: {len(sm.ontology_terms)} ontology term(s)")
        for t in sm.ontology_terms:
            print(f"   {t.origin:8s} {t.source:10s} {t.curie:16s} field={t.matched_field!r} conf={t.confidence:.2f}")
