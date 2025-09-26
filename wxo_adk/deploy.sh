#!/usr/bin/env bash
set -euo pipefail
set +x

# --- Configuration ---
# Name for the orchestrate environment
readonly ORCHESTRATE_ENV_NAME="watsonx-challenge"

# --- Script Setup ---
# Get the directory where the script is located to make it runnable from anywhere
readonly SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Global variables
DRY_RUN=false

# ANSI color codes
readonly COLOR_RESET='\033[0m'
readonly COLOR_RED='\033[0;31m'
readonly COLOR_GREEN='\033[0;32m'
readonly COLOR_YELLOW='\033[1;33m'
readonly COLOR_CYAN='\033[0;36m'
readonly COLOR_BOLD='\033[1m'

# --- Logging and Execution Functions ---
log_info() {
    echo -e "${COLOR_GREEN}[INFO]${COLOR_RESET} $*"
}

log_error() {
    echo -e "${COLOR_RED}[ERROR]${COLOR_RESET} $*" >&2
}

log_warn() {
    echo -e "${COLOR_YELLOW}[WARN]${COLOR_RESET} $*"
}

execute() {
    if [[ "$DRY_RUN" == true ]]; then
        echo -e "${COLOR_CYAN}[DRY-RUN]${COLOR_RESET} Would execute: $*"
        return 0
    else
        "$@"
    fi
}

# --- Core Logic Functions ---
check_requirements() {
    log_info "Checking requirements..."
    if ! command -v orchestrate &>/dev/null; then
        log_error "'orchestrate' CLI is required but not installed. Please install the watsonx-orchestrate-sdk."
        exit 1
    fi

    # Load environment variables from .env file in the same directory as the script
    local ENV_FILE="${SCRIPT_DIR}/../.env"
    if [[ -f "$ENV_FILE" ]]; then
        log_info "Loading environment variables from $ENV_FILE"
        # shellcheck source=.env
        source "$ENV_FILE"
    else
        log_error "$ENV_FILE file not found. Please create one with the required WO_INSTANCE variable."
        exit 1
    fi

    if [[ -z "${WO_INSTANCE:-}" ]]; then
        log_error "WO_INSTANCE is not set in the .env file. This is required to create the environment."
        exit 1
    fi
}

setup_environment() {
    log_info "Setting up Orchestrate environment: '$ORCHESTRATE_ENV_NAME'..."

    # Ensure WO_INSTANCE is available, as this function depends on it.
    if [[ -z "${WO_INSTANCE:-}" ]]; then
        log_error "WO_INSTANCE is not set. Please ensure it is in your .env file."
        exit 1
    fi

    # Check if environment already exists
    if [[ "$DRY_RUN" == true ]]; then
        log_info "[DRY-RUN] Would check if environment '$ORCHESTRATE_ENV_NAME' exists."
        # Assume it doesn't exist for dry-run to show creation logic
        env_exists=false
    else
        env_exists=$(orchestrate env list | grep -q "$ORCHESTRATE_ENV_NAME" && echo "true" || echo "false")
    fi

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
    log_info "Environment setup complete. Current environments:"
    execute orchestrate env list
}

import_tools() {
    log_info "Importing tools..."
    local tools_dir="${SCRIPT_DIR}/tools"
    if [[ ! -d "$tools_dir" ]]; then
        log_error "Tools directory not found at '$tools_dir'."
        exit 1
    fi

    if ! execute orchestrate tools import -k python -r "${tools_dir}/requirements.txt" -f "${tools_dir}/tools.py"; then
        log_error "Failed to import tools from '${tools_dir}/tools.py'."
        exit 1
    fi
    log_info "Tools imported successfully."
}

import_agents() {
    log_info "Importing agents..."
    local agents_dir="${SCRIPT_DIR}/agents"
    if [[ ! -d "$agents_dir" ]]; then
        log_error "Agents directory not found at '$agents_dir'."
        exit 1
    fi

    local agents_to_import=(
        "tfsa_policy_agent.yaml"
        "tfsa_calculation_agent.yaml"
        "tfsa_transaction_agent.yaml"
        "tfsa_orchestrator_agent.yaml"
    )

    for agent_file in "${agents_to_import[@]}"; do
        local agent_path="${agents_dir}/${agent_file}"
        if [[ -f "$agent_path" ]]; then
            log_info "Importing agent: $agent_file"
            if ! execute orchestrate agents import -f "$agent_path"; then
                log_warn "Failed to import agent from '$agent_path', continuing..."
            fi
        else
            log_warn "Agent file not found, skipping: $agent_path"
        fi
    done
    log_info "Agent import process complete."
}

deploy_agents() {
    log_info "Deploying agents..."
    local agents_dir="${SCRIPT_DIR}/agents"
    if [[ ! -d "$agents_dir" ]]; then
        log_error "Agents directory not found at '$agents_dir'."
        exit 1
    fi

    # Agents are deployed in order of dependency.
    # Collaborator agents first, then the orchestrator agent.
    local agents_to_deploy=(
        "tfsa_policy_agent.yaml"
        "tfsa_calculation_agent.yaml"
        "tfsa_transaction_agent.yaml"
        "tfsa_orchestrator_agent.yaml"
    )

    for agent_file in "${agents_to_deploy[@]}"; do
        local agent_path="${agents_dir}/${agent_file}"
        if [[ -f "$agent_path" ]]; then
            local agent_name
            agent_name=$(grep -E '^\s*name:' "$agent_path" | sed -e 's/^\s*name:\s*//' -e 's/"//g' -e "s/'//g" | tr -d '[:space:]')

            if [[ -n "$agent_name" ]]; then
                log_info "Deploying agent: $agent_name"
                if ! execute orchestrate agents deploy --name "$agent_name"; then
                    log_warn "Failed to deploy agent '$agent_name', continuing..."
                fi
            else
                log_warn "Could not determine name for agent in '$agent_file'. Skipping."
            fi
        else
            log_warn "Agent file not found, skipping: $agent_path"
        fi
    done
    log_info "Agent deployment process complete."
}

