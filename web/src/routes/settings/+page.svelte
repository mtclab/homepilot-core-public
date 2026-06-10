<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { api, setToken, getToken, hasCookieSession, type HealthInfo } from '$lib/api';
	import { notify } from '$lib/stores';
	import { validateApiBase } from '$lib/urlValidation';

	let tokenVal = '';
	let apiBase = '';
	let health: string | null = null;
	let checking = false;
	let cookieSession = false;
	let currentScope = '';
	let currentLabel = '';

	$: urlValidation = validateApiBase(apiBase);

	let healthData: HealthInfo | null = null;
	let loadingHealth = false;

	// Proxmox settings
	let pveHost = '';
	let pvePort = 8006;
	let pveVerifySsl = true;
	let pveToken = '';
	let pveWriteToken = '';
	let pveStatus = '';
	let pveTokenConfigured = false;
	let pveTokenSource = '';
	let pveWriteTokenConfigured = false;
	let pveWriteTokenSource = '';
	let pveWriteTokenIsSeparate = false;
	let pveLoading = false;
	let pveSaving = false;
	let pveTesting = false;
	let pveTestResult: { status: string; message: string } | null = null;

	async function loadProxmoxSettings() {
		pveLoading = true;
		try {
			const data = await api.getProxmoxSettings();
			pveHost = data.host || '';
			pvePort = data.port || 8006;
			pveVerifySsl = data.verify_ssl !== false;
			pveTokenConfigured = data.token_configured;
			pveTokenSource = data.token_source || '';
			pveWriteTokenConfigured = data.write_token_configured;
			pveWriteTokenSource = data.write_token_source || '';
			pveWriteTokenIsSeparate = data.write_token_is_separate ?? false;
			pveStatus = data.connection_status || '';
		} catch (e) {
			// may fail if not authenticated — ignore
		} finally {
			pveLoading = false;
		}
	}

	async function saveProxmox() {
		pveSaving = true;
		pveTestResult = null;
		try {
			const data: Record<string, unknown> = {};
			if (pveHost) data.host = pveHost;
			if (pvePort) data.port = pvePort;
			data.verify_ssl = pveVerifySsl;
			if (pveToken) data.token = pveToken;
			if (pveWriteToken) data.write_token = pveWriteToken;
			const result = await api.saveProxmoxSettings(data);
			pveTokenConfigured = result.token_configured ?? !!pveToken;
			pveWriteTokenConfigured = result.write_token_configured ?? !!pveWriteToken;
			pveWriteTokenIsSeparate = !!(pveWriteToken && pveWriteToken !== pveToken);
			pveStatus = 'ok';
			pveToken = '';
			pveWriteToken = '';
			notify('Proxmox settings saved and reloaded');
			await checkHealth();
		} catch (e) {
			notify('Failed to save Proxmox settings: ' + String(e), 'err');
		} finally {
			pveSaving = false;
		}
	}

	async function testProxmox() {
		pveTesting = true;
		pveTestResult = null;
		try {
			const data: Record<string, unknown> = { host: pveHost, port: pvePort, verify_ssl: pveVerifySsl };
			if (pveToken) data.token = pveToken;
			if (pveWriteToken) data.write_token = pveWriteToken;
			const result = await api.testProxmoxConnection(data);
			pveTestResult = result;
		} catch (e) {
			pveTestResult = { status: 'error', message: String(e) };
		} finally {
			pveTesting = false;
		}
	}

	async function checkHealth() {
		loadingHealth = true;
		healthData = null;
		try {
			healthData = await api.getHealth();
		} catch (e) {
			notify('Failed to load health: ' + String(e), 'err');
		} finally {
			loadingHealth = false;
		}
	}

	onMount(() => {
		tokenVal = getToken();
		apiBase =
			typeof localStorage !== 'undefined' ? (localStorage.getItem('hp_api_base') ?? '') : '';
		cookieSession = hasCookieSession();
		checkHealth();
		loadProxmoxSettings();
		if (cookieSession || tokenVal) {
			api.me().then((info) => {
				currentScope = info.scope || '';
				currentLabel = info.token_label || '';
			}).catch(() => {});
		}
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
				const me = await api.login(token);
				cookieSession = true;
				try {
					const info = await api.me();
					currentScope = info.scope || '';
					currentLabel = info.token_label || '';
				} catch {}
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
		currentScope = '';
		currentLabel = '';
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
					{#if currentScope}
						<span class="text-slate-400">Scope: <span class="font-mono {currentScope === 'full' || currentScope === '*' || currentScope === 'admin' ? 'text-emerald-400' : 'text-yellow-400'}">{currentScope}</span></span>
						{#if currentScope !== 'full' && currentScope !== '*' && currentScope !== 'admin'}
							<br /><span class="text-yellow-500">Write actions (save settings, manage tokens) require an admin/full-scope token.</span>
						{/if}
					{/if}
				{:else}
					No active session. Paste token and save to log in. Create tokens with <code class="text-slate-400">hp token create</code>.
				{/if}
			</p>
			{#if cookieSession && (currentScope === 'full' || currentScope === '*' || currentScope === 'admin')}
				<a href="/ui/tokens" class="text-xs text-sky-400 hover:underline">Manage API tokens →</a>
			{/if}
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

	<div class="card space-y-3">
		<h2 class="text-sm font-semibold text-slate-300">System Health</h2>
		{#if loadingHealth}
			<p class="text-xs text-slate-500">Loading…</p>
		{:else if healthData}
			<dl class="text-xs space-y-1">
				{#each Object.entries(healthData.checks ?? healthData) as [key, val]}
					{#if typeof val === 'string'}
						<div class="flex gap-4">
							<dt class="text-slate-500 w-28 capitalize">{key.replace(/_/g, ' ')}</dt>
							<dd class:text-emerald-400={val === 'ok'}
							    class:text-yellow-400={val === 'not_configured'}
							    class:text-red-400={val === 'error' || val === 'unreachable' || val === 'locked'}
							    class:text-slate-300={!['ok','not_configured','error','unreachable','locked'].includes(val)}
							>{val}</dd>
						</div>
					{/if}
				{/each}
			</dl>
			{#if healthData.checks?.proxmox === 'not_configured'}
				<div class="mt-3 p-3 bg-slate-900 border border-slate-700 rounded text-xs text-slate-400">
					<p class="font-semibold text-slate-300 mb-1">Proxmox not configured</p>
					<p>Configure Proxmox in the <strong>Proxmox Connection</strong> section below, or set <code class="text-slate-300">HP_PROXMOX_HOST</code> and <code class="text-slate-300">PVE_API_TOKEN</code> in your environment.</p>
				</div>
			{:else if healthData.checks?.proxmox === 'unreachable'}
				<div class="mt-3 p-3 bg-slate-900 border border-red-700/50 rounded text-xs text-slate-400">
					<p class="font-semibold text-red-400 mb-1">Proxmox unreachable</p>
					<p>The configured Proxmox host is not responding. Check the host address and network connectivity, or reconfigure in the <strong>Proxmox Connection</strong> section below.</p>
				</div>
			{/if}
		{:else}
			<p class="text-xs text-slate-500">Could not load health status.</p>
		{/if}
	</div>

	<div class="card space-y-4">
		<h2 class="text-sm font-semibold text-slate-300">Proxmox Connection</h2>

		{#if pveLoading}
			<p class="text-xs text-slate-500">Loading…</p>
		{:else}
			<div class="space-y-1">
				<label class="text-xs text-slate-400" for="pve-host">Host</label>
				<input id="pve-host" class="input w-full" placeholder="pve.example.com" bind:value={pveHost} />
			</div>

			<div class="space-y-1">
				<label class="text-xs text-slate-400" for="pve-port">Port</label>
				<input id="pve-port" type="number" class="input w-full" placeholder="8006" bind:value={pvePort} />
			</div>

			<div class="space-y-1">
				<label class="text-xs text-slate-400" for="pve-token">Read API Token</label>
				<input id="pve-token" type="password" class="input w-full font-mono" placeholder={pveTokenConfigured ? '•••••••• (configured)' : 'user@pve!tokenid=uuid'} bind:value={pveToken} />
				<p class="text-xs text-slate-600">
					{#if pveTokenConfigured}
						Token stored in vault (source: <span class="text-slate-400">{pveTokenSource}</span>). Leave blank to keep existing.
					{:else}
						Enter a PVE API token with read access. Create one in the PVE UI under <em>Datacenter → Permissions → API Tokens</em>.
					{/if}
				</p>
			</div>

			<div class="space-y-1">
				<label class="text-xs text-slate-400" for="pve-write-token">Write API Token <span class="text-slate-600">(optional)</span></label>
				<input id="pve-write-token" type="password" class="input w-full font-mono" placeholder={pveWriteTokenIsSeparate ? '•••••••• (configured)' : 'Same as read token'} bind:value={pveWriteToken} />
				<p class="text-xs text-slate-600">
					{#if pveWriteTokenIsSeparate}
						Write token stored in vault (source: <span class="text-slate-400">{pveWriteTokenSource}</span>). Used for VM/LXC create, delete, snapshot, config changes.
					{:else if pveWriteTokenConfigured}
						Write operations use the read token (no separate write token). Leave blank to keep.
					{:else}
						Separate token with write permissions (PVEVMAdmin, etc.). If unset, the read token is used for everything.
					{/if}
				</p>
			</div>

			<label class="flex items-center gap-2 text-xs text-slate-300">
				<input type="checkbox" class="rounded border-slate-600" bind:checked={pveVerifySsl} />
				Verify SSL certificate
			</label>

			<div class="flex gap-3 items-center flex-wrap">
				<button class="btn btn-primary" disabled={pveSaving || !pveHost} on:click={saveProxmox}>
					{pveSaving ? 'Saving…' : 'Save & Reload'}
				</button>
				<button class="btn btn-ghost" disabled={pveTesting || !pveHost} on:click={testProxmox}>
					{pveTesting ? 'Testing…' : 'Test Connection'}
				</button>
				{#if pveTestResult}
					<span class="text-xs {pveTestResult.status === 'ok' ? 'text-green-400' : 'text-red-400'}">
						{pveTestResult.message}
					</span>
				{/if}
			</div>

			{#if pveStatus}
				<p class="text-xs text-slate-500">
					Connection status:
					<span class={pveStatus === 'ok' ? 'text-emerald-400' : pveStatus === 'not_configured' ? 'text-yellow-400' : 'text-red-400'}>
						{pveStatus.replace(/_/g, ' ')}
					</span>
				</p>
			{/if}
		{/if}
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
