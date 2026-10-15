import os
import sys
from pathlib import Path

# utils.eld imports data.config, which requires these at import time.
os.environ.setdefault("BOT_TOKEN", "123456:test")
os.environ.setdefault("ADMINS", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
