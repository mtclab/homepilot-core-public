<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import { api, sessionStore, type Artifact } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';

	// Proposing is a write. Default-deny while the session is still loading, so
	// the button never appears for someone who will only get a 403.
	$: canWrite = capCanWrite($sessionStore?.capabilities);

	// Kinds a person can sensibly author by hand here. `composite` is omitted on
	// purpose: it references other artifacts by id, which is a picker, not a
	// textarea, and offering it as free text invites a proposal that cannot apply.
	const CREATABLE_KINDS = ['kb-note', 'host-provision', 'http-sequence', 'shell-script'];
	const IDEMPOTENCE = ['via-precheck', 'declared-natural', 'replay-only'];
	const TARGET_KINDS = ['vm', 'lxc', 'node', 'cluster', 'service', 'network', 'global'];

	const BODY_HINTS: Record<string, string> = {
		'host-provision':
			'```yaml host-provision-spec\npackages:\n  - nginx\nservices:\n  - name: nginx\n    state: started\n```',
		'http-sequence': '```yaml http-sequence\nsteps:\n  - method: GET\n    path: /health\n```',
		'shell-script': '```bash\nsystemctl status nginx\n```',
		'kb-note': 'What this documents, in prose.',
	};

	let showNew = false;
	let submitting = false;
	let newError = '';
	let form = {
		kind: 'kb-note',
		intent: '',
		idempotence: 'via-precheck',
		targetKind: 'service',
		host: '',
		node: '',
		body: '',
	};

	async function submitNew() {
		newError = '';
		submitting = true;
		try {
			const spec: Parameters<typeof api.proposeArtifact>[0] = {
				kind: form.kind,
				intent: form.intent.trim(),
				body: form.body,
			};
			if (form.kind !== 'kb-note') {
				spec.idempotence = form.idempotence;
				// Only the fields the operator actually filled: Target validates
				// its sub-fields per kind, so posting empty strings would fail on
				// something they never typed.
				spec.target = {
					kind: form.targetKind,
					...(form.host.trim() ? { host: form.host.trim() } : {}),
					...(form.node.trim() ? { node: form.node.trim() } : {}),
				};
			}
			const res = await api.proposeArtifact(spec);
			notify(`Proposed ${res.id}`);
			showNew = false;
			form = { ...form, intent: '', body: '' };
			await loadAll(true);
		} catch (e) {
			newError = e instanceof Error ? e.message : String(e);
		} finally {
			submitting = false;
		}
	}

	let items: Artifact[] = [];
	let allItems: Artifact[] = [];
	let loading = true;
	let error = '';
	let filterKind = '';
	// Searched on the SERVER (#445 A4). The page fetches a capped 1000 rows, so a
	// filter applied in the browser can only ever search the rows it already
	// pulled - which is exactly the moment a search stops being useful.
	let search = '';
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
			const res = await api.listArtifacts({ limit: 1000, q: search.trim() || undefined });
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
		if (search) {
			search = '';
			loadAll(false);
			return;
		}
		applyFilter();
	}

	// The query goes to the server, so it is debounced: one request per pause in
	// typing, not one per keystroke.
	const searchAgain = debounce(() => loadAll(false), 300);

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
		<div class="flex gap-2">
			{#if canWrite}
				<button
					class="btn btn-primary text-xs"
					on:click={() => (showNew = !showNew)}
				>{showNew ? 'Cancel' : '+ New artifact'}</button>
			{/if}
			<button class="btn btn-ghost text-xs" on:click={() => loadAll(true)}>↻ Refresh</button>
		</div>
	</div>

	{#if showNew}
		<!-- Until now artifacts could only arrive over MCP or the CLI, so the web
		     UI could review and approve work but never originate it (#445 A2). -->
		<form class="card space-y-3" on:submit|preventDefault={submitNew}>
			<div class="grid gap-3 sm:grid-cols-2">
				<label class="space-y-1">
					<span class="text-xs text-muted">Kind</span>
					<select class="input w-full text-sm" bind:value={form.kind}>
						{#each CREATABLE_KINDS as k}<option value={k}>{k}</option>{/each}
					</select>
				</label>
				<label class="space-y-1">
					<span class="text-xs text-muted">Idempotence</span>
					<select
						class="input w-full text-sm"
						bind:value={form.idempotence}
						disabled={form.kind === 'kb-note'}
					>
						{#each IDEMPOTENCE as i}<option value={i}>{i}</option>{/each}
					</select>
				</label>
			</div>

			<label class="space-y-1 block">
				<span class="text-xs text-muted">Intent (what this is for, 1-200 chars)</span>
				<input class="input w-full text-sm" bind:value={form.intent} maxlength="200" />
			</label>

			{#if form.kind !== 'kb-note'}
				<div class="grid gap-3 sm:grid-cols-3">
					<label class="space-y-1">
						<span class="text-xs text-muted">Target kind</span>
						<select class="input w-full text-sm" bind:value={form.targetKind}>
							{#each TARGET_KINDS as t}<option value={t}>{t}</option>{/each}
						</select>
					</label>
					<label class="space-y-1">
						<span class="text-xs text-muted">Host</span>
						<input class="input w-full text-sm" bind:value={form.host} placeholder="web01" />
					</label>
					<label class="space-y-1">
						<span class="text-xs text-muted">Node / service</span>
						<input class="input w-full text-sm" bind:value={form.node} placeholder="pve1" />
					</label>
				</div>
			{/if}

			<label class="space-y-1 block">
				<span class="text-xs text-muted">Body</span>
				<textarea
					class="input w-full text-xs font-mono h-48"
					bind:value={form.body}
					placeholder={BODY_HINTS[form.kind] ?? ''}
				></textarea>
			</label>

			{#if newError}
				<!-- Inline, next to the button that failed. A toast would take the
				     reason away while the operator is still looking at the form. -->
				<p class="text-xs text-danger">{newError}</p>
			{/if}

			<div class="flex items-center gap-2">
				<button class="btn btn-primary text-xs" disabled={submitting} type="submit">
					{submitting ? 'Proposing…' : 'Propose'}
				</button>
				<span class="text-xs text-muted">
					It lands in Review as <em>proposed</em>; nothing runs until it is approved.
				</span>
			</div>
		</form>
	{/if}

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
		<label class="flex items-center gap-2">
			<span class="sr-only">Search artifacts</span>
			<input
				class="input text-xs w-64"
				type="search"
				placeholder="Search id, intent, target, tags…"
				bind:value={search}
				on:input={searchAgain}
			/>
		</label>
		<select class="input text-xs" bind:value={filterKind} on:change={applyFilter}>
			{#each kinds as k}
				<option value={k}>{k || 'All kinds'}</option>
			{/each}
		</select>
		{#if search || filterKind || activeTab !== 'all'}
			<button class="btn btn-ghost text-xs" on:click={clearFilters}>Clear filters</button>
		{/if}
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
	{:else if allItems.length === 0 && search.trim()}
		<!-- `allItems` is now the SEARCH RESULT, so an empty one must not read as
		     an empty system - that is the same "a failure shown as nothing here"
		     mistake as #445 B4, one step removed. -->
		<div class="card p-6 text-center space-y-3">
			<p class="prose-note text-sm">No artifacts match “{search.trim()}”.</p>
			<button class="btn btn-ghost text-xs" on:click={clearFilters}>Clear search</button>
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
						<th class="text-left">ID</th>
						<th class="text-left">Kind</th>
						<th class="text-left">Intent</th>
						<th class="text-left">Status</th>
						<th class="text-left">Target</th>
						<th class="text-left">Created</th>
					</tr>
				</thead>
				<tbody>
					{#each items as a (a.id)}
						<tr class="border-b border-divider hover:bg-raised transition-colors">
							<!-- A real link, like Tasks/Journal: keyboard-reachable, opens in a
							     new tab, and readable by a screen reader. The old row-level
							     on:click was mouse-only. -->
							<td class="font-mono">
								<a
									href="{base}/artifacts/{a.id}"
									class="text-accent hover:text-accent-strong"
									title={a.id}
								>{a.id.slice(-8)}</a>
							</td>
							<td class="text-ink">{a.kind}</td>
							<td class="text-ink max-w-xs truncate">{a.intent}</td>
							<td>
								<span class="badge {statusClass(a.status)}">{a.status}</span>
							</td>
							<td class="text-muted">{targetStr(a)}</td>
							<td class="text-muted">{fmtDate(a.created_at)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>