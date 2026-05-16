"""PhoneForge CLI — typer entry point.

Commands:
  phoneforge get [--service <name>] [--provider 5sim|textnow] [--country usa] [--operator any]
      mint a fresh number

  phoneforge wait <number> [--service <name>]
      poll for SMS, return code. For 5sim, on NO_CODE timeout prompts to ban+refund.

  phoneforge balance
      show 5sim balance

  phoneforge services [--country usa] [--operator any]
      list available 5sim services + prices

  phoneforge list
  phoneforge mark-burned <number>
  phoneforge db-init
  phoneforge import-manual ...
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import typer

from . import config, core, db

app = typer.Typer(
    name="phoneforge",
    help="On-demand US phone-number provisioning — 5sim.net primary, TextNow manual import fallback.",
    no_args_is_help=True,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


@app.command("db-init")
def cmd_db_init() -> None:
    """Create / migrate the SQLite ledger."""
    path = db.init()
    typer.echo(f"DB initialised at {path}")


@app.command("balance")
def cmd_balance() -> None:
    """Show 5sim balance (smoke-test for API key + connectivity)."""
    if not config.has_5sim_api_key():
        typer.echo(
            "FIVESIM_API_KEY is missing — set it in .env "
            "(get one from https://5sim.net/profile)",
            err=True,
        )
        raise typer.Exit(code=2)
    from .providers import SMS5SimProvider, FiveSimAuthError, FiveSimError

    async def _go() -> None:
        provider = SMS5SimProvider()
        amount, currency = await provider.check_balance()
        typer.echo(f"{amount:.2f} {currency}")

    try:
        asyncio.run(_go())
    except FiveSimAuthError as e:
        typer.echo(f"Auth error: {e}", err=True)
        raise typer.Exit(code=3)
    except FiveSimError as e:
        typer.echo(f"5sim error: {e}", err=True)
        raise typer.Exit(code=1)


@app.command("services")
def cmd_services(
    country: str = typer.Option("", "--country", "-c", help="5sim country slug (default 'usa')"),
    operator: str = typer.Option("", "--operator", "-o", help="5sim operator (default 'any')"),
    limit: int = typer.Option(40, "--limit", "-n", help="Max rows to show"),
    in_stock_only: bool = typer.Option(True, "--in-stock/--all", help="Only services with available numbers"),
) -> None:
    """List available 5sim services + prices (RUB).

    The list is huge (300+ services). By default we filter to ones in stock
    and cap at 40 rows. Use `--all` to see everything.
    """
    from .providers import SMS5SimProvider, FiveSimError

    async def _go() -> list[dict]:
        provider = SMS5SimProvider(
            country=country or None,
            operator=operator or None,
        )
        return await provider.list_services(country=country, operator=operator)

    try:
        rows = asyncio.run(_go())
    except FiveSimError as e:
        typer.echo(f"5sim error: {e}", err=True)
        raise typer.Exit(code=1)

    if in_stock_only:
        rows = [r for r in rows if r["count"] > 0]

    if not rows:
        typer.echo("(no services available for that country/operator)")
        return

    typer.echo(f"{'SERVICE':25}  {'PRICE(RUB)':10}  {'STOCK':>6}  CATEGORY")
    for r in rows[:limit]:
        typer.echo(
            f"{r['name']:25}  {r['price']:>10.2f}  {r['count']:>6}  {r['category']}"
        )
    if len(rows) > limit:
        typer.echo(f"... ({len(rows) - limit} more — use --limit higher)")


@app.command("get")
def cmd_get(
    service: str = typer.Option(..., "--service", "-s", help="5sim service slug (e.g. 'google', 'youtube') OR tag for browser flow"),
    provider: str = typer.Option(core.DEFAULT_PROVIDER, "--provider", "-p", help="5sim | textnow"),
    country: str = typer.Option("", "--country", "-c", help="5sim country (default 'usa')"),
    operator: str = typer.Option("", "--operator", "-o", help="5sim operator (default 'any')"),
) -> None:
    """Mint a fresh disposable number.

    With --provider 5sim (default): rents a number from 5sim.net for `service`.
    With --provider textnow: legacy browser flow (currently dead — TextNow killed web signup).
    """
    if provider.lower() == "5sim" and not config.has_5sim_api_key():
        typer.echo(
            "FIVESIM_API_KEY is missing — set it in .env, or use "
            "`phoneforge import-manual` for a manually-provisioned number.",
            err=True,
        )
        raise typer.Exit(code=2)

    db.init()
    from .providers import FiveSimAuthError, FiveSimError, FiveSimNoInventory

    try:
        number = asyncio.run(
            core.provision_number(
                service=service,
                provider_name=provider,
                country=country,
                operator=operator,
            )
        )
    except FiveSimNoInventory as e:
        typer.echo(f"NO_INVENTORY: {e}", err=True)
        typer.echo(
            "Hints: try a different --operator (e.g. verizon, att, tmobile), "
            "a different --country (e.g. canada, philippines), check "
            "`phoneforge services` for what's in stock right now, or use "
            "`phoneforge import-manual` for a hand-provisioned number.",
            err=True,
        )
        raise typer.Exit(code=4)
    except FiveSimAuthError as e:
        typer.echo(f"Auth error: {e}", err=True)
        raise typer.Exit(code=3)
    except FiveSimError as e:
        typer.echo(f"5sim error: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(number)


@app.command("wait")
def cmd_wait(
    number: str = typer.Argument(..., help="E.164 number returned by `get`"),
    service: str = typer.Option("", "--service", "-s", help="Service hint for parsing"),
    timeout: int = typer.Option(0, "--timeout", "-t", help="Override poll timeout in seconds (0 = provider default)"),
    auto_ban: bool = typer.Option(False, "--auto-ban", help="On NO_CODE timeout, ban the order without prompting"),
    no_ban_prompt: bool = typer.Option(False, "--no-ban-prompt", help="On NO_CODE timeout, never ban (keeps the rental until 5sim expires it)"),
) -> None:
    """Wait for an SMS code.

    For 5sim: polls the order. On success → finish(). On NO_CODE timeout →
    prompts to ban+refund (unless --auto-ban / --no-ban-prompt).

    For TextNow (legacy): logs into the web client and reads the inbox.
    """
    eff_timeout = timeout if timeout > 0 else None
    outcome = asyncio.run(
        core.wait_for_sms(
            number=number,
            service_hint=service,
            timeout_s=eff_timeout,
        )
    )

    if outcome.code:
        typer.echo(outcome.code)
        return

    # No code → typer Exit(2). For 5sim, optionally ban for refund.
    typer.echo("NO_CODE", err=True)

    if outcome.provider == "5sim" and outcome.provider_order_id:
        should_ban: bool
        if auto_ban:
            should_ban = True
        elif no_ban_prompt:
            should_ban = False
        else:
            try:
                should_ban = typer.confirm(
                    f"Number {number} didn't deliver SMS. Ban + refund this 5sim order?",
                    default=True,
                )
            except typer.Abort:
                should_ban = False

        if should_ban:
            try:
                asyncio.run(core.ban_order(number=number, reason="wait timeout"))
                typer.echo(f"OK — banned order {outcome.provider_order_id}.", err=True)
            except Exception as e:  # noqa: BLE001
                typer.echo(f"Ban failed: {e}", err=True)

    raise typer.Exit(code=2)


@app.command("list")
def cmd_list(
    status: str = typer.Option("", "--status", help="Filter by status (active|burned|used)"),
) -> None:
    """Show all numbers in the ledger."""
    db.init()
    rows = db.list_all(status=status or None)
    if not rows:
        typer.echo("(no numbers in DB)")
        return
    typer.echo(
        f"{'NUMBER':17}  {'PROVIDER':10}  {'STATUS':8}  {'CREATED':19}  {'USED_FOR':16}  ORDER_ID"
    )
    for r in rows:
        created = datetime.fromtimestamp(r["created_at"], tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        # provider_order_id is the new column; for old rows it's "" or NULL.
        try:
            order = r["provider_order_id"] or ""
        except (IndexError, KeyError):
            order = ""
        typer.echo(
            f"{r['number']:17}  {r['provider']:10}  {r['status']:8}  "
            f"{created:19}  {(r['used_for'] or ''):16}  {order or r['account_email']}"
        )


@app.command("import-manual")
def cmd_import_manual(
    number: str = typer.Option(..., "--number", "-n", help="E.164 number, e.g. +18475550199"),
    email: str = typer.Option(..., "--email", "-e", help="Account email (whichever upstream)"),
    password: str = typer.Option(..., "--password", "-w", help="Account password", prompt=True, hide_input=True),
    provider: str = typer.Option("textnow", "--provider", "-p"),
    used_for: str = typer.Option("", "--used-for", "-u"),
    notes: str = typer.Option("manual import", "--notes"),
) -> None:
    """Manually register a number you provisioned by hand (e.g. via TextNow mobile app)."""
    db.init()
    from . import browser as _browser
    # Sample a WebGL pair now so re-login is fingerprint-stable.
    webgl = _browser.sample_windows_webgl_pair()
    rowid = db.insert_number(
        number=number,
        provider=provider,
        account_email=email,
        account_password=password,
        identity={"manual": True, "email": email},
        webgl_vendor=webgl[0],
        webgl_renderer=webgl[1],
        proxy_str="",
        used_for=used_for,
        notes=notes,
    )
    typer.echo(f"OK — number stored (id={rowid}). Use `phoneforge wait {number}` to receive SMS.")


@app.command("mark-burned")
def cmd_mark_burned(
    number: str = typer.Argument(...),
    reason: str = typer.Option("", "--reason", "-r", help="Why is this number unusable"),
) -> None:
    """Flip a number's status to 'burned' so it never gets reused."""
    db.init()
    note = f"burned at {int(time.time())}: {reason}" if reason else f"burned at {int(time.time())}"
    ok = db.mark_status(number, "burned", note=note)
    if not ok:
        typer.echo(f"Number not found: {number}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"OK — {number} marked burned.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
