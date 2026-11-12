"""NMDC soil samples as a knowledge graph: ENVO from the record, GOLD from its ecosystem path.

Sources:
* NMDC runtime API, ``GET /biosamples?filter=ecosystem_type:Soil`` (cursor paging):
  https://api.microbiomedata.org/docs
* GOLD ecosystem classification, read from its URL:
  https://raw.githubusercontent.com/cmungall/gold-ontology/main/gold.obo

Each NMDC biosample already carries ENVO terms (env_broad_scale, env_local_scale,
env_medium, each ``{"term": {"id", "name"}}``) and a GOLD ecosystem path
(ecosystem > ecosystem_category > ecosystem_type > ecosystem_subtype >
specific_ecosystem). Nothing is guessed: ENVO comes as given, GOLD is the class
whose name is that path (or the deepest part of it GOLD has).

Output (``<data>/soil/nmdc/``): ``samples.json`` (the fields read) and ``graph.json``.

Run:  python -m soil.nmdc [--data-dir data] [--limit N] [--filter ecosystem_type:Soil]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from pathlib import Path

import requests

logger = logging.getLogger("soil.nmdc")

NMDC_API = "https://api.microbiomedata.org"
SOIL_FILTER = "ecosystem_type:Soil"
GOLD_OBO_URL = "https://raw.githubusercontent.com/cmungall/gold-ontology/main/gold.obo"
ENVO_IRI = "http://purl.obolibrary.org/obo/ENVO_"
GOLD_BIOSAMPLE_URL = "https://gold.jgi.doe.gov/biosample?id="

TRIAD = ["env_broad_scale", "env_local_scale", "env_medium"]
GOLD_PATH = ["ecosystem", "ecosystem_category", "ecosystem_type", "ecosystem_subtype", "specific_ecosystem"]
# Path steps that say nothing; trimmed from the end before looking GOLD up.
EMPTY_STEPS = {"", "unclassified", "unknown", "none"}


# ─── Download ────────────────────────────────────────────────────────────────

def download(filter_: str = SOIL_FILTER, limit: int | None = None, per_page: int = 1000,
             session: requests.Session | None = None) -> list[dict]:
    """Every biosample matching ``filter_``, following ``next_cursor``."""
    session = session or requests.Session()
    out, cursor = [], "*"
    while cursor:
        resp = session.get(f"{NMDC_API}/biosamples", timeout=120,
                           params={"filter": filter_, "per_page": per_page, "cursor": cursor})
        resp.raise_for_status()
        body = resp.json()
        out += body.get("results", [])
        cursor = body.get("meta", {}).get("next_cursor")
        logger.info("NMDC: %d of %s biosamples", len(out), body.get("meta", {}).get("count"))
        if limit and len(out) >= limit:
            return out[:limit]
    return out


# ─── Fields ──────────────────────────────────────────────────────────────────

def _term(value) -> dict | None:
    """{id, name} from an NMDC term value (or a bare CURIE string)."""
    if isinstance(value, dict):
        t = value.get("term") or {}
        if t.get("id"):
            return {"id": t["id"], "name": t.get("name") or ""}
        raw = (value.get("has_raw_value") or "").strip()
        return {"id": None, "name": raw} if raw else None
    if isinstance(value, str) and value.strip():
        return {"id": value.strip(), "name": ""} if ":" in value else {"id": None, "name": value.strip()}
    return None


def _strings(value) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def read_sample(rec: dict) -> dict:
    """The fields the graph uses."""
    return {
        "id": rec.get("id"),
        "name": rec.get("name") or "",
        **{slot: _term(rec.get(slot)) for slot in TRIAD},
        "gold_path": [str(rec.get(f) or "").strip() for f in GOLD_PATH],
        "gold_biosamples": [g.split(":", 1)[-1] for g in _strings(rec.get("gold_biosample_identifiers"))],
        "studies": _strings(rec.get("associated_studies")),
        "geo_loc_name": _term(rec.get("geo_loc_name")),
        "lat_lon": rec.get("lat_lon"),
    }


# ─── GOLD ────────────────────────────────────────────────────────────────────

def fetch_gold(url: str = GOLD_OBO_URL) -> list[dict]:
    """GOLD classes, read from ``url`` (not saved to disk)."""
    logger.info("Reading %s", url)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return parse_gold_obo(resp.text)


def parse_gold_obo(text: str) -> list[dict]:
    """[Term] stanzas: {id, name, parents, mixs: {env_broad: ENVO id, ...}}."""
    terms, cur = [], None
    for line in text.splitlines() + ["["]:
        line = line.strip()
        if line.startswith("["):
            if cur and cur.get("id") and cur.get("name"):
                terms.append(cur)
            cur = {"parents": [], "mixs": {}} if line == "[Term]" else None
            continue
        if cur is None or ": " not in line:
            continue
        tag, value = line.split(": ", 1)
        if tag in ("id", "name"):
            cur[tag] = value.strip()
        elif tag == "is_a":
            cur["parents"].append(value.split(" ! ")[0].strip())
        elif tag == "property_value" and value.startswith("mixs:env_"):
            prop, obj = (value.split() + [""])[:2]
            cur["mixs"][prop.removeprefix("mixs:")] = obj
    return terms


def _key(steps) -> str:
    return " > ".join(" ".join(s.lower().split()) for s in steps)


class GoldIndex:
    """GOLD classes by path name ("Environmental > Terrestrial > Soil > ...")."""

    def __init__(self, terms: list[dict]):
        self.by_id = {t["id"]: t for t in terms}
        self.by_path = {}
        for t in terms:
            self.by_path.setdefault(_key(t["name"].split(">")), t)

    def lookup(self, path: list[str]) -> tuple[dict, str] | None:
        """The class for ``path``, or for its deepest prefix GOLD has; with exact/broadMatch."""
        steps = list(path)
        while steps and steps[-1].strip().lower() in EMPTY_STEPS:
            steps.pop()
        full = len(steps)
        while steps:
            t = self.by_path.get(_key(steps))
            if t:
                return t, "exactMatch" if len(steps) == full else "broadMatch"
            steps.pop()
        return None

    def ancestors(self, gold_id: str) -> list[str]:
        out, todo = [], list(self.by_id.get(gold_id, {}).get("parents", []))
        while todo:
            p = todo.pop()
            if p in self.by_id and p not in out:
                out.append(p)
                todo += self.by_id[p]["parents"]
        return out


# ─── Graph ───────────────────────────────────────────────────────────────────

def _tid(t: dict | None) -> str | None:
    if not t:
        return None
    return t["id"] or "text:" + t["name"].lower()


def _url(curie: str | None) -> str | None:
    if curie and curie.startswith("ENVO:"):
        return ENVO_IRI + curie.split(":", 1)[1]
    return None


def build_graph(samples: list[dict], gold: GoldIndex | None = None) -> dict:
    """Context tree (broad > local > medium) with ENVO terms, linked to GOLD classes and their parents."""
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def add_node(nid, kind, label, **extra):
        return nodes.setdefault(nid, {"id": nid, "kind": kind, "label": label, **extra})

    def add_edge(src, dst, kind, **extra):
        return edges.setdefault((src, dst, kind), {"source": src, "target": dst, "kind": kind, **extra})

    gold_hits = Counter()
    for s in samples:
        # ENVO terms, and the context path they form.
        path, parent = [], None
        for slot in TRIAD:
            t = s.get(slot)
            tid = _tid(t)
            if not tid:
                continue
            add_node(tid, "envo" if t["id"] else "text", t["name"] or t["id"], curie=t["id"], url=_url(t["id"]))
            path.append(tid)
            cid = "ctx:" + " > ".join(path)
            ctx = add_node(cid, "context", t["name"] or t["id"], slot=slot, parent=parent, term=tid,
                           path=list(path), samples=0)
            ctx["samples"] += 1
            add_edge(cid, tid, "envo", match="skos:exactMatch", confidence=1.0, origin="nmdc_record")
            if parent:
                add_edge(parent, cid, "within")
            parent = cid

        # GOLD from the sample's ecosystem path, on its deepest context.
        hit = gold.lookup(s["gold_path"]) if gold else None
        s["gold"] = hit[0]["id"] if hit else None
        if hit and parent:
            g, match = hit
            gold_hits[g["id"]] += 1
            add_node(g["id"], "gold", g["name"].split(">")[-1].strip(), path=g["name"], mixs=g["mixs"])
            e = add_edge(parent, g["id"], "gold", match="skos:" + match, origin="nmdc_gold_path", samples=0)
            e["samples"] += 1
            # Does GOLD's own ENVO triad for that class agree with the sample's?
            mine = {slot.removesuffix("_scale"): (s.get(slot) or {}).get("id") for slot in TRIAD}
            e["agrees"] = all(mine.get(k) == v for k, v in g["mixs"].items()) if g["mixs"] else None

    # GOLD parents, so classes sit in their hierarchy.
    if gold:
        for gid in list(gold_hits):
            for pid in gold.ancestors(gid):
                p = gold.by_id[pid]
                add_node(pid, "gold", p["name"].split(">")[-1].strip(), path=p["name"], mixs=p["mixs"], ancestor=True)
            for pid in [gid] + gold.ancestors(gid):
                for parent in gold.by_id[pid]["parents"]:
                    if parent in gold.by_id:
                        add_edge(pid, parent, "is_a")

    contexts = [n for n in nodes.values() if n["kind"] == "context"]
    meta = {
        "samples": len(samples),
        "with_triad": sum(1 for s in samples if all(s.get(t) for t in TRIAD)),
        "with_gold_path": sum(1 for s in samples if any(s["gold_path"])),
        "matched_to_gold": sum(1 for s in samples if s.get("gold")),
        "contexts": len(contexts),
        "envo_terms": sum(1 for n in nodes.values() if n["kind"] == "envo"),
        "gold_classes": len(gold_hits),
        "gold_disagrees": sum(1 for e in edges.values() if e.get("agrees") is False),
    }
    return {"meta": meta, "nodes": list(nodes.values()), "edges": list(edges.values())}


# ─── Run ─────────────────────────────────────────────────────────────────────

def run(data_dir: Path, filter_: str = SOIL_FILTER, limit: int | None = None, gold_url: str = GOLD_OBO_URL) -> dict:
    out = data_dir / "soil" / "nmdc"
    out.mkdir(parents=True, exist_ok=True)
    samples = [read_sample(r) for r in download(filter_, limit)]
    try:
        gold = GoldIndex(fetch_gold(gold_url))
    except requests.RequestException:
        logger.exception("Could not read GOLD; building without it")
        gold = None
    graph = build_graph(samples, gold)
    graph["meta"].update({"source": f"{NMDC_API}/biosamples?filter={filter_}", "gold_source": gold_url})
    (out / "samples.json").write_text(json.dumps(samples, ensure_ascii=False))
    (out / "graph.json").write_text(json.dumps(graph, ensure_ascii=False))
    logger.info("Wrote %s: %s", out, graph["meta"])
    return graph


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR", "data")))
    ap.add_argument("--filter", default=SOIL_FILTER, help="NMDC biosample filter (default: %(default)s)")
    ap.add_argument("--limit", type=int, help="stop after this many samples (for a quick test)")
    args = ap.parse_args()
    run(args.data_dir, args.filter, args.limit)


if __name__ == "__main__":
    main()
