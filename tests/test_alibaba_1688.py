from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_settings
from ecommerce_agent.alibaba_1688 import (
    Alibaba1688Client,
    Alibaba1688Connector,
    Alibaba1688Error,
    Alibaba1688IntegrationService,
    Alibaba1688RemoteError,
    _platform_datetime,
    sign_1688_request,
)
from ecommerce_agent.api import create_app
from ecommerce_agent.connectors import PullRecord, PullRequest
from ecommerce_agent.database import Database


def configured_settings(tmp_path):
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    return replace(
        make_settings(tmp_path),
        alibaba_1688_enabled=True,
        alibaba_1688_app_key="5043656",
        alibaba_1688_app_secret="1688-app-secret",
        alibaba_1688_redirect_uri=(
            "https://example.test/v1/integrations/alibaba-1688/oauth/callback"
        ),
        alibaba_1688_credential_key=key,
        alibaba_1688_gateway="https://mock.test",
        alibaba_1688_oauth_authorize_url=(
            "https://auth.1688.com/oauth/authorize"
        ),
        alibaba_1688_oauth_site="1688",
    )


def configured_hosted_settings(tmp_path, tenant_id: str):
    return replace(
        configured_settings(tmp_path),
        alibaba_1688_hosted_tenant_id=tenant_id,
    )


def expected_signature(path: str, form: dict[str, list[str]]) -> str:
    flattened = {key: values[0] for key, values in form.items()}
    canonical = path + "".join(
        f"{key}{value}"
        for key, value in sorted(flattened.items())
        if key != "_aop_signature"
    )
    return hmac.new(
        b"1688-app-secret", canonical.encode("utf-8"), hashlib.sha1
    ).hexdigest().upper()


def test_1688_signature_matches_the_official_vector() -> None:
    assert sign_1688_request(
        "param2/1/system/currentTime/1000000",
        {"b": "2", "a": "1"},
        "test123",
    ) == "33E54F4F7B989E3E0E912D3FBD2F1A03CA7CCE88"


def test_1688_compact_timestamp_preserves_the_platform_timezone() -> None:
    assert _platform_datetime("20260903120000000+0800") == datetime(
        2026, 9, 3, 4, 0, tzinfo=UTC
    )


def test_1688_product_list_accepts_observed_page_result_envelope() -> None:
    class StubClient:
        def call(self, namespace, method, business_params, *, access_token):
            assert namespace == "com.alibaba.product"
            assert method == "alibaba.product.list.get"
            assert business_params["pageNo"] == 1
            assert business_params["pageSize"] == 1
            assert access_token == "access-token"
            return {
                "success": True,
                "total": 1,
                "result": {
                    "pageResult": {
                        "pageIndex": 1,
                        "sizePerPage": 1,
                        "totalRecords": 2,
                        "resultList": [
                            {
                                "productID": "PRODUCT-REAL",
                                "subject": "测试商品",
                                "status": "published",
                                "lastUpdateTime": "20260902120000000+0800",
                                "skuInfos": [
                                    {
                                        "skuId": "SKU-REAL",
                                        "price": "12.50",
                                        "priceRange": [
                                            {"startQuantity": 2, "price": "11.00", "ignored": "drop"}
                                        ],
                                    }
                                ],
                                "saleInfo": {
                                    "quoteType": 3,
                                    "priceRanges": [
                                        {"startQuantity": 2, "price": "11.00", "ignored": "drop"}
                                    ],
                                },
                            }
                        ],
                    }
                },
            }

    connector = Alibaba1688Connector(
        StubClient(), access_token="access-token", store_id="merchant-store"
    )

    batch = connector.list_products(
        PullRequest(resource="catalog", cursor=None, limit=1)
    )

    assert len(batch.records) == 1
    assert batch.records[0].payload["productID"] == "PRODUCT-REAL"
    assert batch.records[0].payload["skuInfos"][0]["priceRange"] == [
        {"startQuantity": 2, "price": "11.00"}
    ]
    assert batch.records[0].payload["saleInfo"]["priceRanges"] == [
        {"startQuantity": 2, "price": "11.00"}
    ]
    assert batch.records[0].occurred_at == "2026-09-02T04:00:00+00:00"
    assert batch.next_cursor == "2"
    assert batch.has_more is True
    assert batch.upstream_total == 2


