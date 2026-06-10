<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type AuditEntry } from '$lib/api';
	import { notify } from '$lib/stores';

	let entries: AuditEntry[] = [];
	let loading = true;
	let filterAction = '';
	let filterSource = '';
	let filterArtifactId = '';
	let page = 0;
	let total = 0;
	const PAGE_SIZE = 50;

	const ACTIONS = ['', 'propose', 'approve', 'apply', 'revoke', 'reject', 'replay', 'drift_check'];
	const SOURCES = ['', 'cli', 'ui', 'mcp', 'reconciler', 'system'];

	const ACTION_COLORS: Record<string, string> = {
		propose: 'badge-proposed',
		approve: 'badge-approved',
		apply: 'badge-applied',
		revoke: 'badge-revoked',
		reject: 'badge-rejected',
		replay: 'badge-superseded',
		drift_check: 'bg-blue-900/50 text-blue-300',
	};

	const SOURCE_COLORS: Record<string, string> = {
		cli: 'text-yellow-400',
		ui: 'text-sky-400',
		mcp: 'text-purple-400',
		reconciler: 'text-emerald-400',
		system: 'text-slate-400',
	};

	function actionClass(a: string): string { return ACTION_COLORS[a] ?? 'badge-proposed'; }
	function sourceClass(s: string): string { return SOURCE_COLORS[s] ?? 'text-slate-400'; }

	async function load() {
		loading = true;
		try {
			const res = await api.listAudit({
				action: filterAction || undefined,
				artifact_id: filterArtifactId || undefined,
				source: filterSource || undefined,
				limit: PAGE_SIZE,
				offset: page * PAGE_SIZE,
			});
			entries = res.items;
			total = res.total;
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			loading = false;
		}
	}

	function fmtTs(s: string): string {
		if (!s) return '—';
		try {
			return new Date(s).toLocaleString();
		} catch {
			return s;
		}
	}

	function fmtShortId(id: string | null): string {
		if (!id) return '—';
		return id.length > 8 ? id.slice(-8) : id;
	}

	function parseDetails(json: string | null): string {
		if (!json) return '';
		try {
			const obj = JSON.parse(json);
			return Object.entries(obj).map(([k, v]) => `${k}=${v}`).join(', ');
		} catch {
			return json;
		}
	}

	$: totalPages = Math.ceil(total / PAGE_SIZE);
	$: hasNext = page + 1 < totalPages;
	$: hasPrev = page > 0;

	onMount(load);
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="text-lg font-bold text-slate-100">Journal</h1>
		<button class="btn btn-ghost text-xs" on:click={load}>↻ Refresh</button>
	</div>

	<div class="flex gap-3 flex-wrap items-center">
		<select class="input text-xs" bind:value={filterAction} on:change={() => { page = 0; load(); }}>
			{#each ACTIONS as a}
				<option value={a}>{a || 'All actions'}</option>
			{/each}
		</select>
		<select class="input text-xs" bind:value={filterSource} on:change={() => { page = 0; load(); }}>
			{#each SOURCES as s}
				<option value={s}>{s || 'All sources'}</option>
			{/each}
		</select>
		<input
			class="input text-xs flex-1 max-w-xs"
			placeholder="Artifact ID…"
			bind:value={filterArtifactId}
			on:change={() => { page = 0; load(); }}
		/>
		<span class="text-slate-500 text-xs">{total} entries</span>
	</div>

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if entries.length === 0}
		<p class="text-slate-500 text-sm">No audit entries.</p>
	{:else}
		<div class="card overflow-x-auto">
			<table class="w-full text-xs">
				<thead>
					<tr class="text-slate-400 border-b border-slate-700">
						<th class="text-left pb-2 pr-4">Time</th>
						<th class="text-left pb-2 pr-4">Action</th>
						<th class="text-left pb-2 pr-4">Source</th>
						<th class="text-left pb-2 pr-4">User</th>
						<th class="text-left pb-2 pr-4">Artifact</th>
						<th class="text-left pb-2 pr-4">Host</th>
						<th class="text-left pb-2">Details</th>
					</tr>
				</thead>
				<tbody>
					{#each entries as e}
						<tr class="border-b border-slate-700/50 hover:bg-slate-700/30 transition-colors">
							<td class="py-1.5 pr-4 text-slate-400 whitespace-nowrap">{fmtTs(e.timestamp)}</td>
							<td class="py-1.5 pr-4"><span class="badge {actionClass(e.action)}">{e.action}</span></td>
							<td class="py-1.5 pr-4 {sourceClass(e.source)} font-mono">{e.source}</td>
							<td class="py-1.5 pr-4 text-slate-300">{e.user_id || '—'}</td>
							<td class="py-1.5 pr-4 text-sky-400 font-mono">{fmtShortId(e.artifact_id)}</td>
							<td class="py-1.5 pr-4 text-slate-400 font-mono">{e.target_host || '—'}</td>
							<td class="py-1.5 text-slate-500 truncate max-w-xs">{parseDetails(e.details_json)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>

		<div class="flex gap-3 items-center">
			<button class="btn btn-ghost text-xs" disabled={!hasPrev} on:click={() => { page--; load(); }}>← Prev</button>
			<span class="text-slate-500 text-xs">Page {page + 1} of {totalPages || 1}</span>
			<button class="btn btn-ghost text-xs" disabled={!hasNext} on:click={() => { page++; load(); }}>Next →</button>
		</div>
	{/if}
</div>