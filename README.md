# Mattinees — Movie Night Discord Bot

A Discord slash-command bot to run cozy, well-organized movie nights:

• Manage a **catalog** of available movies (TMDb: posters, trailers, runtime, genres, certification, descriptors).  
• Let members **browse/search** the catalog, get info, and **nominate** titles for a vote.  
• Keep the ballot to **≤ 3 options** at all times for quick consensus.  
• Collect **availability** via a friendly, multi-step **wizard** or text DSL; render a visual **availability chart**.  
• Suggest and vote on **time slots** (also limited to ≤ 3).  
• Handle **seats** (3 guests + host) with waitlist.  
• **Autosave** JSON state, **backups** with rotation, **restore/undo**, and robust error handling.

This project is intentionally modular, safe, and easy to maintain.

----------------------------------------------------------------

## Quick Start

1) Create a Discord application & bot
  - In the Discord Developer Portal, create an application → add a **Bot**.
  - Copy the **Bot Token** (you’ll put it in .env as DISCORD_TOKEN).
  - Invite the bot to your server with scopes: bot and applications.commands.
  - Minimal permissions: Send Messages, Embed Links, Attach Files, Use Slash Commands.

2) Get a TMDb API Key (v3)
  - Create a TMDb account and request an **API Key (v3)**.
  - Use the API Key (v3) in .env as TMDB_API_KEY.
  - Do not use the “API Read Access Token” for this bot.

3) Prepare the project
  - Python 3.11–3.13 supported (this repo tested on 3.13).
  - Create a venv and install requirements.
  - Put a .env file in the project root.

4) Run it from the repository root
  - python3 -m discord_movie_bot.bot

You should see logs like:
  - “Logged in as …”
  - “Slash commands synced to guild …” (instant if DEV_GUILD_ID is set) or “globally synced” (can take a few minutes).

----------------------------------------------------------------

## Configuration (.env in project root)

DISCORD_TOKEN=your_bot_token_here  
TMDB_API_KEY=your_tmdb_v3_api_key_here  
TIMEZONE=America/New_York  
DEV_GUILD_ID=123456789012345678  
MAX_SEATS=3

Notes:
• DEV_GUILD_ID is your development server’s Guild (Server) ID. Enable Discord “Developer Mode”, right-click your server name, Copy Server ID. Guild sync is instant; global sync takes longer.  
• TIMEZONE is used when computing ISO-8601 start/end times (e.g., when finalizing a schedule).  
• MAX_SEATS is the number of guest seats (host is implicit and not counted).

----------------------------------------------------------------

## Directory Layout

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
    state.json            (created on first run)  
    backups/              (timestamped JSON backups)

Run the bot from the repo root using the module form:
  python3 -m discord_movie_bot.bot

If you see “attempted relative import with no known parent package”, you’re not running from the project root.

----------------------------------------------------------------

## Data Persistence & Safety

• All state is in memory and autosaved to data/state.json every 30s.  
• Manual backups go to data/backups/ with rotation (latest N retained).  
• Undo/Restore replaces in-memory state and **restarts autosave** so it writes the restored object (fixes the classic “autosave kept writing the old object” bug).  
• JSON read/writes are confined to the data/ directory.

Backward compatibility:
• Catalog is safe: we do not re-fetch or mutate movies on load. Unknown/legacy fields are ignored.  
• Time-slot votes migrated from fragile indices to stable keys; if an older state used indexes, user time votes are cleared once on load (movie votes are unaffected).  
• Existing time options get a stable key automatically.  
• Active ballot message references are optional and default to None (populated the next time you open a ballot).

----------------------------------------------------------------

## Commands (Slash)

Most personal flows reply ephemerally; collaborative posts (like ballots) are public.

Catalog (/catalog …)
• list — Browse the catalog with optional filters (genre, year, max runtime, certification). Opens an ephemeral “Catalog Browser” with paging and a quick info panel.  
• search — Search by title/genre text; opens the Catalog Browser with results.  
• add — Admin. Add titles or TMDb IDs (comma-separated).  
• remove — Admin. Remove movies by TMDb IDs (comma-separated).  
• clear — Admin. Clear all catalog entries (confirmation modal).  
• info — Fetch info for a title or TMDb ID (runtime, synopsis, genres, certification, trailer, poster).

Movies (/movies …)
• my_nominations — Ephemeral panel showing your nominations with a retract button.  
• nominees_panel — Admin. Public summary with a “Nominate from Catalog” button and “View My Nominations.”  
• nominate — Power users. Nominate by TMDb ID (the browser is preferred for most users).  
• retract_nomination — Power users. Retract by TMDb ID.  
• open_vote — Admin. Open a ballot using top nominees or manual IDs (≤ 3).  
• close_vote — Admin. Close and remove the active ballot (clears options and movie votes).  
• vote — Ephemeral: opens the current movie ballot (if one exists).  
• unvote — Clear your movie vote.  
• clear_votes — Admin. Clear everyone’s movie votes.

Availability (/availability …)
• wizard — Three steps: Confirm Days → Set Time (modal) → Add Blocks or Presets → Review & Save (Replace or Append).  
• list — Show your availability (or another user’s if admin).  
• export — Export your availability as a compact, readable string.  
• set — Replace availability via DSL string.  
• append — Append via DSL; overlaps merged, duplicates removed.  
• add_block — Add one day/time block (HH:MM minute = 00/15/30/45).  
• add_multi — Add one block across multiple days (e.g., Mon-Thu, Sat).  
• remove_block — Remove an exact (day, start, end) block.  
• clear — Clear all availability or just one day.  
• view — Render a “swimlanes” availability chart (optionally for a single day).

