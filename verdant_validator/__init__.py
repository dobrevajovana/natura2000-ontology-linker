"""VERDANT validator: check and review predicted ontology mappings from any source, in Python.

Any mapping data can come in (the Natura 2000 linker's output, PANGAEA or MFD
links, SSSOM, a spreadsheet) and goes through the same steps:

    from verdant_validator import Validator

    v = Validator("data_pangaea/mappings.json", reviews="reviews/pangaea.jsonl")
    v.check(online=True)                    # 1. automatic checks: term exists, not obsolete,
                                            #    labels agree, accepted name, no contradictions
    for f in v.findings(level="warning"):   # 2. what they found
        print(f["subject"], "->", f["object"], ":", f["message"])
    v.reject(key, reviewer="Jovana", comment="a synonym in GBIF")   # 3. expert decisions,
    v.pending()                                                    #    in code or on the page (v.serve())
    v.to_sssom("pangaea.reviewed.sssom.tsv")                       # 4. the result

The building blocks are importable on their own: ``load``, ``check``,
``ReviewStore``, ``export_csv``, ``export_sssom``, ``create_app``, ``serve``.
From the shell: ``python -m verdant_validator check|serve|export``.
"""

from verdant_validator.checks import check
from verdant_validator.export import export_csv, export_sssom
from verdant_validator.mappings import from_records, load, mapping_key
from verdant_validator.store import MATCH_TYPES, VERDICTS, ReviewError, ReviewStore
from verdant_validator.validator import Validator

__version__ = "0.1.0"


def create_app(*args, **kwargs):
    """The review web app (Flask); see ``verdant_validator.web.create_app``."""
    from verdant_validator.web import create_app as _create
    return _create(*args, **kwargs)


def serve(*args, **kwargs):
    """Start the review page; see ``verdant_validator.web.serve``."""
    from verdant_validator.web import serve as _serve
    return _serve(*args, **kwargs)


__all__ = ["Validator", "load", "from_records", "mapping_key", "check", "ReviewStore", "ReviewError", "VERDICTS",
           "MATCH_TYPES", "export_csv", "export_sssom", "create_app", "serve", "__version__"]
