#!/usr/bin/env bash
set -euo pipefail  # Exit on error, undefined variables, pipe failure
BUILD_DIR="langgraph_python"
echo "In $BUILD_DIR folder..."
cd ..

# Load environment variables
if [[ -f .env ]]; then
    source .env
else
    echo "Error: .env file not found. Please create one with required variables."
    exit 1
fi

# Validate required environment variables
required_vars=("WATSONX_API_KEY" "TAVILY_API_KEY" "WATSONX_URL")
for var in "${required_vars[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        echo "Error: $var is not set in .env file"
        exit 1
    fi
done
cd "$BUILD_DIR"

# Configuration
CE_REGION="us-south"
CE_PROJECT_NAME="wxo-agent-tfsa-app1"
IMAGE_NAME="tfsa-agent-app"
REGISTRY_SECRET_NAME="tfsa-agent-app-secret"
CONTAINER_NAMESPACE="$(ibmcloud cr namespaces --output json | jq -r '.[].name' | head -1)"

echo "=== TFSA Agent Deployment to IBM Code Engine ==="

# IBM Cloud Login
echo "1. Authenticating to IBM Cloud..."
ibmcloud login --apikey "$WATSONX_API_KEY" -r "$CE_REGION" --quiet

# Set target resource group
echo "2. Setting up resource group..."
CE_RESOURCE_GROUP="$(ibmcloud resource groups --output json | jq -r '.[].name' | grep '^itz-' | head -1)"
if [[ -z "$CE_RESOURCE_GROUP" ]]; then
    echo "Error: No resource group found"
    exit 1
fi
ibmcloud target -g "$CE_RESOURCE_GROUP"

# Select Code Engine project
echo "3. Selecting Code Engine project..."
CE_PROJECT_ID="$(ibmcloud ce project list --output json | jq -r '.[0].name')"
ibmcloud ce project select -n "$CE_PROJECT_ID"

# Create registry secret
echo "4. Creating registry secret..."
if ! ibmcloud ce registry get -n "$REGISTRY_SECRET_NAME" &>/dev/null; then
    ibmcloud ce registry create --name "$REGISTRY_SECRET_NAME" \
        --server us.icr.io \
        --username iamapikey \
        --password "$WATSONX_API_KEY"
else
    echo "Registry secret already exists, skipping creation"
fi

echo "5. Building and pushing container..."

# Install and setup Podman if not available
if ! command -v podman &>/dev/null; then
    echo "Installing Podman..."
    if command -v brew &>/dev/null; then
        brew install podman
    else
        echo "Error: Homebrew not available. Please install Podman manually."
        exit 1
    fi
fi

# Initialize and start Podman machine if needed
if ! podman machine list | grep -q running; then
    echo "Setting up Podman machine..."
    if ! podman machine list | grep -q qemu; then
        podman machine init --cpus 2 --memory 2048 --disk-size 20
    fi
    podman machine start
fi

# Login to container registry
echo "Logging in to container registry..."
podman login us.icr.io --username iamapikey --password "$WATSONX_API_KEY"

# Build, tag, and push container
echo "Building container image..."
# Check for and navigate to the build directory
if [[ -d "$BUILD_DIR" ]]; then
    echo "Building from $BUILD_DIR directory..."
    cd "$BUILD_DIR"
fi
podman build . -t "$IMAGE_NAME" --platform linux/amd64 --pull-always

echo "Tagging image for registry..."
podman tag localhost/"$IMAGE_NAME" "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest"

echo "Pushing image to registry..."
podman push "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest"

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
    ibmcloud ce application update --name "$CE_PROJECT_NAME" \
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
        --quiet
else
    echo "Creating new application..."
    ibmcloud ce application create --name "$CE_PROJECT_NAME" \
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
        --quiet
fi

# Wait for deployment
echo "7. Waiting for deployment to complete..."
while [[ $(ibmcloud ce application get -n "$CE_PROJECT_NAME" -o json | jq -r '.status.conditions[] | select(.type == "Ready").status') != "True" ]]; do
    echo "Still deploying..."
    sleep 5
done

# Get application URL
PUBLIC_URL=$(ibmcloud ce application get -n "$CE_PROJECT_NAME" -o json | jq -r '.status.url')
echo "8. Deployment complete. Public URL: $PUBLIC_URL"

# Test endpoints
echo "9. Testing endpoints..."
echo "Testing sync endpoint:"
curl -X POST "$PUBLIC_URL/api/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "meta-llama/llama-3-2-90b-vision-instruct",
        "messages": [{
            "role": "user",
            "content": "What are the annual dollar limits for each year of TSFA, including 2025?"
        }],
        "stream": false
    }' || echo "Test call failed"

echo -e "\nTesting streaming endpoint:"
curl -X POST "$PUBLIC_URL/api/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "meta-llama/llama-3-2-90b-vision-instruct",
        "messages": [{
            "role": "user",
            "content": "What are the annual dollar limits for each year of TSFA, including 2025?"
        }],
        "stream": true
    }' || echo "Test call failed"

echo "10. Setting up Orchestrate environment..."
if command -v orchestrate &>/dev/null; then
    # Check if environment already exists
    if orchestrate env list | grep -q "watsonx-challenge"; then
        echo "Environment already exists, activating and updating API key..."
        orchestrate env activate watsonx-challenge --api-key "$WATSONX_API_KEY"
    else
        echo "Creating new environment..."
        if [[ -z "${WO_INSTANCE:-}" ]]; then
            echo "Warning: WO_INSTANCE not set, skipping environment creation"
        else
            orchestrate env add -n watsonx-challenge -u "$WO_INSTANCE" --type ibm_iam --activate
        fi
    fi

    # Import agents if environment is available
    if orchestrate env list | grep -q "watsonx-challenge"; then
        echo "Importing external agents..."
        orchestrate agents import -f agents/tfsa_langgraph_external_agent.yaml || \
            echo "Warning: Failed to import tfsa_langgraph_external_agent.yaml"
        orchestrate agents import -f agents/connection_with_tfsa_external_agent.yaml || \
            echo "Warning: Failed to import connection_with_tfsa_external_agent.yaml"
    else
        echo "Skipping agent import - watsonx-challenge environment not available"
    fi

    # List environments for verification
    orchestrate env list
else
    echo "Orchestrate CLI not found, skipping environment setup"
fi

echo "=== Deployment complete ==="