"""Every "air metagenome" sample in the archives, mapped to ENVO, for comparison with the cloud study.

The list came from a colleague as air_sample.tsv: 35,673 BioSamples, all with
NCBI taxon 655179 (air metagenome). It is exactly what ENA returns for the
query ``tax_eq(655179)``, so it is fetched from there, with the fields the
list lacked: where (lat/lon, country, altitude), when, and the MIxS
environment triad.

Steps:

1. Fetch the samples from ENA in one request (cached as TSV). Given a list
   (``--air-list air_sample.tsv``), keep only those and report how the list and
   ENA differ.
2. Read the environment triad (broad-scale context, local context, medium;
   the older biome / feature / material names are used when the new ones are
   empty). A value that already holds an ENVO ID ("atmosphere [ENVO:01000267]",
   "ENVO:00000428") is taken as given; its label comes from OLS4. When the
   words beside the ID are not its label ("atmosphere [ENVO:01000004]", a
   Mediterranean sea biome) the value is listed as a conflict, not mapped.
3. Free text is searched in OLS4 with geoapp's calibrated matcher, once per
   distinct text used by at least MIN_SAMPLES samples; the best hit is kept.
4. Each sample gets a rough setting (indoor, agricultural, outdoor, not
   stated) from words in its text, and is placed on the map by its own
   coordinates. Samples high up (>= 1000 m) or naming clouds, fog or mountains
   are listed as the ones most like the cloud study.
5. The mappings of common texts become predicted terms in the cloud graph, so
   experts can review them like the rest; terms shared with the cloud study
   meet its nodes there.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

import requests

logger = logging.getLogger("cloud.air")

TAX_ID = "655179"
ENA_SEARCH = "https://www.ebi.ac.uk/ena/portal/api/search"
OLS_TERM = "https://www.ebi.ac.uk/ols4/api/ontologies/envo/terms"
ENVO_URI = "http://purl.obolibrary.org/obo/ENVO_"
FIELDS = ["sample_accession", "description", "lat", "lon", "country", "collection_date", "altitude", "elevation",
          "environment_biome", "environment_feature", "environment_material", "broad_scale_environmental_context",
          "local_environmental_context", "environmental_medium", "isolation_source", "project_name", "center_name"]
# (key, label, current MIxS field, older field, ENVO branch for the search)
TRIAD = [("broad", "Broad-scale environmental context", "broad_scale_environmental_context", "environment_biome", "biome"),
         ("local", "Local environmental context", "local_environmental_context", "environment_feature", ""),
         ("medium", "Environmental medium", "environmental_medium", "environment_material", "material")]
MIN_SAMPLES = 3        # texts used by fewer samples are counted but not searched
REVIEW_MIN = 20        # texts used by at least this many samples become reviewable predicted terms
HIGH_ALTITUDE = 1000   # m; the cloud study's station is at 1465 m
PLACEHOLDERS = {"missing", "not collected", "not applicable", "na", "n/a", "unknown", "not provided", "none", "-",
                "restricted access", "missing: control sample", "not available"}
ID = re.compile(r"ENVO[_:]\s*(\d{7,8})", re.I)

SETTINGS = [  # first match wins
    ("indoor", r"indoor|\broom|house|home|office|hospital|built environment|building|subway|metro|school|classroom|"
               r"museum|aircraft|airplane|cabin|clean ?room|ventilation|hvac|dust|residen|apartment|library|daycare"),
    ("agricultural", r"farm|barn|\bpig|swine|poultry|chicken|livestock|cattle|dairy|greenhouse|agricultur|stable|piggery"),
    ("outdoor", r"outdoor|ambient|urban|city|atmospher|rooftop|roof|tower|forest|field|mountain|coast|marine|ocean|\bsea\b|"
                r"desert|alpine|cloud|fog|rain|snow|pm ?2\.?5|pm ?10|park|street|road|station|island|volcan|glacier|tropic|"
                r"biome|terrestrial|aerosol"),
]
LIKE_CLOUD = re.compile(r"cloud|fog|mountain|alpine|summit|high[- ]altitude|troposph|stratosph|volcan|peak", re.I)


def fetch(raw_dir: Path, refresh: bool = False) -> list[dict]:
    path = raw_dir / "air_metagenome_samples.tsv"
    if refresh or not path.exists():
        logger.info("Fetching all air metagenome samples (tax %s) from ENA", TAX_ID)
        resp = requests.get(ENA_SEARCH, params={"result": "sample", "query": f"tax_eq({TAX_ID})", "fields": ",".join(FIELDS),
                                                "format": "tsv", "limit": 0}, timeout=600)
        resp.raise_for_status()
        raw_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")), delimiter="\t"))


def read_list(path) -> set[str] | None:
    """Sample accessions from the first column of a TSV (such as air_sample.tsv), or None."""
    if not path:
        return None
    path = Path(path).expanduser()
    if not path.exists():
        logger.warning("Air sample list %s not found; using every ENA sample", path)
        return None
    with path.open(encoding="utf-8") as fh:
        rows = csv.reader(fh, delimiter="\t")
        return {r[0].strip() for r in rows if r and re.match(r"SAM[NED]A?\d+", r[0].strip())}


def clean(v: str | None) -> str:
    v = " ".join((v or "").split())
    return "" if v.lower() in PLACEHOLDERS else v


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def id_conflict(text: str, term: dict | None) -> str | None:
    """The words beside an ENVO ID when they are neither its label nor a synonym:
    "atmosphere [ENVO:01000004]" where ENVO:01000004 is a Mediterranean sea biome."""
    words = ID.sub("", text).replace("[", " ").replace("]", " ").replace("(", " ").replace(")", " ").strip(" :;,-_")
    norm = lambda x: " ".join(re.sub(r"[^a-z0-9 ]", " ", (x or "").lower()).split())
    if not words or not term or len(norm(words)) < 3:
        return None
    w = norm(words)
    for name in [term.get("label"), *term.get("synonyms", [])]:
        n = norm(name)
        if n and (w == n or w in n or n in w):
            return None
    return words


def setting(text: str) -> str:
    t = text.lower()
    for name, pattern in SETTINGS:
        if re.search(pattern, t):
            return name
    return "not stated"


def envo_labels(ids: set[str], raw_dir: Path) -> dict[str, dict]:
    """Label and synonyms of ENVO IDs written in the records, from OLS4 (cached)."""
    path = raw_dir / "envo_terms.json"
    try:
        cache = json.loads(path.read_text())
    except (OSError, ValueError):
        cache = {}
    session = requests.Session()
    for i in sorted(ids - set(cache)):
        try:
            r = session.get(OLS_TERM, params={"obo_id": f"ENVO:{i}"}, timeout=30)
            terms = ((r.json().get("_embedded") or {}).get("terms") or []) if r.ok else []
            cache[i] = {"label": terms[0]["label"], "synonyms": terms[0].get("synonyms") or []} if terms else None
        except (requests.RequestException, ValueError):
            continue   # tried again next build
    path.write_text(json.dumps(cache))
    return {i: v for i, v in cache.items() if v}


def search_texts(texts: list[tuple[str, str]], use_cache: bool = True) -> dict[tuple, dict]:
    """Best OLS4 hit per (field key, text), with geoapp's matcher."""
    from geoapp import config as geo_config
    from geoapp import ontolink
    branch = {k: b for k, *_, b in TRIAD}
    session = requests.Session()
    session.headers.update({"User-Agent": geo_config.USER_AGENT, "Accept": "application/json"})
    out = {}
    for n, (key, text) in enumerate(texts, 1):
        core = ontolink.core_text(text) or text
        hits = ontolink.search_text(core, "envo", session, branch[key], use_cache)
        if hits:
            h = max(hits, key=lambda t: t.confidence)
            out[(key, text)] = {"uri": h.uri, "curie": h.curie, "label": h.label, "source": "ENVO",
                                "match": "skos:" + h.match_type, "confidence": round(h.confidence, 2),
                                "method": "ols4", "query": core}
        if n % 50 == 0:
            logger.info("Searched %d of %d air sample texts", n, len(texts))
    return out


