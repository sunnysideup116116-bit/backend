"""Pure v2 identity regression and frozen P0 normal-form equivalence checks."""
import hashlib
import ast
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from matchmaker_agent.concept_identity import (
    MAX_PREFERENCE_TEXT_CHARS, PreferenceTextError, canonicalize_concept,
    canonicalize_concept_v1, canonical_query_provenance, display_preference_label,
    normalize_preference_text, stored_concept_identity, verified_legacy_identity,
    split_compound_concept_label, split_explicit_preference_enumeration,
)

HERE = Path(__file__).parent
PAIRS = json.loads((HERE / "fixtures/preference_identity_collision_pairs.json").read_text())["pairs"]


@pytest.mark.parametrize("pair", PAIRS, ids=lambda p: p["id"])
def test_v2_collision_matrix(pair):
    left, right = (canonicalize_concept(pair[side]) for side in ("left", "right"))
    if pair["kind"] == "deterministic_alias":
        assert left.key == right.key
        assert left.semantic_text == right.semantic_text
    else:
        assert left.key != right.key
        assert left.semantic_text == pair["left"]
        assert right.semantic_text == pair["right"]
    if pair["kind"] == "prefix_collision":
        assert canonicalize_concept_v1(pair["left"]).key == canonicalize_concept_v1(pair["right"]).key
        assert left.display_label == right.display_label  # UI equality is not identity.
    for identity in (left, right):
        assert len(identity.key) == 51
        assert re.fullmatch(r"[a-z][a-z0-9_]{1,50}", identity.key)
        assert identity.canonicalization_version == "v2"
        assert len(identity.display_label) <= 40
        assert stored_concept_identity(identity.as_dict()) == identity


@pytest.mark.parametrize("variants", [
    ["Kpop", "K-pop", "K pop", "k-pop"],
    ["Board Games", "board-games", "board games"],
    ["Science Fiction", "science-fiction", "science fiction"],
    ["Coffee Shop", "coffee-shop", "coffee shop"],
])
def test_full_normal_form_is_deterministic(variants):
    keys = {canonicalize_concept(v, "untrusted_model_key").key for v in variants}
    assert len(keys) == 1
    assert canonical_query_provenance("Kpop") == "deterministic_alias"
    assert canonical_query_provenance("K-pop") == "exact_canonical"


def test_frozen_p0_short_equivalence_without_importing_live_v1_implementation():
    path = HERE / "fixtures/concept_identity_p0.py"
    spec = importlib.util.spec_from_file_location("frozen_p0_identity", path)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    labels = ["Kpop", "K-pop", "J pop", "J-pop", "Board Games", "board-games",
              "Science Fiction", "science-fiction", "Coffee Shop", "coffee shop",
              "安靜咖啡廳", "抽菸", "潛水", "韓國流行音樂", "韓流音樂"]
    for left in labels:
        assert baseline.canonicalize_concept(left).key == canonicalize_concept_v1(left).key
        for right in labels:
            assert (baseline.canonicalize_concept(left).key == baseline.canonicalize_concept(right).key) == (
                canonicalize_concept(left).key == canonicalize_concept(right).key)


def test_full_digest_input_not_prefix_and_meaningful_punctuation_preserved():
    text = "A" * 500
    identity = canonicalize_concept(text)
    expected = hashlib.sha256(("preference:v2\0" + text.casefold()).encode()).hexdigest()[:48]
    assert identity.key == "v2_" + expected
    assert identity.semantic_text == text
    assert canonicalize_concept("C++ Programming").key != canonicalize_concept("C Programming").key
    assert canonicalize_concept("韓流音樂").key != canonicalize_concept("K-pop").key


@pytest.mark.parametrize("value", ["x" * 501, "\ufb03" * 167, "x\ud800", "a\x00b", 42])
def test_invalid_complete_input_fails_closed(value):
    with pytest.raises(PreferenceTextError):
        canonicalize_concept(value)


