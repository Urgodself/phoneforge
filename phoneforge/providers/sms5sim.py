"""5sim.net provider — paid SMS-API with a REST interface.

Docs: https://docs.5sim.net/

Five endpoints we touch:
  GET /v1/user/profile                                          → balance + rating
  GET /v1/user/buy/activation/{country}/{operator}/{product}    → rent a number
  GET /v1/user/check/{id}                                       → poll for SMS
  GET /v1/user/finish/{id}                                      → close order (success)
  GET /v1/user/ban/{id}                                         → mark broken (refund)
  GET /v1/guest/products/{country}/{operator}                   → list services + prices

Auth: `Authorization: Bearer <API_KEY>` + `Accept: application/json`.

A few non-obvious facts that bit us during integration (worth keeping):

1. **No-inventory errors come back as plaintext, not JSON.** When `usa/any` has
   no free numbers for the requested product, 5sim returns HTTP 400 with body
   `no free phones` (or similar). Calling `.json()` on that will raise.
   We always inspect `.text` first and only parse JSON when status is 2xx.

2. **The buy endpoint is GET, not POST.** Their REST design predates the
   "POST creates resources" convention.

3. **Balance is in RUB**, not USD. We return `(amount, currency)` and let the
   CLI format it.

4. **`sms` array items already contain a parsed `code` field.** We prefer that
   over running our own LLM parse, both for cost and accuracy. We fall back
   to `llm.parse_sms_code` only if `code` is empty.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Tuple

import httpx

from .. import config, llm
from .base import ProvisionResult, Provider

log = logging.getLogger(__name__)


class FiveSimError(RuntimeError):
    """Wraps any failure to talk to 5sim.net cleanly."""


class FiveSimNoInventory(FiveSimError):
    """Raised when 5sim has no free numbers for the requested product/country."""


class FiveSimAuthError(FiveSimError):
    """API key invalid / revoked / not allowed for this endpoint."""


def _to_e164(raw: str) -> str:
    """5sim returns numbers in E.164 already (e.g. '+13105550199' or '13105550199').
    We always emit the leading '+'.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    return raw if raw.startswith("+") else f"+{raw}"


