# Tablekeeper Stage 1

From the result repository root, build and start the standalone service:

```sh
docker build -t tablekeeper-stage1 ./stage-1
docker run --rm --name tablekeeper-stage1 -e PORT=8080 -p 8080:8080 --cpus=2 --memory=2g tablekeeper-stage1
```

No database, volume, runtime download, or manual initialization is needed. The service starts
with empty state. It listens on `0.0.0.0`; `PORT` defaults to `8080`. For another port:

```sh
docker run --rm --name tablekeeper-stage1 -e PORT=8097 -p 8097:8097 --cpus=2 --memory=2g tablekeeper-stage1
curl http://localhost:8097/health
```

Reset from a fixture in the Stage 1 specification's JSON format, then log in as a seeded user:

```sh
curl -i -X POST http://localhost:8080/_test/reset -H 'Content-Type: application/json' --data-binary @fixture.json
curl -X POST http://localhost:8080/auth/login -H 'Content-Type: application/json' -d '{"email":"ada@example.com","password":"correct horse"}'
```

Use the returned token as `Authorization: Bearer <token>`. Creation and atomic moves also
require `Idempotency-Key`. Export/import/reset are unauthenticated test controls; exports
contain password hashes and bearer sessions and should be treated as private test data.

State is in memory in one Python process. A process-wide lock commits reservations and their
immutable retry receipts atomically. Restarting the container clears state. Export/import
transfers all accounts, sessions, configuration, reservations and receipts between independent
instances. Do not run multiple worker processes behind the same endpoint.

Python's bundled standard library supplies HTTP, scrypt and zoneinfo. IANA timezone assets are
installed during the image build. Password work is limited to two concurrent scrypt operations;
no hashing or socket I/O occurs while holding the state lock. Duration and occupancy use UTC;
local slots resolve to the first occurrence of an ambiguous time and omit nonexistent times.
When an opening/closing boundary itself falls in a DST gap, its window boundary is the first
valid instant following the gap; the grid remains anchored to the written opening time.

Offline runtime check:

```sh
docker run --network none --cpus=2 --memory=2g -e PORT=8097 --name tablekeeper-offline -d tablekeeper-stage1
docker exec tablekeeper-offline python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8097/health').read().decode())"
docker rm -f tablekeeper-offline
```

Independent tests (start a second instance on port 8081 for state-transfer checks):

```sh
docker run --rm --name tablekeeper-peer -e PORT=8081 -p 8081:8081 --cpus=2 --memory=2g tablekeeper-stage1
python stage-1/tests/contract_stage1.py --base-url http://127.0.0.1:8080 --peer-url http://127.0.0.1:8081
docker run --rm tablekeeper-stage1 python -m unittest discover -s tests -p 'test_core_*.py'
```

For local development only, run `python -m app` from `stage-1` with Python 3.12+ and an
installed IANA timezone database (the `tzdata` package provides one on Windows). The submitted
container does not depend on Python or timezone packages installed on the host.
