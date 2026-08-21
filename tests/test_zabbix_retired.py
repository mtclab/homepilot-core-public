"""Zabbix left the shipped configuration and must not creep back (#458 S5).

ADR-004: "Zabbix leaves the shipped configuration entirely. Operators running it
keep it; HomePilot simply stops assuming it." A removal is only done when
something keeps it done - a half-removed integration is how a fresh install ends
up with a Metrics link that 502s, which is the exact defect the ADR names.

So: grep the tree. Any occurrence outside the allowlist below fails, and every
allowlist entry is a specific line with a written reason, not a blanket path.

Teeth: restore any one of the removed pieces (the nginx `/zabbix/` block, the
`HP_ZABBIX_*` agent variables, `agent/go/zabbix.go`, the `zabbix_push` command,
`hp agent zabbix-push`, the dashboard deep-link) and this test names the file.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that are not the shipped product.
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    ".svelte-kit",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".claude",
    "htmlcov",
}

# Whole files that are the RECORD of decisions rather than shipped configuration:
# a decision record that could not name what it retired would be useless.
ALLOWED_FILES = {
    "docs/CHANGELOG.md",  # history: what past releases shipped
    "docs/adr/ADR-003-matrix-agent-bridge.md",  # accepted ADR, historical record
    "docs/adr/ADR-004-zero-touch-install.md",  # the ADR that ORDERS the removal
    "tests/test_zabbix_retired.py",  # this gate
}

# Individual lines that legitimately keep the word, each with its reason. Listed
# as (path, exact stripped line) so any OTHER line in the same file still fails.
ALLOWED_LINES = {
    # A host NAMED zabbix is a monitoring host. This is a hostname classifier for
    # the OPERATOR's machines, sitting beside "prometheus" and "grafana" - it is
    # not HomePilot depending on, shipping, or configuring Zabbix.
    ("src/homepilot/inventory/service.py", '"zabbix",'),
    (
        "tests/test_inventory_service.py",
        'assert _infer_role("random-vm", "monitoring zabbix") == "monitoring"',
    ),
    # Leak-scrub patterns for the OWNER's private hostnames and PVE token names.
    # These exist to keep private strings out of a public copy; dropping them
    # would weaken that, and they say nothing about HomePilot's own monitoring.
    (
        "scripts/scrub-for-public.sh",
        "scrub_files 's|monitor@pam!monitoring|monitor@pam!monitoring|g'",
    ),
    (
        "scripts/scrub-for-public.sh",
        "scrub_files 's|monitor@pam!monitoring|monitor@pam!monitoring|g'",
    ),
    ("scripts/scrub-for-public.sh", "scrub_files 's|monitor@pam|monitor@pam|g'"),
    ("scripts/scrub-for-public.sh", r"scrub_files 's|zabbix\.hp\.local|zabbix.example.com|g'"),
    (
        "scripts/scrub-for-public.sh",
        r"scrub_files 's|zabbix\.homepilot\.local|zabbix.example.com|g'",
    ),
    (
        "scripts/scrub-for-public.sh",
        r"sed -i 's|DNS\.2 = zabbix\.hp\.local|DNS.2 = zabbix.example.com|g' "
        "monitoring/caddy/certs/openssl.cnf",
    ),
    ("scripts/validate-scrub.sh", '"monitor@pam"'),
    ("scripts/validate-scrub.sh", r'"zabbix\.hp\.local"'),
    ("scripts/validate-scrub.sh", r'"zabbix\.homepilot\.local"'),
}


def _tracked_text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".db"}:
            continue
        files.append(path)
    return files


def test_no_zabbix_reference_remains_in_the_shipped_tree():
    offenders: list[str] = []
    for path in _tracked_text_files():
        rel = str(path.relative_to(REPO_ROOT))
        if rel in ALLOWED_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "zabbix" not in text.lower():
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "zabbix" not in line.lower():
                continue
            if (rel, line.strip()) in ALLOWED_LINES:
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "Zabbix references remain:\n" + "\n".join(offenders)


def test_the_removed_surfaces_are_actually_gone():
    """Named explicitly, so a partial re-introduction cannot hide behind the
    grep's allowlist."""
    assert not (REPO_ROOT / "agent" / "go" / "zabbix.go").exists()
    assert not (REPO_ROOT / "monitoring" / "zabbix").exists()

    nginx = (REPO_ROOT / "deploy/control-plane/nginx-hp-proxy.conf").read_text()
    assert "location /zabbix/" not in nginx
    assert "10.0.0.1" not in nginx, "the hardcoded monitoring host is still proxied"

    from homepilot.config import Settings

    assert "zabbix_url" not in Settings.model_fields

    from homepilot.agent_hub.server import AgentHubServer

    assert not hasattr(AgentHubServer, "send_zabbix_push")

    from homepilot.agent_hub.audit import ActionType

    assert "zabbix_push" not in getattr(ActionType, "__args__", ())


def test_the_native_replacement_is_wired_in_its_place():
    """The removal and the replacement ship together, so there is never a
    release where the UI advertises metrics that do not exist."""
    from homepilot.config import Settings
    from homepilot.main import app

    assert "metrics_retention_days" in Settings.model_fields
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/monitoring/hosts/{hostname}/series" in paths
    assert "/monitoring/hosts/{hostname}/latest" in paths
    assert "/monitoring/rules" in paths
    assert "/monitoring/alerts" in paths
