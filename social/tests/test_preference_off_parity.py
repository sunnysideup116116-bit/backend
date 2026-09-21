"""Differential review against the frozen P0 source, with all I/O substituted.

The baseline object must exist locally (fetch P0 history in shallow CI).
No services, credentials or baseline checkout are used by this comparison.
"""
import ast
from copy import deepcopy
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pymongo.errors import DuplicateKeyError

from routers import match as router
from services.ayue_agent.shared import write_executors
from tests.test_preference_match_search import _flow, _preference_context

P0 = "7f130f60bb895020e67486c7f6467a921a2ec396"
ROOT = Path(__file__).resolve().parents[2]


def baseline_symbols(path, names, namespace):
    try:
        source = subprocess.check_output(
            ["git", "show", f"{P0}:{path}"], cwd=ROOT, text=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError:
        pytest.skip("P0 parity requires the frozen merge object in local git history")
    tree = ast.parse(source)
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in names]
    assert {node.name for node in selected} == set(names)
    env = dict(namespace)
    exec(compile(ast.Module(body=selected, type_ignores=[]), path, "exec"), env)
    return env


@pytest.mark.parametrize("case", [
    "ordered_pool", "all_conflict", "no_hits", "quota", "pair_race",
    "cancel", "late_profile", "other_ground",
])
def test_off_matches_frozen_p0_candidate_input_and_proposal_contract(monkeypatch, case):
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "off")
    monkeypatch.setattr(router.time, "time", lambda: 1_800_000_000.0)
    monkeypatch.setattr(router.time, "monotonic", lambda: 100.0)
    context = _preference_context()

    def run(use_baseline):
        with monkeypatch.context() as patch:
            # Deliberately non-alphabetical Graph order, >20 rows, exclusion
            # and conflict sentinels prove which candidates reach ranking.
            ids = ["z-first", "blocked", "historical", "conflict", "other-ground"]
            ids += [f"person-{n:02}" for n in range(24, 0, -1)]
            if case == "no_hits":
                ids = []
            profiles, matches = _flow(patch, candidate={"user_id": "z-first"})
            target = {"user_id": "owner", "current_context": "", "current_context_revision": 1,
                      "deep_profile": {"values": ["honesty"]}}
            candidates = {uid: {"user_id": uid, "current_context": "", "deep_profile": {}}
                          for uid in ids}
            if case == "other_ground":
                candidates["other-ground"]["deep_profile"] = {"values": ["honesty"]}
            def find_one(query, projection=None):
                uid = query.get("user_id")
                row = deepcopy(target if uid == "owner" else candidates.get(uid, {}))
                if case == "late_profile" and uid == "other-ground" and projection == {"deep_profile": 1, "_id": 0}:
                    row["deep_profile"] = {"values": ["honesty"]}
                return row
            profiles.find_one.side_effect = find_one
            profiles.find.return_value = list(candidates.values())
            matches.find.return_value = [{
                "from_user": "owner", "to_user": "historical", "status": "pending", "created_at": 1,
            }]
            patch.setattr(router.risk_block_service, "excluded_user_ids", lambda _u: {"blocked"})
            def stances(uid):
                if uid == "owner":
                    return {"smoking": {"avoid"}}
                if case == "all_conflict" or uid == "conflict":
                    return {"k_pop": {"like"}, "smoking": {"like"}}
                return {} if uid == "other-ground" else {"k_pop": {"like"}}
            patch.setattr(router, "_trait_stances", stances)
            patch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
                "canonical_key": "k_pop", "candidate_ids": ids,
                "candidate_count_before_filter": len(ids), "candidate_count_after_filter": len(ids),
            })
            patch.setattr(router, "retrieve_semantic_preference_candidates", Mock(side_effect=AssertionError("OFF")))
            reserve, release = Mock(return_value={"status": "reserved"}), Mock()
            if case == "quota":
                reserve.return_value = {"status": "exhausted"}
            patch.setattr(router, "reserve_daily_quota", reserve)
            patch.setattr(router, "release_daily_quota", release)
            select = Mock(side_effect=lambda payload, **_: [{"matched_user_id": payload["candidates"][0]["user_id"]}])
            patch.setattr(router, "_request_matchmaker_selection", select)
            if case == "pair_race":
                matches.insert_one.side_effect = DuplicateKeyError("one_live_proposal_per_pair")
            env = baseline_symbols("social/routers/match.py", [
                "generate_matches_for_user", "candidate_qualification",
            ], router.__dict__) if use_baseline else router.__dict__
            qualified = []
            original_qualification = env["candidate_qualification"]
            def qualify(*args, **kwargs):
                result = original_qualification(*args, **kwargs)
                if result["eligible"]:
                    qualified.append(args[1]["user_id"])
                return result
            if use_baseline:
                env["candidate_qualification"] = qualify
            else:
                patch.setattr(router, "candidate_qualification", qualify)
            result = env["generate_matches_for_user"](
                "owner", source="manual", search_job_id="parity-job",
                search_context=context, report_progress=lambda _s: True,
                can_commit=lambda: case != "cancel",
            )
            # Internal diagnostics are the one allowed additive observation.
            result.pop("diagnostics", None)
            documents = [deepcopy(c.args[0]) for c in matches.insert_one.call_args_list]
            for document in documents:
                document.pop("preference_retrieval_evidence", None)
            return {
                "qualified": qualified,
                "matchmaker_payloads": [c.args[0] for c in select.call_args_list],
                "documents": documents, "result": result,
                "quota_reserve": reserve.call_args_list, "quota_release": release.call_args_list,
            }

    baseline, current = run(True), run(False)
    assert current == baseline


def test_off_confirmation_matches_p0_preview_and_payload(monkeypatch):
    from tests.test_pi_feedback_regressions import make_turn
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "off")
    monkeypatch.setattr(write_executors, "assess_match_opportunity", lambda *_a, **_k: SimpleNamespace(state="ready"))
    _ctx, turn = make_turn("幫我找喜歡 Kpop 的人")
    ctx = turn._raw_ctx
    args = {"search_request": {"kind": "preference", "topic": "K-pop"}}
    baseline = baseline_symbols("social/services/ayue_agent/shared/write_executors.py", [
        "prepare_write_confirmation",
    ], write_executors.__dict__)["prepare_write_confirmation"]
    assert write_executors.prepare_write_confirmation("match.start_search", args, ctx, turn) == baseline(
        "match.start_search", args, ctx, turn,
    )
