from google.genai import types
from app_voice_assistant.language import input_language_codes, display_transcript
from app_voice_assistant.contracts import safe_context
from app_voice_assistant.duplex_session import _system_instruction
from app_voice_assistant.duplex_runtime import session_started_prompt
from app_voice_assistant.provider import response_language_label


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
    english_instruction = _system_instruction(
        {"input_language": "en-US", "response_language": "en-US"},
    )
    assert "response_language=en-US" in english_instruction
    assert "不可因為控制訊息" in english_instruction


def test_initial_live_control_message_follows_response_language():
    english = session_started_prompt({
        "voice_config": {"response_language": "en-US", "self_name": "Sunny"},
    })
    assert "response_language=en-US" in english
    assert "Voice mode is ready" in english
    assert "語音模式已就緒" not in english
    assert "Sunny" in english

    simplified = session_started_prompt({
        "voice_config": {"response_language": "zh-CN"},
    })
    assert "语音模式已就绪" in simplified
    assert response_language_label("en-US") == "English"
    assert response_language_label("untrusted") == "台灣繁體中文"


def test_display_conversion_preserves_english_spacing_and_translates_script_only():
    assert display_transcript("我喜欢和 candy 打篮球。") == "我喜歡和 candy 打籃球。"
    assert display_transcript("Hello candy, nice to meet you.") == "Hello candy, nice to meet you."
    assert display_transcript("我喜欢篮球", traditional=False) == "我喜欢篮球"


def test_display_conversion_removes_only_streamed_cjk_token_spaces():
    assert display_transcript("你 可 以 幫 我 什 麼 忙 啊？") == "你可以幫我什麼忙啊？"
    assert (
        display_transcript("嗨 Candy， 我 是 阿 月， 今 天 想 請 我 幫 什 麼？")
        == "嗨 Candy，我是阿月，今天想請我幫什麼？"
    )
    assert display_transcript("下午 4 點和 Candy 見面") == "下午 4 點和 Candy 見面"
