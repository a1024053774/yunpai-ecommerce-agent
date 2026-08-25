from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.decision_advisor import DecisionAdvisorService

from conftest import make_settings


class _FakeSettings:
    def __init__(self, *, enabled: bool = True) -> None:
        self.model_enabled = enabled


class _FakeModel:
    def __init__(
        self,
        *,
        enabled: bool = True,
        payload: object | None = None,
        error: Exception | None = None,
    ) -> None:
        self.settings = _FakeSettings(enabled=enabled)
        self.payload = payload
        self.error = error
        self.calls: list[tuple[list[dict], dict]] = []

    def generate_json(
        self, messages: list[dict], **kwargs: object
    ) -> dict:
        self.calls.append((messages, kwargs))
        if self.error is not None:
            raise self.error
        assert isinstance(self.payload, dict)
        return self.payload


def _facts() -> dict:
    return {
        "tenant_id": "tenant-test",
        "store_id": "store-test",
        "period": "2026-08",
        "scope": "formal",
        "profit_projection": {"sales": {"status": "available", "amount": "600"}},
        "ordering_drafts": [],
        "inventory_risks": [],
        "marketing": {"available": False},
    }


def _valid_payload() -> dict:
    return {
        "suggestions": [
            {
                "suggestion": "补齐采购成本后核算销售利润",
                "basis": "销售层缺失 purchase_cost，缺失不补零",
                "data_gaps": ["purchase_cost"],
                "owner": "财务负责人人工确认",
                "next_step": "录入采购成本并复核对账",
                "evidence_refs": ["profit:sales:status", "profit:sales:amount"],
                "amount_refs": ["profit.sales.amount=600"],
            }
        ]
    }


def test_model_disabled_returns_unavailable_without_calling_model() -> None:
    model = _FakeModel(enabled=False, payload=_valid_payload())
    result = DecisionAdvisorService(model).suggest(_facts())
    assert result.available is False
    assert result.reason == "model_unavailable"
    assert model.calls == []


def test_no_model_returns_unavailable() -> None:
    result = DecisionAdvisorService(None).suggest(_facts())
    assert result.available is False
    assert result.reason == "model_unavailable"


def test_valid_model_output_is_parsed_and_facts_unchanged() -> None:
    model = _FakeModel(payload=_valid_payload())
    service = DecisionAdvisorService(model)
    facts = _facts()
    snapshot = copy.deepcopy(facts)
    result = service.suggest(facts)
    assert result.available is True
    assert len(result.suggestions) == 1
    suggestion = result.suggestions[0]
    assert suggestion.suggestion == "补齐采购成本后核算销售利润"
    assert suggestion.data_gaps == ["purchase_cost"]
    assert suggestion.owner == "财务负责人人工确认"
    assert suggestion.evidence_refs == ["profit:sales:status", "profit:sales:amount"]
    assert suggestion.amount_refs == ["profit.sales.amount=600"]
    assert result.facts_digest is not None and len(result.facts_digest) == 64
    assert facts == snapshot  # 模型解释不得修改任何事实
    assert model.calls and "decision_suggestion" in model.calls[0][0][1]["content"]


def test_model_error_returns_model_error_reason() -> None:
    model = _FakeModel(error=RuntimeError("timeout"))
    result = DecisionAdvisorService(model).suggest(_facts())
    assert result.available is False
    assert result.reason == "model_error"


def test_invalid_model_output_rejected() -> None:
    for payload in (
        {"suggestions": "not-a-list"},
        {"suggestions": []},
        {
            "suggestions": [
                {
                    "suggestion": "x",
                    "basis": "y",
                    "data_gaps": [],
                    "owner": "",
                    "next_step": "z",
                }
            ]
        },
    ):
        model = _FakeModel(payload=payload)
        result = DecisionAdvisorService(model).suggest(_facts())
        assert result.available is False
        assert result.reason == "model_output_invalid"


def test_evidence_ref_not_in_catalog_rejected() -> None:
    model = _FakeModel(
        payload={
            "suggestions": [
                {
                    "suggestion": "x",
                    "basis": "y",
                    "data_gaps": [],
                    "owner": "z",
                    "next_step": "w",
                    "evidence_refs": ["profit:fake:not_exist"],
                    "amount_refs": [],
                }
            ]
        }
    )
    result = DecisionAdvisorService(model).suggest(_facts())
    assert result.available is False
    assert result.reason == "model_output_invalid"


def test_amount_ref_mismatch_rejected() -> None:
    model = _FakeModel(
        payload={
            "suggestions": [
                {
                    "suggestion": "x",
                    "basis": "y",
                    "data_gaps": [],
                    "owner": "z",
                    "next_step": "w",
                    "evidence_refs": ["profit:sales:amount"],
                    "amount_refs": [
                        "profit.sales.amount=999.99"
                    ],
                }
            ]
        }
    )
    result = DecisionAdvisorService(model).suggest(_facts())
    assert result.available is False
    assert result.reason == "model_output_invalid"


def test_amount_ref_format_invalid_rejected() -> None:
    model = _FakeModel(
        payload={
            "suggestions": [
                {
                    "suggestion": "x",
                    "basis": "y",
                    "data_gaps": [],
                    "owner": "z",
                    "next_step": "w",
                    "evidence_refs": [],
                    "amount_refs": ["profit.sales.amount"],
                }
            ]
        }
    )
    result = DecisionAdvisorService(model).suggest(_facts())
    assert result.available is False
    assert result.reason == "model_output_invalid"


def test_empty_evidence_refs_allowed() -> None:
    model = _FakeModel(
        payload={
            "suggestions": [
                {
                    "suggestion": "当前无待处理事项",
                    "basis": "无缺口",
                    "data_gaps": [],
                    "owner": "管理员",
                    "next_step": "无需操作",
                    "evidence_refs": [],
                    "amount_refs": [],
                }
            ]
        }
    )
    result = DecisionAdvisorService(model).suggest(_facts())
    assert result.available is True
    assert result.suggestions[0].evidence_refs == []


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def test_decision_api_returns_unavailable_when_model_disabled(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/decision/suggestions",
            headers=ADMIN_HEADERS,
            json={
                "store_id": "store-test",
                "period": "2026-08",
                "scope": "formal",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["reason"] == "model_unavailable"
    assert body["suggestions"] == []
    with app.state.agent.db.connect() as conn:
        rows = conn.execute(
            "SELECT event_type, actor FROM audit_log "
            "WHERE event_type='decision.suggestions.requested'"
        ).fetchall()
    assert len(rows) == 1
