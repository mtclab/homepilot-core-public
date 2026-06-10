<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import { api, setToken, hasCookieSession, refreshSession } from '$lib/api';
	import { notify } from '$lib/stores';

	let token = '';
	let error = '';
	let loading = false;

	onMount(async () => {
		if (hasCookieSession()) {
			try {
				await api.me();
				const params = new URLSearchParams(window.location.search);
				const returnTo = params.get('returnTo') || `${base}/artifacts`;
				goto(decodeURIComponent(returnTo));
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
			const params = new URLSearchParams(window.location.search);
			const returnTo = params.get('returnTo') || `${base}/artifacts`;
			goto(decodeURIComponent(returnTo));
		} catch (e: any) {
			error = e?.message || 'Connection failed — check your token';
			setToken('');
		} finally {
			loading = false;
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && token.trim() && !loading) {
			handleLogin();
		}
	}
</script>

<div class="min-h-screen flex items-center justify-center bg-slate-900">
	<div class="w-full max-w-sm">
		<div class="text-center mb-8">
			<span class="text-sky-400 font-bold text-lg tracking-widest">HOMEPILOT</span>
			<p class="text-slate-500 text-sm mt-2">Enter your API token to connect</p>
		</div>

		<div class="card space-y-4">
			<div class="space-y-1">
				<label class="text-xs text-slate-400" for="login-token">API Token</label>
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
				<p class="text-xs text-red-400">{error}</p>
			{/if}

			<button
				class="btn btn-primary w-full"
				disabled={!token.trim() || loading}
				on:click={handleLogin}
			>
				{loading ? 'Connecting…' : 'Connect'}
			</button>
		</div>

		<p class="text-xs text-slate-600 text-center mt-4">
			Create tokens with <code class="text-slate-500">hp token create</code>
		</p>
	</div>
</div>