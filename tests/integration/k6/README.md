# tests/integration/k6/

## Purpose

k6 (`grafana/k6`, run via Docker) load-test scripts driven by
`tests/integration/test_perf_k6_load.py`. Each script targets a real,
out-of-process uvicorn instance over the host network (`host.docker.internal`)
and writes a JSON summary the Python test asserts against.

## Modules

| File | Responsibility |
|---|---|
| `load_events.js` | Constant-arrival-rate load against `POST /v1/events` (no-quota baseline scenario), distributed customer key space. |
| `load_events_quota.js` | Quota-active variant of `load_events.js` (AC20): identical sustained constant-arrival-rate scenario, but every `(customer, metric)` bucket carries a pre-seeded, deliberately high quota (never exceeded) so the `FOR UPDATE` read-and-decide path runs on every request without ever rejecting — isolates the quota check's added latency from rejection-path effects. |
| `load_usage.js` | Constant-arrival-rate load against `GET /v1/usage`. |

## Relationships

- Invoked exclusively via `subprocess.Popen`/Docker from
  `tests/integration/test_perf_k6_load.py`, never run standalone in CI — the Python
  test owns fixture seeding (including quota rows for `load_events_quota.js`), worker
  budget parity between the baseline and quota-active runs, and summary-JSON parsing.
- `BASE_URL`, `API_KEY`, `TARGET_RPS`, `DURATION`, `WARMUP_DURATION` are passed in via
  `__ENV` from the driving Python test, not hardcoded.
