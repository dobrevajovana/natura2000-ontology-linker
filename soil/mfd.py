"""Turn the MFD habitat ontology into the viewer's JSON.

Source: ``data/ontology/latest_mfd-habitat-ontology.xlsx`` from
https://github.com/cmc-aau/mfd_metadata (GPL-3.0), read straight from its URL
(pinned to one commit; set MFD_ONTOLOGY_URL or --url for another). It is the
five-level MFDO (sample type > area type > MFDO1 > MFDO2 > MFDO3) with its
crosswalk to Natura 2000, EUNIS and EMPO.

Classes with a Natura 2000 code go through the Natura 2000 linker. Classes
without one are matched to ENVO by name (OLS4, geoapp's matcher).

Output (``<data>/mfd/``):

* ``ontology.json`` -- a flat list of tree nodes; each has its parent, level
  and crosswalk codes.
* ``envo.json`` -- ENVO matches for class names, kept between runs.
* ``graph.json`` -- the knowledge graph: MFD classes, linked through their
  Natura 2000 code to the linker's habitat terms and on to Wikidata, EUNIS,
  ENVO and any ontology added in the main viewer; or straight to ENVO when
  they have no code; plus MFD's own EUNIS and EMPO crosswalk. Built from the linker's ``terms.json`` and ``mappings.json``
  when they are there; without them the graph holds the MFD crosswalk only.

Run:  python -m soil.mfd [--data-dir data] [--url ONTOLOGY_URL] [--linker-dir data] [--no-search]
      python -m soil.mfd --graph-only    # after a new linker run
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger("soil.mfd")

MFD_REPO = "cmc-aau/mfd_metadata"
MFD_COMMIT = "b1b17d4b4e8e289c4380d878c8bf1516eb107791"  # main, 2026-02-23
ONTOLOGY_PATH = "data/ontology/latest_mfd-habitat-ontology.xlsx"
ONTOLOGY_URL = os.environ.get(
    "MFD_ONTOLOGY_URL", f"https://raw.githubusercontent.com/{MFD_REPO}/{MFD_COMMIT}/{ONTOLOGY_PATH}")

# ENVO by name, for classes without a Natura 2000 code.
MIN_CONFIDENCE = 0.70
REVIEW_BELOW = 0.85
SKIP_LABELS = {"other", "unknown", "mixed", "natural", "urban", "agriculture", "subterranean"}

LEVELS = ["mfd_sampletype", "mfd_areatype", "mfd_hab1", "mfd_hab2", "mfd_hab3"]
LEVEL_NAMES = ["Sample type", "Area type", "MFDO1", "MFDO2", "MFDO3"]
CODE_COLS = {2: "mfd_hab1_code", 3: "mfd_hab2_code", 4: "mfd_hab3_code"}

# Left in the data folder by earlier versions of this viewer.
OBSOLETE = ["samples.json", "raw/latest_mfd_db.xlsx", "raw/latest_mfd-habitat-ontology.xlsx"]


def _clean(v):
    """A cell as a plain string, or None for blanks and NaN."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def _path(row) -> tuple:
    """The row's MFDO path, cut at the first blank level."""
    out = []
    for col in LEVELS:
        v = _clean(row.get(col))
        if v is None:
            break
        out.append(v)
    return tuple(out)


def node_id(path: tuple) -> str:
    return " > ".join(path)


