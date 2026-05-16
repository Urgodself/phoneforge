"""PhoneForge web UI — thin FastAPI wrapper around `phoneforge.core`.

The web app is a single-user dashboard for PIN-protected provisioning of
disposable phone numbers. It imports `phoneforge.core` directly — never
shells out to the CLI — so request handlers share the same provider
plumbing, DB schema, and config as the command-line tool.
"""
from .app import create_app

__all__ = ["create_app"]
