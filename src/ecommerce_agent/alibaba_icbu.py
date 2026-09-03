from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import unquote, urlencode

import httpx

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
from .taobao import CredentialCipher, TaobaoError, sign_parameters


class AlibabaIcbuError(ValueError):
    pass


class AlibabaIcbuRemoteError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _as_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _response_key(method: str) -> str:
    return f"{method.replace('.', '_')}_response"


def _response_body(payload: Mapping[str, Any], method: str) -> dict[str, Any]:
    value = payload.get(_response_key(method), payload)
    if not isinstance(value, dict):
        raise AlibabaIcbuRemoteError(f"Alibaba ICBU {method} returned an invalid response")
    code = str(value.get("code") or "0")
    if code not in {"", "0"}:
        raise AlibabaIcbuRemoteError(
            f"Alibaba ICBU {method} returned code {code}", code=code
        )
    result = value.get("result")
    if isinstance(result, dict) and str(result.get("success", "true")).lower() == "false":
        remote_code = str(result.get("msg_code") or code or "unknown")
        message = str(result.get("biz_message") or "request rejected")
        raise AlibabaIcbuRemoteError(
            f"Alibaba ICBU {method} {remote_code}: {message}", code=remote_code
        )
    return value


def _list_value(value: Any, *nested_keys: str) -> list[dict[str, Any]]:
    current = value
    for key in nested_keys:
        if isinstance(current, dict):
            current = current.get(key)
    if current is None:
        return []
    if isinstance(current, list):
        return [dict(item) for item in current if isinstance(item, dict)]
    if isinstance(current, dict):
        return [dict(current)]
    raise AlibabaIcbuRemoteError("Alibaba ICBU returned an invalid list payload")


def _data_as_of() -> str:
    return datetime.now(UTC).isoformat()


def _platform_time(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(UTC).isoformat()


def _payload_version(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"payload-sha256:{hashlib.sha256(encoded).hexdigest()}"


def _source_version(value: Mapping[str, Any]) -> str:
    return (
        _platform_time(value.get("gmt_modified"))
        or _platform_time(value.get("gmt_create"))
        or _payload_version(value)
    )


def _product_list_window(
    gmt_modified_from: str | None, gmt_modified_to: str | None
) -> dict[str, str]:
    values = {
        "gmt_modified_from": gmt_modified_from,
        "gmt_modified_to": gmt_modified_to,
    }
    parsed: dict[str, datetime] = {}
    normalized: dict[str, str] = {}
    for name, value in values.items():
        if value is None:
            continue
        text = value.strip()
        try:
            parsed[name] = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise AlibabaIcbuError(
                f"Alibaba ICBU {name} must use YYYY-MM-DD HH:MM:SS"
            ) from exc
        normalized[name] = text
    if (
        "gmt_modified_from" in parsed
        and "gmt_modified_to" in parsed
        and parsed["gmt_modified_from"] > parsed["gmt_modified_to"]
    ):
        raise AlibabaIcbuError(
            "Alibaba ICBU gmt_modified_from must not be after gmt_modified_to"
        )
    return normalized


class AlibabaIcbuTopClient:
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

    def exchange_authorization_code(self, code: str) -> dict[str, Any]:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self.settings.alibaba_icbu_app_key,
                "client_secret": self.settings.alibaba_icbu_app_secret,
                "code": code,
                "redirect_uri": self.settings.alibaba_icbu_redirect_uri,
                "sp": "icbu",
            }
        )

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.settings.alibaba_icbu_app_key,
                "client_secret": self.settings.alibaba_icbu_app_secret,
                "refresh_token": refresh_token,
                "sp": "icbu",
            }
        )

    def _token_request(self, data: Mapping[str, Any]) -> dict[str, Any]:
        response = self._ensure_client().post(
            self.settings.alibaba_icbu_oauth_token_url,
            data=dict(data),
            headers={"Accept": "application/json"},
        )
        return self._decode_response(response, oauth=True)

    def call(
        self,
        method: str,
        business_params: Mapping[str, Any],
        *,
        access_token: str,
    ) -> dict[str, Any]:
        if not access_token:
            raise AlibabaIcbuError("Alibaba ICBU access token is required")
        common: dict[str, Any] = {
            "method": method,
            "app_key": self.settings.alibaba_icbu_app_key,
            "session": access_token,
            "timestamp": datetime.now(timezone(timedelta(hours=8))).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "format": "json",
            "v": "2.0",
            "sign_method": "hmac",
            "simplify": "true",
        }
        payload = {
            **common,
            **{key: _as_string(value) for key, value in business_params.items()},
        }
        payload["sign"] = sign_parameters(
            payload, self.settings.alibaba_icbu_app_secret, "hmac"
        )
        response = self._ensure_client().post(
            self.settings.alibaba_icbu_top_gateway, data=payload
        )
        return self._decode_response(response)

    @staticmethod
    def _decode_response(
        response: httpx.Response, *, oauth: bool = False
    ) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AlibabaIcbuRemoteError(
                f"Alibaba ICBU returned an invalid HTTP response: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise AlibabaIcbuRemoteError("Alibaba ICBU returned a non-object response")
        error = payload.get("error_response") or (
            payload if oauth and payload.get("error") else None
        )
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("error") or "unknown")
            message = str(
                error.get("msg")
                or error.get("error_description")
                or "Alibaba ICBU API error"
            )
            raise AlibabaIcbuRemoteError(
                f"Alibaba ICBU API {code}: {message}", code=code
            )
        return payload

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()


