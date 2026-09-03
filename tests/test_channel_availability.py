from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_settings
from ecommerce_agent.alibaba_1688 import Alibaba1688Client
from ecommerce_agent.api import create_app
from ecommerce_agent.business.channel_availability import (
    ChannelAvailabilityRecordInput,
    ChannelAvailabilityService,
    ChannelAvailabilitySnapshotInput,
)
from ecommerce_agent.business.source_versioning import SourceVersionError
from ecommerce_agent.database import Database


def _settings(tmp_path: Path):
    return replace(
        make_settings(tmp_path),
        alibaba_1688_enabled=True,
        alibaba_1688_app_key="5043656",
        alibaba_1688_app_secret="1688-app-secret",
        alibaba_1688_redirect_uri=(
            "https://example.test/v1/integrations/alibaba-1688/oauth/callback"
        ),
        alibaba_1688_credential_key=(
            base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
        ),
        alibaba_1688_gateway="https://mock.test",
        alibaba_1688_hosted_tenant_id="",
    )


def _snapshot(source_time: datetime, payload_hash: str) -> ChannelAvailabilitySnapshotInput:
    return ChannelAvailabilitySnapshotInput(
        connector_id="alibaba-1688",
        store_id="merchant-store",
        source_product_id="PRODUCT-1",
        source_updated_at=source_time,
        observed_at=source_time + timedelta(minutes=1),
        payload_hash=payload_hash,
        source_id="1688:merchant-store:product:PRODUCT-1",
        unit="件",
        inventory_reduce_type="2",
        records=[
            ChannelAvailabilityRecordInput(
                scope="product", available_qty="10"
            ),
            ChannelAvailabilityRecordInput(
                scope="sku", source_sku_id="SKU-1", available_qty="7"
            ),
        ],
    )


def test_v41_migration_is_registered_and_does_not_rewrite_v40(tmp_path: Path) -> None:
    db = Database(tmp_path / "v41.sqlite3")
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO channel_availability_snapshots(
                snapshot_id, tenant_id, connector_id, store_id,
                source_product_id, semantic_role, source_updated_at,
                observed_at, payload_hash, record_count, version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "snap-v40-keep", "tenant-a", "alibaba-1688", "store-a",
                "PRODUCT-KEEP", "channel_available",
                "2026-09-03T04:00:00+00:00", "2026-09-03T04:00:00+00:00",
                "a" * 64, 1, 1,
                "2026-09-03T04:00:00+00:00", "2026-09-03T04:00:00+00:00",
            ),
        )
        conn.execute("DROP TABLE connector_sync_checkpoints")
        conn.execute("DELETE FROM schema_migrations WHERE version=41")

    db.initialize()

    with db.connect() as conn:
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=41"
        ).fetchone()[0]
        v40_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=40"
        ).fetchone()[0]
        checkpoint_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(connector_sync_checkpoints)")
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        preserved = conn.execute(
            "SELECT source_product_id FROM channel_availability_snapshots "
            "WHERE snapshot_id='snap-v40-keep'"
        ).fetchone()

    assert 40 in versions
    assert 41 in versions
    assert v40_count == 1
    assert migration_count == 1
    assert {
        "tenant_id",
        "connector_id",
        "store_id",
        "resource",
        "window_kind",
        "window_start",
        "window_end",
        "status",
        "cursor",
        "watermark",
        "lease_owner",
        "row_version",
    } <= checkpoint_columns
    assert "uq_connector_sync_checkpoints_identity" in indexes
    assert dict(preserved) == {"source_product_id": "PRODUCT-KEEP"}


def test_v41_checkpoint_expired_lease_can_be_recovered(tmp_path: Path) -> None:
    from ecommerce_agent.business.sync_checkpoint import (
        AVAILABILITY_RESOURCE,
        ConnectorSyncCheckpointService,
        SyncCheckpointConflict,
        WINDOW_FULL,
    )

    db = Database(tmp_path / "v41-lease.sqlite3")
    db.initialize()
    service = ConnectorSyncCheckpointService(db)
    first = service.acquire(
        "tenant-a",
        connector_id="alibaba-1688",
        store_id="store-a",
        resource=AVAILABILITY_RESOURCE,
        window_kind=WINDOW_FULL,
        window_start="",
        window_end="",
        owner="owner-a",
    )
    assert first["lease_owner"] == "owner-a"
    with pytest.raises(SyncCheckpointConflict):
        service.acquire(
            "tenant-a",
            connector_id="alibaba-1688",
            store_id="store-a",
            resource=AVAILABILITY_RESOURCE,
            window_kind=WINDOW_FULL,
            window_start="",
            window_end="",
            owner="owner-b",
        )

    with db.connect() as conn:
        conn.execute(
            """
            UPDATE connector_sync_checkpoints
            SET lease_expires_at='2020-01-01T00:00:00+00:00'
            WHERE checkpoint_id=?
            """,
            (first["checkpoint_id"],),
        )

    recovered = service.acquire(
        "tenant-a",
        connector_id="alibaba-1688",
        store_id="store-a",
        resource=AVAILABILITY_RESOURCE,
        window_kind=WINDOW_FULL,
        window_start="",
        window_end="",
        owner="owner-b",
    )
    assert recovered["lease_owner"] == "owner-b"
    assert recovered["status"] == "running"


