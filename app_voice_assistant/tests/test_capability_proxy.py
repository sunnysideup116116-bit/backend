import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from app_voice_assistant.capability_proxy import (
    ACTION_ALIASES,
    ACTION_LABELS,
    CapabilityRefError,
    CapabilityRefSigner,
    MAX_CAPABILITY_MATCHES,
    find_capabilities,
    proposal_from_capability,
)
from app_voice_assistant.capabilities import ACTIONS, ARGUMENT_SCHEMAS, CATALOG
from app_voice_assistant.duplex_session import _live_tools, _system_instruction


class Types:
    class FunctionDeclaration:
        def __init__(self, **values):
            self.__dict__.update(values)

    class Tool:
        def __init__(self, **values):
            self.__dict__.update(values)


def context(**changes):
    base = {
        "scope": "global",
        "revision": 7,
        "can_publish": True,
        "permissions": {
            "navigation": True,
            "post_draft": True,
            "post_publish": True,
            "gallery": True,
            "screen_read": True,
        },
    }
    base.update(changes)
    return base


def test_proxy_exposes_exactly_seven_stable_tools_and_compact_prompt():
    declarations = _live_tools(Types, "proxy")[0].function_declarations
    assert [item.name for item in declarations] == [
        "find_app_capabilities",
        "run_app_capabilities",
        "describe_current_screen",
        "read_tasks",
        "cancel_task",
        "resolve_pending_interaction",
        "close_voice_mode",
    ]
    assert len(_system_instruction({}, "", "proxy").encode("utf-8")) <= 6000


def test_find_explain_returns_verified_guide_without_executable_ref():
    result = find_capabilities(
        "貼文要怎麼發布", "explain", context=context(),
        signer=CapabilityRefSigner(b"secret"), user_id="u1", session_id="s1",
    )
    assert result["status"] == "ok"
    assert result["matches"][0]["guide_id"] == "posts"
    assert result["matches"][0]["steps"]
    assert all(
        "capability_ref" not in action
        for match in result["matches"] for action in match["actions"]
    )


def test_perform_mode_with_ambiguous_domain_never_issues_a_ref():
    for query in ("我想處理貼文", "個人資料", "行事曆"):
        result = find_capabilities(
            query, "perform", context=context(),
            signer=CapabilityRefSigner(b"secret"), user_id="u1", session_id="s1",
        )
        assert result["status"] == "needs_clarification"
        assert not any(
            "capability_ref" in action
            for match in result["matches"] for action in match["actions"]
        )


def test_unavailable_action_is_explained_without_executable_ref():
    result = find_capabilities(
        "發布貼文", "perform", context=context(can_publish=False),
        signer=CapabilityRefSigner(b"secret"), user_id="u1", session_id="s1",
    )
    action = next(
        action
        for match in result["matches"] for action in match["actions"]
        if action["capability_id"] == "post.request_publish"
    )
    assert action["available"] is False
    assert action["unavailable_reason"] == "post_media_not_ready"
    assert "capability_ref" not in action


def test_explicit_multi_intent_search_can_issue_multiple_scoped_refs():
    result = find_capabilities(
        "打開行事曆再查天氣", "perform", context=context(),
        signer=CapabilityRefSigner(b"secret"), user_id="u1", session_id="s1",
    )
    executable = {
        action["capability_id"]
        for match in result["matches"] for action in match["actions"]
        if "capability_ref" in action
    }
    assert {"app.navigate", "weather.query"} <= executable


def test_read_capabilities_include_server_derived_suggested_arguments():
    signer = CapabilityRefSigner(b"secret")
    cases = (
        ("幫我查目前的配對進度", "match.query", {"view": "status"}),
        (
            "幫我查今天行事曆",
            "calendar.query",
            {"source": "all", "range": "today"},
        ),
        ("幫我查台北現在天氣", "weather.query", {"location": "台北"}),
    )
    permitted = context(permissions={
        "match_read": True, "calendar_read": True, "chat_list": True,
    })
    for query, action_id, expected in cases:
        result = find_capabilities(
            query, "perform", context=permitted, signer=signer,
            user_id="u1", session_id="s1",
        )
        action = next(
            action
            for match in result["matches"]
            for action in match["actions"]
            if action["capability_id"] == action_id
        )
        assert action["capability_ref"]
        assert action["suggested_arguments"] == expected


