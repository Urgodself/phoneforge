"""Provider plugins for PhoneForge.

Two flows coexist:

- Browser flow (TextNow): `register()` + `receive_sms()` via Camoufox.
- SMS-API flow (5sim): `provision()` + `fetch_sms()` via httpx.

Each concrete provider implements only the methods that make sense for it;
the others stay raising `NotImplementedError`.
"""
from .base import Provider, ProviderResult, ProvisionResult
from .sms5sim import (
    FiveSimAuthError,
    FiveSimError,
    FiveSimNoInventory,
    SMS5SimProvider,
)
from .textnow import TextNowProvider

PROVIDERS: dict[str, type[Provider]] = {
    "textnow": TextNowProvider,
    "5sim": SMS5SimProvider,
}


def get_provider(name: str) -> Provider:
    cls = PROVIDERS.get(name.lower())
    if cls is None:
        raise KeyError(f"Unknown provider: {name}. Available: {sorted(PROVIDERS)}")
    return cls()


__all__ = [
    "Provider",
    "ProviderResult",
    "ProvisionResult",
    "TextNowProvider",
    "SMS5SimProvider",
    "FiveSimError",
    "FiveSimAuthError",
    "FiveSimNoInventory",
    "get_provider",
    "PROVIDERS",
]
