"""Slash-command Discord bot for movie night (TMDb + catalog + nominations + requests + scheduling wizard)."""

from __future__ import annotations

import asyncio
import math
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from zoneinfo import ZoneInfo

from .config import DISCORD_TOKEN, DEV_GUILD_ID, MAX_SEATS, TIMEZONE
from .models import Movie, ScheduleSlot, TicketHolder, State, MovieRequest
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

# -------------------- Helpers --------------------

def get_or_create_holder(user: discord.User | discord.Member) -> TicketHolder:
    th = state.ticket_holders.get(user.id)
    if not th:
        th = TicketHolder(user_id=user.id, user_name=user.display_name)
        state.ticket_holders[user.id] = th
    return th

def leading_movie() -> Optional[Movie]:
    if not state.movie_options:
        return None
    tally: Dict[int, int] = {mid: 0 for mid in state.movie_options}
    for th in state.ticket_holders.values():
        if th.movie_vote in tally:
            tally[th.movie_vote] += 1
    best = max(tally, key=tally.get) if tally else None
    return state.movies.get(best) if best is not None else None

def uniq_append_nomination(movie_id: int, user_id: int) -> None:
    arr = state.nominations.setdefault(movie_id, [])
    if user_id not in arr:
        arr.append(user_id)

def nominations_top_n(n: int = 3) -> List[int]:
    # Sort movies by nomination count desc
    pairs = [(mid, len(set(uids))) for mid, uids in state.nominations.items()]
    pairs.sort(key=lambda p: p[1], reverse=True)
    return [mid for mid, _ in pairs[:n]]

def ensure_ballot_size() -> None:
    if len(state.movie_options) > 3:
        state.movie_options = state.movie_options[:3]

def ensure_time_options_size() -> None:
    if len(state.time_options) > 3:
        state.time_options = state.time_options[:3]

# -------------------- Persistent Views for Voting --------------------

class MovieVoteView(discord.ui.View):
    def __init__(self, options: List[Movie]):
        super().__init__(timeout=None)  # persistent view
        select_opts = []
        for m in options:
            label = (m.title[:90] + "…") if len(m.title) > 90 else m.title
            cert = (m.advisory.certification if m.advisory else "NR")
            select_opts.append(discord.SelectOption(label=label, description=f"{m.year} • {cert}", value=str(m.tmdb_id)))
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
        slot = state.time_options[idx]
        await interaction.response.send_message(f"Your vote for **{slot.day} {slot.start_time}-{slot.end_time}** has been recorded ✅", ephemeral=True)

def register_persistent_views():
    movie_opts = [state.movies[mid] for mid in state.movie_options if mid in state.movies]
    if movie_opts:
        bot.add_view(MovieVoteView(movie_opts))
    if state.time_options:
        bot.add_view(TimeVoteView(state.time_options))

# -------------------- Catalog Pagination Views --------------------

class CatalogSession:
    def __init__(self, ids: List[int], page_size: int = 10):
        self.ids = ids
        self.page_size = page_size
        self.page = 0

    def num_pages(self) -> int:
        return max(1, math.ceil(len(self.ids) / self.page_size))

    def page_slice(self) -> List[int]:
        start = self.page * self.page_size
        end = start + self.page_size
        return self.ids[start:end]

class CatalogView(discord.ui.View):
    def __init__(self, session: CatalogSession):
        super().__init__(timeout=120)  # ephemeral session
        self.session = session
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        self.add_item(CatalogPrevButton())
        self.add_item(CatalogNextButton())
        # Info select
        options = []
        for mid in self.session.page_slice():
            m = state.movies.get(mid)
            if not m: continue
            label = (m.title[:90] + "…") if len(m.title) > 90 else m.title
            options.append(discord.SelectOption(label=label, value=str(mid)))
        if options:
            self.add_item(CatalogInfoSelect(options))
            self.add_item(CatalogNominateSelect(options))

class CatalogPrevButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Prev", style=discord.ButtonStyle.secondary)
    async def callback(self, interaction: discord.Interaction):
        view: CatalogView = self.view  # type: ignore
        if view.session.page > 0:
            view.session.page -= 1
        view.update_buttons()
        await interaction.response.edit_message(view=view)

class CatalogNextButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Next", style=discord.ButtonStyle.secondary)
    async def callback(self, interaction: discord.Interaction):
        view: CatalogView = self.view  # type: ignore
        if view.session.page < view.session.num_pages() - 1:
            view.session.page += 1
        view.update_buttons()
        await interaction.response.edit_message(view=view)

class CatalogInfoSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Info: choose a movie", options=options, min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        mid = int(self.values[0])
        m = state.movies.get(mid)
        if not m:
            await interaction.response.send_message("Missing movie in catalog.", ephemeral=True)
            return
        rated = f"{m.advisory.certification} ({m.advisory.region})" if m.advisory else "NR"
        desc = (m.overview[:400] + "…") if len(m.overview) > 400 else m.overview
        embed = discord.Embed(title=f"{m.title} ({m.year})", description=desc)
        embed.add_field(name="Runtime", value=f"{m.runtime} min", inline=True)
        embed.add_field(name="Rated", value=rated, inline=True)
        if m.genres:
            embed.add_field(name="Genres", value=", ".join(m.genres)[:1024], inline=False)
        if m.trailer_url: embed.add_field(name="Trailer", value=m.trailer_url, inline=False)
        if m.poster_url: embed.set_thumbnail(url=m.poster_url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class CatalogNominateSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Nominate: choose a movie", options=options, min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        mid = int(self.values[0])
        uniq_append_nomination(mid, interaction.user.id)
        await save_state(state)
        await interaction.response.send_message(f"✅ Nominated **{state.movies[mid].title}**", ephemeral=True)

# -------------------- Availability Wizard Views --------------------

PRESETS = {
    "Weeknights 18:00-22:00": [("Monday","18:00","22:00"), ("Tuesday","18:00","22:00"),
                               ("Wednesday","18:00","22:00"), ("Thursday","18:00","22:00")],
    "Friday 16:00-23:00": [("Friday","16:00","23:00")],
    "Weekends Afternoon": [("Saturday","12:00","18:00"), ("Sunday","12:00","18:00")],
}

class AvailabilityWizardData:
    def __init__(self):
        self.days: List[str] = []
        self.blocks: List[Tuple[str, str, str]] = []  # (day, start, end)

class DayMultiSelect(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=d, value=d) for d in WEEKDAY_ORDER]
        super().__init__(placeholder="Choose days", min_values=1, max_values=len(opts), options=opts)
    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityWizardView = self.view  # type: ignore
        view.data.days = list(self.values)
        await interaction.response.edit_message(content=f"Selected days: {', '.join(view.data.days)}", view=view)

class PresetSelect(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=name, value=name) for name in PRESETS.keys()]
        opts.append(discord.SelectOption(label="Custom…", value="__custom__"))
        super().__init__(placeholder="Choose a preset or Custom…", min_values=1, max_values=1, options=opts)
    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityWizardView = self.view  # type: ignore
        choice = self.values[0]
        if choice == "__custom__":
            await interaction.response.send_modal(CustomTimeModal())
        else:
            blocks = PRESETS[choice]
            # If user selected specific days, map only those; else apply all preset days
            days = set(view.data.days) if view.data.days else set(d for d,_,_ in blocks)
            for d, s, e in blocks:
                if d in days:
                    view.data.blocks.append((d, s, e))
            await interaction.response.edit_message(content=f"Added preset blocks. Current count: {len(view.data.blocks)}", view=view)

