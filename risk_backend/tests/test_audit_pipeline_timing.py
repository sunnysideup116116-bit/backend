#!/usr/bin/env python3
"""
測試訊息經過完整審核程序各階段時長之測試腳本
支援多次測試（預設 5 次）並計算各階段之平均耗時，包含 Appwrite 相關階段的分類小計。

執行方式：
    ../venv/bin/python tests/test_audit_pipeline_timing.py --runs 5
    ../venv/bin/pytest -s tests/test_audit_pipeline_timing.py
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List

import pytest
import urllib3

# 抑制 Appwrite SDK 的 DeprecationWarning 與本地自簽 InsecureRequestWarning 保持測試報表簡潔
warnings.simplefilter("ignore", DeprecationWarning)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 確保載入 risk_backend 根目錄
RISK_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(RISK_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(RISK_BACKEND_ROOT))


@dataclass
class StageRecord:
    stage_id: str
    name: str
    category: str  # "Appwrite 讀取", "Appwrite 寫入", "LLM 網路請求", "本地計算"
    description: str
    duration_ms: float
    percentage: float = 0.0


class AuditPipelineProfiler:
    def __init__(self) -> None:
        self.records: List[StageRecord] = []
        self.start_total_time: float = 0.0
        self.total_duration_ms: float = 0.0

    def sync_stage(self, stage_id: str, name: str, category: str, description: str, func: Callable[[], Any]) -> Any:
        start = time.perf_counter()
        try:
            return func()
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self.records.append(StageRecord(stage_id, name, category, description, elapsed_ms))

    async def async_stage(self, stage_id: str, name: str, category: str, description: str, coro: Awaitable[Any]) -> Any:
        start = time.perf_counter()
        try:
            return await coro
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self.records.append(StageRecord(stage_id, name, category, description, elapsed_ms))

    def finalize(self) -> None:
        self.total_duration_ms = (time.perf_counter() - self.start_total_time) * 1000.0
        sum_stages = sum(r.duration_ms for r in self.records) or 1.0
        for r in self.records:
            r.percentage = (r.duration_ms / sum_stages) * 100.0


async def run_single_message_audit_timing(message: str) -> tuple[AuditPipelineProfiler, Dict[str, Any]]:
    from app.core.guardrail_engine import GuardrailEngine
    from app.core.intervention_engine import InterventionEngine
    from app.core.nlp_engine import NLPEngine
    from app.core.risk_fusion import RiskFusionLayer
    from app.core.risk_state import RiskStateMachine
    from app.core.rule_engine import RuleBasedEngine
    from app.core.scenario_risk_layer import ScenarioRiskLayer
    from app.services.chat_log_service import ChatLogService
    from app.services.temporal_feature_service import TemporalFeatureService

    profiler = AuditPipelineProfiler()

    guardrail = GuardrailEngine()
    rule_engine = RuleBasedEngine()
    nlp_engine = NLPEngine()
    fusion = RiskFusionLayer()
    scenario_layer = ScenarioRiskLayer()
    state_machine = RiskStateMachine()
    intervention_engine = InterventionEngine()
    chat_log = ChatLogService()

    conv_id = f"test_timing_{uuid.uuid4().hex[:8]}"
    sender_id = "test_user_a"
    receiver_id = "test_user_b"
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"

    # 避免測試執行在資料庫留下垃圾歷史記錄
    state_log = state_machine.chat_log_service
    async def skip_history_write(*args: Any, **kwargs: Any) -> None:
        return None
    state_log.save_risk_state_history = skip_history_write

    profiler.start_total_time = time.perf_counter()

    # 階段 01: Guardrail 安全過濾 (敏感字詞 / LLM Classifier)
    guardrail_result = await profiler.async_stage(
        "S01", "Guardrail 檢查", "本地/防護", "敏感詞與安全防護",
        guardrail.check(message)
    )

    if guardrail_result.get("is_blocked"):
        profiler.finalize()
        outcome = {
            "risk_level": "blocked",
            "action": "block",
            "reason": guardrail_result.get("reason", "guardrail_triggered"),
            "nlp_confidence": 1.0,
        }
        return profiler, outcome

    # 階段 02: 關係記憶讀取 (互動天數、平衡度、累積摘要)
    memory_context = await profiler.async_stage(
        "S02", "關係記憶讀取", "Appwrite 讀取", "讀取 relationship_metrics / summary",
        chat_log.rel_service.get_memory_context(conv_id)
    )
    relationship_memory = memory_context.get("metrics") or {}
    last_summary = memory_context.get("summary")

    # 階段 03: 既有風險狀態讀取
    prior_state, _ = await profiler.async_stage(
        "S03", "既有風險狀態讀取", "Appwrite 讀取", "讀取 risk_state_history 最新狀態",
        state_machine.get_user_state(conv_id, sender_id)
    )

    # 階段 04: 已送達歷史訊息讀取
    delivered_history = await profiler.async_stage(
        "S04", "已送達訊息歷史", "Appwrite 讀取", "讀取 messages (delivered)",
        chat_log.get_recent_messages(conv_id, limit=20)
    )

    # 階段 05: 行為統計歷史訊息讀取
    behavior_history = await profiler.async_stage(
        "S05", "行為訊息歷史", "Appwrite 讀取", "讀取 messages (全部近況連發)",
        chat_log.get_recent_behavior_messages(conv_id, limit=20)
    )

    # 階段 06: 時序與行為特徵計算
    computed_features = profiler.sync_stage(
        "S06", "時序特徵計算", "本地計算", "統計連發頻率、間隔與字數比",
        lambda: TemporalFeatureService.calculate(
            current_content=message,
            current_sender=sender_id,
            history=behavior_history,
        )
    )

    # 階段 07: 規則引擎評估
    rule_result = profiler.sync_stage(
        "S07", "規則引擎評估", "本地計算(KB快取)", "正規化特徵與規則匹配評分",
        lambda: rule_engine.calculate(message, computed_features)
    )

    # 階段 08: NLP / LLM 語意深度分析
    nlp_result = await profiler.async_stage(
        "S08", "NLP / LLM 語意分析", "LLM 網路請求", "呼叫 Ollama gpt-oss:120b-cloud",
        asyncio.to_thread(
            nlp_engine.analyze,
            message,
            delivered_history,
            computed_features,
            sender_id=sender_id,
            prior_risk_state=prior_state,
            relationship_memory=relationship_memory,
            last_summary=last_summary,
        )
    )

    # 階段 09: 基礎風險融合
    initial_delta = profiler.sync_stage(
        "S09", "基礎風險融合", "本地計算(KB快取)", "依信心度權衡規則與LLM權重",
        lambda: fusion.fuse(
            rule_result["delta"],
            nlp_result["delta"],
            nlp_confidence=nlp_result.get("confidence", 0.0),
        )
    )

    # 階段 10: 情境規則評估
    bonus_delta, scenarios = profiler.sync_stage(
        "S10", "情境規則評估", "本地計算(KB快取)", "評估時段與關係複合情境加成",
        lambda: scenario_layer.evaluate(
            rule_result,
            nlp_result,
            computed_features,
            memory_metrics=relationship_memory,
            last_summary=last_summary,
        )
    )

    # 階段 11: 情境加成融合
    final_delta = profiler.sync_stage(
        "S11", "情境加成融合", "本地計算", "套用情境加成至最終 Delta",
        lambda: fusion.apply_scenario_bonus(initial_delta, bonus_delta)
    )

    # 階段 12: 累積狀態與等級決策
    nlp_degraded = str(nlp_result.get("reasoning", "")).startswith("Fallback:")
    degraded_with_flags = nlp_degraded and bool(guardrail_result.get("flagged_words"))
    new_state, risk_level = await profiler.async_stage(
        "S12", "狀態累積與決策", "Appwrite 讀取+計算", "查歷史計算 EMA 並決定等級",
        state_machine.update(
            conv_id,
            sender_id,
            msg_id,
            final_delta,
            degraded_with_flags=degraded_with_flags,
        )
    )

    # 階段 13: 介入指令產生
    diagnosis = dict(state_machine.last_diagnostic)
    diagnosis["delta_max"] = max(final_delta.model_dump().values(), default=0.0)
    intervention_cmd = await profiler.async_stage(
        "S13", "介入指令產生", "本地計算", "產生處置指令 (冷卻/警示/封鎖)",
        intervention_engine.execute(
            risk_level=risk_level,
            risk_state=new_state.model_dump(),
            diagnosis=diagnosis,
            conv_id=conv_id,
            sender_id=sender_id,
            receiver_id=receiver_id,
            msg_id=msg_id,
            decision_reason=diagnosis.get("reason", "evaluated"),
        )
    )

    profiler.finalize()
    outcome = {
        "risk_level": risk_level,
        "action": intervention_cmd.get("sender_directive", {}).get("action", "none"),
        "nlp_confidence": nlp_result.get("confidence", 0.0),
        "reasoning": nlp_result.get("reasoning", ""),
    }
    return profiler, outcome


def print_multi_run_report(
    message: str,
    runs_data: List[tuple[AuditPipelineProfiler, Dict[str, Any]]],
) -> None:
    num_runs = len(runs_data)
    stage_names: Dict[str, tuple[str, str, str]] = {}
    stage_durations: Dict[str, List[float]] = {}
    total_walls: List[float] = []

    for profiler, _ in runs_data:
        total_walls.append(profiler.total_duration_ms)
        for r in profiler.records:
            stage_names[r.stage_id] = (r.name, r.category, r.description)
            stage_durations.setdefault(r.stage_id, []).append(r.duration_ms)

    avg_stage_durations: Dict[str, float] = {
        sid: statistics.mean(durations) for sid, durations in stage_durations.items()
    }
    avg_total_wall = statistics.mean(total_walls)
    sum_avg_stages = sum(avg_stage_durations.values()) or 1.0

    print("\n" + "═" * 98)
    print(f"  📊 訊息審核管道效能基準測試報告（共執行 {num_runs} 次平均）")
    print("═" * 98)
    print(f" 測試訊息: 「{message}」")
    last_outcome = runs_data[-1][1]
    print(f" 最終判定: 等級 = [{last_outcome.get('risk_level', 'unknown').upper()}]"
          f" | 動作 = [{last_outcome.get('action', 'none')}]"
          f" | 平均總審核牆鐘 = {avg_total_wall:.2f} ms ({avg_total_wall / 1000.0:.2f} 秒)")
    print("─" * 98)
    print(f" {'編號':<4} {'階段名稱':<22} {'類別':<18} {'平均耗時 (ms)':>13} {'Min (ms)':>10} {'Max (ms)':>10} {'佔比':>7}  {'視覺分佈'}")
    print("─" * 98)

    bar_max_len = 20
    for sid in sorted(stage_names.keys()):
        name, category, _ = stage_names[sid]
        durations = stage_durations[sid]
        avg_d = avg_stage_durations[sid]
        min_d = min(durations)
        max_d = max(durations)
        pct = (avg_d / sum_avg_stages) * 100.0

        bar_len = int(round((pct / 100.0) * bar_max_len))
        bar = "█" * bar_len + "░" * (bar_max_len - bar_len)
        print(f" {sid:<4} {name:<22} {category:<18} {avg_d:>13.2f} {min_d:>10.2f} {max_d:>10.2f} {pct:>6.1f}%  {bar}")

    print("─" * 98)

    # 分類匯總統計
    cat_summary: Dict[str, float] = {}
    for sid, avg_d in avg_stage_durations.items():
        _, cat, _ = stage_names[sid]
        main_cat = "Appwrite 讀取/資料庫" if "Appwrite" in cat else ("LLM 網路請求" if "LLM" in cat else "本地計算與快取")
        cat_summary[main_cat] = cat_summary.get(main_cat, 0.0) + avg_d

    print(" 🏷️  【核心類別耗時匯總 (平均)】:")
    for cat_name, cat_total in sorted(cat_summary.items(), key=lambda x: x[1], reverse=True):
        cat_pct = (cat_total / sum_avg_stages) * 100.0
        print(f"   • {cat_name:<24}: {cat_total:>9.2f} ms ({cat_total / 1000.0:.2f} 秒, 佔 {cat_pct:5.1f}%)")

    print(f"\n 5 次總耗時明細 (Total Wall Clock): {[f'{w:.1f}ms' for w in total_walls]}")
    print("═" * 98 + "\n")


def test_audit_pipeline_timing():
    """Pytest 測試入口：驗證完整審核管道之耗時與階段完整度"""
    test_msg = "今天天氣真好，要一起喝杯咖啡嗎？"
    profiler, outcome = asyncio.run(run_single_message_audit_timing(test_msg))
    assert len(profiler.records) == 13
    assert profiler.total_duration_ms > 0
    assert outcome["risk_level"] in ("safe", "low", "medium", "high", "critical", "blocked")


def main():
    parser = argparse.ArgumentParser(description="單則訊息審核管道各階段耗時評測工具")
    parser.add_argument("--message", "-m", default="今天天氣真好，要一起喝杯咖啡嗎？", help="要測試的訊息文字")
    parser.add_argument("--runs", "-r", type=int, default=5, help="重複測試次數 (預設 5 次)")
    args = parser.parse_args()

    runs_data: List[tuple[AuditPipelineProfiler, Dict[str, Any]]] = []
    print(f"\n🚀 開始進行訊息審核管道測試，共計測試 {args.runs} 次，每次測試間隔重置上下文...")
    for i in range(1, args.runs + 1):
        print(f"   [第 {i}/{args.runs} 次測試執行中...]", end="", flush=True)
        profiler, outcome = asyncio.run(run_single_message_audit_timing(args.message))
        runs_data.append((profiler, outcome))
        print(f" 完成！耗時: {profiler.total_duration_ms:.1f} ms")

    print_multi_run_report(args.message, runs_data)


if __name__ == "__main__":
    main()
