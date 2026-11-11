"""Automatic lookups for the cloud build: nothing here names a term by hand.

Each function takes what the records say (a sample type, a library source, a
plan item, coordinates) and asks a service for the term, caching every answer
under ``<data>/cloud/raw`` so a rebuild is quick and works offline:

* ``ranked``            OLS4 search in OLS's own relevance order, optionally inside a branch
* ``assay_hits``        a run's library source -> OBI assays (inside OBI's "assay" branch)
* ``material_hits``     a sample type -> ENVO environmental materials
* ``gold_path_hit``     a sample type and setting -> GOLD ecosystem path
* ``mgnify_biome_hit``  that path -> MGnify biome, checked against MGnify's biome API
* ``cf_hits``           a metadata plan item -> CF Standard Names, by their words
* ``site``              coordinates -> the named place in Wikidata (landform or observatory)
* ``study``             a project accession -> title, centre, secondary accession (ENA)
* ``repositories``      which microbiome repositories hold the study
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

import requests

logger = logging.getLogger("cloud.auto")

OLS_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"
ENA_SEARCH = "https://www.ebi.ac.uk/ena/portal/api/search"
WIKIDATA = "https://query.wikidata.org/sparql"
MGNIFY_API = "https://www.ebi.ac.uk/metagenomics/api/v1"
MGNIFY_BIOME_URL = "https://www.ebi.ac.uk/metagenomics/biomes/"
MICROBIOMEDB = ("https://microbiomedb.org/mbio/service/record-types/dataset/searches/AllDatasets/reports/standard"
                "?reportConfig=%7B%22attributes%22%3A%5B%22display_name%22%2C%22summary%22%5D%7D")
ROOTS = {"assay": "http://purl.obolibrary.org/obo/OBI_0000070",
         "material": "http://purl.obolibrary.org/obo/ENVO_00010483"}
UA = {"User-Agent": "natura2000-ontology-linker/0.1 (AAU Bio Collaboration)"}
FIELDS = "iri,label,obo_id,short_form,ontology_prefix,description,synonym,exact_synonyms,related_synonyms,narrow_synonyms,broad_synonyms"


class Cache:
    """One JSON file of answers per kind of lookup."""

    def __init__(self, raw_dir: Path, name: str):
        self.path = Path(raw_dir) / f"auto_{name}.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False))
        return value


def _get(url, **kw):
    kw.setdefault("timeout", 60)
    kw.setdefault("headers", UA)
    return requests.get(url, **kw)


def ranked(query: str, ontology: str, raw_dir: Path, branch: str = "", rows: int = 10) -> list[dict] | None:
    """OLS4 hits in OLS's own order (better than string similarity for names such
    as assays and GOLD paths). None when OLS4 did not answer (not cached)."""
    cache = Cache(raw_dir, "ols")
    key = f"{ontology}|{branch}|{rows}|{query.lower()}"
    if cache.get(key) is not None:
        return cache.get(key)
    params = {"q": query, "ontology": ontology, "rows": rows, "fieldList": FIELDS}
    if branch:
        params["allChildrenOf"] = ROOTS[branch]
    try:
        r = _get(OLS_SEARCH, params=params)
        r.raise_for_status()
        docs = (r.json().get("response") or {}).get("docs") or []
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OLS4 search failed for %r in %s: %s", query, ontology, exc)
        return None
    return cache.put(key, docs)


def _curie(doc: dict) -> str:
    return doc.get("obo_id") or f"{(doc.get('ontology_prefix') or '').upper()}:{doc.get('short_form')}"


def _typed(value: str, doc: dict, rank: int) -> tuple[str, float]:
    """The match type the calibrated matcher gives; otherwise OLS's rank decides, weakly."""
    from geoapp import ontolink
    got = ontolink.classify_hit(value, doc)
    if got:
        return "skos:" + got[0], round(got[1], 2)
    return ("skos:closeMatch", 0.5) if rank == 0 else ("skos:relatedMatch", 0.3)


