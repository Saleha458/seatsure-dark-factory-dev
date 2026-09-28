# Independent Stage 1 tests

`python -B stage-1/tests/contract_stage1.py --base-url http://127.0.0.1:8080 --peer-url http://127.0.0.1:8081`

`python -B stage-1/tests/run_managed.py` starts two independent `python -m app` processes, runs the HTTP checks, terminates the source, and proves that the destination can import and replay without it. Both commands mutate only the supplied test services. Do not point them at services containing valuable data. Snapshots and credentials stay in memory and are not printed.

The assertions derive from the full Stage 1 spec and accepted r2 design, not sample tests. R1/R7/R11 are covered by conflict/replay races, full-export rollback, swaps, no-op batches and receipt checks. R3-R6/R8 cover wire contracts, fixtures, public access, auth, privacy, validation and occupancy. R9 covers both documented zones, gaps/folds, absolute duration and closing boundaries. R10 covers envelope error types, atomic replacement and two-process portability. R2 timing checks measure HTTP round trips; native execution does not establish Docker/resource/offline compliance.

Remaining evidence must be reported explicitly: Docker build/default+custom PORT/offline runtime under 2CPU/2GiB; deterministic exact-cutoff and stale-auth replacement race checks require an agreed injected-clock/hash barrier API. No green result from this suite alone claims full acceptance.
