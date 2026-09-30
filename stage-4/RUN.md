# Tablekeeper Stage 4

Run these commands from the `stage-4` directory to build and run the standalone service:

```sh
docker build -t tablekeeper-stage4 .
docker run --rm --name tablekeeper-stage4 -p 8080:8080 --cpus=2 --memory=2g tablekeeper-stage4
```

The container starts with empty in-memory state and listens on `0.0.0.0`. `PORT` defaults
to `8080`; the health check uses the same port. To use another port:

```sh
docker run --rm --name tablekeeper-stage4 -e PORT=8097 -p 8097:8097 --cpus=2 --memory=2g tablekeeper-stage4
curl http://localhost:8097/health
```

Open `http://localhost:8080/` to search. `/signup`, `/login` and `/lookup` are direct HTML
routes. The Stage 2 consumer booking screens and local browser assets are included; the
runtime makes no outbound requests. No database, volume, runtime download or manual
initialization is needed.

To load a fixture in the Stage 4 specification's JSON format and log in as a seeded user:

```sh
curl -i -X POST http://localhost:8080/_test/reset -H 'Content-Type: application/json' --data-binary @fixture.json
curl -X POST http://localhost:8080/auth/login -H 'Content-Type: application/json' -d '{"email":"ada@example.com","password":"correct horse"}'
```

Set the `Authorization` header to `Bearer` followed by the token returned by login.
Reservation creation, policy publication, recurring-series creation, atomic moves,
closure replan preview/apply and series clock amendments require an `Idempotency-Key`.
Export/import/reset are unauthenticated test controls; exports contain password hashes
and bearer sessions and should be treated as private test data.

Stage 4 preserves Stage 1–3 behavior and adds manager seating-change replans (preview +
atomic apply), applied table closures that affect availability and booking, and
owner-scoped recurring series clock-time amendments. Restaurant revision increments once
per successful new booking, real amendment, cancellation, policy publication or plan
application. Closures exclude singles and pairs from availability and reject conflicting
creates/amendments with `table_unavailable`; explanations report `no_overlap` false for
a closed table. Series amendments do not mark exceptions.

State is held in memory by one Python process and a process-wide lock commits changes and
retry receipts atomically. Restarting the container clears state. Export/import transfers
accounts, sessions, policies, reservations, series, closures, plans and receipts between
independent instances, and accepts Stage 1–3 exports. Do not run multiple worker processes
behind one endpoint.

The Python standard library supplies HTTP, scrypt and `zoneinfo`; IANA timezone data is
installed in the image at build time. The runtime needs no package manager or outbound
network access. Password work is limited to two concurrent scrypt operations; no hashing
or socket I/O occurs while holding the state lock. Duration and occupancy use UTC; local
slots resolve to the first occurrence of an ambiguous time and omit nonexistent times.
When an opening/closing boundary falls in a DST gap, its window boundary is the first
valid instant after the gap; the grid remains anchored to the written opening time.

To check the image with networking disabled (run after building it):

```sh
docker run --network none --cpus=2 --memory=2g -e PORT=8097 --name tablekeeper-stage4-offline -d tablekeeper-stage4
docker exec tablekeeper-stage4-offline python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8097/health').read().decode())"
docker rm -f tablekeeper-stage4-offline
```

The container includes the portable core tests:

```sh
docker run --rm tablekeeper-stage4 python -B -m unittest discover -s tests -p 'test_core_*.py' -v
```

For local development, run `python -m app` from `stage-4` with Python 3.12+ and an
installed IANA timezone database. On Windows, the `tzdata` Python package supplies it;
the container installs timezone data during the image build instead.

See [`tests/README.md`](tests/README.md) for the Stage 4 replan/series-amend, Stage 3
policy/history/series/moves/import, Stage 2 API/browser and Stage 1 HTTP regression
commands. The Stage 4 isolated harness workflow is
[`../.github/workflows/stage4-harness.yml`](../.github/workflows/stage4-harness.yml).
Docker tests and an offline container run are necessary to verify the image itself; native
tests alone do not establish container, resource or isolation compliance.
