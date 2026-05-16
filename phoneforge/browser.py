"""Camoufox launch helper for PhoneForge.

Design notes:
- We use AsyncCamoufox as a context manager (same idiom as yt-manager).
- WebGL pair: sampled from Camoufox's bundled webgl_data.db, filtered to Windows
  rows only (TextNow detects ANGLE strings vs Mozilla strings the same way
  CreepJS does — see feedback_camoufox_webgl_db.md).
- For re-login (`wait`) we MUST forward the same WebGL pair that was used at
  signup, otherwise the per-account fingerprint drifts between sessions.
- We don't persist a Firefox profile dir between requests — each TextNow account
  is one-shot. If TextNow demands persistent profile (rare), we'll add it.
"""
from __future__ import annotations

import logging
import random
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional, Tuple

from camoufox.async_api import AsyncCamoufox

from . import config

log = logging.getLogger(__name__)


def _camoufox_webgl_db_path() -> Path:
    """Locate Camoufox's bundled webgl_data.db inside the venv."""
    import camoufox  # late import — needs the package installed first

    pkg_root = Path(camoufox.__file__).resolve().parent
    db_path = pkg_root / "webgl" / "webgl_data.db"
    if not db_path.exists():
        raise RuntimeError(
            f"Camoufox webgl_data.db not found at {db_path}. "
            "Did you install camoufox[geoip]?"
        )
    return db_path


def sample_windows_webgl_pair() -> Tuple[str, str]:
    """Return one (vendor, renderer) pair from Camoufox's Windows pool.

    See feedback_camoufox_webgl_db.md: filter `win > 0`, expect ~17 unique
    pairs. Never feed Linux-format Mozilla strings to a Windows-spoofed launch.
    """
    db_path = _camoufox_webgl_db_path()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT vendor, renderer FROM webgl_fingerprints WHERE win > 0"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        raise RuntimeError("Camoufox webgl_data.db has no Windows-platform rows.")
    return random.choice(rows)


@asynccontextmanager
async def launch_browser(
    *,
    proxy_url: str,
    webgl_pair: Optional[Tuple[str, str]] = None,
    headless: Optional[bool] = None,
    user_data_dir: Optional[Path] = None,
) -> AsyncIterator[tuple]:
    """Yield (browser, context, webgl_pair_used).

    `proxy_url` MUST be a full residential proxy URL — we refuse to launch
    without one (no datacenter exposure, no real-IP leak).
    `webgl_pair`: pass in the locked pair when re-logging into an existing
    account; omit to sample a fresh one.
    """
    from .proxy import proxy_to_playwright_dict

    if not proxy_url or "://" not in proxy_url:
        raise ValueError("Refusing to launch Camoufox without a valid proxy URL.")

    if webgl_pair is None:
        webgl_pair = sample_windows_webgl_pair()
    proxy_dict = proxy_to_playwright_dict(proxy_url)

    is_headless = config.HEADLESS if headless is None else headless

    log.info(
        "Camoufox launch: headless=%s os=windows webgl=%s/%s proxy=%s",
        is_headless,
        webgl_pair[0][:30],
        webgl_pair[1][:40],
        proxy_dict["server"],
    )

    kwargs = dict(
        headless=is_headless,
        os="windows",
        humanize=True,
        block_webrtc=True,  # critical — WebRTC leak = real IP exposure
        proxy=proxy_dict,
        webgl_config=webgl_pair,
        geoip=True,
    )
    if user_data_dir is not None:
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = str(user_data_dir)

    async with AsyncCamoufox(**kwargs) as browser:
        if user_data_dir is not None:
            # In persistent_context mode AsyncCamoufox yields a BrowserContext
            # directly, not a Browser. Match that.
            context = browser
            yield browser, context, webgl_pair
        else:
            context = await browser.new_context()
            yield browser, context, webgl_pair
