from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ecommerce_agent.api import create_app
from ecommerce_agent.config import Settings
from ecommerce_agent.database import SessionScopeError
from ecommerce_agent.schemas import ChatImageInput, ChatRequest
from ecommerce_agent.service import AgentService
from ecommerce_agent.vision import VisionGateway, VisionResult

from conftest import make_settings, principal_for


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}
CLIENT_HEADERS = {
    "X-Client-Id": "client-test",
    "X-Client-Key": "test-client-key-12345",
    "X-Subject-Id": "buyer-vision-history",
}


def _png_image(suffix: bytes = b"test-image") -> ChatImageInput:
    return ChatImageInput(
        mime_type="image/png",
        data_base64=base64.b64encode(b"\x89PNG\r\n\x1a\n" + suffix).decode("ascii"),
    )


def _vision_settings(tmp_path):
    return replace(
        make_settings(tmp_path),
        vision_enabled=True,
        vision_base_url="https://vision.example/v1",
        vision_model_name="Qwen/Qwen2.5-VL-7B-Instruct-test",
        vision_api_key="vision-secret",
        vision_timeout_seconds=0.2,
        vision_max_output_tokens=256,
        vision_temperature=0.0,
    )


def _configure_deepseek_for_media(service: AgentService, captured: list[str]) -> None:
    def generate_json(
        messages: list[dict[str, str]],
        **_: Any,
    ) -> dict[str, Any]:
        captured.extend(item["content"] for item in messages)
        if "intent_classification" in messages[-1]["content"]:
            return {"intent": "product_inquiry", "confidence": 0.98}
        return {
            "intent": "product",
            "mode": "answer",
            "reason": "media_observation_available",
            "confidence": 0.98,
        }

    def generate(messages: list[dict[str, str]]) -> str:
        captured.extend(item["content"] for item in messages)
        return "从图片观察看，这是一台晴川空气炸锅，机身标有5L；具体在售规格仍以商品页为准。"

    def stream_generate(messages: list[dict[str, str]]) -> Iterator[str]:
        captured.extend(item["content"] for item in messages)
        yield "从图片观察看，这是一台晴川空气炸锅，机身标有5L；"
        yield "具体在售规格仍以商品页为准。"

    service.model.generate_json = generate_json  # type: ignore[method-assign]
    service.model.generate = generate  # type: ignore[method-assign]
    service.model.stream_generate = stream_generate  # type: ignore[method-assign]


def _applied_vision_result() -> VisionResult:
    return VisionResult(
        description="图片中可见一台晴川空气炸锅，机身标签写有5L；无法从图片确认价格和库存。",
        status="applied",
        applied=True,
        latency_ms=12,
        model="Qwen/Qwen2.5-VL-7B-Instruct-test",
        image_count=1,
    )


def test_chat_image_contract_requires_supported_matching_image() -> None:
    image = _png_image()
    request = ChatRequest(session_id="image-only", message="", image=image)
    assert request.image == image

    with pytest.raises(ValidationError, match="message or image is required"):
        ChatRequest(session_id="empty", message="")
    with pytest.raises(ValidationError, match="unsupported image mime_type"):
        ChatImageInput(mime_type="image/gif", data_base64=image.data_base64)
    with pytest.raises(ValidationError, match="do not match mime_type"):
        ChatImageInput(mime_type="image/jpeg", data_base64=image.data_base64)


