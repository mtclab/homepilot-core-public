<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type Host } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';

	let items: Host[] = [];
	let loading = true;
	let syncing = false;
	let filterRole = '';
	let filterStatus = '';

	async function load() {
		loading = true;
		try {
			const res = await api.listInventory({
				role: filterRole || undefined,
				status: filterStatus || undefined,
			});
			items = res.items;
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			loading = false;
		}
	}

	async function sync() {
		syncing = true;
		try {
			const res = await api.refreshInventory();
			notify(`Synced: ${res.hosts} hosts, ${res.services} services`, 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			syncing = false;
		}
	}

	function statusColor(s?: string): string {
		if (s === 'online') return 'text-green-400';
		if (s === 'offline') return 'text-red-400';
		return 'text-slate-400';
	}

	onMount(load);
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="text-lg font-bold text-slate-100">Inventory</h1>
		<div class="flex gap-2">
			<button class="btn btn-ghost text-xs" on:click={load} disabled={loading}>↻ Reload</button>
			<button class="btn btn-ghost text-xs" on:click={sync} disabled={syncing}>
				{syncing ? 'Syncing…' : '⟳ Sync from Proxmox'}
			</button>
		</div>
	</div>

	<div class="flex gap-3 flex-wrap">
		<select class="input text-xs" bind:value={filterRole} on:change={load}>
			<option value="">All roles</option>
			<option value="hypervisor">hypervisor</option>
			<option value="vm">vm</option>
			<option value="container">container</option>
			<option value="service">service</option>
		</select>
		<select class="input text-xs" bind:value={filterStatus} on:change={load}>
			<option value="">All statuses</option>
			<option value="online">online</option>
			<option value="offline">offline</option>
			<option value="unknown">unknown</option>
		</select>
		<span class="text-slate-500 text-xs self-center">{items.length} hosts</span>
	</div>

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if items.length === 0}
		<p class="text-slate-500 text-sm">No hosts in inventory.</p>
	{:else}
		<div class="card overflow-x-auto">
			<table class="w-full text-xs">
				<thead>
					<tr class="text-slate-400 border-b border-slate-700">
						<th class="text-left pb-2 pr-4">Hostname</th>
						<th class="text-left pb-2 pr-4">IP</th>
						<th class="text-left pb-2 pr-4">Role</th>
						<th class="text-left pb-2 pr-4">Status</th>
						<th class="text-left pb-2 pr-4">Managed</th>
						<th class="text-left pb-2">Node</th>
					</tr>
				</thead>
				<tbody>
					{#each items as h}
						<tr class="border-b border-slate-700/50 hover:bg-slate-700/30 transition-colors">
							<td class="py-2 pr-4">
								<a
									href="{base}/inventory/{h.id}"
									class="text-sky-400 hover:text-sky-300 font-mono"
								>{h.hostname}</a>
							</td>
							<td class="py-2 pr-4 text-slate-400 font-mono">{h.ip_address || '—'}</td>
							<td class="py-2 pr-4 text-slate-300">{h.role ?? '—'}</td>
							<td class="py-2 pr-4 {statusColor(h.status)}">{h.status ?? 'unknown'}</td>
							<td class="py-2 pr-4">
								{#if h.managed && h.managed !== 0}
									<span class="text-emerald-400">✓</span>
								{:else}
									<span class="text-slate-600">—</span>
								{/if}
							</td>
							<td class="py-2 text-slate-400">{h.node ?? '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
