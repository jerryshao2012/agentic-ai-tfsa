# app.py
import json
import logging
import random
import time
import uuid
from typing import Optional, Dict, Any

from fastapi import FastAPI, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from cache_utils import cache_router  # Import cache router
from llm_utils import get_llm_sync, get_llm_stream
from log_utils import log_access, log_router  # Import log functions and router
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

# Include the log router to add the /logs endpoint
app.include_router(log_router)

# Include the cache router to add the cache endpoints
app.include_router(cache_router)


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
                    time.sleep(random.uniform(0.01, 0.05))

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

        # Capture variables for closure
        async def logging_wrapper(stream_gen,
                                  _thread_cache_key=thread_cache_key,
                                  _thread_state=thread_state,
                                  _user_id=user_id,
                                  _user_input=user_input):
            nonlocal accumulated_response
            success = False
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
                success = True
            except Exception as e:
                logger.error(f"Streaming error: {str(e)}")
                raise
            finally:
                # Log access after the stream completes
                logger.info(f"Logging access for streamed response (len={len(accumulated_response)})")
                log_access(_user_id, thread_id, is_stream, _user_input,
                           response=f"[{(time.time() - start_time):.3f} seconds]\n{accumulated_response}",
                           model=model)

                # Update thread state after successful stream
                if success and accumulated_response and _thread_cache_key:
                    # Create new thread state if none exists
                    if _thread_state is None:
                        _thread_state = {
                            "user_id": _user_id,
                            "messages": []
                        }

                    # Append new messages
                    _thread_state["messages"].append({
                        "role": "user",
                        "content": _user_input
                    })
                    _thread_state["messages"].append({
                        "role": "assistant",
                        "content": accumulated_response
                    })

                    # Save updated state to cache
                    cache.cache(_thread_cache_key, _thread_state)

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

        # Update thread state if exists
        if thread_cache_key:
            # Create new thread state if none exists
            if thread_state is None:
                thread_state = {
                    "user_id": user_id,
                    "messages": []
                }

            # Append new messages
            thread_state["messages"].append({
                "role": "user",
                "content": user_input
            })
            thread_state["messages"].append({
                "role": "assistant",
                "content": last_message
            })

            # Save updated state to cache
            cache.cache(thread_cache_key, thread_state)

        # Log access
        log_access(user_id, thread_id, is_stream, user_input,
                   response=f"[{(time.time() - start_time):.3f} seconds]\n{last_message}",
                   model=model)
        return JSONResponse(content=response.model_dump())


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='0.0.0.0', port=8080)
