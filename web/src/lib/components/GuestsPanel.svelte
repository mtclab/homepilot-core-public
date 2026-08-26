<script lang="ts">
	// Guests (#442 G3): budgets and invites, from the console instead of the CLI.
	// The minted token is shown ONCE, here, and stored nowhere.
	//
	// #549 F7: every input says what it is for and what happens with it. The
	// owner's words after using 3.5.0 - "it just has values to set but no
	// explanation what each value is for" - are the whole reason this panel
	// stopped being two rows of bare boxes. The pattern is SettingFields': label,
	// control, one muted line of prose under it, wired with `aria-describedby` so
	// the explanation is part of the field and not decoration next to it.
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { api, type GuestOverview } from '$lib/api';
	import { notify } from '$lib/stores';

	let data: GuestOverview | null = null;
	let loading = true;
	let loadError = '';
	let minted: {
		cn: string;
		token: string;
		caps?: { node: string; template_vmid: number };
	} | null = null;

	// Blank, not 9000: an empty infra field is the normal case since #553 C3 -
	// it means "whatever this instance provisions with" - and pre-filling a VMID
	// would make the defaults look like something the operator has to override.
	let invite = {
		cn: '',
		template_vmid: '' as string | number,
		node: '',
		cores: 2,
		memory_mb: 2048,
		disk_gb: 20,
		ttl_days: 7,
	};
	let quota = {
		cn: '',
		max_vms: '' as string | number,
		max_cores: '' as string | number,
		max_memory_mb: '' as string | number,
		max_disk_gb: '' as string | number,
	};
	let working = false;

	async function load() {
		loading = true;
		loadError = '';
		try {
			data = await api.getGuests();
		} catch (e) {
			loadError = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	function num(v: string | number): number | null {
		if (v === '' || v === null || v === undefined) return null;
		const n = Number(v);
		return Number.isFinite(n) ? n : null;
	}

	async function mint() {
		if (!invite.cn.trim()) {
			notify('An invite needs the CN from your friend’s certificate', 'err');
			return;
		}
		working = true;
		try {
			// Empty node/template are sent as null, not as '' or 0: the server reads
			// null as "use the provisioning default" and would refuse the empty
			// string outright.
			const res = await api.mintGuestInvite({
				...invite,
				cn: invite.cn.trim(),
				node: invite.node.trim() || null,
				template_vmid: num(invite.template_vmid),
			});
			minted = { cn: res.cn, token: res.token, caps: res.caps };
			notify(`Invite minted for ${res.cn}`);
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			working = false;
		}
	}

	async function saveQuota() {
		if (!quota.cn.trim()) {
			notify('A budget needs a CN', 'err');
			return;
		}
		working = true;
		try {
			await api.setGuestQuota({
				cn: quota.cn.trim(),
				max_vms: num(quota.max_vms),
				max_cores: num(quota.max_cores),
				max_memory_mb: num(quota.max_memory_mb),
				max_disk_gb: num(quota.max_disk_gb),
			});
			notify(`Budget saved for ${quota.cn.trim()}`);
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			working = false;
		}
	}

	async function revoke(prefix: string) {
		try {
			await api.revokeGuestInvite(prefix);
			notify('Invite revoked');
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		}
	}

	function lim(v: number | null | undefined): string {
		return v == null ? '∞' : String(v);
	}

	onMount(load);
</script>

<div class="space-y-4">
	<p class="prose-note prose-measure text-xs">
		A friend with a client certificate gets the guest portal: their own machines,
		power buttons and their budget - nothing else of HomePilot faces them. You
		mint them an invite here; redeeming it builds them one machine at the size
		the invite carries. A budget caps the TOTAL across all of that friend's
		machines and is checked at redemption, so nobody is stopped after the fact.
	</p>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<p class="text-danger text-sm">{loadError}</p>
	{:else if data}
		{#if data.guests.length}
			<p class="prose-note text-xs">Used against budget, per friend. ∞ = no limit set on that axis.</p>
			<table class="data-table text-xs">
				<thead>
					<tr>
						<th class="text-left">Guest</th>
						<th class="text-left">Machines</th>
						<th class="text-left">Cores</th>
						<th class="text-left">Memory (MB)</th>
						<th class="text-left">Disk (GB)</th>
					</tr>
				</thead>
				<tbody>
					{#each data.guests as g (g.cn)}
						<tr class="border-b border-divider">
							<td class="text-ink font-mono">{g.cn}</td>
							<td class="num-inline">{g.usage.vms} / {lim(g.limits?.vms)}</td>
							<td class="num-inline">{g.usage.cores} / {lim(g.limits?.cores)}</td>
							<td class="num-inline">{g.usage.memory_mb} / {lim(g.limits?.memory_mb)}</td>
							<td class="num-inline">{g.usage.disk_gb} / {lim(g.limits?.disk_gb)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{:else}
			<p class="prose-note text-xs">No guests yet — mint an invite below.</p>
		{/if}

		{#if data.invites.length}
			<h3 class="field-label">Invites</h3>
			<p class="prose-note prose-measure text-xs">
				One-time and bound to the CN they were minted for. An invite closes when
				it is redeemed, when it expires, or when you revoke it here.
			</p>
			<table class="data-table text-xs">
				<thead>
					<tr>
						<th class="text-left">Prefix</th>
						<th class="text-left">Guest</th>
						<th class="text-left">Machine</th>
						<th class="text-left">State</th>
						<th class="text-left">Expires</th>
						<th class="text-left"></th>
					</tr>
				</thead>
				<tbody>
					{#each data.invites as inv (inv.id)}
						<tr class="border-b border-divider">
							<td class="font-mono text-muted">{inv.prefix}</td>
							<td class="text-ink font-mono">{inv.cn}</td>
							<td class="num-inline">{inv.caps.cores}c · {inv.caps.memory_mb}MB · {inv.caps.disk_gb}GB</td>
							<td>{inv.state}</td>
							<td class="text-muted">{inv.expires_at}</td>
							<td>
								{#if inv.state === 'open'}
									<button class="btn btn-ghost btn-xs text-danger" on:click={() => revoke(inv.prefix)}>Revoke</button>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	{/if}

	{#if minted}
		<div class="bg-canvas border border-ok-border rounded p-3 space-y-1">
			<p class="text-xs text-ok">
				Invite link for {minted.cn} — copy it now, it is shown once and stored nowhere:
			</p>
			<code class="text-xs text-ink break-all select-all">/invite/{minted.token}</code>
			{#if minted.caps}
				<!-- Which node and template the invite actually got. When the operator
				     named neither, this is the only place the resolved defaults are
				     visible - and the proof that "empty" meant something. -->
				<p class="prose-note text-xs" data-minted-caps>
					Builds from template {minted.caps.template_vmid} on node {minted.caps.node}.
				</p>
			{/if}
			<p class="prose-note text-xs">
				If it is lost, revoke the invite above and mint another - nothing here can
				show it again.
			</p>
		</div>
	{/if}

	<div class="grid gap-6 md:grid-cols-2">
		<div class="section-stack">
			<div class="space-y-1">
				<h3 class="field-label">Mint an invite</h3>
				<p class="prose-note prose-measure text-xs">
					The size below is frozen into the invite when you mint it, so changing a
					default next week never re-points an invite already sitting in someone's
					inbox.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-invite-cn">CN</label>
				<input
					id="guest-invite-cn"
					class="input w-64"
					aria-describedby="guest-invite-cn-note"
					placeholder="friend.example"
					bind:value={invite.cn}
				/>
				<p id="guest-invite-cn-note" class="prose-note prose-measure text-xs">
					The Common Name in your friend's client certificate, exactly as issued.
					The invite is bound to it: no other certificate can redeem it, and the
					machine it builds belongs to that CN.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-invite-template">Template VMID <span class="text-muted">(optional)</span></label>
				<input
					id="guest-invite-template"
					class="input num w-32"
					type="number"
					aria-describedby="guest-invite-template-note"
					placeholder="default"
					bind:value={invite.template_vmid}
				/>
				<p id="guest-invite-template-note" class="prose-note prose-measure text-xs">
					The Proxmox template the machine is cloned from. Leave it empty to use
					this instance's Provisioning default
					(<a class="text-accent hover:underline" href="{base}/settings?tab=subsystems">Subsystems → Provisioning defaults</a>).
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-invite-node">Node <span class="text-muted">(optional)</span></label>
				<input
					id="guest-invite-node"
					class="input w-32"
					aria-describedby="guest-invite-node-note"
					placeholder="default"
					bind:value={invite.node}
				/>
				<p id="guest-invite-node-note" class="prose-note prose-measure text-xs">
					Which Proxmox node builds the machine. Leave it empty to use the
					Provisioning default; the node is resolved and frozen at mint time.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-invite-cores">Cores</label>
				<input
					id="guest-invite-cores"
					class="input num w-20"
					type="number"
					aria-describedby="guest-invite-cores-note"
					bind:value={invite.cores}
				/>
				<p id="guest-invite-cores-note" class="prose-note prose-measure text-xs">
					vCPUs for THIS machine. It is a cap on one machine, not on the friend -
					their total across every machine is the budget on the right.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-invite-memory">Memory (MB)</label>
				<input
					id="guest-invite-memory"
					class="input num w-28"
					type="number"
					aria-describedby="guest-invite-memory-note"
					bind:value={invite.memory_mb}
				/>
				<p id="guest-invite-memory-note" class="prose-note prose-measure text-xs">
					RAM for this machine, in megabytes. The server refuses anything under 256.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-invite-disk">Disk (GB)</label>
				<input
					id="guest-invite-disk"
					class="input num w-20"
					type="number"
					aria-describedby="guest-invite-disk-note"
					bind:value={invite.disk_gb}
				/>
				<p id="guest-invite-disk-note" class="prose-note prose-measure text-xs">
					The machine's total disk, in gigabytes. Proxmox can only grow a
					template's disk, so a value below the template's own size fails the
					build rather than shrinking it.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-invite-ttl">Valid for (days)</label>
				<input
					id="guest-invite-ttl"
					class="input num w-20"
					type="number"
					min="1"
					max="90"
					aria-describedby="guest-invite-ttl-note"
					bind:value={invite.ttl_days}
				/>
				<p id="guest-invite-ttl-note" class="prose-note prose-measure text-xs">
					How long the invite stays redeemable, 1 to 90 days. It is one-time
					either way: redeeming it closes it, and so does expiry.
				</p>
			</div>

			<div class="space-y-1">
				<button class="btn btn-primary text-xs" disabled={working} on:click={mint}>
					{working ? 'Working…' : 'Mint'}
				</button>
				<p class="prose-note prose-measure text-xs">
					Minting shows the invite link once, on this page. HomePilot keeps only a
					prefix and a hash of it.
				</p>
			</div>
		</div>

		<div class="section-stack">
			<div class="space-y-1">
				<h3 class="field-label">Set a budget</h3>
				<p class="prose-note prose-measure text-xs">
					A budget is checked when your friend redeems an invite: over the line and
					redemption stops, with the invite left open so they can free something
					and retry. Lowering a budget never touches machines that already exist.
					A blank axis is unlimited.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-quota-cn">CN</label>
				<input
					id="guest-quota-cn"
					class="input w-64"
					aria-describedby="guest-quota-cn-note"
					placeholder="friend.example"
					bind:value={quota.cn}
				/>
				<p id="guest-quota-cn-note" class="prose-note prose-measure text-xs">
					The same Common Name the invites are minted for. A budget can be set
					before that friend exists; it simply waits for them.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-quota-vms">Machines</label>
				<input
					id="guest-quota-vms"
					class="input num w-20"
					type="number"
					aria-describedby="guest-quota-vms-note"
					placeholder="∞"
					bind:value={quota.max_vms}
				/>
				<p id="guest-quota-vms-note" class="prose-note prose-measure text-xs">
					The most machines this friend may hold at once. Blank = no limit.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-quota-cores">Cores</label>
				<input
					id="guest-quota-cores"
					class="input num w-20"
					type="number"
					aria-describedby="guest-quota-cores-note"
					placeholder="∞"
					bind:value={quota.max_cores}
				/>
				<p id="guest-quota-cores-note" class="prose-note prose-measure text-xs">
					vCPUs summed across every machine they own. Blank = no limit.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-quota-memory">Memory (MB)</label>
				<input
					id="guest-quota-memory"
					class="input num w-28"
					type="number"
					aria-describedby="guest-quota-memory-note"
					placeholder="∞"
					bind:value={quota.max_memory_mb}
				/>
				<p id="guest-quota-memory-note" class="prose-note prose-measure text-xs">
					RAM summed across every machine they own, in megabytes. Blank = no limit.
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="guest-quota-disk">Disk (GB)</label>
				<input
					id="guest-quota-disk"
					class="input num w-20"
					type="number"
					aria-describedby="guest-quota-disk-note"
					placeholder="∞"
					bind:value={quota.max_disk_gb}
				/>
				<p id="guest-quota-disk-note" class="prose-note prose-measure text-xs">
					Disk summed across every machine they own, in gigabytes. Blank = no limit.
				</p>
			</div>

			<div class="space-y-1">
				<button class="btn btn-primary text-xs" disabled={working} on:click={saveQuota}>
					{working ? 'Working…' : 'Save budget'}
				</button>
				<p class="prose-note prose-measure text-xs">
					Saving replaces the whole budget for that CN - an axis left blank becomes
					unlimited again.
				</p>
			</div>
		</div>
	</div>
</div>
