#!/usr/bin/env python3
"""Create the isolated 30-account matching test cohort without deleting data."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


SERVER_ROOT = Path(__file__).resolve().parents[1]
SOCIAL_ROOT = SERVER_ROOT / "social"
load_dotenv(SERVER_ROOT / ".env", override=False)
load_dotenv(SOCIAL_ROOT / ".env", override=False)
sys.path.insert(0, str(SOCIAL_ROOT))

from database import profiles_coll  # noqa: E402
from services.ai_service import get_embeddings  # noqa: E402
from services.match_search_context import context_embedding_source_hash  # noqa: E402
from services.profile_location import normalize_profile_location  # noqa: E402


COHORT = "match_v1"
PASSWORD = "12345678"
APPWRITE_ENDPOINT = os.getenv(
    "MATCH_TEST_APPWRITE_ENDPOINT", "https://appwrite.misproject.us.ci/v1",
).rstrip("/")
APPWRITE_PROJECT_ID = os.getenv("APPWRITE_PROJECT_ID", "6a44de590010fa46afbd")
APPWRITE_DB_ID = os.getenv("MATCH_TEST_APPWRITE_DB_ID", "dating_db")
APPWRITE_PROFILE_COLLECTION = "user_profiles"

GROUPS = [
    ("咖啡", "喝咖啡", [
        "最近想找人去安靜的咖啡館聊天",
        "正在研究手沖咖啡，想和人交換店家清單",
        "週末想去探訪一間沒去過的咖啡館",
        "想找人喝杯咖啡，輕鬆聊聊最近的生活",
        "對咖啡甜點很有興趣，也願意嘗試新店",
    ]),
    ("散步", "散步", [
        "傍晚想找人沿著河邊散步",
        "最近想用散步放鬆，也希望有人可以聊天",
        "週末想去公園慢慢走一圈看看風景",
        "喜歡城市散策，想認識願意一起走走的人",
        "想找步調輕鬆的散步夥伴，不趕行程",
    ]),
    ("攝影", "攝影", [
        "最近在練街頭攝影，想一起外拍",
        "喜歡拍夜景，想找人交換構圖想法",
        "週末想帶相機去港邊拍照",
        "剛開始學攝影，想認識也願意邊走邊拍的人",
        "想找人拍城市光影，也可以互相當模特兒",
    ]),
    ("登山", "登山", [
        "想找人走一條入門登山步道",
        "最近固定爬郊山，希望認識重視安全的山友",
        "週末想去看山景，行程希望不要太趕",
        "剛開始接觸登山，想從簡單路線開始",
        "喜歡健行和自然，想找人一起規劃路線",
    ]),
    ("水上活動", "水上活動", [
        "最近想嘗試衝浪，還是新手，想找人一起體驗",
        "對衝浪和立槳有興趣，想認識願意一起學的人",
        "喜歡海邊活動，也願意和新朋友試試衝浪課",
        "想找人一起參加水上活動，技能程度都可以",
        "週末想去海邊，對衝浪體驗很有興趣",
    ]),
    ("看展", "看展", [
        "最近想去看插畫展，想找人一起逛",
        "喜歡設計和藝術展覽，想交換觀展心得",
        "週末想看展，行程可以慢慢走慢慢聊",
        "對攝影展和當代藝術有興趣，想認識展伴",
        "想找人一起看展，之後也可以喝飲料聊天",
    ]),
]


def build_accounts() -> list[dict]:
    accounts = []
    number = 1
    for topic, activity, contexts in GROUPS:
        for position, context in enumerate(contexts, start=1):
            user_id = f"match_test_{number:02d}"
            accounts.append({
                "user_id": user_id,
                "email": f"matchtest{number:02d}@gmail.com",
                "display_name": f"{topic}小伴{position}",
                "topic": topic,
                "activity": activity,
                "context": context,
                "order": number,
            })
            number += 1
    return accounts


def appwrite_headers() -> dict[str, str]:
    api_key = os.getenv("APPWRITE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("APPWRITE_API_KEY is unavailable")
    return {
        "X-Appwrite-Project": APPWRITE_PROJECT_ID,
        "X-Appwrite-Key": api_key,
        "Content-Type": "application/json",
    }


def checked(response: requests.Response, accepted: set[int]) -> requests.Response:
    if response.status_code not in accepted:
        detail = " ".join(response.text.split())[:300]
        raise RuntimeError(f"Appwrite {response.request.method} failed: {response.status_code} {detail}")
    return response


def upsert_appwrite_account(session: requests.Session, account: dict, headers: dict) -> str:
    user_url = f"{APPWRITE_ENDPOINT}/users/{account['user_id']}"
    existing = session.get(user_url, headers=headers, timeout=20)
    if existing.status_code == 404:
        checked(session.post(
            f"{APPWRITE_ENDPOINT}/users",
            headers=headers,
            json={
                "userId": account["user_id"], "email": account["email"],
                "password": PASSWORD, "name": account["display_name"],
            },
            timeout=20,
        ), {200, 201})
        action = "created"
    else:
        checked(existing, {200})
        found = existing.json()
        if found.get("email") != account["email"]:
            raise RuntimeError(f"Account ID collision: {account['user_id']}")
        checked(session.patch(
            f"{user_url}/password", headers=headers,
            json={"password": PASSWORD}, timeout=20,
        ), {200})
        action = "updated"
    checked(session.patch(
        f"{user_url}/verification", headers=headers,
        json={"emailVerification": True}, timeout=20,
    ), {200})

    document_url = (
        f"{APPWRITE_ENDPOINT}/databases/{APPWRITE_DB_ID}/collections/"
        f"{APPWRITE_PROFILE_COLLECTION}/documents/{account['user_id']}"
    )
    profile_data = {
        "name": account["display_name"],
        "gender": "female" if account["order"] % 2 else "male",
        "phone": f"0901{account['order']:06d}",
        "age": 21 + account["order"] % 9,
        "region": "高雄市",
        "userinfo": account["context"],
        "interest": account["topic"],
    }
    document = session.get(document_url, headers=headers, timeout=20)
    if document.status_code == 404:
        checked(session.post(
            f"{APPWRITE_ENDPOINT}/databases/{APPWRITE_DB_ID}/collections/"
            f"{APPWRITE_PROFILE_COLLECTION}/documents",
            headers=headers,
            json={
                "documentId": account["user_id"], "data": profile_data,
                "permissions": [
                    'read("any")',
                    f'update("user:{account["user_id"]}")',
                ],
            },
            timeout=20,
        ), {200, 201})
    else:
        checked(document, {200})
        checked(session.patch(
            document_url, headers=headers, json={"data": profile_data}, timeout=20,
        ), {200})
    return action


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Create/update the fixed cohort")
    args = parser.parse_args()
    accounts = build_accounts()
    if not args.apply:
        print(f"dry-run: {len(accounts)} accounts, {len(GROUPS)} topics, cohort={COHORT}")
        return 0

    existing = {
        str(profile.get("user_id") or ""): profile
        for profile in profiles_coll.find(
            {"user_id": {"$in": [account["user_id"] for account in accounts]}},
            {
                "_id": 0, "user_id": 1, "current_context": 1,
                "context_embedding": 1, "context_embedding_source_hash": 1,
            },
        )
    }
    vectors_by_id: dict[str, list[float]] = {}
    pending_accounts = []
    for account in accounts:
        profile = existing.get(account["user_id"], {})
        vector = profile.get("context_embedding") or []
        source_hash = context_embedding_source_hash(account["context"])
        if (
            profile.get("current_context") == account["context"]
            and profile.get("context_embedding_source_hash") == source_hash
            and len(vector) == 3072
        ):
            vectors_by_id[account["user_id"]] = vector
        else:
            pending_accounts.append(account)
    for offset in range(0, len(pending_accounts), 20):
        batch = pending_accounts[offset:offset + 20]
        batch_vectors = get_embeddings(
            [account["context"] for account in batch],
            task_type="raw",
            output_dimensionality=3072,
        )
        if len(batch_vectors) != len(batch) or any(
            len(vector) != 3072 for vector in batch_vectors
        ):
            raise RuntimeError("Embedding batch did not match the requested 3072-dimensional profiles")
        vectors_by_id.update({
            account["user_id"]: vector
            for account, vector in zip(batch, batch_vectors)
        })

    headers = appwrite_headers()
    session = requests.Session()
    created = updated = 0
    try:
        for account in accounts:
            vector = vectors_by_id[account["user_id"]]
            action = upsert_appwrite_account(session, account, headers)
            created += action == "created"
            updated += action == "updated"
            now = time.time()
            profiles_coll.update_one(
                {"user_id": account["user_id"]},
                {"$set": {
                    "display_name": account["display_name"],
                    "current_context": account["context"],
                    "current_context_revision": 1,
                    "context_embedding": vector,
                    "context_embedding_source_hash": context_embedding_source_hash(account["context"]),
                    "context_signals": {
                        "activity": account["activity"],
                        "companion_intent": "想找人一起參加",
                        "temporal_status": "近期",
                    },
                    "big_five": {
                        "O": 7 + account["order"] % 3,
                        "C": 6 + account["order"] % 3,
                        "E": 4 + account["order"] % 5,
                        "A": 7 + account["order"] % 2,
                        "N": 3 + account["order"] % 3,
                        "summary": "好奇、友善，願意和新朋友從共同活動開始認識。",
                    },
                    "deep_profile": {
                        "life_philosophy": "從共同體驗慢慢認識彼此",
                        "attachment_style": "安全型",
                        "decision_style": "開放討論型",
                        "core_values": ["尊重", "真誠", account["topic"]],
                        "summary": f"重視尊重與真誠，也喜歡{account['topic']}。",
                    },
                    "initial_interest": account["topic"],
                    "profile_location": normalize_profile_location("高雄市", ""),
                    "onboarding_completed": True,
                    "proactive_care_enabled": False,
                    "match_search": {"status": "idle"},
                    "recent_context_updated_at": now,
                    "recent_context_expires_at": now + 30 * 86400,
                    "test_match_cohort": COHORT,
                    "test_match_topic": account["topic"],
                    "test_login_email": account["email"],
                    "test_account_order": account["order"],
                }},
                upsert=True,
            )
        profiles_coll.update_many(
            {"user_id": {"$in": ["seed_user_01", "seed_user_04"]}},
            {"$set": {"test_match_cohort": COHORT}},
        )
    finally:
        session.close()

    print(
        f"match test cohort ready: accounts={len(accounts)} "
        f"auth_created={created} auth_updated={updated} password={PASSWORD}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
