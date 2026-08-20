<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import { api, type Artifact } from '$lib/api';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';

	let items: Artifact[] = [];
	let allItems: Artifact[] = [];
	let loading = true;
	let error = '';
	let filterKind = '';
	let activeTab = 'all';

	const STATUS_TABS: Array<{ key: string; label: string; filter: string | undefined }> = [
		{ key: 'all', label: 'All', filter: undefined },
		{ key: 'proposed', label: 'Proposed', filter: 'proposed' },
		{ key: 'approved', label: 'Approved', filter: 'approved' },
		{ key: 'applied', label: 'Applied', filter: 'applied' },
		{ key: 'failed', label: 'Failed', filter: 'failed' },
		{ key: 'revoked', label: 'Revoked', filter: 'revoked' },
		{ key: 'superseded', label: 'Superseded', filter: 'superseded' },
	];

	const STATUS_CLASSES: Record<string, string> = {
		proposed: 'badge-proposed',
		approved: 'badge-approved',
		applied: 'badge-applied',
		rejected: 'badge-rejected',
		revoked: 'badge-revoked',
		failed: 'badge-failed',
		superseded: 'badge-superseded',
	};
	function statusClass(s: string): string { return STATUS_CLASSES[s] ?? 'badge-proposed'; }

	const kinds = ['', 'kb-note', 'shell-command', 'ansible-playbook', 'http-sequence', 'composite'];

	function buildCounts(artifacts: Artifact[]): Map<string, number> {
		const m = new Map<string, number>();
		m.set('all', artifacts.length);
		for (const item of artifacts) {
			m.set(item.status, (m.get(item.status) ?? 0) + 1);
		}
		return m;
	}

	$: counts = buildCounts(allItems);

	// `initial` shows the loading skeleton; a live SSE-driven refresh stays silent
	// and keeps the last-good rows on screen.
	async function loadAll(initial = true) {
		if (initial) loading = true;
		error = '';
		try {
			const res = await api.listArtifacts({ limit: 1000 });
			allItems = res.items;
			applyFilter();
		} catch (e) {
			if (initial || allItems.length === 0) error = String(e);
		} finally {
			loading = false;
		}
	}

	function clearFilters() {
		activeTab = 'all';
		filterKind = '';
		applyFilter();
	}

	function applyFilter() {
		const tab = STATUS_TABS.find((t) => t.key === activeTab);
		if (!tab) { items = allItems; return; }
		if (tab.filter === undefined) {
			items = allItems;
		} else {
			items = allItems.filter((a) => a.status === tab.filter);
		}
		if (filterKind) {
			items = items.filter((a) => a.kind === filterKind);
		}
	}

	function setTab(key: string) {
		activeTab = key;
		applyFilter();
	}

	function targetStr(a: Artifact): string {
		const t = a.target ?? {};
		return t.host ?? t.service ?? t.node ?? '—';
	}

	function fmtDate(s: string): string {
		return s ? new Date(s).toLocaleDateString() : '—';
	}

	onMount(() => loadAll(true));
	// Live-refresh the list on any artifact lifecycle event, preserving the
	// current tab/kind filter (applyFilter re-runs inside loadAll). Debounced:
	// this refetch pulls up to 1000 rows, and a bulk apply emits one event per
	// artifact — a burst must cost one request, not one per event.
	const refresh = debounce(() => loadAll(false), 400);
	const unsub = onArtifactEvent(refresh);
	onDestroy(() => {
		unsub();
		refresh.cancel();
	});
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Artifacts</h1>
		<button class="btn btn-ghost text-xs" on:click={() => loadAll(true)}>↻ Refresh</button>
	</div>

	<div class="flex gap-1 flex-wrap border-b border-border pb-px">
		{#each STATUS_TABS as tab}
			<button
				class="px-3 py-1.5 text-xs font-medium transition-colors rounded-t relative
				       {activeTab === tab.key ? 'text-accent bg-surface border-t border-x border-border -mb-px' : 'text-muted hover:text-ink'}"
				on:click={() => setTab(tab.key)}
			>
				{tab.label}
				<span class="ml-1 px-1.5 py-0 rounded-full text-[10px] {activeTab === tab.key ? 'bg-accent-tint text-accent-strong' : 'bg-raised text-muted'}">
					{counts.get(tab.key === 'all' ? 'all' : tab.key) ?? 0}
				</span>
			</button>
		{/each}
	</div>

	<div class="flex gap-3 flex-wrap items-center">
		<select class="input text-xs" bind:value={filterKind} on:change={applyFilter}>
			{#each kinds as k}
				<option value={k}>{k || 'All kinds'}</option>
			{/each}
		</select>
		<span class="text-muted text-xs">{items.length} items</span>
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if error}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load artifacts.</p>
			<p class="text-xs text-muted">{error}</p>
			<button class="btn btn-ghost text-xs" on:click={() => loadAll(true)}>↻ Retry</button>
		</div>
	{:else if allItems.length === 0}
		<div class="card p-6 text-center space-y-1">
			<p class="prose-note text-sm">No artifacts yet.</p>
			<p class="prose-note text-xs">Proposed changes will appear here once created.</p>
		</div>
	{:else if items.length === 0}
		<div class="card p-6 text-center space-y-3">
			<p class="prose-note text-sm">No artifacts match the current filters.</p>
			<button class="btn btn-ghost text-xs" on:click={clearFilters}>Clear filters</button>
		</div>
	{:else}
		<div class="card overflow-x-auto">
			<table class="data-table text-xs">
				<thead>
					<tr>
						<th class="text-left pb-2 pr-4">ID</th>
						<th class="text-left pb-2 pr-4">Kind</th>
						<th class="text-left pb-2 pr-4">Intent</th>
						<th class="text-left pb-2 pr-4">Status</th>
						<th class="text-left pb-2 pr-4">Target</th>
						<th class="text-left pb-2">Created</th>
					</tr>
				</thead>
				<tbody>
					{#each items as a (a.id)}
						<tr class="border-b border-divider hover:bg-raised transition-colors">
							<!-- A real link, like Tasks/Journal: keyboard-reachable, opens in a
							     new tab, and readable by a screen reader. The old row-level
							     on:click was mouse-only. -->
							<td class="py-2 pr-4 font-mono">
								<a
									href="{base}/artifacts/{a.id}"
									class="text-accent hover:text-accent-strong"
									title={a.id}
								>{a.id.slice(-8)}</a>
							</td>
							<td class="py-2 pr-4 text-ink">{a.kind}</td>
							<td class="py-2 pr-4 text-ink max-w-xs truncate">{a.intent}</td>
							<td class="py-2 pr-4">
								<span class="badge {statusClass(a.status)}">{a.status}</span>
							</td>
							<td class="py-2 pr-4 text-muted">{targetStr(a)}</td>
							<td class="py-2 text-muted">{fmtDate(a.created_at)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>