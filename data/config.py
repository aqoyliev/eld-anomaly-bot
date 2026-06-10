from environs import Env

# Use the environs library
env = Env()
env.read_env()

# Read the following from the .env file
BOT_TOKEN = env.str("BOT_TOKEN")  # Bot token
ADMINS = env.list("ADMINS")  # List of admins (full access)
# Read-only viewers: may DM the bot for /status and /history only (no admin
# commands). Telegram numeric user ids, same format as ADMINS. Optional.
VIEWERS = env.list("VIEWERS", [])
IP = env.str("ip", "")  # Hosting IP address (unused; long-polling bot)

# --- ELD anomaly detection ---------------------------------------------------

# LEGACY / SEED-ONLY: QUANTUM_TOKEN, GOMOTIVE_TOKEN and ALERT_CHAT_ID are no
# longer read by the live poll/track loops — those run per-company from the
# `companies` DB table (see utils/eld/store.py, managed via the in-bot admin
# commands /addcompany, /bindhere, etc.). On first start against a DB with no
# companies, init_db() seeds a single "default" company from these values so the
# original single-company production setup keeps working untouched. New companies
# are added with the admin commands instead. The two *_BASE_URL constants are
# the GoMotive and Quantum ELD base URLs — both constant for all companies (the
# per-company Quantum base-URL override was removed; all companies use the same
# Quantum endpoint).

# Quantum ELD vehicle-location API
QUANTUM_BASE_URL = env.str(
    "QUANTUM_BASE_URL", "https://api.quantumeld.com/logger/external"
)
QUANTUM_TOKEN = env.str("QUANTUM_TOKEN", "")  # seed-only (see note above)

# GoMotive (Motive) fleet API — ground-truth movement / speed
GOMOTIVE_BASE_URL = env.str("GOMOTIVE_BASE_URL", "https://api.gomotive.com")
GOMOTIVE_TOKEN = env.str("GOMOTIVE_TOKEN", "")  # seed-only (see note above)

# Chat/channel that receives the anomaly alerts (e.g. -1001234567890).
# Falls back to the first admin if not set. Seed-only (see note above).
ALERT_CHAT_ID = env.str("ALERT_CHAT_ID", ADMINS[0] if ADMINS else "")

# How often to poll both APIs, in seconds (default 5 minutes).
POLL_INTERVAL = env.int("POLL_INTERVAL", 300)

# A Quantum report older than this many seconds means the ELD is treated as
# disconnected / offline (default 10 minutes).
ELD_STALE_THRESHOLD = env.int("ELD_STALE_THRESHOLD", 600)

# Speed (mph) above which a vehicle is considered "moving" on GoMotive.
MOVING_SPEED_THRESHOLD = env.float("MOVING_SPEED_THRESHOLD", 0.0)

# --- Tracking of already-flagged (disconnected) units ------------------------
# How often the tracker re-checks disconnected units, in seconds (default 2 min).
# It re-queries GoMotive by vehicle id (cheap) plus Quantum for reconnection.
TRACK_INTERVAL = env.int("TRACK_INTERVAL", 120)
# Re-notify the group about a still-MOVING disconnected unit this often (default
# 10 min). Stopped units get no reminder — their anomaly is closed on stop.
REMINDER_INTERVAL = env.int("REMINDER_INTERVAL", 600)
# A flagged unit is treated as stopped/pulled over when BOTH hold between two
# tracker checks: it moved less than STOP_DISPLACEMENT_MI miles AND its current
# speed is at/below STOP_SPEED_MPH. The 2-min gap filters out red lights.
STOP_DISPLACEMENT_MI = env.float("STOP_DISPLACEMENT_MI", 0.05)
STOP_SPEED_MPH = env.float("STOP_SPEED_MPH", 1.0)

# Persistence for flagged vehicles and event history.
# If DATABASE_URL is set (Railway provides this for a Postgres plugin), the bot
# uses PostgreSQL via asyncpg; otherwise it falls back to a local SQLite file at
# DB_PATH. Accepts postgres:// or postgresql:// DSNs.
DATABASE_URL = env.str("DATABASE_URL", "")
DB_PATH = env.str("DB_PATH", "db.sqlite3")
