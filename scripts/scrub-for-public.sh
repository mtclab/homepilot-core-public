#!/usr/bin/env bash
# scrub-for-public.sh — Remove sensitive content from repo export before syncing to public repo.
# Usage: ./scrub-for-public.sh <repo-dir> <repo-type>
#   repo-type: homepilot-v2 | proxmox-mcp | homepilot-agent
#
# This script operates ON THE EXPORTED COPY (not the private repo).
# It removes files, replaces sensitive strings, and cleans up git artifacts.
#
# IMPORTANT: Always run validate-scrub.sh AFTER this script to catch regressions.

set -euo pipefail

REPO_DIR="$1"
REPO_TYPE="$2"

if [[ -z "$REPO_DIR" || -z "$REPO_TYPE" ]]; then
    echo "Usage: $0 <repo-dir> <repo-type>"
    exit 1
fi

cd "$REPO_DIR"

echo "=== Scrubbing $REPO_TYPE in $(pwd) ==="

# ─── FILE TYPES TO SCRUB ───
# Covers all text-based file types; skips binary, node_modules, .venv, .git
SCRUB_FIND='find . -type f \( -name "*.md" -o -name "*.yml" -o -name "*.yaml" -o -name "*.py" -o -name "*.toml" -o -name "*.json" -o -name "*.txt" -o -name "*.sh" -o -name "*.cfg" -o -name "*.ini" -o -name "*.env" -o -name "*.env.*" -o -name "*.cnf" -o -name "*.conf" -o -name "Dockerfile*" -o -name "docker-compose*" -o -name "*.svelte" -o -name "*.ts" -o -name "*.js" -o -name "*.css" -o -name "*.html" -o -name "*.lock" -o -name "*.njk" \) ! -path "./.git/*" ! -path "./node_modules/*" ! -path "./.venv/*" ! -path "*/node_modules/*" ! -path "*/.venv/*"'

# ─── COMMON SCRUBBING (all repos) ───

# Remove internal ops files
rm -f AGENTS.md
rm -rf .opencode/

# Remove git history (fresh export, no author emails)
rm -rf .git/

# ─── COMMON STRING REPLACEMENTS (all repos) ───
# These apply to every repo regardless of type.

# Private repo URLs → public repo URLs
eval $SCRUB_FIND -exec sed -i \
    -e 's|github\.com/mtclab/homepilot-v2\([^-]\)|github.com/mtclab/homepilot-core-public\1|g' \
    -e 's|github\.com/mtclab/homepilot-v2$|github.com/mtclab/homepilot-core-public|g' \
    -e 's|ghcr\.io/mtclab/homepilot-v2|ghcr.io/mtclab/homepilot-core-public|g' \
    -e 's|github\.com/mtclab/proxmox-mcp\([^-]\)|github.com/mtclab/proxmox-mcp-public\1|g' \
    -e 's|github\.com/mtclab/proxmox-mcp$|github.com/mtclab/proxmox-mcp-public|g' \
    -e 's|github\.com/mtclab/homepilot-agent\([^-]\)|github.com/mtclab/homepilot-agent-public\1|g' \
    -e 's|github\.com/mtclab/homepilot-agent$|github.com/mtclab/homepilot-agent-public|g' \
    -e 's|github\.com/mtclab/homelab-platform|github.com/mtclab/homepilot-core-public|g' \
    {} +

# PVE token IDs / usernames (all repos)
eval $SCRUB_FIND -exec sed -i \
    -e 's|admin@pam!tokenid|admin@pam!tokenid|g' \
    -e 's|admin@pam!tokenid|admin@pam!tokenid|g' \
    -e 's|admin@pam|admin@pam|g' \
    -e 's|monitor@pam!monitoring|monitor@pam!monitoring|g' \
    -e 's|monitor@pam!monitoring|monitor@pam!monitoring|g' \
    -e 's|monitor@pam|monitor@pam|g' \
    {} +

# Real hostnames (all repos)
eval $SCRUB_FIND -exec sed -i \
    -e 's|your-server\.local|homepilot.example.com|g' \
    -e 's|hp\.local|homepilot.example.com|g' \
    -e 's|zabbix\.hp\.local|zabbix.example.com|g' \
    -e 's|zabbix\.homepilot\.local|zabbix.example.com|g' \
    -e 's|homepilot\.local|homepilot.example.com|g' \
    -e 's|pve\.example\.jotain|pve.example.com|g' \
    -e 's|pve\.lan|pve.example.local|g' \
    -e 's|media-lxc|media-lxc|g' \
    {} +

# Timezone leak (all repos)
eval $SCRUB_FIND -exec sed -i 's|Etc/UTC|Etc/UTC|g' {} +

# Local dev paths (all repos)
eval $SCRUB_FIND -exec sed -i 's|$HOME/|$HOME/|g' {} +

# Internal bot usernames (all repos)
eval $SCRUB_FIND -exec sed -i 's|bot-username|bot-username|g' {} +

