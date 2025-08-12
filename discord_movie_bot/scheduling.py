from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo

from .models import DayTimeRange, ScheduleSlot, TicketHolder

WEEKDAY_ORDER = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
DAY_INDEX = {d:i for i,d in enumerate(WEEKDAY_ORDER)}

# ---------- Time parsing / validation ----------

def is_valid_hhmm(s: str) -> bool:
    try:
        hh, mm = s.split(":")
        h = int(hh); m = int(mm)
        return 0 <= h <= 23 and 0 <= m <= 59 and len(hh) == 2 and len(mm) == 2
    except Exception:
        return False

def is_quarter_minute(s: str) -> bool:
    try:
        mm = int(s.split(":")[1])
        return mm in (0, 15, 30, 45)
    except Exception:
        return False

def canonical_day(s: str) -> str:
    s = s.strip().lower()
    mapping = {
        "mon":"Monday","monday":"Monday",
        "tue":"Tuesday","tues":"Tuesday","tuesday":"Tuesday",
        "wed":"Wednesday","weds":"Wednesday","wednesday":"Wednesday",
        "thu":"Thursday","thur":"Thursday","thurs":"Thursday","thursday":"Thursday",
        "fri":"Friday","friday":"Friday",
        "sat":"Saturday","saturday":"Saturday",
        "sun":"Sunday","sunday":"Sunday",
    }
    if s not in mapping:
        raise ValueError("Invalid day. Use full name or Mon/Tue/…")
    return mapping[s]

def day_expr_to_list(expr: str) -> List[str]:
    # e.g., "Mon-Thu, Sat"
    parts = [p.strip() for p in expr.split(",") if p.strip()]
    out: List[str] = []
    abbr_to_full = {
        "Mon":"Monday", "Tue":"Tuesday", "Wed":"Wednesday", "Thu":"Thursday", "Fri":"Friday", "Sat":"Saturday", "Sun":"Sunday"
    }
    def norm_day(tok: str) -> str:
        tok = tok.strip()
        if len(tok) == 3:
            tok = tok.title()
            if tok in abbr_to_full: return abbr_to_full[tok]
        return canonical_day(tok)
    for p in parts:
        if "-" in p:
            a, b = p.split("-", 1)
            a = norm_day(a); b = norm_day(b)
            ai = DAY_INDEX.get(a, -1); bi = DAY_INDEX.get(b, -1)
            if ai == -1 or bi == -1:
                continue
            if ai <= bi:
                out.extend(WEEKDAY_ORDER[ai:bi+1])
            else:
                # wrap
                out.extend(WEEKDAY_ORDER[ai:] + WEEKDAY_ORDER[:bi+1])
        else:
            out.append(norm_day(p))
    # dedupe in weekday order
    seen = set()
    result = []
    for d in WEEKDAY_ORDER:
        if d in out and d not in seen:
            seen.add(d); result.append(d)
    return result

def parse_availability_string(s: str) -> List[DayTimeRange]:
    """Mon 17:00-23:00, Tue-Thu 18:00-22:00"""
    blocks: List[DayTimeRange] = []
    parts = [p.strip() for p in s.split(",") if p.strip()]
    for part in parts:
        # Expect "<days> <start>-<end>"
        if " " not in part or "-" not in part:
            raise ValueError(f"Bad block: '{part}'")
        days_str, times_str = part.split(" ", 1)
        start, end = [t.strip() for t in times_str.split("-", 1)]
        if not (is_valid_hhmm(start) and is_valid_hhmm(end) and is_quarter_minute(start) and is_quarter_minute(end)):
            raise ValueError(f"Invalid time(s) in '{part}'")
        if start >= end:
            raise ValueError(f"Start must be before end in '{part}'")
        days = day_expr_to_list(days_str)
        if not days:
            raise ValueError(f"No valid days in '{part}'")
        for d in days:
            blocks.append(DayTimeRange(day=d, start_time=start, end_time=end))
    return dedupe_blocks(blocks)

def merge_blocks(existing: List[DayTimeRange], adds: List[DayTimeRange]) -> List[DayTimeRange]:
    """Merge and coalesce overlapping blocks for same day."""
    merged = existing + adds
    # by day then by start
    merged.sort(key=lambda r: (DAY_INDEX.get(r.day, 7), r.start_time, r.end_time))
    out: List[DayTimeRange] = []
    for r in merged:
        if not out:
            out.append(r); continue
        last = out[-1]
        if last.day == r.day and last.end_time >= r.start_time:  # overlap or touch
            # coalesce
            out[-1] = DayTimeRange(day=last.day, start_time=min(last.start_time, r.start_time), end_time=max(last.end_time, r.end_time))
        else:
            out.append(r)
    return out

def dedupe_blocks(ranges: List[DayTimeRange]) -> List[DayTimeRange]:
    return merge_blocks([], ranges)  # merge also dedupes by coalescing