class CustomTimeModal(discord.ui.Modal, title="Custom Time Window"):
    day = discord.ui.TextInput(label="Day (e.g., Monday)", placeholder="Monday")
    start = discord.ui.TextInput(label="Start (HH:MM 24h)", placeholder="18:00")
    end = discord.ui.TextInput(label="End (HH:MM 24h)", placeholder="22:00")
    async def on_submit(self, interaction: discord.Interaction):
        view: AvailabilityWizardView = self.parent  # type: ignore
        d = str(self.day).strip()
        s = str(self.start).strip()
        e = str(self.end).strip()
        view.data.blocks.append((d, s, e))
        await interaction.response.edit_message(content=f"Added custom block {d} {s}-{e}. Current count: {len(view.data.blocks)}", view=view)

class SaveAvailabilityButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save Availability", style=discord.ButtonStyle.success)
    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityWizardView = self.view  # type: ignore
        th = get_or_create_holder(interaction.user)
        # Convert to DayTimeRange; basic validation is in text parser; here keep simple
        from .models import DayTimeRange
        ranges = []
        for d,s,e in view.data.blocks:
            try:
                ranges.append(DayTimeRange(day=d, start_time=s, end_time=e))
            except Exception:
                pass
        th.availability = ranges
        await save_state(state)
        await interaction.response.send_message("✅ Availability saved.", ephemeral=True)

class AvailabilityWizardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.data = AvailabilityWizardData()
        self.add_item(DayMultiSelect())
        self.add_item(PresetSelect())
        self.add_item(SaveAvailabilityButton())

# -------------------- Events --------------------

@bot.event
async def on_ready():
    global state
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    state = await load_state()
    bot.loop.create_task(autosave_loop(state))
    register_persistent_views()
    if DEV_GUILD_ID:
        await bot.tree.sync(guild=discord.Object(id=DEV_GUILD_ID))
        print(f"Slash commands synced to guild {DEV_GUILD_ID}.")
    else:
        await bot.tree.sync()
        print("Slash commands globally synced.")

@bot.event
async def on_close():
    await close_session()

# -------------------- Slash commands --------------------

# Group: Catalog (browse/search/info/add)
catalog_group = app_commands.Group(name="catalog", description="Manage and browse the movie catalog")

@catalog_group.command(name="add", description="Admin: Add titles or TMDb IDs (comma-separated) to the catalog.")
@app_commands.describe(titles_or_ids="E.g. 'The Matrix, 603, Interstellar'")
@app_commands.checks.has_permissions(administrator=True)
async def catalog_add(interaction: discord.Interaction, titles_or_ids: str):
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
    await interaction.followup.send(f"Added {added} movie(s) to the catalog.", ephemeral=True)

def _filter_catalog(genre: Optional[str], year: Optional[int], max_runtime: Optional[int], cert: Optional[str]) -> List[int]:
    ids: List[int] = []
    for mid, m in state.movies.items():
        if genre and genre.lower() not in [g.lower() for g in m.genres]:
            continue
        if year and (not m.year.isdigit() or int(m.year) != year):
            continue
        if max_runtime and m.runtime and m.runtime > max_runtime:
            continue
        if cert and (not m.advisory or m.advisory.certification.lower() != cert.lower()):
            continue
        ids.append(mid)
    # sort by title
    ids.sort(key=lambda mid: state.movies[mid].title.lower())
    return ids

@catalog_group.command(name="list", description="Browse the catalog with optional filters.")
@app_commands.describe(page="Start page (0-based)", genre="Filter by genre", year="Filter by year", max_runtime="Filter by max runtime (min)", cert="Filter by certification")
async def catalog_list(interaction: discord.Interaction, page: Optional[int] = 0, genre: Optional[str] = None, year: Optional[int] = None, max_runtime: Optional[int] = None, cert: Optional[str] = None):
    ids = _filter_catalog(genre, year, max_runtime, cert)
    if not ids:
        await interaction.response.send_message("No catalog entries match your filters.", ephemeral=True)
        return
    session = CatalogSession(ids)
    session.page = max(0, min(page or 0, session.num_pages()-1))
    view = CatalogView(session)
    await interaction.response.send_message(f"Catalog page {session.page+1}/{session.num_pages()}", view=view, ephemeral=True)

