"""Slash-command Discord bot for movie night
(CRUD everywhere + backups/restore + fixed availability wizard + hardening + better error handling)."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from zoneinfo import ZoneInfo

from .config import DISCORD_TOKEN, DEV_GUILD_ID, MAX_SEATS, TIMEZONE
from .models import Movie, ScheduleSlot, TicketHolder, State, MovieRequest, DayTimeRange
from .storage import (
    load_state, save_state, autosave_loop,
    backup_state, list_backups, restore_state_from_backup,
)
from .scheduling import (
    parse_availability_string,
    compute_common_overlaps,
    compute_popular_slots,
    slot_to_iso_start_end,
    WEEKDAY_ORDER,
    DAY_INDEX,
    canonical_day,
    day_expr_to_list,
    is_valid_hhmm,
    is_quarter_minute,
    dedupe_blocks,
    merge_blocks,
    remove_block as remove_block_from_list,
    remove_day as remove_day_from_list,
    blocks_to_dsl,
)
from .availability_chart import create_availability_chart
from .tmdb_api import build_movie_from_title, build_movie_from_tmdb_id, close_session

# -------------------- Logging --------------------

log = logging.getLogger("discord_movie_bot")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s")

# -------------------- Bot setup --------------------

intents = discord.Intents.default()  # message content not needed
bot = commands.Bot(command_prefix="!", intents=intents)  # prefix unused; we rely on slash commands
state: State
autosave_task: Optional[asyncio.Task] = None  # restarted after restore/undo

# -------------------- Global error handler --------------------

from discord.app_commands import AppCommandError, CheckFailure  # noqa: E402

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: AppCommandError):
    """Friendly error surface for slash commands."""
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
    except Exception:  # last-resort logger only
        log.exception("Unhandled error while reporting command failure")

# -------------------- Helpers --------------------

def get_or_create_holder(user: discord.User | discord.Member) -> TicketHolder:
    th = state.ticket_holders.get(user.id)
    if not th:
        th = TicketHolder(user_id=user.id, user_name=user.display_name)
        state.ticket_holders[user.id] = th
    else:
        # keep latest display name
        th.user_name = getattr(user, "display_name", th.user_name) or th.user_name
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
        super().__init__(timeout=None)  # persistent view survives restarts
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
        if mid not in state.movies:
            await interaction.response.send_message("That title is no longer available.", ephemeral=True)
            return
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
        super().__init__(timeout=120)  # ephemeral view
        self.session = session
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        self.add_item(CatalogPrevButton())
        self.add_item(CatalogNextButton())
        options = []
        for mid in self.session.page_slice():
            m = state.movies.get(mid)
            if not m:
                continue
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
        if m.trailer_url:
            embed.add_field(name="Trailer", value=m.trailer_url, inline=False)
        if m.poster_url:
            embed.set_thumbnail(url=m.poster_url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class CatalogNominateSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Nominate: choose a movie", options=options, min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        mid = int(self.values[0])
        if mid not in state.movies:
            await interaction.response.send_message("That title is no longer in the catalog.", ephemeral=True)
            return
        uniq_append_nomination(mid, interaction.user.id)
        await save_state(state)
        await interaction.response.send_message(f"✅ Nominated **{state.movies[mid].title}**", ephemeral=True)

# -------------------- Availability (CRUD + FIXED WIZARD) --------------------

avail_group = app_commands.Group(name="availability", description="Provide and view availability")

# ---- CRUD ----

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

# Back-compat alias
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
    await interaction.response.send_message(file=discord.File(path), ephemeral=True)

# ---- FIXED WIZARD (multi-step; never more than 5 rows) ----

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
        # remove draft and close
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
        # remove drafts entirely
        WIZARD_DRAFTS.pop(view.user_id, None)
        await view.build(1)
        await interaction.response.edit_message(content="Draft cleared. Start again by selecting days.\n\n" + render_draft(get_draft(view.user_id)), view=view)

class SaveReplaceButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save (Replace)", style=discord.ButtonStyle.primary)
    async def callback(self, interaction: discord.Interaction):
        th = get_or_create_holder(interaction.user)
        draft = get_draft(interaction.user.id)
        new_ranges = [DayTimeRange(day=d, start_time=s, end_time=e) for d,s,e in draft.blocks]
        th.availability = dedupe_blocks(new_ranges)
        await save_state(state)
        # remove draft entry
        WIZARD_DRAFTS.pop(interaction.user.id, None)
        await interaction.response.send_message("✅ Availability saved (replaced).", ephemeral=True)

class SaveAppendButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save (Append)", style=discord.ButtonStyle.success)
    async def callback(self, interaction: discord.Interaction):
        th = get_or_create_holder(interaction.user)
        draft = get_draft(interaction.user.id)
        additions = [DayTimeRange(day=d, start_time=s, end_time=e) for d,s,e in draft.blocks]
        th.availability = merge_blocks(th.availability, additions)
        await save_state(state)
        # remove draft entry
        WIZARD_DRAFTS.pop(interaction.user.id, None)
        await interaction.response.send_message("✅ Availability saved (appended).", ephemeral=True)

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
        # Wipe all children and rebuild for the given step
        for child in list(self.children):
            self.remove_item(child)

        if step == 1:
            # Row 1: day selector
            self.add_item(DayMultiSelect())
            # Row 2: buttons
            self.add_item(ConfirmDaysButton())
            self.add_item(CancelWizardButton())

        elif step == 2:
            # Buttons only; times via modal
            self.add_item(SetTimeButton())                # row A
            self.add_item(AddBlockButton())               # row A
            # Preset buttons (up to 4 to keep rows low)
            preset_names = list(PRESETS.keys())
            if preset_names:
                self.add_item(PresetButton(preset_names[0]))
            if len(preset_names) > 1:
                self.add_item(PresetButton(preset_names[1]))
            if len(preset_names) > 2:
                self.add_item(PresetButton(preset_names[2]))
            if len(preset_names) > 3:
                self.add_item(PresetButton(preset_names[3]))
            # Nav / review row
            self.add_item(ChangeDaysButton())
            self.add_item(ReviewButton())
            self.add_item(ClearDraftButton())
            self.add_item(CancelWizardButton())

        elif step == 3:
            # Build options from current draft for this view's user_id (cap to 25)
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
            # Save / back / cancel
            self.add_item(SaveReplaceButton())
            self.add_item(SaveAppendButton())
            self.add_item(BackToTimesButton())
            self.add_item(CancelWizardButton())

    async def on_timeout(self):
        # disable controls and remove draft to avoid leaks
        for child in self.children:
            child.disabled = True
        WIZARD_DRAFTS.pop(self.user_id, None)

@avail_group.command(name="wizard", description="Interactive availability setup: Confirm Days → Set Time → Add Blocks → Review & Save.")
@app_commands.describe(mode="Start fresh (new) or edit your current availability")
@app_commands.choices(mode=[app_commands.Choice(name="new", value="new"), app_commands.Choice(name="edit", value="edit")])
async def availability_wizard(interaction: discord.Interaction, mode: Optional[app_commands.Choice[str]] = None):
    user_id = interaction.user.id
    # reset the user's draft
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

bot.tree.add_command(avail_group, guild=discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else bot.tree.add_command(avail_group)

# -------------------- Catalog (with CRUD + export/import) --------------------

catalog_group = app_commands.Group(name="catalog", description="Manage and browse the movie catalog")

@catalog_group.command(name="add", description="Admin: Add titles or TMDb IDs (comma-separated) to the catalog.")
@app_commands.describe(titles_or_ids="E.g. 'The Matrix, 603, Interstellar'")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
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

@catalog_group.command(name="remove", description="Admin: Remove movies from the catalog by TMDb IDs (comma-separated).")
@app_commands.describe(ids="E.g. '603, 157336'")
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
    await interaction.followup.send(f"Removed {removed} movie(s). Votes referencing them were cleared.", ephemeral=True)

@catalog_group.command(name="clear", description="Admin: Clear the entire catalog (dangerous).")
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

@catalog_group.command(name="refresh", description="Admin: Refresh TMDb metadata for a movie (or list of IDs).")
@app_commands.describe(ids="TMDb ID or comma-separated list")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def catalog_refresh(interaction: discord.Interaction, ids: str):
    await interaction.response.defer(ephemeral=True)
    count = 0
    for part in [p.strip() for p in ids.split(",") if p.strip()]:
        try:
            mid = int(part)
        except ValueError:
            await interaction.followup.send(f"Invalid TMDb ID: `{part}`", ephemeral=True)
            continue
        m = await build_movie_from_tmdb_id(mid)
        if not m:
            await interaction.followup.send(f"TMDb fetch failed for `{mid}`", ephemeral=True)
            continue
        state.movies[mid] = m
        count += 1
    await save_state(state)
    await interaction.followup.send(f"Refreshed {count} movie(s).", ephemeral=True)

@catalog_group.command(name="export", description="Admin: Export the catalog as a text file (TMDb_ID,Title (Year)).")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def catalog_export(interaction: discord.Interaction):
    lines = []
    for m in sorted(state.movies.values(), key=lambda mm: (mm.title.lower(), mm.year)):
        lines.append(f"{m.tmdb_id},{m.title} ({m.year})")
    content = "\n".join(lines)
    if not content or len(content) < 1500:
        await interaction.response.send_message(f"```\n{content}\n```" if content else "(empty catalog)", ephemeral=True)
        return
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".txt") as tf:
        tf.write(content)
        tf.flush()
        path = tf.name
    try:
        await interaction.response.send_message(file=discord.File(path, filename="catalog.txt"), ephemeral=True)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

@catalog_group.command(name="import_file", description="Admin: import a text file (one title or TMDb ID per line).")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def catalog_import_file(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)
    text = (await file.read()).decode("utf-8", errors="ignore")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    added = 0
    for line in lines:
        try:
            if line.isdigit():
                m = await build_movie_from_tmdb_id(int(line))
            else:
                m = await build_movie_from_title(line)
            if m:
                state.movies[m.tmdb_id] = m
                added += 1
            else:
                await interaction.followup.send(f"Not found: `{line}`", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error for `{line}`: {e}", ephemeral=True)
    await save_state(state)
    await interaction.followup.send(f"Imported {added} movie(s).", ephemeral=True)

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
    # try ID first if numeric
    if query.strip().isdigit():
        m = await build_movie_from_tmdb_id(int(query.strip()))
        if not m:
            m = await build_movie_from_title(query)
    else:
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

# -------------------- Movies (nominations, ballot, voting, requests + CRUD) --------------------

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

@movies_group.command(name="retract_nomination", description="Retract your nomination for a movie.")
@app_commands.describe(id="TMDb ID")
async def movies_retract_nomination(interaction: discord.Interaction, id: int):
    uids = state.nominations.get(id)
    if not uids or interaction.user.id not in uids:
        await interaction.response.send_message("You haven't nominated that movie.", ephemeral=True)
        return
    state.nominations[id] = [u for u in uids if u != interaction.user.id]
    if not state.nominations[id]:
        del state.nominations[id]
    await save_state(state)
    await interaction.response.send_message("Nomination retracted.", ephemeral=True)

@movies_group.command(name="clear_nominations", description="Admin: clear all nominations.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def movies_clear_nominations(interaction: discord.Interaction):
    await backup_state(state)
    state.nominations.clear()
    await save_state(state)
    await interaction.response.send_message("All nominations cleared.", ephemeral=True)

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
@app_commands.guild_only()
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
    opts = [state.movies[mid] for mid in state.movie_options if mid in state.movies]
    view = MovieVoteView(opts)
    await interaction.response.send_message("Vote for a movie:", view=view, ephemeral=False)

@movies_group.command(name="clear_ballot", description="Admin: clear current ballot (movie options).")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def movies_clear_ballot(interaction: discord.Interaction):
    await backup_state(state)
    state.movie_options.clear()
    await save_state(state)
    await interaction.response.send_message("Ballot cleared.", ephemeral=True)

@movies_group.command(name="unvote", description="Remove your current movie vote.")
async def movies_unvote(interaction: discord.Interaction):
    th = get_or_create_holder(interaction.user)
    if th.movie_vote is None:
        await interaction.response.send_message("You haven't voted yet.", ephemeral=True)
        return
    th.movie_vote = None
    await save_state(state)
    await interaction.response.send_message("Your movie vote was cleared.", ephemeral=True)

@movies_group.command(name="clear_votes", description="Admin: clear all users' movie votes.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def movies_clear_votes(interaction: discord.Interaction):
    await backup_state(state)
    for th in state.ticket_holders.values():
        th.movie_vote = None
    await save_state(state)
    await interaction.response.send_message("All movie votes cleared.", ephemeral=True)

# Requests CRUD
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

@movies_group.command(name="cancel_request", description="Cancel your own pending request.")
@app_commands.describe(request_id="Your request ID")
async def movies_cancel_request(interaction: discord.Interaction, request_id: int):
    req = state.movie_requests.get(request_id)
    if not req or req.user_id != interaction.user.id:
        await interaction.response.send_message("Request not found or not yours.", ephemeral=True)
        return
    if req.status != "pending":
        await interaction.response.send_message("Only pending requests can be cancelled.", ephemeral=True)
        return
    del state.movie_requests[request_id]
    await save_state(state)
    await interaction.response.send_message("Your request was cancelled.", ephemeral=True)

@movies_group.command(name="requests", description="Admin: review movie requests.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def movies_requests(interaction: discord.Interaction):
    if not state.movie_requests:
        await interaction.response.send_message("No requests.", ephemeral=True)
        return
    lines = []
    for rid, req in sorted(state.movie_requests.items()):
        lines.append(f"#{rid} — {req.user_name}: `{req.query}` [{req.status}]")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

@movies_group.command(name="deny_request", description="Admin: deny a request.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(request_id="Request ID to deny")
async def movies_deny_request(interaction: discord.Interaction, request_id: int):
    req = state.movie_requests.get(request_id)
    if not req:
        await interaction.response.send_message("No such request.", ephemeral=True)
        return
    if req.status != "pending":
        await interaction.response.send_message("Request is not pending.", ephemeral=True)
        return
    req.status = "denied"
    await save_state(state)
    await interaction.response.send_message("Request denied.", ephemeral=True)

@movies_group.command(name="delete_request", description="Admin: delete a request (any status).")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(request_id="Request ID to delete")
async def movies_delete_request(interaction: discord.Interaction, request_id: int):
    if request_id not in state.movie_requests:
        await interaction.response.send_message("No such request.", ephemeral=True)
        return
    del state.movie_requests[request_id]
    await save_state(state)
    await interaction.response.send_message("Request deleted.", ephemeral=True)

@movies_group.command(name="promote", description="Admin: approve a request and add it to the catalog.")
@app_commands.describe(request_id="Request ID to approve")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def movies_promote(interaction: discord.Interaction, request_id: int):
    req = state.movie_requests.get(request_id)
    if not req:
        await interaction.response.send_message("No such request.", ephemeral=True)
        return
    if req.status != "pending":
        await interaction.response.send_message("Request is not pending.", ephemeral=True)
        return
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

# -------------------- Schedule (time options + CRUD) --------------------

schedule_group = app_commands.Group(name="schedule", description="Suggest and choose time slots")

@schedule_group.command(name="suggest", description="Compute overlaps and suggest up to 3 time slots for voting.")
async def schedule_suggest(interaction: discord.Interaction):
    overlaps = compute_common_overlaps(state.ticket_holders)
    options = overlaps[:3]
    if len(options) < 3:
        popular = [s for s in compute_popular_slots(state.ticket_holders) if s not in options]
        for s in popular:
            if len(options) >= 3:
                break
            options.append(s)
    state.time_options = options
    ensure_time_options_size()
    await save_state(state)
    if not state.time_options:
        await interaction.response.send_message("No suitable time windows yet. Ask participants to add availability.", ephemeral=True)
        return
    view = TimeVoteView(state.time_options)
    await interaction.response.send_message("Vote for a time slot:", view=view, ephemeral=False)

@schedule_group.command(name="status", description="Show current time options and vote counts.")
async def schedule_status(interaction: discord.Interaction):
    if not state.time_options:
        await interaction.response.send_message("No active time options.", ephemeral=True)
        return
    tally = [0] * len(state.time_options)
    for th in state.ticket_holders.values():
        idx = th.time_vote
        if isinstance(idx, int) and 0 <= idx < len(tally):
            tally[idx] += 1
    lines = []
    for i, slot in enumerate(state.time_options):
        lines.append(f"{i}: {slot.day} {slot.start_time}-{slot.end_time} — {tally[i]} vote(s)")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@schedule_group.command(name="unvote", description="Remove your current time-slot vote.")
async def schedule_unvote(interaction: discord.Interaction):
    th = get_or_create_holder(interaction.user)
    if th.time_vote is None:
        await interaction.response.send_message("You haven't voted yet.", ephemeral=True)
        return
    th.time_vote = None
    await save_state(state)
    await interaction.response.send_message("Your time-slot vote was cleared.", ephemeral=True)

@schedule_group.command(name="clear_votes", description="Admin: clear all users' time-slot votes.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def schedule_clear_votes(interaction: discord.Interaction):
    await backup_state(state)
    for th in state.ticket_holders.values():
        th.time_vote = None
    await save_state(state)
    await interaction.response.send_message("All time-slot votes cleared.", ephemeral=True)

@schedule_group.command(name="clear_options", description="Admin: clear all suggested time options.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def schedule_clear_options(interaction: discord.Interaction):
    await backup_state(state)
    state.time_options.clear()
    for th in state.ticket_holders.values():
        th.time_vote = None
    await save_state(state)
    await interaction.response.send_message("Time options cleared (and votes reset).", ephemeral=True)

@schedule_group.command(name="choose", description="Finalize a time slot and show ISO-8601 start/end.")
@app_commands.describe(index="Index of the chosen time option (0, 1, or 2)")
@app_commands.guild_only()
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
        await interaction.response.send_message("No leading movie yet. Ask users to vote for a movie.", ephemeral=True)
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

# -------------------- Seats (with admin reset/swap) --------------------

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

bot.tree.add_command(seats_group, guild=discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else bot.tree.add_command(seats_group)

# -------------------- Admin: backups & restore (guild-only) --------------------

admin_group = app_commands.Group(name="admin", description="Backups and restore")

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

async def _restart_autosave(new_state: State):
    """Stop the existing autosave task (if any) and start a new one bound to new_state."""
    global autosave_task, state
    state = new_state
    if autosave_task and not autosave_task.done():
        autosave_task.cancel()
        try:
            await autosave_task
        except Exception:
            pass
    autosave_task = bot.loop.create_task(autosave_loop(state))

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
@app_commands.describe(name="Backup file name, e.g., state-20250807-221045.json")
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

bot.tree.add_command(admin_group, guild=discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else bot.tree.add_command(admin_group)

# -------------------- Events --------------------

@bot.event
async def on_ready():
    global state, autosave_task
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    state = await load_state()
    # (Re)start autosave bound to current state object
    if autosave_task and not autosave_task.done():
        autosave_task.cancel()
        try:
            await autosave_task
        except Exception:
            pass
    autosave_task = bot.loop.create_task(autosave_loop(state))
    register_persistent_views()
    if DEV_GUILD_ID:
        await bot.tree.sync(guild=discord.Object(id=DEV_GUILD_ID))
        log.info("Slash commands synced to guild %s.", DEV_GUILD_ID)
    else:
        await bot.tree.sync()
        log.info("Slash commands globally synced.")

# No on_close event (discord.py doesn't emit it). If you want a clean close:
# You can add a dev-only /admin shutdown that awaits close_session() and bot.close().

# -------------------- Entrypoint --------------------

def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
