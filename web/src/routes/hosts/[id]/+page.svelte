<script lang="ts">
	// The host page (#514 S2): one machine, everything HomePilot knows about it.
	// This is where a host's stats live — the Agents-tab expansion this replaces
	// rendered a raw key/value dump with values flushed to the far table edge
	// and JSON blobs verbatim. Here: one definition-list pattern (label column,
	// value column, left-anchored, tabular numerals), and raw JSON never.
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import {
		api,
		sessionStore,
		type AgentInstallEligibility,
		type AgentOnHost,
		type AuditEntry,
		type Host,
		type HostDoc,
		type LatestMetric,
		type MetricPoint,
		type Service,
		type Task,
	} from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import Sparkline from '$lib/components/Sparkline.svelte';
	import { formatMetricValue, metricLabel } from '$lib/sparkline';

	type HostDetail = Host & { services: Service[]; agent?: AgentOnHost };

	let host: HostDetail | null = null;
	let doc: HostDoc | null = null;
	let latest: LatestMetric[] = [];
	let series: Record<string, MetricPoint[]> = {};
	let activity: AuditEntry[] = [];
	let loading = true;
	let loadError = '';
	let hours = 1;
	let working = false;
	// Destructive actions get ONE dialog, not button-swapping under the cursor.
	let confirmAction: { fn: () => Promise<void>; title: string; body: string } | null = null;

	$: id = $page.params.id ?? '';
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	$: agent = host?.agent ?? null;

	const RANGES = [
		{ label: '1h', hours: 1 },
		{ label: '6h', hours: 6 },
		{ label: '24h', hours: 24 },
		{ label: '7d', hours: 168 },
	];

	async function load() {
		loading = true;
		loadError = '';
		try {
			host = await api.getHost(id);
			// Everything else is additive: a host with no agent has no metrics,
			// and a missing doc must not blank the page.
			const [docRes, actRes] = await Promise.allSettled([
				api.getHostDoc(id),
				api.listAudit({ target_host: host.hostname, limit: 25 }),
			]);
			doc = docRes.status === 'fulfilled' ? docRes.value : null;
			activity = actRes.status === 'fulfilled' ? actRes.value.items : [];
			await Promise.all([loadMetrics(), loadEligibility()]);
		} catch (e) {
			loadError = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	async function loadMetrics() {
		if (!host?.agent_id || !host.hostname) return;
		try {
			const latestRes = await api.getHostLatest(host.hostname);
			latest = latestRes.metrics;
			const wanted = latest.map((m) => m.metric);
			const results = await Promise.allSettled(
				wanted.map((m) => api.getHostSeries(host!.hostname, m, hours))
			);
			const next: Record<string, MetricPoint[]> = {};
			results.forEach((r, i) => {
				if (r.status === 'fulfilled') next[wanted[i]] = r.value.points;
			});
			series = next;
		} catch {
			// The metrics grid renders its own empty state; a fetch error here
			// must not take down the identity header.
			latest = [];
			series = {};
		}
	}

	function setRange(h: number) {
		hours = h;
		void loadMetrics();
	}

	async function revokeAgent() {
		if (!agent) return;
		working = true;
		try {
			const res = await api.revokeAgent(agent.agent_id);
			notify(
				res.channel_closed
					? `Revoked — the open channel is closed`
					: `Revoked — it had no open channel`
			);
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			working = false;
			confirmAction = null;
		}
	}

	async function forgetAgent() {
		if (!agent) return;
		working = true;
		try {
			await api.forgetAgent(agent.agent_id);
			notify(`Forgot the agent — its credential is revoked`);
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			working = false;
			confirmAction = null;
		}
	}

	// ── Management, ported from the legacy /inventory/{id} record page (#514 S4):
	// adopt/ignore, zero-touch agent install, role/IP edits, re-enrich.
	let eligibility: AgentInstallEligibility | null = null;
	let installTask: Task | null = null;
	let installingAgent = false;
	let destroyed = false;
	onDestroy(() => (destroyed = true));
	let editField: 'role' | 'ip' | null = null;
	let editValue = '';

	async function loadEligibility() {
		if (!host) return;
		// Admin-only answer: a read-only session simply gets no install button.
		eligibility = await api.agentInstallEligibility(host.id).catch(() => null);
	}

	async function installAgent() {
		if (!host) return;
		installingAgent = true;
		try {
			const { task_id } = await api.installAgent(host.id);
			notify('Installing the agent — progress in Tasks');
			for (;;) {
				installTask = await api.getTask(task_id);
				if (installTask.status !== 'pending' && installTask.status !== 'running') break;
				await new Promise((r) => setTimeout(r, 3000));
				if (destroyed) return;
			}
			notify(
				installTask.status === 'succeeded'
					? 'Agent enrolled'
					: `Agent install failed: ${installTask.error ?? ''}`,
				installTask.status === 'succeeded' ? 'ok' : 'err'
			);
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			installingAgent = false;
		}
	}

	async function adopt() {
		if (!host) return;
		try {
			await api.adoptHost(host.id);
			notify('Adopted — HomePilot may act on this host now');
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		}
	}

	async function setImportState(state: 'ignored' | 'pending') {
		if (!host) return;
		try {
			if (state === 'ignored') await api.ignoreHost(host.id);
			else await api.updateHost(host.id, { import_state: 'pending' });
			notify(state === 'ignored' ? 'Ignored' : 'Unignored');
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		}
	}

	async function reenrich() {
		if (!host) return;
		try {
			const res = await api.enrichInventory([host.id]);
			notify(`Enriched ${res.enriched}, failed ${res.failed}`);
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		}
	}

	function startEdit(field: 'role' | 'ip') {
		editField = field;
		editValue = (field === 'role' ? host?.role : host?.ip_address) || '';
	}

	async function saveEdit() {
		if (!host || !editField) return;
		try {
			await api.updateHost(
				host.id,
				editField === 'role' ? { role: editValue } : { ip_address: editValue }
			);
			notify(editField === 'role' ? 'Role saved' : 'IP saved');
			editField = null;
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		}
	}

	function fmtDate(v?: string | null): string {
		if (!v) return '—';
		const d = new Date(v);
		return isNaN(d.getTime()) ? v : d.toLocaleString();
	}

	function chip(status?: string): string {
		if (status === 'online') return 'bg-ok-tint text-ok';
		if (status === 'offline') return 'bg-danger-tint text-danger';
		return 'bg-raised text-muted';
	}

	onMount(load);

	let refreshHandle: ReturnType<typeof setInterval> | null = null;
	onMount(() => {
		refreshHandle = setInterval(() => void loadMetrics(), 30_000);
	});
	onDestroy(() => {
		if (refreshHandle) clearInterval(refreshHandle);
	});
</script>

<svelte:head><title>{host?.hostname ?? 'Host'} — HomePilot</title></svelte:head>

{#if loading}
	<p class="text-muted">Loading…</p>
{:else if loadError}
	<div class="card p-4">
		<p class="text-danger">{loadError}</p>
		<a class="btn btn-ghost mt-2 inline-block" href="{base}/hosts">Back to hosts</a>
	</div>
{:else if host}
	<div class="space-y-6">
		<!-- ── Identity ──────────────────────────────────────────────────── -->
		<div class="flex flex-wrap items-start justify-between gap-3">
			<div>
				<div class="flex items-center gap-2 flex-wrap">
					<h1 class="text-xl font-semibold text-ink">{host.hostname}</h1>
					<span class="px-1.5 py-0.5 rounded text-[10px] font-medium {chip(host.status)}"
						>{host.status ?? 'unknown'}</span>
					{#if agent}
						<span
							class="px-1.5 py-0.5 rounded text-[10px] font-medium {agent.connected
								? 'bg-ok-tint text-ok'
								: 'bg-raised text-muted'}"
							>{agent.connected ? 'agent connected' : 'agent not connected'}</span>
					{/if}
					{#if host.absent_since}
						<span class="badge badge-failed" title="Proxmox stopped reporting this host on {fmtDate(host.absent_since)}">gone</span>
					{/if}
				</div>
				<p class="text-muted text-sm mt-1">
					{host.role ?? 'guest'}{host.description ? ` — ${host.description}` : ''}
				</p>
			</div>
			<div class="flex gap-2 flex-wrap">
				{#if canWrite}
					{#if host.source === 'discovered' && host.import_state !== 'ignored'}
						<button class="btn btn-ghost text-xs" on:click={adopt}
							title="Adopting says HomePilot may act on this host">Adopt</button>
						<button class="btn btn-ghost text-xs" on:click={() => setImportState('ignored')}>Ignore</button>
					{:else if host.import_state === 'ignored'}
						<button class="btn btn-ghost text-xs" on:click={() => setImportState('pending')}>Unignore</button>
					{/if}
					{#if eligibility?.eligible && !agent}
						<button class="btn btn-primary text-xs" on:click={installAgent}
							disabled={installingAgent || eligibility?.in_flight}
							title={eligibility?.message}>
							{installingAgent || eligibility?.in_flight ? 'Installing…' : 'Install agent'}
						</button>
					{/if}
					{#if host.proxmox_id != null}
						<button class="btn btn-ghost text-xs" on:click={reenrich}
							title="Re-read this guest's details from Proxmox">Re-enrich</button>
					{/if}
				{/if}
				<a class="btn btn-ghost text-xs" href="{base}/hosts">← All hosts</a>
			</div>
		</div>

		{#if installTask}
			<p class="prose-note text-xs">
				Agent install: <span class="text-ink">{installTask.status}</span>
				{#if installTask.error}<span class="text-danger">— {installTask.error}</span>{/if}
				· <a class="text-accent" href="{base}/records/tasks">Tasks ↗</a>
			</p>
		{:else if canWrite && eligibility && !eligibility.eligible && !agent}
			<p class="prose-note text-xs">No agent install from here: {eligibility.message}</p>
		{/if}

		<!-- ── Facts: ONE definition-list pattern, left-anchored ──────────── -->
		<div class="card p-4">
			<dl class="grid gap-x-8 gap-y-1.5 text-sm" style="grid-template-columns: max-content 1fr;">
				<dt class="text-muted">OS</dt>
				<dd class="text-ink m-0">{host.os_info || '—'}</dd>
				<dt class="text-muted">CPU cores</dt>
				<dd class="text-ink m-0 num-inline">{host.cpu_cores ?? '—'}</dd>
				<dt class="text-muted">Memory</dt>
				<dd class="text-ink m-0 num-inline">
					{host.memory_mb ? `${(host.memory_mb / 1024).toFixed(1)} GB` : '—'}
				</dd>
				<dt class="text-muted">IP address</dt>
				<dd class="text-ink m-0 font-mono">
					{#if editField === 'ip'}
						<input type="text" class="input text-xs w-40" bind:value={editValue} />
						<button class="text-accent text-xs" on:click={saveEdit}>Save</button>
						<button class="text-muted text-xs" on:click={() => (editField = null)}>Cancel</button>
					{:else}
						{(host.ip_address || '—') + (host.ip_source ? ` (${host.ip_source})` : '')}
						{#if canWrite}<button class="text-muted text-xs ml-1" on:click={() => startEdit('ip')}>Edit</button>{/if}
					{/if}
				</dd>
				<dt class="text-muted">Role</dt>
				<dd class="text-ink m-0">
					{#if editField === 'role'}
						<input type="text" class="input text-xs w-40" bind:value={editValue} />
						<button class="text-accent text-xs" on:click={saveEdit}>Save</button>
						<button class="text-muted text-xs" on:click={() => (editField = null)}>Cancel</button>
					{:else}
						{host.role ?? '—'}
						{#if host.role_source === 'inferred'}<span class="text-warn" title="Inferred, not operator-set">?</span>{/if}
						{#if canWrite}<button class="text-muted text-xs ml-1" on:click={() => startEdit('role')}>Edit</button>{/if}
					{/if}
				</dd>
				<dt class="text-muted">Type</dt>
				<dd class="text-ink m-0">{host.host_type ?? '—'}{host.node ? ` on ${host.node}` : ''}</dd>
				<dt class="text-muted">Source</dt>
				<dd class="text-ink m-0">{host.source ?? '—'}</dd>
			</dl>
		</div>

		<!-- ── Metrics ─────────────────────────────────────────────────────── -->
		<div class="card p-4 space-y-3">
			<div class="flex items-center justify-between">
				<h2 class="section-title">Metrics</h2>
				{#if latest.length}
					<div class="flex gap-1">
						{#each RANGES as r (r.hours)}
							<button
								class="btn text-xs {hours === r.hours ? 'btn-primary' : 'btn-ghost'}"
								on:click={() => setRange(r.hours)}>{r.label}</button>
						{/each}
					</div>
				{/if}
			</div>
			{#if !host.agent_id}
				<p class="text-muted text-sm">
					No agent on this host, so nothing reports metrics. Enroll one from the
					<a class="text-accent" href="{base}/agents">fleet page</a> to see load, memory and disk here.
				</p>
			{:else if !latest.length}
				<p class="text-muted text-sm">
					The agent has not reported metrics yet. They arrive with its next report cycle.
				</p>
			{:else}
				<div class="grid gap-x-10 gap-y-2" style="grid-template-columns: repeat(auto-fill, minmax(20rem, 1fr));">
					{#each latest as m (m.metric)}
						<div class="flex items-center gap-3">
							<span class="text-muted text-xs w-24 shrink-0">{metricLabel(m.metric)}</span>
							{#if series[m.metric]?.length}
								<Sparkline points={series[m.metric]} metric={m.metric} showLabel={false} width={150} height={30} />
							{:else}
								<span class="text-ink text-xs num-inline">{formatMetricValue(m.metric, m.value)}</span>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
		</div>

		<!-- ── Changes: what HomePilot has done to this machine ────────────── -->
		<div class="card p-4 space-y-2">
			<h2 class="section-title">Changes</h2>
			{#if doc?.artifact_history?.length}
				<table class="w-full text-sm">
					<thead>
						<tr class="text-left text-muted text-xs">
							<th class="font-medium pb-1">Artifact</th>
							<th class="font-medium pb-1">Status</th>
							<th class="font-medium pb-1">Created</th>
						</tr>
					</thead>
					<tbody>
						{#each doc.artifact_history as a (a.id)}
							<tr class="border-t border-line">
								<td class="py-1.5 pr-4">
									<a class="text-accent hover:text-accent-strong" href="{base}/changes/{a.id}">{a.intent ?? a.id}</a>
								</td>
								<td class="py-1.5 pr-4"><span class="badge badge-{a.status}">{a.status}</span></td>
								<td class="py-1.5 text-muted">{fmtDate(a.created_at)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{:else}
				<p class="text-muted text-sm">
					No artifact has targeted this host yet.
					<a class="text-accent" href="{base}/changes">Propose one</a> to put a change under management.
				</p>
			{/if}
		</div>

		<!-- ── Activity: the journal, scoped to this machine ───────────────── -->
		<div class="card p-4 space-y-2">
			<h2 class="section-title">Activity</h2>
			{#if activity.length}
				<ul class="space-y-1 text-sm">
					{#each activity as entry (entry.id)}
						<li class="flex gap-3 items-baseline">
							<span class="text-muted text-xs shrink-0 num-inline">{fmtDate(entry.timestamp)}</span>
							<span class="text-ink">{entry.action}</span>
							{#if entry.command}<span class="text-muted truncate font-mono text-xs">{entry.command}</span>{/if}
						</li>
					{/each}
				</ul>
			{:else}
				<p class="text-muted text-sm">Nothing in the journal names this host yet.</p>
			{/if}
		</div>

		{#if host.services?.length}
			<div class="card p-4 space-y-2">
				<h2 class="section-title">Services ({host.services.length})</h2>
				<table class="data-table text-xs">
					<thead>
						<tr>
							<th class="text-left pb-1">Name</th>
							<th class="num pb-1">Port</th>
							<th class="text-left pb-1">Status</th>
						</tr>
					</thead>
					<tbody>
						{#each host.services as svc (svc.name + String(svc.port))}
							<tr class="border-b border-divider">
								<td class="py-1 text-ink">{svc.name}</td>
								<td class="num py-1 text-muted font-mono">{svc.port ?? '—'}</td>
								<td class="py-1 text-muted">{svc.status ?? '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if doc?.kb_entries?.length}
			<div class="card p-4 space-y-2">
				<h2 class="section-title">Notes ({doc.kb_entries.length})</h2>
				<div class="space-y-2">
					{#each doc.kb_entries as e (e.id ?? e.title ?? e.content)}
						<div class="bg-canvas rounded p-3 text-xs space-y-1">
							{#if e.title}<p class="text-ink font-medium">{e.title}</p>{/if}
							<p class="prose-body line-clamp-3">{e.content}</p>
							<p class="prose-note">{e.kind} · {e.source}</p>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<!-- ── Agent: the channel, and why it last broke ───────────────────── -->
		<div class="card p-4 space-y-3">
			<h2 class="section-title">Agent</h2>
			{#if agent}
				<dl class="grid gap-x-8 gap-y-1.5 text-sm" style="grid-template-columns: max-content 1fr;">
					<dt class="text-muted">Version</dt>
					<dd class="text-ink m-0">{agent.version ?? 'unversioned'}{agent.arch ? ` · ${agent.arch}` : ''}</dd>
					<dt class="text-muted">State</dt>
					<dd class="m-0 {agent.connected ? 'text-ok' : 'text-muted'}">
						{agent.connected ? `connected since ${fmtDate(agent.connected_at)}` : `disconnected ${fmtDate(agent.disconnected_at)}`}
					</dd>
					<dt class="text-muted">Last heartbeat</dt>
					<dd class="text-ink m-0">{fmtDate(agent.last_heartbeat)}</dd>
					{#if agent.last_error}
						<dt class="text-muted">Last refusal</dt>
						<dd class="text-danger m-0">{agent.last_error} <span class="text-muted">({fmtDate(agent.last_error_at)})</span></dd>
					{/if}
					{#if agent.revoked_at}
						<dt class="text-muted">Credential</dt>
						<dd class="text-danger m-0">revoked {fmtDate(agent.revoked_at)}</dd>
					{:else}
						<dt class="text-muted">Credential</dt>
						<dd class="text-ink m-0">issued {fmtDate(agent.credential_set_at)}</dd>
					{/if}
				</dl>
				{#if canWrite}
					<div class="flex gap-2 pt-1">
						{#if agent.connected}
							<button
								class="btn btn-ghost text-xs text-danger"
								disabled={working}
								on:click={() =>
									(confirmAction = {
										fn: revokeAgent,
										title: `Revoke the agent on ${host?.hostname}?`,
										body: 'Its credential dies and the live channel closes now. The host stays in inventory; re-enrolling needs a fresh token.',
									})}>Revoke</button>
						{:else}
							<button
								class="btn btn-ghost text-xs text-danger"
								disabled={working}
								on:click={() =>
									(confirmAction = {
										fn: forgetAgent,
										title: `Forget the agent on ${host?.hostname}?`,
										body: 'The agent row and its credential are removed for good. The host stays in inventory.',
									})}>Forget agent</button>
						{/if}
					</div>
				{/if}
			{:else}
				<p class="text-muted text-sm">
					No agent is enrolled on this host. <a class="text-accent" href="{base}/agents">Enroll one</a>
					to get a live channel, metrics and native actions.
				</p>
			{/if}
		</div>
	</div>

	{#if confirmAction}
		<div class="fixed inset-0 z-40 flex items-center justify-center bg-black/40" role="dialog" aria-modal="true">
			<div class="card p-5 max-w-sm w-full space-y-3">
				<h3 class="font-semibold text-ink">{confirmAction.title}</h3>
				<p class="text-muted text-sm">{confirmAction.body}</p>
				<div class="flex justify-end gap-2">
					<button class="btn btn-ghost text-xs" disabled={working} on:click={() => (confirmAction = null)}>Cancel</button>
					<button class="btn btn-danger text-xs" disabled={working} on:click={() => void confirmAction?.fn()}>
						{working ? 'Working…' : 'Confirm'}
					</button>
				</div>
			</div>
		</div>
	{/if}
{/if}
