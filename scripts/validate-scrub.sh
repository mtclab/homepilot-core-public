#!/usr/bin/env bash
# validate-scrub.sh — Verify no sensitive content remains in scrubbed exports.
# Returns exit 0 if clean, exit 1 if sensitive content found.
# Run AFTER scrub-for-public.sh.
set -euo pipefail

DIR="$1"
NAME="$2"

if [[ -z "$DIR" || -z "$NAME" ]]; then
    echo "Usage: $0 <dir> <name>"
    exit 1
fi

cd "$DIR"

FAILURES=0

echo "=== Validating scrub of $NAME in $(pwd) ==="

# ─── Helper: search all text file types, exclude binary/vendor dirs ───
grep_sensitive() {
    local pattern="$1"
    grep -rE "$pattern" \
        --include='*.md' --include='*.yml' --include='*.yaml' --include='*.py' \
        --include='*.toml' --include='*.json' --include='*.sh' --include='*.txt' \
        --include='*.env' --include='*.env.*' --include='*.cfg' --include='*.ini' \
        --include='*.cnf' --include='*.conf' --include='*.svelte' --include='*.ts' \
        --include='*.js' --include='*.css' --include='*.html' --include='*.lock' \
        --include='Dockerfile*' --include='docker-compose*' \
        . 2>/dev/null | grep -v node_modules | grep -v '.venv/' | grep -v '.git/' || true
}

# ─── 1. CRITICAL: Real secrets/tokens ───
echo ""
echo "--- CRITICAL: Real secrets/tokens ---"
PATTERNS_CRITICAL=(
    "e70af89f-1663-4f9e-af3c-be33ef1fa93f"
)
for pat in "${PATTERNS_CRITICAL[@]}"; do
    matches=$(grep_sensitive "$pat")
    if [[ -n "$matches" ]]; then
        echo "FAIL: Real secret found: $pat"
        echo "$matches"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $pat"
    fi
done

# ─── 2. HIGH: Private repo URLs ───
echo ""
echo "--- HIGH: Private repo URLs ---"
PATTERNS_URLS=(
    "github\.com/mtclab/homepilot-v2[^-]"
    "github\.com/mtclab/proxmox-mcp[^-]"
    "github\.com/mtclab/homepilot-agent[^-]"
    "github\.com/mtclab/homelab-platform"
    "ghcr\.io/mtclab/homepilot-v2"
    "ghcr\.io/mtclab/proxmox-mcp"
    "ghcr\.io/mtclab/homepilot-agent"
)
for pat in "${PATTERNS_URLS[@]}"; do
    matches=$(grep_sensitive "$pat")
    if [[ -n "$matches" ]]; then
        echo "FAIL: Private URL found: $pat"
        echo "$matches"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $pat"
    fi
done

# ─── 3. HIGH: Real PVE token IDs ───
echo ""
echo "--- HIGH: Real PVE token IDs/usernames ---"
PATTERNS_PVE=(
    "admin@pam"
    "monitor@pam"
)
for pat in "${PATTERNS_PVE[@]}"; do
    matches=$(grep_sensitive "$pat")
    if [[ -n "$matches" ]]; then
        echo "FAIL: Real PVE token found: $pat"
        echo "$matches"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $pat"
    fi
done

# ─── 4. HIGH: Real internal IPs ───
echo ""
echo "--- HIGH: Real internal IPs ---"
PATTERNS_IPS=(
    "192\.168\.100\.2"
    "10\.0\.10\.42"
    "10\.0\.10\.1"
    "10\.96\.16\.18"
    "10\.96\.16\.19"
)
for pat in "${PATTERNS_IPS[@]}"; do
    matches=$(grep_sensitive "$pat")
    # Filter out test fixtures
    filtered=$(echo "$matches" | grep -v 'test_' | grep -v 'conftest' | grep -v '# mock' || true)
    if [[ -n "$filtered" ]]; then
        echo "FAIL: Real IP found: $pat"
        echo "$filtered"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $pat"
    fi
done

