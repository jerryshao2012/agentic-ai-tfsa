# tfsa_assistant_api.py
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from tfsa_assistant import run_tfsa_assistant_sync, run_tfsa_assistant_stream
from wxo_adk_external_agent.langgraph_python.security import get_current_user

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

router = APIRouter(prefix="", tags=["TFSA"])


class ExtraBody(BaseModel):
    thread_id: str = None


class UserInputRequest(BaseModel):
    user_input: Annotated[str, "User input regarding Tax Free Saving Account (TFSA)"]
    user_id: Annotated[str, "bank user ID"] = None
    stream: bool = False
    extra_body: ExtraBody = None


@router.post("/api/v1/get_tfsa_advice")
def get_tfsa_advice(payload: UserInputRequest,
                    x_ibm_thread_id: Optional[str] = Header(None, alias="X-IBM-THREAD-ID",
                                                            description="Optional header to specify the thread ID"),
                    _current_user: Dict[str, Any] = Depends(get_current_user)) -> str:
    """
    As a certified TFSA specialist, respond to user queries using these guidelines:
    1. Verify contribution room before suggesting amounts
    2. Mention penalty risks for over-contributions
    3. Provide current year's contribution limit
    4. Explain withdrawal re-contribution rules
    5. Always include transaction ID when applicable

    Current Date: {datetime.now().date()}
    User ID: {user_id} if login
    Query: {user_input}
    """
    thread_id = ""
    if x_ibm_thread_id:
        thread_id = x_ibm_thread_id
    if payload.extra_body and payload.extra_body.thread_id:
        thread_id = payload.extra_body.thread_id
    logging.info("thread_id: %s", thread_id)

    logging.info("TFSA get_tfsa_advice request: %s", payload.model_dump())
    user_id = payload.user_id
    user_input = payload.user_input

    # Create full user input with user ID
    full_input = f"My user ID is {user_id}. {user_input}" if user_id else user_input

    logging.info(
        f"[{datetime.now().isoformat()}] Resource called: get_tfsa_advice with parameters: user_input='{full_input}'")
    try:
        if payload.stream:
            return StreamingResponse(
                run_tfsa_assistant_stream(full_input),
                media_type="text/event-stream",
            )

        # Execute workflow
        _, result = run_tfsa_assistant_sync(full_input)

        # Extract last assistant message
        assistant_msgs = [msg['content'] for msg in result['messages']
                          if msg.get('role') == 'assistant']
        response_text = assistant_msgs[-1] if assistant_msgs else "No response generated"
        return PlainTextResponse(content=response_text)

    except Exception as e:
        logging.exception("TFSA processing failed")
        if payload.stream:
            def generate_error():
                yield f"data: ❌ Processing error: {str(e)}\n\n"

            return StreamingResponse(
                generate_error(),
                media_type="text/event-stream",
                status_code=500
            )
        return PlainTextResponse(
            content=f"❌ Processing error: {str(e)}",
            status_code=500
        )
