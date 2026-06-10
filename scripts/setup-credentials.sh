#!/usr/bin/env bash
set -euo pipefail

# Setup script for homepilot-agent credentials and Docker secrets.
# Generates secret files in ./secrets/ and creates .env from .env.example
# if it doesn't already exist.
#
# Usage: scripts/setup-credentials.sh [--non-interactive]
#   --non-interactive  Generate all secrets without prompting

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SECRETS_DIR="${REPO_ROOT}/secrets"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"

INTERACTIVE=true
if [[ "${1:-}" == "--non-interactive" ]]; then
    INTERACTIVE=false
fi

generate_hex() {
    local length="${1:-32}"
    openssl rand -hex "$length"
}

write_secret() {
    local name="$1"
    local value="$2"
    local dest="${SECRETS_DIR}/${name}.txt"
    echo -n "${value}" > "${dest}"
    chmod 600 "${dest}"
    echo "  Written ${dest}"
}

prompt_or_generate() {
    local var_name="$1"
    local description="$2"
    local current="${3:-}"

    if [[ -n "${current}" && "${current}" != \<*\> ]]; then
        echo "  ${var_name}: already set in .env, keeping existing value"
        return 0
    fi

    if ${INTERACTIVE}; then
        echo ""
        echo "${description}"
        read -rp "  Enter ${var_name} (leave blank to auto-generate): " user_input
        if [[ -n "${user_input}" ]]; then
            echo "${user_input}"
        else
            generate_hex
        fi
    else
        generate_hex
    fi
}

mkdir -p "${SECRETS_DIR}"

# ── Step 1: Create .env from .env.example if missing ──────────────────────
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "Creating .env from .env.example..."
    cp "${ENV_EXAMPLE}" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    echo "  Created ${ENV_FILE}"
    echo "  Edit it to fill in non-secret values (URLs, user IDs, etc.)"
else
    echo ".env already exists — preserving existing values."
fi

# ── Step 2: Load existing .env values ──────────────────────────────────────
declare -A env_values
if [[ -f "${ENV_FILE}" ]]; then
    while IFS='=' read -r key value; do
        [[ -z "${key}" || "${key}" == \#* ]] && continue
        value="${value%\"}" value="${value#\"}"
        env_values["${key}"]="${value}"
    done < "${ENV_FILE}"
fi

# ── Step 3: Generate Docker secret files ────────────────────────────────────
echo ""
echo "=== Generating Docker secrets in ./secrets/ ==="

# N8N_ENCRYPTION_KEY
n8n_key=""
if [[ -f "${SECRETS_DIR}/n8n_encryption_key.txt" ]]; then
    n8n_key="$(cat "${SECRETS_DIR}/n8n_encryption_key.txt")"
    echo "  n8n_encryption_key.txt already exists, keeping"
elif [[ -n "${env_values[N8N_ENCRYPTION_KEY]:-}" && "${env_values[N8N_ENCRYPTION_KEY]}" != \<* ]]; then
    n8n_key="${env_values[N8N_ENCRYPTION_KEY]}"
else
    n8n_key="$(prompt_or_generate N8N_ENCRYPTION_KEY "n8n encryption key (required for credential storage)")"
fi
[[ -n "${n8n_key}" ]] && write_secret n8n_encryption_key "${n8n_key}"

# HP_MCP_TOKEN
hp_token=""
if [[ -f "${SECRETS_DIR}/hp_mcp_token.txt" ]]; then
    echo "  hp_mcp_token.txt already exists, keeping"
elif [[ -n "${env_values[HP_MCP_TOKEN]:-}" && "${env_values[HP_MCP_TOKEN]}" != \<* ]]; then
    hp_token="${env_values[HP_MCP_TOKEN]}"
    write_secret hp_mcp_token "${hp_token}"
else
    hp_token="$(prompt_or_generate HP_MCP_TOKEN "HomePilot MCP read-only token")"
    write_secret hp_mcp_token "${hp_token}"
fi

# HP_MCP_TOKEN_RW
hp_token_rw=""
if [[ -f "${SECRETS_DIR}/hp_mcp_token_rw.txt" ]]; then
    echo "  hp_mcp_token_rw.txt already exists, keeping"
elif [[ -n "${env_values[HP_MCP_TOKEN_RW]:-}" && "${env_values[HP_MCP_TOKEN_RW]}" != \<* ]]; then
    hp_token_rw="${env_values[HP_MCP_TOKEN_RW]}"
    write_secret hp_mcp_token_rw "${hp_token_rw}"
else
    hp_token_rw="$(prompt_or_generate HP_MCP_TOKEN_RW "HomePilot MCP read-write token")"
    write_secret hp_mcp_token_rw "${hp_token_rw}"
fi

# SEARXNG_SECRET_KEY
searxng_key=""
if [[ -f "${SECRETS_DIR}/searxng_secret_key.txt" ]]; then
    echo "  searxng_secret_key.txt already exists, keeping"
else
    searxng_key="$(prompt_or_generate SEARXNG_SECRET_KEY "SearXNG secret key")"
    write_secret searxng_secret_key "${searxng_key}"
fi

# Radicale htpasswd
if [[ -f "${SECRETS_DIR}/radicale_htpasswd.txt" ]]; then
    echo "  radicale_htpasswd.txt already exists, keeping"
else
    radicale_user="${env_values[RADICALE_USER]:-admin}"
    if ${INTERACTIVE}; then
        echo ""
        echo "Radicale CalDAV credentials (htpasswd)"
        read -rp "  Username [${radicale_user}]: " input_user
        radicale_user="${input_user:-${radicale_user}}"
        read -rsp "  Password: " radicale_pass
        echo ""
    else
        radicale_pass="$(generate_hex 16)"
    fi
    hashed="$(docker run --rm tomsquest/docker-radicale:3.3.2.0 htpasswd -nbB "${radicale_user}" "${radicale_pass}" 2>/dev/null || echo "${radicale_user}:{CRYPT}$(openssl passwd -6 "${radicale_pass}")" )"
    write_secret radicale_htpasswd "${hashed}"
fi

# ── Step 4: Summary ─────────────────────────────────────────────────────────
echo ""
echo "=== Done ==="
echo ""
echo "Secret files created in ./secrets/ (gitignored)."
echo "  These are mounted as Docker secrets and read via _FILE env vars."
echo ""
echo "Next steps:"
echo "  1. Review and edit .env for non-secret config (URLs, user IDs)"
echo "  2. docker compose up -d"
echo "  3. Run scripts/import-workflows.sh to load n8n workflows"