# ─── 5. MEDIUM: Real hostnames ───
echo ""
echo "--- MEDIUM: Real hostnames ---"
PATTERNS_HOSTS=(
    "your-server\.local"
    "hp\.local"
    "zabbix\.hp\.local"
    "homepilot\.local"
    "zabbix\.homepilot\.local"
    "pve\.example\.jotain"
    "pve\.lan"
)
for pat in "${PATTERNS_HOSTS[@]}"; do
    matches=$(grep_sensitive "$pat")
    if [[ -n "$matches" ]]; then
        echo "FAIL: Real hostname found: $pat"
        echo "$matches"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $pat"
    fi
done

# ─── 6. MEDIUM: Internal bot usernames ───
echo ""
echo "--- MEDIUM: Internal bot usernames ---"
PATTERNS_BOTS=("bot-username")
for pat in "${PATTERNS_BOTS[@]}"; do
    matches=$(grep_sensitive "$pat")
    if [[ -n "$matches" ]]; then
        echo "FAIL: Internal bot username found: $pat"
        echo "$matches"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $pat"
    fi
done

# ─── 7. MEDIUM: Internal Matrix room alias ───
echo ""
echo "--- MEDIUM: Internal Matrix room alias ---"
matches=$(grep_sensitive "CHANGE_ME_matrix_room_alias")
if [[ -n "$matches" ]]; then
    echo "FAIL: Internal Matrix room alias found"
    echo "$matches"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: CHANGE_ME_matrix_room_alias"
fi

# ─── 8. MEDIUM: Timezone leak ───
echo ""
echo "--- MEDIUM: Timezone leak ---"
matches=$(grep_sensitive "Etc/UTC")
if [[ -n "$matches" ]]; then
    echo "FAIL: Etc/UTC found (operator timezone/location)"
    echo "$matches"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: Etc/UTC"
fi

# ─── 9. MEDIUM: Matrix server/room references ───
echo ""
echo "--- MEDIUM: Matrix server/room references ---"
PATTERNS_MATRIX=(
    "matrix\.mtcchat\.com"
    "!9PnzYFd"
)
for pat in "${PATTERNS_MATRIX[@]}"; do
    matches=$(grep_sensitive "$pat")
    if [[ -n "$matches" ]]; then
        echo "FAIL: Matrix reference found: $pat"
        echo "$matches"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $pat"
    fi
done

# ─── 10. MEDIUM: Internal usernames/emails ───
echo ""
echo "--- MEDIUM: Internal usernames/emails ---"
PATTERNS_USERS=(
    "ollikurki"
    "bilvi"
    "kurki\.olli"
    "@outlook\.com"
)
for pat in "${PATTERNS_USERS[@]}"; do
    matches=$(grep_sensitive "$pat")
    if [[ -n "$matches" ]]; then
        echo "FAIL: Internal username/email found: $pat"
        echo "$matches"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $pat"
    fi
done

# ─── 11. MEDIUM: Deleted files should not exist ───
echo ""
echo "--- MEDIUM: Deleted files check ---"
DELETED_FILES=(
    "AGENTS.md"
    "docs/archive/PLAN_V2.md"
    "docs/TEST_REPORT_v2.1.1.md"
)
for f in "${DELETED_FILES[@]}"; do
    if [[ -f "$f" ]]; then
        echo "FAIL: Deleted file still exists: $f"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $f removed"
    fi
done

# ─── 12. MEDIUM: .opencode and .git should not exist ───
echo ""
echo "--- MEDIUM: Directory checks ---"
if [[ -d ".opencode" ]]; then
    echo "FAIL: .opencode directory still exists"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: .opencode directory removed"
fi

if [[ -d ".git" ]]; then
    echo "FAIL: .git directory still exists (should be fresh export)"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: No .git directory"
fi

# ─── 13. LOW: Local dev paths with usernames ───
echo ""
echo "--- LOW: Local dev paths ---"
matches=$(grep_sensitive "/home/[a-z]+-?user/")
if [[ -n "$matches" ]]; then
    echo "WARN: Local dev paths with usernames found (review):"
    echo "$matches"
else
    echo "PASS: No local dev paths with usernames"
fi

# ─── Summary ───
echo ""
echo "=========================================="
if [[ $FAILURES -eq 0 ]]; then
    echo "VALIDATION PASSED: $NAME is clean for public sync"
    echo "=========================================="
    exit 0
else
    echo "VALIDATION FAILED: $FAILURES issue(s) found in $NAME"
    echo "=========================================="
    exit 1
fi