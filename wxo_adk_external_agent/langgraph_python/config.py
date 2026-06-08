# config.py
import os
from dotenv import load_dotenv

# Load environment variables from .env file for local development.
# In a containerized environment like Code Engine, these will be injected directly.
load_dotenv()

# --- AI Service Provider Configuration ---
# Defaults to 'watsonxai' for IBM Cloud deployments; set to 'bedrock' for AWS deployments
AI_SERVICES_PROVIDER = os.getenv('AI_SERVICES_PROVIDER', 'watsonxai')

# --- Agent routing ---
# 'supervisor' = an LLM decides which specialist handles each request (more agentic).
# 'rules'      = the legacy deterministic regex router (faster, no extra LLM call).
ROUTER_MODE = os.getenv('ROUTER_MODE', 'supervisor')

# --- Amazon Bedrock Configuration (AWS deployments only) ---
# Model is env-configurable; defaults to Amazon Nova Lite (on-demand, streaming-capable).
# NOTE: amazon.nova-2-lite-v1:0 is inference-profile-only and has no system profile in
# us-east-1, so it can't be invoked on-demand here. Use nova-lite/micro/pro instead.
BEDROCK_MODEL_ID = os.getenv('BEDROCK_MODEL_ID', 'amazon.nova-lite-v1:0')
# AgentCore Runtime supplies AWS credentials via the execution role; region comes from the env.
AWS_REGION = os.getenv('AWS_REGION', os.getenv('BEDROCK_REGION', 'us-east-1'))

# --- Bedrock client resilience ---
# The default botocore retry mode is "legacy" (max 4 attempts) — too few for on-demand Nova/Claude
# under load, which surfaces ThrottlingException as "No response generated". "adaptive" mode adds
# client-side rate limiting + more retries with backoff. max_tokens caps output length (lower =
# faster + cheaper). Tune via env without a code change.
BEDROCK_MAX_ATTEMPTS = int(os.getenv('BEDROCK_MAX_ATTEMPTS', '4'))
BEDROCK_READ_TIMEOUT = int(os.getenv('BEDROCK_READ_TIMEOUT', '60'))
BEDROCK_MAX_TOKENS = int(os.getenv('BEDROCK_MAX_TOKENS', '1024'))

# --- Application-level LLM resilience (provider-agnostic) ---
# On top of the provider's own transport retries (e.g. Bedrock adaptive mode above), each
# _invoke_llm call is retried this many times when it raises OR returns empty content, so a
# transient failure / empty completion becomes a retry instead of a blank reply to the user.
LLM_INVOKE_ATTEMPTS = int(os.getenv('LLM_INVOKE_ATTEMPTS', '2'))

# --- Native extended thinking (Bedrock/Claude only) ---
# When true, LLM nodes that opt in (via _invoke_llm(use_thinking=True)) run through a
# thinking-enabled Claude model and their reasoning blocks are logged on llm_call_end.
# Default OFF: extended thinking forces temperature=1 (slightly less deterministic JSON) and
# adds reasoning tokens + latency, which increases Bedrock throttling pressure. Turn on only
# for debugging. THINKING_BUDGET_TOKENS must be < THINKING_MAX_TOKENS.
ENABLE_THINKING = os.getenv('ENABLE_THINKING', 'false').lower() == 'true'
THINKING_BUDGET_TOKENS = int(os.getenv('THINKING_BUDGET_TOKENS', '1024'))
THINKING_MAX_TOKENS = int(os.getenv('THINKING_MAX_TOKENS', '4096'))

# --- watsonx.ai Configuration (IBM Cloud deployments) ---
WATSONX_PROJECT_ID = os.getenv('WATSONX_PROJECT_ID', '')
WATSONX_API_KEY = os.getenv('WATSONX_API_KEY', '')
WATSONX_URL = os.getenv('WATSONX_URL', 'https://us-south.ml.cloud.ibm.com')
WATSONX_SPACE_ID = os.getenv('WATSONX_SPACE_ID', None)

# --- Optional Provider API Keys ---
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
TAVILY_API_KEY = os.getenv('TAVILY_API_KEY', '')

# --- Synthetic data sources (S3) ---
# When DATA_S3_BUCKET is set, profiles/limits/transactions are loaded from S3 (see
# data_sources.py); otherwise the agent falls back to the built-in mock + local files.
# The AgentCore execution role needs s3:GetObject on this bucket.
DATA_S3_BUCKET = os.getenv('DATA_S3_BUCKET', '')
PROFILE_S3_PREFIX = os.getenv('PROFILE_S3_PREFIX', 'profiles')
TRANSACTIONS_S3_PREFIX = os.getenv('TRANSACTIONS_S3_PREFIX', 'transactions')
LIMITS_S3_KEY = os.getenv('LIMITS_S3_KEY', 'reference/tfsa_limits.json')
DATA_S3_REGION = os.getenv('DATA_S3_REGION', AWS_REGION)
