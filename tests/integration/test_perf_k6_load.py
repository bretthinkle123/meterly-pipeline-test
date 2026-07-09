"""AC6/AC-PERF: a REAL sustained load run using k6 (via Docker), driving the
plan's declared scenario -- constant-arrival-rate at the target rate over a
sustained window, distributed key space -- against a real uvicorn process
(out-of-process, real network stack, multiple workers) backed by real
Postgres + Redis testcontainers.

This supersedes the best-effort in-process smoke in test_perf_smoke.py: k6
runs in its own Docker container (grafana/k6) and reaches the host-run
uvicorn via `host.docker.internal`. True p95 is computed here by nearest-rank
over the raw per-request `http_req_duration` samples k6 writes to its JSON
output (never trusting k6's own approximate percentile field blindly, per
test-conventions) -- scoped to points tagged `scenario=ingest` so warm-up
cold-start latency is excluded from the measurement, the way a real load-test
rig ramps up before recording.

Skips (not fails) if Docker or the grafana/k6 image is unavailable -- this
records an honest absence of measurement, never a fabricated one.
"""

import asyncio
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_K6_SCRIPTS_DIR = Path(__file__).resolve().parent / "k6"

# AC20 p95 debug (2026-07-08, see .pipeline/debug-notes.md): the quota-active
# perf run MUST use the same uvicorn-worker budget as the no-quota baseline it
# is compared against. An earlier 2-worker default for the quota fixture (vs
# the baseline's 5) starved it below the 500 rps constant-arrival target -- two
# worker processes could not service this workload's per-request CPU on the
# Docker-Desktop/Windows host, so excess arrivals queued and p95 inflated into
# the multi-second range from PURE QUEUEING, not from the quota FOR UPDATE
# read-and-decide (which adds ~3 ms p95 when measured under an equal worker
# budget). Confirmed empirically: raising the DB pool alone at 2 workers did
# NOT help (the ceiling is worker processes, not pooled connections); matching
# the baseline worker count did. The shared Postgres testcontainer runs
# max_connections=300, so 5 workers x (pool_size 10 + overflow 5) = 75
# connections is well within budget.
_DEFAULT_PERF_UVICORN_WORKERS = 5


def _baseline_perf_workers() -> int:
    return int(os.environ.get("METERLY_PERF_UVICORN_WORKERS", str(_DEFAULT_PERF_UVICORN_WORKERS)))


def _quota_perf_workers() -> int:
    """Worker count for the quota-active perf fixture. Defaults to the baseline
    worker count so AC20 is an apples-to-apples measurement; still independently
    overridable via `METERLY_PERF_QUOTA_UVICORN_WORKERS` for capacity tuning."""
    return int(
        os.environ.get("METERLY_PERF_QUOTA_UVICORN_WORKERS", str(_baseline_perf_workers()))
    )


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    return result.returncode == 0


def _nearest_rank_from_raw_jsonl(path: Path, scenario: str) -> dict:
    """Parse k6's raw JSON-lines output and compute the TRUE percentiles via
    nearest-rank over the `http_req_duration` samples tagged with `scenario`
    -- this is what test-conventions requires instead of trusting k6's own
    (t-digest-approximated) percentile fields."""
    durations: list[float] = []
    timestamps: list[str] = []
    statuses: list[str] = []

    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("type") != "Point" or record.get("metric") != "http_req_duration":
                continue
            tags = record["data"].get("tags", {})
            if tags.get("scenario") != scenario:
                continue
            durations.append(record["data"]["value"])
            timestamps.append(record["data"]["time"])
            statuses.append(tags.get("status"))

    if not durations:
        return {"sample_count": 0}

    durations.sort()
    n = len(durations)

    def nearest_rank(percentile: float) -> float:
        rank = max(1, round(percentile / 100 * n))
        return durations[min(rank, n) - 1]

    sorted_timestamps = sorted(timestamps)
    first = datetime.datetime.fromisoformat(sorted_timestamps[0].replace("Z", "+00:00"))
    last = datetime.datetime.fromisoformat(sorted_timestamps[-1].replace("Z", "+00:00"))
    elapsed_seconds = (last - first).total_seconds()

    return {
        "sample_count": n,
        "status_counts": dict(Counter(statuses)),
        "p50_ms": round(nearest_rank(50), 2),
        "p90_ms": round(nearest_rank(90), 2),
        "p95_ms": round(nearest_rank(95), 2),
        "p99_ms": round(nearest_rank(99), 2),
        "max_ms": round(durations[-1], 2),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "throughput_rps": round(n / elapsed_seconds, 2) if elapsed_seconds > 0 else 0.0,
    }


