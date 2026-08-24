<script lang="ts">
	// Agent enrolment (#514 S4): lived on the Agents tab, which died - getting a
	// machine under management starts from the fleet page now. Both flows:
	// single-use bootstrap token for a one-off connect, shared hub token for a
	// permanent install.
	import { onMount } from 'svelte';
	import { api, type EnrolmentWindow } from '$lib/api';
	import { notify } from '$lib/stores';
	import { isValidHubHost } from '$lib/hostValidation';
	import { base } from '$app/paths';

	export let show: 'bootstrap' | 'hub' | null = null;

	// The shared token no longer enrols an unknown host whenever it likes (#537):
	// it needs an open window, unless this install has no agents at all. Showing
	// that here is the difference between "run this one-liner" and "run this
	// one-liner and watch the agent be refused for reasons nobody mentioned".
	let window_: EnrolmentWindow | null = null;
	let windowBusy = false;
	let windowMinutes = 15;

	async function loadWindow() {
		try {
			window_ = await api.getEnrolmentWindow();
		} catch (e) {
			notify('Failed to read the enrolment window: ' + String(e), 'err');
		}
	}

	async function openWindow() {
		windowBusy = true;
		try {
			window_ = await api.openEnrolmentWindow(windowMinutes);
			notify(`Enrolment window open for ${windowMinutes} min`, 'ok');
		} catch (e) {
			notify('Failed to open the enrolment window: ' + String(e), 'err');
		} finally {
			windowBusy = false;
		}
	}

	async function closeWindow() {
		windowBusy = true;
		try {
			window_ = await api.closeEnrolmentWindow();
			notify('Enrolment window closed', 'ok');
		} catch (e) {
			notify('Failed to close the enrolment window: ' + String(e), 'err');
		} finally {
			windowBusy = false;
		}
	}

	onMount(loadWindow);

	interface TokenAnswer {
		hub_host: string;
		hub_port: number;
		hub_tls?: boolean;
		hub_cert_sha256?: string;
	}
	let bootstrapData: (TokenAnswer & { bootstrap_token: string }) | null = null;
	let hubData: (TokenAnswer & { auth_token: string }) | null = null;
	let generating = false;
	let loadingHubToken = false;

	async function generateBootstrap() {
		generating = true;
		bootstrapData = null;
		try {
			bootstrapData = await api.getBootstrapToken();
		} catch (e) {
			notify('Failed to generate bootstrap token: ' + String(e), 'err');
		} finally {
			generating = false;
		}
	}

	async function getHubToken() {
		loadingHubToken = true;
		hubData = null;
		try {
			hubData = await api.getHubToken();
		} catch (e) {
			notify('Failed to get hub token: ' + String(e), 'err');
		} finally {
			loadingHubToken = false;
		}
	}

	function fmtHost(host: string, port: number): string {
		return port === 443 ? `https://${host}` : `http://${host}:${port}`;
	}

	// The hub serves a self-signed certificate by default, so the one-liner has
	// to carry its fingerprint: that pin is the only thing the agent can verify
	// the hub against. No pin means no TLS is configured on the hub.
	function tlsArgs(d: { hub_tls?: boolean; hub_cert_sha256?: string } | null): string {
		if (!d?.hub_tls) return '';
		return d.hub_cert_sha256 ? ` --tls --tls-pin sha256:${d.hub_cert_sha256}` : ' --tls';
	}
</script>

