"""The guest network over MCP, at the admin tier (#553).

Full parity with what the API can do here, which is one read: GET
/admin/guest-network. The CHANGE is not a second tool, and that is the design
rather than an omission - a guest network is rebuilt by proposing a
`guest-network` artifact (``propose_artifact``), having a human approve it with
the relayed code, and applying it. So the assistant can survey, plan, and
propose the fix, and a human still decides.

The handler calls the SAME ``guest_network_report`` the admin route calls, so
the console and the assistant cannot describe the estate differently.
"""

from __future__ import annotations

from typing import Any

from homepilot.provision.guest_network import guest_network_report

from .settings_tools import _state

_ADMIN_NOTE = (
    "Admin tier: an MCP token with read_only or full scope is refused. Reads "
    "only - nothing here changes the cluster."
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "query_guest_network",
        "description": (
            "The guest network of this install, three ways: what the operator "
            "settings SAY it should be ('desired'), what the Proxmox cluster "
            "currently has ('survey' - SDN zones, vnets, subnets, the vnet "
            "firewall, which firewall stack the node runs, and any pending SDN "
            "config), and the difference ('plan' - the ordered steps that would "
            "converge it, empty when it already matches). 'blockers' in the plan "
            "are states HomePilot refuses to resolve by itself because doing so "
            "would repurpose or remove something an operator built. "
            "'enforcement' says whether the vnet firewall rules are actually "
            "enforced: under the legacy iptables stack they are stored and NOT "
            "applied to vnet forward traffic, and the fence that holds is the "
            "per-VM rule set written at provision time. To CHANGE any of it, "
            "propose a 'guest-network' artifact with propose_artifact - a human "
            "approves it with a relayed code, and applying it runs exactly the "
            "plan reported here. " + _ADMIN_NOTE
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "configured": {"type": "boolean"},
                "desired": {"type": ["object", "null"]},
                "survey": {"type": ["object", "null"]},
                "plan": {"type": ["object", "null"]},
                "detail": {"type": "string"},
                "enforcement": {"type": "string"},
            },
            "required": ["configured", "detail"],
        },
    },
]


async def handle_query_guest_network(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """GET /admin/guest-network over MCP, through the same report function."""
    state = _state(ctx)
    return await guest_network_report(state, proxmox=ctx.get("proxmox"))
