<script lang="ts">
	// A moved address keeps working (#514 S4 gate: every pre-move URL
	// redirects). Client-side on purpose: the UI ships as a static SPA with an
	// index.html fallback, so this component IS the redirect mechanism.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';

	export let to: string;
	// Carry the query string: /inventory?q=web01 must land on /hosts?q=web01.
	onMount(() => {
		const qs = typeof window !== 'undefined' ? window.location.search : '';
		void goto(`${base}${to}${qs}`, { replaceState: true });
	});
</script>

<p class="text-muted text-sm">
	This page moved. <a class="text-accent" href="{base}{to}">Continue to {to}</a>
</p>
