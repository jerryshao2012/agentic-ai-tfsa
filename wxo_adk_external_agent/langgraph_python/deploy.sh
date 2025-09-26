#!/usr/bin/env bash
set -euo pipefail  # Exit on error, undefined variables, pipe failure
set +x  # Avoid Printing Secrets
# Configuration
readonly CE_REGION="us-south"
readonly CE_PROJECT_NAME="tfsa-agent-app-project"
readonly IMAGE_NAME="tfsa-agent-app-image"
readonly REGISTRY_SECRET_NAME="tfsa-agent-app-secret"
readonly ORCHESTRATE_ENV_NAME="tfsa-agent-app-orchestrate-env"
readonly DEFAULT_MAX_DEPLOYMENT_WAIT_TIME=300  # 5 minutes in seconds
readonly DEPLOYMENT_CHECK_INTERVAL=5   # Check every 5 seconds

# Get the directory where the script is located to make it runnable from anywhere
readonly SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Global variables
DRY_RUN=false
SHOW_HELP=false
MAX_DEPLOYMENT_WAIT_TIME=$DEFAULT_MAX_DEPLOYMENT_WAIT_TIME
AGENT_FILES_ORDERED=()
AGENT_FILES_REVERSED=()

# ANSI color codes
readonly COLOR_RESET='\033[0m'
readonly COLOR_RED='\033[0;31m'
readonly COLOR_GREEN='\033[0;32m'
readonly COLOR_YELLOW='\033[1;33m'
readonly COLOR_CYAN='\033[0;36m'
readonly COLOR_BOLD='\033[1m'

log_info() {
    echo -e "${COLOR_GREEN}[INFO]${COLOR_RESET} $*"
}

log_error() {
    echo -e "${COLOR_RED}[ERROR]${COLOR_RESET} $*" >&2
}

log_warn() {
    echo -e "${COLOR_YELLOW}[WARN]${COLOR_RESET} $*"
}

# New function to handle dry-run execution
execute() {
    if [[ "$DRY_RUN" == true ]]; then
        echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would execute: $*"
        return 0
    else
        "$@"
    fi
}

