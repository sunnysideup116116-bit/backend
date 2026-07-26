from types import SimpleNamespace

from models import BigFiveProfileInitRequest, RelationshipGameRequest
from routers import chat, system


class ProfileCollection:
    def __init__(self):
        self.updates = []

    def update_one(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        return SimpleNamespace(modified_count=1, upserted_id="profile-1")


class MatchCollection:
    def __init__(self, match_doc):
        self.match_doc = match_doc

    def find_one(self, *_args, **_kwargs):
        return self.match_doc


def test_big_five_profile_is_created_during_registration(monkeypatch):
    profiles = ProfileCollection()
    monkeypatch.setattr(system, "profiles_coll", profiles)

    response = system.initialize_big_five_profile(
        BigFiveProfileInitRequest(
            user_id="new-user",
            initial_interest="看電影",
        )
    )

    assert response["status"] == "success"
    args, kwargs = profiles.updates[0]
    assert args[0] == {"user_id": "new-user"}
    assert kwargs["upsert"] is True
    assert args[1]["$setOnInsert"]["temp_big_five"] == {}
    assert args[1]["$setOnInsert"]["interaction_count"] == 0
    assert args[1]["$setOnInsert"]["onboarding_completed"] is False
    assert args[1]["$set"]["initial_interest"] == "看電影"

def test_big_five_answer_templates_are_limited_to_two():
    templates = chat.normalize_answer_templates(
        {
            "is_complete": False,
            "answer_templates": ["先找熟人一起聊", "主動認識新朋友", "第三個答案"],
        }
    )

    assert templates == ["先找熟人一起聊", "主動認識新朋友"]


def test_reopening_todays_topic_sends_it_to_shared_chat(monkeypatch):
    topic = "你們都喜歡散步，可以聊聊最喜歡哪條路線。"
    match_doc = {
        "_id": "match-1",
        "status": "accepted",
        "from_user": "user-a",
        "to_user": "user-b",
        "relationship_games": {
            "compatibility_quiz": {"status": "completed"},
            "topic_box": {
                "drawn_date": __import__("time").strftime("%Y-%m-%d"),
                "topic": topic,
                "source": "quiz_overlap",
            },
        },
    }
    saved = []
    monkeypatch.setattr(chat, "matches_coll", MatchCollection(match_doc))
    monkeypatch.setattr(chat, "find_accepted_match", lambda *_args: match_doc)
    monkeypatch.setattr(
        chat,
        "save_message",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )

    response = chat.draw_relationship_topic(
        RelationshipGameRequest(user_id="user-a", other_id="user-b")
    )

    assert response["status"] == "already_drawn"
    assert response["topic"] == topic
    assert saved[0][0][0] == "user-a_user-b"
    assert saved[0][0][2] == topic
    assert saved[0][1]["metadata"]["event_type"] == "topic_box"
