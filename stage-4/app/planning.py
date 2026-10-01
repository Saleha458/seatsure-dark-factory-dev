"""Deterministic bounded closure replan search."""
from .reservations import members, closed_during
from .time_rules import instant, overlaps, parse_offset_instant
from .validation import require


def option_list(config, booking):
    terms = booking["accepted_terms"]
    capacities = terms["capacities"]
    size = booking["party_size"]
    tables = config["tables"]
    pairs = config.get("combinable", [])
    options = []
    for rank, table in enumerate(tables):
        tid = table["id"]
        if tid in capacities and size <= capacities[tid]:
            options.append(([tid], capacities[tid], rank))
    for pair_idx, pair in enumerate(pairs):
        if all(tid in capacities for tid in pair):
            capacity = sum(capacities[tid] for tid in pair)
            if size <= capacity:
                options.append((list(pair), capacity, len(tables) + pair_idx))
    return options


def assignment_conflicts(probe, fixed, assigned_records, state, proposed_closure):
    if any(overlaps(probe, other) for other in fixed):
        return True
    if any(overlaps(probe, other) for other in assigned_records):
        return True
    if closed_during(state, probe["restaurant_id"], probe["table_ids"],
                     probe["starts_at"], probe["ends_at"],
                     extra_closures=[proposed_closure]):
        return True
    return False


def preview_plan(state, restaurant_id, table_id, from_iso, to_iso):
    config = state["restaurants"][restaurant_id]
    require(len(config["tables"]) <= 6, 422, "planning_limit")
    require(len(config.get("combinable", [])) <= 4, 422, "planning_limit")

    from_at = parse_offset_instant(from_iso)
    to_at = parse_offset_instant(to_iso)
    require(from_at < to_at, 422, "validation_failed")

    considered = []
    for record in state["reservations"].values():
        if (record["restaurant_id"] != restaurant_id
                or record["status"] != "confirmed"):
            continue
        if instant(record["starts_at"]) < to_at and from_at < instant(record["ends_at"]):
            considered.append(record)
    require(len(considered) <= 6, 422, "planning_limit")
    considered = sorted(considered, key=lambda item: item["reference"])
    considered_ids = {item["reservation_id"] for item in considered}
    fixed = [record for record in state["reservations"].values()
             if record["restaurant_id"] == restaurant_id
             and record["status"] == "confirmed"
             and record["reservation_id"] not in considered_ids]

    proposed_closure = {
        "restaurant_id": restaurant_id,
        "table_id": table_id,
        "from": from_iso,
        "to": to_iso,
    }

    if not considered:
        return {
            "closure": {"table_id": table_id, "from": from_iso, "to": to_iso},
            "assignments": [],
            "moved_count": 0,
            "unused_seats": 0,
            "considered": [],
        }

    options_by_booking = [option_list(config, booking) for booking in considered]
    best = None

    def search(index, chosen):
        nonlocal best
        if index == len(considered):
            moved = 0
            unused = 0
            ranks = []
            assignment_rows = []
            for booking, (table_ids, capacity, rank) in zip(considered, chosen):
                before = members(booking)
                changed = set(before) != set(table_ids)
                if changed:
                    moved += 1
                    final_tables = list(table_ids)
                else:
                    final_tables = before
                unused += capacity - booking["party_size"]
                ranks.append(rank)
                assignment_rows.append({
                    "reference": booking["reference"],
                    "reservation_id": booking["reservation_id"],
                    "table_ids": final_tables,
                    "changed": changed,
                })
            key = (moved, unused, tuple(ranks))
            if best is None or key < best[0]:
                best = (key, assignment_rows)
            return

        booking = considered[index]
        assigned_records = []
        for j in range(index):
            rec = dict(considered[j])
            rec["table_ids"] = list(chosen[j][0])
            rec.pop("table_id", None)
            assigned_records.append(rec)

        for table_ids, capacity, rank in options_by_booking[index]:
            probe = dict(booking)
            probe["table_ids"] = list(table_ids)
            probe.pop("table_id", None)
            if assignment_conflicts(probe, fixed, assigned_records, state,
                                    proposed_closure):
                continue
            chosen.append((list(table_ids), capacity, rank))
            search(index + 1, chosen)
            chosen.pop()

    search(0, [])
    require(best is not None, 409, "no_feasible_plan")
    _, assignment_rows = best
    moved_count = sum(1 for row in assignment_rows if row["changed"])
    unused_seats = best[0][1]
    return {
        "closure": {"table_id": table_id, "from": from_iso, "to": to_iso},
        "assignments": [
            {"reference": row["reference"], "table_ids": row["table_ids"],
             "changed": row["changed"]}
            for row in assignment_rows
        ],
        "moved_count": moved_count,
        "unused_seats": unused_seats,
        "considered": assignment_rows,
    }