def test_1688_detail_endpoints_accept_observed_response_envelopes(
    tmp_path,
) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return httpx.Response(
                200,
                json={
                    "access_token": "access-current",
                    "refresh_token": "refresh-current",
                    "expires_in": "36000",
                    "memberId": "merchant-member",
                },
            )
        if request.url.path.endswith(
            "/com.alibaba.trade/alibaba.trade.ec.getOrder.sellerView/5043656"
        ):
            assert form["orderId"] == ["ORDER-DETAIL"]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "baseInfo": {
                            "idOfStr": "ORDER-DETAIL",
                            "status": "waitsellersend",
                            "createTime": "20260903120000000+0800",
                            "modifyTime": "20260903120500000+0800",
                            "totalAmount": "99.00",
                            "buyerContact": {"phone": "must-not-return"},
                        },
                        "productItems": [
                            {
                                "subItemID": "LINE-DETAIL",
                                "productID": "PRODUCT-DETAIL",
                                "skuID": "SKU-DETAIL",
                                "name": "真实详情商品",
                                "price": "99.00",
                                "quantity": 1,
                            }
                        ],
                        "nativeLogistics": {"address": "must-not-return"},
                    },
                },
            )
        assert request.url.path.endswith(
            "/com.alibaba.product/alibaba.product.get/5043656"
        )
        assert form["productID"] == ["PRODUCT-DETAIL"]
        return httpx.Response(
            200,
            json={
                "productInfo": {
                    "productID": "PRODUCT-DETAIL",
                    "subject": "真实详情商品",
                    "status": "published",
                    "lastUpdateTime": "20260903121000000+0800",
                    "skuInfos": [
                        {
                            "skuId": "SKU-DETAIL",
                            "amountOnSale": "8",
                            "consignPrice": "88.00",
                            "ignored": "must-not-return",
                        }
                    ],
                    "saleInfo": {
                        "amountOnSale": "8",
                        "invReduceType": "2",
                        "priceRanges": [
                            {"startQuantity": 2, "price": "88.00"}
                        ],
                    },
                }
            },
        )

    app = create_app(settings)
    integration = app.state.agent.alibaba_1688
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    integration.client = Alibaba1688Client(settings, client=http_client)
    started = integration.begin_authorization(
        "tenant-test", "merchant-member"
    )
    integration.complete_authorization("authorization-code", started["state"])
    headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }

    with TestClient(app) as client:
        order = client.get(
            "/v1/integrations/alibaba-1688/orders/ORDER-DETAIL",
            params={"store_id": "merchant-member"},
            headers=headers,
        )
        availability = client.get(
            "/v1/integrations/alibaba-1688/products/PRODUCT-DETAIL/availability",
            params={"store_id": "merchant-member"},
            headers=headers,
        )
        capabilities = client.get(
            "/v1/integrations/alibaba-1688/capabilities",
            headers=headers,
        )
        product = client.get(
            "/v1/integrations/alibaba-1688/products/PRODUCT-DETAIL",
            params={"store_id": "merchant-member"},
            headers=headers,
        )

    assert order.status_code == 200
    assert order.json()["source_id"].endswith(":order:ORDER-DETAIL")
    assert order.json()["payload"]["baseInfo"]["idOfStr"] == "ORDER-DETAIL"
    assert "buyerContact" not in order.json()["payload"]["baseInfo"]
    assert "nativeLogistics" not in order.json()["payload"]
    assert product.status_code == 200
    assert product.json()["source_id"].endswith(":product:PRODUCT-DETAIL")
    assert product.json()["payload"]["skuInfos"] == [
        {
            "skuId": "SKU-DETAIL",
            "amountOnSale": "8",
            "consignPrice": "88.00",
        }
    ]
    assert product.json()["payload"]["saleInfo"]["invReduceType"] == "2"
    assert availability.status_code == 200
    assert availability.json()["semantic_role"] == "channel_available"
    assert availability.json()["source_product_id"] == "PRODUCT-DETAIL"
    assert availability.json()["records"] == [
        {
            "semantic_role": "channel_available",
            "scope": "product",
            "source_sku_id": None,
            "warehouse_code": None,
            "available_qty": "8",
        },
        {
            "semantic_role": "channel_available",
            "scope": "sku",
            "source_sku_id": "SKU-DETAIL",
            "warehouse_code": None,
            "available_qty": "8",
        },
    ]
    assert not {
        "on_hand", "reserved", "inbound", "warehouse_id"
    } & availability.json().keys()
    assert capabilities.json()["capabilities"][
        "channel_availability_read"
    ]["available"] is True
    assert capabilities.json()["capabilities"]["inventory_read"][
        "available"
    ] is False
    http_client.close()


def test_1688_product_detail_rejects_missing_product_info() -> None:
    class StubClient:
        def call(self, namespace, method, business_params, *, access_token):
            return {"success": True, "result": {}}

    connector = Alibaba1688Connector(
        StubClient(), access_token="access-token", store_id="merchant-store"
    )

    with pytest.raises(Alibaba1688RemoteError, match="product detail"):
        connector.get_product("PRODUCT-MISSING")


def test_1688_channel_availability_rejects_incomplete_sku_snapshot() -> None:
    class StubClient:
        def call(self, namespace, method, business_params, *, access_token):
            return {
                "productInfo": {
                    "productID": "PRODUCT-INCOMPLETE",
                    "subject": "不完整可售量商品",
                    "lastUpdateTime": "20260903121000000+0800",
                    "skuInfos": [
                        {
                            "skuId": "SKU-INCOMPLETE",
                            "consignPrice": "88.00",
                        }
                    ],
                    "saleInfo": {"amountOnSale": "5"},
                }
            }

    connector = Alibaba1688Connector(
        StubClient(), access_token="access-token", store_id="merchant-store"
    )

    with pytest.raises(
        Alibaba1688RemoteError, match="SKU-INCOMPLETE.*amountOnSale"
    ):
        connector.get_product_availability("PRODUCT-INCOMPLETE")


    class ProductOnlyClient:
        def call(self, namespace, method, business_params, *, access_token):
            return {
                "productInfo": {
                    "productID": "PRODUCT-ONLY",
                    "subject": "无 SKU 商品",
                    "lastUpdateTime": "20260903122000000+0800",
                    "saleInfo": {
                        "amountOnSale": "3",
                        "invReduceType": "1",
                        "unit": "件",
                    },
                }
            }

    product_only = Alibaba1688Connector(
        ProductOnlyClient(),
        access_token="access-token",
        store_id="merchant-store",
    ).get_product_availability("PRODUCT-ONLY")

    assert product_only["records"] == [
        {
            "semantic_role": "channel_available",
            "scope": "product",
            "source_sku_id": None,
            "warehouse_code": None,
            "available_qty": "3",
        }
    ]


