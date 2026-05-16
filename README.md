# PhoneForge

On-demand US phone-number provisioning for verification flows.
Primary path: **5sim.net** REST API — rent a number per-service, poll for SMS,
finish or ban (refund). Fallback: `import-manual` for manually-provisioned
TextNow numbers (web signup is dead).

Built on httpx (async REST), OpenAI (SMS parse fallback), Camoufox + Playwright
(only used by the legacy TextNow path — kept for `wait` on imported numbers).

## Status (v0.2.0) — honest

| Component                         | Status                                          |
|-----------------------------------|-------------------------------------------------|
| CLI (typer, 8 commands)           | works                                           |
| SQLite ledger (v2 — order_id column) | works, idempotent migration from v1          |
| **5sim.net SMS-API**              | **primary** — provision / fetch_sms / finish / ban / balance / list_services |
| Smartproxy fetch (local + SSH)    | works — used only by TextNow browser path       |
| Camoufox launch + WebGL lock      | works — used only by TextNow browser path       |
| OpenAI SMS-parse fallback         | works (5sim usually pre-parses `code`)          |
| TextNow web signup                | **dead** — `/signup` → `/download`. Archived for reference. |
| TextNow inbox read (`wait`)       | works for accounts that were created elsewhere first |
| `import-manual` flow              | works — store a hand-provisioned number, then `wait` reads SMS via web |

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
# Initialize / migrate the SQLite ledger (first run + after upgrade)
phoneforge db-init

# === Primary flow: 5sim.net ===

# Smoke-test the API key + connectivity (returns balance in RUB):
phoneforge balance
# → 312.50 RUB

# Browse what's available right now (sorted by stock):
phoneforge services --country usa --operator any
phoneforge services --country usa --limit 100 --all       # see everything

# Rent a US number for a specific service:
phoneforge get --service google --country usa
# → +13105550199

# Wait up to 5 min for the code; on success the order is `finish`ed.
phoneforge wait +13105550199 --service google
# → 482910
# On NO_CODE timeout it asks: "Ban + refund this 5sim order? [Y/n]"
# Override that prompt with --auto-ban (always refund) or --no-ban-prompt (never).

# === Fallback: manual TextNow account (web signup is dead) ===

phoneforge import-manual \
  --number +18475550199 --email someone@gmail.com --provider textnow
phoneforge wait +18475550199 --service youtube

# === Ledger management ===
phoneforge list                                # all numbers
phoneforge list --status active                # only active
phoneforge mark-burned +13105550199 --reason "rate-limited"
```

### Country / operator slugs (5sim)

5sim uses lowercase slugs: `usa`, `russia`, `germany`, `philippines`, etc.
Operator is `any` by default (let 5sim pick the cheapest in-stock SIM).
Defaults live in `.env` (`FIVESIM_COUNTRY`, `FIVESIM_OPERATOR`); CLI flags
override per-call.

### Pricing (5sim, as of 2026)

5sim quotes everything in **RUB**. Typical US numbers run **~5-25 RUB**
(roughly $0.05-0.30) depending on service — `google` / `youtube` /
`telegram` are usually on the cheaper end, popular crypto / banking
services on the higher end. `phoneforge services` shows live prices.

Top up the balance on https://5sim.net/billing (cards via Wise, crypto,
or P2P). Refunds for banned-and-never-delivered orders are automatic —
that's why `phoneforge wait` prompts to ban on NO_CODE timeout: free money
back if you accept.

### Exit codes

| code | meaning                                                       |
|------|---------------------------------------------------------------|
| 0    | success                                                       |
| 1    | generic error (5sim API error, DB error, …)                   |
| 2    | bad input / NO_CODE timeout / missing FIVESIM_API_KEY         |
| 3    | 5sim auth error (key invalid / revoked)                        |
| 4    | 5sim has no inventory for the requested service+country       |

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
├── cli.py              entry point — typer (8 commands)
├── core.py             orchestrator — routes browser vs SMS-API flow
├── config.py           env loading — OpenAI + 5sim + proxy + Camoufox keys
├── db.py               SQLite ledger + idempotent column-level migrations
├── browser.py          Camoufox launcher (used only by TextNow path)
├── proxy.py            Smartproxy template → US-pinned URL (TextNow path only)
├── llm.py              OpenAI: identity / SMS parse fallback / captcha vision
└── providers/
    ├── base.py         Provider base with two flows (browser + SMS-API)
    ├── sms5sim.py      PRIMARY — 5sim.net REST API client
    └── textnow.py      LEGACY — kept for `import-manual` + browser `wait`
```

## Deployment (VPS)

After Aleksej drops the 5sim key on `ytm-vps`:

```bash
ssh ytm-vps
cd /root/phoneforge && git pull
echo "FIVESIM_API_KEY=eyJhbGciOi..." >> .env
chmod 600 .env
source .venv/bin/activate
pip install -e ".[dev]"
phoneforge db-init                              # migrates v1 → v2 silently
phoneforge balance                              # smoke-test, prints "X.YZ RUB"
phoneforge services --country usa | head -20    # see what's in stock
phoneforge get --service google --country usa   # rent a real number
phoneforge wait +1xxxxxxxxxx --service google   # block until SMS or 5 min timeout
```

The key never lands in git: `.env` is in `.gitignore` from commit 1; the CLI
never logs the key value; tests use a stub `dummy-test-key`.

## License

Proprietary.
