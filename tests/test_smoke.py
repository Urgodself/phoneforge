"""Smoke tests — no network, no browser. Just verifies the modules import
and the DB schema applies cleanly."""
from __future__ import annotations

from pathlib import Path

import pytest

from phoneforge import db, llm, proxy
from phoneforge.providers import PROVIDERS, get_provider


def test_db_init_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    # Patch the default db path for this test.
    from phoneforge import config
    config.DB_PATH = path  # type: ignore[misc]
    config.DATA_DIR = tmp_path  # type: ignore[misc]

    db.init(path)
    db.init(path)  # second time must not raise

    pid = db.insert_number(
        number="+15550001111",
        provider="textnow",
        account_email="x@y.com",
        account_password="pw",
        identity={"first_name": "T", "last_name": "S"},
        webgl_vendor="Google Inc. (NVIDIA)",
        webgl_renderer="ANGLE (NVIDIA, ...)",
        proxy_str="http://u:p@host:7000",
        used_for="smoke",
    )
    assert pid > 0
    row = db.get_by_number("+15550001111")
    assert row is not None and row["account_email"] == "x@y.com"


def test_proxy_regex_parses_smartproxy_url() -> None:
    sample = (
        "http://smart-a5f48o6s6v3y_area-IT_life-120_session-Exe1qdiLk"
        ":6oBW6k34DuJQuXoE@proxy.smartproxy.com:7000"
    )
    tpl = proxy._parse_smartproxy(sample)
    assert tpl is not None
    assert tpl.area == "IT"
    assert tpl.host == "proxy.smartproxy.com"
    assert tpl.port == 7000
    rebuilt = tpl.with_session("US", "abc123")
    assert "_area-US_" in rebuilt and "_session-abc123" in rebuilt


def test_proxy_to_playwright_dict() -> None:
    url = (
        "http://smart-acc_area-US_life-120_session-xyz"
        ":mypass@proxy.smartproxy.com:7000"
    )
    d = proxy.proxy_to_playwright_dict(url)
    assert d["server"] == "http://proxy.smartproxy.com:7000"
    assert d["username"].startswith("smart-acc_area-US")
    assert d["password"] == "mypass"


def test_otp_regex_extracts_single_code() -> None:
    assert llm._OTP_REGEX.findall("Your code is 482910 — expires soon") == ["482910"]
    assert llm._OTP_REGEX.findall("No numbers here") == []


def test_providers_registry() -> None:
    assert "textnow" in PROVIDERS
    p = get_provider("textnow")
    assert p.name == "textnow"
    with pytest.raises(KeyError):
        get_provider("noexist")
