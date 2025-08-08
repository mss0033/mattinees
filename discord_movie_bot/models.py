"""Data models for the Discord movie night bot."""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# -------------------- Scheduling primitives --------------------

@dataclass
class DayTimeRange:
    day: str             # "Monday".."Sunday"
    start_time: str      # "HH:MM" 24h
    end_time: str        # "HH:MM" 24h

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DayTimeRange":
        return cls(**data)


@dataclass
class ScheduleSlot:
    day: str
    start_time: str
    end_time: str
    participants: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduleSlot":
        return cls(**data)


# -------------------- Movie metadata --------------------

@dataclass
class MovieContentAdvisory:
    region: str                 # e.g., "US"
    certification: str          # e.g., "PG-13"
    descriptors: List[str]      # e.g., ["Violence", "Language"]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MovieContentAdvisory":
        return cls(**data)


@dataclass
class Movie:
    tmdb_id: int
    title: str
    year: str
    runtime: int           # minutes
    overview: str
    poster_url: str
    trailer_url: Optional[str] = None
    advisory: Optional[MovieContentAdvisory] = None  # certification + descriptors

    # Extra fields to support better browsing/search
    genres: List[str] = field(default_factory=list)
    original_language: str = "en"
    popularity: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.advisory:
            d["advisory"] = self.advisory.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Movie":
        adv = data.get("advisory")
        advisory = MovieContentAdvisory.from_dict(adv) if adv else None
        return cls(
            tmdb_id=int(data["tmdb_id"]),
            title=data["title"],
            year=data.get("year", ""),
            runtime=int(data.get("runtime", 0)),
            overview=data.get("overview", ""),
            poster_url=data.get("poster_url", ""),
            trailer_url=data.get("trailer_url"),
            advisory=advisory,
            genres=list(data.get("genres", [])),
            original_language=data.get("original_language", "en"),
            popularity=float(data.get("popularity", 0.0)),
        )


# -------------------- Users / tickets --------------------

@dataclass
class TicketHolder:
    user_id: int
    user_name: str
    seat: Optional[int] = None                  # 1..MAX_SEATS
    movie_vote: Optional[int] = None            # tmdb_id
    time_vote: Optional[int] = None             # index into active time options
    availability: List[DayTimeRange] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["availability"] = [r.to_dict() for r in self.availability]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TicketHolder":
        av = [DayTimeRange.from_dict(x) for x in data.get("availability", [])]
        return cls(
            user_id=int(data["user_id"]),
            user_name=data.get("user_name", "Unknown"),
            seat=data.get("seat"),
            movie_vote=data.get("movie_vote"),
            time_vote=data.get("time_vote"),
            availability=av,
        )


# -------------------- Movie requests --------------------

@dataclass
class MovieRequest:
    request_id: int
    user_id: int
    user_name: str
    query: str
    note: Optional[str] = None
    resolved_tmdb_id: Optional[int] = None
    status: str = "pending"  # pending, approved, denied

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MovieRequest":
        return cls(**data)


# -------------------- Global state --------------------

@dataclass
class State:
    movies: Dict[int, Movie] = field(default_factory=dict)            # tmdb_id -> Movie
    ticket_holders: Dict[int, TicketHolder] = field(default_factory=dict)  # user_id -> TicketHolder
    movie_options: List[int] = field(default_factory=list)            # active candidate tmdb_ids (≤3)
    time_options: List[ScheduleSlot] = field(default_factory=list)    # active candidate time slots (≤3)
    waitlist: List[int] = field(default_factory=list)                 # queue of user_ids

    # nominations: movie_id -> unique user_id list
    nominations: Dict[int, List[int]] = field(default_factory=dict)

    # requests: request_id -> MovieRequest
    movie_requests: Dict[int, MovieRequest] = field(default_factory=dict)
    next_request_id: int = 1

    version: int = 2  # bump schema

    def to_dict(self) -> Dict[str, Any]:
        return {
            "movies": {str(k): v.to_dict() for k, v in self.movies.items()},
            "ticket_holders": {str(k): v.to_dict() for k, v in self.ticket_holders.items()},
            "movie_options": self.movie_options,
            "time_options": [s.to_dict() for s in self.time_options],
            "waitlist": self.waitlist,
            "nominations": {str(mid): uids for mid, uids in self.nominations.items()},
            "movie_requests": {str(rid): req.to_dict() for rid, req in self.movie_requests.items()},
            "next_request_id": self.next_request_id,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "State":
        movies = {int(k): Movie.from_dict(v) for k, v in data.get("movies", {}).items()}
        holders = {int(k): TicketHolder.from_dict(v) for k, v in data.get("ticket_holders", {}).items()}
        time_opts = [ScheduleSlot.from_dict(s) for s in data.get("time_options", [])]
        reqs = {int(k): MovieRequest.from_dict(v) for k, v in data.get("movie_requests", {}).items()}
        return cls(
            movies=movies,
            ticket_holders=holders,
            movie_options=[int(x) for x in data.get("movie_options", [])],
            time_options=time_opts,
            waitlist=[int(x) for x in data.get("waitlist", [])],
            nominations={int(mid): [int(u) for u in uids] for mid, uids in data.get("nominations", {}).items()},
            movie_requests=reqs,
            next_request_id=int(data.get("next_request_id", 1)),
            version=int(data.get("version", 1)),
        )
