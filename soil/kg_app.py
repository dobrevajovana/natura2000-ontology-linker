"""The joined soil KG viewer (MFD + NMDC), served at /soil/ by the Natura 2000 viewer.

Run:  python -m soil.kg_app      (port 8096; reads <data>/soil/kg.json)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, send_from_directory

from soil import kg

logger = logging.getLogger("soil.kg_app")

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
MFD_DIR = Path(os.environ.get("MFD_DATA_DIR") or DATA_DIR)

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("kg.html")


@app.route("/data/kg.json")
def data():
    if not (DATA_DIR / "soil" / "kg.json").exists():
        return jsonify({"meta": {}, "nodes": [], "edges": [], "trees": {"mfd": [], "nmdc": []}})
    return send_from_directory(DATA_DIR / "soil", "kg.json", max_age=0)


def prepare(strict: bool = True) -> None:
    """Rebuild kg.json from whatever MFD and NMDC graphs exist, so a restart picks up new runs."""
    try:
        kg.build(DATA_DIR, MFD_DIR)
    except Exception:
        if strict:
            raise
        logger.exception("Could not build the soil KG; /soil/ will show it as empty")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    prepare(strict=False)
    app.run(host="0.0.0.0", port=int(os.environ.get("SOIL_PORT", 8096)), threaded=True)


if __name__ == "__main__":
    main()
