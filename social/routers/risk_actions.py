"""Pair-chat risk feedback / sender appeal proxies.

The Flutter app only talks to the social server (:8000). These endpoints
forward receiver feedback (comfortable / uncomfortable + optional detail)
and sender appeals to the risk backend (:8001), which owns the
intervention_logs audit trail and the feedback-based recalibration signal.

Proxy design:
- The social server never interprets the payload; it is a thin transport.
- Failure to reach the risk backend returns 503 so the app can show a
  transient error instead of silently dropping the audit record.
"""

import os

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

_RISK_SERVICE_URL = (
    os.getenv("RISK_SERVICE_URL") or "http://127.0.0.1:8001"
).rstrip("/")
_RISK_TIMEOUT = float(os.getenv("RISK_TIMEOUT_SEC") or "20")


class RiskFeedbackRequest(BaseModel):
    triggered_by_msg_id: str = Field(min_length=1, max_length=128)
    role: str
    feedback: str
    detail: str | None = Field(default=None, max_length=2000)


class SenderAppealProxyRequest(BaseModel):
    triggered_by_msg_id: str = Field(min_length=1, max_length=128)
    sender_id: str = Field(min_length=1, max_length=128)
    appeal_text: str = Field(min_length=1, max_length=2000)


class ReceiverReportProxyRequest(BaseModel):
    triggered_by_msg_id: str = Field(min_length=1, max_length=128)
    receiver_id: str = Field(min_length=1, max_length=128)
    report_text: str = Field(min_length=1, max_length=2000)


class BlockUserProxyRequest(BaseModel):
    blocker_id: str = Field(min_length=1, max_length=128)
    blocked_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    source: str = Field(default="manual", pattern="^(manual|intervention)$")


class UnblockUserProxyRequest(BaseModel):
    blocker_id: str = Field(min_length=1, max_length=128)
    blocked_id: str = Field(min_length=1, max_length=128)


class ReportUserProxyRequest(BaseModel):
    reporter_id: str = Field(min_length=1, max_length=128)
    reported_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    reason_category: str = Field(min_length=1, max_length=30)
    detail_text: str | None = Field(default=None, max_length=2000)
    triggered_by_msg_id: str | None = Field(default=None, max_length=128)


def _error_detail(response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except Exception:
        pass
    return "風險服務處理失敗，請稍後再試"


def _post(path: str, payload: dict) -> dict:
    try:
        response = requests.post(
            f"{_RISK_SERVICE_URL}/api/v1/risk/{path}",
            json=payload,
            timeout=_RISK_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"[risk_actions] risk backend unreachable: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="風險服務暫時無法連線，請稍後再試") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_error_detail(response))
    return response.json()


def _get(path: str, *, params: dict | None = None, raw_query: str = "") -> dict:
    target = f"{_RISK_SERVICE_URL}/api/v1/risk/{path}"
    if raw_query:
        target = f"{target}?{raw_query}"
    try:
        response = requests.get(target, params=params, timeout=_RISK_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[risk_actions] risk backend unreachable: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="風險服務暫時無法連線，請稍後再試") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_error_detail(response))
    return response.json()


@router.post("/pair/risk_feedback")
def submit_risk_feedback(req: RiskFeedbackRequest):
    """Forward receiver/sender intervention feedback to the risk backend."""
    if req.role not in ("sender", "receiver"):
        raise HTTPException(status_code=400, detail="role must be 'sender' or 'receiver'")
    if req.feedback not in ("comfortable", "uncomfortable"):
        raise HTTPException(status_code=400, detail="feedback must be 'comfortable' or 'uncomfortable'")
    return _post("feedback", req.model_dump(exclude_none=True))


@router.post("/pair/risk_appeal")
def submit_sender_appeal(req: SenderAppealProxyRequest):
    """Forward a sender appeal to the risk backend for manual review.

    The appeal text never enters any risk algorithm on the backend side.
    """
    return _post("appeal", req.model_dump())


@router.post("/pair/risk_report")
def submit_receiver_report(req: ReceiverReportProxyRequest):
    """Forward a receiver report to the risk backend for manual review.

    Symmetric to /pair/risk_appeal but from the protected party's side:
    one is the warned party's self-defense, the other is the protected
    party's statement. Kept as separate endpoints/attributes so the audit
    trail can distinguish them. The report text never enters any risk
    algorithm on the backend side.
    """
    return _post("report", req.model_dump())


@router.get("/pair/risk_state")
def get_risk_state(request: Request):
    """Forward a risk-state query to the risk backend.

    The Flutter app uses this when re-entering a chat room to restore the
    cooldown countdown. Without it, closing and reopening the app makes the
    cooldown appear to vanish even though the server has not lifted it.
    The risk backend's GET /api/v1/risk/state returns remaining_cooldown;
    this proxy is a thin transport that forwards the query string verbatim.
    """
    return _get("state", raw_query=request.url.query)


@router.post("/pair/risk_block")
def block_user(req: BlockUserProxyRequest):
    return _post("block", req.model_dump(exclude_none=True))


@router.post("/pair/risk_unblock")
def unblock_user(req: UnblockUserProxyRequest):
    return _post("unblock", req.model_dump())


@router.get("/pair/risk_blocks")
def list_blocked_users(user_id: str):
    payload = _get("blocks", params={"user_id": user_id})
    outgoing = payload.get("blocked_user_ids")
    if not isinstance(outgoing, list):
        raise HTTPException(status_code=502, detail="風險服務回傳格式錯誤")
    safe_ids = [str(item)[:128] for item in outgoing[:500] if str(item or "").strip()]
    return {
        "user_id": user_id,
        "blocked_user_ids": safe_ids,
        "count": len(safe_ids),
    }


@router.post("/pair/risk_report_user")
def report_user(req: ReportUserProxyRequest):
    return _post("report-user", req.model_dump(exclude_none=True))
