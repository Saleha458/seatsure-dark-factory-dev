"""Policy selection and optional per-table availability explanations."""
from pathlib import Path
import sys
import unittest
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.store import Store
from app.validation import APIError
from contract_stage1 import fixture
from test_stage2 import free_port, http_call, start_service


def policy(effective_from, *, opens="18:00", closes="23:00", slot=30,
           duration=90, capacities=None, opening_hours=None):
    return {
        "effective_from": effective_from,
        "slot_minutes": slot,
        "reservation_duration_minutes": duration,
        "cancellation_cutoff_minutes": 60,
        "opening_hours": opening_hours if opening_hours is not None else [
            {"weekday": day, "opens": opens, "closes": closes}
            for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        ],
        "capacities": capacities or {"t1": 2, "t2": 4, "t3": 3},
    }


class PolicyAvailabilityContracts(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        data = fixture()
        data["restaurants"][0]["tables"].append({
            "id": "t3", "label": "Window", "capacity": 3
        })
        data["restaurants"][0]["combinable"] = [["t1", "t2"], ["t2", "t3"]]
        data["restaurants"][0]["manager_user_ids"] = ["u1"]
        self.store.dispatch("POST", "/_test/reset", {}, data, {})
        _, login = self.store.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password"
        }, {})
        self.manager = {"Authorization": "Bearer " + login["token"]}

    def publish(self, body, key):
        return self.store.dispatch(
            "POST", "/restaurants/r1/policies", {}, body,
            {**self.manager, "Idempotency-Key": key})[1]

    def availability(self, day, party_size=2, explain=None):
        query = {"restaurant_id": "r1", "date": day, "party_size": str(party_size)}
        if explain is not None:
            query["explain"] = explain
        return self.store.dispatch("GET", "/availability", query, {}, {})[1]

    def test_default_shape_policy_zero_and_explain_validation(self):
        default = self.availability("2035-09-24")
        self.assertEqual(set(default), {"restaurant_id", "date", "timezone", "slots"})
        self.assertEqual(set(default["slots"][0]), {
            "starts_at_local", "starts_at", "available_table_ids", "available_options"
        })
        baseline = self.availability("2035-09-24", party_size=5, explain="true")
        slot = baseline["slots"][0]
        self.assertEqual(slot["explain"], [
            {"table_id": "t1", "policy_version": 0, "available": False,
             "rules": [{"rule": "capacity", "holds": False},
                       {"rule": "no_overlap", "holds": True}]},
            {"table_id": "t2", "policy_version": 0, "available": False,
             "rules": [{"rule": "capacity", "holds": False},
                       {"rule": "no_overlap", "holds": True}]},
            {"table_id": "t3", "policy_version": 0, "available": False,
             "rules": [{"rule": "capacity", "holds": False},
                       {"rule": "no_overlap", "holds": True}]},
        ])
        for value in ("false", "1", ""):
            with self.subTest(explain=value), self.assertRaises(APIError) as caught:
                self.availability("2035-09-24", explain=value)
            self.assertEqual((caught.exception.status, caught.exception.code),
                             (422, "validation_failed"))

    def test_effective_date_order_and_same_date_highest_version(self):
        late = self.publish(policy("2035-09-27", opens="20:00", closes="22:00",
                                   slot=60, duration=60), "later-effective")
        earlier = self.publish(policy("2035-09-26", opens="19:00", closes="22:00",
                                      slot=60, duration=60), "earlier-effective")
        same_date = self.publish(policy("2035-09-26", opens="18:30", closes="21:00",
                                        slot=30, duration=30), "same-date-newer")
        smaller_grid = self.publish(policy("2035-09-26", opens="18:00", closes="19:00",
                                           slot=15, duration=15), "same-date-smaller-grid")
        self.assertEqual((late["policy_version"], earlier["policy_version"],
                          same_date["policy_version"], smaller_grid["policy_version"]),
                         (1, 2, 3, 4))

        before = self.availability("2035-09-25")
        self.assertEqual(before["slots"][0]["starts_at_local"], "2035-09-25T18:00")
        self.assertEqual(len(before["slots"]), 8)
        selected_earlier = self.availability("2035-09-26", explain="true")
        self.assertEqual([slot["starts_at_local"] for slot in selected_earlier["slots"]],
                         ["2035-09-26T18:00", "2035-09-26T18:15",
                          "2035-09-26T18:30", "2035-09-26T18:45"])
        self.assertTrue(all(item["policy_version"] == 4
                            for item in selected_earlier["slots"][0]["explain"]))

        selected_later = self.availability("2035-09-27", explain="true")
        self.assertEqual(selected_later["slots"][0]["starts_at_local"], "2035-09-27T20:00")
        self.assertEqual([slot["starts_at_local"] for slot in selected_later["slots"]],
                         ["2035-09-27T20:00", "2035-09-27T21:00"])
        self.assertTrue(all(item["policy_version"] == 1
                            for item in selected_later["slots"][0]["explain"]))

    def test_policy_capacity_combines_declared_pairs_and_explain_matches_available(self):
        self.publish(policy("2035-09-26", capacities={"t1": 2, "t2": 5, "t3": 6}),
                     "capacity-change")
        result = self.availability("2035-09-26", party_size=7, explain="true")
        first = result["slots"][0]
        self.assertEqual(first["available_table_ids"], [])
        self.assertEqual(first["available_options"], [
            {"table_ids": ["t1", "t2"], "capacity": 7},
            {"table_ids": ["t2", "t3"], "capacity": 11},
        ])
        self.assertEqual([entry["table_id"] for entry in first["explain"]],
                         ["t1", "t2", "t3"])
        self.assertEqual([entry["available"] for entry in first["explain"]],
                         [False, False, False])
        for slot in result["slots"]:
            true_tables = [entry["table_id"] for entry in slot["explain"]
                           if entry["available"]]
            self.assertEqual(true_tables, slot["available_table_ids"])
            self.assertEqual([entry["table_id"] for entry in slot["explain"]],
                             ["t1", "t2", "t3"])
            for entry in slot["explain"]:
                self.assertEqual([rule["rule"] for rule in entry["rules"]],
                                 ["capacity", "no_overlap"])
                self.assertEqual(entry["available"],
                                 all(rule["holds"] for rule in entry["rules"]))

    def test_overlap_explanation_marks_no_overlap_false_and_closed_policy_has_no_slots(self):
        booking_body = {
            "restaurant_id": "r1", "table_id": "t2",
            "starts_at_local": "2035-09-26T19:00", "party_size": 2,
        }
        _, booking = self.store.dispatch("POST", "/reservations", {}, booking_body, {
            **self.manager, "Idempotency-Key": "seed-overlap"
        })
        self.assertEqual(booking["table_id"], "t2")
        self.publish(policy("2035-09-26", opens="19:00", closes="21:00",
                            capacities={"t1": 1, "t2": 4, "t3": 1}),
                     "explain-overlap")
        slot = self.availability("2035-09-26", party_size=5, explain="true")["slots"][0]
        self.assertEqual(slot["available_table_ids"], [])
        self.assertEqual([item["table_id"] for item in slot["explain"]], ["t1", "t2", "t3"])
        table2 = slot["explain"][1]
        self.assertEqual(table2["rules"], [
            {"rule": "capacity", "holds": False},
            {"rule": "no_overlap", "holds": False},
        ])
        self.assertFalse(table2["available"])

        closed = policy("2035-09-27", opening_hours=[], capacities={
            "t1": 2, "t2": 4, "t3": 3
        })
        self.publish(closed, "closed-policy")
        self.assertEqual(self.availability("2035-09-27", explain="true")["slots"], [])

    def test_policy_slot_steps_preserve_dst_gap_and_fold_rules(self):
        self.publish(policy("2026-03-29", opens="01:00", closes="04:00",
                            slot=30, duration=30), "spring-grid")
        spring = self.availability("2026-03-29", explain="true")
        self.assertEqual([slot["starts_at_local"] for slot in spring["slots"]],
                         ["2026-03-29T01:00", "2026-03-29T01:30",
                          "2026-03-29T03:00", "2026-03-29T03:30"])

        self.publish(policy("2026-10-25", opens="01:00", closes="04:00",
                            slot=30, duration=30), "fall-grid")
        fall = self.availability("2026-10-25", explain="true")
        self.assertEqual([slot["starts_at_local"] for slot in fall["slots"]],
                         ["2026-10-25T01:00", "2026-10-25T01:30",
                          "2026-10-25T02:00", "2026-10-25T02:30",
                          "2026-10-25T03:00", "2026-10-25T03:30"])
        self.assertEqual(len({slot["starts_at_local"] for slot in fall["slots"]}),
                         len(fall["slots"]))

    def test_http_rejects_any_invalid_repeated_explain_value(self):
        port = free_port()
        service = start_service(Path(__file__).resolve().parents[1], port)
        try:
            base = f"http://127.0.0.1:{port}"
            http_call(base, "POST", "/_test/reset", fixture())
            query = urlencode([
                ("restaurant_id", "r1"), ("date", "2035-09-24"),
                ("party_size", "2"), ("explain", "false"), ("explain", "true"),
            ])
            status, _, payload, _ = http_call(base, "GET", "/availability?" + query)
            self.assertEqual(status, 422)
            self.assertEqual(payload["error"]["code"], "validation_failed")
            valid = urlencode([
                ("restaurant_id", "r1"), ("date", "2035-09-24"),
                ("party_size", "2"), ("explain", "true"), ("explain", "true"),
            ])
            status, _, payload, _ = http_call(base, "GET", "/availability?" + valid)
            self.assertEqual(status, 200)
            self.assertIn("explain", payload["slots"][0])
        finally:
            if service.poll() is None:
                service.terminate()
                service.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
