"""Command line for the validation tool.

    python -m verdant_validator check  FILE [--online] [--json OUT]   automatic checks, a summary and the findings
    python -m verdant_validator serve  [FILE] [--folder DIR] [--reviews DIR] [--port 8097] [--offline]
    python -m verdant_validator export FILE --reviews FILE.jsonl [--format csv|sssom] [--out OUT]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from verdant_validator import check, export_csv, export_sssom, load, ReviewStore


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m verdant_validator", description="Check and review predicted mappings.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="run the automatic checks")
    c.add_argument("file")
    c.add_argument("--online", action="store_true", help="also look the terms up (OLS4, GBIF, Wikidata)")
    c.add_argument("--json", type=Path, help="write every mapping with its findings here")
    s = sub.add_parser("serve", help="start the review tool")
    s.add_argument("file", nargs="?")
    s.add_argument("--folder", type=Path, help="review every file in this folder; uploads go here too")
    s.add_argument("--reviews", type=Path, default=Path("reviews"), help="where decisions are saved (default: reviews/)")
    s.add_argument("--port", type=int, default=8097)
    s.add_argument("--offline", action="store_true", help="skip the online term lookups")
    e = sub.add_parser("export", help="write the reviewed mappings")
    e.add_argument("file")
    e.add_argument("--reviews", type=Path, required=True, help="the decisions file (.jsonl)")
    e.add_argument("--format", choices=("csv", "sssom"), default="sssom")
    e.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "check":
        maps = load(args.file)
        summary = check(maps, online=args.online)
        print(json.dumps(summary, indent=2))
        for m in maps:
            for f in m["checks"]:
                if f["level"] != "info":
                    print(f"{f['level']:8} {m['subject_label']} -> {m.get('object_label') or m['object_id']}: {f['message']}")
        if args.json:
            args.json.write_text(json.dumps(maps, indent=2, ensure_ascii=False))
        return 1 if summary["with_errors"] else 0
    if args.cmd == "serve":
        if not args.file and not args.folder:
            ap.error("give a FILE, a --folder, or both")
        from verdant_validator.web import serve
        serve(args.file, folder=args.folder, reviews=args.reviews, port=args.port, online=not args.offline)
        return 0
    maps = load(args.file)
    check(maps)
    state = ReviewStore(args.reviews).state()
    text = export_csv(maps, state) if args.format == "csv" else export_sssom(maps, state, Path(args.file).stem)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