class SMS5SimProvider(Provider):
    name = "5sim"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        country: Optional[str] = None,
        operator: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        # Lazy fetch — lets the registry instantiate w/o env in tests that only
        # exercise the registry, not the network.
        self._api_key = api_key
        self.base_url = (base_url or config.FIVESIM_BASE_URL).rstrip("/")
        self.country = (country or config.FIVESIM_COUNTRY).lower()
        self.operator = (operator or config.FIVESIM_OPERATOR).lower()
        self._transport = transport  # test seam for httpx.MockTransport

    # ───────── HTTP plumbing ─────────

    def _auth_headers(self) -> dict:
        key = self._api_key or config.get_5sim_api_key()
        return {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._auth_headers(),
            timeout=config.FIVESIM_HTTP_TIMEOUT_S,
            transport=self._transport,
        )

    async def _get(self, path: str, *, anonymous: bool = False) -> dict:
        """GET helper that handles 5sim's mixed plaintext/JSON error responses."""
        headers = {"Accept": "application/json"} if anonymous else self._auth_headers()
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=config.FIVESIM_HTTP_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            try:
                resp = await client.get(path)
            except httpx.HTTPError as e:
                raise FiveSimError(f"HTTP error talking to 5sim: {e}") from e

        text = (resp.text or "").strip()

        if resp.status_code == 401:
            raise FiveSimAuthError(
                "5sim returned 401 — API key invalid or revoked."
            )
        if resp.status_code == 403:
            raise FiveSimAuthError(
                f"5sim returned 403 — forbidden. Body: {text[:200]!r}"
            )

        if resp.status_code >= 400:
            # 5sim error bodies are usually plaintext like 'no free phones',
            # 'order not found', 'bad country' …
            low = text.lower()
            if "no free phones" in low or "no number" in low:
                raise FiveSimNoInventory(
                    f"5sim: no inventory for the requested country/operator/product "
                    f"({self.country}/{self.operator}). Body: {text[:200]!r}"
                )
            raise FiveSimError(
                f"5sim HTTP {resp.status_code}: {text[:300]!r}"
            )

        # 2xx → JSON expected. If body is empty (e.g. finish/ban can return
        # an empty success), return {}.
        if not text:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise FiveSimError(
                f"5sim returned non-JSON success body: {text[:200]!r}"
            ) from e

    # ───────── public API ─────────

    async def check_balance(self) -> Tuple[float, str]:
        """Read /v1/user/profile. 5sim returns balance in RUB."""
        data = await self._get("/user/profile")
        balance = float(data.get("balance", 0.0) or 0.0)
        # 5sim profile responses don't include a currency field — it's
        # always RUB by their docs.
        return balance, "RUB"

    async def list_services(self, country: str = "", operator: str = "") -> list[dict]:
        """List available products + prices for a country/operator.

        Returns a list of dicts: [{"name": "google", "price": 9.0, "count": 4521}, …]
        """
        c = (country or self.country).lower()
        o = (operator or self.operator).lower()
        # Public endpoint — Authorization optional but anonymous=True keeps
        # the API key out of an unnecessary request.
        data = await self._get(f"/guest/products/{c}/{o}", anonymous=True)
        if not isinstance(data, dict):
            return []
        out: list[dict] = []
        for name, info in data.items():
            if not isinstance(info, dict):
                continue
            out.append({
                "name": name,
                "price": float(info.get("Price", 0) or 0),
                "count": int(info.get("Qty", 0) or 0),
                "category": info.get("Category", ""),
            })
        # Sort by stock (descending) then price (ascending) — most useful first.
        out.sort(key=lambda x: (-x["count"], x["price"]))
        return out

    async def provision(self, *, service: str) -> ProvisionResult:
        """Rent a number for a specific service. Returns ProvisionResult.

        Raises FiveSimNoInventory when 5sim has no free numbers (caller can
        try a different operator / country / service).
        """
        if not service:
            raise ValueError("service must be a non-empty 5sim product slug, e.g. 'youtube'")

        path = f"/user/buy/activation/{self.country}/{self.operator}/{service}"
        data = await self._get(path)

        order_id_raw = data.get("id")
        if order_id_raw is None:
            raise FiveSimError(f"5sim buy response missing 'id': {data!r}")
        order_id = str(order_id_raw)

        phone = _to_e164(str(data.get("phone", "")))
        if not phone:
            raise FiveSimError(f"5sim buy response missing 'phone': {data!r}")

        # 5sim returns expires as ISO-8601 UTC string. We don't strictly need
        # to parse it — we store it raw and let CLI render it.
        expires_at: Optional[float] = None
        expires_iso = data.get("expires")
        if expires_iso:
            try:
                from datetime import datetime
                # Handle the trailing 'Z' that 5sim sometimes emits.
                iso = expires_iso.replace("Z", "+00:00")
                expires_at = datetime.fromisoformat(iso).timestamp()
            except Exception:
                expires_at = None

        return ProvisionResult(
            number=phone,
            provider_order_id=str(order_id),
            service=service,
            country=self.country,
            operator=self.operator,
            price=float(data.get("price", 0.0) or 0.0),
            currency="RUB",
            expires_at=expires_at,
            raw=data,
        )

    async def fetch_sms(
        self,
        provider_order_id: str,
        *,
        timeout_s: int = 300,
        poll_interval_s: Optional[float] = None,
        service_hint: str = "",
    ) -> Optional[str]:
        """Poll /user/check/{id} until SMS arrives or timeout expires.

        Returns the verification code (digits), or None on timeout.
        5sim usually populates `sms[].code` already; we fall back to
        running `llm.parse_sms_code(text)` only when the upstream code
        field is empty.
        """
        interval = poll_interval_s if poll_interval_s is not None else config.FIVESIM_POLL_INTERVAL_S
        deadline = time.monotonic() + timeout_s
        last_status = ""

        while True:
            data = await self._get(f"/user/check/{provider_order_id}")
            sms = data.get("sms") or []
            status = str(data.get("status", "") or "")

            if status != last_status:
                log.info("5sim order %s status=%s", provider_order_id, status)
                last_status = status

            # If any SMS has arrived, return the first usable code.
            if isinstance(sms, list) and sms:
                first = sms[0] if isinstance(sms[0], dict) else {}
                code = (first.get("code") or "").strip()
                if code:
                    return code
                # Fallback: parse the body ourselves.
                text = (first.get("text") or "").strip()
                if text:
                    parsed = llm.parse_sms_code(text, service_hint=service_hint or None)
                    if parsed:
                        return parsed

            # Terminal upstream states — no point polling further.
            if status in {"CANCELED", "BANNED", "TIMEOUT"}:
                log.warning(
                    "5sim order %s is in terminal status %s — stopping poll",
                    provider_order_id, status,
                )
                return None

            if time.monotonic() >= deadline:
                return None

            await asyncio.sleep(interval)

    async def finish(self, provider_order_id: str) -> None:
        """Close the order successfully. Idempotent on the 5sim side."""
        await self._get(f"/user/finish/{provider_order_id}")

    async def ban(self, provider_order_id: str, reason: str = "") -> None:
        """Mark the order as broken — 5sim refunds if no SMS was received.
        `reason` is logged locally; 5sim's endpoint doesn't accept a reason
        body. Kept in the signature so callers can record intent.
        """
        if reason:
            log.info("Banning 5sim order %s: %s", provider_order_id, reason)
        await self._get(f"/user/ban/{provider_order_id}")
