from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from .auth import AdminPrincipal
from .workspace_agent import WorkspaceAgent, WorkspaceChatRequest


def build_workspace_router(
    agent: WorkspaceAgent,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin/workspace", tags=["workspace-agent"])

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
