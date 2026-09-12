from google.genai import types
from app_voice_assistant.language import input_language_codes, display_transcript
from app_voice_assistant.contracts import safe_context
from app_voice_assistant.duplex_session import _system_instruction


def test_language_preferences_are_bounded_and_independent_of_reply_language():
    context = safe_context({"voice_config": {"input_language": "en-US", "response_language": "zh-TW", "self_name": "candy"}})
    assert context["voice_config"]["input_language"] == "en-US"
    assert context["voice_config"]["response_language"] == "zh-TW"
    assert input_language_codes("zh-en") == ["zh-TW", "en-US"]
    assert input_language_codes("zh-TW") == ["zh-TW"]
    assert input_language_codes("en-US") == ["en-US"]
    assert safe_context({"voice_config": {"input_language": "untrusted"}})["voice_config"]["input_language"] == "zh-en"
    config = types.AudioTranscriptionConfig(language_hints=types.LanguageHints(language_codes=input_language_codes("zh-en")))
    assert config.language_auto is None
    assert config.language_hints.language_codes == ["zh-TW", "en-US"]
    instruction = _system_instruction(context["voice_config"])
    assert "candy" in instruction
    assert "主要說英文" in instruction
    assert "台灣繁體中文" in instruction


def test_display_conversion_preserves_english_spacing_and_translates_script_only():
    assert display_transcript("我喜欢和 candy 打篮球。") == "我喜歡和 candy 打籃球。"
    assert display_transcript("Hello candy, nice to meet you.") == "Hello candy, nice to meet you."
    assert display_transcript("我喜欢篮球", traditional=False) == "我喜欢篮球"
