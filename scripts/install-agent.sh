#!/usr/bin/env bash
# install-agent.sh — Install hp-agent binary on a managed host.
# Usage: curl -fsSL https://raw.githubusercontent.com/mtclab/homepilot-core-public/main/scripts/install-agent.sh | bash
#
# Options (env vars):
#   VERSION     — Release tag (e.g. v2.3.0). Defaults to "latest".
#   INSTALL_DIR — Where to install (default: /usr/local/bin)
#   HUB_URL     — Agent Hub URL to auto-enroll (optional)
#   HUB_TOKEN   — Agent Hub auth token for auto-enroll (optional)

set -euo pipefail

REPO="mtclab/homepilot-core-public"
VERSION="${VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
HUB_URL="${HUB_URL:-}"
HUB_TOKEN="${HUB_TOKEN:-}"

# Detect arch
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  GOARCH="amd64" ;;
    aarch64) GOARCH="arm64" ;;
    arm64)   GOARCH="arm64" ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

echo "=== Installing hp-agent (linux-$GOARCH) ==="

# Resolve version
if [ "$VERSION" = "latest" ]; then
    VERSION=$(curl -sL "https://api.github.com/repos/$REPO/releases/latest" | grep '"tag_name":' | head -1 | sed 's/.*"tag_name": "\([^"]*\)".*/\1/')
    echo "Latest release: $VERSION"
fi

# Download
URL="https://github.com/$REPO/releases/download/$VERSION/hp-agent-linux-$GOARCH"
echo "Downloading from $URL ..."
curl -fSL -o /tmp/hp-agent "$URL"

# Verify it's a binary (basic check)
if ! file /tmp/hp-agent | grep -q "ELF"; then
    echo "Downloaded file is not a valid binary. Check the release exists." >&2
    exit 1
fi

# Install
chmod +x /tmp/hp-agent
sudo mv /tmp/hp-agent "$INSTALL_DIR/hp-agent"
echo "Installed to $INSTALL_DIR/hp-agent"

# Verify
hp-agent --version || true

# Optional: auto-enroll
if [ -n "$HUB_URL" ] && [ -n "$HUB_TOKEN" ]; then
    echo "=== Enrolling with Agent Hub ==="
    hp-agent enroll --hub "$HUB_URL" --token "$HUB_TOKEN"
    echo "Enrolled. Start the agent with: hp-agent start"
else
    echo ""
    echo "To enroll: hp-agent enroll --hub https://your-homepilot:8443 --token YOUR_TOKEN"
    echo "To start:  hp-agent start"
fi