@catalog_group.command(name="search", description="Search catalog by title/genre (case-insensitive).")
@app_commands.describe(query="Search text", page="Start page (0-based)")
async def catalog_search(interaction: discord.Interaction, query: str, page: Optional[int] = 0):
    q = query.strip().lower()
    ids = []
    for mid, m in state.movies.items():
        hay = " ".join([m.title] + m.genres).lower()
        if q in hay:
            ids.append(mid)
    if not ids:
        await interaction.response.send_message("No matches found.", ephemeral=True)
        return
    ids.sort(key=lambda mid: state.movies[mid].title.lower())
    session = CatalogSession(ids)
    session.page = max(0, min(page or 0, session.num_pages()-1))
    view = CatalogView(session)
    await interaction.response.send_message(f"Search results page {session.page+1}/{session.num_pages()}", view=view, ephemeral=True)

@catalog_group.command(name="info", description="Get info about a movie by title or TMDb ID.")
@app_commands.describe(query="Title or TMDb ID")
async def catalog_info(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True)
    m = await build_movie_from_title(query)
    if not m:
        await interaction.followup.send("Movie not found.", ephemeral=True)
        return
    rated = f"{m.advisory.certification} ({m.advisory.region})" if m.advisory else "NR"
    desc = (m.overview[:400] + "…") if len(m.overview) > 400 else m.overview
    embed = discord.Embed(title=f"{m.title} ({m.year})", description=desc)
    embed.add_field(name="Runtime", value=f"{m.runtime} min", inline=True)
    embed.add_field(name="Rated", value=rated, inline=True)
    if m.genres: embed.add_field(name="Genres", value=", ".join(m.genres)[:1024], inline=False)
    if m.trailer_url: embed.add_field(name="Trailer", value=m.trailer_url, inline=False)
    if m.poster_url: embed.set_thumbnail(url=m.poster_url)
    await interaction.followup.send(embed=embed, ephemeral=True)

