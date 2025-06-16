from urllib.request import Request

from fastapi import APIRouter
# Add rate limiting
from slowapi import Limiter
from slowapi.util import get_remote_address

from agentic_workflow.tsfa.tfsa_assistant import run_tfsa_assistant

limiter = Limiter(key_func=get_remote_address)
router = APIRouter()


@router.post("/tfsa-contribution")
@limiter.limit("5/minute")
async def contribute(request: Request, payload: dict):
    return run_tfsa_assistant(payload["query"])
