"""Real BioSample text as a benchmark: ENA metagenomes tagged with microntology.

The microntology team tagged >300k ENA metagenomes (Zenodo 18164252, CC-BY-4.0).
Joining those tags to the text submitters actually typed into the MIxS fields
gives what the GOLD and microntology term lists cannot: real, messy values,
each with the field it came from. The tags are coarse ("soil", "intestine"), so
a hit counts as correct when it is a tagged term *or sits below one* in the
ontology -- "forest soil" under a "soil" tag is right, not wrong.

Samples are spread across studies (a study's samples usually share one text),
their MIxS fields are fetched from the ENA portal API in batches and cached,
and ontology IDs the submitter pasted into the text are stripped so the
matcher only sees words.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import requests

from geoapp import config
from geoapp.ontolink import FIELD_ONTOLOGY_MAP, should_look_up

logger = logging.getLogger(__name__)

ANNOTATIONS_URL = ("https://zenodo.org/api/records/18164252/files/"
                   "ena_drop.v_2_0.microntology.v_1_0.annotations.v_0_2.tsv/content")
ENA_SEARCH = "https://www.ebi.ac.uk/ena/portal/api/search"
OLS4_CLASS = "https://www.ebi.ac.uk/ols4/api/v2/ontologies/{ontology}/classes/{iri}"

# ENA portal field -> the attribute name geoapp routes on. ENA's legacy
# environment_biome / _feature / _material are copies of the three MIxS fields,
# so they are left out rather than counted twice.
ENA_FIELDS = {
    "broad_scale_environmental_context": "env_broad_scale",
    "local_environmental_context": "env_local_scale",
    "environmental_medium": "env_medium",
    "isolation_source": "isolation source",
    "host_body_site": "body site",
    "tissue_type": "tissue",
}

# "soil [ENVO:00001998]", "ENVO_00002003", "(UBERON:0001988)"
_CURIE = re.compile(r"[\[(]?\b[A-Za-z]+[:_]\d{4,}\b[\])]?")
_BATCH = 200


def _clean(value: str) -> str:
    return " ".join(_CURIE.sub(" ", value).replace("|", " ").split()).strip(" ,;:-")


def _download(url: str, path: Path) -> Path:
    if not path.exists():
        logger.info("downloading %s", url)
        resp = requests.get(url, timeout=600, headers={"User-Agent": config.USER_AGENT})
        resp.raise_for_status()
        path.write_bytes(resp.content)
    return path


def pick_samples(annotations: Path, per_study: int) -> dict[str, set[str]]:
    """Up to ``per_study`` BioSample accessions per study -> their microntology tags."""
    by_study: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    with annotations.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            acc = (r.get("ena_ers_sample_id") or "").strip()
            if not acc.startswith("SAM"):
                continue
            tags = {t.strip() for t in (r.get("microntology") or "").split(";") if t.strip()}
            by_study[r.get("study_name") or ""].append((acc, tags))
    picked = {}
    for samples in by_study.values():
        for acc, tags in sorted(samples)[:per_study]:
            picked[acc] = tags
    return picked


def fetch_ena_fields(accessions: list[str], cache: Path, session: requests.Session) -> dict[str, dict]:
    """MIxS text fields per accession from the ENA portal, cached as JSON lines."""
    have: dict[str, dict] = {}
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            have[row["accession"]] = row
    todo = [a for a in accessions if a not in have]
    fields = ",".join(["sample_accession", *ENA_FIELDS])
    with cache.open("a", encoding="utf-8") as out:
        for i in range(0, len(todo), _BATCH):
            batch = todo[i:i + _BATCH]
            try:
                resp = session.post(ENA_SEARCH, timeout=120, data={
                    "result": "sample", "includeAccessions": ",".join(batch),
                    "fields": fields, "format": "json", "limit": 0})
                resp.raise_for_status()
                found = {r.get("sample_accession"): r for r in resp.json()}
            except Exception as exc:          # a bad batch costs its samples, not the run
                logger.warning("ENA batch %d failed: %s", i // _BATCH, exc)
                continue
            for acc in batch:                 # record misses too, so they are not refetched
                r = found.get(acc, {})
                row = {"accession": acc, **{f: r.get(f, "") for f in ENA_FIELDS}}
                have[acc] = row
                out.write(json.dumps(row) + "\n")
            logger.info("ENA: %d/%d samples fetched", min(i + _BATCH, len(todo)), len(todo))
    return have


class Ancestors:
    """CURIE -> CURIEs of its OLS4 hierarchical ancestors (subClassOf and part_of), cached."""

    def __init__(self, cache: Path, session: requests.Session):
        self.cache, self.session = cache, session
        self.known: dict[str, list[str]] = json.loads(cache.read_text()) if cache.exists() else {}

    def __call__(self, curie: str) -> set[str]:
        if curie not in self.known:
            prefix, num = curie.split(":", 1)
            iri = f"http://purl.obolibrary.org/obo/{prefix}_{num}"
            url = OLS4_CLASS.format(ontology=prefix.lower(), iri=quote(quote(iri, safe=""), safe=""))
            try:
                resp = self.session.get(url, timeout=60)
                resp.raise_for_status()
                raw = resp.json().get("hierarchicalAncestor") or []
            except Exception as exc:
                logger.debug("ancestors of %s unavailable: %s", curie, exc)
                return set()                  # not cached: a later run may reach it
            iris = [a if isinstance(a, str) else a.get("iri", "") for a in (raw if isinstance(raw, list) else [raw])]
            self.known[curie] = sorted({i.rsplit("/", 1)[-1].replace("_", ":", 1) for i in iris if "/obo/" in i})
            self.cache.write_text(json.dumps(self.known))
        return set(self.known[curie])


def load_ena(out_dir: Path, terms_path: Path, per_study: int = 3) -> list:
    """Cases from real ENA text: one per distinct (field, text), gold = IDs of the samples' tags."""
    from .benchmark import Case, _rows

    term_ids: dict[str, set[str]] = {}
    for r in _rows(terms_path):
        term_ids[r["id"]] = {c.strip() for c in (r.get("ontology_link") or "").split(";") if ":" in c}

    picked = pick_samples(_download(ANNOTATIONS_URL, out_dir / "micront_annotations.tsv"), per_study)
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    ena = fetch_ena_fields(sorted(picked), out_dir / "ena_fields.jsonl", session)

    cases: dict[tuple[str, str], Case] = {}
    for acc, tags in picked.items():
        gold = set().union(*(term_ids.get(t, set()) for t in tags)) if tags else set()
        for ena_field, field in ENA_FIELDS.items():
            text = _clean(str((ena.get(acc) or {}).get(ena_field) or ""))
            ontology = FIELD_ONTOLOGY_MAP.get(field)
            if ontology not in config.OLS4_ONTOLOGIES or not should_look_up(field, text, ontology):
                continue
            case = cases.setdefault((field, text.lower()), Case("ena", text, ontology, set(), field=field,
                                                                 match_descendants=True))
            case.gold_ids |= gold
    logger.info("ENA: %d samples, %d distinct (field, text) cases", len(picked), len(cases))
    return list(cases.values())
