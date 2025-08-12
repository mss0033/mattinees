from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

# Use a non-interactive backend for headless servers
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .models import TicketHolder, DayTimeRange
from .scheduling import WEEKDAY_ORDER


def _hhmm_to_hours(s: str) -> float:
    """Convert 'HH:MM' to hours as a float (e.g., '18:30' -> 18.5)."""
    hh, mm = s.split(":")
    return int(hh) + int(mm) / 60.0


def _collect_blocks_for_day(th: TicketHolder, day: str) -> List[DayTimeRange]:
    return [r for r in th.availability if r.day == day]


def create_availability_chart(
    ticket_holders: Dict[int, TicketHolder],
    day: Optional[str] = None,
) -> str:
    """
    Render a swimlanes-style availability chart and return the PNG file path.
    - If `day` is provided, show that single day with one lane per user.
    - Otherwise, show all 7 days, grouped top-to-bottom; for each day, one lane per user.
    """
    # Determine which days to plot
    days_to_plot = [day] if day in WEEKDAY_ORDER else WEEKDAY_ORDER

    # Build a stable, readable user order
    users: List[TicketHolder] = sorted(
        ticket_holders.values(),
        key=lambda th: (th.user_name or str(th.user_id)).lower()
    )
    user_labels = [(u.user_name or str(u.user_id)) for u in users]
    num_users = max(1, len(users))  # avoid div-by-zero

    # Lane indexing: for each (day, user) pair assign a y position
    # layout: day 0 group occupies rows [0..num_users-1], day 1 group occupies [num_users+1 .. 2*num_users], etc.
    lane_y_positions: Dict[tuple, float] = {}
    group_gap = 1  # one row gap between day groups
    total_rows = 0
    for di, d in enumerate(days_to_plot):
        base = di * (num_users + group_gap)
        for ui, _user in enumerate(users):
            lane_y_positions[(d, _user.user_id)] = base + ui
        total_rows = base + num_users  # track maximum used row index

    # Figure sizing heuristic: width fixed; height scales with lanes
    lanes_count = len(days_to_plot) * num_users + (len(days_to_plot) - 1) * group_gap
    fig_w = 16
    fig_h = max(3, min(20, 0.6 * lanes_count + 1))  # cap height to keep files reasonable
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    # Axes: time 0..24 on X; categorical lanes on Y
    ax.set_xlim(0, 24)
    ax.set_xlabel("Time (24h)")
    # set up y-ticks and labels
    yticks: List[float] = []
    ylabels: List[str] = []
    for di, d in enumerate(days_to_plot):
        base = di * (num_users + group_gap)
        for ui, u in enumerate(users):
            y = base + ui
            yticks.append(y)
            label = f"{d} — {user_labels[ui]}"
            ylabels.append(label)
    ax.set_yticks(yticks)
    ax.set_yticklabels(ylabels)

    # Gridlines: vertical hour lines, subtle horizontal separators at day group gaps
    ax.set_xticks(list(range(0, 25, 1)))
    ax.grid(axis="x", linestyle=":", linewidth=0.8)
    # horizontal lines between day groups
    for di in range(1, len(days_to_plot)):
        y = di * (num_users + group_gap) - 0.5
        ax.axhline(y=y, linestyle="--", linewidth=0.8, alpha=0.5)

    # Draw each availability block as a horizontal bar on the user's lane
    bar_height = 0.8
    for u in users:
        for d in days_to_plot:
            lane_y = lane_y_positions[(d, u.user_id)]
            blocks = _collect_blocks_for_day(u, d)
            for r in blocks:
                xs = _hhmm_to_hours(r.start_time)
                xe = _hhmm_to_hours(r.end_time)
                if xe <= xs:
                    continue
                ax.broken_barh([(xs, xe - xs)], (lane_y - bar_height / 2, bar_height))

    # Title
    title = "Availability"
    if day in WEEKDAY_ORDER:
        title += f" — {day}"
    ax.set_title(title)

    fig.tight_layout()

    # Save to a temp file and return the path
    ts = int(time.time() * 1000)
    out_path = os.path.join(os.getcwd(), "data", f"availability_{ts}.png")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)

    return out_path
