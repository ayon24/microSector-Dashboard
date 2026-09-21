"""Run the whole nightly pipeline: fetch → validate → build → export.

    python -m pipeline.run_all
"""
from __future__ import annotations

import logging
import sys

from . import build_indices, export_json, fetch_prices, validate


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    fetch_prices.run(full="--full" in sys.argv)
    report = validate.run(check_bhavcopy="--no-bhavcopy" not in sys.argv)
    if report["errors"]:
        logging.error("Validation failed; not rebuilding. See data/validation_report.json")
        sys.exit(1)
    build_indices.run()
    export_json.run()


if __name__ == "__main__":
    main()
