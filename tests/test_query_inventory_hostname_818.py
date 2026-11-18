"""query_inventory applies a hostname filter (#818, the side bug).

The MCP tool turned a non-JSON filter into ``{"hostname": ...}`` and then never
applied it, so asking for one machine by name returned the whole inventory -
seen on prod while drafting KB articles. A JSON ``{"hostname": ...}`` filter was
ignored the same way.

Against a real database, through the real tool dispatcher. Teeth: drop the
``hostname=`` argument from the tool's ``list_hosts`` call and every case here
fails (all three hosts come back).
"""

from __future__ import annotations

import pytest

from homepilot.db.repository import Repository
from homepilot.mcp.server import _handle_tool


@pytest.fixture
async def estate(portal_db):
    repo = Repository(portal_db)
    ids = {}
    for name in ("web01", "web02", "db01"):
        ids[name] = await repo.create_host(hostname=name, host_type="vm")
        await repo.create_service(host_id=ids[name], name=f"svc-{name}", runtime="systemd")
    return repo, ids


@pytest.mark.parametrize("filter_str", ["web01", '{"hostname": "web01"}'])
async def test_a_hostname_filter_returns_only_that_host(estate, filter_str: str) -> None:
    repo, ids = estate
    result = await _handle_tool(
        "query_inventory", {"filter": filter_str}, {"repo": repo, "_mcp_token_scope": "read_only"}
    )
    assert [h["hostname"] for h in result["hosts"]] == ["web01"]
    # Its services, not the estate's.
    assert [s["name"] for s in result["services"]] == ["svc-web01"]
    assert {s["host_id"] for s in result["services"]} == {ids["web01"]}


async def test_an_exact_name_is_not_a_substring_search(estate) -> None:
    repo, _ids = estate
    result = await _handle_tool(
        "query_inventory", {"filter": "web"}, {"repo": repo, "_mcp_token_scope": "read_only"}
    )
    assert result["hosts"] == [] and result["services"] == []


async def test_no_filter_still_returns_everything(estate) -> None:
    repo, _ids = estate
    result = await _handle_tool("query_inventory", {}, {"repo": repo})
    assert sorted(h["hostname"] for h in result["hosts"]) == ["db01", "web01", "web02"]
    assert len(result["services"]) == 3