def build_air(raw_dir: Path, refresh: bool = False, use_cache: bool = True, listed: set[str] | None = None,
              study_samples: set[str] | None = None) -> tuple[dict, list[dict], dict]:
    """The air.json summary, plus lookups and hits for the cloud graph. With
    ``listed`` (a colleague's list of accessions) only those samples are mapped,
    and the list is compared with what ENA returns."""
    rows = fetch(raw_dir, refresh)
    in_ena = {r["sample_accession"] for r in rows}
    compare = None
    if listed is not None:
        compare = {"listed": len(listed), "in_ena": len(listed & in_ena), "not_in_ena": sorted(listed - in_ena)[:50],
                   "ena_not_listed": len(in_ena - listed)}
        rows = [r for r in rows if r["sample_accession"] in listed]
    texts: dict[str, Counter] = {k: Counter() for k, *_ in TRIAD}
    per_sample = []
    for r in rows:
        env = {k: clean(r.get(new)) or clean(r.get(old)) for k, _, new, old, _ in TRIAD}
        for k, v in env.items():
            if v:
                texts[k][v] += 1
        per_sample.append((r, env))

    ids = {m.group(1) for c in texts.values() for v in c for m in ID.finditer(v)}
    labels = envo_labels(ids, raw_dir)
    to_search = sorted({(k, v) for k, c in texts.items() for v, n in c.items() if n >= MIN_SAMPLES and not ID.search(v)})
    logger.info("Air samples: %d; %d distinct texts carry an ENVO ID, %d to search in OLS4", len(rows), len(ids), len(to_search))
    found = search_texts(to_search, use_cache)

    conflicts: dict[str, dict] = {}

    def id_term(key, part):
        i = ID.search(part).group(1)
        words = id_conflict(part, labels.get(i))
        label = (labels.get(i) or {}).get("label")
        if words:
            # The words and the ID disagree: count it, and trust neither.
            c = conflicts.setdefault(part, {"text": part, "words": words, "curie": f"ENVO:{i}", "label": label,
                                            "field": key, "samples": 0})
            c["samples"] += 1
            return None
        return {"uri": ENVO_URI + i, "curie": f"ENVO:{i}", "label": label or f"ENVO:{i}", "source": "ENVO",
                "match": "skos:exactMatch", "confidence": 1.0, "method": "envo_id_in_record", "query": part}

    def terms_for(key, v):
        """A value may list several terms ("biome [ENVO:00000428]|air [ENVO:00002005]"); each ID part counts."""
        if not ID.search(v):
            t = found.get((key, v))
            return [t] if t else []
        parts = [x for x in re.split(r"\s*[|;]\s*", v) if ID.search(x)]
        return [t for t in (id_term(key, x) for x in parts) if t]

    # Terms per field, sites, settings, the samples most like the cloud study.
    term_counts: dict[str, dict] = {k: {} for k, *_ in TRIAD}
    sites: dict[tuple, dict] = {}
    settings_count, similar = Counter(), []
    mapped_samples = 0
    for r, env in per_sample:
        any_term = False
        for k, v in env.items():
            if not v:
                continue
            for t in {t["uri"]: t for t in terms_for(k, v)}.values():
                any_term = True
                e = term_counts[k].setdefault(t["uri"], {**{x: t[x] for x in ("uri", "curie", "label")}, "samples": 0,
                                                        "texts": Counter(), "methods": Counter()})
                e["samples"] += 1
                e["texts"][v] += 1
                e["methods"][t["method"]] += 1
        mapped_samples += any_term
        text = " ".join([*env.values(), r.get("isolation_source") or "", r.get("description") or "", r.get("project_name") or ""])
        st = setting(text)
        settings_count[st] += 1
        lat, lon = num(r.get("lat")), num(r.get("lon"))
        alt = num(r.get("altitude")) or num(r.get("elevation"))
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            site = sites.setdefault((round(lat, 2), round(lon, 2)), {"lat": round(lat, 2), "lon": round(lon, 2), "n": 0,
                                                                     "settings": Counter(), "countries": Counter(),
                                                                     "projects": Counter(), "years": Counter()})
            site["n"] += 1
            site["settings"][st] += 1
            site["countries"][(r.get("country") or "").split(":")[0] or "?"] += 1
            site["projects"][r.get("project_name") or "?"] += 1
            if (r.get("collection_date") or "")[:4].isdigit():
                site["years"][r["collection_date"][:4]] += 1
        if (alt is not None and alt >= HIGH_ALTITUDE) or LIKE_CLOUD.search(text):
            similar.append({"accession": r["sample_accession"], "country": r.get("country"), "date": r.get("collection_date"),
                            "altitude": alt, "lat": lat, "lon": lon, "setting": st, "project": r.get("project_name"),
                            "env": env, "why": (f"altitude {alt:g} m" if alt is not None and alt >= HIGH_ALTITUDE else
                                                "names " + LIKE_CLOUD.search(text).group(0).lower())})

    def top(c, n=3):
        return [k for k, _ in c.most_common(n)]

    summary = {
        "meta": {"tax_id": TAX_ID, "query": f"tax_eq({TAX_ID})", "source": "ENA portal API", "samples": len(rows),
                 "with_coordinates": sum(s["n"] for s in sites.values()), "sites": len(sites),
                 "with_environment": sum(1 for _, env in per_sample if any(env.values())), "mapped": mapped_samples,
                 "texts": {k: len(c) for k, c in texts.items()}, "searched": len(to_search), "envo_ids": len(ids),
                 "settings": dict(settings_count), "min_samples": MIN_SAMPLES, "high_altitude": HIGH_ALTITUDE,
                 "list": compare, "study_samples_here": len((study_samples or set()) & in_ena)},
        "fields": {k: label for k, label, *_ in TRIAD},
        "terms": {k: sorted(({**{x: e[x] for x in ("uri", "curie", "label", "samples")},
                              "texts": [[t, n] for t, n in e["texts"].most_common(4)],
                              "methods": dict(e["methods"])} for e in term_counts[k].values()),
                            key=lambda e: -e["samples"])[:40] for k in term_counts},
        "sites": [{"lat": s["lat"], "lon": s["lon"], "n": s["n"], "setting": s["settings"].most_common(1)[0][0],
                   "settings": dict(s["settings"]), "country": top(s["countries"], 1)[0], "projects": top(s["projects"]),
                   "years": sorted(s["years"])} for s in sites.values()],
        "id_conflicts": sorted(conflicts.values(), key=lambda c: -c["samples"]),
        "similar": sorted(similar, key=lambda s: -(s["altitude"] or 0))[:300],
        "similar_total": len(similar),
    }

    # Reviewable: common free texts and their OLS4 hit (texts with an ENVO ID are the submitter's own choice).
    items, hits = [], {}
    for k, label, *_ in TRIAD:
        for v, n in texts[k].most_common():
            if n < REVIEW_MIN:
                break
            h = None if ID.search(v) else found.get((k, v))
            if not h:
                continue
            items.append({"field": f"air_{k}", "field_label": f"Air metagenomes: {label.lower()}", "text": v, "ontology": "envo",
                          "branch": "", "queries": [], "runs": [], "air": True, "samples": n})
            hits[(f"air_{k}", v.lower())] = [h]
    summary["meta"]["reviewable"] = len(items)
    summary["meta"]["id_conflict_samples"] = sum(c["samples"] for c in conflicts.values())
    return summary, items, hits
