"""Offline tests for the ENA / microntology benchmark source: no network.

Run:  python -m pytest -q
"""

import pytest

from geoapp.schema import OntologyTerm
from validation.benchmark import Case, evaluate
from validation.ena_samples import _clean, pick_samples


@pytest.mark.parametrize("raw, expected", [
    ("soil [ENVO:00001998]", "soil"),
    ("feces (UBERON:0001988)", "feces"),
    ("ENVO_00002003", ""),
    ("marine biome|ENVO:00000447", "marine biome"),
    ("human gut", "human gut"),
])
def test_pasted_ontology_ids_are_stripped(raw, expected):
    assert _clean(raw) == expected


def test_pick_samples_spreads_across_studies(tmp_path):
    p = tmp_path / "a.tsv"
    p.write_text("ena_project_id\tstudy_name\tena_ers_sample_id\tsample_name\tmicrontology\n"
                 "P1\tA\tSAMEA3\tx\tMICRONT:1\n"
                 "P1\tA\tSAMEA1\tx\tMICRONT:1;MICRONT:2\n"
                 "P1\tA\tSAMEA2\tx\tMICRONT:1\n"
                 "P2\tB\tSAMN9\tx\tMICRONT:3\n"
                 "P3\tC\tERR123\tx\tMICRONT:4\n", encoding="utf-8")
    picked = pick_samples(p, per_study=2)
    assert picked == {"SAMEA1": {"MICRONT:1", "MICRONT:2"}, "SAMEA2": {"MICRONT:1"}, "SAMN9": {"MICRONT:3"}}


def _term(curie):
    return OntologyTerm(uri="", curie=curie, source="ENVO", match_type="exactMatch", confidence=1.0)


def test_a_hit_below_a_coarse_gold_term_counts_only_when_allowed():
    ancestors = {"ENVO:forest_soil": {"ENVO:soil", "ENVO:material"}}.get
    coarse = Case("ena", "forest soil", "envo", {"ENVO:soil"}, field="env_medium", match_descendants=True)
    strict = Case("t", "forest soil", "envo", {"ENVO:soil"})
    hits = lambda text, ont, branch, also: [_term("ENVO:forest_soil")]
    assert evaluate([coarse], hits, ancestors=lambda c: ancestors(c, set()))[0]["top1_correct"] == 1.0
    assert evaluate([strict], hits, ancestors=lambda c: ancestors(c, set()))[0]["top1_correct"] == 0.0


def test_a_case_keeps_its_own_field_for_branch_routing():
    seen = []
    case = Case("ena", "forest", "envo", {"ENVO:1"}, field="env_broad_scale")
    evaluate([case], lambda text, ont, branch, also: seen.append(branch) or [], field="env_medium")
    assert seen == ["biome"]
