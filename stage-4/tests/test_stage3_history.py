"""Reservation accepted-terms, revision, history and decision contracts."""
import concurrent.futures
import copy
from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.store import Store
from app.time_rules import UTC
from app.validation import APIError
from contract_stage1 import fixture
from test_stage3_policies import policy


class ReservationHistoryContracts(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        data = fixture()
        data["restaurants"][0]["tables"].append(
            {"id": "t3", "label": "Window", "capacity": 3})
        data["restaurants"][0]["combinable"] = [["t1", "t2"], ["t2", "t3"]]
        data["restaurants"][0]["manager_user_ids"] = ["u1"]
        self.store.dispatch("POST", "/_test/reset", {}, data, {})
        _, first = self.store.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password"
        }, {})
        _, second = self.store.dispatch("POST", "/auth/login", {}, {
            "email": "b@example.test", "password": "synthetic password"
        }, {})
        self.owner = {"Authorization": "Bearer " + first["token"]}
        self.other = {"Authorization": "Bearer " + second["token"]}

    def call(self, method, path, body=None, headers=None):
        return self.store.dispatch(method, path, {}, body or {}, headers or {})

    def booking(self, key="book", **changes):
        body = {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2035-09-24T18:00", "party_size": 2,
        }
        body.update(changes)
        if "table_ids" in changes and "table_id" not in changes:
            body.pop("table_id")
        return self.call("POST", "/reservations", body,
                         {**self.owner, "Idempotency-Key": key})

    def history(self, reference, headers=None):
        return self.call("GET", f"/reservations/{reference}/history",
                         headers=headers)[1]["entries"]

    def publish(self, body, key):
        return self.call("POST", "/restaurants/r1/policies", body,
                         {**self.owner, "Idempotency-Key": key})[1]

    def assert_api_error(self, method, path, body, headers, status, code):
        with self.assertRaises(APIError) as caught:
            self.store.dispatch(method, path, {}, body, headers)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_create_policy_terms_history_lookup_privacy_and_cancel(self):
        self.publish(policy(effective_from="2035-09-24", slot_minutes=15,
                            reservation_duration_minutes=60,
                            capacities={"t1": 4, "t2": 6, "t3": 3}), "published")
        status, created = self.booking()
        self.assertEqual(status, 201)
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["accepted_terms"], {
            "policy_version": 1, "slot_minutes": 15,
            "reservation_duration_minutes": 60, "cancellation_cutoff_minutes": 60,
            "opening_hours": policy(effective_from="2035-09-24")["opening_hours"],
            "capacities": {"t1": 4, "t2": 6, "t3": 3},
        })
        entries = self.history(created["reference"], self.owner)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "created")
        self.assertEqual(entries[0]["seq"], 1)
        self.assertEqual([change["field"] for change in entries[0]["changes"]],
                         ["table_id", "starts_at_local", "party_size"])
        self.assertEqual(entries[0]["accepted_terms"], created["accepted_terms"])
        self.assertEqual(self.call(
            "GET", f"/reservations/{created['reference']}/decision", headers=self.owner)[1],
            {"reference": created["reference"], "revision": 1,
             "accepted_terms": created["accepted_terms"]})
        for headers in ({}, self.other):
            for suffix in ("history", "decision"):
                self.assert_api_error("GET", f"/reservations/{created['reference']}/{suffix}",
                                      {}, headers, 404, "not_found")

        self.publish(policy(effective_from="2035-09-24",
                            cancellation_cutoff_minutes=10080,
                            capacities={"t1": 4, "t2": 6, "t3": 3}), "new-cutoff")
        boundary = datetime.fromisoformat(created["starts_at"]) - timedelta(hours=2)
        with patch("app.store.datetime") as clock:
            clock.now.return_value = boundary
            cancelled = self.call("POST", f"/reservations/{created['reference']}/cancel",
                                  {}, self.owner)[1]
        self.assertEqual(cancelled["revision"], 2)
        self.assertEqual(cancelled["accepted_terms"], created["accepted_terms"])
        cancel_history = self.history(created["reference"], self.owner)
        self.assertEqual([entry["event"] for entry in cancel_history], ["created", "cancelled"])
        self.assertEqual(cancel_history[-1]["changes"], [])
        self.assertEqual(cancel_history[-1]["revision"], 2)
        self.assertEqual(self.call(
            "POST", f"/reservations/{created['reference']}/cancel", {}, self.owner)[1],
            cancelled)
        self.assertEqual(len(self.history(created["reference"], self.owner)), 2)
        self.assertEqual(self.call(
            "GET", f"/reservations/{created['reference']}/decision", headers=self.owner)[1]["revision"], 2)
        self.assert_api_error("GET", f"/reservations/NOBOOK/history", {}, {},
                              404, "not_found")

    def test_patch_change_noop_pair_normalization_and_immutable_history(self):
        _, single = self.booking(table_id="t2")
        created_history = self.history(single["reference"], self.owner)
        no_op = self.call("PATCH", f"/reservations/{single['reference']}", {
            "party_size": 2, "expected_revision": 1,
        }, self.owner)[1]
        self.assertEqual(no_op, single)
        self.assertEqual(self.history(single["reference"], self.owner), created_history)
        self.publish(policy(effective_from="2035-09-24", capacities={
            "t1": 1, "t2": 1, "t3": 1}), "restrict-existing")
        still_no_op = self.call("PATCH", f"/reservations/{single['reference']}", {
            "party_size": 2, "expected_revision": 1,
        }, self.owner)[1]
        self.assertEqual(still_no_op, single)
        self.assertEqual(self.history(single["reference"], self.owner), created_history)
        self.publish(policy(effective_from="2035-09-24", capacities={
            "t1": 3, "t2": 5, "t3": 3}), "restore-capacity")

        changed = self.call("PATCH", f"/reservations/{single['reference']}", {
            "party_size": 3, "expected_revision": 1,
        }, self.owner)[1]
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(changed["party_size"], 3)
        single_history = self.history(single["reference"], self.owner)
        self.assertEqual([entry["seq"] for entry in single_history], [1, 2])
        self.assertEqual(single_history[1]["changes"], [
            {"field": "party_size", "from": 2, "to": 3}
        ])
        self.assertEqual(single_history[0]["accepted_terms"]["policy_version"], 0)
        self.publish(policy(effective_from="2035-09-25", reservation_duration_minutes=60,
                            capacities={"t1": 5, "t2": 5, "t3": 3}), "future")
        moved = self.call("PATCH", f"/reservations/{single['reference']}", {
            "starts_at_local": "2035-09-26T18:00", "expected_revision": 2,
        }, self.owner)[1]
        self.assertEqual(moved["revision"], 3)
        self.assertEqual(moved["accepted_terms"]["policy_version"], 3)
        self.assertEqual(moved["ends_at"], "2035-09-26T19:00:00+02:00")
        self.assertEqual(single_history[0]["accepted_terms"]["policy_version"], 0)
        self.assertEqual(single_history[1]["accepted_terms"]["policy_version"], 2)
        self.assertEqual([change["field"] for change in
                          self.history(single["reference"], self.owner)[-1]["changes"]],
                         ["starts_at_local"])

        _, pair = self.booking("pair", table_ids=["t2", "t1"], party_size=5,
                               starts_at_local="2035-09-24T19:30")
        self.assertEqual(pair["table_ids"], ["t1", "t2"])
        pair_history = self.history(pair["reference"], self.owner)
        self.assertEqual(pair_history[0]["changes"][0],
                         {"field": "table_ids", "from": None, "to": ["t1", "t2"]})
        reversed_noop = self.call("PATCH", f"/reservations/{pair['reference']}", {
            "table_ids": ["t2", "t1"], "expected_revision": 1,
        }, self.owner)[1]
        self.assertEqual(reversed_noop["revision"], 1)
        self.assertEqual(self.history(pair["reference"], self.owner), pair_history)
        pair_changed = self.call("PATCH", f"/reservations/{pair['reference']}", {
            "table_ids": ["t2", "t3"], "starts_at_local": "2035-09-24T21:00",
            "expected_revision": 1,
        }, self.owner)[1]
        self.assertEqual(pair_changed["table_ids"], ["t2", "t3"])
        self.assertEqual(self.history(pair["reference"], self.owner)[-1]["changes"], [
            {"field": "table_ids", "from": ["t1", "t2"], "to": ["t2", "t3"]},
            {"field": "starts_at_local", "from": "2035-09-24T19:30",
             "to": "2035-09-24T21:00"},
        ])

    def test_expected_revision_precedes_cutoff_validation_and_writes_are_serial(self):
        _, created = self.booking(table_id="t2")
        start = datetime.fromisoformat(created["starts_at"])
        cutoff = start - timedelta(minutes=120)
        with patch("app.store.datetime") as clock:
            clock.now.return_value = cutoff
            self.assert_api_error("PATCH", f"/reservations/{created['reference']}", {
                "expected_revision": 9, "party_size": 0,
            }, self.owner, 409, "stale_revision")
            self.assert_api_error("PATCH", f"/reservations/{created['reference']}", {
                "expected_revision": True,
            }, self.owner, 422, "validation_failed")
            self.assert_api_error("PATCH", f"/reservations/{created['reference']}", {
                "expected_revision": 1, "party_size": 0,
            }, self.owner, 409, "cutoff_passed")
            clock.now.return_value = cutoff - timedelta(seconds=1)
            results = []
            def update(party_size):
                try:
                    return self.call("PATCH", f"/reservations/{created['reference']}", {
                        "expected_revision": 1, "party_size": party_size,
                    }, self.owner)[1]
                except APIError as error:
                    if (error.status, error.code) == (409, "stale_revision"):
                        return error.code
                    raise
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(update, (3, 4)))
            current_record = self.store.state["reservations"][created["reservation_id"]]
            next_party_size = 4 if current_record["party_size"] == 3 else 3
            self.call("PATCH", f"/reservations/{created['reference']}", {
                "expected_revision": 2, "party_size": next_party_size,
            }, self.owner)
        self.assertEqual(sum(type(result) is dict for result in results), 1)
        self.assertEqual(results.count("stale_revision"), 1)
        current = self.call("GET", f"/reservations/{created['reference']}", headers=self.owner)[1]
        self.assertEqual(current["revision"], 3)
        history = self.history(created["reference"], self.owner)
        self.assertEqual([entry["seq"] for entry in history], [1, 2, 3])
        self.assertEqual(history[1]["at"], history[2]["at"])

    def test_idempotent_booking_replay_and_legacy_import_terms(self):
        body = {"restaurant_id": "r1", "table_id": "t1",
                "starts_at_local": "2035-09-24T18:00", "party_size": 2}
        status, first = self.call("POST", "/reservations", body, {
            **self.owner, "Idempotency-Key": "receipt"})
        self.assertEqual(status, 201)
        original_history = copy.deepcopy(self.history(first["reference"], self.owner))
        replay = self.call("POST", "/reservations", body, {
            **self.owner, "Idempotency-Key": "receipt"})
        self.assertEqual(replay, (200, first))
        self.assertEqual(self.history(first["reference"], self.owner), original_history)

        exported = self.call("GET", "/_test/export")[1]
        old = copy.deepcopy(exported)
        restaurant = old["state"]["restaurants"]["r1"]
        for name in ("manager_user_ids", "policies", "policy_version"):
            restaurant.pop(name, None)
        record = old["state"]["reservations"][first["reservation_id"]]
        for name in ("revision", "accepted_terms", "history"):
            record.pop(name, None)
        receipt = next(item for item in old["state"]["receipts"]
                       if item["key"] == "receipt")
        for name in ("revision", "accepted_terms"):
            receipt["response"].pop(name, None)
        restored = Store()
        restored.dispatch("POST", "/_test/import", {}, old, {})
        imported = restored.dispatch(
            "GET", f"/reservations/{first['reference']}", {}, {},
            {"Authorization": "Bearer " + self.owner["Authorization"].split()[-1]})[1]
        self.assertEqual(imported["revision"], 1)
        self.assertEqual(imported["accepted_terms"]["policy_version"], 0)
        self.assertEqual(len(restored.dispatch(
            "GET", f"/reservations/{first['reference']}/history", {}, {},
            self.owner)[1]["entries"]), 1)
        self.assertEqual(restored.dispatch("GET", "/_test/export", {}, {}, {})[1]["state"]
                         ["reservations"][first["reservation_id"]]["accepted_terms"]["policy_version"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
