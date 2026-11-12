<script lang="ts">
	// What fires and who hears it: alert rules, notification channels and
	// webhooks, moved here from Settings > Monitoring unchanged (owner
	// decision 1). Rules sit next to the alerts they produce; Settings is
	// plumbing again.
	import AlertRules from '$lib/components/AlertRules.svelte';
	import NotificationChannels from '$lib/components/NotificationChannels.svelte';
	import WebhooksPanel from '$lib/components/WebhooksPanel.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import { sessionStore } from '$lib/api';
	import { isAdmin as capIsAdmin } from '$lib/capabilities';

	$: sessionIsAdmin = capIsAdmin($sessionStore?.capabilities);
</script>

<div class="section-stack max-w-3xl" id="monitoring-rules">
	<Panel title="Alert rules" id="alert-rules">
		<AlertRules />
	</Panel>
	<Panel title="Notification channels" id="notification-channels">
		{#if sessionIsAdmin}
			<NotificationChannels />
		{:else}
			<p class="prose-note text-xs">
				Channels decide who is woken up for an alert, and their URLs are
				credentials, so they need an admin session.
			</p>
		{/if}
	</Panel>
	<Panel title="Webhooks" id="webhooks">
		{#if sessionIsAdmin}
			<WebhooksPanel />
		{:else}
			<p class="prose-note text-xs">
				Webhooks decide where events - approval codes included - are sent, so
				they need an admin session.
			</p>
		{/if}
	</Panel>
</div>
