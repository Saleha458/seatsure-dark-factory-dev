"""Detached fixture construction and strictly validated portable snapshots."""
from copy import deepcopy
from datetime import datetime
import re
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import security
from . import policies
from .reservations import candidate, check_occupancy, public
from .time_rules import UTC, WEEKDAYS, instant
from .validation import APIError, email_password, field, identifier, integer, require


def empty_state():
    return {"users": {}, "tokens": {}, "restaurants": {}, "reservations": {}, "receipts": []}


def object_list(body, name):
    values = field(body, name, list)
    require(all(type(v) is dict for v in values), 400, "malformed_request")
    return values


def restaurant_config(raw, user_ids=None, preserve_policies=False):
    config = {"id": identifier(raw, "id"), "name": field(raw, "name", str),
              "timezone": field(raw, "timezone", str),
              "slot_minutes": integer(raw, "slot_minutes"),
              "reservation_duration_minutes": integer(raw, "reservation_duration_minutes"),
              "cancellation_cutoff_minutes": integer(raw, "cancellation_cutoff_minutes", 0),
              "opening_hours": [], "tables": [], "policies": [], "policy_version": 0}
    manager_user_ids = raw.get("manager_user_ids", [])
    require(type(manager_user_ids) is list, 400, "malformed_request")
    require(all(type(uid) is str for uid in manager_user_ids), 400, "malformed_request")
    require(all(0 < len(uid) <= 64 for uid in manager_user_ids))
    require(len(set(manager_user_ids)) == len(manager_user_ids))
    if user_ids is not None:
        require(all(uid in user_ids for uid in manager_user_ids))
    config["manager_user_ids"] = list(manager_user_ids)
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
    table_ids = {table["id"] for table in config["tables"]}
    combinations = set()
    parsed_combinations = []
    raw_combinations = raw.get("combinable", [])
    require(type(raw_combinations) is list, 400, "malformed_request")
    for raw_pair in raw_combinations:
        require(type(raw_pair) is list, 400, "malformed_request")
        require(len(raw_pair) == 2 and all(type(tid) is str for tid in raw_pair))
        require(all(0 < len(tid) <= 64 for tid in raw_pair))
        require(raw_pair[0] != raw_pair[1] and all(tid in table_ids for tid in raw_pair))
        identity = frozenset(raw_pair)
        require(identity not in combinations)
        combinations.add(identity)
        parsed_combinations.append(list(raw_pair))
    if "combinable" in raw:
        config["combinable"] = parsed_combinations
    if preserve_policies and ("policies" in raw or "policy_version" in raw):
        require("policies" in raw and "policy_version" in raw)
        config["policies"], config["policy_version"] = policies.imported(
            config, raw["policies"], raw["policy_version"])
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
        config = restaurant_config(raw, state["users"])
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
        status = raw.get("status", "confirmed")
        require(status in ("confirmed", "cancelled"))
        record.update(reservation_id=rid, reference=reference, user_id=uid,
                      status=status, created_at=datetime.now(UTC).isoformat())
        if status == "confirmed":
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
    candidate_raw = raw
    if "table_ids" in raw and "table_id" in raw:
        candidate_raw = dict(raw)
        candidate_raw.pop("table_id")
        require(type(raw["table_ids"]) is list and len(raw["table_ids"]) == 1
                and raw["table_id"] == raw["table_ids"][0])
    expected = candidate(state, candidate_raw)
    require(raw["starts_at"] == expected["starts_at"] and raw["ends_at"] == expected["ends_at"])
    if "table_ids" in raw:
        require(raw["table_ids"] == expected["table_ids"])
    else:
        require(raw.get("table_id") == expected["table_ids"][0] and len(expected["table_ids"]) == 1)
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
            parsed = restaurant_config(config, state["users"], preserve_policies=True)
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
            is_policy_path = False
            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "restaurants" and parts[3] == "policies":
                restaurant_id = unquote(parts[2])
                is_policy_path = restaurant_id in state["restaurants"]
            require(path in ("/reservations", "/reservation-moves") or is_policy_path)
            key = field(receipt, "key", str)
            require(1 <= len(key) <= 255)
            identity = (uid, path, key)
            require(identity not in keys)
            keys.add(identity)
            request = field(receipt, "request", dict)
            response = field(receipt, "response", dict)
            if is_policy_path:
                config = state["restaurants"][restaurant_id]
                version = response.get("policy_version")
                require(type(version) is int and version >= 1)
                saved = next((item for item in config["policies"]
                              if item["policy_version"] == version), None)
                require(uid in config["manager_user_ids"] and saved is not None and response == saved
                        and saved == {
                    **policies.validate(config, request), "policy_version": version
                })
                historical = []
            elif path == "/reservations":
                historical = [response]
                expected = candidate(state, request)
                for key, value in expected.items():
                    if key == "table_ids" and key not in response:
                        require(len(value) == 1 and response.get("table_id") == value[0])
                    else:
                        require(response.get(key) == value)
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
                    expected = candidate(state, item, record)
                    for key in ("starts_at_local", "party_size"):
                        if key in item:
                            require(record.get(key) == expected[key])
                    if "table_ids" in record:
                        require(record["table_ids"] == expected["table_ids"])
                    else:
                        require(len(expected["table_ids"]) == 1
                                and record.get("table_id") == expected["table_ids"][0])
            if not is_policy_path:
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
