from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

from ecommerce_agent.llm import ModelUnavailableError
from ecommerce_agent.polish import PolishResult
import ecommerce_agent.service as service_module
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def test_chat_stream_mock_matches_non_stream_answer(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)
    try:
        expected = service.chat(principal, "stream-baseline", "尺码怎么选")
        events = list(
            service.chat_stream(
                principal,
                "stream-generate",
                "尺码怎么选",
                {},
                idempotency_key=None,
            )
        )

        assert events[0]["event"] == "meta"
        assert events[-1]["event"] == "result"
        assert "".join(
            event["text"] for event in events if event["event"] == "delta"
        ) == expected.answer
        assert events[-1]["response"]["answer"] == expected.answer
    finally:
        service.close()


def test_chat_stream_polished_answer_matches_non_stream_and_persistence(
    tmp_path,
) -> None:
    service = AgentService(replace(make_settings(tmp_path), polish_enabled=True))
    principal = principal_for(service)
    try:
        def polish(**request) -> PolishResult:
            return PolishResult(
                answer=f"您好，{request['raw_answer']}",
                status="applied",
                applied=True,
                latency_ms=1,
                model="qwen3-14b-rag-polish-test",
            )

        service.polisher.polish = polish  # type: ignore[method-assign]
        expected = service.chat(principal, "polish-sync", "尺码怎么选")
        events = list(
            service.chat_stream(
                principal,
                "polish-stream",
                "尺码怎么选",
                {},
                idempotency_key=None,
            )
        )

        streamed_answer = "".join(
            event["text"] for event in events if event["event"] == "delta"
        )
        assert streamed_answer == expected.answer
        assert events[-1]["response"]["answer"] == expected.answer
        assert expected.answer.startswith("您好，")
        assert expected.polish_status == "applied"
        assert expected.polish_applied is True
        assert expected.polish_model == "qwen3-14b-rag-polish-test"
        assert expected.polish_latency_ms == 1
        assert events[-1]["response"]["polish_status"] == "applied"
        assert events[-1]["response"]["polish_applied"] is True
        with service.db.connect() as conn:
            stored = dict(
                conn.execute(
                    """
                    SELECT s.external_session_id, m.content
                    FROM messages m
                    JOIN sessions s ON s.id=m.session_id
                    WHERE s.external_session_id IN (?, ?) AND m.role='assistant'
                    """,
                    ("polish-sync", "polish-stream"),
                ).fetchall()
            )
        assert stored == {
            "polish-sync": expected.answer,
            "polish-stream": expected.answer,
        }
    finally:
        service.close()


def test_chat_stream_does_not_emit_unverified_model_draft(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)
    try:
        def stream_generate(_: list[dict[str, str]]) -> Iterator[str]:
            yield "建议购买999件。"

        service.model.stream_generate = stream_generate  # type: ignore[method-assign]
        events = list(
            service.chat_stream(
                principal,
                "stream-unverified-draft",
                "尺码怎么选",
                {},
                idempotency_key=None,
            )
        )

        streamed_answer = "".join(
            event["text"] for event in events if event["event"] == "delta"
        )
        final_answer = events[-1]["response"]["answer"]
        assert "999" not in streamed_answer
        assert streamed_answer == final_answer
    finally:
        service.close()


def test_stream_generation_preserves_the_selected_prompt_variant(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    captured: list[list[dict[str, str]]] = []
    try:
        def stream_generate(messages: list[dict[str, str]]) -> Iterator[str]:
            captured.append(messages)
            yield "已记录"

        service.model.stream_generate = stream_generate  # type: ignore[method-assign]
        deltas, fallback, _, draft_origin = service._generation_deltas(
            {
                "normalized_input": "请查询售后进度",
                "session_id": "stream-planned-prompt",
                "intent": "product",
                "retrieved": [
                    {
                        "id": "kb-1",
                        "intent": "after_sales",
                        "category": "policy",
                        "question": "售后进度如何查询",
                        "answer": "可在订单售后页查看进度。",
                        "source": "seed:test",
                        "version": 1,
                        "score": 0.8,
                    }
                ],
                "context_bundle": {"recent_history": []},
                "tool_result": {},
                "decision": {"reason": "knowledge_is_relevant"},
                "intent_routing": {"prompt_variant": "after_sales_support"},
            }
        )

        assert list(deltas) == ["已记录"]
        assert fallback is False
        assert draft_origin == "model"
        assert "after_sales_support" in captured[0][-1]["content"]
        assert "【本会话场景指令】" in captured[0][0]["content"]
    finally:
        service.close()


def test_chat_stream_model_unavailable_persists_retry_response(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)

    def fail_stream(_: list[dict[str, str]]) -> Iterator[str]:
        yield "不应持久化的半截回复"
        raise ModelUnavailableError("model unavailable")

    service.model.stream_generate = fail_stream  # type: ignore[method-assign]
    try:
        events = list(
            service.chat_stream(
                principal,
                "stream-model-unavailable",
                "尺码怎么选",
                {},
                idempotency_key=None,
            )
        )
        response = events[-1]["response"]
        with service.db.connect() as conn:
            stored = conn.execute(
                """
                SELECT m.content FROM messages m
                JOIN sessions s ON s.id=m.session_id
                WHERE s.external_session_id=? AND m.role='assistant'
                """,
                ("stream-model-unavailable",),
            ).fetchone()[0]

        assert response["reason"] == "model_temporarily_unavailable"
        assert response["requires_human"] is False
        assert "稍等一下再发送一次" in response["answer"]
        assert "半截回复" not in response["answer"]
        assert stored == response["answer"]
    finally:
        service.close()


def test_chat_stream_persists_before_emitting_the_final_answer(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)
    iterator = service.chat_stream(
        principal,
        "stream-interrupted",
        "尺码怎么选",
        {},
        idempotency_key=None,
    )
    try:
        assert next(iterator)["event"] == "meta"
        iterator.close()

        with service.db.connect() as conn:
            assistant_count = conn.execute(
                """
                SELECT COUNT(*) FROM messages m
                JOIN sessions s ON s.id=m.session_id
                WHERE s.external_session_id=? AND m.role='assistant'
                """,
                ("stream-interrupted",),
            ).fetchone()[0]
        assert assistant_count == 1
    finally:
        iterator.close()
        service.close()


def test_chat_stream_builds_one_generation_plan(tmp_path, monkeypatch) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)
    calls = 0
    original = service_module.plan_generation

    def counted_plan(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "plan_generation", counted_plan)
    try:
        list(
            service.chat_stream(
                principal,
                "single-generation-plan",
                "尺码怎么选",
                {},
                idempotency_key=None,
            )
        )

        assert calls == 1
    finally:
        service.close()


def test_chat_stream_non_generation_route_emits_single_complete_result(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)
    try:
        events = list(
            service.chat_stream(
                principal,
                "stream-handoff",
                "转人工",
                {},
                idempotency_key=None,
            )
        )

        assert [event["event"] for event in events] == ["meta", "result"]
        assert events[-1]["response"]["requires_human"] is True
        assert events[-1]["response"]["handoff_id"]
    finally:
        service.close()
