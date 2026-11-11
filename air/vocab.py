"""GOLD and EMPO vocabularies for the aerobiome KG. Sources: air/SOURCES.md."""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

import requests

logger = logging.getLogger("air.vocab")

GOLD_OBO_URL = "https://raw.githubusercontent.com/cmungall/gold-ontology/main/gold.obo"
GOLD_SSSOM_URL = "https://raw.githubusercontent.com/cmungall/gold-ontology/main/mappings/gold-to-envo.curated.sssom.tsv"
GOLD_VOCAB_IRI = "https://w3id.org/gold.vocab/"
ENVO_IRI = "http://purl.obolibrary.org/obo/ENVO_"

# EMPO levels 1-3 (https://qiita.ucsd.edu/static/doc/html/empo.html), each with the
# class from ENVO's EMPO subset (envoEmpo.owl) whose label names the same category.
# (empo_1, empo_2, empo_3, ENVO id or None)
EMPO = [
    ("Free-living", "Non-saline", "Water (non-saline)", "01000306"),       # freshwater environment
    ("Free-living", "Non-saline", "Sediment (non-saline)", "01001049"),    # non-saline sediment environment
    ("Free-living", "Non-saline", "Soil (non-saline)", "01001044"),        # soil environment
    ("Free-living", "Non-saline", "Surface (non-saline)", "01001051"),     # biofilm on a non-saline surface
    ("Free-living", "Non-saline", "Subsurface (non-saline)", "01001047"),  # non-saline planetary subsurface environment
    ("Free-living", "Non-saline", "Aerosol (non-saline)", "01001054"),     # non-saline aerosol environment
    ("Free-living", "Saline", "Water (saline)", "01000307"),               # saline water environment
    ("Free-living", "Saline", "Sediment (saline)", "01001050"),            # saline sediment environment
    ("Free-living", "Saline", "Hypersaline (saline)", "01001043"),         # hypersaline water environment
    ("Free-living", "Saline", "Surface (saline)", "01001056"),             # biofilm on a saline surface
    ("Free-living", "Saline", "Aerosol (saline)", "01001053"),             # saline aerosol environment
    ("Host-associated", "Animal-associated", "Animal distal gut", "01001029"),    # fecal environment
    ("Host-associated", "Animal-associated", "Animal proximal gut", "01001033"),  # digestive tract environment
    ("Host-associated", "Animal-associated", "Animal secretion", "01001030"),     # metazoan secretion
    ("Host-associated", "Animal-associated", "Animal surface", "01001034"),       # biofilm on an animal surface
    ("Host-associated", "Animal-associated", "Animal corpus", "01001055"),        # animal part or small animal
    ("Host-associated", "Fungus-associated", "Fungus corpus", "01001058"),        # fungal tissue
    ("Host-associated", "Fungus-associated", "Fungus surface", "01001035"),       # biofilm on a fungal surface
    ("Host-associated", "Plant-associated", "Plant secretion", "01001031"),       # plant substance
    ("Host-associated", "Plant-associated", "Plant surface", "01001032"),         # biofilm on a plant surface
    ("Host-associated", "Plant-associated", "Plant rhizosphere", "01000999"),     # rhizosphere environment
    ("Host-associated", "Plant-associated", "Plant corpus", "01001057"),          # plant part or small plant
    ("Control", "Negative", "Sterile water blank", "01001042"),   # sterile water environment
    ("Control", "Positive", "Mock community", "01001059"),        # mock community culture
    ("Control", "Positive", "Single strain", "01001060"),         # single strain cell culture
    ("Unknown", "Contradictory", "Unknown (contradictory)", None),
    ("Unknown", "Missing", "Unknown (missing)", None),
]
EMPO_UPPER = {"Non-saline": "01001003", "Saline": "01001040", "Animal-associated": "01001002",
              "Plant-associated": "01001001", "Fungus-associated": "01001041", "Host-associated": "01001000"}


