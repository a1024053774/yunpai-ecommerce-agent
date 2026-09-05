from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from .business.catalog import CatalogItemUpsert
from .business.channel_availability import (
    AvailabilityScope,
    ChannelAvailabilityRecordInput,
    ChannelAvailabilityService,
    ChannelAvailabilitySnapshotInput,
)
from .business.sync_checkpoint import (
    AVAILABILITY_RESOURCE,
    PAGE_RETRY_LIMIT,
    WATERMARK_EXHAUSTED,
    WATERMARK_SOURCE_TIME,
    WINDOW_FULL,
    ConnectorSyncCheckpointService,
    SyncCheckpointConflict,
    availability_window,
    public_checkpoint_view,
)
from .business.orders import OrderLineInput, OrderUpsert
from .business.service import OperationsService
from .business.source_versioning import SourceVersionError
from .config import Settings
from .connectors import (
    ConnectionCheck,
    ConnectorCapabilities,
    ExternalAction,
    ExternalResult,
    PullBatch,
    PullRecord,
    PullRequest,
    VerificationResult,
    VerifiedEvent,
)
from .database import Database
from .taobao import CredentialCipher, TaobaoError


class Alibaba1688Error(ValueError):
    pass


class Alibaba1688RemoteError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _as_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def sign_1688_request(
    url_path: str, params: Mapping[str, Any], app_secret: str
) -> str:
    """Return the official 1688 param2 HMAC-SHA1 signature."""

    if not app_secret:
        raise Alibaba1688Error("1688 app secret is required for signing")
    normalized_path = url_path.lstrip("/")
    canonical = normalized_path + "".join(
        f"{key}{_as_string(value)}"
        for key, value in sorted(params.items())
        if key != "_aop_signature" and value is not None
    )
    return hmac.new(
        app_secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest().upper()


def _data_as_of() -> str:
    return datetime.now(UTC).isoformat()


def _is_http_502(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, httpx.HTTPStatusError):
            return current.response.status_code == 502
        current = current.__cause__
    return False


def _platform_datetime(value: Any) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        parsed = None
        for parser in (
            lambda: datetime.strptime(text, "%Y%m%d%H%M%S%f%z"),
            lambda: datetime.strptime(text, "%Y%m%d%H%M%S%z"),
            lambda: datetime.fromisoformat(text),
            lambda: datetime.strptime(text, "%Y-%m-%d %H:%M:%S"),
        ):
            try:
                parsed = parser()
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(UTC)


def _payload_version(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"payload-sha256:{hashlib.sha256(encoded).hexdigest()}"


def _stable_source_datetime(value: Any) -> datetime:
    parsed = _platform_datetime(value)
    if parsed is None:
        raise Alibaba1688RemoteError(
            "1688 channel availability requires a stable source timestamp"
        )
    return parsed


def _channel_quantity(value: Any, field: str) -> str:
    if value is None or str(value).strip() == "":
        raise Alibaba1688RemoteError(f"1688 {field} is required")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise Alibaba1688RemoteError(f"1688 {field} is invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise Alibaba1688RemoteError(f"1688 {field} is invalid")
    return format(parsed, "f")


def _page(cursor: str | None) -> int:
    if cursor in {None, ""}:
        return 1
    try:
        value = int(cursor)
    except ValueError as exc:
        raise Alibaba1688Error("1688 cursor must be a positive page number") from exc
    if value < 1:
        raise Alibaba1688Error("1688 cursor must be a positive page number")
    return value


def _window(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    parsed_start = _platform_datetime(start) if start else None
    parsed_end = _platform_datetime(end) if end else None
    if start and parsed_start is None:
        raise Alibaba1688Error("1688 start time has an unsupported format")
    if end and parsed_end is None:
        raise Alibaba1688Error("1688 end time has an unsupported format")
    if parsed_start and parsed_end and parsed_start > parsed_end:
        raise Alibaba1688Error("1688 start time must not be after end time")
    return start, end


def _list_result(payload: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    result = payload.get("result")
    if isinstance(result, list):
        return [dict(item) for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        for key in keys:
            nested = result.get(key)
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, dict)]
        page_result = result.get("pageResult")
        if isinstance(page_result, dict):
            nested = page_result.get("resultList")
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, dict)]
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    if result is None:
        return []
    raise Alibaba1688RemoteError("1688 returned an invalid list payload")


def _official_total(payload: Mapping[str, Any]) -> int | None:
    containers: list[Mapping[str, Any]] = [payload]
    result = payload.get("result")
    if isinstance(result, dict):
        containers.append(result)
        page_result = result.get("pageResult")
        if isinstance(page_result, dict):
            containers.append(page_result)
    for key in ("totalRecord", "totalRecords", "total"):
        for container in containers:
            value = container.get(key)
            if value is None:
                continue
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
    return None


def _total(payload: Mapping[str, Any], fallback: int) -> int:
    official = _official_total(payload)
    return fallback if official is None else official


def _sanitize_price_ranges(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        row = {
            key: item[key]
            for key in ("startQuantity", "price")
            if key in item
        }
        if row:
            sanitized.append(row)
    return sanitized


def _sanitize_order(value: Mapping[str, Any]) -> dict[str, Any]:
    base = value.get("baseInfo")
    base_info = dict(base) if isinstance(base, dict) else {}
    allowed_base = {
        "id",
        "idOfStr",
        "status",
        "statusStr",
        "createTime",
        "modifyTime",
        "payTime",
        "allDeliveredTime",
        "totalAmount",
        "sumProductPayment",
        "shippingFee",
        "discount",
        "refund",
        "businessType",
        "tradeType",
    }
    sanitized_items: list[dict[str, Any]] = []
    product_items = value.get("productItems")
    if isinstance(product_items, list):
        allowed_item = {
            "subItemID",
            "productID",
            "skuID",
            "skuId",
            "name",
            "price",
            "quantity",
            "itemAmount",
            "status",
            "productImgUrl",
            "productSnapshotUrl",
        }
        sanitized_items = [
            {key: item[key] for key in allowed_item if key in item}
            for item in product_items
            if isinstance(item, dict)
        ]
    return {
        "baseInfo": {key: base_info[key] for key in allowed_base if key in base_info},
        "productItems": sanitized_items,
    }


def _sanitize_product(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed_product = {
        "productID",
        "productType",
        "categoryID",
        "status",
        "subject",
        "language",
        "periodOfValidity",
        "bizType",
        "createTime",
        "lastUpdateTime",
        "lastRepostTime",
        "approvedTime",
        "expireTime",
    }
    sanitized = {key: value[key] for key in allowed_product if key in value}
    sku_infos = value.get("skuInfos")
    if isinstance(sku_infos, list):
        allowed_sku = {
            "skuId",
            "skuID",
            "skuCode",
            "specId",
            "cargoNumber",
            "amountOnSale",
            "price",
            "retailPrice",
            "consignPrice",
            "takeSamplePrice",
        }
        sanitized_skus: list[dict[str, Any]] = []
        for sku in sku_infos:
            if not isinstance(sku, dict):
                continue
            sanitized_sku = {key: sku[key] for key in allowed_sku if key in sku}
            price_range = _sanitize_price_ranges(sku.get("priceRange"))
            if price_range:
                sanitized_sku["priceRange"] = price_range
            sanitized_skus.append(sanitized_sku)
        sanitized["skuInfos"] = sanitized_skus
    sale_info = value.get("saleInfo")
    if isinstance(sale_info, dict):
        allowed_sale = {
            "supportOnlineTrade",
            "mixWholeSale",
            "saleType",
            "unit",
            "minOrderQuantity",
            "amountOnSale",
            "invReduceType",
            "deliveryLimit",
            "quoteType",
            "priceRanges",
        }
        sanitized_sale = {
            key: sale_info[key] for key in allowed_sale if key in sale_info
        }
        price_ranges = _sanitize_price_ranges(sale_info.get("priceRanges"))
        if price_ranges:
            sanitized_sale["priceRanges"] = price_ranges
        sanitized["saleInfo"] = sanitized_sale
    return sanitized


class Alibaba1688Client:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._injected_client = client
        self._client: httpx.Client | None = None
        self._owns_client = client is None

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = self._injected_client or httpx.Client(
                timeout=20.0, trust_env=False
            )
        return self._client

    @property
    def token_url(self) -> str:
        return (
            f"{self.settings.alibaba_1688_gateway}/openapi/http/1/"
            f"system.oauth2/getToken/{self.settings.alibaba_1688_app_key}"
        )

    def exchange_authorization_code(self, code: str) -> dict[str, Any]:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "need_refresh_token": True,
                "client_id": self.settings.alibaba_1688_app_key,
                "client_secret": self.settings.alibaba_1688_app_secret,
                "redirect_uri": self.settings.alibaba_1688_redirect_uri,
                "code": code,
            }
        )

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.settings.alibaba_1688_app_key,
                "client_secret": self.settings.alibaba_1688_app_secret,
                "refresh_token": refresh_token,
            }
        )

    def _token_request(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(self.token_url, values)

    def call(
        self,
        namespace: str,
        method: str,
        business_params: Mapping[str, Any],
        *,
        access_token: str,
    ) -> dict[str, Any]:
        if not access_token:
            raise Alibaba1688Error("1688 access token is required")
        url_path = (
            f"param2/1/{namespace}/{method}/"
            f"{self.settings.alibaba_1688_app_key}"
        )
        values = {
            key: _as_string(value)
            for key, value in business_params.items()
            if value is not None
        }
        values["access_token"] = access_token
        values["_aop_timestamp"] = str(int(datetime.now(UTC).timestamp() * 1000))
        values["_aop_signature"] = sign_1688_request(
            url_path, values, self.settings.alibaba_1688_app_secret
        )
        return self._post(
            f"{self.settings.alibaba_1688_gateway}/openapi/{url_path}", values
        )

    def _post(self, url: str, values: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._ensure_client().post(
                url,
                data={key: _as_string(value) for key, value in values.items()},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise Alibaba1688RemoteError(
                "1688 request failed", code=type(exc).__name__
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise Alibaba1688RemoteError("1688 returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise Alibaba1688RemoteError("1688 returned an invalid response")
        error_code = payload.get("error_code") or payload.get("errorCode")
        if error_code not in {None, "", 0, "0"}:
            raise Alibaba1688RemoteError(
                "1688 request was rejected", code=str(error_code)
            )
        if str(payload.get("success", "true")).lower() == "false":
            raise Alibaba1688RemoteError(
                "1688 request was rejected",
                code=str(payload.get("code") or "request_rejected"),
            )
        result = payload.get("result")
        if isinstance(result, dict) and str(
            result.get("success", "true")
        ).lower() == "false":
            raise Alibaba1688RemoteError(
                "1688 request was rejected",
                code=str(
                    result.get("errorCode")
                    or result.get("code")
                    or "request_rejected"
                ),
            )
        return payload

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


class Alibaba1688Connector:
    CONNECTOR_ID = "alibaba-1688"
    ORDER_NAMESPACE = "com.alibaba.trade"
    ORDER_LIST = "alibaba.trade.ec.getOrderList.sellerView"
    ORDER_DETAIL = "alibaba.trade.ec.getOrder.sellerView"
    PRODUCT_NAMESPACE = "com.alibaba.product"
    PRODUCT_LIST = "alibaba.product.list.get"
    PRODUCT_DETAIL = "alibaba.product.get"

    def __init__(
        self, client: Alibaba1688Client, *, access_token: str, store_id: str
    ):
        self.client = client
        self.access_token = access_token
        self.store_id = store_id

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.CONNECTOR_ID,
            display_name="1688 Open Platform (read-only)",
            capability_version="1688-read-v1",
            resources=["orders", "catalog"],
            modes=["read", "polling"],
            supports_idempotency=True,
            data_classification="merchant-business",
            required_permissions=[
                self.ORDER_LIST,
                self.ORDER_DETAIL,
                self.PRODUCT_LIST,
                self.PRODUCT_DETAIL,
            ],
        )

    def test_connection(self) -> ConnectionCheck:
        return ConnectionCheck(
            ok=bool(self.access_token),
            connector_id=self.CONNECTOR_ID,
            mode="live",
            detail="authorized credential loaded; no live API probe performed",
        )

    def pull(self, request: PullRequest) -> PullBatch:
        if request.resource == "orders":
            return self.list_orders(request)
        if request.resource == "catalog":
            return self.list_products(request)
        raise Alibaba1688Error(f"unsupported 1688 resource: {request.resource}")

    def list_orders(
        self,
        request: PullRequest,
        *,
        modify_start_time: str | None = None,
        modify_end_time: str | None = None,
    ) -> PullBatch:
        start, end = _window(modify_start_time, modify_end_time)
        current_page = _page(request.cursor)
        limit = min(20, request.limit)
        payload = self.client.call(
            self.ORDER_NAMESPACE,
            self.ORDER_LIST,
            {
                "page": current_page,
                "pageSize": limit,
                "modifyStartTime": start,
                "modifyEndTime": end,
            },
            access_token=self.access_token,
        )
        observed_at = _data_as_of()
        values = _list_result(payload, "toReturn", "orders")
        records: list[PullRecord] = []
        for raw in values:
            value = _sanitize_order(raw)
            base = value["baseInfo"]
            order_id = str(base.get("idOfStr") or base.get("id") or "").strip()
            if not order_id:
                continue
            source_time = _platform_datetime(
                base.get("modifyTime") or base.get("createTime")
            )
            occurred_at = source_time.isoformat() if source_time else observed_at
            records.append(
                PullRecord(
                    source_id=f"1688:{self.store_id}:order:{order_id}",
                    source_version=occurred_at if source_time else _payload_version(value),
                    occurred_at=occurred_at,
                    payload=value,
                )
            )
        total = _total(payload, len(records))
        has_more = current_page * limit < total
        return PullBatch(
            connector_id=self.CONNECTOR_ID,
            resource="orders",
            records=records,
            next_cursor=str(current_page + 1) if has_more else None,
            has_more=has_more,
            data_as_of=observed_at,
        )

    def list_products(
        self,
        request: PullRequest,
        *,
        modify_start_time: str | None = None,
        modify_end_time: str | None = None,
    ) -> PullBatch:
        start, end = _window(modify_start_time, modify_end_time)
        current_page = _page(request.cursor)
        limit = min(20, request.limit)
        payload = self.client.call(
            self.PRODUCT_NAMESPACE,
            self.PRODUCT_LIST,
            {
                "pageNo": current_page,
                "pageSize": limit,
                "startModifyTime": start,
                "endModifyTime": end,
                "needDetail": True,
                "orderByCondition": "MODIFY_DATE" if start or end else "ID",
                "orderByType": "ASC",
            },
            access_token=self.access_token,
        )
        observed_at = _data_as_of()
        values = _list_result(payload, "productInfos", "products")
        records: list[PullRecord] = []
        for raw in values:
            value = _sanitize_product(raw)
            product_id = str(value.get("productID") or "").strip()
            if not product_id:
                continue
            source_time = _platform_datetime(
                value.get("lastUpdateTime") or value.get("createTime")
            )
            occurred_at = source_time.isoformat() if source_time else observed_at
            records.append(
                PullRecord(
                    source_id=f"1688:{self.store_id}:product:{product_id}",
                    source_version=occurred_at if source_time else _payload_version(value),
                    occurred_at=occurred_at,
                    payload=value,
                )
            )
        official_total = _official_total(payload)
        total = len(records) if official_total is None else official_total
        has_more = current_page * limit < total
        return PullBatch(
            connector_id=self.CONNECTOR_ID,
            resource="catalog",
            records=records,
            next_cursor=str(current_page + 1) if has_more else None,
            has_more=has_more,
            data_as_of=observed_at,
            upstream_total=official_total,
        )

    def get_order(self, order_id: str) -> PullRecord:
        requested_id = str(order_id).strip()
        if not requested_id:
            raise Alibaba1688Error("1688 order id is required")
        payload = self.client.call(
            self.ORDER_NAMESPACE,
            self.ORDER_DETAIL,
            {"orderId": requested_id},
            access_token=self.access_token,
        )
        raw = payload.get("result")
        if not isinstance(raw, dict):
            raise Alibaba1688RemoteError(
                "1688 returned an invalid order detail payload"
            )
        value = _sanitize_order(raw)
        base = value["baseInfo"]
        returned_id = str(base.get("idOfStr") or base.get("id") or "").strip()
        if returned_id != requested_id:
            raise Alibaba1688RemoteError(
                "1688 order detail did not match the requested order"
            )
        observed_at = _data_as_of()
        source_time = _platform_datetime(
            base.get("modifyTime") or base.get("createTime")
        )
        occurred_at = source_time.isoformat() if source_time else observed_at
        return PullRecord(
            source_id=f"1688:{self.store_id}:order:{returned_id}",
            source_version=(
                occurred_at if source_time else _payload_version(value)
            ),
            occurred_at=occurred_at,
            payload=value,
        )

    def get_product(self, product_id: str) -> PullRecord:
        requested_id = str(product_id).strip()
        if not requested_id:
            raise Alibaba1688Error("1688 product id is required")
        payload = self.client.call(
            self.PRODUCT_NAMESPACE,
            self.PRODUCT_DETAIL,
            {"productID": requested_id},
            access_token=self.access_token,
        )
        raw = payload.get("productInfo")
        if not isinstance(raw, dict):
            raise Alibaba1688RemoteError(
                "1688 returned an invalid product detail payload"
            )
        value = _sanitize_product(raw)
        returned_id = str(value.get("productID") or "").strip()
        if returned_id != requested_id:
            raise Alibaba1688RemoteError(
                "1688 product detail did not match the requested product"
            )
        observed_at = _data_as_of()
        source_time = _platform_datetime(
            value.get("lastUpdateTime") or value.get("createTime")
        )
        occurred_at = source_time.isoformat() if source_time else observed_at
        return PullRecord(
            source_id=f"1688:{self.store_id}:product:{returned_id}",
            source_version=(
                occurred_at if source_time else _payload_version(value)
            ),
            occurred_at=occurred_at,
            payload=value,
        )

    def get_product_availability(self, product_id: str) -> dict[str, Any]:
        record = self.get_product(product_id)
        return self.availability_from_record(record)

    def availability_from_record(self, record: PullRecord) -> dict[str, Any]:
        """Project one sanitized product record into channel-available facts."""

        value = record.payload
        product_id = str(value.get("productID") or "").strip()
        if not product_id:
            raise Alibaba1688RemoteError(
                "1688 product availability did not contain productID"
            )
        source_updated_at = _stable_source_datetime(record.source_version)
        sale_info = value.get("saleInfo")
        if sale_info is not None and not isinstance(sale_info, dict):
            raise Alibaba1688RemoteError(
                "1688 product availability returned an invalid saleInfo value"
            )
        sale = sale_info if isinstance(sale_info, dict) else {}
        rows: list[dict[str, Any]] = []
        product_quantity = sale.get("amountOnSale")
        if product_quantity is not None and str(product_quantity).strip() != "":
            rows.append(
                {
                    "semantic_role": "channel_available",
                    "scope": "product",
                    "source_sku_id": None,
                    "warehouse_code": None,
                    "available_qty": _channel_quantity(
                        product_quantity, "product amountOnSale"
                    ),
                }
            )
        sku_infos = value.get("skuInfos")
        if sku_infos is not None and not isinstance(sku_infos, list):
            raise Alibaba1688RemoteError(
                "1688 product availability returned an invalid skuInfos value"
            )
        skus = sku_infos if isinstance(sku_infos, list) else []
        seen_sku_ids: set[str] = set()
        for sku in skus:
            if not isinstance(sku, dict):
                raise Alibaba1688RemoteError(
                    "1688 product availability returned an invalid SKU"
                )
            sku_id = str(sku.get("skuId") or sku.get("skuID") or "").strip()
            if not sku_id:
                raise Alibaba1688RemoteError(
                    "1688 product availability returned a SKU without an id"
                )
            if sku_id in seen_sku_ids:
                raise Alibaba1688RemoteError(
                    f"1688 product availability returned duplicate SKU {sku_id}"
                )
            seen_sku_ids.add(sku_id)
            rows.append(
                {
                    "semantic_role": "channel_available",
                    "scope": "sku",
                    "source_sku_id": sku_id,
                    "warehouse_code": None,
                    "available_qty": _channel_quantity(
                        sku.get("amountOnSale"),
                        f"SKU {sku_id} amountOnSale",
                    ),
                }
            )
        if not rows:
            raise Alibaba1688RemoteError(
                "1688 product availability did not contain amountOnSale"
            )
        observed_at = _data_as_of()
        return {
            "connector_id": self.CONNECTOR_ID,
            "resource": "channel_availability",
            "semantic_role": "channel_available",
            "store_id": self.store_id,
            "source_product_id": product_id,
            "unit": sale.get("unit"),
            "inventory_reduce_type": sale.get("invReduceType"),
            "source_id": record.source_id,
            "source_version": record.source_version,
            "source_updated_at": source_updated_at.isoformat(),
            "observed_at": observed_at,
            "source_payload_hash": _payload_version(value).removeprefix(
                "payload-sha256:"
            ),
            "records": rows,
        }

    def verify_webhook(
        self, headers: dict[str, str], body: bytes
    ) -> VerifiedEvent:
        raise Alibaba1688Error("1688 webhook ingestion is not implemented")

    def execute(self, action: ExternalAction) -> ExternalResult:
        raise Alibaba1688Error("1688 connector is read-only")

    def verify(
        self, action: ExternalAction, result: ExternalResult
    ) -> VerificationResult:
        raise Alibaba1688Error("1688 write verification is not implemented")


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or str(value).strip() == "":
        raise Alibaba1688Error(f"1688 {field} is required")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise Alibaba1688Error(f"1688 {field} is invalid") from exc


def _integer(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise Alibaba1688Error(f"1688 {field} is invalid") from exc
    if parsed < 1:
        raise Alibaba1688Error(f"1688 {field} must be positive")
    return parsed


def _order_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"cancel", "canceled", "closed"}:
        return "canceled"
    if status in {"success", "confirm_goods", "signinsuccess"}:
        return "delivered"
    if status in {
        "waitbuyerreceive",
        "waitlogisticstakein",
        "waitbuyersign",
        "send_goods_but_not_fund",
    }:
        return "shipped"
    if status in {"waitbuyerpay", "waitsellerconfirm", "waitbuyerconfirm"}:
        return "created"
    if status == "waitsellersend":
        return "paid"
    return "fulfilling"


def _payment_status(base: Mapping[str, Any], total: Decimal) -> str:
    status = str(base.get("status") or "").strip().lower()
    if status in {"cancel", "canceled", "closed"}:
        return "closed"
    if status in {"waitbuyerpay", "waitsellerconfirm", "waitbuyerconfirm"}:
        return "unpaid"
    refund_value = base.get("refund")
    if refund_value not in {None, ""}:
        refund = _decimal(refund_value, "refund")
        if refund > 0:
            return "refunded" if refund >= total else "partially_refunded"
    return "paid"


def _catalog_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status == "published":
        return "active"
    if status in {"deleted", "tbd"}:
        return "deleted"
    if status in {"expired", "member expired"}:
        return "inactive"
    return "draft"


class Alibaba1688IntegrationService:
    PLATFORM = "alibaba_1688"
    CONNECTOR_ID = Alibaba1688Connector.CONNECTOR_ID

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        operations: OperationsService | None = None,
        top_client: Alibaba1688Client | None = None,
    ):
        self.db = db
        self.settings = settings
        self.operations = operations
        self.client = top_client or Alibaba1688Client(settings)
        self.channel_availability = ChannelAvailabilityService(db)
        self.sync_checkpoints = ConnectorSyncCheckpointService(db)
        try:
            self.cipher = CredentialCipher(
                settings.alibaba_1688_credential_key,
                key_name="ALIBABA_1688_CREDENTIAL_KEY",
                associated_data=b"yunpai-alibaba-1688-v1",
            )
        except TaobaoError:
            self.cipher = None

    def _cipher_configured(self) -> bool:
        return self.cipher is not None

    def _oauth_configured(self) -> bool:
        return bool(
            self.settings.alibaba_1688_enabled
            and self.settings.alibaba_1688_app_key
            and self.settings.alibaba_1688_app_secret
            and self.settings.alibaba_1688_redirect_uri
            and self._cipher_configured()
        )

    def _require_oauth_configured(self) -> None:
        if not self._oauth_configured():
            raise Alibaba1688Error("1688 OAuth is not fully configured")

    def capabilities(self, tenant_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='authorized' THEN 1 ELSE 0 END) AS authorized
                FROM platform_connections WHERE tenant_id=? AND platform=?
                """,
                (tenant_id, self.PLATFORM),
            ).fetchone()
        configured = self._oauth_configured()
        hosted_configured = bool(
            configured
            and self.settings.alibaba_1688_hosted_tenant_id == tenant_id
        )
        authorized = int(row["authorized"] or 0)
        readable = configured and authorized > 0
        return {
            "enabled": self.settings.alibaba_1688_enabled,
            "configured": configured,
            "connections": int(row["total"] or 0),
            "authorized_connections": authorized,
            "connector_id": self.CONNECTOR_ID,
            "protocol": {
                "authorization": (
                    "OAuth2 service-market hosted plus optional WEB flow"
                ),
                "signature": "HMAC-SHA1 over param2 urlPath and sorted parameters",
                "gateway": self.settings.alibaba_1688_gateway,
            },
            "capabilities": {
                "hosted_authorization": {
                    "available": hosted_configured,
                    "reason": (
                        None
                        if hosted_configured
                        else "configure an explicit single-tenant hosted binding"
                    ),
                },
                "order_read": {
                    "available": readable,
                    "reason": None if readable else "complete merchant OAuth first",
                },
                "catalog_read": {
                    "available": readable,
                    "reason": None if readable else "complete merchant OAuth first",
                },
                "channel_availability_read": {
                    "available": readable,
                    "semantic_role": "channel_available",
                    "persistence": True,
                    "batch_sync": readable,
                    "reason": (
                        None if readable else "complete merchant OAuth first"
                    ),
                },
                "inventory_read": {
                    "available": False,
                    "reason": (
                        "only amountOnSale is exposed in the selected product response; "
                        "partial inventory is not written to inventory_balances"
                    ),
                },
                "writes": {
                    "available": False,
                    "reason": "out of scope; connector is read-only",
                },
            },
        }

    def begin_authorization(self, tenant_id: str, store_id: str) -> dict[str, str]:
        self._require_oauth_configured()
        state = secrets.token_urlsafe(32)
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                "DELETE FROM platform_oauth_states WHERE platform=? AND expires_at < ?",
                (self.PLATFORM, now.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO platform_oauth_states(
                    state_hash, tenant_id, platform, shop_id, expires_at, used_at, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    state_hash,
                    tenant_id,
                    self.PLATFORM,
                    store_id,
                    (now + timedelta(minutes=10)).isoformat(),
                    now.isoformat(),
                ),
            )
        query = urlencode(
            {
                "client_id": self.settings.alibaba_1688_app_key,
                "site": self.settings.alibaba_1688_oauth_site,
                "redirect_uri": self.settings.alibaba_1688_redirect_uri,
                "state": state,
            }
        )
        return {
            "authorization_url": (
                f"{self.settings.alibaba_1688_oauth_authorize_url}?{query}"
            ),
            "state": state,
        }

    def complete_authorization(
        self, code: str, state: str | None = None
    ) -> dict[str, Any]:
        self._require_oauth_configured()
        if state:
            state_hash, tenant_id, expected_member_id = (
                self._validate_oauth_state(state)
            )
            authorization_mode = "web"
        else:
            tenant_id = self.settings.alibaba_1688_hosted_tenant_id
            if not tenant_id:
                raise Alibaba1688Error(
                    "1688 hosted tenant binding is not configured"
                )
            state_hash = None
            expected_member_id = None
            authorization_mode = "hosted"

        token = self.client.exchange_authorization_code(code)
        member_id = str(token.get("memberId") or "").strip()
        if not member_id:
            raise Alibaba1688RemoteError(
                "1688 OAuth response did not contain memberId"
            )
        if expected_member_id and expected_member_id != member_id:
            raise Alibaba1688Error(
                "authorized 1688 memberId does not match the requested store"
            )
        return self._store_authorization(
            token,
            tenant_id=tenant_id,
            store_id=member_id,
            authorization_mode=authorization_mode,
            state_hash=state_hash,
        )

    def _validate_oauth_state(self, state: str) -> tuple[str, str, str]:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_oauth_states WHERE state_hash=? AND platform=?",
                (state_hash, self.PLATFORM),
            ).fetchone()
            if row is None or row["used_at"] is not None:
                raise Alibaba1688Error("OAuth state is invalid or has already been used")
            if datetime.fromisoformat(str(row["expires_at"])) < now:
                raise Alibaba1688Error("OAuth state has expired")
        return state_hash, str(row["tenant_id"]), str(row["shop_id"])

    def _store_authorization(
        self,
        token: Mapping[str, Any],
        *,
        tenant_id: str,
        store_id: str,
        authorization_mode: str,
        state_hash: str | None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise Alibaba1688RemoteError(
                "1688 OAuth response did not contain access_token"
            )
        expires_in = int(token.get("expires_in") or 0)
        expires_at = (
            (now + timedelta(seconds=expires_in)).isoformat()
            if expires_in > 0
            else None
        )
        if self.cipher is None:
            raise Alibaba1688Error("1688 credential encryption is not configured")
        try:
            encrypted = self.cipher.encrypt(token)
        except TaobaoError as exc:
            raise Alibaba1688Error(str(exc)) from exc
        connection_id = f"connection-{uuid.uuid4().hex}"
        account_id = str(
            token.get("memberId")
            or token.get("aliId")
            or token.get("resource_owner")
            or ""
        )
        metadata_json = json.dumps(
            {"authorization_mode": authorization_mode},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.db._write_lock, self.db.connect() as conn:
            if state_hash is not None:
                consumed = conn.execute(
                    """
                    UPDATE platform_oauth_states SET used_at=?
                    WHERE state_hash=? AND platform=? AND used_at IS NULL
                      AND expires_at >= ?
                    """,
                    (
                        now.isoformat(),
                        state_hash,
                        self.PLATFORM,
                        now.isoformat(),
                    ),
                )
                if consumed.rowcount != 1:
                    raise Alibaba1688Error(
                        "OAuth state is invalid, expired, or has already been used"
                    )
            conn.execute(
                """
                INSERT INTO platform_connections(
                    id, tenant_id, platform, shop_id, status, account_id, account_nick,
                    credential_ciphertext, token_expires_at, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'authorized', ?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, platform, shop_id) DO UPDATE SET
                    status='authorized', account_id=excluded.account_id,
                    account_nick=NULL, credential_ciphertext=excluded.credential_ciphertext,
                    token_expires_at=excluded.token_expires_at,
                    metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
                """,
                (
                    connection_id,
                    tenant_id,
                    self.PLATFORM,
                    store_id,
                    account_id or None,
                    encrypted,
                    expires_at,
                    metadata_json,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            saved = conn.execute(
                """
                SELECT id, shop_id, status, token_expires_at
                FROM platform_connections WHERE tenant_id=? AND platform=? AND shop_id=?
                """,
                (tenant_id, self.PLATFORM, store_id),
            ).fetchone()
        self.db.audit(
            "alibaba_1688.oauth.authorized",
            f"alibaba-1688-{authorization_mode}-oauth",
            str(saved["id"]),
            {
                "store_id": saved["shop_id"],
                "authorization_mode": authorization_mode,
            },
            tenant_id,
        )
        return dict(saved)

    def _connection_for_store(
        self, tenant_id: str, store_id: str
    ) -> Mapping[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM platform_connections
                WHERE tenant_id=? AND platform=? AND status='authorized' AND shop_id=?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (tenant_id, self.PLATFORM, store_id),
            ).fetchone()
        if row is None:
            raise Alibaba1688Error(
                "no authorized 1688 connection matches this store"
            )
        return row

    def _access_token_for_store(self, tenant_id: str, store_id: str) -> str:
        connection = self._connection_for_store(tenant_id, store_id)
        if self.cipher is None:
            raise Alibaba1688Error("1688 credential encryption is not configured")
        try:
            token = self.cipher.decrypt(str(connection["credential_ciphertext"]))
        except TaobaoError as exc:
            raise Alibaba1688Error(str(exc)) from exc
        expires_at = connection["token_expires_at"]
        if expires_at:
            expiry = datetime.fromisoformat(str(expires_at))
            if expiry <= datetime.now(UTC) + timedelta(minutes=5):
                try:
                    token = self._refresh_connection(connection, token)
                except (Alibaba1688Error, Alibaba1688RemoteError) as exc:
                    self._mark_refresh_failure(connection, exc)
                    raise Alibaba1688RemoteError(
                        "1688 access token refresh failed; "
                        "the connection requires authorization again",
                        code=getattr(exc, "code", None),
                    ) from exc
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise Alibaba1688Error("stored 1688 credential has no access_token")
        return access_token

    def _refresh_connection(
        self, connection: Mapping[str, Any], token: Mapping[str, Any]
    ) -> dict[str, Any]:
        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            raise Alibaba1688Error(
                "1688 access token expired and cannot be refreshed"
            )
        refreshed = self.client.refresh_access_token(refresh_token)
        access_token = str(refreshed.get("access_token") or "")
        if not access_token:
            raise Alibaba1688RemoteError(
                "1688 refresh response did not contain access_token"
            )
        merged = {**dict(token), **refreshed}
        if not merged.get("refresh_token"):
            merged["refresh_token"] = refresh_token
        expires_in = int(merged.get("expires_in") or 0)
        expires_at = (
            (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
            if expires_in > 0
            else None
        )
        if self.cipher is None:
            raise Alibaba1688Error("1688 credential encryption is not configured")
        try:
            encrypted = self.cipher.encrypt(merged)
        except TaobaoError as exc:
            raise Alibaba1688Error(str(exc)) from exc
        now = datetime.now(UTC).isoformat()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE platform_connections
                SET credential_ciphertext=?, token_expires_at=?, updated_at=?
                WHERE id=?
                """,
                (encrypted, expires_at, now, connection["id"]),
            )
        self.db.audit(
            "alibaba_1688.oauth.refreshed",
            "alibaba-1688-token-refresh",
            str(connection["id"]),
            {"store_id": connection["shop_id"]},
            str(connection["tenant_id"]),
        )
        return merged

    def _mark_refresh_failure(
        self, connection: Mapping[str, Any], exc: Exception
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE platform_connections SET status='error', updated_at=?
                WHERE id=? AND status='authorized'
                """,
                (now, connection["id"]),
            )
        self.db.audit(
            "alibaba_1688.oauth.refresh_failed",
            "alibaba-1688-token-refresh",
            str(connection["id"]),
            {
                "store_id": connection["shop_id"],
                "error_code": getattr(exc, "code", None)
                or type(exc).__name__,
            },
            str(connection["tenant_id"]),
        )

    def _connector_for_store(
        self, tenant_id: str, store_id: str
    ) -> Alibaba1688Connector:
        return Alibaba1688Connector(
            self.client,
            access_token=self._access_token_for_store(tenant_id, store_id),
            store_id=store_id,
        )

    def list_orders(
        self,
        tenant_id: str,
        *,
        store_id: str,
        cursor: str | None = None,
        limit: int = 20,
        modify_start_time: str | None = None,
        modify_end_time: str | None = None,
    ) -> PullBatch:
        return self._connector_for_store(tenant_id, store_id).list_orders(
            PullRequest(resource="orders", cursor=cursor, limit=limit),
            modify_start_time=modify_start_time,
            modify_end_time=modify_end_time,
        )

    def list_products(
        self,
        tenant_id: str,
        *,
        store_id: str,
        cursor: str | None = None,
        limit: int = 20,
        modify_start_time: str | None = None,
        modify_end_time: str | None = None,
    ) -> PullBatch:
        return self._connector_for_store(tenant_id, store_id).list_products(
            PullRequest(resource="catalog", cursor=cursor, limit=limit),
            modify_start_time=modify_start_time,
            modify_end_time=modify_end_time,
        )

    def get_order(
        self, tenant_id: str, *, store_id: str, order_id: str
    ) -> PullRecord:
        return self._connector_for_store(tenant_id, store_id).get_order(order_id)

    def get_product(
        self, tenant_id: str, *, store_id: str, product_id: str
    ) -> PullRecord:
        return self._connector_for_store(tenant_id, store_id).get_product(
            product_id
        )

    def get_product_availability(
        self, tenant_id: str, *, store_id: str, product_id: str
    ) -> dict[str, Any]:
        return self._connector_for_store(
            tenant_id, store_id
        ).get_product_availability(product_id)

    @staticmethod
    def _availability_snapshot_input(
        availability: Mapping[str, Any],
    ) -> ChannelAvailabilitySnapshotInput:
        try:
            source_updated_at = _stable_source_datetime(
                availability["source_version"]
            )
            observed_at = datetime.fromisoformat(str(availability["observed_at"]))
            records = [
                ChannelAvailabilityRecordInput(
                    scope=str(row["scope"]),
                    source_sku_id=row.get("source_sku_id"),
                    warehouse_code=row.get("warehouse_code"),
                    available_qty=row["available_qty"],
                )
                for row in availability["records"]
            ]
            return ChannelAvailabilitySnapshotInput(
                connector_id=str(availability["connector_id"]),
                store_id=str(availability["store_id"]),
                source_product_id=str(availability["source_product_id"]),
                source_updated_at=source_updated_at,
                payload_hash=str(availability["source_payload_hash"]),
                source_id=availability.get("source_id"),
                observed_at=observed_at,
                unit=availability.get("unit"),
                inventory_reduce_type=availability.get("inventory_reduce_type"),
                records=records,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise Alibaba1688Error(
                "1688 channel availability snapshot is invalid"
            ) from exc

    def list_persisted_availability(
        self,
        tenant_id: str,
        *,
        store_id: str,
        product_id: str | None = None,
        sku_id: str | None = None,
        scope: AvailabilityScope | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        records = self.channel_availability.list_current(
            tenant_id,
            connector_id=self.CONNECTOR_ID,
            store_id=store_id,
            source_product_id=product_id,
            source_sku_id=sku_id,
            scope=scope,
            limit=limit,
        )
        return {
            "connector_id": self.CONNECTOR_ID,
            "resource": "channel_availability",
            "semantic_role": "channel_available",
            "store_id": store_id,
            "records": records,
            "count": len(records),
        }

    def get_persisted_availability(
        self,
        tenant_id: str,
        *,
        store_id: str,
        product_id: str,
    ) -> dict[str, Any] | None:
        return self.channel_availability.get_snapshot(
            tenant_id,
            connector_id=self.CONNECTOR_ID,
            store_id=store_id,
            source_product_id=product_id,
        )

    @staticmethod
    def _order_upsert(store_id: str, record: PullRecord) -> OrderUpsert:
        value = record.payload
        base = value.get("baseInfo")
        if not isinstance(base, dict):
            raise Alibaba1688Error("1688 order baseInfo is required")
        order_id = str(base.get("idOfStr") or base.get("id") or "").strip()
        if not order_id:
            raise Alibaba1688Error("1688 order id is required")
        placed_at = _platform_datetime(base.get("createTime"))
        source_updated_at = _platform_datetime(
            base.get("modifyTime") or base.get("createTime")
        )
        if placed_at is None or source_updated_at is None:
            raise Alibaba1688Error("1688 order timestamps are required")
        total = _decimal(base.get("totalAmount"), "totalAmount")
        raw_lines = value.get("productItems")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise Alibaba1688Error("1688 order productItems are required")
        lines: list[OrderLineInput] = []
        for raw in raw_lines:
            if not isinstance(raw, dict):
                raise Alibaba1688Error("1688 order line is invalid")
            product_id = str(raw.get("productID") or "").strip()
            line_id = str(raw.get("subItemID") or "").strip()
            title = str(raw.get("name") or "").strip()
            if not product_id or not line_id or not title:
                raise Alibaba1688Error(
                    "1688 order line identifiers and title are required"
                )
            lines.append(
                OrderLineInput(
                    line_id=line_id,
                    sku_id=str(
                        raw.get("skuID") or raw.get("skuId") or product_id
                    ),
                    item_id=product_id,
                    title=title,
                    quantity=_integer(raw.get("quantity"), "quantity"),
                    unit_price=_decimal(raw.get("price"), "price"),
                )
            )
        return OrderUpsert(
            connector_id=Alibaba1688Connector.CONNECTOR_ID,
            store_id=store_id,
            order_id=order_id,
            order_status=_order_status(base.get("status")),
            payment_status=_payment_status(base, total),
            currency="CNY",
            total_amount=total,
            placed_at=placed_at,
            buyer_ref_hash=None,
            lines=lines,
            source_updated_at=source_updated_at,
            source_id=record.source_id,
        )

    @staticmethod
    def _catalog_upserts(
        store_id: str, record: PullRecord
    ) -> list[CatalogItemUpsert]:
        value = record.payload
        product_id = str(value.get("productID") or "").strip()
        title = str(value.get("subject") or "").strip()
        source_updated_at = _platform_datetime(
            value.get("lastUpdateTime") or value.get("createTime")
        )
        if not product_id or not title or source_updated_at is None:
            raise Alibaba1688Error(
                "1688 product id, title and update time are required"
            )
        sku_infos = value.get("skuInfos")
        if not isinstance(sku_infos, list) or not sku_infos:
            raise Alibaba1688Error("1688 product skuInfos are required")
        sale_info = value.get("saleInfo")
        sale = sale_info if isinstance(sale_info, dict) else {}
        upserts: list[CatalogItemUpsert] = []
        for sku in sku_infos:
            if not isinstance(sku, dict):
                raise Alibaba1688Error("1688 product sku is invalid")
            sku_id = str(sku.get("skuId") or sku.get("skuID") or "").strip()
            if not sku_id:
                raise Alibaba1688Error("1688 sku id is required")
            if sku.get("price") in {None, ""}:
                raise Alibaba1688Error(
                    f"1688 sku {sku_id} price is required; consignPrice is not a sale price fallback"
                )
            price = _decimal(sku.get("price"), "sku price")
            attributes: dict[str, Any] = {
                "platform_status": str(value.get("status") or ""),
                "category_id": str(value.get("categoryID") or ""),
                "price_basis": "price",
            }
            for target, source in (
                ("merchant_sku", "skuCode"),
                ("spec_id", "specId"),
                ("cargo_number", "cargoNumber"),
                ("amount_on_sale", "amountOnSale"),
                ("retail_price", "retailPrice"),
                ("consign_price", "consignPrice"),
                ("take_sample_price", "takeSamplePrice"),
            ):
                if sku.get(source) not in {None, ""}:
                    attributes[target] = _as_string(sku[source])
            if isinstance(sku.get("priceRange"), list) and sku["priceRange"]:
                attributes["price_range"] = _as_string(sku["priceRange"])
            if sale.get("quoteType") not in {None, ""}:
                attributes["quote_type"] = _as_string(sale["quoteType"])
            if isinstance(sale.get("priceRanges"), list) and sale["priceRanges"]:
                attributes["price_ranges"] = _as_string(sale["priceRanges"])
            if sale.get("unit") not in {None, ""}:
                attributes["unit"] = _as_string(sale["unit"])
            upserts.append(
                CatalogItemUpsert(
                    connector_id=Alibaba1688Connector.CONNECTOR_ID,
                    store_id=store_id,
                    item_id=product_id,
                    sku_id=sku_id,
                    title=title,
                    status=_catalog_status(value.get("status")),
                    sale_price=price,
                    currency="CNY",
                    attributes=attributes,
                    source_updated_at=source_updated_at,
                    source_id=f"{record.source_id}:sku:{sku_id}",
                )
            )
        return upserts

    @staticmethod
    def _summary(resource: str, received: int) -> dict[str, Any]:
        return {
            "connector_id": Alibaba1688Connector.CONNECTOR_ID,
            "resource": resource,
            "received": received,
            "mapped": 0,
            "applied": 0,
            "idempotent": 0,
            "rejected": 0,
            "issues": [],
        }

    def sync_orders(
        self,
        tenant_id: str,
        *,
        store_id: str,
        cursor: str | None = None,
        limit: int = 20,
        modify_start_time: str | None = None,
        modify_end_time: str | None = None,
    ) -> dict[str, Any]:
        if self.operations is None:
            raise Alibaba1688Error("1688 domain synchronization is unavailable")
        batch = self.list_orders(
            tenant_id,
            store_id=store_id,
            cursor=cursor,
            limit=limit,
            modify_start_time=modify_start_time,
            modify_end_time=modify_end_time,
        )
        summary = self._summary("orders", len(batch.records))
        for record in batch.records:
            try:
                value = self._order_upsert(store_id, record)
                summary["mapped"] += 1
                result = self.operations.orders.merge_order_lines_snapshot(
                    tenant_id, value
                )
                summary[result["write_status"]] += 1
            except (Alibaba1688Error, ValueError) as exc:
                summary["rejected"] += 1
                summary["issues"].append(
                    {"source_id": record.source_id, "code": str(exc)}
                )
        summary.update(
            {
                "next_cursor": batch.next_cursor,
                "has_more": batch.has_more,
                "data_as_of": batch.data_as_of,
            }
        )
        self.db.audit(
            "alibaba_1688.orders.synced",
            "alibaba-1688-sync",
            store_id,
            {
                key: summary[key]
                for key in ("received", "applied", "idempotent", "rejected")
            },
            tenant_id,
        )
        return summary

    def sync_products(
        self,
        tenant_id: str,
        *,
        store_id: str,
        cursor: str | None = None,
        limit: int = 20,
        modify_start_time: str | None = None,
        modify_end_time: str | None = None,
    ) -> dict[str, Any]:
        if self.operations is None:
            raise Alibaba1688Error("1688 domain synchronization is unavailable")
        batch = self.list_products(
            tenant_id,
            store_id=store_id,
            cursor=cursor,
            limit=limit,
            modify_start_time=modify_start_time,
            modify_end_time=modify_end_time,
        )
        summary = self._summary("catalog", len(batch.records))
        for record in batch.records:
            try:
                values = self._catalog_upserts(store_id, record)
                summary["mapped"] += len(values)
                for value in values:
                    result = self.operations.catalog.upsert(tenant_id, value)
                    summary[result["write_status"]] += 1
            except (Alibaba1688Error, ValueError) as exc:
                summary["rejected"] += 1
                summary["issues"].append(
                    {"source_id": record.source_id, "code": str(exc)}
                )
        summary.update(
            {
                "next_cursor": batch.next_cursor,
                "has_more": batch.has_more,
                "data_as_of": batch.data_as_of,
            }
        )
        self.db.audit(
            "alibaba_1688.catalog.synced",
            "alibaba-1688-sync",
            store_id,
            {
                key: summary[key]
                for key in (
                    "received",
                    "mapped",
                    "applied",
                    "idempotent",
                    "rejected",
                )
            },
            tenant_id,
        )
        return summary

    def _fetch_availability_page(
        self,
        connector: Alibaba1688Connector,
        *,
        cursor: str | None,
        limit: int,
        modify_start_time: str | None,
        modify_end_time: str | None,
    ) -> PullBatch:
        last_error: Alibaba1688RemoteError | None = None
        for _attempt in range(PAGE_RETRY_LIMIT):
            try:
                return connector.list_products(
                    PullRequest(resource="catalog", cursor=cursor, limit=limit),
                    modify_start_time=modify_start_time,
                    modify_end_time=modify_end_time,
                )
            except Alibaba1688RemoteError as exc:
                last_error = exc
                if not _is_http_502(exc):
                    raise
        assert last_error is not None
        raise last_error

    def _availability_run_summary(
        self,
        *,
        received: int,
        checkpoint: dict[str, Any],
        recon: dict[str, Any],
        next_cursor: str | None,
        has_more: bool,
        data_as_of: str | None,
        upstream_total: int | None,
        mapped: int = 0,
        applied: int = 0,
        idempotent: int = 0,
        rejected: int = 0,
        issues: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        summary = self._summary("channel_availability", received)
        summary.update(
            {
                "mapped": mapped,
                "applied": applied,
                "idempotent": idempotent,
                "rejected": rejected,
                "issues": issues or [],
                "next_cursor": next_cursor,
                "has_more": has_more,
                "data_as_of": data_as_of,
                "upstream_total": upstream_total,
                "recon": recon,
                "checkpoint": public_checkpoint_view(checkpoint),
            }
        )
        return summary

    def _fail_invalid_availability_cursor(
        self,
        checkpoint: dict[str, Any],
        *,
        owner: str,
    ) -> dict[str, Any]:
        return self.sync_checkpoints.fail(
            {**checkpoint, "cursor": ""},
            owner=owner,
            expected_version=int(checkpoint["row_version"]),
            error_kind="cursor_invalid",
            error="1688 availability cursor is invalid",
        )

    def _fail_availability_checkpoint(
        self,
        checkpoint: dict[str, Any],
        *,
        owner: str,
        error_kind: str,
        error: str,
        received: int,
        mapped: int,
        applied: int,
        idempotent: int,
        rejected: int,
        issues: list[dict[str, Any]],
        data_as_of: str | None,
        recon: dict[str, Any] | None = None,
        upstream_total: int | None = None,
    ) -> dict[str, Any]:
        failed = self.sync_checkpoints.fail(
            checkpoint,
            owner=owner,
            expected_version=int(checkpoint["row_version"]),
            error_kind=error_kind,
            error=error,
        )
        local_product_count = recon["local_product_count"] if recon else None
        if recon is None:
            recon = {
                "status": "failed",
                "code": error_kind,
                "local_product_count": local_product_count,
                "upstream_total": upstream_total,
            }
        return self._availability_run_summary(
            received=received,
            checkpoint=failed,
            recon=recon,
            next_cursor=failed["cursor"] or None,
            has_more=True,
            data_as_of=data_as_of,
            upstream_total=upstream_total if upstream_total is not None else failed["upstream_total"],
            mapped=mapped,
            applied=applied,
            idempotent=idempotent,
            rejected=rejected,
            issues=issues,
        )

    def sync_availability(
        self,
        tenant_id: str,
        *,
        store_id: str,
        cursor: str | None = None,
        limit: int = 20,
        modify_start_time: str | None = None,
        modify_end_time: str | None = None,
    ) -> dict[str, Any]:
        start, end = _window(modify_start_time, modify_end_time)
        window_kind, window_start, window_end = availability_window(start, end)
        is_full = window_kind == WINDOW_FULL
        owner = f"1688-availability-{uuid.uuid4().hex}"
        requested_cursor = cursor
        caller_cursor_error: Alibaba1688Error | None = None
        if cursor not in {None, ""}:
            try:
                _page(cursor)
            except Alibaba1688Error as exc:
                requested_cursor = None
                caller_cursor_error = exc
        try:
            checkpoint = self.sync_checkpoints.acquire(
                tenant_id,
                connector_id=Alibaba1688Connector.CONNECTOR_ID,
                store_id=store_id,
                resource=AVAILABILITY_RESOURCE,
                window_kind=window_kind,
                window_start=window_start,
                window_end=window_end,
                owner=owner,
                requested_cursor=requested_cursor,
            )
        except SyncCheckpointConflict as exc:
            raise Alibaba1688Error(str(exc)) from exc

        if caller_cursor_error is not None:
            self._fail_invalid_availability_cursor(checkpoint, owner=owner)
            raise caller_cursor_error
        page_cursor = str(checkpoint["cursor"] or "") or None
        try:
            _page(page_cursor)
        except Alibaba1688Error:
            self._fail_invalid_availability_cursor(checkpoint, owner=owner)
            raise

        try:
            connector = self._connector_for_store(tenant_id, store_id)
        except (Alibaba1688Error, Alibaba1688RemoteError):
            self.sync_checkpoints.fail(
                checkpoint,
                owner=owner,
                expected_version=int(checkpoint["row_version"]),
                error_kind="store_unavailable",
                error="1688 availability store is not available",
            )
            raise
        expected_total = (
            int(checkpoint["upstream_total"])
            if is_full and checkpoint["upstream_total"] is not None
            else None
        )
        received = 0
        mapped = 0
        applied = 0
        idempotent = 0
        rejected = 0
        issues: list[dict[str, Any]] = []
        data_as_of: str | None = None
        max_source_time: str | None = None

        while True:
            try:
                batch = self._fetch_availability_page(
                    connector,
                    cursor=page_cursor,
                    limit=limit,
                    modify_start_time=start,
                    modify_end_time=end,
                )
            except Alibaba1688Error:
                self.sync_checkpoints.fail(
                    checkpoint,
                    owner=owner,
                    expected_version=int(checkpoint["row_version"]),
                    error_kind="request_invalid",
                    error="1688 availability request failed",
                )
                raise
            except Alibaba1688RemoteError as exc:
                error_kind = "http_502" if _is_http_502(exc) else "remote_error"
                summary = self._fail_availability_checkpoint(
                    checkpoint,
                    owner=owner,
                    error_kind=error_kind,
                    error="1688 availability page request failed",
                    received=received,
                    mapped=mapped,
                    applied=applied,
                    idempotent=idempotent,
                    rejected=rejected,
                    issues=issues,
                    data_as_of=data_as_of,
                    upstream_total=expected_total,
                )
                if error_kind == "http_502":
                    return summary
                raise

            data_as_of = batch.data_as_of
            page_total = batch.upstream_total

            if is_full:
                if page_total is None:
                    local_count = self.channel_availability.count_snapshots(
                        tenant_id,
                        connector_id=Alibaba1688Connector.CONNECTOR_ID,
                        store_id=store_id,
                    )
                    return self._fail_availability_checkpoint(
                        checkpoint,
                        owner=owner,
                        error_kind="upstream_total_missing",
                        error="1688 availability official total is missing",
                        received=received,
                        mapped=mapped,
                        applied=applied,
                        idempotent=idempotent,
                        rejected=rejected,
                        issues=issues,
                        data_as_of=data_as_of,
                        recon={
                            "status": "failed",
                            "code": "upstream_total_missing",
                            "local_product_count": local_count,
                            "upstream_total": None,
                        },
                    )
                if expected_total is not None and page_total != expected_total:
                    local_count = self.channel_availability.count_snapshots(
                        tenant_id,
                        connector_id=Alibaba1688Connector.CONNECTOR_ID,
                        store_id=store_id,
                    )
                    return self._fail_availability_checkpoint(
                        checkpoint,
                        owner=owner,
                        error_kind="upstream_total_changed",
                        error="1688 availability official total changed across pages",
                        received=received,
                        mapped=mapped,
                        applied=applied,
                        idempotent=idempotent,
                        rejected=rejected,
                        issues=issues,
                        data_as_of=data_as_of,
                        recon={
                            "status": "failed",
                            "code": "upstream_total_changed",
                            "local_product_count": local_count,
                            "upstream_total": page_total,
                        },
                    )
                expected_total = page_total

            snapshots: list[ChannelAvailabilitySnapshotInput] = []
            try:
                for record in batch.records:
                    availability = connector.availability_from_record(record)
                    snapshot = self._availability_snapshot_input(availability)
                    snapshots.append(snapshot)
                    mapped += len(snapshot.records)
                    source_time = snapshot.source_updated_at.isoformat()
                    if max_source_time is None or source_time > max_source_time:
                        max_source_time = source_time
            except (
                Alibaba1688Error,
                Alibaba1688RemoteError,
                SourceVersionError,
                ValueError,
            ) as exc:
                return self._fail_availability_checkpoint(
                    checkpoint,
                    owner=owner,
                    error_kind="parse_failed",
                    error="1688 availability page parse failed",
                    received=received,
                    mapped=mapped,
                    applied=applied,
                    idempotent=idempotent,
                    rejected=rejected + 1,
                    issues=issues + [{"code": type(exc).__name__}],
                    data_as_of=data_as_of,
                    upstream_total=expected_total,
                )

            next_cursor = batch.next_cursor or ""
            pages_completed = int(checkpoint["pages_completed"]) + 1
            records_received = int(checkpoint["records_received"]) + len(batch.records)
            page_applied = 0
            page_idempotent = 0
            mismatch_recon: dict[str, Any] | None = None
            try:
                with self.db._write_lock, self.db.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    for snapshot in snapshots:
                        result = self.channel_availability.replace_snapshot_on_conn(
                            conn, tenant_id, snapshot
                        )
                        if result["write_status"] == "applied":
                            page_applied += 1
                        else:
                            page_idempotent += 1
                    records_applied = int(checkpoint["records_applied"]) + page_applied
                    local_count = self.channel_availability.count_snapshots_on_conn(
                        conn,
                        tenant_id,
                        connector_id=Alibaba1688Connector.CONNECTOR_ID,
                        store_id=store_id,
                    )
                    if not batch.has_more and is_full and local_count != expected_total:
                        checkpoint = self.sync_checkpoints.fail_on_conn(
                            conn,
                            checkpoint,
                            owner=owner,
                            expected_version=int(checkpoint["row_version"]),
                            error_kind="upstream_total_mismatch",
                            error="1688 availability local snapshot count does not match official total",
                        )
                        mismatch_recon = {
                            "status": "failed",
                            "code": "upstream_total_mismatch",
                            "local_product_count": local_count,
                            "upstream_total": expected_total,
                        }
                    elif not batch.has_more:
                        watermark = (
                            window_end
                            if not is_full and window_end
                            else max_source_time
                        )
                        watermark_kind = (
                            WATERMARK_SOURCE_TIME
                            if not is_full
                            else WATERMARK_EXHAUSTED
                        )
                        checkpoint = self.sync_checkpoints.complete(
                            conn,
                            checkpoint,
                            owner=owner,
                            expected_version=int(checkpoint["row_version"]),
                            cursor=next_cursor,
                            pages_completed=pages_completed,
                            records_received=records_received,
                            records_applied=records_applied,
                            upstream_total=expected_total if is_full else None,
                            watermark=watermark,
                            watermark_kind=watermark_kind,
                        )
                    else:
                        checkpoint = self.sync_checkpoints.apply_page(
                            conn,
                            checkpoint,
                            owner=owner,
                            cursor=next_cursor,
                            pages_completed=pages_completed,
                            records_received=records_received,
                            records_applied=records_applied,
                            upstream_total=expected_total if is_full else None,
                            expected_version=int(checkpoint["row_version"]),
                        )
            except SyncCheckpointConflict as exc:
                raise Alibaba1688Error(str(exc)) from exc
            except (
                Alibaba1688Error,
                Alibaba1688RemoteError,
                SourceVersionError,
                ValueError,
            ) as exc:
                return self._fail_availability_checkpoint(
                    checkpoint,
                    owner=owner,
                    error_kind="page_apply_failed",
                    error="1688 availability page apply failed",
                    received=received,
                    mapped=mapped,
                    applied=applied,
                    idempotent=idempotent,
                    rejected=rejected + 1,
                    issues=issues + [{"code": type(exc).__name__}],
                    data_as_of=data_as_of,
                    upstream_total=expected_total,
                )

            applied += page_applied
            idempotent += page_idempotent
            received += len(batch.records)
            if mismatch_recon is not None:
                summary = self._availability_run_summary(
                    received=received,
                    checkpoint=checkpoint,
                    recon=mismatch_recon,
                    next_cursor=page_cursor,
                    has_more=False,
                    data_as_of=data_as_of,
                    upstream_total=expected_total,
                    mapped=mapped,
                    applied=applied,
                    idempotent=idempotent,
                    rejected=rejected,
                    issues=issues,
                )
                self.db.audit(
                    "alibaba_1688.channel_availability.synced",
                    "alibaba-1688-sync",
                    store_id,
                    {
                        key: summary[key]
                        for key in (
                            "received",
                            "mapped",
                            "applied",
                            "idempotent",
                            "rejected",
                        )
                    },
                    tenant_id,
                )
                return summary
            if batch.has_more:
                page_cursor = next_cursor or None
                continue

            local_count = self.channel_availability.count_snapshots(
                tenant_id,
                connector_id=Alibaba1688Connector.CONNECTOR_ID,
                store_id=store_id,
            )
            if is_full:
                recon = {
                    "status": "succeeded",
                    "code": "matched",
                    "local_product_count": local_count,
                    "upstream_total": expected_total,
                }
                summary_total = expected_total
            else:
                recon = {
                    "status": "succeeded",
                    "code": "incremental_complete",
                    "local_product_count": local_count,
                    "upstream_total": None,
                }
                summary_total = None
            summary = self._availability_run_summary(
                received=received,
                checkpoint=checkpoint,
                recon=recon,
                next_cursor=None,
                has_more=False,
                data_as_of=data_as_of,
                upstream_total=summary_total,
                mapped=mapped,
                applied=applied,
                idempotent=idempotent,
                rejected=rejected,
                issues=issues,
            )
            self.db.audit(
                "alibaba_1688.channel_availability.synced",
                "alibaba-1688-sync",
                store_id,
                {
                    key: summary[key]
                    for key in (
                        "received",
                        "mapped",
                        "applied",
                        "idempotent",
                        "rejected",
                    )
                },
                tenant_id,
            )
            return summary

        raise RuntimeError("1688 availability sync exited without a terminal page")

    def close(self) -> None:
        self.client.close()