def test_v40_migration_is_registered_and_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "v40.sqlite3")
    db.initialize()
    with db.connect() as conn:
        conn.execute("DROP TABLE channel_availability_records")
        conn.execute("DROP TABLE channel_availability_snapshots")
        conn.execute("DELETE FROM schema_migrations WHERE version=40")
    db.initialize()

    with db.connect() as conn:
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=40"
        ).fetchone()[0]
        snapshot_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(channel_availability_snapshots)")
        }
        record_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(channel_availability_records)")
        }

    assert 40 in versions
    assert migration_count == 1
    assert {"source_product_id", "source_updated_at", "payload_hash", "record_count"} <= snapshot_columns
    assert {"scope", "source_sku_id", "available_qty", "payload_hash"} <= record_columns


def test_v39_to_v40_upgrade_preserves_existing_wms_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "v39-upgrade.sqlite3")
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO inventory_balances(
                id, tenant_id, connector_id, store_id, warehouse_id, sku_id,
                item_id, on_hand, reserved, inbound, average_daily_sales,
                source_id, source_updated_at, payload_hash, version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wms-v39", "tenant-a", "wms", "store-a", "warehouse-a", "sku-a",
                None, "12", "2", "3", "1", "wms:row-a",
                "2026-09-03T04:00:00+00:00", "w" * 64, 1,
                "2026-09-03T04:00:00+00:00", "2026-09-03T04:00:00+00:00",
            ),
        )
        conn.execute("DROP TABLE channel_availability_records")
        conn.execute("DROP TABLE channel_availability_snapshots")
        conn.execute("DELETE FROM schema_migrations WHERE version=40")

    db.initialize()

    with db.connect() as conn:
        preserved = conn.execute(
            "SELECT on_hand, reserved, inbound FROM inventory_balances WHERE id='wms-v39'"
        ).fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_availability_snapshots"
        ).fetchone()[0] == 0
    assert dict(preserved) == {"on_hand": "12", "reserved": "2", "inbound": "3"}


def test_channel_availability_rejects_invalid_scope_before_persistence(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "invalid-availability.sqlite3")
    db.initialize()
    service = ChannelAvailabilityService(db)
    source_time = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="sku_scope_requires_sku"):
        service.replace_snapshot(
            "tenant-a",
            ChannelAvailabilitySnapshotInput(
                connector_id="alibaba-1688",
                store_id="merchant-store",
                source_product_id="PRODUCT-1",
                source_updated_at=source_time,
                payload_hash="a" * 64,
                records=[
                    ChannelAvailabilityRecordInput(
                        scope="sku", available_qty="1"
                    )
                ],
            ),
        )

    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_availability_snapshots"
        ).fetchone()[0] == 0


def test_channel_availability_replaces_by_source_version_without_touching_wms(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "availability.sqlite3")
    db.initialize()
    service = ChannelAvailabilityService(db)
    source_time = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)
    first = _snapshot(source_time, "a" * 64)

    applied = service.replace_snapshot("tenant-a", first)
    repeated = service.replace_snapshot("tenant-a", first)
    assert applied["write_status"] == "applied"
    assert repeated["write_status"] == "idempotent"
    assert [row["scope"] for row in repeated["records"]] == ["product", "sku"]
    assert repeated["records"][0]["available_qty"] == "10"

    with pytest.raises(SourceVersionError, match="source_version_conflict"):
        service.replace_snapshot("tenant-a", _snapshot(source_time, "b" * 64))
    with pytest.raises(SourceVersionError, match="stale_source_version"):
        service.replace_snapshot(
            "tenant-a", _snapshot(source_time - timedelta(seconds=1), "c" * 64)
        )

    newer = _snapshot(source_time + timedelta(minutes=1), "d" * 64)
    newer.records[0].available_qty = 3  # type: ignore[misc]
    replaced = service.replace_snapshot("tenant-a", newer)
    assert replaced["write_status"] == "applied"
    assert replaced["version"] == 2
    assert replaced["records"][0]["available_qty"] == "3"

    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_availability_snapshots"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_availability_records"
        ).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM inventory_balances").fetchone()[0] == 0


def test_channel_availability_replacement_removes_skus_from_the_previous_snapshot(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "availability-replacement.sqlite3")
    db.initialize()
    service = ChannelAvailabilityService(db)
    first = _snapshot(datetime(2026, 9, 3, 4, 0, tzinfo=UTC), "a" * 64)
    service.replace_snapshot("tenant-a", first)

    replacement = ChannelAvailabilitySnapshotInput(
        connector_id="alibaba-1688",
        store_id="merchant-store",
        source_product_id="PRODUCT-1",
        source_updated_at=datetime(2026, 9, 3, 4, 1, tzinfo=UTC),
        payload_hash="b" * 64,
        records=[
            ChannelAvailabilityRecordInput(
                scope="sku", source_sku_id="SKU-2", available_qty="8"
            )
        ],
    )
    service.replace_snapshot("tenant-a", replacement)

    rows = service.list_current(
        "tenant-a", connector_id="alibaba-1688", store_id="merchant-store"
    )
    assert [(row["scope"], row["source_sku_id"]) for row in rows] == [
        ("sku", "SKU-2")
    ]


