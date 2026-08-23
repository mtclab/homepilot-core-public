<script lang="ts">
	// Agent enrolment (#514 S4): lived on the Agents tab, which died - getting a
	// machine under management starts from the fleet page now. Both flows:
	// single-use bootstrap token for a one-off connect, shared hub token for a
	// permanent install.
	import { api } from '$lib/api';
	import { notify } from '$lib/stores';
	import { isValidHubHost } from '$lib/hostValidation';
	import { base } from '$app/paths';

	export let show: 'bootstrap' | 'hub' | null = null;

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
			Bootstrap tokens are single-use and expire — fine for a one-off connect. For a
			permanent install that survives agent reboots, use the shared Hub Auth Token.
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