class AlibabaIcbuConnector:
    CONNECTOR_ID = "alibaba_icbu"
    PRODUCT_LIST = "alibaba.icbu.product.list"
    PRODUCT_DETAIL = "alibaba.icbu.product.get"
    PRODUCT_INVENTORY = "alibaba.icbu.product.sku.inventory.get"

    def __init__(
        self,
        client: AlibabaIcbuTopClient,
        *,
        access_token: str,
        store_id: str,
    ):
        self.client = client
        self.access_token = access_token
        self.store_id = store_id

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.CONNECTOR_ID,
            display_name="阿里国际站商品只读接口",
            capability_version="0.1",
            virtual=False,
            resources=["catalog", "product_detail", "inventory"],
            modes=["read", "polling"],
            actions=[],
            supports_dry_run=False,
            supports_idempotency=False,
            supports_readback=True,
            required_permissions=["国际站基础权限包", "merchant.oauth"],
        )

    def test_connection(self) -> ConnectionCheck:
        try:
            self.pull(PullRequest(resource="catalog", limit=1))
        except (AlibabaIcbuError, AlibabaIcbuRemoteError) as exc:
            return ConnectionCheck(
                ok=False,
                connector_id=self.CONNECTOR_ID,
                mode="live",
                detail=str(exc)[:300],
            )
        return ConnectionCheck(
            ok=True,
            connector_id=self.CONNECTOR_ID,
            mode="live",
            detail="已使用商家授权读取商品列表",
        )

    def pull(self, request: PullRequest) -> PullBatch:
        if request.resource == "catalog":
            return self._pull_catalog(request)
        if request.resource == "product_detail":
            return self._pull_product_detail(request)
        if request.resource == "inventory":
            return self._pull_inventory(request)
        raise AlibabaIcbuError(f"resource not supported: {request.resource}")

    def list_products(
        self,
        request: PullRequest,
        *,
        gmt_modified_from: str | None = None,
        gmt_modified_to: str | None = None,
    ) -> PullBatch:
        return self._pull_catalog(
            request,
            gmt_modified_from=gmt_modified_from,
            gmt_modified_to=gmt_modified_to,
        )

    def _pull_catalog(
        self,
        request: PullRequest,
        *,
        gmt_modified_from: str | None = None,
        gmt_modified_to: str | None = None,
    ) -> PullBatch:
        try:
            current_page = int(request.cursor or "1")
        except ValueError as exc:
            raise AlibabaIcbuError("Alibaba ICBU catalog cursor must be a page number") from exc
        if current_page < 1:
            raise AlibabaIcbuError("Alibaba ICBU catalog cursor must be positive")
        page_size = min(request.limit, 30)
        params = {
            "current_page": current_page,
            "page_size": page_size,
            "language": "ENGLISH",
        }
        if gmt_modified_from is not None:
            params["gmt_modified_from"] = gmt_modified_from
        if gmt_modified_to is not None:
            params["gmt_modified_to"] = gmt_modified_to
        response = self.client.call(
            self.PRODUCT_LIST, params, access_token=self.access_token
        )
        body = _response_body(response, self.PRODUCT_LIST)
        products_value = body.get("products")
        if isinstance(products_value, dict):
            products_value = products_value.get("product", products_value)
        products = _list_value(products_value)
        data_as_of = _data_as_of()
        records: list[PullRecord] = []
        for product in products:
            product_id = str(product.get("id") or product.get("product_id") or "")
            if not product_id:
                raise AlibabaIcbuRemoteError(
                    "Alibaba ICBU product list returned a product without an id"
                )
            version = _source_version(product)
            records.append(
                PullRecord(
                    source_id=f"alibaba-icbu:{self.store_id}:product:{product_id}",
                    source_version=version,
                    occurred_at=(version if not version.startswith("payload-sha256:") else data_as_of),
                    payload={"store_id": self.store_id, "product": product},
                )
            )
        total_item = int(body.get("total_item") or len(products))
        returned_page = int(body.get("current_page") or current_page)
        returned_size = int(body.get("page_size") or page_size)
        has_more = returned_page * returned_size < total_item
        return PullBatch(
            connector_id=self.CONNECTOR_ID,
            resource=request.resource,
            records=records,
            next_cursor=str(returned_page + 1) if has_more else None,
            has_more=has_more,
            data_as_of=data_as_of,
        )

    def _pull_product_detail(self, request: PullRequest) -> PullBatch:
        product_id = str(request.cursor or "").strip()
        if not product_id:
            raise AlibabaIcbuError(
                "Alibaba ICBU product detail requires encrypted_product_id"
            )
        response = self.client.call(
            self.PRODUCT_DETAIL,
            {"product_id": product_id, "language": "ENGLISH"},
            access_token=self.access_token,
        )
        body = _response_body(response, self.PRODUCT_DETAIL)
        product = body.get("product")
        if not isinstance(product, dict):
            result = body.get("result")
            product = result.get("product") if isinstance(result, dict) else None
        if not isinstance(product, dict):
            raise AlibabaIcbuRemoteError(
                "Alibaba ICBU product detail did not contain a product"
            )
        data_as_of = _data_as_of()
        version = _source_version(product)
        return PullBatch(
            connector_id=self.CONNECTOR_ID,
            resource=request.resource,
            records=[
                PullRecord(
                    source_id=f"alibaba-icbu:{self.store_id}:product-detail:{product_id}",
                    source_version=version,
                    occurred_at=(version if not version.startswith("payload-sha256:") else data_as_of),
                    payload={
                        "store_id": self.store_id,
                        "product_id": product_id,
                        "product": dict(product),
                    },
                )
            ],
            data_as_of=data_as_of,
        )

    def _pull_inventory(self, request: PullRequest) -> PullBatch:
        product_id = str(request.cursor or "").strip()
        if not product_id:
            raise AlibabaIcbuError(
                "Alibaba ICBU inventory requires plain_product_id"
            )
        response = self.client.call(
            self.PRODUCT_INVENTORY,
            {"product_id": product_id, "language": "en_US"},
            access_token=self.access_token,
        )
        body = _response_body(response, self.PRODUCT_INVENTORY)
        result = body.get("result")
        if not isinstance(result, dict):
            raise AlibabaIcbuRemoteError(
                "Alibaba ICBU inventory did not contain a result"
            )
        inventory_data = result.get("data_list")
        if inventory_data is None:
            raise AlibabaIcbuRemoteError(
                "Alibaba ICBU inventory result did not contain data_list"
            )
        inventory_rows = _list_value(inventory_data)
        data_as_of = _data_as_of()
        records: list[PullRecord] = []
        for item in inventory_rows:
            sku_id = str(item.get("sku_id") or "").strip()
            if not sku_id:
                raise AlibabaIcbuRemoteError(
                    "Alibaba ICBU inventory returned a row without sku_id"
                )
            inventory_value = item.get("inventory")
            if inventory_value is None or str(inventory_value).strip() == "":
                raise AlibabaIcbuRemoteError(
                    "Alibaba ICBU inventory returned a row without inventory"
                )
            inventory_code = str(item.get("inventory_code") or "").strip()
            source_inventory_code = inventory_code or "default"
            payload = {
                "store_id": self.store_id,
                "product_id": product_id,
                "sku_id": sku_id,
                "inventory": str(inventory_value),
                "inventory_code": inventory_code,
            }
            if item.get("sku_outer_id") not in (None, ""):
                payload["sku_outer_id"] = str(item["sku_outer_id"])
            records.append(
                PullRecord(
                    source_id=(
                        f"alibaba-icbu:{self.store_id}:inventory:"
                        f"{product_id}:{sku_id}:{source_inventory_code}"
                    ),
                    source_version=_payload_version(item),
                    occurred_at=data_as_of,
                    payload=payload,
                )
            )
        return PullBatch(
            connector_id=self.CONNECTOR_ID,
            resource=request.resource,
            records=records,
            data_as_of=data_as_of,
        )

    @staticmethod
    def verify_webhook(_headers: dict[str, str], _body: bytes) -> VerifiedEvent:
        raise AlibabaIcbuError("Alibaba ICBU product connector has no webhook path")

    @staticmethod
    def execute(_action: ExternalAction) -> ExternalResult:
        raise AlibabaIcbuError("Alibaba ICBU connector is read-only")

    @staticmethod
    def verify(_action: ExternalAction, _result: ExternalResult) -> VerificationResult:
        raise AlibabaIcbuError("Alibaba ICBU connector is read-only")


