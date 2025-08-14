# app_unit_test.py
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app import app, logger
from security import get_current_user  # Import for dependency override
from tfsa_assistant_graph import cache


@pytest.fixture(scope="module")
def test_client():
    """
    A pytest fixture to set up the test client.
    This runs once per module, overriding the authentication dependency for all tests.
    """

    def mock_get_current_user():
        return {"user_id": "pytest_user", "permissions": ["chat.access"]}

    # Apply the dependency override for the duration of the tests
    app.dependency_overrides[get_current_user] = mock_get_current_user
    client = TestClient(app)
    yield client  # The test client is now available to test functions
    app.dependency_overrides.clear()  # Clean up the override after tests are done


def test_non_streaming_request(test_client):
    """Test a basic non-streaming chat completion request."""
    test_request = {
        "messages": [{"role": "user", "content": "What are the annual dollar limits for each year of TSFA?"}],
        "stream": False
    }
    headers = {"X-IBM-THREAD-ID": "test_thread_123"}

    logger.info("Testing non-streaming request...")
    response = test_client.post("/api/v1/chat/completions", json=test_request, headers=headers)

    assert response.status_code == 200
    data = response.json()
    logger.info(f"Non-streaming test successful! Response: {data}")
    assert data["object"] == "chat.completion"
    assert len(data["choices"]) > 0
    assert "content" in data["choices"][0]["message"]


def test_streaming_request(test_client):
    """Test a streaming chat completion request."""
    test_request = {
        "messages": [{"role": "user", "content": "What is my contribution room?"}],
        "stream": True
    }
    headers = {"X-IBM-THREAD-ID": "test_thread_456"}

    logger.info("Testing streaming request...")
    reconstructed_message = ""
    full_stream_text = ""
    with test_client.stream("POST", "/api/v1/chat/completions", json=test_request, headers=headers) as response:
        assert response.status_code == 200
        logger.info("Receiving stream...")
        for line in response.iter_lines():
            if line:
                logger.info(f"Stream line: {line.encode()}")
                full_stream_text += line
                if line.startswith("data:"):
                    data_str = line[len("data: "):].strip()
                    if data_str == "[DONE]":
                        break
                    if not data_str:
                        continue
                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        if "content" in delta:
                            reconstructed_message += delta["content"]
                    except (json.JSONDecodeError, IndexError):
                        pass  # Ignore non-json lines like heartbeats

        assert "data: [DONE]" in full_stream_text
        assert "Based on your profile" in reconstructed_message
        assert "available TFSA contribution room" in reconstructed_message
        logger.info("Streaming test successful!")


def test_cached_response_and_streaming_simulation(test_client):
    """Test that a cached response is served correctly and simulates streaming."""
    user_input = "What are the annual dollar limits for each year of TSFA?"
    test_request = {"messages": [{"role": "user", "content": user_input}]}
    headers = {"X-IBM-THREAD-ID": "test_thread_cache_123"}

    # First, clear any existing cache for this item to ensure a clean test
    cache_hash = hashlib.sha256(user_input.encode('UTF-8')).hexdigest()
    cache.delete(cache_hash)

    # Run a non-streaming request to populate the cache
    logger.info("Testing cached response: First request (populating cache)...")
    response1 = test_client.post("/api/v1/chat/completions", json={**test_request, "stream": False}, headers=headers)
    assert response1.status_code == 200
    assert cache.contains(cache_hash)

    # Now, make a streaming request for the same content, which should hit the cache
    logger.info("Testing cached response: Second request (streaming from cache)...")
    streamed_content = ""
    with test_client.stream("POST", "/api/v1/chat/completions", json={**test_request, "stream": True},
                            headers=headers) as response2:
        assert response2.status_code == 200
        # Verify that we receive a simulated stream by parsing the SSE events
        for line in response2.iter_lines():
            if line.startswith("data:"):
                data_str = line[len("data: "):].strip()
                if data_str == "[DONE]":
                    break
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    if "content" in delta:
                        streamed_content += delta["content"]
                except (json.JSONDecodeError, IndexError):
                    pass  # Ignore heartbeats or other non-JSON lines

        assert "TFSA Annual Contribution Limits" in streamed_content
    logger.info("Cached streaming simulation successful!")
