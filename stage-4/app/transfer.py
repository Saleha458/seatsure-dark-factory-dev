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
    return {"users": {}, "tokens": {}, "restaurants": {}, "reservations": {},
            "series": {}, "closures": [], "plans": {}, "receipts": []}


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
              "opening_hours": [], "tables": [], "policies": [], "policy_version": 0,
              "revision": 0}
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
    if preserve_policies and "revision" in raw:
        require(type(raw["revision"]) is int and raw["revision"] >= 0)
        config["revision"] = raw["revision"]
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
        record.update(revision=1, accepted_terms=policies.initial(state["restaurants"][record["restaurant_id"]]))
        record["history"] = [history_entry(
            1, record["created_at"], "created", create_changes(record), 1, record["accepted_terms"])]
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


def validate_accepted_terms(config, terms):
    require(type(terms) is dict
            and set(terms) == {"policy_version", "slot_minutes",
                               "reservation_duration_minutes",
                               "cancellation_cutoff_minutes",
                               "opening_hours", "capacities"})
    version = terms.get("policy_version")
    require(type(version) is int and version >= 0)
    require(type(terms.get("slot_minutes")) is int
            and 1 <= terms["slot_minutes"] <= 1440)
    require(type(terms.get("reservation_duration_minutes")) is int
            and 1 <= terms["reservation_duration_minutes"] <= 1440)
    require(type(terms.get("cancellation_cutoff_minutes")) is int
            and 0 <= terms["cancellation_cutoff_minutes"] <= 10080)
    capacities = terms.get("capacities")
    require(type(capacities) is dict
            and set(capacities) == {table["id"] for table in config["tables"]}
            and all(type(capacity) is int and 1 <= capacity <= 100
                    for capacity in capacities.values()))
    if version == 0:
        expected = policies.initial(config)
    else:
        published = next((item for item in config["policies"]
                          if item["policy_version"] == version), None)
        require(published is not None)
        expected = {key: value for key, value in published.items()
                    if key != "effective_from"}
    require(terms == expected)


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
    terms = raw.get("accepted_terms")
    config = state["restaurants"].get(raw.get("restaurant_id"))
    require(config is not None)
    require("accepted_terms" not in raw or type(terms) is dict)
    if type(terms) is dict:
        validate_accepted_terms(config, terms)
        capacities = terms["capacities"]
        override = {
            "policy_version": terms.get("policy_version"),
            "slot_minutes": terms.get("slot_minutes"),
            "reservation_duration_minutes": terms.get("reservation_duration_minutes"),
            "cancellation_cutoff_minutes": terms.get("cancellation_cutoff_minutes"),
            "opening_hours": terms.get("opening_hours"),
            "capacities": capacities,
        }
        config_for_candidate = override
        expected = candidate(state, candidate_raw, use_policies=False, policy_override=config_for_candidate)
    else:
        expected = candidate(state, candidate_raw, use_policies=False)
    require(raw["starts_at"] == expected["starts_at"] and raw["ends_at"] == expected["ends_at"])
    if "table_ids" in raw:
        require(raw["table_ids"] == expected["table_ids"])
    else:
        require(raw.get("table_id") == expected["table_ids"][0] and len(expected["table_ids"]) == 1)
    if not historical:
        require(identifier(raw, "user_id") in state["users"])
    revision = raw.get("revision", 1)
    require(type(revision) is int and revision >= 1)
    if "history" in raw:
        validate_history(raw, revision, terms, config)
    return raw


def with_legacy_terms(state, raw):
    record = deepcopy(raw)
    config = state["restaurants"][record["restaurant_id"]]
    terms = record.get("accepted_terms", policies.initial(config))
    record.setdefault("revision", 1)
    record["accepted_terms"] = terms
    record.setdefault("history", [history_entry(
        1, record["created_at"], "created", create_changes(record), 1, terms)])
    validate_history(record, record["revision"], terms, config)
    return record


def create_changes(record):
    if len(record["table_ids"]) == 1:
        table_change = {"field": "table_id", "from": None, "to": record["table_ids"][0]}
    else:
        table_change = {"field": "table_ids", "from": None, "to": list(record["table_ids"])}
    return [table_change,
            {"field": "starts_at_local", "from": None, "to": record["starts_at_local"]},
            {"field": "party_size", "from": None, "to": record["party_size"]}]


