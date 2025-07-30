# tfsa_assistant_api.py
import logging
import time
import uuid
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from security import get_current_user
from tfsa_assistant import run_tfsa_assistant_sync, run_tfsa_assistant_stream

logger = logging.getLogger()
logger.setLevel(logging.INFO)
router = APIRouter(prefix="", tags=["TFSA"])


class ExtraBody(BaseModel):
    thread_id: str = None


class Message(BaseModel):
    role: str
    content: str = None
    tool_calls: List[Dict[str, Any]] = None
    tool_call_id: str = None


class ChatCompletionRequest(BaseModel):
    model: str = "tfsa-langgraph"
    messages: List[Message]
    stream: bool = False
    extra_body: ExtraBody = None


class Choice(BaseModel):
    index: int = 0
    message: dict
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Choice]


@router.post("/chat/completions")
async def tfsa_chat_completions(
        payload: ChatCompletionRequest,
        x_ibm_thread_id: Optional[str] = Header(None, alias="X-IBM-THREAD-ID",
                                                description="Optional header to specify the thread ID"),
        _current_user: Dict[str, Any] = Depends(get_current_user),
):
    thread_id = ""
    if x_ibm_thread_id:
        thread_id = x_ibm_thread_id
    if payload.extra_body and payload.extra_body.thread_id:
        thread_id = payload.extra_body.thread_id
    logger.info("thread_id: %s", thread_id)

    logger.info("TFSA request: %s", payload.model_dump())
    raw_messages = [m.model_dump(exclude_none=True) for m in payload.messages]

    if payload.stream:
        return StreamingResponse(
            run_tfsa_assistant_stream(raw_messages),
            media_type="text/event-stream",
        )

    answer = run_tfsa_assistant_sync(raw_messages)
    return ChatCompletionResponse(
        id=str(uuid.uuid4()),
        created=int(time.time()),
        model=payload.model,
        choices=[
            Choice(
                message={
                    "role": "assistant",
                    "content": answer,
                }
            )
        ],
    )
