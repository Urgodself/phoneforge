"""TextFree provider — STUB for v2."""
from __future__ import annotations

from .base import Provider, ProviderResult


class TextFreeProvider(Provider):
    name = "textfree"

    async def register(self, *, used_for: str = "") -> ProviderResult:
        raise NotImplementedError("TextFree provider is a v2 stub.")

    async def receive_sms(self, **kwargs) -> str:  # noqa: D401
        raise NotImplementedError("TextFree provider is a v2 stub.")
