# Tablekeeper Stage 2

Run these commands from the `stage-2` directory. Build the standalone image:

```sh
docker build -t tablekeeper-stage2 .
docker run --rm --name tablekeeper-stage2 -p 8080:8080 --cpus=2 --memory=2g tablekeeper-stage2
```

The image starts with empty in-memory state and listens on `0.0.0.0`. `PORT` defaults to
`8080`; the health check uses the same port. To use another port:

```sh
docker run --rm --name tablekeeper-stage2 -e PORT=8097 -p 8097:8097 --cpus=2 --memory=2g tablekeeper-stage2
curl http://localhost:8097/health
```

Open `http://localhost:8080/` to search. `/signup`, `/login` and `/lookup` are direct HTML
routes. The image contains the app and its browser assets; serving pages makes no outbound
requests. No database, volume, runtime download or manual initialization is needed.

To load a fixture in the Stage 2 specification's JSON format and log in as a seeded user:

```sh
curl -i -X POST http://localhost:8080/_test/reset -H 'Content-Type: application/json' --data-binary @fixture.json
curl -X POST http://localhost:8080/auth/login -H 'Content-Type: application/json' -d '{"email":"ada@example.com","password":"correct horse"}'
```

Set the `Authorization` header to `Bearer` followed by the token returned by login.
Reservation creation and atomic moves also require an `Idempotency-Key`.
Export/import/reset are unauthenticated test controls; exports contain password hashes
and bearer sessions and should be treated as private test data.

Restaurants may declare approved unordered table pairs with `combinable`. Availability
lists those choices in `available_options`; `available_table_ids` remains singles-only.
Combined reservations occupy both member tables for every reservation and move operation.

State is held in memory by one Python process. A process-wide lock commits reservations
and their immutable retry receipts atomically. Restarting the container clears state.
Export/import transfers accounts, sessions, configuration, reservations and receipts
between independent instances. Do not run multiple worker processes behind one endpoint.

The Python standard library supplies HTTP, scrypt and `zoneinfo`; IANA timezone data is
installed in the image at build time. The runtime needs no package manager or outbound
network access. Password work is limited to two concurrent scrypt operations; no hashing
or socket I/O occurs while holding the state lock. Duration and occupancy use UTC; local
slots resolve to the first occurrence of an ambiguous time and omit nonexistent times.
When an opening/closing boundary falls in a DST gap, its window boundary is the first
valid instant after the gap; the grid remains anchored to the written opening time.

To check the image with networking disabled (run after building it):

```sh
docker run --network none --cpus=2 --memory=2g -e PORT=8097 --name tablekeeper-offline -d tablekeeper-stage2
docker exec tablekeeper-offline python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8097/health').read().decode())"
docker rm -f tablekeeper-offline
```

The container includes the portable core tests:

```sh
docker run --rm tablekeeper-stage2 python -B -m unittest discover -s tests -p 'test_core_*.py' -v
```

For local development, run `python -m app` from `stage-2` with Python 3.12+ and an
installed IANA timezone database. On Windows, the `tzdata` Python package supplies it;
the container installs timezone data during the image build instead.

See [`tests/README.md`](tests/README.md) for the Stage 2 API, browser and Stage 1
regression commands. Docker tests and an offline container run are necessary to verify
the image itself; native tests alone do not establish container, resource or isolation
compliance.
