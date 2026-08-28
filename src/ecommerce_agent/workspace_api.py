from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import StreamingResponse

from .auth import AdminPrincipal
from .schemas import ALLOWED_CHAT_IMAGE_MIME_TYPES, MAX_CHAT_IMAGE_BYTES
from .service import VERIFIED_FINAL_DELIVERY_MODE
from .text_utils import redact_sensitive
from .workspace_agent import (
    WorkspaceAgent,
    WorkspaceChatRequest,
    WorkspaceContext,
    WorkspaceHistoryItem,
    WorkspaceMessageContent,
    WORKSPACE_IMAGE_ONLY_MESSAGE,
)
from .workspace_conversations import (
    build_workspace_history,
    derive_workspace_title,
    redact_workspace_title,
    workspace_vision_processing,
)


logger = logging.getLogger("ecommerce_agent.workspace_api")


class WorkspaceConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)


class WorkspaceConversationChatRequest(WorkspaceMessageContent):
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

    def safe_title(value: str, *, derive: bool = False) -> str:
        sanitized = redact_workspace_title(value)
        return derive_workspace_title(sanitized) if derive else sanitized.strip()

    def public_conversation(conversation: dict) -> dict:
        result = {
            key: conversation[key]
            for key in (
                "id",
                "title",
                "status",
                "message_count",
                "created_at",
                "updated_at",
            )
        }
        result["title"] = safe_title(str(result["title"]))
        return result

    @router.post("/conversations", status_code=201)
    def create_conversation(
        payload: WorkspaceConversationCreateRequest | None = None,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        title = safe_title((payload.title if payload else None) or "新会话")
        conversation = agent.service.db.create_workspace_conversation(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            title=derive_workspace_title(title),
        )
        return public_conversation(conversation)


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
                title=(safe_title(payload.title) if payload.title is not None else None),
                status=payload.status,
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="workspace conversation not found"
            )
        return public_conversation(conversation)


    @router.get("/conversations")
    def list_conversations(
        limit: int = Query(default=20, ge=1, le=100),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        return [
            public_conversation(item)
            for item in agent.service.db.list_workspace_conversations(
                tenant_id=admin.tenant_id, admin_id=admin.admin_id, limit=limit
            )
        ]

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
        health = agent.service.health()
        vision = health.get("vision") or {}
        polish = health.get("polish") or {}
        return {
            "mode": "read_first",
            "automatic_writes": False,
            "write_requests": "confirmation_required",
            "tools": agent.tool_catalog(),
            "workspace_multimodal": {
                "entrypoint": "/admin",
                "paste_supported": True,
                "optional_file_selection": True,
                "image_mime_types": list(ALLOWED_CHAT_IMAGE_MIME_TYPES),
                "max_image_bytes": MAX_CHAT_IMAGE_BYTES,
            },
            "customer_service": {
                "entrypoint": "/customer-test",
                "stream_entrypoint": "/v1/test/customer-chat/stream",
                "advanced_entrypoint": "/admin/advanced",
                "simulation_only": True,
                "multimodal": {
                    "enabled": bool(vision.get("enabled")),
                    "ready": bool(vision.get("ok")),
                    "status": str(vision.get("detail") or "unknown"),
                    "model": vision.get("name"),
                },
                "polish": {
                    "enabled": bool(polish.get("enabled")),
                    "ready": bool(polish.get("ok")),
                    "status": str(polish.get("detail") or "unknown"),
                    "model": polish.get("name"),
                },
            },
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
        safe_message = safe_message.strip()
        effective_message = safe_message or WORKSPACE_IMAGE_ONLY_MESSAGE
        persisted_message = (
            f"（已粘贴图片）{effective_message}"
            if payload.image is not None
            else effective_message
        )
        persisted = db.list_workspace_messages(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            conversation_id=conversation_id,
            limit=12,
        )
        history = [
            WorkspaceHistoryItem.model_validate(item)
            for item in build_workspace_history(
                [item for item in persisted if item["status"] == "completed"],
                limit=12,
            )
        ]
        if conversation["message_count"] == 0:
            db.update_workspace_conversation_title(
                tenant_id=admin.tenant_id,
                admin_id=admin.admin_id,
                conversation_id=conversation_id,
                title=safe_title(
                    safe_message or "图片咨询",
                    derive=True,
                ),
            )
        user_message = db.append_workspace_message(
            tenant_id=admin.tenant_id,
            admin_id=admin.admin_id,
            conversation_id=conversation_id,
            role="user",
            content=persisted_message,
            processing={"image_attached": payload.image is not None},
        )
        user_message_id = user_message["id"]
        request = WorkspaceChatRequest(
            session_id=conversation_id,
            message=effective_message,
            history=history,
            context=payload.context,
            image=payload.image,
        )

        def events():
            latest_tool: dict = {}
            processing: dict[str, Any] = {
                "stage": "generating",
                "delivery_mode": VERIFIED_FINAL_DELIVERY_MODE,
            }
            current_status = "generating"
            placeholder = db.append_workspace_message(
                tenant_id=admin.tenant_id,
                admin_id=admin.admin_id,
                conversation_id=conversation_id,
                role="assistant",
                content="",
                status="generating",
                processing=processing,
            )
            message_id = placeholder["id"]

            def save_message(
                updates: dict[str, Any],
                *,
                status: str | None = None,
                content: str | None = None,
                metadata: dict[str, Any] | None = None,
            ) -> None:
                nonlocal current_status
                processing.update(updates)
                if status is not None:
                    current_status = status
                db.update_workspace_message(
                    tenant_id=admin.tenant_id,
                    admin_id=admin.admin_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    status=current_status,
                    content=content,
                    processing=dict(processing),
                    metadata=metadata,
                )

            try:
                for event in agent.stream(request, admin):
                    event_name = event.get("event")
                    if event_name == "status":
                        save_message(
                            {
                                "stage": str(event.get("stage") or "processing"),
                                "status_message": redact_sensitive(
                                    str(event.get("message") or "")
                                )[0][:500],
                            }
                        )
                    if event.get("event") == "tool":
                        latest_tool = event
                        tool_event = {
                            "tool_name": event.get("tool_name"),
                            "tool_label": event.get("tool_label"),
                            "status": event.get("status"),
                            "summary": redact_sensitive(
                                str(event.get("summary") or "")
                            )[0][:1200],
                            "task_id": event.get("task_id"),
                        }
                        tool_events = list(processing.get("tool_events") or [])
                        tool_events.append(tool_event)
                        save_message(
                            {
                                "stage": "observing",
                                "tool_name": event.get("tool_name"),
                                "tool_label": event.get("tool_label"),
                                "tool_summary": tool_event["summary"],
                                "tool_events": tool_events[-12:],
                            }
                        )
                    if event.get("event") == "vision":
                        db.update_workspace_message(
                            tenant_id=admin.tenant_id,
                            admin_id=admin.admin_id,
                            conversation_id=conversation_id,
                            message_id=user_message_id,
                            status="completed",
                            processing=workspace_vision_processing(event),
                        )
                    if event_name == "error":
                        error_code = str(event.get("code") or "workspace_error")[:120]
                        error_message = redact_sensitive(
                            str(event.get("message") or "本轮回答未完成，请稍后重试。")
                        )[0][:500]
                        save_message(
                            {
                                "stage": "error",
                                "error_code": error_code,
                                "error_message": error_message,
                                "retry_advised": bool(event.get("retry_advised")),
                                "trace_id": event.get("trace_id"),
                            },
                            status="incomplete",
                            content="本轮回答未完成，请稍后重试。",
                            metadata={"trace_id": event.get("trace_id")},
                        )
                    if event.get("event") == "done":
                        result = event.get("response") or {}
                        safe_answer, _ = redact_sensitive(
                            str(result.get("answer") or "")
                        )
                        completion_status = str(
                            result.get("completion_status") or "completed"
                        )
                        message_status = (
                            "incomplete"
                            if completion_status == "failed" or not safe_answer.strip()
                            else "completed"
                        )
                        if not safe_answer.strip():
                            safe_answer = "本轮核实未完成，请稍后重试。"
                        degraded_reasons = [
                            redact_sensitive(str(item))[0][:120]
                            for item in (result.get("degraded_reasons") or [])
                            if str(item).strip()
                        ]
                        processing_updates = {
                            "stage": message_status,
                            "delivery_mode": str(
                                result.get("delivery_mode")
                                or VERIFIED_FINAL_DELIVERY_MODE
                            ),
                            "trace_id": result.get("trace_id"),
                            "tool_name": result.get("tool_name"),
                            "tool_label": result.get("tool_label"),
                            "tool_summary": latest_tool.get("summary"),
                            "requires_confirmation": bool(
                                result.get("requires_confirmation")
                            ),
                            "action_summary": result.get("action_summary"),
                            "image_attached": bool(result.get("image_attached")),
                            "vision_status": result.get("vision_status"),
                            "vision_applied": bool(result.get("vision_applied")),
                            "vision_model": result.get("vision_model"),
                            "vision_latency_ms": result.get("vision_latency_ms"),
                            "completion_status": completion_status,
                            "degraded": bool(result.get("degraded")),
                            "degraded_reasons": degraded_reasons,
                            "decision_steps": result.get("decision_steps"),
                            "limit_reached": bool(result.get("limit_reached")),
                            "mode": result.get("mode"),
                            "reason": result.get("reason"),
                        }
                        db.update_workspace_message(
                            tenant_id=admin.tenant_id,
                            admin_id=admin.admin_id,
                            conversation_id=conversation_id,
                            message_id=message_id,
                            status=message_status,
                            content=safe_answer,
                            metadata={
                                "trace_id": result.get("trace_id"),
                                "tool_name": result.get("tool_name"),
                                "tool_label": result.get("tool_label"),
                                "tool_summary": latest_tool.get("summary"),
                                "requires_confirmation": bool(
                                    result.get("requires_confirmation")
                                ),
                                "action_summary": result.get("action_summary"),
                                "image_attached": bool(result.get("image_attached")),
                                "vision_status": result.get("vision_status"),
                                "vision_applied": bool(result.get("vision_applied")),
                                "vision_model": result.get("vision_model"),
                                "vision_latency_ms": result.get("vision_latency_ms"),
                            },
                            processing={**processing, **processing_updates},
                        )
                        processing.update(processing_updates)
                        current_status = message_status
                    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"data: {encoded}\n\n"
            except Exception as exc:
                trace_id = f"workspace-error-{uuid.uuid4().hex}"
                message = "本轮回答未完成，请稍后重试。"
                logger.error(
                    "workspace stream failed trace_id=%s error_type=%s detail=%s",
                    trace_id,
                    type(exc).__name__,
                    redact_sensitive(str(exc))[0],
                )
                save_message(
                    {
                        "stage": "error",
                        "error_code": "workspace_stream_failed",
                        "error_type": type(exc).__name__,
                        "error_message": message,
                        "trace_id": trace_id,
                    },
                    status="incomplete",
                    content=message,
                    metadata={"trace_id": trace_id},
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
                    save_message(
                        {
                            "stage": "error",
                            "error_code": "generator_exit",
                            "error_type": "generator_exit",
                            "error_message": "本轮回答未完成，请稍后重试。",
                        },
                        status="incomplete",
                        content="本轮回答未完成，请稍后重试。",
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
        safe_message, _ = redact_sensitive(payload.message)
        safe_message = safe_message.strip()
        try:
            agent.service.db.get_or_create_workspace_conversation(
                tenant_id=admin.tenant_id,
                admin_id=admin.admin_id,
                conversation_id=payload.session_id,
                title=safe_title(
                    safe_message or "图片咨询",
                    derive=True,
                ),
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="workspace conversation not found"
            )
        # Compatibility entry: the durable conversation path owns persistence,
        # history, and stream semantics. Client-supplied history is intentionally
        # ignored so callers cannot replace the server-side conversation record.
        return conversation_chat_stream(
            payload.session_id,
            WorkspaceConversationChatRequest(
                message=payload.message,
                context=payload.context,
                image=payload.image,
            ),
            admin,
        )

    return router