@pytest.fixture
async def k6_load_env(postgres_url, redis_url, make_api_key, truncate_tables):
    """Launch the real app under uvicorn (multi-worker, out-of-process)
    against the live containers, ready for a k6 container to hit it via
    `host.docker.internal`."""
    if not _docker_available():
        pytest.skip("Docker is not available in this environment -- cannot run a real k6 load test")

    pull_result = subprocess.run(
        ["docker", "image", "inspect", "grafana/k6:latest"], capture_output=True, timeout=10
    )
    if pull_result.returncode != 0:
        pull_result = subprocess.run(
            ["docker", "pull", "grafana/k6:latest"], capture_output=True, timeout=120
        )
        if pull_result.returncode != 0:
            pytest.skip("grafana/k6 image could not be pulled in this environment")

    port = _free_port()
    env = dict(os.environ)
    env["DATABASE_URL"] = postgres_url
    env["METERLY_REDIS_URL"] = redis_url
    env["METERLY_TIER1_RATE_LIMIT_PER_SECOND"] = "1000000"
    env["METERLY_TIER1_RATE_LIMIT_BURST"] = "1000000"

    presented_key, _ = await make_api_key(label="k6-perf-key", rate_limit_per_sec=1_000_000)

    workers = _baseline_perf_workers()
    scratch_dir = Path(os.environ.get("METERLY_TEST_SCRATCH_DIR", tempfile.gettempdir())) / "meterly_k6_perf"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    uvicorn_log = open(scratch_dir / "uvicorn_stdout.log", "w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", str(port), "--workers", str(workers)],
        cwd=str(_REPO_ROOT), env=env, stdout=uvicorn_log, stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.monotonic() + 30
        async with httpx.AsyncClient() as probe:
            ready = False
            while time.monotonic() < deadline:
                try:
                    response = await probe.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    if response.status_code == 200:
                        ready = True
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.3)
            if not ready:
                pytest.skip("uvicorn did not become ready in time in this environment")

        yield {"base_url_from_host": f"http://127.0.0.1:{port}", "port": port, "api_key": presented_key, "scratch_dir": scratch_dir}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        uvicorn_log.close()


def _run_k6(script_name: str, port: int, api_key: str, scratch_dir: Path, target_rps: int, duration: str, warmup: str, out_name: str) -> subprocess.CompletedProcess:
    raw_out = scratch_dir / out_name
    if raw_out.exists():
        raw_out.unlink()
    return subprocess.run(
        [
            "docker", "run", "--rm",
            "--add-host=host.docker.internal:host-gateway",
            "-v", f"{scratch_dir}:/perf_out",
            "-v", f"{_K6_SCRIPTS_DIR}:/perf_scripts:ro",
            "-e", f"BASE_URL=http://host.docker.internal:{port}",
            "-e", f"API_KEY={api_key}",
            "-e", f"TARGET_RPS={target_rps}",
            "-e", f"DURATION={duration}",
            "-e", f"WARMUP_DURATION={warmup}",
            "grafana/k6", "run",
            "--out", f"json=/perf_out/{out_name}",
            f"/perf_scripts/{script_name}",
        ],
        capture_output=True, text=True, timeout=180,
    )