def test_1688_channel_availability_rejects_missing_stable_source_timestamp() -> None:
    class StubClient:
        def call(self, namespace, method, business_params, *, access_token):
            return {
                "productInfo": {
                    "productID": "PRODUCT-NO-SOURCE-TIME",
                    "subject": "缺少来源时间的商品",
                    "skuInfos": [
                        {"skuId": "SKU-NO-SOURCE-TIME", "amountOnSale": "4"}
                    ],
                    "saleInfo": {"amountOnSale": "4"},
                }
            }

    connector = Alibaba1688Connector(
        StubClient(), access_token="access-token", store_id="merchant-store"
    )

    with pytest.raises(
        Alibaba1688RemoteError, match="stable source timestamp"
    ):
        connector.get_product_availability("PRODUCT-NO-SOURCE-TIME")


def test_1688_snapshot_input_uses_source_version_as_timestamp_authority() -> None:
    availability = {
        "connector_id": "alibaba-1688",
        "store_id": "merchant-store",
        "source_product_id": "PRODUCT-NO-SOURCE-TIME",
        "source_updated_at": "2026-09-03T04:00:00+00:00",
        "source_version": "payload-sha256:" + "a" * 64,
        "source_payload_hash": "a" * 64,
        "observed_at": "2026-09-03T04:01:00+00:00",
        "records": [
            {
                "scope": "product",
                "source_sku_id": None,
                "warehouse_code": None,
                "available_qty": "4",
            }
        ],
    }

    with pytest.raises(
        Alibaba1688RemoteError, match="stable source timestamp"
    ):
        Alibaba1688IntegrationService._availability_snapshot_input(availability)


def test_1688_catalog_preserves_price_roles_without_using_consign_price_as_sale_price() -> None:
    record = PullRecord(
        source_id="1688:merchant-store:product:PRODUCT-PRICE",
        source_version="2026-09-02T04:00:00+00:00",
        occurred_at="2026-09-02T04:00:00+00:00",
        payload={
            "productID": "PRODUCT-PRICE",
            "subject": "分层报价商品",
            "status": "published",
            "lastUpdateTime": "20260902120000000+0800",
            "skuInfos": [
                {
                    "skuId": "SKU-PRICE",
                    "price": "12.50",
                    "retailPrice": "20.00",
                    "consignPrice": "9.80",
                    "takeSamplePrice": "15.00",
                    "priceRange": [
                        {"startQuantity": 2, "price": "11.00"}
                    ],
                }
            ],
            "saleInfo": {
                "quoteType": 3,
                "priceRanges": [
                    {"startQuantity": 2, "price": "11.00"}
                ],
            },
        },
    )

    mapped = Alibaba1688IntegrationService._catalog_upserts(
        "merchant-store", record
    )

    assert mapped[0].sale_price == Decimal("12.50")
    assert mapped[0].attributes["price_basis"] == "price"
    assert mapped[0].attributes["retail_price"] == "20.00"
    assert mapped[0].attributes["consign_price"] == "9.80"
    assert mapped[0].attributes["take_sample_price"] == "15.00"
    assert mapped[0].attributes["quote_type"] == "3"
    assert json.loads(mapped[0].attributes["price_range"]) == [
        {"startQuantity": 2, "price": "11.00"}
    ]
    assert json.loads(mapped[0].attributes["price_ranges"]) == [
        {"startQuantity": 2, "price": "11.00"}
    ]


def test_1688_catalog_rejects_consign_price_only_sku() -> None:
    record = PullRecord(
        source_id="1688:merchant-store:product:PRODUCT-NO-PRICE",
        source_version="2026-09-02T04:00:00+00:00",
        occurred_at="2026-09-02T04:00:00+00:00",
        payload={
            "productID": "PRODUCT-NO-PRICE",
            "subject": "无直接报价商品",
            "status": "published",
            "lastUpdateTime": "20260902120000000+0800",
            "skuInfos": [
                {"skuId": "SKU-NO-PRICE", "consignPrice": "9.80"}
            ],
        },
    )

    with pytest.raises(Alibaba1688Error, match="price is required"):
        Alibaba1688IntegrationService._catalog_upserts(
            "merchant-store", record
        )


