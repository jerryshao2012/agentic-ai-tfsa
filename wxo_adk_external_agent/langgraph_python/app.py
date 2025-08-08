# app.py
import glob
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse

from llm_utils import get_llm_sync, get_llm_stream
from models import ChatCompletionRequest, ChatCompletionResponse, Choice, MessageResponse, DEFAULT_MODEL
from security import get_current_user
from tfsa_assistant import chat_tfsa_assistant, cache, extract_user_id

logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

app = FastAPI()


@app.post("/chat/completions")
async def chat_completions(
        request: ChatCompletionRequest,
        x_ibm_thread_id: Optional[str] = Header(None, alias="X-IBM-THREAD-ID",
                                                description="Optional header to specify the thread ID"),
        _current_user: Dict[str, Any] = Depends(get_current_user),
):
    start_time = time.time()
    logger.info(f"Received POST /chat/completions ChatCompletionRequest: {request.model_dump_json()}")
    thread_id = ""
    if x_ibm_thread_id:
        thread_id = x_ibm_thread_id
    if request.extra_body and request.extra_body.thread_id:
        thread_id = request.extra_body.thread_id
    logger.info("thread_id: " + thread_id)

    model = DEFAULT_MODEL
    if request.model:
        model = request.model
    selected_tools = [chat_tfsa_assistant]

    # Extract user input from messages
    user_input = ""
    for msg in reversed(request.messages):
        if msg.role in ["user", "human"]:
            user_input = msg.content
            break

    user_id = _current_user.get("user_id", "unknown")
    # Extract user ID from input. This is for demo purposes only
    if _user_id := extract_user_id(user_input):
        user_id = _user_id
    is_stream = request.stream

    # Create thread state cache key
    thread_cache_key = f"thread_state_{thread_id}" if thread_id else None

    # Retrieve thread state if exists
    thread_state = None
    if thread_cache_key and cache.contains(thread_cache_key):
        thread_state = cache.load_from_cache(thread_cache_key).get("value")
        # Get user ID from thread state
        if "user_id" in thread_state:
            user_id = thread_state["user_id"]

    # Log access start
    log_access(user_id, thread_id, is_stream, user_input, "[Generating...]", model)

    # Handle single tool case directly
    if len(selected_tools) == 1:
        logger.info("Directly invoking single tool")

        # Call the tool directly
        tool_response, _ = selected_tools[0](user_input, thread_id)

        # Update thread state if exists
        if thread_cache_key and thread_state:
            thread_state["user_input"] = user_input
            thread_state["messages"].append({
                "role": "user",
                "content": user_input
            })
            thread_state["messages"].append({
                "role": "assistant",
                "content": tool_response
            })
            cache.cache(thread_cache_key, thread_state)

        # For non-streaming requests
        if not is_stream:
            # Create response
            id = str(uuid.uuid4())
            response = ChatCompletionResponse(
                id=id,
                object="chat.completion",
                created=int(time.time()),
                model=request.model,
                choices=[
                    Choice(
                        index=0,
                        message=MessageResponse(
                            role="assistant",
                            content=tool_response
                        ),
                        finish_reason="stop"
                    )
                ]
            )

            # Log access
            log_access(user_id, thread_id, is_stream, user_input,
                       response=f"[{(time.time() - start_time):.3f} seconds]\n{tool_response}",
                       model=model)
            return JSONResponse(content=response.model_dump())

        # For streaming requests
        else:
            # Create a generator that simulates streaming response
            def generate_stream():
                # Break the response into chunks
                chunk_size = 20  # Number of characters per chunk
                response_length = len(tool_response)

                # Generate a unique ID for this response
                response_id = str(uuid.uuid4())

                # Send chunks
                for i in range(0, response_length, chunk_size):
                    chunk = tool_response[i:i + chunk_size]

                    # Create SSE structure
                    current_timestamp = int(time.time())
                    struct = {
                        "id": response_id,
                        "object": "thread.message.delta",
                        "created": current_timestamp,
                        "thread_id": thread_id,
                        "model": model,
                        "choices": [
                            {
                                "delta": {
                                    "content": chunk,
                                    "role": "assistant",
                                }
                            }
                        ]
                    }

                    # Format as SSE
                    yield f"data: {json.dumps(struct)}\n\n"

                    # Add small delay to simulate streaming
                    # time.sleep(0.05)

                # Send final stop message
                stop_struct = {
                    "id": response_id,
                    "object": "thread.message.delta",
                    "created": int(time.time()),
                    "thread_id": thread_id,
                    "model": model,
                    "choices": [
                        {
                            "delta": {},
                            "finish_reason": "stop"
                        }
                    ]
                }
                yield f"data: {json.dumps(stop_struct)}\n\n"
                yield "data: [DONE]\n\n"

            # Log access
            log_access(user_id, thread_id, is_stream, user_input,
                       response=f"[{(time.time() - start_time):.3f} seconds]\n{tool_response}",
                       model=model)

            # Return streaming response
            return StreamingResponse(generate_stream(), media_type="text/event-stream")

    # Standard processing flow
    if is_stream:
        # Create a wrapper to capture the streamed content and log after completion
        accumulated_response = ""

        async def logging_wrapper(stream_gen):
            nonlocal accumulated_response
            try:
                async for chunk in stream_gen:
                    # Accumulate content chunks
                    if chunk.startswith("data: "):
                        try:
                            data = json.loads(chunk[len("data: "):].strip())
                            if data.get("object") == "thread.message.delta":
                                for choice in data.get("choices", []):
                                    if "content" in choice.get("delta", {}):
                                        accumulated_response += choice["delta"]["content"]
                        except json.JSONDecodeError:
                            pass
                    yield chunk
            finally:
                # Log access after the stream completes
                logger.info(f"Logging access for streamed response (len={len(accumulated_response)})")
                log_access(user_id, thread_id, is_stream, user_input,
                           response=f"[{(time.time() - start_time):.3f} seconds]\n{accumulated_response}",
                           model=model)

        # Get the LLM stream generator
        stream_generator = get_llm_stream(request.messages, model, thread_id, selected_tools)
        wrapped_generator = logging_wrapper(stream_generator)

        # Return streaming response
        return StreamingResponse(wrapped_generator, media_type="text/event-stream")
    else:
        last_message, all_messages = get_llm_sync(request.messages, model, thread_id, selected_tools)
        id = str(uuid.uuid4())
        response = ChatCompletionResponse(
            id=id,
            object="chat.completion",
            created=int(time.time()),
            model=request.model,
            choices=[
                Choice(
                    index=0,
                    message=MessageResponse(
                        role="assistant",
                        content=last_message
                    ),
                    finish_reason="stop"
                )
            ]
        )

        # Log access
        log_access(user_id, thread_id, is_stream, user_input,
                   response=f"[{(time.time() - start_time):.3f} seconds]\n{last_message}",
                   model=model)
        return JSONResponse(content=response.model_dump())


