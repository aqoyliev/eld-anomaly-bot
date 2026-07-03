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

# Samsara fleet API — alternative movement source for companies whose trucks
# carry Samsara devices instead of GoMotive. A company may have either token
# (or both); whichever is set is polled for movement.
SAMSARA_BASE_URL = env.str("SAMSARA_BASE_URL", "https://api.samsara.com")
SAMSARA_TOKEN = env.str("SAMSARA_TOKEN", "")  # seed-only (see note above)

# EVO ELD tracking API — second ELD-side provider (same role as Quantum: report
# freshness = disconnection signal). Per-company credentials (api key, provider
# token, USDOT) live in the companies table; only the base URL is global.
EVO_BASE_URL = env.str("EVO_BASE_URL", "https://read.evoeld.com/api/v2")

# Chat/channel that receives the anomaly alerts (e.g. -1001234567890).
# Falls back to the first admin if not set. Seed-only (see note above).
ALERT_CHAT_ID = env.str("ALERT_CHAT_ID", ADMINS[0] if ADMINS else "")

# How often to poll both APIs, in seconds (default 5 minutes).
POLL_INTERVAL = env.int("POLL_INTERVAL", 300)

# A Quantum report older than this many seconds means the ELD is treated as
# disconnected / offline (default 10 minutes).
ELD_STALE_THRESHOLD = env.int("ELD_STALE_THRESHOLD", 600)

# Speed (mph) above which a vehicle is considered "moving" by the movement
# providers (GoMotive/Samsara). Anything at or below it is ignored by anomaly
# detection — set high enough to skip GPS jitter and yard/dock maneuvering.
MOVING_SPEED_THRESHOLD = env.float("MOVING_SPEED_THRESHOLD", 10.0)

# Samsara's stats snapshot returns the LAST KNOWN GPS reading even if the
# gateway has been offline for months — a dead unit can sit frozen at 60 mph
# forever. A reading older than this many seconds is not treated as current
# movement (default 15 min; a live Samsara gateway reports sub-minute).
SAMSARA_GPS_FRESHNESS = env.int("SAMSARA_GPS_FRESHNESS", 900)

# GoMotive's vehicle_locations endpoint has the SAME pitfall: it returns each
# vehicle's last-known reading (current_location.located_at) even after the
# gateway is powered off, so a truck whose device died days ago keeps reporting
# its final speed and looks like it's still moving. A reading older than this
# many seconds is not treated as current movement (default 15 min; a live
# Motive gateway reports every few minutes). Real case: unit 1264's gateway was
# powered off 6 days yet GoMotive still showed it at speed -> false anomaly.
GOMOTIVE_GPS_FRESHNESS = env.int("GOMOTIVE_GPS_FRESHNESS", 900)

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