Schedule (/schedule …)
• suggest — Compute overlaps, propose up to 3 time options for voting.  
• vote — Ephemeral: opens the current time-slot ballot (if one exists).  
• status — See the current time options with vote counts.  
• unvote — Clear your time-slot vote.  
• clear_votes — Admin. Clear everyone’s time votes.  
• close_vote — Admin. Close and remove the active time-slot vote.  
• choose — Admin. Finalize a time slot; outputs ISO-8601 start/end times using the leading movie’s runtime and TIMEZONE.

Seats (/seats …)
• book — Reserve a guest seat (host not counted).  
• unbook — Alias for release.  
• release — Free your seat; first waitlisted user is promoted.  
• status — See seat assignments and waitlist.  
• clear — Admin. Clear all seats and waitlist.  
• swap — Admin. Swap seats between two users.

Admin (/admin …)
• backup — Create a timestamped backup now.  
• backups — List available backups (newest first).  
• undo — Restore the most recent backup (autosave restarted).  
• restore — Restore from a specific backup file name.

----------------------------------------------------------------

## Catalog Browser v2 (Ephemeral)

• Opens via /catalog list or /catalog search.  
• Clear page label and total count: “Catalog • Page N/M • T total.”  
• Row 1: Select a movie from the current page slice (up to 25).  
• Row 2: Actions — “Info” and “Nominate / Retract”.  
• Row 3: Paging — “Prev”, “Next”, “Close” (Prev/Next disabled at bounds).  
• The info panel shows poster, runtime, rating, genres, synopsis, and a trailer link.  
• All edits update both the view and the content, so paging visibly advances.

----------------------------------------------------------------

## Availability Text DSL

Define availability succinctly with a compact grammar:

Days
• Mon, Tue, Wed, Thu, Fri, Sat, Sun  
• Full names also work.  
• Ranges and lists are allowed: “Mon-Thu, Sat”.

Times
• 24-hour HH:MM.  
• Minutes must be 00, 15, 30, or 45.

Blocks
• Day HH:MM-HH:MM  
• Separate blocks with commas.

Examples
• Mon 17:00-23:00, Tue-Thu 18:00-22:00, Fri 16:00-23:00  
• Sat 12:00-18:00, Sun 18:00-23:00  
• Mon-Thu 19:00-22:00, Sat 14:00-18:00

Validation ensures start < end, quarter-minute alignment, merging overlaps, deduping.

----------------------------------------------------------------

## Voting & Consensus Rules

• Movie and time-slot ballots are limited to ≤ 3 options to speed consensus.  
• You can always bring up the current ballots ephemerally with /movies vote and /schedule vote.  
• Admin actions (open_vote, suggest) manage a **single active ballot message** per channel; new openings edit/replace the existing message to avoid stale UIs.  
• Time-slot select values use stable keys, e.g., slot:Monday|18:00|22:00, so votes remain valid across restarts if the same options persist.

----------------------------------------------------------------

## Rendering Availability Charts

• /availability view generates a schematic “swimlanes” chart showing overlaps across users.  
• Pass a day to focus the chart (e.g., “Monday”); otherwise all days are included.  
• Charts are attached as images (ephemeral).

----------------------------------------------------------------

## TMDb Integration

• All metadata comes from TMDb: title, year, runtime, genres, synopsis, poster, trailer (if available), certification and descriptors.  
• Provide your TMDb **API Key (v3)** via TMDB_API_KEY.  
• We favor US certifications first, with sensible regional fallbacks.  
• This product uses the TMDb API but is not endorsed or certified by TMDb.

----------------------------------------------------------------

## Permissions & Intents

• Uses default intents; no privileged intents required.  
• If you see “PrivilegedIntentsRequired”, verify you didn’t enable privileged intents in code, or toggle them on in the Developer Portal (not necessary for this bot).

----------------------------------------------------------------

## Troubleshooting

Slash commands not appearing
• If DEV_GUILD_ID is set, commands register instantly in that guild. Without it, global sync can take minutes. Check logs for “synced”.  
• Ensure you run from the repo root: python3 -m discord_movie_bot.bot

“Attempted relative import with no known parent package”
• Run from the repo root (module form). Do not execute bot.py directly.

Wizard issues, component timeouts
• The wizard is split into 3 steps to respect Discord’s 5-row limit, uses a time modal, and caps removal selects at 25 options.  
• If a view expires, re-run /availability wizard.

TMDb request failures
• Double-check TMDB_API_KEY is the v3 key and not the Read Access Token.  
• Network or quota hiccups can cause transient errors; try again.

Backups/Restore
• After /admin restore or /admin undo, autosave is restarted automatically and bound to the restored state.

----------------------------------------------------------------

## Security Posture

• Admin/destructive commands are guild-only and permission-gated.  
• We never echo secrets; config comes from .env only.  
• JSON reads/writes are confined to the data/ directory.

----------------------------------------------------------------

## Roadmap Ideas

• Cache TMDb responses (LRU) to lower latency.  
• Edit or auto-cleanup obsolete vote messages.  
• /movies status to summarize ballot tallies.  
• Archive outcomes (which movie/time was chosen).  
• Optional web UI for availability entry and catalog browsing.

----------------------------------------------------------------

## License

This project is intended for personal movie nights. Review TMDb attribution requirements if you publish screenshots or expose metadata publicly.

Acknowledgements
• Movie data by TMDb. This product uses the TMDb API but is not endorsed or certified by TMDb.  
• Built with discord.py.

----------------------------------------------------------------
