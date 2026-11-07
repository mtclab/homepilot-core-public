"""THE GATE for #715: every reader of the Proxmox tokens gets the same answer.

Three resolvers disagreed. With a malformed write token stored (a past settings
save once put an ERROR MESSAGE into that vault slot):

* `ProxmoxClient` sent it on every clone/destroy/config write - 401;
* the SDN/firewall gateway (`pve_sdn._usable_write_token`) quietly swapped in
  the read token - so SDN and firewall writes worked while guest writes failed;
* the settings report (`admin/router._resolve_proxmox_token`) did not validate
  at all, and said `token_source: vault` for a token the running client had
  rejected and replaced with the environment's.

One resolver (`proxmox_config.resolve_proxmox_tokens`) and one token rule
(`is_pve_token`) now serve all of them; these assertions compare what each
consumer actually USES, not what a helper returns.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.adapters.pve_sdn import _usable_write_token
from homepilot.admin.router import proxmox_settings_report
from homepilot.proxmox_config import resolve_proxmox_tokens

READ = "hp@pve!ro=11111111-1111-1111-1111-111111111111"
WRITE = "hp@pve!rw=22222222-2222-2222-2222-222222222222"
ENV = "hp@pve!env=33333333-3333-3333-3333-333333333333"
BROKEN = "Error: request failed with 401"  # what a bad save left in the slot


class Vault:
    def __init__(self, **slots: str) -> None:
        self.slots = {k.replace("_", "-"): v for k, v in slots.items()}

    async def get_secret(self, key: str) -> dict[str, Any]:
        if key not in self.slots:
            raise KeyError(key)
        return {"token": self.slots[key]}


def _write_header_token(client: ProxmoxClient) -> str:
    return client._write_client.headers["Authorization"].removeprefix("PVEAPIToken=")


def test_the_client_and_the_gateway_write_with_the_same_credential():
    client = ProxmoxClient("https://pve.example.com:8006", READ, write_token=BROKEN)

    native = _write_header_token(client)
    gateway = _usable_write_token(client.credentials())

    assert native == gateway, (
        f"guest writes use {native[:12]!r}..., SDN/firewall writes use {gateway[:12]!r}..."
    )
    assert BROKEN not in native, "a stored non-token was sent to PVE as a credential"


async def test_a_broken_write_token_falls_back_to_the_read_token_and_is_named():
    tokens = await resolve_proxmox_tokens(Vault(pve_token=READ, pve_write_token=BROKEN), {})

    assert (tokens.write_token, tokens.write_source) == (READ, "read_token")
    assert tokens.rejected == ("pve-write-token",)


async def test_a_broken_read_token_is_not_reported_as_the_vaults(monkeypatch):
    """The report and the running client resolve identically: the env token,
    from the env, with the rejected vault slot named."""
    tokens = await resolve_proxmox_tokens(Vault(pve_token=BROKEN), {"PVE_API_TOKEN": ENV})
    assert (tokens.token, tokens.token_source) == (ENV, "env")

    monkeypatch.setenv("PVE_API_TOKEN", ENV)
    state = SimpleNamespace(
        vault=Vault(pve_token=BROKEN),
        proxmox=None,
        settings=SimpleNamespace(proxmox_host="", proxmox_port=8006, proxmox_verify_ssl=True),
    )
    report = await proxmox_settings_report(state)

    assert report["token_source"] == "env", report
    assert report["tokens_rejected"] == ["pve-token"]


async def test_good_tokens_are_used_as_stored():
    tokens = await resolve_proxmox_tokens(Vault(pve_token=READ, pve_write_token=WRITE), {})
    assert tokens == (READ, "vault", WRITE, "vault", ())
    client = ProxmoxClient("https://pve.example.com:8006", tokens.token, write_token=WRITE)
    assert _write_header_token(client) == WRITE == _usable_write_token(client.credentials())


async def test_nothing_usable_is_nothing_configured():
    tokens = await resolve_proxmox_tokens(Vault(pve_token=BROKEN), {"PVE_API_TOKEN": "nope"})
    assert tokens.token == "" and tokens.write_token == ""
    assert set(tokens.rejected) == {"pve-token", "PVE_API_TOKEN"}
