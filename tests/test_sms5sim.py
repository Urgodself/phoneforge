"""Tests for the 5sim provider — fully offline via httpx.MockTransport.

We mock every HTTP call so these tests:
- don't burn credit on a real 5sim account
- don't depend on 5sim's availability / inventory
- run in milliseconds

The test seam is `SMS5SimProvider(transport=...)` — passing a MockTransport
swaps out the real network without touching the public API.
"""
from __future__ import annotations

import json
import re
from typing import Callable

import httpx
import pytest

from phoneforge.providers.base import ProvisionResult
from phoneforge.providers.sms5sim import (
    FiveSimAuthError,
    FiveSimError,
    FiveSimNoInventory,
    SMS5SimProvider,
)


def _mock(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _provider_with(transport: httpx.MockTransport, **kw) -> SMS5SimProvider:
    """Construct a provider that always uses our mock transport + dummy key."""
    return SMS5SimProvider(api_key="dummy-test-key", transport=transport, **kw)


# ────────────────────────── balance ──────────────────────────


@pytest.mark.asyncio
async def test_balance_returns_amount_and_currency_rub() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/user/profile"
        assert req.headers.get("Authorization") == "Bearer dummy-test-key"
        return httpx.Response(200, json={"balance": 12.34, "rating": 96, "id": 1})

    provider = _provider_with(_mock(handler))
    amount, currency = await provider.check_balance()
    assert amount == pytest.approx(12.34)
    assert currency == "RUB"


@pytest.mark.asyncio
async def test_balance_401_raises_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    provider = _provider_with(_mock(handler))
    with pytest.raises(FiveSimAuthError):
        await provider.check_balance()


# ────────────────────────── provision ──────────────────────────


@pytest.mark.asyncio
async def test_provision_returns_valid_result() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/user/buy/activation/usa/any/google"
        return httpx.Response(
            200,
            json={
                "id": 998877,
                "phone": "+13105550199",
                "operator": "verizon",
                "product": "google",
                "price": 12.0,
                "status": "PENDING",
                "expires": "2026-05-16T12:00:00Z",
                "country": "usa",
            },
        )

    provider = _provider_with(_mock(handler), country="usa", operator="any")
    result = await provider.provision(service="google")
    assert isinstance(result, ProvisionResult)
    assert result.number == "+13105550199"
    assert result.provider_order_id == "998877"
    assert result.service == "google"
    assert result.country == "usa"
    assert result.price == pytest.approx(12.0)
    assert result.currency == "RUB"
    assert result.expires_at is not None


@pytest.mark.asyncio
async def test_provision_normalises_unprefixed_phone_to_e164() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "1", "phone": "13105550111"})

    provider = _provider_with(_mock(handler))
    result = await provider.provision(service="google")
    assert result.number == "+13105550111"


@pytest.mark.asyncio
async def test_provision_no_inventory_plaintext_400_raises_no_inventory() -> None:
    """5sim returns plaintext 'no free phones' on no-inventory; must not crash on .json()."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="no free phones")

    provider = _provider_with(_mock(handler))
    with pytest.raises(FiveSimNoInventory):
        await provider.provision(service="google")


@pytest.mark.asyncio
async def test_provision_unknown_400_raises_generic_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad country")

    provider = _provider_with(_mock(handler))
    with pytest.raises(FiveSimError) as e:
        await provider.provision(service="google")
    assert not isinstance(e.value, FiveSimNoInventory)


@pytest.mark.asyncio
async def test_provision_empty_service_rejected_client_side() -> None:
    """We don't even hit the network if `service` is empty — fail fast."""
    provider = _provider_with(_mock(lambda req: httpx.Response(500)))
    with pytest.raises(ValueError):
        await provider.provision(service="")


# ────────────────────────── fetch_sms ──────────────────────────


