"""Centralised env loading — .env file is read once at import time.

We deliberately do NOT export the OPENAI_API_KEY into module-level constants;
callers fetch via `get_openai_key()` so the key never lands in a stack trace
that includes locals.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# Project root — works whether installed via `pip install -e .` or run from
# the repo directly. `__file__` is …/phoneforge/phoneforge/config.py, so
# parents[1] is the repo root.
ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DATA_DIR: Final[Path] = ROOT / "data"
DB_PATH: Final[Path] = DATA_DIR / "numbers.db"

# Load .env from repo root. override=False so real env vars still win, which
# matters when running in CI / containers where the env is the source of truth.
load_dotenv(ROOT / ".env", override=False)


def get_openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return key


def get_5sim_api_key() -> str:
    """5sim.net Bearer token. Never logged, never echoed."""
    key = os.environ.get("FIVESIM_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FIVESIM_API_KEY is not set. Get it from https://5sim.net/profile "
            "and put it in .env."
        )
    return key


def has_5sim_api_key() -> bool:
    """Cheap probe — used by CLI to surface a friendly error before importing
    the provider (which would also fail, but with a less user-friendly stack)."""
    return bool(os.environ.get("FIVESIM_API_KEY", "").strip())


# Models — overridable via env, sensible defaults baked in.
LLM_TEXT_MODEL: Final[str] = os.environ.get("PHONEFORGE_LLM_TEXT_MODEL", "gpt-4o-mini")
LLM_VISION_MODEL: Final[str] = os.environ.get("PHONEFORGE_LLM_VISION_MODEL", "gpt-4o")

# Proxy source.
PROXY_SOURCE: Final[str] = os.environ.get("PHONEFORGE_PROXY_SOURCE", "local").lower()
PROXY_SSH_HOST: Final[str] = os.environ.get("PHONEFORGE_PROXY_SSH_HOST", "ytm-vps")
PROXY_VPS_DB: Final[str] = os.environ.get(
    "PHONEFORGE_PROXY_VPS_DB", "/root/yt-manager/data/ytmanager.db"
)
PROXY_LOCAL_DB: Final[str] = os.environ.get(
    "PHONEFORGE_PROXY_LOCAL_DB",
    "/Users/aleksej/Desktop/Nothing/yt-manager/data/ytmanager.db",
)
PROXY_FORCE_AREA: Final[str] = os.environ.get("PHONEFORGE_PROXY_FORCE_AREA", "US")

# Camoufox.
HEADLESS: Final[bool] = os.environ.get("PHONEFORGE_HEADLESS", "false").lower() == "true"
LAUNCH_TIMEOUT_MS: Final[int] = int(
    os.environ.get("PHONEFORGE_LAUNCH_TIMEOUT_MS", "90000")
)

# TextNow flow.
MANUAL_SIGNUP: Final[bool] = (
    os.environ.get("PHONEFORGE_MANUAL_SIGNUP", "false").lower() == "true"
)

# 5sim.net SMS-API flow.
FIVESIM_BASE_URL: Final[str] = os.environ.get(
    "FIVESIM_BASE_URL", "https://5sim.net/v1"
)
FIVESIM_COUNTRY: Final[str] = os.environ.get("FIVESIM_COUNTRY", "usa").lower()
FIVESIM_OPERATOR: Final[str] = os.environ.get("FIVESIM_OPERATOR", "any").lower()
FIVESIM_POLL_INTERVAL_S: Final[float] = float(
    os.environ.get("FIVESIM_POLL_INTERVAL_S", "5.0")
)
FIVESIM_TIMEOUT_S: Final[int] = int(os.environ.get("FIVESIM_TIMEOUT_S", "300"))
FIVESIM_HTTP_TIMEOUT_S: Final[float] = float(
    os.environ.get("FIVESIM_HTTP_TIMEOUT_S", "30.0")
)
