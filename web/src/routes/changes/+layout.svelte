<script lang="ts">
	// One Changes lifecycle (#514 S4, decision D2): the queue is artifacts
	// filtered to proposed, drift is artifacts whose reality disagrees. Three
	// names for one lifecycle is how the nav got to eleven tabs.
	//
	// The tab row itself is the shared TabBar (#549 F1) — Records renders the
	// same component, and the Host page and Settings will. What used to be
	// hand-rolled here and duplicated there is now the `$lib/tabs` addressing
	// plus one widget with the WAI-ARIA roles and keyboard walk.
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import TabBar from '$lib/components/TabBar.svelte';
	import { routeTabs, activeRouteTab, type RouteTabDef } from '$lib/tabs';

	const views: RouteTabDef[] = [
		{ id: 'artifacts', label: 'Artifacts', href: '/changes', exact: true },
		{ id: 'review', label: 'Review queue', href: '/changes/review' },
		{ id: 'drift', label: 'Drift', href: '/changes/drift' },
	];
	$: tabs = routeTabs(views, base);
	// The artifact detail (/changes/{id}) belongs to the Artifacts view.
	$: activeId = activeRouteTab(views, $page.url.pathname, base, base + '/changes/');
</script>

<div class="space-y-4">
	<TabBar {tabs} {activeId} label="Changes views" panelId="changes-panel" />
	<div id="changes-panel" role="tabpanel" aria-labelledby={activeId ? `tab-${activeId}` : undefined}>
		<slot />
	</div>
</div>
