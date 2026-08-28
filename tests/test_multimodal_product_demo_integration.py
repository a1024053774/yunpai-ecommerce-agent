from __future__ import annotations

import importlib
import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.database import Database

from conftest import make_settings


def test_latest_main_exposes_multimodal_modules() -> None:
    assert importlib.import_module("ecommerce_agent.vision")
    assert importlib.import_module("ecommerce_agent.polish")
    assert importlib.import_module("ecommerce_agent.message_media")


def test_product_demo_uses_new_customer_ui_and_ecommerce_context(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), customer_test_enabled=True)
    app = create_app(settings)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        page = client.get("/customer-test")
        assert page.status_code == 200
        assert "智能客服示例" in page.text
        assert 'id="releaseBell"' in page.text
        assert 'id="sessionList"' in page.text
        assert "/v1/test/customer-chat/stream" in page.text
        assert client.cookies.get("yunpai_product_test_subject")

        profile = client.get("/v1/test/customer-chat/profile")
        assert profile.status_code == 200
        profile_payload = profile.json()
        assert profile_payload["context"] == {
            "shop_id": "qingchuan-flagship-001",
            "platform": "virtual-taobao",
            "sku_id": "QC-AF5-WHITE",
            "order_id": "QC-ORDER-1001",
        }
        assert profile_payload["default_demo_subject_id"] == "af5-order-1001"

        fixture = json.loads(
            (
                Path(__file__).parents[1]
                / "src/ecommerce_agent/fixtures/virtual_store_v1.json"
            ).read_text()
        )
        orders = {item["order_id"]: item for item in fixture["orders"]}
        for subject in profile_payload["demo_subjects"]:
            context = subject["context"]
            if "sku_id" not in context or "order_id" not in context:
                continue
            assert context["sku_id"] in {
                line["sku_id"] for line in orders[context["order_id"]]["lines"]
            }


def test_product_demo_stream_and_history_reuse_ecommerce_service(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), customer_test_enabled=True)
    app = create_app(settings)
    session_id = "customer-test:product-demo-stream-001"

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.get("/customer-test")
        with client.stream(
            "POST",
            "/v1/test/customer-chat/stream",
            json={
                "session_id": session_id,
                "message": "晴川 AF5 空气炸锅有什么卖点？",
                "demo_subject_id": "af5-order-1001",
                "context": {"shop_id": "other-shop"},
            },
        ) as response:
            assert response.status_code == 200
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

        result = next(item["response"] for item in events if item["event"] == "result")
        assert result["session_id"] == session_id
        assert result["answer"]

        sessions = client.get("/v1/test/customer-chat/sessions")
        assert sessions.status_code == 200
        assert sessions.json()["items"][0]["session_id"] == session_id

        messages = client.get(
            f"/v1/test/customer-chat/sessions/{session_id}/messages"
        )
        assert messages.status_code == 200
        assert [item["role"] for item in messages.json()["items"]] == [
            "user",
            "assistant",
        ]

        with Database(settings.app_db_path).connect() as conn:
            stored = conn.execute(
                """
                SELECT source_type, source_reference
                FROM sessions WHERE external_session_id=?
                """,
                (session_id,),
            ).fetchone()
            snapshot = conn.execute(
                """
                SELECT bundle_json FROM context_snapshots
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()

        assert dict(stored) == {
            "source_type": "simulation",
            "source_reference": "local-customer-test",
        }
        assert (
            json.loads(snapshot["bundle_json"])["trusted_session_state"]["store_id"]
            == "qingchuan-flagship-001"
        )
        bundle = json.loads(snapshot["bundle_json"])
        assert bundle["trusted_session_state"]["business_context_authorized"] is True
        assert bundle["current_subject"]["order_id"] == "QC-ORDER-1001"
        assert bundle["current_subject"]["sku_id"] == "QC-AF5-WHITE"


def test_product_demo_subject_selection_is_server_controlled(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), customer_test_enabled=True)
    app = create_app(settings)
    session_id = "customer-test:hm3-subject-selection"

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.get("/customer-test")
        response = client.post(
            "/v1/test/customer-chat",
            json={
                "session_id": session_id,
                "message": "我的加湿器订单现在是什么状态？",
                "demo_subject_id": "hm3-order-1005",
                "context": {
                    "shop_id": "other-shop",
                    "sku_id": "QC-AF5-WHITE",
                    "order_id": "QC-ORDER-1001",
                },
            },
        )

        with Database(settings.app_db_path).connect() as conn:
            snapshot = conn.execute(
                """
                SELECT bundle_json FROM context_snapshots
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()

    assert response.status_code == 200
    bundle = json.loads(snapshot["bundle_json"])
    assert bundle["trusted_session_state"]["store_id"] == "qingchuan-flagship-001"
    assert bundle["current_subject"]["sku_id"] == "QC-HM-3L"
    assert bundle["current_subject"]["order_id"] == "QC-ORDER-1005"


def test_product_demo_sessions_are_isolated_by_browser_cookie(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), customer_test_enabled=True)
    app = create_app(settings)
    first_cookie = {"yunpai_product_test_subject": "browser-one-1234567890"}
    second_cookie = {"yunpai_product_test_subject": "browser-two-1234567890"}

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.cookies.set(*next(iter(first_cookie.items())))
        assert client.post(
            "/v1/test/customer-chat",
            json={
                "session_id": "customer-test:browser-one-session",
                "message": "空气炸锅保修多久？",
                "context": {},
            },
        ).status_code == 200
        first_sessions = client.get("/v1/test/customer-chat/sessions").json()[
            "items"
        ]

        client.cookies.set(*next(iter(second_cookie.items())))
        assert client.post(
            "/v1/test/customer-chat",
            json={
                "session_id": "customer-test:browser-two-session",
                "message": "加湿器滤芯怎么换？",
                "context": {},
            },
        ).status_code == 200
        second_sessions = client.get("/v1/test/customer-chat/sessions").json()[
            "items"
        ]

    assert {item["session_id"] for item in first_sessions} == {
        "customer-test:browser-one-session"
    }
    assert {item["session_id"] for item in second_sessions} == {
        "customer-test:browser-two-session"
    }


def test_product_demo_stream_uses_shared_scope_error_protocol(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), customer_test_enabled=True)
    app = create_app(settings)
    session_id = "customer-test:shared-sse-scope"

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.cookies.set("yunpai_product_test_subject", "browser-one-1234567890")
        assert client.post(
            "/v1/test/customer-chat",
            json={
                "session_id": session_id,
                "message": "空气炸锅保修多久？",
                "context": {},
            },
        ).status_code == 200

        client.cookies.set("yunpai_product_test_subject", "browser-two-1234567890")
        with client.stream(
            "POST",
            "/v1/test/customer-chat/stream",
            json={
                "session_id": session_id,
                "message": "继续",
                "context": {},
            },
        ) as response:
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

    assert response.status_code == 200
    assert events[-2]["event"] == "error"
    assert events[-2]["code"] == "session_scope_conflict"
    assert events[-1]["event"] == "done"