def _hit(value, doc, rank, source, method):
    match, conf = _typed(value, doc, rank)
    return {"uri": doc["iri"], "curie": _curie(doc), "label": doc.get("label"), "source": source,
            "match": match, "confidence": conf, "method": method, "query": value}


def assay_hits(library: str, raw_dir: Path, keep: int = 2) -> list[dict]:
    """The OBI assays for a library source ("metagenome", "metatranscriptome")."""
    docs = ranked(library, "obi", raw_dir, "assay") or []
    return [_hit(library, d, i, "OBI", "ols4 (OBI assays)") for i, d in enumerate(docs[:keep])]


def material_hits(text: str, raw_dir: Path, keep: int = 4) -> list[dict]:
    """ENVO environmental materials for a sample type: what the sample is made of.
    The calibrated matcher's hits inside the material branch come first (it finds
    the exact term), then OLS's own order fills up to ``keep``."""
    from geoapp import ontolink
    out = []
    session = requests.Session()
    session.headers.update(UA)
    for t in ontolink.search_text(text, "envo", session, "material"):
        out.append({"uri": t.uri, "curie": t.curie, "label": t.label, "source": "ENVO", "match": "skos:" + t.match_type,
                    "confidence": round(t.confidence, 2), "method": "ols4 (ENVO materials)", "query": text, "material": True})
    docs = [d for d in (ranked(text, "envo", raw_dir, "material") or []) if (d.get("obo_id") or "").startswith("ENVO:")]
    seen = {h["uri"] for h in out}
    out += [{**_hit(text, d, i, "ENVO", "ols4 (ENVO materials)"), "material": True} for i, d in enumerate(docs) if d["iri"] not in seen]
    return out[:keep]


def gold_path_hit(words: list[str], setting: str, raw_dir: Path) -> dict | None:
    """The GOLD ecosystem path for a sample: paths naming one of ``words`` (its type,
    then its broad-scale context), the one matching its setting (indoor, outdoor)
    first, the shortest after that."""
    for i, word in enumerate(w for w in words if w):
        docs = ranked(word, "gold", raw_dir, rows=30) or []
        paths = [d for d in docs if "/gold.path/" in d.get("iri", "") and " > " in (d.get("label") or "")]
        paths = [d for d in paths if any(word.lower() in part.lower() for part in d["label"].split(" > ")[1:])]
        if not paths:
            continue
        want = {"outdoor": "outdoor", "indoor": "indoor", "agricultural": "agricultur"}.get(setting)
        paths.sort(key=lambda d: (not (want and want in d["label"].lower()), d["label"].count(" > "), d["label"]))
        d = paths[0]
        # The sample's own word in the path: close; a word from its context (a cloud in the air): broader.
        match, conf = ("skos:closeMatch", 0.75) if i == 0 else ("skos:broadMatch", 0.6)
        return {"uri": d["iri"], "curie": "GOLD:" + d["iri"].rsplit("/", 1)[-1], "label": d["label"].split(" > ")[-1],
                "lineage": d["label"], "source": "GOLD", "match": match, "confidence": conf,
                "method": f"ols4 (GOLD paths for “{word}”, setting {setting})", "query": word}
    return None


def mgnify_biome_hit(gold: dict, raw_dir: Path) -> dict | None:
    """MGnify's biome on the same path as a GOLD hit, if MGnify has it."""
    lineage = "root:" + ":".join(gold["lineage"].split(" > "))
    cache = Cache(raw_dir, "mgnify_biomes")
    known = cache.get(lineage)
    if known is None:
        try:
            r = _get(f"{MGNIFY_API}/biomes/{requests.utils.quote(lineage)}", params={"format": "json"}, timeout=90)
            known = cache.put(lineage, r.status_code == 200) if r.status_code in (200, 404) else None
        except requests.RequestException as exc:
            logger.warning("MGnify biome check failed: %s", exc)
    if known is False:
        return None
    return {"uri": MGNIFY_BIOME_URL + lineage.replace(" ", "%20"), "curie": "MGnify:" + lineage, "label": lineage.split(":")[-1],
            "lineage": lineage, "source": "MGnify", "match": gold["match"], "confidence": gold["confidence"],
            "method": "the GOLD path, as an MGnify biome" + ("" if known else " (MGnify did not answer; not verified)"),
            "query": gold["query"], "verified": bool(known)}


