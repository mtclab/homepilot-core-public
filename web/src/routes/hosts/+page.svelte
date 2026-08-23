<script lang="ts">
	import { onMount } from 'svelte';
	import { api, hostMetricsUrl, sessionStore, type Host } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import EnrollAgent from '$lib/components/EnrollAgent.svelte';
	import { pruneSelection } from '$lib/selection';
	import { debounce } from '$lib/debounce';
	import { base } from '$app/paths';

	let items: Host[] = [];
	// The list is paged. It used to cap at 100 rows with `total` reporting the
	// PAGE SIZE, so an estate with more hosts than that simply had no page 2
	// (#428).
	let total = 0;
	let page = 0;
	const PAGE_SIZE = 100;
	let loading = true;
	let loadError = '';
	let syncing = false;
	let enriching = false;
	let filterRole = '';
	let filterStatus = '';
	let filterSource = '';
	let filterImportState = '';
	// Searched on the SERVER: the list is paginated, so a filter applied in the
	// browser could only ever search the page already on screen (#445 A4).
	let search = '';

	// The role vocabulary the BACKEND uses: `node` and `guest` are what the sync
	// writes, and the rest are what `ROLE_PATTERNS` in inventory/service.py can
	// infer. One list, used by both the filter and the add form, so the UI cannot
	// offer a role nothing will ever match (#424).
	const ROLES = [
		'node',
		'guest',
		'database',
		'web-server',
		'api-server',
		'monitoring',
		'control-plane',
		'worker',
	];
	let selectedIds: Set<string> = new Set();
	// Enrolment panels, folded in from the retired Agents tab (#514 S4).
	let enrollShow: 'bootstrap' | 'hub' | null = null;
	// Sync / Adopt / Ignore / Enrich are all write-scoped server-side. Offering
	// them to a read-only session only produces 403s. Default-deny while loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);

	// Forgetting is irreversible (services and the observation note go with the
	// host), so it is a two-step confirm inline.
	let forgetConfirm: string | null = null;
	let forgetting: string | null = null;

	async function forgetOne(h: Host) {
		forgetting = h.id;
		try {
			await api.forgetHost(h.id);
			notify(`Removed ${h.hostname} from inventory`);
			forgetConfirm = null;
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			forgetting = null;
		}
	}

	function fmtDate(s: string | null | undefined): string {
		if (!s) return '—';
		try {
			return new Date(s).toLocaleDateString();
		} catch {
			return s;
		}
	}

	// Adding a host by hand: the only way a non-Proxmox machine can enter
	// inventory at all (#445 A5).
	let showAdd = false;
	let adding = false;
	let addForm = { hostname: '', ip_address: '', role: 'guest', description: '' };

	async function submitAdd() {
		adding = true;
		try {
			const host = await api.addHost({
				hostname: addForm.hostname.trim(),
				ip_address: addForm.ip_address.trim() || undefined,
				role: addForm.role,
				description: addForm.description.trim() || undefined,
			});
			notify(`Added ${host.hostname}`);
			showAdd = false;
			addForm = { hostname: '', ip_address: '', role: 'guest', description: '' };
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			adding = false;
		}
	}

	// One request per pause in typing, not one per keystroke - the query goes to
	// the server.
	const searchAgain = debounce(() => {
		page = 0;
		load();
	}, 300);

	function firstPage() {
		page = 0;
		return load();
	}

	async function load() {
		loading = true;
		loadError = '';
		try {
			const res = await api.listInventory({
				role: filterRole || undefined,
				status: filterStatus || undefined,
				source: filterSource || undefined,
				import_state: filterImportState || undefined,
				q: search.trim() || undefined,
				limit: PAGE_SIZE,
				offset: page * PAGE_SIZE,
			});
			items = res.items;
			total = res.total;
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
		filterRole !== '' ||
		filterStatus !== '' ||
		filterSource !== '' ||
		filterImportState !== '' ||
		search.trim() !== '';

	function clearFilters() {
		filterRole = '';
		filterStatus = '';
		filterSource = '';
		filterImportState = '';
		search = '';
		page = 0;
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
		if (source === 'agent') return 'Agent';
		return '';
	}

	function sourceClass(source?: string): string {
		if (source === 'hp_created') return 'bg-accent-tint text-accent-strong';
		if (source === 'imported') return 'bg-ok-tint text-ok';
		if (source === 'discovered') return 'bg-warn-tint text-warn';
		if (source === 'agent') return 'bg-ok-tint text-ok';
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

	// ── Agent fleet ops (#514 S4): the Agents tab folded into Hosts. Same
	// mechanics as S3 built them - one dialog for the batch, optimistic
	// in-place updates, no full reload.
	let agentBatch: { kind: 'forget' | 'revoke'; targets: Host[] } | null = null;
	let agentWorking = false;

	function selectDisconnectedAgents() {
		selectedIds = new Set(
			items.filter((h) => h.agent_id && !h.agent_connected).map((h) => h.id)
		);
	}

	$: selectedHosts = items.filter((h) => selectedIds.has(h.id));
	// Forget needs no live channel (#415); revoke needs one.
	$: agentForgettable = selectedHosts.filter((h) => h.agent_id && !h.agent_connected);
	$: agentRevokable = selectedHosts.filter((h) => h.agent_id && h.agent_connected);

	async function runAgentBatch() {
		if (!agentBatch) return;
		const { kind, targets } = agentBatch;
		agentWorking = true;
		const results = await Promise.allSettled(
			targets.map((h) =>
				kind === 'forget' ? api.forgetAgent(h.agent_id!) : api.revokeAgent(h.agent_id!)
			)
		);
		const failed: Host[] = [];
		results.forEach((r, i) => {
			if (r.status === 'rejected') failed.push(targets[i]);
		});
		const doneIds = new Set(
			targets.filter((t) => !failed.includes(t)).map((t) => t.id)
		);
		// In place: the host row stays (the machine exists); its agent state flips.
		items = items.map((h) =>
			doneIds.has(h.id)
				? kind === 'forget'
					? { ...h, agent_id: null, agent_connected: undefined, agent_version: null }
					: { ...h, agent_connected: false }
				: h
		);
		if (failed.length) {
			notify(
				`${failed.length} of ${targets.length} failed: ${failed.map((f) => f.hostname).join(', ')}`,
				'err'
			);
		} else {
			notify(
				kind === 'forget'
					? `Forgot ${targets.length} agent${targets.length === 1 ? '' : 's'} — credentials revoked`
					: `Revoked ${targets.length} agent${targets.length === 1 ? '' : 's'}`
			);
		}
		agentWorking = false;
		agentBatch = null;
		selectedIds = new Set();
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
		<h1 class="page-title">Hosts</h1>
		<div class="flex gap-2">
			<button class="btn btn-ghost text-xs" on:click={load} disabled={loading}>↻ Reload</button>
			{#if canWrite}
				<button class="btn btn-primary text-xs" on:click={() => (showAdd = !showAdd)}>
					{showAdd ? 'Cancel' : '+ Add host'}
				</button>
				<button class="btn btn-ghost text-xs" on:click={sync} disabled={syncing}>
					{syncing ? 'Syncing…' : '⟳ Sync from Proxmox'}
				</button>
				<button class="btn btn-ghost text-xs"
					on:click={() => (enrollShow = enrollShow === 'bootstrap' ? null : 'bootstrap')}>
					{enrollShow === 'bootstrap' ? 'Cancel' : 'Enroll agent'}
				</button>
				<button class="btn btn-ghost text-xs"
					on:click={() => (enrollShow = enrollShow === 'hub' ? null : 'hub')}>
					{enrollShow === 'hub' ? 'Hide' : 'Hub token'}
				</button>
			{/if}
		</div>
	</div>

	{#if showAdd}
		<!-- Inventory could only be filled by a Proxmox sync, so the NAS, the
		     router and the Pi were literally unrepresentable (#445 A5). -->
		<form class="card space-y-3" on:submit|preventDefault={submitAdd}>
			<div class="grid gap-3 sm:grid-cols-2">
				<label class="space-y-1">
					<span class="text-xs text-muted">Hostname</span>
					<input
						class="input w-full text-sm"
						bind:value={addForm.hostname}
						placeholder="nas01"
						required
					/>
				</label>
				<label class="space-y-1">
					<span class="text-xs text-muted">IP address (optional)</span>
					<input class="input w-full text-sm" bind:value={addForm.ip_address} placeholder="10.0.0.4" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-muted">Role</span>
					<select class="input w-full text-sm" bind:value={addForm.role}>
						{#each ROLES as role}<option value={role}>{role}</option>{/each}
					</select>
				</label>
				<label class="space-y-1">
					<span class="text-xs text-muted">Description (optional)</span>
					<input
						class="input w-full text-sm"
						bind:value={addForm.description}
						placeholder="Synology NAS in the cupboard"
					/>
				</label>
			</div>
			<p class="prose-note text-xs">
				Added by hand, so it is recorded as a manual host and adopted straight away - and a
				Proxmox sync will never mark it gone, because Proxmox never looked for it.
			</p>
			<button class="btn btn-primary text-xs" type="submit" disabled={adding || !addForm.hostname.trim()}>
				{adding ? 'Adding…' : 'Add host'}
			</button>
		</form>
	{/if}

	<div class="flex gap-3 flex-wrap">
		<label class="flex items-center gap-2">
			<span class="sr-only">Search hosts</span>
			<input
				class="input text-xs w-64"
				type="search"
				placeholder="Search name, address, role, tags…"
				bind:value={search}
				on:input={searchAgain}
			/>
		</label>
		<select class="input text-xs" bind:value={filterRole} on:change={firstPage}>
			<!-- Exactly the roles the code writes (#424). It used to offer
			     hypervisor / vm / container / service - none of which anything
			     ever writes - while omitting `node` and `guest`, the two that
			     actually exist. Filtering by them returned nothing, forever. -->
			<option value="">All roles</option>
			{#each ROLES as role}<option value={role}>{role}</option>{/each}
		</select>
		<select class="input text-xs" bind:value={filterStatus} on:change={firstPage}>
			<option value="">All statuses</option>
			<option value="online">online</option>
			<option value="offline">offline</option>
			<option value="unknown">unknown</option>
		</select>
		<select class="input text-xs" bind:value={filterSource} on:change={firstPage}>
			<option value="">All sources</option>
			<option value="hp_created">HP-Created</option>
			<option value="discovered">Discovered</option>
			<option value="imported">Imported</option>
			<option value="manual">Manual</option>
			<option value="agent">Agent</option>
		</select>
		<select class="input text-xs" bind:value={filterImportState} on:change={firstPage}>
			<option value="">All import states</option>
			<option value="pending">Pending</option>
			<option value="adopted">Adopted</option>
			<option value="ignored">Ignored</option>
		</select>
		{#if hasActiveFilters}
			<button class="btn btn-ghost text-xs self-center" on:click={clearFilters}>Clear filters</button>
		{/if}
		<span class="text-muted text-xs self-center">
			{#if total > items.length}
				{page * PAGE_SIZE + 1}-{page * PAGE_SIZE + items.length} of {total} hosts
			{:else}
				{total} host{total === 1 ? '' : 's'}
			{/if}
		</span>
	</div>

	<EnrollAgent show={enrollShow} />

	{#if items.some((h) => h.agent_id && !h.agent_connected)}
		<div class="flex justify-end">
			<button class="btn btn-ghost text-xs" on:click={selectDisconnectedAgents}
				title="Select every host whose agent has no live channel">Select disconnected agents</button>
		</div>
	{/if}

	{#if selectedIds.size > 0}
		<div class="flex gap-2 items-center">
			<span class="text-xs text-ink">{selectedIds.size} selected</span>
			{#if canWrite}
				<button class="btn btn-sm text-xs" on:click={() => bulk('adopt')}>Adopt</button>
				<button class="btn btn-sm text-xs" on:click={() => bulk('ignore')}>Ignore</button>
				<button class="btn btn-sm text-xs" on:click={() => bulk('enrich')}>Enrich</button>
				{#if agentForgettable.length}
					<button class="btn btn-sm btn-danger text-xs"
						on:click={() => (agentBatch = { kind: 'forget', targets: agentForgettable })}
					>Forget {agentForgettable.length} agent{agentForgettable.length === 1 ? '' : 's'}</button>
				{/if}
				{#if agentRevokable.length}
					<button class="btn btn-sm text-xs text-danger"
						on:click={() => (agentBatch = { kind: 'revoke', targets: agentRevokable })}
					>Revoke {agentRevokable.length} agent{agentRevokable.length === 1 ? '' : 's'}</button>
				{/if}
			{/if}
			<button class="btn btn-sm btn-ghost text-xs" on:click={() => (selectedIds = new Set())}>Clear</button>
		</div>
	{/if}

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load hosts.</p>
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
			<p class="prose-note text-sm">No hosts yet.</p>
			<p class="prose-note text-xs">
				Sync from Proxmox to discover guests, add a host by hand, or enroll an
				agent — any of the three puts a machine on this page.
			</p>
		</div>
	{:else}
		<div class="card overflow-x-auto">
			<table class="data-table text-xs">
				<thead>
					<tr>
						<th class="text-left pr-2"><input type="checkbox" on:change={toggleSelectAll} checked={selectedIds.size > 0 && selectedIds.size === items.length} /></th>
						<th class="text-left">Hostname</th>
						<th class="text-left">IP</th>
						<th class="text-left">Role</th>
						<th class="text-left">Status</th>
						<th class="text-left">Source</th>
						<th class="text-left">Actions</th>
						<th class="text-left">Node</th>
					</tr>
				</thead>
				<tbody>
					{#each items as h}
						<tr class="border-b border-divider hover:bg-raised transition-colors">
							<td class="pr-2"><input type="checkbox" checked={selectedIds.has(h.id)} on:change={() => toggleSelect(h.id)} /></td>
							<td>
								<a
									href="{base}/hosts/{h.id}"
									class="text-accent hover:text-accent-strong font-mono"
								>{h.hostname}</a>
								{#if h.absent_since}
									<!-- A destroyed guest used to look exactly like a powered-off
									     one: the row simply stopped being updated (#445 A5). -->
									<span
										class="badge badge-failed ml-1"
										title="Proxmox stopped reporting this host on {fmtDate(h.absent_since)}"
									>gone</span>
								{/if}
							</td>
							<td class="text-muted font-mono">{h.ip_address || '—'}</td>
							<td class="text-ink">
								{h.role ?? '—'}
								{#if h.role_source === 'inferred'}<span class="text-warn" title="Inferred">?</span>{/if}
							</td>
							<td class="{statusColor(h.status)}">
								{h.status ?? 'unknown'}
								{#if h.agent_id}
									<span
										class="ml-1 px-1.5 py-0.5 rounded text-[10px] font-medium {h.agent_connected
											? 'bg-ok-tint text-ok'
											: 'bg-raised text-muted'}"
										title={h.agent_connected
											? `Agent ${h.agent_version ?? ''} connected`
											: 'Agent enrolled but not connected'}
									>{h.agent_connected ? 'agent ·' : 'agent ∅'}{h.agent_connected && h.agent_version ? ` ${h.agent_version}` : ''}</span>
								{/if}
							</td>
							<td>
								{#if h.source}
									<span class="px-1.5 py-0.5 rounded text-[10px] font-medium {sourceClass(h.source)}">{sourceBadge(h.source)}</span>
								{:else}
									<span class="text-muted">—</span>
								{/if}
							</td>
							<td>
								{#if canWrite && h.source === 'discovered' && h.import_state !== 'ignored'}
									<button class="btn btn-xs text-[10px]" on:click={() => adoptOne(h)}>Adopt</button>
									<button class="btn btn-xs text-[10px]" on:click={() => ignoreOne(h)}>Ignore</button>
								{:else if h.import_state === 'ignored'}
									<span class="text-muted">Ignored</span>
								{:else}
									<span class="text-muted">—</span>
								{/if}
								{#if canWrite && (h.source === 'manual' || h.absent_since)}
									{#if forgetConfirm === h.id}
										<button
											class="btn btn-danger btn-xs text-[10px]"
											disabled={forgetting === h.id}
											on:click={() => forgetOne(h)}
										>{forgetting === h.id ? 'Removing…' : 'Confirm'}</button>
										<button
											class="btn btn-ghost btn-xs text-[10px]"
											on:click={() => (forgetConfirm = null)}
										>Cancel</button>
									{:else}
										<button
											class="btn btn-xs text-[10px] text-danger"
											title="Remove this host from inventory, with its services and observation note"
											on:click={() => (forgetConfirm = h.id)}
										>Forget</button>
									{/if}
								{/if}
								{#if h.hostname}
									<a
										class="text-accent hover:text-accent-strong text-[10px] ml-1"
										href={hostMetricsUrl(base, h.hostname, h.id)}
										title="Open this host's recent metrics">Metrics</a
									>
								{/if}
							</td>
							<td class="text-muted">{h.node ?? '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		{#if total > PAGE_SIZE}
			<div class="flex gap-2 items-center justify-end">
				<button
					class="btn btn-ghost text-xs"
					disabled={page === 0}
					on:click={() => {
						page -= 1;
						load();
					}}>← Previous</button
				>
				<span class="text-xs text-muted">
					Page {page + 1} of {Math.ceil(total / PAGE_SIZE)}
				</span>
				<button
					class="btn btn-ghost text-xs"
					disabled={(page + 1) * PAGE_SIZE >= total}
					on:click={() => {
						page += 1;
						load();
					}}>Next →</button
				>
			</div>
		{/if}
	{/if}
</div>


{#if agentBatch}
	<div class="fixed inset-0 z-40 flex items-center justify-center bg-black/40" role="dialog" aria-modal="true">
		<div class="card p-5 max-w-md w-full space-y-3">
			<h3 class="font-semibold text-ink">
				{agentBatch.kind === 'forget' ? 'Forget' : 'Revoke'}
				{agentBatch.targets.length} agent{agentBatch.targets.length === 1 ? '' : 's'}?
			</h3>
			<p class="text-muted text-sm">
				{agentBatch.kind === 'forget'
					? 'Each agent and its credential are removed for good. The hosts stay in inventory; re-enrolling needs a fresh token.'
					: 'Each credential dies and any open channel closes now. Re-enrolling needs a fresh token.'}
			</p>
			<ul class="text-sm text-ink max-h-40 overflow-y-auto space-y-0.5">
				{#each agentBatch.targets as t (t.id)}
					<li class="font-mono">{t.hostname}</li>
				{/each}
			</ul>
			<div class="flex justify-end gap-2">
				<button class="btn btn-ghost text-xs" disabled={agentWorking} on:click={() => (agentBatch = null)}>Cancel</button>
				<button class="btn btn-danger text-xs" disabled={agentWorking} on:click={runAgentBatch}>
					{agentWorking ? 'Working…' : `${agentBatch.kind === 'forget' ? 'Forget' : 'Revoke'} ${agentBatch.targets.length}`}
				</button>
			</div>
		</div>
	</div>
{/if}