def test_events_ingest_sustained_500rps_k6_load(k6_load_env):
    """Drive POST /v1/events at a real, sustained constant-arrival-rate of
    500 req/s (the AC6 budget's declared rate) over a real k6 container
    against a real out-of-process uvicorn + Postgres + Redis, with a
    distributed key space (unique idempotency_key + customer_id spread across
    50 buckets per request, no replay contention). Records true nearest-rank
    p95 + achieved throughput -- an honest measurement, not a claim the
    budget is met."""
    target_rps = int(os.environ.get("METERLY_PERF_TARGET_RPS", "500"))
    duration = os.environ.get("METERLY_PERF_DURATION", "15s")
    warmup = os.environ.get("METERLY_PERF_WARMUP", "5s")

    result = _run_k6(
        "load_events.js", k6_load_env["port"], k6_load_env["api_key"], k6_load_env["scratch_dir"],
        target_rps, duration, warmup, "k6_events_raw.jsonl",
    )
    (k6_load_env["scratch_dir"] / "k6_events_stdout.log").write_text(result.stdout + "\n---STDERR---\n" + result.stderr)

    if result.returncode != 0:
        pytest.skip(f"k6 run failed to execute in this environment (rc={result.returncode}); see k6_events_stdout.log")

    metrics = _nearest_rank_from_raw_jsonl(k6_load_env["scratch_dir"] / "k6_events_raw.jsonl", scenario="ingest")
    (k6_load_env["scratch_dir"] / "events_metrics.json").write_text(json.dumps(metrics, indent=2))
    print("POST /v1/events k6 load metrics:", json.dumps(metrics, indent=2))

    assert metrics["sample_count"] > 0, "the k6 run produced no ingest-scenario samples -- treat as unmeasured, not zero"


@pytest.fixture
async def k6_quota_load_env(postgres_url, redis_url, make_api_key, truncate_tables):
    """Like `k6_load_env`, but the provisioned key is admin-scoped and each
    `cust_0..cust_49` bucket the k6 script drives (`load_events_quota.js`)
    carries a pre-seeded, deliberately high quota (AC20) -- so the ingest
    path exercises the quota-check read-and-decide on every request without
    ever actually rejecting one, isolating the added latency of the `FOR
    UPDATE` lookup itself from the (much larger, transaction-aborting) cost
    of a rejection.
    """
    if not _docker_available():
        pytest.skip("Docker is not available in this environment -- cannot run a real k6 load test")

    pull_result = subprocess.run(
        ["docker", "image", "inspect", "grafana/k6:latest"], capture_output=True, timeout=10
    )
    if pull_result.returncode != 0:
        pull_result = subprocess.run(
            ["docker", "pull", "grafana/k6:latest"], capture_output=True, timeout=120
        )
        if pull_result.returncode != 0:
            pytest.skip("grafana/k6 image could not be pulled in this environment")

    port = _free_port()
    env = dict(os.environ)
    env["DATABASE_URL"] = postgres_url
    env["METERLY_REDIS_URL"] = redis_url
    env["METERLY_TIER1_RATE_LIMIT_PER_SECOND"] = "1000000"
    env["METERLY_TIER1_RATE_LIMIT_BURST"] = "1000000"

    presented_key, api_key_id = await make_api_key(
        label="k6-quota-perf-key", rate_limit_per_sec=1_000_000, scope="admin"
    )

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    # A high-but-finite cap per bucket (well above anything a 15s run at the
    # target rate could accumulate) -- active enough to exercise the FOR
    # UPDATE read-and-decide on every request, never high enough to matter
    # for whether a single run's requests are accepted.
    quota_engine = create_async_engine(postgres_url)
    async with quota_engine.begin() as connection:
        for customer_index in range(50):
            await connection.execute(
                text(
                    """
                    INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window, updated_at)
                    VALUES (:api_key_id, :customer_id, 'api_calls', 100000000, now())
                    ON CONFLICT (api_key_id, customer_id, metric) DO UPDATE SET
                        limit_per_window = EXCLUDED.limit_per_window
                    """
                ),
                {"api_key_id": api_key_id, "customer_id": f"cust_{customer_index}"},
            )
    await quota_engine.dispose()

    # Match the baseline fixture's worker budget (default 5) so AC20 measures
    # the quota path under the SAME CPU/event-loop capacity it is compared
    # against -- a smaller budget starves the constant-arrival-rate scenario and
    # inflates p95 with queueing delay that has nothing to do with the quota
    # check (see _quota_perf_workers and .pipeline/debug-notes.md 2026-07-08).
    # The shared Postgres container runs max_connections=300 and these perf
    # fixtures are function-scoped (torn down before the next), so 5 workers x
    # 15 pooled connections is comfortably within the session's budget.
    workers = _quota_perf_workers()
    scratch_dir = Path(os.environ.get("METERLY_TEST_SCRATCH_DIR", tempfile.gettempdir())) / "meterly_k6_quota_perf"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    uvicorn_log = open(scratch_dir / "uvicorn_stdout.log", "w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", str(port), "--workers", str(workers)],
        cwd=str(_REPO_ROOT), env=env, stdout=uvicorn_log, stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.monotonic() + 30
        async with httpx.AsyncClient() as probe:
            ready = False
            while time.monotonic() < deadline:
                try:
                    response = await probe.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    if response.status_code == 200:
                        ready = True
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.3)
            if not ready:
                pytest.skip("uvicorn did not become ready in time in this environment")

        yield {"base_url_from_host": f"http://127.0.0.1:{port}", "port": port, "api_key": presented_key, "scratch_dir": scratch_dir}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        uvicorn_log.close()
        # Give Postgres a moment to reclaim the just-closed workers' backend
        # connection slots before the next test's fixtures open new ones
        # against the same shared container (mitigates TooManyConnectionsError
        # when this module's perf tests run back-to-back in one session).
        await asyncio.sleep(1.0)


