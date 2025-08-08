"""Persistence for the bot's unified State (atomic JSON, autosave, lock) + backups."""

from __future__ import annotations
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from .models import State
from .config import STATE_FILE, AUTOSAVE_INTERVAL, BACKUPS_DIR, MAX_BACKUPS

# Single shared lock for writes
_state_lock = asyncio.Lock()

async def load_state() -> State:
    """Load state from STATE_FILE; return empty State if missing/corrupt."""
    try:
        if not STATE_FILE.exists():
            return State()
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return State.from_dict(data)
    except Exception:
        return State()

async def save_state(state: State) -> None:
    """Atomically save the entire state.json under a lock."""
    payload = state.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = Path(str(STATE_FILE) + ".tmp")
    async with _state_lock:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(STATE_FILE)

async def autosave_loop(state: State) -> None:
    """Background task to periodically save the state."""
    while True:
        await asyncio.sleep(AUTOSAVE_INTERVAL)
        try:
            await save_state(state)
        except Exception as e:
            print(f"[autosave] Failed to save state: {e}")

# -------------------- Backups --------------------

def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")

def _backup_filename(ts: str) -> Path:
    return BACKUPS_DIR / f"state-{ts}.json"

def _list_backup_files() -> List[Path]:
    return sorted(BACKUPS_DIR.glob("state-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

async def backup_state(state: State) -> str:
    """Write a timestamped backup of the current state. Returns backup file name (basename)."""
    ts = _timestamp()
    path = _backup_filename(ts)
    payload = state.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    async with _state_lock:
        path.write_text(text, encoding="utf-8")
    # prune old backups
    files = _list_backup_files()
    for old in files[MAX_BACKUPS:]:
        try:
            old.unlink(missing_ok=True)
        except Exception:
            pass
    return path.name

async def list_backups() -> List[str]:
    """Return backup basenames in newest-first order."""
    return [p.name for p in _list_backup_files()]

async def restore_state_from_backup(backup_name: str) -> State:
    """Restore STATE_FILE from a backup file (by basename). Returns loaded State."""
    candidate = BACKUPS_DIR / backup_name
    if not candidate.exists():
        raise FileNotFoundError("Backup not found.")
    text = candidate.read_text(encoding="utf-8")
    data = json.loads(text)
    new_state = State.from_dict(data)
    # write atomically
    tmp = Path(str(STATE_FILE) + ".tmp")
    async with _state_lock:
        tmp.write_text(json.dumps(new_state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    return new_state
