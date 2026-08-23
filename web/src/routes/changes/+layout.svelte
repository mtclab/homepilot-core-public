<script lang="ts">
	// One Changes lifecycle (#514 S4, decision D2): the queue is artifacts
	// filtered to proposed, drift is artifacts whose reality disagrees. Three
	// names for one lifecycle is how the nav got to eleven tabs.
	import { page } from '$app/stores';
	import { base } from '$app/paths';

	const views = [
		{ href: '/changes', label: 'Artifacts', exact: true },
		{ href: '/changes/review', label: 'Review queue', exact: false },
		{ href: '/changes/drift', label: 'Drift', exact: false },
	];
	$: current = $page.url.pathname;
	function active(v: { href: string; exact: boolean }): boolean {
		return v.exact
			? current === base + v.href || current === base + v.href + '/'
			: current.startsWith(base + v.href);
	}
	// The artifact detail (/changes/{id}) belongs to the Artifacts view.
	$: detailOpen = !views.some((v) => active(v)) && current.startsWith(base + '/changes/');
</script>

<div class="space-y-4">
	<div class="flex gap-1 border-b border-border pb-2">
		{#each views as v (v.href)}
			<a
				href="{base}{v.href}"
				class="px-3 py-1 rounded text-sm {active(v) || (v.exact && detailOpen)
					? 'bg-raised text-accent'
					: 'text-muted hover:text-ink'}">{v.label}</a>
		{/each}
	</div>
	<slot />
</div>
