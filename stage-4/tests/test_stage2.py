"""Stage 2 combination, portability, and web-route regression coverage."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.store import Store
from app.validation import APIError
from contract_stage1 import fixture as stage1_fixture


ROOT = Path(__file__).resolve().parents[2]
STAGE1 = ROOT / "stage-1"
STAGE2 = ROOT / "stage-2"


def combined_fixture():
    data = stage1_fixture()
    data["restaurants"][0]["tables"].append({"id": "t3", "label": "Window nook", "capacity": 2})
    data["restaurants"][0]["combinable"] = [["t1", "t2"], ["t2", "t3"]]
    return data


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def start_service(directory, port):
    environment = dict(os.environ, PORT=str(port), PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen([sys.executable, "-B", "-m", "app"], cwd=directory,
                               env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Service in {directory} exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return process
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    process.terminate()
    process.wait(timeout=10)
    raise RuntimeError(f"Service in {directory} did not become healthy")


def http_call(base, method, path, body=None, headers=None):
    request_headers = dict(headers or {})
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers=request_headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        content = response.read()
        result = json.loads(content) if content and "application/json" in response.headers.get(
            "Content-Type", "").lower() else None
        return response.status, response.headers, result, content


class CombinedTableContracts(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.headers = {}
        self.reset(combined_fixture())
        _, login = self.call("POST", "/auth/login", {
            "email": "a@example.test", "password": "synthetic password"
        })
        self.headers = {"Authorization": "Bearer " + login["token"]}

    def call(self, method, path, body=None, extra=None, query=None):
        return self.store.dispatch(method, path, query or {}, body or {}, {**self.headers, **(extra or {})})

    def reset(self, data):
        return self.store.dispatch("POST", "/_test/reset", {}, data, {})

    def error(self, method, path, body, expected, key):
        with self.assertRaises(APIError) as caught:
            self.call(method, path, body, {"Idempotency-Key": key})
        self.assertEqual((caught.exception.status, caught.exception.code), expected)

    def booking(self, key, **changes):
        body = {
            "restaurant_id": "r1", "table_id": "t1",
            "starts_at_local": "2035-09-24T18:00", "party_size": 2
        }
        body.update(changes)
        if "table_ids" in changes and "table_id" not in changes:
            body.pop("table_id")
        return self.call("POST", "/reservations", body, {"Idempotency-Key": key})

    def test_options_are_ordered_and_pair_occupancy_is_shared(self):
        restaurant = self.call("GET", "/restaurants/r1")[1]
        self.assertEqual(restaurant["combinable"], [["t1", "t2"], ["t2", "t3"]])
        restaurants = self.call("GET", "/restaurants")[1]["restaurants"]
        self.assertEqual(restaurants[0]["combinable"], [["t1", "t2"], ["t2", "t3"]])
        result = self.call("GET", "/availability", query={
            "restaurant_id": "r1", "date": "2035-09-24", "party_size": "4"
        })[1]
        first = result["slots"][0]
        self.assertEqual(first["available_table_ids"], ["t2"])
        self.assertEqual(first["available_options"], [
            {"table_ids": ["t2"], "capacity": 4},
            {"table_ids": ["t1", "t2"], "capacity": 6},
            {"table_ids": ["t2", "t3"], "capacity": 6},
        ])

        status, pair = self.booking("pair", table_ids=["t2", "t1"], party_size=6)
        self.assertEqual(status, 201)
        self.assertEqual(pair["table_ids"], ["t1", "t2"])
        self.assertNotIn("table_id", pair)
        self.error("POST", "/reservations", {
            "restaurant_id": "r1", "table_ids": ["t2", "t3"],
            "starts_at_local": "2035-09-24T18:00", "party_size": 6
        }, (409, "table_unavailable"), "overlap")
        available = self.call("GET", "/availability", query={
            "restaurant_id": "r1", "date": "2035-09-24", "party_size": "2"
        })[1]["slots"][0]
        self.assertEqual(available["available_table_ids"], ["t3"])
        self.assertEqual(available["available_options"], [{"table_ids": ["t3"], "capacity": 2}])

        cancelled = self.call("POST", f"/reservations/{pair['reference']}/cancel", {})[1]
        self.assertEqual(cancelled["table_ids"], ["t1", "t2"])
        restored = self.call("GET", "/availability", query={
            "restaurant_id": "r1", "date": "2035-09-24", "party_size": "4"
        })[1]["slots"][0]
        self.assertEqual(restored["available_table_ids"], ["t2"])
        self.assertEqual([o["table_ids"] for o in restored["available_options"]],
                         [["t2"], ["t1", "t2"], ["t2", "t3"]])

    def test_combination_validation_precedence_and_single_compatibility(self):
        self.error("POST", "/reservations", {
            "restaurant_id": "r1", "table_ids": ["t1", "t1"],
            "starts_at_local": "2035-09-24T18:00", "party_size": 2
        }, (422, "validation_failed"), "duplicate")
        self.error("POST", "/reservations", {
            "restaurant_id": "r1", "table_id": "t1", "table_ids": ["t1", "t2"],
            "starts_at_local": "2035-09-24T18:00", "party_size": 2
        }, (422, "validation_failed"), "both")
        for key, ids, code in (
            ("unlisted", ["t1", "t3"], "combination_not_allowed"),
            ("too-many", ["t1", "t2", "t3"], "combination_not_allowed"),
        ):
            self.error("POST", "/reservations", {
                "restaurant_id": "r1", "table_ids": ids,
                "starts_at_local": "2035-09-24T18:00", "party_size": 2
            }, (422, code), key)
        self.error("POST", "/reservations", {
            "restaurant_id": "r1", "table_ids": ["t1", "t2"],
            "starts_at_local": "2035-09-24T18:00", "party_size": 7
        }, (422, "party_exceeds_capacity"), "capacity")
        status, single = self.booking("single")
        self.assertEqual(status, 201)
        self.assertEqual(single["table_ids"], ["t1"])
        self.assertEqual(single["table_id"], "t1")

    def test_pair_amendment_and_atomic_moves_rollback_and_commit(self):
        _, first = self.booking("pair", table_ids=["t1", "t2"], party_size=6)
        _, second = self.booking("single", table_id="t3", party_size=2)
        original = self.store.dispatch("GET", "/_test/export", {}, {}, {})[1]
        with self.assertRaises(APIError) as caught:
            self.call("PATCH", f"/reservations/{second['reference']}", {"table_ids": ["t2", "t3"]})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "table_unavailable"))
        self.assertEqual(self.store.dispatch("GET", "/_test/export", {}, {}, {})[1], original)

        moved = self.call("PATCH", f"/reservations/{second['reference']}", {
            "table_ids": ["t2", "t3"], "starts_at_local": "2035-09-24T19:30"
        })[1]
        self.assertEqual(moved["table_ids"], ["t2", "t3"])
        batch_before = self.store.dispatch("GET", "/_test/export", {}, {}, {})[1]
        with self.assertRaises(APIError) as caught:
            self.call("POST", "/reservation-moves", {"moves": [
                {"reference": first["reference"], "table_ids": ["t1", "t2"]},
                {"reference": second["reference"], "table_ids": ["t2", "t3"],
                 "starts_at_local": "2035-09-24T18:00"},
            ]}, {"Idempotency-Key": "move-conflict"})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "table_unavailable"))
        self.assertEqual(self.store.dispatch("GET", "/_test/export", {}, {}, {})[1], batch_before)
        status, result = self.call("POST", "/reservation-moves", {"moves": [
            {"reference": second["reference"], "table_ids": ["t2", "t3"],
             "starts_at_local": "2035-09-24T20:00"},
        ]}, {"Idempotency-Key": "move-pair"})
        self.assertEqual(status, 201)
        self.assertEqual(result["reservations"][0]["table_ids"], ["t2", "t3"])

    def test_seeded_cancelled_pairs_do_not_occupy_and_confirmed_pairs_do(self):
        data = combined_fixture()
        data["reservations"] = [
            {"id": "res-cancel", "reference": "CANCEL1", "user_id": "u1",
             "restaurant_id": "r1", "table_ids": ["t1", "t2"],
             "starts_at_local": "2035-09-24T18:00", "party_size": 6, "status": "cancelled"},
            {"id": "res-live", "reference": "LIVE01", "user_id": "u1",
             "restaurant_id": "r1", "table_ids": ["t2", "t3"],
             "starts_at_local": "2035-09-24T21:00", "party_size": 6},
        ]
        self.reset(data)
        _, login = self.store.dispatch("POST", "/auth/login", {}, {
            "email": "a@example.test", "password": "synthetic password"
        }, {})
        token = {"Authorization": "Bearer " + login["token"]}
        slots = self.store.dispatch("GET", "/availability", {
            "restaurant_id": "r1", "date": "2035-09-24", "party_size": "6"
        }, {}, {})[1]["slots"]
        self.assertEqual([item["table_ids"] for item in slots[0]["available_options"]],
                         [["t1", "t2"], ["t2", "t3"]])
        self.assertEqual(slots[6]["available_options"], [])
        records = self.store.dispatch("GET", "/reservations", {}, {}, token)[1]["reservations"]
        self.assertEqual({r["reference"]: r["status"] for r in records},
                         {"CANCEL1": "cancelled", "LIVE01": "confirmed"})
        self.assertEqual(next(r for r in records if r["reference"] == "CANCEL1")["table_ids"], ["t1", "t2"])

    def test_concurrent_declared_pairs_sharing_a_member_serialize(self):
        bodies = [
            {"restaurant_id": "r1", "table_ids": pair,
             "starts_at_local": "2035-09-24T18:00", "party_size": 4}
            for pair in (["t1", "t2"], ["t2", "t3"]) for _ in range(25)
        ]

        def create(index_body):
            index, body = index_body
            try:
                return self.call("POST", "/reservations", body,
                                 {"Idempotency-Key": f"race-{index}"})[0]
            except APIError as error:
                if (error.status, error.code) != (409, "table_unavailable"):
                    raise
                return error.status

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            statuses = list(pool.map(create, enumerate(bodies)))
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(409), 49)
        reservations = self.store.state["reservations"].values()
        self.assertEqual(sum(record["status"] == "confirmed" for record in reservations), 1)

    def test_stage2_pair_and_move_receipts_survive_export_import_and_cancellation(self):
        status, original = self.booking("pair-portable", table_ids=["t1", "t2"], party_size=6)
        self.assertEqual(status, 201)
        status, moved = self.call("POST", "/reservation-moves", {"moves": [{
            "reference": original["reference"],
            "table_ids": ["t3", "t2"],
            "starts_at_local": "2035-09-24T21:00",
        }]}, {"Idempotency-Key": "move-portable"})
        self.assertEqual(status, 201)
        exported = self.store.dispatch("GET", "/_test/export", {}, {}, {})[1]

        destination = Store()
        self.assertEqual(destination.dispatch("POST", "/_test/import", {}, exported, {})[0], 204)
        self.assertEqual(destination.dispatch(
            "POST", "/reservations", {}, {
                "restaurant_id": "r1", "table_ids": ["t1", "t2"],
                "starts_at_local": "2035-09-24T18:00", "party_size": 6
            }, {**self.headers, "Idempotency-Key": "pair-portable"}
        ), (200, original))
        self.assertEqual(destination.dispatch(
            "POST", "/reservation-moves", {}, {"moves": [{
                "reference": original["reference"], "table_ids": ["t3", "t2"],
                "starts_at_local": "2035-09-24T21:00"
            }]}, {**self.headers, "Idempotency-Key": "move-portable"}
        ), (200, moved))
        cancelled = destination.dispatch(
            "POST", f"/reservations/{original['reference']}/cancel", {}, {}, self.headers
        )[1]
        self.assertEqual(cancelled["table_ids"], ["t2", "t3"])
        slots = destination.dispatch("GET", "/availability", {
            "restaurant_id": "r1", "date": "2035-09-24", "party_size": "6"
        }, {}, {})[1]["slots"]
        self.assertEqual([option["table_ids"] for option in slots[6]["available_options"]],
                         [["t1", "t2"], ["t2", "t3"]])

    def test_bad_pair_declarations_leave_previous_state_untouched(self):
        previous = self.store.dispatch("GET", "/_test/export", {}, {}, {})[1]
        for pair in (["t1", "t1"], ["t1", "missing"], ["t1", "t2", "t3"], ["t1", "t2"]):
            invalid = combined_fixture()
            invalid["restaurants"][0]["combinable"] = [pair, ["t2", "t1"]]
            with self.assertRaises(APIError):
                self.reset(invalid)
            self.assertEqual(self.store.dispatch("GET", "/_test/export", {}, {}, {})[1], previous)


class Stage1UpgradeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage1_port = free_port()
        cls.stage2_port = free_port()
        cls.stage1 = start_service(STAGE1, cls.stage1_port)
        try:
            cls.stage2 = start_service(STAGE2, cls.stage2_port)
        except BaseException:
            cls.stage1.terminate()
            cls.stage1.wait(timeout=10)
            raise
        cls.stage1_url = f"http://127.0.0.1:{cls.stage1_port}"
        cls.stage2_url = f"http://127.0.0.1:{cls.stage2_port}"

    @classmethod
    def tearDownClass(cls):
        for process in (cls.stage1, cls.stage2):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

    def test_real_stage1_export_keeps_live_session_and_original_receipt(self):
        status, _, _, _ = http_call(self.stage1_url, "POST", "/_test/reset", stage1_fixture())
        self.assertEqual(status, 204)
        status, _, auth, _ = http_call(self.stage1_url, "POST", "/auth/login", {
            "email": "a@example.test", "password": "synthetic password"
        })
        self.assertEqual(status, 200)
        token = auth["token"]
        body = {"restaurant_id": "r1", "table_id": "t1",
                "starts_at_local": "2035-09-24T18:00", "party_size": 2}
        source_headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "pre-upgrade"}
        status, _, original, _ = http_call(self.stage1_url, "POST", "/reservations", body, source_headers)
        self.assertEqual(status, 201)
        status, _, exported, _ = http_call(self.stage1_url, "GET", "/_test/export")
        self.assertEqual(status, 200)
        status, _, _, _ = http_call(self.stage2_url, "POST", "/_test/import", exported)
        self.assertEqual(status, 204)
        status, _, current, _ = http_call(self.stage2_url, "GET", "/reservations",
                                          headers={"Authorization": "Bearer " + token})
        self.assertEqual(status, 200)
        self.assertEqual(current["reservations"][0]["reference"], original["reference"])
        self.assertEqual(current["reservations"][0]["table_ids"], ["t1"])
        status, _, replay, _ = http_call(self.stage2_url, "POST", "/reservations", body, source_headers)
        self.assertEqual(status, 200)
        self.assertEqual(replay, original)

if __name__ == "__main__":
    unittest.main(verbosity=2)
