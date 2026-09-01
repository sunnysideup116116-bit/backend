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


@router.post("/pair/risk_feedback")
def submit_risk_feedback(req: RiskFeedbackRequest):
    """Forward receiver/sender intervention feedback to the risk backend."""
    if req.role not in ("sender", "receiver"):
        raise HTTPException(status_code=400, detail="role must be 'sender' or 'receiver'")
    if req.feedback not in ("comfortable", "uncomfortable"):
        raise HTTPException(status_code=400, detail="feedback must be 'comfortable' or 'uncomfortable'")
    try:
        response = requests.post(
            f"{_RISK_SERVICE_URL}/api/v1/risk/feedback",
            json=req.model_dump(exclude_none=True),
            timeout=_RISK_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"[risk_actions] risk backend unreachable: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="風險服務暫時無法連線，請稍後再試") from exc
    if response.status_code >= 400:
        detail = _error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()


@router.post("/pair/risk_appeal")
def submit_sender_appeal(req: SenderAppealProxyRequest):
    """Forward a sender appeal to the risk backend for manual review.

    The appeal text never enters any risk algorithm on the backend side.
    """
    try:
        response = requests.post(
            f"{_RISK_SERVICE_URL}/api/v1/risk/appeal",
            json=req.model_dump(),
            timeout=_RISK_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"[risk_actions] risk backend unreachable: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="風險服務暫時無法連線，請稍後再試") from exc
    if response.status_code >= 400:
        detail = _error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()


@router.post("/pair/risk_report")
def submit_receiver_report(req: ReceiverReportProxyRequest):
    """Forward a receiver report to the risk backend for manual review.

    Symmetric to /pair/risk_appeal but from the protected party's side:
    one is the warned party's self-defense, the other is the protected
    party's statement. Kept as separate endpoints/attributes so the audit
    trail can distinguish them. The report text never enters any risk
    algorithm on the backend side.
    """
    try:
        response = requests.post(
            f"{_RISK_SERVICE_URL}/api/v1/risk/report",
            json=req.model_dump(),
            timeout=_RISK_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"[risk_actions] risk backend unreachable: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="風險服務暫時無法連線，請稍後再試") from exc
    if response.status_code >= 400:
        detail = _error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()


@router.get("/pair/risk_state")
def get_risk_state(request: Request):
    """Forward a risk-state query to the risk backend.

    The Flutter app uses this when re-entering a chat room to restore the
    cooldown countdown. Without it, closing and reopening the app makes the
    cooldown appear to vanish even though the server has not lifted it.
    The risk backend's GET /api/v1/risk/state returns remaining_cooldown;
    this proxy is a thin transport that forwards the query string verbatim.
    """
    query = request.url.query
    target = f"{_RISK_SERVICE_URL}/api/v1/risk/state"
    if query:
        target = f"{target}?{query}"
    try:
        response = requests.get(target, timeout=_RISK_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[risk_actions] risk backend unreachable: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="風險服務暫時無法連線，請稍後再試") from exc
    if response.status_code >= 400:
        detail = _error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except Exception:
        pass
    return "風險服務處理失敗，請稍後再試"
