"""Frozen synthetic evidence for historical v1 truncation, not the v2 contract.

Keep the original assertions/labels against the frozen P0 implementation. The
new v2 contract has separate collision/fidelity tests; do not require current
runtime code to reproduce historical loss just to preserve this evidence.
No service imports, dotenv, provider calls or database access are needed.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

def canonical_module():
    """Test-only snapshot from main@62da19c, never a runtime fallback."""
    path = Path(__file__).resolve().parents[2] / "fixtures" / "concept_identity_p0.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "9a7616ea55fdc523274d730a417b905e88ad851b29ac0c689727974eebe279d7"
    )
    spec = importlib.util.spec_from_file_location("frozen_r32c_v1_identity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("text, lost", [
    ("Watching Waterfalls on Short Accessible Trails", "Trails"),
    ("Visiting Historic Gardens with Wheelchair Accessible Paths", "r Accessible Paths"),
    ("Walking around Historic Castles using Step-Free Routes", "ep-Free Routes"),
    ("Visiting Quiet Castles without Guided Groups", "oups"),
    ("Visiting Famous Castles Quietly with No Guided Tours", "Guided Tours"),
    ("Attending Community Dinner Parties with Vegetarian Food Only", "Vegetarian Food Only"),
    ("Attending Community Dinner Parties with Alcohol-Free Drinks Only", "Alcohol-Free Drinks Only"),
    ("Enjoying Weekend Football Matches by Watching Only", "ching Only"),
    ("Enjoying Weekend Football Matches by Playing Only", "ying Only"),
    ("Exploring Historical Mystery Novels through Reading", "ugh Reading"),
    ("Exploring Historical Mystery Novels through Writing", "ugh Writing"),
])
def test_current_long_qualifiers_are_truncated(text, lost):
    concept = canonical_module().canonicalize_concept(text)
    assert len(text) > 40
    assert concept.label == text[:40]
    assert text[40:] == lost
    assert len(concept.label) == 40


@pytest.mark.parametrize("left, right", [
    ("Attending Community Dinner Parties with Vegetarian Food Only",
     "Attending Community Dinner Parties with Alcohol-Free Drinks Only"),
    ("Enjoying Weekend Football Matches through Watching",
     "Enjoying Weekend Football Matches through Playing"),
    ("Exploring Historical Mystery Novels through Reading",
     "Exploring Historical Mystery Novels through Writing"),
])
def test_current_identity_collisions(left, right):
    identity = canonical_module()
    a, b = identity.canonicalize_concept(left), identity.canonicalize_concept(right)
    assert left != right
    assert left[:40] == right[:40]
    assert a.label == b.label
    assert a.key == b.key


@pytest.mark.parametrize("text", [
    "Accessible Trails", "Wheelchair Accessible Paths", "Step-Free Routes",
    "Quiet Castle Visits", "Castle Visits with No Guided Tours",
    "Vegetarian Food", "Alcohol-Free Drinks", "Watching Football",
    "Playing Football", "Reading Mystery Novels", "Writing Mystery Novels",
])
def test_short_qualifiers_remain_intact(text):
    assert len(text) <= 40
    assert canonical_module().canonicalize_concept(text).label == text


def test_frozen_development_input_loss():
    path = Path(__file__).with_name("r3_holdout.json")
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "0943c22f0f3aa4e9091644f811161ca8342fa00838359f2ded9239cf303b70e7"
    )
    groups = json.loads(raw)["groups"]
    identity = canonical_module()
    lost = {
        (group["id"], field): (group[field], identity.canonicalize_concept(group[field]).label)
        for group in groups for field in ("a", "b")
        if group[field] != identity.canonicalize_concept(group[field]).label
    }
    assert len(groups) == 48
    assert max(len(group[field]) for group in groups for field in ("a", "b")) == 46
    assert lost == {
        ("h11", "b"): ("Watching Waterfalls on Short Accessible Trails",
                       "Watching Waterfalls on Short Accessible "),
        ("h47", "b"): ("Visiting Quiet Castles without Guided Groups",
                       "Visiting Quiet Castles without Guided Gr"),
    }
    # Each group appears in both query/candidate directions: four affected cases.
    assert len(lost) * 2 == 4
