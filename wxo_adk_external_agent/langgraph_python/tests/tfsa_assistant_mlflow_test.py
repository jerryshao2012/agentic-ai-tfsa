# tfsa_assistant_mlflow_test.py
import os
import sys

# Add the parent directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import mlflow

from tfsa_assistant_graph import run_tfsa_assistant_sync

# Enabling tracing for LangGraph (LangChain)
mlflow.langchain.autolog()

# Optional: Set a tracking URI and an experiment
mlflow.set_tracking_uri("http://localhost:5000")
mlflow.set_experiment("TFSA LangGraph")

if __name__ == "__main__":
    result = run_tfsa_assistant_sync("What are the annual dollar limits for each year of TSFA, including 2025?")
    print(result)
