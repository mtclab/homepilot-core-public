# The guest portal (#442): a friend's window into the lab

Same backend, different client. A cert-holding friend opens `/guest/`
— the page ships inside the backend, nothing to copy — and it talks to `/guest/*` (their machines,
their budget, power buttons) and `/invite/*` (redeeming a new machine). Nothing
else of HomePilot faces them: not the admin UI, not the API, not MCP.

```
friend's browser ──mTLS──> front nginx ── /guest/* + /invite/* ──> backend :8000
```

## What goes where

- **Nothing is copied anywhere.** The portal page ships inside the backend and
  is served at `/guest/`; the front nginx adds ONE location block.
- **Backend env** (control-plane box): the four portal variables must be set —
  `HP_PORTAL_TRUSTED_PROXY` (the front server's address as the backend sees it),
  `HP_PORTAL_PROXY_SECRET`, `HP_PORTAL_VERIFY_HEADER=ssl-client-verify`,
  `HP_PORTAL_CN_HEADER=ssl-client-subject-dn`. Unset = every guest route
  answers 503, fail-closed.

## The vhost

The vhost is a committed file, not a snippet in this page:
**`deploy/portal/nginx-guest-portal.conf`** is the source of truth, and
`tests/test_guest_vhost_reference.py` lints it (only `^/(guest|invite)` may
proxy to the backend; `Authorization` and `Cookie` are stripped; the catch-all
never reaches the app). Copy that file and adapt `<backend-host>`, `server_name`
and the certificate paths - nothing else.

The heart of it:

```nginx
location = / { return 302 /guest/; }

# ONLY these two prefixes reach HomePilot. Everything else 404s here.
location ~ ^/(guest|invite)(/|$) {
    proxy_pass http://<backend-host>:8000;
    proxy_set_header X-Hp-Portal-Secret     "<value of HP_PORTAL_PROXY_SECRET>";
    proxy_set_header ssl-client-verify      $ssl_client_verify;
    proxy_set_header ssl-client-subject-dn  $ssl_client_s_dn;
    # Never forward auth material a client might try to smuggle.
    proxy_set_header Authorization "";
    proxy_set_header Cookie "";
}
```

Three factors, all required by the backend on every request: the request must
come from the address in `HP_PORTAL_TRUSTED_PROXY`, carry the shared secret,
and carry `ssl-client-verify: SUCCESS` with exactly one `CN=` in the subject.
Anything less renders "no client certificate".

The vhost is the FIRST wall, not the only one: the backend serves the operator
API from the same process, and `tests/test_portal_management_boundary.py` proves
that a request carrying perfect guest trust is refused by every management route
even when it reaches the backend directly.

**Not yet proven live.** No guest portal is deployed. A real deployment is
verified by a live smoke at go-live (mTLS handshake, `/guest/` renders,
`/inventory` through the public vhost 404s); that is owner-gated and has not
been run.

## Per-guest budgets

`hp quota set --cn <friend> --max-vms 2 --max-cores 8 --max-memory-mb 16384 --max-disk-gb 100`

Totals across ALL the friend's machines. Redemption stops at the line (the
invite stays open so they can free resources and retry), and the portal shows
usage-vs-budget meters so the line is never a surprise. No quota row = no
quota; unset axes are unlimited. `hp quota list` shows every budget next to
real usage.

## What a guest can and cannot do