_STOP = {"of", "the", "and", "a", "an", "in", "at", "above", "concentration"}
_SAME = {"height": {"height", "thickness", "altitude"}, "content": {"content", "concentration"}}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP]


def cf_hits(item: str, cf: dict[str, dict], context: set[str], keep: int = 2) -> list[dict]:
    """CF Standard Names whose words cover the item's, those naming the study's
    medium (air, atmosphere, cloud) first, then the shortest."""
    want = _words(item)
    if not want or not cf:
        return []
    scored = []
    for name, info in cf.items():
        parts = set(name.split("_"))
        got = sum(1 for w in want if parts & _SAME.get(w, {w}))
        if got < len(want) - (1 if len(want) > 2 else 0) or got == 0:
            continue
        # Words the name adds that are neither the item's nor the study's medium.
        extra = len({x for x in parts - context - _STOP if not any(x in _SAME.get(w, {w}) for w in want)})
        scored.append((got / len(want), -extra, len(parts & context), -len(name), name, info))
    scored.sort(reverse=True)
    out = []
    for share, neg_extra, ctx, _, name, info in scored[:keep]:
        exact = share == 1 and neg_extra == 0
        match, conf = (("skos:exactMatch", 0.9) if exact else ("skos:closeMatch", 0.75) if share == 1 and neg_extra > -2
                       else ("skos:relatedMatch", 0.5))
        out.append({"uri": f"http://vocab.nerc.ac.uk/standard_name/{name}/", "curie": f"CF:{name}", "label": name,
                    "source": "CF", "units": info.get("units"), "description": (info.get("description") or "")[:400],
                    "cf_version": info.get("version"), "match": match, "confidence": conf,
                    "method": "CF table, by the item's words", "query": item})
    return out


def site(lat: float, lon: float, raw_dir: Path) -> dict:
    """The named landform or observatory nearest the coordinates (within 1 km), from Wikidata."""
    fallback = {"label": f"Sampling site {lat:.3f}, {lon:.3f}", "wikidata": None, "type": None, "distance_km": None}
    cache = Cache(raw_dir, "site")
    key = f"{lat:.4f},{lon:.4f}"
    if cache.get(key):
        return cache.get(key)
    query = f'''SELECT ?place ?placeLabel ?dist (SAMPLE(?tl) AS ?type) WHERE {{
      SERVICE wikibase:around {{ ?place wdt:P625 ?loc . bd:serviceParam wikibase:center "Point({lon} {lat})"^^geo:wktLiteral .
        bd:serviceParam wikibase:radius "1" . bd:serviceParam wikibase:distance ?dist . }}
      ?place wdt:P31 ?t . ?t wdt:P279* ?root . VALUES ?root {{ wd:Q271669 wd:Q62832 wd:Q3914 }}
      ?t rdfs:label ?tl . FILTER(LANG(?tl) = "en")
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,fr,de,es,it". }}
    }} GROUP BY ?place ?placeLabel ?dist ORDER BY ?dist LIMIT 1'''
    try:
        r = _get(WIKIDATA, params={"query": query}, headers={**UA, "Accept": "application/sparql-results+json"}, timeout=60)
        r.raise_for_status()
        b = (r.json().get("results") or {}).get("bindings") or []
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Wikidata place lookup failed: %s", exc)
        return fallback
    if not b:
        return cache.put(key, fallback)
    return cache.put(key, {"label": b[0]["placeLabel"]["value"], "wikidata": b[0]["place"]["value"],
                           "type": b[0]["type"]["value"], "distance_km": round(float(b[0]["dist"]["value"]), 2)})


