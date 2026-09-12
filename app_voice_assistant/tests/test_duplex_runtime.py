import asyncio
import json
from types import SimpleNamespace

from app_voice_assistant.duplex_runtime import _proposal_from_function, run_duplex_session


def message(*, tool_calls=None, content=None, cancellation=None, go_away=None):
    return SimpleNamespace(
        go_away=go_away,
        voice_activity=None,
        server_content=content,
        tool_call=(
            SimpleNamespace(function_calls=tool_calls)
            if tool_calls is not None
            else None
        ),
        tool_call_cancellation=cancellation,
        session_resumption_update=None,
    )


class FakeLive:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.tool_responses = []
        self.audio = []
        self.text = []
        self.closed = False
        self.reconnect_count = 0

    async def connect(self):
        return False

    async def reconnect(self):
        self.reconnect_count += 1
        return True

    async def receive_turn(self):
        yield await self.incoming.get()

    async def send_audio(self, data):
        self.audio.append(data)

    async def send_text(self, text):
        self.text.append(text)

    async def send_tool_response(self, *, call_id, name, response):
        self.tool_responses.append((call_id, name, response))

    async def close(self):
        self.closed = True


class FakeProvider:
    def __init__(self, live):
        self.live = live

    def create_duplex_session(self, **_options):
        return self.live


class FakeLimiter:
    def allow_tts(self, identity):
        return True


class FakeWebSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()

    async def receive(self):
        return await self.incoming.get()

    async def close(self, code=1000):
        return None


async def wait_until(predicate, timeout=1):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition")
        await asyncio.sleep(0)


def test_duplex_confirmation_calls_the_app_once_and_returns_tool_result():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={"scope": "settings", "revision": 0},
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))

        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="setting-call",
            name="set_app_setting",
            args={
                "key": "notifications.global",
                "enabled": False,
            },
        )]))
        await wait_until(lambda: any(
            item.get("type") == "confirmation_required" for item in events
        ))
        assert live.tool_responses[-1][2]["status"] == "confirmation_required"

        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="repeated-setting-call",
            name="set_app_setting",
            args={"key": "notifications.global", "enabled": False},
        )]))
        await wait_until(lambda: any(
            response[2].get("status") == "awaiting_confirmation"
            for response in live.tool_responses
        ))
        assert len([
            item for item in events if item.get("type") == "confirmation_required"
        ]) == 1

        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="confirm-call",
            name="confirm_pending_action",
            args={"spoken_phrase": "確認"},
        )]))
        await wait_until(lambda: any(
            item.get("type") == "action_proposal" for item in events
        ))
        actions = [item for item in events if item.get("type") == "action_proposal"]
        assert len(actions) == 1
        assert actions[0]["arguments"] == {
            "key": "notifications.global", "enabled": False,
        }

        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "type": "action_result",
                "action_id": actions[0]["action_id"],
                "success": True,
                "message": "訊息通知已關閉。",
            }),
        })
        await wait_until(lambda: any(
            response[2].get("status") == "success"
            for response in live.tool_responses
        ))
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task
        assert live.closed is True

    asyncio.run(scenario())


