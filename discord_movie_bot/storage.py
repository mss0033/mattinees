from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from .models import (
    DayTimeRange, MessageRef, Movie, MovieAdvisory, MovieRequest, ScheduleSlot,
    State, TicketHolder, state_to_dict,
)
from .scheduling import _slot_key

DATA_DIR = os.path.join(os.getcwd(), "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
BACKUPS_DIR = os.path.join(DATA_DIR, "backups")

MAX_BACKUPS = 12
AUTOSAVE_SEC = 30

_lock = asyncio.Lock()


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUPS_DIR, exist_ok=True)


def _obj_hook(d: Dict[str, Any]) -> Dict[str, Any]:
    """No special hook needed; handled in load_state migration."""
    return d


async def load_state() -> State:
    _ensure_dirs()
    if not os.path.exists(STATE_PATH):
        return State()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f, object_hook=_obj_hook)
    except Exception:
        return State()

    # ---- migration & validation ----
    def as_movie(m: Dict[str, Any]) -> Movie:
        adv = m.get("advisory")
        advisory = MovieAdvisory(**adv) if isinstance(adv, dict) else None
        return Movie(
            tmdb_id=int(m["tmdb_id"]),
            title=m.get("title", ""),
            year=str(m.get("year", "")),
            runtime=int(m.get("runtime", 0) or 0),
            overview=m.get("overview", ""),
            genres=list(m.get("genres", []) or []),
            poster_url=m.get("poster_url"),
            trailer_url=m.get("trailer_url"),
            advisory=advisory,
        )

    def as_range(r: Dict[str, Any]) -> DayTimeRange:
        return DayTimeRange(day=r["day"], start_time=r["start_time"], end_time=r["end_time"])

    def as_slot(s: Dict[str, Any]) -> ScheduleSlot:
        day = s["day"]; st = s["start_time"]; et = s["end_time"]
        key = s.get("key") or _slot_key(day, st, et)
        parts = list(s.get("participants", []) or [])
        return ScheduleSlot(day=day, start_time=st, end_time=et, participants=parts, key=key)

    def as_holder(h: Dict[str, Any]) -> TicketHolder:
        av = [as_range(r) for r in (h.get("availability") or [])]
        tv = h.get("time_vote")
        # migrate time_vote int -> None (stale), we now use string keys
        if isinstance(tv, int):
            tv = None
        return TicketHolder(
            user_id=int(h["user_id"]),
            user_name=h.get("user_name", str(h["user_id"])),
            seat=h.get("seat"),
            movie_vote=h.get("movie_vote"),
            time_vote=tv,
            availability=av
        )

    def as_request(r: Dict[str, Any]) -> MovieRequest:
        return MovieRequest(
            request_id=int(r["request_id"]),
            user_id=int(r["user_id"]),
            user_name=r.get("user_name", str(r["user_id"])),
            query=r.get("query", ""),
            note=r.get("note"),
            status=r.get("status", "pending"),
            resolved_tmdb_id=r.get("resolved_tmdb_id"),
        )

    movies = {int(k): as_movie(v) for k, v in (raw.get("movies") or {}).items()}
    nominations = {int(k): list(v or []) for k, v in (raw.get("nominations") or {}).items()}
    holders = {int(k): as_holder(v) for k, v in (raw.get("ticket_holders") or {}).items()}

    movie_options = [int(x) for x in (raw.get("movie_options") or [])]
    time_options = [as_slot(s) for s in (raw.get("time_options") or [])]

    waitlist = [int(x) for x in (raw.get("waitlist") or [])]
    reqs = {int(k): as_request(v) for k, v in (raw.get("movie_requests") or {}).items()}
    nreq = int(raw.get("next_request_id", 1) or 1)

    def as_msgref(d: Any):
        if not isinstance(d, dict):
            return None
        try:
            return MessageRef(channel_id=int(d["channel_id"]), message_id=int(d["message_id"]))
        except Exception:
            return None

    return State(
        movies=movies,
        nominations=nominations,
        ticket_holders=holders,
        movie_options=movie_options,
        time_options=time_options,
        waitlist=waitlist,
        movie_requests=reqs,
        next_request_id=nreq,
        active_movie_ballot_message=as_msgref(raw.get("active_movie_ballot_message")),
        active_time_ballot_message=as_msgref(raw.get("active_time_ballot_message")),
    )


async def save_state(state: State) -> None:
    _ensure_dirs()
    data = state_to_dict(state)
    # atomic write
    tmp = STATE_PATH + ".tmp"
    async with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)


async def autosave_loop(state: State) -> None:
    while True:
        try:
            await asyncio.sleep(AUTOSAVE_SEC)
            await save_state(state)
        except asyncio.CancelledError:
            break
        except Exception:
            # swallow; next loop will try again
            pass


async def backup_state(state: State) -> str:
    _ensure_dirs()
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"state-{ts}.json"
    path = os.path.join(BACKUPS_DIR, name)
    data = state_to_dict(state)
    async with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    # rotate
    names = sorted(os.listdir(BACKUPS_DIR), reverse=True)
    for old in names[MAX_BACKUPS:]:
        try:
            os.remove(os.path.join(BACKUPS_DIR, old))
        except Exception:
            pass
    return name


async def list_backups() -> List[str]:
    _ensure_dirs()
    try:
        names = sorted(os.listdir(BACKUPS_DIR), reverse=True)
        return names
    except Exception:
        return []


async def restore_state_from_backup(name: str) -> State:
    _ensure_dirs()
    path = os.path.join(BACKUPS_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # Reuse load_state migration by writing temp and reading via load_state()
    tmp = STATE_PATH + ".restore_tmp"
    with open(tmp, "w", encoding="utf-8") as wf:
        json.dump(raw, wf, ensure_ascii=False, indent=2)
    # Atomic replace to trigger the exact same reader
    os.replace(tmp, STATE_PATH)
    return await load_state()
