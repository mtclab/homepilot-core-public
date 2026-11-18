<script lang="ts">
	// "Get alerts on your phone" (#822). Three choices - HomePilot's own ntfy
	// server, an ntfy server you already run, ntfy.sh - each ending in a real
	// test push and a subscribe link + QR code for the ntfy app. Nothing extra
	// runs until one is chosen (owner decision 2026-11-18: opt-in).
	//
	// No answer this page reads carries HomePilot's own ntfy tokens. Two carry a
	// credential once, because the phone has to be given it: a new phone's
	// password and the ntfy.sh topic URL. They live in this component's memory
	// only, until the page is left.
	import { createEventDispatcher, onDestroy, onMount } from 'svelte';
	import {
		api,
		addPhone,
		allowPhone,
		chooseNtfySh,
		disableBundledNtfy,
		enableBundledNtfy,
		getPhoneAlerts,
		removePhone,
		setBundledNtfyPublicUrl,
		type NewPhone,
		type NtfyShChannel,
		type PhoneAlertsStatus,
		type PublicUrlCheck,
	} from '$lib/api';
	import { notify } from '$lib/stores';
	import QrCode from './QrCode.svelte';

	/** Tells the page a channel was added or removed, so its list reloads. */
	const dispatch = createEventDispatcher<{ changed: void }>();

	let status: PhoneAlertsStatus | null = null;
	let loadError = '';
	let choice: 'bundled' | 'existing' | 'ntfysh' = 'bundled';
	let busy = '';

	let addressDraft = '';
	let addressCheck: PublicUrlCheck | null = null;
	let phoneName = '';
	let newPhone: NewPhone | null = null;
	let lastTest: { delivered: boolean; error: string | null } | null = null;
	let ntfySh: NtfyShChannel | null = null;
	let ntfyShTest: { delivered: boolean; error: string | null } | null = null;
	let existing = { url: '', token: '' };
	let existingDone: { link: string; test: { delivered: boolean; error: string | null } } | null =
		null;

	let poll: ReturnType<typeof setInterval> | undefined;

	async function load() {
		try {
			status = await getPhoneAlerts();
			loadError = '';
			// This install cannot run HomePilot's own server: start on a way it can use.
			if (status.bundled_unavailable && choice === 'bundled') choice = 'existing';
			if (status.bundled && !addressDraft) addressDraft = status.bundled.public_url ?? '';
		} catch (e) {
			loadError = String(e);
		}
		// While the server is not up with HomePilot's config, look again on our
		// own: the operator is at a terminal starting it.
		const waiting = !!status?.bundled && !status.bundled.server.configured;
		if (waiting && !poll) poll = setInterval(load, 5000);
		if (!waiting && poll) {
			clearInterval(poll);
			poll = undefined;
		}
	}

	async function run<T>(label: string, fn: () => Promise<T>): Promise<T | undefined> {
		busy = label;
		try {
			return await fn();
		} catch (e) {
			notify(String(e), 'err');
			return undefined;
		} finally {
			busy = '';
		}
	}

	async function enable() {
		if (await run('enable', enableBundledNtfy)) {
			dispatch('changed');
			await load();
		}
	}

	async function disable() {
		const ok =
			typeof window === 'undefined' ||
			window.confirm(
				'Stop HomePilot\'s ntfy server? Its alert channel and tokens are removed; phone logins stay for next time.',
			);
		if (!ok) return;
		if (await run('disable', disableBundledNtfy)) {
			newPhone = null;
			lastTest = null;
			addressCheck = null;
			dispatch('changed');
			await load();
		}
	}

	async function checkAddress() {
		const result = await run('address', () => setBundledNtfyPublicUrl(addressDraft.trim()));
		if (result) {
			addressCheck = result;
			await load();
		}
	}

	async function add() {
		const made = await run('phone', () => addPhone(phoneName.trim()));
		if (made) {
			newPhone = made;
			phoneName = '';
			await load();
		}
	}

	async function allow(name: string) {
		if (await run(`allow:${name}`, () => allowPhone(name))) await load();
	}

	async function remove(name: string) {
		if (typeof window !== 'undefined' && !window.confirm(`Remove the phone login "${name}"?`)) return;
		if (await run(`remove:${name}`, () => removePhone(name))) {
			if (newPhone?.name === name) newPhone = null;
			await load();
		}
	}

	async function sendTest(channelId: number) {
		const res = await run('test', () => api.testNotificationChannel(channelId));
		dispatch('changed');
		return res ? { delivered: res.delivered, error: res.error } : null;
	}

	async function useNtfySh() {
		const made = await run('ntfysh', chooseNtfySh);
		if (made) {
			ntfySh = made;
			dispatch('changed');
			ntfyShTest = await sendTest(made.channel.id);
		}
	}

	/** The ntfy app's deep link for a topic URL (docs/subscribe/phone.md). */
	function linkFor(topicUrl: string): string {
		const u = new URL(topicUrl);
		const path = u.pathname.replace(/\/+$/, '');
		return `ntfy://${u.host}${path}?display=HomePilot${u.protocol === 'http:' ? '&secure=false' : ''}`;
	}

	async function addExisting() {
		const url = existing.url.trim();
		let link = '';
		try {
			link = linkFor(url);
		} catch {
			notify('Paste the full topic URL, e.g. https://ntfy.example.com/hp-alerts', 'err');
			return;
		}
		const made = await run('existing', () =>
			api.createNotificationChannel({
				kind: 'ntfy',
				name: 'Phone',
				url,
				...(existing.token.trim() ? { token: existing.token.trim() } : {}),
			}),
		);
		if (!made) return;
		existing = { url: '', token: '' };
		dispatch('changed');
		const test = await sendTest(made.id);
		existingDone = { link, test: test ?? { delivered: false, error: 'the test could not be sent' } };
	}

	$: b = status?.bundled ?? null;
	$: phonesThatHear = b ? b.phones.filter((p) => p.can_read) : [];
	$: done = !!b && b.server.configured && !!b.public_url_ok_at && phonesThatHear.length > 0 && !!lastTest?.delivered;

	onMount(load);
	onDestroy(() => poll && clearInterval(poll));