def test_duplex_contact_list_reads_app_result_without_guessing_names():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "revision": 0,
                "permissions": {"chat_list": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))

        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="contacts-call",
            name="list_contacts",
            args={},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "contacts.query" for item in events
        ))
        action = next(
            item for item in events if item.get("intent") == "contacts.query"
        )
        assert action["arguments"] == {}

        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "type": "action_result",
                "action_id": action["action_id"],
                "success": True,
                "message": "目前可以聊天的配對對象有：a、小美。",
            }),
        })
        await wait_until(lambda: any(
            response[0] == "contacts-call"
            and response[2].get("status") == "success"
            for response in live.tool_responses
        ))
        response = next(
            response for response in live.tool_responses
            if response[0] == "contacts-call"
        )
        assert "a、小美" in response[2]["message"]

        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_visible_confirmation_reroutes_a_wrong_message_tool_to_the_button():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "ayue_private",
                "revision": 4,
                "permissions": {"chat_send": True},
                "feature_status": {"visible_choice_pending": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        transcript = SimpleNamespace(text="確認", finished=True)
        await live.incoming.put(message(
            content=SimpleNamespace(
                interim_input_transcription=None,
                input_transcription=transcript,
                output_transcription=None,
                interrupted=False,
                model_turn=None,
                turn_complete=False,
            ),
            tool_calls=[SimpleNamespace(
                id="wrong-send-call",
                name="send_chat_message",
                args={"contact_name": "確認", "message": "確認"},
            )],
        ))
        await wait_until(lambda: any(
            item.get("intent") == "ui.choice.activate" for item in events
        ))
        action = next(
            item for item in events
            if item.get("intent") == "ui.choice.activate"
        )
        assert action["arguments"] == {"action": "confirm"}
        assert not any(
            item.get("intent") == "chat.request_send" for item in events
        )
        assert not any(
            item.get("type") == "confirmation_required" for item in events
        )

        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "type": "action_result",
                "action_id": action["action_id"],
                "success": True,
                "message": "確認完成。",
            }),
        })
        await wait_until(lambda: any(
            response[0] == "wrong-send-call"
            and response[2].get("status") == "success"
            for response in live.tool_responses
        ))
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_self_and_memory_live_tools_stay_direct_and_memory_add_confirms():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "revision": 0,
                "permissions": {
                    "memory_read": True,
                    "memory_write": True,
                },
                "voice_config": {"self_name": "Sunny"},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))

        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="self-call",
            name="read_self_profile",
            args={"detail": "summary"},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "self.query" for item in events
        ))
        self_action = next(
            item for item in events if item.get("intent") == "self.query"
        )
        assert self_action["arguments"] == {"detail": "summary"}

        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "type": "action_result",
                "action_id": self_action["action_id"],
                "success": True,
                "message": "你是 Sunny。",
            }),
        })
        await wait_until(lambda: any(
            response[0] == "self-call"
            and response[2].get("status") == "success"
            for response in live.tool_responses
        ))

        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="memory-add-call",
            name="add_memory",
            args={"label": "打籃球", "stance": "like"},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "memory.add"
            and item.get("type") == "confirmation_required"
            for item in events
        ))
        confirmation = next(
            item for item in events
            if item.get("intent") == "memory.add"
            and item.get("type") == "confirmation_required"
        )
        assert confirmation["arguments"] == {
            "label": "打籃球",
            "stance": "like",
        }
        assert confirmation["phrase"] == "確認新增阿月記憶"

        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_duplex_audio_has_response_id_and_interruption_discards_old_turn():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={"scope": "global", "revision": 0},
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x01\x02"))
        await live.incoming.put(message(content=SimpleNamespace(
            interim_input_transcription=None,
            input_transcription=None,
            output_transcription=SimpleNamespace(text="嗨", finished=False),
            interrupted=False,
            model_turn=SimpleNamespace(parts=[part]),
            turn_complete=False,
        )))
        await wait_until(lambda: any(item.get("type") == "audio_chunk" for item in events))
        reply = next(item for item in events if item.get("type") == "assistant_reply")
        chunk = next(item for item in events if item.get("type") == "audio_chunk")
        assert reply["response_id"] == chunk["response_id"]

        await live.incoming.put(message(content=SimpleNamespace(
            interim_input_transcription=None,
            input_transcription=None,
            output_transcription=None,
            interrupted=True,
            model_turn=None,
            turn_complete=True,
        )))
        await wait_until(lambda: any(
            item.get("type") == "audio_interrupted" for item in events
        ))
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task
        assert not any(item.get("type") == "audio_complete" for item in events)

    asyncio.run(scenario())


