"""OpenAI wrapper for PhoneForge.

Three responsibilities:
1. generate_identity()   → US-flavoured fake identity for TextNow signup
2. parse_sms_code(text)  → extract OTP from inbox text
3. solve_captcha(image)  → best-effort vision read of captcha
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
from functools import lru_cache
from typing import Optional

from openai import OpenAI

from . import config

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=config.get_openai_key())


# ───────────────────────── identity generation ─────────────────────────

_IDENTITY_PROMPT = """You generate a single believable US-based fake identity for
signing up to a free virtual-phone service.

Return ONLY valid JSON with these exact keys (no markdown, no commentary):
{
  "first_name": "<common American first name>",
  "last_name":  "<common American last name>",
  "email":      "<plausible @gmail.com email derived from the name + 3-4 digit suffix>",
  "password":   "<16-char strong password, mixed case + digits + 1 symbol>",
  "birthdate":  "YYYY-MM-DD between 1985-01-01 and 2003-12-31",
  "username":   "<8-14 char alphanumeric handle derived from the name>",
  "city":       "<one US city>",
  "state":      "<two-letter US state code matching the city>",
  "zip":        "<5-digit ZIP plausibly matching that city>"
}

Constraints:
- The identity must look like a real person, not a marketing avatar
- The email local-part should be lowercased name + a 3-4 digit number
- Avoid celebrity names, avoid obviously-test handles like "john.doe123"
"""


def generate_identity() -> dict:
    """Use gpt-4o-mini to mint a fake US identity. Returns a plain dict."""
    seed = random.randint(1000, 9999)
    resp = _client().chat.completions.create(
        model=config.LLM_TEXT_MODEL,
        messages=[
            {"role": "system", "content": _IDENTITY_PROMPT},
            {
                "role": "user",
                "content": f"Generate identity #{seed}. Output JSON only.",
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.9,
        max_tokens=300,
    )
    raw = (resp.choices[0].message.content or "").strip()
    try:
        identity = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM returned non-JSON identity: {raw[:200]!r}") from e

    required = {
        "first_name",
        "last_name",
        "email",
        "password",
        "birthdate",
        "username",
    }
    missing = required - set(identity.keys())
    if missing:
        raise RuntimeError(f"Identity is missing keys: {missing}. Raw: {raw[:200]}")
    return identity


# ───────────────────────── SMS code parsing ─────────────────────────

# Cheap regex pre-pass — covers >90% of real OTP SMS without an LLM call.
_OTP_REGEX = re.compile(r"(?<![\w\d])(\d{4,8})(?![\w\d])")


def parse_sms_code(text: str, service_hint: Optional[str] = None) -> str:
    """Extract a verification code from an SMS body.

    Strategy:
    1. Regex first (cheap, deterministic): pull the longest 4-8 digit run.
    2. If multiple candidates or none, fall back to LLM.
    """
    candidates = _OTP_REGEX.findall(text or "")
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        # No digits at all — definitely not a code.
        return ""

    # Ambiguous — let the LLM pick.
    prompt = (
        f"Extract the single verification/login code from this SMS. "
        f"Reply with ONLY the digits, no surrounding text.\n"
        f"Service hint: {service_hint or 'unknown'}\n"
        f"SMS:\n{text}"
    )
    resp = _client().chat.completions.create(
        model=config.LLM_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=16,
    )
    answer = (resp.choices[0].message.content or "").strip()
    # Strip any wrapping like backticks or quotes.
    digits = re.sub(r"\D", "", answer)
    return digits


# ───────────────────────── captcha vision ─────────────────────────


def solve_captcha(image_bytes: bytes, hint: str = "alphanumeric") -> str:
    """Best-effort vision-based captcha read. NOT guaranteed to succeed —
    return empty string on failure so caller can fall back to a human prompt.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    try:
        resp = _client().chat.completions.create(
            model=config.LLM_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Read the text shown in this captcha image and "
                                f"return ONLY the answer — no quotes, no commentary. "
                                f"Hint: {hint}."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                }
            ],
            temperature=0,
            max_tokens=32,
        )
        return (resp.choices[0].message.content or "").strip().strip("\"' ")
    except Exception as e:
        log.warning("captcha vision failed: %s", e)
        return ""
