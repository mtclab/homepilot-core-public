<script lang="ts">
	// Hosts carries two views (owner decision 2, 2026-11-08): the fleet list,
	// and Agents - the agent system's chores (who is waiting to join, unused
	// bootstrap tokens, the enrolment window, the hub token), which used to
	// sit above the fleet table on every visit. A host page (/hosts/{id})
	// belongs to neither view and carries its own tabs, so no bar shows there.
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import TabBar from '$lib/components/TabBar.svelte';
	import { routeTabs, activeRouteTab } from '$lib/tabs';
	import { HOSTS_TABS } from '$lib/sections';

	$: tabs = routeTabs(HOSTS_TABS, base);
	$: activeId = activeRouteTab(HOSTS_TABS, $page.url.pathname, base);
</script>

{#if activeId}
	<div class="section-stack">
		<TabBar {tabs} {activeId} label="Hosts views" panelId="hosts-panel" />
		<div id="hosts-panel" role="tabpanel" aria-labelledby="tab-{activeId}">
			<slot />
		</div>
	</div>
{:else}
	<slot />
{/if}
