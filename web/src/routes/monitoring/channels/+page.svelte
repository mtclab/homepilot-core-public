<script lang="ts">
	// Who hears an alert (#822): getting alerts onto a phone, the notification
	// channels, and the webhooks - moved here from the Rules tab so "what fires"
	// and "who is told" each have a place. Admin only: a channel decides who is
	// woken up, and its URLs are credentials.
	import NotificationChannels from '$lib/components/NotificationChannels.svelte';
	import PhoneAlerts from '$lib/components/PhoneAlerts.svelte';
	import WebhooksPanel from '$lib/components/WebhooksPanel.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import { sessionStore } from '$lib/api';
	import { isAdmin as capIsAdmin } from '$lib/capabilities';

	$: sessionIsAdmin = capIsAdmin($sessionStore?.capabilities);
	// The phone flow adds and removes channels; the list below re-reads them.
	let channelsVersion = 0;
</script>

<div class="section-stack max-w-3xl" id="monitoring-channels">
	{#if sessionIsAdmin}
		<Panel title="Get alerts on your phone" id="phone-alerts">
			<PhoneAlerts on:changed={() => (channelsVersion += 1)} />
		</Panel>
		<Panel title="Notification channels" id="notification-channels">
			{#key channelsVersion}
				<NotificationChannels />
			{/key}
		</Panel>
		<Panel title="Webhooks" id="webhooks">
			<WebhooksPanel />
		</Panel>
	{:else}
		<Panel title="Channels">
			<p class="prose-note text-xs">
				Channels and webhooks decide who is woken up for an alert and where events -
				approval codes included - are sent, and their URLs are credentials, so they need
				an admin session.
			</p>
		</Panel>
	{/if}
</div>
