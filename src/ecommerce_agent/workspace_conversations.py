"""Typed contracts for the durable统筹 Agent conversation boundary.

The database-backed repository is intentionally supplied by the Schema 31
migration.  These contracts keep API and UI work independent from that
migration while making the persistence payload explicit and validated.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .message_media import annotate_workspace_history_content
from .text_utils import redact_sensitive


WorkspaceRole = Literal["user", "assistant"]
WorkspaceConversationStatus = Literal["active", "archived"]
WorkspaceMessageStatus = Literal["completed", "generating", "incomplete"]


class WorkspaceConversationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=120)
    status: WorkspaceConversationStatus = "active"
    message_count: int = Field(ge=0)
    created_at: datetime | str
    updated_at: datetime | str


class WorkspaceMessageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    role: WorkspaceRole
    content: str = Field(min_length=1, max_length=12000)
    status: WorkspaceMessageStatus = "completed"
    created_at: datetime | str
    updated_at: datetime | str
    trace_id: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=128)
    tool_label: str | None = Field(default=None, max_length=200)
    tool_summary: str | None = Field(default=None, max_length=2400)
    requires_confirmation: bool = False
    action_summary: str | None = Field(default=None, max_length=500)
    processing: dict[str, Any] = Field(default_factory=dict)


_TITLE_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}(?![A-Za-z0-9.-])"
)
_TITLE_ADDRESS_PATTERN = re.compile(
    r"(?P<label>收货地址|收件地址|配送地址|寄送地址|联系地址|客户地址|顾客地址|买家地址|地址)"
    r"\s*[:：=]?\s*"
    r"(?P<value>[^，。；;，,\n]{2,80}"
    r"(?:省|市|区|县|路|街|号|栋|单元|室|镇|村)"
    r"[^，。；;，,\n]*)"
)


def redact_workspace_title(value: str) -> str:
    """Sanitize user-provided workspace titles before storage or display."""

    sanitized, _ = redact_sensitive(value)
    sanitized = _TITLE_EMAIL_PATTERN.sub("[已脱敏邮箱]", sanitized)
    sanitized = _TITLE_ADDRESS_PATTERN.sub(
        lambda match: f"{match.group('label')}[已脱敏]", sanitized
    )
    return " ".join(sanitized.split())


def derive_workspace_title(message: str, *, limit: int = 20) -> str:
    """Build a stable display title from the first user message."""

    normalized = redact_workspace_title(message)
    if len(normalized) <= limit:
        return normalized or "新会话"
    truncated = normalized[:limit]
    # Do not expose a half-written redaction marker when the title limit cuts
    # through the replacement text.
    if truncated.rfind("[") > truncated.rfind("]"):
        return truncated[: truncated.rfind("[")].rstrip() + "…"
    return truncated


def build_workspace_history(
    messages: list[dict[str, Any]], *, limit: int = 12
) -> list[dict[str, str]]:
    """Return the latest model-safe user/assistant turns only."""

    if limit < 1:
        return []
    history = []
    for item in messages:
        if item.get("role") not in {"user", "assistant"}:
            continue
        content = annotate_workspace_history_content(
            str(item.get("content", "")), item.get("processing")
        )
        if content.strip():
            history.append({"role": str(item["role"]), "content": content})
    return history[-limit:]


def workspace_vision_processing(event: dict[str, Any]) -> dict[str, Any]:
    """Build a redacted, non-authoritative image record for workspace history."""

    status = str(event.get("status") or "unknown")
    applied = bool(event.get("applied"))
    processing: dict[str, Any] = {
        "image_attached": True,
        "vision_status": status,
        "vision_applied": applied,
        "vision_model": event.get("model"),
        "vision_latency_ms": event.get("latency_ms"),
    }
    evidence = event.get("evidence")
    if applied and isinstance(evidence, dict):
        description = evidence.get("description")
        if isinstance(description, str) and description.strip():
            safe_description, _ = redact_sensitive(description[:2000])
            processing["vision_evidence"] = {
                "status": "applied",
                "source_kind": "customer_image",
                "description": safe_description.strip(),
                "authority": "multimodal_model_observation",
                "semantic_authority": False,
                "business_execution_authority": False,
            }
    return processing
