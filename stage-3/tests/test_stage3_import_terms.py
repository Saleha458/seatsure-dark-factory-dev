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
from test_stage2 import combined_fixture, free_port, http_call, start_service

ROOT = Path(__file__).resolve().parents[2]
STAGE2 = ROOT / "stage-2"


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

    def test_real_stage2_pair_and_move_receipts_import_without_stage3_mutation(self):
        port = free_port()
        process = start_service(STAGE2, port)
        base = f"http://127.0.0.1:{port}"
        try:
            status, _, _, _ = http_call(
                base, "POST", "/_test/reset", combined_fixture())
            self.assertEqual(status, 204)
            status, _, login, _ = http_call(base, "POST", "/auth/login", {
                "email": "a@example.test", "password": "synthetic password",
            })
            self.assertEqual(status, 200)
            token = login["token"]
            headers = {"Authorization": "Bearer " + token}
            pair_body = {
                "restaurant_id": "r1", "table_ids": ["t1", "t2"],
                "starts_at_local": "2035-09-24T18:00", "party_size": 6,
            }
            pair_key = "stage2-pair-receipt"
            status, _, pair, _ = http_call(
                base, "POST", "/reservations", pair_body,
                {**headers, "Idempotency-Key": pair_key})
            self.assertEqual(status, 201)
            self.assertEqual(pair["table_ids"], ["t1", "t2"])
            self.assertNotIn("accepted_terms", pair)

            move_body = {"moves": [{
                "reference": pair["reference"], "table_ids": ["t2", "t1"],
                "starts_at_local": "2035-09-24T19:30",
            }]}
            move_key = "stage2-move-receipt"
            status, _, moved, _ = http_call(
                base, "POST", "/reservation-moves", move_body,
                {**headers, "Idempotency-Key": move_key})
            self.assertEqual(status, 201)
            self.assertEqual(moved["reservations"][0]["table_ids"], ["t1", "t2"])
            self.assertNotIn("accepted_terms", moved["reservations"][0])
            status, _, exported, _ = http_call(base, "GET", "/_test/export")
            self.assertEqual(status, 200)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

        destination = Store()
        self.assertEqual(destination.dispatch(
            "POST", "/_test/import", {}, exported, {})[0], 204)
        stage3_headers = {"Authorization": "Bearer " + token}
        listed = destination.dispatch(
            "GET", "/reservations", {}, {}, stage3_headers)[1]["reservations"]
        imported_pair = next(item for item in listed
                             if item["reference"] == pair["reference"])
        self.assertEqual(imported_pair["table_ids"], ["t1", "t2"])
        self.assertEqual(imported_pair["starts_at_local"], "2035-09-24T19:30")
        self.assertEqual(imported_pair["accepted_terms"]["policy_version"], 0)
        before = copy.deepcopy(destination.state)
        self.assertEqual(destination.dispatch(
            "POST", "/reservations", {}, pair_body,
            {**stage3_headers, "Idempotency-Key": pair_key}), (200, pair))
        self.assertEqual(destination.dispatch(
            "POST", "/reservation-moves", {}, move_body,
            {**stage3_headers, "Idempotency-Key": move_key}), (200, moved))
        self.assertEqual(destination.state, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
