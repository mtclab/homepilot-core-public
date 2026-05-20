<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { api, setToken, getToken, hasCookieSession } from '$lib/api';
	import { notify } from '$lib/stores';
	import { validateApiBase } from '$lib/urlValidation';

	let tokenVal = '';
	let apiBase = '';
	let health: string | null = null;
	let checking = false;
	let cookieSession = false;

	$: urlValidation = validateApiBase(apiBase);

	onMount(() => {
		tokenVal = getToken();
		apiBase =
			typeof localStorage !== 'undefined' ? (localStorage.getItem('hp_api_base') ?? '') : '';
		cookieSession = hasCookieSession();
	});

	async function save() {
		if (urlValidation.error) {
			notify(urlValidation.error, 'err');
			return;
		}
		const token = tokenVal.trim();
		setToken(token);
		if (apiBase.trim()) {
			localStorage.setItem('hp_api_base', apiBase.trim());
		} else {
			localStorage.removeItem('hp_api_base');
		}
		if (token) {
			try {
				await api.login(token);
				cookieSession = true;
				notify('Settings saved — session cookie set');
				const params = new URLSearchParams(window.location.search);
				const returnTo = params.get('returnTo');
				if (returnTo) goto(decodeURIComponent(returnTo));
			} catch {
				cookieSession = false;
				notify('Settings saved (cookie session failed — using in-memory token)');
			}
		} else {
			notify('Settings saved');
		}
	}

	async function logout() {
		try {
			await api.logout();
		} catch {
			// ignore network errors on logout
		}
		setToken('');
		tokenVal = '';
		cookieSession = false;
		notify('Logged out');
	}

	async function testConnection() {
		if (urlValidation.error) {
			notify(urlValidation.error, 'err');
			return;
		}
		checking = true;
		health = null;
		try {
			const base = apiBase.trim() || '';
			const res = await fetch(`${base}/health`, { credentials: 'include' });
			const data = await res.json();
			health = res.ok ? `✓ ${data.status} — v${data.version}` : `✗ ${res.status}`;
		} catch (e) {
			health = `✗ ${String(e)}`;
		} finally {
			checking = false;
		}
	}
</script>

<div class="space-y-6 max-w-lg">
	<h1 class="text-lg font-bold text-slate-100">Settings</h1>

	<div class="card space-y-4">
		<h2 class="text-sm font-semibold text-slate-300">API Connection</h2>

		<div class="space-y-1">
			<label class="text-xs text-slate-400" for="api-base">API Base URL</label>
			<input
				id="api-base"
				class="input w-full {urlValidation.error ? 'border-red-500' : urlValidation.warning ? 'border-yellow-500' : ''}"
				placeholder="http://localhost:8000 (leave blank = same origin)"
				bind:value={apiBase}
			/>
			{#if urlValidation.error}
				<p class="text-xs text-red-400">{urlValidation.error}</p>
			{:else if urlValidation.warning}
				<p class="text-xs text-yellow-400">{urlValidation.warning}</p>
			{:else}
				<p class="text-xs text-slate-600">Used when the web UI is served from a different origin than the API.</p>
			{/if}
		</div>

		<div class="space-y-1">
			<label class="text-xs text-slate-400" for="token">API Token</label>
			<input
				id="token"
				type="password"
				class="input w-full font-mono"
				placeholder="Bearer token from hp token create"
				bind:value={tokenVal}
			/>
			<p class="text-xs {cookieSession ? 'text-green-500' : 'text-slate-600'}">
				{#if cookieSession}
					Session active — token stored in HttpOnly cookie, cleared on browser close.
				{:else}
					No active session. Paste token and save to log in. Create tokens with <code class="text-slate-400">hp token create</code>.
				{/if}
			</p>
		</div>

		<div class="flex gap-3 items-center flex-wrap">
			<button class="btn btn-primary" on:click={save}>Save</button>
			{#if cookieSession}
				<button class="btn btn-ghost" on:click={logout}>Log out</button>
			{/if}
			<button class="btn btn-ghost" disabled={checking} on:click={testConnection}>
				{checking ? 'Checking…' : 'Test connection'}
			</button>
			{#if health}
				<span class="text-xs {health.startsWith('✓') ? 'text-green-400' : 'text-red-400'}">{health}</span>
			{/if}
		</div>
	</div>

	<div class="card space-y-2">
		<h2 class="text-sm font-semibold text-slate-300">About</h2>
		<dl class="text-xs space-y-1">
			<div class="flex gap-4">
				<dt class="text-slate-500 w-24">Version</dt>
				<dd class="text-slate-300">HomePilot v2</dd>
			</div>
			<div class="flex gap-4">
				<dt class="text-slate-500 w-24">Chat</dt>
				<dd class="text-slate-300">Use opencode or Claude Code — no chat UI here</dd>
			</div>
		</dl>
	</div>
</div>
