"""Abstract Provider interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Tuple


@dataclass
class ProviderResult:
    """Outcome of a `register()` call."""
    number: str
    account_email: str
    account_password: str
    identity: dict
    webgl_pair: Tuple[str, str] = ("", "")
    proxy_url: str = ""
    notes: str = ""
    extras: dict = field(default_factory=dict)


class Provider(ABC):
    """Provider plugin contract.

    A provider knows how to:
    - mint a fresh account on the upstream service and obtain a phone number
    - log back into that account and dump the SMS inbox as raw text
    """

    name: str = "abstract"

    @abstractmethod
    async def register(self, *, used_for: str = "") -> ProviderResult:
        """Mint a fresh account + number. Raise on failure."""

    @abstractmethod
    async def receive_sms(
        self,
        *,
        account_email: str,
        account_password: str,
        webgl_pair: Tuple[str, str],
        timeout_s: int = 180,
        service_hint: str = "",
    ) -> str:
        """Log in to the existing account and return the latest inbox snapshot
        as one big string (so the LLM can extract the code).
        Should poll up to `timeout_s` before giving up.
        Raise on login failure.
        """
