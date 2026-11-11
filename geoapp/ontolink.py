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
from urllib.parse import quote

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

# MIxS pins two of the environment triad to an ENVO branch: env_broad_scale is a
# biome, env_medium an environmental material. Searching only that branch is what
# tells "forest" the biome from "forest" the area. env_local_scale stays open --
# MIxS accepts features there, and UBERON anatomy for host-associated samples.
# Each branch: (root IRI, generic head words its labels add: "wetland" -> "wetland area").
ENVO_BRANCHES: dict[str, tuple[str, frozenset[str]]] = {
    "biome": ("http://purl.obolibrary.org/obo/ENVO_00000428", frozenset({"biome", "area"})),
    "material": ("http://purl.obolibrary.org/obo/ENVO_00010483", frozenset({"material"})),
    # Not ENVO, but the same mechanism: OBI's assays, for a run's library ("metagenome").
    "assay": ("http://purl.obolibrary.org/obo/OBI_0000070", frozenset({"assay", "sequencing", "profiling"})),
}
FIELD_BRANCH_MAP = {
    "env_broad_scale": "biome", "environment (biome)": "biome", "biome": "biome",
    "env_medium": "material", "environment (material)": "material",
}

# MIxS accepts UBERON anatomy in env_local_scale and env_medium for
# host-associated samples ("colon", "lower respiratory tract"), and isolation
# source is often anatomy too. Those fields try UBERON when ENVO has no exact
# or close match.
FIELD_ALSO_MAP = {
    "env_local_scale": "uberon", "env_medium": "uberon", "isolation source": "uberon",
    "environment (feature)": "uberon", "environment (material)": "uberon",
}

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


def should_look_up(field: str, value: str, ontology: str) -> bool:
    """Whether ``value`` from attribute ``field`` is worth an OLS4 request.

    Lab codes turn up in every field, not only free text: real isolation
    sources include "p17.W1", "29707" and "V000307258.fastq.gz". Taxon fields
    are spared the identifier screen -- "SAR11" is a name, not a code.
    """
    if not value or _is_placeholder(value) or not (3 <= len(value) <= 120):
        return False
    if ontology != "ncbitaxon" and _looks_like_identifier(value):
        return False
    return True


# Words that describe the sampling, not the environment: "fecal sample",
# "Soil CK replicate 1", "soil metagenome".
_FILLER_WORDS = {"sample", "samples", "metagenome", "metagenomes", "specimen", "specimens",
                 "replicate", "replicates", "rep", "isolate", "isolates", "swab", "swabs"}


# Botanical shorthand in Natura 2000 names ("Chenopodion rubri p.p.", "Quercus spp."):
# no ontology holds it, and OLS4 times out on "p.p" (over 40 s, where the same
# phrase without it answers in 0.6 s), which stalled every link to habitat 3270.
_BOTANICAL_ABBREVIATIONS = {"p.p", "pp", "s.l", "sl", "s.str", "s.s", "agg", "sp", "spp", "ssp",
                            "subsp", "var", "cf", "aff", "sensu", "lato", "stricto"}


def core_text(value: str) -> str:
    """The words of ``value`` an ontology can hold.

    Drops tokens carrying digits ("#7", "Hour3-4", "12"), single characters and
    sampling words, and cuts a trailing "from ..." source phrase: "microbial mat
    from Paralvinella palmiformis worms" is a microbial mat. "of" phrases are
    kept -- in "serosa of intestine" both halves matter.
    """
    words = []
    for token in value.split():
        if token.lower() == "from" and words:
            break
        bare = token.strip(".,;:()[]{}'\"-_/")
        if (len(bare) < 2 or any(c.isdigit() or c == "#" for c in bare) or bare.lower() in _FILLER_WORDS
                or bare.lower().rstrip(".") in _BOTANICAL_ABBREVIATIONS):
            continue
        words.append(bare)
    return " ".join(words)


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


# Bump when match classification changes, so cached terms are not served with stale types.
_CACHE_VERSION = 13


def _cache_key(value: str, ontology: str, origin: str, branch: str = "") -> str:
    raw = f"v{_CACHE_VERSION}|{origin}|{ontology}|{branch}|{value.strip().lower()}".encode("utf-8")
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


def _norm(text: str) -> str:
    """Lower-case, punctuation and underscores to spaces, whitespace collapsed."""
    return " ".join("".join(c if c.isalnum() else " " for c in str(text).lower()).split())


