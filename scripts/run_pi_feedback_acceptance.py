#!/usr/bin/env python3
"""Live Pi model through real preparation, publication and fixture confirmation.

All account data, storage and matching side effects are isolated. Only the
configured model provider is contacted. No Candy record is read or written.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["AYUE_TEST_MODE"] = "on"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "social"))

import mongomock
from services.ayue_agent.contracts import PublicAgentRequestContext, PublicAgentTurnContext, ToolResult
from services.ayue_agent.pi import public_turn, tool_runtime
from services.ayue_agent.time_context import build_turn_clock
from services.ayue_agent.shared import write_executors
from services.ayue_agent.shared.confirmation import ConfirmationManager, project_match_choice_history


CASES = [
    ("surfing", "好幫我找一起衝浪的人", [], "衝浪"),
    ("surfing_skill", "幫我配對會衝浪的人，先給我看人選", [], "衝浪"),
    ("surfing_followup", "對，幫我找", [
        {"role": "user", "content": "我要找一起衝浪的人"},
        {"role": "assistant", "content": "可以依衝浪主題找人，想開始嗎？"},
    ], "衝浪"),
    ("recent_context", "用我的近期情境幫我開始配對", [], None),
    ("change_to_recent", "不要衝浪了，改用近期情境幫我配對", [
        {"role": "user", "content": "幫我找一起衝浪的人"},
    ], None),
]


def run_case(name, message, history, topic):
    store = mongomock.MongoClient().pi_acceptance
    owner, room = "fixture-pi-owner", "fixture-pi-room"
    request = PublicAgentRequestContext(user_id=owner, room_id=room, message=message,
                                       external_calendar_authorized=True)
    turn = PublicAgentTurnContext(user_id=owner, room_id=room, message=message,
                                  recent_messages=history, clock=build_turn_clock(message))
    started_searches = []
    attempted = []
    original_dispatch = tool_runtime.PiToolRuntime.dispatch
    def fixture_dispatch(runtime, tool, args):
        attempted.append(tool)
        if tool not in {"match.start_search", "match.get_status", "system.get_current_time",
                        "product.get_info", "profile.get_self_summary", "profile.get_recent_context"}:
            return runtime.project(name=tool, status="failed", error_code="fixture_tool_not_allowed")
        return original_dispatch(runtime, tool, args)

    def fixture_read(call, *_args, **_kwargs):
        if call.name == "match.get_status":
            return ToolResult(ok=True, data={"stage": "idle", "search": {"status": "idle"},
                                           "active_proposal_count": 0, "remaining_today": 3})
        return ToolResult(ok=True, data={"summary": "近期想找人一起運動", "current_context": "近期想找人一起運動"})

    def start_search(_owner, **kwargs):
        started_searches.append(kwargs)
        return {"status": "queued"}

    started = time.perf_counter()
    with patch.object(public_turn, "build_public_agent_turn_context", return_value=turn), \
         patch.object(public_turn, "validated_mentioned_contact_ids", return_value=([], False)), \
         patch.object(public_turn, "messages_coll", store.messages), \
         patch.object(tool_runtime.PiToolRuntime, "dispatch", fixture_dispatch), \
         patch.object(tool_runtime, "execute_tool", fixture_read), \
         patch.object(write_executors, "assess_match_opportunity", return_value=SimpleNamespace(state="ready")), \
         patch.object(write_executors, "TOOL_CALLS", store.tool_calls), \
         patch.object(write_executors, "start_match_search", side_effect=start_search):
        result = public_turn.run_pi_public_turn(
            request, confirmation_collection=store.confirmations, contact_selection_collection=store.selections,
            operation_batch_collection=store.batches, runs_collection=store.runs,
        )
        failure = result.fallback_reason
        assert not failure, failure
        assert result.choice_prompt, "no_real_confirmation"
        assert "物件" not in result.reply, "person_terminology"
        assert started_searches == [], "unconfirmed_search"
        record = store.confirmations.find_one({})
        assert (record["payload"].get("search_context") or {}).get("invitation_topic") == topic, "wrong_search_topic"
        assert "delivery_mode" not in record["payload"], "unrequested_invitation"
        stored = {"room_id": room, "content": result.reply, "metadata": {
            "choice_prompt": result.choice_prompt, "interaction_blocks_v1": result.interaction_blocks_v1,
        }}
        message_id = str(store.messages.insert_one(stored).inserted_id)
        manager = ConfirmationManager(store.confirmations)
        assert manager.mark_presented(user_id=owner, origin_run_id=result.agent_run_id, message_id=message_id,
                                      persisted_content=result.reply, interaction_blocks_v1=result.interaction_blocks_v1), "publication_failed"
        for _ in range(3):
            refreshed = project_match_choice_history([stored], user_id=owner, room_id=room, collection=store.confirmations)
            assert refreshed[0]["metadata"]["choice_prompt"]["display"] == result.choice_prompt["display"], "card_copy_lost"
        for _ in range(2):
            manager.execute_confirmed(user_id=owner, room_id=room, surface="public_ayue", choice_id=record["_id"],
                                      executor=lambda tool, args, uid, payload: write_executors.execute_write(
                                          tool, args, request, turn, "fixture-confirm", 0, payload=payload))
        assert len(started_searches) == 1, "duplicate_or_missing_submission"
        assert (started_searches[0].get("search_context") or {}).get("invitation_topic") == topic, "executor_lost_topic"
        trace = store.runs.find_one({}) or {}
        return {"case": name, "status": "passed", "run_id": result.agent_run_id,
                "duration_ms": round((time.perf_counter()-started)*1000), "tools": attempted,
                "model_calls": trace.get("llm_call_count"), "topic": topic,
                "publication": "activated", "refreshes": 3, "submissions": 1}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    rows = []
    for case in CASES:
        if args.case and case[0] not in args.case:
            continue
        try:
            row = run_case(*case)
        except Exception as exc:
            row = {"case": case[0], "status": "failed", "error_type": type(exc).__name__,
                   "code": str(exc)[:100] if isinstance(exc, AssertionError) else "acceptance_exception"}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    print(json.dumps({"passed": sum(row["status"] == "passed" for row in rows), "total": len(rows)}))
    return int(any(row["status"] != "passed" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