Can: see their machines (name, address, size, state), start / stop / reboot
them, watch their budget, redeem invites. Cannot: see anyone else's machines
(another guest's id answers exactly like a typo), see nodes / templates /
tasks / topology, read hypervisor error text, destroy or resize anything, or
reach any other HomePilot surface.

## The guest network (#553)

A friend's machine must reach the internet and nothing of the operator's. That
is a network and a fence, and HomePilot builds both.

### The network

One SDN zone (`simple`, dnsmasq DHCP, PVE IPAM), one vnet, one subnet with a
gateway, SNAT and a DHCP range. The estate's shape: vnet `innkeep` in zone
`guest`, subnet `198.51.100.0/24`, gateway `198.51.100.1`, DHCP `.100-.199`,
isolated from `192.0.2.0/24` - which is the operator LAN, on the same node,
because `elizabeth` is both hypervisor and gateway.

The desired state lives in the `guest_network_*` settings (Settings ->
Subsystems -> **Guest network**, or `HP_GUEST_NETWORK_*`). Each field is checked
locally before it is stored: a gateway outside its own subnet, a DHCP range that
would hand out the router's address, a vnet name longer than PVE's 8-character
field. PVE accepts all three happily, and the first anybody hears of them is a
guest with no network.

### Who gives the guest its address (#630)

**HomePilot does, by default.** `provision_ip_mode` ships as `static`: at
provision time a free address is allocated out of the guest subnet and written
into the guest's cloud-init as
`ipconfig0=ip=<addr>/<prefix>,gw=<gateway>`, together with
`nameserver=<provision_default_nameserver>`.

The DHCP range above is therefore optional equipment, not the mechanism. It has
to be: a `simple` SDN zone serves DHCP through **dnsmasq**, and on a node
without that package installed the zone exists, the settings look right, and a
guest booting on the vnet gets a link-local address and no explanation. That is
exactly how the first real guest came up. Set `provision_ip_mode` to `dhcp`
only on an install where something on the wire genuinely answers.

Free addresses are found by scanning the cluster's own guest configs for NICs
on the vnet - no allocation table, so a destroyed guest's address is free again
immediately. The lowest free address at or above the tenth host wins; `.1`-`.9`
are left for infrastructure. An address named on the invite or the request wins
over the allocator, and an exhausted subnet fails the provision before anything
is cloned.

The friend sees their address on the portal immediately, without the guest
agent having to answer: HomePilot chose the address, so it can record it.

### The change ships as an artifact

Describing the network does not build it. `GET /admin/guest-network` (and the
`query_guest_network` MCP tool) reports the **survey** (what the cluster has),
the **desired** state and the **plan** between them - empty when they already
match. To build or repair it, propose a **`guest-network` artifact** carrying a
```yaml guest-network-spec``` block, have a human approve it with the relayed
approval code, and apply it. The apply runs exactly the plan that was reported.

There is deliberately no "ensure" endpoint and no Apply button on the Settings
card. The owner's rule: *we do things through HomePilot, so the artifacts and
the KB stay up to date.* A bare admin mutation would change the estate and leave
no record of who decided to.

Nothing in this slice deletes. Every planned step creates or updates, so a
mistaken desired state cannot take an operator's existing zone away - and
because nothing deletes, the kind cannot roll itself back, which is why a
`rollback: true` claim on a guest-network body is refused at propose rather than
discovered at revoke. A zone somebody else built with a different type, or a
vnet already living in another zone, is reported as a **blocker**: HomePilot
will not repurpose it.

Drift for this kind is the same `plan()` function: an applied guest-network
artifact is "in spec" exactly when re-applying it would change nothing. An
unreachable cluster reads as `unknown`, never as green.

### Why the per-VM rules are the fence that actually holds

PVE 9 can carry firewall rules on a vnet
(`/cluster/sdn/vnets/{vnet}/firewall/*`), and that is the right place for
"guests may not reach the operator LAN". **Those rules are enforced only under
the nftables `proxmox-firewall` stack.** A node on the LEGACY iptables firewall
- which is what `elizabeth` runs - accepts them, stores them, shows them, and
does not apply them to vnet forward traffic.

So HomePilot writes both:

* the **vnet rules**, as part of the guest-network artifact, because they are
  the correct place for the intent and they become live the moment the node is
  switched to the nftables stack. The survey reports which stack the node runs,
  and the Settings card says so in as many words rather than implying a fence
  that is not there;
* the **per-VM rules**, at provision time, on the guest's own tap device. These
  are enforced by BOTH stacks, and they are what stands between a friend's
  machine and the operator's LAN today.

The per-VM fence is applied when the resolved bridge equals the configured guest
vnet and the isolate list is non-empty. The NIC gets `firewall=1` (without it
PVE stores the rules and applies none of them), the VM firewall is enabled with
`policy_out: ACCEPT` - the guest must still reach the internet - and the rules
go on in this order:

1. `ACCEPT udp dport 67:68` to the gateway (DHCP)
2. `ACCEPT udp dport 53` to the gateway (DNS)
3. `ACCEPT tcp dport 53` to the gateway (DNS)
4. `DROP` to each isolated CIDR
5. `DROP` to `<gateway>/32`

The ACCEPTs come first because the DROPs below them cover the gateway too;
reverse them and a fenced guest cannot get an address or resolve a name. The
applied ruleset is recorded on the provision result, so "is my friend's box
walled off" is answered with the rules rather than with a boolean.

A fence that cannot be written **fails the provision loudly and destroys the
half-made guest**, before it ever boots. A guest on the guest wire with the
operator's LAN in reach is the one outcome this exists to prevent, and "we
added the rules a second later" is not a property anybody can rely on.

### The fence is verified from inside the guest, not assumed from its rules

Everything above is configuration: rules on the tap, `firewall=1` on the NIC,
the datacenter switch on, an enforcing stack. All of it was true of the first
real guest on prod - and nobody had ever run a command inside a guest and
watched a packet towards the operator LAN go nowhere. Written is not
established, so once the guest is up the provision **asks the guest itself**
(through qemu-guest-agent, before the machine is recorded or handed to anyone):

* open a TCP connection to an address HomePilot *knows* is alive inside the
  isolated range - the Proxmox host it just cloned the guest through, on its
  API port and on tcp/53. The second port exists for SELinux-enforcing images,
  where qemu-guest-agent runs confined and gets `EPERM` on arbitrary ports
  before a packet leaves but may open DNS; behind a DROP both ports are
  silent, and without one the API port completes a handshake while 53 answers
  with a reset - either proves the host was reached;
* and to one it expects to reach outside the fence - the guest gateway's
  resolver (tcp/53, which the fence ACCEPTs) and the guest's own nameserver.

The probe runs after cloud-init has finished (the agent answers before the
network exists), and a probe that reached nothing at all is repeated a few
seconds apart, a bounded number of times, before "no network yet" is reported.

The provision result carries the verdict as `fence` and the sentence behind it
as `fence_detail`, and `guest_network_fence.verification` records every probe:

| `fence` | what was established |
|---|---|
| `verified` | the isolated address gave no answer while a control answered. A DROP at the tap is exactly "silence while the network is fine". |
| `unverified` | nothing was established, and `fence_detail` says why: no qemu-guest-agent, silence on both sides (a guest with no network yet), the agent confined by SELinux (`EPERM` before a packet leaves), no python3 or bash in the image, or no known-alive address inside the isolate list to probe. The guest is still fenced by its rules; the operator is told the fence was written, not proven. |
| `null` | the guest is not on the guest vnet, so there is no fence to verify. |

A guest that **reaches** the isolated range - a completed handshake or a reset,
either proves the host answered - is treated exactly like one whose rules could
not be written: **the provision fails and the guest is stopped and destroyed**,
with the address it reached in the error. `breached` never appears on a
succeeded task. The check itself can never fail a provision: anything that goes
wrong inside it is reported as `unverified`, with the reason.

A reset is read as a reach on purpose: the fence HomePilot writes is DROP-only,
so nothing it wrote can produce one. An operator rule of action `REJECT` that
matches before the fence would - the guest is then destroyed and the error
names the port that answered with a reset, which is the thing to read.

The probe is only ever run when the Proxmox host resolves to an address inside
one of the isolate CIDRs. A hypervisor reachable solely over a management
network the fence does not cover leaves HomePilot with nothing it can vouch for,
and it says so rather than testing against an address that may simply be
absent - silence towards a dead host would look like a fence.

### Where the PVE calls live

HomePilot does not re-implement Proxmox endpoints. The SDN and firewall calls go
through the estate's own library, `homepilot-proxmox-mcp` (repo
`mtclab/proxmox-mcp`, public mirror `mtclab/proxmox-mcp-public`), via the single
adapter `src/homepilot/adapters/pve_sdn.py`. That adapter is the only place in
this codebase that names a PVE SDN or firewall endpoint, and it records the two
kinds of gap it has to work around: reads whose library function returns a
formatted string that has dropped the fields a plan needs, and endpoints the
library does not cover yet (the vnet firewall, and subnet update).
