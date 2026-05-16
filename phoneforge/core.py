"""Orchestrator — picks a provider, runs register/receive_sms, persists to DB."""
from __future__ import annotations

import json
import logging
from typing import Optional, Tuple

from . import db, llm
from .providers import get_provider

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = "textnow"


async def provision_number(
    *,
    service: str,
    provider_name: str = DEFAULT_PROVIDER,
) -> str:
    """Mint a brand-new number, persist it, return the E.164 string."""
    provider = get_provider(provider_name)
    log.info("Provisioning a fresh number from %s for service=%s", provider_name, service)
    result = await provider.register(used_for=service)

    db.init()  # idempotent
    db.insert_number(
        number=result.number,
        provider=provider_name,
        account_email=result.account_email,
        account_password=result.account_password,
        identity=result.identity,
        webgl_vendor=result.webgl_pair[0] if result.webgl_pair else "",
        webgl_renderer=result.webgl_pair[1] if result.webgl_pair else "",
        proxy_str=result.proxy_url,
        used_for=service,
        notes=result.notes,
    )
    return result.number


async def wait_for_sms(*, number: str, service_hint: str = "") -> str:
    """Log in to the stored account, poll inbox, extract OTP via LLM."""
    row = db.get_by_number(number)
    if row is None:
        raise KeyError(f"Number not in DB: {number}")
    if row["status"] != "active":
        raise RuntimeError(f"Number {number} status={row['status']} — refusing to use.")

    provider = get_provider(row["provider"])
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
    # Log it regardless of whether we parsed a code — useful for forensics.
    db.log_sms(int(row["id"]), snapshot, parsed_code=code)
    db.touch_last_used(number, used_for=service_hint or row["used_for"])
    return code
