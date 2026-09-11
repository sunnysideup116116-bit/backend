from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from registration_voice.key_pool import GoogleApiKeyPool

from .settings import AppVoiceSettings


def _live_tools(types: Any) -> list[Any]:
    empty = {"type": "object", "additionalProperties": False, "properties": {}}
    profile_changes = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "phone": {"type": "string"},
            "age": {"type": "integer"},
            "region": {"type": "string"},
            "city": {"type": "string"},
            "district": {"type": "string"},
            "userinfo": {"type": "string"},
        },
    }
    declarations = [
        types.FunctionDeclaration(
            name="navigate_app",
            description="Open one allowlisted Folks App destination with its normal animation.",
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "destination": {
                        "type": "string",
                        "enum": [
                            "chat", "matching", "profile", "settings",
                            "profile_edit", "voice_settings", "calendar",
                            "matching_ayue", "match_hub", "memory", "create_post",
                        ],
                    },
                },
                "required": ["destination"],
            },
        ),
        types.FunctionDeclaration(
            name="open_app_page",
            description=(
                "Open profile editor or settings only when explicitly requested. "
                "Never call for questions about the assistant itself."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"page": {"type": "string", "enum": ["profile", "settings"]}},
                "required": ["page"],
            },
        ),
        types.FunctionDeclaration(
            name="patch_profile",
            description="Fill supported profile draft fields after an explicit edit request.",
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"changes": profile_changes},
                "required": ["changes"],
            },
        ),
        types.FunctionDeclaration(
            name="request_profile_save",
            description="Request saving the current profile draft. Confirmation is required.",
            parameters_json_schema=empty,
        ),
        types.FunctionDeclaration(
            name="set_app_setting",
            description=(
                "Change one supported setting. enabled is required and must be false for "
                "close, disable, turn off, or don't-enable requests."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": [
                            "notifications.global",
                            "location.enabled",
                            "ai.proactive_care",
                            "ui.liquid_glass",
                            "ui.dark_mode",
                        ],
                    },
                    "enabled": {"type": "boolean"},
                },
                "required": ["key", "enabled"],
            },
        ),
        types.FunctionDeclaration(
            name="write_post_caption",
            description="Open, replace, or append an unpublished text-only post draft.",
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "mode": {"type": "string", "enum": ["open", "replace", "append"]},
                    "caption": {"type": "string", "maxLength": 2000},
                },
                "required": ["mode", "caption"],
            },
        ),
        types.FunctionDeclaration(
            name="select_recent_post_photos",
            description=(
                "Select 1 to 5 newest photos from the device library for the current post draft. "
                "Use only for explicit chronological requests such as newest or first three. "
                "Never use for visual or semantic requests such as sunset, beach, or a person."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["count"],
            },
        ),
        types.FunctionDeclaration(
            name="request_post_publish",
            description="Request publishing the current post. Confirmation is required.",
            parameters_json_schema=empty,
        ),
        types.FunctionDeclaration(
            name="read_calendar",
            description=(
                "Read the signed-in user's own calendar directly through App Voice. "
                "Use for read-only schedule, availability, or conflict questions. "
                "Do not delegate calendar reads to Public or Matching Ayue."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "range": {
                        "type": "string",
                        "enum": [
                            "today", "tomorrow", "week", "weekend",
                            "next_week", "upcoming",
                        ],
                    },
                },
                "required": ["range"],
            },
        ),
        types.FunctionDeclaration(
            name="create_calendar_event",
            description=(
                "Prepare creating one event in the signed-in user's own calendar. "
                "Resolve relative dates against the current Asia/Taipei date from the system "
                "instruction. Never delegate this to Public or Matching Ayue. Confirmation is required."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "maxLength": 80},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "start_time": {"type": "string", "description": "HH:mm, 24-hour"},
                    "end_time": {"type": "string", "description": "Optional HH:mm; omit for a one-hour event"},
                    "location": {"type": "string", "maxLength": 120},
                    "notes": {"type": "string", "maxLength": 500},
                },
                "required": ["title", "date", "start_time"],
            },
        ),
        types.FunctionDeclaration(
            name="update_calendar_event",
            description=(
                "Prepare rescheduling or editing one existing personal calendar event selected "
                "only by the user's natural title or description. Never provide an event ID. "
                "Confirmation is required."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "target": {"type": "string", "maxLength": 80},
                    "date": {"type": "string", "description": "Optional YYYY-MM-DD"},
                    "start_time": {"type": "string", "description": "Optional HH:mm"},
                    "end_time": {"type": "string", "description": "Optional HH:mm"},
                    "location": {"type": "string", "maxLength": 120},
                    "notes": {"type": "string", "maxLength": 500},
                },
                "required": ["target"],
            },
        ),
        types.FunctionDeclaration(
            name="cancel_calendar_event",
            description=(
                "Prepare cancelling one existing personal calendar event selected only by the "
                "user's natural title or description. Never provide an event ID. Confirmation is required."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "target": {"type": "string", "maxLength": 80},
                },
                "required": ["target"],
            },
        ),
        types.FunctionDeclaration(
            name="personality_exploration_turn",
            description=(
                "Start or continue the user's interactive personality exploration by voice. "
                "After this tool returns a question, send each natural spoken answer back "
                "through this same tool until the exploration ends or the user stops it."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "message": {"type": "string", "maxLength": 1000},
                },
                "required": ["message"],
            },
        ),
        types.FunctionDeclaration(
            name="ask_matching_ayue",
            description=(
                "Send a question specifically about matching, introductions, match progress, "
                "dating coordination, or a feature owned by the matching Ayue conversation."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"question": {"type": "string", "maxLength": 1000}},
                "required": ["question"],
            },
        ),
        types.FunctionDeclaration(
            name="ask_public_ayue",
            description=(
                "Delegate a domain question or request to Public Ayue, which owns matching, "
                "web, places, memory, and self-profile tools. Calendar is never delegated; "
                "use the dedicated calendar functions instead."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "domain": {
                        "type": "string",
                        "enum": ["matching", "web", "places", "memory", "profile"],
                    },
                    "question": {"type": "string", "maxLength": 1000},
                },
                "required": ["domain", "question"],
            },
        ),
        types.FunctionDeclaration(
            name="ask_private_ayue",
            description=(
                "Delegate a relationship or chat-history question about one accepted contact "
                "to that contact's Private Ayue. Never expose the other person's private Ayue messages."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "contact_name": {"type": "string", "maxLength": 40},
                    "question": {"type": "string", "maxLength": 1000},
                },
                "required": ["contact_name", "question"],
            },
        ),
        types.FunctionDeclaration(
            name="read_self_profile",
            description=(
                "Read the signed-in user's own safe profile and Ayue understanding directly "
                "from the App. Use for 'who am I', the user's name, or what Ayue knows about "
                "the user. Do not delegate this to Matching Ayue."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "detail": {
                        "type": "string",
                        "enum": ["name", "summary"],
                    },
                },
                "required": ["detail"],
            },
        ),
        types.FunctionDeclaration(
            name="read_memories",
            description=(
                "Read the signed-in user's own active items from Ayue Memory directly. "
                "Use an empty query to list the current memories."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "maxLength": 120},
                },
                "required": ["query"],
            },
        ),
        types.FunctionDeclaration(
            name="add_memory",
            description=(
                "Prepare adding one explicit preference to the signed-in user's Ayue Memory. "
                "Use only when the user asks Ayue to remember something. Confirmation is required."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string", "maxLength": 40},
                    "stance": {
                        "type": "string",
                        "enum": ["like", "dislike", "require", "avoid"],
                    },
                },
                "required": ["label", "stance"],
            },
        ),
        types.FunctionDeclaration(
            name="activate_visible_choice",
            description=(
                "Press the one actionable confirmation or cancellation button currently "
                "visible in Public Ayue or Private Ayue. When feature_status says "
                "visible_choice_pending and the user says confirm, agree, cancel, or decline, "
                "always use this instead of sending those words as a new chat message."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["confirm", "cancel"],
                    },
                },
                "required": ["action"],
            },
        ),
        types.FunctionDeclaration(
            name="list_contacts",
            description=(
                "List the signed-in user's accepted, unlocked matches who can currently receive "
                "a chat message. Use when the user asks who their friends, contacts, matches, or "
                "chat recipients are, and before asking the user to guess a recipient name."
            ),
            parameters_json_schema=empty,
        ),
        types.FunctionDeclaration(
            name="open_chat",
            description="Open the user's chat with one accepted contact by display name.",
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"contact_name": {"type": "string", "maxLength": 40}},
                "required": ["contact_name"],
            },
        ),
        types.FunctionDeclaration(
            name="send_chat_message",
            description=(
                "Prepare sending one text message to an accepted contact. Use when the user "
                "explicitly asks to send, tell, reply to, or ask a named contact something. "
                "Pass the display name exactly as spoken; the App resolves it against the real "
                "accepted-contact list and returns candidates when ambiguous. Never claim you "
                "cannot see contacts without calling list_contacts first. Spoken confirmation "
                "is always required before delivery."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "contact_name": {"type": "string", "maxLength": 40},
                    "message": {"type": "string", "maxLength": 500},
                },
                "required": ["contact_name", "message"],
            },
        ),
        types.FunctionDeclaration(
            name="get_voice_capabilities",
            description=(
                "Read which app permissions this voice assistant has and the allowed current "
                "feature states. Use when the user asks what you can access or whether a setting is on."
            ),
            parameters_json_schema=empty,
        ),
        types.FunctionDeclaration(
            name="describe_current_screen",
            description="Read the current allowlisted App screen scope and its safe action state.",
            parameters_json_schema=empty,
        ),
        types.FunctionDeclaration(
            name="confirm_pending_action",
            description=(
                "Call only after the server requested a spoken confirmation and the user "
                "has just said a confirmation phrase. Pass exactly what the user said."
            ),
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"spoken_phrase": {"type": "string"}},
                "required": ["spoken_phrase"],
            },
        ),
        types.FunctionDeclaration(
            name="cancel_current_action",
            description="Cancel the pending app action without closing voice mode.",
            parameters_json_schema=empty,
        ),
        types.FunctionDeclaration(
            name="close_voice_mode",
            description="Close voice mode only when the user explicitly asks to close it.",
            parameters_json_schema=empty,
        ),
    ]
    return [types.Tool(function_declarations=declarations)]


