#!/usr/bin/env bash
set -euo pipefail  # Exit on error, undefined variables, pipe failure

# Configuration
BUILD_DIR="langgraph_python"
CE_REGION="us-south"
CE_PROJECT_NAME="tfsa-agent-app-project"
IMAGE_NAME="tfsa-agent-app-image"
REGISTRY_SECRET_NAME="tfsa-agent-app-secret"
ORCHESTRATE_ENV_NAME="tfsa-agent-app-orchestrate-env"
MAX_DEPLOYMENT_WAIT_TIME=300  # 5 minutes in seconds
DEPLOYMENT_CHECK_INTERVAL=5   # Check every 5 seconds

echo "Starting TFSA Agent deployment process..."

# Check required commands are available
check_command() {
    if ! command -v "$1" &> /dev/null; then
        echo "Error: $1 is required but not installed."
        exit 1
    fi
}

required_commands=("ibmcloud" "jq" "curl")
for cmd in "${required_commands[@]}"; do
    check_command "$cmd"
done

# Load environment variables
ENV_FILE="../.env"
if [[ -f "$ENV_FILE" ]]; then
    echo "Loading environment variables from $ENV_FILE"
    # shellcheck source=../.env
    source "$ENV_FILE"
else
    echo "Error: $ENV_FILE file not found. Please create one with required variables."
    exit 1
fi

# Validate required environment variables
required_vars=("WATSONX_API_KEY" "TAVILY_API_KEY" "WATSONX_URL")
missing_vars=()
for var in "${required_vars[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        missing_vars+=("$var")
    fi
done