def test_mixed_polarity_rejected_before_prefix_removal():
    with pytest.raises(PreferenceTextError, match="mixed_preference_polarity"):
        canonicalize_concept("我喜歡 K-pop，但不喜歡吵鬧的音樂")
    assert canonicalize_concept("我不喜歡吵鬧的酒吧").semantic_text == "吵鬧的酒吧"


def test_explicit_enumeration_over_limit_is_not_an_unsplit_concept():
    items = "A,B,C,D,E,F,G,H,I"
    with pytest.raises(PreferenceTextError, match="memory_limit_exceeded"):
        split_compound_concept_label(items)
    with pytest.raises(PreferenceTextError, match="memory_limit_exceeded"):
        split_explicit_preference_enumeration("我喜歡 " + items)
    assert split_compound_concept_label("適合讀書、安靜又不吵的咖啡廳") == []


def test_display_label_cannot_change_stored_identity():
    concept = canonicalize_concept(PAIRS[0]["left"])
    record = concept.as_dict()
    record["display_label"] = "not the semantic source"
    record["label"] = "legacy presentation field"
    assert stored_concept_identity(record) == concept
    assert display_preference_label(concept.semantic_text, 12) != concept.semantic_text
    assert canonicalize_concept(concept.semantic_text).key == concept.key


@pytest.mark.parametrize("field,value", [("key", "wrong"), ("canonical_key", "wrong"),
    ("semantic_input_hash", "wrong"), ("semantic_text", "Different Preference"),
    ("canonicalization_version", "v1"), ("fidelity_status", "unknown")])
def test_bad_stored_metadata_is_not_promoted(field, value):
    record = canonicalize_concept("K-pop").as_dict()
    record[field] = value
    assert stored_concept_identity(record) is None


def test_legacy_prefix_is_not_proof_but_owner_grounded_full_assertion_is():
    source = PAIRS[0]["left"]
    old = canonicalize_concept_v1(source)
    new = canonicalize_concept(source)
    legacy = {"key": old.key, "label": old.label, "canonicalization_version": "legacy_unknown"}
    assert stored_concept_identity(legacy) is None
    assert verified_legacy_identity(legacy) is None
    proven = {**legacy, "legacy_evidence_scope": "owner_assertion", "legacy_fidelity_status": "complete",
              "legacy_semantic_text": source, "legacy_semantic_input_hash": new.semantic_input_hash}
    assert verified_legacy_identity(proven) == new
    assert verified_legacy_identity(proven).key != canonicalize_concept(PAIRS[0]["right"]).key
    assert verified_legacy_identity({**proven, "legacy_evidence_scope": "concept_label"}) is None
    assert verified_legacy_identity({**proven, "legacy_semantic_input_hash": "wrong"}) is None
    assert verified_legacy_identity({**proven, "canonicalization_version": "v9"}) is None


def test_r3_complete_validator_input():
    """Real frozen R3 payload expression, full Q/C; no model/scoring or Graph."""
    fixture = json.loads((HERE / "readiness/neo4j/r3_holdout.json").read_text())
    batch = []
    for pair in fixture["groups"]:
        for suffix, query, candidate in (("f", pair["a"], pair["b"]), ("r", pair["b"], pair["a"])):
            batch.append({"id": pair["id"] + "-" + suffix,
                          "query_label": canonicalize_concept(query).semantic_text,
                          "candidate_label": canonicalize_concept(candidate).semantic_text,
                          "original_q": query, "original_c": candidate})
    tree = ast.parse((HERE / "readiness/neo4j/run_r3_offline.py").read_text())
    run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_validator")
    payload = next(node.value for node in ast.walk(run) if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "payload" for target in node.targets))
    expected = ast.parse("[{'id': c['id'], 'Q': c['query_label'], 'C': c['candidate_label']} for c in batch]", mode="eval").body
    assert ast.dump(payload) == ast.dump(expected)
    actual = eval(compile(ast.Expression(payload), "frozen_r3_payload", "eval"), {"__builtins__": {}, "batch": batch})
    assert len(actual) == 96
    assert all(row["Q"] == case["original_q"] and row["C"] == case["original_c"]
               for row, case in zip(actual, batch))
    assert any(len(row["Q"]) > 40 for row in actual)