def test_common_new_match_and_navigation_phrases_are_executable():
    signer = CapabilityRefSigner(b"secret")
    permitted = context(permissions={
        "navigation": True, "match_ayue": True, "match_read": True,
        "match_actions": True,
    })
    cases = (
        (
            "幫我找新的配對",
            "match.ayue_query",
            {"question": "幫我找新的配對"},
        ),
        ("跳到配對頁面", "app.navigate", {"destination": "matching"}),
        ("跳到行事曆", "app.navigate", {"destination": "calendar"}),
    )
    for query, action_id, arguments in cases:
        result = find_capabilities(
            query, "perform", context=permitted, signer=signer,
            user_id="u1", session_id="s1",
        )
        action = next(
            action
            for match in result["matches"]
            for action in match["actions"]
            if action["capability_id"] == action_id
        )
        assert action["capability_ref"]
        assert action["suggested_arguments"] == arguments
        if action_id == "match.ayue_query":
            assert action["confirmation_required"] is True
        assert result["recommended_operations"] == [{
            "operation_key": "op1",
            "capability_ref": action["capability_ref"],
            "arguments": arguments,
        }]
        assert result["next_step"].startswith("Call run_app_capabilities")

    for query in (
        "尋找新配對", "開始新一輪配對搜尋", "請幫我媒合新對象",
        "我要重新找對象", "幫我配對",
    ):
        result = find_capabilities(
            query, "perform", context=permitted, signer=signer,
            user_id="u1", session_id="s1",
        )
        action = next(
            action
            for match in result["matches"]
            for action in match["actions"]
            if action["capability_id"] == "match.ayue_query"
        )
        assert action["capability_ref"], query
        assert action["suggested_arguments"] == {"question": query}

    for query in (
        "find a new match", "start a new match search", "find me a new partner",
    ):
        result = find_capabilities(
            query, "perform", context=permitted, signer=signer,
            user_id="u1", session_id="s1",
        )
        assert result["status"] == "ok"
        assert result["recommended_operations"][0]["arguments"] == {
            "question": query,
        }

    for query, destination in (
        ("帶我到配對主頁", "matching"),
        ("切換到行事曆頁面", "calendar"),
        ("進入聊天頁面", "chat"),
    ):
        result = find_capabilities(
            query, "perform", context=permitted, signer=signer,
            user_id="u1", session_id="s1",
        )
        action = next(
            action
            for match in result["matches"]
            for action in match["actions"]
            if action["capability_id"] == "app.navigate"
        )
        assert action["capability_ref"], query
        assert action["suggested_arguments"] == {"destination": destination}

    for query, destination in (
        ("open calendar", "calendar"),
        ("navigate to the matching page", "matching"),
    ):
        result = find_capabilities(
            query, "perform", context=permitted, signer=signer,
            user_id="u1", session_id="s1",
        )
        assert result["status"] == "ok"
        operation = result["recommended_operations"][0]
        assert operation["arguments"] == {"destination": destination}


def test_perform_result_is_compact_and_execution_first():
    result = find_capabilities(
        "幫我找新的配對", "perform",
        context=context(permissions={
            "navigation": True, "match_ayue": True, "match_read": True,
            "match_actions": True,
        }),
        signer=CapabilityRefSigner(b"secret"), user_id="u1", session_id="s1",
    )
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= 5000
    assert len(result["matches"]) <= MAX_CAPABILITY_MATCHES
    assert result["recommended_operations"][0]["arguments"] == {
        "question": "幫我找新的配對",
    }


def test_starting_match_without_action_permission_returns_repair_not_a_ref():
    result = find_capabilities(
        "幫我找新的配對", "perform",
        context=context(permissions={
            "match_ayue": True, "match_read": True, "match_actions": False,
        }),
        signer=CapabilityRefSigner(b"secret"), user_id="u1", session_id="s1",
    )
    assert result["status"] == "permission_denied"
    assert "配對操作" in result["message"]
    assert result["permission_repair"]["missing_permissions"] == [
        "match_actions",
    ]
    assert not any(
        action.get("capability_ref")
        for match in result["matches"] for action in match["actions"]
    )


