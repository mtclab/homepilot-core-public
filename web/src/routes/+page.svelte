<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import { api, type DashboardSummary } from '$lib/api';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import Donut from '$lib/components/Donut.svelte';
	import StatCard from '$lib/components/StatCard.svelte';

	let d: DashboardSummary | null = null;
	let error = '';
	let loading = true;

	// One status color language across the whole UI — the status tokens, never a
	// loose hex.
	const STATUS_COLORS: Record<string, string> = {
		online: 'var(--color-ok)',
		offline: 'var(--color-danger)',
		unknown: 'var(--color-muted)',
		running: 'var(--color-ok)',
		stopped: 'var(--color-danger)'
	};
	// Role is categorical, not status: it uses the neutral chart ramp so a green
	// slice never reads as "healthy".
	const ROLE_COLORS = [
		'var(--chart-1)',
		'var(--chart-2)',
		'var(--chart-3)',
		'var(--chart-4)',
		'var(--chart-5)',
		'var(--chart-6)'
	];

	function toSegments(m: Record<string, number>, colors?: Record<string, string>) {
		return Object.entries(m || {})
			.filter(([, v]) => v > 0)
			.map(([label, value], i) => ({
				label,
				value,
				color: (colors && colors[label]) || ROLE_COLORS[i % ROLE_COLORS.length]
			}));
	}

	async function load() {
		loading = true;
		try {
			d = await api.getDashboard();
		} catch (e) {
			error = String(e);
		} finally {
			loading = false;
		}
	}
	onMount(load);
	// The dashboard summary rolls up artifact + drift counts, so refresh it live
	// on any lifecycle/drift event — coalesced, so a burst is one summary call.
	const refresh = debounce(() => load(), 400);
	const unsub = onArtifactEvent(refresh);
	onDestroy(() => {
		unsub();
		refresh.cancel();
	});

	$: statusSegments = d ? toSegments(d.inventory.by_status, STATUS_COLORS) : [];
	$: roleSegments = d ? toSegments(d.inventory.by_role) : [];
	$: artifactSegments = d ? toSegments(d.artifacts) : [];
</script>

<div class="space-y-5">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Overview</h1>
		<button class="btn btn-ghost text-xs" on:click={load} disabled={loading}>↻ Refresh</button>
	</div>

	{#if loading && !d}
		<p class="text-muted text-sm">Loading…</p>
	{:else if error && !d}
		<div class="card text-sm text-muted">Could not load the dashboard: {error}</div>
	{:else if d}
		<div class="grid grid-cols-2 md:grid-cols-4 gap-3">
			<StatCard
				label="Coverage"
				value="{d.inventory.coverage_pct}%"
				sub="{d.inventory.managed}/{d.inventory.total} hosts managed"
				accent={d.inventory.coverage_pct >= 80 ? 'ok' : 'warn'}
				href="{base}/inventory"
			/>
			<StatCard
				label="Uncovered hosts"
				value={d.inventory.uncovered}
				sub="discovered, pending adoption"
				accent={d.inventory.uncovered === 0 ? 'ok' : 'warn'}
				href="{base}/inventory"
			/>
			<StatCard
				label="In spec"
				value="{d.drift.in_spec_pct}%"
				sub="{d.drift.drifted} drifting / {d.drift.total} checked"
				accent={d.drift.drifted === 0 ? 'ok' : 'danger'}
				href="{base}/drift"
			/>
			<StatCard
				label="Agents"
				value="{d.agents.connected}/{d.agents.known}"
				sub="connected / known"
				accent={d.agents.known > 0 && d.agents.connected === d.agents.known
					? 'ok'
					: d.agents.connected === 0
						? 'danger'
						: 'warn'}
				href="{base}/agents"
			/>
		</div>

		<div class="grid grid-cols-1 md:grid-cols-3 gap-3">
			<div class="card">
				<h2 class="section-title mb-3">Hosts by status</h2>
				{#if statusSegments.length}
					<Donut segments={statusSegments} centerLabel={String(d.inventory.total)} centerSub="hosts" />
				{:else}
					<p class="prose-note text-xs">No hosts yet.</p>
				{/if}
			</div>
			<div class="card">
				<h2 class="section-title mb-3">Hosts by role</h2>
				{#if roleSegments.length}
					<Donut segments={roleSegments} centerLabel={String(d.inventory.total)} centerSub="hosts" />
				{:else}
					<p class="prose-note text-xs">No hosts yet.</p>
				{/if}
			</div>
			<div class="card">
				<h2 class="section-title mb-3">Artifacts</h2>
				{#if artifactSegments.length}
					<Donut
						segments={artifactSegments}
						centerLabel={String(artifactSegments.reduce((s, x) => s + x.value, 0))}
						centerSub="total"
					/>
				{:else}
					<p class="prose-note text-xs">No artifacts yet.</p>
				{/if}
			</div>
		</div>

		<a
			href="{base}/agents"
			class="card flex items-center justify-between hover:border-accent transition-colors"
		>
			<div>
				<div class="section-title">Monitoring &amp; history</div>
				<div class="text-xs text-muted">
					Agents report system metrics over the hub. Kept for {d.metrics.retention_days} days.
				</div>
			</div>
			<span class="flex items-center gap-3 text-sm">
				{#if d.metrics.firing_alerts > 0}
					<span class="badge badge-failed"
						>{d.metrics.firing_alerts} alert{d.metrics.firing_alerts === 1 ? '' : 's'} firing</span
					>
				{:else}
					<span class="text-muted text-xs">No alerts firing</span>
				{/if}
				<span class="text-accent">Host metrics →</span>
			</span>
		</a>
	{/if}
</div>