undeploy_agents() {
    log_info "Undeploying agents..."
    local agents_dir="${SCRIPT_DIR}/agents"
    if [[ ! -d "$agents_dir" ]]; then
        log_error "Agents directory not found at '$agents_dir'."
        exit 1
    fi

    # Agents are undeployed in reverse order of dependency.
    # Orchestrator agent first, then the collaborator agents.
    local agents_to_undeploy=(
        "tfsa_orchestrator_agent.yaml"
        "tfsa_transaction_agent.yaml"
        "tfsa_calculation_agent.yaml"
        "tfsa_policy_agent.yaml"
    )

    for agent_file in "${agents_to_undeploy[@]}"; do
        local agent_path="${agents_dir}/${agent_file}"
        if [[ -f "$agent_path" ]]; then
            local agent_name
            agent_name=$(grep -E '^\s*name:' "$agent_path" | sed -e 's/^\s*name:\s*//' -e 's/"//g' -e "s/'//g" | tr -d '[:space:]')

            if [[ -n "$agent_name" ]]; then
                log_info "Undeploying agent: $agent_name"
                if ! execute orchestrate agents undeploy --name "$agent_name"; then
                    log_warn "Failed to undeploy agent '$agent_name', continuing..."
                fi
            else
                log_warn "Could not determine name for agent in '$agent_file'. Skipping."
            fi
        else
            log_warn "Agent file not found, skipping: $agent_path"
        fi
    done
    log_info "Agent undeployment process complete."
}

cleanup_resources() {
    log_info "=== Cleaning up Orchestrate Resources ==="
    # Ensure the environment is active to issue 'agents remove' commands
    setup_environment

    undeploy_agents

    local agents_dir="${SCRIPT_DIR}/agents"
    local agents_to_remove=(
        "tfsa_orchestrator_agent.yaml"
        "tfsa_transaction_agent.yaml"
        "tfsa_calculation_agent.yaml"
        "tfsa_policy_agent.yaml"
    )

    log_info "Removing agents..."
    for agent_file in "${agents_to_remove[@]}"; do
        local agent_path="${agents_dir}/${agent_file}"
        if [[ -f "$agent_path" ]]; then
            # Extract name and kind from the YAML file using grep and sed for portability
            local agent_name
            agent_name=$(grep -E '^\s*name:' "$agent_path" | sed -e 's/^\s*name:\s*//' -e 's/"//g' -e "s/'//g" | tr -d '[:space:]')
            local agent_kind
            agent_kind=$(grep -E '^\s*kind:' "$agent_path" | sed -e 's/^\s*kind:\s*//' -e 's/"//g' -e "s/'//g" | tr -d '[:space:]')

            if [[ -n "$agent_name" && -n "$agent_kind" ]]; then
                log_info "Removing agent: '$agent_name' (kind: $agent_kind)"
                if ! execute orchestrate agents remove --name "$agent_name" --kind "$agent_kind" --force; then
                    log_warn "Could not remove agent '$agent_name'. It might not exist or an error occurred."
                fi
            else
                log_warn "Could not determine name and kind for agent in '$agent_file'. Skipping."
            fi
        else
            log_warn "Agent file not found, skipping: $agent_path"
        fi
    done

    log_info "Skipping tool removal. Please remove tools manually if needed (e.g., 'orchestrate tools remove -n search_cra_tfsa_policy')."
    log_info "The environment '$ORCHESTRATE_ENV_NAME' was NOT removed. You can remove it manually with:"
    log_info "orchestrate env remove -n '$ORCHESTRATE_ENV_NAME' --force"
    log_info "=== Cleanup Complete ==="
}

run_all() {
    log_info "=== Starting Orchestrate Setup ==="
    setup_environment
    import_tools
    import_agents
    deploy_agents
    log_info "=== Orchestrate Setup Complete ==="
    log_info "Agents and tools have been imported and deployed. You can now use them in the watsonx Orchestrate UI."
}

# --- Argument Parsing and Main Execution ---
show_help() {
    cat << EOF
Usage: $0 [OPTION] [FUNCTION]

Sets up the watsonx.Orchestrate environment by creating/activating the environment
and importing and deploying the necessary tools and agents.

OPTIONS:
    -h, --help          Show this help message and exit
    --dry-run           Simulate actions without executing them

FUNCTIONS:
    run_all             (default) Runs all setup steps: setup_environment, import_tools, import_agents, deploy_agents.
    setup_environment   Creates or activates the Orchestrate environment.
    import_tools        Imports tools from the 'tools' directory.
    import_agents       Imports agents from the 'agents' directory.
    deploy_agents       Deploys imported agents.
    undeploy_agents     Undeploys agents.
    cleanup_resources   Undeploys and removes imported agents.

If no function is specified, 'run_all' is executed.
EOF
}

main() {
    local func_to_run="run_all"

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            -h|--help)
                show_help
                exit 0
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            *)
                # Assume it's a function name
                if declare -f "$1" > /dev/null; then
                    func_to_run="$1"
                    shift
                else
                    log_error "Unknown option or function: $1"
                    show_help
                    exit 1
                fi
                ;;
        esac
    done

    check_requirements

    if [[ "$DRY_RUN" == true ]]; then
        log_info "=== ${COLOR_BOLD}DRY RUN MODE ENABLED${COLOR_RESET} ==="
        log_info "No actual changes will be made to your environment."
    fi

    log_info "Executing function: '$func_to_run'"
    "$func_to_run"
}

main "$@"
