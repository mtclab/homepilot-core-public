from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# autoescape is ON for every template extension we ship: the portal renders
# operator-chosen and redeemer-submitted strings, and a single unescaped one
# would be a stored-XSS hole on a page reached with a client certificate.
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(default_for_string=True, default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)

# Headers on every portal response: the portal is not an app shell, it embeds
# nothing and is embedded by nothing, and no page here may be cached by a shared
# proxy (a rendered page names a guest and a username).
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def render(template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    html = _env.get_template(template).render(**context)
    return HTMLResponse(content=html, status_code=status_code, headers=dict(SECURITY_HEADERS))