# Access log
# Create log directory if it doesn't exist
LOG_DIR = "log"
os.makedirs(LOG_DIR, exist_ok=True)


def log_access(user_id: str, thread_id: str, is_stream: bool, user_input: str, response: str, model: str):
    """Log user access with input and response"""
    log_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stream": is_stream,
        "user_id": user_id,
        "thread_id": thread_id,
        "model": model,
        "user_input": user_input,
        "response": response
    }

    # Create daily log file
    today = datetime.today().strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"access_{today}.log")

    with open(log_file, "a") as f:
        f.write(json.dumps(log_entry) + "\n")


# View access logs
@app.get("/logs", response_class=HTMLResponse)
async def view_access_logs():
    """Endpoint to view access logs"""
    # Get all log files
    log_files = glob.glob(os.path.join(LOG_DIR, "access_*.log"))
    log_entries = []

    # Read all log entries
    for log_file in log_files:
        try:
            with open(log_file, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        log_entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            continue

    # Sort by timestamp descending
    log_entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # Generate HTML table
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Access Logs</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background-color: #f2f2f2; }
            tr:nth-child(even) { background-color: #f9f9f9; }
            .log-container { 
                overflow-y: auto; 
                margin-bottom: 20px;
            }
            .timestamp { white-space: nowrap; }
        </style>
    </head>
    <body>
        <h1>Access Logs</h1>
        <div class="log-container">
            <table>
                <thead>
                    <tr>
                        <th>Timestamp</th>
                        <th>User ID</th>
                        <th>Thread ID</th>
                        <th>Stream</th>
                        <th>Model</th>
                        <th>User Input</th>
                        <th>Response</th>
                    </tr>
                </thead>
                <tbody>
    """

    for entry in log_entries:
        html_content += f"""
        <tr>
            <td class="timestamp">{entry.get('timestamp', '')}</td>
            <td>{entry.get('user_id', '')}</td>
            <td>{entry.get('thread_id', '')}</td>
            <td>{entry.get('stream', '')}</td>
            <td>{entry.get('model', '')}</td>
            <td>{entry.get('user_input', '')}</td>
            <td>{entry.get('response', '')}</td>
        </tr>
        """

    html_content += """
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)


# End point to manage cache
@app.get("/cache", response_class=HTMLResponse)
async def manage_cache():
    """Endpoint to manage TFSA assistant cache"""
    cache_data = cache.get_all()
    cache_enabled = cache.is_enabled()

    # Generate HTML with cache controls
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>TFSA Assistant Cache</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; }}
            .cache-controls {{ margin-bottom: 20px; }}
            .cache-btn {{ 
                padding: 8px 16px; 
                border-radius: 4px; 
                cursor: pointer;
                font-weight: bold;
                margin-right: 10px;
            }}
            .toggle-btn {{ 
                background-color: {"#4CAF50" if cache_enabled else "#f44336"};
                color: white;
                border: none;
            }}
            .clear-btn {{
                background-color: #2196F3;
                color: white;
                border: none;
            }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            tr:nth-child(even) {{ background-color: #f9f9f9; }}
            .delete-btn {{ 
                background-color: #ff4d4d; 
                color: white; 
                border: none; 
                padding: 5px 10px; 
                border-radius: 4px; 
                cursor: pointer;
            }}
            .delete-btn:hover {{ background-color: #ff1a1a; }}
            .cache-status {{
                padding: 8px 16px;
                background-color: {"#4CAF50" if cache_enabled else "#f44336"};
                color: white;
                border-radius: 4px;
                font-weight: bold;
                display: inline-block;
            }}
            .value-container {{
                max-height: 150px;
                overflow-y: auto;
                white-space: pre-wrap;
                word-break: break-all;
                font-family: monospace;
                font-size: 14px;
                padding: 5px;
                background-color: #f8f8f8;
                border: 1px solid #ddd;
                border-radius: 4px;
            }}
            .countdown {{
                font-weight: bold;
                color: #e67e22;
            }}
            .expired {{
                color: #e74c3c;
                font-weight: bold;
            }}
        </style>
        <script>
            async function toggleCache() {{
                const response = await fetch('/cache/toggle', {{ method: 'POST' }});
                if (response.ok) {{
                    const result = await response.json();
                    alert('Cache is now ' + (result.status ? 'ENABLED' : 'DISABLED'));
                    location.reload();
                }} else {{
                    alert('Failed to toggle cache');
                }}
            }}

            async function clearCache() {{
                if (confirm('Are you sure you want to clear ALL cache items?')) {{
                    const response = await fetch('/cache/clear', {{ method: 'POST' }});
                    if (response.ok) {{
                        alert('Cache cleared successfully!');
                        location.reload();
                    }} else {{
                        alert('Failed to clear cache');
                    }}
                }}
            }}

            async function deleteItem(key) {{
                if (confirm('Are you sure you want to delete this cache item?')) {{
                    const response = await fetch(`/cache/${{key}}`, {{ method: 'DELETE' }});
                    if (response.ok) {{
                        alert('Item deleted successfully!');
                        location.reload();
                    }} else {{
                        alert('Failed to delete item');
                    }}
                }}
            }}
            
            // Function to update countdown timers
            function updateCountdowns() {{
                const now = Math.floor(Date.now() / 1000);
                document.querySelectorAll('.countdown').forEach(element => {{
                    const expiresAt = parseInt(element.dataset.expiresAt);
                    if (expiresAt <= 0) {{
                        element.textContent = "Never expires";
                    }} else {{
                        const secondsLeft = expiresAt - now;
                        if (secondsLeft <= 0) {{
                            element.textContent = "EXPIRED";
                            element.classList.add('expired');
                            
                            // Remove row after 2 seconds
                            setTimeout(() => {{
                                const row = element.closest('tr');
                                if (row) row.remove();
                            }}, 2000);
                        }} else {{
                            const hours = Math.floor(secondsLeft / 3600);
                            const minutes = Math.floor((secondsLeft % 3600) / 60);
                            const seconds = secondsLeft % 60;
                            element.textContent = `${{hours}}h ${{minutes}}m ${{seconds}}s`;
                        }}
                    }}
                }});
            }}
            
            // Initialize countdown timers
            document.addEventListener('DOMContentLoaded', () => {{
                updateCountdowns();
                setInterval(updateCountdowns, 1000);
            }});
        </script>
    </head>
    <body>
        <div class="header">
            <h1>TFSA Assistant Cache</h1>
            <div class="cache-status">
                Cache Status: {cache_enabled}
            </div>
        </div>

        <div class="cache-controls">
            <button class="cache-btn toggle-btn" onclick="toggleCache()">
                {"Disable" if cache_enabled else "Enable"} Cache
            </button>
            <button class="cache-btn clear-btn" onclick="clearCache()">
                Clear Entire Cache
            </button>
        </div>

        <p>Total items: {len(cache_data)}</p>
        <table>
            <thead>
                <tr>
                    <th>Key</th>
                    <th>Value</th>
                    <th>Time Remaining</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
    """

    # Add table rows with full values in scrollable containers
    for key, item in cache_data.items():
        metadata = item.get("metadata", "")
        expires_at = item["expires_at"]
        value_str = str(item["value"])

        # Format expiration time
        expires_display = "Never"
        if expires_at > 0:
            expires_dt = datetime.fromtimestamp(expires_at)
            expires_display = expires_dt.strftime('%Y-%m-%d %H:%M:%S')

        html_content += f"""
        <tr>
            <td>{key}<br/>{expires_display}<br/>{metadata}</td>
            <td class="value-container">{value_str}</td>
            <td class="countdown" data-expires-at="{expires_at}">
                Calculating...
            </td>
            <td>
                <button class="delete-btn" onclick="deleteItem('{key}')">
                    Delete
                </button>
            </td>
        </tr>
        """

    html_content += """
            </tbody>
        </table>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)


# Add new endpoints for cache control
@app.post("/cache/toggle")
async def toggle_cache():
    """Toggle cache enabled state"""
    current_state = not cache.is_enabled()
    cache.set_enabled(current_state)
    return {"status": current_state, "message": f"Cache is now {'ENABLED' if current_state else 'DISABLED'}"}


@app.post("/cache/clear")
async def clear_cache():
    """Clear entire cache"""
    cache_data = cache.get_all()

    # Delete all items
    for key in list(cache_data.keys()):
        cache.delete(key)

    return {"status": "success", "message": f"Cleared {len(cache_data)} cache items"}


@app.delete("/cache/{cache_key}")
async def delete_cache_item(cache_key: str):
    """Delete a specific cache item"""
    if cache.delete(cache_key):
        return {"status": "success", "message": f"Cache item {cache_key} deleted"}
    return {"status": "error", "message": "Item not found"}, 404


if __name__ == '__main__':
    import sys
    from fastapi.testclient import TestClient
    from security import get_current_user  # Import for dependency override

    # Only run tests if 'test' argument is passed
    if len(sys.argv) > 1 and sys.argv[1] == 'test':
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
        response = client.post("/chat/completions", json=test_request, headers=headers)
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

        with client.stream("POST", "/chat/completions", json=test_request, headers=headers) as response:
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
        response = client.post("/chat/completions", json=test_request, headers=headers)
        if response.status_code == 200:
            logger.info("Test 3 successful!")
        else:
            logger.error(f"Test 3 failed: {response.text}")

        logger.info("All tests completed!")
        # Exit after tests
        sys.exit(0)

    import uvicorn

    uvicorn.run(app, host='0.0.0.0', port=8080)
