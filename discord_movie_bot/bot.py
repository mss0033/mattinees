"""Discord Movie Night Bot — Slash-commands with:
- Catalog Browser v2 (paging + info panel + nominate/retract)
- My Nominations & Nominees Panel
- Single active ballot messages (edit/replace) for movies/time
- Stable time-slot keys (no index fragility)
- Availability CRUD + multi-step Wizard (fixed rows, modal time entry)
- Seats management (3 guest seats + waitlist)
- Error handling & guardrails
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from zoneinfo import ZoneInfo

from .config import DISCORD_TOKEN, DEV_GUILD_ID, MAX_SEATS, TIMEZONE
from .models import (
    MessageRef, Movie, ScheduleSlot, TicketHolder, State, MovieRequest, DayTimeRange
)
from .storage import (
    autosave_loop, backup_state, list_backups, load_state, restore_state_from_backup,
    save_state,
)
from .scheduling import (
    WEEKDAY_ORDER, DAY_INDEX, blocks_to_dsl, canonical_day, compute_common_overlaps,
    compute_popular_slots, day_expr_to_list, dedupe_blocks, is_quarter_minute,
    is_valid_hhmm, merge_blocks, parse_availability_string, remove_block as remove_block_from_list,
    remove_day as remove_day_from_list, slot_to_iso_start_end,
)
from .availability_chart import create_availability_chart
from .tmdb_api import build_movie_from_title, build_movie_from_tmdb_id, close_session

# -------------------- Logging --------------------

log = logging.getLogger("discord_movie_bot")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s")

# -------------------- Bot setup --------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
state: State
autosave_task: Optional[asyncio.Task] = None

# -------------------- Global error handler --------------------

from discord.app_commands import AppCommandError, CheckFailure  # noqa: E402

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: AppCommandError):
    try:
        if isinstance(error, CheckFailure):
            msg = "You don’t have permission to use that command here."
        else:
            original = getattr(error, "original", None)
            msg = str(original) if original else str(error)
        if interaction.response.is_done():
            await interaction.followup.send(f"Error: {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"Error: {msg}", ephemeral=True)
    except Exception:
        log.exception("Error while reporting command failure")

# -------------------- Helpers --------------------

def get_or_create_holder(user: discord.User | discord.Member) -> TicketHolder:
    th = state.ticket_holders.get(user.id)
    if not th:
        th = TicketHolder(user_id=user.id, user_name=getattr(user, "display_name", str(user.id)))
        state.ticket_holders[user.id] = th
    else:
        th.user_name = getattr(user, "display_name", th.user_name) or th.user_name
    return th

def ensure_ballot_size():
    if len(state.movie_options) > 3:
        state.movie_options = state.movie_options[:3]

def ensure_time_options_size():
    if len(state.time_options) > 3:
        state.time_options = state.time_options[:3]

def leading_movie() -> Optional[Movie]:
    if not state.movie_options:
        return None
    tally: Dict[int, int] = {mid: 0 for mid in state.movie_options}
    for th in state.ticket_holders.values():
        if th.movie_vote in tally:
            tally[th.movie_vote] += 1
    best = max(tally, key=tally.get) if tally else None
    return state.movies.get(best) if best is not None else None

async def _restart_autosave(new_state: State):
    global autosave_task, state
    state = new_state
    if autosave_task and not autosave_task.done():
        autosave_task.cancel()
        try:
            await autosave_task
        except Exception:
            pass
    autosave_task = bot.loop.create_task(autosave_loop(state))

async def _fetch_message(ref: Optional[MessageRef]) -> Optional[discord.Message]:
    if not ref:
        return None
    ch = bot.get_channel(ref.channel_id)
    if not isinstance(ch, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
        try:
            ch = await bot.fetch_channel(ref.channel_id)
        except Exception:
            return None
    try:
        msg = await ch.fetch_message(ref.message_id)
        return msg
    except Exception:
        return None
    
def _cleanup_old_charts(keep_latest: int = 10, max_age_hours: int = 24):
    """
    Keep only the most recent `keep_latest` availability_*.png files in ./data,
    and remove any older than `max_age_hours`.
    """
    try:
        import glob, time
        data_dir = os.path.join(os.getcwd(), "data")
        files = sorted(
            glob.glob(os.path.join(data_dir, "availability_*.png")),
            key=lambda p: os.path.getmtime(p),
            reverse=True
        )
        now = time.time()
        # Remove beyond keep_latest
        for p in files[keep_latest:]:
            # Also remove if too old
            age_hours = (now - os.path.getmtime(p)) / 3600.0
            if age_hours > max_age_hours or True:
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass

# -------------------- Persistent Voting Views --------------------

class MovieVoteView(discord.ui.View):
    def __init__(self, options: List[Movie]):
        super().__init__(timeout=None)
        select_opts = []
        for m in options:
            label = (m.title[:90] + "…") if len(m.title) > 90 else m.title
            cert = (m.advisory.certification if m.advisory else "NR")
            select_opts.append(discord.SelectOption(label=label, description=f"{m.year} • {cert}", value=f"tmdb:{m.tmdb_id}"))
        self.add_item(MovieSelect(select_opts))

class MovieSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(custom_id="movie_vote_select_v1", placeholder="Vote: choose your movie", options=options, min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if not val.startswith("tmdb:"):
            await interaction.response.send_message("Unknown option.", ephemeral=True)
            return
        mid = int(val.split(":", 1)[1])
        if mid not in state.movies or mid not in state.movie_options:
            await interaction.response.send_message("That title is no longer on the ballot.", ephemeral=True)
            return
        th = get_or_create_holder(interaction.user)
        th.movie_vote = mid
        await save_state(state)
        await interaction.response.send_message(f"Your vote for **{state.movies[mid].title}** has been recorded ✅", ephemeral=True)

class TimeVoteView(discord.ui.View):
    def __init__(self, options: List[ScheduleSlot]):
        super().__init__(timeout=None)
        opts = []
        for slot in options:
            label = f"{slot.day} {slot.start_time}-{slot.end_time}"
            opts.append(discord.SelectOption(label=label, description="Choose to vote", value=f"{slot.key}"))
        self.add_item(TimeSelect(opts))

class TimeSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(custom_id="time_vote_select_v2", placeholder="Vote: choose a time slot", options=options, min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        match = next((s for s in state.time_options if s.key == key), None)
        if not match:
            await interaction.response.send_message("That time option is no longer available.", ephemeral=True)
            return
        th = get_or_create_holder(interaction.user)
        th.time_vote = key
        await save_state(state)
        await interaction.response.send_message(f"Your vote for **{match.day} {match.start_time}-{match.end_time}** has been recorded ✅", ephemeral=True)

def register_persistent_views():
    movie_opts = [state.movies[mid] for mid in state.movie_options if mid in state.movies]
    if movie_opts:
        bot.add_view(MovieVoteView(movie_opts))
    if state.time_options:
        bot.add_view(TimeVoteView(state.time_options))

# -------------------- Catalog Browser v2 --------------------

def _filter_catalog_ids(genre: Optional[str], year: Optional[int], max_runtime: Optional[int], cert: Optional[str]) -> List[int]:
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
    ids.sort(key=lambda mid: state.movies[mid].title.lower())
    return ids

def _catalog_embed(page: int, pages: int, total: int, selected: Optional[Movie], filters_label: str) -> discord.Embed:
    title = f"🎬 Catalog • Page {page+1}/{pages} • {total} total"
    description = filters_label or "Browse available movies."
    embed = discord.Embed(title=title, description=description)
    if selected:
        rated = f"{selected.advisory.certification} ({selected.advisory.region})" if selected.advisory else "NR"
        desc = (selected.overview[:400] + "…") if len(selected.overview) > 400 else selected.overview
        embed.add_field(name=f"{selected.title} ({selected.year})", value=desc or "(no synopsis)", inline=False)
        embed.add_field(name="Runtime", value=f"{selected.runtime} min", inline=True)
        embed.add_field(name="Rated", value=rated, inline=True)
        if selected.genres:
            embed.add_field(name="Genres", value=", ".join(selected.genres)[:1024], inline=False)
        if selected.trailer_url:
            embed.add_field(name="Trailer", value=selected.trailer_url, inline=False)
        if selected.poster_url:
            embed.set_thumbnail(url=selected.poster_url)
    return embed

class CatalogSession:
    def __init__(self, owner_id: int, ids: List[int], page_size: int = 10, label: str = ""):
        self.owner_id = owner_id
        self.ids = ids
        self.page_size = max(1, min(page_size, 25))
        self.page = 0
        self.selected_mid: Optional[int] = None
        self.label = label

    def num_pages(self) -> int:
        return max(1, (len(self.ids) + self.page_size - 1) // self.page_size)

    def slice_ids(self) -> List[int]:
        a = self.page * self.page_size
        b = a + self.page_size
        return self.ids[a:b]

    def selected_movie(self) -> Optional[Movie]:
        if self.selected_mid is None:
            return None
        return state.movies.get(self.selected_mid)

class CatalogView(discord.ui.View):
    def __init__(self, session: CatalogSession):
        super().__init__(timeout=120)
        self.session = session
        self._rebuild_components()

    def _rebuild_components(self):
        """Rebuild all UI components so the Select reflects the *current page*."""
        self.clear_items()

        # Clamp page bounds in case the catalog changed
        pages = self.session.num_pages()
        if pages <= 0:
            self.session.page = 0
        elif self.session.page >= pages:
            self.session.page = pages - 1
        elif self.session.page < 0:
            self.session.page = 0

        current_slice = self.session.slice_ids()

        # Row 0 — page-specific Select
        options: List[discord.SelectOption] = []
        for mid in current_slice:
            m = state.movies.get(mid)
            if not m:
                continue
            label = (m.title[:90] + "…") if len(m.title) > 90 else m.title
            options.append(discord.SelectOption(label=label, value=f"tmdb:{mid}"))

        if options:
            self.add_item(CatalogSelect(options))
        else:
            # Rare case: page became empty due to mutations — add a disabled placeholder
            placeholder = discord.ui.Select(
                placeholder="(This page is empty — try Prev/Next)",
                options=[discord.SelectOption(label="No items", value="noop")],
                min_values=1,
                max_values=1,
                disabled=True,
            )
            self.add_item(placeholder)

        # Row 1 — actions (Info, Nominate/Retact), disabled if no selection on this page
        info_btn = CatalogInfoButton()
        nom_btn = CatalogNominateToggleButton()
        selected_in_page = self.session.selected_mid in current_slice
        if not selected_in_page:
            info_btn.disabled = True
            nom_btn.disabled = True
        self.add_item(info_btn)
        self.add_item(nom_btn)

        # Row 2 — paging (Prev/Next disabled at bounds) + Close
        prev_btn = CatalogPrevButton()
        next_btn = CatalogNextButton()
        prev_btn.disabled = self.session.page <= 0
        next_btn.disabled = self.session.page >= (self.session.num_pages() - 1)
        close_btn = CatalogCloseButton()
        self.add_item(prev_btn)
        self.add_item(next_btn)
        self.add_item(close_btn)

    async def refresh(self, interaction: discord.Interaction, content_embed: Optional[discord.Embed] = None):
        """Rebuild components and edit the message so the dropdown matches the new page."""
        # Always rebuild so the Select shows the *current* slice
        self._rebuild_components()

        embed = content_embed or _catalog_embed(
            self.session.page, self.session.num_pages(), len(self.session.ids),
            self.session.selected_movie(), self.session.label
        )

        # Handle both initial response and follow-up edits
        if interaction.response.is_done():
            await interaction.followup.edit_message(
                message_id=interaction.message.id,  # type: ignore
                embed=embed,
                view=self
            )
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        # Disable all controls and (best-effort) edit message to show expiry note
        for child in self.children:
            child.disabled = True
        try:
            # We cannot reliably access the message here if ephemeral; skip editing silently on failure
            pass
        except Exception:
            pass

    def ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.owner_id:
            asyncio.create_task(
                interaction.response.send_message("This isn’t your catalog session. Run `/catalog list` for your own.", ephemeral=True)
            )
            return False
        return True

class CatalogSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Select a movie", options=options, min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        view: CatalogView = self.view  # type: ignore
        if not view.ensure_owner(interaction): return
        val = self.values[0]
        mid = int(val.split(":", 1)[1]) if val.startswith("tmdb:") else None
        view.session.selected_mid = mid
        # update embed with info panel
        embed = _catalog_embed(
            view.session.page, view.session.num_pages(), len(view.session.ids),
            view.session.selected_movie(), view.session.label
        )
        await view.refresh(interaction, embed)

class CatalogInfoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Info", style=discord.ButtonStyle.primary)
    async def callback(self, interaction: discord.Interaction):
        view: CatalogView = self.view  # type: ignore
        if not view.ensure_owner(interaction): return
        embed = _catalog_embed(
            view.session.page, view.session.num_pages(), len(view.session.ids),
            view.session.selected_movie(), view.session.label
        )
        await view.refresh(interaction, embed)

class CatalogNominateToggleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Nominate / Retract", style=discord.ButtonStyle.secondary)
    async def callback(self, interaction: discord.Interaction):
        view: CatalogView = self.view  # type: ignore
        if not view.ensure_owner(interaction): return
        mid = view.session.selected_mid
        if not mid or mid not in state.movies:
            await interaction.response.send_message("Choose a movie first.", ephemeral=True)
            return
        user_id = interaction.user.id
        voters = state.nominations.setdefault(mid, [])
        if user_id in voters:
            # retract
            state.nominations[mid] = [u for u in voters if u != user_id]
            if not state.nominations[mid]:
                del state.nominations[mid]
            await save_state(state)
            await interaction.response.send_message(f"❎ Retracted your nomination for **{state.movies[mid].title}**", ephemeral=True)
        else:
            # add
            voters.append(user_id)
            await save_state(state)
            await interaction.response.send_message(f"✅ Nominated **{state.movies[mid].title}**", ephemeral=True)

class CatalogPrevButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Prev", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: CatalogView = self.view  # type: ignore
        if not view.ensure_owner(interaction):
            return
        if view.session.page > 0:
            view.session.page -= 1
            # Clear selection when the page changes to avoid stale selections
            view.session.selected_mid = None
        await view.refresh(interaction)

class CatalogNextButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Next", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: CatalogView = self.view  # type: ignore
        if not view.ensure_owner(interaction):
            return
        if view.session.page < view.session.num_pages() - 1:
            view.session.page += 1
            # Clear selection when the page changes to avoid stale selections
            view.session.selected_mid = None
        await view.refresh(interaction)


class CatalogCloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.danger)
    async def callback(self, interaction: discord.Interaction):
        view: CatalogView = self.view  # type: ignore
        if not view.ensure_owner(interaction): return
        await interaction.response.edit_message(content="Catalog closed.", embed=None, view=None)

# -------------------- Slash Command Groups --------------------

catalog_group = app_commands.Group(name="catalog", description="Manage and browse the movie catalog")
movies_group = app_commands.Group(name="movies", description="Nominations, ballot and voting")
schedule_group = app_commands.Group(name="schedule", description="Suggest and choose time slots")
avail_group = app_commands.Group(name="availability", description="Provide and view availability")
seats_group = app_commands.Group(name="seats", description="Manage seats")
admin_group = app_commands.Group(name="admin", description="Backups and restore")
help_group = app_commands.Group(name="help", description="Offers explanations and walkthroughs of other commands")


# ===== Catalog Commands =====

@catalog_group.command(name="list", description="Browse the catalog with optional filters.")
@app_commands.describe(page_size="Items per page (<=25)", genre="Filter by genre", year="Filter by year", max_runtime="Max runtime (min)", cert="Certification (e.g., PG-13)")
async def catalog_list(interaction: discord.Interaction, page_size: Optional[int] = 10, genre: Optional[str] = None, year: Optional[int] = None, max_runtime: Optional[int] = None, cert: Optional[str] = None):
    ids = _filter_catalog_ids(genre, year, max_runtime, cert)
    if not ids:
        await interaction.response.send_message("No catalog entries match your filters.", ephemeral=True)
        return
    lbl_parts = []
    if genre: lbl_parts.append(f"Genre={genre}")
    if year: lbl_parts.append(f"Year={year}")
    if max_runtime: lbl_parts.append(f"≤{max_runtime} min")
    if cert: lbl_parts.append(f"Cert={cert}")
    label = "Filters: " + ", ".join(lbl_parts) if lbl_parts else ""
    session = CatalogSession(interaction.user.id, ids, page_size or 10, label)
    view = CatalogView(session)
    embed = _catalog_embed(session.page, session.num_pages(), len(ids), None, session.label)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@catalog_group.command(name="search", description="Search catalog by title/genre (case-insensitive).")
@app_commands.describe(query="Search text", page_size="Items per page (<=25)")
async def catalog_search(interaction: discord.Interaction, query: str, page_size: Optional[int] = 10):
    q = query.strip().lower()
    ids = []
    for mid, m in state.movies.items():
        hay = " ".join([m.title] + m.genres).lower()
        if q in hay:
            ids.append(mid)
    if not ids:
        await interaction.response.send_message("No matches found.", ephemeral=True)
        return
    label = f"Search: “{query}”"
    session = CatalogSession(interaction.user.id, sorted(ids, key=lambda x: state.movies[x].title.lower()), page_size or 10, label)
    view = CatalogView(session)
    embed = _catalog_embed(session.page, session.num_pages(), len(ids), None, session.label)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@catalog_group.command(name="add", description="Admin: Add titles or TMDb IDs (comma-separated).")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(titles_or_ids="e.g., 'The Matrix, 603, Interstellar'")
async def catalog_add(interaction: discord.Interaction, titles_or_ids: str):
    await interaction.response.defer(ephemeral=True)
    added = 0
    for part in [p.strip() for p in titles_or_ids.split(",") if p.strip()]:
        try:
            if part.isdigit():
                movie = await build_movie_from_tmdb_id(int(part))
            else:
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

@catalog_group.command(name="remove", description="Admin: Remove movies by TMDb IDs (comma-separated).")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def catalog_remove(interaction: discord.Interaction, ids: str):
    await interaction.response.defer(ephemeral=True)
    await backup_state(state)
    to_remove = []
    for part in [p.strip() for p in ids.split(",") if p.strip()]:
        try:
            to_remove.append(int(part))
        except ValueError:
            await interaction.followup.send(f"Invalid TMDb ID: `{part}`", ephemeral=True)
    removed = 0
    for mid in to_remove:
        if mid in state.movies:
            del state.movies[mid]
            removed += 1
            state.nominations.pop(mid, None)
            if mid in state.movie_options:
                state.movie_options = [m for m in state.movie_options if m != mid]
            for th in state.ticket_holders.values():
                if th.movie_vote == mid:
                    th.movie_vote = None
    await save_state(state)
    await interaction.followup.send(f"Removed {removed} movie(s).", ephemeral=True)

@catalog_group.command(name="clear", description="Admin: Clear the entire catalog (danger!).")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def catalog_clear(interaction: discord.Interaction):
    class ConfirmModal(discord.ui.Modal, title="Confirm Clear Catalog"):
        confirm = discord.ui.TextInput(label="Type 'CLEAR' to confirm", placeholder="CLEAR", max_length=5)
        async def on_submit(self, i: discord.Interaction):
            if str(self.confirm).strip().upper() != "CLEAR":
                await i.response.send_message("Cancelled.", ephemeral=True)
                return
            await backup_state(state)
            state.movies.clear()
            state.nominations.clear()
            state.movie_options.clear()
            for th in state.ticket_holders.values():
                th.movie_vote = None
            await save_state(state)
            await i.response.send_message("Catalog cleared. Ballot and movie votes reset.", ephemeral=True)
    await interaction.response.send_modal(ConfirmModal())

@catalog_group.command(name="info", description="Get info for a title or TMDb ID.")
@app_commands.describe(query="Title or TMDb ID")
async def catalog_info(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True)
    if query.strip().isdigit():
        m = await build_movie_from_tmdb_id(int(query.strip()))
        if not m:
            m = await build_movie_from_title(query)
    else:
        m = await build_movie_from_title(query)
    if not m:
        await interaction.followup.send("Movie not found.", ephemeral=True)
        return
    desc = (m.overview[:400] + "…") if len(m.overview) > 400 else m.overview
    rated = f"{m.advisory.certification} ({m.advisory.region})" if m.advisory else "NR"
    embed = discord.Embed(title=f"{m.title} ({m.year})", description=desc or "(no synopsis)")
    embed.add_field(name="Runtime", value=f"{m.runtime} min", inline=True)
    embed.add_field(name="Rated", value=rated, inline=True)
    if m.genres:
        embed.add_field(name="Genres", value=", ".join(m.genres)[:1024], inline=False)
    if m.trailer_url:
        embed.add_field(name="Trailer", value=m.trailer_url, inline=False)
    if m.poster_url:
        embed.set_thumbnail(url=m.poster_url)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ===== Movies (nominations, panels, ballots) =====

def _top_nominees(n: int = 10) -> List[Tuple[int,int]]:
    pairs = [(mid, len(set(uids))) for mid, uids in state.nominations.items()]
    pairs.sort(key=lambda p: p[1], reverse=True)
    return pairs[:n]

@movies_group.command(name="my_nominations", description="See and retract your nominations.")
async def movies_my_nominations(interaction: discord.Interaction):
    def _options_for_user(uid: int) -> List[discord.SelectOption]:
        mine = [mid for mid, uids in state.nominations.items() if uid in uids]
        opts: List[discord.SelectOption] = []
        for mid in mine[:25]:  # cap to 25
            m = state.movies.get(mid)
            if not m:
                continue
            label = (m.title[:90] + "…") if len(m.title) > 90 else m.title
            opts.append(discord.SelectOption(label=label, value=f"tmdb:{mid}"))
        return opts

    class MyNomsView(discord.ui.View):
        def __init__(self, user_id: int):
            super().__init__(timeout=120)
            self.user_id = user_id
            options = _options_for_user(user_id)
            if options:
                self.select = discord.ui.Select(placeholder="Pick a nomination to retract", options=options, min_values=1, max_values=1)
                self.select.callback = self.on_select  # type: ignore
                self.add_item(self.select)
            else:
                sel = discord.ui.Select(placeholder="No nominations found", options=[discord.SelectOption(label="Empty", value="noop")], disabled=True)
                self.add_item(sel)
            self.add_item(RetractButton())
            self.add_item(RefreshMyNomsButton())
            self.add_item(CloseViewButton())
            self.selected: Optional[int] = None

        async def on_select(self, i: discord.Interaction):
            val = self.select.values[0]
            self.selected = int(val.split(":", 1)[1]) if val.startswith("tmdb:") else None
            await i.response.defer()

    class RetractButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="Retract", style=discord.ButtonStyle.danger)

        async def callback(self, i: discord.Interaction):
            view: MyNomsView = self.view  # type: ignore
            mid = getattr(view, "selected", None)
            if not mid:
                await i.response.send_message("Select a title first.", ephemeral=True)
                return
            voters = state.nominations.get(mid, [])
            if i.user.id not in voters:
                await i.response.send_message("You haven’t nominated that movie.", ephemeral=True)
                return
            state.nominations[mid] = [u for u in voters if u != i.user.id]
            if not state.nominations[mid]:
                del state.nominations[mid]
            await save_state(state)

            # Rebuild the view to reflect the change
            new_view = MyNomsView(i.user.id)
            await i.response.edit_message(content="Nomination retracted. (View refreshed.)", view=new_view)
            # Optional toast as a follow-up
            await i.followup.send("Nomination retracted ✅", ephemeral=True)

    class RefreshMyNomsButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="Refresh", style=discord.ButtonStyle.secondary)

        async def callback(self, i: discord.Interaction):
            new_view = MyNomsView(i.user.id)
            await i.response.edit_message(content="Refreshed your nominations.", view=new_view)

    class CloseViewButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="Close", style=discord.ButtonStyle.secondary)

        async def callback(self, i: discord.Interaction):
            await i.response.edit_message(content="Closed.", view=None)

    view = MyNomsView(interaction.user.id)
    await interaction.response.send_message("Your nominations:", view=view, ephemeral=True)

@movies_group.command(name="nominees_panel", description="Post a public nominees panel with CTAs.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def movies_nominees_panel(interaction: discord.Interaction):
    def _embed() -> discord.Embed:
        pairs = _top_nominees(10)
        if not pairs:
            return discord.Embed(title="🎬 Top Nominees", description="No nominations yet.")
        lines = []
        for mid, count in pairs:
            m = state.movies.get(mid)
            if not m:
                continue
            lines.append(f"• **{m.title} ({m.year})** — {count} nomination(s)")
        return discord.Embed(title="🎬 Top Nominees", description="\n".join(lines))

    class PanelView(discord.ui.View):
        def __init__(self, user_id: int):
            super().__init__(timeout=600)
            self.user_id = user_id
            self.add_item(OpenCatalogButton())
            self.add_item(OpenMyNomsButton())
            self.add_item(RefreshPanelButton())

    class OpenCatalogButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="Nominate from Catalog", style=discord.ButtonStyle.primary)

        async def callback(self, i: discord.Interaction):
            ids = sorted(list(state.movies.keys()), key=lambda x: state.movies[x].title.lower())
            session = CatalogSession(i.user.id, ids, 10, "Browse all")
            view = CatalogView(session)
            embed2 = _catalog_embed(session.page, session.num_pages(), len(ids), None, session.label)
            await i.response.send_message(embed=embed2, view=view, ephemeral=True)

    class OpenMyNomsButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="View My Nominations", style=discord.ButtonStyle.secondary)

        async def callback(self, i: discord.Interaction):
            await movies_my_nominations(i)  # reuse

    class RefreshPanelButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="Refresh", style=discord.ButtonStyle.secondary)

        async def callback(self, i: discord.Interaction):
            emb = _embed()
            # Update the original public panel message
            await i.response.edit_message(embed=emb, view=self.view)  # type: ignore

    embed = _embed()
    await interaction.response.send_message(embed=embed, view=PanelView(interaction.user.id))

@movies_group.command(name="nominate", description="Nominate a movie by TMDb ID (power users).")
async def movies_nominate(interaction: discord.Interaction, id: int):
    if id not in state.movies:
        await interaction.response.send_message("That TMDb ID is not in the catalog.", ephemeral=True)
        return
    voters = state.nominations.setdefault(id, [])
    if interaction.user.id not in voters:
        voters.append(interaction.user.id)
        await save_state(state)
    await interaction.response.send_message(f"✅ Nominated **{state.movies[id].title}**", ephemeral=True)

@movies_group.command(name="retract_nomination", description="Retract your nomination by TMDb ID (power users).")
async def movies_retract_nomination(interaction: discord.Interaction, id: int):
    voters = state.nominations.get(id)
    if not voters or interaction.user.id not in voters:
        await interaction.response.send_message("You haven’t nominated that movie.", ephemeral=True)
        return
    state.nominations[id] = [u for u in voters if u != interaction.user.id]
    if not state.nominations[id]:
        del state.nominations[id]
    await save_state(state)
    await interaction.response.send_message("Nomination retracted.", ephemeral=True)

@movies_group.command(name="open_vote", description="Admin: open a movie ballot (≤3 options).")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(strategy="Use top nominees or manual IDs", id1="TMDb ID 1", id2="TMDb ID 2", id3="TMDb ID 3")
@app_commands.choices(strategy=[app_commands.Choice(name="top", value="top"), app_commands.Choice(name="manual", value="manual")])
async def movies_open_vote(interaction: discord.Interaction, strategy: app_commands.Choice[str], id1: Optional[int] = None, id2: Optional[int] = None, id3: Optional[int] = None):
    if strategy.value == "top":
        ids = [mid for mid, _ in _top_nominees(3) if mid in state.movies]
        if not ids:
            await interaction.response.send_message("No nominees to open a vote with.", ephemeral=True)
            return
    else:
        ids = [x for x in [id1, id2, id3] if x]
        if not ids or len(ids) > 3:
            await interaction.response.send_message("Provide 1–3 TMDb IDs.", ephemeral=True); return
        for mid in ids:
            if mid not in state.movies:
                await interaction.response.send_message(f"TMDb ID `{mid}` not in catalog.", ephemeral=True); return

    state.movie_options = ids[:3]
    ensure_ballot_size()
    await save_state(state)

    # Build view and upsert the single active message
    view = MovieVoteView([state.movies[mid] for mid in state.movie_options])
    content = "🎬 **Vote for a movie** (choose one):"
    prior = await _fetch_message(state.active_movie_ballot_message)
    if prior:
        try:
            await prior.edit(content=content, view=view)
            await interaction.response.send_message("Updated the existing movie ballot.", ephemeral=True)
            return
        except Exception:
            pass
    # Create new message
    msg = await interaction.channel.send(content, view=view)  # type: ignore
    state.active_movie_ballot_message = MessageRef(channel_id=msg.channel.id, message_id=msg.id)
    await save_state(state)
    await interaction.response.send_message("Opened a new movie ballot.", ephemeral=True)

@movies_group.command(name="close_vote", description="Admin: close and remove the active movie ballot.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def movies_close_vote(interaction: discord.Interaction):
    msg = await _fetch_message(state.active_movie_ballot_message)
    state.active_movie_ballot_message = None
    state.movie_options.clear()
    for th in state.ticket_holders.values():
        th.movie_vote = None
    await save_state(state)
    if msg:
        try:
            await msg.delete()
        except Exception:
            pass
    await interaction.response.send_message("Closed the movie ballot and cleared votes.", ephemeral=True)

@movies_group.command(name="status", description="Show current movie ballot options and vote counts.")
async def movies_status(interaction: discord.Interaction):
    if not state.movie_options:
        await interaction.response.send_message("No active movie ballot.", ephemeral=True)
        return
    tally: Dict[int, int] = {mid: 0 for mid in state.movie_options}
    for th in state.ticket_holders.values():
        if th.movie_vote in tally:
            tally[th.movie_vote] += 1
    lines = []
    for mid in state.movie_options:
        m = state.movies.get(mid)
        if not m:
            continue
        lines.append(f"• **{m.title} ({m.year})** — {tally[mid]} vote(s)")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@movies_group.command(name="vote", description="Open the current movie ballot (ephemeral) if one exists.")
async def movies_vote(interaction: discord.Interaction):
    if not state.movie_options:
        await interaction.response.send_message("No active movie ballot. Ask an admin to `/movies open_vote`.", ephemeral=True)
        return
    view = MovieVoteView([state.movies[mid] for mid in state.movie_options if mid in state.movies])
    await interaction.response.send_message("Vote for a movie:", view=view, ephemeral=True)

@movies_group.command(name="unvote", description="Remove your current movie vote.")
async def movies_unvote(interaction: discord.Interaction):
    th = get_or_create_holder(interaction.user)
    if th.movie_vote is None:
        await interaction.response.send_message("You haven’t voted yet.", ephemeral=True)
        return
    th.movie_vote = None
    await save_state(state)
    await interaction.response.send_message("Your movie vote was cleared.", ephemeral=True)

@movies_group.command(name="clear_votes", description="Admin: clear all users’ movie votes.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def movies_clear_votes(interaction: discord.Interaction):
    await backup_state(state)
    for th in state.ticket_holders.values():
        th.movie_vote = None
    await save_state(state)
    await interaction.response.send_message("All movie votes cleared.", ephemeral=True)


# ===== Availability (CRUD + Wizard) =====

# CRUD

@avail_group.command(name="list", description="List your availability (or another user's if you are an admin).")
@app_commands.describe(user="Optional: another user")
async def availability_list(interaction: discord.Interaction, user: Optional[discord.User] = None):
    target = user or interaction.user
    if user and not getattr(interaction.user.guild_permissions, "administrator", False):
        await interaction.response.send_message("Only admins can view others' availability.", ephemeral=True)
        return
    th = get_or_create_holder(target)
    if not th.availability:
        await interaction.response.send_message(f"{target.display_name} has no availability set.", ephemeral=True)
        return
    dsl = blocks_to_dsl(th.availability)
    await interaction.response.send_message(f"**{target.display_name}'s availability:**\n{dsl}", ephemeral=True)

@avail_group.command(name="export", description="Export your availability as a compact string.")
async def availability_export(interaction: discord.Interaction):
    th = get_or_create_holder(interaction.user)
    dsl = blocks_to_dsl(th.availability)
    await interaction.response.send_message(dsl or "(no availability set)", ephemeral=True)

@avail_group.command(name="clear", description="Clear all availability, or only a specific day.")
@app_commands.describe(day="Optional day to clear (e.g., Monday)")
async def availability_clear(interaction: discord.Interaction, day: Optional[str] = None):
    th = get_or_create_holder(interaction.user)
    if day:
        try:
            d = canonical_day(day)
        except Exception as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        th.availability = remove_day_from_list(th.availability, d)
        await save_state(state)
        await interaction.response.send_message(f"Removed all blocks for **{d}**.", ephemeral=True)
    else:
        th.availability = []
        await save_state(state)
        await interaction.response.send_message("Cleared all availability.", ephemeral=True)

@avail_group.command(name="add_block", description="Add a single availability block.")
@app_commands.describe(day="Weekday", start="Start HH:MM (00/15/30/45)", end="End HH:MM (00/15/30/45)")
async def availability_add_block(interaction: discord.Interaction, day: str, start: str, end: str):
    try:
        d = canonical_day(day)
    except Exception as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    if not (is_valid_hhmm(start) and is_valid_hhmm(end) and is_quarter_minute(start) and is_quarter_minute(end)):
        await interaction.response.send_message("Invalid time(s). Use HH:MM with minutes in {00,15,30,45}.", ephemeral=True)
        return
    if start >= end:
        await interaction.response.send_message("Start must be before end.", ephemeral=True)
        return
    th = get_or_create_holder(interaction.user)
    new = DayTimeRange(day=d, start_time=start, end_time=end)
    th.availability = merge_blocks(th.availability, [new])
    await save_state(state)
    await interaction.response.send_message(f"Added **{d} {start}-{end}**.", ephemeral=True)

@avail_group.command(name="add_multi", description="Add the same block across multiple days (e.g., 'Mon-Thu, Sat').")
@app_commands.describe(days="Day expression like 'Mon-Thu, Sat'", start="Start HH:MM", end="End HH:MM")
async def availability_add_multi(interaction: discord.Interaction, days: str, start: str, end: str):
    if not (is_valid_hhmm(start) and is_valid_hhmm(end) and is_quarter_minute(start) and is_quarter_minute(end)):
        await interaction.response.send_message("Invalid time(s). Use HH:MM with minutes in {00,15,30,45}.", ephemeral=True)
        return
    if start >= end:
        await interaction.response.send_message("Start must be before end.", ephemeral=True)
        return
    try:
        day_list = day_expr_to_list(days)
        if not day_list:
            raise ValueError("No valid days found.")
    except Exception as e:
        await interaction.response.send_message(f"Day parse error: {e}", ephemeral=True)
        return
    th = get_or_create_holder(interaction.user)
    adds = [DayTimeRange(day=d, start_time=start, end_time=end) for d in day_list]
    th.availability = merge_blocks(th.availability, adds)
    await save_state(state)
    await interaction.response.send_message(f"Added {len(adds)} block(s).", ephemeral=True)

@avail_group.command(name="remove_block", description="Remove an exact block if it exists.")
@app_commands.describe(day="Weekday", start="Start HH:MM", end="End HH:MM")
async def availability_remove_block(interaction: discord.Interaction, day: str, start: str, end: str):
    try:
        d = canonical_day(day)
    except Exception as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    th = get_or_create_holder(interaction.user)
    before = len(th.availability)
    th.availability = remove_block_from_list(th.availability, (d, start, end))
    removed = before - len(th.availability)
    await save_state(state)
    if removed:
        await interaction.response.send_message(f"Removed **{d} {start}-{end}**.", ephemeral=True)
    else:
        await interaction.response.send_message("No matching block found.", ephemeral=True)

@avail_group.command(name="set", description="Replace your availability using text (e.g., 'Mon 17:00-23:00, Tue-Thu 18:00-22:00').")
@app_commands.describe(input="Comma-separated blocks")
async def availability_set(interaction: discord.Interaction, input: str):
    try:
        ranges = parse_availability_string(input)
    except ValueError as e:
        await interaction.response.send_message(f"Parse error: {e}", ephemeral=True)
        return
    th = get_or_create_holder(interaction.user)
    th.availability = dedupe_blocks(ranges)
    await save_state(state)
    await interaction.response.send_message("✅ Availability replaced.", ephemeral=True)

@avail_group.command(name="add", description="(Alias) Replace availability via text DSL (same as /availability set).")
@app_commands.describe(input="Comma-separated blocks")
async def availability_add_alias(interaction: discord.Interaction, input: str):
    await availability_set.callback(interaction, input)  # type: ignore

@avail_group.command(name="append", description="Append availability using text (deduped).")
@app_commands.describe(input="Comma-separated blocks")
async def availability_append(interaction: discord.Interaction, input: str):
    try:
        adds = parse_availability_string(input)
    except ValueError as e:
        await interaction.response.send_message(f"Parse error: {e}", ephemeral=True)
        return
    th = get_or_create_holder(interaction.user)
    th.availability = merge_blocks(th.availability, adds)
    await save_state(state)
    await interaction.response.send_message("✅ Availability appended.", ephemeral=True)

@avail_group.command(name="view", description="Render the availability chart (optionally for a specific day).")
@app_commands.describe(day="Optional day filter, e.g., Monday")
async def availability_view(interaction: discord.Interaction, day: Optional[str] = None):
    if day and day not in WEEKDAY_ORDER:
        await interaction.response.send_message("Invalid day. Use full weekday name.", ephemeral=True)
        return
    path = create_availability_chart(state.ticket_holders, day=day)
    # Include a small caption with counts
    total_users = len(state.ticket_holders)
    users_with_any = sum(1 for th in state.ticket_holders.values() if th.availability)
    caption = f"Users shown: {total_users} • With availability: {users_with_any}"
    await interaction.response.send_message(content=caption, file=discord.File(path), ephemeral=True)
    # Best-effort cleanup of older charts
    _cleanup_old_charts()

# Wizard (3 steps, fixed rows, time modal, capped remove list)

PRESETS = {
    "Weeknights 18:00–22:00": [("Monday","18:00","22:00"), ("Tuesday","18:00","22:00"),
                               ("Wednesday","18:00","22:00"), ("Thursday","18:00","22:00")],
    "Friday 16:00–23:00":     [("Friday","16:00","23:00")],
    "Weekends Afternoon":     [("Saturday","12:00","18:00"), ("Sunday","12:00","18:00")],
    "Weekends Evening":       [("Saturday","18:00","23:00"), ("Sunday","18:00","23:00")],
}

class WizardDraft:
    def __init__(self):
        self.selected_days: List[str] = []
        self.blocks: List[Tuple[str,str,str]] = []  # (day,start,end)
        self.days_confirmed: bool = False
        self.cur_start: Optional[str] = None
        self.cur_end: Optional[str] = None
    def add_block_for_selected_days(self):
        if not (self.cur_start and self.cur_end):
            return 0
        added = 0
        for d in self.selected_days:
            tup = (d, self.cur_start, self.cur_end)
            if tup not in self.blocks:
                self.blocks.append(tup)
                added += 1
        return added
    def add_preset_for_selected_days(self, preset_name: str):
        triples = PRESETS.get(preset_name, [])
        sel = set(self.selected_days) if self.selected_days else set(d for d,_,_ in triples)
        added = 0
        for d,s,e in triples:
            if d in sel and (d,s,e) not in self.blocks:
                self.blocks.append((d,s,e))
                added += 1
        return added
    def remove_block(self, block_str: str):
        try:
            day, rest = block_str.split(" ", 1)
            start, end = rest.split("-", 1)
            start, end = start.strip(), end.strip()
        except Exception:
            return False
        before = len(self.blocks)
        self.blocks = [b for b in self.blocks if not (b[0]==day and b[1]==start and b[2]==end)]
        return len(self.blocks) < before
    def clear(self):
        self.selected_days = []
        self.blocks = []
        self.days_confirmed = False
        self.cur_start = None
        self.cur_end = None

WIZARD_DRAFTS: Dict[int, WizardDraft] = {}

def get_draft(user_id: int) -> WizardDraft:
    d = WIZARD_DRAFTS.get(user_id)
    if not d:
        d = WizardDraft()
        WIZARD_DRAFTS[user_id] = d
    return d

def render_draft(draft: WizardDraft) -> str:
    lines = []
    if draft.selected_days:
        lines.append(f"**Selected days:** {', '.join(draft.selected_days)}")
    else:
        lines.append("**Selected days:** *(none yet)*")
    if draft.cur_start and draft.cur_end:
        lines.append(f"**Current time:** {draft.cur_start}–{draft.cur_end}")
    else:
        lines.append("**Current time:** *(not set)*")
    if draft.blocks:
        lines.append("**Draft blocks:**")
        blocks_sorted = sorted(draft.blocks, key=lambda t: (DAY_INDEX.get(t[0],7), t[1], t[2]))
        for d,s,e in blocks_sorted:
            lines.append(f"• {d} {s}-{e}")
    else:
        lines.append("**Draft blocks:** *(none yet)*")
    return "\n".join(lines)

# Step 1 components
class DayMultiSelect(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=d, value=d) for d in WEEKDAY_ORDER]
        super().__init__(placeholder="Step 1: Choose day(s)", min_values=1, max_values=len(opts), options=opts)
    async def callback(self, interaction: discord.Interaction):
        draft = get_draft(interaction.user.id)
        draft.selected_days = list(self.values)
        draft.days_confirmed = False
        view: AvailabilityWizardView = self.view  # type: ignore
        await interaction.response.edit_message(content="Days selected. Click **Confirm Days** to proceed.\n\n" + render_draft(draft), view=view)

class ConfirmDaysButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Confirm Days", style=discord.ButtonStyle.primary)
    async def callback(self, interaction: discord.Interaction):
        draft = get_draft(interaction.user.id)
        if not draft.selected_days:
            await interaction.response.send_message("Select at least one day first.", ephemeral=True)
            return
        draft.days_confirmed = True
        view: AvailabilityWizardView = self.view  # type: ignore
        await view.build(2)
        await interaction.response.edit_message(content="Days confirmed. Now **Set Time…** then **Add Block**, or use **Preset**.\n\n" + render_draft(draft), view=view)

class CancelWizardButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)
    async def callback(self, interaction: discord.Interaction):
        WIZARD_DRAFTS.pop(interaction.user.id, None)
        await interaction.response.edit_message(content="Wizard cancelled. No changes saved.", view=None)

# Step 2 components
class ChangeDaysButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Change Days", style=discord.ButtonStyle.secondary)
    async def callback(self, interaction: discord.Interaction):
        draft = get_draft(interaction.user.id)
        draft.days_confirmed = False
        view: AvailabilityWizardView = self.view  # type: ignore
        await view.build(1)
        await interaction.response.edit_message(content="You can change the selected days.\n\n" + render_draft(draft), view=view)

class SetTimeModal(discord.ui.Modal, title="Set Time Range"):
    start = discord.ui.TextInput(label="Start (HH:MM, minutes 00/15/30/45)", placeholder="18:00", max_length=5)
    end   = discord.ui.TextInput(label="End (HH:MM, minutes 00/15/30/45)", placeholder="22:00", max_length=5)
    def __init__(self, parent_view: "AvailabilityWizardView"):
        super().__init__()
        self.parent_view = parent_view
    async def on_submit(self, interaction: discord.Interaction):
        draft = get_draft(self.parent_view.user_id)
        s = str(self.start).strip()
        e = str(self.end).strip()
        if not (is_valid_hhmm(s) and is_valid_hhmm(e) and is_quarter_minute(s) and is_quarter_minute(e)):
            await interaction.response.send_message("Invalid time(s). Use HH:MM with minutes in {00,15,30,45}.", ephemeral=True)
            return
        if s >= e:
            await interaction.response.send_message("Start must be before end.", ephemeral=True)
            return
        draft.cur_start, draft.cur_end = s, e
        await interaction.response.edit_message(content="Time set.\n\n" + render_draft(draft), view=self.parent_view)

class SetTimeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set Time…", style=discord.ButtonStyle.primary)
    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityWizardView = self.view  # type: ignore
        await interaction.response.send_modal(SetTimeModal(view))

class AddBlockButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Add Block for Selected Days", style=discord.ButtonStyle.success)
    async def callback(self, interaction: discord.Interaction):
        draft = get_draft(interaction.user.id)
        if not draft.days_confirmed or not draft.selected_days:
            await interaction.response.send_message("Please select day(s) and click **Confirm Days** first.", ephemeral=True)
            return
        if not (draft.cur_start and draft.cur_end):
            await interaction.response.send_message("Please **Set Time…** first.", ephemeral=True)
            return
        added = draft.add_block_for_selected_days()
        view: AvailabilityWizardView = self.view  # type: ignore
        await interaction.response.edit_message(content=f"Added {added} block(s) to draft.\n\n" + render_draft(draft), view=view)

class PresetButton(discord.ui.Button):
    def __init__(self, name: str):
        super().__init__(label=name, style=discord.ButtonStyle.secondary)
        self.name = name
    async def callback(self, interaction: discord.Interaction):
        draft = get_draft(interaction.user.id)
        added = draft.add_preset_for_selected_days(self.name)
        view: AvailabilityWizardView = self.view  # type: ignore
        await interaction.response.edit_message(content=f"Preset **{self.name}** added {added} block(s).\n\n" + render_draft(draft), view=view)

class ReviewButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Review Draft", style=discord.ButtonStyle.primary)
    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityWizardView = self.view  # type: ignore
        await view.build(3)
        draft = get_draft(view.user_id)
        await interaction.response.edit_message(content="Review your draft. Save or modify below.\n\n" + render_draft(draft), view=view)

# Step 3 components
class RemoveDraftBlockSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Remove a Draft Block…", options=options, min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityWizardView = self.view  # type: ignore
        draft = get_draft(view.user_id)
        ok = draft.remove_block(self.values[0])
        await view.build(3)
        await interaction.response.edit_message(content=("Block removed.\n\n" if ok else "No change.\n\n") + render_draft(draft), view=view)

class ClearDraftButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Clear Draft", style=discord.ButtonStyle.danger)
    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityWizardView = self.view  # type: ignore
        WIZARD_DRAFTS.pop(view.user_id, None)
        await view.build(1)
        await interaction.response.edit_message(content="Draft cleared. Start again by selecting days.\n\n" + render_draft(get_draft(view.user_id)), view=view)

class SaveReplaceButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save (Replace)", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        th = get_or_create_holder(interaction.user)
        draft = get_draft(interaction.user.id)
        new_ranges = [DayTimeRange(day=d, start_time=s, end_time=e) for d, s, e in draft.blocks]
        th.availability = dedupe_blocks(new_ranges)
        await save_state(state)
        WIZARD_DRAFTS.pop(interaction.user.id, None)
        summary = blocks_to_dsl(th.availability) or "(no availability set)"
        await interaction.response.send_message(f"✅ Availability saved (replaced).\n\n**Current:**\n{summary}", ephemeral=True)


class SaveAppendButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save (Append)", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        th = get_or_create_holder(interaction.user)
        draft = get_draft(interaction.user.id)
        additions = [DayTimeRange(day=d, start_time=s, end_time=e) for d, s, e in draft.blocks]
        th.availability = merge_blocks(th.availability, additions)
        await save_state(state)
        WIZARD_DRAFTS.pop(interaction.user.id, None)
        summary = blocks_to_dsl(th.availability) or "(no availability set)"
        await interaction.response.send_message(f"✅ Availability saved (appended).\n\n**Current:**\n{summary}", ephemeral=True)

class BackToTimesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back to Time Entry", style=discord.ButtonStyle.secondary)
    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityWizardView = self.view  # type: ignore
        await view.build(2)
        draft = get_draft(view.user_id)
        await interaction.response.edit_message(content="Back to Step 2.\n\n" + render_draft(draft), view=view)

class AvailabilityWizardView(discord.ui.View):
    """Three-step wizard that never exceeds 5 rows of components, and doesn't rely on View.message."""
    def __init__(self, user_id: int):
        super().__init__(timeout=600)  # 10 minutes
        self.step = 1  # 1=days, 2=times, 3=review
        self.user_id = user_id

    async def build(self, step: int):
        self.step = step
        for child in list(self.children):
            self.remove_item(child)

        if step == 1:
            self.add_item(DayMultiSelect())
            self.add_item(ConfirmDaysButton())
            self.add_item(CancelWizardButton())

        elif step == 2:
            self.add_item(SetTimeButton())
            self.add_item(AddBlockButton())
            # up to 4 preset buttons
            preset_names = list(PRESETS.keys())
            if preset_names:
                self.add_item(PresetButton(preset_names[0]))
            if len(preset_names) > 1:
                self.add_item(PresetButton(preset_names[1]))
            if len(preset_names) > 2:
                self.add_item(PresetButton(preset_names[2]))
            if len(preset_names) > 3:
                self.add_item(PresetButton(preset_names[3]))
            self.add_item(ChangeDaysButton())
            self.add_item(ReviewButton())
            self.add_item(ClearDraftButton())
            self.add_item(CancelWizardButton())

        elif step == 3:
            draft = get_draft(self.user_id)
            opts: List[discord.SelectOption] = []
            if draft and draft.blocks:
                blocks_sorted = sorted(draft.blocks, key=lambda t: (DAY_INDEX.get(t[0],7), t[1], t[2]))
                for d,s,e in blocks_sorted[:25]:
                    label = f"{d} {s}-{e}"
                    opts.append(discord.SelectOption(label=label, value=label))
                self.add_item(RemoveDraftBlockSelect(opts))
            else:
                disabled_select = RemoveDraftBlockSelect([discord.SelectOption(label="No draft blocks", value="noop")])
                disabled_select.disabled = True
                self.add_item(disabled_select)
            self.add_item(SaveReplaceButton())
            self.add_item(SaveAppendButton())
            self.add_item(BackToTimesButton())
            self.add_item(CancelWizardButton())

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        WIZARD_DRAFTS.pop(self.user_id, None)