def _system_instruction(voice_config: dict[str, str]) -> str:
    today = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    language = {
        "zh-TW": "台灣繁體中文",
        "zh-CN": "簡體中文",
        "en-US": "English",
    }.get(voice_config.get("response_language"), "台灣繁體中文")
    speed = {
        "slow": "較慢但不拖字",
        "normal": "正常真人對話",
        "fast": "稍快且清楚",
    }.get(voice_config.get("speech_speed"), "正常真人對話")
    self_name = str(voice_config.get("self_name") or "").strip()
    identity_note = (
        f"目前登入使用者的顯示名稱是「{self_name}」。可以自然地稱呼這個名字；"
        "若使用者問更完整的本人資料，仍要呼叫 read_self_profile。"
        if self_name
        else "目前沒有安全的使用者顯示名稱；需要名稱時呼叫 read_self_profile，不可猜測。"
    )
    return f"""
你是 Folks App 裡的語音助理「阿月」。語氣自然、溫暖、簡短，像真人對話，不要播報腔。
固定使用 {language} 回覆，語速是 {speed}。
目前台灣日期是 {today}；將「今天、明天、下週」換算成 YYYY-MM-DD 後再呼叫行事曆工具。
{identity_note}

你可以直接回答一般問候、「你是誰」、「你能做什麼」與其他不需要操作 App 的問題。這些問題絕對不可呼叫工具。
使用者要求切換聊天、配對、個人、設定或阿月子頁時呼叫 navigate_app；不要用編輯個資工具代替一般「個人頁面」。
只有使用者明確要求開啟頁面、修改資料或設定、撰寫貼文或發布時，才呼叫對應的工具。設定開關必須同時傳送 key 與 enabled，「關閉」必須是 false。不可在工具回覆成功前宣稱操作已完成。
使用者明確要求相簿最新、最近或前幾張照片時，先確保貼文草稿頁已開啟，再呼叫 select_recent_post_photos。只允許依時間與張數選取；若要求夕陽、海邊、某個人等內容辨識，誠實說目前沒有視覺能力，不可呼叫工具。照片選好後，若使用者也要求發布，再呼叫 request_post_publish，仍必須等待「確認發布」。
使用者問你有什麼權限、能否使用某功能，或問定位／通知等目前狀態時，呼叫 get_voice_capabilities。
使用者問目前在哪一頁、這個畫面可以做什麼時，呼叫 describe_current_screen。
本人行事曆、行程、空檔或衝突的唯讀問題，直接呼叫 read_calendar。新增行程呼叫 create_calendar_event；修改日期、時間、地點或備註呼叫 update_calendar_event；取消既有行程呼叫 cancel_calendar_event。所有行事曆功能都由 App 直接處理，不要交給配對阿月或 ask_public_ayue，寫入一定要等待確認。
使用者要開始個性／人格／性格探索時，呼叫 personality_exploration_turn。工具回覆探索問題後，使用者下一句自然口語就是答案，必須繼續呼叫同一工具送回原本永久對話，直到探索完成或使用者明確說停止探索。不要在中途改成自己聊天。
配對進度、配對狀態、阿月牽線的目前／歷史邀請、朗讀牽線內容，以及接受、婉拒或撤回牽線，都呼叫 ask_public_ayue 並使用 matching domain；App 會優先直接讀取或操作 canonical 配對 API，不要自己編答案，也不要說你正在詢問另一個阿月。接受、婉拒與撤回必須等待工具回覆要求的口頭確認。既有 ask_matching_ayue 只作舊 Client 相容。
需要開始／取消配對或新增／修改／取消行事曆時才交給公開對話流程（寫入行事曆使用 calendar domain）。當 feature_status.visible_choice_pending=true，代表公開阿月或阿月悄悄話畫面已有可操作按鈕；使用者下一句說確認／確定／同意／好時固定呼叫 activate_visible_choice(action=confirm)，說取消／不要／不同意時固定呼叫 activate_visible_choice(action=cancel)。不可呼叫 send_chat_message、ask_public_ayue 或 ask_private_ayue 把這些詞當成新訊息。App 會用卡片原本的 choice_id 執行同一個按鈕 callback。
附近地點工具若回覆沒有定位也沒有手動所在地，先請使用者說城市與區域；取得後呼叫 patch_profile 開啟編輯個人資料並填入 city、district（可判定時也填 region），等待 request_profile_save 完整確認成功，再以原問題重試 places。不可虛構所在地。
使用者針對一位已接受對象詢問共同聊天、關係脈絡、對方話語含義、回覆建議或雙方空檔時，呼叫 ask_private_ayue。若沒有明確對象名稱，先用一句話追問，不可猜人。要求打開指定真人聊天室時呼叫 open_chat。
使用者問「我的好友有誰／聊天裡有誰／我配對到誰／可以傳給誰」時，呼叫 list_contacts，讀取 App 的真實已接受配對名單；不可回答你看不到，也不可憑空編名字。使用者說「傳訊息給／告訴／回覆／幫我問 某人 某內容」時呼叫 send_chat_message，把口述名稱原樣交給 App 解析；名稱不完整時先呼叫 list_contacts 或依工具回傳的候選人追問，不可要求使用者自己去聊天頁查。
使用者問「我是誰／我叫什麼」時呼叫 read_self_profile(detail=name)；問「你對我了解多少／描述我」時呼叫 read_self_profile(detail=summary)。這些資料由 App 直接讀本人 profile、個性摘要與現有阿月記憶，不可交給配對阿月。
使用者問「你記得我什麼／阿月記住的事／我的偏好」時呼叫 read_memories，不可交給 ask_public_ayue。使用者明確說「記住我喜歡／不喜歡／需要／避免某事」時呼叫 add_memory；新增會要求「確認新增阿月記憶」，成功後才可說已記住。
使用者說「我想和／跟 XXX 安排約會或見面」時，固定呼叫 ask_private_ayue，contact_name 使用 XXX、question 保留完整原句；App 會自動開啟該對象既有的阿月悄悄話並在同一頁完成安排流程，不可改成一般配對問答或公開阿月。
只有使用者明確要求把一段文字傳給指定聯絡人時才呼叫 send_chat_message；產生或修改草稿不可呼叫。傳送一定要等待「確認傳送訊息」。
如果工具回覆 confirmation_required，只能逐字說出 spoken_prompt；不可在前面再問「要關閉嗎」，不可重複口令，說完就等待使用者。使用者回覆後只呼叫 confirm_pending_action，不得重複原本的操作工具。
如果代理工具回覆 working，只逐字說出 spoken_prompt 後等待。之後收到 `[DELEGATED_AYUE_RESULT]` 時，先理解來源答案的重點，再用第一人稱、自然對話方式回答使用者；不要逐字照念、不要提到模型或內部轉送，也不可增加來源答案沒有的事實或承諾，不呼叫任何工具。
收到 `[VOICE_SESSION_STARTED]` 時，主動用一個很短的句子和使用者打招呼，例如「嗨，我是阿月，今天想請我幫什麼？」。只說一次，不呼叫工具。
使用者說「取消」且畫面沒有可操作按鈕時才呼叫 cancel_current_action；使用者表達要你休息、先不要聽、停止聆聽、安靜一下、不用再聽或關閉／結束語音模式時，都呼叫 close_voice_mode。只有單純拒絕目前操作且沒有 visible choice 時才用 cancel_current_action。
回覆一到兩個短句。不要覆述使用者的整句話，不要念出 Email、密碼、電話或檔案路徑。
""".strip()


