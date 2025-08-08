"""TMDb API wrapper (HTTPS, shared aiohttp session, v3 API key).

We use:
- /search/movie          to find a TMDb ID from a title.
- /movie/{id}            for details (runtime, overview, poster path).
- /movie/{id}/videos     for trailers (YouTube key).
- /movie/{id}/release_dates  for certifications and *descriptors* (content reasons).

Docs:
- Getting started & v3 endpoints: https://developer.themoviedb.org/docs/getting-started
- Movie details: https://developer.themoviedb.org/reference/movie-details
- Release dates + certifications: https://developer.themoviedb.org/reference/movie-release-dates
- Movie certifications list: https://developer.themoviedb.org/reference/certification-movie-list
(See those pages for current parameter names and formats.)
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
import asyncio
import aiohttp
from urllib.parse import quote_plus

from .config import TMDB_API_KEY
from .models import Movie, MovieContentAdvisory

_API_BASE = "https://api.themoviedb.org/3"
_IMAGE_BASE_FALLBACK = "https://image.tmdb.org/t/p"  # we’ll use /w500 fallback

_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()

async def _get_session() -> aiohttp.ClientSession:
    global _session
    async with _session_lock:
        if _session and not _session.closed:
            return _session
        timeout = aiohttp.ClientTimeout(total=10)
        _session = aiohttp.ClientSession(timeout=timeout)
        return _session

async def close_session() -> None:
    global _session
    async with _session_lock:
        if _session and not _session.closed:
            await _session.close()
        _session = None

def _require_key():
    if not TMDB_API_KEY:
        raise RuntimeError("TMDB_API_KEY is not configured.")

async def _get(path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    _require_key()
    params = params or {}
    params["api_key"] = TMDB_API_KEY  # using v3 API key
    url = f"{_API_BASE}{path}"
    s = await _get_session()
    async with s.get(url, params=params, ssl=True) as resp:
        resp.raise_for_status()
        return await resp.json()

def poster_url_from_path(poster_path: Optional[str], size: str = "w500") -> str:
    """Build a poster URL from TMDb poster_path."""
    if not poster_path or poster_path == "N/A":
        return ""
    return f"{_IMAGE_BASE_FALLBACK}/{size}{poster_path}"

async def search_movie(title: str) -> List[Dict[str, Any]]:
    """Return a list of TMDb search results for a title."""
    data = await _get("/search/movie", {"query": title, "include_adult": "false"})
    return data.get("results", [])

async def get_movie_details(tmdb_id: int) -> Dict[str, Any]:
    """Raw TMDb 'movie details' JSON."""
    return await _get(f"/movie/{tmdb_id}")

async def get_movie_videos(tmdb_id: int) -> List[Dict[str, Any]]:
    data = await _get(f"/movie/{tmdb_id}/videos")
    return data.get("results", [])

async def get_movie_release_dates(tmdb_id: int) -> List[Dict[str, Any]]:
    """Returns list of country entries; each has 'iso_3166_1' and 'release_dates' array.
    Each release_date entry may include 'certification' and optional 'descriptors'."""
    data = await _get(f"/movie/{tmdb_id}/release_dates")
    return data.get("results", [])

def _pick_trailer(videos: List[Dict[str, Any]]) -> Optional[str]:
    # Prefer official YouTube trailer
    for v in videos:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            key = v.get("key")
            if key:
                return f"https://www.youtube.com/watch?v={key}"
    return None

def _extract_advisory(release_dates: List[Dict[str, Any]], preferred_regions: Tuple[str, ...] = ("US","GB","CA")) -> Optional[MovieContentAdvisory]:
    """Pick the best certification/descriptors from release dates.

    TMDb 'release_dates' entries include:
      {
        "iso_3166_1": "US",
        "release_dates": [
            {
                "certification": "PG-13",
                "descriptors": ["violence","language"],   # field presence varies
                ...
            }, ...
        ]
      }
    We take the first entry with a non-empty certification; collect descriptors if present.
    """
    for region in preferred_regions:
        for entry in release_dates:
            if entry.get("iso_3166_1") != region:
                continue
            for rd in entry.get("release_dates", []):
                cert = (rd.get("certification") or "").strip()
                if cert:
                    desc = rd.get("descriptors") or []
                    # normalize descriptors to Title Case for display
                    norm = [str(x).strip().title() for x in desc if str(x).strip()]
                    return MovieContentAdvisory(region=region, certification=cert, descriptors=norm)
    # fallback: pick any certification
    for entry in release_dates:
        for rd in entry.get("release_dates", []):
            cert = (rd.get("certification") or "").strip()
            if cert:
                return MovieContentAdvisory(region=entry.get("iso_3166_1",""), certification=cert, descriptors=rd.get("descriptors") or [])
    return None

async def build_movie_from_tmdb_id(tmdb_id: int) -> Optional[Movie]:
    """Fetch full movie info and build a Movie dataclass."""
    details = await get_movie_details(tmdb_id)
    if not details:
        return None
    title = details.get("title") or details.get("original_title") or "Unknown"
    year = (details.get("release_date") or "")[:4]
    runtime = int(details.get("runtime") or 0)
    overview = details.get("overview") or ""
    poster_url = poster_url_from_path(details.get("poster_path"))
    videos = await get_movie_videos(tmdb_id)
    trailer_url = _pick_trailer(videos)
    rel = await get_movie_release_dates(tmdb_id)
    advisory = _extract_advisory(rel)
    return Movie(
        tmdb_id=tmdb_id,
        title=title,
        year=year,
        runtime=runtime,
        overview=overview,
        poster_url=poster_url,
        trailer_url=trailer_url,
        advisory=advisory,
    )

async def build_movie_from_title(title_or_id: str) -> Optional[Movie]:
    """Accepts a title; if it's an integer, treat as TMDb id."""
    # allow numeric string tmdb_id too
    try:
        tmdb_id = int(title_or_id)
        return await build_movie_from_tmdb_id(tmdb_id)
    except ValueError:
        pass
    results = await search_movie(title_or_id)
    if not results:
        return None
    # take best hit
    return await build_movie_from_tmdb_id(int(results[0]["id"]))
