#!/usr/bin/env python3
"""Profile the real risk-decision pipeline without writing test records.

Run from ``Server/risk_backend``:

    ../venv/bin/python tests/profile_risk_pipeline.py
    ../venv/bin/python tests/profile_risk_pipeline.py --runs 2
    ../venv/bin/python tests/profile_risk_pipeline.py --message "自訂測試訊息"

The script uses the configured Appwrite, guardrail, and NLP providers. It
measures the foreground decision stages in the same order as ``/detect`` while
intentionally skipping message/risk-history writes and background tasks. This
keeps repeated profiling runs from polluting production-like data.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import threading
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar


warnings.simplefilter("ignore", DeprecationWarning)
_default_showwarning = warnings.showwarning


def _hide_appwrite_list_documents_warning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: Any = None,
    line: str | None = None,
) -> None:
    """Hide only the SDK warning that its decorator forcibly enables."""
    if issubclass(category, DeprecationWarning) and str(message).startswith(
        "Call to deprecated function 'list_documents'"
    ):
        return
    _default_showwarning(message, category, filename, lineno, file, line)


RISK_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(RISK_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(RISK_BACKEND_ROOT))


T = TypeVar("T")


@dataclass(frozen=True)
class StageTiming:
    name: str
    milliseconds: float


@dataclass(frozen=True)
class KBFetchTiming:
    run_number: int
    stage: str
    collection_id: str
    milliseconds: float
    succeeded: bool


class KBFetchProbe:
    """Observe real Appwrite KB cache misses without changing their behavior."""

    def __init__(self, kb_service_class: type) -> None:
        self._kb_service_class = kb_service_class
        self._original_fetch = kb_service_class._fetch
        self._lock = threading.Lock()
        self._run_number = 0
        self._stage = "initialization"
        self.events: list[KBFetchTiming] = []

    def install(self) -> None:
        probe = self

        def measured_fetch(
            collection_id: str,
            queries: Any = None,
            limit: int = 100,
        ) -> list:
            started = time.perf_counter()
            succeeded = False
            try:
                result = probe._original_fetch(collection_id, queries, limit)
                succeeded = True
                return result
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                with probe._lock:
                    probe.events.append(
                        KBFetchTiming(
                            run_number=probe._run_number,
                            stage=probe._stage,
                            collection_id=str(collection_id),
                            milliseconds=elapsed,
                            succeeded=succeeded,
                        )
                    )

        self._kb_service_class._fetch = staticmethod(measured_fetch)

    def set_context(self, run_number: int, stage: str) -> None:
        with self._lock:
            self._run_number = run_number
            self._stage = stage

    def restore(self) -> None:
        self._kb_service_class._fetch = staticmethod(self._original_fetch)


class RunProfiler:
    def __init__(self, run_number: int, kb_probe: KBFetchProbe) -> None:
        self.run_number = run_number
        self.kb_probe = kb_probe
        self.timings: list[StageTiming] = []
        self.wall_milliseconds = 0.0

    async def async_stage(
        self,
        name: str,
        operation: Awaitable[T],
    ) -> T:
        self.kb_probe.set_context(self.run_number, name)
        started = time.perf_counter()
        try:
            return await operation
        finally:
            self.timings.append(
                StageTiming(name, (time.perf_counter() - started) * 1000)
            )

    def sync_stage(self, name: str, operation: Callable[[], T]) -> T:
        self.kb_probe.set_context(self.run_number, name)
        started = time.perf_counter()
        try:
            return operation()
        finally:
            self.timings.append(
                StageTiming(name, (time.perf_counter() - started) * 1000)
            )


def _build_runtime() -> dict[str, Any]:
    from app.core.guardrail_engine import GuardrailEngine
    from app.core.intervention_engine import InterventionEngine
    from app.core.nlp_engine import NLPEngine
    from app.core.risk_fusion import RiskFusionLayer
    from app.core.risk_state import RiskStateMachine
    from app.core.rule_engine import RuleBasedEngine
    from app.core.scenario_risk_layer import ScenarioRiskLayer
    from app.services.chat_log_service import ChatLogService
    from app.services.temporal_feature_service import TemporalFeatureService

    return {
        "guardrail": GuardrailEngine(),
        "rule": RuleBasedEngine(),
        "nlp": NLPEngine(),
        "fusion": RiskFusionLayer(),
        "state": RiskStateMachine(),
        "scenario": ScenarioRiskLayer(),
        "intervention": InterventionEngine(),
        "chat_log": ChatLogService(),
        "temporal_type": TemporalFeatureService,
    }


async def _profile_one_run(
    *,
    run_number: int,
    runtime: dict[str, Any],
    kb_probe: KBFetchProbe,
    message: str,
    conversation_id: str,
    sender_id: str,
    receiver_id: str,
) -> tuple[RunProfiler, dict[str, Any]]:
    profiler = RunProfiler(run_number, kb_probe)
    guardrail = runtime["guardrail"]
    rule_engine = runtime["rule"]
    nlp_engine = runtime["nlp"]
    fusion = runtime["fusion"]
    state_machine = runtime["state"]
    scenario_layer = runtime["scenario"]
    intervention_engine = runtime["intervention"]
    chat_log = runtime["chat_log"]
    temporal_type = runtime["temporal_type"]
    message_id = f"perf_probe_{uuid.uuid4().hex[:16]}"

    # RiskStateMachine.update writes risk history at the end. Replace only that
    # instance method with a no-op so all reads and calculations remain real.
    state_log = state_machine.chat_log_service
    original_save_history = state_log.save_risk_state_history

    async def skip_history_write(*args: Any, **kwargs: Any) -> None:
        return None

    state_log.save_risk_state_history = skip_history_write

    pipeline_started = time.perf_counter()
    try:
        guardrail_result = await profiler.async_stage(
            "01 Guardrail 檢查",
            guardrail.check(message),
        )
        if guardrail_result.get("is_blocked"):
            profiler.wall_milliseconds = (
                time.perf_counter() - pipeline_started
            ) * 1000
            return profiler, {
                "risk_level": "blocked",
                "early_exit": True,
                "guardrail_degraded": guardrail_result.get("degraded", False),
                "nlp_degraded": None,
            }

        memory_context = await profiler.async_stage(
            "02 關係記憶讀取",
            chat_log.rel_service.get_memory_context(conversation_id),
        )
        relationship_memory = memory_context.get("metrics") or {}
        last_summary = memory_context.get("summary")

        prior_state, _ = await profiler.async_stage(
            "03 既有風險狀態讀取",
            state_machine.get_user_state(conversation_id, sender_id),
        )
        delivered_history = await profiler.async_stage(
            "04 已送達訊息歷史",
            chat_log.get_recent_messages(conversation_id, limit=20),
        )
        behavior_history = await profiler.async_stage(
            "05 行為訊息歷史",
            chat_log.get_recent_behavior_messages(conversation_id, limit=20),
        )

        computed_features = profiler.sync_stage(
            "06 時序特徵計算",
            lambda: temporal_type.calculate(
                current_content=message,
                current_sender=sender_id,
                history=behavior_history,
            ),
        )
        rule_result = profiler.sync_stage(
            "07 規則引擎",
            lambda: rule_engine.calculate(message, computed_features),
        )
        nlp_result = await profiler.async_stage(
            "08 NLP / LLM 分析",
            asyncio.to_thread(
                nlp_engine.analyze,
                message,
                delivered_history,
                computed_features,
                sender_id=sender_id,
                prior_risk_state=prior_state,
                relationship_memory=relationship_memory,
                last_summary=last_summary,
            ),
        )
        initial_delta = profiler.sync_stage(
            "09 風險融合",
            lambda: fusion.fuse(
                rule_result["delta"],
                nlp_result["delta"],
                nlp_confidence=nlp_result.get("confidence", 0.0),
            ),
        )
        bonus_delta, scenarios = profiler.sync_stage(
            "10 情境規則",
            lambda: scenario_layer.evaluate(
                rule_result,
                nlp_result,
                computed_features,
                memory_metrics=relationship_memory,
                last_summary=last_summary,
            ),
        )
        final_delta = profiler.sync_stage(
            "11 情境加成融合",
            lambda: fusion.apply_scenario_bonus(initial_delta, bonus_delta),
        )

        nlp_degraded = str(nlp_result.get("reasoning", "")).startswith(
            "Fallback:"
        )
        degraded_with_flags = nlp_degraded and bool(
            guardrail_result.get("flagged_words")
        )
        new_state, risk_level = await profiler.async_stage(
            "12 累積狀態與等級決策",
            state_machine.update(
                conversation_id,
                sender_id,
                message_id,
                final_delta,
                degraded_with_flags=degraded_with_flags,
            ),
        )
        diagnosis = dict(state_machine.last_diagnostic)
        diagnosis["delta_max"] = max(
            final_delta.model_dump().values(),
            default=0.0,
        )
        intervention = await profiler.async_stage(
            "13 介入指令產生",
            intervention_engine.execute(
                risk_level=risk_level,
                risk_state=new_state.model_dump(),
                diagnosis=diagnosis,
                conv_id=conversation_id,
                sender_id=sender_id,
                receiver_id=receiver_id,
                msg_id=message_id,
                decision_reason=diagnosis.get("reason", "normal"),
                chat_log_service=chat_log,
                message_delta=final_delta.model_dump(),
            ),
        )
        profiler.wall_milliseconds = (
            time.perf_counter() - pipeline_started
        ) * 1000
        return profiler, {
            "risk_level": risk_level,
            "early_exit": False,
            "guardrail_degraded": guardrail_result.get("degraded", False),
            "nlp_degraded": nlp_degraded,
            "nlp_confidence": nlp_result.get("confidence", 0.0),
            "triggered_rules": rule_result.get("triggered_rules", []),
            "triggered_scenarios": scenarios,
            "intervention_action": (
                intervention.get("sender_directive") or {}
            ).get("action"),
        }
    finally:
        state_log.save_risk_state_history = original_save_history


def _print_run(
    profiler: RunProfiler,
    outcome: dict[str, Any],
    kb_events: list[KBFetchTiming],
) -> None:
    print(f"\n{'=' * 76}")
    print(f"第 {profiler.run_number} 次決策路徑")
    print(f"{'=' * 76}")
    print(f"{'階段':<34}{'耗時':>14}{'占整體':>12}")
    print("-" * 76)
    for timing in profiler.timings:
        share = (
            timing.milliseconds / profiler.wall_milliseconds * 100
            if profiler.wall_milliseconds > 0
            else 0.0
        )
        print(f"{timing.name:<32}{timing.milliseconds:>11.1f} ms{share:>10.1f}%")
    print("-" * 76)
    print(f"{'決策路徑總牆鐘':<32}{profiler.wall_milliseconds:>11.1f} ms")

    if profiler.timings:
        slowest = max(profiler.timings, key=lambda item: item.milliseconds)
        print(
            f"\n最慢階段：{slowest.name} — {slowest.milliseconds:.1f} ms "
            f"({slowest.milliseconds / profiler.wall_milliseconds * 100:.1f}%)"
        )

    print(
        "結果："
        f"risk_level={outcome.get('risk_level')}, "
        f"nlp_degraded={outcome.get('nlp_degraded')}, "
        f"guardrail_degraded={outcome.get('guardrail_degraded')}, "
        f"sender_action={outcome.get('intervention_action')}"
    )

    if kb_events:
        print("\n本次實際 Appwrite KB HTTP（快取 miss）：")
        for event in kb_events:
            status = "ok" if event.succeeded else "error"
            print(
                f"  {event.collection_id:<24}{event.milliseconds:>9.1f} ms  "
                f"[{event.stage}; {status}]"
            )
        print(
            f"  {'KB HTTP 合計':<24}"
            f"{sum(event.milliseconds for event in kb_events):>9.1f} ms  "
            f"({len(kb_events)} 次)"
        )
    else:
        print("\n本次實際 Appwrite KB HTTP：0 次（全部命中快取）")


def _print_aggregate(profiles: list[RunProfiler]) -> None:
    if len(profiles) < 2:
        return
    names = [timing.name for timing in profiles[0].timings]
    medians: list[StageTiming] = []
    for name in names:
        samples = [
            timing.milliseconds
            for profile in profiles
            for timing in profile.timings
            if timing.name == name
        ]
        if samples:
            medians.append(StageTiming(name, statistics.median(samples)))
    if not medians:
        return
    slowest = max(medians, key=lambda item: item.milliseconds)
    print(f"\n{'=' * 76}")
    print(f"{len(profiles)} 次執行中位數最慢階段：{slowest.name}")
    print(f"中位耗時：{slowest.milliseconds:.1f} ms")
    print(f"{'=' * 76}")


async def _main_async(args: argparse.Namespace) -> int:
    import_started = time.perf_counter()
    from app.services.kb_service import KBService

    kb_probe = KBFetchProbe(KBService)
    kb_probe.install()
    try:
        if hasattr(KBService, "clear_cache"):
            KBService.clear_cache()
        kb_probe.set_context(0, "冷啟動／元件初始化")
        runtime = _build_runtime()
        initialization_ms = (time.perf_counter() - import_started) * 1000
        print("風險偵測決策路徑效能量測（唯讀模式）")
        print(f"冷啟動／元件初始化：{initialization_ms:.1f} ms")
        print("不包含：pending 訊息寫入、風險歷史寫入、HTTP/gateway、背景工作")
        print(f"測試訊息：{args.message}")

        probe_suffix = uuid.uuid4().hex[:12]
        conversation_id = f"perf_probe_{probe_suffix}"
        sender_id = f"perf_sender_{probe_suffix}"
        receiver_id = f"perf_receiver_{probe_suffix}"
        profiles: list[RunProfiler] = []

        for run_number in range(1, args.runs + 1):
            profiler, outcome = await _profile_one_run(
                run_number=run_number,
                runtime=runtime,
                kb_probe=kb_probe,
                message=args.message,
                conversation_id=conversation_id,
                sender_id=sender_id,
                receiver_id=receiver_id,
            )
            profiles.append(profiler)
            run_events = [
                event
                for event in kb_probe.events
                if event.run_number == run_number
            ]
            _print_run(profiler, outcome, run_events)

        init_events = [event for event in kb_probe.events if event.run_number == 0]
        if init_events:
            print("\n初始化期間的 KB HTTP：")
            for event in init_events:
                print(
                    f"  {event.collection_id:<24}{event.milliseconds:>9.1f} ms"
                )
        _print_aggregate(profiles)
        return 0
    finally:
        kb_probe.restore()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="量測風險偵測各前景決策階段的實際耗時",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="執行次數；第 2 次起可觀察暖 KB 快取（預設：1）",
    )
    parser.add_argument(
        "--message",
        default="你都不理我，我一個人好孤單。",
        help="送入 Guardrail／規則／NLP 的測試文字",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs 必須至少為 1")
    previous_showwarning = warnings.showwarning
    warnings.showwarning = _hide_appwrite_list_documents_warning
    try:
        return asyncio.run(_main_async(args))
    finally:
        warnings.showwarning = previous_showwarning


if __name__ == "__main__":
    raise SystemExit(main())
