from __future__ import annotations

import base64
import json
import os
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ecommerce_agent.api import create_app
from ecommerce_agent.config import Settings
from ecommerce_agent.database import SessionScopeError
from ecommerce_agent.message_media import MessageMediaStore
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


def _vision_gateway_settings(tmp_path):
    return replace(
        _vision_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
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


def test_vision_client_does_not_inherit_process_proxy_settings(tmp_path) -> None:
    gateway = VisionGateway(
        _vision_gateway_settings(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    try:
        assert gateway._client._trust_env is False
    finally:
        gateway.close()


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
                            "content": json.dumps(
                                {
                                    "description": (
                                        "图片中是晴川空气炸锅，标签电话为13800138000。"
                                    ),
                                    "order_candidate": None,
                                    "uncertainties": [],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    gateway = VisionGateway(
        _vision_gateway_settings(tmp_path),
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
        _vision_gateway_settings(tmp_path),
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


def test_vision_gateway_drops_sensitive_order_reference(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "description": "截图显示退款申请正在处理。",
                                    "order_candidate": {
                                        "order_reference": "13800138000",
                                        "refund_status": "refund_approved",
                                    },
                                    "uncertainties": [],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    gateway = VisionGateway(
        _vision_gateway_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = gateway.describe(image=_png_image(), user_message="退款到账了吗")
    finally:
        gateway.close()

    assert result.order_candidate == {"refund_status": "refund_approved"}
    assert "13800138000" not in json.dumps(result.media_evidence(), ensure_ascii=False)
    assert "订单引用包含敏感信息，已忽略。" in result.uncertainties


def test_vision_gateway_rejects_non_json_model_output(tmp_path) -> None:
    gateway = VisionGateway(
        _vision_gateway_settings(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "图片里有一台空气炸锅"}}]},
            )
        ),
    )
    try:
        result = gateway.describe(image=_png_image(), user_message="这是什么")
    finally:
        gateway.close()

    assert result.status == "error"
    assert result.applied is False
    assert result.description == ""
    assert result.media_evidence() == {}


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


@pytest.mark.parametrize(
    ("model_enabled", "model_mock_mode"),
    [(False, False), (True, True)],
)
def test_vision_respects_global_model_switch(
    tmp_path,
    model_enabled: bool,
    model_mock_mode: bool,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    gateway = VisionGateway(
        replace(
            _vision_settings(tmp_path),
            model_enabled=model_enabled,
            model_mock_mode=model_mock_mode,
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = gateway.describe(image=_png_image(), user_message="说明图片")

        assert result.status == "disabled"
        assert calls == 0
    finally:
        gateway.close()


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


def test_vision_audit_failure_removes_unpersisted_image(tmp_path) -> None:
    service = AgentService(_vision_settings(tmp_path))
    principal = principal_for(service)
    service.vision.describe = lambda **_: _applied_vision_result()  # type: ignore[method-assign]

    def fail_audit(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("audit unavailable")

    service.db.audit = fail_audit  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            service.chat(
                principal,
                "vision-audit-cleanup",
                "请说明图片",
                image=_png_image(b"audit-cleanup"),
            )

        media_root = service.settings.data_dir / "objects" / "chat-media"
        assert [path for path in media_root.rglob("*") if path.is_file()] == []
    finally:
        service.close()


def test_unexpected_vision_defect_propagates_and_removes_image(tmp_path) -> None:
    service = AgentService(_vision_settings(tmp_path))
    principal = principal_for(service)

    def fail_vision(**_: Any) -> VisionResult:
        raise RuntimeError("vision implementation defect")

    service.vision.describe = fail_vision  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="vision implementation defect"):
            service.chat(
                principal,
                "vision-defect-cleanup",
                "请说明图片",
                image=_png_image(b"vision-defect"),
            )

        media_root = service.settings.data_dir / "objects" / "chat-media"
        assert [path for path in media_root.rglob("*") if path.is_file()] == []
    finally:
        service.close()


def test_media_marker_failure_rolls_back_every_file(tmp_path, monkeypatch) -> None:
    store = MessageMediaStore(tmp_path)
    original_touch = Path.touch

    def fail_pending_marker(self, *args, **kwargs):
        if self.name.endswith(".pending"):
            raise OSError("marker unavailable")
        return original_touch(self, *args, **kwargs)

    monkeypatch.setattr(Path, "touch", fail_pending_marker)

    with pytest.raises(OSError, match="marker unavailable"):
        store.persist("marker-failure", _png_image(b"marker-failure"))

    assert [path for path in store.root.rglob("*") if path.is_file()] == []


def test_reconciliation_removes_crashed_pending_temporary_file(tmp_path) -> None:
    store = MessageMediaStore(tmp_path)
    media = store.persist("crashed-write", _png_image(b"crashed-write"))
    path = tmp_path / media["storage_ref"]
    marker = path.with_name(f"{path.name}.pending")
    temporary = path.with_name(f".{path.name}.tmp")
    path.unlink()
    temporary.write_bytes(b"partial")

    restarted = MessageMediaStore(tmp_path)
    deleted, failed = restarted.remove_unreferenced(set())

    assert (deleted, failed) == (0, 0)
    assert not temporary.exists()
    assert not marker.exists()


def test_retention_retries_old_unreferenced_media_files(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        media = service.message_media.persist(
            "orphan-message",
            _png_image(b"orphan-cleanup"),
        )
        orphan = service.settings.data_dir / media["storage_ref"]
        os.utime(orphan, (0, 0))
        # Simulate a process restart: the pending marker remains on disk while
        # the in-memory active ownership set is empty.
        service.maintenance.media_store = MessageMediaStore(service.settings.data_dir)

        report = service.purge_expired(actor="test", dry_run=False)

        assert report["media_orphans_deleted"] == 1
        assert report["media_orphans_delete_failed"] == 0
        assert not orphan.exists()
    finally:
        service.close()


def test_retention_does_not_delete_media_owned_by_an_active_request(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        media = service.message_media.persist(
            "active-message",
            _png_image(b"active-pending-media"),
        )
        active_path = service.settings.data_dir / media["storage_ref"]
        os.utime(active_path, (0, 0))

        report = service.purge_expired(actor="test", dry_run=False)

        assert report["media_orphans_deleted"] == 0
        assert active_path.is_file()
    finally:
        service.message_media.remove([media])
        service.close()


def test_pending_media_ownership_is_reference_counted(tmp_path) -> None:
    store = MessageMediaStore(tmp_path)
    image = _png_image(b"shared-owner")
    first = store.persist("shared-message", image)
    second = store.persist("shared-message", image)
    path = tmp_path / first["storage_ref"]

    store.remove([first])
    assert path.is_file()
    assert list(store.root.rglob("*.pending"))

    store.mark_persisted([second])
    assert path.is_file()
    assert list(store.root.rglob("*.pending")) == []


def test_pending_media_registration_is_atomic_with_reconciliation(
    tmp_path,
    monkeypatch,
) -> None:
    store = MessageMediaStore(tmp_path)
    marker_chmod_started = threading.Event()
    release_persist = threading.Event()
    original_chmod = os.chmod

    def block_marker_chmod(path, mode):
        if Path(path).name.endswith(".pending"):
            marker_chmod_started.set()
            assert release_persist.wait(2)
        return original_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", block_marker_chmod)
    persisted: dict[str, Any] = {}
    persist_errors: list[BaseException] = []

    def persist_media() -> None:
        try:
            persisted["media"] = store.persist(
                "registration-race",
                _png_image(b"registration-race"),
            )
        except BaseException as exc:
            persist_errors.append(exc)

    persist_thread = threading.Thread(target=persist_media)
    persist_thread.start()
    assert marker_chmod_started.wait(2)

    reconciled: dict[str, tuple[int, int]] = {}
    reconcile_done = threading.Event()

    def reconcile_media() -> None:
        try:
            reconciled["result"] = store.remove_unreferenced(set())
        finally:
            reconcile_done.set()

    reconcile_thread = threading.Thread(target=reconcile_media)
    reconcile_thread.start()
    reconcile_done.wait(0.25)
    release_persist.set()
    persist_thread.join(2)
    reconcile_thread.join(2)

    assert not persist_thread.is_alive()
    assert not reconcile_thread.is_alive()
    assert persist_errors == []
    media = persisted["media"]
    assert (tmp_path / media["storage_ref"]).is_file()
    assert reconciled["result"] == (0, 0)


def test_mark_persisted_is_atomic_with_reconciliation(tmp_path, monkeypatch) -> None:
    store = MessageMediaStore(tmp_path)
    media = store.persist("persisted-race", _png_image(b"persisted-race"))
    path = tmp_path / media["storage_ref"]
    marker = path.with_name(f"{path.name}.pending")
    marker_unlink_started = threading.Event()
    release_mark_persisted = threading.Event()
    original_unlink = Path.unlink

    def block_marker_unlink(self, *args, **kwargs):
        if self == marker and threading.current_thread().name == "mark-persisted":
            marker_unlink_started.set()
            assert release_mark_persisted.wait(2)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", block_marker_unlink)
    mark_errors: list[BaseException] = []

    def mark_persisted() -> None:
        try:
            store.mark_persisted([media])
        except BaseException as exc:
            mark_errors.append(exc)

    mark_thread = threading.Thread(target=mark_persisted, name="mark-persisted")
    mark_thread.start()
    assert marker_unlink_started.wait(2)

    reconciled: dict[str, tuple[int, int]] = {}
    reconcile_done = threading.Event()

    def reconcile_media() -> None:
        try:
            reconciled["result"] = store.remove_unreferenced(set())
        finally:
            reconcile_done.set()

    reconcile_thread = threading.Thread(target=reconcile_media)
    reconcile_thread.start()
    reconcile_done.wait(0.25)
    release_mark_persisted.set()
    mark_thread.join(2)
    reconcile_thread.join(2)

    assert not mark_thread.is_alive()
    assert not reconcile_thread.is_alive()
    assert mark_errors == []
    assert path.is_file()
    assert not marker.exists()
    assert reconciled["result"] == (0, 0)


def test_retention_retries_persisted_media_delete_failure(tmp_path) -> None:
    settings = replace(_vision_settings(tmp_path), message_retention_days=1)
    service = AgentService(settings)
    service.vision.describe = lambda **_: _applied_vision_result()  # type: ignore[method-assign]
    original_remove = service.message_media.remove
    original_reconcile = service.message_media.remove_unreferenced
    try:
        service.chat(
            principal_for(service),
            "vision-retention-retry",
            "请说明图片",
            image=_png_image(b"retention-retry"),
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

        def fail_remove(_value):
            raise OSError("delete denied")

        service.message_media.remove = fail_remove  # type: ignore[method-assign]
        service.message_media.remove_unreferenced = (  # type: ignore[method-assign]
            lambda _referenced: (0, 0)
        )
        first = service.purge_expired(actor="test", dry_run=False)
        assert first["media_files_delete_failed"] == 1
        assert stored_path.is_file()
        assert list(service.message_media.root.rglob("*.pending"))

        service.message_media.remove = original_remove  # type: ignore[method-assign]
        service.message_media.remove_unreferenced = original_reconcile  # type: ignore[method-assign]
        second = service.purge_expired(actor="test", dry_run=False)
        assert second["media_orphans_deleted"] == 1
        assert not stored_path.exists()
    finally:
        service.message_media.remove = original_remove  # type: ignore[method-assign]
        service.message_media.remove_unreferenced = original_reconcile  # type: ignore[method-assign]
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
