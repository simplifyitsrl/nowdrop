"""PayPal REST client (Orders v2 + Payments v2) and an in-memory fake for tests.

Every call is reported through `on_call(method, path, status_code, debug_id)` so
the seller console can show the real PayPal activity.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

CallLogger = Callable[[str, str, int, str | None], None]


class PayPalError(Exception):
    def __init__(self, message: str, status_code: int = 0, debug_id: str | None = None, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.debug_id = debug_id
        self.details = details


class PayPalNotConfigured(PayPalError):
    pass


@dataclass
class Authorization:
    authorization_id: str
    status: str
    payer_name: str
    payer_email: str


def format_amount(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


class PayPalClient(Protocol):
    async def create_order(self, *, amount_cents: int, currency: str, drop_id: int, description: str) -> str: ...

    async def authorize_order(self, order_id: str, *, request_id: str) -> Authorization: ...

    async def capture_authorization(self, authorization_id: str, *, request_id: str) -> str: ...

    async def void_authorization(self, authorization_id: str, *, request_id: str) -> None: ...

    async def refund_capture(self, capture_id: str, *, request_id: str) -> str: ...


def _payer_from_order(body: dict) -> tuple[str, str]:
    payer = body.get("payer") or {}
    name = payer.get("name") or {}
    full = " ".join(p for p in (name.get("given_name"), name.get("surname")) if p).strip()
    return full or "PayPal buyer", payer.get("email_address") or ""


class RealPayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        on_call: CallLogger | None = None,
        http: httpx.AsyncClient | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.on_call = on_call
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _log(self, method: str, path: str, response: httpx.Response | None) -> None:
        if self.on_call is None:
            return
        status = response.status_code if response is not None else 0
        debug_id = response.headers.get("paypal-debug-id") if response is not None else None
        try:
            self.on_call(method, path, status, debug_id)
        except Exception:  # logging must never break a payment
            pass

    async def _access_token(self) -> str:
        if not (self.client_id and self.client_secret):
            raise PayPalNotConfigured("PayPal credentials are not configured")
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            path = "/v1/oauth2/token"
            try:
                resp = await self._http.post(
                    self.base_url + path,
                    headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
                    data={"grant_type": "client_credentials"},
                )
            except httpx.HTTPError as exc:
                self._log("POST", path, None)
                raise PayPalError(f"Could not reach PayPal: {exc}") from exc
            self._log("POST", path, resp)
            if resp.status_code != 200:
                raise PayPalError("PayPal authentication failed", resp.status_code,
                                  resp.headers.get("paypal-debug-id"), _safe_json(resp))
            body = resp.json()
            self._token = body["access_token"]
            # Refresh a minute before PayPal says the token expires.
            self._token_expires_at = time.monotonic() + max(60, int(body.get("expires_in", 3600)) - 60)
            return self._token

    async def _request(self, method: str, path: str, *, json: dict | None = None,
                       request_id: str | None = None) -> dict:
        token = await self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        try:
            resp = await self._http.request(method, self.base_url + path, headers=headers, json=json)
        except httpx.HTTPError as exc:
            self._log(method, path, None)
            raise PayPalError(f"Could not reach PayPal: {exc}") from exc
        self._log(method, path, resp)
        if resp.status_code == 401:
            self._token = None  # force a fresh token next time
        if resp.status_code >= 400:
            body = _safe_json(resp)
            message = (body or {}).get("message") or f"PayPal returned HTTP {resp.status_code}"
            raise PayPalError(message, resp.status_code, resp.headers.get("paypal-debug-id"), body)
        return _safe_json(resp) or {}

    async def create_order(self, *, amount_cents: int, currency: str, drop_id: int, description: str) -> str:
        body = await self._request("POST", "/v2/checkout/orders", json={
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "custom_id": str(drop_id),
                "description": description[:127],
                "amount": {"currency_code": currency, "value": format_amount(amount_cents)},
            }],
        })
        return body["id"]

    async def authorize_order(self, order_id: str, *, request_id: str) -> Authorization:
        body = await self._request("POST", f"/v2/checkout/orders/{order_id}/authorize",
                                   json={}, request_id=request_id)
        try:
            auth = body["purchase_units"][0]["payments"]["authorizations"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise PayPalError("PayPal did not return an authorization", details=body) from exc
        name, email = _payer_from_order(body)
        return Authorization(authorization_id=auth["id"], status=auth.get("status", ""),
                             payer_name=name, payer_email=email)

    async def capture_authorization(self, authorization_id: str, *, request_id: str) -> str:
        body = await self._request("POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                                   json={"final_capture": True}, request_id=request_id)
        if body.get("status") not in ("COMPLETED", "PENDING"):
            raise PayPalError(f"Capture status is {body.get('status')}", details=body)
        return body["id"]

    async def void_authorization(self, authorization_id: str, *, request_id: str) -> None:
        # Returns 204 No Content (or 200 with a body when Prefer is set).
        await self._request("POST", f"/v2/payments/authorizations/{authorization_id}/void",
                            request_id=request_id)

    async def refund_capture(self, capture_id: str, *, request_id: str) -> str:
        body = await self._request("POST", f"/v2/payments/captures/{capture_id}/refund",
                                   json={}, request_id=request_id)
        return body.get("id", "")


def _safe_json(resp: httpx.Response) -> dict | None:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


@dataclass
class FakePayPalClient:
    """Deterministic in-memory PayPal used by the test suite.

    `delay` adds random-ish latency so concurrent claims actually interleave.
    """

    on_call: CallLogger | None = None
    fail_capture: bool = False
    fail_authorize: bool = False
    delay: float = 0.0
    calls: list[tuple[str, str]] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    orders: dict[str, dict] = field(default_factory=dict)

    async def _call(self, method: str, path: str, status: int = 201, request_id: str | None = None) -> None:
        if self.delay:
            await asyncio.sleep(self.delay * (next(self._seq) % 5) / 5)
        self.calls.append((method, path))
        if request_id:
            self.request_ids.append(request_id)
        if self.on_call:
            self.on_call(method, path, status, f"fake-{len(self.calls)}")

    def count(self, kind: str) -> int:
        return sum(1 for _, path in self.calls if path.endswith("/" + kind) or path == kind)

    async def create_order(self, *, amount_cents: int, currency: str, drop_id: int, description: str) -> str:
        order_id = f"FAKEORDER{next(self._seq):06d}"
        self.orders[order_id] = {"amount_cents": amount_cents, "currency": currency, "drop_id": drop_id}
        await self._call("POST", "/v2/checkout/orders")
        return order_id

    async def authorize_order(self, order_id: str, *, request_id: str) -> Authorization:
        if self.fail_authorize:
            await self._call("POST", f"/v2/checkout/orders/{order_id}/authorize", 422, request_id)
            raise PayPalError("INSTRUMENT_DECLINED", 422)
        await self._call("POST", f"/v2/checkout/orders/{order_id}/authorize", 201, request_id)
        n = order_id.removeprefix("FAKEORDER")
        return Authorization(authorization_id=f"AUTH{n}", status="CREATED",
                             payer_name=f"Buyer {int(n)}", payer_email=f"buyer{int(n)}@example.com")

    async def capture_authorization(self, authorization_id: str, *, request_id: str) -> str:
        if self.fail_capture:
            await self._call("POST", f"/v2/payments/authorizations/{authorization_id}/capture", 422, request_id)
            raise PayPalError("AUTHORIZATION_VOIDED", 422)
        await self._call("POST", f"/v2/payments/authorizations/{authorization_id}/capture", 201, request_id)
        return "CAP" + authorization_id.removeprefix("AUTH")

    async def void_authorization(self, authorization_id: str, *, request_id: str) -> None:
        await self._call("POST", f"/v2/payments/authorizations/{authorization_id}/void", 204, request_id)

    async def refund_capture(self, capture_id: str, *, request_id: str) -> str:
        await self._call("POST", f"/v2/payments/captures/{capture_id}/refund", 201, request_id)
        return "REF" + capture_id.removeprefix("CAP")