def test_chat_places_and_calendar_source_queries_return_one_exact_operation():
    signer = CapabilityRefSigner(b"secret")
    permissions = {
        "navigation": True,
        "screen_read": True,
        "chat_list": True,
        "chat_content": True,
        "private_ayue": True,
        "places": True,
        "public_ayue": True,
        "match_read": True,
        "match_actions": True,
        "match_ayue": True,
        "calendar_read": True,
    }
    base = context(permissions=permissions)
    chat = context(
        scope="chat",
        revision=4,
        permissions=permissions,
        screen={
            "surface_id": "surface-1",
            "ready": True,
            "selected_ref": "surface-1-4-1",
            "available_actions": ["chat.open", "ayue.private_query"],
            "items": [{
                "ref": "surface-1-4-1",
                "kind": "contact",
                "label": "小美",
                "actions": ["chat.open", "ayue.private_query"],
            }],
        },
    )
    cases = (
        ("開啟聊天室", base, "app.navigate", {"destination": "chat"}),
        ("幫我開聊天室", base, "app.navigate", {"destination": "chat"}),
        ("開啟和小美的聊天室", base, "chat.open", {"contact_name": "小美"}),
        ("和小美的聊天室", base, "chat.open", {"contact_name": "小美"}),
        (
            "讀取裡面的內容",
            chat,
            "ayue.private_query",
            {
                "contact_name": "小美",
                "question": "讀取裡面的內容",
                "target_ref": "surface-1-4-1",
            },
        ),
        (
            "讀取「小美」的聊天內容",
            base,
            "ayue.private_query",
            {
                "contact_name": "小美",
                "question": "讀取「小美」的聊天內容",
            },
        ),
        (
            "高雄哪裡有好玩的",
            base,
            "ayue.public_query",
            {"domain": "places", "question": "高雄哪裡有好玩的"},
        ),
        ("幫我找配對", base, "match.ayue_query", {"question": "幫我找配對"}),
        ("新的配對", base, "match.ayue_query", {"question": "新的配對"}),
        (
            "想找人一起去上海玩",
            base,
            "match.ayue_query",
            {"question": "想找人一起去上海玩"},
        ),
        (
            "查 Google 日曆接下來一個月",
            base,
            "calendar.query",
            {"source": "google", "range": "upcoming"},
        ),
        (
            "查自己的行事曆",
            base,
            "calendar.query",
            {"source": "personal", "range": "upcoming"},
        ),
        (
            "查 personal 行事曆",
            base,
            "calendar.query",
            {"source": "personal", "range": "upcoming"},
        ),
        (
            "查我和小美的共同約會",
            base,
            "date.query",
            {"contact_name": "小美"},
        ),
    )
    for phrase, current_context, action_id, arguments in cases:
        result = find_capabilities(
            phrase,
            "perform",
            context=current_context,
            signer=signer,
            user_id="u1",
            session_id="s1",
        )
        assert result["status"] == "ok", (phrase, result)
        assert len(result["recommended_operations"]) == 1, phrase
        operation = result["recommended_operations"][0]
        assert operation["arguments"] == arguments, phrase
        action = next(
            action
            for match in result["matches"]
            for action in match["actions"]
            if action.get("capability_ref")
        )
        assert action["capability_id"] == action_id, phrase


def test_google_calendar_writes_never_fall_through_to_personal_calendar():
    result = find_capabilities(
        "修改 Google 日曆的會議",
        "perform",
        context=context(permissions={
            "calendar_read": True,
            "calendar_write": True,
        }),
        signer=CapabilityRefSigner(b"secret"),
        user_id="u1",
        session_id="s1",
    )
    assert result["status"] == "not_supported"
    assert "Google 日曆" in result["message"]
    assert "只能查詢" in result["message"]
    assert "recommended_operations" not in result


