# Stage 3 tests

Run commands from the `stage-3` directory. Stage 3 policy and inherited API/model tests,
plus deterministic core-state tests, are:

```sh
python -B -m unittest discover -s tests -p 'test_stage3_policies.py' -v
python -B -m unittest discover -s tests -p 'test_stage3_availability.py' -v
python -B -m unittest discover -s tests -p 'test_stage3_history.py' -v
python -B -m unittest discover -s tests -p 'test_stage3_series.py' -v
python -B -m unittest discover -s tests -p 'test_stage3_moves.py' -v
python -B -m unittest discover -s tests -p 'test_stage3_import_terms.py' -v
python -B -m unittest discover -s tests -p 'test_stage2.py' -v
python -B -m unittest discover -s tests -p 'test_core_*.py' -v
python -B -m unittest discover -s tests -p 'test_packaging.py' -v
```

`test_packaging.py` checks that the app assets and test contracts are included in the
standalone image, along with its timezone, non-root, port and health-check configuration.

`test_stage3_policies.py`, `test_stage3_availability.py`, `test_stage3_history.py`,
`test_stage3_series.py`, `test_stage3_moves.py` and `test_stage3_import_terms.py` exercise
Stage 3 policy selection, accepted terms, revisions, history, recurring-series,
collective-move and imported snapshot integrity behavior.
`test_stage2.py` exercises the inherited Stage 1
and Stage 2 APIs against their sibling directories, including state transfer, so it
requires the complete result repository layout. The core tests run against Stage 3's
public dispatcher and are self-contained.

The browser tests require Microsoft Edge and Node.js on the host:

```sh
python -B -m unittest discover -s tests -p 'test_stage2_ui_browser.py' -v
python -B -m unittest discover -s tests -p 'test_stage2_browser.py' -v
```

For the copied Stage 1 HTTP contract suite against Stage 3, run the managed runner. It starts
two isolated Stage 3 service processes on temporary ports, then verifies the HTTP contracts and
source-stop state-transfer behavior:

```sh
python -B tests/run_managed.py
```

To run the contract against already-running services instead, start two instances from the
Stage 3 directory, using ports `8080` and `8081`, then run:

```sh
python -B tests/contract_stage1.py --base-url http://127.0.0.1:8080 --peer-url http://127.0.0.1:8081
```

Tests mutate only their isolated in-memory services. Do not point them at services
containing valuable data. Snapshots and credentials are kept in memory and are not printed.
The HTTP latency assertion remains at its specified five-second limit. Native test results
do not verify Docker build, container resources or runtime network isolation.
