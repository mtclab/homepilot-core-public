<script lang="ts">
	// Records (#514 S4): what happened, and what we know. Tasks, the journal
	// and the knowledge base are evidence surfaces - one group, three views.
	//
	// The tab row is the shared TabBar (#549 F1); see changes/+layout.svelte.
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import TabBar from '$lib/components/TabBar.svelte';
	import { routeTabs, activeRouteTab, type RouteTabDef } from '$lib/tabs';

	const views: RouteTabDef[] = [
		{ id: 'tasks', label: 'Tasks', href: '/records/tasks' },
		{ id: 'journal', label: 'Journal', href: '/records/journal' },
		{ id: 'kb', label: 'Knowledge base', href: '/records/kb' },
	];
	$: tabs = routeTabs(views, base);
	$: activeId = activeRouteTab(views, $page.url.pathname, base);
</script>

<div class="space-y-4">
	<TabBar {tabs} {activeId} label="Records views" panelId="records-panel" />
	<div id="records-panel" role="tabpanel" aria-labelledby={activeId ? `tab-${activeId}` : undefined}>
		<slot />
	</div>
</div>
