<script lang="ts">
	// Notification channels (#660, review 03 step 3): one field filled in and an
	// alert can reach a phone. ntfy first. Admin only - a channel decides who is
	// woken up, and its topic URL is a credential: it is sent once, stored in the
	// vault, and never shown again (the list says scheme://host only).
	import { onMount } from 'svelte';
	import { api, type NotificationChannel } from '$lib/api';
	import { notify } from '$lib/stores';
	import { timeAgo } from '$lib/relativeTime';

	let channels: NotificationChannel[] = [];
	let loadError = '';
	let saving = false;
	let testing: number | null = null;
	let draft = { name: '', url: '', token: '' };

	async function load() {
		try {
			channels = (await api.listNotificationChannels()).items;
			loadError = '';
		} catch (e) {
			// Not "no channels": the list could not be read.
			loadError = String(e);
		}
	}

	async function create() {
		if (!draft.name.trim() || !draft.url.trim()) {
			notify('Give the channel a name and its ntfy topic URL', 'err');
			return;
		}
		saving = true;
		try {
			const made = await api.createNotificationChannel({
				kind: 'ntfy',
				name: draft.name.trim(),
				url: draft.url.trim(),
				...(draft.token.trim() ? { token: draft.token.trim() } : {}),
			});
			draft = { name: '', url: '', token: '' };
			await load();
			notify(`Channel "${made.name}" added - press Send test to prove it reaches you`, 'ok');
		} catch (e) {
			notify('Could not add the channel: ' + String(e), 'err');
		} finally {
			saving = false;
		}
	}

	async function test(c: NotificationChannel) {
		testing = c.id;
		try {
			const res = await api.testNotificationChannel(c.id);
			if (res.delivered) notify(`Test message accepted by ${res.endpoint}`, 'ok');
			else notify(`Test message NOT accepted by ${res.endpoint}: ${res.error ?? 'unknown error'}`, 'err');
		} catch (e) {
			notify('Could not send the test message: ' + String(e), 'err');
		} finally {
			testing = null;
			await load();
		}
	}

	async function toggle(c: NotificationChannel) {
		try {
			await api.setNotificationChannelEnabled(c.id, !c.enabled);
			await load();
		} catch (e) {
			notify('Could not update the channel: ' + String(e), 'err');
		}
	}

	async function remove(c: NotificationChannel) {
		if (typeof window !== 'undefined' && !window.confirm(`Delete channel "${c.name}" (${c.endpoint})?`)) return;
		try {
			await api.deleteNotificationChannel(c.id);
			await load();
		} catch (e) {
			notify('Could not delete the channel: ' + String(e), 'err');
		}
	}

	const STATE_CLASS: Record<string, string> = {
		ok: 'text-ok',
		failing: 'text-danger',
		untested: 'text-warn',
		disabled: 'text-muted',
	};

	function lastDelivery(c: NotificationChannel): string {
		if (c.state === 'failing') return `failed ${timeAgo(c.last_error_at)}: ${c.last_error ?? ''}`;
		if (c.state === 'ok') return `ok ${timeAgo(c.last_ok_at)}`;
		if (c.state === 'untested') return 'never delivered - send a test';
		return 'disabled';
	}

	onMount(load);
</script>

<div class="space-y-4" data-notification-channels>
	<p class="prose-note prose-measure text-xs">
		Firing and resolved alerts are pushed to every enabled channel. For ntfy, paste
		the topic URL you subscribed to in the ntfy app, e.g.
		<span class="font-mono">https://ntfy.example.com/CHANGE_ME_matrix_room_alias</span>, and
		an access token if the topic is protected. The URL is stored in the vault and
		listed by host only - anyone holding it can read the topic. Messages carry host
		and rule names, so a public ntfy server sees them; a server on your own network
		does not.
	</p>

	<div class="flex flex-wrap items-end gap-2">
		<label class="flex flex-col gap-1">
			<span class="field-label">Name</span>
			<input class="input w-32" bind:value={draft.name} placeholder="phone" />
		</label>
		<label class="flex flex-col gap-1">
			<span class="field-label">ntfy topic URL</span>
			<input
				class="input w-72"
				type="url"
				autocomplete="off"
				bind:value={draft.url}
				placeholder="https://ntfy.example.com/topic"
			/>
		</label>
		<label class="flex flex-col gap-1">
			<span class="field-label">Access token (optional)</span>
			<input class="input w-40" type="password" autocomplete="off" bind:value={draft.token} />
		</label>
		<button class="btn btn-primary text-xs" on:click={create} disabled={saving}>
			{saving ? 'Saving…' : 'Add channel'}
		</button>
	</div>

	{#if loadError}
		<p class="text-xs text-warn" role="alert">Could not read the channels: {loadError}</p>
	{:else if channels.length === 0}
		<p class="prose-note text-xs text-warn" data-no-channels>
			No channel yet - a firing alert reaches this console and the alert history, and
			no phone.
		</p>
	{:else}
		<table class="data-table text-xs">
			<thead>
				<tr>
					<th class="text-left">Name</th>
					<th class="text-left">Endpoint</th>
					<th class="text-left">Last delivery</th>
					<th class="text-left">Actions</th>
				</tr>
			</thead>
			<tbody>
				{#each channels as c (c.id)}
					<tr class="border-b border-divider">
						<td class="text-ink">{c.name} <span class="text-muted">({c.kind})</span></td>
						<td class="font-mono text-muted">{c.endpoint}{c.has_token ? ' · token' : ''}</td>
						<td class={STATE_CLASS[c.state] ?? ''} data-channel-state={c.state}>{lastDelivery(c)}</td>
						<td class="space-x-1 whitespace-nowrap">
							<button class="btn btn-ghost btn-xs" on:click={() => test(c)} disabled={testing === c.id || !c.enabled}>
								{testing === c.id ? 'Sending…' : 'Send test'}
							</button>
							<button class="btn btn-ghost btn-xs" on:click={() => toggle(c)}>
								{c.enabled ? 'Disable' : 'Enable'}
							</button>
							<button class="btn btn-ghost btn-xs" on:click={() => remove(c)}>Delete</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>
