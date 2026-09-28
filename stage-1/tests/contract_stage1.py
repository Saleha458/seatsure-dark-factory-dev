"""Independent Stage 1 HTTP contracts. No imports from the service.

Run with --base-url URL --peer-url URL. All fixtures are synthetic.
Exports stay in memory; assertion output deliberately excludes snapshot contents.
"""
import argparse
import concurrent.futures
import copy
import datetime as dt
import json
import re
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

BASE = 'http://127.0.0.1:8080'
PEER = 'http://127.0.0.1:8081'


def fixture(zone='Europe/Berlin', opens='18:00', closes='23:00', duration=90):
    return {'users': [{'id': 'u1', 'email': 'a@example.test', 'password': 'synthetic password', 'display_name': 'Ada'},
                      {'id': 'u2', 'email': 'b@example.test', 'password': 'synthetic password', 'display_name': 'Bob'}],
            'restaurants': [{'id': 'r1', 'name': 'Example', 'timezone': zone,
                             'slot_minutes': 30, 'reservation_duration_minutes': duration,
                             'cancellation_cutoff_minutes': 120,
                             'opening_hours': [{'weekday': day, 'opens': opens, 'closes': closes}
                                               for day in 'mon tue wed thu fri sat sun'.split()],
                             'tables': [{'id': 't1', 'label': '1', 'capacity': 2},
                                        {'id': 't2', 'label': '2', 'capacity': 4}]}], 'reservations': []}


