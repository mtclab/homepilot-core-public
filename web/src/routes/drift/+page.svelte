<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type Artifact, type Host, type DriftCheck } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';

	let artifacts: Artifact[] = [];
	let hosts: Host[] = [];
	let driftChecks: DriftCheck[] = [];
	let loading = true;
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

	async function load() {
		loading = true;
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
			notify(String(e), 'err');
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
		drifted: 'text-red-400 bg-red-900/40 border-red-700',
		'not-drifted': 'text-green-400 bg-green-900/40 border-green-700',
		unchecked: 'text-slate-400 bg-slate-800 border-slate-600',
	};

	function fmtDate(s: string): string {
		return s ? new Date(s).toLocaleDateString() : '—';
	}

	onMount(load);
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="text-lg font-bold text-slate-100">Drift</h1>
		<button class="btn btn-ghost text-xs" on:click={load}>↻ Refresh</button>
	</div>

	<div class="flex gap-3 flex-wrap text-xs">
		<button
			class="px-3 py-1 rounded-full border transition-colors
			       {showCovered ? 'bg-green-900 border-green-700 text-green-300' : 'border-slate-600 text-slate-500'}"
			on:click={() => (showCovered = !showCovered)}
		>
			✓ {coveredRows.length} covered
		</button>
		<button
			class="px-3 py-1 rounded-full border transition-colors
			       {showOrphans ? 'bg-orange-900 border-orange-700 text-orange-300' : 'border-slate-600 text-slate-500'}"
			on:click={() => (showOrphans = !showOrphans)}
		>
			⚠ {orphanRows.length} orphaned artifacts
		</button>
		<button
			class="px-3 py-1 rounded-full border transition-colors
			       {showUnmanaged ? 'bg-slate-700 border-slate-600 text-slate-300' : 'border-slate-600 text-slate-500'}"
			on:click={() => (showUnmanaged = !showUnmanaged)}
		>
			○ {unmanagedHosts.length} unmanaged hosts
		</button>
	</div>

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else}
		{#if showOrphans && orphanRows.length}
			<div class="card space-y-2">
				<h2 class="text-sm font-semibold text-orange-400">⚠ Orphaned Artifacts</h2>
				<p class="text-xs text-slate-500">Active artifacts whose target is absent from inventory.</p>
				<table class="w-full text-xs">
					<thead>
						<tr class="text-slate-400 border-b border-slate-700">
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
							<tr class="border-b border-slate-700/40">
								<td class="py-1.5 pr-4 text-slate-200 truncate max-w-xs">{r.artifact.intent}</td>
								<td class="py-1.5 pr-4 text-slate-400">{r.artifact.kind}</td>
								<td class="py-1.5 pr-4"><span class="badge {statusClass(r.artifact.status)}">{r.artifact.status}</span></td>
								<td class="py-1.5 pr-4 text-orange-400 font-mono">{r.target || '(global)'}</td>
								<td class="py-1.5 pr-4">
									<span class="px-1.5 py-0.5 rounded border text-[10px] {DRIFT_COLORS[r.driftStatus]}">
										{r.driftStatus === 'drifted' ? '⚠ drifted' : r.driftStatus === 'not-drifted' ? '✓ ok' : '— unchecked'}
									</span>
									{#if rechecking && recheckingId === r.artifact.id}
										<span class="text-slate-500 ml-1 animate-pulse">checking…</span>
									{:else}
										<button
											class="text-sky-400 hover:text-sky-300 ml-1"
											on:click={() => recheck(r.artifact.id)}
										>↻</button>
									{/if}
								</td>
								<td class="py-1.5 text-slate-500">{fmtDate(r.artifact.created_at)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if showCovered && coveredRows.length}
			<div class="card space-y-2">
				<h2 class="text-sm font-semibold text-green-400">✓ Covered</h2>
				<table class="w-full text-xs">
					<thead>
						<tr class="text-slate-400 border-b border-slate-700">
							<th class="text-left pb-1 pr-4">Intent</th>
							<th class="text-left pb-1 pr-4">Kind</th>
							<th class="text-left pb-1 pr-4">Status</th>
							<th class="text-left pb-1 pr-4">Drift</th>
							<th class="text-left pb-1">Host</th>
						</tr>
					</thead>
					<tbody>
						{#each coveredRows as r}
							<tr class="border-b border-slate-700/40">
								<td class="py-1 pr-4 text-slate-200 truncate max-w-xs">{r.artifact.intent}</td>
								<td class="py-1 pr-4 text-slate-400">{r.artifact.kind}</td>
								<td class="py-1 pr-4"><span class="badge {statusClass(r.artifact.status)}">{r.artifact.status}</span></td>
								<td class="py-1 pr-4">
									<span class="px-1.5 py-0.5 rounded border text-[10px] {DRIFT_COLORS[r.driftStatus]}">
										{r.driftStatus === 'drifted' ? '⚠ drifted' : r.driftStatus === 'not-drifted' ? '✓ ok' : '— unchecked'}
									</span>
									{#if rechecking && recheckingId === r.artifact.id}
										<span class="text-slate-500 ml-1 animate-pulse">checking…</span>
									{:else}
										<button
											class="text-sky-400 hover:text-sky-300 ml-1"
											on:click={() => recheck(r.artifact.id)}
										>↻</button>
									{/if}
								</td>
								<td class="py-1">
									{#if r.matchedHost}
										<a href="{base}/inventory/{r.matchedHost.id}" class="text-sky-400 hover:text-sky-300 font-mono">
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
				<h2 class="text-sm font-semibold text-slate-400">○ Unmanaged Hosts</h2>
				<p class="text-xs text-slate-500">Hosts in inventory with no active artifacts.</p>
				<div class="flex flex-wrap gap-2">
					{#each unmanagedHosts as h}
						<a
							href="{base}/inventory/{h.id}"
							class="px-2 py-1 bg-slate-700 hover:bg-slate-600 rounded text-xs font-mono text-slate-300 transition-colors"
						>
							{h.hostname}
						</a>
					{/each}
				</div>
			</div>
		{/if}

		{#if rows.length === 0 && unmanagedHosts.length === 0}
			<p class="text-slate-500 text-sm">No data — no artifacts or inventory yet.</p>
		{/if}
	{/if}
</div>