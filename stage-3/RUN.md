# Tablekeeper Stage 3

Run these commands from the `stage-3` directory to build and run the standalone service:

```sh
docker build -t tablekeeper-stage3 .
docker run --rm --name tablekeeper-stage3 -p 8080:8080 --cpus=2 --memory=2g tablekeeper-stage3
```

The container starts with empty in-memory state and listens on `0.0.0.0`. `PORT` defaults
to `8080`; the health check uses the same port. To use another port:

```sh
docker run --rm --name tablekeeper-stage3 -e PORT=8097 -p 8097:8097 --cpus=2 --memory=2g tablekeeper-stage3
curl http://localhost:8097/health
```

Open `http://localhost:8080/` to search. `/signup`, `/login` and `/lookup` are direct HTML
routes. The Stage 2 consumer booking screens and local browser assets are included; the
runtime makes no outbound requests. No database, volume, runtime download or manual
initialization is needed.

To load a fixture in the Stage 3 specification's JSON format and log in as a seeded user:

```sh
curl -i -X POST http://localhost:8080/_test/reset -H 'Content-Type: application/json' --data-binary @fixture.json
curl -X POST http://localhost:8080/auth/login -H 'Content-Type: application/json' -d '{"email":"ada@example.com","password":"correct horse"}'
```

Set the `Authorization` header to `Bearer` followed by the token returned by login.
Reservation creation, policy publication, recurring-series creation and atomic moves
require an `Idempotency-Key`. Export/import/reset are unauthenticated test controls;
exports contain password hashes and bearer sessions and should be treated as private test
data.

Stage 3 preserves Stage 1 reservations/authentication and Stage 2 web booking and approved
unordered two-table combinations. Restaurants can publish complete, immutable, dated
policies. Availability and booking decisions use the policy selected for each local start
date; `explain=true` reports capacity and overlap decisions. Reservations retain accepted
policy terms, revisions and immutable history, with owner-only history/decision and series
reads. Recurring series adopt an existing confirmed reservation and create policy-aware
occurrences. Individual and collective amendments, cancellations, replay receipts and
series exceptions remain atomic and owner-scoped.

State is held in memory by one Python process and a process-wide lock commits changes and
retry receipts atomically. Restarting the container clears state. Export/import transfers
accounts, sessions, policies, reservations, series and receipts between independent
instances. Do not run multiple worker processes behind one endpoint.

The Python standard library supplies HTTP, scrypt and `zoneinfo`; IANA timezone data is
installed in the image at build time. The runtime needs no package manager or outbound
network access. Password work is limited to two concurrent scrypt operations; no hashing
or socket I/O occurs while holding the state lock. Duration and occupancy use UTC; local
slots resolve to the first occurrence of an ambiguous time and omit nonexistent times.
When an opening/closing boundary falls in a DST gap, its window boundary is the first
valid instant after the gap; the grid remains anchored to the written opening time.

To check the image with networking disabled (run after building it):

```sh
docker run --network none --cpus=2 --memory=2g -e PORT=8097 --name tablekeeper-stage3-offline -d tablekeeper-stage3
docker exec tablekeeper-stage3-offline python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8097/health').read().decode())"
docker rm -f tablekeeper-stage3-offline
```

The container includes the portable core tests:

```sh
docker run --rm tablekeeper-stage3 python -B -m unittest discover -s tests -p 'test_core_*.py' -v
```

For local development, run `python -m app` from `stage-3` with Python 3.12+ and an
installed IANA timezone database. On Windows, the `tzdata` Python package supplies it;
the container installs timezone data during the image build instead.

See [`tests/README.md`](tests/README.md) for the Stage 3 policy, availability, history,
series, collective-move, Stage 2 API/import, browser and Stage 1 HTTP regression commands.
The Stage 3 isolated harness workflow is
[`../.github/workflows/stage3-harness.yml`](../.github/workflows/stage3-harness.yml).
Docker tests and an offline container run are necessary to verify the image itself; native
tests alone do not establish container, resource or isolation compliance.
