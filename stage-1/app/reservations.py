"""Pure candidate construction; callers commit only after all checks pass."""
from datetime import datetime, timedelta
import re

from .time_rules import UTC, instant, interval, overlaps, parse_date, window
from .validation import field, identifier, party, require


def public(record):
    return {key: value for key, value in record.items() if key != "user_id"}


def restaurant(state, rid):
    require(rid in state["restaurants"], 404, "not_found")
    return state["restaurants"][rid]


def candidate(state, body, current=None):
    merged = {} if current is None else dict(current)
    allowed = ("restaurant_id", "table_id", "starts_at_local", "party_size") if current is None else (
        "table_id", "starts_at_local", "party_size")
    merged.update({k: body[k] for k in allowed if k in body})
    rid = identifier(merged, "restaurant_id")
    tid = identifier(merged, "table_id")
    local = field(merged, "starts_at_local", str)
    size = party(merged)
    config = restaurant(state, rid)
    table = next((t for t in config["tables"] if t["id"] == tid), None)
    require(table is not None, 404, "not_found")
    start, end = interval(config, local)
    require(size <= table["capacity"], 422, "party_exceeds_capacity")
    merged.update(starts_at=start, ends_at=end)
    return merged


def check_occupancy(state, candidates, excluded=()):
    others = [r for rid, r in state["reservations"].items()
              if rid not in excluded and r["status"] == "confirmed"]
    for proposed in candidates:
        require(not any(overlaps(proposed, r) for r in others), 409, "table_unavailable")
        others.append(proposed)


def owned(state, reference, uid):
    record = next((r for r in state["reservations"].values() if r["reference"] == reference), None)
    require(record is not None and record["user_id"] == uid, 404, "not_found")
    return record


def check_cutoff(state, record, now):
    minutes = state["restaurants"][record["restaurant_id"]]["cancellation_cutoff_minutes"]
    require((instant(record["starts_at"]) - now).total_seconds() > minutes * 60,
            409, "cutoff_passed")


def amendment(state, record, changes, now):
    require(record["status"] != "cancelled", 409, "reservation_cancelled")
    check_cutoff(state, record, now)
    return candidate(state, changes, record)


def moves(state, body, uid, now):
    items = body.get("moves")
    require(type(items) is list and 1 <= len(items) <= 8)
    require(all(type(item) is dict and type(item.get("reference")) is str for item in items))
    refs = [item["reference"] for item in items]
    require(len(set(refs)) == len(refs))
    proposed = []
    for item in items:
        record = owned(state, item["reference"], uid)
        require(not proposed or record["restaurant_id"] == proposed[0]["restaurant_id"])
        proposed.append(amendment(state, record, item, now))
    check_occupancy(state, proposed, {r["reservation_id"] for r in proposed})
    return proposed


def availability(state, query):
    rid = identifier(query, "restaurant_id")
    date = field(query, "date", str)
    day = parse_date(date)
    size = field(query, "party_size", str)
    require(re.fullmatch(r"[0-9]+", size) is not None)
    # Compare decimal length before int conversion; enormous parties fit no table.
    normalized = size.lstrip("0")
    require(normalized)
    size = int(normalized) if len(normalized) < 100 else 10 ** 100
    config = restaurant(state, rid)
    result = {"restaurant_id": rid, "date": date, "timezone": config["timezone"], "slots": []}
    bounds = window(config, day)
    if bounds is None:
        return result
    opening, closing, _, _ = bounds
    local = opening
    from .validation import APIError
    while local < closing:
        value = local.isoformat(timespec="minutes")
        try:
            start, end = interval(config, value)
        except APIError as exc:
            if exc.code not in ("invalid_local_time", "outside_opening_hours"):
                raise
        else:
            available = []
            for table in config["tables"]:
                probe = {"restaurant_id": rid, "table_id": table["id"], "starts_at": start, "ends_at": end}
                if table["capacity"] >= size and not any(
                    r["status"] == "confirmed" and overlaps(probe, r)
                    for r in state["reservations"].values()
                ):
                    available.append(table["id"])
            result["slots"].append({"starts_at_local": value, "starts_at": start,
                                    "available_table_ids": available})
        remaining = (closing - local).total_seconds() / 60
        if config["slot_minutes"] >= remaining:
            break
        local += timedelta(minutes=config["slot_minutes"])
    return result
