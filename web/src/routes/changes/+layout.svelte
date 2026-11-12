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
	import { shellCounts } from '$lib/nav';
	import { CHANGES_TABS } from '$lib/sections';

	// Inbox first (UI_PLAN 3.6): proposals AND approved changes still to run,
	// so the place a change waits in never looks empty while work is pending.
	// The Inbox label carries the count of what waits in it and drops it when
	// nothing does ("Inbox empty" is then the page's own sentence).
	$: waiting = $shellCounts.proposed?.n ?? 0;
	$: views = CHANGES_TABS.map((t) =>
		t.id === 'inbox' && waiting > 0 ? { ...t, label: `${t.label} ${waiting}` } : t,
	) satisfies RouteTabDef[];
	$: tabs = routeTabs(views, base);
	// A change's own page (/changes/{id}) belongs to All, whichever tab led there.
	$: activeId =
		activeRouteTab(views, $page.url.pathname, base) ||
		($page.url.pathname.startsWith(base + '/changes/') ? 'artifacts' : '');
</script>

<div class="space-y-4">
	<TabBar {tabs} {activeId} label="Changes views" panelId="changes-panel" />
	<div id="changes-panel" role="tabpanel" aria-labelledby={activeId ? `tab-${activeId}` : undefined}>
		<slot />
	</div>
</div>
