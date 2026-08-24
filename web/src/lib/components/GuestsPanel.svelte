<script lang="ts">
	// Guests (#442 G3): budgets and invites, from the console instead of the CLI.
	// The minted token is shown ONCE, here, and stored nowhere.
	import { onMount } from 'svelte';
	import { api, type GuestOverview } from '$lib/api';
	import { notify } from '$lib/stores';

	let data: GuestOverview | null = null;
	let loading = true;
	let loadError = '';
	let mintedToken: { cn: string; token: string } | null = null;

	let invite = { cn: '', template_vmid: 9000, node: '', cores: 2, memory_mb: 2048, disk_gb: 20, ttl_days: 7 };
	let quota = { cn: '', max_vms: '' as string | number, max_cores: '' as string | number, max_memory_mb: '' as string | number, max_disk_gb: '' as string | number };
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
		if (!invite.cn.trim() || !invite.node.trim()) {
			notify('An invite needs a CN and a node', 'err');
			return;
		}
		working = true;
		try {
			const res = await api.mintGuestInvite({ ...invite, cn: invite.cn.trim(), node: invite.node.trim() });
			mintedToken = { cn: res.cn, token: res.token };
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
	<p class="prose-note text-xs">
		Friends with a client certificate get the guest portal: their machines, power
		buttons, and their budget — nothing else. Invites are one-time and CN-bound;
		budgets cap the TOTAL across all of a guest's machines.
	</p>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<p class="text-danger text-sm">{loadError}</p>
	{:else if data}
		{#if data.guests.length}
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

	{#if mintedToken}
		<div class="bg-canvas border border-ok-border rounded p-3">
			<p class="text-xs text-ok mb-1">
				Invite link for {mintedToken.cn} — copy now, it is shown once:
			</p>
			<code class="text-xs text-ink break-all select-all">/invite/{mintedToken.token}</code>
		</div>
	{/if}

	<div class="grid gap-4 md:grid-cols-2">
		<div class="space-y-2">
			<h3 class="field-label">Mint an invite</h3>
			<div class="flex flex-wrap gap-2">
				<input class="input w-32" placeholder="CN" bind:value={invite.cn} />
				<input class="input num w-24" type="number" placeholder="template" bind:value={invite.template_vmid} title="Template VMID" />
				<input class="input w-24" placeholder="node" bind:value={invite.node} />
				<input class="input num w-16" type="number" bind:value={invite.cores} title="Cores" />
				<input class="input num w-24" type="number" bind:value={invite.memory_mb} title="Memory MB" />
				<input class="input num w-20" type="number" bind:value={invite.disk_gb} title="Disk GB" />
				<button class="btn btn-primary text-xs" disabled={working} on:click={mint}>Mint</button>
			</div>
		</div>
		<div class="space-y-2">
			<h3 class="field-label">Set a budget (blank = unlimited)</h3>
			<div class="flex flex-wrap gap-2">
				<input class="input w-32" placeholder="CN" bind:value={quota.cn} />
				<input class="input num w-16" type="number" placeholder="VMs" bind:value={quota.max_vms} title="Max machines" />
				<input class="input num w-16" type="number" placeholder="cores" bind:value={quota.max_cores} title="Max cores" />
				<input class="input num w-24" type="number" placeholder="mem MB" bind:value={quota.max_memory_mb} title="Max memory MB" />
				<input class="input num w-20" type="number" placeholder="disk GB" bind:value={quota.max_disk_gb} title="Max disk GB" />
				<button class="btn btn-primary text-xs" disabled={working} on:click={saveQuota}>Save</button>
			</div>
		</div>
	</div>
</div>