def study(project: str, raw_dir: Path) -> dict:
    """Title, centre and secondary accession of an ENA study."""
    cache = Cache(raw_dir, "study")
    if cache.get(project):
        return cache.get(project)
    try:
        r = _get(ENA_SEARCH, params={"result": "study", "query": f'study_accession="{project}"', "format": "json",
                                     "fields": "study_accession,secondary_study_accession,study_title,center_name"})
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("ENA study lookup failed: %s", exc)
        return {"study_accession": project}
    return cache.put(project, rows[0] if rows else {"study_accession": project})


def _remember(raw_dir: Path, name: str, answer: str | None) -> tuple[str, str]:
    """Keep the last definite answer per repository; use it, dated, when a check fails."""
    cache = Cache(raw_dir, "repositories")
    if answer:
        cache.put(name, {"status": answer, "date": date.today().isoformat()})
        return answer, date.today().isoformat()
    last = cache.get(name)
    if last:
        return f"{last['status']} (as of {last['date']}; no answer this time)", last["date"]
    return "unknown (no answer)", "-"


def check_mgnify(secondary: str) -> str | None:
    try:
        r = _get(f"{MGNIFY_API}/studies/{secondary}", params={"format": "json"}, timeout=90)
        if r.status_code == 404:
            return "not analysed yet (no MGnify study for it)"
        r.raise_for_status()
        return f"analysed as {r.json()['data']['id']}"
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("MGnify check failed: %s", exc)
        return None


def check_microbiomedb(project: str, title: str) -> str | None:
    """MicrobiomeDB's curated datasets: does any name or summary mention the project or its medium?"""
    try:
        r = _get(MICROBIOMEDB)
        r.raise_for_status()
        records = r.json().get("records") or []
    except (requests.RequestException, ValueError) as exc:
        logger.warning("MicrobiomeDB check failed: %s", exc)
        return None
    words = {project.lower(), *[w for w in re.findall(r"[a-z]+", (title or "").lower()) if len(w) > 4]}
    hits = [rec["attributes"].get("display_name") for rec in records
            if any(w in json.dumps(rec["attributes"]).lower() for w in words if w in (project.lower(), "cloud", "aerosol"))]
    return f"possibly in: {'; '.join(hits[:3])}" if hits else f"not included (none of its {len(records)} datasets mentions it)"


def repositories(project: str, secondary: str | None, title: str, runs: int, first_run: str | None, raw_dir: Path) -> list[dict]:
    """Which microbiome repositories hold the study, checked by function where the site allows it."""
    out = [{"name": "ENA", "url": f"https://www.ebi.ac.uk/ena/browser/view/{project}", "status": f"holds the {runs} runs",
            "checked": "every build"}]
    status, when = _remember(raw_dir, "MGnify", check_mgnify(secondary) if secondary else None)
    out.append({"name": "MGnify", "url": "https://www.ebi.ac.uk/metagenomics/", "status": status, "checked": when})
    status, when = _remember(raw_dir, "MicrobiomeDB", check_microbiomedb(project, title))
    out.append({"name": "MicrobiomeDB", "url": "https://microbiomedb.org/", "status": status, "checked": when})
    out.append({"name": "MicrobeAtlas", "url": f"https://microbeatlas.org/sample_detail?sid={first_run or ''}",
                "status": "cannot be checked by a program: the site requires a browser challenge (Cloudflare). "
                          "Open the link to look; on 2026-11-10 the first run was not found there.", "checked": "in a browser"})
    out.append({"name": "Qiita", "url": "https://qiita.ucsd.edu/",
                "status": "cannot be checked by a program without a Qiita account (its study API needs a login)", "checked": "-"})
    out.append({"name": "GOLD", "url": "https://gold.jgi.doe.gov/",
                "status": "study lookup needs a JGI GOLD API token; the samples' GOLD ecosystem path is linked automatically",
                "checked": "-"})
    return out
