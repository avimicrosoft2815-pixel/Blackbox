PY ?= .venv/bin/python
PYTHONPATH := src

.PHONY: test test-fast smoke bench kill fuzz docs fmt clean verify

test:            ## full suite (stdlib guards, property tests, crash matrix)
	PYTHONPATH=$(PYTHONPATH) $(PY) -m pytest tests -q

test-fast:       ## skip the process-spawning crash matrix
	PYTHONPATH=$(PYTHONPATH) $(PY) -m pytest tests -q -k "not crash_points and not kill"

smoke:           ## bench smoke against docs/STATE.json (C31): fails on a missing key or a 10 % regression
	$(PY) tools/bench_smoke.py

bench:           ## P1-17 per-stage microbench into bench/results/
	PYTHONPATH=$(PYTHONPATH) $(PY) bench/bench_frame.py

kill:            ## randomized parent-SIGKILL matrix (P2-09)
	PYTHONPATH=$(PYTHONPATH) $(PY) tests/kill/kill_matrix.py --trials 500

fuzz:            ## CBOR decoder fuzz (P1-03)
	PYTHONPATH=$(PYTHONPATH) $(PY) bench/fuzz_cbor.py --iterations 100000

docs:            ## regenerate the progress ledger from the plan
	$(PY) tools/gen_progress.py

fmt:             ## C6: lint and type-check (dev tools only; the package has no runtime deps)
	$(PY) -m ruff check src tests bench tools
	$(PY) -m mypy
	$(PY) -m compileall -q src tests

lint: fmt        ## alias

clean:
	rm -rf .pytest_cache **/__pycache__ src/**/__pycache__
