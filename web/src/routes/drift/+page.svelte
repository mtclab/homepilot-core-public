<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { api, type Artifact, type Host, type DriftCheck } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';

	let artifacts: Artifact[] = [];
	let hosts: Host[] = [];
	let driftChecks: DriftCheck[] = [];
	let loading = true;
	let loadError = '';
	let rechecking = false;
	let recheckingId = '';

	interface DriftRow {
		artifact: Artifact;
		target: string;
		matchedHost: Host | null;
		driftStatus: 'drifted' | 'not-drifted' | 'unchecked';
		driftCheck: DriftCheck | null;
	}

	let rows: DriftRow[] = [];
	let unmanagedHosts: Host[] = [];

	async function load(initial = true) {
		if (initial) loading = true;
		loadError = '';
		try {
			const [aRes, hRes, dRes] = await Promise.all([
				api.listArtifacts({ limit: 500 }),
				api.listInventory(),
				api.getDriftStatus({ limit: 500 }),
			]);
			artifacts = aRes.items.filter((a) => ['applied', 'approved'].includes(a.status));
			hosts = hRes.items;
			driftChecks = dRes.items;
			compute();
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			// On a silent live refresh, keep the last-good tables rather than
			// replacing them with the error card / a toast.
			if (initial || rows.length === 0) {
				loadError = msg;
				notify(msg, 'err');
			}
		} finally {
			loading = false;
		}
	}

	function artifactTarget(a: Artifact): string {
		const t = a.target ?? {};
		return t.host ?? t.service ?? t.node ?? '';
	}

	function getDriftStatus(artifactId: string): DriftCheck | null {
		return driftChecks.find((dc) => dc.artifact_id === artifactId) ?? null;
	}

	function compute() {
		const hostByName = new Map<string, Host>();
		for (const h of hosts) {
			hostByName.set(h.hostname, h);
			if (h.node) hostByName.set(h.node, h);
		}

		rows = artifacts.map((a) => {
			const target = artifactTarget(a);
			const matchedHost = target ? (hostByName.get(target) ?? null) : null;
			const dc = getDriftStatus(a.id);
			let driftStatus: 'drifted' | 'not-drifted' | 'unchecked' = 'unchecked';
			if (dc) {
				driftStatus = dc.drifted ? 'drifted' : 'not-drifted';
			}
			return { artifact: a, target, matchedHost, driftStatus, driftCheck: dc };
		});

		const coveredHosts = new Set(rows.map((r) => r.matchedHost?.id).filter(Boolean));
		unmanagedHosts = hosts.filter((h) => !coveredHosts.has(h.id));
	}

	async function recheck(artifactId: string) {
		rechecking = true;
		recheckingId = artifactId;
		try {
			const res = await api.recheckDrift(artifactId);
			if (res.items.length > 0) {
				const existingIdx = driftChecks.findIndex((dc) => dc.artifact_id === artifactId);
				if (existingIdx >= 0) {
					driftChecks[existingIdx] = res.items[0];
				} else {
					driftChecks.push(res.items[0]);
				}
			}
			compute();
			notify('Drift recheck complete', 'ok');
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			rechecking = false;
			recheckingId = '';
		}
	}

	let showOrphans = true;
	let showCovered = true;
	let showUnmanaged = true;

	$: orphanRows = rows.filter((r) => !r.matchedHost);
	$: coveredRows = rows.filter((r) => r.matchedHost);

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

	const DRIFT_COLORS: Record<string, string> = {
		drifted: 'text-danger bg-danger-tint border-danger-border',
		'not-drifted': 'text-ok bg-ok-tint border-ok-border',
		unchecked: 'text-muted bg-raised border-border-strong',
	};

	function fmtDate(s: string): string {
		return s ? new Date(s).toLocaleDateString() : '—';
	}

	onMount(load);
	// Live-refresh on drift + lifecycle events (an apply/revoke changes what's
	// covered; artifact_drifted changes a row's status). Debounced hard: EVERY
	// event here fired three parallel calls (500 artifacts + full inventory +
	// 500 drift rows), so a burst of events was a burst of full reloads.
	const refresh = debounce(() => load(false), 400);
	const unsub = onArtifactEvent(refresh);
	onDestroy(() => {
		unsub();
		refresh.cancel();
	});
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Drift</h1>
		<button class="btn btn-ghost text-xs" on:click={() => load(true)}>↻ Refresh</button>
	</div>

	<div class="flex gap-3 flex-wrap text-xs">
		<button
			class="px-3 py-1 rounded-full border transition-colors
			       {showCovered ? 'bg-ok-tint border-ok-border text-ok' : 'border-border-strong text-muted'}"
			on:click={() => (showCovered = !showCovered)}
		>
			✓ {coveredRows.length} covered
		</button>
		<button
			class="px-3 py-1 rounded-full border transition-colors
			       {showOrphans ? 'bg-warn-tint border-warn-border text-warn' : 'border-border-strong text-muted'}"
			on:click={() => (showOrphans = !showOrphans)}
		>
			⚠ {orphanRows.length} orphaned artifacts
		</button>
		<button
			class="px-3 py-1 rounded-full border transition-colors
			       {showUnmanaged ? 'bg-raised border-border-strong text-ink' : 'border-border-strong text-muted'}"
			on:click={() => (showUnmanaged = !showUnmanaged)}
		>
			○ {unmanagedHosts.length} uncovered hosts
		</button>
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load drift data.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={() => load(true)}>↻ Retry</button>
		</div>
	{:else}
		{#if showOrphans && orphanRows.length}
			<div class="card space-y-2">
				<h2 class="section-title text-warn">⚠ Orphaned Artifacts</h2>
				<p class="prose-note text-xs">Active artifacts whose target is absent from inventory.</p>
				<table class="data-table text-xs">
					<thead>
						<tr>
							<th class="text-left pb-1 pr-4">Intent</th>
							<th class="text-left pb-1 pr-4">Kind</th>
							<th class="text-left pb-1 pr-4">Status</th>
							<th class="text-left pb-1 pr-4">Target</th>
							<th class="text-left pb-1 pr-4">Drift</th>
							<th class="text-left pb-1">Date</th>
						</tr>
					</thead>
					<tbody>
						{#each orphanRows as r}
							<tr class="border-b border-divider">
								<td class="py-1.5 pr-4 text-ink truncate max-w-xs">{r.artifact.intent}</td>
								<td class="py-1.5 pr-4 text-muted">{r.artifact.kind}</td>
								<td class="py-1.5 pr-4"><span class="badge {statusClass(r.artifact.status)}">{r.artifact.status}</span></td>
								<td class="py-1.5 pr-4 text-warn font-mono">{r.target || '(global)'}</td>
								<td class="py-1.5 pr-4">
									<span class="px-1.5 py-0.5 rounded border text-[10px] {DRIFT_COLORS[r.driftStatus]}">
										{r.driftStatus === 'drifted' ? '⚠ drifted' : r.driftStatus === 'not-drifted' ? '✓ ok' : '— unchecked'}
									</span>
									{#if rechecking && recheckingId === r.artifact.id}
										<span class="text-muted ml-1 animate-pulse">checking…</span>
									{:else}
										<button
											class="text-accent hover:text-accent-strong ml-1"
											on:click={() => recheck(r.artifact.id)}
										>↻</button>
									{/if}
								</td>
								<td class="py-1.5 text-muted">{fmtDate(r.artifact.created_at)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if showCovered && coveredRows.length}
			<div class="card space-y-2">
				<h2 class="section-title text-ok">✓ Covered</h2>
				<table class="data-table text-xs">
					<thead>
						<tr>
							<th class="text-left pb-1 pr-4">Intent</th>
							<th class="text-left pb-1 pr-4">Kind</th>
							<th class="text-left pb-1 pr-4">Status</th>
							<th class="text-left pb-1 pr-4">Drift</th>
							<th class="text-left pb-1">Host</th>
						</tr>
					</thead>
					<tbody>
						{#each coveredRows as r}
							<tr class="border-b border-divider">
								<td class="py-1 pr-4 text-ink truncate max-w-xs">{r.artifact.intent}</td>
								<td class="py-1 pr-4 text-muted">{r.artifact.kind}</td>
								<td class="py-1 pr-4"><span class="badge {statusClass(r.artifact.status)}">{r.artifact.status}</span></td>
								<td class="py-1 pr-4">
									<span class="px-1.5 py-0.5 rounded border text-[10px] {DRIFT_COLORS[r.driftStatus]}">
										{r.driftStatus === 'drifted' ? '⚠ drifted' : r.driftStatus === 'not-drifted' ? '✓ ok' : '— unchecked'}
									</span>
									{#if rechecking && recheckingId === r.artifact.id}
										<span class="text-muted ml-1 animate-pulse">checking…</span>
									{:else}
										<button
											class="text-accent hover:text-accent-strong ml-1"
											on:click={() => recheck(r.artifact.id)}
										>↻</button>
									{/if}
								</td>
								<td class="py-1">
									{#if r.matchedHost}
										<a href="{base}/inventory/{r.matchedHost.id}" class="text-accent hover:text-accent-strong font-mono">
											{r.matchedHost.hostname}
										</a>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if showUnmanaged && unmanagedHosts.length}
			<div class="card space-y-2">
				<h2 class="section-title text-muted">○ Uncovered Hosts</h2>
				<p class="prose-note text-xs">
					In inventory but not targeted by any applied artifact — config drift can't be
					tracked for them. Adopting a host in inventory does not cover it; an artifact must
					target it. Not related to the inventory "managed" flag.
				</p>
				<div class="flex flex-wrap gap-2">
					{#each unmanagedHosts as h}
						<a
							href="{base}/inventory/{h.id}"
							class="px-2 py-1 bg-raised hover:bg-border rounded text-xs font-mono text-ink transition-colors"
						>
							{h.hostname}
						</a>
					{/each}
				</div>
			</div>
		{/if}

		{#if rows.length === 0 && unmanagedHosts.length === 0}
			<p class="prose-note text-sm">No data — no artifacts or inventory yet.</p>
		{/if}
	{/if}
</div>