if [[ ${#missing_vars[@]} -gt 0 ]]; then
    echo "Error: The following required variables are not set in .env file:"
    printf '%s\n' "${missing_vars[@]}"
    exit 1
fi

# Get container namespace
echo "Retrieving container registry namespace..."
CONTAINER_NAMESPACE=$(ibmcloud cr namespaces --output json | jq -r '.[].name' | head -1)
if [[ -z "$CONTAINER_NAMESPACE" ]]; then
    echo "Error: No container registry namespace found."
    exit 1
fi
echo "Using container namespace: $CONTAINER_NAMESPACE"

echo "=== TFSA Agent Deployment to IBM Code Engine ==="

# IBM Cloud Login
echo "1. Authenticating to IBM Cloud..."
if ! ibmcloud login --apikey "$WATSONX_API_KEY" -r "$CE_REGION" --quiet; then
    echo "Error: Failed to login to IBM Cloud"
    exit 1
fi

# Set target resource group
echo "2. Setting up resource group..."
CE_RESOURCE_GROUP=$(ibmcloud resource groups --output json | jq -r '.[].name' | grep '^itz-' | head -1)
if [[ -z "$CE_RESOURCE_GROUP" ]]; then
    echo "Error: No resource group found"
    exit 1
fi

if ! ibmcloud target -g "$CE_RESOURCE_GROUP"; then
    echo "Error: Failed to set target resource group"
    exit 1
fi

# Select Code Engine project
echo "3. Selecting Code Engine project..."
CE_PROJECT_ID=$(ibmcloud ce project list --output json | jq -r '.[0].name')
if [[ -z "$CE_PROJECT_ID" ]]; then
    echo "Error: No Code Engine project found"
    exit 1
fi

if ! ibmcloud ce project select -n "$CE_PROJECT_ID"; then
    echo "Error: Failed to select Code Engine project"
    exit 1
fi

# Create registry secret
echo "4. Creating registry secret..."
if ! ibmcloud ce registry get -n "$REGISTRY_SECRET_NAME" &>/dev/null; then
    if ! ibmcloud ce registry create --name "$REGISTRY_SECRET_NAME" \
        --server us.icr.io \
        --username iamapikey \
        --password "$WATSONX_API_KEY"; then
        echo "Error: Failed to create registry secret"
        exit 1
    fi
    echo "Registry secret created successfully"
else
    echo "Registry secret already exists, skipping creation"
fi

echo "5. Building and pushing container..."

# Install and setup Podman if not available
if ! command -v podman &>/dev/null; then
    echo "Installing Podman..."
    if command -v brew &>/dev/null; then
        if ! brew install podman; then
            echo "Error: Failed to install Podman"
            exit 1
        fi
    else
        echo "Error: Homebrew not available. Please install Podman manually."
        exit 1
    fi
fi

# Initialize and start Podman machine if needed
if ! podman machine list | grep -q running; then
    echo "Setting up Podman machine..."
    if ! podman machine list | grep -q qemu; then
        if ! podman machine init --cpus 2 --memory 2048 --disk-size 20; then
            echo "Error: Failed to initialize Podman machine"
            exit 1
        fi
    fi
    if ! podman machine start; then
        echo "Error: Failed to start Podman machine"
        exit 1
    fi
fi

# Login to container registry
echo "Logging in to container registry..."
if ! podman login us.icr.io --username iamapikey --password "$WATSONX_API_KEY"; then
    echo "Error: Failed to login to container registry"
    exit 1
fi

# Check for and navigate to the build directory
CURRENT_DIR=$(basename "$PWD")
ORIGINAL_DIR="$PWD"

if [[ "$CURRENT_DIR" == "$BUILD_DIR" ]]; then
    echo "Already in $BUILD_DIR directory, building from current location..."
elif [[ -d "$BUILD_DIR" ]]; then
    echo "Building from $BUILD_DIR directory..."
    cd "$BUILD_DIR"
else
    echo "Warning: $BUILD_DIR directory not found, building from current directory ($CURRENT_DIR)"
    # Check if essential build files exist in current directory
    if [[ ! -f "Dockerfile" ]] && [[ ! -f "requirements.txt" ]]; then
        echo "Error: No build files (Dockerfile, or requirements.txt) found in current directory"
        exit 1
    fi
fi

# Build, tag, and push container
echo "Building container image..."
if ! podman build . -t "$IMAGE_NAME" --platform linux/amd64 --pull-always; then
    echo "Error: Failed to build container image"
    exit 1
fi

# Return to original directory if we changed
if [[ "$PWD" != "$ORIGINAL_DIR" ]]; then
    cd "$ORIGINAL_DIR"
fi

echo "Tagging image for registry..."
if ! podman tag localhost/"$IMAGE_NAME" "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest"; then
    echo "Error: Failed to tag image"
    exit 1
fi

echo "Pushing image to registry..."
if ! podman push "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest"; then
    echo "Error: Failed to push image to registry"
    exit 1
fi

# Verify the image was pushed successfully
echo "Verifying image push..."
if ibmcloud cr image-list --restrict "$CONTAINER_NAMESPACE/$IMAGE_NAME" | grep -q "latest"; then
    echo "✓ Image successfully pushed to registry"
else
    echo "✗ Error: Image not found in registry"
    exit 1
fi

# Deploy application
echo "6. Deploying application..."
if ibmcloud ce application get --name "$CE_PROJECT_NAME" --output json >/dev/null 2>&1; then
    echo "Application already exists, updating deployment..."
    if ! ibmcloud ce application update --name "$CE_PROJECT_NAME" \
        --image "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest" \
        --registry-secret "$REGISTRY_SECRET_NAME" \
        --env LOGGING_LEVEL=DEBUG \
        --env AI_SERVICES_PROVIDER=watsonxai \
        --env TAVILY_API_KEY="$TAVILY_API_KEY" \
        --env WATSONX_API_KEY="$WATSONX_API_KEY" \
        --env WATSONX_URL="$WATSONX_URL" \
        --min-scale 1 \
        --max-scale 1 \
        --port 8080 \
        --quiet; then
        echo "Error: Failed to update application"
        exit 1
    fi
else
    echo "Creating new application..."
    if ! ibmcloud ce application create --name "$CE_PROJECT_NAME" \
        --image "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest" \
        --registry-secret "$REGISTRY_SECRET_NAME" \
        --env LOGGING_LEVEL=DEBUG \
        --env AI_SERVICES_PROVIDER=watsonxai \
        --env TAVILY_API_KEY="$TAVILY_API_KEY" \
        --env WATSONX_API_KEY="$WATSONX_API_KEY" \
        --env WATSONX_URL="$WATSONX_URL" \
        --min-scale 1 \
        --max-scale 1 \
        --visibility public \
        --port 8080 \
        --quiet; then
        echo "Error: Failed to create application"
        exit 1
    fi
fi

# Wait for deployment
echo "7. Waiting for deployment to complete..."
start_time=$(date +%s)
while true; do
    current_time=$(date +%s)
    elapsed_time=$((current_time - start_time))

    if [[ $elapsed_time -ge $MAX_DEPLOYMENT_WAIT_TIME ]]; then
        echo "Error: Deployment timeout after $MAX_DEPLOYMENT_WAIT_TIME seconds"
        exit 1
    fi

    status=$(ibmcloud ce application get -n "$CE_PROJECT_NAME" -o json | jq -r '.status.conditions[] | select(.type == "Ready").status' 2>/dev/null || echo "Unknown")

    if [[ "$status" == "True" ]]; then
        echo "Deployment completed successfully"
        break
    elif [[ "$status" == "False" ]]; then
        echo "Error: Deployment failed"
        exit 1
    else
        echo "Still deploying... (elapsed: ${elapsed_time}s)"
        sleep "$DEPLOYMENT_CHECK_INTERVAL"
    fi
done

# Get application URL
PUBLIC_URL=$(ibmcloud ce application get -n "$CE_PROJECT_NAME" -o json | jq -r '.status.url')
if [[ -z "$PUBLIC_URL" || "$PUBLIC_URL" == "null" ]]; then
    echo "Error: Failed to get application URL"
    exit 1
fi
echo "8. Deployment complete. Public URL: $PUBLIC_URL"

# Test endpoints
echo "9. Testing endpoints..."
echo "Testing sync endpoint:"
if ! curl -s -X POST "$PUBLIC_URL/api/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "meta-llama/llama-3-2-90b-vision-instruct",
        "messages": [{
            "role": "user",
            "content": "What are the annual dollar limits for each year of TSFA, including 2025?"
        }],
        "stream": false
    }'; then
    echo "Test call failed"
fi

echo -e "\nTesting streaming endpoint:"
if ! curl -s -X POST "$PUBLIC_URL/api/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "meta-llama/llama-3-2-90b-vision-instruct",
        "messages": [{
            "role": "user",
            "content": "What are the annual dollar limits for each year of TSFA, including 2025?"
        }],
        "stream": true
    }'; then
    echo "Test call failed"
fi

echo "10. Setting up Orchestrate environment..."
if command -v orchestrate &>/dev/null; then
    # Check if environment already exists
    if orchestrate env list | grep -q "$ORCHESTRATE_ENV_NAME"; then
        echo "Environment already exists, activating and updating API key..."
        if ! orchestrate env activate $ORCHESTRATE_ENV_NAME --api-key "$WATSONX_API_KEY"; then
            echo "Warning: Failed to activate environment"
        fi
    else
        echo "Creating new environment..."
        if [[ -z "${WO_INSTANCE:-}" ]]; then
            echo "Warning: WO_INSTANCE not set, skipping environment creation"
        else
            if ! orchestrate env add -n $ORCHESTRATE_ENV_NAME -u "$WO_INSTANCE" --type ibm_iam --activate; then
                echo "Warning: Failed to create environment"
            fi
        fi
    fi

    # Import agents if environment is available
    if orchestrate env list | grep -q "$ORCHESTRATE_ENV_NAME"; then
        echo "Importing external agents..."
        if ! orchestrate agents import -f agents/tfsa_langgraph_external_agent.yaml; then
            echo "Warning: Failed to import tfsa_langgraph_external_agent.yaml"
        fi
        if ! orchestrate agents import -f agents/connection_with_tfsa_external_agent.yaml; then
            echo "Warning: Failed to import connection_with_tfsa_external_agent.yaml"
        fi
    else
        echo "Skipping agent import - $ORCHESTRATE_ENV_NAME environment not available"
    fi

    # List environments for verification
    orchestrate env list
else
    echo "Orchestrate CLI not found, skipping environment setup"
fi

echo "=== Deployment complete ==="