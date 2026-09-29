"""Stage 3 policy authorization, validation, publication and replay contracts."""
import concurrent.futures
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.store import Store
from app import policies as policy_model
from app.validation import APIError
from contract_stage1 import fixture


def policy(**changes):
    result = {
        "effective_from": "2035-09-24",
        "slot_minutes": 30,
        "reservation_duration_minutes": 120,
        "cancellation_cutoff_minutes": 60,
        "opening_hours": [
            {"weekday": "mon", "opens": "18:00", "closes": "23:00"},
            {"weekday": "wed", "opens": "17:00", "closes": "22:00"},
        ],
        "capacities": {"t1": 5, "t2": 7},
    }
    result.update(changes)
    return result


class PolicyContracts(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        data = fixture()
        data["restaurants"][0]["manager_user_ids"] = ["u1"]
        self.store.dispatch("POST", "/_test/reset", {}, data, {})
        _, manager = self.store.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password"
        }, {})
        _, diner = self.store.dispatch("POST", "/auth/login", {}, {
            "email": "b@example.test", "password": "synthetic password"
        }, {})
        self.manager = {"Authorization": "Bearer " + manager["token"]}
        self.diner = {"Authorization": "Bearer " + diner["token"]}

    def call(self, method, path, body=None, headers=None):
        return self.store.dispatch(method, path, {}, body or {}, headers or {})

    def snapshot(self):
        return copy.deepcopy(self.call("GET", "/_test/export")[1])

    def assert_error(self, method, path, body, headers, status, code):
        with self.assertRaises(APIError) as caught:
            self.store.dispatch(method, path, {}, body, headers)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def create(self, body=None, headers=None, key="policy"):
        return self.call("POST", "/restaurants/r1/policies", body or policy(),
                         {**self.manager, "Idempotency-Key": key, **(headers or {})})

    def test_public_policy_list_manager_permissions_and_legacy_detail(self):
        self.assertEqual(self.call("GET", "/restaurants/r1/policies"),
                         (200, {"policies": []}))
        self.assert_error("GET", "/restaurants/unknown/policies", {}, {}, 404, "not_found")
        self.assert_error("POST", "/restaurants/unknown/policies", policy(), {}, 404, "not_found")
        self.assert_error("POST", "/restaurants/r1/policies", policy(), {}, 401, "unauthenticated")
        self.assert_error("POST", "/restaurants/r1/policies", policy(), self.diner, 403, "forbidden")
        self.assert_error("POST", "/restaurants/r1/policies", policy(), {
            **self.manager, "Idempotency-Key": ""
        }, 400, "missing_idempotency_key")
        self.assert_error("POST", "/restaurants/r1/policies", policy(), {
            **self.manager, "Idempotency-Key": "x" * 256
        }, 422, "validation_failed")

        detail = self.call("GET", "/restaurants/r1")[1]
        self.assertEqual(set(detail), {
            "id", "name", "timezone", "slot_minutes", "reservation_duration_minutes",
            "cancellation_cutoff_minutes", "opening_hours", "tables",
        })
        self.assertNotIn("manager_user_ids", detail)
        self.assertNotIn("policies", detail)
        self.assertNotIn("policy_version", detail)
        self.assertEqual(detail["slot_minutes"], 30)
        self.assertEqual(detail["reservation_duration_minutes"], 90)
        self.assertEqual(detail["cancellation_cutoff_minutes"], 120)

    def test_complete_validation_is_atomic_and_failed_key_can_be_reused(self):
        base = policy()
        invalid = [
            {"effective_from": "2035-02-29"},
            {"effective_from": "2035-9-24"},
            {"slot_minutes": 0},
            {"slot_minutes": 1441},
            {"slot_minutes": True},
            {"reservation_duration_minutes": False},
            {"cancellation_cutoff_minutes": -1},
            {"cancellation_cutoff_minutes": 10081},
            {"opening_hours": [{"weekday": "mon", "opens": "18:00", "closes": "23:00"},
                               {"weekday": "mon", "opens": "17:00", "closes": "22:00"}]},
            {"opening_hours": [{"weekday": "tue", "opens": "23:00", "closes": "18:00"}]},
            {"opening_hours": [{"weekday": "bogus", "opens": "18:00", "closes": "23:00"}]},
            {"capacities": {"t1": 1}},
            {"capacities": {"t1": 1, "t2": 2, "extra": 3}},
            {"capacities": {"t1": 1, "t2": True}},
            {"capacities": {"t1": 0, "t2": 2}},
            {"opening_hours": "mon"},
        ]
        initial = self.snapshot()
        for field_name in ("effective_from", "slot_minutes", "reservation_duration_minutes",
                           "cancellation_cutoff_minutes", "opening_hours", "capacities"):
            body = dict(base)
            body.pop(field_name)
            self.assert_error("POST", "/restaurants/r1/policies", body,
                              {**self.manager, "Idempotency-Key": f"missing-{field_name}"},
                              422, "validation_failed")
            self.assertEqual(self.snapshot(), initial)
        for index, changes in enumerate(invalid):
            with self.subTest(changes=changes):
                body = dict(base)
                body.update(changes)
                self.assert_error("POST", "/restaurants/r1/policies", body,
                                  {**self.manager, "Idempotency-Key": f"bad-{index}"},
                                  422, "validation_failed")
                self.assertEqual(self.snapshot(), initial)
        body = dict(base, ignored_unknown_field="ignored")
        status, published = self.create(body, key="bad-0")
        self.assertEqual(status, 201)
        self.assertEqual(published["policy_version"], 1)
        self.assertNotIn("ignored_unknown_field", published)

    def test_publication_order_replay_versioning_and_export_import(self):
        _, booking = self.call("POST", "/reservations", {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2035-09-24T18:00", "party_size": 2,
        }, {**self.manager, "Idempotency-Key": "before-policy"})
        original_booking = self.call(
            "GET", "/reservations/" + booking["reference"], headers=self.manager)[1]
        first_body = policy(effective_from="2035-09-26", extra="ignored")
        status, first = self.create(first_body, key="create-1")
        self.assertEqual(status, 201)
        self.assertEqual(first["policy_version"], 1)
        self.assertNotIn("extra", first)
        self.assertEqual(self.call(
            "GET", "/reservations/" + booking["reference"], headers=self.manager)[1],
            original_booking)
        after_first = self.snapshot()

        replay_body = dict(reversed(list(first_body.items())))
        status, replay = self.create(replay_body, key="create-1")
        self.assertEqual((status, replay), (200, first))
        self.assertEqual(self.snapshot(), after_first)
        self.assert_error("POST", "/restaurants/r1/policies",
                          policy(effective_from="2035-09-25"),
                          {**self.manager, "Idempotency-Key": "create-1"},
                          409, "idempotency_key_reuse")
        self.assertEqual(self.snapshot(), after_first)

        _, second = self.create(policy(effective_from="2035-09-24"), key="create-2")
        _, third = self.create(policy(effective_from="2035-09-24", slot_minutes=15), key="create-3")
        self.assertEqual([first["policy_version"], second["policy_version"],
                          third["policy_version"]], [1, 2, 3])
        self.assertEqual(self.call("GET", "/restaurants/r1/policies")[1],
                         {"policies": [first, second, third]})

        exported = self.snapshot()
        imported = Store()
        imported.dispatch("POST", "/_test/import", {}, exported, {})
        self.assertEqual(imported.dispatch("GET", "/restaurants/r1/policies", {}, {}, {}),
                         (200, {"policies": [first, second, third]}))
        replay_headers = {**self.manager, "Idempotency-Key": "create-1"}
        self.assertEqual(imported.dispatch("POST", "/restaurants/r1/policies", {},
                                           first_body, replay_headers), (200, first))
        self.assertEqual(imported.dispatch("GET", "/_test/export", {}, {}, {})[1], exported)

    def test_legacy_export_defaults_to_policy_zero_and_concurrent_versions_are_unique(self):
        legacy = self.snapshot()
        restaurant = legacy["state"]["restaurants"]["r1"]
        restaurant.pop("manager_user_ids")
        restaurant.pop("policies")
        restaurant.pop("policy_version")
        restored = Store()
        restored.dispatch("POST", "/_test/import", {}, legacy, {})
        self.assertEqual(restored.dispatch("GET", "/restaurants/r1/policies", {}, {}, {}),
                         (200, {"policies": []}))
        restored_detail = restored.dispatch("GET", "/restaurants/r1", {}, {}, {})[1]
        self.assertEqual(restored_detail["slot_minutes"], 30)
        self.assertEqual(restored_detail["opening_hours"], fixture()["restaurants"][0]["opening_hours"])
        restored_config = restored.dispatch("GET", "/_test/export", {}, {}, {})[1]["state"]["restaurants"]["r1"]
        self.assertEqual(restored_config["manager_user_ids"], [])
        self.assertEqual(restored_config["policy_version"], 0)
        self.assertEqual(policy_model.initial(restored_config), {
            "policy_version": 0,
            "slot_minutes": 30,
            "reservation_duration_minutes": 90,
            "cancellation_cutoff_minutes": 120,
            "opening_hours": fixture()["restaurants"][0]["opening_hours"],
            "capacities": {"t1": 2, "t2": 4},
        })

        bodies = [policy(effective_from=f"2035-10-{1 + index:02d}")
                  for index in range(8)]
        def publish(item):
            index, body = item
            return self.create(body, key=f"parallel-{index}")[1]["policy_version"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            versions = list(pool.map(publish, enumerate(bodies)))
        self.assertEqual(sorted(versions), list(range(1, 9)))
        published = self.call("GET", "/restaurants/r1/policies")[1]["policies"]
        self.assertEqual([item["policy_version"] for item in published],
                         list(range(1, 9)))

    def test_versions_are_independent_per_restaurant(self):
        data = fixture()
        second = copy.deepcopy(data["restaurants"][0])
        second["id"] = "r2"
        second["manager_user_ids"] = ["u1"]
        data["restaurants"][0]["manager_user_ids"] = ["u1"]
        data["restaurants"].append(second)
        store = Store()
        store.dispatch("POST", "/_test/reset", {}, data, {})
        _, login = store.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password"
        }, {})
        headers = {"Authorization": "Bearer " + login["token"]}
        for rid in ("r1", "r2"):
            status, published = store.dispatch(
                "POST", f"/restaurants/{rid}/policies", {}, policy(),
                {**headers, "Idempotency-Key": f"first-{rid}"})
            self.assertEqual((status, published["policy_version"]), (201, 1))
        self.assertEqual([item["policy_version"] for item in
                          store.dispatch("GET", "/restaurants/r1/policies", {}, {}, {})[1]["policies"]],
                         [1])
        self.assertEqual([item["policy_version"] for item in
                          store.dispatch("GET", "/restaurants/r2/policies", {}, {}, {})[1]["policies"]],
                         [1])

    def test_inclusive_policy_range_boundaries_are_valid(self):
        _, minimums = self.create(policy(
            slot_minutes=1, reservation_duration_minutes=1,
            cancellation_cutoff_minutes=0,
            opening_hours=[], capacities={"t1": 1, "t2": 1}),
            key="minimums")
        _, maximums = self.create(policy(
            effective_from="2035-09-25", slot_minutes=1440,
            reservation_duration_minutes=1440, cancellation_cutoff_minutes=10080,
            opening_hours=[], capacities={"t1": 100, "t2": 100}),
            key="maximums")
        self.assertEqual((minimums["policy_version"], maximums["policy_version"]), (1, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