def test_1688_syncs_orders_and_catalog_through_public_domain_services(
    tmp_path,
) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            assert form["grant_type"] == ["authorization_code"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-current",
                    "refresh_token": "refresh-current",
                    "expires_in": "36000",
                    "memberId": "merchant-member",
                },
            )

        path = request.url.path.removeprefix("/openapi/")
        assert form["access_token"] == ["access-current"]
        assert form["_aop_signature"][0] == expected_signature(path, form)
        changed = {key: list(values) for key, values in form.items()}
        changed["pageSize"] = ["19"]
        assert expected_signature(path, changed) != form["_aop_signature"][0]

        if request.url.path.endswith(
            "/com.alibaba.trade/alibaba.trade.ec.getOrderList.sellerView/5043656"
        ):
            assert form["page"] == ["1"]
            assert form["pageSize"] == ["20"]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "totalRecord": 1,
                    "result": [
                        {
                            "baseInfo": {
                                "idOfStr": "ORDER-1",
                                "status": "waitsellersend",
                                "createTime": "20260901120000000+0800",
                                "modifyTime": "20260901120500000+0800",
                                "totalAmount": "199.00",
                                "buyerContact": {
                                    "phone": "sensitive-phone-must-not-persist"
                                },
                            },
                            "productItems": [
                                {
                                    "subItemID": "LINE-1",
                                    "productID": "PRODUCT-1",
                                    "skuID": "SKU-1",
                                    "name": "测试商品",
                                    "price": "99.50",
                                    "quantity": 2,
                                }
                            ],
                        }
                    ],
                },
            )

        assert request.url.path.endswith(
            "/com.alibaba.product/alibaba.product.list.get/5043656"
        )
        assert form["pageNo"] == ["1"]
        assert form["pageSize"] == ["20"]
        assert form["needDetail"] == ["true"]
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalRecords": 1,
                "result": [
                    {
                        "productID": "PRODUCT-1",
                        "subject": "测试商品",
                        "status": "published",
                        "categoryID": "1031910",
                        "lastUpdateTime": "20260901121000000+0800",
                        "skuInfos": [
                            {
                                "skuId": "SKU-1",
                                "skuCode": "MERCHANT-1",
                                "price": "99.50",
                                "amountOnSale": "7",
                            }
                        ],
                        "saleInfo": {"unit": "件", "amountOnSale": "7"},
                    }
                ],
            },
        )

    app = create_app(settings)
    integration = app.state.agent.alibaba_1688
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    integration.client = Alibaba1688Client(settings, client=http_client)
    started = integration.begin_authorization(
        "tenant-test", "merchant-member"
    )
    query = parse_qs(urlparse(started["authorization_url"]).query)
    assert query["client_id"] == ["5043656"]
    assert query["site"] == ["1688"]
    integration.complete_authorization("authorization-code", started["state"])

    headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }
    with TestClient(app) as client:
        orders = client.post(
            "/v1/integrations/alibaba-1688/sync/orders",
            params={"store_id": "merchant-member"},
            headers=headers,
        )
        products = client.post(
            "/v1/integrations/alibaba-1688/sync/products",
            params={"store_id": "merchant-member"},
            headers=headers,
        )
        repeated = client.post(
            "/v1/integrations/alibaba-1688/sync/orders",
            params={"store_id": "merchant-member"},
            headers=headers,
        )
        wrong_store = client.post(
            "/v1/integrations/alibaba-1688/sync/orders",
            params={"store_id": "store-2"},
            headers=headers,
        )

    assert orders.status_code == 200
    assert orders.json()["applied"] == 1
    assert products.status_code == 200
    assert products.json()["applied"] == 1
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] == 1
    assert wrong_store.status_code == 409

    stored_orders = app.state.agent.operations.orders.list_orders(
        "tenant-test", store_id="merchant-member"
    )
    assert stored_orders[0]["order_id"] == "ORDER-1"
    assert stored_orders[0]["order_status"] == "paid"
    assert stored_orders[0]["lines"][0]["sku_id"] == "SKU-1"
    stored_catalog = app.state.agent.operations.catalog.list_items(
        "tenant-test", store_id="merchant-member"
    )
    assert stored_catalog[0]["sku_id"] == "SKU-1"
    assert stored_catalog[0]["attributes"]["amount_on_sale"] == "7"
    assert app.state.agent.operations.inventory.list_balances(
        "tenant-test", store_id="merchant-member"
    ) == []
    with app.state.agent.db.connect() as conn:
        snapshots = "".join(
            row[0]
            for row in conn.execute(
                "SELECT snapshot_json FROM commerce_order_events"
            ).fetchall()
        )
    assert "sensitive-phone-must-not-persist" not in snapshots
    http_client.close()


def _authorize_1688(service: Alibaba1688IntegrationService, store_id: str) -> None:
    started = service.begin_authorization("tenant-test", store_id)
    service.complete_authorization("authorization-code", started["state"])


def _oauth_token_response(store_id: str = "merchant-member") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "access-current",
            "refresh_token": "refresh-current",
            "expires_in": "36000",
            "memberId": store_id,
        },
    )


def _availability_page(page: int, product_id: str, total: int | None) -> dict:
    page_result = {
        "pageIndex": page,
        "sizePerPage": 1,
        "resultList": [
            {
                "productID": product_id,
                "subject": "测试可售商品",
                "status": "published",
                "lastUpdateTime": "20260903120000000+0800",
                "saleInfo": {"amountOnSale": "10"},
            }
        ],
    }
    if total is not None:
        page_result["totalRecords"] = total
    return {"success": True, "result": {"pageResult": page_result}}


