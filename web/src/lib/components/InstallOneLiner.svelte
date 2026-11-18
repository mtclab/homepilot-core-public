<script lang="ts">
	// The install one-liner the SERVER built for a token (lane A3), and where it
	// installs from. The one rendering of it: Enroll agent shows it for a
	// bootstrap token and for the shared token, and Re-enrol shows it on a
	// refused host's line (#789) - one builder, never a second hand-rolled command.
	import type { HubEndpoint } from '$lib/api';
	import { isValidHubHost } from '$lib/hostValidation';

	export let data: HubEndpoint;
	export let label = 'One-liner install (run as root on the host):';
	// Say so when there is no safe command, rather than show nothing.
	export let explainMissing = true;

	// The server knows whether this control plane can serve the installer and
	// agent binary, so a host installed by hand gets THIS hub's agent version -
	// not GitHub's latest. A hub address that is not a valid host never goes
	// into a root shell command.
	$: command = data.install_command && isValidHubHost(data.hub_host) ? data.install_command : null;
</script>

{#if command}
	<div class="bg-canvas border border-border-strong rounded p-3 space-y-1">
		<p class="field-label mb-1">{label}</p>
		<code class="text-xs text-ink break-all select-all" data-testid="install-command">{command}</code>
		{#if data.install_source === 'control_plane'}
			<p class="prose-note text-xs" data-testid="install-source">
				Installs agent {data.install_version} from this HomePilot - the host needs no internet
				access, and its agent matches this control plane.
				{#if data.installer_sha256}It checks the installer against sha256
					<code class="text-ink">{data.installer_sha256.slice(0, 12)}…</code> before running it, and
					refuses one that does not match.{/if}
			</p>
		{:else if data.install_source === 'github'}
			<p class="text-xs text-warn" data-testid="install-source">
				This HomePilot cannot serve the agent itself ({data.install_fallback_reason}), so this
				one-liner fetches release {data.install_version} from GitHub instead. The host needs
				internet access.
			</p>
		{/if}
	</div>
{:else if explainMissing}
	<div class="bg-canvas border border-danger-border rounded p-3">
		<p class="text-xs text-danger font-semibold mb-1">Install one-liner unavailable</p>
		<p class="prose-note text-xs">The hub host reported by the server (<code class="text-ink break-all">{data.hub_host}</code>) is not a valid hostname or IP. Copying a root <code class="text-ink">curl … | bash</code> with it would be unsafe. Fix <code class="text-ink">HP_HUB_HOST</code> on the server and regenerate.</p>
	</div>
{/if}
