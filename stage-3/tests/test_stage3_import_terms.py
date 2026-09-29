"""Accepted-policy snapshot integrity during state import."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.store import Store
from app.validation import APIError
from contract_stage1 import fixture
from test_stage3_policies import policy


class ImportedTermsContracts(unittest.TestCase):
    def make_export(self, publish):
        source = Store()
        data = fixture()
        data["restaurants"][0]["manager_user_ids"] = ["u1"]
        source.dispatch("POST", "/_test/reset", {}, data, {})
        _, login = source.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password",
        }, {})
        headers = {"Authorization": "Bearer " + login["token"]}
        if publish:
            source.dispatch("POST", "/restaurants/r1/policies", {}, policy(
                effective_from="2035-09-24",
                capacities={"t1": 3, "t2": 6}), {
                    **headers, "Idempotency-Key": "policy",
                })
        source.dispatch("POST", "/reservations", {}, {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2035-09-24T18:00", "party_size": 2,
        }, {**headers, "Idempotency-Key": "reservation"})
        return source.dispatch("GET", "/_test/export", {}, {}, {})[1]

    @staticmethod
    def mutate_terms(envelope, edit):
        state = envelope["state"]
        record = next(iter(state["reservations"].values()))
        edit(record["accepted_terms"])
        for entry in record["history"]:
            edit(entry["accepted_terms"])
        receipt = next(item for item in state["receipts"]
                       if item["path"] == "/reservations")
        edit(receipt["response"]["accepted_terms"])

    def test_rejects_unknown_or_malformed_policy_snapshots_without_replacing_destination(self):
        policy_export = self.make_export(publish=True)
        policy_cases = {
            "missing_version": lambda terms: terms.pop("policy_version"),
            "unknown_version": lambda terms: terms.__setitem__("policy_version", 999),
            "mismatched_contents": lambda terms: terms.__setitem__(
                "reservation_duration_minutes",
                terms["reservation_duration_minutes"] + 1),
            "boolean_numeric_field": lambda terms: terms.__setitem__(
                "slot_minutes", True),
            "missing_receipt_terms": None,
            "history_mismatch": None,
            "receipt_mismatch": None,
        }
        for name, edit in policy_cases.items():
            with self.subTest(name=name):
                malformed = copy.deepcopy(policy_export)
                if edit is not None:
                    self.mutate_terms(malformed, edit)
                elif name == "missing_receipt_terms":
                    receipt = next(item for item in malformed["state"]["receipts"]
                                   if item["path"] == "/reservations")
                    receipt["response"].pop("accepted_terms")
                elif name == "history_mismatch":
                    record = next(iter(malformed["state"]["reservations"].values()))
                    record["history"][0]["accepted_terms"]["policy_version"] = 999
                else:
                    receipt = next(item for item in malformed["state"]["receipts"]
                                   if item["path"] == "/reservations")
                    receipt["response"]["accepted_terms"]["policy_version"] = 999
                self.assert_rejected_without_replacement(malformed)

        policy_zero_export = self.make_export(publish=False)
        malformed_zero = copy.deepcopy(policy_zero_export)
        self.mutate_terms(malformed_zero, lambda terms: terms.__setitem__(
            "slot_minutes", terms["slot_minutes"] + 1))
        self.assert_rejected_without_replacement(malformed_zero)

    def assert_rejected_without_replacement(self, envelope):
        destination = Store()
        destination.dispatch("POST", "/_test/reset", {}, fixture(), {})
        _, login = destination.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password",
        }, {})
        headers = {"Authorization": "Bearer " + login["token"]}
        destination.dispatch("POST", "/reservations", {}, {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2035-09-24T18:00", "party_size": 2,
        }, {**headers, "Idempotency-Key": "destination"})
        before = copy.deepcopy(destination.state)
        with self.assertRaises(APIError) as caught:
            destination.dispatch("POST", "/_test/import", {}, envelope, {})
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (422, "validation_failed"))
        self.assertEqual(destination.state, before)
        self.assertEqual(destination.dispatch(
            "GET", "/reservations", {}, {}, headers)[0], 200)
        self.assertEqual(destination.dispatch(
            "POST", "/reservations", {}, {
                "restaurant_id": "r1", "table_id": "t1",
                "starts_at_local": "2035-09-24T18:00", "party_size": 2,
            }, {**headers, "Idempotency-Key": "destination"})[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
