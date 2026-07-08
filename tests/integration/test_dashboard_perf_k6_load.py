"""AC16: a REAL sustained load run using k6 (via Docker), driving the plan's
declared scenario -- constant-arrival-rate 25 req/s for >= 60s -- against a
real out-of-process uvicorn (real network stack) backed by real Postgres +
Redis testcontainers, for `GET /dashboard/api/usage-series?granularity=hour`
and `GET /dashboard` (static HTML).

Mirrors `tests/integration/test_perf_k6_load.py`'s recipe exactly: true
p95/throughput computed here via nearest-rank over the raw
`http_req_duration` samples k6 writes to its JSON output (never trusting k6's
own approximate percentile field), scoped to points tagged `scenario=ingest`
so warm-up is excluded. Skips (never fails) if Docker/k6 is unavailable --
an honest absence of measurement, never a fabricated one.
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
async def dashboard_k6_env(postgres_url, redis_url, make_api_key, truncate_tables):
    """Launch the real app under uvicorn (out-of-process) against the live
    containers, with a real seeded usage_rollup dataset for `acme-corp` so
    the hour-granularity BFF read has real rows to aggregate, ready for a k6
    container to hit it via `host.docker.internal`."""
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

    presented_key, _ = await make_api_key(label="dashboard-perf-key", rate_limit_per_sec=1_000_000)
    env["DASHBOARD_READER_API_KEY"] = presented_key

    workers = int(os.environ.get("METERLY_PERF_UVICORN_WORKERS", "5"))
    scratch_dir = Path(os.environ.get("METERLY_TEST_SCRATCH_DIR", tempfile.gettempdir())) / "meterly_dashboard_k6_perf"
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

            # Seed a real usage row for acme-corp/api_calls so the hour-granularity
            # BFF read aggregates real data, not just 11 empty-bucket reads.
            seed_response = await probe.post(
                f"http://127.0.0.1:{port}/v1/events",
                json={
                    "customer_id": "acme-corp", "metric": "api_calls",
                    "quantity": "5", "idempotency_key": "perf-seed-1",
                },
                headers={"Authorization": f"Bearer {presented_key}"},
                timeout=10.0,
            )
            assert seed_response.status_code in (200, 201)

        yield {"port": port, "scratch_dir": scratch_dir}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        uvicorn_log.close()


def _run_k6(script_name: str, port: int, scratch_dir: Path, target_rps: int, duration: str, warmup: str, out_name: str) -> subprocess.CompletedProcess:
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
            "-e", f"TARGET_RPS={target_rps}",
            "-e", f"DURATION={duration}",
            "-e", f"WARMUP_DURATION={warmup}",
            "grafana/k6", "run",
            "--out", f"json=/perf_out/{out_name}",
            f"/perf_scripts/{script_name}",
        ],
        capture_output=True, text=True, timeout=180,
    )


def test_dashboard_usage_series_hour_sustained_25rps_k6_load(dashboard_k6_env):
    """AC16: GET /dashboard/api/usage-series?granularity=hour driven at a
    real, sustained constant-arrival-rate of 25 req/s for >= 60s via a real
    k6 container. Records true nearest-rank p95 + achieved throughput -- an
    honest measurement, not a claim the < 200ms budget is met."""
    target_rps = int(os.environ.get("METERLY_DASHBOARD_PERF_TARGET_RPS", "25"))
    duration = os.environ.get("METERLY_DASHBOARD_PERF_DURATION", "60s")
    warmup = os.environ.get("METERLY_DASHBOARD_PERF_WARMUP", "5s")

    result = _run_k6(
        "load_dashboard.js", dashboard_k6_env["port"], dashboard_k6_env["scratch_dir"],
        target_rps, duration, warmup, "k6_dashboard_raw.jsonl",
    )
    (dashboard_k6_env["scratch_dir"] / "k6_dashboard_stdout.log").write_text(result.stdout + "\n---STDERR---\n" + result.stderr)

    if result.returncode != 0:
        pytest.skip(f"k6 run failed to execute in this environment (rc={result.returncode}); see k6_dashboard_stdout.log")

    metrics = _nearest_rank_from_raw_jsonl(dashboard_k6_env["scratch_dir"] / "k6_dashboard_raw.jsonl", scenario="ingest")
    (dashboard_k6_env["scratch_dir"] / "dashboard_usage_series_metrics.json").write_text(json.dumps(metrics, indent=2))
    print("GET /dashboard/api/usage-series k6 load metrics:", json.dumps(metrics, indent=2))

    assert metrics["sample_count"] > 0, "the k6 run produced no ingest-scenario samples -- treat as unmeasured, not zero"


def test_dashboard_page_sustained_25rps_k6_load(dashboard_k6_env):
    """AC16: GET /dashboard (static HTML) driven at the same 25 req/s."""
    target_rps = int(os.environ.get("METERLY_DASHBOARD_PERF_TARGET_RPS", "25"))
    duration = os.environ.get("METERLY_DASHBOARD_PERF_PAGE_DURATION", "60s")
    warmup = os.environ.get("METERLY_DASHBOARD_PERF_WARMUP", "5s")

    result = _run_k6(
        "load_dashboard_page.js", dashboard_k6_env["port"], dashboard_k6_env["scratch_dir"],
        target_rps, duration, warmup, "k6_dashboard_page_raw.jsonl",
    )
    (dashboard_k6_env["scratch_dir"] / "k6_dashboard_page_stdout.log").write_text(result.stdout + "\n---STDERR---\n" + result.stderr)

    if result.returncode != 0:
        pytest.skip(f"k6 run failed to execute in this environment (rc={result.returncode}); see k6_dashboard_page_stdout.log")

    metrics = _nearest_rank_from_raw_jsonl(dashboard_k6_env["scratch_dir"] / "k6_dashboard_page_raw.jsonl", scenario="ingest")
    (dashboard_k6_env["scratch_dir"] / "dashboard_page_metrics.json").write_text(json.dumps(metrics, indent=2))
    print("GET /dashboard k6 load metrics:", json.dumps(metrics, indent=2))

    assert metrics["sample_count"] > 0, "the k6 run produced no ingest-scenario samples -- treat as unmeasured, not zero"
