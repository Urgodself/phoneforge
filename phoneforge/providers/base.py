"""Abstract Provider interface.

Two distinct flows are supported:

1. **Browser flow** (e.g. TextNow): mint a fresh account on the upstream
   service through Camoufox, store credentials, log back in later to read
   the SMS inbox. Implemented via `register()` + `receive_sms()`.

2. **SMS-API flow** (e.g. 5sim.net): pay an upstream aggregator who already
   owns the numbers, rent a number for a specific service, poll their REST
   API for delivered SMS, finish/ban the order. Implemented via
   `provision()` + `fetch_sms()` + `finish()` + `ban()` + `check_balance()`.

Concrete providers implement whichever flow fits — methods they don't
support default to `NotImplementedError`, so callers can probe support
explicitly. We deliberately do NOT use `@abstractmethod` for these, because
forcing every provider to stub out the unrelated flow just to instantiate
the class is uglier than a clear runtime error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple


@dataclass
class ProviderResult:
    """Outcome of a browser-flow `register()` call (TextNow-style)."""
    number: str
    account_email: str
    account_password: str
    identity: dict
    webgl_pair: Tuple[str, str] = ("", "")
    proxy_url: str = ""
    notes: str = ""
    extras: dict = field(default_factory=dict)


@dataclass
class ProvisionResult:
    """Outcome of an SMS-API `provision()` call (5sim-style).

    `provider_order_id` is the upstream identifier we'll need to poll, finish
    or ban the rental. `expires_at` is unix-ts when the upstream will auto-
    release the number if we never call finish().
    """
    number: str            # E.164, e.g. "+13105550199"
    provider_order_id: str  # upstream order id, e.g. "5sim:12345678"
    service: str           # service slug ("youtube", "google", …)
    country: str = ""
    operator: str = ""
    price: float = 0.0
    currency: str = ""     # "RUB" for 5sim, etc.
    expires_at: Optional[float] = None  # unix ts
    raw: dict = field(default_factory=dict)  # full upstream response


class Provider:
    """Base provider class. Concrete providers override whichever flow they
    support; unsupported methods stay raising NotImplementedError.
    """

    name: str = "abstract"

    # ───────── browser flow (TextNow) ─────────

    async def register(self, *, used_for: str = "") -> ProviderResult:
        """Mint a fresh account + number through a browser. Raise on failure."""
        raise NotImplementedError(
            f"{self.name} does not support browser-based registration. "
            f"Use the SMS-API flow (provision/fetch_sms) instead."
        )

    async def receive_sms(
        self,
        *,
        account_email: str,
        account_password: str,
        webgl_pair: Tuple[str, str],
        timeout_s: int = 180,
        service_hint: str = "",
    ) -> str:
        """Log in to an existing account and return the latest inbox snapshot.
        Browser-flow providers override this.
        """
        raise NotImplementedError(
            f"{self.name} does not support browser-based inbox reading. "
            f"Use fetch_sms() instead."
        )

    # ───────── SMS-API flow (5sim) ─────────

    async def provision(self, *, service: str) -> ProvisionResult:
        """Rent a number from the upstream API for the given service."""
        raise NotImplementedError(
            f"{self.name} does not support API-based provisioning. "
            f"Use register() (browser flow) instead."
        )

    async def fetch_sms(self, provider_order_id: str, *, timeout_s: int = 300) -> Optional[str]:
        """Poll upstream until SMS arrives or timeout. Returns the parsed code,
        or None on timeout. Raises on API errors.
        """
        raise NotImplementedError(
            f"{self.name} does not support API-based SMS polling. "
            f"Use receive_sms() instead."
        )

    async def finish(self, provider_order_id: str) -> None:
        """Mark the rental successfully completed. No refund."""
        raise NotImplementedError(f"{self.name} does not implement finish().")

    async def ban(self, provider_order_id: str, reason: str = "") -> None:
        """Mark the rental as broken — upstream typically refunds if no SMS arrived."""
        raise NotImplementedError(f"{self.name} does not implement ban().")

    async def check_balance(self) -> Tuple[float, str]:
        """Return (amount, currency_code). 5sim returns ("RUB",)."""
        raise NotImplementedError(f"{self.name} does not implement check_balance().")

    async def list_services(self, country: str = "", operator: str = "") -> list[dict]:
        """List available services + their prices for a country/operator."""
        raise NotImplementedError(f"{self.name} does not implement list_services().")
