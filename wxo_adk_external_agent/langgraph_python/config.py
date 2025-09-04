# config.py
import os
from dotenv import load_dotenv

# Load environment variables from .env file for local development.
# In a containerized environment like Code Engine, these will be injected directly.
load_dotenv()

# --- AI Service Provider Configuration ---
AI_SERVICES_PROVIDER = os.getenv('AI_SERVICES_PROVIDER', 'watsonxai')

# --- watsonx.ai Configuration ---
WATSONX_PROJECT_ID = os.getenv('WATSONX_PROJECT_ID', '')
WATSONX_API_KEY = os.getenv('WATSONX_API_KEY', '')
WATSONX_URL = os.getenv('WATSONX_URL', 'https://us-south.ml.cloud.ibm.com')

# --- Optional Provider API Keys ---
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
TAVILY_API_KEY = os.getenv('TAVILY_API_KEY', '')