def history_entry(seq, at, event, changes, revision, accepted_terms, plan_id=None):
    entry = {"seq": seq, "at": at, "event": event, "changes": deepcopy(changes),
             "revision": revision, "accepted_terms": deepcopy(accepted_terms)}
    if plan_id is not None:
        entry["plan_id"] = plan_id
    return entry


def validate_history(record, revision, terms, config):
    entries = record["history"]
    require(type(entries) is list and len(entries) >= 1)
    require(len(entries) == revision)
    previous_at = None
    for index, entry in enumerate(entries, 1):
        require(type(entry) is dict and entry.get("seq") == index)
        timestamp(entry.get("at"))
        require(entry.get("event") in ("created", "changed", "cancelled", "reassigned"))
        require(index == 1 or entry.get("event") in ("changed", "cancelled", "reassigned"))
        require(index != 1 or entry.get("event") == "created")
        require(type(entry.get("changes")) is list)
        require(type(entry.get("revision")) is int and entry["revision"] == index)
        require(type(entry.get("accepted_terms")) is dict)
        validate_accepted_terms(config, entry["accepted_terms"])
        if entry["event"] == "cancelled":
            require(entry["changes"] == [] and index == len(entries)
                    and record.get("status") == "cancelled")
        elif entry["event"] == "reassigned":
            plan_id = entry.get("plan_id")
            require(type(plan_id) is str and 0 < len(plan_id) <= 64)
            changes = entry["changes"]
            require(len(changes) == 1 and type(changes[0]) is dict)
            require(set(changes[0]) == {"field", "from", "to"}
                    and changes[0].get("field") == "table_ids")
            require(type(changes[0].get("from")) is list and type(changes[0].get("to")) is list)
        elif entry["event"] == "changed":
            changes = entry["changes"]
            fields = [change.get("field") for change in changes if type(change) is dict]
            require(len(fields) == len(changes) and fields)
            ranks = {"table_id": 0, "table_ids": 0, "starts_at_local": 1, "party_size": 2}
            require(all(field_name in ranks for field_name in fields))
            require(not ("table_id" in fields and "table_ids" in fields))
            require(fields == sorted(fields, key=ranks.__getitem__))
            require(len(set(fields)) == len(fields))
            require(all(set(change) == {"field", "from", "to"} for change in changes))
        else:
            changes = entry["changes"]
            fields = [change.get("field") for change in changes if type(change) is dict]
            require(len(fields) == 3 and len(fields) == len(changes))
            require(fields in (["table_id", "starts_at_local", "party_size"],
                               ["table_ids", "starts_at_local", "party_size"]))
            require(all(change.get("from") is None
                        and set(change) == {"field", "from", "to"} for change in changes))
        if index > 1:
            require(entries[index - 2].get("event") != "cancelled")
        if previous_at is not None:
            require(timestamp(entry["at"]) >= previous_at)
        previous_at = timestamp(entry["at"])
    if terms is not None:
        require(entries[-1]["accepted_terms"] == terms)


def validate_series(state, raw_series):
    require(type(raw_series) is dict)
    result = {}
    seen_reservations = set()
    for series_id, raw in raw_series.items():
        require(type(series_id) is str and 0 < len(series_id) <= 64)
        require(type(raw) is dict and raw.get("series_id") == series_id)
        restaurant_id = identifier(raw, "restaurant_id")
        user_id = identifier(raw, "user_id")
        require(restaurant_id in state["restaurants"] and user_id in state["users"])
        revision = raw.get("revision")
        interval_weeks = raw.get("interval_weeks")
        require(type(revision) is int and revision >= 1)
        require(type(interval_weeks) is int and 1 <= interval_weeks <= 4)
        anchor_id = identifier(raw, "anchor_reservation_id")
        occurrences = raw.get("occurrences")
        require(type(occurrences) is list and 2 <= len(occurrences) <= 12)
        require(set(raw) == {"series_id", "restaurant_id", "user_id", "revision",
                             "interval_weeks", "anchor_reservation_id", "occurrences"})
        for index, occurrence in enumerate(occurrences):
            require(type(occurrence) is dict
                    and set(occurrence) == {"index", "reservation_id", "exception"}
                    and type(occurrence.get("index")) is int
                    and occurrence.get("index") == index
                    and type(occurrence.get("exception")) is bool)
            reservation_id = identifier(occurrence, "reservation_id")
            require(reservation_id not in seen_reservations)
            record = state["reservations"].get(reservation_id)
            require(record is not None and record["restaurant_id"] == restaurant_id
                    and record["user_id"] == user_id)
            if index == 0:
                require(reservation_id == anchor_id)
            seen_reservations.add(reservation_id)
        require(occurrences[0]["reservation_id"] == anchor_id)
        result[series_id] = deepcopy(raw)
    return result


