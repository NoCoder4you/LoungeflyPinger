"""Conservative parsing and display of retailer-published release information."""

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import ReleaseInfo, ReleasePrecision

MONTHS = {name.casefold(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1
)}
MONTH_PATTERN = "|".join(MONTHS)
PREFIX = r"(?:release(?:\s+date|s|d)?|launch(?:es|ing)?|available(?:\s+from)?|pre-?order[^.]{0,20}?release(?:s|d)?)"


def parse_release_text(
    text: object, *, source: str, local_timezone: str | None = None,
    date_order: str = "DMY",
) -> ReleaseInfo | None:
    """Parse only explicit release phrases; unrelated/malformed text yields no evidence."""
    normalized_order = date_order.strip().upper()
    if normalized_order not in {"DMY", "MDY"}:
        raise ValueError("date_order must be DMY or MDY")
    if not isinstance(text, str):
        return None
    raw = " ".join(text.split())
    if not raw:
        return None
    if re.search(r"\bcoming\s+soon\b", raw, re.I):
        return ReleaseInfo(ReleasePrecision.COMING_SOON, text=raw, source=source)
    match = re.search(
        rf"\b{PREFIX}\s*:?[\s-]*(?:(?P<number_a>\d{{1,2}})[/-](?P<number_b>\d{{1,2}})[/-](?P<number_year>\d{{4}})|(?P<day_first>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<day_month>{MONTH_PATTERN})\s+(?P<day_year>\d{{4}})|(?P<month_first>{MONTH_PATTERN})\s+(?P<month_day>\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(?P<month_day_year>\d{{4}})|(?P<month_only>{MONTH_PATTERN})\s+(?P<month_year>\d{{4}}))(?:\s+(?:at\s+)?(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})(?:\s+(?P<zone>UTC|GMT|BST|[A-Za-z_]+/[A-Za-z_]+))?)?",
        raw, re.I,
    )
    if not match:
        return None
    if match.group("number_a"):
        first, second = int(match.group("number_a")), int(match.group("number_b"))
        day, month = (first, second) if normalized_order == "DMY" else (second, first)
        year = int(match.group("number_year"))
    elif match.group("day_first"):
        day = int(match.group("day_first"))
        month = MONTHS[match.group("day_month").casefold()]
        year = int(match.group("day_year"))
    elif match.group("month_day"):
        day = int(match.group("month_day"))
        month = MONTHS[match.group("month_first").casefold()]
        year = int(match.group("month_day_year"))
    else:
        # Month precision deliberately has no synthetic day/date.
        return ReleaseInfo(
            ReleasePrecision.MONTH_ONLY, text=raw, source=source,
            release_month=MONTHS[match.group("month_only").casefold()],
            release_year=int(match.group("month_year")),
        )
    try:
        release_date = date(year, month, day)
    except ValueError:
        return None
    hour, minute = match.group("hour"), match.group("minute")
    if hour is None:
        return ReleaseInfo(ReleasePrecision.DATE_ONLY, release_date=release_date, text=raw, source=source)
    try:
        release_time = time(int(hour), int(minute))
        explicit_zone = match.group("zone")
        abbreviation = (
            explicit_zone.upper()
            if explicit_zone is not None and "/" not in explicit_zone
            else explicit_zone
        )
        if abbreviation == "BST":
            zone = timezone(timedelta(hours=1), "BST")
        elif abbreviation in {"UTC", "GMT"}:
            zone = ZoneInfo("UTC")
        else:
            zone = ZoneInfo(explicit_zone or local_timezone) if (explicit_zone or local_timezone) else None
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if zone is None:
        # A time without a configured/explicit timezone cannot become an exact instant.
        return None
    instant = datetime.combine(release_date, release_time, zone)
    zone_name = abbreviation or local_timezone
    return ReleaseInfo(
        ReleasePrecision.EXACT_DATETIME, release_date, release_time, zone_name, instant,
        raw, source, timezone_inferred=explicit_zone is None,
    )


def format_release(info: ReleaseInfo | None) -> str:
    if info is None or info.precision == ReleasePrecision.UNKNOWN:
        return "Unknown"
    if info.precision == ReleasePrecision.COMING_SOON:
        return "Coming Soon"
    if info.precision == ReleasePrecision.MONTH_ONLY:
        assert info.release_month and info.release_year
        return date(info.release_year, info.release_month, 1).strftime("%B %Y")
    assert info.release_date is not None
    value = info.release_date.strftime("%d %B %Y").lstrip("0")
    if info.release_datetime is not None:
        value += info.release_datetime.strftime(" at %H:%M %Z")
    return value
