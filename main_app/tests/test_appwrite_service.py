import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from unittest.mock import MagicMock

def _svc():
    # AppwriteService.__init__ 只建立 Client/Databases 物件（不發網路請求），
    # 這裡把 databases 換成 MagicMock 後即可純單元測試 save_chat_message。
    from services.appwrite_service import AppwriteService
    svc = AppwriteService()
    svc.databases = MagicMock()
    return svc

def test_save_chat_message_with_tbm():
    svc = _svc()
    svc.databases.create_document.return_value = {"$id": "d1"}
    svc.save_chat_message("s1", "r1", "room1", "hi", triggered_by_msg_id="tbm_9")
    data = svc.databases.create_document.call_args.kwargs["data"]
    assert data["triggered_by_msg_id"] == "tbm_9"

def test_save_chat_message_without_tbm_omits_field():
    svc = _svc()
    svc.databases.create_document.return_value = {"$id": "d2"}
    svc.save_chat_message("s1", "r1", "room1", "hi")
    data = svc.databases.create_document.call_args.kwargs["data"]
    assert "triggered_by_msg_id" not in data