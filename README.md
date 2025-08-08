# Mattinees — Movie Night Discord Bot

A Discord slash-command bot to run cozy, well-organized movie nights:

- Manage a **catalog** of available movies (with posters, trailers, runtime, genres, certification, and granular content warnings from TMDb).
- Let members **browse/search** the catalog, get info, and **nominate** titles for a vote.
- Keep the ballot to **≤ 3 options** at all times for quick consensus.
- Collect **availability** via a friendly, multi-step **wizard** or text DSL; render a visual **availability chart**.
- Suggest and vote on **time slots** (also limited to ≤ 3).
- Handle **seats** (3 guests + host) with waitlist.
- **Autosave** JSON state, **backups** with rotation, **restore/undo**, and robust error handling.

The bot is intentionally modular, safe, and easy to maintain.

---

## Quick Start

1) **Create a Discord application & bot**
- Visit the Discord Developer Portal and create an application.
- Add a **Bot** to the application.
- Copy the **Bot Token** (you’ll put it in `.env` as `DISCORD_TOKEN`).
- Invite the bot to your server with scopes: `bot` and `applications.commands`. Minimal permissions needed: `Send Messages`, `Embed Links`, `Attach Files`, `Use Slash Commands`.

2) **Get a TMDb API Key (v3)**
- Create an account at The Movie Database (TMDb), request an **API Key (v3)**.
- Use the **API Key** (NOT the “API Read Access Token”) in `.env` as `TMDB_API_KEY`.

3) **Prepare the project**
- Python 3.11–3.13 works (we tested on 3.13).
- Create a virtual environment and install requirements.
- Put a `.env` file in the project root.
- Run the bot module.

Example shell session (Linux/macOS):

    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

4) **Create `.env` in the project root**

    DISCORD_TOKEN=your_bot_token_here
    TMDB_API_KEY=your_tmdb_v3_api_key_here
    TIMEZONE=America/New_York
    DEV_GUILD_ID=123456789012345678
    MAX_SEATS=3

Notes:
- `DEV_GUILD_ID` is your development server’s **Guild (Server) ID**. It speeds up slash-command propagation during development. You can omit it for global sync (takes longer).
- `TIMEZONE` is used to compute ISO-8601 start/end times when scheduling.
- `MAX_SEATS` is the number of guest seats (host is implicit).

5) **Run it**

From the repo root:

    python3 -m discord_movie_bot.bot

You should see logs like:
- “Logged in as …”
- “Slash commands synced to guild …” (if `DEV_GUILD_ID` set), otherwise “globally synced”.

---

## Directory Layout

This repo is a Python package. Run the bot with `-m` from the project root.

    <repo-root>/
      .env
      requirements.txt
      discord_movie_bot/
        __init__.py
        bot.py
        config.py
        models.py
        scheduling.py
        storage.py
        availability_chart.py
        tmdb_api.py
      data/
        state.json             (created on first run)
        backups/               (timestamped JSON backups)

If you see “ImportError: attempted relative import with no known parent package”, make sure you’re running from the repo root:

    python3 -m discord_movie_bot.bot

---

## Configuration

These are read from `.env` in the **repo root**:

- `DISCORD_TOKEN`: Bot token from the Discord Developer Portal.
- `TMDB_API_KEY`: TMDb **API Key (v3)** — we do **not** use the Read Access Token.
- `TIMEZONE`: IANA timezone (e.g., `America/New_York`) for ISO-8601 schedule outputs.
- `DEV_GUILD_ID`: (Optional) Guild ID to sync commands quickly during development.
- `MAX_SEATS`: Number of guest seats (default 3). Host is not counted.

### How to get your Dev Guild ID

1. In Discord: **Settings → Advanced → Developer Mode → On**.  
2. Right-click your server name → **Copy Server ID**.  
3. Put it in `.env` as `DEV_GUILD_ID=...`.

---

## Data Persistence

- The in-memory state is serialized to `data/state.json`.
- There’s a background **autosave** loop (interval is defined in `storage.py`).
- Manual **backups** go into `data/backups/` with timestamped filenames and rotation.
- **Restore/Undo** replaces the in-memory state and **restarts autosave** so it writes the restored object (we fixed this explicitly).

You can also export/import catalog data with commands.

---

