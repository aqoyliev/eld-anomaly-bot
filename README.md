# eld-anomaly-bot

A Telegram bot that detects **ELD-disconnection anomalies / fraud** in a
logistics fleet by cross-referencing two telematics sources:

- **GoMotive (Motive)** or **Samsara** — ground-truth vehicle movement
  (speed + location); each company uses whichever device its trucks carry
  (or both, for a mixed fleet)
- **Quantum ELD** — the electronic logging device that *should* be
  recording the trip

If a truck is **moving on GoMotive/Samsara** while its **Quantum ELD has stopped
reporting** (its last report is stale), the ELD looks disconnected/offline while
the vehicle keeps driving — the classic disconnection-fraud pattern. The bot
fires a 🚨 alert to a Telegram channel.

## How it works

Every 5 minutes the poller runs this pipeline:

1. **Ask the company's movement provider(s) which vehicles are moving** —
   GoMotive: page through `/v3/vehicle_locations` (preferred, Vehicle Gateway
   feed) with a `/v1` fallback for vehicles v3 doesn't return, speeds
   normalised to mph. Samsara: page through `/fleet/vehicles/stats?types=gps`
   (mph natively). Only vehicles with `speed > 0` are kept; each is tagged
   with the provider that reported it.
2. **Look each moving truck up in Quantum** by unit number
   (`GET /vehicles/{unit_number}`), reading its last report time.
3. **Flag an anomaly** when the Quantum report is older than
   `ELD_STALE_THRESHOLD` (default 10 min) while the truck is moving.
4. **Alert, de-duplicate, and track** — a new anomaly sends one alert and opens
   an event; ongoing anomalies update silently (no duplicate alerts); when a
   truck starts reporting again the event auto-resolves.

```
GoMotive/Samsara (moving vehicles) ──► Quantum lookup per unit ──► stale? ──► 🚨 alert
                                                                  └────► SQLite event + /status + /history
```

### Alert format

```
🚨 ELD DISCONNECTION ANOMALY

Vehicle: 2462 — driver DESROSIERS ODLET
Last known location: Dwight D. Eisenhower Hwy, Green River, WY 82935
Current speed: 72 mph
ELD disconnected at: 2026-06-04 09:39:04 UTC
Anomaly duration: 17m 6s

Disconnected on Quantum ELD but still moving on GoMotive.
```

## Commands