def _checkpoint_row(db: Database, store_id: str, window_kind: str = "full"):
    with db.connect() as conn:
        return conn.execute(
            """
            SELECT status, cursor, window_kind, window_start, window_end,
                   store_id, resource, upstream_total, pages_completed,
                   watermark, watermark_kind, last_error_kind, lease_owner
            FROM connector_sync_checkpoints
            WHERE tenant_id='tenant-test' AND store_id=? AND window_kind=?
            """,
            (store_id, window_kind),
        ).fetchone()


def test_1688_availability_full_run_pages_until_upstream_total_matches(
    tmp_path,
) -> None:
    settings = configured_settings(tmp_path)
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return _oauth_token_response()
        form = parse_qs(request.content.decode("utf-8"))
        page = int((form.get("pageNo") or ["1"])[0])
        pages.append(page)
        product_id = "PRODUCT-1" if page == 1 else "PRODUCT-2"
        return httpx.Response(200, json=_availability_page(page, product_id, 2))

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    _authorize_1688(service, "merchant-member")

    result = service.sync_availability(
        "tenant-test", store_id="merchant-member", limit=1
    )
    checkpoint = _checkpoint_row(db, "merchant-member")

    assert pages == [1, 2]
    assert result["received"] == 2
    assert result["applied"] == 2
    assert result["has_more"] is False
    assert result["recon"]["status"] == "succeeded"
    assert result["recon"]["local_product_count"] == 2
    assert result["checkpoint"]["status"] == "complete"
    assert checkpoint is not None
    assert dict(checkpoint)["status"] == "complete"
    assert dict(checkpoint)["resource"] == "channel_availability"
    assert service.channel_availability.count_snapshots(
        "tenant-test",
        connector_id="alibaba-1688",
        store_id="merchant-member",
    ) == 2
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM inventory_balances WHERE store_id='merchant-member'"
        ).fetchone()[0] == 0
    http_client.close()


def test_1688_availability_failed_page_keeps_cursor_and_resumes(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    page_two_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_two_calls
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return _oauth_token_response()
        form = parse_qs(request.content.decode("utf-8"))
        page = int((form.get("pageNo") or ["1"])[0])
        if page == 1:
            return httpx.Response(200, json=_availability_page(1, "PRODUCT-1", 2))
        page_two_calls += 1
        if page_two_calls <= 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=_availability_page(2, "PRODUCT-2", 2))

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    _authorize_1688(service, "merchant-member")

    failed = service.sync_availability(
        "tenant-test", store_id="merchant-member", limit=1
    )
    failed_row = dict(_checkpoint_row(db, "merchant-member"))
    assert failed["checkpoint"]["status"] == "failed"
    assert failed_row["status"] == "failed"
    assert failed_row["cursor"] == "2"
    assert failed_row["pages_completed"] == 1
    assert service.channel_availability.count_snapshots(
        "tenant-test",
        connector_id="alibaba-1688",
        store_id="merchant-member",
    ) == 1

    resumed = service.sync_availability(
        "tenant-test", store_id="merchant-member", limit=1
    )
    resumed_row = dict(_checkpoint_row(db, "merchant-member"))
    assert page_two_calls == 4
    assert resumed["checkpoint"]["status"] == "complete"
    assert resumed["recon"]["status"] == "succeeded"
    assert resumed_row["status"] == "complete"
    assert service.channel_availability.count_snapshots(
        "tenant-test",
        connector_id="alibaba-1688",
        store_id="merchant-member",
    ) == 2
    http_client.close()


def test_1688_availability_incremental_502_does_not_store_window_total(
    tmp_path,
) -> None:
    settings = configured_settings(tmp_path)
    page_two_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_two_calls
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return _oauth_token_response()
        form = parse_qs(request.content.decode("utf-8"))
        page = int((form.get("pageNo") or ["1"])[0])
        assert form.get("startModifyTime") == ["20260903120000000+0800"]
        if page == 1:
            return httpx.Response(200, json=_availability_page(1, "PRODUCT-1", 2))
        page_two_calls += 1
        return httpx.Response(502, text="bad gateway")

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    _authorize_1688(service, "merchant-member")

    failed = service.sync_availability(
        "tenant-test",
        store_id="merchant-member",
        limit=1,
        modify_start_time="20260903120000000+0800",
        modify_end_time="20260903130000000+0800",
    )
    row = dict(_checkpoint_row(db, "merchant-member", "incremental"))
    assert page_two_calls == 3
    assert failed["checkpoint"]["status"] == "failed"
    assert failed["upstream_total"] is None
    assert failed["recon"]["upstream_total"] is None
    assert failed["recon"]["code"] == "http_502"
    assert row["status"] == "failed"
    assert row["cursor"] == "2"
    assert row["pages_completed"] == 1
    assert row["window_kind"] == "incremental"
    assert row["upstream_total"] is None
    assert row["last_error_kind"] == "http_502"
    http_client.close()


