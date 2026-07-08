import sys, os, warnings
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
warnings.filterwarnings("ignore")  # 壓掉 google.generativeai FutureWarning 雜訊
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import routers.chat as chat_mod  # patch 前先 import，使 routers.chat 名字存在

@contextmanager
def _app_with_level(level):
    """把 human_chat 會用到的 risk_client / appwrite_srv / save_message /
    track_message_in_buffer / messages_coll 都 mock 掉，yield (client, aw)。"""
    with patch("routers.chat.risk_client") as rc, \
         patch("routers.chat.appwrite_srv") as aw, \
         patch("routers.chat.save_message") as sm, \
         patch("routers.chat.track_message_in_buffer") as tmb, \
         patch("routers.chat.messages_coll") as mc:
        rc.check_risk.return_value = {
            "risk_level": level,
            "intervention_command": {"triggered_by_msg_id": "tbm_1",
                "sender_directive": {"cooldown_seconds": 60 if level == "restricted" else 1800,
                                     "action": "x", "content": None}},
        }
        aw.save_chat_message.return_value = {"$id": "aw1"}
        sm.return_value = {"sender_id": "s1", "content": "MSG", "timestamp": 1.0, "_id": "m1"}
        mc.find.return_value.sort.return_value = []
        app = FastAPI()
        app.include_router(chat_mod.router)
        yield TestClient(app), aw

def test_human_chat_blocked_writes_warning_no_tbm():
    with _app_with_level("blocked") as (client, aw):
        r = client.post("/api/human_chat", json={"sender_id": "s1", "receiver_id": "r1", "message": "bad"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_blocked"] is True
        assert body["warning_msg"]
        kwargs = aw.save_chat_message.call_args.kwargs
        assert kwargs["sender_id"] == "ai_assistant"          # 寫警告氣泡
        assert kwargs.get("triggered_by_msg_id") is None      # blocked 不帶 tbm

def test_human_chat_restricted_delivers_original_with_tbm():
    with _app_with_level("restricted") as (client, aw):
        body = client.post("/api/human_chat", json={"sender_id": "s1", "receiver_id": "r1", "message": "gray"}).json()
        assert body["is_blocked"] is False
        assert body["message"]["content"] == "MSG"            # 原文已投遞（內容來自 save_message 回傳）
        kwargs = aw.save_chat_message.call_args.kwargs
        assert kwargs["sender_id"] == "s1"
        assert kwargs["triggered_by_msg_id"] == "tbm_1"

def test_human_chat_warning_delivers_original_with_tbm():
    with _app_with_level("warning") as (client, aw):
        body = client.post("/api/human_chat", json={"sender_id": "s1", "receiver_id": "r1", "message": "hey"}).json()
        assert body["is_blocked"] is False
        assert aw.save_chat_message.call_args.kwargs["triggered_by_msg_id"] == "tbm_1"

def test_human_chat_safe_no_tbm():
    with _app_with_level("safe") as (client, aw):
        body = client.post("/api/human_chat", json={"sender_id": "s1", "receiver_id": "r1", "message": "yo"}).json()
        assert body["is_blocked"] is False
        assert aw.save_chat_message.call_args.kwargs.get("triggered_by_msg_id") is None