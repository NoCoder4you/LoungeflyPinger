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
PREFIX = r"(?:release(?:s|d)?|launch(?:es|ing)?|available(?:\s+from)?|pre-?order[^.]{0,20}?release(?:s|d)?)"


def parse_release_text(
    text: object, *, source: str, local_timezone: str | None = None
) -> ReleaseInfo | None:
    """Parse only explicit release phrases; unrelated/malformed text yields no evidence."""
    if not isinstance(text, str):
        return None
    raw = " ".join(text.split())
    if not raw:
        return None
    if re.search(r"\bcoming\s+soon\b", raw, re.I):
        return ReleaseInfo(ReleasePrecision.COMING_SOON, text=raw, source=source)
    match = re.search(
        rf"\b{PREFIX}\s*:?[\s-]*(?:(\d{{1,2}})[/-](\d{{1,2}})[/-](\d{{4}})|(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_PATTERN})\s+(\d{{4}})|({MONTH_PATTERN})\s+(\d{{4}}))(?:\s+(?:at\s+)?(\d{{1,2}}):(\d{{2}})(?:\s+(UTC|GMT|BST|[A-Za-z_]+/[A-Za-z_]+))?)?",
        raw, re.I,
    )
    if not match:
        return None
    if match.group(1):
        day, month, year = map(int, match.group(1, 2, 3))
    elif match.group(4):
        day, month, year = int(match.group(4)), MONTHS[match.group(5).casefold()], int(match.group(6))
    else:
        # Month precision deliberately has no synthetic day/date.
        return ReleaseInfo(
            ReleasePrecision.MONTH_ONLY, text=raw, source=source,
            release_month=MONTHS[match.group(7).casefold()], release_year=int(match.group(8)),
        )
    try:
        release_date = date(year, month, day)
    except ValueError:
        return None
    hour, minute = match.group(9), match.group(10)
    if hour is None:
        return ReleaseInfo(ReleasePrecision.DATE_ONLY, release_date=release_date, text=raw, source=source)
    try:
        release_time = time(int(hour), int(minute))
        explicit_zone = match.group(11)
        if explicit_zone == "BST":
            zone = timezone(timedelta(hours=1), "BST")
        elif explicit_zone in {"UTC", "GMT"}:
            zone = ZoneInfo("UTC")
        else:
            zone = ZoneInfo(explicit_zone or local_timezone) if (explicit_zone or local_timezone) else None
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if zone is None:
        # A time without a configured/explicit timezone cannot become an exact instant.
        return None
    instant = datetime.combine(release_date, release_time, zone)
    zone_name = explicit_zone or local_timezone
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
