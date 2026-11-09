"""Benchmark geoapp's OLS4 matcher against curated text -> ontology mappings.

Each source is turned into the same kind of case -- a piece of text, the
ontology to search, the gold IDs and, where the source states it, the gold
SKOS predicate -- and every case is run through ``ontolink.ols4_search``
exactly as a BioSample attribute would be.

Sources (downloaded on first use into ``data/validation/``):

* ``gold``  -- GOLD ecosystem terms -> ENVO, curated SSSOM (cmungall/gold-ontology).
  The only source with typed predicates (exact / close / broad / narrow), so it
  is the one that checks match-type classification. Its ``owl:differentFrom``
  rows are kept as negatives: returning that ID is a known wrong match.
  Caveat: the curated set started from a lexical matcher, so it flatters ours.
* ``micront`` -- microntology terms -> ENVO / UBERON / FOODON / PO (Zenodo,
  CC-BY-4.0). IDs only, no predicates; labels are short and often identical to
  the target label, so read it as a floor, not a hard test.
* ``ena`` -- real MIxS field text of ENA metagenomes, gold = their microntology
  tags (see :mod:`validation.ena_samples`). Each case keeps its field, and a hit
  below a tagged term counts as correct, because the tags are coarse.

Run:  python -m validation.benchmark [--source gold,micront,ena] [--limit N] [--field env_broad_scale]
                                     [--per-study 3]

``--field`` treats every case as text from that MIxS field, so ENVO lookups are
confined to the field's branch (biome, material) the way BioSample values are.
``ena`` cases carry their own field and ignore it.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import requests

from geoapp import config
from geoapp.ontolink import FIELD_ALSO_MAP, FIELD_BRANCH_MAP, _cached_lookup

logger = logging.getLogger(__name__)

SOURCES = {
    "gold": "https://raw.githubusercontent.com/cmungall/gold-ontology/main/mappings/gold-to-envo.curated.sssom.tsv",
    "micront": "https://zenodo.org/records/18162129/files/microntology.terms.tsv?download=1",
}

# Confidence bands: each rule has its own calibrated confidence (ontolink._CONFIDENCE),
# so a band per value checks each rule's claimed precision against the measured one.
BANDS = [(1.0, "1.00"), (0.90, "0.90"), (0.85, "0.85"), (0.75, "0.75"), (0.70, "0.70"),
         (0.35, "0.35"), (0.30, "0.30"), (0.0, "<0.30")]


@dataclass
class Case:
    source: str
    text: str
    ontology: str                       # OLS4 ontology id, e.g. "envo"
    gold_ids: set[str]                  # CURIEs, e.g. {"ENVO:00002003"}
    gold_predicate: str = ""            # bare SKOS name ("broadMatch"), "" when unknown
    wrong_ids: set[str] = field(default_factory=set)   # known-wrong targets
    field: str = ""                     # MIxS field the text came from, "" when unknown
    match_descendants: bool = False     # a hit below a gold term also counts as correct


def _download(name: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.tsv"
    if not path.exists():
        logger.info("downloading %s", SOURCES[name])
        resp = requests.get(SOURCES[name], timeout=60, headers={"User-Agent": config.USER_AGENT})
        resp.raise_for_status()
        path.write_bytes(resp.content)
    return path


def _rows(path: Path) -> list[dict]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    return list(csv.DictReader(lines, delimiter="\t"))


def load_gold(path: Path) -> list[Case]:
    """One case per GOLD leaf; positives and differentFrom negatives merged per text."""
    by_text: dict[str, Case] = {}
    for r in _rows(path):
        obj, pred = (r.get("object_id") or "").strip(), (r.get("predicate_id") or "").strip()
        label = (r.get("subject_label") or "").split(">")[-1].strip()
        if not obj.startswith("ENVO:") or not label:
            continue
        case = by_text.setdefault(label.lower(), Case("gold", label, "envo", set()))
        if pred == "owl:differentFrom":
            case.wrong_ids.add(obj)
        elif pred.startswith("skos:"):
            case.gold_ids.add(obj)
            case.gold_predicate = case.gold_predicate or pred.split(":", 1)[1]
    return list(by_text.values())


def load_micront(path: Path) -> list[Case]:
    """One case per (term, target ontology): 'intestine' yields an ENVO and an UBERON case."""
    cases = []
    for r in _rows(path):
        text = (r.get("display_term") or "").strip()
        by_prefix: dict[str, set[str]] = defaultdict(set)
        for curie in (r.get("ontology_link") or "").split(";"):
            curie = curie.strip()
            if ":" in curie:
                by_prefix[curie.split(":", 1)[0]].add(curie)
        for prefix, ids in by_prefix.items():
            cases.append(Case("micront", text, prefix.lower(), ids))
    return [c for c in cases if c.text]


def _load_ena(out_dir: Path) -> list[Case]:
    from .ena_samples import load_ena
    return load_ena(out_dir, _download("micront", out_dir), _PER_STUDY)


_PER_STUDY = 3
LOADERS = {
    "gold": lambda out_dir: load_gold(_download("gold", out_dir)),
    "micront": lambda out_dir: load_micront(_download("micront", out_dir)),
    "ena": _load_ena,
}


def _band(score: float) -> str:
    return next(label for edge, label in BANDS if score >= edge)


def evaluate(cases: list[Case], lookup, field: str = "", ancestors=None) -> tuple[dict, list[dict]]:
    """Run ``lookup(text, ontology, branch, also) -> list[OntologyTerm]`` over ``cases``.

    ``field`` names the MIxS field every case without its own is treated as
    coming from. ``ancestors(curie) -> set[curie]`` lets a hit below a gold term
    count for cases with ``match_descendants``.

    Returns summary metrics and one row per case for review. Gold sets are
    incomplete -- a top hit outside them is not proven wrong -- so "top-1
    correct" is a lower bound on precision.
    """
    rows, band_hits = [], defaultdict(lambda: [0, 0])
    types: Counter = Counter()
    n_pos = found = top1 = no_hit = wrong_returned = 0
    by_field: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])   # cases, any hit, found, top-1
    for c in cases:
        f = c.field or field
        branch = FIELD_BRANCH_MAP.get(f, "") if c.ontology == "envo" else ""
        also = FIELD_ALSO_MAP.get(f, "") if c.ontology == "envo" else ""
        hits = lookup(c.text, c.ontology, branch, also)
        curies = [h.curie for h in hits]

        def correct(curie: str, c=c) -> bool:
            return curie in c.gold_ids or bool(
                c.match_descendants and ancestors and c.gold_ids & ancestors(curie))

        gold_hit = next((h for h in hits if correct(h.curie)), None)
        top = hits[0] if hits else None
        if c.gold_ids:
            n_pos += 1
            found += gold_hit is not None
            no_hit += not hits
            if top is not None:
                ok = top.curie == gold_hit.curie if gold_hit else False
                top1 += ok
                b = band_hits[_band(top.confidence)]
                b[0] += ok
                b[1] += 1
            if c.gold_predicate and gold_hit is not None:
                types[(c.gold_predicate, gold_hit.match_type)] += 1
            bf = by_field[f or "-"]
            bf[0] += 1
            bf[1] += bool(hits)
            bf[2] += gold_hit is not None
            bf[3] += bool(top is not None and gold_hit is not None and top.curie == gold_hit.curie)
        wrong = sorted(c.wrong_ids.intersection(curies))
        wrong_returned += bool(wrong)
        rows.append({
            "source": c.source, "field": c.field or field, "text": c.text, "ontology": c.ontology,
            "gold_ids": ";".join(sorted(c.gold_ids)), "gold_predicate": c.gold_predicate,
            "top_id": top.curie if top else "", "top_label": top.label if top else "",
            "top_type": top.match_type if top else "", "top_confidence": top.confidence if top else "",
            "top_correct": (gold_hit is not None and top.curie == gold_hit.curie) if (top and c.gold_ids) else "",
            "gold_found": gold_hit is not None, "gold_found_type": gold_hit.match_type if gold_hit else "",
            "known_wrong_returned": ";".join(wrong), "all_hits": ";".join(curies),
            "expert_verdict": "",
        })
    summary = {
        "cases": len(cases), "positives": n_pos,
        "recall_at_3": found / n_pos if n_pos else 0.0,
        "top1_correct": top1 / n_pos if n_pos else 0.0,
        "no_hit": no_hit / n_pos if n_pos else 0.0,
        "negatives": sum(1 for c in cases if c.wrong_ids),
        "known_wrong_returned": wrong_returned,
        "bands": {label: tuple(band_hits[label]) for _, label in BANDS if label in band_hits},
        "types": dict(types),
        "by_field": {k: tuple(v) for k, v in by_field.items()},
    }
    return summary, rows


def format_report(name: str, s: dict) -> str:
    out = [f"== {name}: {s['cases']} cases, {s['positives']} with gold IDs",
           f"   recall@3 {s['recall_at_3']:.1%}   top-1 correct {s['top1_correct']:.1%}   no hit {s['no_hit']:.1%}"]
    if s["negatives"]:
        out.append(f"   known-wrong ID returned: {s['known_wrong_returned']} of {s['negatives']} negative cases")
    out.append("   top-1 accuracy by confidence (should be close to the confidence itself):")
    for label, (ok, n) in s["bands"].items():
        out.append(f"     {label:>10}  {ok:>4}/{n:<4} {ok / n:6.1%}")
    if len(s.get("by_field", {})) > 1:
        out.append("   by field:            cases  any hit  recall@3  top-1")
        for f, (n, hit, fnd, t1) in sorted(s["by_field"].items(), key=lambda kv: -kv[1][0]):
            out.append(f"     {f:<20}{n:>6}{hit / n:>9.1%}{fnd / n:>10.1%}{t1 / n:>7.1%}")
    if s["types"]:
        gold_types = sorted({g for g, _ in s["types"]})
        pred_types = sorted({p for _, p in s["types"]})
        out.append("   match type, gold (rows) x ours (columns), for gold IDs found:")
        out.append("     " + " " * 13 + "".join(f"{p[:-5]:>9}" for p in pred_types))
        for g in gold_types:
            out.append(f"     {g:>13}" + "".join(f"{s['types'].get((g, p), 0):>9}" for p in pred_types))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", default="gold,micront", help="comma-separated: " + ",".join(SOURCES))
    ap.add_argument("--limit", type=int, default=0, help="first N cases per source (0 = all)")
    ap.add_argument("--out-dir", default=str(config.DATA_DIR / "validation"))
    ap.add_argument("--field", default="", help="treat every case as this MIxS field, e.g. env_broad_scale")
    ap.add_argument("--per-study", type=int, default=3, help="ena: samples per study")
    ap.add_argument("--no-cache", action="store_true", help="bypass the OLS4 lookup cache")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    out_dir = Path(args.out_dir)
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})

    global _PER_STUDY
    _PER_STUDY = args.per_study
    out_dir.mkdir(parents=True, exist_ok=True)
    from .ena_samples import Ancestors
    ancestors = Ancestors(out_dir / "ancestors.json", session)

    def lookup(text, ontology, branch, also):
        if also not in config.OLS4_ONTOLOGIES:
            also = ""
        return _cached_lookup("ols4", text, ontology, session, not args.no_cache, branch, also)

    all_rows = []
    for name in [s.strip() for s in args.source.split(",") if s.strip()]:
        if name not in LOADERS:
            ap.error(f"unknown source {name!r}")
        cases = LOADERS[name](out_dir)
        if args.limit:
            cases = cases[: args.limit]
        logger.info("%s: %d cases", name, len(cases))
        summary, rows = evaluate(cases, lookup, args.field, ancestors)
        print(format_report(name + (f" as {args.field}" if args.field else ""), summary))
        all_rows += rows

    if all_rows:
        path = out_dir / "results.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0]))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nper-case results (with an empty expert_verdict column): {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
