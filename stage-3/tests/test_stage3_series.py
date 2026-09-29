"""Recurring reservation adoption and occurrence lifecycle contracts."""
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


class SeriesContracts(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        data = fixture()
        data["restaurants"][0]["combinable"] = [["t1", "t2"]]
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

    def book(self, token=None, key="anchor", **changes):
        body = {"restaurant_id": "r1", "table_id": "t1",
                "starts_at_local": "2035-09-24T18:00", "party_size": 2}
        body.update(changes)
        if "table_ids" in changes:
            body.pop("table_id")
        return self.call("POST", "/reservations", body, token or self.owner, key)[1]

    def create_series(self, anchor, token=None, key="series", **changes):
        body = {"anchor_reference": anchor, "count": 3, "interval_weeks": 1}
        body.update(changes)
        return self.call("POST", "/series", body, token or self.owner, key)

    def assert_error(self, method, path, body, token, key, status, code):
        with self.assertRaises(APIError) as caught:
            self.call(method, path, body, token, key)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_adoption_replay_owner_view_and_occurrence_lifecycle(self):
        anchor = self.book()
        anchor_before = copy.deepcopy(
            self.store.state["reservations"][anchor["reservation_id"]])
        booking_receipt_before = copy.deepcopy(next(
            receipt for receipt in self.store.state["receipts"]
            if receipt["path"] == "/reservations"))
        status, original = self.create_series(
            anchor["reference"], key="recurring", ignored="unknown")
        self.assertEqual(status, 201)
        self.assertEqual(original["revision"], 1)
        self.assertEqual(original["interval_weeks"], 1)
        self.assertEqual([entry["index"] for entry in original["occurrences"]], [0, 1, 2])
        self.assertEqual([entry["reference"] for entry in original["occurrences"]][0],
                         anchor["reference"])
        self.assertEqual(len({entry["reference"] for entry in original["occurrences"]}), 3)
        self.assertEqual([entry["reservation"]["starts_at_local"]
                          for entry in original["occurrences"]],
                         ["2035-09-24T18:00", "2035-10-01T18:00",
                          "2035-10-08T18:00"])
        self.assertEqual(self.store.state["reservations"][anchor["reservation_id"]],
                         anchor_before)
        self.assertEqual(next(receipt for receipt in self.store.state["receipts"]
                              if receipt["path"] == "/reservations"),
                         booking_receipt_before)
        self.assertEqual(self.store.state["restaurants"]["r1"]["revision"], 1)
        series_id = original["series_id"]
        self.assertEqual(self.call("GET", f"/series/{series_id}", token=self.owner),
                         (200, original))
        for token in (None, self.other):
            self.assert_error("GET", f"/series/{series_id}", {}, token, None,
                              404, "not_found")

        first_ref = original["occurrences"][1]["reference"]
        changed = self.call("PATCH", f"/reservations/{first_ref}",
                            {"party_size": 1, "expected_revision": 1},
                            self.owner)[1]
        self.assertEqual(changed["revision"], 2)
        current = self.call("GET", f"/series/{series_id}", token=self.owner)[1]
        self.assertEqual(current["revision"], 2)
        self.assertTrue(current["occurrences"][1]["exception"])
        self.assertFalse(current["occurrences"][0]["exception"])
        self.assertEqual(current["occurrences"][2]["reservation"]["status"], "confirmed")

        no_op = self.call("PATCH", f"/reservations/{first_ref}",
                          {"party_size": 1}, self.owner)[1]
        self.assertEqual(no_op, changed)
        self.assertEqual(self.call("GET", f"/series/{series_id}",
                                   token=self.owner)[1]["revision"], 2)
        cancelled = self.call("POST", f"/reservations/{first_ref}/cancel",
                              {}, self.owner)[1]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["revision"], 3)
        cancelled_series = self.call("GET", f"/series/{series_id}",
                                     token=self.owner)[1]
        self.assertEqual(cancelled_series["revision"], 3)
        self.assertTrue(cancelled_series["occurrences"][1]["exception"])
        self.assertEqual(self.call("POST", f"/reservations/{first_ref}/cancel",
                                   {}, self.owner)[1], cancelled)
        self.assertEqual(self.call("GET", f"/series/{series_id}",
                                   token=self.owner)[1]["revision"], 3)

        self.call("POST", f"/reservations/{anchor['reference']}/cancel",
                  {}, self.owner)
        current = self.call("GET", f"/series/{series_id}", token=self.owner)[1]
        self.assertEqual(current["revision"], 4)
        self.assertEqual(current["occurrences"][0]["reservation"]["status"], "cancelled")
        self.assertEqual(current["occurrences"][2]["reservation"]["status"], "confirmed")
        before_replay = copy.deepcopy(self.store.state)
        self.assertEqual(self.create_series(
            anchor["reference"], key="recurring", ignored="unknown"), (200, original))
        self.assertEqual(self.store.state, before_replay)
        envelope = copy.deepcopy(self.call("GET", "/_test/export")[1])
        restored = Store()
        restored.dispatch("POST", "/_test/import", {}, envelope, {})
        self.assertEqual(restored.dispatch(
            "GET", f"/series/{series_id}", {}, {},
            {"Authorization": "Bearer " + self.owner})[1], current)
        imported_before = copy.deepcopy(restored.state)
        replay_after_import = restored.dispatch("POST", "/series", {}, {
            "anchor_reference": anchor["reference"], "count": 3,
            "interval_weeks": 1, "ignored": "unknown",
        }, {"Authorization": "Bearer " + self.owner,
            "Idempotency-Key": "recurring"})
        self.assertEqual(replay_after_import, (200, original))
        self.assertEqual(restored.state, imported_before)

    def test_validation_ownership_cutoff_cancel_and_adoption_errors_are_atomic(self):
        anchor = self.book()
        self.assert_error("POST", "/series", {"anchor_reference": anchor["reference"],
                                              "count": 2, "interval_weeks": 1},
                          None, "missing-auth", 401, "unauthenticated")
        self.assert_error("POST", "/series", {"anchor_reference": "MISSING",
                                              "count": 2, "interval_weeks": 1},
                          self.owner, "unknown", 404, "not_found")
        self.assert_error("POST", "/series", {"anchor_reference": anchor["reference"],
                                              "count": 2, "interval_weeks": 1},
                          self.other, "other-owner", 404, "not_found")
        before = copy.deepcopy(self.store.state)
        for name, value in (("count", True), ("count", 1), ("count", 13),
                            ("count", "2"), ("interval_weeks", False),
                            ("interval_weeks", 0), ("interval_weeks", 5),
                            ("interval_weeks", 1.0)):
            body = {"anchor_reference": anchor["reference"], "count": 2,
                    "interval_weeks": 1, name: value}
            self.assert_error("POST", "/series", body, self.owner,
                              f"bad-{name}-{value!r}", 422, "validation_failed")
            self.assertEqual(self.store.state, before)
        self.assert_error("POST", "/series", {
            "anchor_reference": anchor["reference"], "count": 2,
            "interval_weeks": 1,
        }, self.owner, None, 400, "missing_idempotency_key")

        self.call("POST", f"/reservations/{anchor['reference']}/cancel",
                  {}, self.owner)
        cancelled_state = copy.deepcopy(self.store.state)
        self.assert_error("POST", "/series", {"anchor_reference": anchor["reference"],
                                              "count": 2, "interval_weeks": 1},
                          self.owner, "cancelled", 409, "reservation_cancelled")
        self.assertEqual(self.store.state, cancelled_state)

        active = self.book(key="active")
        status, adopted = self.create_series(active["reference"], key="first")
        self.assertEqual(status, 201)
        adopted_state = copy.deepcopy(self.store.state)
        self.assert_error("POST", "/series", {"anchor_reference": active["reference"],
                                              "count": 2, "interval_weeks": 1},
                          self.owner, "already", 409, "already_in_series")
        self.assertEqual(self.store.state, adopted_state)
        self.assert_error("POST", "/series", {"anchor_reference": active["reference"],
                                              "count": 2, "interval_weeks": 1},
                          self.owner, "first", 409, "idempotency_key_reuse")
        self.assertEqual(self.store.state, adopted_state)

        cutoff_anchor = self.book(key="cutoff", starts_at_local="2035-09-25T18:00")
        starts_at = datetime.fromisoformat(cutoff_anchor["starts_at"])
        with patch("app.store.datetime") as clock:
            clock.now.return_value = starts_at - timedelta(minutes=120)
            before_cutoff = copy.deepcopy(self.store.state)
            self.assert_error("POST", "/series", {
                "anchor_reference": cutoff_anchor["reference"], "count": 2,
                "interval_weeks": 1,
            }, self.owner, "cutoff", 409, "cutoff_passed")
            self.assertEqual(self.store.state, before_cutoff)

    def test_policies_combination_capacity_and_conflicts_roll_back_everything(self):
        data = fixture()
        data["restaurants"][0]["combinable"] = [["t1", "t2"]]
        data["restaurants"][0]["manager_user_ids"] = ["u1"]
        self.store.dispatch("POST", "/_test/reset", {}, data, {})
        self.owner = self.login("a@example.test")
        self.other = self.login("b@example.test")
        self.call("POST", "/restaurants/r1/policies", policy(
            effective_from="2035-10-01", reservation_duration_minutes=120,
            capacities={"t1": 3, "t2": 3}),
            self.owner, "duration-policy")
        anchor = self.book(key="pair-anchor", table_ids=["t2", "t1"], party_size=6)
        original = copy.deepcopy(self.store.state["reservations"][anchor["reservation_id"]])
        status, series = self.create_series(anchor["reference"], key="policy-pair")
        self.assertEqual(status, 201)
        occurrences = [item["reservation"] for item in series["occurrences"]]
        self.assertEqual(occurrences[0]["table_ids"], ["t1", "t2"])
        self.assertEqual(occurrences[1]["accepted_terms"]["policy_version"], 1)
        self.assertEqual(occurrences[1]["ends_at"],
                         "2035-10-01T20:00:00+02:00")
        self.assertEqual(self.store.state["reservations"][anchor["reservation_id"]],
                         original)

        failing_store = Store()
        failure_fixture = fixture()
        failure_fixture["restaurants"][0]["combinable"] = [["t1", "t2"]]
        failure_fixture["restaurants"][0]["manager_user_ids"] = ["u1"]
        failing_store.dispatch("POST", "/_test/reset", {}, failure_fixture, {})
        owner = failing_store.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password",
        }, {})[1]["token"]
        headers = {"Authorization": "Bearer " + owner}
        failing_store.dispatch("POST", "/restaurants/r1/policies", {}, policy(
            effective_from="2035-10-01", capacities={"t1": 2, "t2": 2}), {
                **headers, "Idempotency-Key": "low-pair-capacity",
            })
        pair = failing_store.dispatch("POST", "/reservations", {}, {
            "restaurant_id": "r1", "table_ids": ["t1", "t2"],
            "starts_at_local": "2035-09-24T18:00", "party_size": 5,
        }, {**headers, "Idempotency-Key": "pair"})
        before = copy.deepcopy(failing_store.state)
        with self.assertRaises(APIError) as caught:
            failing_store.dispatch("POST", "/series", {}, {
                "anchor_reference": pair[1]["reference"], "count": 3,
                "interval_weeks": 1,
            }, {**headers, "Idempotency-Key": "future-capacity"})
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (422, "party_exceeds_capacity"))
        self.assertEqual(failing_store.state, before)
        failing_store.dispatch("POST", "/restaurants/r1/policies", {}, policy(
            effective_from="2035-10-01", capacities={"t1": 3, "t2": 3}), {
                **headers, "Idempotency-Key": "restored-pair-capacity",
            })
        retried = failing_store.dispatch("POST", "/series", {}, {
            "anchor_reference": pair[1]["reference"], "count": 3,
            "interval_weeks": 1,
        }, {**headers, "Idempotency-Key": "future-capacity"})
        self.assertEqual(retried[0], 201)
        self.assertEqual(retried[1]["occurrences"][1]["reservation"]
                         ["accepted_terms"]["policy_version"], 2)

        conflict_store = Store()
        conflict_store.dispatch("POST", "/_test/reset", {}, fixture(), {})
        conflict_owner = conflict_store.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password",
        }, {})[1]["token"]
        conflict_headers = {"Authorization": "Bearer " + conflict_owner}
        occupied = conflict_store.dispatch("POST", "/reservations", {}, {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2035-10-01T18:00", "party_size": 2,
        }, {**conflict_headers, "Idempotency-Key": "occupied"})[1]
        blocked = conflict_store.dispatch("POST", "/reservations", {}, {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2035-09-24T18:00", "party_size": 2,
        }, {**conflict_headers, "Idempotency-Key": "blocked-anchor"})[1]
        before = copy.deepcopy(conflict_store.state)
        with self.assertRaises(APIError) as caught:
            conflict_store.dispatch("POST", "/series", {}, {
            "anchor_reference": blocked["reference"], "count": 2,
            "interval_weeks": 1,
            }, {**conflict_headers, "Idempotency-Key": "occupied"})
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (409, "table_unavailable"))
        self.assertEqual(conflict_store.state, before)
        self.assertNotEqual(occupied["reference"], blocked["reference"])

    def test_concurrent_replay_and_legacy_stage2_imported_anchor(self):
        anchor = self.book()
        body = {"anchor_reference": anchor["reference"], "count": 4,
                "interval_weeks": 2, "ignored": "accepted"}
        headers = {**{"Authorization": "Bearer " + self.owner},
                   "Idempotency-Key": "parallel-series"}
        def adopt(_):
            return self.store.dispatch("POST", "/series", {}, body, headers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(adopt, range(8)))
        self.assertEqual([status for status, _ in results].count(201), 1)
        self.assertEqual([status for status, _ in results].count(200), 7)
        self.assertTrue(all(result == results[0][1] for _, result in results))
        self.assertEqual(len(self.store.state["series"]), 1)
        self.assertEqual(len(self.store.state["reservations"]), 4)
        self.assertEqual(self.store.state["restaurants"]["r1"]["revision"], 1)

        legacy_store = Store()
        legacy_store.dispatch("POST", "/_test/reset", {}, fixture(), {})
        legacy_owner = legacy_store.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password",
        }, {})[1]["token"]
        legacy_headers = {"Authorization": "Bearer " + legacy_owner}
        legacy_anchor = legacy_store.dispatch("POST", "/reservations", {}, {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2035-09-25T18:00", "party_size": 2,
        }, {**legacy_headers, "Idempotency-Key": "legacy-anchor"})[1]
        envelope = copy.deepcopy(legacy_store.dispatch(
            "GET", "/_test/export", {}, {}, {})[1])
        raw_state = envelope["state"]
        raw_state.pop("series", None)
        restaurant = raw_state["restaurants"]["r1"]
        for name in ("manager_user_ids", "policies", "policy_version", "revision"):
            restaurant.pop(name, None)
        raw_record = raw_state["reservations"][legacy_anchor["reservation_id"]]
        for name in ("revision", "accepted_terms", "history"):
            raw_record.pop(name, None)
        booking_receipt = next(item for item in raw_state["receipts"]
                               if item["key"] == "legacy-anchor")
        for name in ("revision", "accepted_terms"):
            booking_receipt["response"].pop(name, None)
        imported = Store()
        imported.dispatch("POST", "/_test/import", {}, envelope, {})
        token_headers = legacy_headers
        status, adopted = imported.dispatch("POST", "/series", {}, {
            "anchor_reference": legacy_anchor["reference"], "count": 2,
            "interval_weeks": 1,
        }, {**token_headers, "Idempotency-Key": "legacy-series"})
        self.assertEqual(status, 201)
        self.assertEqual(adopted["occurrences"][0]["reservation"]["accepted_terms"]
                         ["policy_version"], 0)
        replay = imported.dispatch("POST", "/reservations", {}, {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2035-09-25T18:00", "party_size": 2,
        }, {**token_headers, "Idempotency-Key": "legacy-anchor"})
        self.assertEqual(replay[0], 200)
        self.assertNotIn("accepted_terms", replay[1])
        self.assertEqual(imported.dispatch(
            "GET", f"/series/{adopted['series_id']}", {}, {}, token_headers)[1],
            adopted)

    def test_dst_gap_fails_without_partial_series_or_receipt(self):
        data = fixture(zone="America/New_York", opens="01:00", closes="04:00")
        self.store.dispatch("POST", "/_test/reset", {}, data, {})
        self.owner = self.login("a@example.test")
        anchor = self.book(key="dst-anchor",
                           starts_at_local="2026-03-01T02:30")
        before = copy.deepcopy(self.store.state)
        with patch("app.store.datetime") as clock:
            clock.now.return_value = datetime(2026, 2, 28, 12, tzinfo=UTC)
            self.assert_error("POST", "/series", {
                "anchor_reference": anchor["reference"], "count": 3,
                "interval_weeks": 1,
            }, self.owner, "dst-gap", 422, "invalid_local_time")
            self.assertEqual(self.store.state, before)

        fall_store = Store()
        fall_store.dispatch("POST", "/_test/reset", {}, fixture(
            zone="America/New_York", opens="01:00", closes="03:00"), {})
        fall_owner = fall_store.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password",
        }, {})[1]["token"]
        fall_headers = {"Authorization": "Bearer " + fall_owner}
        fall_anchor = fall_store.dispatch("POST", "/reservations", {}, {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2026-10-25T01:30", "party_size": 2,
        }, {**fall_headers, "Idempotency-Key": "fall-anchor"})[1]
        with patch("app.store.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 20, 12, tzinfo=UTC)
            _, fall_series = fall_store.dispatch("POST", "/series", {}, {
                "anchor_reference": fall_anchor["reference"], "count": 2,
                "interval_weeks": 1,
            }, {**fall_headers, "Idempotency-Key": "fall-series"})
        self.assertEqual(fall_series["occurrences"][1]["reservation"]["starts_at"],
                         "2026-11-01T01:30:00-04:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
