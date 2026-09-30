"""Deterministic public dispatcher checks using the agreed clock/hash seams."""
import concurrent.futures
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import security
from app.store import Store
from app.validation import APIError
from contract_stage1 import fixture


class StateContracts(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.call('POST', '/_test/reset', fixture())
        _, login = self.call('POST', '/auth/login', {'email': 'a@example.test', 'password': 'synthetic password'})
        self.headers = {'Authorization': 'Bearer ' + login['token'], 'Idempotency-Key': 'test'}

    def call(self, method, path, body=None, headers=None):
        return self.store.dispatch(method, path, {}, body or {}, headers or {})

    def snapshot(self):
        return self.call('GET', '/_test/export')[1]

    def test_replaced_credentials_cannot_gain_a_session_from_inflight_login(self):
        for route in ('/_test/reset', '/_test/import'):
            with self.subTest(route=route):
                self.setUp()
                replacement = fixture()
                replacement['users'][0]['password'] = 'replacement password'
                if route.endswith('import'):
                    peer = Store()
                    peer.dispatch('POST', '/_test/reset', {}, replacement, {})
                    replacement = peer.dispatch('GET', '/_test/export', {}, {}, {})[1]
                entered, release = threading.Event(), threading.Event()
                real_verify = security.verify_password

                def gated(password, credential):
                    result = real_verify(password, credential)
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('Test did not release verification barrier')
                    return result

                with patch('app.security.verify_password', side_effect=gated):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(self.call, 'POST', '/auth/login',
                                              {'email': 'a@example.test', 'password': 'synthetic password'})
                        try:
                            self.assertTrue(entered.wait(5), 'Login did not reach verification barrier')
                            self.assertEqual(self.call('POST', route, replacement)[0], 204)
                            replaced = self.snapshot()
                        finally:
                            release.set()
                        with self.assertRaises(APIError) as caught:
                            pending.result(timeout=5)
                self.assertEqual((caught.exception.status, caught.exception.code), (401, 'unauthenticated'))
                self.assertTrue(self.snapshot() == replaced, 'Stale login mutated replacement state')
                status, login = self.call('POST', '/auth/login', {'email': 'a@example.test', 'password': 'replacement password'})
                self.assertEqual(status, 200)
                self.assertEqual(self.call('GET', '/reservations', headers={'Authorization': 'Bearer ' + login['token']}), (200, {'reservations': []}))

    def test_exact_cutoff_rejects_cancel_patch_and_batch_without_mutation(self):
        body = {'restaurant_id': 'r1', 'table_id': 't1', 'starts_at_local': '2035-09-24T18:00', 'party_size': 2}
        _, booking = self.call('POST', '/reservations', body, self.headers)
        boundary = datetime.fromisoformat(booking['starts_at']) - timedelta(minutes=120)
        reference = booking['reference']
        before = self.snapshot()
        with patch('app.store.datetime') as clock:
            clock.now.return_value = boundary
            for method, path, change in [
                ('POST', '/reservations/' + reference + '/cancel', {}),
                ('PATCH', '/reservations/' + reference, {'party_size': 0}),
                ('POST', '/reservation-moves', {'moves': [{'reference': reference, 'party_size': 0}]})]:
                with self.subTest(method=method, path=path):
                    with self.assertRaises(APIError) as caught:
                        self.call(method, path, change, self.headers)
                    self.assertEqual((caught.exception.status, caught.exception.code), (409, 'cutoff_passed'))
                    self.assertTrue(before == self.snapshot(), 'Cutoff rejection changed state')
            clock.now.return_value = boundary - timedelta(microseconds=1)
            status, cancelled = self.call('POST', '/reservations/' + reference + '/cancel', {}, self.headers)
            self.assertEqual((status, cancelled['status']), (200, 'cancelled'))
            clock.now.return_value = boundary + timedelta(days=2)
            self.assertEqual(self.call('POST', '/reservations/' + reference + '/cancel', {}, self.headers), (200, cancelled))


if __name__ == '__main__':
    unittest.main(verbosity=2)
