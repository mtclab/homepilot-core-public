<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type AuditEntry } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import { debounce } from '$lib/debounce';

	let entries: AuditEntry[] = [];
	let loading = true;
	let loadError = '';
	let filterAction = '';
	let filterSource = '';
	let filterArtifactId = '';
	// Free text across the whole trail, searched on the SERVER: the journal is
	// paginated 50 at a time, so anything else searches one page (#445 A4).
	let search = '';
	let page = 0;
	let total = 0;
	const PAGE_SIZE = 50;

	const ACTIONS = [
		'',
		'propose',
		'approve',
		'apply',
		'revoke',
		'reject',
		'replay',
		'drift_check',
		// Inventory lifecycle (#445 A5): adding and forgetting a host are
		// operator decisions about what the estate IS, so they belong in the
		// trail as first-class actions rather than as unfilterable strays.
		'host_added',
		'host_forgotten',
	];
	const SOURCES = ['', 'cli', 'ui', 'mcp', 'reconciler', 'system'];

	const ACTION_COLORS: Record<string, string> = {
		propose: 'badge-proposed',
		approve: 'badge-approved',
		apply: 'badge-applied',
		revoke: 'badge-revoked',
		reject: 'badge-rejected',
		replay: 'badge-superseded',
		drift_check: 'bg-info-tint text-info',
		host_added: 'badge-approved',
		host_forgotten: 'badge-revoked',
	};

	const SOURCE_COLORS: Record<string, string> = {
		cli: 'text-warn',
		ui: 'text-accent',
		mcp: 'text-note',
		reconciler: 'text-ok',
		system: 'text-muted',
	};

	function actionClass(a: string): string { return ACTION_COLORS[a] ?? 'badge-proposed'; }
	function sourceClass(s: string): string { return SOURCE_COLORS[s] ?? 'text-muted'; }

	async function load() {
		loading = true;
		loadError = '';
		try {
			const res = await api.listAudit({
				action: filterAction || undefined,
				artifact_id: filterArtifactId || undefined,
				source: filterSource || undefined,
				q: search.trim() || undefined,
				limit: PAGE_SIZE,
				offset: page * PAGE_SIZE,
			});
			entries = res.items;
			total = res.total;
		} catch (e) {
			// A toast alone left the previous page's rows on screen (or an empty
			// table that read as "no entries") with no way to retry.
			const msg = e instanceof Error ? e.message : String(e);
			if (entries.length === 0) loadError = msg;
			else notify(msg, 'err');
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

	// Populate the artifact-id filter box from a row and reload from page 1.
	function filterByArtifact(id: string) {
		filterArtifactId = id;
		page = 0;
		load();
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

	$: hasActiveFilters =
		filterAction !== '' ||
		filterSource !== '' ||
		filterArtifactId !== '' ||
		search.trim() !== '';

	// A new query is a new result set, so it always restarts at page 1 - leaving
	// the offset behind would show page 3 of a 1-page result: an empty table.
	const searchAgain = debounce(() => {
		page = 0;
		load();
	}, 300);

	function clearFilters() {
		filterAction = '';
		filterSource = '';
		filterArtifactId = '';
		search = '';
		page = 0;
		load();
	}

	$: totalPages = Math.ceil(total / PAGE_SIZE);
	$: hasNext = page + 1 < totalPages;
	$: hasPrev = page > 0;

	onMount(load);
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Journal</h1>
		<button class="btn btn-ghost text-xs" on:click={load}>↻ Refresh</button>
	</div>

	<div class="flex gap-3 flex-wrap items-center">
		<label class="flex items-center gap-2">
			<span class="sr-only">Search the journal</span>
			<input
				class="input text-xs w-64"
				type="search"
				placeholder="Search artifact, host, command, actor…"
				bind:value={search}
				on:input={searchAgain}
			/>
		</label>
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
		{#if hasActiveFilters}
			<button class="btn btn-ghost text-xs" on:click={clearFilters}>Clear filters</button>
		{/if}
		<span class="text-muted text-xs">{total} entries</span>
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load the journal.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Retry</button>
		</div>
	{:else if entries.length === 0 && hasActiveFilters}
		<div class="card p-6 text-center space-y-3">
			<p class="prose-note text-sm">No journal entries match the current filters.</p>
			<button class="btn btn-ghost text-xs" on:click={clearFilters}>Clear filters</button>
		</div>
	{:else if entries.length === 0}
		<div class="card p-6 text-center space-y-1">
			<p class="prose-note text-sm">No journal entries yet.</p>
			<p class="prose-note text-xs">Actions across the system will be recorded here.</p>
		</div>
	{:else}
		<div class="card overflow-x-auto">
			<table class="data-table text-xs">
				<thead>
					<tr>
						<th class="text-left">Time</th>
						<th class="text-left">Action</th>
						<th class="text-left">Source</th>
						<th class="text-left">User</th>
						<th class="text-left">Artifact</th>
						<th class="text-left">Host</th>
						<th class="text-left">Details</th>
					</tr>
				</thead>
				<tbody>
					{#each entries as e}
						<tr class="border-b border-divider hover:bg-raised transition-colors">
							<td class="py-1.5 text-muted whitespace-nowrap">{fmtTs(e.timestamp)}</td>
							<td class="py-1.5"><span class="badge {actionClass(e.action)}">{e.action}</span></td>
							<td class="py-1.5 {sourceClass(e.source)}">{e.source}</td>
							<td class="py-1.5 text-ink">{e.user_id || '—'}</td>
							<td class="py-1.5 font-mono whitespace-nowrap">
								{#if e.artifact_id}
									<a
										href="{base}/artifacts/{e.artifact_id}"
										class="text-accent hover:text-accent-strong"
										title={e.artifact_id}
									>{fmtShortId(e.artifact_id)}</a>
									<button
										class="text-muted hover:text-accent-strong ml-1"
										title="Filter journal by this artifact"
										on:click={() => filterByArtifact(e.artifact_id ?? '')}
									>⊃</button>
								{:else}
									<span class="text-muted">—</span>
								{/if}
							</td>
							<td class="py-1.5 text-muted font-mono">{e.target_host || '—'}</td>
							<td class="py-1.5 text-muted truncate max-w-xs">{parseDetails(e.details_json)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>

		<div class="flex gap-3 items-center">
			<button class="btn btn-ghost text-xs" disabled={!hasPrev} on:click={() => { page--; load(); }}>← Prev</button>
			<span class="text-muted text-xs">Page {page + 1} of {totalPages || 1}</span>
			<button class="btn btn-ghost text-xs" disabled={!hasNext} on:click={() => { page++; load(); }}>Next →</button>
		</div>
	{/if}
</div>