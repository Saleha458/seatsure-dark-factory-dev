"""One process-wide state owner and linearization point for every operation."""
from copy import deepcopy
from datetime import datetime, timedelta
import re
import secrets
from threading import RLock
from uuid import uuid4
from urllib.parse import unquote

from . import planning, policies, reservations, security, transfer
from .reservations import closed_during, members
from .time_rules import UTC, instant, overlaps, parse_local, parse_offset_instant
from .validation import email_password, field, json_equal, require


class Store:
    def __init__(self):
        self.lock = RLock()
        self.generation = 0
        self.state = transfer.empty_state()

    def replace(self, state):
        with self.lock:
            self.state = state
            self.generation += 1

    def auth(self, body, signup):
        email, password = email_password(body)
        if signup:
            require(len(password) >= 8)
            display = field(body, "display_name", str)
            with self.lock:
                require(not any(u["email"] == email for u in self.state["users"].values()), 409, "email_taken")
            credential = security.hash_password(password)
            with self.lock:
                # Signup commits against the current root even if a reset occurred while hashing.
                require(not any(u["email"] == email for u in self.state["users"].values()), 409, "email_taken")
                uid = uuid4().hex
                user = {"id": uid, "email": email, "display_name": display, "credential": credential}
                self.state["users"][uid] = user
                return 201, self.new_session(user)
        for _ in range(2):
            with self.lock:
                generation = self.generation
                user = next((u for u in self.state["users"].values() if u["email"] == email), None)
                require(user is not None, 401, "unauthenticated")
                user = deepcopy(user)
            valid = security.verify_password(password, user["credential"])
            with self.lock:
                if generation != self.generation:
                    continue
                require(valid and self.state["users"].get(user["id"]) == user, 401, "unauthenticated")
                return 200, self.new_session(user)
        require(False, 401, "unauthenticated")

    def new_session(self, user):
        token = secrets.token_urlsafe(32)
        while token in self.state["tokens"]:
            token = secrets.token_urlsafe(32)
        self.state["tokens"][token] = user["id"]
        return {"user_id": user["id"], "display_name": user["display_name"], "token": token}

    def authenticate(self, headers):
        value = headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer +([A-Za-z0-9._~+/-]+=*)", value, re.IGNORECASE)
        uid = self.state["tokens"].get(match[1]) if match else None
        require(uid is not None, 401, "unauthenticated")
        return uid

    def history_owner(self, reference, headers):
        value = headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]+)", value, re.IGNORECASE)
        uid = self.state["tokens"].get(match[1]) if match else None
        record = next((item for item in self.state["reservations"].values()
                       if item["reference"] == reference), None)
        require(uid is not None and record is not None and record["user_id"] == uid,
                404, "not_found")
        return record

    @staticmethod
    def append_history(record, event, changes, at, plan_id=None):
        entries = record.setdefault("history", [])
        if entries:
            previous = instant(entries[-1]["at"])
            if at < previous:
                at = previous
        entries.append(transfer.history_entry(
            len(entries) + 1, at.isoformat(), event, changes,
            record["revision"], record["accepted_terms"], plan_id=plan_id))

    @staticmethod
    def series_for_reservation(state, reservation_id):
        for series in state["series"].values():
            for occurrence in series["occurrences"]:
                if occurrence["reservation_id"] == reservation_id:
                    return series, occurrence
        return None, None

    @staticmethod
    def series_response(state, series):
        return {
            "series_id": series["series_id"],
            "revision": series["revision"],
            "interval_weeks": series["interval_weeks"],
            "occurrences": [
                {"index": occurrence["index"],
                 "reference": state["reservations"][occurrence["reservation_id"]]["reference"],
                 "exception": occurrence["exception"],
                 "reservation": reservations.public(
                     state["reservations"][occurrence["reservation_id"]])}
                for occurrence in series["occurrences"]
            ],
        }

    def create_series(self, state, body, uid, now):
        anchor_reference = field(body, "anchor_reference", str)
        require(0 < len(anchor_reference) <= 64)
        anchor = reservations.owned(state, anchor_reference, uid)
        require(anchor["status"] != "cancelled", 409, "reservation_cancelled")
        existing, _ = self.series_for_reservation(state, anchor["reservation_id"])
        require(existing is None, 409, "already_in_series")
        count = body.get("count")
        interval_weeks = body.get("interval_weeks")
        require(type(count) is int and 2 <= count <= 12)
        require(type(interval_weeks) is int and 1 <= interval_weeks <= 4)
        reservations.check_cutoff(state, anchor, now)

        anchor_local = parse_local(anchor["starts_at_local"])
        generated = []
        for index in range(1, count):
            try:
                occurrence_day = anchor_local.date() + timedelta(
                    weeks=index * interval_weeks)
            except OverflowError:
                require(False)
            local_start = (occurrence_day.isoformat() + "T"
                           + anchor_local.strftime("%H:%M"))
            candidate = reservations.candidate(
                state, {"starts_at_local": local_start}, anchor)
            generated.append(candidate)
            reservations.check_occupancy(
                state, generated, {anchor["reservation_id"]})

        reserved_references = {item["reference"]
                               for item in state["reservations"].values()}
        records = []
        occurrences = [{"index": 0, "reservation_id": anchor["reservation_id"],
                        "exception": False}]
        for index, proposed in enumerate(generated, 1):
            reservation_id = uuid4().hex
            while reservation_id in state["reservations"]:
                reservation_id = uuid4().hex
            reference = secrets.token_hex(5).upper()
            while reference in reserved_references:
                reference = secrets.token_hex(5).upper()
            reserved_references.add(reference)
            proposed.update(
                reservation_id=reservation_id, reference=reference, user_id=uid,
                status="confirmed", created_at=now.isoformat(), revision=1,
                accepted_terms=policies.accepted(
                    state["restaurants"][anchor["restaurant_id"]],
                    proposed["starts_at_local"][:10]))
            proposed["history"] = [transfer.history_entry(
                1, proposed["created_at"], "created",
                transfer.create_changes(proposed), 1, proposed["accepted_terms"])]
            records.append(proposed)
            occurrences.append({"index": index, "reservation_id": reservation_id,
                                "exception": False})

        series_id = uuid4().hex
        while series_id in state["series"]:
            series_id = uuid4().hex
        series = {
            "series_id": series_id,
            "restaurant_id": anchor["restaurant_id"],
            "user_id": uid,
            "revision": 1,
            "interval_weeks": interval_weeks,
            "anchor_reservation_id": anchor["reservation_id"],
            "occurrences": occurrences,
        }
        for record in records:
            state["reservations"][record["reservation_id"]] = record
        state["series"][series_id] = series
        restaurant = state["restaurants"][anchor["restaurant_id"]]
        restaurant["revision"] = restaurant.get("revision", 0) + 1
        return self.series_response(state, series)

    def commit_moves(self, state, proposed, now):
        changed_records = []
        changed_series = {}
        for record in proposed:
            current = state["reservations"][record["reservation_id"]]
            changes = self.reservation_changes(current, record)
            if not changes:
                continue
            record = dict(record)
            record["revision"] = current.get("revision", 1) + 1
            record["accepted_terms"] = policies.accepted(
                state["restaurants"][record["restaurant_id"]],
                record["starts_at_local"][:10])
            record["history"] = deepcopy(current.get("history", []))
            self.append_history(record, "changed", changes, now)
            changed_records.append(record)
            series, occurrence = self.series_for_reservation(
                state, current["reservation_id"])
            if series is not None:
                entry = changed_series.setdefault(series["series_id"], (series, []))
                entry[1].append(occurrence)

        for record in changed_records:
            state["reservations"][record["reservation_id"]] = record
        for series, occurrences in changed_series.values():
            for occurrence in occurrences:
                occurrence["exception"] = True
            series["revision"] += 1
        if changed_records:
            restaurant = state["restaurants"][changed_records[0]["restaurant_id"]]
            restaurant["revision"] = restaurant.get("revision", 0) + 1

    @staticmethod
    def reservation_changes(current, proposed):
        changes = []
        before_tables = reservations.members(current)
        after_tables = reservations.members(proposed)
        if before_tables != after_tables:
            if len(before_tables) == len(after_tables) == 1:
                changes.append({"field": "table_id", "from": before_tables[0],
                                "to": after_tables[0]})
            else:
                changes.append({"field": "table_ids", "from": before_tables,
                                "to": after_tables})
        if current["starts_at_local"] != proposed["starts_at_local"]:
            changes.append({"field": "starts_at_local",
                            "from": current["starts_at_local"],
                            "to": proposed["starts_at_local"]})
        if current["party_size"] != proposed["party_size"]:
            changes.append({"field": "party_size", "from": current["party_size"],
                            "to": proposed["party_size"]})
        return changes

    def receipt_for(self, state, uid, method, path, key, body):
        require(type(key) is str and key != "", 400, "missing_idempotency_key")
        require(len(key) <= 255)
        receipt = next((item for item in state["receipts"]
                        if item["user_id"] == uid and item["method"] == method
                        and item["path"] == path and item["key"] == key), None)
        if receipt is not None:
            require(json_equal(body, receipt["request"]), 409, "idempotency_key_reuse")
        return receipt

    @staticmethod
    def save_receipt(state, uid, method, path, key, body, response):
        state["receipts"].append({
            "user_id": uid, "method": method, "path": path, "key": key,
            "request": deepcopy(body), "response": deepcopy(response),
        })

    def original_series_dates(self, state, series):
        series_id = series["series_id"]
        receipt = next((item for item in state["receipts"]
                        if item.get("path") == "/series"
                        and type(item.get("response")) is dict
                        and item["response"].get("series_id") == series_id), None)
        require(receipt is not None)
        occurrences = receipt["response"].get("occurrences")
        require(type(occurrences) is list
                and len(occurrences) == len(series["occurrences"]))
        dates = []
        for index, entry in enumerate(occurrences):
            require(type(entry) is dict and entry.get("index") == index)
            saved = entry.get("reservation")
            require(type(saved) is dict)
            value = saved.get("starts_at_local")
            require(type(value) is str and re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", value))
            dates.append(value[:10])
        return dates

    def amend_series(self, state, series, body, now):
        expected = body.get("expected_revision")
        require(type(expected) is int and expected >= 1)
        require(expected == series["revision"], 409, "stale_revision")
        from_index = body.get("from_index")
        require(type(from_index) is int and 0 <= from_index < len(series["occurrences"]))
        local_time = body.get("local_time")
        require(type(local_time) is str, 400, "malformed_request")
        require(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", local_time) is not None)

        scheduled_dates = self.original_series_dates(state, series)
        changes = []
        for occurrence in series["occurrences"][from_index:]:
            if occurrence["exception"]:
                continue
            current = state["reservations"][occurrence["reservation_id"]]
            if current["status"] != "confirmed":
                continue
            desired_local = scheduled_dates[occurrence["index"]] + "T" + local_time
            if desired_local == current["starts_at_local"]:
                continue
            proposed = reservations.amendment(
                state, current, {"starts_at_local": desired_local}, now)
            changes.append((current, proposed))

        if not changes:
            return self.series_response(state, series)

        proposed_records = [record for _, record in changes]
        excluded = {record["reservation_id"] for record in proposed_records}
        reservations.check_occupancy(state, proposed_records, excluded)

        for current, proposed in changes:
            proposed = dict(proposed)
            proposed["revision"] = current.get("revision", 1) + 1
            proposed["history"] = deepcopy(current.get("history", []))
            event_changes = self.reservation_changes(current, proposed)
            self.append_history(proposed, "changed", event_changes, now)
            state["reservations"][current["reservation_id"]] = proposed

        series["revision"] += 1
        restaurant = state["restaurants"][series["restaurant_id"]]
        restaurant["revision"] = restaurant.get("revision", 0) + 1
        return self.series_response(state, series)

    def preview_replan(self, state, restaurant_id, body, uid):
        config = reservations.restaurant(state, restaurant_id)
        require(uid in config["manager_user_ids"], 403, "forbidden")
        table_id = field(body, "table_id", str)
        require(0 < len(table_id) <= 64)
        require(any(table["id"] == table_id for table in config["tables"]),
                404, "not_found")
        from_iso = field(body, "from", str)
        to_iso = field(body, "to", str)
        preview = planning.preview_plan(
            state, restaurant_id, table_id, from_iso, to_iso)

        plan_id = uuid4().hex
        while plan_id in state["plans"]:
            plan_id = uuid4().hex
        captured_revision = config.get("revision", 0)
        response = {
            "plan_id": plan_id,
            "restaurant_id": restaurant_id,
            "restaurant_revision": captured_revision,
            "closure": deepcopy(preview["closure"]),
            "assignments": deepcopy(preview["assignments"]),
            "moved_count": preview["moved_count"],
            "unused_seats": preview["unused_seats"],
        }
        snapshots = {}
        for row in preview["considered"]:
            record = state["reservations"][row["reservation_id"]]
            snapshots[row["reservation_id"]] = {
                "revision": record.get("revision", 1),
                "status": record["status"],
                "starts_at": record["starts_at"],
                "ends_at": record["ends_at"],
                "table_ids": members(record),
            }
        state["plans"][plan_id] = {
            **deepcopy(response),
            "considered": deepcopy(preview["considered"]),
            "snapshots": snapshots,
            "applied": False,
        }
        return response

    def apply_replan(self, state, restaurant_id, plan_id, body, uid, now):
        require(body == {})
        config = reservations.restaurant(state, restaurant_id)
        require(uid in config["manager_user_ids"], 403, "forbidden")
        plan = state["plans"].get(plan_id)
        require(plan is not None and plan.get("restaurant_id") == restaurant_id,
                404, "not_found")
        require(not plan.get("applied", False), 409, "plan_already_applied")
        require(config.get("revision", 0) == plan.get("restaurant_revision"),
                409, "stale_revision")

        closure = {
            "restaurant_id": restaurant_id,
            "table_id": plan["closure"]["table_id"],
            "from": plan["closure"]["from"],
            "to": plan["closure"]["to"],
        }
        changed = []
        all_candidates = []
        considered_ids = set()
        changed_series = {}
        assignments_by_reference = {
            item["reference"]: item for item in plan.get("assignments", [])
        }
        for row in plan.get("considered", []):
            reservation_id = row["reservation_id"]
            current = state["reservations"].get(reservation_id)
            snapshot = plan.get("snapshots", {}).get(reservation_id)
            require(current is not None and snapshot is not None, 409, "stale_revision")
            require(current.get("revision", 1) == snapshot["revision"]
                    and current["status"] == snapshot["status"]
                    and current["starts_at"] == snapshot["starts_at"]
                    and current["ends_at"] == snapshot["ends_at"]
                    and members(current) == snapshot["table_ids"],
                    409, "stale_revision")
            assignment = assignments_by_reference.get(current["reference"])
            require(assignment is not None)
            target_tables = list(assignment["table_ids"])
            proposed = dict(current)
            proposed["table_ids"] = target_tables
            proposed.pop("table_id", None)
            require(not closed_during(
                state, restaurant_id, target_tables,
                proposed["starts_at"], proposed["ends_at"],
                extra_closures=[closure]), 409, "table_unavailable")
            all_candidates.append(proposed)
            considered_ids.add(reservation_id)
            if members(current) != target_tables:
                changed.append((current, proposed))

        reservations.check_occupancy(state, all_candidates, considered_ids)

        for current, proposed in changed:
            proposed = dict(proposed)
            proposed["revision"] = current.get("revision", 1) + 1
            proposed["history"] = deepcopy(current.get("history", []))
            self.append_history(proposed, "reassigned", [{
                "field": "table_ids", "from": members(current),
                "to": list(proposed["table_ids"]),
            }], now, plan_id=plan_id)
            state["reservations"][current["reservation_id"]] = proposed
            series, _ = self.series_for_reservation(state, current["reservation_id"])
            if series is not None:
                changed_series[series["series_id"]] = series

        for series in changed_series.values():
            series["revision"] += 1
        state["closures"].append(closure)
        config["revision"] = config.get("revision", 0) + 1
        plan["applied"] = True
        plan["applied_restaurant_revision"] = config["revision"]
        response = {
            "plan_id": plan_id,
            "restaurant_id": restaurant_id,
            "restaurant_revision": config["revision"],
            "closure": deepcopy(plan["closure"]),
            "assignments": deepcopy(plan["assignments"]),
            "moved_count": plan["moved_count"],
            "unused_seats": plan["unused_seats"],
        }
        return response

    def dispatch(self, method, path, query, body, headers):
        if method == "POST" and path == "/_test/reset":
            self.replace(transfer.fixture(body))
            return 204, None
        if method == "POST" and path == "/_test/import":
            self.replace(transfer.import_state(body))
            return 204, None
        if method == "POST" and path in ("/auth/signup", "/auth/login"):
            return self.auth(body, path == "/auth/signup")
        with self.lock:
            status, response = self.dispatch_locked(method, path, query, body, headers)
            return status, deepcopy(response)

    def dispatch_locked(self, method, path, query, body, headers):
        state = self.state
        if method == "GET":
            if path == "/health":
                return 200, {"status": "ok"}
            if path == "/_test/export":
                return 200, {"track": "tablekeeper", "format_version": 1, "state": state}
            if path == "/restaurants":
                return 200, {"restaurants": [{k: r[k] for k in ("id", "name", "timezone")
                                              if k in r} | ({"combinable": deepcopy(r["combinable"])}
                                                            if "combinable" in r else {})
                                              for r in state["restaurants"].values()]}
            if path.startswith("/restaurants/") and path.count("/") == 2:
                rid = unquote(path.split("/")[2])
                require(0 < len(rid) <= 64)
                config = reservations.restaurant(state, rid)
                return 200, {key: value for key, value in config.items()
                             if key not in ("manager_user_ids", "policies", "policy_version",
                                            "revision")}
            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "restaurants" and parts[3] == "policies":
                rid = unquote(parts[2])
                require(0 < len(rid) <= 64)
                config = reservations.restaurant(state, rid)
                return 200, {"policies": config["policies"]}
            if path == "/availability":
                return 200, reservations.availability(state, query)
        parts = path.split("/")
        if method == "GET" and len(parts) == 4 and parts[1] == "reservations" \
                and parts[3] in ("history", "decision"):
            record = self.history_owner(unquote(parts[2]), headers)
            if parts[3] == "history":
                return 200, {"reference": record["reference"],
                             "entries": record.get("history", [])}
            return 200, {"reference": record["reference"],
                         "revision": record["revision"],
                         "accepted_terms": record["accepted_terms"]}
        if method == "GET" and len(parts) == 3 and parts[1] == "series":
            value = headers.get("Authorization", "")
            match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]+)", value, re.IGNORECASE)
            uid = state["tokens"].get(match[1]) if match else None
            series = state["series"].get(unquote(parts[2]))
            require(uid is not None and series is not None and series["user_id"] == uid,
                    404, "not_found")
            return 200, self.series_response(state, series)
        if method == "POST" and len(parts) == 4 and parts[1] == "series" \
                and parts[3] == "amend":
            uid = self.authenticate(headers)
            series_id = unquote(parts[2])
            series = state["series"].get(series_id)
            require(series is not None and series["user_id"] == uid, 404, "not_found")
            key = headers.get("Idempotency-Key", "")
            receipt = self.receipt_for(state, uid, method, path, key, body)
            if receipt is not None:
                return 200, receipt["response"]
            now = datetime.now(UTC)
            response = self.amend_series(state, series, body, now)
            self.save_receipt(state, uid, method, path, key, body, response)
            return 201, response
        if method == "POST" and len(parts) == 4 and parts[1] == "restaurants" \
                and parts[3] == "replans":
            rid = unquote(parts[2])
            require(0 < len(rid) <= 64)
            uid = self.authenticate(headers)
            key = headers.get("Idempotency-Key", "")
            receipt = self.receipt_for(state, uid, method, path, key, body)
            if receipt is not None:
                return 200, receipt["response"]
            response = self.preview_replan(state, rid, body, uid)
            self.save_receipt(state, uid, method, path, key, body, response)
            return 201, response
        if method == "POST" and len(parts) == 6 and parts[1] == "restaurants" \
                and parts[3] == "replans" and parts[5] == "apply":
            rid = unquote(parts[2])
            plan_id = unquote(parts[4])
            require(0 < len(rid) <= 64 and 0 < len(plan_id) <= 64)
            uid = self.authenticate(headers)
            key = headers.get("Idempotency-Key", "")
            receipt = self.receipt_for(state, uid, method, path, key, body)
            if receipt is not None:
                return 200, receipt["response"]
            now = datetime.now(UTC)
            response = self.apply_replan(state, rid, plan_id, body, uid, now)
            self.save_receipt(state, uid, method, path, key, body, response)
            return 201, response
        if method == "POST" and len(parts) == 4 and parts[1] == "restaurants" and parts[3] == "policies":
            rid = unquote(parts[2])
            require(0 < len(rid) <= 64)
            uid = self.authenticate(headers)
            config = reservations.restaurant(state, rid)
            require(uid in config["manager_user_ids"], 403, "forbidden")
            key = headers.get("Idempotency-Key", "")
            require(key != "", 400, "missing_idempotency_key")
            require(len(key) <= 255)
            receipt = next((item for item in state["receipts"] if item["user_id"] == uid
                            and item["path"] == path and item["method"] == method
                            and item["key"] == key), None)
            if receipt:
                require(json_equal(body, receipt["request"]), 409, "idempotency_key_reuse")
                return 200, receipt["response"]
            validated = policies.validate(config, body)
            version = config["policy_version"] + 1
            record = {**validated, "policy_version": version}
            response = deepcopy(record)
            config["policies"].append(record)
            config["policy_version"] = version
            state["receipts"].append({
                "user_id": uid, "method": method, "path": path, "key": key,
                "request": deepcopy(body), "response": deepcopy(response),
            })
            return 201, response
        uid = self.authenticate(headers)
        if method == "POST" and path in ("/reservations", "/reservation-moves", "/series"):
            key = headers.get("Idempotency-Key", "")
            require(key != "", 400, "missing_idempotency_key")
            require(len(key) <= 255)
            receipt = next((r for r in state["receipts"] if r["user_id"] == uid
                            and r["path"] == path and r["method"] == method and r["key"] == key), None)
            if receipt:
                require(json_equal(body, receipt["request"]), 409, "idempotency_key_reuse")
                return 200, receipt["response"]
            now = datetime.now(UTC)
            if path == "/reservations":
                record = reservations.candidate(state, body)
                reservations.check_occupancy(state, [record])
                reference = secrets.token_hex(5).upper()
                references = {r["reference"] for r in state["reservations"].values()}
                while reference in references:
                    reference = secrets.token_hex(5).upper()
                record.update(reservation_id=uuid4().hex, reference=reference, user_id=uid,
                              status="confirmed", created_at=now.isoformat(), revision=1,
                              accepted_terms=policies.accepted(
                                  state["restaurants"][record["restaurant_id"]],
                                  record["starts_at_local"][:10]))
                record["history"] = [transfer.history_entry(
                    1, record["created_at"], "created",
                    transfer.create_changes(record), 1, record["accepted_terms"])]
                proposed = [record]
                response = reservations.public(record)
            elif path == "/reservation-moves":
                proposed = reservations.moves(state, body, uid, now)
                self.commit_moves(state, proposed, now)
                response = {"reservations": [
                    reservations.public(state["reservations"][record["reservation_id"]])
                    for record in proposed]}
            else:
                response = self.create_series(state, body, uid, now)
                proposed = []
            receipt = {"user_id": uid, "method": method, "path": path, "key": key,
                       "request": deepcopy(body), "response": deepcopy(response)}
            if path == "/reservations":
                for record in proposed:
                    state["reservations"][record["reservation_id"]] = record
            state["receipts"].append(receipt)
            return 201, response
        if method == "GET" and path == "/reservations":
            records = [r for r in state["reservations"].values() if r["user_id"] == uid]
            records.sort(key=lambda r: instant(r["starts_at"]), reverse=True)
            return 200, {"reservations": [reservations.public(r) for r in records]}
        parts = path.split("/")
        if len(parts) in (3, 4) and parts[1] == "reservations":
            record = reservations.owned(state, unquote(parts[2]), uid)
            if method == "GET" and len(parts) == 3:
                return 200, reservations.public(record)
            if method == "POST" and len(parts) == 4 and parts[3] == "cancel":
                if record["status"] != "cancelled":
                    now = datetime.now(UTC)
                    reservations.check_cutoff(state, record, now)
                    record = {**record, "status": "cancelled",
                              "revision": record.get("revision", 1) + 1}
                    self.append_history(record, "cancelled", [], now)
                    state["reservations"][record["reservation_id"]] = record
                    series, _ = self.series_for_reservation(
                        state, record["reservation_id"])
                    if series is not None:
                        series["revision"] += 1
                return 200, reservations.public(record)
            if method == "PATCH" and len(parts) == 3:
                now = datetime.now(UTC)
                proposed = reservations.amendment(state, record, body, now)
                changed = self.reservation_changes(record, proposed)
                if not changed:
                    return 200, reservations.public(record)
                reservations.check_occupancy(state, [proposed], {record["reservation_id"]})
                proposed["revision"] = record.get("revision", 1) + 1
                proposed["accepted_terms"] = policies.accepted(
                    state["restaurants"][record["restaurant_id"]],
                    proposed["starts_at_local"][:10])
                proposed["history"] = deepcopy(record.get("history", []))
                self.append_history(proposed, "changed", changed, now)
                state["reservations"][record["reservation_id"]] = proposed
                series, occurrence = self.series_for_reservation(
                    state, record["reservation_id"])
                if series is not None:
                    occurrence["exception"] = True
                    series["revision"] += 1
                return 200, reservations.public(proposed)
        require(False, 404, "not_found")
