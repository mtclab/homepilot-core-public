<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { api, type DashboardSummary } from '$lib/api';
	import Donut from '$lib/components/Donut.svelte';
	import StatCard from '$lib/components/StatCard.svelte';

	let d: DashboardSummary | null = null;
	let error = '';
	let loading = true;

	// One status color language across the whole UI.
	const STATUS_COLORS: Record<string, string> = {
		online: '#34d399',
		offline: '#f87171',
		unknown: '#64748b',
		running: '#34d399',
		stopped: '#f87171'
	};
	const ROLE_COLORS = ['#38bdf8', '#a78bfa', '#fbbf24', '#34d399', '#f87171', '#64748b'];

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

	$: statusSegments = d ? toSegments(d.inventory.by_status, STATUS_COLORS) : [];
	$: roleSegments = d ? toSegments(d.inventory.by_role) : [];
	$: artifactSegments = d ? toSegments(d.artifacts) : [];
</script>

<div class="space-y-5">
	<div class="flex items-center justify-between">
		<h1 class="text-lg font-bold text-slate-100">Overview</h1>
		<button class="btn btn-ghost text-xs" on:click={load} disabled={loading}>↻ Refresh</button>
	</div>

	{#if loading && !d}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if error && !d}
		<div class="card text-sm text-slate-400">Could not load the dashboard: {error}</div>
	{:else if d}
		<div class="grid grid-cols-2 md:grid-cols-4 gap-3">
			<StatCard
				label="Coverage"
				value="{d.inventory.coverage_pct}%"
				sub="{d.inventory.managed}/{d.inventory.total} hosts managed"
				accent={d.inventory.coverage_pct >= 80 ? 'emerald' : 'yellow'}
				href="{base}/inventory"
			/>
			<StatCard
				label="Uncovered hosts"
				value={d.inventory.uncovered}
				sub="discovered, pending adoption"
				accent={d.inventory.uncovered === 0 ? 'emerald' : 'yellow'}
				href="{base}/inventory"
			/>
			<StatCard
				label="In spec"
				value="{d.drift.in_spec_pct}%"
				sub="{d.drift.drifted} drifting / {d.drift.total} checked"
				accent={d.drift.drifted === 0 ? 'emerald' : 'red'}
				href="{base}/drift"
			/>
			<StatCard
				label="Agents"
				value="{d.agents.connected}/{d.agents.known}"
				sub="connected / known"
				accent={d.agents.known > 0 && d.agents.connected === d.agents.known
					? 'emerald'
					: d.agents.connected === 0
						? 'red'
						: 'yellow'}
				href="{base}/agents"
			/>
		</div>

		<div class="grid grid-cols-1 md:grid-cols-3 gap-3">
			<div class="card">
				<h2 class="text-sm font-semibold text-slate-300 mb-3">Hosts by status</h2>
				{#if statusSegments.length}
					<Donut segments={statusSegments} centerLabel={String(d.inventory.total)} centerSub="hosts" />
				{:else}
					<p class="text-xs text-slate-500">No hosts yet.</p>
				{/if}
			</div>
			<div class="card">
				<h2 class="text-sm font-semibold text-slate-300 mb-3">Hosts by role</h2>
				{#if roleSegments.length}
					<Donut segments={roleSegments} centerLabel={String(d.inventory.total)} centerSub="hosts" />
				{:else}
					<p class="text-xs text-slate-500">No hosts yet.</p>
				{/if}
			</div>
			<div class="card">
				<h2 class="text-sm font-semibold text-slate-300 mb-3">Artifacts</h2>
				{#if artifactSegments.length}
					<Donut
						segments={artifactSegments}
						centerLabel={String(artifactSegments.reduce((s, x) => s + x.value, 0))}
						centerSub="total"
					/>
				{:else}
					<p class="text-xs text-slate-500">No artifacts yet.</p>
				{/if}
			</div>
		</div>

		{#if d.zabbix_url}
			<a
				href={d.zabbix_url}
				target="_blank"
				rel="noopener"
				class="card flex items-center justify-between hover:border-slate-500 transition-colors"
			>
				<div>
					<div class="text-sm font-semibold text-slate-200">Monitoring &amp; history</div>
					<div class="text-xs text-slate-500">
						HomePilot shows current state; open Zabbix for historical metrics and graphs.
					</div>
				</div>
				<span class="text-sky-400 text-sm">Open Zabbix ↗</span>
			</a>
		{/if}
	{/if}
</div>
