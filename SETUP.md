# Setup

Steps to get the bot running on a fresh machine.

## 1. Clone

```bash
git clone https://github.com/aqoyliev/eld-anomaly-bot.git
cd eld-anomaly-bot
```

## 2. Virtual environment (Python 3.9–3.11)

aiogram 2.x pins `aiohttp 3.8.x`, which has no wheels for Python 3.12+. Use
Python 3.9–3.11 (developed on 3.11).

```bash
# Windows
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1

# macOS / Linux
python3.11 -m venv .venv
source .venv/bin/activate
```

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

## 4. Configure `.env`

`.env` is **gitignored**, so it is never in the repo — you must recreate it.
Copy the template and fill in your real values:

```bash
cp .env.example .env        # Windows: Copy-Item .env.example .env
```

Then edit `.env`:

| Variable                 | How to get it |
|--------------------------|---------------|
| `BOT_TOKEN`              | [@BotFather](https://t.me/BotFather) → `/newbot` (or `/token`) |
| `ADMINS`                 | Your numeric Telegram user ID ([@userinfobot](https://t.me/userinfobot)) |
| `GREENLIGHT_TOKEN`       | Carrier-issued GreenLight ELD external-API token |
| `GOMOTIVE_TOKEN`         | Motive API key (Motive app → Developers → API keys) |
| `ALERT_CHAT_ID`          | The channel/group ID that receives alerts (e.g. `-1001234567890`) |

The other variables (`*_BASE_URL`, `POLL_INTERVAL`, `ELD_STALE_THRESHOLD`,
`MOVING_SPEED_THRESHOLD`, `DB_PATH`) have sensible defaults — see
[README.md](README.md#configuration).

> **Carrying secrets between machines:** store the filled-in `.env` in a
> password manager or encrypted note. Never email or commit it.

## 5. Run

```bash
python app.py
```

On start the bot registers its commands, notifies admins, and launches the
5-minute anomaly poller plus the 2-minute disconnected-unit tracker.

## Deploy to Railway

The bot is a long-polling **worker** (no web port). The repo is deploy-ready:

- `Procfile` → `worker: python app.py`
- `runtime.txt` / `.python-version` → pin **Python 3.11** (aiogram 2.x needs <3.12)

Steps:

1. Create a Railway project from this GitHub repo.
2. Add the **PostgreSQL** plugin. Railway injects `DATABASE_URL`, and the bot
   uses Postgres automatically (no `DATABASE_URL` → it falls back to SQLite).
   The schema is created/migrated on first boot.
3. Set service **Variables**: `BOT_TOKEN`, `ADMINS`, `GREENLIGHT_TOKEN`,
   `GOMOTIVE_TOKEN`, `ALERT_CHAT_ID` (plus any tuning overrides from
   `.env.example`). `DATABASE_URL` comes from the Postgres plugin.
4. Deploy — the worker runs `python app.py`.

> **One instance per `BOT_TOKEN`.** Telegram allows a single long-poller, so
> don't run Railway and a local copy at the same time on the same token.

## Notes

- **One poller per bot token.** Telegram allows only one long-polling instance
  per `BOT_TOKEN`. Don't run the bot in two places at once (e.g. work + home)
  or you'll get `Conflict: terminated by other getUpdates request`. Stop one,
  or use a separate test bot token.
- **`db.sqlite3`** (event history) is gitignored and machine-local, so
  `/history` starts empty on a fresh clone.
- **Verify GreenLight access:** `python scripts/greenlight_probe.py`.
- **Send a test alert:** `python scripts/mock_alert.py` (then
  `python scripts/mock_alert.py --cleanup`).
