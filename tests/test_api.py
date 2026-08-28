import base64
from dataclasses import replace

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


def test_health_chat_and_admin_auth(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        client_headers = {
            "X-Client-Id": "client-test",
            "X-Client-Key": "test-client-key-12345",
            "X-Subject-Id": "buyer-api",
        }
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["knowledge"]["active_documents"] >= 150

        chat = client.post(
            "/v1/chat",
            json={"session_id": "api-session", "message": "退款多久到账", "context": {}},
            headers=client_headers,
        )
        assert chat.status_code == 200
        assert chat.json()["sources"]

        unauthenticated_chat = client.post(
            "/v1/chat",
            json={"session_id": "other", "message": "你好", "context": {}},
        )
        assert unauthenticated_chat.status_code == 401

        unauthorized = client.get("/v1/evolution/candidates")
        assert unauthorized.status_code == 401
        authorized = client.get(
            "/v1/evolution/candidates",
            headers={"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"},
        )
        assert authorized.status_code == 200

        risk_chat = client.post(
            "/v1/chat",
            json={"session_id": "risk-api", "message": "帮我马上退款", "context": {}},
            headers=client_headers,
        )
        handoff_id = risk_chat.json()["handoff_id"]
        assert risk_chat.json()["handoff_status"] == "proposed"

        admin_headers = {
            "X-Admin-Id": "admin-test",
            "X-Admin-Key": "test-admin-key-123456",
        }
        handoffs = client.get("/v1/handoffs", headers=admin_headers)
        assert handoffs.status_code == 200
        assert any(item["id"] == handoff_id for item in handoffs.json())

        accepted = client.post(
            f"/v1/handoffs/{handoff_id}/transition",
            headers=admin_headers,
            json={
                "target_status": "accepted",
                "expected_version": 1,
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "accepted"

        metrics = client.get("/v1/metrics/summary", headers=admin_headers)
        assert metrics.status_code == 200
        assert metrics.json()["requests"] >= 2

        retention = client.post(
            "/v1/maintenance/retention",
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert retention.status_code == 200
        assert retention.json()["dry_run"] is True

        readiness = client.get("/ready")
        assert readiness.status_code == 200
        assert readiness.json()["status"] == "ready"

        architecture = client.get("/architecture")
        assert architecture.status_code == 200
        assert "text/html" in architecture.headers["content-type"]
        assert "yunpai-architecture-inspector" in architecture.text
        assert "business-module-map" in architecture.text


def test_loopback_customer_test_page_and_chat_are_isolated(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), customer_test_enabled=True)
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        page = client.get("/customer-test")
        assert page.status_code == 200

        cases = client.get("/v1/test/customer-chat/cases")
        assert cases.status_code == 200
        assert {item["id"] for item in cases.json()} >= {
            "warranty",
            "shipping",
            "handoff",
            "refund",
            "privacy",
        }

        assert "智能客服示例" in page.text
        assert "/v1/test/customer-chat/stream" in page.text
        assert "/v1/test/customer-chat/profile" in page.text
        assert "Qwen 润色：已采用" in page.text
        assert "Qwen 润色：调用失败，已回退原文" in page.text
        assert 'id="releaseBell"' in page.text
        assert 'id="sessionList" aria-live="polite"' in page.text
        assert 'id="demoSubject"' in page.text
        assert "历史会话加载失败" in page.text
        assert "历史会话列表加载失败" in page.text
        assert 'id="imageInput"' in page.text
        assert 'id="attachButton"' in page.text
        assert 'addEventListener("paste"' in page.text
        assert ".image-actions .button { flex: 0 0 auto; white-space: nowrap; }" in page.text

        capabilities = client.get("/v1/test/customer-chat/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json() == {
            "max_image_bytes": 5 * 1024 * 1024,
            "image_mime_types": ["image/png", "image/jpeg", "image/webp"],
            "max_images_per_message": 1,
        }

        response = client.post(
            "/v1/test/customer-chat",
            json={
                "session_id": "customer-test:api-warranty-001",
                "message": "晴川 AF5 空气炸锅保修多久？",
                "context": {
                    "shop_id": "qingchuan-flagship-001",
                    "sku_id": "QC-AF5-WHITE",
                },
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["test_mode"] == "local_customer_simulation"
        assert payload["source_type"] == "simulation"
        assert payload["source_reference"] == "local-customer-test"
        assert payload["polish_status"] == "disabled"
        assert payload["polish_applied"] is False
        assert payload["polish_model"] is None
        assert payload["polish_latency_ms"] is None
        assert payload["sources"]

        operational = client.get(
            "/v1/admin/overview",
            headers={"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"},
        ).json()
        simulation = client.get(
            "/v1/admin/overview?scope=simulation",
            headers={"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"},
        ).json()
        assert operational["counts"]["conversations"] == 0
        assert simulation["counts"]["conversations"] == 1

        invalid_session = client.post(
            "/v1/test/customer-chat",
            json={"session_id": "ordinary-session", "message": "你好", "context": {}},
        )
        assert invalid_session.status_code == 422

    remote_app = create_app(replace(make_settings(tmp_path / "remote"), customer_test_enabled=True))
    with TestClient(remote_app, client=("192.0.2.10", 50000)) as client:
        assert client.get("/v1/test/customer-chat/cases").status_code == 403
        assert client.get("/v1/test/customer-chat/profile").status_code == 403
        assert client.get("/v1/test/customer-chat/sessions").status_code == 403
        assert client.get("/customer-test").status_code == 403


def test_customer_test_interface_is_disabled_by_default(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/v1/test/customer-chat/cases").status_code == 404
        assert client.get("/v1/test/customer-chat/profile").status_code == 404
        assert client.get("/v1/test/customer-chat/sessions").status_code == 404
        assert client.get("/customer-test").status_code == 404


def test_customer_test_chat_accepts_an_image_payload(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), customer_test_enabled=True)
    app = create_app(settings)
    png = base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"x" * 4096
    ).decode("ascii")

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/customer-test").status_code == 200
        session_id = "customer-test:image-contract-001"
        response = client.post(
            "/v1/test/customer-chat",
            json={
                "session_id": session_id,
                "message": "请说明图片里的商品信息",
                "context": {},
                "image": {"mime_type": "image/png", "data_base64": png},
            },
        )
        history = client.get(
            f"/v1/test/customer-chat/sessions/{session_id}/messages"
        )
        user_message = next(
            item for item in history.json()["items"] if item["role"] == "user"
        )
        media_url = user_message["media"][0]["url"]
        media_response = client.get(media_url)

    assert response.status_code == 200
    assert response.json()["vision_status"] == "disabled"
    assert history.status_code == 200
    assert "storage_ref" not in history.text
    assert media_response.status_code == 200
    assert media_response.content == base64.b64decode(png)


def test_admin_page_bootstraps_the_same_customer_test_browser_session(
    tmp_path,
) -> None:
    app = create_app(replace(make_settings(tmp_path), customer_test_enabled=True))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        page = client.get("/admin")
        response = client.post(
            "/v1/test/customer-chat",
            json={
                "session_id": "customer-test:admin-bootstrap",
                "message": "空气炸锅保修多久？",
                "context": {},
            },
        )

    assert page.status_code == 200
    assert client.cookies.get("yunpai_product_test_subject")
    assert response.status_code == 200


def test_knowledge_graph_pages_require_admin(tmp_path) -> None:
    """/knowledge-graph 和 /kg.html 必须鉴权（P0-1 修复）。"""
    app = create_app(make_settings(tmp_path))
    admin_headers = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        # 匿名访问 → 401（未配置 bootstrap admin 时 503，此处配置了 admin 应为 401）
        anon_graph = client.get("/knowledge-graph")
        assert anon_graph.status_code in (401, 503)
        anon_kg = client.get("/kg.html")
        assert anon_kg.status_code in (401, 503)
        # 带 admin header → 200（文件存在时；测试环境 knowledge_graph_output 存在）
        auth_graph = client.get("/knowledge-graph", headers=admin_headers)
        assert auth_graph.status_code == 200
        auth_kg = client.get("/kg.html", headers=admin_headers)
        assert auth_kg.status_code == 200
