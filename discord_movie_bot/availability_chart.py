"""Generate swimlane availability charts (portable temp path, per-day or all)."""

from __future__ import annotations
import tempfile
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .models import TicketHolder
from .scheduling import WEEKDAY_ORDER, compute_common_overlaps, _time_to_minutes


def create_availability_chart(ticket_holders: Dict[int, TicketHolder],
                              output_path: Optional[str] = None,
                              day: Optional[str] = None) -> str:
    """Render a chart (optionally for a single day). Returns path to PNG."""
    path = output_path or (tempfile.gettempdir() + "/availability_chart.png")

    # Gather days
    all_days = set()
    for th in ticket_holders.values():
        for r in th.availability:
            all_days.add(r.day)
    days = [d for d in WEEKDAY_ORDER if d in all_days]
    if day:
        days = [d for d in days if d == day]

    if not days:
        fig, ax = plt.subplots(figsize=(8,2))
        ax.text(0.5,0.5,"No availability data submitted.", ha="center", va="center", fontsize=14)
        ax.axis("off")
        fig.savefig(path)
        plt.close(fig)
        return path

    num_users = len(ticket_holders)
    row_labels: List[str] = []
    row_map: List[tuple[str, int]] = []
    for d in days:
        for uid, th in ticket_holders.items():
            row_labels.append(f"{d} – {th.user_name}")
            row_map.append((d, uid))
        row_labels.append(f"{d} – Common Overlap")
        row_map.append((d, -1))

    fig_height = max(2, len(row_labels) * 0.4)
    fig, ax = plt.subplots(figsize=(12, fig_height))

    # draw user ranges
    color_user = "#6baed6"
    color_overlap = "#fd8d3c"
    for idx, (d, uid) in enumerate(row_map):
        if uid == -1:
            continue
        th = ticket_holders[uid]
        for r in th.availability:
            if r.day != d: continue
            start = _time_to_minutes(r.start_time)
            end = _time_to_minutes(r.end_time)
            ax.barh(idx, end - start, left=start, height=0.8, color=color_user)

    # draw full overlaps on special rows
    overlaps = compute_common_overlaps(ticket_holders)
    # index of overlap row for each day
    day_to_overlap_idx = {}
    idx = 0
    for d in days:
        idx += num_users  # skip user rows
        day_to_overlap_idx[d] = idx
        idx += 1
    for s in overlaps:
        if s.day not in day_to_overlap_idx: continue
        start = _time_to_minutes(s.start_time)
        end = _time_to_minutes(s.end_time)
        ax.barh(day_to_overlap_idx[s.day], end - start, left=start, height=0.8, color=color_overlap)

    # axes
    ax.set_yticks(range(len(row_labels)))
    ax.set_yticklabels(row_labels, fontsize=8)
    ax.set_ylim(-0.5, len(row_labels)-0.5)
    ax.set_xlim(0, 24*60)
    ax.set_xticks([i*60*2 for i in range(13)])
    ax.set_xticklabels([f"{i*2:02d}:00" for i in range(13)], rotation=45)
    ax.set_xlabel("Time of day (HH:MM)")
    ax.set_title("User Availability")
    plt.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path
