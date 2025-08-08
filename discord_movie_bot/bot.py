"""Slash-command Discord bot for movie night (TMDb + scheduling + voting)."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .config import DISCORD_TOKEN, DEV_GUILD_ID, MAX_SEATS, TIMEZONE
from .models import Movie, ScheduleSlot, TicketHolder, State
from .storage import load_state, save_state, autosave_loop
from .scheduling import (
    parse_availability_string,
    compute_common_overlaps,
    compute_popular_slots,
    slot_to_iso_start_end,
    WEEKDAY_ORDER,
)
from .availability_chart import create_availability_chart
from .tmdb_api import build_movie_from_title, build_movie_from_tmdb_id, close_session

# -------------------- Bot setup --------------------

intents = discord.Intents.default()  # no message content needed
bot = commands.Bot(command_prefix="!", intents=intents)  # prefix unused; using slash

state: State

# persistent views registry (re-added on startup)
PERSISTENT_VIEWS: List[discord.ui.View] = []

# -------------------- Helpers --------------------

def get_or_create_holder(user: discord.User | discord.Member) -> TicketHolder:
    th = state.ticket_holders.get(user.id)
    if not th:
        th = TicketHolder(user_id=user.id, user_name=user.display_name)
        state.ticket_holders[user.id] = th
    return th

def enforce_movie_options_limit() -> Optional[str]:
    if len(state.movie_options) > 3:
        return "There can be at most 3 active movie options. Remove one before adding another."
    return None

def enforce_time_options_limit() -> Optional[str]:
    if len(state.time_options) > 3:
        return "There can be at most 3 active time options. Remove one before adding another."
    return None

def leading_movie() -> Optional[Movie]:
    if not state.movie_options:
        return None
    tally: Dict[int, int] = {mid: 0 for mid in state.movie_options}
    for th in state.ticket_holders.values():
        if th.movie_vote in tally:
            tally[th.movie_vote] += 1
    # pick max
    best = max(tally, key=tally.get) if tally else None
    return state.movies.get(best) if best is not None else None

# -------------------- Persistent Views --------------------

class MovieVoteView(discord.ui.View):
    def __init__(self, options: List[Movie]):
        super().__init__(timeout=None)  # persistent view
        # Build select options
        select_opts = []
        for m in options:
            label = (m.title[:90] + "…") if len(m.title) > 90 else m.title
            select_opts.append(discord.SelectOption(label=label, description=f"{m.year} • {m.advisory.certification if m.advisory else 'NR'}", value=str(m.tmdb_id)))
        self.add_item(MovieSelect(options=select_opts))

class MovieSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(custom_id="movie_vote_select_v1", placeholder="Choose your movie", options=options, min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        mid = int(self.values[0])
        th = get_or_create_holder(interaction.user)
        th.movie_vote = mid
        await save_state(state)
        await interaction.response.send_message(f"Your vote for **{state.movies[mid].title}** has been recorded ✅", ephemeral=True)

class TimeVoteView(discord.ui.View):
    def __init__(self, options: List[ScheduleSlot]):
        super().__init__(timeout=None)  # persistent
        opts = []
        for idx, slot in enumerate(options):
            label = f"{slot.day} {slot.start_time}-{slot.end_time}"
            opts.append(discord.SelectOption(label=label, description=f"{len(slot.participants)} available", value=str(idx)))
        self.add_item(TimeSelect(options=opts))

class TimeSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(custom_id="time_vote_select_v1", placeholder="Choose a time slot", options=options, min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        if idx < 0 or idx >= len(state.time_options):
            await interaction.response.send_message("That time option is no longer available.", ephemeral=True)
            return
        th = get_or_create_holder(interaction.user)
        th.time_vote = idx
        await save_state(state)
        await interaction.response.send_message(f"Your vote for **{state.time_options[idx].day} {state.time_options[idx].start_time}-{state.time_options[idx].end_time}** has been recorded ✅", ephemeral=True)

def register_persistent_views():
    # Build and register views from current options
    movie_opts = [state.movies[mid] for mid in state.movie_options if mid in state.movies]
    if movie_opts:
        mv = MovieVoteView(movie_opts)
        bot.add_view(mv)
        PERSISTENT_VIEWS.append(mv)
    if state.time_options:
        tv = TimeVoteView(state.time_options)
        bot.add_view(tv)
        PERSISTENT_VIEWS.append(tv)

# -------------------- Events --------------------

@bot.event
async def on_ready():
    global state
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Load or init state
    state = await load_state()
    # Start autosave
    bot.loop.create_task(autosave_loop(state))
    # Re-register persistent views (required to keep components alive after restarts)
    register_persistent_views()
    # Sync slash commands (guild-specific during development)
    if DEV_GUILD_ID:
        await bot.tree.sync(guild=discord.Object(id=DEV_GUILD_ID))
        print(f"Slash commands synced to guild {DEV_GUILD_ID}.")
    else:
        await bot.tree.sync()
        print("Slash commands globally synced.")

@bot.event
async def on_close():
    # ensure HTTP session is closed cleanly
    await close_session()

# -------------------- Slash commands --------------------

# Group: Movies
movies_group = app_commands.Group(name="movies", description="Manage movies")

@movies_group.command(name="setup", description="Admin: Add titles or TMDb IDs (comma-separated) to the catalog.")
@app_commands.describe(titles_or_ids="E.g. 'The Matrix, 603, Interstellar'")
@app_commands.checks.has_permissions(administrator=True)
async def movies_setup(interaction: discord.Interaction, titles_or_ids: str):
    await interaction.response.defer(ephemeral=True)
    added = 0
    for part in [p.strip() for p in titles_or_ids.split(",") if p.strip()]:
        try:
            movie = await build_movie_from_title(part)
            if not movie:
                await interaction.followup.send(f"Not found: `{part}`", ephemeral=True)
                continue
            state.movies[movie.tmdb_id] = movie
            added += 1
        except Exception as e:
            await interaction.followup.send(f"Error for `{part}`: {e}", ephemeral=True)
    await save_state(state)
    await interaction.followup.send(f"Added {added} movie(s) to the catalog. Use `/movies set_options` to choose up to 3 options.", ephemeral=True)

@movies_group.command(name="set_options", description="Admin: choose up to 3 active movies by TMDb ID.")
@app_commands.describe(id1="First TMDb ID", id2="Second TMDb ID (optional)", id3="Third TMDb ID (optional)")
@app_commands.checks.has_permissions(administrator=True)
async def movies_set_options(interaction: discord.Interaction, id1: int, id2: Optional[int] = None, id3: Optional[int] = None):
    ids = [id1] + ([id2] if id2 else []) + ([id3] if id3 else [])
    # Validate they exist
    for mid in ids:
        if mid not in state.movies:
            await interaction.response.send_message(f"TMDb ID `{mid}` is not in the catalog. Add with `/movies setup` first.", ephemeral=True)
            return
    if len(ids) > 3:
        await interaction.response.send_message("You can set at most 3 options.", ephemeral=True)
        return
    state.movie_options = ids
    await save_state(state)
    await interaction.response.send_message("Active movie options updated.", ephemeral=True)

@movies_group.command(name="list", description="Show active movie options.")
async def movies_list(interaction: discord.Interaction):
    if not state.movie_options:
        await interaction.response.send_message("No active movie options. Admins can run `/movies set_options`.", ephemeral=True)
        return
    embed = discord.Embed(title="Active Movies")
    for mid in state.movie_options:
        m = state.movies.get(mid)
        if not m: continue
        rated = f"{m.advisory.certification} ({m.advisory.region})" if m.advisory else "NR"
        embed.add_field(
            name=f"{m.title} ({m.year})",
            value=f"TMDb ID: {m.tmdb_id}\nRuntime: {m.runtime} min\nRated: {rated}",
            inline=False
        )
        if m.poster_url:
            embed.set_thumbnail(url=m.poster_url)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@movies_group.command(name="vote", description="Vote for one of the active movies.")
async def movies_vote(interaction: discord.Interaction):
    opts = [state.movies[mid] for mid in state.movie_options if mid in state.movies]
    if not opts:
        await interaction.response.send_message("No active movie options to vote on.", ephemeral=True)
        return
    view = MovieVoteView(opts)
    await interaction.response.send_message("Pick your movie:", view=view, ephemeral=True)

@movies_group.command(name="info", description="Get info about a movie by title or TMDb ID.")
@app_commands.describe(query="Title or TMDb ID")
async def movies_info(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True)
    try:
        m = await build_movie_from_title(query)
        if not m:
            await interaction.followup.send("Movie not found.", ephemeral=True)
            return
        rated = f"{m.advisory.certification} ({m.advisory.region})" if m.advisory else "NR"
        desc = (m.overview[:400] + "…") if len(m.overview) > 400 else m.overview
        embed = discord.Embed(title=f"{m.title} ({m.year})", description=desc)
        embed.add_field(name="Runtime", value=f"{m.runtime} min", inline=True)
        embed.add_field(name="Rated", value=rated, inline=True)
        if m.trailer_url: embed.add_field(name="Trailer", value=m.trailer_url, inline=False)
        if m.poster_url: embed.set_thumbnail(url=m.poster_url)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)

bot.tree.add_command(movies_group, guild=discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else bot.tree.add_command(movies_group)

# Group: Availability
avail_group = app_commands.Group(name="availability", description="Provide and view availability")

@avail_group.command(name="add", description="Add or update your weekly availability (e.g., 'Mon 17:00-23:00, Tue-Thu 18:00-22:00').")
@app_commands.describe(input="Comma-separated blocks like 'Mon 17:00-23:00, Tue-Thu 18:00-22:00'")
async def availability_add(interaction: discord.Interaction, input: str):
    try:
        ranges = parse_availability_string(input)
    except ValueError as e:
        await interaction.response.send_message(f"Parse error: {e}", ephemeral=True)
        return
    th = get_or_create_holder(interaction.user)
    th.availability = ranges  # overwrite
    await save_state(state)
    await interaction.response.send_message("Availability updated.", ephemeral=True)

@avail_group.command(name="view", description="Render the availability chart (optionally for a specific day).")
@app_commands.describe(day="Optional day filter, e.g., Monday")
async def availability_view(interaction: discord.Interaction, day: Optional[str] = None):
    if day and day not in WEEKDAY_ORDER:
        await interaction.response.send_message("Invalid day. Use full weekday name.", ephemeral=True)
        return
    path = create_availability_chart(state.ticket_holders, day=day)
    await interaction.response.send_message(file=discord.File(path), ephemeral=True)

bot.tree.add_command(avail_group, guild=discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else bot.tree.add_command(avail_group)

# Group: Schedule (time options)
schedule_group = app_commands.Group(name="schedule", description="Suggest and choose time slots")

@schedule_group.command(name="suggest", description="Compute overlaps and suggest up to 3 time slots for voting.")
async def schedule_suggest(interaction: discord.Interaction):
    # full overlap first
    overlaps = compute_common_overlaps(state.ticket_holders)
    options = overlaps[:3]
    if len(options) < 3:
        popular = [s for s in compute_popular_slots(state.ticket_holders) if s not in options]
        # fill up to 3
        for s in popular:
            if len(options) >= 3: break
            options.append(s)
    state.time_options = options
    err = enforce_time_options_limit()
    if err:
        state.time_options = options[:3]
    await save_state(state)
    if not state.time_options:
        await interaction.response.send_message("No suitable time windows found yet. Ask participants to add availability.", ephemeral=True)
        return
    # post voting view
    view = TimeVoteView(state.time_options)
    await interaction.response.send_message("Vote for a time slot:", view=view, ephemeral=True)

@schedule_group.command(name="choose", description="Finalize the most-voted time slot and show ISO-8601 start/end.")
@app_commands.describe(index="Index of the chosen time option (0, 1, or 2)")
@app_commands.checks.has_permissions(administrator=True)
async def schedule_choose(interaction: discord.Interaction, index: int):
    if not state.time_options:
        await interaction.response.send_message("No active time options; run `/schedule suggest` first.", ephemeral=True)
        return
    if index < 0 or index >= len(state.time_options):
        await interaction.response.send_message(f"Index must be within 0..{len(state.time_options)-1}.", ephemeral=True)
        return
    m = leading_movie()
    if not m:
        await interaction.response.send_message("No leading movie yet. Ask users to `/movies vote`.", ephemeral=True)
        return
    tz = ZoneInfo(TIMEZONE)
    slot = state.time_options[index]
    iso_start, iso_end = slot_to_iso_start_end(tz, slot, m.runtime)
    await interaction.response.send_message(
        f"Scheduled **{m.title}**\n"
        f"Start: `{iso_start}`\nEnd: `{iso_end}`\n"
        f"Rating: `{m.advisory.certification if m.advisory else 'NR'}` "
        f"{'• ' + ', '.join(m.advisory.descriptors) if (m.advisory and m.advisory.descriptors) else ''}",
        ephemeral=False
    )

bot.tree.add_command(schedule_group, guild=discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else bot.tree.add_command(schedule_group)

# Group: Seats
seats_group = app_commands.Group(name="seats", description="Manage seats")

@seats_group.command(name="book", description="Book a guest seat (host excluded).")
async def seats_book(interaction: discord.Interaction):
    th = get_or_create_holder(interaction.user)
    if th.seat is not None:
        await interaction.response.send_message(f"You already have seat #{th.seat}.", ephemeral=True)
        return
    assigned = {h.seat for h in state.ticket_holders.values() if h.seat is not None}
    for s in range(1, MAX_SEATS + 1):
        if s not in assigned:
            th.seat = s
            await save_state(state)
            await interaction.response.send_message(f"Seat #{s} reserved ✅", ephemeral=True)
            return
    # no seats, join waitlist
    if th.user_id not in state.waitlist:
        state.waitlist.append(th.user_id)
        await save_state(state)
    await interaction.response.send_message("All seats are taken. You have been added to the waitlist.", ephemeral=True)

@seats_group.command(name="status", description="Show seat assignments and waitlist.")
async def seats_status(interaction: discord.Interaction):
    assigned = [h for h in state.ticket_holders.values() if h.seat is not None]
    assigned.sort(key=lambda x: x.seat or 0)
    lines = [f"Seat #{h.seat}: {h.user_name}" for h in assigned] or ["No seats assigned."]
    if state.waitlist:
        names = [state.ticket_holders.get(uid).user_name if uid in state.ticket_holders else str(uid) for uid in state.waitlist]
        lines.append("Waitlist: " + ", ".join(names))
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@seats_group.command(name="release", description="Release your seat (promotes next in waitlist).")
async def seats_release(interaction: discord.Interaction):
    th = get_or_create_holder(interaction.user)
    if th.seat is None:
        await interaction.response.send_message("You don't have a seat reserved.", ephemeral=True)
        return
    freed = th.seat
    th.seat = None
    # promote first in waitlist
    promoted = None
    if state.waitlist:
        uid = state.waitlist.pop(0)
        cand = state.ticket_holders.get(uid)
        if cand and cand.seat is None:
            cand.seat = freed
            promoted = cand.user_name
    await save_state(state)
    if promoted:
        await interaction.response.send_message(f"Seat #{freed} released. Promoted **{promoted}** from waitlist.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Seat #{freed} released.", ephemeral=True)

bot.tree.add_command(seats_group, guild=discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else bot.tree.add_command(seats_group)

# -------------------- Entrypoint --------------------

def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    from zoneinfo import ZoneInfo  # used in schedule_choose
    main()
