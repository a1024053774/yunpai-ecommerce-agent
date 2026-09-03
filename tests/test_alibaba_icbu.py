from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_settings
from ecommerce_agent.api import create_app
from ecommerce_agent.alibaba_icbu import (
    AlibabaIcbuConnector,
    AlibabaIcbuError,
    AlibabaIcbuIntegrationService,
    AlibabaIcbuRemoteError,
    AlibabaIcbuTopClient,
)
from ecommerce_agent.connectors import PullRequest
from ecommerce_agent.database import Database


def configured_settings(tmp_path):
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    return replace(
        make_settings(tmp_path),
        alibaba_icbu_enabled=True,
        alibaba_icbu_app_key="512464",
        alibaba_icbu_app_secret="icbu-app-secret",
        alibaba_icbu_redirect_uri="https://example.test/v1/integrations/alibaba-icbu/oauth/callback",
        alibaba_icbu_credential_key=key,
        alibaba_icbu_oauth_token_url="https://mock.test/oauth/token",
        alibaba_icbu_top_gateway="https://mock.test/top",
    )


def expected_hmac(form: dict[str, list[str]]) -> str:
    flattened = {key: values[0] for key, values in form.items()}
    canonical = "".join(
        f"{key}{value}"
        for key, value in sorted(flattened.items())
        if key != "sign"
    ).encode("utf-8")
    return hmac.new(b"icbu-app-secret", canonical, hashlib.md5).hexdigest().upper()  # noqa: S324


