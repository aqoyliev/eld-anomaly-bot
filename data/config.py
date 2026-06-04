from environs import Env

# Use the environs library
env = Env()
env.read_env()

# Read the following from the .env file
BOT_TOKEN = env.str("BOT_TOKEN")  # Bot token
ADMINS = env.list("ADMINS")  # List of admins
IP = env.str("ip")  # Hosting IP address

# --- ELD anomaly detection ---------------------------------------------------

# GreenLight ELD (GL ELD) vehicle-location API
GREENLIGHT_BASE_URL = env.str(
    "GREENLIGHT_BASE_URL", "https://api.greenlighteld.com/logger/external"
)
GREENLIGHT_TOKEN = env.str("GREENLIGHT_TOKEN", "")

# GoMotive (Motive) fleet API — ground-truth movement / speed
GOMOTIVE_BASE_URL = env.str("GOMOTIVE_BASE_URL", "https://api.gomotive.com")
GOMOTIVE_TOKEN = env.str("GOMOTIVE_TOKEN", "")

# Chat/channel that receives the anomaly alerts (e.g. -1001234567890).
# Falls back to the first admin if not set.
ALERT_CHAT_ID = env.str("ALERT_CHAT_ID", ADMINS[0] if ADMINS else "")

# How often to poll both APIs, in seconds (default 5 minutes).
POLL_INTERVAL = env.int("POLL_INTERVAL", 300)

# A GreenLight report older than this many seconds means the ELD is treated as
# disconnected / offline (default 10 minutes).
ELD_STALE_THRESHOLD = env.int("ELD_STALE_THRESHOLD", 600)

# Speed (mph) above which a vehicle is considered "moving" on GoMotive.
MOVING_SPEED_THRESHOLD = env.float("MOVING_SPEED_THRESHOLD", 0.0)

# SQLite database file for flagged vehicles and event history.
DB_PATH = env.str("DB_PATH", "db.sqlite3")