def import_state(envelope):
    # The import contract overrides generic field-type errors for its envelope/state.
    try:
        require(envelope.get("track") == "tablekeeper")
        require(type(envelope.get("format_version")) is int and envelope["format_version"] == 1)
        raw = envelope.get("state")
        required_state = {"users", "tokens", "restaurants", "reservations", "receipts"}
        require(type(raw) is dict and required_state.issubset(raw))
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
        legacy_reservations = set()
        for rid, record in raw["reservations"].items():
            validate_record(state, record)
            require(record["reservation_id"] == rid and record["reference"] not in references)
            if "history" in record:
                require("accepted_terms" in record)
            else:
                legacy_reservations.add(rid)
            record = with_legacy_terms(state, record)
            if record["status"] == "confirmed":
                check_occupancy(state, [record])
            state["reservations"][rid] = deepcopy(record)
            references.add(record["reference"])
        state["series"] = validate_series(state, raw.get("series", {}))
        state["closures"] = []
        for closure in raw.get("closures", []):
            require(type(closure) is dict)
            rid = identifier(closure, "restaurant_id")
            require(rid in state["restaurants"])
            config = state["restaurants"][rid]
            table_id = field(closure, "table_id", str)
            require(any(t["id"] == table_id for t in config["tables"]))
            from_str = field(closure, "from", str)
            to_str = field(closure, "to", str)
            require(instant(from_str) < instant(to_str))
            state["closures"].append({
                "restaurant_id": rid, "table_id": table_id,
                "from": from_str, "to": to_str,
            })
        state["plans"] = {}
        for plan_id, plan in raw.get("plans", {}).items():
            require(type(plan) is dict and type(plan_id) is str and 0 < len(plan_id) <= 64)
            require(plan.get("plan_id") == plan_id)
            rid = identifier(plan, "restaurant_id")
            require(rid in state["restaurants"])
            state["plans"][plan_id] = deepcopy(plan)
        series_counts = {}
        for series in state["series"].values():
            restaurant_id = series["restaurant_id"]
            series_counts[restaurant_id] = series_counts.get(restaurant_id, 0) + 1
        require(all(config["revision"] >= series_counts.get(restaurant_id, 0)
                    for restaurant_id, config in state["restaurants"].items()))
        receipt_series = set()
        keys = set()
        for receipt in raw["receipts"]:
            require(type(receipt) is dict)
            uid = identifier(receipt, "user_id")
            require(uid in state["users"] and receipt.get("method") == "POST")
            path = field(receipt, "path", str)
            parts = path.split("/")
            is_policy_path = len(parts) == 4 and parts[1] == "restaurants" and parts[3] == "policies"
            is_replan_preview = len(parts) == 4 and parts[1] == "restaurants" and parts[3] == "replans"
            is_replan_apply = len(parts) == 6 and parts[1] == "restaurants" and parts[3] == "replans" and parts[5] == "apply"
            is_series_amend = len(parts) == 4 and parts[1] == "series" and parts[3] == "amend"
            is_series_path = path == "/series"
            require(path in ("/reservations", "/reservation-moves")
                    or is_policy_path or is_series_path
                    or is_replan_preview or is_replan_apply or is_series_amend)
            key = field(receipt, "key", str)
            require(1 <= len(key) <= 255)
            identity = (uid, path, key)
            require(identity not in keys)
            keys.add(identity)
            request = field(receipt, "request", dict)
            response = field(receipt, "response", dict)
            if is_policy_path:
                restaurant_id = unquote(parts[2])
                require(restaurant_id in state["restaurants"])
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
            elif is_replan_preview:
                restaurant_id = unquote(parts[2])
                require(restaurant_id in state["restaurants"])
                config = state["restaurants"][restaurant_id]
                require(uid in config["manager_user_ids"])
                require(type(response) is dict and "plan_id" in response)
                historical = []
            elif is_replan_apply:
                restaurant_id = unquote(parts[2])
                require(restaurant_id in state["restaurants"])
                config = state["restaurants"][restaurant_id]
                require(uid in config["manager_user_ids"])
                plan_id = unquote(parts[4])
                require(type(response) is dict and response.get("plan_id") == plan_id)
                historical = []
            elif is_series_amend:
                series_id = unquote(parts[2])
                series = state["series"].get(series_id)
                require(series is not None and series["user_id"] == uid)
                require(type(response) is dict and response.get("series_id") == series_id)
                historical = []
            elif path == "/reservations":
                historical = [response]
                expected = candidate(state, request, use_policies=False,
                                     policy_override=response.get("accepted_terms"))
                for key, value in expected.items():
                    if key == "table_ids" and key not in response:
                        require(len(value) == 1 and response.get("table_id") == value[0])
                    else:
                        require(response.get(key) == value)
                reservation_id = response.get("reservation_id")
                current = state["reservations"].get(reservation_id)
                require(current is not None and current["user_id"] == uid)
                if reservation_id not in legacy_reservations:
                    require("accepted_terms" in response)
            elif path == "/reservation-moves":
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
                    expected = candidate(state, item, record, use_policies=False)
                    for key in ("starts_at_local", "party_size"):
                        if key in item:
                            require(record.get(key) == expected[key])
                    if "table_ids" in record:
                        require(record["table_ids"] == expected["table_ids"])
                    else:
                        require(len(expected["table_ids"]) == 1
                                and record.get("table_id") == expected["table_ids"][0])
            else:
                series_id = response.get("series_id")
                series = state["series"].get(series_id)
                anchor_reference = field(request, "anchor_reference", str)
                count = request.get("count")
                interval_weeks = request.get("interval_weeks")
                historical = field(response, "occurrences", list)
                require(series is not None and series["user_id"] == uid
                        and type(count) is int and 2 <= count <= 12
                        and type(interval_weeks) is int and 1 <= interval_weeks <= 4
                        and type(response.get("revision")) is int
                        and response.get("revision") == 1
                        and response.get("interval_weeks") == interval_weeks
                        and series["interval_weeks"] == interval_weeks
                        and len(historical) == count
                        and len(series["occurrences"]) == count
                        and series["anchor_reservation_id"]
                        == series["occurrences"][0]["reservation_id"])
                anchor = state["reservations"][series["anchor_reservation_id"]]
                require(anchor["reference"] == anchor_reference
                        and response.get("series_id") == series_id)
                receipt_series.add(series_id)
                for index, (entry, occurrence) in enumerate(
                        zip(historical, series["occurrences"])):
                    require(type(entry) is dict and entry.get("index") == index
                            and type(entry.get("index")) is int
                            and type(entry.get("exception")) is bool
                            and entry.get("exception") is False
                            and type(entry.get("reservation")) is dict)
                    saved = entry["reservation"]
                    validate_record(state, saved, historical=True)
                    require(saved.get("status") == "confirmed"
                            and "user_id" not in saved
                            and saved.get("reservation_id") == occurrence["reservation_id"]
                            and saved.get("reference")
                            == state["reservations"][occurrence["reservation_id"]]["reference"])
            if not (is_policy_path or is_replan_preview or is_replan_apply or is_series_amend):
                if not is_series_path:
                    for record in historical:
                        validate_record(state, record, historical=True)
                        require(record["status"] == "confirmed" and "user_id" not in record)
                        current = state["reservations"].get(record["reservation_id"])
                        require(current is not None and current["user_id"] == uid)
                        require(all(record[k] == current[k]
                                    for k in ("reference", "restaurant_id", "created_at")))
                    require(len({r["restaurant_id"] for r in historical}) == 1)
                    check_occupancy(empty_state(), historical)
            state["receipts"].append(deepcopy(receipt))
        require(receipt_series == set(state["series"]))
        return state
    except (APIError, KeyError, TypeError, ValueError, OverflowError):
        raise APIError() from None