def test_1688_availability_store_error_after_acquire_marks_checkpoint_failed(
    tmp_path,
) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return _oauth_token_response()
        return httpx.Response(200, json=_availability_page(1, "PRODUCT-1", 1))

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    _authorize_1688(service, "merchant-member")

    with pytest.raises(Alibaba1688Error, match="no authorized 1688 connection"):
        service.sync_availability("tenant-test", store_id="missing-store")
    full_row = dict(_checkpoint_row(db, "missing-store", "full"))
    assert full_row["status"] == "failed"
    assert full_row["cursor"] in {None, ""}
    assert full_row["lease_owner"] is None
    assert full_row["last_error_kind"] == "store_unavailable"

    with pytest.raises(Alibaba1688Error, match="no authorized 1688 connection"):
        service.sync_availability("tenant-test", store_id="missing-store")
    with pytest.raises(Alibaba1688Error, match="no authorized 1688 connection"):
        service.sync_availability(
            "tenant-test",
            store_id="missing-store",
            modify_start_time="20260903120000000+0800",
            modify_end_time="20260903130000000+0800",
        )
    incremental_row = dict(_checkpoint_row(db, "missing-store", "incremental"))
    assert incremental_row["status"] == "failed"
    assert incremental_row["cursor"] in {None, ""}
    assert incremental_row["lease_owner"] is None
    assert incremental_row["last_error_kind"] == "store_unavailable"
    http_client.close()


def test_1688_availability_invalid_cursor_does_not_leave_running_checkpoint(
    tmp_path,
) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return _oauth_token_response("merchant-member")
        return httpx.Response(200, json=_availability_page(1, "PRODUCT-1", 1))

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    _authorize_1688(service, "merchant-member")
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_connections(
                id, tenant_id, platform, shop_id, status, account_id,
                credential_ciphertext, token_expires_at, metadata_json,
                created_at, updated_at
            )
            SELECT 'connection-probe', tenant_id, platform, 'probe-store', status,
                   'probe-store', credential_ciphertext, token_expires_at,
                   metadata_json, created_at, updated_at
            FROM platform_connections
            WHERE shop_id='merchant-member'
            """
        )

    with pytest.raises(Alibaba1688Error, match="cursor must be a positive page number"):
        service.sync_availability(
            "tenant-test", store_id="probe-store", cursor="not-a-page"
        )
    row = _checkpoint_row(db, "probe-store", "full")
    assert row is not None
    probe = dict(row)
    assert probe["status"] == "failed"
    assert probe["cursor"] != "not-a-page"
    assert probe["cursor"] in {None, ""}
    assert probe["lease_owner"] is None
    assert probe["last_error_kind"] == "cursor_invalid"

    resumed = service.sync_availability("tenant-test", store_id="probe-store")
    assert resumed["checkpoint"]["status"] == "complete"
    assert resumed["checkpoint"]["last_error_kind"] is None

    app = create_app(settings)
    app.state.agent.alibaba_1688.client = Alibaba1688Client(
        settings, client=http_client
    )
    headers = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
    with TestClient(app) as client:
        response = client.post(
            "/v1/integrations/alibaba-1688/sync/availability",
            params={"store_id": "probe-store", "cursor": "not-a-page"},
            headers=headers,
        )
    assert response.status_code == 409
    assert "cursor must be a positive page number" in response.json()["detail"]
    api_row = dict(_checkpoint_row(db, "probe-store", "full"))
    assert api_row["status"] == "failed"
    assert api_row["cursor"] != "not-a-page"
    assert api_row["cursor"] in {None, ""}
    assert api_row["lease_owner"] is None
    assert api_row["last_error_kind"] == "cursor_invalid"
    http_client.close()


def test_1688_availability_non_cursor_error_after_page_keeps_resume_cursor(
    tmp_path, monkeypatch
) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return _oauth_token_response()
        form = parse_qs(request.content.decode("utf-8"))
        page = int((form.get("pageNo") or ["1"])[0])
        return httpx.Response(200, json=_availability_page(page, f"PRODUCT-{page}", 2))

    real_list = Alibaba1688Connector.list_products

    def list_products(self, request, **kwargs):
        if str(request.cursor or "") == "2":
            raise Alibaba1688Error("1688 access token is required")
        return real_list(self, request, **kwargs)

    monkeypatch.setattr(Alibaba1688Connector, "list_products", list_products)

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    _authorize_1688(service, "merchant-member")

    with pytest.raises(Alibaba1688Error, match="access token is required"):
        service.sync_availability(
            "tenant-test", store_id="merchant-member", limit=1
        )
    row = dict(_checkpoint_row(db, "merchant-member"))
    assert row["status"] == "failed"
    assert row["cursor"] == "2"
    assert row["lease_owner"] is None
    assert row["last_error_kind"] != "cursor_invalid"
    assert row["last_error_kind"] == "request_invalid"

    with pytest.raises(Alibaba1688Error, match="access token is required"):
        service.sync_availability(
            "tenant-test", store_id="merchant-member", limit=1
        )
    resumed_row = dict(_checkpoint_row(db, "merchant-member"))
    assert resumed_row["status"] == "failed"
    assert resumed_row["cursor"] == "2"
    assert resumed_row["lease_owner"] is None
    assert resumed_row["last_error_kind"] == "request_invalid"
    http_client.close()


def test_1688_availability_checkpoint_isolates_store_and_window(tmp_path) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return _oauth_token_response("merchant-a")
        form = parse_qs(request.content.decode("utf-8"))
        page = int((form.get("pageNo") or ["1"])[0])
        if form.get("startModifyTime"):
            return httpx.Response(200, json=_availability_page(1, "PRODUCT-WINDOW", 1))
        product_id = "PRODUCT-A1" if page == 1 else "PRODUCT-A2"
        return httpx.Response(200, json=_availability_page(page, product_id, 2))

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    _authorize_1688(service, "merchant-a")
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_connections(
                id, tenant_id, platform, shop_id, status, account_id,
                credential_ciphertext, token_expires_at, metadata_json,
                created_at, updated_at
            )
            SELECT 'connection-b', tenant_id, platform, 'merchant-b', status,
                   'merchant-b', credential_ciphertext, token_expires_at,
                   metadata_json, created_at, updated_at
            FROM platform_connections
            WHERE shop_id='merchant-a'
            """
        )

    full_a = service.sync_availability(
        "tenant-test", store_id="merchant-a", limit=1
    )
    incremental = service.sync_availability(
        "tenant-test",
        store_id="merchant-a",
        limit=1,
        modify_start_time="20260903120000000+0800",
        modify_end_time="20260903130000000+0800",
    )
    full_b = service.sync_availability(
        "tenant-test", store_id="merchant-b", limit=1
    )

    row_a = dict(_checkpoint_row(db, "merchant-a", "full"))
    row_window = dict(_checkpoint_row(db, "merchant-a", "incremental"))
    row_b = dict(_checkpoint_row(db, "merchant-b", "full"))
    assert full_a["checkpoint"]["status"] == "complete"
    assert incremental["checkpoint"]["status"] == "complete"
    assert incremental["has_more"] is False
    assert full_b["checkpoint"]["status"] == "complete"
    assert row_a["store_id"] == "merchant-a"
    assert row_b["store_id"] == "merchant-b"
    assert row_window["window_kind"] == "incremental"
    assert row_window["window_start"] == "20260903120000000+0800"
    assert row_a["resource"] == "channel_availability"
    assert "merchant-" not in row_a["resource"]
    http_client.close()


