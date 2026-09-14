#!/usr/bin/env python3
"""Read-only live-model acceptance for the Candy Pi Calendar tool surface.

This script calls the configured model but never dispatches a tool, creates a
confirmation, reads a real account, or writes Calendar data.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError


SERVER_ROOT = Path(__file__).resolve().parents[1]
SOCIAL_ROOT = SERVER_ROOT / "social"
sys.path.insert(0, str(SOCIAL_ROOT))

from config import load_gpt_settings  # noqa: E402
from services.ai_service import generate_chat_completion_with_tools  # noqa: E402
from services.ayue_agent.pi import calendar_tools  # noqa: E402
from services.ayue_agent.pi.runtime import (  # noqa: E402
    INITIAL_TOOLS, POLICY, provider_messages, run_bridge, tool_schemas,
)


CLOCK = {
    "timezone": "Asia/Taipei",
    "local_date": "2026-09-14",
    "local_time": "14:30",
    "local_iso": "2026-09-14T14:30:00+08:00",
    "utc_iso": "2026-09-14T06:30:00+00:00",
    "weekday_zh_tw": "星期一",
    "temporal_references": {"下週四": "2026-09-24", "明天": "2026-09-15"},
}


CASES = [
    ("boer_complete", "下週四下午四點去駁二逛街，幫我加到行事曆，逛一小時", [], "calendar.prepare_create"),
    ("boer_missing_time", "下週四去駁二逛街，幫我加到行事曆", [], "calendar.prepare_create"),
    ("boer_followup_16", "下午四點，大概一小時", ["下週四去駁二逛街，幫我加到行事曆"], "calendar.prepare_create"),
    ("boer_followup_17", "下午五點，一小時結束", ["下週四去駁二逛街，幫我加到行事曆"], "calendar.prepare_create"),
    ("dentist", "明天下午兩點到五點看牙醫，幫我加行事曆", [], "calendar.prepare_create"),
    ("birthday_all_day", "9月28日生日，幫我加成全天行程", [], "calendar.prepare_create"),
    ("trip_multiday", "10月2日至10月4日去台東，幫我記成全天行程", [], "calendar.prepare_create"),
    ("two_creates", "明天九點開會十點結束，下午兩點運動一小時，都加進行事曆", [], "calendar.prepare_create"),
    ("update_start", "把駁二逛街改成下午六點開始", [], "calendar.prepare_update"),
    ("update_duration", "把牙醫行程改成兩小時", [], "calendar.prepare_update"),
    ("update_shift", "把明天的開會延後一小時", [], "calendar.prepare_update"),
    ("update_location", "把週四逛街的地點改成駁二藝術特區", [], "calendar.prepare_update"),
    ("update_followup", "改成下午三點", ["把明天的牙醫行程改時間", "你要改成幾點？"], "calendar.prepare_update"),
    ("cancel_one", "取消下週四的駁二行程", [], "calendar.prepare_cancel"),
    ("cancel_with_time", "取消明天下午兩點的牙醫", [], "calendar.prepare_cancel"),
    ("cancel_two", "取消明天的開會和運動", [], "calendar.prepare_cancel"),
    ("cancel_all", "把我未來所有行程都取消", [], "calendar.prepare_cancel_many"),
    ("list_tomorrow", "我明天有哪些行程？", [], "calendar.list_my_events"),
    ("next_event", "我下一個行程是什麼？", [], "calendar.get_next_my_event"),
    ("find_event", "牙醫是幾點？", [], "calendar.find_my_event"),
    ("verify_write", "剛才那筆有新增成功嗎？", [], "calendar.verify_recent_mutation"),
]
EXPECTED_ALTERNATIVES = {
    "cancel_two": frozenset({"calendar.prepare_cancel", "calendar.prepare_cancel_many"}),
}


def _initial(message: str, history: list[str], schemas: list[dict]) -> dict:
    recent = []
    for index, content in enumerate(history):
        recent.append({
            "role": "user" if index % 2 == 0 else "assistant",
            "content": content,
            "sent_at": "2026-09-14T14:29:00+08:00",
            "timezone": "Asia/Taipei",
        })
    prompt = json.dumps({
        "message": message,
        "recent_messages": recent,
        "clock": CLOCK,
    }, ensure_ascii=False)
    return {
        "systemPrompt": POLICY,
        "prompt": prompt,
        "history": [],
        "tools": schemas,
        "initialToolNames": sorted(INITIAL_TOOLS),
        "maxRounds": 5,
    }


def _fake_read(name: str, arguments: dict) -> dict:
    def event(activity: str, date: str, start: str, end: str) -> dict:
        return {
            "activity": activity,
            "date": date,
            "end_date": date,
            "start_time": start,
            "end_time": end,
            "all_day": False,
            "status": "confirmed",
        }
    events = [
        event("駁二逛街", "2026-09-24", "16:00", "17:00"),
        event("看牙醫", "2026-09-15", "14:00", "17:00"),
        event("開會", "2026-09-15", "09:00", "10:00"),
        event("運動", "2026-09-15", "14:00", "15:00"),
    ]
    if name == "calendar.list_my_events":
        return {"events": events, "range": "next_90_days"}
    if name == "calendar.find_my_event":
        hint = str(arguments.get("event_hint") or "")
        selected = next((item for item in events if item["activity"] in hint or hint in item["activity"]), events[0])
        return {"status": "found", "event": selected}
    if name == "calendar.get_next_my_event":
        return {"status": "found", "event": events[1]}
    return {"calendar_mutation_verification": {"status": "verified_success", "action": "create"}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=len(CASES))
    parser.add_argument("--case", action="append", dest="case_names", default=[])
    args = parser.parse_args()
    load_gpt_settings()
    schemas = tool_schemas()
    results = []
    selected = [case for case in CASES if not args.case_names or case[0] in set(args.case_names)]
    for name, message, history, expected in selected[:max(0, min(args.limit, len(selected)))]:
        status = "failed"
        actual: list[str] = []
        code = "missing_tool_call"
        validation_error = ""
        try:
            def model_call(messages, deadline, active_tool_names=None):
                allowed = set(INITIAL_TOOLS if active_tool_names is None else active_tool_names)
                completion = generate_chat_completion_with_tools(
                    "",
                    [
                        {"type": "function", "function": schema}
                        for schema in schemas if schema["name"] in allowed
                    ],
                    system_prompt=POLICY,
                    temperature=0,
                    max_tokens=1024,
                    model_owner="pi",
                    deadline_monotonic=deadline,
                    conversation_messages=provider_messages(messages),
                )
                return {
                    "content": completion.content,
                    "tool_calls": completion.tool_calls,
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                }

            def tool_call(tool_name, arguments):
                nonlocal validation_error
                actual.append(tool_name)
                if tool_name in calendar_tools.WRITE_TOOLS:
                    try:
                        calendar_tools._commands(tool_name, arguments)
                    except ValidationError as exc:
                        validation_error = ",".join(
                            f"{'.'.join(str(part) for part in item.get('loc') or [])}:{item.get('type')}"
                            for item in exc.errors(include_input=False, include_url=False)[:4]
                        )[:240]
                        return {"observations": [{"status": "failed", "code": "tool_schema_invalid"}]}
                    except Exception as exc:
                        validation_error = type(exc).__name__[:80]
                        return {"observations": [{"status": "failed", "code": "tool_schema_invalid"}]}
                    return {"observations": [{"status": "ok", "result": {"pending_confirmation": True}}], "stop": True}
                if tool_name in calendar_tools.READ_TOOLS:
                    return {"observations": [{"status": "ok", "tool": tool_name, "result": _fake_read(tool_name, arguments)}]}
                return {"observations": [{"status": "failed", "code": "tool_not_allowed"}]}

            outcome = run_bridge(_initial(message, history, schemas), model_call, tool_call, timeout=90)
            accepted_tools = EXPECTED_ALTERNATIVES.get(name, frozenset({expected}))
            if any(tool in actual for tool in accepted_tools) and not validation_error:
                status, code = "passed", ""
            elif validation_error:
                code = validation_error
            elif outcome.get("error"):
                failures = outcome.get("toolFailures") or []
                fields = (failures[-1].get("argumentFields") or []) if failures else []
                actual.extend(str(item.get("tool") or "") for item in failures if item.get("tool"))
                code = (str(outcome["error"]) + (":" + ",".join(str(field) for field in fields) if fields else ""))[:240]
            elif actual:
                code = "expected_tool_not_reached"
        except ValidationError as exc:
            code = ",".join(
                f"{'.'.join(str(part) for part in item.get('loc') or [])}:{item.get('type')}"
                for item in exc.errors(include_input=False, include_url=False)[:4]
            )[:240]
        except Exception as exc:
            code = type(exc).__name__[:80]
        results.append({"case": name, "status": status, "expected": expected, "actual": actual, "code": code})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    passed = sum(item["status"] == "passed" for item in results)
    print(json.dumps({"summary": {"passed": passed, "total": len(results)}}, ensure_ascii=False))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