@avail_group.command(name="wizard", description="Interactive availability setup: Confirm Days → Set Time → Add Blocks → Review & Save.")
@app_commands.describe(mode="Start fresh (new) or edit your current availability")
@app_commands.choices(mode=[app_commands.Choice(name="new", value="new"), app_commands.Choice(name="edit", value="edit")])
async def availability_wizard(interaction: discord.Interaction, mode: Optional[app_commands.Choice[str]] = None):
    user_id = interaction.user.id
    WIZARD_DRAFTS.pop(user_id, None)
    draft = get_draft(user_id)
    if mode and mode.value == "edit":
        th = get_or_create_holder(interaction.user)
        draft.blocks = [(r.day, r.start_time, r.end_time) for r in th.availability]
    view = AvailabilityWizardView(user_id=user_id)
    await view.build(1)
    await interaction.response.send_message(
        "Step 1: Select day(s) and click **Confirm Days**.\n"
        "Step 2: **Set Time…**, then **Add Block for Selected Days** (or use **Preset** buttons).\n"
        "Step 3: **Review Draft** → Save (Replace/Append) or Remove Blocks / Clear / Cancel.\n\n" +
        render_draft(draft),
        view=view,
        ephemeral=True
    )


# ===== Schedule (suggestions, voting, single active message) =====