@pytest.mark.asyncio
async def test_fetch_sms_returns_code_from_first_sms() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/user/check/12345"
        return httpx.Response(
            200,
            json={
                "id": 12345,
                "status": "RECEIVED",
                "sms": [
                    {
                        "id": 1,
                        "code": "482910",
                        "text": "Your Google verification code is 482910",
                        "date": "2026-05-16T12:00:00Z",
                    }
                ],
            },
        )

    provider = _provider_with(_mock(handler))
    # poll_interval_s=0 so the test doesn't sleep
    code = await provider.fetch_sms("12345", timeout_s=5, poll_interval_s=0)
    assert code == "482910"


@pytest.mark.asyncio
async def test_fetch_sms_polls_then_succeeds(monkeypatch) -> None:
    """First call returns pending, second returns the code."""
    state = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(200, json={"id": 1, "status": "PENDING", "sms": []})
        return httpx.Response(
            200,
            json={
                "id": 1,
                "status": "RECEIVED",
                "sms": [{"code": "111222", "text": "Code: 111222"}],
            },
        )

    provider = _provider_with(_mock(handler))
    code = await provider.fetch_sms("1", timeout_s=5, poll_interval_s=0)
    assert code == "111222"
    assert state["calls"] == 2


@pytest.mark.asyncio
async def test_fetch_sms_timeout_returns_none() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1, "status": "PENDING", "sms": []})

    provider = _provider_with(_mock(handler))
    # Tiny timeout — should give up almost immediately.
    code = await provider.fetch_sms("1", timeout_s=0, poll_interval_s=0)
    assert code is None


@pytest.mark.asyncio
async def test_fetch_sms_terminal_canceled_status_short_circuits() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1, "status": "CANCELED", "sms": []})

    provider = _provider_with(_mock(handler))
    code = await provider.fetch_sms("1", timeout_s=60, poll_interval_s=0)
    assert code is None


@pytest.mark.asyncio
async def test_fetch_sms_uses_code_field_over_text_parsing() -> None:
    """If 5sim already gave us `code`, we trust it and don't call the LLM."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 1,
                "status": "RECEIVED",
                "sms": [
                    {
                        "code": "999000",
                        "text": "Your code is 12345 actually no it's 67890",
                    }
                ],
            },
        )

    provider = _provider_with(_mock(handler))
    code = await provider.fetch_sms("1", timeout_s=5, poll_interval_s=0)
    # Trust the upstream-parsed code, not whatever the text could've yielded.
    assert code == "999000"


# ────────────────────────── finish / ban ──────────────────────────


@pytest.mark.asyncio
async def test_finish_hits_right_endpoint() -> None:
    seen = {"path": ""}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, text="")  # 5sim returns empty body sometimes

    provider = _provider_with(_mock(handler))
    await provider.finish("777")
    assert seen["path"] == "/v1/user/finish/777"


@pytest.mark.asyncio
async def test_ban_hits_right_endpoint() -> None:
    seen = {"path": ""}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, text="")

    provider = _provider_with(_mock(handler))
    await provider.ban("777", reason="no sms")
    assert seen["path"] == "/v1/user/ban/777"


# ────────────────────────── list_services ──────────────────────────


@pytest.mark.asyncio
async def test_list_services_parses_and_sorts() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/guest/products/usa/any"
        return httpx.Response(
            200,
            json={
                "google":   {"Price": 9, "Qty": 1500, "Category": "activation"},
                "youtube":  {"Price": 10, "Qty": 0, "Category": "activation"},
                "telegram": {"Price": 12, "Qty": 50, "Category": "activation"},
            },
        )

    provider = _provider_with(_mock(handler), country="usa", operator="any")
    rows = await provider.list_services()
    # Sorted by stock desc, then price asc.
    assert rows[0]["name"] == "google"
    assert rows[1]["name"] == "telegram"
    assert rows[2]["name"] == "youtube"
    assert rows[0]["count"] == 1500
    assert rows[0]["price"] == pytest.approx(9.0)


# ────────────────────────── registry ──────────────────────────


def test_registry_exposes_5sim() -> None:
    from phoneforge.providers import PROVIDERS
    assert "5sim" in PROVIDERS
    assert PROVIDERS["5sim"] is SMS5SimProvider
