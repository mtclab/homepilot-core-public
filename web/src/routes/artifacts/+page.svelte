<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { api, type Artifact } from '$lib/api';

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

	async function loadAll() {
		loading = true;
		error = '';
		try {
			const res = await api.listArtifacts({ limit: 2000 });
			allItems = res.items;
			applyFilter();
		} catch (e) {
			error = String(e);
		} finally {
			loading = false;
		}
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

	function navigateTo(id: string) {
		goto(`${base}/artifacts/${id}`);
	}

	function targetStr(a: Artifact): string {
		const t = a.target ?? {};
		return t.host ?? t.service ?? t.node ?? '—';
	}

	function fmtDate(s: string): string {
		return s ? new Date(s).toLocaleDateString() : '—';
	}

	onMount(loadAll);
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="text-lg font-bold text-slate-100">Artifacts</h1>
		<button class="btn btn-ghost text-xs" on:click={loadAll}>↻ Refresh</button>
	</div>

	<div class="flex gap-1 flex-wrap border-b border-slate-700 pb-px">
		{#each STATUS_TABS as tab}
			<button
				class="px-3 py-1.5 text-xs font-medium transition-colors rounded-t relative
				       {activeTab === tab.key ? 'text-sky-400 bg-slate-800 border-t border-x border-slate-700 -mb-px' : 'text-slate-500 hover:text-slate-300'}"
				on:click={() => setTab(tab.key)}
			>
				{tab.label}
				<span class="ml-1 px-1.5 py-0 rounded-full text-[10px] {activeTab === tab.key ? 'bg-sky-900 text-sky-300' : 'bg-slate-700 text-slate-500'}">
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
		<span class="text-slate-500 text-xs">{items.length} items</span>
	</div>

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if error}
		<p class="text-red-400 text-sm">{error}</p>
	{:else if items.length === 0}
		<p class="text-slate-500 text-sm">No artifacts.</p>
	{:else}
		<div class="card overflow-x-auto">
			<table class="w-full text-xs">
				<thead>
					<tr class="text-slate-400 border-b border-slate-700">
						<th class="text-left pb-2 pr-4">ID</th>
						<th class="text-left pb-2 pr-4">Kind</th>
						<th class="text-left pb-2 pr-4">Intent</th>
						<th class="text-left pb-2 pr-4">Status</th>
						<th class="text-left pb-2 pr-4">Target</th>
						<th class="text-left pb-2">Created</th>
					</tr>
				</thead>
				<tbody>
					{#each items as a}
						<tr
							class="border-b border-slate-700/50 hover:bg-slate-700/30 cursor-pointer transition-colors"
							on:click={() => navigateTo(a.id)}
						>
							<td class="py-2 pr-4 text-sky-400 font-mono">{a.id.slice(-8)}</td>
							<td class="py-2 pr-4 text-slate-300">{a.kind}</td>
							<td class="py-2 pr-4 text-slate-200 max-w-xs truncate">{a.intent}</td>
							<td class="py-2 pr-4">
								<span class="badge {statusClass(a.status)}">{a.status}</span>
							</td>
							<td class="py-2 pr-4 text-slate-400">{targetStr(a)}</td>
							<td class="py-2 text-slate-500">{fmtDate(a.created_at)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>