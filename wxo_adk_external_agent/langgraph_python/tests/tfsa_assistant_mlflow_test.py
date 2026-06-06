# tfsa_assistant_mlflow_test.py
import mlflow

from tfsa_assistant_graph import run_tfsa_assistant_sync

if __name__ == "__main__":
    # Enabling tracing for LangGraph (LangChain)
    try:
        mlflow.langchain.autolog()
        # Optional: Set a tracking URI and an experiment
        mlflow.set_tracking_uri("http://localhost:5000")
        mlflow.set_experiment("TFSA LangGraph")
    except Exception as e:
        print(f"Failed to initialize MLflow: {e}")

    result = run_tfsa_assistant_sync("What are the annual dollar limits for each year of TSFA, including 2025?")
    print(result)