| Command   | Description                                  |
|-----------|----------------------------------------------|
| `/status` | List vehicles currently flagged as anomalous (for this chat's company) |
| `/history`| Recent disconnection events (last 20)        |
| `/start`  | Start the bot                                |
| `/help`   | Help                                         |

> Regular users can only use the bot **inside a company's group chat**. A direct
> message from a non-admin is turned away — private chats are reserved for admins
> (so the admin commands below work in DM).

**Admin-only** (restricted to `ADMINS`):

| Command        | Description                                                    |
|----------------|----------------------------------------------------------------|
| `/addcompany`  | Step-by-step wizard to create a company; token messages are auto-deleted |
| `/bindhere`    | Link the current chat as a company's alert group: `/bindhere <name>` (a chat can host several companies) |
| `/unbindhere`  | Unlink a company from the current chat: `/unbindhere [name]`  |
| `/companies`   | List all companies (tokens masked)                             |
| `/activate`    | Re-activate a company: `/activate <name or id>`               |
| `/deactivate`  | Stop polling a company (history kept): `/deactivate <name or id>` |
| `/cancel`      | Abort the `/addcompany` wizard                                 |

## Multi-company

The bot serves **multiple trucking companies** at once. Each company has its own
movement-provider token(s) (GoMotive and/or Samsara — at least one), Quantum
token, and Telegram alert group, and is stored in a
`companies` table; every event is scoped by `company_id`, so two companies can
share a unit number without colliding. Each poll/track tick iterates the active
companies (those that are active **and** bound to an alert chat).

**Adding a company** (all in Telegram, as an admin):

1. DM the bot `/addcompany` and follow the prompts (name → GoMotive token →
   Samsara token → Quantum token; either movement-provider step can be
   skipped with `skip`, but not both). The bot deletes the messages
   containing tokens.
2. Add the bot to that company's Telegram alert group and send
   `/bindhere <name>` there. It starts being polled on the next cycle.
   Several companies may share one group — `/bindhere` each of them there;
   `/status` and `/history` then show a block per company.

**Migration is automatic:** on first start against a database that has no
companies, the bot seeds a single `default` company from the legacy
`GOMOTIVE_TOKEN` / `QUANTUM_TOKEN` / `ALERT_CHAT_ID` env vars and backfills
`company_id` onto existing events — so an existing single-company deployment
keeps working untouched. After that, those env vars are seed-only.

## Quick start

> For detailed, step-by-step setup (including how to fill in `.env` and common
> gotchas), see **[SETUP.md](SETUP.md)**.

aiogram 2.x pins `aiohttp 3.8.x`, which has no wheels for Python 3.12+. **Use
Python 3.9–3.11** (developed on 3.11).

```bash
# 1. Clone
git clone https://github.com/aqoyliev/eld-anomaly-bot.git
cd eld-anomaly-bot

# 2. Virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1

# 3. Dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env               # then edit .env (see below)

# 5. Run
python app.py
```

## Configuration

All settings live in `.env` (gitignored, never committed):

| Variable                 | Default                                          | Description |
|--------------------------|--------------------------------------------------|-------------|
| `BOT_TOKEN`              | —                                                | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `ADMINS`                 | —                                                | Comma-separated Telegram admin user IDs |
| `QUANTUM_BASE_URL`       | `https://api.quantumeld.com/logger/external`     | Quantum ELD API base URL |
| `QUANTUM_TOKEN`          | —                                                | **Seed-only:** Quantum token for the auto-seeded `default` company (see [Multi-company](#multi-company)) |
| `GOMOTIVE_BASE_URL`      | `https://api.gomotive.com`                       | GoMotive API base URL (constant for all companies) |
| `GOMOTIVE_TOKEN`         | —                                                | **Seed-only:** Motive API key for the `default` company |
| `SAMSARA_BASE_URL`       | `https://api.samsara.com`                        | Samsara API base URL (constant for all companies) |
| `SAMSARA_TOKEN`          | —                                                | **Seed-only:** Samsara API token for the `default` company |
| `ALERT_CHAT_ID`          | first admin                                      | **Seed-only:** alert chat for the `default` company. New companies are bound with `/bindhere`. |
| `POLL_INTERVAL`          | `300`                                            | Seconds between poll cycles |
| `ELD_STALE_THRESHOLD`    | `600`                                            | Seconds before a Quantum report counts as disconnected |
| `MOVING_SPEED_THRESHOLD` | `15`                                             | mph above which a vehicle is "moving" |
| `DATABASE_URL`           | — (unset)                                        | Postgres DSN. If set, the bot uses PostgreSQL (asyncpg); Railway's Postgres plugin injects this. Unset → SQLite. |
| `DB_PATH`                | `db.sqlite3`                                     | SQLite database file (used when `DATABASE_URL` is unset) |

Deploying to Railway? See **[SETUP.md → Deploy to Railway](SETUP.md#deploy-to-railway)**.

## Project structure

```
.
├── app.py                     # Entry point; starts polling + background poller
├── loader.py                  # Bot & Dispatcher
├── data/config.py             # Reads settings from .env
├── handlers/users/
│   ├── status.py              # /status (scoped to the chat's company)
│   ├── history.py             # /history
│   └── company_admin.py       # admin: /addcompany wizard, /bindhere, /companies, …
├── filters/is_admin.py        # IsAdmin filter (gates admin commands by ADMINS)
├── states/company.py          # FSM states for the /addcompany wizard
├── utils/eld/
│   ├── gomotive.py            # Motive client (v1+v3, pagination, kph→mph)
│   ├── samsara.py             # Samsara client (same interface as gomotive)
│   ├── quantumeld.py          # Quantum ELD per-vehicle lookups
│   ├── detector.py            # Anomaly cross-reference
│   ├── store.py               # companies + events; per-company scoping, seed/backfill
│   ├── poller.py              # 5-min loop, per company
│   ├── tracker.py             # 2-min loop: resolve / stopped / reminders, per company
│   └── formatting.py          # Alert / status / history rendering
└── scripts/
    ├── quantum_probe.py       # One-shot Quantum API check
    ├── quantum_watch.py       # Poll until a Quantum token is authorized
    └── mock_alert.py          # Send a test alert through the real pipeline
```

## Helper scripts

```bash
# Check the Quantum token / endpoint
python scripts/quantum_probe.py

# Check a Samsara token (per-vehicle GPS summary; --raw for the raw JSON)
python scripts/samsara_probe.py --token <api-token>

# Send a test alert using a real moving truck (mocks only the Quantum side)
python scripts/mock_alert.py            # picks a moving truck, runs 2 cycles
python scripts/mock_alert.py --cleanup  # remove test events afterwards
```

## Notes & assumptions

- **Matching is by unit number** (GoMotive `vehicle.number` / Samsara `name`
  ↔ Quantum `unit_number`), with owner/tag suffixes stripped (e.g. `"0942  O/O"` →
  `"0942"`). VIN is intentionally **not** the join key because the two systems
  occasionally disagree on a VIN (data-entry transpositions).
- **Times are UTC.** Quantum report times are UTC (naive); staleness and
  durations are computed in UTC and displayed with a `UTC` label.
- **Pagination is total-independent** — the Motive fleet list is live and its
  `pagination.total` can change between page requests, so paging stops on a
  short/empty page and de-duplicates by vehicle id.

## License

[MIT](LICENSE) — built on the
[aiogram](https://docs.aiogram.dev/) 2.x bot template.
