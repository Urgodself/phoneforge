# PhoneForge

On-demand US phone number provisioning for verification flows.
Each `phoneforge get` creates a brand-new TextNow account behind a fresh US
residential exit IP, returns its phone number, and stores the credentials so
`phoneforge wait` can later log back in and pick up the SMS code.

Built on Camoufox (antidetect Firefox) + Playwright + OpenAI. Reuses the
residential proxy pool from `yt-manager` — read-only, no mutations there.

## Status (v0.1.0)

- Skeleton: complete
- SQLite ledger: complete
- Smartproxy fetch (local + SSH): complete
- Camoufox launch with WebGL-pair lock: complete
- TextNow signup automation: **best-effort**, see "TextNow caveats" below
- TextNow inbox reading for `wait`: complete
- OpenAI identity generation + SMS parsing + (optional) captcha vision: complete

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
# Mint a new disposable US number for a YouTube signup
phoneforge get --service youtube
# → +18475550199

# Later, when YouTube asks for the SMS code:
phoneforge wait +18475550199 --service youtube
# → 482910

# Inspect the ledger
phoneforge list

# Mark a number unusable
phoneforge mark-burned +18475550199 --reason "shadowbanned"
```

## TextNow caveats

TextNow's signup gauntlet is the hard part. Three failure modes:

1. **Email verification.** TextNow may send a confirmation email before
   granting a number. v0.1 does NOT auto-create a mailbox; set
   `PHONEFORGE_MANUAL_SIGNUP=true` and complete the email click manually in
   the open browser, then press Enter to resume.
2. **reCAPTCHA.** If TextNow serves v2 reCAPTCHA, v0.1 does not auto-solve.
   Same manual-mode flow applies.
3. **Phone verification on signup.** Chicken-and-egg — TextNow asks for an
   existing phone number to confirm. No automated workaround; the run aborts
   with a clear message. If TextNow rolls this out for everyone, we'd switch
   to a different upstream (TextFree, SecondLine stubs are in `providers/`).

The `wait` command (login + inbox read) is much more reliable — no captcha,
no email walls.

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