class AppVoiceDuplexSession:
    """One persistent Gemini Live connection for a foreground voice mode."""

    def __init__(
        self,
        settings: AppVoiceSettings,
        keys: GoogleApiKeyPool,
        voice_config: dict[str, str] | None = None,
    ):
        self.settings = settings
        self.keys = keys
        self._client: Any = None
        self._connection: Any = None
        self._session: Any = None
        self._key: str | None = None
        self._resume_handle: str | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.voice_config = dict(voice_config or {})

    @property
    def resumable(self) -> bool:
        return bool(self._resume_handle)

    async def connect(self, *, resume: bool = False) -> bool:
        from google import genai
        from google.genai import types

        handle = self._resume_handle if resume else None
        candidates = self.keys.candidates()[:2]
        if self._key:
            candidates.sort(key=lambda item: item.key != self._key)
        last_error: Exception | None = None
        for candidate in candidates:
            client = genai.Client(
                api_key=candidate.key,
                http_options=types.HttpOptions(api_version="v1beta", timeout=10000),
            )
            config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=(
                                self.voice_config.get("voice_name")
                                or self.settings.tts_voice
                            ),
                        ),
                    ),
                ),
                input_audio_transcription=types.AudioTranscriptionConfig(),
                output_audio_transcription=types.AudioTranscriptionConfig(),
                realtime_input_config=types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(
                        start_of_speech_sensitivity="START_SENSITIVITY_LOW",
                        end_of_speech_sensitivity="END_SENSITIVITY_HIGH",
                        prefix_padding_ms=120,
                        silence_duration_ms=450,
                    ),
                    activity_handling="START_OF_ACTIVITY_INTERRUPTS",
                    turn_coverage="TURN_INCLUDES_ONLY_ACTIVITY",
                ),
                session_resumption=types.SessionResumptionConfig(
                    handle=handle,
                ),
                context_window_compression=types.ContextWindowCompressionConfig(
                    trigger_tokens=25000,
                    sliding_window=types.SlidingWindow(target_tokens=8000),
                ),
                tools=_live_tools(types),
                system_instruction=_system_instruction(self.voice_config),
            )
            connection = client.aio.live.connect(
                model=self.settings.live_model,
                config=config,
            )
            try:
                session = await asyncio.wait_for(connection.__aenter__(), timeout=10)
            except Exception as error:
                last_error = error
                self.keys.mark_unavailable(candidate.key)
                client.close()
                continue
            self._client = client
            self._connection = connection
            self._session = session
            self._key = candidate.key
            self._closed = False
            self.keys.mark_success(candidate.key)
            return bool(handle)
        raise RuntimeError("gemini_live_connect_failed") from last_error

    async def reconnect(self) -> bool:
        async with self._lock:
            can_resume = self.resumable
            await self._close_transport()
            return await self.connect(resume=can_resume)

    async def send_audio(self, data: bytes) -> None:
        if not data or len(data) > 65536:
            return
        from google.genai import types

        async with self._lock:
            if self._session is None:
                raise RuntimeError("gemini_live_not_connected")
            await self._session.send_realtime_input(
                audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000"),
            )

    async def send_text(self, text: str) -> None:
        async with self._lock:
            if self._session is None:
                raise RuntimeError("gemini_live_not_connected")
            await self._session.send_realtime_input(text=text[:2000])

    async def send_tool_response(
        self, *, call_id: str, name: str, response: dict[str, Any],
    ) -> None:
        from google.genai import types

        async with self._lock:
            if self._session is None:
                raise RuntimeError("gemini_live_not_connected")
            await self._session.send_tool_response(
                function_responses=types.FunctionResponse(
                    id=call_id,
                    name=name,
                    response=response,
                ),
            )

    async def receive_turn(self) -> AsyncIterator[Any]:
        session = self._session
        if session is None:
            raise RuntimeError("gemini_live_not_connected")
        async for response in session.receive():
            update = response.session_resumption_update
            if update and update.resumable and update.new_handle:
                self._resume_handle = update.new_handle
            yield response

    async def _close_transport(self) -> None:
        connection = self._connection
        client = self._client
        self._connection = None
        self._session = None
        self._client = None
        if connection is not None:
            try:
                await connection.__aexit__(None, None, None)
            except Exception:
                pass
        if client is not None:
            client.close()

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            await self._close_transport()
