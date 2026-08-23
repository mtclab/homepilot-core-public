<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { api, setToken, hasCookieSession, refreshSession } from '$lib/api';
	import { notify } from '$lib/stores';
	import { safeReturnTo } from '$lib/nav';

	let token = '';
	let error = '';
	let loading = false;

	// 'checking' until the backend has said whether it has ever been claimed.
	// An unclaimed instance has no token to type, so the token form would be a
	// dead end — it shows the claim screen instead.
	let mode: 'checking' | 'claim' | 'login' = 'checking';
	// True only when this browser reaches the instance from OUTSIDE its own
	// network. On the local network the claim needs nothing typed at all.
	let codeRequired = false;

	let claimCode = '';
	let pveHost = '';
	let pvePort: number | '' = '';
	let pveToken = '';
	let pveVerifySsl = true;

	function landing(): string {
		const params = new URLSearchParams(window.location.search);
		return safeReturnTo(params.get('returnTo'), base, `${base}/changes`);
	}

	onMount(async () => {
		try {
			const status = await api.claimStatus();
			if (status.state === 'unclaimed') {
				codeRequired = status.code_required === true;
				mode = 'claim';
				return;
			}
		} catch {
			// A backend without /claim (or unreachable) still has the token form.
		}
		mode = 'login';
		if (hasCookieSession()) {
			try {
				await api.me();
				goto(landing());
			} catch {
				// cookie stale, show login
			}
		}
	});

	async function handleLogin() {
		error = '';
		loading = true;
		try {
			setToken(token.trim());
			await api.login(token.trim());
			await refreshSession();
			notify('Connected');
			goto(landing());
		} catch (e: any) {
			error = e?.message || 'Connection failed — check your token';
			setToken('');
		} finally {
			loading = false;
		}
	}

	async function handleClaim() {
		error = '';
		loading = true;
		try {
			const result = await api.claimInstance({
				code: claimCode.trim() || undefined,
				proxmox_host: pveHost.trim() || undefined,
				proxmox_port: pvePort === '' ? undefined : Number(pvePort),
				proxmox_token: pveToken.trim() || undefined,
				proxmox_verify_ssl: pveVerifySsl,
			});
			// From here on this is the ordinary login path: the claim hands back an
			// admin token, and the session is established exactly as a paste of that
			// token would establish it.
			setToken(result.token);
			await api.login(result.token);
			await refreshSession();
			notify(result.proxmox_configured ? 'Claimed — Proxmox connected' : 'Claimed');
			goto(landing());
		} catch (e: any) {
			error = e?.message || 'Claim failed — check the code';
			setToken('');
		} finally {
			loading = false;
		}
	}

	// The claim button is live without a code on the local path, so Enter is too.
	$: claimReady = !codeRequired || claimCode.trim().length > 0;

	function handleKeydown(e: KeyboardEvent) {
		if (e.key !== 'Enter' || loading) return;
		if (mode === 'login' && token.trim()) handleLogin();
		if (mode === 'claim' && claimReady) handleClaim();
	}
</script>

<div class="min-h-screen flex items-center justify-center bg-canvas">
	<div class="w-full max-w-sm">
		<div class="text-center mb-8">
			<span class="wordmark text-2xl">HomePilot</span>
			<p class="prose-note text-sm mt-2">
				{#if mode === 'claim'}
					This instance has not been claimed yet
				{:else}
					Enter your API token to connect
				{/if}
			</p>
		</div>

		{#if mode === 'checking'}
			<div class="card">
				<p class="prose-note text-sm">Checking this instance…</p>
			</div>
		{:else if mode === 'claim'}
			<div class="card space-y-4">
				{#if codeRequired}
					<div class="space-y-1">
						<label class="field-label" for="claim-code">Claim code</label>
						<input
							id="claim-code"
							class="input w-full font-mono"
							placeholder="hpc_…"
							autocomplete="off"
							bind:value={claimCode}
							on:keydown={handleKeydown}
						/>
						<p class="prose-note text-xs">
							You are reaching this instance from outside its own network, so it asks for the
							claim code. Run <code class="text-muted">hp claim-code</code> on the host to read
							it. From the local network no code is needed.
						</p>
					</div>
				{:else}
					<p class="prose-body text-sm">
						You are on this instance's own network, so you can claim it now — no code, no
						shell. Anyone else on this network could claim it just as easily, so do it now.
						Claiming creates the first admin credential and closes this screen for good.
					</p>
				{/if}

				<div class="space-y-1">
					<label class="field-label" for="claim-pve-host">
						Proxmox address <span class="text-muted">(optional)</span>
					</label>
					<input
						id="claim-pve-host"
						class="input w-full"
						placeholder="pve.example.com"
						bind:value={pveHost}
						on:keydown={handleKeydown}
					/>
				</div>

				<div class="space-y-1">
					<label class="field-label" for="claim-pve-port">
						Proxmox port <span class="text-muted">(default 8006)</span>
					</label>
					<input
						id="claim-pve-port"
						type="number"
						class="input w-full"
						placeholder="8006"
						bind:value={pvePort}
						on:keydown={handleKeydown}
					/>
				</div>

				<div class="space-y-1">
					<label class="field-label" for="claim-pve-token">
						Proxmox API token <span class="text-muted">(optional)</span>
					</label>
					<input
						id="claim-pve-token"
						type="password"
						class="input w-full font-mono"
						placeholder="user@pve!tokenid=uuid"
						bind:value={pveToken}
						on:keydown={handleKeydown}
					/>
					<p class="prose-note text-xs">
						Checked against the live API before it is stored. You can leave both blank and add
						Proxmox later in Settings.
					</p>
				</div>

				<label class="field-label flex items-center gap-2 text-ink">
					<input type="checkbox" class="rounded border-border-strong" bind:checked={pveVerifySsl} />
					Verify SSL certificate
				</label>
				<p class="prose-note text-xs">
					Proxmox ships a self-signed certificate; clear this if verification fails.
				</p>

				{#if error}
					<p class="text-xs text-danger">{error}</p>
				{/if}

				<button
					class="btn btn-primary w-full"
					disabled={!claimReady || loading}
					on:click={handleClaim}
				>
					{loading ? 'Claiming…' : 'Claim this instance'}
				</button>
			</div>
		{:else}
			<div class="card space-y-4">
				<div class="space-y-1">
					<label class="field-label" for="login-token">API Token</label>
					<input
						id="login-token"
						type="password"
						class="input w-full font-mono"
						placeholder="hp_..."
						bind:value={token}
						on:keydown={handleKeydown}
					/>
				</div>

				{#if error}
					<p class="text-xs text-danger">{error}</p>
				{/if}

				<button
					class="btn btn-primary w-full"
					disabled={!token.trim() || loading}
					on:click={handleLogin}
				>
					{loading ? 'Connecting…' : 'Connect'}
				</button>
			</div>

			<p class="prose-note text-xs text-center mt-4">
				Create tokens with <code class="text-muted">hp token create</code>
			</p>
		{/if}
	</div>
</div>