{#if show === 'bootstrap'}
	<div class="card p-4 space-y-4">
		<h2 class="section-title">Enroll New Agent</h2>
		<p class="prose-note text-xs">
			Bootstrap tokens are single-use and expire — fine for a one-off connect, and they
			enrol a new host whether or not the enrolment window is open. For a permanent
			install that survives agent reboots, use the shared Hub Auth Token.
		</p>
		<p class="prose-note text-xs">
			A running Proxmox guest that answers on qemu-guest-agent needs none of this: open its
			<a class="text-accent hover:text-accent-strong" href="{base}/hosts">host page</a>
			and press <span class="text-ink">Install agent</span>. The one-liner below is for
			everything else — bare metal, containers, and privileged installs.
		</p>

		{#if !bootstrapData}
			<button class="btn btn-primary text-xs" on:click={generateBootstrap} disabled={generating}>
				{generating ? 'Generating…' : 'Generate Bootstrap Token'}
			</button>
		{:else}
			<div class="space-y-3">
				<div class="bg-canvas border border-ok-border rounded p-3">
					<p class="text-xs text-ok mb-1">Hub endpoint:</p>
					<code class="text-xs text-ink select-all">{fmtHost(bootstrapData.hub_host, bootstrapData.hub_port)}</code>
				</div>
				<div class="bg-canvas border border-ok-border rounded p-3">
					<p class="text-xs text-ok mb-1">Bootstrap token (copy now — shown once):</p>
					<code class="text-xs text-ink break-all select-all">{bootstrapData.bootstrap_token}</code>
				</div>
				{#if isValidHubHost(bootstrapData.hub_host)}
					<div class="bg-canvas border border-border-strong rounded p-3">
						<p class="field-label mb-1">One-liner install:</p>
						<code class="text-xs text-ink break-all select-all">
							curl -fsSL https://github.com/mtclab/homepilot-core-public/releases/latest/download/install-agent.sh | bash -s -- --hub {fmtHost(bootstrapData.hub_host, bootstrapData.hub_port)} --token {bootstrapData.bootstrap_token}{tlsArgs(bootstrapData)}
						</code>
					</div>
				{:else}
					<div class="bg-canvas border border-danger-border rounded p-3">
						<p class="text-xs text-danger font-semibold mb-1">Install one-liner unavailable</p>
						<p class="prose-note text-xs">The hub host reported by the server (<code class="text-ink break-all">{bootstrapData.hub_host}</code>) is not a valid hostname or IP. Copying a root <code class="text-ink">curl … | bash</code> with it would be unsafe. Fix <code class="text-ink">HP_HUB_HOST</code> on the server and regenerate.</p>
					</div>
				{/if}
				<button class="btn btn-ghost text-xs" on:click={generateBootstrap} disabled={generating}>
					Regenerate
				</button>
			</div>
		{/if}
	</div>
{/if}

{#if show === 'hub'}
	<div class="card p-4 space-y-3">
		<h2 class="section-title">Hub Auth Token</h2>
		<p class="prose-note text-xs">The shared token agents use to connect to the hub.</p>

		{#if window_}
			<div
				class="bg-canvas border rounded p-3 space-y-2 {window_.open
					? 'border-ok-border'
					: 'border-warn'}"
				data-testid="enrolment-window"
			>
				{#if window_.open}
					<p class="text-xs text-ok" data-testid="enrolment-window-state">
						Enrolment window open until {window_.expires_at} — the shared token can enrol a
						host this install has never seen.
					</p>
				{:else if window_.fleet_empty}
					<p class="prose-note text-xs" data-testid="enrolment-window-state">
						No agents enrolled yet, so the first host joins with the shared token whether or
						not a window is open. After that, new hosts need an open window.
					</p>
				{:else}
					<p class="text-xs text-warn" data-testid="enrolment-window-state">
						Enrolment window closed — a host this install has never seen will be refused
						with “not accepting new hosts right now”. Open a window, or enrol it with a
						single-use bootstrap token.
					</p>
				{/if}
				<div class="flex items-center gap-2">
					<label class="field-label" for="enrolment-window-minutes">Minutes</label>
					<input
						id="enrolment-window-minutes"
						class="input w-20 text-xs"
						type="number"
						min="1"
						max="1440"
						bind:value={windowMinutes}
					/>
					<button
						class="btn btn-primary text-xs"
						on:click={openWindow}
						disabled={windowBusy}
					>
						{window_.open ? 'Extend window' : 'Open window'}
					</button>
					{#if window_.open}
						<button class="btn btn-ghost text-xs" on:click={closeWindow} disabled={windowBusy}>
							Close now
						</button>
					{/if}
				</div>
			</div>
		{/if}

		{#if !hubData}
			<button class="btn btn-primary text-xs" on:click={getHubToken} disabled={loadingHubToken}>
				{loadingHubToken ? 'Loading…' : 'Show Hub Token'}
			</button>
		{:else}
			<div class="bg-canvas border border-border-strong rounded p-3">
				<p class="prose-note text-xs mb-1">Hub: <code class="text-ink">{fmtHost(hubData.hub_host, hubData.hub_port)}</code></p>
				<code class="text-xs text-ink select-all">{hubData.auth_token}</code>
			</div>
			{#if isValidHubHost(hubData.hub_host)}
				<div class="bg-canvas border border-border-strong rounded p-3">
					<p class="field-label mb-1">One-liner install (survives reboots):</p>
					<code class="text-xs text-ink break-all select-all">
						curl -fsSL https://github.com/mtclab/homepilot-core-public/releases/latest/download/install-agent.sh | bash -s -- --hub {fmtHost(hubData.hub_host, hubData.hub_port)} --token {hubData.auth_token}{tlsArgs(hubData)}
					</code>
				</div>
			{/if}
		{/if}
	</div>
{/if}