</script>

<div class="space-y-4" data-phone-alerts>
	{#if loadError}
		<p class="text-xs text-warn" role="alert">Could not read the phone-alert setup: {loadError}</p>
	{:else if !status}
		<p class="text-muted text-sm">Loading…</p>
	{:else if b}
		<!-- HomePilot's own ntfy server is chosen: walk it to a buzzing phone. -->
		<ol class="space-y-4">
			<li data-step="server">
				<h3 class="text-sm text-ink">1. The ntfy server {#if b.server.configured}<span class="text-ok">- running{b.server.version ? ` (ntfy ${b.server.version})` : ''}</span>{/if}</h3>
				{#if !b.server.configured}
					<p class="prose-note text-xs">
						HomePilot wrote the server's config. Start the service once, on the control
						plane's host, in the folder with HomePilot's compose file:
					</p>
					<code class="block text-xs select-all my-1" data-compose-command>{status.compose_command}</code>
					<p class="prose-note text-xs text-warn" data-server-detail>
						Waiting - {b.server.detail ?? 'not answering yet'}. This page checks again by itself.
					</p>
				{/if}
			</li>

			<li data-step="address">
				<h3 class="text-sm text-ink">2. The address your phone will use {#if b.public_url_ok_at}<span class="text-ok">- answers</span>{/if}</h3>
				<p class="prose-note text-xs">
					HomePilot cannot make a name or a public proxy appear. Give the address the phone
					reaches this server at: a LAN or VPN address, or a public name behind a reverse
					proxy (the reference vhost is <span class="font-mono">deploy/ntfy/nginx-ntfy.conf</span>).
				</p>
				<div class="flex flex-wrap items-end gap-2">
					<label class="flex flex-col gap-1">
						<span class="field-label">Server address</span>
						<input class="input w-72" type="url" autocomplete="off" bind:value={addressDraft} placeholder="https://ntfy.example.com" />
					</label>
					<button class="btn btn-ghost text-xs" on:click={checkAddress} disabled={!addressDraft.trim() || busy === 'address'}>
						{busy === 'address' ? 'Checking…' : 'Check it answers'}
					</button>
				</div>
				{#if addressCheck}
					<p class="text-xs {addressCheck.ok ? 'text-ok' : 'text-danger'}" data-address-check={addressCheck.ok ? 'ok' : 'failed'}>
						{addressCheck.detail}
					</p>
				{/if}
			</li>

			<li data-step="phone">
				<h3 class="text-sm text-ink">3. Add a phone {#if phonesThatHear.length}<span class="text-ok">- {phonesThatHear.length} added</span>{/if}</h3>
				<div class="flex flex-wrap items-end gap-2">
					<label class="flex flex-col gap-1">
						<span class="field-label">Phone name</span>
						<input class="input w-40" autocomplete="off" bind:value={phoneName} placeholder="my-phone" />
					</label>
					<button class="btn btn-primary text-xs" on:click={add} disabled={!phoneName.trim() || !b.server.configured || busy === 'phone'}>
						{busy === 'phone' ? 'Adding…' : 'Add a phone'}
					</button>
				</div>
				{#if newPhone}
					<div class="bg-canvas border border-ok-border rounded p-3 space-y-2" data-new-phone>
						<p class="text-xs text-ok">
							Login for "{newPhone.name}" - the password is shown this once and kept nowhere.
						</p>
						<p class="text-xs">Name: <code class="select-all">{newPhone.username}</code></p>
						<p class="text-xs">Password: <code class="select-all" data-phone-password>{newPhone.password}</code></p>
						<p class="prose-note text-xs">
							In the ntfy app: Settings -> Manage users -> add the server address, this name
							and password. Then scan the code (or open the link) to subscribe.
						</p>
						{#if newPhone.subscribe_link}
							<QrCode value={newPhone.subscribe_link} label="Subscribe link for the ntfy app" />
							<p class="text-xs font-mono break-all select-all">{newPhone.subscribe_link}</p>
						{:else}
							<p class="text-xs text-warn">Give the server address (step 2) to get a subscribe code.</p>
						{/if}
					</div>
				{/if}
				{#if b.phones.length}
					<ul class="text-xs space-y-1" data-phones>
						{#each b.phones as p (p.name)}
							<li class="flex items-center gap-2">
								<span class="text-ink">{p.name}</span>
								{#if !p.can_read}
									<span class="text-warn">cannot read the alerts</span>
									<button class="btn btn-ghost btn-xs" on:click={() => allow(p.name)}>Allow</button>
								{/if}
								<button class="btn btn-ghost btn-xs" on:click={() => remove(p.name)}>Remove</button>
							</li>
						{/each}
					</ul>
				{/if}
				{#if b.subscribe_link && !newPhone}
					<details class="text-xs">
						<summary>Subscribe code for a phone that already has its login</summary>
						<QrCode value={b.subscribe_link} label="Subscribe link for the ntfy app" />
						<p class="font-mono break-all select-all">{b.subscribe_link}</p>
					</details>
				{/if}
			</li>

			<li data-step="test">
				<h3 class="text-sm text-ink">4. Send a test push</h3>
				<button
					class="btn btn-ghost text-xs"
					disabled={!b.channel_id || !b.server.configured || busy === 'test'}
					on:click={async () => b?.channel_id && (lastTest = await sendTest(b.channel_id))}
				>
					{busy === 'test' ? 'Sending…' : 'Send a test push'}
				</button>
				{#if lastTest}
					<p class="text-xs {lastTest.delivered ? 'text-ok' : 'text-danger'}" data-test-result>
						{lastTest.delivered
							? 'Sent. Your phone should show "HomePilot test notification".'
							: `Not sent: ${lastTest.error ?? 'unknown error'}`}
					</p>
				{/if}
			</li>
		</ol>

		{#if done}
			<p class="text-sm text-ok" data-phone-alerts-done>Done: alerts reach your phone.</p>
		{/if}

		<div class="border border-warn rounded p-3 space-y-2" data-limit>
			<p class="prose-note text-xs">{b.limit}</p>
			<button class="btn btn-ghost btn-xs" on:click={useNtfySh} disabled={busy === 'ntfysh' || !!ntfySh}>
				Add ntfy.sh as a second channel
			</button>
		</div>

		<button class="btn btn-ghost btn-xs" on:click={disable} disabled={busy === 'disable'}>
			Stop using HomePilot's ntfy server
		</button>
	{:else}
		<!-- Nothing chosen yet: the three ways, and nothing runs until one is. -->
		<fieldset class="space-y-2">
			<legend class="field-label">Where should alerts go?</legend>
			<label class="flex items-start gap-2 text-xs">
				<input type="radio" bind:group={choice} value="bundled" disabled={!!status.bundled_unavailable} />
				<span><span class="text-ink">HomePilot's own ntfy server</span> (recommended) - runs next to
					HomePilot, private to your logins.</span>
			</label>
			{#if status.bundled_unavailable}
				<p class="prose-note text-xs text-warn ml-6" data-bundled-unavailable>
					Not available on this install: {status.bundled_unavailable}
				</p>
			{/if}
			<label class="flex items-start gap-2 text-xs">
				<input type="radio" bind:group={choice} value="existing" />
				<span><span class="text-ink">An ntfy server you already run</span> - its topic URL and an
					access token.</span>
			</label>
			<label class="flex items-start gap-2 text-xs">
				<input type="radio" bind:group={choice} value="ntfysh" />
				<span><span class="text-ink">ntfy.sh</span> - the public server, nothing to run.</span>
			</label>
		</fieldset>

		{#if choice === 'bundled'}
			<p class="prose-note text-xs">
				HomePilot writes the server's config (no anonymous access, no signup), keeps its
				tokens in the vault and creates the alert channel. You start the service once with
				<code>{status.compose_command}</code>.
			</p>
			<button class="btn btn-primary text-xs" on:click={enable} disabled={busy === 'enable'}>
				{busy === 'enable' ? 'Setting up…' : 'Set it up'}
			</button>
		{:else if choice === 'existing'}
			<div class="flex flex-wrap items-end gap-2">
				<label class="flex flex-col gap-1">
					<span class="field-label">ntfy topic URL</span>
					<input class="input w-72" type="url" autocomplete="off" bind:value={existing.url} placeholder="https://ntfy.example.com/hp-alerts" />
				</label>
				<label class="flex flex-col gap-1">
					<span class="field-label">Access token (optional)</span>
					<input class="input w-40" type="password" autocomplete="off" bind:value={existing.token} />
				</label>
				<button class="btn btn-primary text-xs" on:click={addExisting} disabled={!existing.url.trim() || busy === 'existing'}>
					{busy === 'existing' ? 'Adding…' : 'Add and send a test'}
				</button>
			</div>
		{:else}
			<p class="text-xs text-warn" data-ntfysh-warning>
				Alert text - host names, rule names, values - leaves your network, and anyone who
				learns the topic name can read it. HomePilot picks a long random topic.
			</p>
			<button class="btn btn-primary text-xs" on:click={useNtfySh} disabled={busy === 'ntfysh' || !!ntfySh}>
				Use ntfy.sh
			</button>
		{/if}

		{#if existingDone}
			<div class="space-y-2" data-existing-done>
				<p class="text-xs {existingDone.test.delivered ? 'text-ok' : 'text-danger'}">
					{existingDone.test.delivered ? 'Test push sent.' : `Test push not sent: ${existingDone.test.error ?? ''}`}
				</p>
				<QrCode value={existingDone.link} label="Subscribe link for the ntfy app" />
				<p class="text-xs font-mono break-all select-all">{existingDone.link}</p>
			</div>
		{/if}
	{/if}

	{#if ntfySh}
		<div class="bg-canvas border border-ok-border rounded p-3 space-y-2" data-ntfysh>
			<p class="text-xs text-warn">{ntfySh.warning}</p>
			<p class="text-xs">Topic (shown this once): <code class="select-all break-all">{ntfySh.topic_url}</code></p>
			<QrCode value={ntfySh.subscribe_link} label="Subscribe link for the ntfy app" />
			{#if ntfyShTest}
				<p class="text-xs {ntfyShTest.delivered ? 'text-ok' : 'text-danger'}">
					{ntfyShTest.delivered ? 'Test push sent.' : `Test push not sent: ${ntfyShTest.error ?? ''}`}
				</p>
			{/if}
		</div>
	{/if}
</div>