def test_service_directory_import_uses_central_contract():
    # Match start_all.sh's service cwd; do not let the test runner's root
    # sys.path hide a package import failure. No app startup or provider client.
    script = (
        "import os,socket;os.environ['AYUE_SKIP_DOTENV']='1';"
        "socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
        "import matchmaker;"
        "c=matchmaker.safe_search_context({'search_intent':'preference','normalized_topic':'Kpop'});"
        "assert c['canonicalization_version']=='v2';assert len(c['canonical_preference_key'])==51;"
        "assert c['semantic_text']=='K-pop';print('service-import-ok')"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=HERE.parent / "matchmaker_agent",
                            capture_output=True, text=True, timeout=20, check=True)
    assert result.stdout.strip() == "service-import-ok"


@pytest.mark.parametrize("source,expected_key", [
    ("Kpop", "v2_60a8688e4f76ca245955a6e9ed65f99eedfa0600e80fd93d"),
    ("K-pop", "v2_60a8688e4f76ca245955a6e9ed65f99eedfa0600e80fd93d"),
    (" K POP ", "v2_60a8688e4f76ca245955a6e9ed65f99eedfa0600e80fd93d"),
    ("Ｋ－ｐｏｐ", "v2_60a8688e4f76ca245955a6e9ed65f99eedfa0600e80fd93d"),
    ("Board_Games", "v2_f2cbbe82b4aeb5c74fab0539959dbb2303f9d0245d1f6c4f"),
    (" board-\tgames ", "v2_f2cbbe82b4aeb5c74fab0539959dbb2303f9d0245d1f6c4f"),
    ("Science Fiction", "v2_cfabb6eba187660ba2a396ab11f10f588d44c398cd9d6f8d"),
    ("coffee shop", "v2_d0db5d9e41985017e48a30cded54723465e1020b5ae6473f"),
    ("我喜歡 安靜的咖啡廳", "v2_d06b6e6a174d7b035bc732b75b5974b236d97ab50c5b8861"),
    (" Café　Reading ", "v2_9c506c14280ad11835ee6c94369c9d57567dd5968a0133c6"),
    ("Cafe\u0301 Reading", "v2_9c506c14280ad11835ee6c94369c9d57567dd5968a0133c6"),
    ("C++ Programming", "v2_6c62362a2f54ca13491b4c1475a4ebffec57d8448c15594d"),
    ("C Programming", "v2_64eb78251b54b66e24806aadda7f699055187a224ac1b879"),
    ("步道散步（需無階梯）", "v2_7aecd82a0527721fe0058f3b69c716cb1e2d58964a444219"),
    ("步道散步（可爬階梯）", "v2_d981db9e6a79ea5ea17d830525ed0b18820a280dbedadecb"),
    ("Watching   live music — alcohol-free", "v2_a3d2d3a2b43520c3d7cbb015fe0b4282b7e0b365ba5ee89a"),
    ("Reading mystery novels, not writing", "v2_2ed07b941f9c98a5171dae64f1ae8f1446c47bebac450811"),
    ("写真を撮る／静かな場所", "v2_5e815fbb3552ac3a23eb0f28ca4f9a5b37cc5bc6ce9d2f59"),
])
def test_v2_canonical_key_golden_vectors(source, expected_key):
    # Literal golden keys are independently computed from reviewed normal forms,
    # not derived with the live normalizer/hash helper inside this test.
    identity = canonicalize_concept(source, "ignored_model_key")
    assert identity.key == expected_key
    assert canonicalize_concept(identity.semantic_text).key == expected_key
