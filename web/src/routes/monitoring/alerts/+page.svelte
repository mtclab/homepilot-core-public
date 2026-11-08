<script lang="ts">
	// The full alerts list: what is firing, what fired and cleared, and where
	// alerts are sent. The same self-contained panel the Overview and the host
	// page drop in; here it is the page.
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { api, type Host } from '$lib/api';
	import AlertsPanel from '$lib/components/AlertsPanel.svelte';

	// Host names in the list link to the host page when the host is known;
	// a failed inventory read leaves them as plain names, not dead links.
	let fleet: Host[] = [];
	onMount(async () => {
		try {
			fleet = (await api.listInventory({ limit: 500, templates: 'exclude' })).items;
		} catch {
			fleet = [];
		}
	});

	function hostHref(hostname: string): string {
		const h = fleet.find((x) => x.hostname === hostname);
		return h ? `${base}/hosts/${h.id}` : '';
	}
</script>

<AlertsPanel {hostHref} />
