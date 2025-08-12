# app.py
import json
import logging
import time
import uuid
from typing import Optional, Dict, Any

import mlflow
from fastapi import FastAPI, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from cache_utils import cache_router  # Import cache router
from llm_utils import get_llm_sync, get_llm_stream
from log_utils import log_access, log_router  # Import log functions and router
from models import ChatCompletionRequest, ChatCompletionResponse, Choice, MessageResponse, DEFAULT_MODEL
from security import get_current_user
from tfsa_assistant_graph import run_tfsa_assistant_sync, run_tfsa_assistant_stream, cache, extract_user_id

logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# 1. Create the application that will hold all your API logic and endpoints.
#    We'll call it `api_app`. Note we remove the invalid `prefix` argument.
api_app = FastAPI(
    title="TFSA LangGraph Assistant",
    description="An agentic assistant for TFSA related queries.",
    version="1.0.0",
    # These paths are relative to the mount point, so they will become /api/v1/docs etc.
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)

# 2. Create the main, top-level application. This will be our entrypoint.
app = FastAPI()

# 3. Mount your api_app onto the main app at the desired prefix.
#    This is the key step that prefixes all routes, including docs.
app.mount("/api/v1", api_app)

# Now, all routers and routes are added to the `api_app`, not the main `app`.
# Include the log router to add the /logs endpoint
api_app.include_router(log_router)

# Include the cache router to add the cache endpoints
api_app.include_router(cache_router)

# Enabling tracing for LangGraph (LangChain)
mlflow.langchain.autolog()

# Optional: Set a tracking URI and an experiment
mlflow.set_tracking_uri("http://localhost:5000")
mlflow.set_experiment("TFSA LangGraph")


@api_app.post("/chat/completions")
async def chat_completions(
        request: ChatCompletionRequest,
        x_ibm_thread_id: Optional[str] = Header(None, alias="X-IBM-THREAD-ID",
                                                description="Optional header to specify the thread ID"),
        _current_user: Dict[str, Any] = Depends(get_current_user),
):
    start_time = time.time()
    logger.info(f"Received POST /api/v1/chat/completions ChatCompletionRequest: {request.model_dump_json()}")
    thread_id = ""
    if x_ibm_thread_id:
        thread_id = x_ibm_thread_id
    if request.extra_body and request.extra_body.thread_id:
        thread_id = request.extra_body.thread_id
    logger.info("thread_id: " + thread_id)

    model = DEFAULT_MODEL
    if request.model:
        model = request.model
    # Set up tools as an array of tuples: first function is synchronous, second function is async, together is the first element
    selected_tools = [[run_tfsa_assistant_sync, run_tfsa_assistant_stream]]

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

        # For non-streaming requests
        if not is_stream:
            # Call the tool directly in synchronous mode
            tool_response, _ = selected_tools[0][0](user_input, thread_id, model)

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
            # Create an async generator that wraps the async streaming tool
            async def generate_stream():
                try:
                    # Call the async streaming tool directly
                    async for chunk in selected_tools[0][1](user_input, thread_id, model):
                        yield chunk

                    # Log access after streaming is complete
                    log_access(user_id, thread_id, is_stream, user_input,
                               response=f"[{(time.time() - start_time):.3f} seconds] [Streaming completed]",
                               model=model)

                except Exception as e:
                    logger.error(f"Error in streaming: {str(e)}")
                    # Send error message to client
                    error_struct = {
                        "id": str(uuid.uuid4()),
                        "object": "thread.message.delta",
                        "created": int(time.time()),
                        "thread_id": thread_id,
                        "model": model,
                        "choices": [
                            {
                                "delta": {
                                    "content": f"Error occurred: {str(e)}"
                                },
                                "finish_reason": "error"
                            }
                        ]
                    }
                    yield f"data: {json.dumps(error_struct)}\n\n"
                    yield "data: [DONE]\n\n"

            return StreamingResponse(generate_stream(), media_type="text/event-stream")

    # Standard processing flow
    sync_selected_tools = [tool[0] for tool in selected_tools]
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
        stream_generator = get_llm_stream(request.messages, model, thread_id, sync_selected_tools)
        wrapped_generator = logging_wrapper(stream_generator)

        # Return streaming response
        return StreamingResponse(wrapped_generator, media_type="text/event-stream")
    else:
        last_message, all_messages = get_llm_sync(request.messages, model, thread_id, sync_selected_tools)
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
