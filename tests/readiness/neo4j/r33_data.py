"""R3.3 synthetic input/novelty freeze. Pure local checks, no provider/Graph."""
from __future__ import annotations
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from matchmaker_agent.concept_identity import canonicalize_concept, canonicalize_fresh_concept, stored_concept_identity
from r32c_relation import RELATION_TO_PRIMARY
GATE_COMMIT = "3ee4d91395c2f4648f02bd107f016f33651bf819"


def r33_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def r33_gate():
    for name in ("r33_gate.json", "R33_ACCEPTANCE_GATE.md"):
        frozen = subprocess.check_output(["git", "show", GATE_COMMIT + ":tests/readiness/neo4j/" + name], cwd=ROOT)
        if (HERE / name).read_bytes() != frozen:
            raise ValueError("prospective_gate_changed")
    gate = json.loads((HERE / "r33_gate.json").read_text())
    if gate["relation_to_primary"] != RELATION_TO_PRIMARY:
        raise ValueError("mapping_changed")
    for path, digest in gate["frozen_sources"].items():
        if r33_digest(ROOT / path) != digest:
            raise ValueError("frozen_source_changed")
    return gate


def r33_inputs():
    gate = r33_gate()
    fixture = json.loads((HERE / "r33_holdout.json").read_text())
    if fixture["data_classification"] != "synthetic_non_user_data" or fixture["gate_commit"] != GATE_COMMIT:
        raise ValueError("unapproved_fixture_source")
    if len(fixture["groups"]) != 96:
        raise ValueError("holdout_size_changed")
    cases, audit, seen = [], [], set()
    old = json.loads((HERE / "r3_holdout.json").read_text())["groups"]
    calibration = json.loads((HERE / "fixtures.json").read_text())["semantic_pairs"]
    old_pairs = [(g["a"], g["b"]) for g in old] + [(g["left"], g["right"]) for g in calibration]
    previous = set()
    for canonicalizer in (canonicalize_concept, canonicalize_fresh_concept):
        for a, b in old_pairs:
            ka, kb = canonicalizer(a).key, canonicalizer(b).key
            previous.update(((ka, kb), (kb, ka)))
    prefix_groups = []
    for group in fixture["groups"]:
        a, b = group["a"], group["b"]
        raw = [canonicalize_concept(text) for text in (a, b)]
        identities = [canonicalize_fresh_concept(text) for text in (a, b)]
        if any(identity is None for identity in raw + identities):
            raise ValueError("missing_identity")
        if identities[0].key == identities[1].key:
            raise ValueError("deterministic_identity_pair_not_semantic")
        for identities_to_check in (raw, identities):
            pair = tuple(i.key for i in identities_to_check)
            if pair in previous or tuple(reversed(pair)) in previous:
                raise ValueError("holdout_overlaps_old_canonical_pair")
        pair = tuple(i.key for i in identities)
        if pair in seen or tuple(reversed(pair)) in seen:
            raise ValueError("duplicate_holdout_pair")
        seen.add(pair)
        if group["identical_prefix40_required"]:
            left, right = [i.semantic_text for i in identities]
            if len(left) <= 40 or len(right) <= 40 or left[:40] != right[:40] or left == right:
                raise ValueError("missing_long_prefix_challenge:" + group["id"])
            prefix_groups.append(group["id"])
        for text, identity in zip((a, b), identities):
            if stored_concept_identity(identity.as_dict()) != identity:
                raise ValueError("persisted_full_input_mismatch")
            audit.append({"group": group["id"], "source": text, **identity.as_dict()})
        for direction, source_q, source_c, q, c, languages in (
            ("f", a, b, identities[0], identities[1], group["languages"]),
            ("r", b, a, identities[1], identities[0], list(reversed(group["languages"]))),
        ):
            gold = group[direction]
            if RELATION_TO_PRIMARY.get(gold["relation"]) != gold["primary"] or not gold["rationale"]:
                raise ValueError("inconsistent_prescoring_label")
            cases.append({"id": group["id"] + "-" + direction, "group": group["id"],
                "Q": q.semantic_text, "C": c.semantic_text, "source_Q": source_q, "source_C": source_c,
                "query_key": q.key, "candidate_key": c.key, "category": group["category"],
                "language": "-".join(languages), "relation": gold["relation"], "expected": gold["primary"],
                "label_rationale": gold["rationale"], "prefix40": group["identical_prefix40_required"]})
    if (len(cases) != gate["configuration"]["case_count"] or len({c["id"] for c in cases}) != len(cases)
            or Counter(c["language"] for c in cases) != dict.fromkeys(("en-en","zh-zh","en-zh","zh-en"),48)
            or len(prefix_groups) < 16):
        raise ValueError("frozen_coverage_plan_not_met")
    return cases, {"novelty_canonical_pair_overlap": 0, "identical_prefix40_groups": prefix_groups,
        "languages": dict(sorted(Counter(c["language"] for c in cases).items())),
        "categories": dict(sorted(Counter(c["category"] for c in cases).items())),
        "primary_labels": dict(Counter(c["expected"] for c in cases)),
        "source_max_codepoints": max(len(row["source"]) for row in audit),
        "semantic_max_codepoints": max(len(row["semantic_text"]) for row in audit),
        "normalization_changed_sources": sum(row["source"] != row["semantic_text"] for row in audit),
        "identity_audit": audit}


def r33_schedule(cases):
    config = r33_gate()["configuration"]
    jobs = []
    for repeat, seed in enumerate(config["seeds"], 1):
        ordered = list(cases)
        random.Random(seed).shuffle(ordered)
        for offset in range(0, len(ordered), 2):
            jobs.append({"job_id": f"r33-r{repeat}-b{offset//2+1:03}", "repeat":repeat,
                         "seed":seed, "control":"default", "batch_size":2, "cases":ordered[offset:offset+2]})
    random.Random(config["schedule_seed"]).shuffle(jobs)
    return jobs


def r33_snapshot():
    gate = r33_gate()
    cases, audit = r33_inputs()
    jobs = r33_schedule(cases)
    files = ("r33_holdout.json", "r33_gate.json", "R33_ACCEPTANCE_GATE.md",
             "r33_data.py", "r33_metrics.py", "run_r33_offline.py", "test_r33_offline.py")
    hashes = {"tests/readiness/neo4j/" + name: r33_digest(HERE/name) for name in files}
    hashes.update(gate["frozen_sources"])
    return {"experiment":"R3.3","gate_commit":GATE_COMMIT,"hashes":hashes,
        "expanded_inputs_sha256":hashlib.sha256(json.dumps(cases,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        "schedule_sha256":hashlib.sha256(json.dumps(jobs,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        "configuration":gate["configuration"],"runtime_flags":gate["runtime_flags"],
        "fixture_label_provenance":gate["label_provenance"],"coverage":{k:v for k,v in audit.items() if k!="identity_audit"},
        "no_production_graph":True,"production_ready":False}, cases, jobs, audit