check_requirements() {
  log_info "Checking requirements..."
  # Check required commands are available
  check_command() {
      if ! command -v "$1" &> /dev/null; then
          log_error "$1 is required but not installed."
          exit 1
      fi
  }

  required_commands=("ibmcloud" "jq" "curl" "brew" "yq")
  for cmd in "${required_commands[@]}"; do
      check_command "$cmd"
  done

  # Load environment variables
  ENV_FILE="${SCRIPT_DIR}/../../.env"
  if [[ -f "$ENV_FILE" ]]; then
      log_info "Loading and exporting environment variables from $ENV_FILE"
      source "$ENV_FILE"
  else
      log_error "$ENV_FILE file not found. Please create one with required variables."
      exit 1
  fi

  # Validate required environment variables
  required_vars=("WATSONX_API_KEY" "WATSONX_PROJECT_ID" "WATSONX_URL" "TAVILY_API_KEY" "WO_INSTANCE" "WO_API_KEY" "AI_SERVICES_PROVIDER" "LOGGING_LEVEL")
  missing_vars=()
  for var in "${required_vars[@]}"; do
      if [[ -z "${!var:-}" ]]; then
          missing_vars+=("$var")
      fi
  done

  if [[ ${#missing_vars[@]} -gt 0 ]]; then
      log_error "The following required variables are not set in .env file. Please check the file for formatting issues (e.g., extra spaces, special characters, or incorrect quoting)."
      printf '%s\n' "${missing_vars[@]}"
      exit 1
  fi
}

# IBM Cloud Login
authenticate_to_ibmcloud() {
  log_info "1. Authenticating to IBM Cloud..."
  if ! execute ibmcloud login --apikey "$WATSONX_API_KEY" -r "$CE_REGION" --quiet; then
      log_error "Failed to login to IBM Cloud"
      exit 1
  fi
  log_info "Authentication successful."
}

setup_resource_group() {
  # Set target resource group, just grab the first resource group
  log_info "2. Setting up resource group..."
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would retrieve resource group"
    CE_RESOURCE_GROUP="dry-run-resource-group"
  else
    CE_RESOURCE_GROUP=$(ibmcloud resource groups --output json | jq -r '.[].name' | grep '^itz-' | head -1) || {
        log_error "Failed to parse code engine resource group"
        exit 1
    }
  fi

  if [[ -z "$CE_RESOURCE_GROUP" ]]; then
      log_error "No resource group found"
      exit 1
  fi

  if ! execute ibmcloud target -g "$CE_RESOURCE_GROUP"; then
      log_error "Failed to set target resource group"
      exit 1
  fi
  log_info "Resource group set to: $CE_RESOURCE_GROUP"
}

select_code_engine_project() {
  # Select Code Engine project
  log_info "3. Selecting Code Engine project..."
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would retrieve Code Engine project"
    CE_PROJECT_ID="dry-run-project"
  else
    CE_PROJECT_ID=$(ibmcloud ce project list --output json | jq -r '.[0].name') || {
        log_error "Failed to parse project list"
        exit 1
    }
  fi

  if [[ -z "$CE_PROJECT_ID" ]]; then
      log_error "No Code Engine project found"
      exit 1
  fi

  if ! execute ibmcloud ce project select -n "$CE_PROJECT_ID"; then
      log_error "Failed to select Code Engine project"
      exit 1
  fi
  log_info "Code Engine project selected: $CE_PROJECT_ID"
}

create_registry_secret() {
  # Create registry secret
  log_info "4. Creating registry secret..."
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would check if registry secret exists"
    secret_exists=false
  else
    secret_exists=false
    if ibmcloud ce registry get -n "$REGISTRY_SECRET_NAME" &>/dev/null; then
      secret_exists=true
    fi
  fi

  if [[ "$secret_exists" == false ]]; then
      if ! execute ibmcloud ce registry create --name "$REGISTRY_SECRET_NAME" \
          --server us.icr.io \
          --username iamapikey \
          --password "$WATSONX_API_KEY"; then
          log_error "Failed to create registry secret"
          exit 1
      fi
      log_info "Registry secret created successfully"
  else
      log_info "Registry secret '$REGISTRY_SECRET_NAME' already exists, skipping creation."
  fi
}

get_container_namespace() {
  # If CONTAINER_NAMESPACE is not set, retrieve it.
  if [[ -z "${CONTAINER_NAMESPACE:-}" ]]; then
    log_info "Retrieving container registry namespace..."
    if [[ "$DRY_RUN" == true ]]; then
      echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would retrieve container registry namespace"
      CONTAINER_NAMESPACE="dry-run-namespace"
    else
      CONTAINER_NAMESPACE=$(ibmcloud cr namespaces --output json | jq -r '.[].name' | head -1) || {
          log_error "Failed to parse container registry namespace."
          exit 1
      }
    fi

    if [[ -z "$CONTAINER_NAMESPACE" ]]; then
        log_error "No container registry namespace found."
        exit 1
    fi
    log_info "Using container namespace: $CONTAINER_NAMESPACE"
  fi
}

build_and_push_image() {
  log_info "5. Building and pushing container image..."

  get_container_namespace
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would call create_registry_secret"
  else
    create_registry_secret
  fi

  # Install and setup Podman if not available
  if ! command -v podman &>/dev/null; then
      log_warn "Podman not found. Attempting to install via package manager..."

      if command -v apt-get &>/dev/null; then
          if [[ "$DRY_RUN" == false ]]; then
            sudo apt-get update && sudo apt-get install -y podman
          else
            echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would install podman via apt-get"
          fi
      elif command -v yum &>/dev/null; then
          if [[ "$DRY_RUN" == false ]]; then
            sudo yum install -y podman
          else
            echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would install podman via yum"
          fi
      elif command -v brew &>/dev/null; then
          if [[ "$DRY_RUN" == false ]]; then
            brew install podman
          else
            echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would install podman via brew"
          fi
      else
          log_error "Unsupported OS. Please install Podman manually."
          exit 1
      fi
  fi

  # Initialize and start Podman machine if needed
  if ! podman machine list | grep -q running; then
      log_info "Setting up Podman machine..."
      if ! podman machine list | grep -q qemu; then
          if ! execute podman machine init --cpus 2 --memory 2048 --disk-size 20; then
              log_error "Failed to initialize Podman machine"
              exit 1
          fi
      fi
      if ! execute podman machine start; then
          log_error "Failed to start Podman machine"
          exit 1
      fi
  fi

  # Login to container registry
  log_info "Logging in to container registry..."
  if ! execute podman login us.icr.io --username iamapikey --password "$WATSONX_API_KEY"; then
      log_error "Failed to login to container registry"
      exit 1
  fi

  # Build, tag, and push container
  log_info "Building container image from context: ${SCRIPT_DIR}"
  # The build context is the script's directory, where the Dockerfile is expected to be.
  if ! execute podman build "${SCRIPT_DIR}" -t "$IMAGE_NAME" --platform linux/amd64 --pull-always; then
      log_error "Failed to build container image"
      exit 1
  fi

  log_info "Tagging image for registry..."
  if ! execute podman tag localhost/"$IMAGE_NAME" "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest"; then
      log_error "Failed to tag image"
      exit 1
  fi

  log_info "Pushing image to registry..."
  if ! execute podman push "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest"; then
      log_error "Failed to push image to registry"
      exit 1
  fi

  # Verify the image was pushed successfully
  log_info "Verifying image push..."
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would verify image push"
  else
    if ibmcloud cr image-list --restrict "$CONTAINER_NAMESPACE/$IMAGE_NAME" | grep -q "latest"; then
        log_info "✓ Image successfully pushed to registry"
    else
        log_error "✗ Image not found in registry"
        exit 1
    fi
  fi
}

deploy_application() {
  # Deploy application
  log_info "6. Deploying application..."
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would check if application exists"
    app_exists=false
  else
    app_exists=false
    if ibmcloud ce application get --name "$CE_PROJECT_NAME" --output json >/dev/null 2>&1; then
      app_exists=true
    fi
  fi

  get_container_namespace

  if [[ "$app_exists" == true ]]; then
      log_info "Application already exists, updating deployment..."
      if ! execute ibmcloud ce application update --name "$CE_PROJECT_NAME" \
          --image "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest" \
          --registry-secret "$REGISTRY_SECRET_NAME" \
          --env LOGGING_LEVEL="$LOGGING_LEVEL" \
          --env AI_SERVICES_PROVIDER="$AI_SERVICES_PROVIDER" \
          --env DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
          --env WO_DEVELOPER_EDITION_SOURCE="orchestrate" \
          --env WO_INSTANCE="$WO_INSTANCE" \
          --env WO_API_KEY="$WO_API_KEY" \
          --env WATSONX_URL="$WATSONX_URL" \
          --env WATSONX_API_KEY="$WATSONX_API_KEY" \
          --env WATSONX_PROJECT_ID="$WATSONX_PROJECT_ID" \
          --env WATSONX_SPACE_ID="$WATSONX_SPACE_ID" \
          --env OPENAI_API_KEY="$OPENAI_API_KEY" \
          --env TAVILY_API_KEY="$TAVILY_API_KEY" \
          --min-scale 1 \
          --max-scale 1 \
          --port 8080 \
          --quiet; then
          log_error "Failed to update application"
          exit 1
      fi
  else
      log_info "Creating new application..."
      if ! execute ibmcloud ce application create --name "$CE_PROJECT_NAME" \
          --image "us.icr.io/$CONTAINER_NAMESPACE/$IMAGE_NAME:latest" \
          --registry-secret "$REGISTRY_SECRET_NAME" \
          --env LOGGING_LEVEL="$LOGGING_LEVEL" \
          --env AI_SERVICES_PROVIDER="$AI_SERVICES_PROVIDER" \
          --env DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
          --env WO_DEVELOPER_EDITION_SOURCE="orchestrate" \
          --env WO_INSTANCE="$WO_INSTANCE" \
          --env WO_API_KEY="$WO_API_KEY" \
          --env WATSONX_URL="$WATSONX_URL" \
          --env WATSONX_API_KEY="$WATSONX_API_KEY" \
          --env WATSONX_PROJECT_ID="$WATSONX_PROJECT_ID" \
          --env WATSONX_SPACE_ID="$WATSONX_SPACE_ID" \
          --env OPENAI_API_KEY="$OPENAI_API_KEY" \
          --env TAVILY_API_KEY="$TAVILY_API_KEY" \
          --min-scale 1 \
          --max-scale 1 \
          --visibility public \
          --port 8080 \
          --quiet; then
          log_error "Failed to create application"
          exit 1
      fi
  fi
}

wait_for_deployment() {
  # Wait for deployment
  log_info "7. Waiting for deployment to complete (timeout: ${MAX_DEPLOYMENT_WAIT_TIME}s)..."
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would wait for deployment completion"
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would simulate deployment wait for $DEPLOYMENT_CHECK_INTERVAL seconds"
    PUBLIC_URL="https://dry-run-example-url.example.com"
    return 0
  fi

  start_time=$(date +%s)
  while true; do
      current_time=$(date +%s)
      elapsed_time=$((current_time - start_time))

      if [[ $elapsed_time -ge $MAX_DEPLOYMENT_WAIT_TIME ]]; then
          log_error "Deployment timeout after $MAX_DEPLOYMENT_WAIT_TIME seconds"
          exit 1
      fi

      status=$(ibmcloud ce application get -n "$CE_PROJECT_NAME" -o json | jq -r '.status.conditions[] | select(.type == "Ready").status' 2>/dev/null || echo "Unknown")

      if [[ "$status" == "True" ]]; then
          log_info "Deployment completed successfully"
          break
      elif [[ "$status" == "False" ]]; then
          log_error "Deployment failed"
          exit 1
      else
          log_info "Still deploying... (elapsed: ${elapsed_time}s)"
          sleep "$DEPLOYMENT_CHECK_INTERVAL"
      fi
  done

  # Get application URL
  PUBLIC_URL=$(ibmcloud ce application get -n "$CE_PROJECT_NAME" -o json | jq -r '.status.url')
  if [[ -z "$PUBLIC_URL" || "$PUBLIC_URL" == "null" ]]; then
      log_error "Failed to get application URL"
      exit 1
  fi
  log_info "8. Deployment complete. Public URL: $PUBLIC_URL"
  # Update the api_url in tfsa_langgraph_external_agent.yaml with the new PUBLIC_URL
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would update api_url in tfsa_langgraph_external_agent.yaml to $PUBLIC_URL"
  else
    if command -v yq &>/dev/null; then
      yq -i ".api_url = \"$PUBLIC_URL/api/v1/chat/completions\"" "${SCRIPT_DIR}/agents/tfsa_langgraph_external_agent.yaml"
      log_info "Updated api_url in tfsa_langgraph_external_agent.yaml"
    else
      log_error "yq is required to update the YAML file, but it's not installed."
      exit 1
    fi
  fi
}

test_endpoints() {
  # Test endpoints
  log_info "9. Testing endpoints..."
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would test sync endpoint at https://dry-run-example-url.example.com"
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would test streaming endpoint at https://dry-run-example-url.example.com"
    return 0
  fi

  log_info "Testing sync endpoint:"
  if ! execute curl -s -X POST "${PUBLIC_URL}/api/v1/chat/completions" \
      -H "Content-Type: application/json" \
      -d '{
          "model": "meta-llama/llama-3-2-90b-vision-instruct",
          "messages": [{
              "role": "user",
              "content": "What are the annual dollar limits for each year of TSFA, including 2025?"
          }],
          "stream": false
      }'; then
      log_error "Test call failed"
  fi

  log_info -e "\nTesting streaming endpoint:"
  if ! execute curl -s -X POST "${PUBLIC_URL}/api/v1/chat/completions" \
      -H "Content-Type: application/json" \
      -d '{
          "model": "meta-llama/llama-3-2-90b-vision-instruct",
          "messages": [{
              "role": "user",
              "content": "What are the annual dollar limits for each year of TSFA, including 2025?"
          }],
          "stream": true
      }'; then
      log_error "Test call failed"
  fi
}

generate_agent_deployment_order() {
    log_info "Analyzing agent deployment order..."
    local agents_dir="${SCRIPT_DIR}/agents"
    if [[ ! -d "$agents_dir" ]]; then
        log_error "Agents directory not found at '$agents_dir'."
        exit 1
    fi

    local all_agent_files=()
    local base_agents=()
    local dependent_agents=()

    while IFS= read -r -d $'\0' file; do
        all_agent_files+=("$file")
    done < <(find "$agents_dir" -name "*.yaml" -print0)

    if [[ ${#all_agent_files[@]} -eq 0 ]]; then
        log_warn "No agent YAML files found in $agents_dir."
        return
    fi

    for agent_file in "${all_agent_files[@]}"; do
        if yq -e '.collaborators' "$agent_file" >/dev/null 2>&1; then
            dependent_agents+=("$agent_file")
        else
            base_agents+=("$agent_file")
        fi
    done

    AGENT_FILES_ORDERED=("${base_agents[@]}" "${dependent_agents[@]}")

    for ((i=${#AGENT_FILES_ORDERED[@]}-1; i>=0; i--)); do
        AGENT_FILES_REVERSED+=("${AGENT_FILES_ORDERED[i]}")
    done

    log_info "Agent deployment order determined:"
    for file in "${AGENT_FILES_ORDERED[@]}"; do
        log_info "  -> $(basename "$file")"
    done
}

setup_orchestrate() {
  log_info "10. Setting up and activating Orchestrate environment: '$ORCHESTRATE_ENV_NAME'..."

  if [[ -z "${PUBLIC_URL:-}" ]]; then
      if [[ "$DRY_RUN" == false ]]; then
          wait_for_deployment
      else
          echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would ensure PUBLIC_URL is set before setting up orchestrate."
          PUBLIC_URL="https://dry-run-example-url.example.com"
      fi
  fi

  if [[ "$DRY_RUN" == true ]]; then
      echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would check for orchestrate CLI"
      echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would simulate orchestrate environment setup"
      return 0
  fi

  # Ensure WO_INSTANCE is available, as this function depends on it.
  if [[ -z "${WO_INSTANCE:-}" ]]; then
      log_error "WO_INSTANCE is not set. Please ensure it is in your .env file."
      exit 1
  fi

  if command -v orchestrate &>/dev/null; then
      # Check if environment already exists
      env_exists=$(orchestrate env list | grep -q "$ORCHESTRATE_ENV_NAME" && echo "true" || echo "false")

      if [[ "$env_exists" == "true" ]]; then
          log_info "Environment '$ORCHESTRATE_ENV_NAME' already exists. Checking URL..."
          local env_line
          env_line=$(orchestrate env list | grep "$ORCHESTRATE_ENV_NAME")
          if [[ "$env_line" != *"$WO_INSTANCE"* ]]; then
              log_info "Environment '$ORCHESTRATE_ENV_NAME' URL does not match. Updating URL..."
              if ! execute orchestrate env add -n "$ORCHESTRATE_ENV_NAME" -u "$WO_INSTANCE" --type ibm_iam --activate; then
                  log_error "Failed to update environment '$ORCHESTRATE_ENV_NAME'."
                  exit 1
             fi
          else
             log_info "Environment '$ORCHESTRATE_ENV_NAME' already exists with correct URL. Activating it..."
             if ! execute orchestrate env activate "$ORCHESTRATE_ENV_NAME"; then
                log_error "Failed to activate environment '$ORCHESTRATE_ENV_NAME'."
                exit 1
             fi
          fi
      else
          log_info "Environment '$ORCHESTRATE_ENV_NAME' not found. Creating and activating it..."
          if ! execute orchestrate env add -n "$ORCHESTRATE_ENV_NAME" -u "$WO_INSTANCE" --type ibm_iam --activate; then
              log_error "Failed to create and activate environment '$ORCHESTRATE_ENV_NAME'."
              exit 1
          fi
      fi

      if orchestrate env list | grep -q "$ORCHESTRATE_ENV_NAME"; then
          import_agents
          deploy_agents
      else
          log_error "Environment $ORCHESTRATE_ENV_NAME not available after setup"
          exit 1
      fi

      orchestrate env list
  else
    log_info "Orchestrate CLI not found, skipping environment setup"
  fi
}

import_agents() {
    log_info "Importing agents in dependency order..."
    if [[ ${#AGENT_FILES_ORDERED[@]} -eq 0 ]]; then
        log_warn "No agent files found to import."
        return
    fi

    for agent_file in "${AGENT_FILES_ORDERED[@]}"; do
        if [[ -f "$agent_file" ]]; then
            log_info "Importing agent: $(basename "$agent_file")"
            if ! execute orchestrate agents import -f "$agent_file"; then
                log_warn "Failed to import agent from '$(basename "$agent_file")', continuing..."
            fi
        else
            log_warn "Agent file not found, skipping: $agent_file"
        fi
    done
    log_info "Agent import process complete."
}

deploy_agents() {
    log_info "Deploying agents in dependency order..."
    if [[ ${#AGENT_FILES_ORDERED[@]} -eq 0 ]]; then
        log_warn "No agent files found to deploy."
        return
    fi

    for agent_file in "${AGENT_FILES_ORDERED[@]}"; do
        if [[ -f "$agent_file" ]]; then
            local agent_name
            agent_name=$(yq -r '.name' "$agent_file")

            if [[ -n "$agent_name" ]]; then
                log_info "Deploying agent: $agent_name"
                if ! execute orchestrate agents deploy --name "$agent_name"; then
                    log_warn "Failed to deploy agent '$agent_name', continuing..."
                fi
            else
                log_warn "Could not determine name for agent in '$(basename "$agent_file")'. Skipping."
            fi
        else
            log_warn "Agent file not found, skipping: $agent_file"
        fi
    done
    log_info "Agent deployment process complete."
}

undeploy_agents() {
    log_info "Undeploying agents in reverse dependency order..."
    if [[ ${#AGENT_FILES_REVERSED[@]} -eq 0 ]]; then
        log_warn "No agent files found to undeploy."
        return
    fi

    for agent_file in "${AGENT_FILES_REVERSED[@]}"; do
        if [[ -f "$agent_file" ]]; then
            local agent_name
            agent_name=$(yq -r '.name' "$agent_file")

            if [[ -n "$agent_name" ]]; then
                log_info "Undeploying agent: $agent_name"
                if ! execute orchestrate agents undeploy --name "$agent_name"; then
                    log_warn "Failed to undeploy agent '$agent_name', continuing..."
                fi
            else
                log_warn "Could not determine name for agent in '$(basename "$agent_file")'. Skipping."
            fi
        else
            log_warn "Agent file not found, skipping: $agent_file"
        fi
    done
    log_info "Agent undeployment process complete."
}

remove_agents() {
    log_info "Removing agents..."
    if [[ ${#AGENT_FILES_REVERSED[@]} -eq 0 ]]; then
        log_warn "No agents to remove."
        return
    fi

    for agent_file in "${AGENT_FILES_REVERSED[@]}"; do
        if [[ -f "$agent_file" ]]; then
            local agent_name
            agent_name=$(yq -r '.name' "$agent_file")
            local agent_kind
            agent_kind=$(yq -r '.kind' "$agent_file")

            if [[ -n "$agent_name" && -n "$agent_kind" ]]; then
                log_info "Removing agent: '$agent_name' (kind: $agent_kind)"
                if ! execute orchestrate agents remove --name "$agent_name" --kind "$agent_kind" --force; then
                    log_warn "Could not remove agent '$agent_name'. It might not exist or an error occurred."
                fi
            else
                log_warn "Could not determine name and kind for agent in '$(basename "$agent_file")'. Skipping."
            fi
        else
            log_warn "Agent file not found, skipping: $agent_file"
        fi
    done
    log_info "Agent removal process complete."
}

cleanup_resources() {
  log_info "=== Cleaning up TFSA Agent Deployment Resources ==="

  if [[ "$DRY_RUN" == false ]]; then
    if ! ibmcloud login --apikey "$WATSONX_API_KEY" -r "$CE_REGION" --quiet; then
        log_error "Failed to login to IBM Cloud"
        exit 1
    fi
  else
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would authenticate to IBM Cloud"
  fi

  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would set target resource group"
    CE_RESOURCE_GROUP="dry-run-resource-group"
  else
    CE_RESOURCE_GROUP=$(ibmcloud resource groups --output json | jq -r '.[].name' | grep '^itz-' | head -1) || {
        log_error "Failed to parse code engine resource group"
        exit 1
    }
  fi

  if [[ -z "$CE_RESOURCE_GROUP" ]]; then
      log_warn "No resource group found, skipping resource group targeting"
  else
      if ! execute ibmcloud target -g "$CE_RESOURCE_GROUP"; then
          log_warn "Failed to set target resource group, continuing with cleanup"
      fi
  fi

  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would select Code Engine project"
    CE_PROJECT_ID="dry-run-project"
  else
    CE_PROJECT_ID=$(ibmcloud ce project list --output json | jq -r '.[0].name') || {
        log_warn "Failed to parse project list, skipping project selection"
    }
  fi

  if [[ -n "${CE_PROJECT_ID:-}" ]]; then
      if ! execute ibmcloud ce project select -n "$CE_PROJECT_ID"; then
          log_warn "Failed to select Code Engine project, continuing with cleanup"
      fi
  fi

  log_info "Deleting application: $CE_PROJECT_NAME"
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would delete application: $CE_PROJECT_NAME"
  else
    if ibmcloud ce application get --name "$CE_PROJECT_NAME" --output json >/dev/null 2>&1; then
      if ! execute ibmcloud ce application delete --name "$CE_PROJECT_NAME" --force --quiet; then
          log_warn "Failed to delete application: $CE_PROJECT_NAME"
      else
          log_info "Successfully deleted application: $CE_PROJECT_NAME"
      fi
    else
      log_info "Application $CE_PROJECT_NAME does not exist, skipping deletion"
    fi
  fi

  log_info "Deleting registry secret: $REGISTRY_SECRET_NAME"
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would delete registry secret: $REGISTRY_SECRET_NAME"
  else
    if ibmcloud ce registry get -n "$REGISTRY_SECRET_NAME" &>/dev/null; then
      if ! execute ibmcloud ce registry delete --name "$REGISTRY_SECRET_NAME" --force --quiet; then
          log_warn "Failed to delete registry secret: $REGISTRY_SECRET_NAME"
      else
          log_info "Successfully deleted registry secret: $REGISTRY_SECRET_NAME"
      fi
    else
      log_info "Registry secret $REGISTRY_SECRET_NAME does not exist, skipping deletion"
    fi
  fi

  get_container_namespace
  if [[ -n "${CONTAINER_NAMESPACE:-}" ]]; then
    log_info "Deleting container image: $CONTAINER_NAMESPACE/$IMAGE_NAME"
    if [[ "$DRY_RUN" == true ]]; then
      echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would delete container image: $CONTAINER_NAMESPACE/$IMAGE_NAME"
    else
      if ibmcloud cr image-list --restrict "$CONTAINER_NAMESPACE/$IMAGE_NAME" | grep -q "latest"; then
        if ! execute ibmcloud cr image-rm "$CONTAINER_NAMESPACE/$IMAGE_NAME:latest" --force --quiet; then
            log_warn "Failed to delete container image: $CONTAINER_NAMESPACE/$IMAGE_NAME:latest"
        else
            log_info "Successfully deleted container image: $CONTAINER_NAMESPACE/$IMAGE_NAME:latest"
        fi
      else
        log_info "Container image $CONTAINER_NAMESPACE/$IMAGE_NAME does not exist, skipping deletion"
      fi
    fi
  fi

  log_info "Cleaning up Orchestrate environment: $ORCHESTRATE_ENV_NAME"
  if [[ "$DRY_RUN" == true ]]; then
    echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would cleanup Orchestrate environment: $ORCHESTRATE_ENV_NAME"
  else
    if command -v orchestrate &>/dev/null; then
      if orchestrate env list | grep -q "$ORCHESTRATE_ENV_NAME"; then
        undeploy_agents
        remove_agents
        if ! execute orchestrate env remove -n "$ORCHESTRATE_ENV_NAME" --force; then
            log_warn "Failed to delete Orchestrate environment: $ORCHESTRATE_ENV_NAME"
        else
            log_info "Successfully deleted Orchestrate environment: $ORCHESTRATE_ENV_NAME"
        fi
      else
        log_info "Orchestrate environment $ORCHESTRATE_ENV_NAME does not exist, skipping deletion"
      fi
    else
      log_info "Orchestrate CLI not found, skipping environment cleanup"
    fi
  fi

  log_info "=== Cleanup Complete ==="
}

run_all() {
     log_info "=== Starting Full TFSA Agent Deployment to IBM Code Engine ==="
     authenticate_to_ibmcloud
     setup_resource_group
     select_code_engine_project
     create_registry_secret
     build_and_push_image
     deploy_application
     wait_for_deployment
     test_endpoints
     setup_orchestrate
     log_info "=== Full Deployment Complete ==="
 }

REMAINING_ARGS=()

show_help() {
    cat << EOF
Usage: $0 [OPTION] [FUNCTION]

Deploy the TFSA Agent application to IBM Code Engine.

OPTIONS:
    -h, --help                    Show this help message and exit
    --dry-run                     Simulate actions without executing them
    --timeout SECONDS             Override deployment timeout (default: 300 seconds)

FUNCTIONS:
    authenticate_to_ibmcloud      Authenticate to IBM Cloud
    setup_resource_group          Set up resource group
    select_code_engine_project    Select Code Engine project
    create_registry_secret        Create registry secret
    get_container_namespace       Retrieves the container registry namespace
    build_and_push_image          Build and push container image
    deploy_application            Deploy application to Code Engine
    wait_for_deployment           Wait for deployment to complete
    test_endpoints                Test deployed endpoints
    setup_orchestrate             Set up Orchestrate environment, import and deploy agents
    import_agents                 Import agents to Orchestrate
    deploy_agents                 Deploy agents to Orchestrate
    undeploy_agents               Undeploy agents from Orchestrate
    remove_agents                 Remove agents from Orchestrate
    cleanup_resources             Remove all deployed resources
    run_all                       Run all deployment steps (default)

If no function is specified, run_all is executed by default.

Examples:
    $0                            # Run complete deployment
    $0 --dry-run                  # Simulate complete deployment
    $0 --timeout 600              # Run deployment with 10-minute timeout
    $0 deploy_application         # Run only application deployment
    $0 --dry-run deploy_application  # Simulate application deployment
    $0 cleanup_resources          # Remove all deployed resources
    $0 --dry-run cleanup_resources   # Simulate cleanup process
    $0 -h                         # Show this help message
EOF
}

parse_args() {
    REMAINING_ARGS=() # Reset the array for each parse
    while [[ $# -gt 0 ]]; do
        case $1 in
            -h|--help)
                SHOW_HELP=true
                shift # Consume the option
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --timeout)
                if [[ -n "${2:-}" ]] && [[ "$2" =~ ^[0-9]+$ ]]; then
                    MAX_DEPLOYMENT_WAIT_TIME="$2"
                    shift 2
                else
                    log_error "Timeout value must be a positive integer"
                    exit 1
                fi
                ;;
            *)
                REMAINING_ARGS+=("$1") # Add to remaining args
                shift # Consume the argument
                ;;
        esac
    done
}

main() {
    check_requirements
    generate_agent_deployment_order

    parse_args "$@"

    if [[ "$SHOW_HELP" == true ]]; then
        show_help
        exit 0
    fi

    if [[ ${#REMAINING_ARGS[@]} -gt 0 ]]; then
        set -- "${REMAINING_ARGS[@]}"
    else
        set --
    fi

    if [[ "$DRY_RUN" == true ]]; then
        log_info "=== ${COLOR_BOLD}DRY RUN MODE ENABLED${COLOR_RESET} ==="
        log_info "No actual changes will be made to your environment"
    fi

    log_info "Deployment timeout set to: ${MAX_DEPLOYMENT_WAIT_TIME} seconds"

    if [[ $# -eq 0 ]]; then
        run_all
    else
        if declare -f "$1" > /dev/null; then
            log_info "Running specified function: $1"
            "$@"
        else
            log_error "Function '$1' not found. See --help for available functions."
            exit 1
        fi
    fi
}

main "$@"
