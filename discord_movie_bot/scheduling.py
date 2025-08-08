"""Availability parsing and schedule computation (with merging & ISO helpers)."""

from __future__ import annotations
import datetime as dt
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .models import DayTimeRange, ScheduleSlot, TicketHolder

DAY_ALIASES = {
    "mon": "Monday", "monday": "Monday",
    "tue": "Tuesday", "tues": "Tuesday", "tuesday": "Tuesday",
    "wed": "Wednesday", "wednesday": "Wednesday",
    "thu": "Thursday", "thur": "Thursday", "thurs": "Thursday", "thursday": "Thursday",
    "fri": "Friday", "friday": "Friday",
    "sat": "Saturday", "saturday": "Saturday",
    "sun": "Sunday", "sunday": "Sunday",
}
WEEKDAY_ORDER = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
DAY_INDEX = {d:i for i,d in enumerate(WEEKDAY_ORDER)}

def _canonical_day(name: str) -> str:
    key = name.strip().lower()
    if key in DAY_ALIASES: return DAY_ALIASES[key]
    raise ValueError(f"Unrecognised day name: {name}")

def _expand_day_range(part: str) -> List[str]:
    part = part.strip()
    if "-" in part:
        a, b = [x.strip() for x in part.split("-", 1)]
        sa, sb = _canonical_day(a), _canonical_day(b)
        ia, ib = DAY_INDEX[sa], DAY_INDEX[sb]
        if ia <= ib: return WEEKDAY_ORDER[ia:ib+1]
        return WEEKDAY_ORDER[ia:] + WEEKDAY_ORDER[:ib+1]
    return [_canonical_day(part)]

def _time_to_minutes(t: str) -> int:
    h, m = [int(x) for x in t.split(":")]
    return h*60 + m

def _minutes_to_time(n: int) -> str:
    return f"{(n//60):02d}:{(n%60):02d}"

def parse_availability_string(avail_str: str) -> List[DayTimeRange]:
    if not avail_str: return []
    ranges: List[DayTimeRange] = []
    for block in avail_str.split(","):
        block = block.strip()
        if not block: continue
        try:
            day_part, time_part = block.split(" ", 1)
        except ValueError:
            raise ValueError(f"Invalid availability block: '{block}'")
        days = _expand_day_range(day_part)
        if "-" not in time_part:
            raise ValueError(f"Invalid time range: '{time_part}'")
        start, end = [x.strip() for x in time_part.split("-", 1)]
        # basic validation
        dt.datetime.strptime(start, "%H:%M")
        dt.datetime.strptime(end, "%H:%M")
        if start >= end:
            raise ValueError(f"Start must be before end: '{start}-{end}'")
        for d in days:
            ranges.append(DayTimeRange(day=d, start_time=start, end_time=end))
    return ranges

def compute_common_overlaps(ticket_holders: Dict[int, TicketHolder]) -> List[ScheduleSlot]:
    participants = [uid for uid, th in ticket_holders.items() if th.availability]
    if not participants:
        return []
    day_map: Dict[str, List[tuple[int, DayTimeRange]]] = {}
    for uid, th in ticket_holders.items():
        for r in th.availability:
            day_map.setdefault(r.day, []).append((uid, r))
    result: List[ScheduleSlot] = []
    for day, entries in day_map.items():
        if set(u for u,_ in entries) != set(participants):
            continue
        events: List[tuple[int,int]] = []
        for _, r in entries:
            events.append((_time_to_minutes(r.start_time), +1))
            events.append((_time_to_minutes(r.end_time), -1))
        events.sort()
        count = 0
        start: Optional[int] = None
        for t, delta in events:
            count += delta
            if count == len(participants) and start is None:
                start = t
            elif count < len(participants) and start is not None:
                result.append(ScheduleSlot(day, _minutes_to_time(start), _minutes_to_time(t), participants.copy()))
                start = None
    result.sort(key=lambda s: (DAY_INDEX.get(s.day, 7), s.start_time))
    return result

def compute_popular_slots(ticket_holders: Dict[int, TicketHolder]) -> List[ScheduleSlot]:
    day_to_user_ranges: Dict[str, Dict[int, List[DayTimeRange]]] = {}
    for uid, th in ticket_holders.items():
        for r in th.availability:
            day_to_user_ranges.setdefault(r.day, {}).setdefault(uid, []).append(r)
    slots: List[ScheduleSlot] = []
    for day, user_ranges in day_to_user_ranges.items():
        events: List[tuple[int,str,int]] = []
        for uid, ranges in user_ranges.items():
            for r in ranges:
                events.append((_time_to_minutes(r.start_time), "start", uid))
                events.append((_time_to_minutes(r.end_time), "end", uid))
        events.sort(key=lambda x: (x[0], 0 if x[1]=="start" else 1))
        active: set[int] = set()
        prev: Optional[int] = None
        for t, typ, uid in events:
            if prev is not None and t > prev and active:
                slots.append(ScheduleSlot(day, _minutes_to_time(prev), _minutes_to_time(t), sorted(active)))
            if typ == "start": active.add(uid)
            else: active.discard(uid)
            prev = t
    # merge adjacent identical windows
    slots.sort(key=lambda s: (DAY_INDEX.get(s.day, 7), s.start_time))
    merged: List[ScheduleSlot] = []
    for s in slots:
        if merged and merged[-1].day == s.day and merged[-1].participants == s.participants and merged[-1].end_time == s.start_time:
            merged[-1].end_time = s.end_time
        else:
            merged.append(s)
    # popularity sort
    def duration(s: ScheduleSlot) -> int:
        return _time_to_minutes(s.end_time) - _time_to_minutes(s.start_time)
    merged.sort(key=lambda s: (len(s.participants), duration(s)), reverse=True)
    return merged

def next_date_for_weekday(tz: ZoneInfo, weekday_name: str) -> dt.date:
    now = dt.datetime.now(tz).date()
    target = DAY_INDEX[weekday_name]
    today_idx = dt.datetime.now(tz).weekday()
    days_ahead = (target - today_idx) % 7 or 7
    return now + dt.timedelta(days=days_ahead)

def slot_to_iso_start_end(tz: ZoneInfo, slot: ScheduleSlot, runtime_minutes: int) -> tuple[str, str]:
    date = next_date_for_weekday(tz, slot.day)
    h, m = [int(x) for x in slot.start_time.split(":")]
    start_dt = dt.datetime(date.year, date.month, date.day, h, m, tzinfo=tz)
    end_dt = start_dt + dt.timedelta(minutes=runtime_minutes)
    return start_dt.isoformat(), end_dt.isoformat()