def test_channel_availability_isolated_by_tenant_and_store(tmp_path: Path) -> None:
    db = Database(tmp_path / "availability-isolation.sqlite3")
    db.initialize()
    service = ChannelAvailabilityService(db)
    source_time = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)

    service.replace_snapshot("tenant-a", _snapshot(source_time, "a" * 64))
    other_tenant = _snapshot(source_time, "b" * 64)
    other_tenant.records[0].available_qty = 20  # type: ignore[misc]
    service.replace_snapshot("tenant-b", other_tenant)
    other_store = _snapshot(source_time, "c" * 64).model_copy(
        update={"store_id": "another-store"}
    )
    other_store.records[0].available_qty = 30  # type: ignore[misc]
    service.replace_snapshot("tenant-a", other_store)

    tenant_a_store_a = service.list_current("tenant-a", store_id="merchant-store")
    tenant_b_store_a = service.list_current("tenant-b", store_id="merchant-store")
    tenant_a_store_b = service.list_current("tenant-a", store_id="another-store")
    assert {row["available_qty"] for row in tenant_a_store_a} == {"10", "7"}
    assert {row["available_qty"] for row in tenant_b_store_a} == {"20", "7"}
    assert {row["available_qty"] for row in tenant_a_store_b} == {"30", "7"}
    assert {row["store_id"] for row in tenant_a_store_a} == {"merchant-store"}
    assert {row["store_id"] for row in tenant_b_store_a} == {"merchant-store"}
    assert {row["store_id"] for row in tenant_a_store_b} == {"another-store"}


def test_1688_batch_sync_persists_product_and_sku_scopes_and_rejects_incomplete_sku(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    list_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
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
        assert form["access_token"] == ["access-current"]
        list_calls += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalRecords": 1,
                "result": [
                    {
                        "productID": "PRODUCT-1",
                        "subject": "测试可售商品",
                        "status": "published",
                        "lastUpdateTime": "20260903120000000+0800",
                        "skuInfos": [
                            {"skuId": "SKU-1", "amountOnSale": "7"},
                            {"skuId": "SKU-2", "amountOnSale": "8"},
                        ],
                        "saleInfo": {
                            "unit": "件",
                            "amountOnSale": "10",
                            "invReduceType": "2",
                        },
                    }
                ],
            },
        )

    app = create_app(settings)
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    app.state.agent.alibaba_1688.client = Alibaba1688Client(
        settings, client=http_client
    )
    started = app.state.agent.alibaba_1688.begin_authorization(
        "tenant-test", "merchant-member"
    )
    app.state.agent.alibaba_1688.complete_authorization("authorization-code", started["state"])
    headers = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}

    with TestClient(app) as client:
        synced = client.post(
            "/v1/integrations/alibaba-1688/sync/availability",
            params={"store_id": "merchant-member"},
            headers=headers,
        )
        repeated = client.post(
            "/v1/integrations/alibaba-1688/sync/availability",
            params={"store_id": "merchant-member"},
            headers=headers,
        )
        queried = client.get(
            "/v1/integrations/alibaba-1688/availability",
            params={"store_id": "merchant-member", "product_id": "PRODUCT-1"},
            headers=headers,
        )
        capabilities = client.get(
            "/v1/integrations/alibaba-1688/capabilities", headers=headers
        )
        detail = client.get(
            "/v1/integrations/alibaba-1688/availability/PRODUCT-1",
            params={"store_id": "merchant-member"},
            headers=headers,
        )

    assert list_calls == 2
    assert synced.status_code == 200
    assert synced.json()["received"] == 1
    assert synced.json()["mapped"] == 3
    assert synced.json()["applied"] == 1
    assert synced.json()["upstream_total"] == 1
    assert synced.json()["recon"] == {
        "status": "succeeded",
        "code": "matched",
        "local_product_count": 1,
        "upstream_total": 1,
    }
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] == 1
    assert queried.status_code == 200
    records = queried.json()["records"]
    assert {row["scope"] for row in records} == {"product", "sku"}
    assert {row["source_sku_id"] for row in records} == {None, "SKU-1", "SKU-2"}
    assert {row["source_updated_at"] for row in records} == {
        "2026-09-03T04:00:00+00:00"
    }
    assert detail.status_code == 200
    assert detail.json()["record_count"] == 3
    assert detail.json()["source_updated_at"] == "2026-09-03T04:00:00+00:00"
    assert capabilities.json()["capabilities"]["channel_availability_read"][
        "persistence"
    ] is True
    assert not {
        "on_hand", "reserved", "inbound", "warehouse_id"
    } & queried.json().keys()

    http_client.close()
