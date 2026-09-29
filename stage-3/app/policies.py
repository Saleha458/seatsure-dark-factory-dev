"""Complete restaurant policy validation and immutable policy snapshots."""
from datetime import date
import re

from .time_rules import WEEKDAYS
from .validation import require


def validate(config, raw):
    require(type(raw) is dict)

    effective_from = raw.get("effective_from")
    require(type(effective_from) is str
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", effective_from) is not None)
    try:
        date.fromisoformat(effective_from)
    except ValueError:
        require(False)

    slot_minutes = raw.get("slot_minutes")
    duration = raw.get("reservation_duration_minutes")
    cutoff = raw.get("cancellation_cutoff_minutes")
    require(type(slot_minutes) is int and 1 <= slot_minutes <= 1440)
    require(type(duration) is int and 1 <= duration <= 1440)
    require(type(cutoff) is int and 0 <= cutoff <= 10080)

    opening_hours = raw.get("opening_hours")
    require(type(opening_hours) is list)
    days = set()
    normalized_hours = []
    for hours in opening_hours:
        require(type(hours) is dict)
        weekday = hours.get("weekday")
        opens = hours.get("opens")
        closes = hours.get("closes")
        require(type(weekday) is str and weekday in WEEKDAYS and weekday not in days)
        require(type(opens) is str and type(closes) is str)
        require(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", opens) is not None)
        require(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", closes) is not None)
        require(opens < closes)
        days.add(weekday)
        normalized_hours.append({"weekday": weekday, "opens": opens, "closes": closes})

    capacities = raw.get("capacities")
    table_ids = {table["id"] for table in config["tables"]}
    require(type(capacities) is dict and set(capacities) == table_ids)
    require(all(type(capacity) is int and 1 <= capacity <= 100
                for capacity in capacities.values()))

    return {
        "effective_from": effective_from,
        "slot_minutes": slot_minutes,
        "reservation_duration_minutes": duration,
        "cancellation_cutoff_minutes": cutoff,
        "opening_hours": normalized_hours,
        "capacities": {table["id"]: capacities[table["id"]] for table in config["tables"]},
    }


def initial(config):
    return {
        "policy_version": 0,
        "slot_minutes": config["slot_minutes"],
        "reservation_duration_minutes": config["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": config["cancellation_cutoff_minutes"],
        "opening_hours": [dict(hours) for hours in config["opening_hours"]],
        "capacities": {table["id"]: table["capacity"] for table in config["tables"]},
    }


def selected(config, local_date):
    effective = [policy for policy in config["policies"]
                 if policy["effective_from"] <= local_date]
    if not effective:
        return initial(config)
    return max(effective, key=lambda policy: (policy["effective_from"],
                                               policy["policy_version"]))


def accepted(config, local_date):
    current = selected(config, local_date)
    return {key: value for key, value in current.items() if key != "effective_from"}


def imported(config, raw_policies, raw_version):
    require(type(raw_policies) is list and type(raw_version) is int and raw_version >= 0)
    require(raw_version == len(raw_policies))
    result = []
    for expected_version, raw in enumerate(raw_policies, 1):
        policy = validate(config, raw)
        require(type(raw.get("policy_version")) is int
                and raw["policy_version"] == expected_version)
        policy["policy_version"] = expected_version
        require(raw == policy)
        result.append(policy)
    return result, raw_version
