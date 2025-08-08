"""Configuration for the Discord movie night bot (slash-commands version, dotenv)."""

from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv()

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

__all__ = [
    "DISCORD_TOKEN",
    "TMDB_API_KEY",
    "MAX_SEATS",
    "AUTOSAVE_INTERVAL",
    "TIMEZONE",
    "DEV_GUILD_ID",
    "DATA_DIR",
    "STATE_FILE",
]
