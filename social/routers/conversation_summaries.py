"""Owner-authenticated summary progress; never returns summary content."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from services.appwrite_identity_service import authenticate_owner, AppwriteIdentityError
from services.conversation_summary_operations import enqueue_rebuild, summary_status

router = APIRouter()


class RebuildRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    room_id: str = Field(min_length=1, max_length=128)


def _owner(request):
    try:
        return authenticate_owner(request.headers.get('Authorization'))
    except AppwriteIdentityError as exc:
        raise HTTPException(exc.status_code, detail='需要有效的本人登入驗證') from exc


@router.get('/conversation/summary-status')
def get_summary_status(request: Request, room_id: str):
    owner = _owner(request)
    if len(room_id) > 128:
        raise HTTPException(422, detail='聊天室參數無效')
    try:
        return summary_status(owner, room_id)
    except PermissionError as exc:
        raise HTTPException(404, detail='找不到可存取的聊天室') from exc
    except Exception as exc:
        raise HTTPException(503, detail='摘要狀態暫時無法取得') from exc


@router.post('/conversation/summary-rebuild')
def rebuild_summary(req: RebuildRequest, request: Request):
    owner = _owner(request)
    try:
        return enqueue_rebuild(owner, req.room_id, retry=True)
    except PermissionError as exc:
        raise HTTPException(404, detail='找不到可存取的聊天室') from exc
    except Exception as exc:
        raise HTTPException(503, detail='摘要重建暫時無法排入') from exc
