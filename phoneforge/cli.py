"""PhoneForge CLI — typer entry point.

Commands:
  phoneforge get  --service <name>            mint a fresh number
  phoneforge wait <number> --service <name>   poll for SMS, return code
  phoneforge list                              list all numbers
  phoneforge mark-burned <number>              mark unusable
  phoneforge db-init                           (re)create schema
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import typer

from . import core, db

app = typer.Typer(
    name="phoneforge",
    help="On-demand US phone number provisioning via TextNow + Camoufox + OpenAI.",
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


@app.command("get")
def cmd_get(
    service: str = typer.Option(..., "--service", "-s", help="Tag for what this number is for"),
    provider: str = typer.Option("textnow", "--provider", "-p", help="Provider plugin name"),
) -> None:
    """Mint a fresh disposable number and store credentials in the DB."""
    db.init()
    number = asyncio.run(core.provision_number(service=service, provider_name=provider))
    typer.echo(number)


@app.command("wait")
def cmd_wait(
    number: str = typer.Argument(..., help="E.164 number returned by `get`"),
    service: str = typer.Option("", "--service", "-s", help="Service hint for LLM parsing"),
) -> None:
    """Log in to the account behind `number` and wait for the OTP."""
    code = asyncio.run(core.wait_for_sms(number=number, service_hint=service))
    if not code:
        typer.echo("NO_CODE", err=True)
        raise typer.Exit(code=2)
    typer.echo(code)


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
        f"{'NUMBER':17}  {'PROVIDER':10}  {'STATUS':8}  {'CREATED':19}  {'USED_FOR':16}  EMAIL"
    )
    for r in rows:
        created = datetime.fromtimestamp(r["created_at"], tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        typer.echo(
            f"{r['number']:17}  {r['provider']:10}  {r['status']:8}  "
            f"{created:19}  {(r['used_for'] or ''):16}  {r['account_email']}"
        )


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
