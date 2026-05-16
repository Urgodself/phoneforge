"""TextNow provider — free US virtual numbers via https://www.textnow.com/

Honest disclaimer (read the README): TextNow's signup gauntlet evolves
constantly — captchas, reCAPTCHA v3 risk-scoring, email verification,
sometimes a phone-verify step. We try the full automated path, and if a
human step is detected we pause and surface a manual-completion prompt.

The `receive_sms` path is much more reliable: login → /messaging → read
the most recent conversation panel as text.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import time
from typing import Optional, Tuple

from .. import browser as _browser
from .. import config, llm
from ..proxy import get_us_proxy
from .base import Provider, ProviderResult

log = logging.getLogger(__name__)


SIGNUP_URL = "https://www.textnow.com/signup"
LOGIN_URL = "https://www.textnow.com/login"
MESSAGING_URL = "https://www.textnow.com/messaging"


async def _human_pause(min_ms: int = 120, max_ms: int = 480) -> None:
    """Inject a short randomised delay — TextNow does timing-based bot checks."""
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


async def _type_humanlike(loc, text: str) -> None:
    """Type one char at a time with small jitter — emulates Camoufox humanize=True
    but at field-level granularity (some forms check inter-keystroke timing)."""
    await loc.click()
    for ch in text:
        await loc.press_sequentially(ch, delay=random.uniform(40, 160))


async def _first_visible(page, selectors: list[str], timeout_ms: int = 8000):
    """Race multiple selectors — TextNow A/B-tests its DOM constantly."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_err = None
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=500):
                    return loc
            except Exception as e:  # noqa: BLE001
                last_err = e
        await asyncio.sleep(0.25)
    raise TimeoutError(
        f"None of these selectors became visible within {timeout_ms}ms: {selectors}. "
        f"Last error: {last_err}"
    )


async def _detect_phone_number_on_page(page) -> Optional[str]:
    """Scan the current page for a US-format phone number.

    TextNow shows the granted number on the dashboard header / sidebar. We
    don't rely on a specific selector — we just regex-scan the textContent.
    """
    body_text = await page.evaluate("() => document.body.innerText || ''")
    m = re.search(r"\(?(\d{3})\)?[\s\-.]?(\d{3})[\s\-.]?(\d{4})", body_text)
    if not m:
        return None
    return f"+1{m.group(1)}{m.group(2)}{m.group(3)}"


