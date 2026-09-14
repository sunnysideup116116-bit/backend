#!/usr/bin/env python3
"""Read-only live-model smoke checks for every migrated Candy Pi domain.

The configured model selects tools through the real Pi Agent loop. Tool calls
are schema-validated and answered with synthetic observations; no account,
database, calendar, match, contact, assessment, Web, or Places service is read
or mutated.
"""
from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]
SOCIAL_ROOT = SERVER_ROOT / "social"
sys.path.insert(0, str(SOCIAL_ROOT))

from config import load_gpt_settings  # noqa: E402
from services.ai_service import generate_chat_completion_with_tools  # noqa: E402
from services.ayue_agent.pi.runtime import (  # noqa: E402
    INITIAL_TOOLS, POLICY, initial_tools_for_message,
    provider_messages, run_bridge, tool_schemas,
)
from services.ayue_agent.tool_registry import get_tool_spec, planner_arguments_allowed  # noqa: E402
from services.ayue_agent.pi.places_tools import _normalize_nearby  # noqa: E402
from services.ayue_agent.pi.reply import validate_pi_reply_result, verified_observation_fallback  # noqa: E402


CASES = [
    ("relationship_list", "我目前有哪些已建立聯絡的朋友？", "relationship.list_accepted_contacts"),
    ("relationship_invite", "幫我約小安出去", "relationship.start_date_coordination"),
    ("product_info", "這個 App 的約會邀請卡怎麼用？", "product.get_info"),
    ("profile", "你目前了解我多少？", "profile.get_self_summary"),
    ("memory", "你記得我喜歡哪類咖啡店嗎？", "memory.search_my_profile"),
    ("places", "推薦三間高雄的咖啡店", "places.search_nearby"),
    ("web", "查一下高雄這週末最新的公開活動", "web.search"),
    ("match_status", "我現在的配對進度如何？", "match.get_status"),
    ("match_start", "現在開始幫我找新的對象", "match.start_search"),
    ("assessment", "我想重新做基本性格探索", "profile.start_assessment"),
    ("workflow", "先幫我約小安，然後新增明天下午三點的行程", "workflow.queue_operations"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", dest="case_names", default=[])
    args = parser.parse_args()
    load_gpt_settings()
    schemas = tool_schemas()
    by_name = {schema["name"]: schema for schema in schemas}
    output = []
    selected = [case for case in CASES if not args.case_names or case[0] in set(args.case_names)]
    for case_name, message, expected in selected:
        actual: list[str] = []
        model_tool_fields: list[dict] = []
        observations: list[dict] = []
        error = ""
        result: dict = {}
        fallback_reply = ""
        initial_names = initial_tools_for_message(message)

        def model_call(messages, deadline, active_tool_names=None):
            allowed = set(initial_names if active_tool_names is None else active_tool_names)
            completion = generate_chat_completion_with_tools(
                "",
                [
                    {"type": "function", "function": schema}
                    for schema in schemas if schema["name"] in allowed
                ],
                system_prompt=POLICY,
                temperature=0,
                max_tokens=1024,
                deadline_monotonic=deadline,
                model_owner="pi",
                conversation_messages=provider_messages(messages),
            )
            for call in completion.tool_calls:
                arguments = call.get("arguments") or {}
                fields = []
                if isinstance(arguments, dict):
                    for key, value in arguments.items():
                        fields.append(str(key))
                        if isinstance(value, list) and value and isinstance(value[0], dict):
                            fields.extend(f"{key}[].{child}" for child in value[0])
                model_tool_fields.append({"tool": str(call.get("name") or ""), "fields": fields[:16]})
            return {
                "content": completion.content,
                "tool_calls": completion.tool_calls,
                "input_tokens": completion.input_tokens,
                "output_tokens": completion.output_tokens,
            }

        def tool_call(name, arguments):
            nonlocal error
            actual.append(name)
            if name == "product.get_info":
                valid = isinstance(arguments.get("query"), str) and bool(arguments["query"].strip())
            elif name == "workflow.queue_operations":
                operations = arguments.get("operations")
                valid = isinstance(operations, list) and 2 <= len(operations) <= 4
            else:
                spec = get_tool_spec(name)
                canonical = (
                    _normalize_nearby(arguments)
                    if name == "places.search_nearby" else arguments
                )
                valid = bool(spec and planner_arguments_allowed(spec, canonical))
            if not valid:
                error = "tool_schema_invalid"
                return {"observations": [{"status": "failed", "code": error}]}
            synthetic = {"synthetic": True}
            if name == "relationship.list_accepted_contacts":
                synthetic = {
                    "contacts": [{"contact_ref": "contact_1", "display_name": "小安", "safe_summary": "已建立聯絡"}],
                    "total_count": 1, "truncated": False,
                }
            elif name == "relationship.get_contact_evidence":
                synthetic = {"contacts": [{"contact_ref": "contact_1", "display_name": "小安"}], "unavailable_refs": []}
            elif name == "match.get_status":
                synthetic = {"state": "idle", "active_proposal_count": 0, "pending_action_count": 0, "waiting_other_count": 0}
            elif name == "profile.get_self_summary":
                synthetic = {"status": "available", "summary": "已完成基本資料"}
            elif name == "web.search":
                synthetic = {
                    "query_complete": True,
                    "results": [
                        {"title": "高雄週末藝術展", "url": "https://example.com/event-1", "snippet": "週六 14:00，高雄駁二藝術特區。"},
                        {"title": "港邊音樂活動", "url": "https://example.com/event-2", "snippet": "週日 16:00，高雄流行音樂中心戶外廣場。"},
                    ],
                }
            elif name == "places.search_nearby":
                synthetic = {
                    "query_complete": True,
                    "places": [
                        {"name": "港邊咖啡", "category": "cafe", "address_summary": "高雄市鹽埕區", "distance_m": 300},
                        {"name": "倉庫咖啡", "category": "cafe", "address_summary": "高雄市鹽埕區", "distance_m": 550},
                        {"name": "海風咖啡", "category": "cafe", "address_summary": "高雄市鼓山區", "distance_m": 900},
                    ],
                }
            elif name == "product.get_info":
                synthetic = {"query_complete": True, "coverage": "sufficient", "facts": {"relationship.date_invitation": {"requires_confirmation": True}}}
            observation = {"status": "ok", "tool": name, "result": synthetic}
            observations.append(observation)
            spec = get_tool_spec(name)
            terminal_write = bool(spec and getattr(spec.risk, "value", "") == "write")
            return {"observations": [observation], "stop": terminal_write}

        initial = {
            "systemPrompt": POLICY,
            "prompt": json.dumps({
                "message": message,
                "clock": {
                    "timezone": "Asia/Taipei", "local_date": "2026-09-14",
                    "local_time": "17:30", "weekday_zh_tw": "星期一",
                },
            }, ensure_ascii=False),
            "history": [],
            "tools": schemas,
            "initialToolNames": sorted(initial_names),
            "maxRounds": 5,
            "maxToolCalls": 8,
            "validateFinal": True,
        }
        try:
            def final_validate(text, repair_attempted):
                validation = validate_pi_reply_result(text, observations)
                if validation.reply:
                    return {"accept": True, "text": validation.reply}
                if validation.repairable and not repair_attempted:
                    return {
                        "accept": False, "disableTools": True,
                        "repairPrompt": "請只用已驗證結果重寫答案，不呼叫工具、不輸出內部資料。",
                    }
                return {"accept": False, "error": validation.code or "pi_reply_invalid"}

            result = run_bridge(
                initial, model_call, tool_call,
                final_validate=final_validate,
                timeout=90, max_model_calls=5, max_tool_calls=8,
            )
            if result.get("error") and not error:
                error = str(result["error"])
        except Exception as exc:
            error = type(exc).__name__
        if not result.get("finalText"):
            fallback_reply = verified_observation_fallback(observations) or ""
        expected_spec = get_tool_spec(expected)
        expected_write = bool(
            (expected_spec and getattr(expected_spec.risk, "value", "") == "write")
            or expected == "workflow.queue_operations"
        )
        reached_terminal = (
            bool(result.get("stopped")) if expected_write
            else bool(result.get("finalText") or fallback_reply)
        )
        passed = expected in actual and reached_terminal and not error
        row = {
            "case": case_name, "status": "passed" if passed else "failed",
            "expected": expected, "actual": actual, "error": error,
            "terminal": (
                "confirmation" if result.get("stopped")
                else "reply" if result.get("finalText")
                else "verified_fallback" if fallback_reply else "none"
            ),
            "model_tool_fields": model_tool_fields[-4:],
        }
        output.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    passed = sum(row["status"] == "passed" for row in output)
    print(json.dumps({"summary": {"passed": passed, "total": len(output)}}, ensure_ascii=False))
    return 0 if passed == len(output) else 1


if __name__ == "__main__":
    raise SystemExit(main())
