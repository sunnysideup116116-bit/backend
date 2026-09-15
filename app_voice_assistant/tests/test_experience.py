import pytest

from app_voice_assistant.capability_proxy import (
    CapabilityRefSigner,
    find_capabilities,
    proposal_from_capability,
)
from app_voice_assistant.experience import expand_catalog_operations


def context(**changes):
    base = {
        "scope": "global",
        "revision": 2,
        "can_publish": False,
        "permissions": {
            "navigation": True,
            "screen_read": True,
            "profile": True,
            "status_read": True,
            "match_read": True,
            "calendar_read": True,
            "calendar_write": True,
            "chat_list": True,
            "post_draft": True,
            "gallery": True,
            "post_publish": True,
        },
        "voice_config": {},
    }
    base.update(changes)
    return base


def test_explain_mode_projects_coach_and_permission_repair():
    result = find_capabilities(
        "怎麼發布貼文", "explain",
        context=context(permissions={"status_read": True}),
        signer=CapabilityRefSigner(b"secret"), user_id="u1", session_id="s1",
    )
    assert result["coach"]["guide_id"] == "posts"
    assert result["coach"]["steps"]
    assert result["permission_repair"]["destination"] == "voice_settings"
    assert "post_publish" in result["permission_repair"]["missing_permissions"]


def test_saved_routine_name_is_dynamically_discoverable_and_context_bound():
    signer = CapabilityRefSigner(b"secret")
    current = context(voice_config={
        "routines": [{"name": "早安小幫手", "template_id": "daily_briefing"}],
    })
    result = find_capabilities(
        "執行早安小幫手", "perform", context=current,
        signer=signer, user_id="u1", session_id="s1",
    )
    action = next(
        action
        for match in result["matches"]
        for action in match["actions"]
        if action.get("suggested_arguments") == {"name": "早安小幫手"}
    )
    assert action["capability_id"] == "routine.run"
    ref = action["capability_ref"]
    assert signer.verify(
        ref, user_id="u1", session_id="s1", context=current,
    ) == "routine.run"
    with pytest.raises(Exception, match="context_stale"):
        signer.verify(
            ref, user_id="u1", session_id="s1",
            context=context(voice_config={}),
        )


def test_fixed_workflow_expands_to_allowlisted_ordered_operations():
    expanded = expand_catalog_operations([{
        "operation_key": "post",
        "depends_on": [],
        "action_id": "workflow.prepare_post",
        "arguments": {"caption": "今天很開心", "count": 2},
    }])
    assert [item["action_id"] for item in expanded] == [
        "post.open_draft", "post.select_recent_photos", "post.request_publish",
    ]
    assert expanded[0]["arguments"] == {"caption": "今天很開心"}
    assert expanded[1]["arguments"] == {"count": 2}
    assert expanded[1]["depends_on"] == [expanded[0]["operation_key"]]
    assert expanded[2]["depends_on"] == [expanded[1]["operation_key"]]


def test_workflow_external_dependency_targets_terminal_step():
    expanded = expand_catalog_operations([
        {
            "operation_key": "profile",
            "depends_on": [],
            "action_id": "workflow.update_profile",
            "arguments": {"changes": {"age": 25}},
        },
        {
            "operation_key": "after",
            "depends_on": ["profile"],
            "action_id": "self.query",
            "arguments": {"detail": "summary"},
        },
    ])
    assert expanded[-1]["depends_on"] == [expanded[1]["operation_key"]]


def test_new_capability_arguments_are_canonically_validated():
    search = proposal_from_capability(
        "app.search",
        {"query": "咖啡", "domains": ["memory", "calendar"]},
        revision=3,
    )
    assert search is not None
    assert search.arguments["domains"] == ["memory", "calendar"]
    assert proposal_from_capability(
        "app.search",
        {"query": "咖啡", "domains": ["private_messages"]},
        revision=3,
    ) is None
    routine = proposal_from_capability(
        "routine.save",
        {
            "name": "我的記憶搜尋",
            "template_id": "search_memory",
            "arguments": {"query": "咖啡"},
        },
        revision=3,
    )
    assert routine is not None
    assert proposal_from_capability(
        "routine.save",
        {
            "name": "危險捷徑",
            "template_id": "send_message",
            "arguments": {},
        },
        revision=3,
    ) is None