def _str_list(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    return [x for x in raw if isinstance(x, str)]


# In 'serosa of intestine' or 'soil from rice paddy' the head noun comes first,
# so the trailing words name something else and say nothing about hierarchy.
_PREPOSITIONS = {"of", "from", "in", "on", "at", "near", "with", "without", "for", "by", "under", "into"}


def _ends_with_words(longer: str, shorter: str) -> bool:
    """True when ``shorter`` is the head noun phrase of ``longer``: 'coastal lagoon' / 'lagoon'."""
    lw, sw = longer.split(), shorter.split()
    return (0 < len(sw) < len(lw) and lw[-len(sw):] == sw
            and not _PREPOSITIONS.intersection(lw))


# Confidence per rule, set to the precision each rule's top hit measured on the
# GOLD and microntology benchmarks (validation/benchmark.py), so a confidence
# means what it says. String similarity is only precise when the words differ by
# a plural; otherwise it is right about a third of the time and tops out at
# relatedMatch. The head-noun rules are the weakest evidence of all and are kept
# only so a reviewer can see them. "hierarchy" (parent links) was not in that
# measurement and borrows the typed-synonym value, the other ontology-asserted
# relation.
_CONFIDENCE = {
    "id": 1.0,                  # the text is the term's ID
    "exact": 0.90,              # label or exact synonym                    90%
    "plural": 0.90,             # same words bar a plural                   93%
    "ambiguous": 0.90,          # exact name shared by several terms        7/7
    "related_synonym": 0.85,    #                                           17/17
    "synonym": 0.75,            # typed broad / narrow synonym              75%
    "hierarchy": 0.75,          # ancestor / descendant of the one exact hit
    "field_head": 0.70,         # 'wetland' -> 'wetland area' in a branch   68%
    "similarity": 0.35,         # string similarity alone                   36%
    "head_broad": 0.30,         # 'coastal lagoon' -> 'lagoon'              29%
    "head_narrow": 0.15,        # 'sea water' -> 'coastal sea water'        12%
}

# OLS4's own ranking is poor -- for "sea water" ENVO's "sea water" is not in the
# top 3 -- so fetch more, classify them all, and keep the best few by our ranking.
_FETCH_ROWS = 10
_KEEP_HITS = 3
_TYPE_RANK = {"exactMatch": 4, "closeMatch": 3, "broadMatch": 2, "narrowMatch": 2, "relatedMatch": 1}


def classify_hit(value: str, doc: dict, heads: frozenset[str] = frozenset()) -> tuple[str, float] | None:
    """SKOS match type and confidence for attribute text ``value`` against one OLS4 hit.

    exactMatch needs evidence, not just a high score: the text is the term's
    label, one of its *exact* synonyms (spacing aside: 'sea water' / 'seawater'),
    or its ID. Inside a field-restricted ENVO branch, ``heads`` are the generic
    words its labels add, so 'wetland' in env_broad_scale meeting 'wetland area'
    is a closeMatch: the field supplies the word, but the names still differ.
    The ontology's typed synonyms then settle broad and narrow: a narrow synonym
    means the text names something inside the term ('scat' -> feces), so the
    term is broader. The head-noun rule covers the rest: 'coastal lagoon' ->
    lagoon is a broadMatch, 'intestine' -> large intestine a narrowMatch. Only
    when none of that applies does string similarity decide, and it tops out
    at relatedMatch -- except for words that differ by a plural only, a
    closeMatch. Confidences come from :data:`_CONFIDENCE`.
    """
    verdict = _classify(value, doc, heads)
    return verdict[:2] if verdict else None


def _classify(value: str, doc: dict, heads: frozenset[str]) -> tuple[str, float, bool] | None:
    """:func:`classify_hit` plus whether string similarity alone decided the type.

    Only those similarity verdicts may later be overruled by the hierarchy;
    a type the ontology's synonyms or labels gave is better evidence than a
    parent link seen from another hit.
    """
    v = _norm(value)
    label = _norm(doc.get("label") or "")
    exact = {label} | {_norm(s) for s in _str_list(doc.get("exact_synonyms"))}
    exact.discard("")
    ids = {_norm(doc.get(k) or "") for k in ("obo_id", "short_form")} - {""}

    vs = " ".join(_singular(w) for w in v.split())   # 'active raised bogs' meets 'bog'
    if v in ids:
        mtype, rule = "exactMatch", "id"
    elif v in exact or v.replace(" ", "") in {e.replace(" ", "") for e in exact}:
        mtype, rule = "exactMatch", "exact"
    elif any(_plural_variant(v, e) for e in exact):
        mtype, rule = "closeMatch", "plural"
    elif heads and any(e.rsplit(" ", 1)[-1] in heads and e.rsplit(" ", 1)[0] == v for e in exact if " " in e):
        mtype, rule = "closeMatch", "field_head"
    elif v in {_norm(s) for s in _str_list(doc.get("narrow_synonyms"))}:
        mtype, rule = "broadMatch", "synonym"
    elif v in {_norm(s) for s in _str_list(doc.get("broad_synonyms"))}:
        mtype, rule = "narrowMatch", "synonym"
    elif v in {_norm(s) for s in _str_list(doc.get("related_synonyms"))}:
        mtype, rule = "relatedMatch", "related_synonym"
    elif any(_ends_with_words(t, e) for t in (v, vs) for e in exact):
        mtype, rule = "broadMatch", "head_broad"
    elif any(_ends_with_words(e, t) for t in (v, vs) for e in exact):
        mtype, rule = "narrowMatch", "head_narrow"
    else:
        score = max((difflib.SequenceMatcher(None, v, e).ratio() for e in exact), default=0.0)
        if score < config.OLS4_MIN_SCORE:
            return None
        return "relatedMatch", _CONFIDENCE["similarity"], True
    # A head-noun guess is weaker than similarity, so the hierarchy may overrule it too.
    return mtype, _CONFIDENCE[rule], rule in ("head_broad", "head_narrow")


def _plural_variant(a: str, b: str) -> bool:
    """Same words except that some differ by a plural ending: 'soils' / 'soil', 'estuaries' / 'estuary'."""
    aw, bw = a.split(), b.split()
    if len(aw) != len(bw) or aw == bw:
        return False
    return all(x == y or _plural_of(x, y) or _plural_of(y, x) for x, y in zip(aw, bw))


def _plural_of(plural: str, singular: str) -> bool:
    return (plural in (singular + "s", singular + "es")
            or (singular.endswith("y") and len(singular) > 2 and plural == singular[:-1] + "ies"))


def _singular(word: str) -> str:
    """A cautious English singular: 'estuaries' -> 'estuary', 'lagoons' -> 'lagoon', 'grass' stays."""
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return word[:-3] + "y"
    if len(w) > 4 and w.endswith(("sses", "shes", "ches", "xes")):
        return word[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


class LookupFailed(Exception):
    """The endpoint could not be reached -- unlike an empty result, never cached."""


# --- hierarchy: broad/narrow from the ontology's parent links -----------------

# OLS4's v2 class endpoint, which lists every ancestor of a term in one response.
_OLS4_CLASS = config.OLS4_SEARCH.rsplit("/search", 1)[0] + "/v2/ontologies/{ontology}/classes/{iri}"

# Each narrowMatch check costs a request for that hit's ancestors, so only the
# best few similarity hits are checked -- enough to fill the kept slice.
_NARROW_CHECKS = _KEEP_HITS


def _ols4_ancestors(iri: str, ontology: str, session: requests.Session) -> Optional[frozenset[str]]:
    """IRIs of every ancestor of ``iri``, or None when OLS4 could not be asked.

    ``hierarchicalAncestor`` rather than ``directAncestor``: it follows part_of
    as well as subClassOf, and for anatomy and environments partonomy is most
    of the hierarchy -- caecum is *part of* the large intestine, not a kind of
    intestine. Ancestor sets are cached on disk like search results; a failed
    fetch returns None and is not cached, so the next run asks again.
    """
    key = _cache_key(iri, ontology, "ols4_ancestors")
    cached = _cache_read(key)
    if cached is not None:
        return frozenset(cached)
    # OLS4 wants the IRI URL-encoded twice inside the path.
    url = _OLS4_CLASS.format(ontology=ontology, iri=quote(quote(iri, safe=""), safe=""))
    headers = {"Accept": "application/json", "User-Agent": config.USER_AGENT}
    data = _request_json(session, url, params={}, headers=headers, limiter=_ols4_limiter)
    if not isinstance(data, dict):
        return None
    ancestors = _str_list(data.get("hierarchicalAncestor"))
    _cache_write(key, ancestors)
    return frozenset(ancestors)


def _apply_hierarchy(hits: list[tuple[OntologyTerm, bool]], below: list[OntologyTerm], ontology: str,
                     session: requests.Session) -> list[OntologyTerm]:
    """Retype similarity-only hits by their place relative to the one exact hit.

    When the text names exactly one term (the anchor), the other hits OLS4
    returns are often its relatives with unlike names: for 'intestine', 'large
    intestine' sits below it. String similarity calls those closeMatch or
    relatedMatch; the hierarchy says which way they lie. An ancestor of the
    anchor is broader than the text (broadMatch), a descendant narrower
    (narrowMatch). Ancestry includes part_of, so 'serosa of intestine' counts
    as narrower than 'intestine': defensible for locating a sample, though it
    is a part, not a kind. Exact hits and types decided by synonyms or labels
    are never touched, and any failed fetch just leaves the hits as they were.

    ``below`` are hits too dissimilar to keep ('caecum' / 'intestine'). The
    anchor's ancestors are fetched anyway, so testing them costs nothing: one
    that is an ancestor is kept as a broadMatch, the rest stay dropped.
    """
    terms = [t for t, _ in hits]
    anchors = {t.uri for t in terms if t.match_type == "exactMatch"}
    if len(anchors) != 1:
        return terms
    [anchor] = anchors
    movable = [i for i, (t, by_similarity) in enumerate(hits) if by_similarity and t.uri != anchor]
    if not movable and not below:
        return terms

    above = _ols4_ancestors(anchor, ontology, session) or frozenset()
    terms += [replace(t, match_type="broadMatch", confidence=_CONFIDENCE["hierarchy"])
              for t in below if t.uri in above]
    for i in movable:
        if terms[i].uri in above:
            terms[i] = replace(terms[i], match_type="broadMatch", confidence=_CONFIDENCE["hierarchy"])
    # Narrow needs each candidate's own ancestors, so check only the best few.
    rest = sorted((i for i in movable if terms[i].match_type != "broadMatch"),
                  key=lambda i: terms[i].confidence, reverse=True)[:_NARROW_CHECKS]
    for i in rest:
        if anchor in (_ols4_ancestors(terms[i].uri, ontology, session) or ()):
            terms[i] = replace(terms[i], match_type="narrowMatch", confidence=_CONFIDENCE["hierarchy"])
    return terms


def _ols4_query(query: str, ontology: str, value: str,
                session: requests.Session, branch: str = "") -> list[OntologyTerm]:
    """Run one OLS4 search, classifying every hit against ``value``, not ``query``.

    Keeping the classification anchored to the original attribute text is what
    stops a broadened fallback query from being reported as an exact match.
    ``branch`` (a key of :data:`ENVO_BRANCHES`) limits the search to that subtree.
    """
    params = {
        "q": query, "ontology": ontology, "rows": _FETCH_ROWS, "exact": "false",
        "fieldList": "iri,label,ontology_prefix,short_form,obo_id,description,"
                     "exact_synonyms,narrow_synonyms,broad_synonyms,related_synonyms",
    }
    heads: frozenset[str] = frozenset()
    if branch:
        root, heads = ENVO_BRANCHES[branch]
        params["allChildrenOf"] = root
    headers = {"Accept": "application/json", "User-Agent": config.USER_AGENT}
    data = _request_json(session, config.OLS4_SEARCH, params=params, headers=headers, limiter=_ols4_limiter)
    if data is None:
        raise LookupFailed(f"OLS4 search failed for {query!r} in {ontology}")

    docs = ((data.get("response") or {}).get("docs")) or []
    out: list[tuple[OntologyTerm, bool]] = []
    below: list[OntologyTerm] = []   # too dissimilar, unless the hierarchy vouches for them
    for doc in docs:
        iri = doc.get("iri") or ""
        label = doc.get("label") or doc.get("short_form") or ""
        if isinstance(label, list):
            label = label[0] if label else ""
        if not iri or not label:
            continue
        curie, source = schema.curie_from_uri(iri)
        if not source:
            # Not an OBO PURL (GOLD's are w3id.org/gold.vocab/Estuary), so the IRI
            # does not name its ontology; the one searched does.
            source = ontology.upper()
            curie = f"{source}:{curie or iri.rstrip('/').rsplit('/', 1)[-1]}"
        term = OntologyTerm(uri=iri, curie=curie, source=source, label=str(label),
                            match_type="relatedMatch", confidence=0.0,
                            origin="ols4", matched_value=value, matched_field="")
        verdict = _classify(value, {**doc, "label": label}, heads)
        if verdict is None:
            below.append(term)
            continue
        match_type, score, by_similarity = verdict
        out.append((replace(term, confidence=score, match_type=match_type), by_similarity))
    # ENVO names several classes "forest" (an area, an ecosystem): when the text
    # is the exact name of more than one term, the name cannot say which is meant.
    if len({t.curie for t, _ in out if t.match_type == "exactMatch"}) > 1:
        out = [(replace(t, match_type="closeMatch", confidence=_CONFIDENCE["ambiguous"]), s)
               if t.match_type == "exactMatch" else (t, s) for t, s in out]
    # With one exact hit left, its relatives in the result set get broad/narrow.
    out = _apply_hierarchy(out, below, ontology, session)
    # Confidence is calibrated precision, so it ranks; type only breaks ties.
    out.sort(key=lambda t: (t.confidence, _TYPE_RANK[t.match_type]), reverse=True)
    return out[:_KEEP_HITS]


def ols4_search(value: str, ontology: str, session: requests.Session, branch: str = "") -> list[OntologyTerm]:
    """Search OLS4 for ``value`` restricted to ``ontology``, scored by label similarity.

    OLS4 effectively ANDs the query tokens against term labels, so a descriptive
    phrase such as "coastal lagoon" returns nothing even though ENVO holds
    "lagoon". When the full phrase finds no acceptable hit we retry with
    progressively shorter tails, which walks a modifier-plus-head-noun phrase
    down to its head noun. Those broader hits are still classified against the
    original text, so they land as broadMatch rather than masquerading as exact.
    With a ``branch``, the search starts in that subtree and widens to the
    whole ontology unless the subtree gives an exact or close match.
    """
    try:
        return _ols4_search_or_raise(value, ontology, session, branch)
    except LookupFailed as exc:
        logger.warning("%s", exc)
        return []
    except Exception:
        logger.exception("ols4_search failed for value=%r ontology=%r", value, ontology)
        return []


def _ols4_search_or_raise(value: str, ontology: str, session: requests.Session,
                          branch: str = "", also: str = "") -> list[OntologyTerm]:
    """:func:`ols4_search` that raises :class:`LookupFailed` instead of returning [].

    The search runs on :func:`core_text`, the words an ontology can hold, but
    every hit keeps the submitter's text as ``matched_value``. ``also`` is a
    second ontology tried when the first gives no exact or close match.
    """
    core = core_text(value)
    if len(core) < 3:
        return []
    hits = _search_with_branch(core, ontology, session, branch)
    if also and not _has_good_hit(hits):
        hits = _best_of(hits, _ols4_search_unrestricted(core, also, session))
    return [replace(h, matched_value=value) for h in hits]


def _has_good_hit(hits: list[OntologyTerm]) -> bool:
    return any(_TYPE_RANK[h.match_type] >= _TYPE_RANK["closeMatch"] for h in hits)


def _best_of(first: list[OntologyTerm], second: list[OntologyTerm]) -> list[OntologyTerm]:
    """Both result sets ranked together by confidence, ``first`` winning ties."""
    seen = {h.curie for h in first}
    merged = first + [h for h in second if h.curie not in seen]
    merged.sort(key=lambda t: (t.confidence, _TYPE_RANK[t.match_type], t.curie in seen), reverse=True)
    return merged[:_KEEP_HITS]


def _search_with_branch(value: str, ontology: str, session: requests.Session, branch: str) -> list[OntologyTerm]:
    """Search within ``branch``, widening to the whole ontology when it gives nothing good.

    A branch nearly always yields *something* ("park" among biomes finds
    "business park"), and submitters often put non-biomes in env_broad_scale.
    So unless the branch gives an exact or close match, the whole ontology is
    searched too and the best-ranked hits of both are kept, the branch's first.
    """
    if not branch:
        return _ols4_search_unrestricted(value, ontology, session)
    hits = _ols4_search_unrestricted(value, ontology, session, branch)
    if _has_good_hit(hits):
        return hits
    return _best_of(hits, _ols4_search_unrestricted(value, ontology, session))


def _ols4_search_unrestricted(value: str, ontology: str, session: requests.Session,
                              branch: str = "") -> list[OntologyTerm]:
    """Full phrase, then in the singular, then shorter tails, within ``branch`` if given.

    OLS4 does not reduce plurals: "Coastal lagoons" and even "lagoons" find
    nothing in GOLD although it holds "Lagoon". Natura 2000 names are plural
    almost throughout, so the singular phrase is tried before shortening it;
    hits are still classified against the original text, as plural variants.
    """
    hits = _ols4_query(value, ontology, value, session, branch)
    if hits:
        return hits
    singular = " ".join(_singular(w) for w in value.split())
    if singular != value:
        hits = _ols4_query(singular, ontology, value, session, branch)
        if hits:
            return hits
        value_for_tails = singular
    else:
        value_for_tails = value

    tokens = value_for_tails.strip().split()
    for start in range(1, min(len(tokens), 4)):
        tail = " ".join(tokens[start:])
        if len(tail) < 3:
            break
        hits = _ols4_query(tail, ontology, value, session, branch)
        if hits:
            logger.debug("ols4 fallback %r -> %r matched %s", value, tail, hits[0].curie)
            return hits
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


def _cached_lookup(kind: str, value: str, ontology: str, session: requests.Session, use_cache: bool,
                   branch: str = "", also: str = "") -> list[OntologyTerm]:
    origin = "ols4" if kind == "ols4" else "wikidata"
    key = _cache_key(value, ontology, origin, f"{branch}+{also}" if also else branch)
    if use_cache:
        cached = _cache_read(key)
        if cached is not None:
            try:
                return [OntologyTerm(**d) for d in cached]
            except Exception:
                pass
    if kind == "ols4":
        try:
            terms = _ols4_search_or_raise(value, ontology, session, branch, also)
        except LookupFailed as exc:
            logger.warning("%s; not caching", exc)
            return []
        except Exception:
            logger.exception("ols4 lookup failed for value=%r ontology=%r", value, ontology)
            return []
    else:
        terms = wikidata_taxon(value, session)
    if use_cache:
        _cache_write(key, [t.to_dict() for t in terms])
    return terms


def search_text(value: str, ontology: str, session: requests.Session, branch: str = "",
                use_cache: bool = True) -> list[OntologyTerm]:
    """The best OLS4 hits for ``value`` as written, cached on disk; never raises.

    Unlike the sample enrichment this skips :func:`core_text`, so a taxon name
    such as "air metagenome" is searched whole instead of losing "metagenome".
    ``branch`` is a key of :data:`ENVO_BRANCHES`.
    """
    key = _cache_key(value, ontology, "ols4-text", branch)
    if use_cache:
        cached = _cache_read(key)
        if cached is not None:
            try:
                return [OntologyTerm(**d) for d in cached]
            except Exception:
                pass
    try:
        terms = _search_with_branch(value, ontology, session, branch)
    except LookupFailed as exc:
        logger.warning("%s; not caching", exc)
        return []
    except Exception:
        logger.exception("search_text failed for value=%r ontology=%r", value, ontology)
        return []
    terms = [replace(t, matched_value=value) for t in terms]
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

    ols4_tasks: dict[tuple[str, str, str, str], list[tuple[int, str]]] = {}
    wikidata_tasks: dict[str, list[tuple[int, str]]] = {}

    for i, sample in enumerate(samples):
        covered_sources = {t.source for t in sample.ontology_terms if t.origin == "biosamples"}
        for field, raw_value in _iter_candidate_fields(sample):
            value = (raw_value or "").strip()
            fkey = field.strip().lower()
            ontology = FIELD_ONTOLOGY_MAP.get(fkey)
            if not ontology or ontology not in config.OLS4_ONTOLOGIES:
                continue
            if not should_look_up(fkey, value, ontology):
                continue
            source_key = ONTOLOGY_ID_TO_SOURCE.get(ontology)
            if source_key and source_key in covered_sources:
                continue
            branch = FIELD_BRANCH_MAP.get(fkey, "") if ontology == "envo" else ""
            also = FIELD_ALSO_MAP.get(fkey, "") if ontology == "envo" else ""
            if also not in config.OLS4_ONTOLOGIES:
                also = ""
            ols4_tasks.setdefault((value, ontology, branch, also), []).append((i, field))
            if config.ENRICH_WIKIDATA and fkey in SCIENTIFIC_NAME_FIELDS:
                wikidata_tasks.setdefault(value, []).append((i, field))

    total = len(ols4_tasks) + len(wikidata_tasks)
    if total == 0:
        return
    done = 0
    ols4_results: dict[tuple[str, str, str, str], list[OntologyTerm]] = {}
    wikidata_results: dict[str, list[OntologyTerm]] = {}

    with ThreadPoolExecutor(max_workers=max(1, config.FETCH_WORKERS)) as pool:
        futures = {}
        for task in ols4_tasks:
            value, ontology, branch, also = task
            fut = pool.submit(_cached_lookup, "ols4", value, ontology, session, use_cache, branch, also)
            futures[fut] = ("ols4", task)
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

    for task, occurrences in ols4_tasks.items():
        for term in ols4_results.get(task, []):
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
