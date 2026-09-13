"""Authenticated HTTP boundary for the optional read-only Google Calendar link."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse

from services.appwrite_identity_service import AppwriteIdentityError, authenticate_owner
from services.calendar_service import get_timezone
from services.google_calendar_service import (
    GoogleCalendarError,
    get_google_calendar_service,
)


router = APIRouter(prefix="/api/integrations/google-calendar", tags=["Google Calendar"])


def _detail(error: GoogleCalendarError | AppwriteIdentityError) -> dict[str, str]:
    return {"code": error.code, "message": error.code}


def _mark_private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _owner_id(request: Request) -> str:
    try:
        return authenticate_owner(request.headers.get("Authorization"))
    except AppwriteIdentityError as exc:
        raise HTTPException(status_code=exc.status_code, detail=_detail(exc)) from exc


def _calendar_range(from_value: str, to_value: str) -> tuple[datetime, datetime]:
    try:
        start_day = date.fromisoformat(from_value)
        end_day = date.fromisoformat(to_value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "google_calendar_range_invalid", "message": "日期格式需為 YYYY-MM-DD"},
        ) from exc
    if end_day < start_day or (end_day - start_day).days > 91:
        raise HTTPException(
            status_code=400,
            detail={"code": "google_calendar_range_invalid", "message": "查詢區間最多 92 天"},
        )
    zone = get_timezone("Asia/Taipei")
    start = datetime.combine(start_day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(end_day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    return start, end


def _callback_page(title: str, message: str, *, success: bool) -> HTMLResponse:
    color = "#2f6d5b" if success else "#9a4a42"
    body = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;background:#f6f8fc;color:#182033;margin:0;display:grid;min-height:100vh;place-items:center}}main{{max-width:32rem;margin:1.5rem;padding:2rem;border:1px solid #dce2ec;border-radius:1rem;background:#fff;box-shadow:0 1rem 3rem rgba(24,32,51,.08)}}h1{{font-size:1.55rem;color:{color}}}p{{line-height:1.7}}small{{color:#687184}}</style>
</head><body><main><h1>{escape(title)}</h1><p>{escape(message)}</p><small>你可以關閉這個頁面，返回阿月 Dating App。</small></main></body></html>"""
    return HTMLResponse(
        body,
        status_code=200 if success else 400,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@router.get("/status")
def connection_status(response: Response, owner_id: str = Depends(_owner_id)):
    _mark_private(response)
    return get_google_calendar_service().status(owner_id)


@router.post("/authorize")
def authorize(response: Response, owner_id: str = Depends(_owner_id)):
    _mark_private(response)
    try:
        return get_google_calendar_service().start_authorization(owner_id)
    except GoogleCalendarError as exc:
        raise HTTPException(status_code=exc.status_code, detail=_detail(exc)) from exc


@router.get("/callback", response_class=HTMLResponse)
def callback(
    state: str = Query("", max_length=512),
    code: str | None = Query(None, max_length=4096),
    error: str | None = Query(None, max_length=256),
):
    try:
        result = get_google_calendar_service().complete_authorization(
            state=state,
            code=code,
            oauth_error=error,
        )
    except GoogleCalendarError:
        return _callback_page(
            "無法完成 Google Calendar 連接",
            "這次授權已失效或無法驗證，請返回 APP 後重新連接。",
            success=False,
        )
    if result == "cancelled":
        return _callback_page(
            "已取消 Google Calendar 授權",
            "你的 Google Calendar 尚未連接，APP 不會讀取任何行程。",
            success=False,
        )
    return _callback_page(
        "Google Calendar 已連接",
        "唯讀授權已完成；阿月 Dating App 無法修改或刪除你的 Google 行程。",
        success=True,
    )


@router.get("/events")
def events(
    response: Response,
    from_value: str = Query(..., alias="from"),
    to_value: str = Query(..., alias="to"),
    owner_id: str = Depends(_owner_id),
):
    _mark_private(response)
    start, end = _calendar_range(from_value, to_value)
    try:
        return get_google_calendar_service().list_events(owner_id, start, end)
    except GoogleCalendarError as exc:
        raise HTTPException(status_code=exc.status_code, detail=_detail(exc)) from exc


@router.delete("/connection")
def disconnect(response: Response, owner_id: str = Depends(_owner_id)):
    _mark_private(response)
    return get_google_calendar_service().disconnect(owner_id)
