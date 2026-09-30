"""Policy-aware atomic collective-move contracts."""
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


class CollectiveMoveContracts(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        data = fixture()
        data["restaurants"][0]["tables"].append(
            {"id": "t3", "label": "Window", "capacity": 3})
        data["restaurants"][0]["combinable"] = [["t1", "t2"], ["t2", "t3"]]
        data["restaurants"][0]["manager_user_ids"] = ["u1"]
        self.store.dispatch("POST", "/_test/reset", {}, data, {})
        self.owner = self.login("a@example.test")
        self.other = self.login("b@example.test")

    def login(self, email):
        return self.store.dispatch("POST", "/auth/login", {}, {
            "email": email, "password": "synthetic password",
        }, {})[1]["token"]

    def call(self, method, path, body=None, token=None, key=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.store.dispatch(method, path, {}, body or {}, headers)

    def booking(self, key, **changes):
        body = {"restaurant_id": "r1", "table_id": "t1",
                "starts_at_local": "2035-09-24T18:00", "party_size": 2}
        body.update(changes)
        if "table_ids" in changes:
            body.pop("table_id")
        return self.call("POST", "/reservations", body, self.owner, key)

    def adopt(self, anchor, key="series", count=3):
        return self.call("POST", "/series", {
            "anchor_reference": anchor["reference"], "count": count,
            "interval_weeks": 1,
        }, self.owner, key)

    def batch(self, moves, key="move", token=None):
        return self.call("POST", "/reservation-moves", {"moves": moves},
                         token or self.owner, key)

    def assert_error(self, moves, key, status, code):
        with self.assertRaises(APIError) as caught:
            self.batch(moves, key)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_mixed_change_noop_pair_history_and_series_revision_once(self):
        _, anchor = self.booking("anchor")
        _, series = self.adopt(anchor)
        occurrences = series["occurrences"]
        pair_status, pair = self.booking(
            "pair", table_ids=["t2", "t1"], party_size=5,
            starts_at_local="2035-09-24T20:00")
        self.assertEqual(pair_status, 201)
        _, no_op_booking = self.booking(
            "no-op", table_id="t3", starts_at_local="2035-09-24T21:30")
        before_pair_history = self.call(
            "GET", f"/reservations/{pair['reference']}/history",
            token=self.owner)[1]["entries"]
        before_no_op_history = self.call(
            "GET", f"/reservations/{no_op_booking['reference']}/history",
            token=self.owner)[1]["entries"]
        response_status, response = self.batch([
            {"reference": occurrences[1]["reference"],
             "starts_at_local": "2035-10-01T19:30", "expected_revision": 1},
            {"reference": occurrences[2]["reference"],
             "starts_at_local": "2035-10-08T19:30", "expected_revision": 1},
            {"reference": pair["reference"], "table_ids": ["t3", "t2"],
             "expected_revision": 1},
            {"reference": no_op_booking["reference"], "party_size": 2,
             "expected_revision": 1},
        ])
        self.assertEqual(response_status, 201)
        first, second, pair_after, unchanged = response["reservations"]
        self.assertEqual(first["revision"], 2)
        self.assertEqual(first["starts_at_local"], "2035-10-01T19:30")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["starts_at_local"], "2035-10-08T19:30")
        self.assertEqual(unchanged, no_op_booking)
        self.assertEqual(pair_after["table_ids"], ["t2", "t3"])
        self.assertEqual(pair_after["revision"], 2)
        self.assertEqual(self.call(
            "GET", f"/reservations/{pair['reference']}/history",
            token=self.owner)[1]["entries"][-1]["changes"], [{
                "field": "table_ids", "from": ["t1", "t2"], "to": ["t2", "t3"],
            }])
        self.assertEqual(self.call(
            "GET", f"/reservations/{pair['reference']}/history",
            token=self.owner)[1]["entries"][:-1], before_pair_history)
        self.assertEqual(self.call(
            "GET", f"/reservations/{no_op_booking['reference']}/history",
            token=self.owner)[1]["entries"], before_no_op_history)

        current_series = self.call("GET", f"/series/{series['series_id']}",
                                   token=self.owner)[1]
        self.assertEqual(current_series["revision"], 2)
        self.assertTrue(current_series["occurrences"][1]["exception"])
        self.assertTrue(current_series["occurrences"][2]["exception"])
        self.assertEqual(self.store.state["restaurants"]["r1"]["revision"], 2)
        changed_history = self.call(
            "GET", f"/reservations/{first['reference']}/history",
            token=self.owner)[1]["entries"]
        self.assertEqual([item["event"] for item in changed_history],
                         ["created", "changed"])
        self.assertEqual(changed_history[-1]["revision"], 2)
        self.assertEqual(response["reservations"][0]["accepted_terms"],
                         changed_history[-1]["accepted_terms"])
        exported = self.call("GET", "/_test/export")[1]
        destination = Store()
        destination.dispatch("POST", "/_test/import", {}, exported, {})
        self.assertEqual(destination.dispatch(
            "GET", f"/series/{series['series_id']}", {}, {},
            {"Authorization": "Bearer " + self.owner})[1], current_series)
        self.assertEqual(destination.dispatch(
            "POST", "/reservation-moves", {}, {"moves": [
                {"reference": occurrences[1]["reference"],
                 "starts_at_local": "2035-10-01T19:30", "expected_revision": 1},
                {"reference": occurrences[2]["reference"],
                 "starts_at_local": "2035-10-08T19:30", "expected_revision": 1},
                {"reference": pair["reference"], "table_ids": ["t3", "t2"],
                 "expected_revision": 1},
                {"reference": no_op_booking["reference"], "party_size": 2,
                 "expected_revision": 1},
            ]}, {"Authorization": "Bearer " + self.owner,
                 "Idempotency-Key": "move"}), (200, response))

    def test_policy_result_date_expected_revision_and_old_cutoff_precedence(self):
        _, first = self.booking("first")
        _, second = self.booking("second", table_id="t2")
        self.store.dispatch("POST", "/restaurants/r1/policies", {}, policy(
            effective_from="2035-09-24", slot_minutes=30,
            reservation_duration_minutes=60, cancellation_cutoff_minutes=60,
            capacities={"t1": 6, "t2": 6, "t3": 3}), {
                "Authorization": "Bearer " + self.owner,
                "Idempotency-Key": "october-policy",
            })
        _, series = self.adopt(first, key="first-series", count=2)
        # Keep the second booking out of the policy-date target interval.
        start = datetime.fromisoformat(first["starts_at"])
        with patch("app.store.datetime") as clock:
            clock.now.return_value = start - timedelta(minutes=121)
            before = copy.deepcopy(self.store.state)
            self.assert_error([
                {"reference": first["reference"], "expected_revision": 1,
                 "starts_at_local": "2035-09-24T19:30"},
                {"reference": second["reference"], "expected_revision": 9,
                 "party_size": 100},
            ], "stale", 409, "stale_revision")
            self.assertEqual(self.store.state, before)
            self.assert_error([
                {"reference": first["reference"], "expected_revision": True},
            ], "invalid-revision", 422, "validation_failed")
            clock.now.return_value = start - timedelta(minutes=120)
            self.assert_error([
                {"reference": first["reference"], "expected_revision": 1,
                 "party_size": 100,
                 "starts_at_local": "2035-10-01T19:30"},
            ], "cutoff", 409, "cutoff_passed")
        self.assertEqual(self.store.state["restaurants"]["r1"]["revision"], 1)
        self.assertEqual(self.store.state["series"][series["series_id"]]["revision"], 1)
        response = self.batch([{
            "reference": series["occurrences"][1]["reference"],
            "starts_at_local": "2035-10-01T19:30",
            "party_size": 5, "expected_revision": 1,
        }], "policy-move")
        moved = response[1]["reservations"][0]
        self.assertEqual(moved["accepted_terms"]["policy_version"], 1)
        self.assertEqual(moved["ends_at"], "2035-10-01T20:30:00+02:00")
        history = self.call("GET", f"/reservations/{moved['reference']}/history",
                            token=self.owner)[1]["entries"]
        self.assertEqual(history[-1]["accepted_terms"], moved["accepted_terms"])
        self.assertEqual(self.store.state["restaurants"]["r1"]["revision"], 2)
        self.assertEqual(self.store.state["series"][series["series_id"]]["revision"], 2)

    def test_failure_rolls_back_all_state_receipt_and_series_exception(self):
        _, anchor = self.booking("anchor")
        _, series = self.adopt(anchor, count=3)
        occurrence = series["occurrences"][1]["reservation"]
        _, blocker = self.booking("blocker", table_id="t2",
                                  starts_at_local="2035-10-01T19:30")
        snapshot = copy.deepcopy(self.store.state)
        moves = [
            {"reference": occurrence["reference"], "table_id": "t2",
             "starts_at_local": "2035-10-01T19:30", "expected_revision": 1},
            {"reference": blocker["reference"], "party_size": 3,
             "expected_revision": 1},
        ]
        self.assert_error(moves, "retry-after-failure", 409, "table_unavailable")
        self.assertEqual(self.store.state, snapshot)
        self.call("PATCH", f"/reservations/{blocker['reference']}",
                  {"table_id": "t3"}, self.owner)
        moves[1]["expected_revision"] = 2
        moved = self.batch(moves, "retry-after-failure")
        self.assertEqual(moved[0], 201)
        current_series = self.call("GET", f"/series/{series['series_id']}",
                                   token=self.owner)[1]
        self.assertEqual(current_series["revision"], 2)
        self.assertTrue(current_series["occurrences"][1]["exception"])
        self.assertEqual(self.store.state["restaurants"]["r1"]["revision"], 2)

        receipt_state = copy.deepcopy(self.store.state)
        self.assertEqual(self.batch(moves, "retry-after-failure"),
                         (200, moved[1]))
        self.assertEqual(self.store.state, receipt_state)

    def test_replay_and_concurrent_expected_revision_writes(self):
        _, booking = self.booking("booking", table_id="t2", party_size=2)
        moves = [{"reference": booking["reference"], "party_size": 3,
                  "expected_revision": 1}]
        before_replay = copy.deepcopy(self.store.state)
        status, original = self.batch(moves, "replay")
        self.assertEqual(status, 201)
        saved = copy.deepcopy(self.store.state)
        self.assertEqual(self.batch(moves, "replay"), (200, original))
        self.assertEqual(self.store.state, saved)
        self.assertNotEqual(saved, before_replay)

        def update(size):
            try:
                return self.batch([{
                    "reference": booking["reference"],
                    "party_size": size,
                    "expected_revision": 2,
                }], f"concurrent-{size}")
            except APIError as error:
                if (error.status, error.code) == (409, "stale_revision"):
                    return error.code
                raise
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, (4, 5)))
        self.assertEqual(sum(type(result) is tuple for result in results), 1)
        self.assertEqual(results.count("stale_revision"), 1)
        current = self.call("GET", f"/reservations/{booking['reference']}",
                            token=self.owner)[1]
        self.assertEqual(current["revision"], 3)
        history = self.call("GET", f"/reservations/{booking['reference']}/history",
                            token=self.owner)[1]["entries"]
        self.assertEqual([entry["seq"] for entry in history], [1, 2, 3])

        exported = self.call("GET", "/_test/export")[1]
        destination = Store()
        destination.dispatch("POST", "/_test/import", {}, exported, {})
        before = copy.deepcopy(destination.state)
        replay = destination.dispatch("POST", "/reservation-moves", {}, {
            "moves": moves,
        }, {"Authorization": "Bearer " + self.owner,
            "Idempotency-Key": "replay"})
        self.assertEqual(replay, (200, original))
        self.assertEqual(destination.state, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
