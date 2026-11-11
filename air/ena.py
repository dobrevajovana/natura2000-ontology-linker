"""List the ENA samples under the air metagenome taxon (655179), for air.build.

Same query as:
  curl -X POST -H "Content-Type: application/x-www-form-urlencoded" \
    -d 'result=sample&query=tax_tree(655179)&fields=sample_accession%2Csample_description%2Ctax_id&format=tsv' \
    "https://www.ebi.ac.uk/ena/portal/api/search"

Writes ``<data>/air/ena_air_samples.tsv`` (the full answer) and
``<data>/air/accessions.txt`` (all of them, or every Nth with --every).

Run:  python -m air.ena [--every 70] [--data-dir data]
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger("air.ena")

ENA_SEARCH = "https://www.ebi.ac.uk/ena/portal/api/search"
AIR_METAGENOME = 655179


def search(tax_id: int = AIR_METAGENOME) -> str:
    resp = requests.post(ENA_SEARCH, timeout=300, data={
        "result": "sample", "query": f"tax_tree({tax_id})",
        "fields": "sample_accession,sample_description,tax_id", "format": "tsv"})
    resp.raise_for_status()
    return resp.text


def accessions(tsv: str, every: int = 1) -> list[str]:
    """Sample accessions from ENA's TSV, every Nth one (spread over all studies, same picks each run)."""
    rows = [l.split("\t")[0].strip() for l in tsv.splitlines()[1:] if l.strip()]
    return [a for i, a in enumerate(rows) if i % every == 0]


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("AIR_DATA_DIR") or os.environ.get("DATA_DIR", "data")))
    ap.add_argument("--every", type=int, default=1, help="keep every Nth sample (e.g. 70 for ~500 of 35,000)")
    args = ap.parse_args()
    out = args.data_dir / "air"
    out.mkdir(parents=True, exist_ok=True)
    tsv = search()
    (out / "ena_air_samples.tsv").write_text(tsv)
    accs = accessions(tsv, max(1, args.every))
    (out / "accessions.txt").write_text("\n".join(accs) + "\n")
    logger.info("ENA: %d air samples; wrote %d accessions to %s", len(tsv.splitlines()) - 1, len(accs), out / "accessions.txt")


if __name__ == "__main__":
    main()