def test_icbu_oauth_refresh_and_readonly_product_access(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        calls.append({"url": str(request.url), "form": form})
        if request.url.path == "/oauth/token":
            grant_type = form.get("grant_type", [""])[0]
            if grant_type == "authorization_code":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access-old",
                        "refresh_token": "refresh-secret",
                        "expires_in": 3600,
                        "re_expires_in": 7200,
                        "taobao_user_id": "seller-1",
                        "taobao_user_nick": "seller-one",
                    },
                )
            assert grant_type == "refresh_token"
            assert form["refresh_token"] == ["refresh-secret"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-refreshed",
                    "expires_in": 3600,
                },
            )

        method = form.get("method", [""])[0]
        assert form["session"] == ["access-refreshed"]
        assert form["sign_method"] == ["hmac"]
        assert form["sign"][0] == expected_hmac(form)
        changed = {key: list(values) for key, values in form.items()}
        changed["page_size"] = ["29"]
        assert expected_hmac(changed) != form["sign"][0]
        if method == "alibaba.icbu.product.list":
            assert form["page_size"] == ["30"]
            return httpx.Response(
                200,
                json={
                    "alibaba_icbu_product_list_response": {
                        "code": "0",
                        "request_id": "req-list",
                        "current_page": 1,
                        "page_size": 30,
                        "total_item": 1,
                        "products": [
                            {
                                "id": "123",
                                "product_id": "encrypted-123",
                                "subject": "Test Product",
                                "gmt_modified": "2026-08-30 12:00:00",
                            }
                        ],
                    }
                },
            )
        if method == "alibaba.icbu.product.get":
            assert form["product_id"] == ["encrypted-123"]
            return httpx.Response(
                200,
                json={
                    "alibaba_icbu_product_get_response": {
                        "code": "0",
                        "request_id": "req-detail",
                        "product": {
                            "product_id": "123",
                            "subject": "Test Product",
                            "gmt_modified": "2026-08-30 12:00:00",
                            "product_sku": {
                                "skus": [{"sku_id": "SKU-1", "sku_code": "OUTER-1"}]
                            },
                        },
                    }
                },
            )
        assert method == "alibaba.icbu.product.sku.inventory.get"
        assert form["product_id"] == ["123"]
        return httpx.Response(
            200,
            json={
                "alibaba_icbu_product_sku_inventory_get_response": {
                    "code": "0",
                    "request_id": "req-inventory",
                    "result": {
                        "success": True,
                        "trace_id": "trace-inventory",
                        "data_list": [
                            {
                                "sku_id": "SKU-1",
                                "inventory": "9",
                                "inventory_code": "CN_LOCAL_01",
                            }
                        ],
                    },
                }
            },
        )

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    top = AlibabaIcbuTopClient(settings, client=http_client)
    service = AlibabaIcbuIntegrationService(db, settings, top_client=top)

    started = service.begin_authorization("tenant-test", "store-1")
    auth_query = parse_qs(urlparse(started["authorization_url"]).query)
    assert auth_query["client_id"] == ["512464"]
    assert auth_query["sp"] == ["icbu"]

    connected = service.complete_authorization("authorization-code", started["state"])
    assert connected["status"] == "authorized"
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT credential_ciphertext FROM platform_connections WHERE platform='alibaba_icbu'"
        ).fetchone()[0]
        assert "access-old" not in stored
        conn.execute(
            "UPDATE platform_connections SET token_expires_at=? WHERE platform='alibaba_icbu'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )

    catalog = service.list_products(
        "tenant-test", store_id="store-1", cursor=None, limit=50
    )
    assert catalog.resource == "catalog"
    assert catalog.has_more is False
    assert catalog.records[0].source_id == "alibaba-icbu:store-1:product:123"
    assert catalog.records[0].payload["product"]["subject"] == "Test Product"

    detail = service.product_detail(
        "tenant-test",
        store_id="store-1",
        encrypted_product_id="encrypted-123",
    )
    assert detail.records[0].payload["product"]["product_sku"]["skus"][0]["sku_id"] == "SKU-1"

    inventory = service.product_inventory(
        "tenant-test", store_id="store-1", plain_product_id="123"
    )
    assert inventory.resource == "inventory"
    assert inventory.records[0].payload == {
        "store_id": "store-1",
        "product_id": "123",
        "sku_id": "SKU-1",
        "inventory": "9",
        "inventory_code": "CN_LOCAL_01",
    }

    token_calls = [call["form"] for call in calls if str(call["url"]).endswith("/oauth/token")]
    assert [form["grant_type"] for form in token_calls] == [
        ["authorization_code"],
        ["refresh_token"],
    ]
    service.close()


def test_icbu_read_fails_closed_without_authorized_store(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    db = Database(settings.app_db_path)
    db.initialize()
    service = AlibabaIcbuIntegrationService(db, settings)

    capability = service.capabilities("tenant-test")
    assert capability["capabilities"]["catalog_read"]["available"] is False
    with pytest.raises(AlibabaIcbuError, match="no authorized Alibaba ICBU connection"):
        service.list_products("tenant-test", store_id="store-1")
    service.close()


def test_icbu_capabilities_endpoint_is_admin_only(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), alibaba_icbu_enabled=True)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/v1/integrations/alibaba-icbu/capabilities").status_code == 401
        response = client.get(
            "/v1/integrations/alibaba-icbu/capabilities",
            headers={
                "X-Admin-Id": "admin-test",
                "X-Admin-Key": "test-admin-key-123456",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["platform"] == "alibaba_icbu"
        assert body["official_contract"]["write_methods_enabled"] is False
        assert body["capabilities"]["domain_sync"]["available"] is False


def test_icbu_inventory_missing_quantity_is_not_defaulted_to_zero(tmp_path) -> None:
    settings = configured_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "alibaba_icbu_product_sku_inventory_get_response": {
                    "code": "0",
                    "result": {
                        "success": True,
                        "data_list": [
                            {
                                "sku_id": "SKU-1",
                                "inventory_code": "CN_LOCAL_01",
                            }
                        ],
                    },
                }
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    connector = AlibabaIcbuConnector(
        AlibabaIcbuTopClient(settings, client=http_client),
        access_token="access-token",
        store_id="store-1",
    )
    with pytest.raises(AlibabaIcbuRemoteError, match="without inventory"):
        connector.pull(PullRequest(resource="inventory", cursor="123"))
    http_client.close()


def test_icbu_inventory_missing_data_list_is_not_treated_as_empty(tmp_path) -> None:
    settings = configured_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "alibaba_icbu_product_sku_inventory_get_response": {
                    "code": "0",
                    "result": {"success": True},
                }
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    connector = AlibabaIcbuConnector(
        AlibabaIcbuTopClient(settings, client=http_client),
        access_token="access-token",
        store_id="store-1",
    )
    with pytest.raises(AlibabaIcbuRemoteError, match="data_list"):
        connector.pull(PullRequest(resource="inventory", cursor="123"))
    http_client.close()


def test_icbu_refresh_failure_disables_the_connection(tmp_path) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        if form.get("grant_type") == ["authorization_code"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "access-old",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(503, json={"error": "temporarily_unavailable"})

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = AlibabaIcbuIntegrationService(
        db,
        settings,
        top_client=AlibabaIcbuTopClient(settings, client=http_client),
    )
    started = service.begin_authorization("tenant-test", "store-1")
    service.complete_authorization("authorization-code", started["state"])
    with db.connect() as conn:
        conn.execute(
            "UPDATE platform_connections SET token_expires_at=? "
            "WHERE platform='alibaba_icbu'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )

    with pytest.raises(AlibabaIcbuRemoteError):
        service.list_products("tenant-test", store_id="store-1")

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM platform_connections "
            "WHERE platform='alibaba_icbu'"
        ).fetchone()
    assert row["status"] == "error"
    assert service.capabilities("tenant-test")["capabilities"][
        "catalog_read"
    ]["available"] is False
    service.close()


def test_icbu_product_routes_use_the_authorized_store_boundary(tmp_path) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 3600,
                },
            )
        assert form["sign"][0] == expected_hmac(form)
        method = form["method"][0]
        if method == AlibabaIcbuConnector.PRODUCT_LIST:
            assert form["gmt_modified_from"] == ["2026-08-30 00:00:00"]
            assert form["gmt_modified_to"] == ["2026-08-31 00:00:00"]
            assert form["page_size"] == ["30"]
            return httpx.Response(
                200,
                json={
                    "alibaba_icbu_product_list_response": {
                        "code": "0",
                        "current_page": 1,
                        "page_size": 30,
                        "total_item": 1,
                        "products": [
                            {
                                "id": "123",
                                "product_id": "encrypted-123",
                                "gmt_modified": "2026-08-30 12:00:00",
                            }
                        ],
                    }
                },
            )
        if method == AlibabaIcbuConnector.PRODUCT_DETAIL:
            assert form["product_id"] == ["encrypted-123"]
            return httpx.Response(
                200,
                json={
                    "alibaba_icbu_product_get_response": {
                        "code": "0",
                        "product": {
                            "product_id": "123",
                            "gmt_modified": "2026-08-30 12:00:00",
                        },
                    }
                },
            )
        assert method == AlibabaIcbuConnector.PRODUCT_INVENTORY
        assert form["product_id"] == ["123"]
        return httpx.Response(
            200,
            json={
                "alibaba_icbu_product_sku_inventory_get_response": {
                    "code": "0",
                    "result": {
                        "success": True,
                        "data_list": [
                            {"sku_id": "SKU-1", "inventory": "9"}
                        ],
                    },
                }
            },
        )

    app = create_app(settings)
    service = app.state.agent.alibaba_icbu
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service.top = AlibabaIcbuTopClient(settings, client=http_client)
    started = service.begin_authorization("tenant-test", "store-1")
    service.complete_authorization("authorization-code", started["state"])
    headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }

    with TestClient(app) as client:
        catalog = client.get(
            "/v1/integrations/alibaba-icbu/products",
            params={
                "store_id": "store-1",
                "gmt_modified_from": "2026-08-30 00:00:00",
                "gmt_modified_to": "2026-08-31 00:00:00",
            },
            headers=headers,
        )
        reversed_window = client.get(
            "/v1/integrations/alibaba-icbu/products",
            params={
                "store_id": "store-1",
                "gmt_modified_from": "2026-08-31 00:00:00",
                "gmt_modified_to": "2026-08-30 00:00:00",
            },
            headers=headers,
        )
        detail = client.get(
            "/v1/integrations/alibaba-icbu/products/encrypted-123",
            params={"store_id": "store-1"},
            headers=headers,
        )
        inventory = client.get(
            "/v1/integrations/alibaba-icbu/products/123/inventory",
            params={"store_id": "store-1"},
            headers=headers,
        )

    assert catalog.status_code == 200
    assert catalog.json()["records"][0]["source_id"] == (
        "alibaba-icbu:store-1:product:123"
    )
    assert reversed_window.status_code == 409
    assert "gmt_modified_from" in reversed_window.json()["detail"]
    assert detail.status_code == 200
    assert detail.json()["records"][0]["payload"]["product_id"] == (
        "encrypted-123"
    )
    assert inventory.status_code == 200
    assert inventory.json()["records"][0]["payload"]["inventory"] == "9"
    http_client.close()
