"""Generator docstring honesty (audit A-10): the module long claimed
"40 templates" while ``TEMPLATES`` holds 4. The claim and the registry
must never drift apart again — the N-family number in the docstring is
parsed and compared against ``len(TEMPLATES)``, and the default-mix
instance claims (800 total / ~20% OOD) are pinned to what
``generate_benchmark`` actually produces.
"""
from __future__ import annotations

import re

from taskvm_bench.benchmark import generator


def test_docstring_family_claim_matches_registry():
    m = re.search(r"(\d+)-family", generator.__doc__ or "")
    assert m, "docstring must state the family count as 'N-family'"
    assert int(m.group(1)) == len(generator.TEMPLATES), (
        "docstring family claim drifted from the template registry")
    assert len(generator.TEMPLATES) == 4


def test_no_stale_template_count_claims():
    blob = (generator.__doc__ or "") + \
        (generator.generate_benchmark.__doc__ or "")
    assert "40 templates" not in blob, (
        "stale template-count claim (audit A-10): state the real "
        "family count, do not pad the registry to match the claim")


def test_default_mix_claims_are_real():
    split = generator.generate_benchmark(per_template=200)
    assert split.n_templates == 4
    assert split.total == 800
    assert split.ood_fraction == 0.2
