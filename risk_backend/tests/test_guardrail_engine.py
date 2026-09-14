"""Step 0 Guardrail trigger_mode + flagged_words tests (Phase 3.1)."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest


FAKE_RECORDS = [
    {"keyword": "炸彈", "reason_label": "violence", "trigger_mode": "flag"},
    {"keyword": "殺人", "reason_label": "violence", "trigger_mode": "flag"},
    {"keyword": "殺死你", "reason_label": "violence_threat", "trigger_mode": "block"},
    {"keyword": "傳裸照給我", "reason_label": "sexual_demand", "trigger_mode": "block"},
]

CRITICAL_VARIANTS = [
    "我要殺了你",
    "我要弄死你",
    "我要把你弄死",
    "我要把你殺了",
    "我要殺掉你",
    "我要把你宰了",
    "我要把你砍死",
    "我一定會宰了你",
    "我要砍死你",
    "我要性侵你",
    "我會把你的裸照公開",
    "我會讓你的私密照外流",
    "我就把你的私密照傳給所有人",
    "我知道你住哪裡",
    "我會去你家堵你",
    "我會每天跟蹤你",
    "我會傷害你的家人",
    "你沒有拒絕的權利",
    "不准拒絕我",
]


@pytest.fixture
def guardrail(monkeypatch):
    """Build GuardrailEngine with mocked KBService records and disabled Layer 2."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GUARDRAIL_PROVIDER", raising=False)
    monkeypatch.delenv("GUARDRAIL_API_KEY", raising=False)
    monkeypatch.delenv("GUARDRAIL_BASE_URL", raising=False)

    with patch("app.core.guardrail_engine.KBService.get_hard_block_records", return_value=FAKE_RECORDS):
        from app.core.guardrail_engine import GuardrailEngine

        g = GuardrailEngine()
        g._openai_mod_client = None
        yield g


def test_block_mode_keyword_blocks(guardrail):
    """命中 block mode 詞 -> 立即攔截"""
    result = asyncio.run(guardrail.check("我要殺死你"))
    assert result["is_blocked"] is True
    assert "殺死你" in result["reason"]
    assert result["flagged_words"] == ["殺死你"]
    assert result["degraded"] is False


def test_block_mode_phrase_blocks(guardrail):
    """命中 block mode 片語 -> 攔截"""
    result = asyncio.run(guardrail.check("傳裸照給我"))
    assert result["is_blocked"] is True
    assert "傳裸照給我" in result["reason"]


@pytest.mark.parametrize("message", CRITICAL_VARIANTS)
def test_critical_threat_variants_are_hard_blocked(monkeypatch, message):
    records = [
        {"keyword": keyword, "reason_label": "critical", "trigger_mode": "block"}
        for keyword in (
            "殺了你",
            "弄死你",
            "把你弄死",
            "把你殺了",
            "殺掉你",
            "宰了你",
            "把你宰了",
            "砍死你",
            "把你砍死",
            "性侵你",
            "裸照公開",
            "私密照外流",
            "私密照傳給",
            "我知道你住哪",
            "去你家堵你",
            "跟蹤你",
            "傷害你的家人",
            "沒有拒絕的權利",
            "不准拒絕",
        )
    ]
    with patch(
        "app.core.guardrail_engine.KBService.get_hard_block_records",
        return_value=records,
    ):
        from app.core.guardrail_engine import GuardrailEngine

        engine = GuardrailEngine()
        result = asyncio.run(engine.check(message))

    assert result["is_blocked"] is True


def test_flag_mode_keyword_passes_with_flag(guardrail):
    """命中 flag mode 詞 -> 不攔截，但 flagged_words 包含該詞"""
    result = asyncio.run(guardrail.check("新聞說那個殺人案"))
    assert result["is_blocked"] is False
    assert "殺人" in result.get("flagged_words", [])


def test_multiple_flag_words_collected(guardrail):
    """多個 flag 詞同時命中 -> 全部進 flagged_words"""
    result = asyncio.run(guardrail.check("新聞說那個殺人犯被炸彈炸傷"))
    assert result["is_blocked"] is False
    assert "殺人" in result["flagged_words"]
    assert "炸彈" in result["flagged_words"]


def test_block_takes_precedence_over_flag(guardrail):
    """同時含 block 與 flag -> 走 block 路徑"""
    result = asyncio.run(guardrail.check("殺死你 跟那個殺人犯一樣"))
    assert result["is_blocked"] is True
    assert "殺死你" in result["reason"]


