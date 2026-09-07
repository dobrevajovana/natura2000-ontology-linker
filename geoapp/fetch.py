"""Resolve INSDC accessions to normalised :class:`~geoapp.schema.Sample` records.

Pipeline for one accession:

1. :func:`resolve_accession` turns a secondary (SRS/ERS/DRS) accession into a
   primary BioSample accession (SAMN/SAMEA/SAMD) via NCBI E-utilities.
2. :func:`fetch_biosample` retrieves the record from the EBI BioSamples API.
3. :func:`parse_sample` flattens the raw JSON into a :class:`Sample`.

:func:`fetch_all` runs this pipeline concurrently over a list of accessions
and always returns one ``Sample`` per input, using ``fetch_error`` to report
failures instead of dropping them.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

import requests

from . import config
from .schema import ArchiveRef, Location, OntologyTerm, Sample, curie_from_uri

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_CACHE_MISS = object()  # sentinel: distinguishes "no cache entry" from "cached None"


# --- Rate limiting -----------------------------------------------------------


class RateLimiter:
    """Thread-safe minimum-interval limiter (``rate`` requests per second)."""

    def __init__(self, rate: float) -> None:
        self._min_interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next_time = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_time - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_time = max(now, self._next_time) + self._min_interval


_NCBI_LIMITER = RateLimiter(config.NCBI_RATE)
_EBI_LIMITER = RateLimiter(config.EBI_RATE)


# --- HTTP plumbing -----------------------------------------------------------


def _headers(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers = {"User-Agent": config.USER_AGENT}
    if extra:
        headers.update(extra)
    return headers


def _eutils_params(params: dict[str, str]) -> dict[str, str]:
    out = dict(params)
    if config.NCBI_API_KEY:
        out["api_key"] = config.NCBI_API_KEY
    if config.NCBI_EMAIL:
        out["email"] = config.NCBI_EMAIL
    return out


def _retry_delay(retry_after: Optional[str], attempt: int) -> float:
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return float(2 ** attempt)


def _request(
    session: requests.Session,
    url: str,
    *,
    limiter: RateLimiter,
    params: Optional[dict[str, str]] = None,
    headers: Optional[dict[str, str]] = None,
) -> requests.Response:
    """GET ``url`` with rate limiting and exponential-backoff retries.

    Retries on timeout, connection error, and HTTP 429/500/502/503/504
    (honouring ``Retry-After`` when present). Any other non-2xx status raises
    via ``raise_for_status``.
    """
    attempts = max(1, config.HTTP_RETRIES + 1)
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        limiter.acquire()
        try:
            resp = session.get(url, params=params, headers=headers, timeout=config.HTTP_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                delay = _retry_delay(None, attempt)
                logger.warning("request error for %s (attempt %d/%d): %s", url, attempt + 1, attempts, exc)
                time.sleep(delay)
            continue
        if resp.status_code == 200 or resp.status_code == 404:
            return resp
        if resp.status_code in _RETRY_STATUSES and attempt < attempts - 1:
            delay = _retry_delay(resp.headers.get("Retry-After"), attempt)
            logger.warning(
                "HTTP %d for %s (attempt %d/%d), retrying in %.1fs",
                resp.status_code, url, attempt + 1, attempts, delay,
            )
            last_exc = requests.HTTPError(f"HTTP {resp.status_code} for {url}")
            time.sleep(delay)
            continue
        resp.raise_for_status()
        return resp
    if last_exc:
        raise last_exc
    raise RuntimeError(f"request to {url} failed with no response")  # pragma: no cover


# --- On-disk cache -----------------------------------------------------------


def _cache_path(acc: str) -> Path:
    return config.CACHE_DIR / f"{acc}.json"


def _cache_fresh(ts: float) -> bool:
    return (time.time() - ts) <= config.CACHE_TTL_DAYS * 86400


def _read_biosample_cache(acc: str):
    path = _cache_path(acc)
    if not path.exists():
        return _CACHE_MISS
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _CACHE_MISS
    if not _cache_fresh(payload.get("ts", 0)):
        return _CACHE_MISS
    return payload.get("data")


def _write_biosample_cache(acc: str, data: Optional[dict]) -> None:
    config.ensure_dirs()
    try:
        _cache_path(acc).write_text(json.dumps({"ts": time.time(), "data": data}), encoding="utf-8")
    except OSError:
        logger.exception("failed to write cache for %s", acc)


_RESOLUTION_LOCK = threading.Lock()


def _resolution_map_path() -> Path:
    return config.CACHE_DIR / "resolution_map.json"


def _load_resolution_map() -> dict:
    path = _resolution_map_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _resolution_cache_get(acc: str):
    with _RESOLUTION_LOCK:
        entry = _load_resolution_map().get(acc)
    if entry is None or not _cache_fresh(entry.get("ts", 0)):
        return _CACHE_MISS
    return entry.get("target") or None


def _resolution_cache_set(acc: str, target: Optional[str]) -> None:
    config.ensure_dirs()
    with _RESOLUTION_LOCK:
        data = _load_resolution_map()
        data[acc] = {"target": target or "", "ts": time.time()}
        try:
            _resolution_map_path().write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            logger.exception("failed to write resolution map")


# --- Public API ---------------------------------------------------------------


def load_accessions(path: Path) -> list[str]:
    """Read one accession per line, skipping blanks and ``#`` comments.

    A ``#`` also ends a line, so an accession may carry a trailing note.
    Accessions are upper-cased and de-duplicated while preserving first-seen
    order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        acc = line.split()[0].upper()
        if acc not in seen:
            seen.add(acc)
            out.append(acc)
    return out