def test_vision_settings_default_off_and_env_overrides(monkeypatch) -> None:
    names = (
        "VISION_ENABLED",
        "VISION_BASE_URL",
        "VISION_MODEL_NAME",
        "VISION_API_KEY",
        "VISION_TIMEOUT_SECONDS",
        "VISION_MAX_OUTPUT_TOKENS",
        "VISION_TEMPERATURE",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    defaults = Settings.from_env()
    assert defaults.vision_enabled is False
    assert defaults.vision_base_url == ""
    assert defaults.vision_model_name == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert defaults.vision_timeout_seconds == 45.0

    monkeypatch.setenv("VISION_ENABLED", "true")
    monkeypatch.setenv("VISION_BASE_URL", " https://vision.example/v1/ ")
    monkeypatch.setenv("VISION_MODEL_NAME", "qwen-vl-test")
    monkeypatch.setenv("VISION_API_KEY", "vision-test-secret")
    monkeypatch.setenv("VISION_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("VISION_MAX_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("VISION_TEMPERATURE", "0.1")

    configured = Settings.from_env()
    assert configured.vision_enabled is True
    assert configured.vision_base_url == "https://vision.example/v1"
    assert configured.vision_model_name == "qwen-vl-test"
    assert configured.vision_api_key == "vision-test-secret"
    assert configured.vision_timeout_seconds == 0.001
    assert configured.vision_max_output_tokens == 1
    assert configured.vision_temperature == 0.1


def test_vision_gateway_uses_openai_multimodal_payload_and_redacts_output(
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "图片中是晴川空气炸锅，标签电话为13800138000。"
                        }
                    }
                ]
            },
        )

    gateway = VisionGateway(
        _vision_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    image = _png_image()
    try:
        result = gateway.describe(image=image, user_message="这是什么商品？")
    finally:
        gateway.close()

    assert result.status == "applied"
    assert result.applied is True
    assert "138****8000" in result.description
    assert "13800138000" not in result.description
    assert captured["authorization"] == "Bearer vision-secret"
    payload = captured["payload"]
    assert payload["model"] == "Qwen/Qwen2.5-VL-7B-Instruct-test"
    content = payload["messages"][1]["content"]
    assert [item["type"] for item in content] == ["text", "image_url"]
    assert content[1]["image_url"]["url"] == (
        f"data:image/png;base64,{image.data_base64}"
    )
    assert result.media_evidence()["business_execution_authority"] is False


def test_vision_gateway_passes_unverified_order_candidate_to_deepseek(
    tmp_path,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "description": "截图显示该订单退款申请已获批准。",
                                    "order_candidate": {
                                        "order_reference": "DEMO-ORDER-001",
                                        "order_status": "shipped",
                                        "payment_status": "paid",
                                        "refund_status": "refund_approved",
                                        "amount": "129.00",
                                        "currency": "CNY",
                                        "logistics_status": "in_transit",
                                    },
                                    "uncertainties": ["截图不能证明退款已经到账"],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    gateway = VisionGateway(
        _vision_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = gateway.describe(
            image=_png_image(),
            user_message="退款同意了，但钱还没到账",
        )
    finally:
        gateway.close()

    evidence = result.media_evidence()
    assert result.description == "截图显示该订单退款申请已获批准。"
    assert evidence["order_candidate"]["order_reference"] == "DEMO-ORDER-001"
    assert evidence["order_candidate"]["refund_status"] == "refund_approved"
    assert evidence["order_identity_verified"] is False
    assert evidence["business_execution_authority"] is False
    assert "DEMO-ORDER-001" not in json.dumps(
        result.audit_detail(), ensure_ascii=False
    )


def test_vision_gateway_disabled_never_calls_http(tmp_path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    gateway = VisionGateway(
        make_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = gateway.describe(image=_png_image(), user_message="说明图片")
    finally:
        gateway.close()

    assert result.status == "disabled"
    assert result.model is None
    assert result.media_evidence() == {}
    assert calls == 0


def test_image_observation_reaches_deepseek_without_persisting_raw_image(
    tmp_path,
) -> None:
    service = AgentService(_vision_settings(tmp_path))
    principal = principal_for(service)
    image = _png_image(b"raw-image-must-not-persist")
    captured: list[str] = []
    _configure_deepseek_for_media(service, captured)
    service.vision.describe = lambda **_: _applied_vision_result()  # type: ignore[method-assign]
    service.knowledge.retrieve = lambda *_, **__: []  # type: ignore[method-assign]
    try:
        response = service.chat(
            principal,
            "vision-non-stream",
            "请说明图片中的可见内容",
            image=image,
        )
        with service.db.connect() as conn:
            snapshots = "\n".join(
                row[0] for row in conn.execute("SELECT bundle_json FROM context_snapshots")
            )
            messages = "\n".join(
                row[0] for row in conn.execute("SELECT content FROM messages")
            )
    finally:
        service.close()

    assert response.vision_status == "applied"
    assert response.vision_model == "Qwen/Qwen2.5-VL-7B-Instruct-test"
    assert response.vision_latency_ms == 12
    assert response.vision_image_count == 1
    assert response.model_fallback is False
    assert response.reason == "media_observation_answer_allowed"
    assert any("图片中可见一台晴川空气炸锅" in prompt for prompt in captured)
    assert "multimodal_model_observation" in snapshots
    assert image.data_base64 not in snapshots
    assert image.data_base64 not in messages


def test_order_candidate_reaches_deepseek_without_becoming_trusted_context(
    tmp_path,
) -> None:
    service = AgentService(_vision_settings(tmp_path))
    principal = principal_for(service)
    captured: list[str] = []

    def generate_json(
        messages: list[dict[str, str]],
        **_: Any,
    ) -> dict[str, Any]:
        captured.extend(item["content"] for item in messages)
        if "intent_classification" in messages[-1]["content"]:
            return {"intent": "after_sales", "confidence": 0.98}
        return {
            "intent": "refund_status",
            "mode": "handoff",
            "response": (
                "我已从截图识别到对应订单和退款获批状态，"
                "但到账结果仍需业务系统或人工继续核对。"
            ),
            "reason": "image_order_candidate_requires_verified_lookup",
            "confidence": 0.98,
        }

    service.model.generate_json = generate_json  # type: ignore[method-assign]
    service.vision.describe = lambda **_: VisionResult(  # type: ignore[method-assign]
        description="截图显示该订单退款申请已获批准，但无法证明款项已经到账。",
        status="applied",
        applied=True,
        latency_ms=12,
        model="Qwen/Qwen2.5-VL-7B-Instruct-test",
        image_count=1,
        order_candidate={
            "order_reference": "DEMO-ORDER-001",
            "refund_status": "refund_approved",
            "amount": "129.00",
            "currency": "CNY",
        },
        uncertainties=("截图不能证明退款已经到账",),
    )
    service.knowledge.retrieve = lambda *_, **__: []  # type: ignore[method-assign]
    try:
        response = service.chat(
            principal,
            "vision-order-candidate",
            "我买的这个东西现在退款同意了，但钱还没到账",
            image=_png_image(),
        )
        with service.db.connect() as conn:
            bundles = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT bundle_json FROM context_snapshots ORDER BY sequence"
                )
            ]
    finally:
        service.close()

    assert response.requires_human is True
    assert "已从截图识别到对应订单" in response.answer
    assert "请提供" not in response.answer
    assert any("DEMO-ORDER-001" in prompt for prompt in captured)
    assert any(
        bundle["media_evidence"]["order_candidate"]["order_reference"]
        == "DEMO-ORDER-001"
        for bundle in bundles
    )
    assert all("order_id" not in bundle["current_subject"] for bundle in bundles)


def test_admin_conversation_history_serves_persisted_customer_image(
    tmp_path,
) -> None:
    app = create_app(_vision_settings(tmp_path))
    image = _png_image(b"history-image")
    with TestClient(app) as client:
        app.state.agent.vision.describe = (  # type: ignore[method-assign]
            lambda **_: _applied_vision_result()
        )
        sent = client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={
                "session_id": "vision-history",
                "message": "请看这张图",
                "image": image.model_dump(),
            },
        )
        assert sent.status_code == 200

        conversations = client.get(
            "/v1/admin/conversations?query=vision-history",
            headers=ADMIN_HEADERS,
        ).json()["items"]
        detail = client.get(
            f"/v1/admin/conversations/{conversations[0]['id']}",
            headers=ADMIN_HEADERS,
        ).json()
        user_message = next(
            item for item in detail["messages"] if item["role"] == "user"
        )

        assert len(user_message["media"]) == 1
        media_meta = user_message["media"][0]
        assert media_meta["mime_type"] == "image/png"
        assert media_meta["size_bytes"] == len(image.decoded_bytes())
        assert media_meta["url"].startswith(
            f"/v1/admin/conversations/{conversations[0]['id']}/messages/"
        )

        assert client.get(media_meta["url"]).status_code == 401
        media = client.get(media_meta["url"], headers=ADMIN_HEADERS)
        assert media.status_code == 200
        assert media.headers["content-type"] == "image/png"
        assert media.content == image.decoded_bytes()

        history = client.get(
            "/v1/chat/sessions/vision-history/messages",
            headers=CLIENT_HEADERS,
        ).json()["items"]
        history_user = next(item for item in history if item["role"] == "user")
        client_media_url = history_user["media"][0]["url"]
        assert client.get(client_media_url).status_code == 401
        client_media = client.get(client_media_url, headers=CLIENT_HEADERS)
        assert client_media.status_code == 200
        assert client_media.content == image.decoded_bytes()


def test_follow_up_turn_receives_previous_image_observation(tmp_path) -> None:
    service = AgentService(_vision_settings(tmp_path))
    principal = principal_for(service)
    captured: list[str] = []
    _configure_deepseek_for_media(service, captured)
    service.vision.describe = lambda **_: _applied_vision_result()  # type: ignore[method-assign]
    service.knowledge.retrieve = lambda *_, **__: []  # type: ignore[method-assign]
    try:
        service.chat(
            principal,
            "vision-follow-up",
            "请看这张图",
            image=_png_image(b"follow-up-image"),
        )
        captured.clear()
        service.chat(
            principal,
            "vision-follow-up",
            "就是图里那台，容量是多少？",
        )
        with service.db.connect() as conn:
            user_contents = [
                str(row[0])
                for row in conn.execute(
                    "SELECT content FROM messages WHERE role='user'"
                )
            ]
    finally:
        service.close()

    # 上一轮的图片观察必须作为带标记的非权威历史进入后续决策/生成提示
    follow_up_prompts = [
        prompt for prompt in captured if "图片中可见一台晴川空气炸锅" in prompt
    ]
    assert follow_up_prompts, "第二轮提示缺失上一轮图片观察"
    assert any("非顾客原话" in prompt for prompt in follow_up_prompts)
    # 观察不得混入顾客消息正文本身
    assert all("晴川空气炸锅" not in content for content in user_contents)


def test_customer_session_history_hides_internal_media_details(tmp_path) -> None:
    app = create_app(_vision_settings(tmp_path))
    with TestClient(app) as client:
        app.state.agent.vision.describe = (  # type: ignore[method-assign]
            lambda **_: _applied_vision_result()
        )
        sent = client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={
                "session_id": "vision-media-privacy",
                "message": "请看这张图",
                "image": _png_image(b"privacy-image").model_dump(),
            },
        )
        assert sent.status_code == 200

        history = client.get(
            "/v1/chat/sessions/vision-media-privacy/messages",
            headers=CLIENT_HEADERS,
        ).json()["items"]

    history_user = next(item for item in history if item["role"] == "user")
    assert len(history_user["media"]) == 1
    serialized = json.dumps(history_user, ensure_ascii=False)
    assert "storage_ref" not in serialized
    assert "chat-media" not in serialized
    assert "vision_description" not in serialized


def test_retention_removes_persisted_customer_image(tmp_path) -> None:
    settings = replace(_vision_settings(tmp_path), message_retention_days=1)
    service = AgentService(settings)
    service.vision.describe = lambda **_: _applied_vision_result()  # type: ignore[method-assign]
    try:
        service.chat(
            principal_for(service),
            "vision-retention",
            "请说明图片",
            image=_png_image(b"retention-image"),
        )
        with service.db._write_lock, service.db.connect() as conn:
            sources = json.loads(
                conn.execute(
                    "SELECT sources_json FROM messages WHERE role='user'"
                ).fetchone()[0]
            )
            stored_path = service.settings.data_dir / sources[0]["storage_ref"]
            old = "2000-01-01T00:00:00+00:00"
            conn.execute("UPDATE messages SET created_at=?", (old,))
            conn.execute("UPDATE context_snapshots SET created_at=?", (old,))
            conn.execute("UPDATE request_metrics SET created_at=?", (old,))
            conn.execute("UPDATE audit_log SET created_at=?", (old,))
            conn.execute("UPDATE sessions SET last_seen_at=?", (old,))
        assert stored_path.is_file()

        report = service.purge_expired(actor="test", dry_run=False)

        assert report["media_files_selected"] == 1
        assert report["media_files_deleted"] == 1
        assert not stored_path.exists()
        with service.db.connect() as conn:
            stored_report = json.loads(
                conn.execute(
                    "SELECT detail_json FROM retention_runs"
                ).fetchone()[0]
            )
        # 落库的留存报告必须包含媒体删除结果，供审计回放
        assert stored_report["media_files_deleted"] == 1
    finally:
        service.close()


def test_streaming_chat_reuses_the_same_image_observation(tmp_path) -> None:
    service = AgentService(_vision_settings(tmp_path))
    principal = principal_for(service)
    captured: list[str] = []
    _configure_deepseek_for_media(service, captured)
    service.vision.describe = lambda **_: _applied_vision_result()  # type: ignore[method-assign]
    try:
        events = list(
            service.chat_stream(
                principal,
                "vision-stream",
                "请说明图片中的可见内容",
                image=_png_image(),
                idempotency_key=None,
            )
        )
    finally:
        service.close()

    assert events[0]["event"] == "meta"
    assert events[0]["vision_status"] == "applied"
    assert events[-1]["response"]["vision_status"] == "applied"
    assert events[-1]["response"]["vision_image_count"] == 1
    assert events[-1]["response"]["reason"] == "media_and_knowledge_answer_allowed"
    assert any("图片中可见一台晴川空气炸锅" in prompt for prompt in captured)


def test_image_digest_participates_in_idempotency_without_repeating_vision(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)
    calls = 0

    def describe(**_: Any) -> VisionResult:
        nonlocal calls
        calls += 1
        return VisionResult(
            description="",
            status="disabled",
            applied=False,
            latency_ms=0,
            model=None,
            image_count=1,
        )

    service.vision.describe = describe  # type: ignore[method-assign]
    try:
        first = service.chat(
            principal,
            "vision-idempotency",
            "说明图片",
            image=_png_image(b"first"),
            idempotency_key="vision-key",
        )
        replay = service.chat(
            principal,
            "vision-idempotency",
            "说明图片",
            image=_png_image(b"first"),
            idempotency_key="vision-key",
        )
        with pytest.raises(SessionScopeError, match="idempotency key"):
            service.chat(
                principal,
                "vision-idempotency",
                "说明图片",
                image=_png_image(b"different"),
                idempotency_key="vision-key",
            )
    finally:
        service.close()

    assert replay.message_id == first.message_id
    assert calls == 1
