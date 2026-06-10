<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type Host } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';

	let items: Host[] = [];
	let loading = true;
	let syncing = false;
	let enriching = false;
	let filterRole = '';
	let filterStatus = '';
	let filterSource = '';
	let filterImportState = '';
	let selectedIds: Set<string> = new Set();

	async function load() {
		loading = true;
		try {
			const res = await api.listInventory({
				role: filterRole || undefined,
				status: filterStatus || undefined,
				source: filterSource || undefined,
				import_state: filterImportState || undefined,
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
			await enrich();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			syncing = false;
		}
	}

	async function enrich() {
		enriching = true;
		try {
			const res = await api.enrichInventory();
			notify(`Enriched ${res.enriched}, failed ${res.failed}, skipped ${res.skipped}`, 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			enriching = false;
		}
	}

	function statusColor(s?: string): string {
		if (s === 'online') return 'text-green-400';
		if (s === 'offline') return 'text-red-400';
		return 'text-slate-400';
	}

	function sourceBadge(source?: string): string {
		if (source === 'hp_created') return 'HP';
		if (source === 'imported') return 'Imported';
		if (source === 'discovered') return 'Discovered';
		return '';
	}

	function sourceClass(source?: string): string {
		if (source === 'hp_created') return 'bg-sky-600/20 text-sky-300';
		if (source === 'imported') return 'bg-emerald-600/20 text-emerald-300';
		if (source === 'discovered') return 'bg-amber-600/20 text-amber-300';
		return 'bg-slate-600/20 text-slate-300';
	}

	function toggleSelect(id: string) {
		const next = new Set(selectedIds);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selectedIds = next;
	}

	function toggleSelectAll() {
		if (selectedIds.size === items.length) {
			selectedIds = new Set();
		} else {
			selectedIds = new Set(items.map((h) => h.id));
		}
	}

	async function bulk(action: string) {
		if (selectedIds.size === 0) return;
		try {
			const res = await api.bulkInventory(action, Array.from(selectedIds));
			notify(`${action}: ${res.succeeded} succeeded, ${res.failed} failed`, 'ok');
			selectedIds = new Set();
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function adoptOne(host: Host) {
		try {
			await api.adoptHost(host.id);
			notify('Adopted', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function ignoreOne(host: Host) {
		try {
			await api.ignoreHost(host.id);
			notify('Ignored', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
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
			<option value="database">database</option>
			<option value="web-server">web-server</option>
			<option value="api-server">api-server</option>
			<option value="monitoring">monitoring</option>
			<option value="control-plane">control-plane</option>
			<option value="worker">worker</option>
		</select>
		<select class="input text-xs" bind:value={filterStatus} on:change={load}>
			<option value="">All statuses</option>
			<option value="online">online</option>
			<option value="offline">offline</option>
			<option value="unknown">unknown</option>
		</select>
		<select class="input text-xs" bind:value={filterSource} on:change={load}>
			<option value="">All sources</option>
			<option value="hp_created">HP-Created</option>
			<option value="discovered">Discovered</option>
			<option value="imported">Imported</option>
		</select>
		<select class="input text-xs" bind:value={filterImportState} on:change={load}>
			<option value="">All import states</option>
			<option value="pending">Pending</option>
			<option value="adopted">Adopted</option>
			<option value="ignored">Ignored</option>
		</select>
		<span class="text-slate-500 text-xs self-center">{items.length} hosts</span>
	</div>

	{#if selectedIds.size > 0}
		<div class="flex gap-2 items-center">
			<span class="text-xs text-slate-300">{selectedIds.size} selected</span>
			<button class="btn btn-sm text-xs" on:click={() => bulk('adopt')}>Adopt</button>
			<button class="btn btn-sm text-xs" on:click={() => bulk('ignore')}>Ignore</button>
			<button class="btn btn-sm text-xs" on:click={() => bulk('enrich')}>Enrich</button>
			<button class="btn btn-sm btn-ghost text-xs" on:click={() => (selectedIds = new Set())}>Clear</button>
		</div>
	{/if}

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if items.length === 0}
		<p class="text-slate-500 text-sm">No hosts in inventory.</p>
	{:else}
		<div class="card overflow-x-auto">
			<table class="w-full text-xs">
				<thead>
					<tr class="text-slate-400 border-b border-slate-700">
						<th class="text-left pb-2 pr-2"><input type="checkbox" on:change={toggleSelectAll} checked={selectedIds.size > 0 && selectedIds.size === items.length} /></th>
						<th class="text-left pb-2 pr-4">Hostname</th>
						<th class="text-left pb-2 pr-4">IP</th>
						<th class="text-left pb-2 pr-4">Role</th>
						<th class="text-left pb-2 pr-4">Status</th>
						<th class="text-left pb-2 pr-4">Source</th>
						<th class="text-left pb-2 pr-4">Actions</th>
						<th class="text-left pb-2">Node</th>
					</tr>
				</thead>
				<tbody>
					{#each items as h}
						<tr class="border-b border-slate-700/50 hover:bg-slate-700/30 transition-colors">
							<td class="py-2 pr-2"><input type="checkbox" checked={selectedIds.has(h.id)} on:change={() => toggleSelect(h.id)} /></td>
							<td class="py-2 pr-4">
								<a
									href="{base}/inventory/{h.id}"
									class="text-sky-400 hover:text-sky-300 font-mono"
								>{h.hostname}</a>
							</td>
							<td class="py-2 pr-4 text-slate-400 font-mono">{h.ip_address || '—'}</td>
							<td class="py-2 pr-4 text-slate-300">
								{h.role ?? '—'}
								{#if h.role_source === 'inferred'}<span class="text-amber-400" title="Inferred">?</span>{/if}
							</td>
							<td class="py-2 pr-4 {statusColor(h.status)}">{h.status ?? 'unknown'}</td>
							<td class="py-2 pr-4">
								{#if h.source}
									<span class="px-1.5 py-0.5 rounded text-[10px] font-medium {sourceClass(h.source)}">{sourceBadge(h.source)}</span>
								{:else}
									<span class="text-slate-600">—</span>
								{/if}
							</td>
							<td class="py-2 pr-4">
								{#if h.source === 'discovered' && h.import_state !== 'ignored'}
									<button class="btn btn-xs text-[10px]" on:click={() => adoptOne(h)}>Adopt</button>
									<button class="btn btn-xs text-[10px]" on:click={() => ignoreOne(h)}>Ignore</button>
								{:else if h.import_state === 'ignored'}
									<span class="text-slate-500">Ignored</span>
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
