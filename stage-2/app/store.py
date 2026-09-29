"""One process-wide state owner and linearization point for every operation."""
from copy import deepcopy
from datetime import datetime
import re
import secrets
from threading import RLock
from uuid import uuid4
from urllib.parse import unquote

from . import reservations, security, transfer
from .time_rules import UTC, instant
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
                return 200, reservations.restaurant(state, rid)
            if path == "/availability":
                return 200, reservations.availability(state, query)
        uid = self.authenticate(headers)
        if method == "POST" and path in ("/reservations", "/reservation-moves"):
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
                              status="confirmed", created_at=now.isoformat())
                proposed = [record]
                response = reservations.public(record)
            else:
                proposed = reservations.moves(state, body, uid, now)
                response = {"reservations": [reservations.public(r) for r in proposed]}
            receipt = {"user_id": uid, "method": method, "path": path, "key": key,
                       "request": deepcopy(body), "response": deepcopy(response)}
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
                    reservations.check_cutoff(state, record, datetime.now(UTC))
                    record = {**record, "status": "cancelled"}
                    state["reservations"][record["reservation_id"]] = record
                return 200, reservations.public(record)
            if method == "PATCH" and len(parts) == 3:
                proposed = reservations.amendment(state, record, body, datetime.now(UTC))
                reservations.check_occupancy(state, [proposed], {record["reservation_id"]})
                state["reservations"][record["reservation_id"]] = proposed
                return 200, reservations.public(proposed)
        require(False, 404, "not_found")
