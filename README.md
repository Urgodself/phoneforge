# PhoneForge

On-demand US phone number provisioning for verification flows.
Each `phoneforge get` creates a brand-new TextNow account behind a fresh US
residential exit IP, returns its phone number, and stores the credentials so
`phoneforge wait` can later log back in and pick up the SMS code.

Built on Camoufox (antidetect Firefox) + Playwright + OpenAI. Reuses the
residential proxy pool from `yt-manager` — read-only, no mutations there.

## Status (v0.1.0) — honest

| Component                         | Status                                          |
|-----------------------------------|-------------------------------------------------|
| CLI (typer, 5 commands)           | works                                           |
| SQLite ledger                     | works                                           |
| Smartproxy fetch (local + SSH)    | works — verified end-to-end on VPS              |
| Camoufox launch + WebGL lock      | works — verified on VPS, opens proxied browser  |
| OpenAI identity / SMS-parse / vision | works (modules ready, gpt-4o-mini + gpt-4o)  |
| **TextNow web signup**            | **dead** — TextNow killed web signup in 2023-2024; /signup redirects to /download. The mobile app is now mandatory. |
| TextNow inbox read (`wait`)       | works *only* for accounts that were created elsewhere first |
| `import-manual` flow              | works — store a hand-provisioned number, then `wait` automates SMS reading |

## Install

```bash
cd ~/phoneforge
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
python -m playwright install firefox  # camoufox patches a forked Firefox
# Camoufox bundle:
python -m camoufox fetch
```

## Configure

```bash
cp .env.example .env
chmod 600 .env
# Then edit .env: paste OPENAI_API_KEY, choose PHONEFORGE_PROXY_SOURCE.
phoneforge db-init
```

`.env` is in `.gitignore` from commit 1 — never check it in.

## Usage

```bash
# Initialize the SQLite ledger (first run only)
phoneforge db-init

# === Realistic flow (recommended after TextNow killed web signup) ===

# 1. On a clean Android device / emulator, install TextNow, sign up, note the
#    email, password and granted phone number.
# 2. Register the credentials with PhoneForge:
phoneforge import-manual --number +18475550199 --email someone@gmail.com
# (prompts for password)

# 3. Whenever a service sends an SMS to that number, pull the code:
phoneforge wait +18475550199 --service youtube
# → 482910

# === Web-signup path (BROKEN — kept for completeness) ===
phoneforge get --service youtube
# → RuntimeError: TextNow has deprecated web-based signup — /signup now
#   redirects to /download. ...

# === Ledger management ===
phoneforge list
phoneforge mark-burned +18475550199 --reason "shadowbanned"
```

## Why TextNow signup doesn't work

Empirical test on 2026-05-16 from a US-residential Smartproxy exit IP:
`https://www.textnow.com/signup` does an unconditional 30x redirect to
`https://www.textnow.com/download`. There is no signup form in the DOM —
the page is a pure mobile-app store-link page. This is TextNow's anti-bot
strategy: account creation is locked behind iOS/Android device attestation
(SafetyNet / DeviceCheck). Web-only automation cannot bypass it.

Alternative paths considered:

- **TextFree / SecondLine** (stubs already in `providers/`): need
  empirical probe — likely also app-only by now.
- **Paid SMS-activation APIs** (`5sim.net`, `sms-activate.org`,
  `daisysms.com`): ~$0.05-0.30 per US number, REST API, no browser. This
  is the de-facto standard "disposable US number" path in 2024-2026 and is
  much cheaper than building app-emulator infrastructure.
- **Android emulator + Appium for the TextNow app**: roughly 1 week of work,
  fragile against app updates, requires ARM-Android image on the VPS or a
  remote device farm. Possible but not free in any real sense.

The `wait` command (login + inbox read on the existing web client) is
unaffected — it works fine for accounts created via the mobile app.

## Proxy source

`.env` setting: `PHONEFORGE_PROXY_SOURCE=local|ssh`.

- `local` — opens `/Users/aleksej/Desktop/Nothing/yt-manager/data/ytmanager.db`
  in read-only mode, picks one Smartproxy template, rewrites `area-XX` → `area-US`
  and a fresh session token, hands the URL to Camoufox.
- `ssh` — runs `sqlite3` on `ytm-vps` over SSH and parses the same way.

Useful for VPS deploys where the local Mac DB isn't reachable.

## Files

```
phoneforge/
├── cli.py              entry point — typer
├── core.py             orchestrator
├── config.py           env loading
├── db.py               SQLite ledger
├── browser.py          Camoufox launcher (Windows OS, WebGL pair locked per account)
├── proxy.py            Smartproxy template → US-pinned URL
├── llm.py              OpenAI: identity / SMS parse / captcha vision
└── providers/
    ├── base.py         abstract Provider
    ├── textnow.py      PRIMARY
    ├── textfree.py     v2 stub
    └── secondline.py   v2 stub
```

## License

Proprietary.
