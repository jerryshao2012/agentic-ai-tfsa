# config.py
import os

from dotenv import load_dotenv

load_dotenv('.env')
# Default AI Services provider
# AI_SERVICES_PROVIDER = os.getenv('AI_SERVICES_PROVIDER', 'ollama')

# AI_SERVICES_PROVIDER = os.getenv('AI_SERVICES_PROVIDER', 'deepseek')
DEEPSEEK_API_KEY = os.environ['DEEPSEEK_API_KEY']

AI_SERVICES_PROVIDER = os.getenv('AI_SERVICES_PROVIDER', 'watsonxai')

WATSONX_SPACE_ID = os.getenv('WATSONX_SPACE_ID', None)
WATSONX_PROJECT_ID = os.getenv('WATSONX_PROJECT_ID', None)
WATSONX_API_KEY = os.getenv('WATSONX_API_KEY', None)
WATSONX_URL = os.getenv('WATSONX_URL', 'https://us-south.ml.cloud.ibm.com')

# AI_SERVICES_PROVIDER = os.getenv('AI_SERVICES_PROVIDER', 'openai')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', None)

# Load Tavily API key (set as environment variable TAVILY_API_KEY)
TAVILY_API_KEY = os.getenv('TAVILY_API_KEY', None)
