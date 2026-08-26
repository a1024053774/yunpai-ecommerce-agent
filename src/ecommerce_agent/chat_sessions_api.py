from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth import Principal
from .database import SessionScopeError
from .message_media import non_media_sources, public_message_media
from .service import AgentService


class ChatSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )


def build_chat_sessions_router(
    service: AgentService,
    require_client: Callable[..., Principal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/chat/sessions", tags=["chat-sessions"])

    def scoped_session(
        external_session_id: str,
        principal: Principal,
    ) -> dict[str, Any] | None:
        with service.db.connect() as conn:
            row = conn.execute(
                """
                SELECT s.id AS internal_id, s.external_session_id AS id,
                       s.status, s.created_at, s.last_seen_at,
                       COUNT(m.id) AS message_count
                FROM sessions s
                LEFT JOIN messages m ON m.session_id=s.id
                WHERE s.external_session_id=?
                  AND s.tenant_id=? AND s.subject_hash=?
                GROUP BY s.id
                """,
                (
                    external_session_id,
                    principal.tenant_id,
                    principal.subject_hash,
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    @router.post("")
    def create_session(
        payload: ChatSessionCreateRequest,
        response: Response,
        principal: Principal = Depends(require_client),
    ) -> dict[str, Any]:
        with service.db.connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM sessions
                WHERE external_session_id=? AND tenant_id=? AND subject_hash=?
                """,
                (
                    payload.session_id,
                    principal.tenant_id,
                    principal.subject_hash,
                ),
            ).fetchone()
        try:
            service.db.resolve_session(
                tenant_id=principal.tenant_id,
                client_id=principal.client_id,
                external_session_id=payload.session_id,
                subject_hash=principal.subject_hash,
            )
        except SessionScopeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session = scoped_session(payload.session_id, principal)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        session.pop("internal_id")
        response.status_code = 200 if existing is not None else 201
        return session

    @router.get("/{session_id}")
    def get_session(
        session_id: str,
        principal: Principal = Depends(require_client),
    ) -> dict[str, Any]:
        session = scoped_session(session_id, principal)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        session.pop("internal_id")
        return session

    @router.get("/{session_id}/messages")
    def list_messages(
        session_id: str,
        cursor: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        principal: Principal = Depends(require_client),
    ) -> dict[str, Any]:
        session = scoped_session(session_id, principal)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        page = service.db.paginated_messages(
            session["internal_id"],
            cursor,
            limit,
            tenant_id=principal.tenant_id,
            subject_hash=principal.subject_hash,
        )
        for item in page["items"]:
            sources_json = item.get("sources_json")
            item["media"] = public_message_media(
                sources_json,
                url_prefix=(
                    f"/v1/chat/sessions/{quote(session_id, safe='')}"
                    f"/messages/{quote(str(item['id']), safe='')}/media"
                ),
            )
            # 媒体元数据（内部 storage_ref、模型观察）不对外，只保留引用来源
            item["sources_json"] = json.dumps(
                non_media_sources(sources_json), ensure_ascii=False
            )
        return page

    @router.get(
        "/{session_id}/messages/{message_id}/media/{media_id}",
        response_class=FileResponse,
    )
    def get_message_media(
        session_id: str,
        message_id: str,
        media_id: str,
        principal: Principal = Depends(require_client),
    ) -> FileResponse:
        session = scoped_session(session_id, principal)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        result = service.message_media.resolve_for_message(
            service.db,
            tenant_id=principal.tenant_id,
            session_id=session["internal_id"],
            message_id=message_id,
            media_id=media_id,
            subject_hash=principal.subject_hash,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="message media not found")
        path, mime_type = result
        return FileResponse(
            path,
            media_type=mime_type,
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete("/{session_id}")
    def close_session(
        session_id: str,
        principal: Principal = Depends(require_client),
    ) -> dict[str, Any]:
        session = scoped_session(session_id, principal)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        with service.db._write_lock, service.db.connect() as conn:
            open_handoff = conn.execute(
                """
                SELECT 1
                FROM handoff_tasks h
                JOIN sessions s ON s.id=h.session_id
                WHERE h.session_id=? AND h.tenant_id=?
                  AND s.tenant_id=? AND s.subject_hash=?
                  AND h.status NOT IN ('completed','rejected','failed','canceled')
                LIMIT 1
                """,
                (
                    session["internal_id"],
                    principal.tenant_id,
                    principal.tenant_id,
                    principal.subject_hash,
                ),
            ).fetchone()
            if open_handoff is not None:
                raise HTTPException(
                    status_code=409,
                    detail="session has a non-terminal handoff",
                )
            conn.execute(
                """
                UPDATE sessions SET status='closed'
                WHERE id=? AND tenant_id=? AND subject_hash=?
                """,
                (
                    session["internal_id"],
                    principal.tenant_id,
                    principal.subject_hash,
                ),
            )
        closed = scoped_session(session_id, principal)
        if closed is None:
            raise HTTPException(status_code=404, detail="session not found")
        closed.pop("internal_id")
        return closed

    return router
