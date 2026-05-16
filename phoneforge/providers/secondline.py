"""SecondLine provider — STUB for v2."""
from __future__ import annotations

from .base import Provider, ProviderResult


class SecondLineProvider(Provider):
    name = "secondline"

    async def register(self, *, used_for: str = "") -> ProviderResult:
        raise NotImplementedError("SecondLine provider is a v2 stub.")

    async def receive_sms(self, **kwargs) -> str:  # noqa: D401
        raise NotImplementedError("SecondLine provider is a v2 stub.")
