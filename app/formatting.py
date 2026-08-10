from __future__ import annotations

from datetime import datetime

from app.rates import METERS_PER_MILE


def format_duration(started_at: datetime, ended_at: datetime) -> str:
    secs = int((ended_at - started_at).total_seconds())
    hours, minutes = divmod(secs // 60, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def format_miles(meters: float) -> str:
    return f"{meters / METERS_PER_MILE:.1f}"


def format_usd(amount: float | None) -> str:
    return "--" if amount is None else f"${amount:,.2f}"
