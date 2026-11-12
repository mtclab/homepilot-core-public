<script lang="ts">
	// The full alerts list (UI slice 3): first what is being watched at all,
	// then what is firing, what fired and cleared, and who was told. The same
	// self-contained panel the host page drops in; here it is the page, with
	// the rule of each firing alert, the deliveries and "Send test".
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { api, type DashboardSummary, type Host } from '$lib/api';
	import AlertsPanel from '$lib/components/AlertsPanel.svelte';
	import Readout from '$lib/components/Readout.svelte';

	const INVENTORY_PAGE = 500;

	// Host names in the list link to the host page when the host is known;
	// a failed inventory read leaves them as plain names, not dead links.
	let fleet: Host[] = [];
	let fleetTotal: number | null = null;
	let summary: DashboardSummary | null = null;
	let loaded = false;
	onMount(async () => {
		const [inv, dash] = await Promise.allSettled([
			api.listInventory({ limit: INVENTORY_PAGE, templates: 'exclude' }),
			api.getDashboard(),
		]);
		if (inv.status === 'fulfilled') {
			fleet = inv.value.items ?? [];
			fleetTotal = inv.value.total ?? fleet.length;
		}
		summary = dash.status === 'fulfilled' ? dash.value : null;
		loaded = true;
	});

	// "Watching N/M" (plan 3.5): a host is watched when its agent is connected
	// - that is the only way a metric, and so an alert, can exist for it. An
	// empty firing list on a fleet nobody watches is silence, not calm.
	$: watched = fleet.filter((h) => h.agent_connected === true).length;
	$: partial = fleetTotal !== null && fleetTotal > fleet.length;
	$: unwatched = fleetTotal === null ? null : fleet.length - watched;
	$: rulesOn = summary?.metrics.rules_enabled ?? null;
	$: inert = summary?.metrics.rules_watching_nothing ?? 0;

	// Reactive, not a plain function: the panel renders before the inventory
	// answers, and it only re-renders its links when this prop CHANGES. A
	// function that merely closed over `fleet` left every host name plain text
	// on the real build.
	$: hostHref = (hostname: string): string => {
		const h = fleet.find((x) => x.hostname === hostname);
		return h ? `${base}/hosts/${h.id}` : '';
	};
</script>

<div class="section-stack">
	<section class="panel" aria-label="What is watched" data-alerts-coverage>
		{#if !loaded}
			<p class="panel-body text-xs text-muted">Reading what is watched...</p>
		{:else}
			<div class="readouts" style="--readouts: 3">
				{#if fleetTotal === null}
					<Readout label="Watching" value="?" sub="could not read the host inventory" tone="warn" href="{base}/hosts" />
				{:else}
					<Readout
						label="Watching"
						value={watched}
						unit="/{fleet.length} hosts"
						sub={partial
							? `with a connected agent, of the first ${fleet.length} of ${fleetTotal} read`
							: 'with a connected agent'}
						tone={fleet.length > 0 && watched === 0 ? 'crit' : 'neutral'}
						href="{base}/hosts"
					/>
				{/if}
				{#if unwatched === null}
					<Readout label="Not watched" value="?" sub="could not read the host inventory" tone="warn" />
				{:else}
					<Readout
						label="Not watched"
						value={unwatched}
						unit={unwatched === 1 ? 'host' : 'hosts'}
						sub={unwatched > 0 ? 'no agent connected - enrol one on Hosts > Agents' : 'every host read has an agent'}
						tone={unwatched > 0 ? 'warn' : 'ok'}
						href="{base}/hosts/agents"
					/>
				{/if}
				{#if rulesOn === null}
					<Readout label="Rules" value="?" sub="could not read the summary" tone="warn" href="{base}/monitoring/rules" />
				{:else}
					<Readout
						label="Rules"
						value={rulesOn}
						unit="on"
						sub={rulesOn === 0
							? 'none enabled - nothing can fire'
							: inert > 0
								? `${inert} ${inert === 1 ? 'matches' : 'match'} no host`
								: 'every rule matches a host'}
						tone={rulesOn === 0 || inert > 0 ? 'warn' : 'neutral'}
						href="{base}/monitoring/rules"
					/>
				{/if}
			</div>
		{/if}
	</section>

	<AlertsPanel {hostHref} page />
</div>
