# Stage 2 tests

Run commands from the `stage-2` directory. The portable API/model tests and deterministic
core-state tests are:

```sh
python -B -m unittest discover -s tests -p 'test_stage2.py' -v
python -B -m unittest discover -s tests -p 'test_core_*.py' -v
python -B -m unittest discover -s tests -p 'test_packaging.py' -v
```

`test_packaging.py` checks that the app assets and test contracts are included in the
standalone image, along with its timezone, non-root, port and health-check configuration.

`test_stage2.py` exercises the API against the Stage 1 and Stage 2 sibling directories,
including state transfer. It therefore requires the complete result repository layout.
The core tests are self-contained and can also run in the Stage 2 image.

The browser tests require Microsoft Edge and Node.js on the host:

```sh
python -B -m unittest discover -s tests -p 'test_stage2_ui_browser.py' -v
python -B -m unittest discover -s tests -p 'test_stage2_browser.py' -v
```

For the copied Stage 1 HTTP contract suite, run the managed runner. It starts two isolated
Stage 2 service processes on temporary ports, then verifies the HTTP contracts and
source-stop state-transfer behavior:

```sh
python -B tests/run_managed.py
```

To run the contract against already-running services instead, start two instances from the
Stage 2 directory, using ports `8080` and `8081`, then run:

```sh
python -B tests/contract_stage1.py --base-url http://127.0.0.1:8080 --peer-url http://127.0.0.1:8081
```

Tests mutate only their isolated in-memory services. Do not point them at services
containing valuable data. Snapshots and credentials are kept in memory and are not printed.
The HTTP latency assertion remains at its specified five-second limit. Native test results
do not verify Docker build, container resources or runtime network isolation.