@schedule_group.command(name="suggest", description="Compute overlaps and suggest up to 3 time slots for voting.")
async def schedule_suggest(interaction: discord.Interaction):
    overlaps = compute_common_overlaps(state.ticket_holders)
    options: List[ScheduleSlot] = []
    seen_keys = set()
    for s in overlaps:
        if s.key in seen_keys:
            continue
        options.append(s)
        seen_keys.add(s.key)
        if len(options) >= 3:
            break
    if len(options) < 3:
        for s in compute_popular_slots(state.ticket_holders):
            if s.key in seen_keys:
                continue
            options.append(s)
            seen_keys.add(s.key)
            if len(options) >= 3:
                break

    state.time_options = options[:3]
    ensure_time_options_size()

    # Scrub stale user time votes that don't match current options
    valid_keys = {s.key for s in state.time_options}
    if valid_keys:
        for th in state.ticket_holders.values():
            if th.time_vote and th.time_vote not in valid_keys:
                th.time_vote = None
    else:
        # If no options, clear all votes
        for th in state.ticket_holders.values():
            th.time_vote = None

    await save_state(state)

    if not state.time_options:
        await interaction.response.send_message("No suitable time windows yet. Ask participants to add availability.", ephemeral=True)
        return

    view = TimeVoteView(state.time_options)
    content = "🗓️ **Vote for a time slot** (choose one):"
    prior = await _fetch_message(state.active_time_ballot_message)
    if prior:
        try:
            await prior.edit(content=content, view=view)
            await interaction.response.send_message("Updated the existing time-slot vote.", ephemeral=True)
            return
        except Exception:
            pass
    msg = await interaction.channel.send(content, view=view)  # type: ignore
    state.active_time_ballot_message = MessageRef(channel_id=msg.channel.id, message_id=msg.id)
    await save_state(state)
    await interaction.response.send_message("Opened a new time-slot vote.", ephemeral=True)

