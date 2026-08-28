from __future__ import annotations

import json
import re
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

from ecommerce_agent.config import Settings
from ecommerce_agent.graph import finalize_response
from ecommerce_agent.polish import PolishGateway, PolishResult

from conftest import make_settings


def _enabled_gateway(
    tmp_path,
    handler: Callable[[httpx.Request], httpx.Response],
) -> PolishGateway:
    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        polish_enabled=True,
        polish_base_url="https://polish.example/v1",
        polish_model_name="qwen3-14b-rag-polish-test",
        polish_api_key="polish-secret",
        polish_timeout_seconds=0.1,
        polish_temperature=0.0,
    )
    return PolishGateway(settings, transport=httpx.MockTransport(handler))


def _response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
    )


def _polish(gateway: PolishGateway, raw_answer: str) -> PolishResult:
    return gateway.polish(
        raw_answer=raw_answer,
        user_message="这件商品怎么处理？",
        facts="商品原价499元；无破损时可以申请退货。",
        recent_history=[{"role": "user", "content": "请继续"}],
    )


def test_polish_client_does_not_inherit_process_proxy_settings(tmp_path) -> None:
    gateway = _enabled_gateway(tmp_path, lambda _: _response("原始回复。"))
    try:
        assert gateway._client._trust_env is False
    finally:
        gateway.close()


def _literal_keeps_from_prompt(prompt: str) -> list[str]:
    raw_section = prompt.split("【原始回复内容】\n", 1)[1].split(
        "\n\n【事实依据】", 1
    )[0]
    return re.findall(r"<keep>(.*?)</keep>", raw_section, re.DOTALL)


