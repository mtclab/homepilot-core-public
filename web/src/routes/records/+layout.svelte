<script lang="ts">
	// Records (#514 S4): what happened, and what we know. Tasks, the journal
	// and the knowledge base are evidence surfaces - one group, three views.
	import { page } from '$app/stores';
	import { base } from '$app/paths';

	const views = [
		{ href: '/records/tasks', label: 'Tasks' },
		{ href: '/records/journal', label: 'Journal' },
		{ href: '/records/kb', label: 'Knowledge base' },
	];
	$: current = $page.url.pathname;
</script>

<div class="space-y-4">
	<div class="flex gap-1 border-b border-border pb-2">
		{#each views as v (v.href)}
			<a
				href="{base}{v.href}"
				class="px-3 py-1 rounded text-sm {current.startsWith(base + v.href)
					? 'bg-raised text-accent'
					: 'text-muted hover:text-ink'}">{v.label}</a>
		{/each}
	</div>
	<slot />
</div>
