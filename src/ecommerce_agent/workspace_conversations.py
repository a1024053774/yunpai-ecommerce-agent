"""Typed contracts for the durable统筹 Agent conversation boundary.

The database-backed repository is intentionally supplied by the Schema 31
migration.  These contracts keep API and UI work independent from that
migration while making the persistence payload explicit and validated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


WorkspaceRole = Literal["user", "assistant"]
WorkspaceConversationStatus = Literal["active", "archived"]


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
    created_at: datetime | str
    trace_id: str | None = Field(default=None, max_length=128)
    processing: dict[str, Any] = Field(default_factory=dict)


def derive_workspace_title(message: str, *, limit: int = 20) -> str:
    """Build a stable display title from the first user message."""

    normalized = " ".join(message.split())
    return normalized[:limit] or "新会话"


def build_workspace_history(
    messages: list[dict[str, Any]], *, limit: int = 12
) -> list[dict[str, str]]:
    """Return the latest model-safe user/assistant turns only."""

    if limit < 1:
        return []
    history = [
        {"role": str(item["role"]), "content": str(item["content"])}
        for item in messages
        if item.get("role") in {"user", "assistant"}
        and str(item.get("content", "")).strip()
    ]
    return history[-limit:]
