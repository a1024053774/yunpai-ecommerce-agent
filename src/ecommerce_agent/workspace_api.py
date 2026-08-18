from __future__ import annotations

import json
import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import StreamingResponse

from .auth import AdminPrincipal
from .text_utils import redact_sensitive
from .workspace_agent import (
    WorkspaceAgent,
    WorkspaceChatRequest,
    WorkspaceContext,
    WorkspaceHistoryItem,
)
from .workspace_conversations import derive_workspace_title


class WorkspaceConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)


class WorkspaceConversationChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    context: WorkspaceContext = Field(default_factory=WorkspaceContext)


class WorkspaceConversationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)
    status: str | None = Field(default=None, pattern=r"^(active|archived)$")


def build_workspace_router(
    agent: WorkspaceAgent,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin/workspace", tags=["workspace-agent"])

    @router.post("/conversations", status_code=201)
    def create_conversation(
        payload: WorkspaceConversationCreateRequest | None = None,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        title = (payload.title if payload else None) or "新会话"
        conversation = agent.service.db.create_workspace_conversation(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            title=derive_workspace_title(title),
        )
        return {
            key: conversation[key]
            for key in ("id", "title", "status", "message_count", "created_at", "updated_at")
        }


    @router.patch("/conversations/{conversation_id}")
    def update_conversation(
        conversation_id: str,
        payload: WorkspaceConversationUpdateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            conversation = agent.service.db.update_workspace_conversation(
                tenant_id=admin.tenant_id,
                admin_id=admin.admin_id,
                conversation_id=conversation_id,
                title=payload.title,
                status=payload.status,
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="workspace conversation not found"
            )
        return {
            key: conversation[key]
            for key in ("id", "title", "status", "message_count", "created_at", "updated_at")
        }


    @router.get("/conversations")
    def list_conversations(
        limit: int = Query(default=20, ge=1, le=100),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        return agent.service.db.list_workspace_conversations(
            tenant_id=admin.tenant_id, admin_id=admin.admin_id, limit=limit
        )

    @router.get("/conversations/{conversation_id}/messages")
    def list_conversation_messages(
        conversation_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        conversation = agent.service.db.get_workspace_conversation(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            conversation_id=conversation_id,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="workspace conversation not found")
        return agent.service.db.list_workspace_messages(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            conversation_id=conversation_id,
            limit=limit,
        )

    @router.get("/capabilities")
    def capabilities(
        _: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return {
            "mode": "read_first",
            "automatic_writes": False,
            "write_requests": "confirmation_required",
            "tools": agent.tool_catalog(),
        }

    @router.post("/conversations/{conversation_id}/chat/stream")
    def conversation_chat_stream(
        conversation_id: str,
        payload: WorkspaceConversationChatRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> StreamingResponse:
        db = agent.service.db
        conversation = db.get_workspace_conversation(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            conversation_id=conversation_id,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="workspace conversation not found")
        db.recover_stale_generating(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            conversation_id=conversation_id,
        )
        safe_message, _ = redact_sensitive(payload.message)
        persisted = db.list_workspace_messages(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            conversation_id=conversation_id,
            limit=12,
        )
        history = [
            WorkspaceHistoryItem(role=item["role"], content=item["content"])
            for item in persisted
            if item["status"] == "completed" and item["role"] in {"user", "assistant"}
        ][-12:]
        if conversation["message_count"] == 0:
            db.update_workspace_conversation_title(
                tenant_id=admin.tenant_id,
                admin_id=admin.admin_id,
                conversation_id=conversation_id,
                title=derive_workspace_title(safe_message),
            )
        db.append_workspace_message(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            conversation_id=conversation_id,
            role="user",
            content=safe_message,
        )
        request = WorkspaceChatRequest(
            session_id=conversation_id,
            message=safe_message,
            history=history,
            context=payload.context,
        )

        def events():
            latest_tool: dict = {}
            placeholder = db.append_workspace_message(
                tenant_id=admin.tenant_id,
                admin_id=admin.admin_id,
                conversation_id=conversation_id,
                role="assistant",
                content="",
                status="generating",
                processing={"stage": "generating"},
            )
            message_id = placeholder["id"]
            try:
                for event in agent.stream(request, admin):
                    if event.get("event") == "tool":
                        latest_tool = event
                    if event.get("event") == "done":
                        result = event.get("response") or {}
                        safe_answer, _ = redact_sensitive(
                            str(result.get("answer") or "")
                        )
                        db.update_workspace_message(
                            tenant_id=admin.tenant_id,
                            admin_id=admin.admin_id,
                            conversation_id=conversation_id,
                            message_id=message_id,
                            status="completed",
                            content=safe_answer,
                            processing={
                                "stage": "completed",
                                "trace_id": result.get("trace_id"),
                                "tool_name": result.get("tool_name"),
                                "tool_label": result.get("tool_label"),
                                "tool_summary": latest_tool.get("summary"),
                                "requires_confirmation": bool(result.get("requires_confirmation")),
                                "action_summary": result.get("action_summary"),
                            },
                        )
                    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"data: {encoded}\n\n"
            except Exception as exc:
                trace_id = f"workspace-error-{uuid.uuid4().hex}"
                message = "本轮回答未完成，请稍后重试。"
                db.update_workspace_message(
                    tenant_id=admin.tenant_id,
                    admin_id=admin.admin_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    status="incomplete",
                    content=message,
                    processing={"stage": "error", "error_type": type(exc).__name__},
                )
                yield f"data: {json.dumps({'event': 'error', 'code': 'workspace_stream_failed', 'message': message, 'trace_id': trace_id}, ensure_ascii=False)}\n\n"
            finally:
                current = db.get_workspace_message(
                    tenant_id=admin.tenant_id,
                    admin_id=admin.admin_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                )
                if current is not None and current["status"] == "generating":
                    db.update_workspace_message(
                        tenant_id=admin.tenant_id,
                        admin_id=admin.admin_id,
                        conversation_id=conversation_id,
                        message_id=message_id,
                        status="incomplete",
                        content="本轮回答未完成，请稍后重试。",
                        processing={"stage": "error", "error_type": "generator_exit"},
                    )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/chat/stream")
    def chat_stream(
        payload: WorkspaceChatRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> StreamingResponse:
        def events():
            for event in agent.stream(payload, admin):
                encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                yield f"data: {encoded}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