def _resolve_via_eutils(acc: str, session: requests.Session) -> Optional[str]:
    try:
        search_resp = _request(
            session,
            f"{config.EUTILS_BASE}/esearch.fcgi",
            limiter=_NCBI_LIMITER,
            params=_eutils_params({"db": "biosample", "term": acc, "retmode": "json"}),
            headers=_headers(),
        )
        idlist = search_resp.json().get("esearchresult", {}).get("idlist", [])
    except (requests.RequestException, ValueError):
        logger.exception("esearch failed for %s", acc)
        return None
    if not idlist:
        return None
    uid = idlist[0]
    try:
        summary_resp = _request(
            session,
            f"{config.EUTILS_BASE}/esummary.fcgi",
            limiter=_NCBI_LIMITER,
            params=_eutils_params({"db": "biosample", "id": uid, "retmode": "json"}),
            headers=_headers(),
        )
        result = summary_resp.json().get("result", {})
    except (requests.RequestException, ValueError):
        logger.exception("esummary failed for %s", acc)
        return None
    return result.get(uid, {}).get("accession") or None


def resolve_accession(acc: str, session: requests.Session, use_cache: bool = True) -> Optional[str]:
    """Resolve any INSDC accession to a primary BioSample accession.

    Accessions already starting with ``SAM`` are returned unchanged. Secondary
    accessions (SRS*/ERS*/DRS*) are resolved via NCBI E-utilities. Returns
    ``None`` when no BioSample accession can be found.
    """
    acc = acc.upper()
    if acc.startswith("SAM"):
        return acc
    if use_cache:
        cached = _resolution_cache_get(acc)
        if cached is not _CACHE_MISS:
            return cached
    resolved = _resolve_via_eutils(acc, session)
    if use_cache:
        _resolution_cache_set(acc, resolved)
    return resolved


def fetch_biosample(acc: str, session: requests.Session, use_cache: bool = True) -> Optional[dict]:
    """Fetch the raw EBI BioSamples JSON record for ``acc``.

    Returns ``None`` on HTTP 404. Other non-2xx responses raise (after
    retries) so the caller can record the failure.
    """
    if use_cache:
        cached = _read_biosample_cache(acc)
        if cached is not _CACHE_MISS:
            return cached
    resp = _request(
        session,
        f"{config.BIOSAMPLES_API}/{acc}",
        limiter=_EBI_LIMITER,
        headers=_headers({"Accept": "application/json"}),
    )
    if resp.status_code == 404:
        if use_cache:
            _write_biosample_cache(acc, None)
        return None
    data = resp.json()
    if use_cache:
        _write_biosample_cache(acc, data)
    return data


def parse_sample(raw: dict, query_accession: str) -> Sample:
    """Flatten a raw EBI BioSamples JSON record into a :class:`Sample`.

    ``location`` is left as a default :class:`Location`; a later module fills
    it in from ``attributes``.
    """
    accession = raw.get("accession") or query_accession
    characteristics: dict[str, list[dict]] = raw.get("characteristics") or {}

    def first_text(*names: str) -> str:
        for name in names:
            entries = characteristics.get(name)
            if entries:
                text = entries[0].get("text")
                if text:
                    return text
        return ""

    name = raw.get("name", "")
    attributes: dict[str, str] = {}
    for attr_name, entries in characteristics.items():
        if entries:
            text = entries[0].get("text", "")
            if text:
                attributes[attr_name] = text

    tax_id_raw = raw.get("taxId")
    try:
        tax_id = int(tax_id_raw) if tax_id_raw is not None else None
    except (TypeError, ValueError):
        tax_id = None

    archives = [ArchiveRef(source="biosample", accession=accession, url=config.NCBI_BIOSAMPLE + accession)]
    for ref in raw.get("externalReferences") or []:
        url = ref.get("url", "")
        if "ena/browser" in url:
            ena_acc = url.rstrip("/").rsplit("/", 1)[-1]
            archives.append(ArchiveRef(source="ena", accession=ena_acc, url=url))
    sra_acc = raw.get("sraAccession") or first_text("SRA accession")
    if sra_acc:
        archives.append(ArchiveRef(source="sra", accession=sra_acc, url=config.NCBI_SRA + sra_acc))

    ontology_terms: list[OntologyTerm] = []
    seen_terms: set[tuple[str, str]] = set()
    for attr_name, entries in characteristics.items():
        for entry in entries:
            text = entry.get("text", "")
            for uri in entry.get("ontologyTerms") or []:
                if not uri:
                    continue
                key = (uri, attr_name)
                if key in seen_terms:
                    continue
                seen_terms.add(key)
                curie, source = curie_from_uri(uri)
                ontology_terms.append(
                    OntologyTerm(
                        uri=uri,
                        curie=curie,
                        source=source,
                        confidence=1.0,
                        match_type="exactMatch",
                        origin="biosamples",
                        matched_value=text,
                        matched_field=attr_name,
                    )
                )

    return Sample(
        accession=accession,
        query_accession=query_accession,
        name=name,
        title=first_text("title") or name,
        organism=first_text("organism", "scientific_name"),
        tax_id=tax_id,
        collection_date=first_text("collection date", "collection_date"),
        release_date=raw.get("release", ""),
        center_name=first_text("INSDC center name"),
        checklist=first_text("ENA-CHECKLIST"),
        location=Location(),
        ontology_terms=ontology_terms,
        archives=archives,
        attributes=attributes,
        source_url=f"{config.BIOSAMPLES_API}/{accession}",
        fetch_error="",
    )


