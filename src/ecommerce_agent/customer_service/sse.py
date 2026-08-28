from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from ..database import SessionScopeError
from ..llm import ModelError, ModelUnavailableError


def encode_sse(event: dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"data: {data}\n\n"


def project_chat_sse_events(
    stream_factory: Callable[[], Iterator[dict[str, Any]]],
    *,
    include_response: bool = False,
) -> Iterator[dict[str, Any]]:
    metadata: dict[str, Any] = {}
    generated = False
    try:
        for item in stream_factory():
            event_name = item["event"]
            if event_name == "meta":
                metadata = item
                yield item
                continue
            if event_name == "delta":
                generated = generated or not item.get("replay", False)
                yield {"event": "delta", "text": item["text"]}
                continue

            response = item["response"]
            if generated and response["sources"]:
                yield {"event": "citations", "sources": response["sources"]}
            if response["requires_human"]:
                yield {
                    "event": "handoff",
                    "requires_human": True,
                    "handoff_id": response["handoff_id"],
                    "handoff_status": response["handoff_status"],
                    "reason": response["reason"],
                }
            if include_response:
                yield {"event": "result", "response": response}
            yield {
                "event": "done",
                "message_id": response["message_id"],
                "intent": response["intent"],
                "risk_level": response["risk_level"],
                "model_fallback": response["model_fallback"],
            }
    except ModelUnavailableError:
        yield {
            "event": "error",
            "code": "model_unavailable",
            "message": "model service is temporarily unavailable",
            "retry_advised": True,
        }
        yield _error_done(metadata)
    except ModelError:
        yield {
            "event": "error",
            "code": "model_error",
            "message": "model generation failed",
            "retry_advised": False,
        }
        yield _error_done(metadata)
    except SessionScopeError as exc:
        yield {
            "event": "error",
            "code": getattr(exc, "code", "session_conflict"),
            "message": str(exc),
            "retry_advised": False,
        }
        yield _error_done(metadata)
    except Exception:
        yield {
            "event": "error",
            "code": "internal_error",
            "message": "streaming response failed",
            "retry_advised": False,
        }
        yield _error_done(metadata)


def _error_done(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": "done",
        "message_id": metadata.get("message_id", ""),
        "intent": "unknown",
        "risk_level": "low",
        "model_fallback": True,
    }
