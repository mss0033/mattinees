"""Persistence for the bot's unified State (atomic JSON, autosave, lock)."""

from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Optional

from .models import State
from .config import STATE_FILE, AUTOSAVE_INTERVAL

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
