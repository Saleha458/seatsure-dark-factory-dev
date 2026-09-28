"""Run HTTP contracts against two isolated service processes, then stop source.

Uses the documented `python -m app` startup contract. Credentials/snapshots only
exist in memory. This is native-process evidence, not container evidence.
"""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

import contract_stage1 as contract


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def start(python, port):
    env = dict(os.environ, PORT=str(port), PYTHONDONTWRITEBYTECODE='1')
    process = subprocess.Popen([python, '-B', '-m', 'app'], cwd=Path(__file__).resolve().parents[1],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'Service exited before health with code {process.returncode}; run documented launch directly for diagnostics')
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                if response.status == 200:
                    return process
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    process.terminate(); process.wait(timeout=10)
    raise RuntimeError('Service startup exceeded 60 seconds')


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        process.wait(timeout=10)


def source_stop_transfer(source):
    check = contract.Contracts()
    check.setUp()
    token = check.token
    original = check.create()
    moves = {'moves': [{'reference': original['reference'], 'table_id': 't2'}]}
    receipt = check.expect(201, 'POST', '/reservation-moves', moves, token=token, key='batch')
    cancelled = check.expect(200, 'POST', '/reservations/' + original['reference'] + '/cancel', {}, token=token)
    check.expect(422, 'POST', '/reservations', check.body(party_size=0), token=token, key='failed', code='validation_failed')
    exported = check.snapshot()
    check.create('post-snapshot', starts_at_local='2035-09-24T20:00')
    stop(source)
    check.assertIsNotNone(source.poll(), 'Source must be stopped before import')
    check.reset(base=contract.PEER)
    destination_token = check.login(base=contract.PEER)
    for _ in range(2):
        check.expect(204, 'POST', '/_test/import', exported, base=contract.PEER)
        check.expect(401, 'GET', '/reservations', token=destination_token, base=contract.PEER, code='unauthenticated')
        check.assertEqual(check.expect(200, 'GET', '/reservations', token=token, base=contract.PEER), {'reservations': [cancelled]})
        check.assertEqual(check.expect(200, 'POST', '/reservations', check.body(), token=token, key='create', base=contract.PEER), original)
        check.assertEqual(check.expect(200, 'POST', '/reservation-moves', moves, token=token, key='batch', base=contract.PEER), receipt)
        check.unchanged(exported, contract.PEER)
    check.login(base=contract.PEER)
    check.expect(201, 'POST', '/reservations', check.body(), token=token, key='failed', base=contract.PEER)
    print('PASS source-stop import: two independent processes, source terminated before import, receipts/tokens/identities preserved, failed key reusable')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    source = peer = None
    try:
        source_port = free_port()
        source = start(args.python, source_port)
        peer_port = free_port()
        peer = start(args.python, peer_port)
        contract.BASE = f'http://127.0.0.1:{source_port}'
        contract.PEER = f'http://127.0.0.1:{peer_port}'
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(contract.Contracts)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        source_stop_transfer(source)
        sys.exit(0 if result.wasSuccessful() else 1)
    finally:
        stop(source)
        stop(peer)
