"""Provider plugins for PhoneForge.

Each provider implements `register()` (mint a fresh account+number) and
`receive_sms()` (login to an existing account and pull inbox text).
"""
from .base import Provider, ProviderResult
from .textnow import TextNowProvider

PROVIDERS: dict[str, type[Provider]] = {
    "textnow": TextNowProvider,
}


def get_provider(name: str) -> Provider:
    cls = PROVIDERS.get(name.lower())
    if cls is None:
        raise KeyError(f"Unknown provider: {name}. Available: {sorted(PROVIDERS)}")
    return cls()


__all__ = ["Provider", "ProviderResult", "TextNowProvider", "get_provider", "PROVIDERS"]