def empo_terms() -> list[dict]:
    """Every EMPO class, levels 1-3, as {id, label, level, parent, envo}."""
    out, seen = [], set()
    for e1, e2, e3, envo in EMPO:
        for level, label, parent, eid in ((1, e1, None, EMPO_UPPER.get(e1)), (2, e2, e1, EMPO_UPPER.get(e2)),
                                          (3, e3, e2, envo)):
            if label in seen:
                continue
            seen.add(label)
            out.append({"id": "EMPO:" + label, "label": label, "level": level,
                        "parent": "EMPO:" + parent if parent else None,
                        "envo": f"ENVO:{eid}" if eid else None, "url": ENVO_IRI + eid if eid else None})
    return out


def download(url: str, dest: Path) -> Path:
    """``url`` saved to ``dest`` once; later runs reuse the file."""
    if not dest.exists():
        logger.info("Downloading %s", url)
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
    return dest


_SYNONYM = re.compile(r'^"(.*)"\s+(EXACT|BROAD|NARROW|RELATED)')


def parse_gold_obo(text: str) -> list[dict]:
    """GOLD ecosystem paths from gold.obo: {id, path, label (last step), exact_synonyms, ..., parents, mixs}."""
    terms, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            if cur and cur.get("id", "").startswith("GOLDTERMS:") and cur.get("path"):
                terms.append(cur)
            cur = {"exact_synonyms": [], "narrow_synonyms": [], "broad_synonyms": [], "related_synonyms": [],
                   "parents": [], "mixs": {}} if line == "[Term]" else None
            continue
        if cur is None or ": " not in line:
            continue
        tag, value = line.split(": ", 1)
        if tag == "id":
            cur["id"] = value
        elif tag == "name":
            cur["path"] = value
            cur["label"] = value.rsplit(" > ", 1)[-1]
        elif tag == "synonym":
            m = _SYNONYM.match(value)
            if m:
                cur[f"{m.group(2).lower()}_synonyms"].append(m.group(1))
        elif tag == "is_a":
            cur["parents"].append(value.split(" ! ")[0].strip())
        elif tag == "property_value" and value.startswith("mixs:env_"):
            prop, obj = (value.split() + [""])[:2]
            cur["mixs"][prop.removeprefix("mixs:")] = obj
    if cur and cur.get("id", "").startswith("GOLDTERMS:") and cur.get("path"):
        terms.append(cur)
    return terms


# SSSOM says GOLD <predicate> ENVO; we go from ENVO to GOLD, so broad and narrow swap.
_INVERSE = {"skos:exactMatch": "exactMatch", "skos:closeMatch": "closeMatch",
            "skos:broadMatch": "narrowMatch", "skos:narrowMatch": "broadMatch"}


def parse_gold_sssom(text: str) -> dict[str, list[dict]]:
    """ENVO CURIE -> the GOLD classes mapped to it, as seen from ENVO. differentFrom and blank rows are dropped."""
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    out: dict[str, list[dict]] = {}
    for row in csv.DictReader(lines, delimiter="\t"):
        match = _INVERSE.get((row.get("predicate_id") or "").strip())
        envo = (row.get("object_id") or "").strip()
        if not match or not envo.startswith("ENVO:"):
            continue
        out.setdefault(envo, []).append({"id": row["subject_id"].strip(), "label": (row.get("subject_label") or "").strip(),
                                         "match_type": match})
    return out


def load_gold(vocab_dir: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    obo = download(GOLD_OBO_URL, vocab_dir / "gold.obo")
    sssom = download(GOLD_SSSOM_URL, vocab_dir / "gold-to-envo.curated.sssom.tsv")
    return parse_gold_obo(obo.read_text(encoding="utf-8")), parse_gold_sssom(sssom.read_text(encoding="utf-8"))


def gold_url(gold_id: str) -> str | None:
    """Only GOLDVOCAB IRIs are known to resolve (w3id.org/gold.vocab/...)."""
    return GOLD_VOCAB_IRI + gold_id.split(":", 1)[1] if gold_id.startswith("GOLDVOCAB:") else None
