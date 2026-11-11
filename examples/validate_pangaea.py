"""Validate the PANGAEA test's links in Python with the VERDANT validator.

The input is the Natura 2000 linker run on the 30 PANGAEA names of the
pangaea-species-biome-test branch (experiments/sample_species_biome.csv):
examples/pangaea/mappings.json, 38 links to GBIF, NCBITaxon, Wikidata, ENVO
and DBpedia, with terms.json beside it for each name's group.

    python examples/validate_pangaea.py                      # checks and a report; nothing is decided
    python examples/validate_pangaea.py --offline            # without looking terms up online
    python examples/validate_pangaea.py --reviewer "Jovana"  # also record your decisions, one prompt per open link
    python examples/validate_pangaea.py --serve              # or decide on the review page instead

Decisions go to reviews/pangaea.jsonl (the same file the review page uses);
the decided links come out as reviews/pangaea.reviewed.sssom.tsv.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
try:
    from verdant_validator import Validator
except ImportError:            # run from a checkout without `pip install -e .`
    sys.path.insert(0, str(ROOT))
    from verdant_validator import Validator


def report(v: Validator) -> None:
    s = v.summary()
    print(f"{s['mappings']} links for {s['subjects']} names")
    print("  by source:     ", ", ".join(f"{k} {n}" for k, n in sorted(s["by_source"].items(), key=lambda kv: -kv[1])))
    print("  by match type: ", ", ".join(f"{k.replace('skos:', '')} {n}" for k, n in s["by_match_type"].items()))
    c = s["checks"]
    print(f"  checks ({'online' if c['online'] else 'offline'}): {c['with_errors']} links with an error, "
          f"{c['with_warnings']} with a warning")
    for f in v.findings(level="warning"):
        print(f"    {f['level']:8} {f['subject']} -> {f['object']}: {f['message']}")
    d = s["decisions"]
    print("  decisions:     ", ", ".join(f"{k} {n}" for k, n in d.items()))


def names_without_links(v: Validator) -> list[str]:
    """PANGAEA names the linker found nothing for (they are not in mappings.json at all)."""
    import json
    terms = json.loads((HERE / "pangaea" / "terms.json").read_text(encoding="utf-8"))
    linked = {m["subject_id"] for m in v}
    return [t["label"] for t in terms if t["id"] not in linked]


def ask(v: Validator, reviewer: str) -> None:
    """One prompt per open link, those the checks flag first."""
    keys = {"c": "correct", "i": "incorrect", "u": "unsure"}
    for m in v.pending():
        print(f"\n{m['subject_label']} ({m.get('subject_category')}) -> {m.get('object_label') or m['object_id']}")
        print(f"  {m['object_id']}  {m['predicate_id']}  {m.get('object_source')}  confidence {m.get('confidence')}")
        for f in m.get("checks") or []:
            print(f"  ! {f['message']}")
        answer = input("  [c]orrect, [i]ncorrect, [u]nsure, [s]kip, [q]uit: ").strip().lower()[:1]
        if answer == "q":
            break
        if answer in keys:
            comment = input("  comment (optional): ").strip()
            v.decide(m["key"], keys[answer], reviewer=reviewer, comment=comment)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true", help="skip the OLS4 / GBIF / Wikidata lookups")
    ap.add_argument("--reviewer", help="record decisions under this name")
    ap.add_argument("--serve", action="store_true", help="open the review page on http://localhost:8097")
    args = ap.parse_args()

    v = Validator(HERE / "pangaea" / "mappings.json", reviews=ROOT / "reviews" / "pangaea.jsonl", name="pangaea")
    v.check(online=not args.offline)
    report(v)
    missing = names_without_links(v)
    print(f"  no link at all: {len(missing)} names: {', '.join(missing)}")
    groups = Counter(m.get("subject_category") for m in v)
    print(f"  links per group: {dict(groups)}")

    if args.serve:
        v.serve()
        return
    if args.reviewer:
        ask(v, args.reviewer)
        report(v)
    out = ROOT / "reviews" / "pangaea.reviewed.sssom.tsv"
    out.parent.mkdir(exist_ok=True)
    v.to_sssom(out)
    v.to_csv(ROOT / "reviews" / "pangaea-reviews.csv")
    print(f"\nWrote {out.relative_to(ROOT)} and reviews/pangaea-reviews.csv")


if __name__ == "__main__":
    main()