class TextNowProvider(Provider):
    name = "textnow"

    async def register(self, *, used_for: str = "") -> ProviderResult:
        identity = llm.generate_identity()
        proxy_url = get_us_proxy()
        webgl_pair: Tuple[str, str] = ("", "")

        async with _browser.launch_browser(proxy_url=proxy_url) as (_, ctx, wgl):
            webgl_pair = wgl
            page = await ctx.new_page()
            await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60_000)
            await _human_pause(800, 1600)

            # Empirical 2026-05: TextNow deprecated web signup. Hitting
            # /signup unconditionally redirects to /download (their mobile-app
            # install page). Web-only automation cannot mint new numbers
            # anymore. Detect that redirect explicitly so the operator sees
            # the actual blocker, not a "form layout drifted" red herring.
            if "/download" in page.url or "/signup" not in page.url:
                raise RuntimeError(
                    "TextNow has deprecated web-based signup — /signup now "
                    f"redirects to {page.url}. Web automation cannot create "
                    "new TextNow numbers. Use the iOS/Android app manually "
                    "(once), then store credentials and use `phoneforge wait`."
                )

            # Dismiss cookie / GDPR banner if present (TextNow shows one to
            # EU exit IPs even though we're US-proxied; some accept-all
            # selector usually works).
            for sel in [
                "button:has-text('Accept')",
                "button:has-text('Accept All')",
                "button:has-text('Got it')",
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        await _human_pause(300, 700)
                        break
                except Exception:
                    pass

            # Fill the form. We probe a few selector variants because TextNow
            # rebrands fields between releases.
            try:
                email_field = await _first_visible(
                    page,
                    [
                        "input[type='email']",
                        "input[name='email']",
                        "input[id*='email' i]",
                    ],
                )
                await _type_humanlike(email_field, identity["email"])
                await _human_pause()

                pwd_field = await _first_visible(
                    page,
                    [
                        "input[type='password']",
                        "input[name='password']",
                        "input[id*='password' i]",
                    ],
                )
                await _type_humanlike(pwd_field, identity["password"])
                await _human_pause()
            except TimeoutError as e:
                # Unknown form — bail to manual mode rather than guessing.
                msg = (
                    f"TextNow signup form layout not recognised: {e}. "
                    "Set PHONEFORGE_MANUAL_SIGNUP=true and re-run to complete by hand."
                )
                log.error(msg)
                raise RuntimeError(msg) from e

            # Submit — try a few CTA labels.
            submitted = False
            for sel in [
                "button[type='submit']",
                "button:has-text('Sign Up')",
                "button:has-text('Continue')",
                "button:has-text('Create Account')",
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1000):
                        await _human_pause(400, 900)
                        await btn.click()
                        submitted = True
                        break
                except Exception:
                    continue
            if not submitted:
                raise RuntimeError(
                    "Could not find signup submit button — DOM likely changed."
                )

            # After submit, three things can happen:
            #   A) email-verify wall   → manual (we don't auto-create mailbox in v1)
            #   B) phone-verify wall   → fatal (chicken-and-egg, can't auto-solve)
            #   C) dashboard with a number → success
            # We give TextNow up to 25s to settle, then assess.
            try:
                await page.wait_for_load_state("networkidle", timeout=25_000)
            except Exception:
                pass

            await _human_pause(800, 1500)

            page_text = (await page.evaluate("() => document.body.innerText || ''")).lower()

            if "verify your email" in page_text or "check your inbox" in page_text:
                # Email-verify wall. Manual mode required.
                manual_mode = config.MANUAL_SIGNUP
                if not manual_mode:
                    raise RuntimeError(
                        "TextNow asked for email verification. v1 doesn't auto-create "
                        "the mailbox. Re-run with PHONEFORGE_MANUAL_SIGNUP=true to pause "
                        "for human completion, or extend providers/textnow.py with a "
                        "guerrillamail/mail.tm integration."
                    )
                _print_manual_pause(
                    "Email verification required. Complete it in the open browser, "
                    "then press Enter here when you see the TextNow dashboard with "
                    "your assigned phone number."
                )

            if any(
                phrase in page_text
                for phrase in [
                    "verify your phone",
                    "enter a phone number",
                    "we need your number",
                ]
            ):
                raise RuntimeError(
                    "TextNow demanded phone verification on signup — chicken-and-egg. "
                    "No automated workaround available. Try a different upstream provider."
                )

            # Captcha? If we see a recaptcha frame, attempt vision or punt to
            # manual mode.
            try:
                if await page.locator("iframe[src*='recaptcha']").first.is_visible(
                    timeout=1500
                ):
                    if not config.MANUAL_SIGNUP:
                        raise RuntimeError(
                            "TextNow served a reCAPTCHA — automated solve not in scope "
                            "for v1. Re-run with PHONEFORGE_MANUAL_SIGNUP=true to solve "
                            "it by hand."
                        )
                    _print_manual_pause(
                        "Solve the captcha in the open browser, then press Enter."
                    )
            except RuntimeError:
                raise
            except Exception:
                pass  # no recaptcha frame — nothing to handle

            # Navigate to /messaging to make sure we landed on the dashboard,
            # then scrape the number.
            try:
                await page.goto(
                    MESSAGING_URL, wait_until="domcontentloaded", timeout=30_000
                )
            except Exception:
                pass
            await _human_pause(1500, 2500)

            number = await _detect_phone_number_on_page(page)
            if not number:
                # Last try — give it more time and look at the title bar / settings.
                try:
                    await page.goto(
                        "https://www.textnow.com/settings/account",
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                    await _human_pause(1200, 2000)
                    number = await _detect_phone_number_on_page(page)
                except Exception:
                    pass

            if not number:
                raise RuntimeError(
                    "Signup probably succeeded but we could not extract the phone "
                    "number from the page. Re-run with PHONEFORGE_HEADLESS=false "
                    "to inspect the dashboard, then read the number off-screen."
                )

        return ProviderResult(
            number=number,
            account_email=identity["email"],
            account_password=identity["password"],
            identity=identity,
            webgl_pair=webgl_pair,
            proxy_url=proxy_url,
            notes="auto-signup v1",
        )

    async def receive_sms(
        self,
        *,
        account_email: str,
        account_password: str,
        webgl_pair: Tuple[str, str],
        timeout_s: int = 180,
        service_hint: str = "",
    ) -> str:
        proxy_url = get_us_proxy()
        async with _browser.launch_browser(
            proxy_url=proxy_url, webgl_pair=webgl_pair or None
        ) as (_, ctx, _wgl):
            page = await ctx.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            await _human_pause(800, 1500)

            try:
                email_field = await _first_visible(
                    page,
                    [
                        "input[type='email']",
                        "input[name='username']",
                        "input[name='email']",
                    ],
                )
                await _type_humanlike(email_field, account_email)
                await _human_pause()

                pwd_field = await _first_visible(
                    page,
                    ["input[type='password']", "input[name='password']"],
                )
                await _type_humanlike(pwd_field, account_password)
                await _human_pause()
            except TimeoutError as e:
                raise RuntimeError(f"Login form not recognised: {e}") from e

            for sel in [
                "button[type='submit']",
                "button:has-text('Log In')",
                "button:has-text('Sign In')",
                "button:has-text('Continue')",
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        break
                except Exception:
                    continue

            try:
                await page.wait_for_url("**/messaging*", timeout=20_000)
            except Exception:
                # Manual fallback: go there directly.
                try:
                    await page.goto(
                        MESSAGING_URL,
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                except Exception:
                    pass

            await _human_pause(1500, 2500)

            # Poll the inbox for up to timeout_s. We snapshot the visible
            # text of the messaging area each cycle; the LLM parses the code.
            deadline = time.monotonic() + timeout_s
            last_snapshot = ""
            while time.monotonic() < deadline:
                # Click the most recent conversation if list is visible — that
                # opens its message panel.
                try:
                    convo = page.locator(
                        "[class*='conversation'], [data-test*='conversation']"
                    ).first
                    if await convo.is_visible(timeout=1500):
                        await convo.click()
                        await _human_pause(600, 1200)
                except Exception:
                    pass

                snapshot = await page.evaluate(
                    "() => document.body.innerText || ''"
                )
                if snapshot and snapshot != last_snapshot:
                    # Heuristic — if we see digits that look like a code, return.
                    if re.search(r"\b\d{4,8}\b", snapshot):
                        return snapshot
                    last_snapshot = snapshot
                await asyncio.sleep(5)

            return last_snapshot  # caller will see "no code" and react


def _print_manual_pause(message: str) -> None:
    """Block on stdin so the user can complete a step in the open browser."""
    print(f"\n[MANUAL] {message}", file=sys.stderr, flush=True)
    try:
        input("[MANUAL] Press Enter to continue... ")
    except EOFError:
        # Non-interactive run — log and continue (best-effort).
        log.warning("Manual pause requested but stdin is closed; continuing.")