def _fetch_one(acc: str, session: requests.Session, use_cache: bool) -> Sample:
    """Run resolve -> fetch -> parse for one accession, never raising."""
    try:
        primary = resolve_accession(acc, session, use_cache=use_cache)
        if primary is None:
            return Sample(accession=acc, query_accession=acc, fetch_error=f"could not resolve {acc} to a BioSample accession")
        raw = fetch_biosample(primary, session, use_cache=use_cache)
        if raw is None:
            return Sample(accession=primary, query_accession=acc, fetch_error=f"BioSample {primary} not found (404)")
        return parse_sample(raw, acc)
    except Exception as exc:  # noqa: BLE001 - a failed accession must not abort the batch
        logger.exception("failed to fetch %s", acc)
        return Sample(accession=acc, query_accession=acc, fetch_error=str(exc))


def fetch_all(
    accessions: list[str],
    workers: Optional[int] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    use_cache: bool = True,
) -> list[Sample]:
    """Concurrently resolve and fetch a list of accessions.

    Returns one ``Sample`` per input accession, in input order. A failed
    accession still yields a ``Sample`` with ``fetch_error`` populated.
    """
    config.ensure_dirs()
    workers = workers or config.FETCH_WORKERS
    total = len(accessions)
    results: list[Optional[Sample]] = [None] * total
    done = 0
    done_lock = threading.Lock()

    def run(idx: int, acc: str, session: requests.Session) -> tuple[int, Sample]:
        nonlocal done
        sample = _fetch_one(acc, session, use_cache)
        with done_lock:
            done += 1
            current = done
        if progress is not None:
            try:
                progress(current, total, acc)
            except Exception:
                logger.exception("progress callback raised")
        return idx, sample

    with requests.Session() as session:
        session.headers.update(_headers())
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, i, acc, session) for i, acc in enumerate(accessions)]
            for fut in as_completed(futures):
                idx, sample = fut.result()
                results[idx] = sample
    return [s for s in results if s is not None]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # NB: SRS000001 itself is not resolvable via any live NCBI/EBI service
    # (confirmed 2026-09-04: esearch, ENA browser and EBI BioSamples all
    # return "not found" for it) -- SRS000002 is used here instead to
    # exercise the same SRS->SAM resolution path against a record that
    # actually exists.
    test_accessions = ["SAMEA10021153", "SAMEA10018461", "SAMD00000670", "SRS000002"]

    def _report(done: int, total: int, acc: str) -> None:
        print(f"[{done}/{total}] fetched {acc}")

    samples = fetch_all(test_accessions, progress=_report)

    print()
    for s in samples:
        print(f"query={s.query_accession!r} -> accession={s.accession!r}")
        if s.fetch_error:
            print(f"  ERROR: {s.fetch_error}")
            continue
        print(f"  name={s.name!r} organism={s.organism!r} tax_id={s.tax_id}")
        print(f"  collection_date={s.collection_date!r} release_date={s.release_date!r}")
        print(f"  center_name={s.center_name!r} checklist={s.checklist!r}")
        print(f"  attributes: {len(s.attributes)} keys -> {sorted(s.attributes)[:6]}...")
        print(f"  archives: {[(a.source, a.accession) for a in s.archives]}")
        print(f"  ontology_terms: {len(s.ontology_terms)} -> {[t.curie for t in s.ontology_terms][:5]}")
        lat_lon = s.attributes.get("lat lon", "")
        if lat_lon:
            print(f"  lat lon (raw): {lat_lon!r}")
