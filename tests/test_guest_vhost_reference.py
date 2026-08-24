"""Lint the committed guest-portal vhost (deploy/portal/nginx-guest-portal.conf).

The backend serves the guest portal, the operator API and MCP from ONE port.
The front nginx is what keeps the public mTLS vhost pointed at `/guest/*` and
`/invite/*` only - so the shape of that config is a security control, and it
used to exist solely as a fenced block inside a markdown page, where nothing
could check it and a copy-paste edit could widen the proxy to `/`.

This lints the committed file as text (no nginx binary is installed anywhere in
this project's toolchain, and shelling out to one would make the gate skip
silently where it is missing):

  a. every `proxy_pass` sits inside a `location` anchored to `^/(guest|invite)`;
  b. that block strips `Authorization` and `Cookie`, so auth material a client
     smuggles in never reaches the backend;
  c. the catch-all location does not proxy to the backend at all.

It does NOT prove a live deployment: no guest portal is deployed yet, and the
real proof is a smoke at go-live (owner-gated). This gate proves the reference
the operator copies is not quietly wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONF = Path(__file__).resolve().parents[1] / "deploy" / "portal" / "nginx-guest-portal.conf"

# The location match that may reach HomePilot. Anchored: `^/(guest|invite)` and
# nothing looser. A prefix location (`location /guest/`) would also be safe in
# nginx, but the committed reference uses the regex form for both prefixes at
# once, and pinning the exact shape is what makes a widening edit visible.
_ALLOWED_MATCH = re.compile(r"^~\s*\^/\(guest\|invite\)")
_CATCH_ALL = re.compile(r"^(=\s*)?/$")


class LintError(AssertionError):
    """A lint finding. AssertionError so a bare `assert` reads the same way."""


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def parse_locations(text: str) -> list[tuple[str, str]]:
    """Return (match, body) for every `location` block, innermost bodies intact.

    A hand-rolled brace matcher rather than a config parser: the file is small
    and fully under our control, and a dependency-free lint runs everywhere.
    """
    body = _strip_comments(text)
    blocks: list[tuple[str, str]] = []
    for opener in re.finditer(r"\blocation\s+([^{]*?)\s*\{", body):
        depth = 1
        i = opener.end()
        while i < len(body) and depth:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            i += 1
        if depth:
            raise LintError(f"unbalanced braces after `location {opener.group(1)}`")
        blocks.append((opener.group(1).strip(), body[opener.end() : i - 1]))
    return blocks


def _proxy_pass_lines(chunk: str) -> list[str]:
    return [m.group(0).strip() for m in re.finditer(r"\bproxy_pass\b[^;]*;", chunk)]


def lint_only_guest_prefixes_are_proxied(text: str) -> None:
    """(a) No `proxy_pass` anywhere except inside the guest/invite location."""
    total = len(_proxy_pass_lines(_strip_comments(text)))
    if not total:
        raise LintError("the reference vhost proxies nothing - it cannot be the reference")

    inside_allowed = 0
    for match, body in parse_locations(text):
        passes = _proxy_pass_lines(body)
        if not passes:
            continue
        if not _ALLOWED_MATCH.match(match):
            raise LintError(
                f"`location {match}` proxies to the backend: {passes}. Only "
                "^/(guest|invite) may reach HomePilot - the same port serves the "
                "operator API and MCP."
            )
        inside_allowed += len(passes)

    if inside_allowed != total:
        raise LintError(
            f"{total - inside_allowed} of {total} proxy_pass directives are not inside any "
            "location block - a top-level proxy_pass publishes the whole backend"
        )


def lint_smuggled_auth_is_stripped(text: str) -> None:
    """(b) The proxied block blanks Authorization and Cookie."""
    for match, body in parse_locations(text):
        if not _ALLOWED_MATCH.match(match) or not _proxy_pass_lines(body):
            continue
        collapsed = re.sub(r"\s+", " ", body)
        for header in ("Authorization", "Cookie"):
            if f'proxy_set_header {header} "";' not in collapsed:
                raise LintError(
                    f"`location {match}` does not strip {header}: add "
                    f'`proxy_set_header {header} "";` so a guest cannot smuggle '
                    "operator credentials through the portal vhost"
                )
        return
    raise LintError("no guest/invite location with a proxy_pass was found at all")


def lint_catch_all_does_not_reach_the_backend(text: str) -> None:
    """(c) `location /` (or `location = /`) must not proxy - redirect or 404."""
    for match, body in parse_locations(text):
        if not _CATCH_ALL.match(match):
            continue
        passes = _proxy_pass_lines(body)
        if passes:
            raise LintError(
                f"the catch-all `location {match}` proxies to the backend ({passes}) - "
                "it may only redirect to /guest/ or refuse"
            )


ALL_LINTS = (
    lint_only_guest_prefixes_are_proxied,
    lint_smuggled_auth_is_stripped,
    lint_catch_all_does_not_reach_the_backend,
)


@pytest.fixture(scope="module")
def conf_text() -> str:
    assert CONF.is_file(), (
        f"{CONF} is missing - the guest vhost must be a committed, lintable file, "
        "not a fenced block in a markdown page"
    )
    return CONF.read_text()


class TestTheCommittedReferenceIsClean:
    def test_only_guest_and_invite_reach_the_backend(self, conf_text):
        lint_only_guest_prefixes_are_proxied(conf_text)

    def test_authorization_and_cookie_are_stripped(self, conf_text):
        lint_smuggled_auth_is_stripped(conf_text)

    def test_the_catch_all_never_reaches_the_backend(self, conf_text):
        lint_catch_all_does_not_reach_the_backend(conf_text)

    def test_the_catch_all_still_sends_people_to_the_portal(self, conf_text):
        """Not security, but the reason the catch-all exists: a friend typing the
        bare hostname must land on the portal rather than a bare 404."""
        assert "return 302 /guest/;" in conf_text

    def test_the_parser_actually_sees_the_file(self, conf_text):
        """An empty finding must mean "clean", never "nothing was parsed"."""
        locations = parse_locations(conf_text)
        assert len(locations) >= 2, locations
        assert any(_ALLOWED_MATCH.match(m) for m, _ in locations)
        assert len(_proxy_pass_lines(_strip_comments(conf_text))) >= 1

    def test_docs_point_at_the_committed_file_as_the_source(self):
        doc = (Path(__file__).resolve().parents[1] / "docs" / "guest-portal.md").read_text()
        assert "deploy/portal/nginx-guest-portal.conf" in doc, (
            "docs/guest-portal.md must name the committed vhost as the source of truth - "
            "two copies of a security control drift"
        )


def _mutate(text: str, old: str, new: str) -> str:
    """Replace `old` with `new` and PROVE the edit landed.

    A teeth test whose mutation silently no-ops (the anchor string was reworded
    in the conf) would assert that the lint fails on the pristine file - which it
    would not - and the failure would look like a broken lint instead of a stale
    test. Fail loudly on the no-op instead.
    """
    assert old in text, f"teeth anchor no longer present in the conf: {old!r}"
    mutated = text.replace(old, new)
    assert mutated != text
    return mutated


class TestTheLintsHaveTeeth:
    """Each lint is fed a MUTATED copy of the real file and must fail. A lint
    that passes on a broken config is decoration."""

    def test_a_second_proxied_location_is_caught(self, conf_text):
        mutated = _mutate(
            conf_text,
            "    location = / { return 302 /guest/; }",
            "    location = / { return 302 /guest/; }\n"
            "    location /api { proxy_pass http://<backend-host>:8000; }",
        )
        with pytest.raises(LintError, match=r"location /api` proxies to the backend"):
            lint_only_guest_prefixes_are_proxied(mutated)

    def test_a_top_level_proxy_pass_is_caught(self, conf_text):
        mutated = _mutate(
            conf_text,
            "    location = / { return 302 /guest/; }",
            "    proxy_pass http://<backend-host>:8000;",
        )
        with pytest.raises(LintError, match="not inside any location block"):
            lint_only_guest_prefixes_are_proxied(mutated)

    def test_a_widened_location_match_is_caught(self, conf_text):
        mutated = _mutate(conf_text, "location ~ ^/(guest|invite)(/|$)", "location /")
        with pytest.raises(LintError, match="proxies to the backend"):
            lint_only_guest_prefixes_are_proxied(mutated)
        # ...and the catch-all lint independently objects to the same edit.
        with pytest.raises(LintError, match="catch-all"):
            lint_catch_all_does_not_reach_the_backend(mutated)

    def test_dropping_the_authorization_strip_is_caught(self, conf_text):
        mutated = _mutate(conf_text, '        proxy_set_header Authorization "";\n', "")
        assert 'proxy_set_header Authorization ""' not in mutated
        with pytest.raises(LintError, match="does not strip Authorization"):
            lint_smuggled_auth_is_stripped(mutated)

    def test_dropping_the_cookie_strip_is_caught(self, conf_text):
        mutated = _mutate(conf_text, '        proxy_set_header Cookie "";\n', "")
        with pytest.raises(LintError, match="does not strip Cookie"):
            lint_smuggled_auth_is_stripped(mutated)

    def test_a_catch_all_that_proxies_is_caught(self, conf_text):
        mutated = _mutate(
            conf_text,
            "    location = / { return 302 /guest/; }",
            "    location / { proxy_pass http://<backend-host>:8000; }",
        )
        with pytest.raises(LintError, match="catch-all"):
            lint_catch_all_does_not_reach_the_backend(mutated)

    def test_an_empty_config_fails_every_lint(self):
        """The degenerate case: a lint that quietly passes on nothing would pass
        on a truncated or moved file too."""
        with pytest.raises(LintError):
            lint_only_guest_prefixes_are_proxied("server { }")
        with pytest.raises(LintError):
            lint_smuggled_auth_is_stripped("server { }")

    def test_a_commented_out_proxy_pass_is_not_counted(self, conf_text):
        """The other direction: comments must not create phantom findings, or
        the teeth above would fire for the wrong reason."""
        mutated = _mutate(
            conf_text,
            "    location = / { return 302 /guest/; }",
            "    # location /api { proxy_pass http://elsewhere:9000; }\n"
            "    location = / { return 302 /guest/; }",
        )
        for lint in ALL_LINTS:
            lint(mutated)