@schedule_group.command(name="close_vote", description="Admin: close and remove the active time-slot vote.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def schedule_close_vote(interaction: discord.Interaction):
    msg = await _fetch_message(state.active_time_ballot_message)
    state.active_time_ballot_message = None
    state.time_options.clear()
    for th in state.ticket_holders.values():
        th.time_vote = None
    await save_state(state)
    if msg:
        try:
            await msg.delete()
        except Exception:
            pass
    await interaction.response.send_message("Closed the time-slot vote and cleared votes.", ephemeral=True)

@schedule_group.command(name="vote", description="Open the current time-slot voting panel (ephemeral) if one exists.")
async def schedule_vote(interaction: discord.Interaction):
    if not state.time_options:
        await interaction.response.send_message("No active time-slot vote. Ask an admin to `/schedule suggest`.", ephemeral=True)
        return
    view = TimeVoteView(state.time_options)
    await interaction.response.send_message("Vote for a time slot:", view=view, ephemeral=True)

@schedule_group.command(name="status", description="Show current time options and vote counts.")
async def schedule_status(interaction: discord.Interaction):
    if not state.time_options:
        await interaction.response.send_message("No active time options.", ephemeral=True)
        return
    tally: Dict[str, int] = {s.key: 0 for s in state.time_options}
    for th in state.ticket_holders.values():
        key = th.time_vote
        if key and key in tally:
            tally[key] += 1
    lines = []
    for s in state.time_options:
        lines.append(f"{s.day} {s.start_time}-{s.end_time} — {tally[s.key]} vote(s)")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@schedule_group.command(name="unvote", description="Remove your current time-slot vote.")
async def schedule_unvote(interaction: discord.Interaction):
    th = get_or_create_holder(interaction.user)
    if not th.time_vote:
        await interaction.response.send_message("You haven’t voted yet.", ephemeral=True)
        return
    th.time_vote = None
    await save_state(state)
    await interaction.response.send_message("Your time-slot vote was cleared.", ephemeral=True)

@schedule_group.command(name="clear_votes", description="Admin: clear all users’ time-slot votes.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def schedule_clear_votes(interaction: discord.Interaction):
    await backup_state(state)
    for th in state.ticket_holders.values():
        th.time_vote = None
    await save_state(state)
    await interaction.response.send_message("All time-slot votes cleared.", ephemeral=True)

@schedule_group.command(name="choose", description="Finalize a time slot using the leading movie runtime and show ISO-8601 start/end.")
@app_commands.describe(day="Weekday", start="Start HH:MM", end="End HH:MM")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def schedule_choose(interaction: discord.Interaction, day: str, start: str, end: str):
    try:
        norm_day = canonical_day(day)
    except Exception as e:
        await interaction.response.send_message(f"Day error: {e}", ephemeral=True)
        return
    if not (is_valid_hhmm(start) and is_valid_hhmm(end) and is_quarter_minute(start) and is_quarter_minute(end)):
        await interaction.response.send_message("Invalid time(s). Use HH:MM with minutes in {00,15,30,45}.", ephemeral=True)
        return
    if start >= end:
        await interaction.response.send_message("Start must be before end.", ephemeral=True)
        return

    key = f"slot:{norm_day}|{start}|{end}"
    slot = next((s for s in state.time_options if s.key == key), None)
    if not slot:
        await interaction.response.send_message("That slot is not in the current options.", ephemeral=True)
        return
    m = leading_movie()
    if not m:
        await interaction.response.send_message("No leading movie yet. Ask users to vote for a movie.", ephemeral=True)
        return
    tz = ZoneInfo(TIMEZONE)
    iso_start, iso_end = slot_to_iso_start_end(tz, slot, m.runtime)
    await interaction.response.send_message(
        f"🗓️ Scheduled **{m.title}**\nStart: `{iso_start}`\nEnd: `{iso_end}`\n"
        f"Rating: `{m.advisory.certification if m.advisory else 'NR'}` "
        f"{'• ' + ', '.join(m.advisory.descriptors) if (m.advisory and m.advisory.descriptors) else ''}",
        ephemeral=False
    )

# ===== Seats =====

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
    if th.user_id not in state.waitlist:
        state.waitlist.append(th.user_id)
        await save_state(state)
    await interaction.response.send_message("All seats are taken. You have been added to the waitlist.", ephemeral=True)

@seats_group.command(name="unbook", description="Alias for release your seat.")
async def seats_unbook(interaction: discord.Interaction):
    await seats_release.callback(interaction)  # type: ignore

@seats_group.command(name="status", description="Show seat assignments and waitlist.")
async def seats_status(interaction: discord.Interaction):
    assigned = [h for h in state.ticket_holders.values() if h.seat is not None]
    assigned.sort(key=lambda x: x.seat or 0)
    lines = [f"Seat #{h.seat}: {h.user_name}" for h in assigned] or ["No seats assigned."]
    if state.waitlist:
        names = []
        for uid in state.waitlist:
            name = state.ticket_holders.get(uid).user_name if uid in state.ticket_holders else None
            if not name and interaction.guild:
                member = interaction.guild.get_member(uid)
                if member:
                    name = member.display_name
            names.append(name or str(uid))
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

@seats_group.command(name="clear", description="Admin: clear all seat assignments and waitlist.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def seats_clear(interaction: discord.Interaction):
    await backup_state(state)
    for th in state.ticket_holders.values():
        th.seat = None
    state.waitlist.clear()
    await save_state(state)
    await interaction.response.send_message("All seats and waitlist cleared.", ephemeral=True)

@seats_group.command(name="swap", description="Admin: swap seats between two users.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(user1="First user", user2="Second user")
async def seats_swap(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
    th1 = get_or_create_holder(user1)
    th2 = get_or_create_holder(user2)
    th1.seat, th2.seat = th2.seat, th1.seat
    await save_state(state)
    await interaction.response.send_message(f"Swapped seats between {th1.user_name} and {th2.user_name}.", ephemeral=True)

# ===== Admin (backups/restore with autosave restart) =====

@admin_group.command(name="backup", description="Create a timestamped backup of current state.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def admin_backup(interaction: discord.Interaction):
    name = await backup_state(state)
    await interaction.response.send_message(f"Backup created: `{name}`", ephemeral=True)

@admin_group.command(name="backups", description="List available backups (newest first).")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def admin_backups(interaction: discord.Interaction):
    names = await list_backups()
    if not names:
        await interaction.response.send_message("No backups found.", ephemeral=True)
        return
    await interaction.response.send_message("\n".join(names[:30]), ephemeral=True)

@admin_group.command(name="undo", description="Restore the most recent backup.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def admin_undo(interaction: discord.Interaction):
    names = await list_backups()
    if not names:
        await interaction.response.send_message("No backups to restore.", ephemeral=True)
        return
    new_state = await restore_state_from_backup(names[0])
    await _restart_autosave(new_state)
    await interaction.response.send_message(f"Restored from `{names[0]}`.", ephemeral=True)

@admin_group.command(name="restore", description="Restore a specific backup by name (from /admin backups).")
@app_commands.describe(name="Backup file name, e.g., state-YYYYMMDD-HHMMSS.json")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def admin_restore(interaction: discord.Interaction, name: str):
    try:
        new_state = await restore_state_from_backup(name)
    except FileNotFoundError:
        await interaction.response.send_message("Backup not found.", ephemeral=True)
        return
    await _restart_autosave(new_state)
    await interaction.response.send_message(f"Restored from `{name}`.", ephemeral=True)

# Help topics
_HELP_TOPICS = [
    ("overview", "Overview"),
    ("catalog", "Catalog"),
    ("movies", "Movies"),
    ("availability", "Availability"),
    ("schedule", "Schedule"),
    ("seats", "Seats"),
    ("admin", "Admin"),
    ("dsl", "Availability DSL"),
]

def _help_embed(topic_key: str) -> discord.Embed:
    """Return a compact, actionable help embed for the given topic."""
    title_map = {k: v for k, v in _HELP_TOPICS}
    title = title_map.get(topic_key, "Overview")

    if topic_key == "overview":
        desc = (
            "Welcome to **Mattinees** — organize a movie night in Discord.\n\n"
            "• **Catalog:** Browse/search the movie list, view info, nominate titles.\n"
            "• **Movies:** Open a ≤3-option ballot, vote/unvote, view status.\n"
            "• **Availability:** Use a guided wizard or compact text to set your times; render a chart.\n"
            "• **Schedule:** Suggest ≤3 time slots, vote/unvote, finalize with ISO-8601 start/end.\n"
            "• **Seats:** Reserve one of the guest seats (host excluded), or waitlist.\n"
            "• **Admin:** Backups, restore/undo, and catalog maintenance.\n\n"
            "Use the selector below to jump to a topic, or try Quick Actions."
        )
    elif topic_key == "catalog":
        desc = (
            "**Catalog** commands:\n"
            "• `/catalog list` — Browse with filters (genre, year, runtime, certification).\n"
            "• `/catalog search` — Search by title/genre.\n"
            "• `/catalog info` — Detailed info for a title or TMDb ID.\n"
            "• *(Admin)* `/catalog add|remove|clear` — Manage entries.\n\n"
            "In the **Catalog Browser**:\n"
            "• Use the dropdown to pick a title on the current page.\n"
            "• **Info** shows poster, runtime, rating & descriptors, genres, synopsis, trailer.\n"
            "• **Nominate / Retract** toggles your nomination for the selected title.\n"
            "• **Prev / Next** pages through the catalog.\n"
            "Tip: sessions expire after a short time; just reopen if needed."
        )
    elif topic_key == "movies":
        desc = (
            "**Movies** (nominations & voting):\n"
            "• `/movies my_nominations` — View/retract your nominations (panel).\n"
            "• `/movies nominees_panel` *(Admin)* — Post a public summary with CTAs.\n"
            "• `/movies nominate|retract_nomination` — Power users (by TMDb ID).\n"
            "• `/movies open_vote` *(Admin)* — Start a ≤3-option ballot (top nominees or manual IDs).\n"
            "• `/movies vote` — Open the ballot ephemerally; choose your movie.\n"
            "• `/movies unvote` — Clear your movie vote.\n"
            "• `/movies status` — See current options and vote counts.\n"
            "• `/movies close_vote` *(Admin)* — Close ballot and clear votes.\n"
            "• `/movies clear_votes` *(Admin)* — Clear all movie votes."
        )
    elif topic_key == "availability":
        desc = (
            "**Availability** (your schedule):\n"
            "• `/availability wizard` — 3 steps: Confirm Days → Set Time → Add Blocks/Presets → Review & Save.\n"
            "• `/availability set|append` — Use the DSL (see *Availability DSL* topic).\n"
            "• `/availability add_block|add_multi|remove_block|clear` — CRUD tools.\n"
            "• `/availability list|export` — Show or export your availability.\n"
            "• `/availability view` — Render a swimlanes chart (optionally filter by day).\n\n"
            "Tips:\n"
            "• Time format is 24h HH:MM with minutes 00/15/30/45.\n"
            "• \"Add Multi\" applies one block across multiple days (e.g., `Mon-Thu, Sat`)."
        )
    elif topic_key == "schedule":
        desc = (
            "**Schedule** (pick a time together):\n"
            "• `/schedule suggest` *(Admin)* — Propose ≤3 time slots based on overlaps.\n"
            "• `/schedule vote` — Open the time ballot ephemerally; choose your slot.\n"
            "• `/schedule status` — See options and vote counts.\n"
            "• `/schedule unvote` — Clear your time vote.\n"
            "• `/schedule close_vote` *(Admin)* — Close ballot and clear time votes.\n"
            "• `/schedule clear_votes` *(Admin)* — Clear all time votes.\n"
            "• `/schedule choose` *(Admin)* — Finalize a slot; output ISO-8601 start/end using the leading movie runtime."
        )
    elif topic_key == "seats":
        desc = (
            "**Seats** (3 guests + host):\n"
            "• `/seats book` — Reserve your seat; if full, you go to the waitlist.\n"
            "• `/seats release` (or `/seats unbook`) — Free your seat; next waitlisted user is promoted.\n"
            "• `/seats status` — See assignments and waitlist.\n"
            "• `/seats clear` *(Admin)* — Clear all seats and waitlist.\n"
            "• `/seats swap` *(Admin)* — Swap seats between two users."
        )
    elif topic_key == "admin":
        desc = (
            "**Admin** tools:\n"
            "• `/admin backup` — Save a timestamped backup.\n"
            "• `/admin backups` — List backups.\n"
            "• `/admin undo` — Restore the most recent backup.\n"
            "• `/admin restore` — Restore by file name.\n"
            "• Catalog management: `/catalog add|remove|clear`.\n"
            "• Ballots: `/movies open_vote|close_vote|clear_votes` and `/schedule suggest|close_vote|clear_votes`."
        )
    elif topic_key == "dsl":
        desc = (
            "**Availability DSL** (quick text format):\n"
            "• **Days:** `Mon, Tue, Wed, Thu, Fri, Sat, Sun` (full names also ok). Ranges/lists: `Mon-Thu, Sat`.\n"
            "• **Time:** 24h `HH:MM` with minutes `00/15/30/45`.\n"
            "• **Blocks:** `Day HH:MM-HH:MM`, comma-separated.\n\n"
            "**Examples**\n"
            "• `Mon 17:00-23:00, Tue-Thu 18:00-22:00, Fri 16:00-23:00`\n"
            "• `Sat 12:00-18:00, Sun 18:00-23:00`\n"
            "• `Mon-Thu 19:00-22:00, Sat 14:00-18:00`\n\n"
            "Use `/availability set` to replace, or `/availability append` to add."
        )
    else:
        desc = "Use the selector below to choose a help topic."

    embed = discord.Embed(title=f"Help — {title}", description=desc)
    embed.set_footer(text="Quick Actions below are safe and ephemeral (where applicable).")
    return embed


class HelpTopicSelect(discord.ui.Select):
    def __init__(self, current_key: str):
        opts = [
            discord.SelectOption(
                label=label,
                value=key,
                default=(key == current_key)
            )
            for key, label in _HELP_TOPICS
        ]
        super().__init__(placeholder="Choose a help topic…", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        view: "HelpView" = self.view  # type: ignore
        key = self.values[0]
        await view.rebuild_and_edit(interaction, key)


class HelpView(discord.ui.View):
    """Interactive help: topic selector + context-aware quick actions."""
    def __init__(self, owner_id: int, topic_key: str = "overview"):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.topic_key = topic_key
        self._build_components()

    def _build_components(self):
        self.clear_items()
        # Row 0 — topic selector
        self.add_item(HelpTopicSelect(self.topic_key))

        # Row 1+ — quick action buttons (context-aware; only non-admin, safe actions)
        if self.topic_key in ("overview", "catalog", "movies"):
            self.add_item(HelpOpenCatalogButton())
        if self.topic_key in ("overview", "availability", "dsl"):
            self.add_item(HelpStartWizardButton())
        if self.topic_key in ("overview", "movies"):
            self.add_item(HelpOpenMovieVoteButton())
            self.add_item(HelpMyNominationsButton())
        if self.topic_key in ("overview", "schedule"):
            self.add_item(HelpOpenTimeVoteButton())
        if self.topic_key in ("overview", "seats"):
            self.add_item(HelpSeatsStatusButton())
            self.add_item(HelpSeatsBookButton())
            self.add_item(HelpSeatsReleaseButton())

        # Row last — close/dismiss
        self.add_item(HelpCloseButton())

    async def rebuild_and_edit(self, interaction: discord.Interaction, new_key: str):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This help session belongs to someone else. Run `/help` to open yours.", ephemeral=True)
            return
        self.topic_key = new_key
        self._build_components()
        embed = _help_embed(self.topic_key)
        if interaction.response.is_done():
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)  # type: ignore
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        # Disable controls on timeout
        for c in self.children:
            c.disabled = True

# Quick action buttons — use safe, ephemeral flows only (no admin actions here)

class HelpOpenCatalogButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Open Catalog Browser", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        # Ephemeral catalog list with default filters
        ids = sorted(list(state.movies.keys()), key=lambda x: state.movies[x].title.lower())
        if not ids:
            await interaction.response.send_message("The catalog is empty. Ask an admin to add titles with `/catalog add`.", ephemeral=True)
            return
        session = CatalogSession(interaction.user.id, ids, 10, "Browse all")
        view = CatalogView(session)
        embed = _catalog_embed(session.page, session.num_pages(), len(ids), None, session.label)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class HelpStartWizardButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Start Availability Wizard", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        # Fresh wizard draft for the user
        WIZARD_DRAFTS.pop(interaction.user.id, None)
        draft = get_draft(interaction.user.id)
        view = AvailabilityWizardView(user_id=interaction.user.id)
        await view.build(1)
        await interaction.response.send_message(
            "Step 1: Select day(s) and click **Confirm Days**.\n"
            "Step 2: **Set Time…**, then **Add Block for Selected Days** (or use **Preset** buttons).\n"
            "Step 3: **Review Draft** → Save (Replace/Append) or Remove Blocks / Clear / Cancel.\n\n" +
            render_draft(draft),
            view=view,
            ephemeral=True
        )

class HelpOpenMovieVoteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Open Movie Vote", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        if not state.movie_options:
            await interaction.response.send_message("No active movie ballot. Ask an admin to `/movies open_vote`.", ephemeral=True)
            return
        view = MovieVoteView([state.movies[mid] for mid in state.movie_options if mid in state.movies])
        await interaction.response.send_message("Vote for a movie:", view=view, ephemeral=True)

class HelpOpenTimeVoteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Open Time Vote", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        if not state.time_options:
            await interaction.response.send_message("No active time-slot vote. Ask an admin to `/schedule suggest`.", ephemeral=True)
            return
        view = TimeVoteView(state.time_options)
        await interaction.response.send_message("Vote for a time slot:", view=view, ephemeral=True)

class HelpMyNominationsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="My Nominations", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        # Reuse existing command for consistency
        await movies_my_nominations(interaction)

class HelpSeatsStatusButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Seats Status", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await seats_status(interaction)

class HelpSeatsBookButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Book Seat", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        await seats_book(interaction)

class HelpSeatsReleaseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Release Seat", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        await seats_release(interaction)

class HelpCloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Help closed.", embed=None, view=None)


# Slash command: /help with optional topic choice
@help_group.command(name="help", description="How to use the movie night bot (interactive).")
@app_commands.describe(topic="Optional: jump directly to a topic")
@app_commands.choices(topic=[
    app_commands.Choice(name=label, value=key) for key, label in _HELP_TOPICS
])
async def help_command(interaction: discord.Interaction, topic: Optional[app_commands.Choice[str]] = None):
    key = topic.value if topic else "overview"
    view = HelpView(interaction.user.id, key)
    embed = _help_embed(key)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ===== Register groups =====

def _register_groups():
    for grp in [catalog_group, movies_group, schedule_group, avail_group, seats_group, admin_group, help_group]:
        if DEV_GUILD_ID:
            bot.tree.add_command(grp, guild=discord.Object(id=DEV_GUILD_ID))
        else:
            bot.tree.add_command(grp)

# -------------------- Events --------------------

@bot.event
async def on_ready():
    global state, autosave_task
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    state = await load_state()
    if autosave_task and not autosave_task.done():
        autosave_task.cancel()
        try:
            await autosave_task
        except Exception:
            pass
    autosave_task = bot.loop.create_task(autosave_loop(state))
    register_persistent_views()
    _register_groups()
    if DEV_GUILD_ID:
        await bot.tree.sync(guild=discord.Object(id=DEV_GUILD_ID))
        log.info("Slash commands synced to guild %s.", DEV_GUILD_ID)
    else:
        await bot.tree.sync()
        log.info("Slash commands globally synced.")

# Cleanup of TMDb session on disconnect
@bot.event
async def on_disconnect():
    try:
        await close_session()
    except Exception:
        pass


# -------------------- Entrypoint --------------------

def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