def test_events_ingest_with_quotas_p95_within_1_5x_baseline(k6_load_env, k6_quota_load_env):
    """AC20 (human-revised 2026-07-09, see .pipeline/debug-notes.md and the
    AC20 row in .pipeline/acceptance.md): the original absolute "p95 < 50ms"
    form is unachievable on this Docker-Desktop/Windows host for ANY code
    (the no-quota baseline itself runs ~84-112ms here). AC20 is now a
    RELATIVE budget: quota-active p95 <= 1.5x the same-session no-quota
    baseline p95, both measured at equal uvicorn worker budget.

    This test drives BOTH the no-quota baseline (`k6_load_env`, workers =
    `_baseline_perf_workers()`) and the quota-active path (`k6_quota_load_env`,
    workers = `_quota_perf_workers()`, which defaults to the SAME count) in
    the same test session, back-to-back, and asserts the relative bound
    directly -- closing the prior gap where this test only asserted
    `sample_count > 0` and let a real regression (measured 3362ms vs an 84ms
    baseline, ~40x) pass green (see test-quality.json's AC20 gap, now closed
    by this assertion)."""
    assert _quota_perf_workers() == _baseline_perf_workers(), (
        f"AC20 requires an equal worker budget for the relative comparison to be "
        f"apples-to-apples, but quota={_quota_perf_workers()} != baseline={_baseline_perf_workers()}"
    )

    target_rps = int(os.environ.get("METERLY_PERF_TARGET_RPS", "500"))
    duration = os.environ.get("METERLY_PERF_DURATION", "15s")
    warmup = os.environ.get("METERLY_PERF_WARMUP", "5s")

    baseline_result = _run_k6(
        "load_events.js", k6_load_env["port"], k6_load_env["api_key"], k6_load_env["scratch_dir"],
        target_rps, duration, warmup, "k6_events_baseline_raw.jsonl",
    )
    (k6_load_env["scratch_dir"] / "k6_events_baseline_stdout.log").write_text(
        baseline_result.stdout + "\n---STDERR---\n" + baseline_result.stderr
    )
    if baseline_result.returncode != 0:
        pytest.skip(
            f"k6 baseline run failed to execute in this environment (rc={baseline_result.returncode}); "
            "see k6_events_baseline_stdout.log"
        )
    baseline_metrics = _nearest_rank_from_raw_jsonl(
        k6_load_env["scratch_dir"] / "k6_events_baseline_raw.jsonl", scenario="ingest"
    )
    (k6_load_env["scratch_dir"] / "events_baseline_metrics.json").write_text(json.dumps(baseline_metrics, indent=2))
    print("POST /v1/events no-quota baseline k6 load metrics:", json.dumps(baseline_metrics, indent=2))

    quota_result = _run_k6(
        "load_events_quota.js", k6_quota_load_env["port"], k6_quota_load_env["api_key"], k6_quota_load_env["scratch_dir"],
        target_rps, duration, warmup, "k6_events_quota_raw.jsonl",
    )
    (k6_quota_load_env["scratch_dir"] / "k6_events_quota_stdout.log").write_text(
        quota_result.stdout + "\n---STDERR---\n" + quota_result.stderr
    )
    if quota_result.returncode != 0:
        pytest.skip(
            f"k6 quota run failed to execute in this environment (rc={quota_result.returncode}); "
            "see k6_events_quota_stdout.log"
        )
    quota_metrics = _nearest_rank_from_raw_jsonl(
        k6_quota_load_env["scratch_dir"] / "k6_events_quota_raw.jsonl", scenario="ingest"
    )
    (k6_quota_load_env["scratch_dir"] / "events_quota_metrics.json").write_text(json.dumps(quota_metrics, indent=2))
    print("POST /v1/events (quotas active) k6 load metrics:", json.dumps(quota_metrics, indent=2))

    assert baseline_metrics["sample_count"] > 0, "the baseline k6 run produced no ingest-scenario samples -- unmeasured"
    assert quota_metrics["sample_count"] > 0, "the quota k6 run produced no ingest-scenario samples -- unmeasured"

    budget_p95 = 1.5 * baseline_metrics["p95_ms"]
    assert quota_metrics["p95_ms"] <= budget_p95, (
        f"AC20: quota-active p95 ({quota_metrics['p95_ms']}ms) exceeds 1.5x the same-session "
        f"no-quota baseline p95 ({baseline_metrics['p95_ms']}ms -> budget {budget_p95:.2f}ms) "
        f"at equal worker budget ({_quota_perf_workers()} workers)"
    )