def build_ontology(onto: pd.DataFrame) -> list[dict]:
    """Tree nodes for every path in the ontology, ancestors before descendants."""
    nodes: dict[tuple, dict] = {}

    for row in onto.to_dict("records"):
        path = _path(row)
        if not path:
            continue
        for i in range(1, len(path) + 1):
            p = path[:i]
            nodes.setdefault(p, {
                "id": node_id(p),
                "label": p[-1],
                "level": i - 1,
                "level_name": LEVEL_NAMES[i - 1],
                "parent": node_id(p[:-1]) if i > 1 else None,
                "code": None,
                "natura2000": None,
                "eunis": None,
                "empo": None,
            })
        n = nodes[path]
        # A row describes its deepest named level; its crosswalk belongs there.
        n["code"] = _clean(row.get(CODE_COLS.get(len(path) - 1, ""), None))
        n["natura2000"] = _clean(row.get("Natura2000"))
        n["eunis"] = _clean(row.get("EUNIS"))
        n["empo"] = " ".join((_clean(row.get("EMPO")) or "").split()) or None

    return list(nodes.values())


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def build_graph(nodes: list[dict], terms: list[dict], mappings: list[dict], envo: dict | None = None) -> dict:
    """Nodes and edges joining the MFD ontology to the linker's knowledge graph.

    Node ids are prefixed by kind (``mfd:``, ``n2k:``, ``eunis:``, ``empo:``) or
    are the target URI for the linker's external terms. Each MFD node keeps the
    tree id it came from in ``ref`` so the viewer can follow the tree selection.
    ``envo`` (label -> hits) links classes without a Natura 2000 code to ENVO.
    """
    by_code: dict[str, dict] = {}
    for t in terms:
        if t.get("category") == "habitat":
            for c in t.get("codes") or []:
                by_code.setdefault(str(c), t)
    maps_by_term: dict[str, list[dict]] = {}
    for m in mappings:
        maps_by_term.setdefault(m.get("source_id"), []).append(m)

    g_nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def add_node(nid, kind, label, **extra):
        g_nodes.setdefault(nid, {"id": nid, "kind": kind, "label": label, **extra})

    def add_edge(src, dst, kind, **extra):
        edges.setdefault((src, dst, kind), {"source": src, "target": dst, "kind": kind, **extra})

    for n in nodes:
        links = [(k, n.get(k)) for k in ("natura2000", "eunis", "empo") if n.get(k)]
        direct = [] if n.get("natura2000") else (envo or {}).get(n["label"], [])
        if not links and not direct:
            continue
        mid = "mfd:" + n["id"]
        add_node(mid, "mfd", n["label"], ref=n["id"], level=n["level_name"])
        for h in direct:
            add_node(h["uri"], "ols4_envo", h["label"] or h["curie"], url=h["uri"], curie=h["curie"])
            add_edge(mid, h["uri"], "linker", match="skos:" + h["match_type"], confidence=h["confidence"],
                     method="ols4_envo_by_name", term=None, direct=True,
                     review=h["confidence"] < REVIEW_BELOW)
        for kind, value in links:
            if kind == "natura2000":
                term = by_code.get(value)
                nid = "n2k:" + value
                # Codes ending in 00 (1200, 9100, ...) are Annex I group headings,
                # not habitat types, so the linker has no term for them.
                label = term["label"] if term else (
                    f"Natura 2000 group {value}" if value.endswith("00") else f"Natura 2000 {value}")
                add_node(nid, "natura2000", label,
                         code=value, linked=bool(term), url=(term or {}).get("source_url"))
                add_edge(mid, nid, "mfd_crosswalk")
                for m in maps_by_term.get((term or {}).get("id"), []):
                    uri = m.get("target_uri")
                    if not uri:
                        continue
                    add_node(uri, m.get("source_ontology") or "external", m.get("target_label") or uri, url=uri)
                    # review: the linker itself flagged this one for expert review.
                    add_edge(nid, uri, "linker", match=m.get("match_type"),
                             confidence=m.get("confidence"), method=m.get("method"),
                             term=term["id"], review=bool(m.get("review")))
            elif kind == "eunis":
                add_node("eunis:" + value, "eunis_mfd", f"EUNIS {value}", code=value)
                add_edge(mid, "eunis:" + value, "mfd_crosswalk")
            else:
                add_node("empo:" + value, "empo", value.split(";")[-1].strip(), full=value)
                add_edge(mid, "empo:" + value, "mfd_crosswalk")

    return {"nodes": list(g_nodes.values()), "edges": list(edges.values())}


