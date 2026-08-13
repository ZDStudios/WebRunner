"""A small, dependency-free cron expression parser.

Supports the classic 5-field form ``m h dom mon dow`` and an optional 6-field
form where the first field is seconds. Within a field: ``*``, ``?`` (alias for
``*``), ``a``, ``a-b``, ``*/n``, ``a-b/n``, comma-separated lists, and three
letter names for months (JAN..DEC) and weekdays (SUN..SAT).

Nicknames: ``@yearly @annually @monthly @weekly @daily @midnight @hourly
@minutely``.

Vixie semantics are honoured: when *both* day-of-month and day-of-week are
restricted the expression matches if *either* one matches.

Not supported (rejected with a clear error rather than silently ignored):
``L``, ``W``, ``#`` and ``LW`` qualifiers.
"""

from __future__ import annotations

from datetime import datetime, timedelta

__all__ = ["CronExpr", "CronError", "parse", "next_after", "describe"]


class CronError(ValueError):
    """Raised when a cron expression cannot be parsed."""


NICKNAMES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
    "@minutely": "* * * * *",
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DOWS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

# (name, low, high, aliases)
_SPEC = [
    ("second", 0, 59, {}),
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, MONTHS),
    ("day of week", 0, 7, DOWS),
]

_SEARCH_LIMIT_DAYS = 366 * 5


def _parse_field(raw: str, name: str, low: int, high: int, aliases: dict[str, int]) -> tuple[set[int], bool]:
    """Return (matching values, restricted) for one cron field."""
    text = raw.strip().lower()
    if not text:
        raise CronError(f"empty {name} field")
    for bad in ("l", "w", "#"):
        if bad in text and text not in aliases:
            # Allow the letters that make up month/weekday names.
            if not any(alias in text for alias in aliases):
                raise CronError(f"unsupported qualifier {bad.upper()!r} in {name} field")
    restricted = text not in ("*", "?")
    values: set[int] = set()

    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty entry in {name} field")
        step = 1
        if "/" in part:
            part, _, step_raw = part.partition("/")
            if not step_raw.isdigit() or int(step_raw) == 0:
                raise CronError(f"invalid step {step_raw!r} in {name} field")
            step = int(step_raw)
            part = part.strip() or "*"

        if part in ("*", "?"):
            start, end = low, high
        elif "-" in part[1:]:  # part[1:] so a leading '-' is not read as a range
            start_raw, _, end_raw = part.partition("-")
            start = _to_int(start_raw, name, aliases)
            end = _to_int(end_raw, name, aliases)
        else:
            start = _to_int(part, name, aliases)
            end = start if step == 1 else high

        if start > end:
            # Wrap-around ranges such as FRI-MON or 22-2.
            values.update(range(start, high + 1, step))
            offset = (high + 1 - start) % step
            values.update(range(low + offset, end + 1, step))
        else:
            values.update(range(start, end + 1, step))

    for value in values:
        if not low <= value <= high:
            raise CronError(f"value {value} out of range {low}-{high} in {name} field")

    if name == "day of week":
        # Cron allows both 0 and 7 for Sunday; normalise to 0.
        if 7 in values:
            values.discard(7)
            values.add(0)
    if not values:
        raise CronError(f"{name} field matches nothing")
    return values, restricted


def _to_int(raw: str, name: str, aliases: dict[str, int]) -> int:
    token = raw.strip().lower()
    if token in aliases:
        return aliases[token]
    try:
        return int(token)
    except ValueError:
        raise CronError(f"invalid value {raw!r} in {name} field") from None


