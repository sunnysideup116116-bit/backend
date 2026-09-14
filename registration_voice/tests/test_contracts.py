from registration_voice.contracts import (
    redact_sensitive_transcript,
    safe_form,
    validate_function_patch,
)


def test_voice_patch_keeps_supported_fields_and_rejects_underage_password():
    decision = validate_function_patch(
        {
            "base_revision": 4,
            "nickname": " 小歌 ",
            "email": "123@GMAIL.COM",
            "gender": "male",
            "phone": "0912-555-441",
            "age": 16,
            "region": "臺東卑南鄉",
            "interest": "吃稀飯、醬油膏，和朋友打球",
            "password": "must-never-leave-the-model-boundary",
            "warning_codes": ["password_spoken"],
        },
        current_revision=4,
    )

    assert decision.changes == {
        "nickname": "小歌",
        "email": "123@gmail.com",
        "gender": "male",
        "phone": "0912555441",
        "region": "台東縣",
        "interest": "吃稀飯、醬油膏，和朋友打球",
    }
    assert {item["code"] for item in decision.rejected} == {"age_out_of_range"}
    assert "password_spoken" in decision.warnings
    assert "district_not_stored" in decision.warnings
    assert "password" not in decision.changes


def test_stale_patch_is_never_applied():
    decision = validate_function_patch(
        {"base_revision": 2, "age": 25},
        current_revision=3,
    )

    assert decision.stale is True
    assert decision.changes == {}
    assert decision.warnings == ("stale_revision",)


def test_safe_form_drops_password_and_unknown_fields():
    form = safe_form({
        "nickname": "小歌",
        "age": "25",
        "password": "secret",
        "unknown": "value",
    })

    assert form["nickname"] == "小歌"
    assert form["age"] == 25
    assert "password" not in form
    assert "unknown" not in form


def test_spoken_password_is_redacted_before_transcript_reaches_flutter():
    transcript = redact_sensitive_transcript(
        "我叫小歌，密碼是12345678，信箱是123@gmail.com。Password is another-secret."
    )

    assert "12345678" not in transcript
    assert "another-secret" not in transcript
    assert "密碼是[已隱藏]" in transcript
    assert "Password is [hidden]" in transcript
    assert "123@gmail.com" in transcript


def test_unsupported_language_warning_is_preserved_without_form_changes():
    decision = validate_function_patch(
        {"base_revision": 0, "warning_codes": ["unsupported_language"]},
        current_revision=0,
    )

    assert decision.changes == {}
    assert decision.warnings == ("unsupported_language",)
