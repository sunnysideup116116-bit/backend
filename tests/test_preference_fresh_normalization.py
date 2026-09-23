"""Pinned fresh-input conversion, Unicode bounds and immutable persisted v2."""
from pathlib import Path
from importlib import metadata
import builtins

import pytest

from matchmaker_agent import concept_identity as identity


@pytest.fixture(autouse=True)
def isolated_converter_cache():
    identity._fresh_preference_converter.cache_clear()
    yield
    identity._fresh_preference_converter.cache_clear()


@pytest.mark.parametrize("raw,expected", [
    ("安静的咖啡厅", "安靜的咖啡廳"),
    ("安靜的咖啡廳", "安靜的咖啡廳"),
    ("我喜欢　安静的咖啡厅（无烟）", "安靜的咖啡廳(無煙)"),
    ("安静的咖啡厅， 无烟", "安靜的咖啡廳, 無煙"),
    ("安靜的咖啡廳, 無烟", "安靜的咖啡廳, 無煙"),
    ("  安静的咖啡厅\t无烟 \n", "安靜的咖啡廳 無煙"),
    ("Kpop", "K-pop"), ("K pop", "K-pop"), ("k-pop", "K-pop"),
    ("Board Games", "Board Games"),
    ("Science Fiction", "Science Fiction"),
    ("Coffee Shop", "Coffee Shop"),
    ("C++ Programming", "C++ Programming"),
    ("Cafe\u0301 Reading", "Café Reading"),
])
def test_fresh_normalizer_has_one_pinned_result(raw, expected):
    result = identity.canonicalize_fresh_concept(raw, "untrusted_key")
    assert result.semantic_text == expected
    assert result.key == identity.canonicalize_concept(expected).key
    assert identity.canonicalize_fresh_concept(result.semantic_text).key == result.key
    assert identity.stored_concept_identity(result.as_dict()) == result


def test_fixed_converter_contract_and_service_dependency_pins():
    assert identity.FRESH_PREFERENCE_CONVERTER_DISTRIBUTION == "OpenCC"
    assert identity.FRESH_PREFERENCE_CONVERTER_VERSION == "1.4.1"
    assert identity.FRESH_PREFERENCE_CONVERTER_CONFIG == "s2twp"
    assert metadata.version("OpenCC") == "1.4.1"
    identity.check_fresh_preference_normalizer()
    root = Path(__file__).resolve().parents[1]
    for service in ("social", "matchmaker_agent"):
        raw = (root / service / "requirements.txt").read_bytes()
        requirements = raw.decode("utf-16" if raw.startswith(b"\xff\xfe") else "utf-8")
        assert "OpenCC==1.4.1" in requirements.splitlines()
    startup = (root / "start_all.sh").read_text()
    assert startup.index("check_fresh_preference_normalizer()") < startup.index("cleanup_port 8000")


@pytest.mark.parametrize("version", [None, "0.1.7", "1.4.0", "1.4.2"])
def test_missing_or_different_converter_cannot_choose_a_fallback(monkeypatch, version):
    record = identity.canonicalize_concept("安静的咖啡厅").as_dict()

    def installed(_name):
        if version is None:
            raise metadata.PackageNotFoundError("OpenCC")
        return version

    monkeypatch.setattr(identity.metadata, "version", installed)
    with pytest.raises(identity.PreferenceTextError) as raised:
        identity.canonicalize_fresh_concept("Coffee Shop")
    assert raised.value.code == ("preference_normalizer_unavailable" if version is None
                                 else "preference_normalizer_version_mismatch")
    # Existing v2 is verified byte-for-byte without running a converter, even
    # if it historically stored Simplified script. No read-time identity change.
    assert identity.stored_concept_identity(record).as_dict() == record
    assert record["semantic_text"] == "安静的咖啡厅"


def test_broken_import_cannot_fall_back_to_ui_translation(monkeypatch):
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "opencc":
            raise ImportError("synthetic converter unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(identity.PreferenceTextError, match="preference_normalizer_unavailable"):
        identity.normalize_fresh_preference_text("安静的咖啡厅")


def test_conversion_failures_are_secret_safe_and_never_return_original(monkeypatch):
    class Broken:
        def convert(self, _value):
            raise RuntimeError("synthetic-private-input-must-not-escape")

    monkeypatch.setattr(identity, "_fresh_preference_converter", lambda: Broken())
    with pytest.raises(identity.PreferenceTextError) as raised:
        identity.normalize_fresh_preference_text("安静的咖啡厅")
    assert str(raised.value) == "preference_normalization_failed"


@pytest.mark.parametrize("count", [40, 41, 499, 500, 501])
@pytest.mark.parametrize("kind", ["chinese", "supplementary", "combining"])
def test_length_counts_raw_unicode_codepoints_not_utf16_or_graphemes(count, kind):
    text = {"chinese": "茶" * count, "supplementary": "🚀" * count,
            "combining": "e\u0301" * (count // 2) + ("e" if count % 2 else "")}[kind]
    assert len(text) == count
    if count > 500:
        with pytest.raises(identity.PreferenceTextError, match="preference_text_too_long"):
            identity.normalize_fresh_preference_text(text)
    else:
        result = identity.normalize_fresh_preference_text(text)
        assert result == identity.normalize_preference_text(text)
        if kind == "supplementary":
            assert len(text.encode("utf-16-le")) // 2 == count * 2


def test_raw_overlimit_whitespace_cannot_be_trimmed_to_pass():
    with pytest.raises(identity.PreferenceTextError, match="preference_text_too_long"):
        identity.normalize_fresh_preference_text(" " + "茶" * 500)


def test_conversion_and_nfkc_expansion_must_also_fit_semantic_bound():
    # Real pinned s2twp converts two codepoints to three, without truncation.
    assert identity.normalize_fresh_preference_text("内存") == "記憶體"
    with pytest.raises(identity.PreferenceTextError, match="preference_text_too_long"):
        identity.normalize_fresh_preference_text("内存" * 4, max_length=10)
    with pytest.raises(identity.PreferenceTextError, match="preference_text_too_long"):
        identity.normalize_fresh_preference_text("\ufb03" * 167)


def test_persisted_semantic_source_never_reconverts(monkeypatch):
    record = identity.canonicalize_concept("安静的咖啡厅").as_dict()

    def forbidden():
        raise AssertionError("stored record must not invoke fresh conversion")

    monkeypatch.setattr(identity, "_fresh_preference_converter", forbidden)
    assert identity.stored_concept_identity(record).as_dict() == record
