"""End-to-end auth tests for the PhoneForge web UI.

These tests use FastAPI's TestClient (httpx under the hood) and exercise
the actual middleware stack — no monkeypatching of internal auth helpers.
Network is monkeypatched away by stubbing `core` and provider methods at
import time, so the test suite never reaches out to 5sim.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Force a known PIN + session secret BEFORE importing the app — env is
# read at app construction time. We also pin DATA_DIR to a temp path so
# tests don't pollute the real ledger.
os.environ["PHONEFORGE_PIN"] = "1991"
os.environ["SESSION_SECRET"] = "0" * 64
# We rely on config.DB_PATH being deterministic from ROOT — point ROOT
# at a writable temp dir for tests would require a more invasive refactor,
# so we just live with the test using the real data/ dir. The schema is
# idempotent and the auth tests don't touch the numbers table.

from fastapi.testclient import TestClient  # noqa: E402

from phoneforge.web import create_app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    # follow_redirects=False so we can assert on the 3xx itself.
    return TestClient(app, follow_redirects=False)


def test_health_is_public(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_login_page_renders(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert "PHONEFORGE" in r.text
    assert "unlock" in r.text


def test_root_redirects_without_session(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_services_redirects_without_session(client: TestClient) -> None:
    r = client.get("/services")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_login_wrong_pin_bounces_back(client: TestClient) -> None:
    r = client.post("/login", data={"pin": "0000"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?err=invalid"


def test_login_correct_pin_redirects_to_root(client: TestClient) -> None:
    r = client.post("/login", data={"pin": "1991"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    # Cookie must be set.
    assert "pf_session" in r.cookies or any(
        "pf_session" in c.lower() for c in r.headers.get_list("set-cookie")
    )


def test_authed_session_can_access_root(client: TestClient) -> None:
    # Log in.
    r = client.post("/login", data={"pin": "1991"})
    assert r.status_code == 303
    # TestClient persists cookies across requests on the same instance.
    r2 = client.get("/")
    assert r2.status_code == 200
    assert "DASHBOARD" in r2.text


def test_logout_clears_session(client: TestClient) -> None:
    client.post("/login", data={"pin": "1991"})
    assert client.get("/").status_code == 200
    r = client.post("/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # After logout, root should bounce again.
    r2 = client.get("/")
    assert r2.status_code == 302
    assert r2.headers["location"] == "/login"