def test_1688_availability_full_run_fails_closed_on_total_gaps(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    mode = "missing"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return _oauth_token_response()
        form = parse_qs(request.content.decode("utf-8"))
        page = int((form.get("pageNo") or ["1"])[0])
        if mode == "missing":
            return httpx.Response(
                200, json=_availability_page(page, "PRODUCT-1", None)
            )
        total = 2 if page == 1 else 3
        product_id = "PRODUCT-1" if page == 1 else "PRODUCT-2"
        return httpx.Response(200, json=_availability_page(page, product_id, total))

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    _authorize_1688(service, "merchant-member")

    missing = service.sync_availability(
        "tenant-test", store_id="merchant-member", limit=1
    )
    missing_row = _checkpoint_row(db, "merchant-member")
    assert missing["recon"]["code"] == "upstream_total_missing"
    assert missing["checkpoint"]["status"] == "failed"
    assert missing_row is not None
    assert dict(missing_row)["status"] == "failed"
    assert dict(missing_row)["cursor"] in {None, ""}

    mode = "changed"
    changed = service.sync_availability(
        "tenant-test", store_id="merchant-member", limit=1
    )
    assert changed["recon"]["code"] == "upstream_total_changed"
    assert changed["checkpoint"]["status"] == "failed"
    http_client.close()


def test_1688_availability_recon_fails_when_upstream_total_mismatches(
    tmp_path,
) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system.oauth2/getToken/5043656"):
            return httpx.Response(
                200,
                json={
                    "access_token": "access-current",
                    "refresh_token": "refresh-current",
                    "expires_in": "36000",
                    "memberId": "merchant-member",
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "pageResult": {
                        "pageIndex": 1,
                        "sizePerPage": 1,
                        "totalRecords": 2,
                        "resultList": [
                            {
                                "productID": "PRODUCT-1",
                                "subject": "测试可售商品",
                                "status": "published",
                                "lastUpdateTime": "20260903120000000+0800",
                                "saleInfo": {"amountOnSale": "10"},
                            }
                        ],
                    }
                },
            },
        )

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    started = service.begin_authorization("tenant-test", "merchant-member")
    service.complete_authorization("authorization-code", started["state"])

    result = service.sync_availability("tenant-test", store_id="merchant-member")

    assert result["received"] == 1
    assert result["upstream_total"] == 2
    assert result["recon"]["status"] == "failed"
    assert result["recon"]["code"] == "upstream_total_mismatch"
    assert result["recon"]["local_product_count"] == 1
    assert result["recon"]["upstream_total"] == 2
    http_client.close()


