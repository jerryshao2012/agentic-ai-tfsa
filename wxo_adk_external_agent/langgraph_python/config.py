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