class CronExpr:
    """A parsed cron expression."""

    __slots__ = ("expression", "seconds", "minutes", "hours", "days", "months",
                 "dows", "_dom_restricted", "_dow_restricted")

    def __init__(self, expression: str):
        self.expression = expression.strip()
        text = self.expression.lower()
        if text in NICKNAMES:
            text = NICKNAMES[text]
        elif text.startswith("@"):
            raise CronError(
                f"unknown nickname {expression!r}; try one of {', '.join(sorted(NICKNAMES))}"
            )

        fields = text.split()
        if len(fields) == 5:
            fields = ["0"] + fields  # seconds default to :00
        elif len(fields) != 6:
            raise CronError(
                f"expected 5 fields (m h dom mon dow) or 6 (with leading seconds), got {len(fields)}"
            )

        parsed = []
        restricted = []
        for value, (name, low, high, aliases) in zip(fields, _SPEC):
            got, is_restricted = _parse_field(value, name, low, high, aliases)
            parsed.append(got)
            restricted.append(is_restricted)

        self.seconds, self.minutes, self.hours, self.days, self.months, self.dows = parsed
        self._dom_restricted = restricted[3]
        self._dow_restricted = restricted[5]

    def matches_day(self, dt: datetime) -> bool:
        dom_ok = dt.day in self.days
        dow_ok = (dt.isoweekday() % 7) in self.dows  # Mon=1..Sun=7  ->  Sun=0..Sat=6
        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok
        if self._dom_restricted:
            return dom_ok
        if self._dow_restricted:
            return dow_ok
        return True

    def matches(self, dt: datetime) -> bool:
        return (
            dt.second in self.seconds
            and dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.month in self.months
            and self.matches_day(dt)
        )

    def next_after(self, after: datetime) -> datetime:
        """First matching *naive* datetime strictly after ``after``."""
        dt = (after + timedelta(seconds=1)).replace(microsecond=0)
        deadline = dt + timedelta(days=_SEARCH_LIMIT_DAYS)

        while dt < deadline:
            if dt.month not in self.months:
                dt = _start_of_next_month(dt)
                continue
            if not self.matches_day(dt):
                dt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0)
                continue
            if dt.hour not in self.hours:
                dt = (dt + timedelta(hours=1)).replace(minute=0, second=0)
                continue
            if dt.minute not in self.minutes:
                dt = (dt + timedelta(minutes=1)).replace(second=0)
                continue
            if dt.second not in self.seconds:
                dt = dt + timedelta(seconds=1)
                continue
            return dt

        raise CronError(
            f"{self.expression!r} has no matching time within "
            f"{_SEARCH_LIMIT_DAYS // 366} years (impossible date?)"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CronExpr({self.expression!r})"


def _start_of_next_month(dt: datetime) -> datetime:
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return datetime(year, month, 1)


def parse(expression: str) -> CronExpr:
    return CronExpr(expression)


def next_after(expression: str, after: datetime) -> datetime:
    return CronExpr(expression).next_after(after)


_ORDINALS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def describe(expression: str) -> str:
    """A short human-readable summary; falls back to the raw expression."""
    try:
        expr = CronExpr(expression)
    except CronError:
        return expression

    def step_of(values: set[int], low: int, high: int) -> int | None:
        """Detect an evenly spaced set such as */5, so it reads as an interval."""
        ordered = sorted(values)
        if len(ordered) < 2 or ordered[0] != low:
            return None
        gap = ordered[1] - ordered[0]
        if gap < 2 or (high + 1) % gap != 0:
            return None
        if any(b - a != gap for a, b in zip(ordered, ordered[1:])):
            return None
        return gap if ordered[-1] + gap > high else None

    def summarise(values: set[int], low: int, high: int, names: list[str] | None = None) -> str | None:
        if len(values) == high - low + 1:
            return None
        ordered = sorted(values)
        if len(ordered) > 6:
            return f"{len(ordered)} values"
        if names:
            return ", ".join(names[v] for v in ordered)
        return ", ".join(str(v) for v in ordered)

    minute = summarise(expr.minutes, 0, 59)
    hour = summarise(expr.hours, 0, 23)
    parts: list[str] = []

    minute_step = step_of(expr.minutes, 0, 59)
    hour_step = step_of(expr.hours, 0, 23)

    if minute_step and hour is None:
        parts.append(f"every {minute_step} minutes")
    elif hour_step and expr.minutes == {0}:
        parts.append(f"every {hour_step} hours")
    elif hour is None and minute is None:
        parts.append("every minute")
    elif hour is None:
        parts.append(f"at minute {minute} of every hour")
    elif minute is None:
        parts.append(f"every minute during hour {hour}")
    else:
        times = [f"{h:02d}:{m:02d}" for h in sorted(expr.hours) for m in sorted(expr.minutes)]
        parts.append("at " + ", ".join(times) if len(times) <= 4 else f"{len(times)} times a day")

    dom = summarise(expr.days, 1, 31)
    dow = summarise(expr.dows, 0, 6, _ORDINALS)
    month = summarise(expr.months, 1, 12, _MONTH_NAMES)

    if dow:
        parts.append(f"on {dow}")
    if dom:
        parts.append(f"on day {dom}")
    if month:
        parts.append(f"in {month}")
    return " ".join(parts)