class AlibabaIcbuIntegrationService:
    PLATFORM = "alibaba_icbu"

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        top_client: AlibabaIcbuTopClient | None = None,
    ):
        self.db = db
        self.settings = settings
        self.top = top_client or AlibabaIcbuTopClient(settings)

    @property
    def cipher(self) -> CredentialCipher:
        try:
            return CredentialCipher(
                self.settings.alibaba_icbu_credential_key,
                key_name="ALIBABA_ICBU_CREDENTIAL_KEY",
                associated_data=b"yunpai-alibaba-icbu-v1",
                credential_name="Alibaba ICBU",
            )
        except TaobaoError as exc:
            raise AlibabaIcbuError(str(exc)) from exc

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
        app_credentials = bool(
            self.settings.alibaba_icbu_app_key
            and self.settings.alibaba_icbu_app_secret
        )
        secure_store = self._cipher_configured()
        oauth = bool(
            app_credentials
            and self.settings.alibaba_icbu_redirect_uri
            and secure_store
        )
        authorized = int(row["authorized"] or 0)
        readable = bool(self.settings.alibaba_icbu_enabled and oauth and authorized > 0)
        return {
            "platform": self.PLATFORM,
            "enabled": self.settings.alibaba_icbu_enabled,
            "connections": {
                "total": int(row["total"] or 0),
                "authorized": authorized,
            },
            "official_contract": {
                "authorization": "OAuth2.0 Server-side, sp=icbu",
                "top_gateway": self.settings.alibaba_icbu_top_gateway,
                "read_methods": [
                    AlibabaIcbuConnector.PRODUCT_LIST,
                    AlibabaIcbuConnector.PRODUCT_DETAIL,
                    AlibabaIcbuConnector.PRODUCT_INVENTORY,
                ],
                "write_methods_enabled": False,
            },
            "capabilities": {
                "oauth_authorization": self._gate(
                    oauth, "AppKey/AppSecret、回调地址和独立凭据加密密钥"
                ),
                "catalog_read": self._gate(
                    readable, "启用接入并由具体商家完成 OAuth 授权"
                ),
                "inventory_read": self._gate(
                    readable, "启用接入并由具体商家完成 OAuth 授权"
                ),
                "domain_sync": self._gate(
                    False, "真实商家字段样本和价格语义尚未完成映射验收"
                ),
                "platform_write": self._gate(
                    False, "首期范围明确禁止商品、库存和上下架写操作"
                ),
            },
        }

    @staticmethod
    def _gate(available: bool, missing: str) -> dict[str, Any]:
        return {
            "available": available,
            "missing_when_unavailable": None if available else missing,
        }

    def _cipher_configured(self) -> bool:
        try:
            self.cipher
        except AlibabaIcbuError:
            return False
        return True

    def begin_authorization(self, tenant_id: str, store_id: str) -> dict[str, str]:
        if not self.capabilities(tenant_id)["capabilities"][
            "oauth_authorization"
        ]["available"]:
            raise AlibabaIcbuError("Alibaba ICBU OAuth is not fully configured")
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
                "response_type": "code",
                "client_id": self.settings.alibaba_icbu_app_key,
                "redirect_uri": self.settings.alibaba_icbu_redirect_uri,
                "state": state,
                "view": "web",
                "sp": "icbu",
            }
        )
        return {
            "authorization_url": (
                f"{self.settings.alibaba_icbu_oauth_authorize_url}?{query}"
            ),
            "state": state,
        }

    def complete_authorization(self, code: str, state: str) -> dict[str, Any]:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_oauth_states WHERE state_hash=? AND platform=?",
                (state_hash, self.PLATFORM),
            ).fetchone()
            if row is None or row["used_at"] is not None:
                raise AlibabaIcbuError(
                    "OAuth state is invalid or has already been used"
                )
            if datetime.fromisoformat(str(row["expires_at"])) < now:
                raise AlibabaIcbuError("OAuth state has expired")
            conn.execute(
                "UPDATE platform_oauth_states SET used_at=? WHERE state_hash=?",
                (now.isoformat(), state_hash),
            )
        token = self.top.exchange_authorization_code(code)
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise AlibabaIcbuRemoteError(
                "Alibaba ICBU OAuth response did not contain access_token"
            )
        account_id = str(
            token.get("taobao_user_id") or token.get("user_id") or ""
        ) or None
        raw_nick = str(
            token.get("taobao_user_nick") or token.get("user_nick") or ""
        )
        account_nick = unquote(raw_nick) if raw_nick else None
        expires_in = int(token.get("expires_in") or 0)
        expires_at = (
            (now + timedelta(seconds=expires_in)).isoformat()
            if expires_in > 0
            else None
        )
        try:
            encrypted = self.cipher.encrypt(token)
        except TaobaoError as exc:
            raise AlibabaIcbuError(str(exc)) from exc
        connection_id = f"connection-{uuid.uuid4().hex}"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_connections(
                    id, tenant_id, platform, shop_id, status, account_id, account_nick,
                    credential_ciphertext, token_expires_at, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'authorized', ?, ?, ?, ?, '{}', ?, ?)
                ON CONFLICT(tenant_id, platform, shop_id) DO UPDATE SET
                    status='authorized', account_id=excluded.account_id,
                    account_nick=excluded.account_nick,
                    credential_ciphertext=excluded.credential_ciphertext,
                    token_expires_at=excluded.token_expires_at, updated_at=excluded.updated_at
                """,
                (
                    connection_id,
                    row["tenant_id"],
                    self.PLATFORM,
                    row["shop_id"],
                    account_id,
                    account_nick,
                    encrypted,
                    expires_at,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            saved = conn.execute(
                """
                SELECT id, shop_id, status, account_id, account_nick, token_expires_at
                FROM platform_connections WHERE tenant_id=? AND platform=? AND shop_id=?
                """,
                (row["tenant_id"], self.PLATFORM, row["shop_id"]),
            ).fetchone()
        self.db.audit(
            "alibaba_icbu.oauth.authorized",
            "alibaba-icbu-oauth",
            str(saved["id"]),
            {"store_id": saved["shop_id"], "account_id": saved["account_id"]},
            str(row["tenant_id"]),
        )
        return dict(saved)

    def _connection_for_store(
        self, tenant_id: str, store_id: str
    ) -> Mapping[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM platform_connections
                WHERE tenant_id=? AND platform=? AND shop_id=? AND status='authorized'
                """,
                (tenant_id, self.PLATFORM, store_id),
            ).fetchone()
        if row is None:
            raise AlibabaIcbuError(
                "no authorized Alibaba ICBU connection matches this store"
            )
        return row

    def _access_token_for_store(self, tenant_id: str, store_id: str) -> str:
        connection = self._connection_for_store(tenant_id, store_id)
        try:
            token = self.cipher.decrypt(str(connection["credential_ciphertext"]))
        except TaobaoError as exc:
            raise AlibabaIcbuError(str(exc)) from exc
        expires_at_value = connection["token_expires_at"]
        if expires_at_value:
            expires_at = datetime.fromisoformat(str(expires_at_value))
            if expires_at <= datetime.now(UTC):
                try:
                    token = self._refresh_connection(connection, token)
                except (AlibabaIcbuError, AlibabaIcbuRemoteError) as exc:
                    self._mark_refresh_failure(connection, exc)
                    raise AlibabaIcbuRemoteError(
                        "Alibaba ICBU access token refresh failed; "
                        "the connection requires authorization again",
                        code=getattr(exc, "code", None),
                    ) from exc
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise AlibabaIcbuError("stored Alibaba ICBU credential has no access_token")
        return access_token

    def _refresh_connection(
        self, connection: Mapping[str, Any], token: Mapping[str, Any]
    ) -> dict[str, Any]:
        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            raise AlibabaIcbuError(
                "Alibaba ICBU access token expired and cannot be refreshed"
            )
        refreshed = self.top.refresh_access_token(refresh_token)
        access_token = str(refreshed.get("access_token") or "")
        if not access_token:
            raise AlibabaIcbuRemoteError(
                "Alibaba ICBU refresh response did not contain access_token"
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
        try:
            encrypted = self.cipher.encrypt(merged)
        except TaobaoError as exc:
            raise AlibabaIcbuError(str(exc)) from exc
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE platform_connections
                SET credential_ciphertext=?, token_expires_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    encrypted,
                    expires_at,
                    datetime.now(UTC).isoformat(),
                    connection["id"],
                ),
            )
        self.db.audit(
            "alibaba_icbu.oauth.refreshed",
            "alibaba-icbu-token-refresh",
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
                UPDATE platform_connections
                SET status='error', updated_at=?
                WHERE id=? AND status='authorized'
                """,
                (now, connection["id"]),
            )
        self.db.audit(
            "alibaba_icbu.oauth.refresh_failed",
            "alibaba-icbu-token-refresh",
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
    ) -> AlibabaIcbuConnector:
        return AlibabaIcbuConnector(
            self.top,
            access_token=self._access_token_for_store(tenant_id, store_id),
            store_id=store_id,
        )

    def list_products(
        self,
        tenant_id: str,
        *,
        store_id: str,
        cursor: str | None = None,
        limit: int = 30,
        gmt_modified_from: str | None = None,
        gmt_modified_to: str | None = None,
    ) -> PullBatch:
        window = _product_list_window(gmt_modified_from, gmt_modified_to)
        return self._connector_for_store(tenant_id, store_id).list_products(
            PullRequest(resource="catalog", cursor=cursor, limit=limit),
            **window,
        )

    def product_detail(
        self,
        tenant_id: str,
        *,
        store_id: str,
        encrypted_product_id: str,
    ) -> PullBatch:
        return self._connector_for_store(tenant_id, store_id).pull(
            PullRequest(
                resource="product_detail",
                cursor=encrypted_product_id,
                limit=1,
            )
        )

    def product_inventory(
        self,
        tenant_id: str,
        *,
        store_id: str,
        plain_product_id: str,
    ) -> PullBatch:
        return self._connector_for_store(tenant_id, store_id).pull(
            PullRequest(
                resource="inventory", cursor=plain_product_id, limit=500
            )
        )

    def close(self) -> None:
        self.top.close()