def test_duplex_go_away_resumes_without_closing_the_app_socket():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={"scope": "global", "revision": 0},
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x09\x08"))
        completed_turn = SimpleNamespace(
            interim_input_transcription=None,
            input_transcription=None,
            output_transcription=SimpleNamespace(text="前一輪", finished=True),
            interrupted=False,
            model_turn=SimpleNamespace(parts=[part]),
            turn_complete=True,
        )
        await live.incoming.put(message(content=completed_turn))
        await wait_until(lambda: any(item.get("type") == "audio_complete" for item in events))
        await live.incoming.put(message(go_away=SimpleNamespace(time_left="10s")))
        await wait_until(lambda: live.reconnect_count == 1)
        ready = [
            item for item in events
            if item.get("type") == "state" and item.get("state") == "duplex_ready"
        ]
        assert ready[-1]["resumed"] is True
        assert ready[-1]["confirmations_preserved"] is True
        await live.incoming.put(message(content=completed_turn))
        await asyncio.sleep(0.01)
        assert len([
            item for item in events if item.get("type") == "audio_chunk"
        ]) == 1
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_matching_ayue_gets_one_progress_cue_then_reads_background_result():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "revision": 0,
                "permissions": {"match_ayue": True, "match_read": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="matching-call",
            name="ask_matching_ayue",
            args={"question": "我的配對進度如何？"},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "match.ayue_query" for item in events
        ))
        action = next(
            item for item in events if item.get("intent") == "match.ayue_query"
        )
        assert live.tool_responses[-1][2] == {
            "status": "working",
            "spoken_prompt": "我找一下，稍等一下。",
            "message": "只逐字說出 spoken_prompt，不可自行編造結果。",
        }
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "type": "action_result",
                "action_id": action["action_id"],
                "success": True,
                "message": "目前正在幫你尋找合適的對象。",
            }),
        })
        await live.incoming.put(message(content=SimpleNamespace(
            interim_input_transcription=None,
            input_transcription=None,
            output_transcription=None,
            interrupted=False,
            model_turn=None,
            turn_complete=True,
        )))
        await wait_until(lambda: any(
            "[DELEGATED_AYUE_RESULT" in text for text in live.text
        ))
        assert any("目前正在幫你" in text for text in live.text)
        matching_prompt = next(
            text for text in live.text if "[DELEGATED_AYUE_RESULT" in text
        )
        assert "不要逐字照念" in matching_prompt
        assert "第一人稱" in matching_prompt
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_match_status_is_a_direct_app_tool_without_delegated_progress_cue():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "revision": 0,
                "permissions": {"match_read": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="direct-match-status",
            name="read_match_status",
            args={},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "match.query" for item in events
        ))
        action = next(item for item in events if item.get("intent") == "match.query")
        assert action["arguments"] == {"view": "status"}
        assert live.tool_responses == []
        assert not any("DELEGATED_AYUE_RESULT" in text for text in live.text)
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "type": "action_result",
                "action_id": action["action_id"],
                "success": True,
                "message": "目前正在配對，進度 64%。",
            }),
        })
        await wait_until(lambda: bool(live.tool_responses))
        assert live.tool_responses[-1] == (
            "direct-match-status",
            "read_match_status",
            {"status": "success", "message": "目前正在配對，進度 64%。"},
        )
        assert not any("DELEGATED_AYUE_RESULT" in text for text in live.text)
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_match_hub_live_tool_maps_to_the_direct_read_contract():
    proposal = _proposal_from_function(
        SimpleNamespace(name="read_match_hub", args={}), revision=7,
    )
    assert proposal is not None
    assert proposal.intent == "match.query"
    assert proposal.arguments == {"view": "hub"}
    assert proposal.base_revision == 7


