"""Detached fixture construction and strictly validated portable snapshots."""
from copy import deepcopy
from datetime import datetime
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import security
from .reservations import candidate, check_occupancy, public
from .time_rules import UTC, WEEKDAYS, instant
from .validation import APIError, email_password, field, identifier, integer, require


def empty_state():
    return {"users": {}, "tokens": {}, "restaurants": {}, "reservations": {}, "receipts": []}


def object_list(body, name):
    values = field(body, name, list)
    require(all(type(v) is dict for v in values), 400, "malformed_request")
    return values


def restaurant_config(raw):
    config = {"id": identifier(raw, "id"), "name": field(raw, "name", str),
              "timezone": field(raw, "timezone", str),
              "slot_minutes": integer(raw, "slot_minutes"),
              "reservation_duration_minutes": integer(raw, "reservation_duration_minutes"),
              "cancellation_cutoff_minutes": integer(raw, "cancellation_cutoff_minutes", 0),
              "opening_hours": [], "tables": []}
    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise APIError() from None
    days = set()
    for raw_hours in object_list(raw, "opening_hours"):
        day = field(raw_hours, "weekday", str)
        opens = field(raw_hours, "opens", str)
        closes = field(raw_hours, "closes", str)
        require(day in WEEKDAYS and day not in days)
        require(all(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", v) for v in (opens, closes)))
        require(opens < closes)
        days.add(day)
        config["opening_hours"].append({"weekday": day, "opens": opens, "closes": closes})
    tids = set()
    for raw_table in object_list(raw, "tables"):
        tid = identifier(raw_table, "id")
        require(tid not in tids)
        tids.add(tid)
        config["tables"].append({"id": tid, "label": field(raw_table, "label", str),
                                  "capacity": integer(raw_table, "capacity")})
    return config


def fixture(body):
    state = empty_state()
    emails = set()
    for raw in object_list(body, "users"):
        uid = identifier(raw, "id")
        email, password = email_password(raw)
        require(uid not in state["users"] and email not in emails)
        emails.add(email)
        state["users"][uid] = {"id": uid, "email": email, "display_name": field(raw, "display_name", str),
                               "credential": security.hash_password(password)}
    for raw in object_list(body, "restaurants"):
        config = restaurant_config(raw)
        require(config["id"] not in state["restaurants"])
        state["restaurants"][config["id"]] = config
    refs = set()
    for raw in object_list(body, "reservations"):
        rid = identifier(raw, "id")
        uid = identifier(raw, "user_id")
        reference = field(raw, "reference", str)
        require(re.fullmatch(r"[A-Z0-9]{6,12}", reference) and reference not in refs)
        require(rid not in state["reservations"] and uid in state["users"])
        record = candidate(state, raw)
        record.update(reservation_id=rid, reference=reference, user_id=uid,
                      status="confirmed", created_at=datetime.now(UTC).isoformat())
        check_occupancy(state, [record])
        state["reservations"][rid] = record
        refs.add(reference)
    return state


def timestamp(value):
    require(type(value) is str and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})", value))
    parsed = datetime.fromisoformat(value)
    require(parsed.utcoffset() is not None)
    return parsed


def validate_record(state, raw, historical=False):
    require(type(raw) is dict)
    identifier(raw, "reservation_id")
    ref = field(raw, "reference", str)
    require(re.fullmatch(r"[A-Z0-9]{6,12}", ref))
    require(raw.get("status") in ("confirmed", "cancelled"))
    timestamp(raw.get("created_at"))
    timestamp(raw.get("starts_at"))
    timestamp(raw.get("ends_at"))
    expected = candidate(state, raw)
    require(raw["starts_at"] == expected["starts_at"] and raw["ends_at"] == expected["ends_at"])
    if not historical:
        require(identifier(raw, "user_id") in state["users"])
    return raw


def import_state(envelope):
    # The import contract overrides generic field-type errors for its envelope/state.
    try:
        require(envelope.get("track") == "tablekeeper")
        require(type(envelope.get("format_version")) is int and envelope["format_version"] == 1)
        raw = envelope.get("state")
        require(type(raw) is dict and set(empty_state()).issubset(raw))
        require(all(type(raw[k]) is dict for k in ("users", "tokens", "restaurants", "reservations")))
        require(type(raw["receipts"]) is list)
        state = empty_state()
        emails = set()
        for uid, user in raw["users"].items():
            require(type(user) is dict and identifier(user, "id") == uid)
            email = field(user, "email", str)
            require(re.fullmatch(r"[^\s@]+@[^\s@]+", email) and email not in emails)
            field(user, "display_name", str)
            credential = field(user, "credential", dict)
            require(credential.get("algorithm") == "scrypt")
            require(all(type(credential.get(k)) is int and credential[k] == v
                        for k, v in security.PARAMETERS.items()))
            require(type(credential.get("salt")) is str and re.fullmatch(r"[0-9a-f]{32}", credential["salt"]))
            require(type(credential.get("digest")) is str and re.fullmatch(r"[0-9a-f]{64}", credential["digest"]))
            # Only persisted schema fields are retained; never accept plaintext password storage.
            state["users"][uid] = {k: deepcopy(user[k]) for k in ("id", "email", "display_name", "credential")}
            emails.add(email)
        for token, uid in raw["tokens"].items():
            require(type(token) is str and re.fullmatch(r"[A-Za-z0-9_-]+", token))
            require(type(uid) is str and uid in state["users"])
            state["tokens"][token] = uid
        for rid, config in raw["restaurants"].items():
            require(type(config) is dict)
            parsed = restaurant_config(config)
            require(parsed["id"] == rid)
            state["restaurants"][rid] = parsed
        references = set()
        for rid, record in raw["reservations"].items():
            validate_record(state, record)
            require(record["reservation_id"] == rid and record["reference"] not in references)
            if record["status"] == "confirmed":
                check_occupancy(state, [record])
            state["reservations"][rid] = deepcopy(record)
            references.add(record["reference"])
        keys = set()
        for receipt in raw["receipts"]:
            require(type(receipt) is dict)
            uid = identifier(receipt, "user_id")
            require(uid in state["users"] and receipt.get("method") == "POST")
            path = field(receipt, "path", str)
            require(path in ("/reservations", "/reservation-moves"))
            key = field(receipt, "key", str)
            require(1 <= len(key) <= 255)
            identity = (uid, path, key)
            require(identity not in keys)
            keys.add(identity)
            request = field(receipt, "request", dict)
            response = field(receipt, "response", dict)
            if path == "/reservations":
                historical = [response]
                expected = candidate(state, request)
                require(all(response.get(k) == v for k, v in expected.items()))
            else:
                items = request.get("moves")
                require(type(items) is list and 1 <= len(items) <= 8)
                require(all(type(i) is dict and type(i.get("reference")) is str for i in items))
                refs = [i["reference"] for i in items]
                require(len(set(refs)) == len(refs))
                historical = field(response, "reservations", list)
                require(len(historical) == len(items))
                require(all(type(r) is dict and r.get("reference") == item["reference"]
                            for r, item in zip(historical, items)))
                for record, item in zip(historical, items):
                    require(all(record.get(k) == item[k] for k in ("table_id", "starts_at_local", "party_size") if k in item))
            for record in historical:
                validate_record(state, record, historical=True)
                require(record["status"] == "confirmed" and "user_id" not in record)
                current = state["reservations"].get(record["reservation_id"])
                require(current is not None and current["user_id"] == uid)
                require(all(record[k] == current[k] for k in ("reference", "restaurant_id", "created_at")))
            require(len({r["restaurant_id"] for r in historical}) == 1)
            check_occupancy(empty_state(), historical)
            state["receipts"].append(deepcopy(receipt))
        return state
    except (APIError, KeyError, TypeError, ValueError, OverflowError):
        raise APIError() from None
