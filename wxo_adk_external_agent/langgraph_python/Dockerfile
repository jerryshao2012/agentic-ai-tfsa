FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app

# All environment variables in one layer
ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    AWS_REGION=us-east-1 \
    AWS_DEFAULT_REGION=us-east-1 \
    BEDROCK_AGENTCORE_MEMORY_ID=tfsa_langgraph_agentcore_mem-VNcBuN2LGP \
    BEDROCK_AGENTCORE_MEMORY_NAME=tfsa_langgraph_agentcore_mem

COPY requirements_agentcore.txt requirements_agentcore.txt
# Install from requirements file
RUN uv pip install -r requirements_agentcore.txt

# Keep the version comparator quoted so it is parsed as a package spec, not shell redirection.
RUN uv pip install 'aws-opentelemetry-distro>=0.10.1'

# Create non-root user
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080

# Copy entire project (respecting .dockerignore)
COPY . .
CMD ["opentelemetry-instrument", "python", "-m", "tfsa_agentcore"]