def test_polish_disabled_returns_original_without_http_call(tmp_path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response("不应调用")

    gateway = PolishGateway(
        make_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = _polish(gateway, "原始回复。")

        assert result.answer == "原始回复。"
        assert result.status == "disabled"
        assert result.applied is False
        assert calls == 0
    finally:
        gateway.close()


@pytest.mark.parametrize(
    ("model_enabled", "model_mock_mode"),
    [(False, False), (True, True)],
)
def test_polish_respects_global_model_switch(
    tmp_path,
    model_enabled: bool,
    model_mock_mode: bool,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response("不应调用")

    gateway = PolishGateway(
        replace(
            make_settings(tmp_path),
            model_enabled=model_enabled,
            model_mock_mode=model_mock_mode,
            polish_enabled=True,
            polish_base_url="https://polish.example/v1",
            polish_api_key="secret",
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = _polish(gateway, "原始回复。")

        assert result.status == "disabled"
        assert calls == 0
    finally:
        gateway.close()


def test_polish_restores_keep_text_and_uses_non_thinking_request(tmp_path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.update(payload)
        captured["authorization"] = request.headers.get("authorization")
        prompt = payload["messages"][1]["content"]
        assert _literal_keeps_from_prompt(prompt) == ["商品原价499元"]
        return _response(
            "您好，商品原价499元，如果没有破损，可以申请退货。"
            "希望以上信息能帮到您。"
        )

    gateway = _enabled_gateway(tmp_path, handler)
    try:
        result = _polish(
            gateway,
            "<keep>商品原价499元</keep>，如果没有破损，可以申请退货。",
        )

        assert result.answer == (
            "您好，商品原价499元，如果没有破损，可以申请退货。"
            "希望以上信息能帮到您。"
        )
        assert result.status == "applied"
        assert result.applied is True
        assert "<keep>" not in result.answer
        assert captured["temperature"] == 0.0
        assert captured["stream"] is False
        assert captured["chat_template_kwargs"] == {"enable_thinking": False}
        assert captured["authorization"] == "Bearer polish-secret"
    finally:
        gateway.close()


def test_polish_sends_literal_keep_text_expected_by_finetune(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][1]["content"]
        assert "<keep>商品原价499元</keep>" in prompt
        assert "__POLISH_KEEP_" not in prompt
        return _response("您好，商品原价499元，如果没有破损，可以申请退货。")

    gateway = _enabled_gateway(tmp_path, handler)
    try:
        result = _polish(
            gateway,
            "<keep>商品原价499元</keep>，如果没有破损，可以申请退货。",
        )

        assert result.answer == "您好，商品原价499元，如果没有破损，可以申请退货。"
        assert result.status == "applied"
    finally:
        gateway.close()


def test_polish_settings_default_off_and_env_overrides(monkeypatch) -> None:
    names = (
        "POLISH_ENABLED",
        "POLISH_BASE_URL",
        "POLISH_MODEL_NAME",
        "POLISH_API_KEY",
        "POLISH_TIMEOUT_SECONDS",
        "POLISH_MAX_OUTPUT_TOKENS",
        "POLISH_TEMPERATURE",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    defaults = Settings.from_env()
    assert defaults.polish_enabled is False
    assert defaults.polish_base_url == ""
    assert defaults.polish_model_name == "qwen3-14b-rag-polish"
    assert defaults.polish_timeout_seconds == 15.0
    assert defaults.polish_max_output_tokens == 320
    assert defaults.polish_temperature == 0.0

    monkeypatch.setenv("POLISH_ENABLED", "true")
    monkeypatch.setenv("POLISH_BASE_URL", " https://polish.example/v1/ " )
    monkeypatch.setenv("POLISH_MODEL_NAME", "qwen-polish-test")
    monkeypatch.setenv("POLISH_API_KEY", "test-secret")
    monkeypatch.setenv("POLISH_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("POLISH_MAX_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("POLISH_TEMPERATURE", "0.2")

    configured = Settings.from_env()
    assert configured.polish_enabled is True
    assert configured.polish_base_url == "https://polish.example/v1"
    assert configured.polish_model_name == "qwen-polish-test"
    assert configured.polish_api_key == "test-secret"
    assert configured.polish_timeout_seconds == 0.001
    assert configured.polish_max_output_tokens == 1
    assert configured.polish_temperature == 0.2


def test_enabled_polish_configuration_participates_in_health_and_readiness(
    tmp_path,
) -> None:
    from ecommerce_agent.service import AgentService

    service = AgentService(
        replace(
            make_settings(tmp_path),
            model_enabled=True,
            model_mock_mode=False,
            polish_enabled=True,
            polish_base_url="",
        )
    )
    service.start()
    try:
        health = service.health()
        ready, readiness = service.readiness()

        assert health["polish"] == {
            "enabled": True,
            "ok": False,
            "detail": "misconfigured",
            "name": "qwen3-14b-rag-polish",
            "required_for_foundation_health": False,
        }
        assert ready is False
        assert readiness["checks"]["polish_configuration"] is False
    finally:
        service.close()


@pytest.mark.parametrize(
    ("candidate_builder", "expected_status"),
    [
        (lambda tokens: "已为您处理。", "rejected_protected_phrase_mismatch"),
        (
            lambda tokens: f"{tokens[0]}，再次说明{tokens[0]}。",
            "rejected_protected_phrase_mismatch",
        ),
        (
            lambda tokens: f"{tokens[1]}，之后是{tokens[0]}。",
            "rejected_protected_phrase_mismatch",
        ),
        (
            lambda tokens: f"<think>分析</think>{''.join(tokens)}",
            "rejected_internal_tag",
        ),
    ],
)
def test_polish_rejects_placeholder_or_internal_tag_damage(
    tmp_path,
    candidate_builder: Callable[[list[str]], str],
    expected_status: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        tokens = _literal_keeps_from_prompt(payload["messages"][1]["content"])
        return _response(candidate_builder(tokens))

    gateway = _enabled_gateway(tmp_path, handler)
    raw = "<keep>价格499元</keep>，物流通常需要<keep>2天</keep>。"
    try:
        result = _polish(gateway, raw)

        assert result.answer == raw
        assert result.status == expected_status
        assert result.applied is False
    finally:
        gateway.close()


@pytest.mark.parametrize(
    ("raw_answer", "candidate", "expected_status"),
    [
        (
            "库存充足，可以下单。",
            "库存充足，可以购买1件。",
            "rejected_numeric_tokens",
        ),
        (
            "如果商品没有破损，通常可能可以退货。",
            "商品可以退货。",
            "rejected_semantic_anchors",
        ),
        (
            "这不是涂抹式商品，建议先查看规格。",
            "这款商品建议先查看规格。",
            "rejected_semantic_anchors",
        ),
        (
            "商品材质是纯棉。",
            "商品材质是真丝。",
            "rejected_content_drift",
        ),
        (
            "支持七天无理由退货。",
            "支持十五天无理由退货。",
            "rejected_content_drift",
        ),
    ],
)
def test_polish_rejects_fact_or_semantic_drift(
    tmp_path,
    raw_answer: str,
    candidate: str,
    expected_status: str,
) -> None:
    gateway = _enabled_gateway(tmp_path, lambda _: _response(candidate))
    try:
        result = _polish(gateway, raw_answer)

        assert result.answer == raw_answer
        assert result.status == expected_status
        assert result.applied is False
    finally:
        gateway.close()


def test_polish_rejects_cross_entity_fact_value_swaps(tmp_path) -> None:
    gateway = _enabled_gateway(
        tmp_path,
        lambda _: _response("AF5 售价399元，AF3 售价529元。"),
    )
    try:
        result = _polish(gateway, "AF5 售价529元，AF3 售价399元。")

        assert result.answer == "AF5 售价529元，AF3 售价399元。"
        assert result.status == "rejected_fact_binding"
        assert result.applied is False
    finally:
        gateway.close()


def test_polish_rejects_chinese_entity_fact_value_swaps(tmp_path) -> None:
    gateway = _enabled_gateway(
        tmp_path,
        lambda _: _response("空气炸锅售价399元，电饭煲售价529元。"),
    )
    try:
        result = _polish(gateway, "空气炸锅售价529元，电饭煲售价399元。")

        assert result.answer == "空气炸锅售价529元，电饭煲售价399元。"
        assert result.status == "rejected_fact_binding"
        assert result.applied is False
    finally:
        gateway.close()


@pytest.mark.parametrize(
    ("raw_answer", "candidate"),
    [
        (
            "Air Fryer costs $529, Rice Cooker costs $399.",
            "Air Fryer costs $399, Rice Cooker costs $529.",
        ),
        (
            "晴川 Air Fryer costs $529, 晴川 Rice Cooker costs $399.",
            "晴川 Air Fryer costs $399, 晴川 Rice Cooker costs $529.",
        ),
    ],
)
def test_polish_rejects_english_entity_fact_value_swaps(
    tmp_path,
    raw_answer: str,
    candidate: str,
) -> None:
    gateway = _enabled_gateway(tmp_path, lambda _: _response(candidate))
    try:
        result = _polish(gateway, raw_answer)

        assert result.answer == raw_answer
        assert result.status == "rejected_fact_binding"
        assert result.applied is False
    finally:
        gateway.close()


def test_polish_accepts_real_qwen_connector_word_rewrite(tmp_path) -> None:
    raw = "晴川空气炸锅 AF5 5L 松绿色，售价529元，容量5L，整机保修12个月。"
    candidate = (
        "您好，这款晴川空气炸锅 AF5 5L 松绿色的售价是529元，"
        "容量为5L，整机保修12个月。"
    )
    gateway = _enabled_gateway(tmp_path, lambda _: _response(candidate))
    try:
        result = _polish(gateway, raw)

        assert result.answer == candidate
        assert result.status == "applied"
        assert result.applied is True
    finally:
        gateway.close()


def test_polish_accepts_real_qwen_fact_reordering_without_new_fact(tmp_path) -> None:
    raw = "晴川空气炸锅 AF5 5L 松绿色，售价529元，容量5L，整机保修12个月。"
    candidate = (
        "您好，关于您咨询的晴川空气炸锅 AF5 5L 松绿色，以下是其主要参数介绍：\n\n"
        "- 型号：AF5\n"
        "- 容量：5L\n"
        "- 颜色：松绿色\n"
        "- 售价：529 元\n"
        "- 整机保修：12 个月\n\n"
        "如您还有其他疑问，欢迎随时咨询！"
    )
    gateway = _enabled_gateway(tmp_path, lambda _: _response(candidate))
    try:
        result = _polish(gateway, raw)

        assert result.answer == candidate
        assert result.status == "applied"
        assert result.applied is True
    finally:
        gateway.close()


def test_polish_rejects_missing_automatic_fact(tmp_path) -> None:
    raw = "价格499元，物流通常需要2天。"
    gateway = _enabled_gateway(tmp_path, lambda _: _response("价格499元。"))
    try:
        result = _polish(gateway, raw)

        assert result.answer == raw
        assert result.status == "rejected_protected_phrase_mismatch"
        assert result.applied is False
    finally:
        gateway.close()


def test_polish_auto_protects_complete_business_tokens(tmp_path) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.extend(_literal_keeps_from_prompt(payload["messages"][1]["content"]))
        return _response(
            "晴川空气炸锅 AF5 5L 松绿色，售价529元，容量5L，"
            "整机保修12个月。"
        )

    gateway = _enabled_gateway(tmp_path, handler)
    try:
        result = _polish(
            gateway,
            "晴川空气炸锅 AF5 5L 松绿色，售价529元，容量5L，"
            "整机保修12个月。",
        )

        assert result.status == "unchanged"
        assert captured == ["5L", "529元", "5L", "12个月"]
    finally:
        gateway.close()


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: (_ for _ in ()).throw(
            httpx.ReadTimeout("private upstream detail", request=request)
        ),
        lambda _: httpx.Response(503, text="private upstream detail"),
        lambda _: _response("   "),
    ],
)
def test_polish_transport_or_response_failure_falls_back(
    tmp_path,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    gateway = _enabled_gateway(tmp_path, handler)
    try:
        result = _polish(gateway, "原始回复。")

        assert result.answer == "原始回复。"
        assert result.status == "error"
        assert result.applied is False
        assert result.error_type in {
            "ReadTimeout",
            "HTTPStatusError",
            "PolishResponseError",
        }
    finally:
        gateway.close()


class _CapturingDb:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def audit(self, *args: Any) -> str:
        self.events.append(args)
        return "audit-test"


class _StubPolisher:
    settings = SimpleNamespace(polish_model_name="qwen3-14b-rag-polish-test")

    def __init__(self, result: PolishResult) -> None:
        self.result = result
        self.calls = 0

    def polish(self, **_: Any) -> PolishResult:
        self.calls += 1
        return self.result


def _finalization_state(*, draft_origin: str = "model") -> dict[str, Any]:
    return {
        "retrieved": [
            {
                "answer": "商品售价499元，无破损时可以申请退货。",
            }
        ],
        "context_bundle": {"recent_history": []},
        "tool_result": {},
        "draft": "商品售价499元，可以申请退货。",
        "draft_origin": draft_origin,
        "model_fallback": False,
        "model_retry_advised": False,
        "normalized_input": "顾客私密问题",
        "trace": ["generate:model"],
        "trace_id": "trace-polish",
        "tenant_id": "tenant-test",
        "decision": {"route": "answer", "reason": "model_decision"},
        "route": "answer",
        "route_reason": "model_decision",
        "requires_human": False,
    }


def test_finalize_response_audit_is_metadata_only_and_preserves_decision() -> None:
    candidate = "您好，商品售价499元，可以申请退货。"
    polisher = _StubPolisher(
        PolishResult(
            answer=candidate,
            status="applied",
            applied=True,
            latency_ms=12,
            model="qwen3-14b-rag-polish-test",
        )
    )
    db = _CapturingDb()
    state = _finalization_state()

    finalized = finalize_response(state, polisher=polisher, db=db)
    merged = {**state, **finalized}

    assert finalized["answer"] == candidate
    assert finalized["polish_status"] == "applied"
    assert finalized["polish_applied"] is True
    assert finalized["polish_model"] == "qwen3-14b-rag-polish-test"
    assert finalized["polish_latency_ms"] == 12
    assert merged["decision"] == state["decision"]
    assert merged["route"] == state["route"]
    assert merged["route_reason"] == state["route_reason"]
    assert merged["requires_human"] is False
    detail = db.events[0][3]
    assert set(detail) == {
        "status",
        "applied",
        "latency_ms",
        "model",
        "error_type",
    }
    serialized = json.dumps(detail, ensure_ascii=False)
    assert "顾客私密问题" not in serialized
    assert state["draft"] not in serialized
    assert candidate not in serialized


def test_finalize_response_propagates_unexpected_polisher_defect() -> None:
    class BrokenPolisher:
        settings = SimpleNamespace(
            polish_enabled=True,
            polish_model_name="qwen3-14b-rag-polish-test",
        )

        @staticmethod
        def polish(**_: Any) -> PolishResult:
            raise RuntimeError("polisher implementation defect")

    with pytest.raises(RuntimeError, match="polisher implementation defect"):
        finalize_response(
            _finalization_state(),
            polisher=BrokenPolisher(),  # type: ignore[arg-type]
            db=_CapturingDb(),  # type: ignore[arg-type]
        )


def test_finalize_response_skips_non_model_and_rejected_drafts() -> None:
    polisher = _StubPolisher(
        PolishResult(
            answer="不应使用",
            status="applied",
            applied=True,
            latency_ms=1,
            model="qwen3-14b-rag-polish-test",
        )
    )
    db = _CapturingDb()

    approved = _finalization_state(draft_origin="approved_knowledge")
    approved_result = finalize_response(approved, polisher=polisher, db=db)
    rejected = {**_finalization_state(), "draft": "商品售价999元。"}
    rejected_result = finalize_response(rejected, polisher=polisher, db=db)

    assert approved_result["answer"] == approved["draft"]
    assert approved_result["polish_status"] == "skipped_non_model"
    assert approved_result["polish_applied"] is False
    assert approved_result["polish_model"] == "qwen3-14b-rag-polish-test"
    assert approved_result["polish_latency_ms"] is None
    assert rejected_result["review_route"] == "handoff"
    assert rejected_result["polish_status"] == "skipped_review"
    assert polisher.calls == 0
    assert db.events == []
