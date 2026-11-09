<script lang="ts">
	// Monitoring (owner decision 1, 2026-11-08): its own noun, out of
	// Settings. It RECEIVES what already existed - the alerts list (firing,
	// history, where they go) and the rules, notification channels and
	// webhooks that used to sit in Settings > Monitoring. The views the plan
	// adds later (capacity, storage, GPU) arrive as tabs here when their data
	// does; there is no dashboard editor, by the fence decision D3 kept.
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import TabBar from '$lib/components/TabBar.svelte';
	import { routeTabs, activeRouteTab } from '$lib/tabs';
	import { MONITORING_TABS } from '$lib/sections';

	$: tabs = routeTabs(MONITORING_TABS, base);
	$: activeId = activeRouteTab(MONITORING_TABS, $page.url.pathname, base);
</script>

<div class="page-stack">
	<h1 class="page-title">Monitoring</h1>
	<div class="section-stack">
		<TabBar {tabs} {activeId} label="Monitoring views" panelId="monitoring-panel" />
		<div id="monitoring-panel" role="tabpanel" aria-labelledby={activeId ? `tab-${activeId}` : undefined}>
			<slot />
		</div>
	</div>
</div>