## Commands (Slash)

Below is a concise overview. Most commands reply **ephemerally** unless collaboration makes public replies better (like opening a vote).

### Catalog (`/catalog …`)

- `add` — Admin. Add titles or TMDb IDs (comma-separated).
- `remove` — Admin. Remove movies by TMDb IDs (comma-separated).
- `clear` — Admin. Clear entire catalog (confirmation modal).
- `refresh` — Admin. Re-fetch TMDb metadata for given ID(s).
- `export` — Admin. Export catalog as text.
- `import_file` — Admin. Import from an uploaded text file (one title or TMDb ID per line).
- `list` — Browse catalog with optional filters.
- `search` — Search by title/genre fragments.
- `info` — Get info for a title or TMDb ID.

Notes: Admin actions are **guild-only** (cannot be run in DMs).

### Movies (`/movies …`)

- `nominate` — Nominate a TMDb ID that exists in the catalog.
- `retract_nomination` — Remove your nomination.
- `clear_nominations` — Admin. Clear all nominations.
- `nominees` — Show current top nominees.
- `open_vote` — Admin. Open a movie vote (top nominees or manual IDs).
- `clear_ballot` — Admin. Clear the movie ballot.
- `unvote` — Remove your movie vote.
- `clear_votes` — Admin. Clear everyone’s movie votes.
- `request` — Request a movie not in the catalog (modal).
- `cancel_request` — Cancel your pending request.
- `requests` — Admin. List requests.
- `deny_request` — Admin. Deny a request.
- `delete_request` — Admin. Delete a request.
- `promote` — Admin. Approve request and add to catalog.

### Availability (`/availability …`)

- `wizard` — Multi-step wizard: select days → set time → add blocks → review/save.
- `set` — Replace availability using text DSL.
- `add` — Alias for `set` (back-compat).
- `append` — Append using text DSL (deduped/merged).
- `add_block` — Add a single day/time block.
- `add_multi` — Add the same block across multiple days.
- `remove_block` — Remove an exact block.
- `clear` — Clear all availability or all blocks for a given day.
- `list` — List your availability (or another user’s if admin).
- `export` — Export your availability as a compact DSL string.
- `view` — Render a chart of availability; optionally filter by day.

**Wizard UX** (important):
- The wizard is split into **3 steps** to respect Discord’s 5-row UI limit.
- Times are entered via a **modal** (no row used), which keeps the view within limits.
- The “Remove Draft Block” dropdown caps at **25 items** to respect Discord’s select limit.
- Your in-progress wizard “draft” is **deleted** on save/cancel/timeout to avoid memory buildup.

### Schedule (`/schedule …`)

- `suggest` — Compute overlaps; suggest up to 3 time options for voting.
- `status` — Show time options with vote counts.
- `unvote` — Remove your time-slot vote.
- `clear_votes` — Admin. Clear all time-slot votes.
- `clear_options` — Admin. Clear all time options (resets votes).
- `choose` — Admin. Finalize a time option; prints **ISO-8601** start/end based on runtime + timezone.

### Seats (`/seats …`)

- `book` — Reserve a seat (guest). Max is `MAX_SEATS`.
- `unbook` — Alias for `release`.
- `release` — Free your seat; promotes first waitlisted user.
- `status` — Show seat assignments and waitlist.
- `clear` — Admin. Clear all seats and waitlist.
- `swap` — Admin. Swap seats between two users.

### Admin (`/admin …`) — guild-only

- `backup` — Create a backup now.
- `backups` — List available backups.
- `undo` — Restore the most recent backup. (Autosave is restarted so it writes the restored state.)
- `restore` — Restore by backup filename.

---

## Availability Text DSL

You can define your availability quickly without the wizard. Grammar:

- **Days:** full names (`Monday`), or ranges/lists (`Mon-Thu, Sat`). Abbreviations: `Mon, Tue, Wed, Thu, Fri, Sat, Sun`.
- **Times:** 24-hour `HH:MM`. Minutes must be one of `00`, `15`, `30`, `45`.
- **Blocks:** `Day HH:MM-HH:MM`, separated by commas.

Examples:

    Mon 17:00-23:00, Tue-Thu 18:00-22:00, Fri 16:00-23:00
    Sat 12:00-18:00, Sun 18:00-23:00
    Mon-Thu 19:00-22:00, Sat 14:00-18:00