def test_english_progress_prompt_never_mentions_another_ayue():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "permissions": {"match_ayue": True},
                "voice_config": {"response_language": "en-US"},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="matching-english",
            name="ask_matching_ayue",
            args={"question": "How is my matching going?"},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "match.ayue_query" for item in events
        ))
        response = live.tool_responses[-1][2]
        assert response["spoken_prompt"] == "Let me check. One moment."
        assert "Ayue" not in response["spoken_prompt"]
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_legacy_public_calendar_write_is_rejected_in_favor_of_direct_tools():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "revision": 0,
                "permissions": {
                    "public_ayue": True,
                    "calendar_read": True,
                    "calendar_write": True,
                },
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="calendar-write",
            name="ask_public_ayue",
            args={
                "domain": "calendar",
                "question": "新增明天上午十點的籃球行程",
            },
        )]))
        await wait_until(lambda: any(
            response[0] == "calendar-write" for response in live.tool_responses
        ))
        response = next(
            item for item in live.tool_responses if item[0] == "calendar-write"
        )
        assert response[2]["status"] == "rejected"
        assert not any(
            item.get("intent") == "ayue.public_query" for item in events
        )
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_personality_exploration_keeps_each_spoken_turn_in_one_flow():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "permissions": {"public_ayue": True, "memory_read": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        for call_id, spoken in (
            ("personality-start", "開始個性探索"),
            ("personality-answer", "我在人多的場合會先觀察"),
        ):
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id=call_id,
                name="personality_exploration_turn",
                args={"message": spoken},
            )]))
            await wait_until(lambda: any(
                item.get("action_id") == call_id for item in events
            ))
        actions = [
            item for item in events
            if item.get("intent") == "personality.explore"
        ]
        assert [item["arguments"]["message"] for item in actions] == [
            "開始個性探索",
            "我在人多的場合會先觀察",
        ]
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_recent_photo_tool_emits_only_a_count_bound_gallery_action():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "post",
                "revision": 4,
                "permissions": {"gallery": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="recent-photos",
            name="select_recent_post_photos",
            args={"count": 3, "query": "sunset", "path": "/private/photo.jpg"},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "post.select_recent_photos" for item in events
        ))
        action = next(
            item for item in events
            if item.get("intent") == "post.select_recent_photos"
        )
        assert action["arguments"] == {"count": 3}
        assert action["base_revision"] == 4
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_calendar_read_is_direct_and_does_not_delegate_to_public_ayue():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "revision": 0,
                "permissions": {"public_ayue": False, "calendar_read": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="calendar-read",
            name="read_calendar",
            args={"range": "weekend"},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "calendar.query" for item in events
        ))
        action = next(
            item for item in events if item.get("intent") == "calendar.query"
        )
        assert action["arguments"] == {"range": "weekend"}
        assert not any(
            item.get("intent") == "ayue.public_query" for item in events
        )
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "type": "action_result",
                "action_id": "calendar-read",
                "success": True,
                "message": "這個週末有一個籃球練習。",
            }),
        })
        await wait_until(lambda: any(
            response[0] == "calendar-read"
            and response[2].get("status") == "success"
            for response in live.tool_responses
        ))
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_calendar_create_is_direct_and_executes_after_one_spoken_confirmation():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "revision": 0,
                "permissions": {"public_ayue": False, "calendar_write": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="calendar-create",
            name="create_calendar_event",
            args={
                "title": "籃球練習",
                "date": "2026-09-12",
                "start_time": "10:00",
                "end_time": "11:00",
            },
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "calendar.create" for item in events
        ))
        confirmation = next(
            item for item in events if item.get("intent") == "calendar.create"
        )
        assert confirmation["type"] == "confirmation_required"
        assert not any(
            item.get("intent") == "ayue.public_query" for item in events
        )

        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="calendar-confirm",
            name="confirm_pending_action",
            args={"spoken_phrase": "確認"},
        )]))
        await wait_until(lambda: any(
            item.get("type") == "action_proposal"
            and item.get("intent") == "calendar.create"
            for item in events
        ))
        action = next(
            item for item in events
            if item.get("type") == "action_proposal"
        )
        assert action["arguments"]["title"] == "籃球練習"
        assert action["confirmed"] is True
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_private_ayue_requires_one_confirmation_per_voice_session():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "revision": 0,
                "permissions": {"private_ayue": True, "chat_content": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="private-first",
            name="ask_private_ayue",
            args={"contact_name": "小美", "question": "我們聊過籃球嗎？"},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "ayue.private_query"
            and item.get("type") == "confirmation_required"
            for item in events
        ))
        assert not any(
            item.get("type") == "action_proposal" for item in events
        )
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="private-confirm",
            name="confirm_pending_action",
            args={"spoken_phrase": "確認讀取私人聊天"},
        )]))
        await wait_until(lambda: any(
            item.get("intent") == "ayue.private_query"
            and item.get("type") == "action_proposal"
            for item in events
        ))
        first_action_count = sum(
            item.get("intent") == "ayue.private_query"
            and item.get("type") == "action_proposal"
            for item in events
        )
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="private-second",
            name="ask_private_ayue",
            args={"contact_name": "小美", "question": "那我該怎麼回？"},
        )]))
        await wait_until(lambda: sum(
            item.get("intent") == "ayue.private_query"
            and item.get("type") == "action_proposal"
            for item in events
        ) > first_action_count)
        assert sum(
            item.get("type") == "confirmation_required" for item in events
        ) == 1
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


def test_duplex_permission_denies_matching_and_reports_its_own_access():
    async def scenario():
        live = FakeLive()
        socket = FakeWebSocket()
        events = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={
                "scope": "global",
                "permissions": {"match_ayue": False, "status_read": True},
                "feature_status": {"location_enabled": True},
            },
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="denied-matching",
            name="ask_matching_ayue",
            args={"question": "配對進度如何？"},
        )]))
        await wait_until(lambda: any(
            response[2].get("status") == "permission_denied"
            for response in live.tool_responses
        ))
        assert not any(item.get("type") == "action_proposal" for item in events)

        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="capabilities",
            name="get_voice_capabilities",
            args={},
        )]))
        await wait_until(lambda: any(
            response[0] == "capabilities" for response in live.tool_responses
        ))
        capability = next(
            response[2] for response in live.tool_responses
            if response[0] == "capabilities"
        )
        assert capability["permissions"]["match_ayue"] is False
        assert capability["feature_status"] == {"location_enabled": True}
        await socket.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({"type": "stop"}),
        })
        await task

    asyncio.run(scenario())


async def _append(items, item):
    items.append(item)
