from services import ai_service


def test_date_orchestration_returns_structured_form(monkeypatch):
    monkeypatch.setattr(
        ai_service,
        "generate_chat_completion",
        lambda *_args, **_kwargs: """
        {
          "reply": "週六晚上看電影如何？",
          "show_form": true,
          "form": {
            "date": "2026-08-01",
            "time": "19:00",
            "activity": "看電影",
            "budget": "500"
          }
        }
        """,
    )

    result = ai_service.orchestrate_date_coordination(
        "週六晚上可以",
        {"form": {}},
        {"viewer": {"user_id": "user_a"}, "partner": {"user_id": "user_b"}},
    )

    assert result["show_form"] is True
    assert result["form"]["activity"] == "看電影"
