<script lang="ts">
	// What fires: the alert rules, moved here from Settings > Monitoring
	// (owner decision 1). Rules sit next to the alerts they produce. Who hears
	// them - notification channels, webhooks, phone alerts - is the Channels
	// tab (#822).
	import AlertRules from '$lib/components/AlertRules.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import { base } from '$app/paths';
</script>

<div class="section-stack max-w-3xl" id="monitoring-rules">
	<Panel title="Alert rules" id="alert-rules">
		<AlertRules />
	</Panel>
	<p class="prose-note text-xs">
		Who is told when a rule fires - your phone, notification channels, webhooks - is set
		up under <a href="{base}/monitoring/channels">Channels</a>.
	</p>
</div>