def test_no_match_clean_result(guardrail):
    """完全不含禁詞 -> 走 Layer 2，flagged_words 空陣列"""
    result = asyncio.run(guardrail.check("今天天氣真好我們去公園走走"))
    assert result["is_blocked"] is False
    assert result.get("flagged_words", []) == []
    assert result["degraded"] is True


def test_block_preserves_prior_flag_words(guardrail):
    """命中 block 之前累積的 flag 詞應該保留在 flagged_words（audit 完整性）"""
    result = asyncio.run(guardrail.check("那個殺人犯說 殺死你"))
    assert result["is_blocked"] is True
    assert "殺死你" in result["flagged_words"]
    assert "殺人" in result["flagged_words"]


def test_null_trigger_mode_defaults_to_flag(monkeypatch):
    """trigger_mode 為 None / 缺欄位時應視為 flag（防 DB 漏填）"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GUARDRAIL_PROVIDER", raising=False)
    monkeypatch.delenv("GUARDRAIL_API_KEY", raising=False)
    monkeypatch.delenv("GUARDRAIL_BASE_URL", raising=False)

    null_records = [
        {"keyword": "測試詞A", "reason_label": "test", "trigger_mode": None},
        {"keyword": "測試詞B", "reason_label": "test"},
    ]
    with patch("app.core.guardrail_engine.KBService.get_hard_block_records", return_value=null_records):
        from app.core.guardrail_engine import GuardrailEngine

        g = GuardrailEngine()
        g._openai_mod_client = None
        result = asyncio.run(g.check("這是測試詞A 跟 測試詞B"))

    assert result["is_blocked"] is False
    assert "測試詞A" in result.get("flagged_words", [])
    assert "測試詞B" in result.get("flagged_words", [])


def test_classifier_unsafe_no_longer_blocks(monkeypatch):
    """Llama Guard 判 unsafe 不再直接 block，只標 classifier_flagged"""
    monkeypatch.setenv("GUARDRAIL_PROVIDER", "llm_classifier")
    monkeypatch.setenv("GUARDRAIL_BASE_URL", "http://test/v1")
    monkeypatch.setenv("GUARDRAIL_API_KEY", "test")
    monkeypatch.setenv("GUARDRAIL_MODEL", "test-model")

    mock_adapter = MagicMock()
    mock_adapter.generate.return_value = "unsafe\nS1"

    with patch("app.core.guardrail_engine.KBService.get_hard_block_records", return_value=[]):
        from app.core.guardrail_engine import GuardrailEngine

        g = GuardrailEngine()
        g._classifier_adapter = mock_adapter
        g._classifier_model = "test"
        result = asyncio.run(g.check("HIIIIIII!!!"))

    assert result["is_blocked"] is False
    assert result["classifier_flagged"] is True
    assert "S1" in result["classifier_categories"]


def test_classifier_safe_no_flag(monkeypatch):
    """Llama Guard 判 safe -> classifier_flagged=False"""
    monkeypatch.setenv("GUARDRAIL_PROVIDER", "llm_classifier")
    monkeypatch.setenv("GUARDRAIL_BASE_URL", "http://test/v1")
    monkeypatch.setenv("GUARDRAIL_API_KEY", "test")
    monkeypatch.setenv("GUARDRAIL_MODEL", "test-model")

    mock_adapter = MagicMock()
    mock_adapter.generate.return_value = "safe"

    with patch("app.core.guardrail_engine.KBService.get_hard_block_records", return_value=[]):
        from app.core.guardrail_engine import GuardrailEngine

        g = GuardrailEngine()
        g._classifier_adapter = mock_adapter
        g._classifier_model = "test"
        result = asyncio.run(g.check("普通閒聊"))

    assert result["is_blocked"] is False
    assert result["classifier_flagged"] is False
    assert result["degraded"] is False


def test_classifier_error_is_marked_degraded(monkeypatch):
    monkeypatch.setenv("GUARDRAIL_PROVIDER", "llm_classifier")
    monkeypatch.setenv("GUARDRAIL_BASE_URL", "http://test/v1")
    monkeypatch.setenv("GUARDRAIL_API_KEY", "test")
    monkeypatch.setenv("GUARDRAIL_MODEL", "test-model")

    mock_adapter = MagicMock()
    mock_adapter.generate.side_effect = ConnectionError("unavailable")

    with patch("app.core.guardrail_engine.KBService.get_hard_block_records", return_value=[]):
        from app.core.guardrail_engine import GuardrailEngine

        g = GuardrailEngine()
        g._classifier_adapter = mock_adapter
        g._classifier_model = "test"
        result = asyncio.run(g.check("普通閒聊"))

    assert result["is_blocked"] is False
    assert result["classifier_flagged"] is False
    assert result["degraded"] is True


def test_mysql_block_still_blocks_even_if_classifier_safe(guardrail):
    """MySQL block mode 仍立即 blocked，Llama Guard 不會被諮詢"""
    result = asyncio.run(guardrail.check("我要殺死你"))
    assert result["is_blocked"] is True
    assert "殺死你" in result["reason"]
    assert "classifier_flagged" not in result or result.get("classifier_flagged") is None


# --- 降級標記完整覆蓋（整合進度確認 2026-08-23 第 5 項 / B4）---
# guardrail_engine 有 5 處 degraded=True 路徑；既有測試已覆蓋 clean result
# （無 provider）與 classifier error。以下補齊其餘降級路徑，並驗證正常路徑
# degraded=False 不被誤標。

def test_openai_moderation_no_client_marks_degraded(monkeypatch):
    """openai_moderation provider 但無 OPENAI_API_KEY → degraded=True。"""
    monkeypatch.delenv("GUARDRAIL_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch("app.core.guardrail_engine.KBService.get_hard_block_records", return_value=[]):
        from app.core.guardrail_engine import GuardrailEngine

        g = GuardrailEngine()
        assert g._openai_mod_client is None
        result = asyncio.run(g.check("普通閒聊"))

    assert result["is_blocked"] is False
    assert result["degraded"] is True
    assert "no client" in result["reason"]


def test_openai_moderation_transient_error_marks_degraded(monkeypatch):
    """openai_moderation 呼叫拋例外 → degraded=True，不 block。"""
    monkeypatch.delenv("GUARDRAIL_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")

    with patch("app.core.guardrail_engine.KBService.get_hard_block_records", return_value=[]):
        from app.core.guardrail_engine import GuardrailEngine

        g = GuardrailEngine()
        # 用 MagicMock 替換真實 OpenAI client，讓 moderations.create 拋暫時性例外
        g._openai_mod_client = MagicMock()
        g._openai_mod_client.moderations.create.side_effect = ConnectionError("timeout")
        result = asyncio.run(g.check("普通閒聊"))

    assert result["is_blocked"] is False
    assert result["degraded"] is True
    assert "moderation error" in result["reason"]


def test_classifier_empty_response_marks_degraded(monkeypatch):
    """llm_classifier 回傳空字串 → degraded=True。"""
    monkeypatch.setenv("GUARDRAIL_PROVIDER", "llm_classifier")
    monkeypatch.setenv("GUARDRAIL_BASE_URL", "http://test/v1")
    monkeypatch.setenv("GUARDRAIL_API_KEY", "test")
    monkeypatch.setenv("GUARDRAIL_MODEL", "test-model")

    mock_adapter = MagicMock()
    mock_adapter.generate.return_value = ""

    with patch("app.core.guardrail_engine.KBService.get_hard_block_records", return_value=[]):
        from app.core.guardrail_engine import GuardrailEngine

        g = GuardrailEngine()
        g._classifier_adapter = mock_adapter
        g._classifier_model = "test"
        result = asyncio.run(g.check("普通閒聊"))

    assert result["is_blocked"] is False
    assert result["classifier_flagged"] is False
    assert result["degraded"] is True
    assert "empty response" in result["reason"]


def test_block_mode_not_degraded(guardrail):
    """命中 block 禁詞 → 攔截，但 degraded 必須是 False（這是可靠路徑）。"""
    result = asyncio.run(guardrail.check("我要殺死你"))
    assert result["is_blocked"] is True
    assert result["degraded"] is False


def test_classifier_safe_not_degraded(monkeypatch):
    """llm_classifier 判 safe → 正常路徑，degraded=False。"""
    monkeypatch.setenv("GUARDRAIL_PROVIDER", "llm_classifier")
    monkeypatch.setenv("GUARDRAIL_BASE_URL", "http://test/v1")
    monkeypatch.setenv("GUARDRAIL_API_KEY", "test")
    monkeypatch.setenv("GUARDRAIL_MODEL", "test-model")

    mock_adapter = MagicMock()
    mock_adapter.generate.return_value = "safe"

    with patch("app.core.guardrail_engine.KBService.get_hard_block_records", return_value=[]):
        from app.core.guardrail_engine import GuardrailEngine

        g = GuardrailEngine()
        g._classifier_adapter = mock_adapter
        g._classifier_model = "test"
        result = asyncio.run(g.check("普通閒聊"))

    assert result["is_blocked"] is False
    assert result["classifier_flagged"] is False
    assert result["degraded"] is False
