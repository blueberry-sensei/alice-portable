"""
Time handling utilities
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

# Common time zones
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
UTC_TZ = ZoneInfo("UTC")


def get_utc_now() -> datetime:
    """
    Get the current UTC time

    Returns:
        A UTC datetime object
    """
    return datetime.now(timezone.utc)


def parse_iso_datetime(dt_str: str) -> Optional[datetime]:
    """
    Parse an ISO 8601 time string

    Args:
        dt_str: the ISO format time string

    Returns:
        A datetime object, or None when parsing fails
    """
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def format_datetime(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    Format a datetime object

    Args:
        dt: the datetime object
        fmt: the format string

    Returns:
        The formatted string
    """
    return dt.strftime(fmt)


def get_time_ago(dt: datetime) -> str:
    """
    Get a relative time description

    Args:
        dt: the datetime object

    Returns:
        A relative time description (such as "3 minutes ago")
    """
    now = get_utc_now()

    # Make sure dt carries time zone information
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = now - dt

    if delta < timedelta(minutes=1):
        return "just now"
    elif delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() / 60)
        return f"{minutes} minutes ago"
    elif delta < timedelta(days=1):
        hours = int(delta.total_seconds() / 3600)
        return f"{hours} hours ago"
    elif delta < timedelta(days=30):
        days = delta.days
        return f"{days} days ago"
    elif delta < timedelta(days=365):
        months = int(delta.days / 30)
        return f"{months} months ago"
    else:
        years = int(delta.days / 365)
        return f"{years} years ago"


def calculate_time_decay(
    created_time: datetime,
    decay_factor: float = 0.01,
) -> float:
    """
    Compute the time decay factor

    Uses the exponential decay formula e^(-lambda t)

    Args:
        created_time: creation time
        decay_factor: the decay factor lambda (default 0.01)

    Returns:
        The decay factor (between 0 and 1)
    """
    import math

    now = get_utc_now()

    # Make sure created_time carries time zone information
    if created_time.tzinfo is None:
        created_time = created_time.replace(tzinfo=timezone.utc)

    days_ago = (now - created_time).days
    return math.exp(-decay_factor * days_ago)


def utc_to_beijing(dt: Optional[datetime], fmt: Optional[str] = None) -> Union[datetime, str, None]:
    """
    Convert a UTC time to Beijing time

    Args:
        dt: the UTC datetime object (naive or aware)
        fmt: an optional format string; when given, a formatted string is returned

    Returns:
        - when fmt is None: a Beijing time datetime object
        - when fmt is not None: the formatted string
        - when dt is None: None (or an empty string when fmt is not None)

    Examples:
        >>> utc_to_beijing(dt)  # returns a datetime
        >>> utc_to_beijing(dt, "%Y-%m-%d %H:%M:%S")  # returns "2026-01-08 13:49:03"
        >>> utc_to_beijing(dt, "%H:%M")  # returns "13:49"
    """
    if dt is None:
        return "" if fmt else None

    # A naive datetime is assumed to be UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)

    # Convert to Beijing time
    beijing_time = dt.astimezone(BEIJING_TZ)

    if fmt:
        return beijing_time.strftime(fmt)
    return beijing_time