def test_1688_refresh_transport_failure_disables_the_connection(tmp_path) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        if form.get("grant_type") == ["authorization_code"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "access-old",
                    "refresh_token": "refresh-current",
                    "expires_in": "36000",
                    "memberId": "merchant-member",
                },
            )
        raise httpx.ConnectError("offline", request=request)

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    started = service.begin_authorization(
        "tenant-test", "merchant-member"
    )
    service.complete_authorization("authorization-code", started["state"])
    with db.connect() as conn:
        conn.execute(
            "UPDATE platform_connections SET token_expires_at=? "
            "WHERE platform='alibaba_1688'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )

    with pytest.raises(Alibaba1688RemoteError, match="authorization again"):
        service.list_orders("tenant-test", store_id="merchant-member")

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM platform_connections "
            "WHERE platform='alibaba_1688'"
        ).fetchone()
    assert row["status"] == "error"
    service.close()


def test_1688_hosted_callback_binds_member_to_explicit_tenant(tmp_path) -> None:
    settings = configured_hosted_settings(tmp_path, "tenant-hosted")

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        assert request.url.path.endswith("/system.oauth2/getToken/5043656")
        assert form["grant_type"] == ["authorization_code"]
        assert form["code"] == ["hosted-code"]
        return httpx.Response(
            200,
            json={
                "access_token": "hosted-access",
                "refresh_token": "hosted-refresh",
                "expires_in": "36000",
                "memberId": "merchant-member",
            },
        )

    app = create_app(settings)
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    app.state.agent.alibaba_1688.client = Alibaba1688Client(
        settings, client=http_client
    )

    with TestClient(app) as client:
        response = client.get(
            "/v1/integrations/alibaba-1688/oauth/callback",
            params={"code": "hosted-code"},
        )

    assert response.status_code == 200
    assert response.json()["shop_id"] == "merchant-member"
    with app.state.agent.db.connect() as conn:
        saved = conn.execute(
            "SELECT tenant_id, shop_id, status, metadata_json "
            "FROM platform_connections WHERE platform='alibaba_1688'"
        ).fetchone()
    assert saved["tenant_id"] == "tenant-hosted"
    assert saved["shop_id"] == "merchant-member"
    assert saved["status"] == "authorized"
    assert json.loads(saved["metadata_json"])["authorization_mode"] == "hosted"
    assert (
        app.state.agent.alibaba_1688.capabilities("tenant-hosted")[
            "capabilities"
        ]["hosted_authorization"]["available"]
        is True
    )
    assert (
        app.state.agent.alibaba_1688.capabilities("tenant-other")[
            "capabilities"
        ]["hosted_authorization"]["available"]
        is False
    )
    http_client.close()


def test_1688_hosted_callback_rejects_before_exchange_without_tenant(
    tmp_path,
) -> None:
    settings = configured_hosted_settings(tmp_path, "")
    remote_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal remote_called
        remote_called = True
        return httpx.Response(500)

    app = create_app(settings)
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    app.state.agent.alibaba_1688.client = Alibaba1688Client(
        settings, client=http_client
    )

    with TestClient(app) as client:
        response = client.get(
            "/v1/integrations/alibaba-1688/oauth/callback",
            params={"code": "hosted-code"},
        )

    assert response.status_code == 400
    assert "hosted tenant binding is not configured" in response.json()["detail"]
    assert remote_called is False
    http_client.close()


def test_1688_web_authorization_rejects_member_mismatch(tmp_path) -> None:
    settings = configured_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "access-current",
                "refresh_token": "refresh-current",
                "expires_in": "36000",
                "memberId": "merchant-member-b",
            },
        )

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    started = service.begin_authorization(
        "tenant-test", "merchant-member-a"
    )

    with pytest.raises(Alibaba1688Error, match="does not match"):
        service.complete_authorization("authorization-code", started["state"])

    with db.connect() as conn:
        state_row = conn.execute(
            "SELECT used_at FROM platform_oauth_states"
        ).fetchone()
        connection_count = conn.execute(
            "SELECT COUNT(*) FROM platform_connections "
            "WHERE platform='alibaba_1688'"
        ).fetchone()[0]
    assert state_row["used_at"] is None
    assert connection_count == 0
    service.close()


def test_1688_web_state_retries_after_first_token_transport_failure(
    tmp_path,
) -> None:
    settings = configured_settings(tmp_path)
    token_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_attempts
        token_attempts += 1
        if token_attempts == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-current",
                "refresh_token": "refresh-current",
                "expires_in": "36000",
                "memberId": "merchant-member",
            },
        )

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = Alibaba1688IntegrationService(
        db,
        settings,
        top_client=Alibaba1688Client(settings, client=http_client),
    )
    started = service.begin_authorization(
        "tenant-test", "merchant-member"
    )

    with pytest.raises(Alibaba1688RemoteError, match="request failed"):
        service.complete_authorization("authorization-code", started["state"])
    with db.connect() as conn:
        assert conn.execute(
            "SELECT used_at FROM platform_oauth_states"
        ).fetchone()["used_at"] is None

    saved = service.complete_authorization(
        "authorization-code", started["state"]
    )

    assert saved["shop_id"] == "merchant-member"
    assert token_attempts == 2
    with db.connect() as conn:
        assert conn.execute(
            "SELECT used_at FROM platform_oauth_states"
        ).fetchone()["used_at"] is not None
    service.close()