def test_catalog_is_the_authority_for_proxy_execution_metadata():
    assert all(
        action["title"] == ACTION_LABELS[action_id]
        and action["risk"] in {"read", "write", "control"}
        and action["execution_kind"] in {
            "inline_server", "inline_device", "background_server", "background_device",
        }
        for action_id, action in ACTIONS.items()
    )
    assert set(ARGUMENT_SCHEMAS) == set(ACTIONS)
    for schema in ARGUMENT_SCHEMAS.values():
        Draft202012Validator.check_schema(schema)


def test_perform_ref_is_forgery_owner_session_context_and_expiry_bound(monkeypatch):
    signer = CapabilityRefSigner(b"secret")
    result = find_capabilities(
        "發布貼文", "perform", context=context(), signer=signer,
        user_id="u1", session_id="s1",
    )
    action = next(
        action
        for match in result["matches"]
        for action in match["actions"]
        if action["capability_id"] == "post.request_publish"
    )
    ref = action["capability_ref"]
    assert action["arguments_schema"] == ARGUMENT_SCHEMAS["post.request_publish"]
    assert signer.verify(ref, user_id="u1", session_id="s1", context=context()) == "post.request_publish"
    with pytest.raises(CapabilityRefError, match="owner_mismatch"):
        signer.verify(ref, user_id="u2", session_id="s1", context=context())
    with pytest.raises(CapabilityRefError, match="owner_mismatch"):
        signer.verify(ref, user_id="u1", session_id="other", context=context())
    with pytest.raises(CapabilityRefError, match="context_stale"):
        signer.verify(ref, user_id="u1", session_id="s1", context=context(revision=8))
    with pytest.raises(CapabilityRefError, match="context_stale"):
        signer.verify(
            ref, user_id="u1", session_id="s1",
            context=context(permissions={"post_publish": False}),
        )
    forged = f"{ref[:-1]}{'A' if ref[-1] != 'A' else 'B'}"
    with pytest.raises(CapabilityRefError, match="invalid"):
        signer.verify(forged, user_id="u1", session_id="s1", context=context())

    timed = signer.issue(
        "post.request_publish", user_id="u1", session_id="s1",
        context=context(), now=100,
    )
    with pytest.raises(CapabilityRefError, match="expired"):
        signer.verify(
            timed, user_id="u1", session_id="s1", context=context(), now=221,
        )

    monkeypatch.setitem(CATALOG, "version", int(CATALOG["version"]) + 1)
    with pytest.raises(CapabilityRefError, match="catalog_stale"):
        signer.verify(ref, user_id="u1", session_id="s1", context=context())


def test_proxy_arguments_still_use_canonical_proposal_validation():
    proposal = proposal_from_capability(
        "post.select_recent_photos", {"count": 3}, revision=4,
    )
    assert proposal is not None
    assert proposal.intent == "post.select_recent_photos"
    assert proposal.arguments == {"count": 3}
    assert proposal_from_capability(
        "post.select_recent_photos", {"count": 99}, revision=4,
    ) is None


def test_every_catalog_action_has_five_top_ranked_parity_phrases():
    signer = CapabilityRefSigner(b"secret")
    for action_id, label in ACTION_LABELS.items():
        phrases = [
            label,
            f"幫我{label}",
            f"我要{label}",
            f"請{label}",
            f"可以{label}嗎",
        ]
        for phrase in phrases:
            result = find_capabilities(
                phrase, "explain", context=context(), signer=signer,
                user_id="u1", session_id="s1",
            )
            top_action = result["matches"][0]["actions"][0]["capability_id"]
            assert top_action == action_id, (action_id, phrase, result["matches"])
    assert proposal_from_capability(
        "post.select_recent_photos", {"count": 3, "user_id": "forged"}, revision=4,
    ) is None


def test_catalog_alias_top_one_accuracy_is_at_least_ninety_five_percent():
    signer = CapabilityRefSigner(b"secret")
    outcomes = []
    for action_id, aliases in ACTION_ALIASES.items():
        for phrase in aliases:
            result = find_capabilities(
                phrase, "explain", context=context(), signer=signer,
                user_id="u1", session_id="s1",
            )
            top = result["matches"][0]["actions"][0]["capability_id"]
            outcomes.append(top == action_id)
    assert len(outcomes) >= 100
    assert sum(outcomes) / len(outcomes) >= 0.95
