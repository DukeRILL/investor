"""Beijing time helpers and the DeepSeek off-peak rule (see docs/RULES.md)."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

# Weekday peak windows in Beijing time (UTC 01:00-04:00 and 06:00-10:00), widened by 15 min.
PEAK_WINDOWS = ((time(8, 45), time(12, 15)), (time(13, 45), time(18, 15)))


class PeakHoursError(RuntimeError):
    """Raised when a DeepSeek call is attempted during peak (expensive) hours."""


def now_bj() -> datetime:
    return datetime.now(timezone.utc).astimezone(BEIJING)


def to_bj(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING)


def is_peak(dt: datetime) -> bool:
    local = to_bj(dt)
    if local.weekday() >= 5:
        return False
    t = local.time()
    return any(start <= t < end for start, end in PEAK_WINDOWS)


def next_offpeak(dt: datetime) -> datetime:
    """Return dt if it is off-peak, otherwise the end of the current peak window."""
    local = to_bj(dt)
    if not is_peak(local):
        return local
    t = local.time()
    for start, end in PEAK_WINDOWS:
        if start <= t < end:
            return local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    return local


def minutes_until_peak(dt: datetime) -> float:
    """Minutes of off-peak time left before the next peak window (inf on weekends)."""
    local = to_bj(dt)
    if is_peak(local):
        return 0.0
    for days in range(0, 4):
        day = (local + timedelta(days=days)).date()
        if datetime.combine(day, time(0), BEIJING).weekday() >= 5:
            continue
        for start, _ in PEAK_WINDOWS:
            candidate = datetime.combine(day, start, BEIJING)
            if candidate > local:
                return (candidate - local).total_seconds() / 60
    return float("inf")


def fmt_bj(dt: datetime) -> str:
    """Published time; the post never says which time zone it is (owner's rule, docs/RULES.md §5)."""
    return to_bj(dt).strftime("%d.%m.%Y %H:%M")


RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря"]


def ru_date(dt: datetime) -> str:
    local = to_bj(dt)
    return f"{local.day} {RU_MONTHS[local.month - 1]} {local.year}"