# Internal Matrix room alias (all repos)
eval $SCRUB_FIND -exec sed -i 's|CHANGE_ME_matrix_room_alias|CHANGE_ME_matrix_room_alias|g' {} +

# ─── REPO-SPECIFIC SCRUBBING ───

case "$REPO_TYPE" in
    homepilot-v2)
        # Delete files with too much real infra data
        rm -f docs/archive/PLAN_V2.md
        rm -f docs/TEST_REPORT_v2.1.1.md
        rm -f docs/archive/VALIDATION.md
        rm -f docs/archive/ARCHITECTURE_CRITIQUE.md

        # Fix PLAN_V2.md references in remaining docs
        eval $SCRUB_FIND -exec sed -i 's|`ARCHITECTURE.md` (the architecture doc).|`ARCHITECTURE.md` (the architecture doc).|g' {} +

        # Scrub specific IP patterns (only in docs, not tests or source validation code)
        find docs -type f \( -name '*.md' -o -name '*.yml' -o -name '*.yaml' \) -exec sed -i \
            -e 's|10\.0\.10\.42|10\.0\.0\.1|g' \
            -e 's|10\.0\.10\.1|10\.0\.0\.1|g' \
            -e 's|10\.0\.0\.5|10\.0\.0\.2|g' \
            -e 's|192\.168\.100\.2|10\.0\.0\.1|g' \
            {} +

        # Scrub vault secret name patterns
        find . -type f -name '*.py' ! -path './.venv/*' ! -path '*/node_modules/*' \
            -exec sed -i 's|pve-pve1|pve-node1|g' {} +

        # Scrub ADR-003 Matrix channel name and opencode paths
        if [[ -f 'docs/adr/ADR-003-matrix-agent-bridge.md' ]]; then
            sed -i 's|#homepilot-agents|#public-monitoring|g' docs/adr/ADR-003-matrix-agent-bridge.md
            sed -i 's|~/.config/opencode/handoffs/|$HOME/.config/homepilot/handoffs/|g' docs/adr/ADR-003-matrix-agent-bridge.md
        fi

        # Scrub test mock PVE token to be clearly fake
        if [[ -f 'tests/test_cli_inventory.py' ]]; then
            sed -i 's|user@pve!token=secret|test-user@pve!test-token=test-secret|g' tests/test_cli_inventory.py
        fi

        # Fix test data that global hostname scrub broke (media-lxc was changed to media-lxc globally,
        # but the test asserts on "jellyfin" which no longer matches)
        if [[ -f 'tests/test_inventory_service.py' ]]; then
            sed -i 's|hostname="media-lxc"|hostname="media-lxc"|g' tests/test_inventory_service.py
        fi
        ;;

    proxmox-mcp)
        # Scrub local dev paths with opencode references
        find . -type f -name '*.py' ! -path './.git/*' \
            -exec sed -i 's|$HOME/.config/opencode/secrets/proxmox\.json|$HOME/.config/proxmox-mcp/secrets.json|g' {} +

        # Fix .gitignore if ruff_cache and AGENTS.md got merged
        if [[ -f '.gitignore' ]]; then
            sed -i 's|ruff_cacheAGENTS\.md|.ruff_cache/\nAGENTS.md|' .gitignore
        fi
        ;;

    homepilot-agent)
        # CRITICAL: Scrub real PVE API token secret
        eval $SCRUB_FIND -exec sed -i 's|e70af89f-1663-4f9e-af3c-be33ef1fa93f|CHANGE_ME_pve_token_secret|g' {} +

        # Scrub CI hardcoded IPs (use RFC 5737 TEST-NET)
        find . -type f -name '*.yml' -path './.github/*' \
            -exec sed -i 's|10\.0\.0\.1|192\.0\.2\.1|g' {} +

        # Scrub Caddy/openssl config hostnames
        if [[ -f 'monitoring/caddy/certs/openssl.cnf' ]]; then
            sed -i 's|CN = hp\.local|CN = homepilot.example.com|g' monitoring/caddy/certs/openssl.cnf
            sed -i 's|DNS\.1 = hp\.local|DNS.1 = homepilot.example.com|g' monitoring/caddy/certs/openssl.cnf
            sed -i 's|DNS\.2 = zabbix\.hp\.local|DNS.2 = zabbix.example.com|g' monitoring/caddy/certs/openssl.cnf
            sed -i 's|DNS\.3 = n8n\.hp\.local|DNS.3 = n8n.example.com|g' monitoring/caddy/certs/openssl.cnf
            sed -i 's|IP\.1 = your-server\.local|IP.1 = 192.0.2.1|g' monitoring/caddy/certs/openssl.cnf
        fi

        # Scrub LEARNINGS.md specifically for PVE cert CN leak
        find . -type f -name '*.md' -exec sed -i 's|pve\.example\.jotain|pve.example.com|g' {} +
        ;;
esac

echo "=== Scrubbing $REPO_TYPE complete ==="