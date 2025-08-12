from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


# ---------- Movie metadata ----------

@dataclass
class MovieAdvisory:
    certification: str
    region: str
    descriptors: List[str] = field(default_factory=list)


@dataclass
class Movie:
    tmdb_id: int
    title: str
    year: str
    runtime: int
    overview: str
    genres: List[str] = field(default_factory=list)
    poster_url: Optional[str] = None
    trailer_url: Optional[str] = None
    advisory: Optional[MovieAdvisory] = None


# ---------- Availability ----------

@dataclass
class DayTimeRange:
    day: str            # Full day name: Monday..Sunday
    start_time: str     # "HH:MM" 24h
    end_time: str       # "HH:MM" 24h


# ---------- Scheduling ----------

@dataclass
class ScheduleSlot:
    day: str
    start_time: str
    end_time: str
    participants: List[int] = field(default_factory=list)
    key: str = ""  # stable identity: e.g., "slot:Monday|18:00|22:00"


# ---------- Tickets / Users ----------

@dataclass
class TicketHolder:
    user_id: int
    user_name: str
    seat: Optional[int] = None
    movie_vote: Optional[int] = None               # TMDb ID of choice
    time_vote: Optional[str] = None                # slot key (not index anymore)
    availability: List[DayTimeRange] = field(default_factory=list)


# ---------- Requests ----------

@dataclass
class MovieRequest:
    request_id: int
    user_id: int
    user_name: str
    query: str
    note: Optional[str] = None
    status: str = "pending"                        # pending|approved|denied
    resolved_tmdb_id: Optional[int] = None


# ---------- Message references (for single active ballots) ----------

@dataclass
class MessageRef:
    channel_id: int
    message_id: int


# ---------- Global State ----------

@dataclass
class State:
    movies: Dict[int, Movie] = field(default_factory=dict)                  # TMDb ID -> Movie
    nominations: Dict[int, List[int]] = field(default_factory=dict)         # TMDb ID -> [user_ids]
    ticket_holders: Dict[int, TicketHolder] = field(default_factory=dict)   # user_id -> TicketHolder

    movie_options: List[int] = field(default_factory=list)                  # active movie ballot (TMDb IDs, ≤3)
    time_options: List[ScheduleSlot] = field(default_factory=list)          # active schedule slots (≤3)

    waitlist: List[int] = field(default_factory=list)                       # user_ids

    movie_requests: Dict[int, MovieRequest] = field(default_factory=dict)   # request_id -> MovieRequest
    next_request_id: int = 1

    # New (single active ballot messages)
    active_movie_ballot_message: Optional[MessageRef] = None
    active_time_ballot_message: Optional[MessageRef] = None


# --------- Helpers for (de)serialization ---------

def state_to_dict(s: State) -> dict:
    """Convert State to JSON-serializable dict."""
    def _dataclass_to_dict(obj):
        if hasattr(obj, "__dataclass_fields__"):
            d = asdict(obj)
            return d
        return obj

    data = _dataclass_to_dict(s)
    return data
