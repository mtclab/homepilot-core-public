<script lang="ts">
	// Event webhooks (#700): where the platform's events go. They used to exist
	// only in a CLI that opened the database, so a running install could not
	// configure one at all. Admin only - the artifact_proposed event carries the
	// approval code to these endpoints.
	import { onMount } from 'svelte';
	import { api, type WebhookConfig } from '$lib/api';
	import { notify } from '$lib/stores';

	let hooks: WebhookConfig[] = [];
	let eventTypes: string[] = [];
	let wildcard = '*';
	let loadError = '';
	let saving = false;
	// The full URL is shown once, right after it is created - never again.
	let createdUrl = '';
	let testing: number | null = null;
	let draft = { url: '', all: true, events: [] as string[], secret: '', max_retries: 3 };

	async function load() {
		try {
			const res = await api.listWebhooks();
			hooks = res.items;
			eventTypes = res.event_types;
			wildcard = res.wildcard || '*';
			loadError = '';
		} catch (e) {
			// Not "no webhooks": the list could not be read.
			loadError = String(e);
		}
	}

	function toggleEvent(name: string, on: boolean) {
		draft.events = on ? [...draft.events, name] : draft.events.filter((e) => e !== name);
	}

	async function create() {
		const events = draft.all ? [wildcard] : draft.events;
		if (!draft.url.trim()) {
			notify('Give the webhook a URL', 'err');
			return;
		}
		if (events.length === 0) {
			notify('Pick at least one event, or all of them', 'err');
			return;
		}
		saving = true;
		try {
			const made = await api.createWebhook({
				url: draft.url.trim(),
				event_types: events,
				max_retries: Number(draft.max_retries),
				...(draft.secret ? { secret: draft.secret } : {}),
			});
			createdUrl = made.url;
			draft = { url: '', all: true, events: [], secret: '', max_retries: 3 };
			await load();
			notify(`Webhook #${made.id} added`, 'ok');
		} catch (e) {
			notify('Could not add the webhook: ' + String(e), 'err');
		} finally {
			saving = false;
		}
	}

	async function remove(hook: WebhookConfig) {
		if (typeof window !== 'undefined' && !window.confirm(`Delete webhook #${hook.id} (${hook.endpoint})?`))
			return;
		try {
			await api.deleteWebhook(hook.id);
			await load();
		} catch (e) {
			notify('Could not delete the webhook: ' + String(e), 'err');
		}
	}

	async function test(hook: WebhookConfig) {
		testing = hook.id;
		try {
			const res = await api.testWebhook(hook.id);
			if (res.delivered) notify(`Test event accepted by ${res.endpoint}`, 'ok');
			else notify(`Test event NOT accepted by ${res.endpoint}`, 'err');
		} catch (e) {
			notify('Could not send the test event: ' + String(e), 'err');
		} finally {
			testing = null;
		}
	}

	onMount(load);
</script>

<div class="space-y-4" data-webhooks>
	<p class="prose-note prose-measure text-xs">
		A webhook receives the events you pick as signed JSON POSTs: changes being
		proposed, approved, applied or rolled back, drift, and alerts firing or
		resolving. The proposed-change event carries the change's approval code, so
		only point these at endpoints you control. An endpoint is listed by its
		host only - its path often carries its own credential, and is shown once,
		when you add it.
	</p>

	<div class="flex flex-wrap items-end gap-2">
		<label class="flex flex-col gap-1">
			<span class="field-label">URL</span>
			<input class="input w-72" type="url" bind:value={draft.url} placeholder="https://hooks.example.com/…" />
		</label>
		<label class="flex flex-col gap-1">
			<span class="field-label">Signing key (optional)</span>
			<input class="input w-44" type="password" autocomplete="off" bind:value={draft.secret} />
		</label>
		<label class="flex flex-col gap-1">
			<span class="field-label">Retries</span>
			<input class="input num w-16" type="number" min="0" max="10" bind:value={draft.max_retries} />
		</label>
		<button class="btn btn-primary text-xs" on:click={create} disabled={saving}>
			{saving ? 'Saving…' : 'Add webhook'}
		</button>
	</div>
	<fieldset class="text-xs space-y-1">
		<legend class="field-label">Events</legend>
		<label class="flex items-center gap-2">
			<input type="checkbox" bind:checked={draft.all} />
			Every event, including ones added later
		</label>
		{#if !draft.all}
			<div class="flex flex-wrap gap-x-4 gap-y-1">
				{#each eventTypes as name (name)}
					<label class="flex items-center gap-1 font-mono">
						<input
							type="checkbox"
							checked={draft.events.includes(name)}
							on:change={(e) => toggleEvent(name, e.currentTarget.checked)}
						/>
						{name}
					</label>
				{/each}
			</div>
		{/if}
	</fieldset>

	{#if createdUrl}
		<p class="prose-note text-xs" data-created-url>
			Added: <span class="font-mono break-all">{createdUrl}</span> - shown this once.
		</p>
	{/if}

	{#if loadError}
		<p class="text-xs text-warn" role="alert">Could not read the webhooks: {loadError}</p>
	{:else if hooks.length === 0}
		<p class="prose-note text-xs">No webhooks yet - events reach this console and the log only.</p>
	{:else}
		<table class="data-table text-xs">
			<thead>
				<tr>
					<th class="text-left">Endpoint</th>
					<th class="text-left">Events</th>
					<th class="text-left">Signed</th>
					<th class="text-left">Retries</th>
					<th class="text-left">Actions</th>
				</tr>
			</thead>
			<tbody>
				{#each hooks as h (h.id)}
					<tr class="border-b border-divider">
						<td class="font-mono text-ink">{h.endpoint}</td>
						<td class="font-mono text-muted">{h.event_types.join(', ')}</td>
						<td>{h.has_secret ? 'yes' : 'no'}</td>
						<td class="num-inline">{h.max_retries}</td>
						<td class="space-x-1">
							<button class="btn btn-ghost btn-xs" on:click={() => test(h)} disabled={testing === h.id}>
								{testing === h.id ? 'Sending…' : 'Send test'}
							</button>
							<button class="btn btn-ghost btn-xs" on:click={() => remove(h)}>Delete</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>