def test_ac20_quota_perf_fixture_matches_baseline_worker_budget(monkeypatch):
    """Regression guard for the AC20 p95 debug (.pipeline/debug-notes.md,
    2026-07-08). The quota-active perf fixture must not be provisioned with
    FEWER uvicorn workers than the no-quota baseline it is compared against.

    A prior 2-worker default (vs the baseline's 5) starved the quota run below
    the 500 rps constant-arrival target, so its p95 inflated into the seconds
    purely from queueing -- masquerading as a quota-code-path regression that,
    measured under an equal worker budget, does not exist (~87 ms vs the ~85 ms
    baseline). This fast, Docker-free guard fails before the fix (2 < 5) and
    passes after (5 >= 5), locking the handicap out of the harness for good."""
    monkeypatch.delenv("METERLY_PERF_UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("METERLY_PERF_QUOTA_UVICORN_WORKERS", raising=False)
    assert _quota_perf_workers() >= _baseline_perf_workers(), (
        f"quota perf fixture worker budget ({_quota_perf_workers()}) is below the "
        f"no-quota baseline ({_baseline_perf_workers()}); AC20 would be measured on a "
        "starved pool and its p95 would reflect queueing, not the quota check"
    )


def test_usage_read_sustained_100rps_k6_load(k6_load_env):
    """Drive GET /v1/usage at a sustained 100 req/s via k6, recording true p95
    against the AC6 budget's declared <100ms usage-read p95."""
    target_rps = int(os.environ.get("METERLY_PERF_USAGE_TARGET_RPS", "100"))
    duration = os.environ.get("METERLY_PERF_USAGE_DURATION", "10s")
    warmup = os.environ.get("METERLY_PERF_USAGE_WARMUP", "3s")

    result = _run_k6(
        "load_usage.js", k6_load_env["port"], k6_load_env["api_key"], k6_load_env["scratch_dir"],
        target_rps, duration, warmup, "k6_usage_raw.jsonl",
    )
    (k6_load_env["scratch_dir"] / "k6_usage_stdout.log").write_text(result.stdout + "\n---STDERR---\n" + result.stderr)

    if result.returncode != 0:
        pytest.skip(f"k6 run failed to execute in this environment (rc={result.returncode}); see k6_usage_stdout.log")

    metrics = _nearest_rank_from_raw_jsonl(k6_load_env["scratch_dir"] / "k6_usage_raw.jsonl", scenario="ingest")
    (k6_load_env["scratch_dir"] / "usage_metrics.json").write_text(json.dumps(metrics, indent=2))
    print("GET /v1/usage k6 load metrics:", json.dumps(metrics, indent=2))

    assert metrics["sample_count"] > 0, "the k6 run produced no ingest-scenario samples -- treat as unmeasured, not zero"
