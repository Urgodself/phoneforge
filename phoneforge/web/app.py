"""FastAPI app factory + route handlers for the PhoneForge web UI.

Design constraints baked into this file:

- **All work goes through `phoneforge.core`** — never shell out, never
  duplicate provider logic. If you find yourself reimplementing what's in
  `core.py`, stop and fix `core.py` instead.

- **Async everywhere on the I/O path.** Blocking sqlite3 calls are wrapped
  in `asyncio.to_thread` so they don't stall the event loop under
  HTMX-polling pressure.

- **5sim API calls are bounded.** A dead upstream must not freeze the
  dashboard — every external call has an explicit `wait_for` timeout and
  a graceful degraded-state fallback.

- **Templates do no logic.** Hand them ready-to-render dicts, not provider
  objects. Keeps the Jinja files glanceable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import config, core, db
from ..providers import (
    FiveSimAuthError,
    FiveSimError,
    FiveSimNoInventory,
    SMS5SimProvider,
)
from . import auth

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Caches the last successful balance read so the dashboard never blocks on
# 5sim hiccups. Tiny TTL — we don't want stale balance, but we do want a
# graceful fallback if upstream is slow.
_BALANCE_CACHE: dict[str, Any] = {"amount": None, "currency": "RUB", "fetched_at": 0.0}
_BALANCE_TTL_S = 15.0
_BALANCE_FETCH_TIMEOUT_S = 3.0


# ───────────────────────────── helpers ─────────────────────────────


def _format_number_row(row: Any) -> dict:
    """Squash a sqlite3.Row into a render-ready dict.

    Identity_json is parsed lazily — only the bits the dashboard actually
    shows (service, country, operator, expires_at, price). Stored credentials
    are NEVER returned: web UI doesn't render the password (no use case).
    """
    identity = {}
    try:
        identity = json.loads(row["identity_json"] or "{}")
    except (json.JSONDecodeError, KeyError, IndexError):
        pass

    created_at = float(row["created_at"] or 0.0)
    last_used_at = row["last_used_at"]
    try:
        order_id = row["provider_order_id"] or ""
    except (IndexError, KeyError):
        order_id = ""

    return {
        "id": int(row["id"]),
        "number": row["number"],
        "provider": row["provider"],
        "status": row["status"],
        "used_for": row["used_for"] or "",
        "notes": row["notes"] or "",
        "provider_order_id": order_id,
        "created_at": created_at,
        "created_at_iso": datetime.fromtimestamp(created_at, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ) if created_at else "",
        "last_used_at_iso": datetime.fromtimestamp(float(last_used_at), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ) if last_used_at else "",
        "elapsed_s": int(time.time() - created_at) if created_at else 0,
        "service": identity.get("service", ""),
        "country": identity.get("country", ""),
        "operator": identity.get("operator", ""),
        "price": identity.get("price"),
        "currency": identity.get("currency", "RUB"),
        "expires_at_iso": (
            datetime.fromtimestamp(float(identity["expires_at"]), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            if identity.get("expires_at")
            else ""
        ),
    }


def _format_sms_row(row: Any) -> dict:
    received_at = float(row["received_at"] or 0.0)
    return {
        "id": int(row["id"]),
        "received_at_iso": datetime.fromtimestamp(received_at, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ),
        "raw_text": row["raw_text"] or "",
        "parsed_code": row["parsed_code"] or "",
        "sender": row["sender"] or "",
    }


async def _fetch_sms_list(number_id: int) -> list[dict]:
    """Read the sms_log table for one number. Synchronous SQLite via to_thread."""

    def _q() -> list:
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT * FROM sms_log WHERE number_id = ? ORDER BY received_at DESC",
                (number_id,),
            )
            return list(cur.fetchall())

    rows = await asyncio.to_thread(_q)
    return [_format_sms_row(r) for r in rows]


async def _get_number_by_id(number_id: int) -> Optional[dict]:
    def _q():
        with db.connect() as conn:
            cur = conn.execute("SELECT * FROM numbers WHERE id = ?", (number_id,))
            return cur.fetchone()

    row = await asyncio.to_thread(_q)
    return _format_number_row(row) if row else None


async def _list_numbers(status: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
    rows = await asyncio.to_thread(db.list_all, status)
    out = [_format_number_row(r) for r in rows]
    return out[:limit] if limit else out


async def _read_balance() -> dict:
    """Cached 5sim balance read. Falls back to last-known on timeout/error.

    Returns dict with keys: amount (float|None), currency (str),
    fetched_at (float epoch), stale (bool). The dashboard renders "—"
    when amount is None.
    """
    now = time.time()
    if (
        _BALANCE_CACHE["amount"] is not None
        and (now - _BALANCE_CACHE["fetched_at"]) < _BALANCE_TTL_S
    ):
        return {**_BALANCE_CACHE, "stale": False}

    if not config.has_5sim_api_key():
        return {"amount": None, "currency": "RUB", "fetched_at": 0.0, "stale": True}

    try:
        provider = SMS5SimProvider()
        amount, currency = await asyncio.wait_for(
            provider.check_balance(), timeout=_BALANCE_FETCH_TIMEOUT_S
        )
        _BALANCE_CACHE["amount"] = float(amount)
        _BALANCE_CACHE["currency"] = currency
        _BALANCE_CACHE["fetched_at"] = now
        return {**_BALANCE_CACHE, "stale": False}
    except (asyncio.TimeoutError, FiveSimError, FiveSimAuthError) as e:
        log.warning("Balance read failed (using cache if any): %s", e)
        return {
            "amount": _BALANCE_CACHE["amount"],
            "currency": _BALANCE_CACHE["currency"],
            "fetched_at": _BALANCE_CACHE["fetched_at"],
            "stale": True,
        }


# ───────────────────────────── app factory ─────────────────────────────


def create_app() -> FastAPI:
    db.init()  # idempotent — ensures schema exists before any request

    app = FastAPI(
        title="PhoneForge",
        description="Disposable phone numbers via 5sim.net",
        version="0.1.0",
        # The UI is intentionally not part of any API docs page.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Middleware order matters: Starlette executes the LAST-added middleware
    # FIRST (outermost wrapper). We want SessionMiddleware to run before the
    # auth gate so `request.session` is populated when AuthMiddleware reads
    # it. Therefore: add Auth FIRST, then Session — Session ends up outermost.
    app.add_middleware(auth.AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.get_session_secret(),
        session_cookie="pf_session",
        max_age=auth.SESSION_MAX_AGE_S,
        same_site="lax",
        # https_only=True would drop cookies on plain HTTP — fine in prod
        # (Caddy terminates TLS) but breaks `phoneforge serve` on localhost.
        # We keep it off; Caddy is the actual TLS boundary, and the cookie
        # is already HttpOnly + signed. Flip to True if Aleksej ever wants
        # to expose this directly on a non-localhost interface.
        https_only=False,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Make a few helpers available to all templates.
    templates.env.globals["app_name"] = "PHONEFORGE"

    # ───────── routes ─────────

    @app.get("/health", response_class=JSONResponse)
    async def health() -> dict:
        return {"ok": True, "service": "phoneforge", "ts": time.time()}

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, err: str = "") -> Response:
        if auth.is_authed(request):
            return RedirectResponse(url="/", status_code=302)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": err, "balance": None},
        )

    @app.post("/login")
    async def login_submit(request: Request, pin: str = Form(...)) -> Response:
        if not auth.verify_pin(pin):
            log.info("Failed login attempt")
            return RedirectResponse(url="/login?err=invalid", status_code=303)
        request.session["authed"] = True
        request.session["since"] = time.time()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        balance, numbers = await asyncio.gather(
            _read_balance(),
            _list_numbers(limit=10),
        )
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "balance": balance,
                "numbers": numbers,
                "default_country": config.FIVESIM_COUNTRY,
                "default_operator": config.FIVESIM_OPERATOR,
            },
        )

    @app.get("/services", response_class=HTMLResponse)
    async def services_page(
        request: Request,
        country: str = "",
        operator: str = "",
        q: str = "",
        in_stock: int = 1,
    ) -> Response:
        balance = await _read_balance()
        eff_country = (country or config.FIVESIM_COUNTRY).lower()
        eff_operator = (operator or config.FIVESIM_OPERATOR).lower()
        rows: list[dict] = []
        error = ""
        if config.has_5sim_api_key():
            try:
                provider = SMS5SimProvider(country=eff_country, operator=eff_operator)
                rows = await asyncio.wait_for(
                    provider.list_services(country=eff_country, operator=eff_operator),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                error = "5sim timed out — try again."
            except FiveSimError as e:
                error = f"5sim error: {e}"
        else:
            error = "FIVESIM_API_KEY is not configured on the server."

        if in_stock:
            rows = [r for r in rows if r.get("count", 0) > 0]

        if q:
            ql = q.strip().lower()
            rows = [r for r in rows if ql in (r.get("name", "") or "").lower()]

        return templates.TemplateResponse(
            "services.html",
            {
                "request": request,
                "balance": balance,
                "rows": rows,
                "country": eff_country,
                "operator": eff_operator,
                "q": q,
                "in_stock": in_stock,
                "error": error,
            },
        )

    @app.post("/buy")
    async def buy_number(
        request: Request,
        service: str = Form(...),
        country: str = Form(""),
        operator: str = Form(""),
    ) -> Response:
        service = (service or "").strip()
        if not service:
            return RedirectResponse(
                url="/services?err=missing_service", status_code=303
            )

        try:
            number = await core.provision_number(
                service=service,
                provider_name="5sim",
                country=country.strip(),
                operator=operator.strip(),
            )
        except FiveSimNoInventory as e:
            log.info("Buy %s: no inventory — %s", service, e)
            return RedirectResponse(
                url=f"/services?err=no_inventory&q={service}", status_code=303
            )
        except FiveSimAuthError as e:
            log.error("Buy %s: auth error — %s", service, e)
            return RedirectResponse(url="/?err=auth", status_code=303)
        except FiveSimError as e:
            log.error("Buy %s: 5sim error — %s", service, e)
            return RedirectResponse(url=f"/?err=5sim_error", status_code=303)

        # Find the row we just inserted so we know its id.
        def _lookup_id() -> Optional[int]:
            row = db.get_by_number(number)
            return int(row["id"]) if row else None

        new_id = await asyncio.to_thread(_lookup_id)
        if new_id is None:
            log.error("Bought number %s but couldn't find it in DB", number)
            return RedirectResponse(url="/", status_code=303)
        return RedirectResponse(url=f"/number/{new_id}", status_code=303)

    @app.get("/number/{number_id}", response_class=HTMLResponse)
    async def number_detail(request: Request, number_id: int) -> Response:
        number = await _get_number_by_id(number_id)
        if number is None:
            raise HTTPException(status_code=404, detail="Number not found")
        sms_list = await _fetch_sms_list(number_id)
        balance = await _read_balance()
        return templates.TemplateResponse(
            "number_detail.html",
            {
                "request": request,
                "balance": balance,
                "n": number,
                "sms_list": sms_list,
            },
        )

    @app.get("/number/{number_id}/sms", response_class=HTMLResponse)
    async def number_sms_fragment(request: Request, number_id: int) -> Response:
        """HTMX fragment: poll 5sim once (short timeout), render SMS list.

        The trick here is the SHORT poll — 2s upstream timeout, then we
        just rely on HTMX to fire again in 5s. Long-polling from the
        browser side would block the worker thread; short polls keep
        the system responsive.
        """
        number = await _get_number_by_id(number_id)
        if number is None:
            raise HTTPException(status_code=404, detail="Number not found")

        # Only poll 5sim if the order is still active and has an order id.
        if (
            number["status"] == "active"
            and number["provider"].lower() == "5sim"
            and number["provider_order_id"]
        ):
            try:
                provider = SMS5SimProvider()
                code = await asyncio.wait_for(
                    provider.fetch_sms(
                        number["provider_order_id"],
                        timeout_s=1,            # ~one upstream check
                        poll_interval_s=0.5,
                        service_hint=number["service"] or "",
                    ),
                    timeout=2.5,
                )
                if code:
                    # Persist it so /sms reflects it on next render.
                    await asyncio.to_thread(
                        db.log_sms,
                        number_id,
                        f"5sim order {number['provider_order_id']}",
                        code,
                    )
                    # Best-effort finish — same fire-and-forget pattern as CLI.
                    try:
                        await asyncio.wait_for(
                            provider.finish(number["provider_order_id"]), timeout=2.0
                        )
                        await asyncio.to_thread(
                            db.mark_status,
                            number["number"],
                            "done",
                            "auto-finish after SMS",
                        )
                    except Exception as fin_e:
                        log.warning(
                            "finish() failed for order %s: %s",
                            number["provider_order_id"],
                            fin_e,
                        )
            except asyncio.TimeoutError:
                # Normal — no SMS yet. HTMX will retry in 5s.
                pass
            except (FiveSimError, FiveSimAuthError) as e:
                log.warning("fetch_sms failed for number_id=%s: %s", number_id, e)

        # Re-read after potential mutation above.
        number_after = await _get_number_by_id(number_id)
        sms_list = await _fetch_sms_list(number_id)
        return templates.TemplateResponse(
            "partials/sms_list.html",
            {
                "request": request,
                "n": number_after or number,
                "sms_list": sms_list,
            },
        )

    @app.post("/number/{number_id}/burn")
    async def burn_number(request: Request, number_id: int) -> Response:
        number = await _get_number_by_id(number_id)
        if number is None:
            raise HTTPException(status_code=404, detail="Number not found")
        try:
            if number["provider"].lower() == "5sim" and number["provider_order_id"]:
                provider = SMS5SimProvider()
                await asyncio.wait_for(
                    provider.ban(number["provider_order_id"], reason="web ui burn"),
                    timeout=10.0,
                )
            await asyncio.to_thread(
                db.mark_status, number["number"], "burned", "web ui burn"
            )
        except (asyncio.TimeoutError, FiveSimError, FiveSimAuthError) as e:
            log.warning("Burn %s: %s", number["number"], e)
            # Still mark locally — the upstream order will time out anyway.
            await asyncio.to_thread(
                db.mark_status,
                number["number"],
                "burned",
                f"web ui burn (upstream failed: {type(e).__name__})",
            )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/number/{number_id}/finish")
    async def finish_number(request: Request, number_id: int) -> Response:
        number = await _get_number_by_id(number_id)
        if number is None:
            raise HTTPException(status_code=404, detail="Number not found")
        try:
            if number["provider"].lower() == "5sim" and number["provider_order_id"]:
                provider = SMS5SimProvider()
                await asyncio.wait_for(
                    provider.finish(number["provider_order_id"]), timeout=10.0
                )
            await asyncio.to_thread(
                db.mark_status, number["number"], "done", "web ui finish"
            )
        except (asyncio.TimeoutError, FiveSimError, FiveSimAuthError) as e:
            log.warning("Finish %s: %s", number["number"], e)
            await asyncio.to_thread(
                db.mark_status,
                number["number"],
                "done",
                f"web ui finish (upstream failed: {type(e).__name__})",
            )
        return RedirectResponse(url="/", status_code=303)

    @app.get("/numbers", response_class=HTMLResponse)
    async def numbers_page(request: Request, status: str = "") -> Response:
        numbers = await _list_numbers(status=status or None)
        balance = await _read_balance()
        return templates.TemplateResponse(
            "numbers.html",
            {
                "request": request,
                "balance": balance,
                "numbers": numbers,
                "status_filter": status,
            },
        )

    return app
