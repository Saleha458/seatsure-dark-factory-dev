"""Wall-clock grids, first-fold resolution and absolute-time occupancy."""
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from .validation import APIError, require

UTC = timezone.utc
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_local(value):
    require(type(value) is str, 400, "malformed_request")
    require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", value))
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise APIError() from None


def parse_date(value):
    require(type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
    return parse_local(value + "T00:00").date()


def resolve_local(local, zone):
    if isinstance(local, str):
        local = parse_local(local)
    if isinstance(zone, str):
        zone = ZoneInfo(zone)
    candidates = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        try:
            instant = aware.astimezone(UTC)
            if instant.astimezone(zone).replace(tzinfo=None) == local:
                candidates.append(instant)
        except (OverflowError, ValueError):
            continue
    require(candidates, 422, "invalid_local_time")
    return min(candidates).astimezone(zone)


def boundary(local, zone):
    # A gap boundary denotes the first valid instant after the skipped range.
    for _ in range(1441):
        try:
            return resolve_local(local, zone)
        except APIError as exc:
            if exc.code != "invalid_local_time":
                raise
            local += timedelta(minutes=1)
    raise APIError(422, "invalid_local_time")


def window(restaurant, day):
    hours = next((h for h in restaurant["opening_hours"]
                  if h["weekday"] == WEEKDAYS[day.weekday()]), None)
    if hours is None:
        return None
    opening = parse_local(day.isoformat() + "T" + hours["opens"])
    closing = parse_local(day.isoformat() + "T" + hours["closes"])
    zone = ZoneInfo(restaurant["timezone"])
    return opening, closing, boundary(opening, zone), boundary(closing, zone)


def rfc3339(value):
    # RFC3339 cannot express historical IANA offsets containing seconds.
    # UTC preserves the instant without rounding or truncating that offset.
    if value.utcoffset().total_seconds() % 60:
        value = value.astimezone(UTC)
    return value.isoformat()


def interval(restaurant, value):
    local = parse_local(value)
    start = resolve_local(local, restaurant["timezone"])
    try:
        end = (start.astimezone(UTC) + timedelta(
            minutes=restaurant["reservation_duration_minutes"])).astimezone(start.tzinfo)
    except (OverflowError, ValueError):
        raise APIError(422, "outside_opening_hours") from None
    bounds = window(restaurant, local.date())
    require(bounds is not None, 422, "outside_opening_hours")
    opening, closing, open_at, close_at = bounds
    require(opening <= local < closing and start.astimezone(UTC) >= open_at.astimezone(UTC)
            and end.astimezone(UTC) <= close_at.astimezone(UTC), 422, "outside_opening_hours")
    minutes = (local - opening).total_seconds() / 60
    require(minutes % restaurant["slot_minutes"] == 0, 422, "not_on_slot_grid")
    return rfc3339(start), rfc3339(end)


def instant(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def overlaps(a, b):
    return (a["restaurant_id"] == b["restaurant_id"] and a["table_id"] == b["table_id"]
            and instant(a["starts_at"]) < instant(b["ends_at"])
            and instant(b["starts_at"]) < instant(a["ends_at"]))
