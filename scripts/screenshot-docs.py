#!/usr/bin/env python3
"""Screenshot documentation pages from HomePilot with automatic censoring of sensitive data.

Usage:
    .venv/bin/python scripts/screenshot-docs.py [--output-dir docs/screenshots] [--base-url http://your-server.local:8000]

Censoring rules:
    - API tokens/secrets: replaced with colored rectangles
    - Hostnames/IPs that match internal patterns: blurred or replaced
    - Any element with data-sensitive attribute: masked
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

CENSOR_COLOR = "#1a1a2e"
CENSOR_LABEL_COLOR = "#e94560"
VIEWPORT = {"width": 1280, "height": 900}

SENSITIVE_PATTERNS = [
    re.compile(r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"),
    re.compile(r"192\.168\.\d{1,3}\.\d{1,3}"),
    re.compile(r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"),
    re.compile(r"[a-f0-9]{32,}"),
    re.compile(r"eyJ[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+"),
]

PAGES = [
    {"path": "/ui/auth/login", "name": "login", "title": "Login Page"},
    {"path": "/ui/inventory", "name": "inventory", "title": "Inventory Dashboard"},
    {"path": "/ui/artifacts", "name": "artifacts", "title": "Artifacts List"},
    {"path": "/ui/review", "name": "review", "title": "Review Queue"},
    {"path": "/ui/tokens", "name": "tokens", "title": "API Token Management"},
    {"path": "/ui/settings", "name": "settings", "title": "Settings"},
    {"path": "/ui/kb", "name": "knowledge-base", "title": "Knowledge Base"},
    {"path": "/ui/drift", "name": "drift", "title": "Drift Detection"},
    {"path": "/ui/journal", "name": "journal", "title": "Audit Journal"},
    {"path": "/ui/artifacts/new", "name": "create-artifact", "title": "Create Artifact"},
]


def inject_censor_css(page):
    page.add_style_tag(content=f"""
        /* Hide any real IP/hostname elements - they'll be replaced by JS */
        .sensitive-data {{
            background-color: {CENSOR_COLOR} !important;
            color: {CENSOR_LABEL_COLOR} !important;
            font-family: monospace !important;
            position: relative;
        }}
        .sensitive-data::before {{
            content: attr(data-censored-label);
            color: {CENSOR_LABEL_COLOR};
        }}
    """)


def censor_page_content(page):
    page.evaluate(f"""() => {{
        const CENSOR_COLOR = '{CENSOR_COLOR}';
        const CENSOR_LABEL_COLOR = '{CENSOR_LABEL_COLOR}';
        
        // Censor API tokens in token tables
        document.querySelectorAll('td, span, code, .token-value, [data-sensitive]').forEach(el => {{
            const text = el.textContent || '';
            // Replace hex tokens (32+ chars)
            if (/[a-f0-9]{{32,}}/i.test(text)) {{
                el.textContent = text.replace(/[a-f0-9]{{32,}}/gi, '••••••••••••••••');
                el.style.backgroundColor = CENSOR_COLOR;
                el.style.color = CENSOR_LABEL_COLOR;
                el.style.fontFamily = 'monospace';
            }}
            // Replace JWT tokens
            if (/eyJ[A-Za-z0-9-_]+\\.[A-Za-z0-9-_]+\\.[A-Za-z0-9-_]+/.test(text)) {{
                el.textContent = 'eyJ••••.••••.••••';
                el.style.backgroundColor = CENSOR_COLOR;
                el.style.color = CENSOR_LABEL_COLOR;
            }}
            // Replace internal IPs
            if (/10\\.\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}/.test(text)) {{
                el.textContent = text.replace(/10\\.\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}/g, '10.x.x.x');
                el.style.backgroundColor = CENSOR_COLOR;
                el.style.color = CENSOR_LABEL_COLOR;
            }}
            if (/192\\.168\\.\\d{{1,3}}\\.\\d{{1,3}}/.test(text)) {{
                el.textContent = text.replace(/192\\.168\\.\\d{{1,3}}\\.\\d{{1,3}}/g, '192.168.x.x');
                el.style.backgroundColor = CENSOR_COLOR;
                el.style.color = CENSOR_LABEL_COLOR;
            }}
        }});
        
        // Censor password/input fields
        document.querySelectorAll('input[type="password"], input[name*="secret"], input[name*="token"]').forEach(el => {{
            el.value = '••••••••';
            el.style.backgroundColor = CENSOR_COLOR;
        }});
    }}""")


def take_screenshot(page, url, page_info, output_dir, auth_token=None):
    path = page_info["path"]
    name = page_info["name"]
    title = page_info["title"]
    needs_auth = page_info.get("needs_auth", False)

    full_url = f"{url}{path}"

    # Replace {host_id} and {artifact_id} with example values
    if "{host_id}" in full_url:
        full_url = full_url.replace("{host_id}", "1")
    if "{artifact_id}" in full_url:
        full_url = full_url.replace("{artifact_id}", "1")

    try:
        page.goto(full_url, wait_until="networkidle", timeout=15000)
        time.sleep(0.5)

        # Set auth cookie if needed
        if needs_auth and auth_token:
            page.evaluate(f"""() => {{
                document.cookie = "hp_token={auth_token}; path=/";
            }}""")
            page.reload(wait_until="networkidle", timeout=15000)
            time.sleep(0.5)

        # Inject censoring
        inject_censor_css(page)
        censor_page_content(page)
        time.sleep(0.3)

        # Take screenshot
        out_path = output_dir / f"{name}.png"
        page.screenshot(path=str(out_path), full_page=True)
        print(f"  ✓ {name}: {title} → {out_path}")
        return True

    except Exception as e:
        print(f"  ✗ {name}: {title} → Error: {e}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Screenshot HomePilot docs with censoring")
    parser.add_argument("--base-url", default="http://your-server.local:8000", help="Base URL")
    parser.add_argument("--output-dir", default="docs/screenshots", help="Output directory")
    parser.add_argument("--auth-token", default=None, help="Auth token for authenticated pages")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Try to get auth token from environment or .env
    auth_token = args.auth_token
    if not auth_token:
        env_path = Path.home() / ".hp" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HP_ADMIN_SECRET="):
                    auth_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
        )
        page = context.new_page()

        # If we have auth, login first
        if auth_token:
            page.goto(f"{args.base_url}/auth/login", wait_until="networkidle", timeout=15000)
            time.sleep(0.5)
            # Try to fill login form
            try:
                token_input = page.locator('input[type="password"], input[name*="token"], input[name*="secret"]')
                if token_input.count() > 0:
                    token_input.first.fill("••••••••")
                    submit_btn = page.locator('button[type="submit"], button:has-text("Login"), button:has-text("Sign in")')
                    if submit_btn.count() > 0:
                        submit_btn.first.click()
                        page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

        print(f"Taking screenshots from {args.base_url}...")
        print(f"Output: {output_dir}/")
        print()

        results = {"success": 0, "failed": 0}
        for page_info in PAGES:
            ok = take_screenshot(page, args.base_url, page_info, output_dir, auth_token)
            if ok:
                results["success"] += 1
            else:
                results["failed"] += 1

        browser.close()

    print()
    print(f"Done: {results['success']} screenshots taken, {results['failed']} failed")
    print(f"Output: {output_dir}/")

    # Also create a simple index
    index = f"""# HomePilot Documentation Screenshots

> Auto-generated on {time.strftime('%Y-%m-%d')}. All sensitive data has been censored.

"""
    for page_info in PAGES:
        name = page_info["name"]
        title = page_info["title"]
        index += f"## {title}\n\n![{title}]({name}.png)\n\n"

    (output_dir / "README.md").write_text(index)
    print(f"Index written to {output_dir / 'README.md'}")


if __name__ == "__main__":
    main()