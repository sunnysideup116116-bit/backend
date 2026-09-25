"""Voice must never turn a clipped display label into a memory write."""
import pytest

from app_voice_assistant.capabilities import ARGUMENT_SCHEMAS
from app_voice_assistant.contracts import deterministic_proposal, validate_proposal
from matchmaker_agent.concept_identity import MAX_PREFERENCE_TEXT_CHARS


@pytest.mark.parametrize("label", [
    "Visiting quiet historical castles with no guided tours and step-free wheelchair access",
    "Learning about literature by reading mystery novels rather than writing mystery novels",
])
def test_voice_memory_retains_complete_label(label):
    deterministic = deterministic_proposal("請記住我喜歡" + label, context={"revision": 1})
    validated = validate_proposal({"intent": "memory.add", "arguments": {"label": label}}, base_revision=1)
    assert deterministic.arguments["label"] == label
    assert validated.arguments["label"] == label


@pytest.mark.parametrize("label", ["x" * 501, "ﬃ" * 200])
def test_voice_memory_overlimit_is_rejected_not_shortened(label):
    assert deterministic_proposal("請記住我喜歡" + label, context={"revision": 1}) is None
    assert validate_proposal({"intent": "memory.add", "arguments": {"label": label}}, base_revision=1) is None


def test_voice_declarative_schema_matches_authoritative_semantic_limit():
    assert ARGUMENT_SCHEMAS["memory.add"]["properties"]["label"]["maxLength"] == MAX_PREFERENCE_TEXT_CHARS


@pytest.mark.parametrize("size", [40, 41, 499, 500, 501])
@pytest.mark.parametrize("script", ["chinese", "emoji", "combining"])
def test_voice_memory_unicode_codepoint_boundary_matrix(size, script):
    """The real validator counts raw/normalized codepoints, not UTF-16 or glyphs."""
    import unicodedata

    if script == "chinese":
        label = "閱讀" * (size // 2) + ("書" if size % 2 else "")
    elif script == "emoji":
        label = "🧩" * size
    else:
        label = "e\u0301" * (size // 2) + ("z" if size % 2 else "")
    assert len(label) == size
    utf16_units = len(label.encode("utf-16-le")) // 2
    assert utf16_units == (size * 2 if script == "emoji" else size)
    normalized = unicodedata.normalize("NFKC", label)
    if script == "combining":
        assert len(normalized) == (size + 1) // 2

    proposal = validate_proposal({
        "intent": "memory.add", "arguments": {"label": label, "stance": "like"},
    }, base_revision=7)
    if size > MAX_PREFERENCE_TEXT_CHARS:
        # In particular, 501 combining codepoints cannot be admitted merely
        # because NFKC would compose them into a shorter string.
        assert proposal is None
    else:
        assert proposal is not None
        assert proposal.intent == "memory.add"
        assert proposal.arguments == {"label": normalized, "stance": "like"}
        assert proposal.base_revision == 7