The bot validates times, ensures start < end, merges overlaps, and deduplicates.

---

## Movie Metadata (TMDb)

- We use **TMDb** for movie search/details, **content certification**, and **granular content descriptors** (when available).
- Provide **API Key (v3)** via `TMDB_API_KEY`.
- The bot favors US certification first, then sensible regional fallbacks (implementation details in `tmdb_api.py`).
- Posters, trailers, runtime, genres, and synopsis are included in embeds.

---

## Voting & Consensus Rules

- At any time, **movie** and **time-slot** ballots are limited to **≤ 3 options** to force consensus quickly.
- Casting a vote is done via a dropdown UI. You can unvote with `/movies unvote` or `/schedule unvote`.
- When an admin runs `/schedule choose`, the bot computes ISO-8601 start/end based on the finalized slot + leading movie runtime + timezone.

---

## Backups, Restore, Autosave

- The bot autosaves JSON state on an interval (see `storage.autosave_loop`).
- You can create manual backups with `/admin backup` (rotation keeps the latest N).
- `/admin undo` restores the newest backup; `/admin restore` restores by filename.
- **Important:** After restore/undo, the bot **restarts** the autosave task to bind it to the new state object. This prevents the common “old autosave kept writing the pre-restore object” bug.

---

## Rendering Availability Charts

- `/availability view` generates a “swimlanes”/waterfall chart showing overlaps and gaps across all users.  
- You can pass a `day` filter to focus on a single day (e.g., `Monday`).
- Charts are attached as images (ephemeral).

---

## Permissions & Intents

- This bot uses **default intents**; **no privileged intents** are required.
- If you see “PrivilegedIntentsRequired”, verify you’re not enabling privileged intents in code, or turn them on in the Developer Portal (not necessary for this bot).

---

## Troubleshooting

- **Slash commands not appearing**
  - If `DEV_GUILD_ID` is set, commands register instantly in that guild. If not, global sync can take several minutes. Check logs: you should see “synced”.
  - Make sure you’re running from repo root with `python3 -m discord_movie_bot.bot`.

- **“Attempted relative import with no known parent package”**
  - Run the module from the project root: `python3 -m discord_movie_bot.bot`.

- **Wizard errors / “no open space for item”**
  - Fixed by the multi-step wizard and modals. Each step respects Discord’s 5-row limit.
  - The “Remove Draft Block” select is capped at 25 options (Discord limit).

- **TMDb requests failing**
  - Confirm your `TMDB_API_KEY` is correct (v3 key).
  - Network or quota issues can cause transient failures; try again.

- **Backups/Restore confusion**
  - After an `/admin restore` or `/admin undo`, the bot now restarts autosave automatically and uses the restored state going forward.

- **Seats/waitlist names**
  - If a user is on the waitlist but hasn’t interacted with the bot, their name may show as their current Discord display name if the guild can resolve it; otherwise, the numeric user ID is shown.

---

## Security Posture

- Admin/destructive commands are **guild-only** and permission-gated.
- We never echo secrets. Configuration is via `.env` only (no shell env required).
- JSON reads/writes are confined to the `data/` directory (no path traversal).

---

## Roadmap Ideas

- Cache TMDb responses (LRU) to reduce latency and API usage.
- Track and **edit** existing vote messages instead of posting new ones; or auto-delete stale vote messages.
- Add `/movies status` to summarize current ballot tallies.
- Archive event outcomes (which movie/time ended up chosen).
- Optional web UI for availability entry and catalog browsing.

---

## Development Notes

- Code is structured to be easy to split into cogs later (`catalog`, `movies`, `availability`, `schedule`, `seats`, `admin`).
- Logging: the bot uses Python `logging`. Adjust level/format in `bot.py` as desired.
- Python 3.13 is supported by the tested dependency versions; if future library updates lag behind 3.13, consider using Python 3.11–3.12.

---

## License

This project aims to be friendly for personal movie nights. Check TMDb’s attribution requirements if you publish screenshots or expose metadata publicly.

---

## Acknowledgements

- Movie data provided by **TMDb**. This product uses the TMDb API but is not endorsed or certified by TMDb.
- Discord bot framework: **discord.py**.

---
