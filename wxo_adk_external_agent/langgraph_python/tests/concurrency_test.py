"""Concurrency tests for the /chat/completions endpoint.

These verify the Phase-1 fix (1A): the async handler offloads the blocking
``run_tfsa_assistant_sync`` work to a worker thread, so concurrent requests run in
parallel instead of serializing on the event loop (the bug that made the agent
"reply nothing" under multiple concurrent threads).

The real LLM/graph tool is replaced with a fake that just sleeps, so the tests are
fast, deterministic, and need no Bedrock/watsonx credentials or network.

Run standalone (recommended, so the provider env below applies before import):
    AI_SERVICES_PROVIDER=ollama ./.venv/bin/python -m pytest tests/concurrency_test.py -v
"""
import os

# Must be set BEFORE importing app -> tfsa_assistant_graph (which builds the LLM client at
# import). 'ollama' constructs without network/credentials; the LLM is never actually called
# because the tool is stubbed below. Empty bucket forces local data (no S3 calls).
os.environ.setdefault("AI_SERVICES_PROVIDER", "ollama")
os.environ.setdefault("DATA_S3_BUCKET", "")

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import app as app_module
from app import app
from security import get_current_user

# Each fake request "takes" this long inside the tool. Total serialized time would be
# SLEEP * N; parallel time is ~SLEEP. We assert we are clearly in the parallel regime.
SLEEP = 0.3
N = 10


def _fake_sync_tool(user_input, thread_id, model):
    """Stand-in for run_tfsa_assistant_sync: blocks (like a real LLM call), then echoes."""
    time.sleep(SLEEP)
    return f"answer to: {user_input}", {}


def _body(i):
    # No X-IBM-THREAD-ID header -> no thread cache, so requests are fully independent
    # and the timing reflects the handler's concurrency, not cache contention.
    return {"messages": [{"role": "user", "content": f"req-{i}"}], "stream": False}


@pytest.fixture
def concurrent_app(monkeypatch):
    """Patch the blocking tool + auth + access logging for fast, isolated concurrency tests."""
    monkeypatch.setattr(app_module, "run_tfsa_assistant_sync", _fake_sync_tool)
    # Avoid per-request disk writes from access logging during the test.
    monkeypatch.setattr(app_module, "log_access", lambda *a, **k: None)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "pytest_user"}
    yield app
    app.dependency_overrides.clear()


def test_concurrent_requests_run_in_parallel(concurrent_app):
    """N simultaneous requests should finish in ~SLEEP, not ~SLEEP*N.

    This is the regression guard for 1A: if the handler ever calls the blocking tool
    directly on the event loop again, these requests serialize and the elapsed time
    jumps to ~SLEEP*N, failing the assertion.
    """

    async def _run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            start = time.perf_counter()
            responses = await asyncio.gather(
                *[ac.post("/api/v1/chat/completions", json=_body(i)) for i in range(N)]
            )
            elapsed = time.perf_counter() - start
            return responses, elapsed

    responses, elapsed = asyncio.run(_run())

    # All requests succeeded with a non-empty, correctly-routed answer.
    for i, resp in enumerate(responses):
        assert resp.status_code == 200, resp.text
        content = resp.json()["choices"][0]["message"]["content"]
        assert content and content.strip(), "empty response under concurrency"
        assert f"req-{i}" in content, "responses got crossed between concurrent requests"

    serialized = SLEEP * N
    assert elapsed < serialized * 0.6, (
        f"requests appear serialized: {elapsed:.2f}s for {N} requests "
        f"(serialized would be ~{serialized:.2f}s, parallel ~{SLEEP:.2f}s)"
    )


def test_many_threads_all_get_responses(concurrent_app):
    """Fire requests from many OS threads; every one must get its own non-empty answer."""
    client = TestClient(app)
    n_threads = 20

    def _fire(i):
        resp = client.post("/api/v1/chat/completions", json=_body(i))
        return i, resp

    results = {}
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(_fire, i) for i in range(n_threads)]
        for fut in as_completed(futures):
            i, resp = fut.result()
            results[i] = resp

    assert len(results) == n_threads
    for i, resp in results.items():
        assert resp.status_code == 200, resp.text
        content = resp.json()["choices"][0]["message"]["content"]
        assert content and content.strip(), f"thread {i} got an empty response"
        assert f"req-{i}" in content, f"thread {i} received another request's response"
