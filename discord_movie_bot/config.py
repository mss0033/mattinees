"""Configuration for the Discord movie night bot (slash-commands version, dotenv)."""

from __future__ import annotations
import os
from pathlib import Path

# Load .env from project root
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- Credentials / API keys ---
DISCORD_TOKEN: str | None = os.getenv("DISCORD_TOKEN")
TMDB_API_KEY: str | None = os.getenv("TMDB_API_KEY")  # TMDb v3 API key

# --- Behaviour / defaults ---
MAX_SEATS: int = int(os.getenv("MAX_SEATS", "3"))  # excludes host
AUTOSAVE_INTERVAL: int = int(os.getenv("AUTOSAVE_INTERVAL", "300"))
TIMEZONE: str = os.getenv("TIMEZONE", "America/New_York")

# During development, set a DEV_GUILD_ID for fast slash-command sync (instant).
DEV_GUILD_ID: int | None = int(os.getenv("DEV_GUILD_ID", "0")) or None

# --- Data paths ---
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

# Backups (for undo/restore)
BACKUPS_DIR = DATA_DIR / "backups"
BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
MAX_BACKUPS: int = int(os.getenv("MAX_BACKUPS", "12"))  # keep last N backups

__all__ = [
    "DISCORD_TOKEN",
    "TMDB_API_KEY",
    "MAX_SEATS",
    "AUTOSAVE_INTERVAL",
    "TIMEZONE",
    "DEV_GUILD_ID",
    "DATA_DIR",
    "STATE_FILE",
    "BACKUPS_DIR",
    "MAX_BACKUPS",
]