def write_graph(data_dir: Path, linker_dir: Path) -> dict:
    """graph.json from ontology.json and the linker's output, whichever is there."""
    out_dir = data_dir / "mfd"
    nodes = _load_json(out_dir / "ontology.json", {}).get("nodes", [])
    terms = _load_json(linker_dir / "terms.json", [])
    mappings = _load_json(linker_dir / "mappings.json", [])
    mappings += _load_json(linker_dir / "mappings_added.json", {}).get("mappings", [])
    envo = _load_json(out_dir / "envo.json", {})
    graph = build_graph(nodes, terms, mappings, envo)
    graph["meta"] = {"linker_terms": len(terms), "linker_mappings": len(mappings),
                     "envo_by_name": sum(1 for e in graph["edges"] if e.get("direct"))}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "graph.json").write_text(json.dumps(graph, ensure_ascii=False))
    logger.info("Wrote graph: %d nodes, %d edges (linker: %d terms, %d mappings from %s)",
                len(graph["nodes"]), len(graph["edges"]), len(terms), len(mappings), linker_dir)
    return graph


def fetch_ontology(url: str = ONTOLOGY_URL) -> pd.DataFrame:
    """The ontology workbook, read from its URL (not saved to disk)."""
    logger.info("Reading %s", url)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return pd.read_excel(io.BytesIO(resp.content))


_session: requests.Session | None = None


def ols4_envo(label: str) -> list[dict]:
    """ENVO hits for a class name, via geoapp's calibrated OLS4 matcher (cached on disk)."""
    from geoapp.ontolink import _cached_lookup

    global _session
    if _session is None:
        _session = requests.Session()
    return [t.to_dict() for t in _cached_lookup("ols4", label, "envo", _session, True)]


def match_envo(nodes: list[dict], known: dict, search=ols4_envo) -> dict:
    """label -> best ENVO hit(s) for classes without a Natura 2000 code. ``known`` is reused."""
    out = {}
    for n in nodes:
        label = n["label"]
        if n.get("natura2000") or label.lower() in SKIP_LABELS:
            continue
        if label in known:
            out[label] = known[label]
            continue
        hits = [h for h in search(label) if (h.get("confidence") or 0) >= MIN_CONFIDENCE]
        best = max((h.get("confidence") or 0 for h in hits), default=None)
        out[label] = [{k: h.get(k) for k in ("uri", "curie", "label", "match_type", "confidence")}
                      for h in hits if h.get("confidence") == best]
    return out


def build(data_dir: Path, onto: pd.DataFrame | None = None, url: str = ONTOLOGY_URL, search=ols4_envo) -> dict:
    """ontology.json (and envo.json) from the ontology workbook, read from ``url`` unless ``onto`` is given."""
    out_dir = data_dir / "mfd"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in OBSOLETE:
        (out_dir / name).unlink(missing_ok=True)
    if onto is None:
        onto = fetch_ontology(url)

    nodes = build_ontology(onto)
    meta = {
        "source": url,
        "classes": len(nodes),
        "natura2000_codes": len({n["natura2000"] for n in nodes if n["natura2000"]}),
    }
    (out_dir / "ontology.json").write_text(json.dumps({"meta": meta, "nodes": nodes}, ensure_ascii=False))
    if search:
        envo = match_envo(nodes, _load_json(out_dir / "envo.json", {}), search)
        (out_dir / "envo.json").write_text(json.dumps(envo, ensure_ascii=False, indent=1))
        meta["envo_matched"] = sum(1 for v in envo.values() if v)
    logger.info("Wrote %d ontology classes to %s", len(nodes), out_dir)
    return meta


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR", "data")))
    ap.add_argument("--url", default=ONTOLOGY_URL, help="where to read latest_mfd-habitat-ontology.xlsx from")
    ap.add_argument("--linker-dir", type=Path, default=Path(os.environ.get("LINKER_DIR", "data")),
                    help="folder with the Natura 2000 linker's terms.json and mappings.json")
    ap.add_argument("--no-search", action="store_true", help="skip ENVO matching for classes without a code")
    ap.add_argument("--graph-only", action="store_true", help="rebuild graph.json only")
    args = ap.parse_args()
    if not args.graph_only:
        build(args.data_dir, url=args.url, search=None if args.no_search else ols4_envo)
    write_graph(args.data_dir, args.linker_dir)


if __name__ == "__main__":
    main()
