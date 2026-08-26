<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type AuditEntry } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import { debounce } from '$lib/debounce';
	import { groupByDay } from '$lib/grouping';

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
			// A new result set invalidates whichever row was expanded: that id may
			// not even be on this page any more.
			openId = null;
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

	// Inside a day group the date is already in the header, so a row only needs
	// the clock time.
	function fmtTime(s: string): string {
		if (!s) return '—';
		const d = new Date(s);
		if (Number.isNaN(d.getTime())) return s;
		try {
			return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
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

	// Progressive disclosure: an entry is one line until it is asked to be more.
	// One open at a time - the detail is a definition list several lines tall,
	// and fifty of those expanded is the wall this slice exists to remove.
	let openId: number | null = null;
	function toggle(id: number) {
		openId = openId === id ? null : id;
	}
	/** The one-line gist of an entry: who/what it touched, without the detail. */
	function summaryOf(e: AuditEntry): string {
		return [e.user_id, e.target_host, e.target_service].filter(Boolean).join(' · ');
	}

	/** Rows of the expanded detail, skipping the fields this entry has nothing for. */
	function detailRows(e: AuditEntry): [string, string][] {
		const rows: [string, string][] = [
			['Time', fmtTs(e.timestamp)],
			['User', e.user_id || '—'],
			['Source', e.source],
		];
		if (e.target_host) rows.push(['Host', e.target_host]);
		if (e.target_service) rows.push(['Service', e.target_service]);
		if (e.command) rows.push(['Command', e.command]);
		if (e.exit_code !== null) rows.push(['Exit code', String(e.exit_code)]);
		if (e.duration_ms !== null) rows.push(['Duration', `${e.duration_ms} ms`]);
		if (e.snapshot_id) rows.push(['Snapshot', e.snapshot_id]);
		const details = parseDetails(e.details_json);
		if (details) rows.push(['Details', details]);
		return rows;
	}

	// The journal arrives newest-first and paginated, so a page is grouped as it
	// stands: no group is ever split across a page boundary in the wrong order.
	$: dayGroups = groupByDay(entries, (e) => e.timestamp);

	$: totalPages = Math.ceil(total / PAGE_SIZE);
	$: hasNext = page + 1 < totalPages;
	$: hasPrev = page > 0;

	onMount(load);
</script>

<div class="page-stack">
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
		{#each dayGroups as group (group.key)}
			<section class="section-stack" aria-labelledby="journal-group-{group.key || 'undated'}">
				<h2 class="section-title" id="journal-group-{group.key || 'undated'}">
					{group.label} <span class="text-muted font-normal num-inline">({group.items.length})</span>
				</h2>
				<ul class="card divide-y divide-divider">
					{#each group.items as e (e.id)}
						<li>
							<div class="flex items-baseline gap-2 px-3 py-1.5 text-xs">
								<button
									class="flex items-baseline gap-2 flex-1 min-w-0 text-left hover:text-ink-strong"
									aria-expanded={openId === e.id}
									on:click={() => toggle(e.id)}
								>
									<span class="text-muted num-inline whitespace-nowrap">{fmtTime(e.timestamp)}</span>
									<span class="badge {actionClass(e.action)}">{e.action}</span>
									<span class={sourceClass(e.source)}>{e.source}</span>
									<span class="text-muted truncate">{summaryOf(e)}</span>
									<span class="text-muted ml-auto pl-2" aria-hidden="true"
									>{openId === e.id ? '▾' : '▸'}</span>
								</button>
								{#if e.artifact_id}
									<a
										href="{base}/changes/{e.artifact_id}"
										class="text-accent hover:text-accent-strong font-mono whitespace-nowrap"
										title={e.artifact_id}
									>{fmtShortId(e.artifact_id)}</a>
									<button
										class="text-muted hover:text-accent-strong"
										title="Filter journal by this artifact"
										on:click={() => filterByArtifact(e.artifact_id ?? '')}
									>⊃</button>
								{/if}
							</div>
							{#if openId === e.id}
								<dl class="px-3 pb-3 pt-1 grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1 text-xs">
									{#each detailRows(e) as [label, value]}
										<dt class="text-muted">{label}</dt>
										<dd class="text-ink break-words font-mono">{value}</dd>
									{/each}
								</dl>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/each}

		<div class="flex gap-3 items-center">
			<button class="btn btn-ghost text-xs" disabled={!hasPrev} on:click={() => { page--; load(); }}>← Prev</button>
			<span class="text-muted text-xs">Page {page + 1} of {totalPages || 1}</span>
			<button class="btn btn-ghost text-xs" disabled={!hasNext} on:click={() => { page++; load(); }}>Next →</button>
		</div>
	{/if}
</div>