class Contracts(unittest.TestCase):
    def call(self, method, path, body=None, token=None, key=None, base=None, raw=None):
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        if key is not None:
            headers['Idempotency-Key'] = key
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request((base or BASE) + path, data=data, headers=headers, method=method)
        started = time.monotonic()
        try:
            response = urllib.request.urlopen(request, timeout=10 if path.startswith('/_test/') else 5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            status = response.status
            if content:
                self.assertIn('application/json', response.headers.get('Content-Type', '').lower())
                self.assertIn('charset=utf-8', response.headers.get('Content-Type', '').lower().replace(' ', ''))
            result = json.loads(content) if content else None
        self.assertLessEqual(time.monotonic() - started, 10 if path.startswith('/_test/') else 5)
        self.assertLess(status, 500, 'Service returned a server error')
        if status >= 400:
            self.assertIsInstance(result.get('error', {}).get('code'), str)
            self.assertIsInstance(result['error'].get('message'), str)
            self.assertTrue(result['error']['message'])
        if status == 204:
            self.assertEqual(content, b'')
        return status, result

    def expect(self, status, method, path, body=None, code=None, **kwargs):
        actual, result = self.call(method, path, body, **kwargs)
        self.assertEqual(actual, status, f'{method} {path}: expected {status}, got {actual}; code={result.get("error", {}).get("code") if isinstance(result, dict) else None}')
        if code:
            self.assertEqual(result['error']['code'], code)
        return result

    def reset(self, data=None, base=None):
        self.expect(204, 'POST', '/_test/reset', data or fixture(), base=base)

    def login(self, email='a@example.test', base=None):
        return self.expect(200, 'POST', '/auth/login', {'email': email, 'password': 'synthetic password'}, base=base)['token']

    def setUp(self):
        self.reset()
        self.token = self.login()

    def body(self, **changes):
        body = {'restaurant_id': 'r1', 'table_id': 't1', 'starts_at_local': '2035-09-24T18:00', 'party_size': 2}
        body.update(changes)
        return body

    def create(self, key='create', **changes):
        return self.expect(201, 'POST', '/reservations', self.body(**changes), token=self.token, key=key)

    def snapshot(self, base=None):
        return self.expect(200, 'GET', '/_test/export', base=base)

    def unchanged(self, before, base=None):
        self.assertTrue(before == self.snapshot(base), 'Rejected operation changed exported state (private values suppressed)')

    def availability(self, date='2035-09-24', party='2'):
        return self.expect(200, 'GET', '/availability?' + urllib.parse.urlencode({'restaurant_id': 'r1', 'date': date, 'party_size': party}))['slots']

    def test_public_browsing_grid_capacity_order_and_closed_days(self):
        self.assertEqual(self.expect(200, 'GET', '/health'), {'status': 'ok'})
        self.assertEqual(self.expect(200, 'GET', '/restaurants?ignored=yes')['restaurants'],
                         [{'id': 'r1', 'name': 'Example', 'timezone': 'Europe/Berlin'}])
        self.assertEqual(self.expect(200, 'GET', '/restaurants/r1'), fixture()['restaurants'][0])
        slots = self.availability()
        self.assertEqual([s['starts_at_local'][-5:] for s in slots], ['18:00', '18:30', '19:00', '19:30', '20:00', '20:30', '21:00', '21:30'])
        self.assertTrue(all(s['available_table_ids'] == ['t1', 't2'] for s in slots))
        self.assertTrue(all(s['available_table_ids'] == ['t2'] for s in self.availability(party='4')))
        self.assertTrue(all(s['available_table_ids'] == [] for s in self.availability(party='5')))
        data = fixture(); data['restaurants'][0]['opening_hours'] = []
        self.reset(data)
        self.assertEqual(self.availability(), [])
        self.expect(404, 'GET', '/restaurants/absent', code='not_found')

    def test_authentication_sessions_privacy_and_reset_revocation(self):
        other = self.login('b@example.test')
        second = self.login()
        booking = self.create()
        for token in (self.token, second):
            self.assertEqual(self.expect(200, 'GET', '/reservations', token=token)['reservations'], [booking])
        for method, suffix, body in [('GET', '', None), ('PATCH', '', {'party_size': 1}), ('POST', '/cancel', {})]:
            self.expect(404, method, '/reservations/' + booking['reference'] + suffix, body, token=other, code='not_found')
        for method, path, body in [('GET', '/reservations', None), ('POST', '/reservations', self.body()),
                                   ('POST', '/reservation-moves', {'moves': []}), ('PATCH', '/reservations/X', {}),
                                   ('POST', '/reservations/X/cancel', {})]:
            for token in (None, 'unknown'):
                self.expect(401, method, path, body, token=token, key='x', code='unauthenticated')
        for body, status, code in [({'email': 'bad', 'password': '12345678', 'display_name': 'X'}, 422, 'validation_failed'),
                                    ({'email': 'x@y', 'password': 'short', 'display_name': 'X'}, 422, 'validation_failed'),
                                    ({'email': 'a@example.test', 'password': '12345678', 'display_name': 'X'}, 409, 'email_taken')]:
            self.expect(status, 'POST', '/auth/signup', body, code=code)
        self.expect(401, 'POST', '/auth/login', {'email': 'a@example.test', 'password': 'wrong'}, code='unauthenticated')
        self.reset()
        self.expect(401, 'GET', '/reservations', token=second, code='unauthenticated')

    def test_create_errors_have_exact_status_codes_and_failed_keys_are_free(self):
        cases = [({'party_size': x}, 422, 'validation_failed') for x in (True, '2', 0, -1, 1.5, None)]
        cases += [({'table_id': 2}, 400, 'malformed_request'), ({'starts_at_local': 2}, 400, 'malformed_request'),
                  ({'starts_at_local': '2035-09-24T18:00Z'}, 422, 'validation_failed'),
                  ({'starts_at_local': '2035-09-24T18:00:00'}, 422, 'validation_failed'),
                  ({'starts_at_local': '2035-02-30T18:00'}, 422, 'validation_failed'),
                  ({'starts_at_local': '2035-09-24T18:15'}, 422, 'not_on_slot_grid'),
                  ({'starts_at_local': '2035-09-24T22:00'}, 422, 'outside_opening_hours'),
                  ({'party_size': 3}, 422, 'party_exceeds_capacity'),
                  ({'table_id': 'absent'}, 404, 'not_found'), ({'restaurant_id': 'absent'}, 404, 'not_found')]
        for change, status, code in cases:
            with self.subTest(change=change):
                before = self.snapshot()
                self.expect(status, 'POST', '/reservations', self.body(**change), token=self.token, key='retry', code=code)
                self.unchanged(before)
        self.create('retry')
        for party in ('1e9', '4.0', '+4', '-1', ' 4', '0'):
            self.expect(422, 'GET', '/availability?' + urllib.parse.urlencode({'restaurant_id': 'r1', 'date': '2035-09-24', 'party_size': party}), code='validation_failed')
        self.expect(422, 'GET', '/availability', code='validation_failed')
        for raw in (b'{', b'[]', b'null', b'1'):
            self.expect(400, 'POST', '/reservations', token=self.token, key='bad-json', raw=raw, code='malformed_request')

    def test_half_open_occupancy_cancellation_and_immutable_receipt(self):
        original = self.create()
        self.assertRegex(original['reference'], r'^[A-Z0-9]{6,12}$')
        self.assertLessEqual(len(original['reservation_id']), 64)
        for field in ('starts_at', 'ends_at', 'created_at'):
            self.assertIsNotNone(dt.datetime.fromisoformat(original[field].replace('Z', '+00:00')).utcoffset())
        self.expect(409, 'POST', '/reservations', self.body(starts_at_local='2035-09-24T19:00'), token=self.token, key='overlap', code='table_unavailable')
        adjacent = self.create('adjacent', starts_at_local='2035-09-24T19:30')
        self.assertNotEqual(original['reference'], adjacent['reference'])
        cancelled = self.expect(200, 'POST', '/reservations/' + original['reference'] + '/cancel', {}, token=self.token)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(self.expect(200, 'POST', '/reservations/' + original['reference'] + '/cancel', {}, token=self.token), cancelled)
        self.assertIn('t1', self.availability()[0]['available_table_ids'])
        before = self.snapshot()
        self.assertEqual(self.expect(200, 'POST', '/reservations', self.body(), token=self.token, key='create'), original)
        self.unchanged(before)
        self.expect(409, 'PATCH', '/reservations/' + original['reference'], {'party_size': 1}, token=self.token, code='reservation_cancelled')
        self.assertEqual(self.expect(200, 'GET', '/reservations', token=self.token)['reservations'], [adjacent, cancelled])

    def test_idempotency_json_equality_precedence_scope_and_key_boundaries(self):
        for key in (None, ''):
            self.expect(400, 'POST', '/reservations', self.body(), token=self.token, key=key, code='missing_idempotency_key')
        self.expect(422, 'POST', '/reservations', self.body(), token=self.token, key='x' * 256, code='validation_failed')
        body = self.body(ignored={'a': [1, True]})
        original = self.expect(201, 'POST', '/reservations', body, token=self.token, key='x' * 255)
        reordered = dict(reversed(list(body.items()))); reordered['party_size'] = 2.0
        self.assertEqual(self.expect(200, 'POST', '/reservations', reordered, token=self.token, key='x' * 255), original)
        for changed in ({}, self.body(party_size=False), self.body(ignored={'a': [1, 1]})):
            self.expect(409, 'POST', '/reservations', changed, token=self.token, key='x' * 255, code='idempotency_key_reuse')
        self.expect(201, 'POST', '/reservations', self.body(table_id='t2'), token=self.login('b@example.test'), key='x' * 255)
        self.expect(201, 'POST', '/reservation-moves', {'moves': [{'reference': original['reference']}]}, token=self.token, key='x' * 255)

    def test_patch_is_atomic_and_preserves_identity(self):
        first = self.create(); self.create('other', table_id='t2')
        path = '/reservations/' + first['reference']
        before = self.snapshot()
        self.expect(409, 'PATCH', path, {'table_id': 't2'}, token=self.token, code='table_unavailable')
        self.unchanged(before)
        changed = self.expect(200, 'PATCH', path, {'starts_at_local': '2035-09-24T20:00', 'party_size': 1, 'ignored': True}, token=self.token)
        for field in ('reservation_id', 'reference', 'created_at', 'restaurant_id'):
            self.assertEqual(changed[field], first[field])
        self.assertIn('t1', self.availability()[0]['available_table_ids'])
        self.assertNotIn('t1', next(s for s in self.availability() if s['starts_at_local'].endswith('20:00'))['available_table_ids'])

    def test_past_creation_allowed_but_current_cutoff_precedes_changes(self):
        original = self.create(starts_at_local='2000-01-03T18:00')
        before = self.snapshot()
        for method, suffix, body in [('POST', '/cancel', {}), ('PATCH', '', {'party_size': 'bad'})]:
            self.expect(409, method, '/reservations/' + original['reference'] + suffix, body, token=self.token, code='cutoff_passed')
        self.expect(409, 'POST', '/reservation-moves', {'moves': [{'reference': original['reference'], 'party_size': 'bad'}]}, token=self.token, key='cutoff', code='cutoff_passed')
        self.unchanged(before)

    def test_batch_swap_noop_and_replay_keep_original_response(self):
        first = self.create(); second = self.create('second', table_id='t2')
        body = {'moves': [{'reference': second['reference'], 'table_id': 't1'}, {'reference': first['reference'], 'table_id': 't2'}]}
        result = self.expect(201, 'POST', '/reservation-moves', body, token=self.token, key='swap')
        self.assertEqual([r['reference'] for r in result['reservations']], [second['reference'], first['reference']])
        self.assertEqual([r['table_id'] for r in result['reservations']], ['t1', 't2'])
        noop = {'moves': [{'reference': r['reference'], 'unknown': 1} for r in result['reservations']]}
        self.assertEqual(self.expect(201, 'POST', '/reservation-moves', noop, token=self.token, key='noop'), result)
        self.expect(200, 'POST', '/reservations/' + first['reference'] + '/cancel', {}, token=self.token)
        before = self.snapshot()
        self.assertEqual(self.expect(200, 'POST', '/reservation-moves', body, token=self.token, key='swap'), result)
        self.unchanged(before)
        self.expect(409, 'POST', '/reservation-moves', {'moves': []}, token=self.token, key='swap', code='idempotency_key_reuse')

    def test_batch_failures_rollback_every_record_and_key_with_error_precedence(self):
        first = self.create(); second = self.create('second', table_id='t2')
        cases = [([], 422, 'validation_failed'), ([{'reference': first['reference']}] * 2, 422, 'validation_failed'),
                 ([{'reference': 'MISSING'}], 404, 'not_found'),
                 ([{'reference': first['reference'], 'table_id': 't2'}, {'reference': second['reference']}], 409, 'table_unavailable'),
                 ([{'reference': first['reference'], 'table_id': 't2'}, {'reference': second['reference'], 'party_size': 0}], 422, 'validation_failed')]
        for moves, status, code in cases:
            before = self.snapshot()
            self.expect(status, 'POST', '/reservation-moves', {'moves': moves}, token=self.token, key='failed', code=code)
            self.unchanged(before)
        result = self.expect(201, 'POST', '/reservation-moves', {'moves': [{'reference': first['reference']}]}, token=self.token, key='failed')
        self.assertEqual(result, {'reservations': [first]})

    def test_fifty_concurrent_conflicts_and_replays_complete_without_duplicates(self):
        for same_key in (False, True):
            self.reset(); self.token = self.login()
            def send(index):
                return self.call('POST', '/reservations', self.body(), token=self.token, key='same' if same_key else str(index))
            started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
                results = list(pool.map(send, range(50)))
            elapsed = time.monotonic() - started
            self.assertLessEqual(elapsed, 5, '50-request burst exceeded five seconds')
            self.assertEqual(sum(status == 201 for status, _ in results), 1)
            self.assertEqual(sum(status == (200 if same_key else 409) for status, _ in results), 49)
            if same_key:
                self.assertTrue(all(body == results[0][1] for _, body in results))
            else:
                self.assertTrue(all(body['error']['code'] == 'table_unavailable' for status, body in results if status == 409))
            self.assertEqual(len(self.expect(200, 'GET', '/reservations', token=self.token)['reservations']), 1)
            print(f'50-request {"replay" if same_key else "conflict"} burst: {elapsed:.3f}s')

    def test_dst_gaps_folds_and_absolute_duration_in_both_zones(self):
        cases = [('Europe/Berlin', '2026-03-29', '2026-10-25', '02:30', '+02:00', '03:00:00+01:00'),
                 ('America/New_York', '2026-03-08', '2026-11-01', '01:30', '-04:00', '02:00:00-05:00')]
        for zone, spring, fall, repeated, offset, end in cases:
            with self.subTest(zone=zone):
                self.reset(fixture(zone, '00:00', '05:00')); self.token = self.login()
                slots = self.availability(spring)
                self.assertFalse(any(s['starts_at_local'][11:13] == '02' for s in slots))
                self.expect(422, 'POST', '/reservations', self.body(starts_at_local=spring + 'T02:30'), token=self.token, key='gap', code='invalid_local_time')
                spring_booking = self.create('spring', starts_at_local=spring + 'T01:30')
                self.assertIn('T04:00:00', spring_booking['ends_at'])
                fold_slots = [s for s in self.availability(fall) if s['starts_at_local'].endswith(repeated)]
                self.assertEqual(len(fold_slots), 1)
                self.assertTrue(fold_slots[0]['starts_at'].endswith(offset))
                booking = self.create('fold', starts_at_local=fall + 'T' + repeated)
                self.assertEqual(booking['ends_at'], fall + 'T' + end)
                self.assertEqual((dt.datetime.fromisoformat(booking['ends_at']) - dt.datetime.fromisoformat(booking['starts_at'])).total_seconds(), 5400)
                if zone == 'Europe/Berlin':
                    before_fold = self.create('before-fold', table_id='t2', starts_at_local=fall + 'T01:30')
                    self.assertEqual(before_fold['starts_at'], fall + 'T01:30:00+02:00')
                    self.assertEqual(before_fold['ends_at'], fall + 'T02:00:00+01:00')
                    self.assertEqual((dt.datetime.fromisoformat(before_fold['ends_at']) - dt.datetime.fromisoformat(before_fold['starts_at'])).total_seconds(), 5400)

    def test_import_field_types_reject_with_422_and_leave_all_state_unchanged(self):
        self.create()
        before = self.snapshot()
        self.assertEqual(before['track'], 'tablekeeper'); self.assertEqual(before['format_version'], 1)
        self.assertIsInstance(before['state'], dict)
        for field, values in [('track', [None, 1, [], {}, 'wrong']), ('format_version', [None, '1', True, [], {}, 2]),
                              ('state', [None, [], '', 1, True, {}])]:
            for value in values:
                with self.subTest(field=field, value=value):
                    bad = copy.deepcopy(before); bad[field] = value
                    self.expect(422, 'POST', '/_test/import', bad, code='validation_failed')
                    self.unchanged(before)
            bad = copy.deepcopy(before); del bad[field]
            self.expect(422, 'POST', '/_test/import', bad, code='validation_failed')
            self.unchanged(before)
        for raw in (b'{', b'null', b'[]', b'"text"', b'1', b'true'):
            self.expect(400, 'POST', '/_test/import', raw=raw, code='malformed_request')
            self.unchanged(before)

    def test_dst_closing_boundaries_and_non_hour_grid_agree_with_booking(self):
        for day, closes, expected in [('2026-10-25', '02:30', ['00:00', '00:30', '01:00']),
                                      ('2026-03-29', '02:30', ['00:00', '00:30'])]:
            self.reset(fixture('Europe/Berlin', '00:00', closes)); self.token = self.login()
            slots = self.availability(day)
            self.assertEqual([s['starts_at_local'][-5:] for s in slots], expected)
            self.create('last', starts_at_local=day + 'T' + expected[-1])
            excluded = '01:30' if '10-25' in day else '01:00'
            self.expect(422, 'POST', '/reservations', self.body(starts_at_local=day + 'T' + excluded), token=self.token, key='outside', code='outside_opening_hours')
        self.reset(fixture(opens='18:10')); self.token = self.login()
        self.assertEqual(self.availability()[0]['starts_at_local'][-5:], '18:10')
        self.create(starts_at_local='2035-09-24T18:10')
        self.expect(422, 'POST', '/reservations', self.body(starts_at_local='2035-09-24T19:00'), token=self.token, key='grid', code='not_on_slot_grid')

    def test_seeded_identity_and_same_table_id_in_distinct_restaurants(self):
        data = fixture()
        second = copy.deepcopy(data['restaurants'][0]); second['id'] = 'r2'
        data['restaurants'].append(second)
        data['reservations'] = [dict(self.body(), id='seed-id', reference='SEED01', user_id='u1')]
        self.reset(data); self.token = self.login()
        seeded = self.expect(200, 'GET', '/reservations/SEED01', token=self.token)
        self.assertEqual(seeded['reservation_id'], 'seed-id')
        self.assertEqual(seeded['status'], 'confirmed')
        self.create('independent', restaurant_id='r2')
        self.expect(409, 'POST', '/reservations', self.body(), token=self.token, key='occupied', code='table_unavailable')

    def test_fixture_id_limits_and_unknown_fields_preserve_contract(self):
        data = fixture()
        data['users'][0]['id'] = 'u' * 64
        data['restaurants'][0]['id'] = 'r' * 64
        data['restaurants'][0]['tables'][0]['id'] = 't' * 64
        data['unknown'] = {'ignored': True}
        self.reset(data); self.token = self.login()
        created = self.create(restaurant_id='r' * 64, table_id='t' * 64, ignored='accepted')
        self.assertEqual(created['restaurant_id'], 'r' * 64)
        before = self.snapshot()
        for field in ('user', 'restaurant', 'table'):
            invalid = copy.deepcopy(data)
            target = invalid['users'][0] if field == 'user' else invalid['restaurants'][0]
            if field == 'table':
                target = target['tables'][0]
            target['id'] = 'x' * 65
            self.expect(422, 'POST', '/_test/reset', invalid, code='validation_failed')
            self.unchanged(before)

    def test_signup_concurrency_and_unicode_password_length(self):
        body = {'email': 'unicode@example.test', 'password': 'é' * 8, 'display_name': 'Zoë', 'unknown': True}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.call('POST', '/auth/signup', body), range(2)))
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        failure = next(value for status, value in results if status == 409)
        self.assertEqual(failure['error']['code'], 'email_taken')
        logged = self.expect(200, 'POST', '/auth/login', {'email': body['email'], 'password': body['password']})
        self.assertEqual(logged['display_name'], body['display_name'])
        self.expect(422, 'POST', '/auth/signup', dict(body, email='short@example.test', password='é' * 7), code='validation_failed')

    def test_batch_shape_one_eight_nine_and_cross_owner_restaurant(self):
        data = fixture()
        second = copy.deepcopy(data['restaurants'][0]); second['id'] = 'r2'
        data['restaurants'].append(second)
        self.reset(data); self.token = self.login()
        bookings = [self.create(str(i), starts_at_local=f'2035-09-{i + 10:02}T18:00') for i in range(9)]
        for moves in (None, {}, 'bad', [None], [{'reference': 2}], [{'reference': b['reference']} for b in bookings]):
            before = self.snapshot()
            self.expect(422, 'POST', '/reservation-moves', {'moves': moves}, token=self.token, key='shape', code='validation_failed')
            self.unchanged(before)
        eight = {'moves': [{'reference': b['reference']} for b in bookings[:8]]}
        self.assertEqual(self.expect(201, 'POST', '/reservation-moves', eight, token=self.token, key='shape'), {'reservations': bookings[:8]})
        foreign = self.expect(201, 'POST', '/reservations', self.body(table_id='t2'), token=self.login('b@example.test'), key='foreign')
        other_restaurant = self.create('restaurant', restaurant_id='r2')
        for other, status, code in [(foreign, 404, 'not_found'), (other_restaurant, 422, 'validation_failed')]:
            before = self.snapshot()
            self.expect(status, 'POST', '/reservation-moves', {'moves': [{'reference': bookings[0]['reference']}, {'reference': other['reference']}]}, token=self.token, key='bad-owner', code=code)
            self.unchanged(before)

    def test_fifty_auth_requests_keep_both_session_and_latency_contract(self):
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            tokens = list(pool.map(lambda _: self.login(), range(50)))
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, 5)
        for token in tokens:
            self.assertEqual(self.expect(200, 'GET', '/reservations', token=token), {'reservations': []})
        print(f'50-login burst: {elapsed:.3f}s')

    def test_same_key_different_bodies_race_records_only_winning_body(self):
        bodies = [self.body(table_id='t1' if i % 2 else 't2') for i in range(50)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda body: self.call('POST', '/reservations', body, token=self.token, key='race'), bodies))
        self.assertEqual(sum(status == 201 for status, _ in results), 1)
        winner = next(body for status, body in results if status == 201)
        for sent, (status, result) in zip(bodies, results):
            if sent['table_id'] == winner['table_id']:
                self.assertIn(status, (200, 201)); self.assertEqual(result, winner)
            else:
                self.assertEqual(status, 409); self.assertEqual(result['error']['code'], 'idempotency_key_reuse')
        self.assertEqual(self.expect(200, 'GET', '/reservations', token=self.token), {'reservations': [winner]})

    def test_patch_cancel_race_cannot_resurrect_cancelled_occupancy(self):
        original = self.create()
        path = '/reservations/' + original['reference']
        jobs = [('PATCH', path, {'table_id': 't2'}), ('POST', path + '/cancel', {})]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda job: self.call(*job, token=self.token), jobs))
        self.assertEqual(results[1][0], 200)
        self.assertIn(results[0][0], (200, 409))
        if results[0][0] == 409:
            self.assertEqual(results[0][1]['error']['code'], 'reservation_cancelled')
        current = self.expect(200, 'GET', path, token=self.token)
        self.assertEqual(current['status'], 'cancelled')
        self.assertEqual(self.availability()[0]['available_table_ids'], ['t1', 't2'])

    def test_export_import_preserves_sessions_identity_and_both_receipts(self):
        second_token = self.login()
        original = self.create()
        moves = {'moves': [{'reference': original['reference'], 'table_id': 't2'}]}
        receipt = self.expect(201, 'POST', '/reservation-moves', moves, token=self.token, key='batch')
        cancelled = self.expect(200, 'POST', '/reservations/' + original['reference'] + '/cancel', {}, token=self.token)
        exported = self.snapshot()
        self.create('after-export', starts_at_local='2035-09-24T20:00')
        self.reset(base=PEER)
        destination_token = self.login(base=PEER)
        for _ in range(2):
            self.expect(204, 'POST', '/_test/import', exported, base=PEER)
            self.expect(401, 'GET', '/reservations', token=destination_token, base=PEER, code='unauthenticated')
            for token in (self.token, second_token):
                self.assertEqual(self.expect(200, 'GET', '/reservations', token=token, base=PEER), {'reservations': [cancelled]})
            self.assertEqual(self.expect(200, 'POST', '/reservations', self.body(), token=self.token, key='create', base=PEER), original)
            self.assertEqual(self.expect(200, 'POST', '/reservation-moves', moves, token=self.token, key='batch', base=PEER), receipt)
            self.unchanged(exported, PEER)
        self.login(base=PEER)
        self.reset(base=PEER)
        self.expect(401, 'GET', '/reservations', token=self.token, base=PEER, code='unauthenticated')

    def test_invalid_internal_import_state_rolls_back_accounts_occupancy_and_receipts(self):
        # Export state is implementation-defined: these corruption locations use
        # the delivered schema. Assertions remain entirely at the HTTP boundary.
        original = self.create()
        self.expect(201, 'POST', '/reservation-moves', {'moves': [{'reference': original['reference']}]}, token=self.token, key='batch')
        before = self.snapshot()
        rid = original['reservation_id']
        cases = []
        def corrupt(label, mutate):
            value = copy.deepcopy(before)
            mutate(value['state'])
            cases.append((label, value))
        for collection in ('users', 'tokens', 'restaurants', 'reservations', 'receipts'):
            corrupt('missing ' + collection, lambda s, k=collection: s.pop(k))
            corrupt('wrong type ' + collection, lambda s, k=collection: s.__setitem__(k, None))
        corrupt('orphan token', lambda s: s['tokens'].__setitem__('synthetic_orphan', 'absent'))
        corrupt('wrong user ID', lambda s: s['users']['u1'].__setitem__('id', 'mismatch'))
        corrupt('duplicate email', lambda s: s['users']['u2'].__setitem__('email', s['users']['u1']['email']))
        corrupt('unsafe hash parameters', lambda s: s['users']['u1']['credential'].__setitem__('n', 1))
        corrupt('invalid digest', lambda s: s['users']['u1']['credential'].__setitem__('digest', 'bad'))
        corrupt('invalid salt', lambda s: s['users']['u1']['credential'].__setitem__('salt', 'bad'))
        corrupt('wrong restaurant ID', lambda s: s['restaurants']['r1'].__setitem__('id', 'mismatch'))
        corrupt('invalid timezone', lambda s: s['restaurants']['r1'].__setitem__('timezone', 'Invalid/Zone'))
        corrupt('duplicate table ID', lambda s: s['restaurants']['r1']['tables'].append(copy.deepcopy(s['restaurants']['r1']['tables'][0])))
        for field, value in [('user_id', 'absent'), ('reservation_id', 'mismatch'), ('reference', 'bad'),
                             ('status', 'unknown'), ('created_at', 'not-a-time'), ('ends_at', '2035-09-24T23:00:00+02:00'),
                             ('table_id', 'absent'), ('party_size', True)]:
            corrupt('invalid reservation ' + field, lambda s, f=field, v=value: s['reservations'][rid].__setitem__(f, v))
        def overlap(state):
            duplicate = copy.deepcopy(state['reservations'][rid])
            duplicate.update(reservation_id='duplicate', reference='DUPL01')
            state['reservations']['duplicate'] = duplicate
        corrupt('confirmed overlap', overlap)
        def duplicate_reference(state):
            duplicate = copy.deepcopy(state['reservations'][rid])
            duplicate.update(reservation_id='duplicate', status='cancelled')
            state['reservations']['duplicate'] = duplicate
        corrupt('duplicate reference', duplicate_reference)
        corrupt('duplicate receipt', lambda s: s['receipts'].append(copy.deepcopy(s['receipts'][0])))
        for field, value in [('user_id', 'absent'), ('method', 'GET'), ('path', '/unknown'), ('key', ''),
                             ('request', []), ('response', {})]:
            corrupt('invalid receipt ' + field, lambda s, f=field, v=value: s['receipts'][0].__setitem__(f, v))
        corrupt('batch receipt missing record', lambda s: s['receipts'][1]['response'].__setitem__('reservations', []))
        corrupt('batch receipt wrong reference', lambda s: s['receipts'][1]['request']['moves'][0].__setitem__('reference', 'WRONG1'))
        for label, invalid in cases:
            with self.subTest(corruption=label):
                self.expect(422, 'POST', '/_test/import', invalid, code='validation_failed')
                self.unchanged(before)
                self.assertEqual(self.expect(200, 'GET', '/reservations/' + original['reference'], token=self.token), original)
                self.assertEqual(self.expect(200, 'POST', '/reservations', self.body(), token=self.token, key='create'), original)
        print(f'Invalid internal import cases: {len(cases)}; complete state and live token/receipt checked after each')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--peer-url', required=True)
    args, remaining = parser.parse_known_args()
    BASE, PEER = args.base_url.rstrip('/'), args.peer_url.rstrip('/')
    unittest.main(argv=[__file__] + remaining, verbosity=2)
