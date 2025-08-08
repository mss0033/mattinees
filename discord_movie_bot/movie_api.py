"""OMDb API wrapper for the Discord movie night bot.

This module provides asynchronous functions to search for movies and
retrieve detailed information from the OMDb API.  The OMDb API is a
community‑maintained interface to IMDb data.  See https://www.omdbapi.com
for details.  You must obtain an API key and set it via the
``OMDB_API_KEY`` environment variable (see :mod:`config`).

Errors encountered during API calls are propagated to the caller.  The
functions return ``None`` if the API reports an unsuccessful response.
"""

from __future__ import annotations

import aiohttp
from typing import Optional, Dict, Any

from .config import OMDB_API_KEY


async def _fetch_json(url: str) -> Dict[str, Any]:
    """Helper to perform an HTTP GET and return JSON.

    Raises
    ------
    ValueError
        If the HTTP response status is not 200 or the response cannot be
        decoded as JSON.
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}: {await resp.text()}")
            try:
                return await resp.json()
            except Exception as exc:
                raise ValueError(f"Failed to parse JSON: {exc}")


async def search_movie(title: str) -> Optional[Dict[str, Any]]:
    """Search OMDb for movies matching the given title.

    Parameters
    ----------
    title:
        Movie title to search for.

    Returns
    -------
    Optional[Dict[str, Any]]
        A dictionary containing the search results or ``None`` if no
        results are found.
    """
    if not OMDB_API_KEY:
        raise RuntimeError("OMDB_API_KEY is not configured.")
    query = title.replace(" ", "+")
    url = f"http://www.omdbapi.com/?apikey={OMDB_API_KEY}&s={query}&type=movie"
    data = await _fetch_json(url)
    if data.get("Response") == "True":
        return data
    return None


async def get_movie_details(title_or_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve detailed movie information from OMDb.

    Accepts either an IMDb ID (starting with ``tt``) or a title.  The
    returned dictionary includes fields such as ``Title``, ``Year``,
    ``Runtime``, ``Plot``, ``Rated`` and ``Poster``.  Content warnings
    and trailers are not provided by OMDb and may need to be sourced
    elsewhere.

    Parameters
    ----------
    title_or_id:
        IMDb ID (e.g., ``tt0111161``) or movie title.

    Returns
    -------
    Optional[Dict[str, Any]]
        A dictionary of movie details or ``None`` if the movie is not
        found.
    """
    if not OMDB_API_KEY:
        raise RuntimeError("OMDB_API_KEY is not configured.")
    # Determine whether the argument looks like an IMDb ID
    param_key = "i" if title_or_id.lower().startswith("tt") else "t"
    query = title_or_id.replace(" ", "+")
    url = f"http://www.omdbapi.com/?apikey={OMDB_API_KEY}&{param_key}={query}&plot=full&r=json"
    data = await _fetch_json(url)
    if data.get("Response") == "True":
        return data
    return None

__all__ = [
    "search_movie",
    "get_movie_details",
]
