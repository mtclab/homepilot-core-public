<script lang="ts">
	import { onMount } from 'svelte';
	import { api, hostMetricsUrl, sessionStore, type Host } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { pruneSelection } from '$lib/selection';
	import { base } from '$app/paths';

	let items: Host[] = [];
	let loading = true;
	let loadError = '';
	let syncing = false;
	let enriching = false;
	let filterRole = '';
	let filterStatus = '';
	let filterSource = '';
	let filterImportState = '';
	let selectedIds: Set<string> = new Set();
	// Sync / Adopt / Ignore / Enrich are all write-scoped server-side. Offering
	// them to a read-only session only produces 403s. Default-deny while loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);

	async function load() {
		loading = true;
		loadError = '';
		try {
			const res = await api.listInventory({
				role: filterRole || undefined,
				status: filterStatus || undefined,
				source: filterSource || undefined,
				import_state: filterImportState || undefined,
			});
			items = res.items;
			// The selection must never outlive the rows it was made on: a filter
			// change used to leave off-screen hosts selected, so "3 selected →
			// Adopt" acted on hosts the user could not see.
			selectedIds = pruneSelection(selectedIds, items);
		} catch (e) {
			loadError = e instanceof Error ? e.message : String(e);
			notify(loadError, 'err');
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

	$: hasActiveFilters =
		filterRole !== '' || filterStatus !== '' || filterSource !== '' || filterImportState !== '';

	function clearFilters() {
		filterRole = '';
		filterStatus = '';
		filterSource = '';
		filterImportState = '';
		load();
	}

	function statusColor(s?: string): string {
		if (s === 'online') return 'text-ok';
		if (s === 'offline') return 'text-danger';
		return 'text-muted';
	}

	function sourceBadge(source?: string): string {
		if (source === 'hp_created') return 'HP';
		if (source === 'imported') return 'Imported';
		if (source === 'discovered') return 'Discovered';
		return '';
	}

	function sourceClass(source?: string): string {
		if (source === 'hp_created') return 'bg-accent-tint text-accent-strong';
		if (source === 'imported') return 'bg-ok-tint text-ok';
		if (source === 'discovered') return 'bg-warn-tint text-warn';
		return 'bg-raised text-ink';
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
		// Belt and braces: act only on rows that are on screen right now, whatever
		// happened to `items` since the selection was made.
		const ids = Array.from(pruneSelection(selectedIds, items));
		if (ids.length === 0) return;
		try {
			const res = await api.bulkInventory(action, ids);
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

	onMount(async () => {
		await load();
	});
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Inventory</h1>
		<div class="flex gap-2">
			<button class="btn btn-ghost text-xs" on:click={load} disabled={loading}>↻ Reload</button>
			{#if canWrite}
				<button class="btn btn-ghost text-xs" on:click={sync} disabled={syncing}>
					{syncing ? 'Syncing…' : '⟳ Sync from Proxmox'}
				</button>
			{/if}
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
		{#if hasActiveFilters}
			<button class="btn btn-ghost text-xs self-center" on:click={clearFilters}>Clear filters</button>
		{/if}
		<span class="text-muted text-xs self-center">{items.length} hosts</span>
	</div>

	{#if selectedIds.size > 0}
		<div class="flex gap-2 items-center">
			<span class="text-xs text-ink">{selectedIds.size} selected</span>
			{#if canWrite}
				<button class="btn btn-sm text-xs" on:click={() => bulk('adopt')}>Adopt</button>
				<button class="btn btn-sm text-xs" on:click={() => bulk('ignore')}>Ignore</button>
				<button class="btn btn-sm text-xs" on:click={() => bulk('enrich')}>Enrich</button>
			{/if}
			<button class="btn btn-sm btn-ghost text-xs" on:click={() => (selectedIds = new Set())}>Clear</button>
		</div>
	{/if}

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load inventory.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Retry</button>
		</div>
	{:else if items.length === 0 && hasActiveFilters}
		<div class="card p-6 text-center space-y-3">
			<p class="prose-note text-sm">No hosts match the current filters.</p>
			<button class="btn btn-ghost text-xs" on:click={clearFilters}>Clear filters</button>
		</div>
	{:else if items.length === 0}
		<div class="card p-6 text-center space-y-1">
			<p class="prose-note text-sm">No hosts in inventory yet.</p>
			<p class="prose-note text-xs">Sync from Proxmox to discover hosts.</p>
		</div>
	{:else}
		<div class="card overflow-x-auto">
			<table class="data-table text-xs">
				<thead>
					<tr>
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
						<tr class="border-b border-divider hover:bg-raised transition-colors">
							<td class="py-2 pr-2"><input type="checkbox" checked={selectedIds.has(h.id)} on:change={() => toggleSelect(h.id)} /></td>
							<td class="py-2 pr-4">
								<a
									href="{base}/inventory/{h.id}"
									class="text-accent hover:text-accent-strong font-mono"
								>{h.hostname}</a>
							</td>
							<td class="py-2 pr-4 text-muted font-mono">{h.ip_address || '—'}</td>
							<td class="py-2 pr-4 text-ink">
								{h.role ?? '—'}
								{#if h.role_source === 'inferred'}<span class="text-warn" title="Inferred">?</span>{/if}
							</td>
							<td class="py-2 pr-4 {statusColor(h.status)}">{h.status ?? 'unknown'}</td>
							<td class="py-2 pr-4">
								{#if h.source}
									<span class="px-1.5 py-0.5 rounded text-[10px] font-medium {sourceClass(h.source)}">{sourceBadge(h.source)}</span>
								{:else}
									<span class="text-muted">—</span>
								{/if}
							</td>
							<td class="py-2 pr-4">
								{#if canWrite && h.source === 'discovered' && h.import_state !== 'ignored'}
									<button class="btn btn-xs text-[10px]" on:click={() => adoptOne(h)}>Adopt</button>
									<button class="btn btn-xs text-[10px]" on:click={() => ignoreOne(h)}>Ignore</button>
								{:else if h.import_state === 'ignored'}
									<span class="text-muted">Ignored</span>
								{:else}
									<span class="text-muted">—</span>
								{/if}
								{#if h.hostname}
									<a
										class="text-accent hover:text-accent-strong text-[10px] ml-1"
										href={hostMetricsUrl(base, h.hostname)}
										title="Open this host's recent metrics">Metrics</a
									>
								{/if}
							</td>
							<td class="py-2 text-muted">{h.node ?? '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
