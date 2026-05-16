"""Fetch a US residential proxy from yt-manager's pool.

We read the live `accounts.proxy` column from yt-manager's SQLite, pick a
Smartproxy template, and rewrite the `area-XX` segment to `area-US` plus a
fresh random `session-XXX` so we get a sticky-but-unique exit IP per request.

We do NOT mutate yt-manager's DB. Read-only.

Two source modes (set via PHONEFORGE_PROXY_SOURCE env):
- "local": read /Users/aleksej/Desktop/Nothing/yt-manager/data/ytmanager.db directly
- "ssh"  : `ssh <host> "sqlite3 <vps_db> '...'"`
"""
from __future__ import annotations

import logging
import random
import re
import secrets
import shlex
import sqlite3
import string
import subprocess
from dataclasses import dataclass
from typing import Optional

from . import config

log = logging.getLogger(__name__)

# Smartproxy URL pattern. Example we observed:
#   http://smart-a5f48o6s6v3y_area-IT_life-120_session-Exe1qdiLk:PASS@proxy.smartproxy.com:7000
# - smart-<account>
# - _area-<COUNTRY>
# - _life-<minutes>   (session TTL — we keep it)
# - _session-<id>     (sticky token — we randomise per request so we don't
#                      collide with yt-manager's live sessions)
SMARTPROXY_RE = re.compile(
    r"""
    ^(?P<scheme>https?://)
    (?P<user>smart-[a-zA-Z0-9]+)        # smartproxy account id
    _area-(?P<area>[A-Z]{2})            # country code
    _life-(?P<life>\d+)                 # session lifetime
    _session-(?P<session>[a-zA-Z0-9]+)  # session token
    :(?P<password>[^@]+)                # password
    @(?P<host>[^:]+):(?P<port>\d+)      # endpoint
    $
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class ProxyTemplate:
    """Parsed Smartproxy template we can re-issue with a fresh session/area."""

    scheme: str
    user: str
    area: str
    life: str
    password: str
    host: str
    port: int

    def with_session(self, area: str, session: str) -> str:
        return (
            f"{self.scheme}{self.user}_area-{area}_life-{self.life}_session-{session}"
            f":{self.password}@{self.host}:{self.port}"
        )


def _random_session_id(n: int = 10) -> str:
    """Match Smartproxy session token format (alnum, ~10 chars)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _fetch_proxies_local() -> list[str]:
    db_path = config.PROXY_LOCAL_DB
    log.info("Reading proxy pool from local DB: %s", db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        cur = conn.execute(
            "SELECT proxy FROM accounts WHERE proxy IS NOT NULL AND length(proxy) > 10"
        )
        return [row[0] for row in cur.fetchall() if row[0]]
    finally:
        conn.close()


def _fetch_proxies_ssh() -> list[str]:
    host = config.PROXY_SSH_HOST
    db = config.PROXY_VPS_DB
    log.info("Reading proxy pool via SSH: %s:%s", host, db)
    # Use sqlite3 on VPS. -separator '|' is implicit; we ask for raw single
    # column so the output is one proxy URL per line.
    sql = "SELECT proxy FROM accounts WHERE proxy IS NOT NULL AND length(proxy) > 10;"
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        f"sqlite3 {shlex.quote(db)} {shlex.quote(sql)}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"SSH proxy fetch failed (rc={result.returncode}): {result.stderr.strip()[:200]}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _fetch_pool() -> list[str]:
    if config.PROXY_SOURCE == "ssh":
        return _fetch_proxies_ssh()
    return _fetch_proxies_local()


def _parse_smartproxy(url: str) -> Optional[ProxyTemplate]:
    m = SMARTPROXY_RE.match(url)
    if not m:
        return None
    return ProxyTemplate(
        scheme=m.group("scheme"),
        user=m.group("user"),
        area=m.group("area"),
        life=m.group("life"),
        password=m.group("password"),
        host=m.group("host"),
        port=int(m.group("port")),
    )


def get_us_proxy(force_new_session: bool = True) -> str:
    """Return a Smartproxy URL pinned to US area with a fresh sticky session.

    Raises RuntimeError if the pool is empty or no Smartproxy templates are
    parseable — we deliberately do NOT silently fall back to a non-residential
    proxy because TextNow flags datacenter ranges aggressively.
    """
    pool = _fetch_pool()
    if not pool:
        raise RuntimeError(
            "Empty proxy pool from yt-manager. Check PHONEFORGE_PROXY_SOURCE "
            "and that accounts.proxy is populated."
        )

    # Parse all and dedup by Smartproxy account/template — different rows
    # mostly share the same credentials, just different area+session.
    templates: list[ProxyTemplate] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in pool:
        tpl = _parse_smartproxy(raw)
        if not tpl:
            continue
        key = (tpl.user, tpl.host, tpl.password)
        if key in seen:
            continue
        seen.add(key)
        templates.append(tpl)

    if not templates:
        raise RuntimeError(
            "Pool has proxies but none match the Smartproxy template — "
            "extend phoneforge/proxy.py with the new provider."
        )

    tpl = random.choice(templates)
    area = config.PROXY_FORCE_AREA or "US"
    session = _random_session_id() if force_new_session else "phoneforge"
    return tpl.with_session(area=area, session=session)


def proxy_to_playwright_dict(proxy_url: str) -> dict:
    """Convert `http://user:pass@host:port` → Camoufox/Playwright proxy dict."""
    m = re.match(
        r"^(https?)://([^:]+):([^@]+)@([^:]+):(\d+)$", proxy_url
    )
    if not m:
        raise ValueError(f"Cannot parse proxy URL: {proxy_url[:40]}...")
    scheme, user, password, host, port = m.groups()
    return {
        "server": f"{scheme}://{host}:{port}",
        "username": user,
        "password": password,
    }
