"""Authenticated owner management for person-scoped relationship memories."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.appwrite_identity_service import authenticated_owner_matches
from services.ayue_agent.public_relationship_projection import display_name
from services.relationship_memory_service import (
    list_relationship_memories,
    update_relationship_memory,
)


router = APIRouter()


class RelationshipMemoryActionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    memory_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1)
    action: str
    statement: str | None = Field(default=None, max_length=300)
    facet_id: str | None = Field(default=None, max_length=100)


def _require_owner(request: Request, user_id: str) -> None:
    if not authenticated_owner_matches(request.headers.get("Authorization"), user_id):
        raise HTTPException(status_code=403, detail="需要本人登入驗證")


@router.get("/relationship/memories")
def get_relationship_memories(request: Request, user_id: str, other_id: str | None = None):
    _require_owner(request, user_id)
    rows = list_relationship_memories(user_id, other_id)
    names: dict[str, str] = {}
    for row in rows:
        other = row["other_user_id"]
        names.setdefault(other, display_name(other))
        row["other_display_name"] = names[other]
    return {"memories": rows}


@router.post("/relationship/memories/action")
def relationship_memory_action(req: RelationshipMemoryActionRequest, request: Request):
    _require_owner(request, req.user_id)
    action = req.action.strip().lower()
    if action not in {"update", "delete", "undo"}:
        raise HTTPException(status_code=422, detail="不支援的關係記憶操作")
    updated = update_relationship_memory(
        req.user_id,
        req.memory_id,
        expected_version=req.expected_version,
        statement=req.statement,
        delete=action in {"delete", "undo"},
        facet_id=req.facet_id,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="記憶已更新，請重新整理")
    return {"memory": updated}
