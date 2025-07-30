# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (for Ollama, curl, etc.)
RUN apt-get update && \
    apt-get install -y curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY *.py ./

# Expose port
EXPOSE 8080

# Start the FastAPI service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]