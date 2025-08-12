from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .config import TMDB_API_KEY
from .models import Movie, MovieAdvisory

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# We use the v3 API key via the "api_key" query param (NOT the read access token).
# Session is created lazily and closed via close_session().
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def _ensure_session() -> aiohttp.ClientSession:
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession()
        return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


async def _tmdb_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not TMDB_API_KEY:
        raise RuntimeError("TMDB_API_KEY is not set in the environment/.env")
    sess = await _ensure_session()
    p = dict(params or {})
    p["api_key"] = TMDB_API_KEY
    url = f"{TMDB_API_BASE}{path}"
    async with sess.get(url, params=p, timeout=20) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"TMDb GET {path} failed: {resp.status} {text}")
        return await resp.json()


def _pick_certification_and_descriptors(release_dates: Dict[str, Any]) -> Optional[MovieAdvisory]:
    """
    Parse /movie/{id}/release_dates to select the best certification + descriptors.
    Preference order by region: US, GB, CA, AU, else first available.
    Within a region, prefer type=3 (Theatrical), then 2,1,4,5,6.
    """
    if not release_dates or "results" not in release_dates:
        return None

    region_priority = ["US", "GB", "CA", "AU"]
    type_order = {3: 0, 2: 1, 1: 2, 4: 3, 5: 4, 6: 5}  # lower is better

    def best_for_region(entries: List[Dict[str, Any]]) -> Optional[Tuple[str, List[str]]]:
        # entries is list of release_dates dicts with fields: certification, type, descriptors
        # we want the one with non-empty certification; break ties by type_order
        candidates = []
        for e in entries:
            cert = (e.get("certification") or "").strip()
            if not cert:
                continue
            t = int(e.get("type") or 0)
            descriptors = e.get("descriptors") or []
            candidates.append((type_order.get(t, 99), cert, descriptors))
        if not candidates:
            # If descriptors exist even without certification, we can still return them (rare)
            for e in entries:
                desc = e.get("descriptors") or []
                if desc:
                    return ("NR", list(desc))
            return None
        candidates.sort(key=lambda x: x[0])
        _, cert, desc = candidates[0]
        return (cert, list(desc or []))

    best_region = None
    best_value: Optional[Tuple[str, List[str]]] = None

    # Scan preferred regions first
    for r in region_priority:
        region_entry = next((x for x in release_dates["results"] if x.get("iso_3166_1") == r), None)
        if not region_entry:
            continue
        val = best_for_region(region_entry.get("release_dates") or [])
        if val:
            best_region = r
            best_value = val
            break

    # Fallback: first region with a viable certification
    if not best_value:
        for region_entry in release_dates["results"]:
            r = region_entry.get("iso_3166_1") or "XX"
            val = best_for_region(region_entry.get("release_dates") or [])
            if val:
                best_region = r
                best_value = val
                break

    if not best_value:
        return None

    cert, descriptors = best_value
    return MovieAdvisory(certification=cert, region=best_region or "XX", descriptors=descriptors)


def _extract_trailer(videos: Dict[str, Any]) -> Optional[str]:
    """
    From /movie/{id}?append_to_response=videos, pick the best YouTube trailer.
    Prefer official trailers; fallback to first YouTube trailer.
    """
    try:
        results = videos.get("results") if isinstance(videos, dict) else None
        if not results:
            return None
        # official YouTube trailer
        for v in results:
            if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("official") is True:
                key = v.get("key")
                if key:
                    return f"https://www.youtube.com/watch?v={key}"
        # any YouTube trailer
        for v in results:
            if v.get("site") == "YouTube" and v.get("type") == "Trailer":
                key = v.get("key")
                if key:
                    return f"https://www.youtube.com/watch?v={key}"
    except Exception:
        return None
    return None


def _poster_url_from_path(poster_path: Optional[str]) -> Optional[str]:
    if not poster_path:
        return None
    return f"{TMDB_IMAGE_BASE}{poster_path}"


def _year_from_date(date_str: Optional[str]) -> str:
    if not date_str:
        return ""
    try:
        return date_str[:4]
    except Exception:
        return ""


async def _build_movie_from_details(details: Dict[str, Any]) -> Optional[Movie]:
    if not details:
        return None

    tmdb_id = details.get("id")
    if tmdb_id is None:
        return None

    title = details.get("title") or details.get("name") or ""
    year = _year_from_date(details.get("release_date") or details.get("first_air_date"))
    runtime = int(details.get("runtime") or 0)
    overview = details.get("overview") or ""
    genres = [g.get("name") for g in (details.get("genres") or []) if g.get("name")]
    poster_url = _poster_url_from_path(details.get("poster_path"))
    trailer_url = _extract_trailer(details.get("videos") or {})

    advisory = _pick_certification_and_descriptors(details.get("release_dates") or {})

    return Movie(
        tmdb_id=int(tmdb_id),
        title=title,
        year=str(year),
        runtime=runtime,
        overview=overview,
        genres=genres,
        poster_url=poster_url,
        trailer_url=trailer_url,
        advisory=advisory,
    )


async def build_movie_from_tmdb_id(tmdb_id: int) -> Optional[Movie]:
    """
    Fetch a movie by TMDb ID, including videos and release dates:
      GET /movie/{id}?append_to_response=videos,release_dates
    """
    params = {"append_to_response": "videos,release_dates", "language": "en-US"}
    details = await _tmdb_get(f"/movie/{tmdb_id}", params)
    return await _build_movie_from_details(details)


async def build_movie_from_title(title: str) -> Optional[Movie]:
    """
    Search for a title and then fetch details for the top hit.
      GET /search/movie?query=...
      GET /movie/{id}?append_to_response=videos,release_dates
    """
    q = (title or "").strip()
    if not q:
        return None
    search = await _tmdb_get("/search/movie", {"query": q, "include_adult": "false", "language": "en-US"})
    results = search.get("results") or []
    if not results:
        return None
    # pick the best result (highest popularity, then most recent year)
    results.sort(key=lambda r: (float(r.get("popularity") or 0.0), _year_from_date(r.get("release_date"))), reverse=True)
    top = results[0]
    mid = top.get("id")
    if not mid:
        return None
    return await build_movie_from_tmdb_id(int(mid))
