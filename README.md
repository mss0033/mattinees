# Mattinees Bot 🎬 — Movie-Night Scheduler & Vote Manager

A fully-featured **Discord slash-command bot** that lets your server

* Browse / search a local **catalog** of movies (metadata from TMDb)  
* **Nominate** any catalog title, then open a **ballot** (≤ 3 options) and **vote**  
* **Request** movies not yet in the catalog (admin approval flow)  
* Collect individual **availability** (text, presets or wizard) and compute overlap  
* Suggest ≤ 3 **time-slots** and vote on them  
* Manage **seating** (host + 3 guests + wait-list)  
* Persist state to a single atomic JSON file with autosave  
* Zero privileged intents — slash commands only  

---

## 1  Features at a Glance

| Domain | Highlights |
|--------|------------|
| **Catalog** | Bulk import, list (with filters), free-text search, rich *info* embeds |
| **Nominations** | Unlimited nominations → admin/top-3 ballot (max 3) |
| **Voting** | Persistent Select menus for movie & time voting |
| **Requests** | Members file modal requests; admins approve & auto-import |
| **Availability** | Three UIs: text, presets, *wizard* (day select + preset/custom) |
| **Scheduling** | Computes 100 % overlaps first, then most-popular windows |
| **ISO-8601** | Final start/end in server timezone (default America/New_York) |
| **Seats** | 3 guest seats + auto-promoted wait-list |
| **Persistence** | `state.json` (single file, versioned schema, autosave) |
| **Networking** | One shared `aiohttp.ClientSession`, HTTPS, 10 s timeout |
| **Extensible** | Modular ― swap storage, change seat count, add slash groups |

---

## 2  Requirements

* Python 3.11 + (tested on 3.13)  
* Install deps:

~~~bash
pip install discord.py aiohttp matplotlib python-dotenv
~~~

---

## 3  Environment / Config (`.env`)

~~~env
DISCORD_TOKEN=your_bot_token
TMDB_API_KEY=your_tmdb_v3_api_key
DEV_GUILD_ID=123456789012345678   # optional — instant slash-sync
TIMEZONE=America/New_York
MAX_SEATS=3                       # guest seats (host excluded)
~~~

`config.py` auto-loads `.env` via **python-dotenv**.

---

## 4  Quick Start

~~~bash
# clone, create venv, activate …
pip install -r requirements.txt      # or the four libs above
python -m discord_movie_bot.bot
~~~

*In your dev server (`DEV_GUILD_ID`) type `/` and explore commands.*

---

## 5  Slash Command Cheat-Sheet

| Group | Command | Action |
|-------|---------|--------|
| **/catalog** | `add` (admin) | Bulk add titles/IDs |
| | `list` | Paginated browse (filters) |
| | `search` | Free-text search |
| | `info` | Rich embed |
| **/movies** | `nominate` | Nominate any catalog movie |
| | `nominees` | Show top nominations |
| | `open_vote` (admin) | Create ballot (top / manual ≤ 3) |
| | `vote` | Vote on ballot |
| | `request` | Modal to request unavailable movie |
| | `requests` (admin) | List pending |
| | `promote` (admin) | Approve request → catalog |
| **/availability** | `wizard` | No-typing GUI |
| | `preset` | One-click presets |
| | `add` | Power-user text input |
| | `copy_last` | Re-use previous entries |
| | `view` | Chart (all or per-day) |
| **/schedule** | `suggest` | Compute ≤ 3 time slots |
| | `choose` (admin) | Finalize slot, post ISO-8601 |
| **/seats** | `book` / `status` / `release` | 3 seats + wait-list |

---

## 6  Project Layout

```
discord_movie_bot/
    __init__.py            ← marks package
    bot.py                 ← slash-command entrypoint
    config.py              ← dotenv settings
    models.py              ← dataclasses for state
    tmdb_api.py            ← TMDb wrapper (shared session)
    scheduling.py          ← availability logic
    availability_chart.py  ← matplotlib chart
    storage.py             ← atomic JSON save/load
data/
    state.json             ← created at runtime
README.md
requirements.txt
```

---

## 7  Update & Upgrade Guidelines (v⇄v)

* **discord.py** breaking change → update decorators in `bot.py`.
* **TMDb v4** → swap endpoints, bearer token header.
* **Seats > 3** → change `MAX_SEATS` in `.env`; done.
* **Schema bump** → bump `State.version`, migrate in `load_state()`.
* **Python 3.x** → verify `zoneinfo` & discord.py; bump `requirements.txt`.

---

## 8  Troubleshooting

| Symptom | Remedy |
|---------|--------|
| Slash commands missing | Ensure **Developer Mode** → “Copy Server ID” to `DEV_GUILD_ID`. |
| Privileged-intent warning | Harmless — bot never enables `message_content`. |
| TMDb 401 / 429 | Check API key / free-tier rate (40 req per 10 s). |
| Empty availability chart | Members haven’t added availability yet. |

---

## 9  Extending Ideas

* **ICS import** for automatic availability.  
* **Recurring ballots** with cooldown on recent winners.  
* **SQLite** or cloud DB instead of `state.json`.  
* **Webhook** announcements for final schedule.

---

**Enjoy your movie nights!**   Open issues / PRs welcome. 🍿
