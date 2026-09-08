import json
from unittest.mock import patch

from services.ayue_agent.v3.date_coordination_references import date_coordination_summary
from services.ayue_agent.v3.scheduler import _server_owned_date_coordination_reply
from services.ayue_agent.v3.contracts import SubTaskResult, SubTaskStatus


def _match(index: int, status: str = "pending_partner") -> dict:
    return {
        "_id": f"date-match-{index}",
        "from_user": "owner",
        "to_user": f"other-{index}",
        "status": "accepted",
        "updated_at": float(index),
        "date_coordination": {
            "coordination_id": f"coord-{index}",
            "status": status,
            "revision": index + 1,
        },
    }


def test_date_coordination_summary_keeps_public_projection_bounded_and_private_ids_separate():
    rows = [_match(index) for index in range(4)]
    with patch(
        "services.ayue_agent.v3.date_coordination_references.matches_coll.find",
        return_value=rows,
    ), patch(
        "services.ayue_agent.v3.date_coordination_references.display_name",
        side_effect=lambda user_id: f"名字-{user_id}",
    ):
        public, authority = date_coordination_summary("owner")

    assert public["count"] == 4
    assert public["truncated"] is True
    assert len(public["cards"]) == 3
    assert public["cards"][0] == {
        "display_name": "名字-other-3",
        "status": "pending_partner",
        "allowed_actions": ["cancel"],
    }
    assert "date-match-3" not in json.dumps(public, ensure_ascii=False)
    assert authority[0]["match_id"] == "date-match-3"
    assert authority[0]["coordination_id"] == "coord-3"


def test_date_preflight_rejection_is_server_owned_and_not_rewritten_by_synthesizer():
    result = SubTaskResult(
        task_id="relationship",
        status=SubTaskStatus.FAILED,
        tool_name="relationship.start_date_coordination",
        error_code="preflight_rejected",
        observation={"preview": "你和小宇已有進行中的約會安排，我不會再建立新的卡片。"},
    )
    assert _server_owned_date_coordination_reply({"relationship": [result]}) == (
        "你和小宇已有進行中的約會安排，我不會再建立新的卡片。"
    )
