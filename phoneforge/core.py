"""Orchestrator — picks a provider, runs the right flow, persists to DB.

Two flows live side-by-side:

- **Browser flow** (TextNow-style): `provider.register()` mints a fresh
  account, we save credentials, later `provider.receive_sms()` logs back in
  and reads the inbox. Stays compatible with the existing CLI and DB rows.

- **SMS-API flow** (5sim-style): `provider.provision(service)` rents a
  number, we save the upstream order id, later `provider.fetch_sms(order_id)`
  polls upstream until SMS arrives. On success we call `finish()`; on
  timeout the caller (CLI) decides whether to `ban()` for a refund.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from . import config, db, llm
from .providers import (
    FiveSimNoInventory,
    Provider,
    ProvisionResult,
    SMS5SimProvider,
    get_provider,
)
from .providers.base import ProviderResult

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = "5sim"


@dataclass
class WaitOutcome:
    """Result of `wait_for_sms`. CLI consumes this to render output."""
    code: str = ""
    snapshot: str = ""
    provider: str = ""
    provider_order_id: str = ""
    timed_out: bool = False
    finished: bool = False   # True if we successfully called provider.finish()


async def provision_number(
    *,
    service: str,
    provider_name: str = DEFAULT_PROVIDER,
    country: str = "",
    operator: str = "",
) -> str:
    """Mint a brand-new number, persist it, return the E.164 string.

    Routes to the right flow based on provider type:
      - 5sim-style (`provision`)   → SMS-API flow
      - TextNow-style (`register`) → browser flow
    """
    db.init()  # idempotent
    provider = _instantiate_provider(provider_name, country=country, operator=operator)

    # Distinguish flows by which method is overridden (clean duck-typing).
    # SMS-API path: any provider that overrides `provision` (i.e. doesn't
    # raise NotImplementedError on the base class default).
    if isinstance(provider, SMS5SimProvider):
        log.info(
            "Provisioning a 5sim number for service=%s country=%s operator=%s",
            service, provider.country, provider.operator,
        )
        result = await provider.provision(service=service)
        db.insert_number(
            number=result.number,
            provider=provider.name,
            account_email="",            # not applicable for SMS-API rentals
            account_password="",
            identity={
                "country": result.country,
                "operator": result.operator,
                "service": result.service,
                "price": result.price,
                "currency": result.currency,
                "expires_at": result.expires_at,
            },
            proxy_str="",                # 5sim sits on their own infra
            used_for=service,
            notes=f"5sim order={result.provider_order_id}",
            provider_order_id=result.provider_order_id,
        )
        return result.number

    # Browser flow (TextNow).
    log.info("Provisioning a fresh number from %s for service=%s", provider_name, service)
    result_b: ProviderResult = await provider.register(used_for=service)
    db.insert_number(
        number=result_b.number,
        provider=provider_name,
        account_email=result_b.account_email,
        account_password=result_b.account_password,
        identity=result_b.identity,
        webgl_vendor=result_b.webgl_pair[0] if result_b.webgl_pair else "",
        webgl_renderer=result_b.webgl_pair[1] if result_b.webgl_pair else "",
        proxy_str=result_b.proxy_url,
        used_for=service,
        notes=result_b.notes,
    )
    return result_b.number


async def wait_for_sms(
    *,
    number: str,
    service_hint: str = "",
    timeout_s: Optional[int] = None,
) -> WaitOutcome:
    """Wait for an SMS code for the given number, regardless of provider flow.

    Returns a WaitOutcome — the CLI decides how to render success/timeout
    and (for 5sim) whether to call ban() for a refund.
    """
    db.init()
    row = db.get_by_number(number)
    if row is None:
        raise KeyError(f"Number not in DB: {number}")
    if row["status"] != "active":
        raise RuntimeError(f"Number {number} status={row['status']} — refusing to use.")

    provider_name = row["provider"]
    provider = _instantiate_provider(provider_name)

    # SMS-API path?
    if isinstance(provider, SMS5SimProvider):
        order_id = (row["provider_order_id"] or "").strip()
        if not order_id:
            raise RuntimeError(
                f"Number {number} is marked as 5sim but has no provider_order_id "
                f"stored. Can't poll without an order id — re-provision."
            )
        eff_timeout = timeout_s if timeout_s is not None else config.FIVESIM_TIMEOUT_S
        code = await provider.fetch_sms(
            order_id,
            timeout_s=eff_timeout,
            service_hint=service_hint,
        )
        outcome = WaitOutcome(
            code=code or "",
            snapshot=f"5sim order {order_id}",
            provider=provider_name,
            provider_order_id=order_id,
            timed_out=(code is None),
        )
        if code:
            db.log_sms(int(row["id"]), raw_text=f"5sim order {order_id}", parsed_code=code)
            try:
                await provider.finish(order_id)
                outcome.finished = True
            except Exception as e:  # noqa: BLE001
                log.warning("finish() failed for order %s: %s", order_id, e)
            db.touch_last_used(number, used_for=service_hint or row["used_for"])
        return outcome

    # Browser flow (TextNow).
    webgl_pair: Tuple[str, str] = (
        row["webgl_vendor"] or "",
        row["webgl_renderer"] or "",
    )
    snapshot = await provider.receive_sms(
        account_email=row["account_email"],
        account_password=row["account_password"],
        webgl_pair=webgl_pair,
        service_hint=service_hint,
    )
    code = llm.parse_sms_code(snapshot, service_hint=service_hint)
    db.log_sms(int(row["id"]), snapshot, parsed_code=code)
    db.touch_last_used(number, used_for=service_hint or row["used_for"])
    return WaitOutcome(
        code=code,
        snapshot=snapshot,
        provider=provider_name,
        provider_order_id="",
        timed_out=not bool(code),
    )


async def ban_order(*, number: str, reason: str = "") -> None:
    """Ban a 5sim order to request a refund — exposed for CLI's NO_CODE branch."""
    row = db.get_by_number(number)
    if row is None:
        raise KeyError(f"Number not in DB: {number}")
    order_id = (row["provider_order_id"] or "").strip()
    if not order_id:
        raise RuntimeError(f"Number {number} has no provider_order_id to ban.")
    provider = _instantiate_provider(row["provider"])
    if not isinstance(provider, SMS5SimProvider):
        raise RuntimeError(f"Provider {row['provider']} does not support ban().")
    await provider.ban(order_id, reason=reason)
    db.mark_status(number, "burned", note=f"5sim ban: {reason or 'no_code_timeout'}")


def _instantiate_provider(
    name: str,
    *,
    country: str = "",
    operator: str = "",
) -> Provider:
    """Like `get_provider`, but lets us pass 5sim-specific overrides."""
    if name.lower() == "5sim":
        return SMS5SimProvider(
            country=country or None,
            operator=operator or None,
        )
    return get_provider(name)
