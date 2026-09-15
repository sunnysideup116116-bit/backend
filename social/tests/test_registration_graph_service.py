from unittest.mock import MagicMock, patch

import mongomock
import pytest

from services import registration_graph_service as service
from services import memory_outbox_service as worker


@pytest.fixture
def queue(monkeypatch):
    collection = mongomock.MongoClient().db.outbox
    monkeypatch.setattr(service, "OUTBOX", collection)
    monkeypatch.setattr(worker, "MEMORY_OUTBOX", collection)
    return collection


def test_enqueue_is_idempotent_and_does_not_reset_completed_job(queue):
    assert service.enqueue_registration_bootstrap("account1")
    queue.update_one({}, {"$set": {"status": "applied"}})
    assert service.enqueue_registration_bootstrap("account1")
    assert queue.count_documents({}) == 1
    assert queue.find_one()["status"] == "applied"
    assert "name" not in queue.find_one()


def test_rename_revisions_do_not_collide_on_return_to_old_name(queue):
    for name in ["小宇", "小明", "小宇"]:
        service.enqueue_registration_bootstrap("account1", name_revision=name)
    assert queue.count_documents({"job_kind": "registration_identity"}) == 3


def test_enqueue_failure_does_not_block_registration():
    with patch.object(service.OUTBOX, "update_one", side_effect=RuntimeError("offline")):
        assert service.enqueue_registration_bootstrap("account1") is False


def _projection(allowed=True, version=service.VERSION):
    return MagicMock(json=lambda: {"version": version, "status": "success",
                                  "registration_seed_allowed": allowed})


def test_worker_dispatches_registration_and_persists_prepared_memories(queue):
    service.enqueue_registration_bootstrap("account1")
    memory = {"key": "reading", "label": "閱讀", "stance": "like", "category": "activity",
              "confidence": .99, "evidence_span": "閱讀"}
    with patch.object(service, "read_registration_profile", return_value={"name": "小宇", "interest": "閱讀"}), \
         patch.object(service.requests, "post", return_value=_projection()) as post, \
         patch.object(service, "registration_memories", return_value=[memory]), \
         patch("services.memory_service.apply_profile_memory_proposals") as apply:
        assert worker.process_memory_outbox_once(1) == {"processed": 1, "applied": 1, "failed": 0}
    assert queue.find_one()["prepared_memories"] == [memory]
    assert post.call_args.kwargs["json"]["name"] == "小宇"
    assert apply.call_args.args[2] == "registration_interest"


@pytest.mark.parametrize("allowed,identity", [(False, False), (True, True)])
def test_existing_memory_or_identity_job_never_extracts(queue, allowed, identity):
    service.enqueue_registration_bootstrap("account1", name_revision="小宇" if identity else None)
    with patch.object(service, "read_registration_profile", return_value={"name": "小宇", "interest": "閱讀"}), \
         patch.object(service.requests, "post", return_value=_projection(allowed)), \
         patch.object(service, "registration_memories") as extract:
        result = worker.process_memory_outbox_once(1)
    assert result["applied"] == 1
    extract.assert_not_called()


def test_missing_appwrite_profile_retries_without_creating_ghost_user(queue):
    service.enqueue_registration_bootstrap("account1")
    with patch.object(service, "read_registration_profile", return_value=None), \
         patch.object(service.requests, "post") as post:
        assert worker.process_memory_outbox_once(1)["failed"] == 1
    post.assert_not_called()
    assert queue.find_one()["status"] == "pending"


def test_old_matchmaker_cannot_receive_registration_memory(queue):
    service.enqueue_registration_bootstrap("account1")
    with patch.object(service, "read_registration_profile", return_value={"name": "小宇", "interest": "閱讀"}), \
         patch.object(service.requests, "post", return_value=_projection(version="old")), \
         patch("services.memory_service.apply_profile_memory_proposals") as apply:
        assert worker.process_memory_outbox_once(1)["failed"] == 1
    apply.assert_not_called()


def test_extraction_only_accepts_positive_original_evidence():
    base = {"key": "reading", "label": "閱讀", "stance": "like", "category": "activity",
            "confidence": .99, "evidence_span": "閱讀"}
    with patch("services.profile_skills.analyze_profile_message", return_value={
        "contract": {"memories": []}, "memories": [base, {**base, "stance": "avoid"},
                                                        {**base, "evidence_span": "爬山"}],
    }):
        assert service.registration_memories("閱讀，不喜歡爬高") == [base]


@pytest.mark.parametrize("evidence,interest,expected", [
    ("平常興趣是:吃飯", "吃飯", "吃飯"),
    ("我在註冊表單填寫的平常興趣是:爬山", "爬山和游泳", "爬山"),
    ("咖啡,閱讀", "咖啡，閱讀", "咖啡，閱讀"),
    ("平常興趣是:", "吃飯", ""),
    ("不存在的游泳", "爬山", ""),
])
def test_framed_evidence_retains_only_owner_form_text(evidence, interest, expected):
    assert service.registration_evidence(evidence, interest) == expected


def test_provider_failure_is_not_successful_empty_extraction():
    with patch("services.profile_skills.analyze_profile_message", return_value={
        "contract": {}, "recent_context": {"reason_code": "model_Timeout"}, "memories": [],
    }), pytest.raises(RuntimeError, match="extraction_unavailable"):
        service.registration_memories("閱讀")


def test_changed_source_does_not_replay_stale_prepared_preferences(queue):
    service.enqueue_registration_bootstrap("account1")
    queue.update_one({}, {"$set": {"source_hash": service._digest("舊興趣"), "prepared_memories": []}})
    with patch.object(service, "read_registration_profile", return_value={"name": "小宇", "interest": "新興趣"}), \
         patch.object(service.requests, "post", return_value=_projection()), \
         patch("services.memory_service.apply_profile_memory_proposals") as apply:
        assert worker.process_memory_outbox_once(1)["failed"] == 1
    apply.assert_not_called()
