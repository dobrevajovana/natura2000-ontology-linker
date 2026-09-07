"""Command-line entry point: accessions in, knowledge graph out.

    python -m geoapp.cli --accessions accessions.txt

Runs four stages, any of which can be selected with ``--steps``:

    1  fetch      BioSample records (NCBI E-utilities + EBI BioSamples)
    2  geo        normalise geolocation           <- the step everything hangs off
    3  ontology   link attributes to ENVO / NCBITaxon / Wikidata
    4  graph      build the knowledge graph and write JSON / GeoJSON / Turtle
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from . import config


log = logging.getLogger("geoapp.cli")


def _setup_logging(verbose: bool) -> None:
    config.ensure_dirs()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(config.RUN_LOG))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # These are chatty at DEBUG and tell us nothing we want.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _progress(stage: str):
    """Return a callback the worker modules use to report incremental progress."""
    state = {"last": 0.0}

    def report(done: int, total: int, detail: str = "") -> None:
        now = time.monotonic()
        if done >= total or now - state["last"] >= 2.0:
            state["last"] = now
            pct = (done / total * 100) if total else 100.0
            log.info("%s %d/%d (%.0f%%) %s", stage, done, total, pct, detail)

    return report


def run(
    accessions_file: Path,
    steps: set[int],
    limit: int | None = None,
    workers: int | None = None,
    use_cache: bool = True,
) -> int:
    from . import fetch, geo, graph, ontolink

    config.ensure_dirs()
    started = time.monotonic()

    # --- 1. fetch ----------------------------------------------------------
    if 1 in steps:
        accessions = fetch.load_accessions(accessions_file)
        if limit:
            accessions = accessions[:limit]
        if not accessions:
            log.error("no accessions found in %s", accessions_file)
            return 1
        log.info("step 1/4  fetching %d accessions", len(accessions))
        samples = fetch.fetch_all(
            accessions, workers=workers, progress=_progress("  fetched"), use_cache=use_cache
        )
        ok = [s for s in samples if not s.fetch_error]
        log.info("step 1/4  %d/%d resolved (%d failed)", len(ok), len(samples), len(samples) - len(ok))
    else:
        samples = graph.load_samples()
        log.info("step 1/4  skipped, loaded %d cached samples", len(samples))

    # --- 2. geolocation ----------------------------------------------------
    if 2 in steps:
        log.info("step 2/4  resolving geolocation")
        geo.apply_locations(samples)
        with_coords = sum(1 for s in samples if s.location.has_coords)
        by_precision: dict[str, int] = {}
        for s in samples:
            by_precision[s.location.precision] = by_precision.get(s.location.precision, 0) + 1
        log.info(
            "step 2/4  %d/%d samples placed on the map  (%s)",
            with_coords,
            len(samples),
            ", ".join(f"{k}={v}" for k, v in sorted(by_precision.items())),
        )
        if not with_coords and samples:
            log.warning("no sample carried usable coordinates - the map will be empty")

    # --- 3. ontology linking ----------------------------------------------
    if 3 in steps:
        if config.ENRICH_ONTOLOGY:
            log.info("step 3/4  linking attributes to %s", ", ".join(config.OLS4_ONTOLOGIES))
            ontolink.enrich(samples, use_cache=use_cache, progress=_progress("  linked"))
            n_terms = sum(len(s.ontology_terms) for s in samples)
            log.info("step 3/4  %d ontology terms across %d samples", n_terms, len(samples))
        else:
            log.info("step 3/4  skipped (GEO_ENRICH_ONTOLOGY=0)")

    # --- 4. graph ----------------------------------------------------------
    if 4 in steps:
        log.info("step 4/4  building knowledge graph")
        nodes, edges = graph.build_graph(samples)
        paths = graph.save_all(samples, nodes, edges)
        summary = graph.stats(samples, nodes, edges)
        log.info("step 4/4  %d nodes, %d edges", len(nodes), len(edges))
        for name, path in paths.items():
            log.info("          wrote %s -> %s", name, path)
        by_source = summary.get("by_ontology_source", {})
        log.info(
            "summary   %d samples, %d with coordinates, %d countries, %d ontology terms (%s)",
            summary.get("total_samples", 0),
            summary.get("samples_with_coordinates", 0),
            len(summary.get("by_country", {})),
            sum(by_source.values()),
            ", ".join(f"{k}={v}" for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])),
        )

    log.info("done in %.1fs  -  start the explorer with: python -m geoapp.app", time.monotonic() - started)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m geoapp.cli",
        description="Fetch BioSamples, geolocate them, link them to external ontologies "
                    "and archives, and build a knowledge graph.",
    )
    p.add_argument(
        "--accessions", type=Path, default=config.ACCESSIONS_FILE,
        help=f"file with one accession per line (default: {config.ACCESSIONS_FILE})",
    )
    p.add_argument(
        "--steps", default="1,2,3,4",
        help="comma-separated steps to run: 1=fetch 2=geo 3=ontology 4=graph",
    )
    p.add_argument("--limit", type=int, help="only process the first N accessions")
    p.add_argument("--workers", type=int, help=f"parallel fetches (default {config.FETCH_WORKERS})")
    p.add_argument("--no-cache", action="store_true", help="ignore the on-disk cache")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _setup_logging(args.verbose)

    try:
        steps = {int(s) for s in args.steps.split(",") if s.strip()}
    except ValueError:
        log.error("--steps must be comma-separated integers, got %r", args.steps)
        return 2
    if not steps <= {1, 2, 3, 4}:
        log.error("--steps accepts 1, 2, 3 and 4 only")
        return 2

    if 1 in steps and not args.accessions.exists():
        log.error("accessions file not found: %s", args.accessions)
        log.error("write one accession per line (SAMEA.../SAMN.../SRS...) and try again")
        return 2

    try:
        return run(
            args.accessions,
            steps,
            limit=args.limit,
            workers=args.workers,
            use_cache=not args.no_cache,
        )
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
