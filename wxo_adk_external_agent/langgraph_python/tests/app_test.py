# app_test.py
from fastapi.testclient import TestClient

from app import app, logger
from security import get_current_user  # Import for dependency override

if __name__ == '__main__':

    # Override authentication dependency for testing
    def mock_get_current_user():
        return {"user_id": "test_user", "permissions": ["chat.access"]}


    # Apply dependency override BEFORE creating TestClient
    app.dependency_overrides[get_current_user] = mock_get_current_user

    client = TestClient(app)
    logger.info("Starting tests...")

    # Test 1: Basic non-streaming request
    test_request = {
        "messages": [{"role": "user", "content": "What are the annual dollar limits for each year of TSFA?"}],
        "stream": False
    }
    headers = {"X-IBM-THREAD-ID": "test_thread_123"}

    logger.info("Sending test request 1...")
    response = client.post("/api/v1/chat/completions", json=test_request, headers=headers)
    logger.info(f"Response status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        logger.info(f"Test 1 successful! Response: {data}")
        assert data["object"] == "chat.completion"
        assert len(data["choices"]) > 0
    else:
        logger.error(f"Test 1 failed: {response.text}")

    # Test 2: Streaming request
    test_request["stream"] = True
    logger.info("Sending test request 2 (streaming)...")

    with client.stream("POST", "/api/v1/chat/completions", json=test_request, headers=headers) as response:
        logger.info(f"Streaming response status: {response.status_code}")
        if response.status_code == 200:
            logger.info("Receiving stream...")
            for chunk in response.iter_lines():
                if chunk:
                    encode = chunk.encode()
                    logger.info(f"Stream chunk: {encode}")
        else:
            logger.error(f"Test 2 failed: {response.text}")

    # Test 3: Request with extra_body thread_id
    test_request["extra_body"] = {"thread_id": "extra_body_thread_456"}
    test_request["stream"] = False
    logger.info("Sending test request 3 (with extra_body)...")
    response = client.post("/api/v1/chat/completions", json=test_request, headers=headers)
    if response.status_code == 200:
        logger.info("Test 3 successful!")
    else:
        logger.error(f"Test 3 failed: {response.text}")

    logger.info("All tests completed!")