def remove_block(ranges: List[DayTimeRange], triple: Tuple[str,str,str]) -> List[DayTimeRange]:
    d, s, e = triple
    return [r for r in ranges if not (r.day == d and r.start_time == s and r.end_time == e)]

def remove_day(ranges: List[DayTimeRange], day: str) -> List[DayTimeRange]:
    return [r for r in ranges if r.day != day]

def blocks_to_dsl(ranges: List[DayTimeRange]) -> str:
    if not ranges:
        return ""
    # group by day in order
    lines: List[str] = []
    for d in WEEKDAY_ORDER:
        slots = [(r.start_time, r.end_time) for r in ranges if r.day == d]
        if not slots: continue
        parts = [f"{s}-{e}" for s, e in slots]
        lines.append(f"{d} " + ", ".join(parts))
    return "\n".join(lines)

# ---------- Scheduling logic ----------

def _slot_key(day: str, start: str, end: str) -> str:
    return f"slot:{day}|{start}|{end}"

def _iter_user_ranges(ticket_holders: Dict[int, TicketHolder]) -> Iterable[Tuple[int, DayTimeRange]]:
    for uid, th in ticket_holders.items():
        for r in th.availability:
            yield uid, r

def _intersections_for_day(day: str, entries: List[Tuple[int, DayTimeRange]]) -> List[ScheduleSlot]:
    """Compute naive overlaps: here we propose 3 'best' windows by max participants and length."""
    # Build timeline of quarter hours: 00..23:45 (96 ticks)
    def to_idx(hhmm: str) -> int:
        h, m = map(int, hhmm.split(":"))
        return h*4 + (m//15)
    def to_hhmm(idx: int) -> str:
        h = idx // 4
        m = (idx % 4) * 15
        return f"{h:02d}:{m:02d}"

    # availability grid per user: 96-length boolean
    users = {}
    for uid, r in entries:
        if r.day != day: continue
        grid = users.setdefault(uid, [False]*96)
        a = to_idx(r.start_time); b = to_idx(r.end_time)
        for i in range(a, b):
            grid[i] = True

    if not users:
        return []

    # count participants across users for each tick
    counts = [0]*96
    for grid in users.values():
        for i, ok in enumerate(grid):
            if ok: counts[i] += 1

    # find contiguous regions of at least one participant; then rank by (max participants, length)
    slots: List[ScheduleSlot] = []
    i = 0
    while i < 96:
        if counts[i] == 0:
            i += 1; continue
        start = i
        max_p = 0
        while i < 96 and counts[i] > 0:
            max_p = max(max_p, counts[i])
            i += 1
        end = i
        # use representative participants: here we conservatively skip participant list fill (optional)
        start_hh = to_hhmm(start); end_hh = to_hhmm(end)
        slot = ScheduleSlot(day=day, start_time=start_hh, end_time=end_hh, participants=[], key=_slot_key(day, start_hh, end_hh))
        slots.append(slot)

    # rank: by max participation then by duration
    def score(slot: ScheduleSlot) -> Tuple[int, int]:
        a = to_idx(slot.start_time); b = to_idx(slot.end_time)
        duration = b - a
        # approximate max participants in this region:
        max_p = max(counts[a:b]) if a < b else 0
        return (max_p, duration)

    slots.sort(key=score, reverse=True)
    return slots

def compute_common_overlaps(ticket_holders: Dict[int, TicketHolder]) -> List[ScheduleSlot]:
    """Return up to many candidate slots in best-first order."""
    day_entries = list(_iter_user_ranges(ticket_holders))
    out: List[ScheduleSlot] = []
    for day in WEEKDAY_ORDER:
        out.extend(_intersections_for_day(day, day_entries))
    return out

def compute_popular_slots(ticket_holders: Dict[int, TicketHolder]) -> List[ScheduleSlot]:
    """As a fallback, propose the 'most popular' fixed windows (rough heuristic)."""
    # For simplicity, reuse overlaps; callers will merge/dedupe.
    return compute_common_overlaps(ticket_holders)

def slot_to_iso_start_end(tz: ZoneInfo, slot: ScheduleSlot, runtime_minutes: int) -> Tuple[str, str]:
    """Turn (weekday + clock) into next occurrence in the future (starting this week), with runtime."""
    # Find the next date for slot.day
    today = datetime.now(tz).date()
    target_idx = DAY_INDEX[slot.day]
    today_idx = today.weekday()  # Monday=0..Sunday=6
    delta_days = (target_idx - today_idx) % 7
    date = today + timedelta(days=delta_days)
    start_dt = datetime.combine(date, datetime.strptime(slot.start_time, "%H:%M").time(), tzinfo=tz)
    end_dt = start_dt + timedelta(minutes=runtime_minutes)
    return start_dt.isoformat(), end_dt.isoformat()
