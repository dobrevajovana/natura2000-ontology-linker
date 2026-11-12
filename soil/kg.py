"""One soil knowledge graph from the MFD and NMDC graphs, joined on shared ontology terms.

Inputs (built first by their own entrypoints):
* ``<mfd dir>/mfd/{ontology,graph}.json`` from ``python -m soil.mfd``
* ``<data>/soil/nmdc/graph.json`` from ``python -m soil.nmdc``
* GOLD (gold.obo, read from its URL) to link ENVO terms to the GOLD classes that use them

ENVO terms get one id (``ENVO:...``) on both sides, so a term either dataset
uses is one node. GOLD classes that name an ENVO term in the graph as their
``env_local`` get an edge from it, so MFD (which has no GOLD) reaches GOLD too.
Each node lists the datasets that reach it; nodes reached by both are shared.

Output: ``<data>/soil/kg.json``.

Run:  python -m soil.kg [--data-dir data] [--mfd-dir data] [--no-gold]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import defaultdict
from pathlib import Path

import requests

from soil import nmdc

logger = logging.getLogger("soil.kg")

DATASETS = ("mfd", "nmdc")
# Most GOLD classes added per ENVO term, shallowest first; "soil"-like terms would add hundreds.
MAX_GOLD_PER_TERM = 3
OBO_PURL = "http://purl.obolibrary.org/obo/"
KIND = {"ols4_envo": "envo"}


def norm_id(nid: str) -> str:
    """OBO PURLs as CURIEs (http://purl.obolibrary.org/obo/ENVO_1 -> ENVO:1); everything else unchanged."""
    if nid.startswith(OBO_PURL):
        local = nid[len(OBO_PURL):]
        if "_" in local:
            prefix, rest = local.split("_", 1)
            return f"{prefix}:{rest}"
    return nid


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def merge(mfd_graph: dict, nmdc_graph: dict, gold: nmdc.GoldIndex | None = None) -> dict:
    """The two graphs as one, with ``datasets`` on every node and edge."""
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def add_node(n: dict):
        nid = norm_id(n["id"])
        cur = nodes.setdefault(nid, {**n, "id": nid, "kind": KIND.get(n.get("kind"), n.get("kind"))})
        if not cur.get("label") or cur["label"] == nid:
            cur["label"] = n.get("label") or nid
        if nid.startswith("ENVO:"):
            cur["curie"] = nid
            cur["url"] = cur.get("url") or OBO_PURL + nid.replace(":", "_")

    def add_edge(e: dict):
        src, dst = norm_id(e["source"]), norm_id(e["target"])
        edges.setdefault((src, dst, e["kind"]), {**e, "source": src, "target": dst})

    for g in (mfd_graph, nmdc_graph):
        for n in g.get("nodes", []):
            add_node(n)
        for e in g.get("edges", []):
            add_edge(e)

    if gold:
        _bridge_gold(nodes, edges, gold, add_node, add_edge)

    _reach(nodes, edges)
    return {"meta": _meta(nodes, edges), "nodes": list(nodes.values()), "edges": list(edges.values())}


def _bridge_gold(nodes, edges, gold, add_node, add_edge):
    """ENVO term -> GOLD classes whose env_local is that term, and their is_a parents."""
    by_local = defaultdict(list)
    for t in gold.by_id.values():
        if t["mixs"].get("env_local"):
            by_local[t["mixs"]["env_local"]].append(t)
    for envo in [nid for nid, n in nodes.items() if n["kind"] == "envo"]:
        users = sorted(by_local.get(envo, []), key=lambda t: (t["name"].count(">"), t["name"]))
        for t in users[:MAX_GOLD_PER_TERM]:
            add_node({"id": t["id"], "kind": "gold", "label": t["name"].split(">")[-1].strip(),
                      "path": t["name"], "mixs": t["mixs"]})
            add_edge({"source": envo, "target": t["id"], "kind": "gold_env_local",
                      "match": "skos:relatedMatch", "origin": "gold_mixs"})
            for pid in gold.ancestors(t["id"]):
                p = gold.by_id[pid]
                add_node({"id": pid, "kind": "gold", "label": p["name"].split(">")[-1].strip(),
                          "path": p["name"], "mixs": p["mixs"], "ancestor": True})
            for pid in [t["id"]] + gold.ancestors(t["id"]):
                for parent in gold.by_id[pid]["parents"]:
                    if parent in gold.by_id:
                        add_edge({"source": pid, "target": parent, "kind": "is_a"})


def _below(starts, out, nodes) -> set:
    """Every node reachable from ``starts`` along the edges."""
    seen, todo = set(starts), list(starts)
    while todo:
        for t in out[todo.pop()]:
            if t in nodes and t not in seen:
                seen.add(t)
                todo.append(t)
    return seen


def _reach(nodes, edges):
    """Each node's ``datasets``: which datasets' own nodes (MFD classes, NMDC contexts) lead to it."""
    out = defaultdict(list)
    for (s, t, _k) in edges:
        out[s].append(t)
    own = {"mfd": "mfd", "nmdc": "context"}
    for n in nodes.values():
        n["datasets"] = []
    for dataset, kind in own.items():
        for nid in _below([n["id"] for n in nodes.values() if n["kind"] == kind], out, nodes):
            nodes[nid]["datasets"].append(dataset)
    for e in edges.values():
        e["datasets"] = list(nodes.get(e["source"], {}).get("datasets", []))
    # MFD classes that lead to something NMDC also reaches.
    for n in nodes.values():
        if n["kind"] == "mfd":
            n["meets_nmdc"] = any("nmdc" in nodes[t]["datasets"] for t in _below([n["id"]], out, nodes))


def _meta(nodes, edges) -> dict:
    shared = [n for n in nodes.values() if len(n["datasets"]) == len(DATASETS)]
    by_kind = defaultdict(int)
    for n in shared:
        by_kind[n["kind"]] += 1
    return {
        "nodes": len(nodes), "edges": len(edges),
        "mfd_classes": sum(1 for n in nodes.values() if n["kind"] == "mfd"),
        "nmdc_contexts": sum(1 for n in nodes.values() if n["kind"] == "context"),
        "shared": len(shared), "shared_by_kind": dict(by_kind),
        "mfd_classes_meeting_nmdc": sum(1 for n in nodes.values() if n.get("meets_nmdc")),
    }


def trees(mfd_ontology: dict, nmdc_graph: dict) -> dict:
    """The two left-panel trees: MFD hierarchy and NMDC context tree (id, label, parent, ...)."""
    mfd = [{"id": "mfd:" + n["id"], "label": n["label"], "parent": "mfd:" + n["parent"] if n["parent"] else None,
            "level": n["level_name"], "code": n.get("natura2000")} for n in mfd_ontology.get("nodes", [])]
    ctx = [{"id": n["id"], "label": n["label"], "parent": n.get("parent"), "level": n.get("slot"),
            "samples": n.get("samples")} for n in nmdc_graph.get("nodes", []) if n["kind"] == "context"]
    return {"mfd": mfd, "nmdc": ctx}


def build(data_dir: Path, mfd_dir: Path, gold_url: str | None = nmdc.GOLD_OBO_URL) -> dict:
    mfd_graph = _load(mfd_dir / "mfd" / "graph.json", {})
    mfd_onto = _load(mfd_dir / "mfd" / "ontology.json", {})
    nmdc_graph = _load(data_dir / "soil" / "nmdc" / "graph.json", {})
    if not mfd_graph and not nmdc_graph:
        raise FileNotFoundError("No MFD or NMDC graph yet: run python -m soil.mfd and python -m soil.nmdc first")
    gold = None
    if gold_url:
        try:
            gold = nmdc.GoldIndex(nmdc.fetch_gold(gold_url))
        except requests.RequestException:
            logger.exception("Could not read GOLD; MFD will not reach GOLD")
    kg = merge(mfd_graph, nmdc_graph, gold)
    kg["trees"] = trees(mfd_onto, nmdc_graph)
    kg["meta"]["inputs"] = {"mfd": bool(mfd_graph), "nmdc": bool(nmdc_graph), "gold": bool(gold)}
    out = data_dir / "soil" / "kg.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(kg, ensure_ascii=False))
    logger.info("Wrote %s: %s", out, kg["meta"])
    return kg


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR", "data")))
    ap.add_argument("--mfd-dir", type=Path,
                    default=Path(os.environ.get("MFD_DATA_DIR") or os.environ.get("DATA_DIR", "data")),
                    help="folder holding mfd/graph.json (the MFD viewer's data folder)")
    ap.add_argument("--no-gold", action="store_true", help="do not read GOLD (MFD will not reach GOLD)")
    args = ap.parse_args()
    build(args.data_dir, args.mfd_dir, None if args.no_gold else nmdc.GOLD_OBO_URL)


if __name__ == "__main__":
    main()
