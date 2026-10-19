"""
Natura 2000 Ontology Linker — Pipeline Orchestrator

Usage:
    python -m pipeline.main [--dry-run] [--steps 1,2,3,4] [--data-dir /path/to/data]
"""

import argparse
import logging
import os
import sys
import time
import traceback

from dotenv import load_dotenv

# FAISS and PyTorch each load an OpenMP runtime; on macOS multi-threaded search then crashes
# without a traceback. Set before any of them is imported.
os.environ.setdefault("OMP_NUM_THREADS", "1")

# Load .env from the project root (one level up from pipeline/)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_project_root, ".env"))

PIPELINE_VERSION = "1.0.0"

STEP_NAMES = {
    1: "Scrape Natura 2000 Terms",
    2: "Build OWL Ontology",
    3: "Link to External Ontologies",
    4: "Vector Similarity Matching",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Natura 2000 Ontology Linker — Pipeline Orchestrator"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        default=False,
        help="Only process a small sample (uses DRY_RUN_SIZE from env, default 50).",
    )
    parser.add_argument(
        "--steps",
        default="1,2,3,4",
        help="Comma-separated list of steps to run (default: 1,2,3,4).",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("DATA_DIR", "./data"),
        help="Path to data directory (default: DATA_DIR env var or ./data).",
    )
    return parser.parse_args()


def setup_logging(data_dir: str) -> logging.Logger:
    os.makedirs(data_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(data_dir, "pipeline.log")),
        ],
    )
    for noisy in ("httpx", "urllib3", "huggingface_hub", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(__name__)


def print_banner(dry_run: bool) -> None:
    run_mode = "DRY-RUN (sample)" if dry_run else "FULL"
    print("=" * 60)
    print(f"  Natura 2000 Ontology Linker — Pipeline v{PIPELINE_VERSION}")
    print(f"  Run mode: {run_mode}")
    print("=" * 60)


def main() -> None:
    args = parse_args()
    data_dir: str = args.data_dir
    dry_run: bool = args.dry_run

    try:
        requested_steps = [int(s.strip()) for s in args.steps.split(",")]
    except ValueError:
        print(f"ERROR: --steps must be comma-separated integers, got: {args.steps!r}")
        sys.exit(1)

    invalid = [s for s in requested_steps if s not in STEP_NAMES]
    if invalid:
        print(f"ERROR: Unknown step(s): {invalid}. Valid steps are 1, 2, 3, 4.")
        sys.exit(1)

    os.makedirs(data_dir, exist_ok=True)
    logger = setup_logging(data_dir)
    print_banner(dry_run)

    # Import pipeline modules after env is loaded
    from pipeline import scraper, ontology, linker, vector_match

    pipeline_start = time.time()
    step_results: dict[int, bool] = {}

    for step_num in sorted(requested_steps):
        step_name = STEP_NAMES[step_num]
        logger.info("=== Starting Step %d: %s ===", step_num, step_name)
        step_start = time.time()
        try:
            if step_num == 1:
                scraper.run_scraper(data_dir, dry_run)
            elif step_num == 2:
                ontology.run_ontology_builder(data_dir)
            elif step_num == 3:
                linker.run_linker(data_dir, dry_run)
            elif step_num == 4:
                vector_match.run_vector_matching(data_dir, dry_run)
            elapsed = time.time() - step_start
            logger.info("=== Step %d complete in %.1fs ===", step_num, elapsed)
            step_results[step_num] = True
        except Exception:
            elapsed = time.time() - step_start
            logger.error(
                "=== Step %d (%s) FAILED after %.1fs ===",
                step_num, step_name, elapsed,
            )
            logger.error(traceback.format_exc())
            step_results[step_num] = False

    total_elapsed = time.time() - pipeline_start

    print()
    print("=" * 60)
    print("  Pipeline Summary")
    print("=" * 60)
    any_failed = False
    for step_num in sorted(step_results):
        status = "OK" if step_results[step_num] else "FAILED"
        print(f"  Step {step_num} ({STEP_NAMES[step_num]}): {status}")
        if not step_results[step_num]:
            any_failed = True
    print(f"  Total runtime: {total_elapsed:.1f}s")
    print("=" * 60)

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