bot.tree.add_command(catalog_group, guild=discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else bot.tree.add_command(catalog_group)

# Group: Movies (nominations, ballot, voting, requests)
movies_group = app_commands.Group(name="movies", description="Nominations, ballot and voting")

@movies_group.command(name="nominate", description="Nominate a movie by TMDb ID (from the catalog).")
@app_commands.describe(id="TMDb ID")
async def movies_nominate(interaction: discord.Interaction, id: int):
    if id not in state.movies:
        await interaction.response.send_message("That TMDb ID is not in the catalog.", ephemeral=True)
        return
    uniq_append_nomination(id, interaction.user.id)
    await save_state(state)
    await interaction.response.send_message(f"✅ Nominated **{state.movies[id].title}**", ephemeral=True)

@movies_group.command(name="nominees", description="Show current top nominees.")
async def movies_nominees(interaction: discord.Interaction):
    if not state.nominations:
        await interaction.response.send_message("No nominations yet.", ephemeral=True)
        return
    pairs = [(mid, len(set(uids))) for mid, uids in state.nominations.items()]
    pairs.sort(key=lambda p: p[1], reverse=True)
    lines = []
    for mid, count in pairs[:20]:
        m = state.movies.get(mid)
        if m: lines.append(f"{m.title} ({m.year}) — {count} nomination(s)")
    await interaction.response.send_message("\n".join(lines) or "No nominations.", ephemeral=True)

@movies_group.command(name="open_vote", description="Admin: create ballot of up to 3 options from nominees or manual IDs.")
@app_commands.describe(strategy="top (from nominees) or manual", id1="TMDb ID 1 (for manual)", id2="TMDb ID 2 (optional)", id3="TMDb ID 3 (optional)")
@app_commands.choices(strategy=[
    app_commands.Choice(name="top", value="top"),
    app_commands.Choice(name="manual", value="manual"),
])
@app_commands.checks.has_permissions(administrator=True)
async def movies_open_vote(interaction: discord.Interaction, strategy: app_commands.Choice[str], id1: Optional[int] = None, id2: Optional[int] = None, id3: Optional[int] = None):
    if strategy.value == "top":
        ids = [mid for mid in nominations_top_n(3) if mid in state.movies]
        if not ids:
            await interaction.response.send_message("No nominees to open a vote with.", ephemeral=True)
            return
    else:
        ids = [x for x in [id1, id2, id3] if x]
        for mid in ids:
            if mid not in state.movies:
                await interaction.response.send_message(f"TMDb ID `{mid}` not found in catalog.", ephemeral=True)
                return
        if len(ids) == 0:
            await interaction.response.send_message("Provide at least one TMDb ID for manual strategy.", ephemeral=True)
            return
        if len(ids) > 3:
            await interaction.response.send_message("You can pick at most 3 options.", ephemeral=True)
            return
    state.movie_options = ids[:3]
    ensure_ballot_size()
    await save_state(state)
    # present voting select
    opts = [state.movies[mid] for mid in state.movie_options if mid in state.movies]
    if not opts:
        await interaction.response.send_message("No valid ballot options.", ephemeral=True)
        return
    view = MovieVoteView(opts)
    await interaction.response.send_message("Vote for a movie:", view=view, ephemeral=False)

@movies_group.command(name="vote", description="Vote among current ballot options.")
async def movies_vote(interaction: discord.Interaction):
    if not state.movie_options:
        await interaction.response.send_message("No active ballot yet. Ask an admin to run `/movies open_vote`.", ephemeral=True)
        return
    opts = [state.movies[mid] for mid in state.movie_options if mid in state.movies]
    if not opts:
        await interaction.response.send_message("No valid ballot options.", ephemeral=True)
        return
    view = MovieVoteView(opts)
    await interaction.response.send_message("Pick your movie:", view=view, ephemeral=True)

# Requests
@movies_group.command(name="request", description="Request a movie not in the catalog.")
async def movies_request(interaction: discord.Interaction):
    class RequestModal(discord.ui.Modal, title="Request a Movie"):
        title = discord.ui.TextInput(label="Title (required)", placeholder="Movie title", max_length=200)
        year = discord.ui.TextInput(label="Year (optional)", required=False, max_length=4)
        note = discord.ui.TextInput(label="Why this movie? (optional)", required=False, style=discord.TextStyle.paragraph, max_length=500)

        async def on_submit(self, i: discord.Interaction):
            q = str(self.title).strip()
            y = str(self.year).strip()
            note = str(self.note).strip() or None
            if y and y.isdigit():
                q = f"{q} {y}"
            req_id = state.next_request_id
            state.next_request_id += 1
            req = MovieRequest(request_id=req_id, user_id=i.user.id, user_name=i.user.display_name, query=q, note=note)
            state.movie_requests[req_id] = req
            await save_state(state)
            await i.response.send_message(f"✅ Request submitted (ID: {req_id}).", ephemeral=True)

    await interaction.response.send_modal(RequestModal())

@movies_group.command(name="requests", description="Admin: review movie requests.")
@app_commands.checks.has_permissions(administrator=True)
async def movies_requests(interaction: discord.Interaction):
    if not state.movie_requests:
        await interaction.response.send_message("No requests.", ephemeral=True)
        return
    lines = []
    for rid, req in sorted(state.movie_requests.items()):
        lines.append(f"#{rid} — {req.user_name}: `{req.query}` [{req.status}]")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

@movies_group.command(name="promote", description="Admin: approve a request and add it to the catalog.")
@app_commands.describe(request_id="Request ID to approve")
@app_commands.checks.has_permissions(administrator=True)
async def movies_promote(interaction: discord.Interaction, request_id: int):
    req = state.movie_requests.get(request_id)
    if not req:
        await interaction.response.send_message("No such request.", ephemeral=True)
        return
    if req.status != "pending":
        await interaction.response.send_message("Request is not pending.", ephemeral=True)
        return
    # resolve via TMDb
    m = await build_movie_from_title(req.query)
    if not m:
        await interaction.response.send_message("Could not resolve that title on TMDb.", ephemeral=True)
        return
    state.movies[m.tmdb_id] = m
    req.resolved_tmdb_id = m.tmdb_id
    req.status = "approved"
    await save_state(state)
    await interaction.response.send_message(f"✅ Approved and added to catalog: **{m.title}** (TMDb {m.tmdb_id})", ephemeral=False)

bot.tree.add_command(movies_group, guild=discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else bot.tree.add_command(movies_group)

# Group: Availability (wizard, preset, copy_last, add/view)
avail_group = app_commands.Group(name="availability", description="Provide and view availability")

@avail_group.command(name="wizard", description="Interactive availability setup (no typing).")
async def availability_wizard(interaction: discord.Interaction):
    await interaction.response.send_message("Availability Wizard: pick days, choose preset or add custom blocks, then Save.", view=AvailabilityWizardView(), ephemeral=True)

@avail_group.command(name="preset", description="Apply a quick preset of availability windows.")
@app_commands.describe(name="Preset name")
@app_commands.choices(name=[app_commands.Choice(name=k, value=k) for k in PRESETS.keys()])
async def availability_preset(interaction: discord.Interaction, name: app_commands.Choice[str]):
    th = get_or_create_holder(interaction.user)
    from .models import DayTimeRange
    blocks = PRESETS[name.value]
    th.availability = [DayTimeRange(day=d, start_time=s, end_time=e) for d,s,e in blocks]
    await save_state(state)
    await interaction.response.send_message(f"✅ Applied preset: {name.value}", ephemeral=True)

@avail_group.command(name="copy_last", description="Copy your last availability (if any).")
async def availability_copy_last(interaction: discord.Interaction):
    th = get_or_create_holder(interaction.user)
    if not th.availability:
        await interaction.response.send_message("No previous availability found.", ephemeral=True)
        return
    # no-op since it's already current; but we could clone; here just confirm
    await interaction.response.send_message("✅ Availability retained from your last setup.", ephemeral=True)

@avail_group.command(name="add", description="(Power users) Set availability via text, e.g., 'Mon 17:00-23:00, Tue-Thu 18:00-22:00'.")
@app_commands.describe(input="Comma-separated blocks")
async def availability_add(interaction: discord.Interaction, input: str):
    try:
        ranges = parse_availability_string(input)
    except ValueError as e:
        await interaction.response.send_message(f"Parse error: {e}", ephemeral=True)
        return
    th = get_or_create_holder(interaction.user)
    th.availability = ranges
    await save_state(state)
    await interaction.response.send_message("✅ Availability updated.", ephemeral=True)

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
    overlaps = compute_common_overlaps(state.ticket_holders)
    options = overlaps[:3]
    if len(options) < 3:
        popular = [s for s in compute_popular_slots(state.ticket_holders) if s not in options]
        for s in popular:
            if len(options) >= 3: break
            options.append(s)
    state.time_options = options
    ensure_time_options_size()
    await save_state(state)
    if not state.time_options:
        await interaction.response.send_message("No suitable time windows yet. Ask participants to add availability.", ephemeral=True)
        return
    view = TimeVoteView(state.time_options)
    await interaction.response.send_message("Vote for a time slot:", view=view, ephemeral=False)

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
        f"Scheduled **{m.title}**\nStart: `{iso_start}`\nEnd: `{iso_end}`\n"
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
    main()
