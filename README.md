# eld-anomaly-bot

A Telegram bot that detects **ELD-disconnection anomalies / fraud** in a
logistics fleet by cross-referencing two telematics sources:

- **GoMotive (Motive)** — ground-truth vehicle movement (speed + location)
- **GreenLight ELD (GL ELD)** — the electronic logging device that *should* be
  recording the trip

If a truck is **moving on GoMotive** while its **GreenLight ELD has stopped
reporting** (its last report is stale), the ELD looks disconnected/offline while
the vehicle keeps driving — the classic disconnection-fraud pattern. The bot
fires a 🚨 alert to a Telegram channel.

## How it works

Every 5 minutes the poller runs this pipeline:

1. **Ask GoMotive which vehicles are moving** — page through
   `/v3/vehicle_locations` (preferred, Vehicle Gateway feed) with a `/v1`
   fallback for vehicles v3 doesn't return. Speeds are normalised to mph; only
   vehicles with `speed > 0` are kept.
2. **Look each moving truck up in GreenLight** by unit number
   (`GET /vehicles/{unit_number}`), reading its last report time.
3. **Flag an anomaly** when the GreenLight report is older than
   `ELD_STALE_THRESHOLD` (default 10 min) while the truck is moving.
4. **Alert, de-duplicate, and track** — a new anomaly sends one alert and opens
   an event; ongoing anomalies update silently (no duplicate alerts); when a
   truck starts reporting again the event auto-resolves.

```
GoMotive (moving vehicles) ──► GreenLight lookup per unit ──► stale? ──► 🚨 alert
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

Disconnected on GreenLight ELD but still moving on GoMotive.
```

## Commands

| Command   | Description                                  |
|-----------|----------------------------------------------|
| `/status` | List vehicles currently flagged as anomalous |
| `/history`| Recent disconnection events (last 20)        |
| `/start`  | Start the bot                                |
| `/help`   | Help                                         |

## Quick start

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
| `GREENLIGHT_BASE_URL`    | `https://api.greenlighteld.com/logger/external`  | GreenLight API base URL |
| `GREENLIGHT_TOKEN`       | —                                                | Carrier-issued GreenLight token (Bearer) |
| `GOMOTIVE_BASE_URL`      | `https://api.gomotive.com`                       | GoMotive API base URL |
| `GOMOTIVE_TOKEN`         | —                                                | Motive API key (sent as `X-Api-Key`) |
| `ALERT_CHAT_ID`          | first admin                                      | Chat/channel ID that receives alerts (e.g. `-1001234567890`) |
| `POLL_INTERVAL`          | `300`                                            | Seconds between poll cycles |
| `ELD_STALE_THRESHOLD`    | `600`                                            | Seconds before a GreenLight report counts as disconnected |
| `MOVING_SPEED_THRESHOLD` | `0`                                              | mph above which a vehicle is "moving" |
| `DB_PATH`                | `db.sqlite3`                                     | SQLite database file |

## Project structure

```
.
├── app.py                     # Entry point; starts polling + background poller
├── loader.py                  # Bot & Dispatcher
├── data/config.py             # Reads settings from .env
├── handlers/users/
│   ├── status.py              # /status
│   └── history.py             # /history
├── utils/eld/
│   ├── gomotive.py            # Motive client (v1+v3, pagination, kph→mph)
│   ├── greenlight.py          # GreenLight per-vehicle lookups
│   ├── detector.py            # Anomaly cross-reference
│   ├── store.py               # SQLite events (dedup + auto-resolve)
│   ├── poller.py              # 5-min loop
│   └── formatting.py          # Alert / status / history rendering
└── scripts/
    ├── greenlight_probe.py    # One-shot GreenLight API check
    ├── greenlight_watch.py    # Poll until a GreenLight token is authorized
    └── mock_alert.py          # Send a test alert through the real pipeline
```

## Helper scripts

```bash
# Check the GreenLight token / endpoint
python scripts/greenlight_probe.py

# Send a test alert using a real moving truck (mocks only the GreenLight side)
python scripts/mock_alert.py            # picks a moving truck, runs 2 cycles
python scripts/mock_alert.py --cleanup  # remove test events afterwards
```

## Notes & assumptions

- **Matching is by unit number** (GoMotive `vehicle.number` ↔ GreenLight
  `unit_number`), with owner/tag suffixes stripped (e.g. `"0942  O/O"` →
  `"0942"`). VIN is intentionally **not** the join key because the two systems
  occasionally disagree on a VIN (data-entry transpositions).
- **Times are UTC.** GreenLight report times are UTC (naive); staleness and
  durations are computed in UTC and displayed with a `UTC` label.
- **Pagination is total-independent** — the Motive fleet list is live and its
  `pagination.total` can change between page requests, so paging stops on a
  short/empty page and de-duplicates by vehicle id.

## License

[MIT](LICENSE) — built on the
[aiogram](https://docs.aiogram.dev/) 2.x bot template.
