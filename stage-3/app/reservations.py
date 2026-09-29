"""Pure candidate construction; callers commit only after all checks pass."""
from datetime import datetime, timedelta
import re

from . import policies
from .time_rules import UTC, instant, interval, overlaps, parse_date, window
from .validation import field, identifier, party, require


def public(record):
    result = {key: value for key, value in record.items() if key not in ("user_id", "history")}
    table_ids = members(record)
    result["table_ids"] = table_ids
    if len(table_ids) == 1:
        result["table_id"] = table_ids[0]
    else:
        result.pop("table_id", None)
    return result


def members(record):
    table_ids = record.get("table_ids")
    if type(table_ids) is list:
        return table_ids
    table_id = record.get("table_id")
    return [table_id] if type(table_id) is str else []


def fits(capacity, normalized_size):
    capacity_text = str(capacity)
    return (len(normalized_size), normalized_size) <= (len(capacity_text), capacity_text)


def restaurant(state, rid):
    require(rid in state["restaurants"], 404, "not_found")
    return state["restaurants"][rid]


def candidate(state, body, current=None, use_policies=True, policy_override=None):
    merged = {} if current is None else dict(current)
    allowed = ("restaurant_id", "table_id", "table_ids", "starts_at_local", "party_size") if current is None else (
        "table_id", "table_ids", "starts_at_local", "party_size")
    if "table_id" in body and "table_ids" in body:
        require(False)
    merged.update({k: body[k] for k in allowed if k in body})
    if "table_ids" in body:
        merged.pop("table_id", None)
    elif "table_id" in body:
        merged.pop("table_ids", None)
    rid = identifier(merged, "restaurant_id")
    if "table_ids" in merged:
        table_ids = field(merged, "table_ids", list)
        require(all(type(tid) is str for tid in table_ids), 400, "malformed_request")
        require(all(0 < len(tid) <= 64 for tid in table_ids))
        require(len(set(table_ids)) == len(table_ids))
        if len(table_ids) > 2:
            require(False, 422, "combination_not_allowed")
        require(bool(table_ids))
    else:
        table_ids = [identifier(merged, "table_id")]
    local = field(merged, "starts_at_local", str)
    size = party(merged)
    config = restaurant(state, rid)
    tables = {table["id"]: table for table in config["tables"]}
    require(all(tid in tables for tid in table_ids), 404, "not_found")
    if len(table_ids) == 2:
        pair = next((declared for declared in config.get("combinable", [])
                     if set(declared) == set(table_ids)), None)
        require(pair is not None, 422, "combination_not_allowed")
        table_ids = list(pair)
    local_day = local[:10]
    selected = (policy_override if policy_override is not None else
                policies.accepted(config, local_day) if use_policies else policies.initial(config))
    effective = {
        **config,
        "slot_minutes": selected["slot_minutes"],
        "reservation_duration_minutes": selected["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": selected["cancellation_cutoff_minutes"],
        "opening_hours": selected["opening_hours"],
    }
    start, end = interval(effective, local)
    capacity = sum(selected["capacities"][tid] for tid in table_ids)
    require(size <= capacity, 422, "party_exceeds_capacity")
    merged.update(table_ids=list(table_ids), starts_at_local=local, starts_at=start,
                  ends_at=end, party_size=size)
    merged.pop("table_id", None)
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


def check_cutoff(state, record, now, accepted=True):
    minutes = (record.get("accepted_terms", {}).get(
        "cancellation_cutoff_minutes",
        state["restaurants"][record["restaurant_id"]]["cancellation_cutoff_minutes"])
        if accepted else state["restaurants"][record["restaurant_id"]]["cancellation_cutoff_minutes"])
    require((instant(record["starts_at"]) - now).total_seconds() > minutes * 60,
            409, "cutoff_passed")


def unchanged_request(record, changes):
    if "table_id" in changes and "table_ids" in changes:
        return False
    ids = members(record)
    if "table_id" in changes:
        if type(changes["table_id"]) is not str or ids != [changes["table_id"]]:
            return False
    if "table_ids" in changes:
        requested = changes["table_ids"]
        if (type(requested) is not list or len(requested) != len(ids)
                or any(type(table_id) is not str for table_id in requested)
                or len(set(requested)) != len(requested)
                or set(requested) != set(ids)):
            return False
    if "starts_at_local" in changes and (
            type(changes["starts_at_local"]) is not str
            or changes["starts_at_local"] != record["starts_at_local"]):
        return False
    if "party_size" in changes and (
            type(changes["party_size"]) is not int
            or changes["party_size"] != record["party_size"]):
        return False
    return True


def amendment(state, record, changes, now, stage3=True):
    require(record["status"] != "cancelled", 409, "reservation_cancelled")
    expected = changes.get("expected_revision")
    if stage3 and "expected_revision" in changes:
        require(type(expected) is int and expected >= 1)
        require(expected == record.get("revision", 1), 409, "stale_revision")
    check_cutoff(state, record, now, accepted=stage3)
    if stage3 and unchanged_request(record, changes):
        candidate(state, changes, record, use_policies=False,
                  policy_override=record["accepted_terms"])
        return record
    proposed = candidate(state, changes, record, use_policies=stage3)
    if not stage3:
        return proposed
    selected = policies.accepted(state["restaurants"][record["restaurant_id"]],
                                 proposed["starts_at_local"][:10])
    proposed.update(revision=record.get("revision", 1), accepted_terms=selected)
    return proposed


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
        proposed.append(amendment(state, record, item, now, stage3=False))
    check_occupancy(state, proposed, {r["reservation_id"] for r in proposed})
    return proposed


def availability(state, query):
    rid = identifier(query, "restaurant_id")
    date = field(query, "date", str)
    day = parse_date(date)
    explain = "explain" in query
    if explain:
        require(query["explain"] == "true")
    size = field(query, "party_size", str)
    require(re.fullmatch(r"[0-9]+", size) is not None)
    # Canonical decimal ordering is exact and avoids int's digit-conversion limit.
    normalized = size.lstrip("0")
    require(normalized)
    config = restaurant(state, rid)
    selected = policies.selected(config, day.isoformat())
    effective = {
        **config,
        "slot_minutes": selected["slot_minutes"],
        "reservation_duration_minutes": selected["reservation_duration_minutes"],
        "opening_hours": selected["opening_hours"],
    }
    capacities = selected["capacities"]
    result = {"restaurant_id": rid, "date": date, "timezone": config["timezone"], "slots": []}
    bounds = window(effective, day)
    if bounds is None:
        return result
    opening, closing, _, _ = bounds
    local = opening
    from .validation import APIError
    while local < closing:
        value = local.isoformat(timespec="minutes")
        try:
            start, end = interval(effective, value)
        except APIError as exc:
            if exc.code not in ("invalid_local_time", "outside_opening_hours"):
                raise
        else:
            available = []
            options = []
            explanations = []
            for table in config["tables"]:
                table_id = table["id"]
                capacity_holds = fits(capacities[table_id], normalized)
                probe = {"restaurant_id": rid, "table_ids": [table_id], "starts_at": start, "ends_at": end}
                no_overlap_holds = not any(
                    r["status"] == "confirmed" and overlaps(probe, r)
                    for r in state["reservations"].values())
                table_available = capacity_holds and no_overlap_holds
                if table_available:
                    available.append(table_id)
                    options.append({"table_ids": [table_id], "capacity": capacities[table_id]})
                if explain:
                    explanations.append({
                        "table_id": table_id,
                        "policy_version": selected["policy_version"],
                        "available": table_available,
                        "rules": [
                            {"rule": "capacity", "holds": capacity_holds},
                            {"rule": "no_overlap", "holds": no_overlap_holds},
                        ],
                    })
            for pair in config.get("combinable", []):
                capacity = sum(capacities[tid] for tid in pair)
                if not fits(capacity, normalized):
                    continue
                probe = {"restaurant_id": rid, "table_ids": pair, "starts_at": start, "ends_at": end}
                if not any(r["status"] == "confirmed" and overlaps(probe, r)
                           for r in state["reservations"].values()):
                    options.append({"table_ids": list(pair), "capacity": capacity})
            slot = {"starts_at_local": value, "starts_at": start,
                    "available_table_ids": available, "available_options": options}
            if explain:
                slot["explain"] = explanations
            result["slots"].append(slot)
        remaining = (closing - local).total_seconds() / 60
        if effective["slot_minutes"] >= remaining:
            break
        local += timedelta(minutes=effective["slot_minutes"])
    return result
