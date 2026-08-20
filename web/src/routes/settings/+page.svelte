<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { api, setToken, getToken, hasCookieSession, refreshSession, sessionStore, type HealthInfo, type SelfcheckReport } from '$lib/api';
	import { isAdmin as capIsAdmin } from '$lib/capabilities';
	import { envApiBase, readStoredApiBase, resolveApiBase, writeStoredApiBase } from '$lib/apiBase';
	import { startEventStream, stopEventStream } from '$lib/events';
	import { notify } from '$lib/stores';
	import { validateApiBase } from '$lib/urlValidation';
	import { safeReturnTo } from '$lib/nav';

	let tokenVal = '';
	let apiBase = '';
	// Admin-only endpoints back the Proxmox card; a read-only session gets a 403
	// on load and another on save. Show it only to admins. Default-deny while
	// the session is still loading.
	$: isAdminUser = capIsAdmin($sessionStore?.capabilities);
	let health: string | null = null;
	let checking = false;
	let cookieSession = false;
	let currentScope = '';
	let currentLabel = '';

	$: urlValidation = validateApiBase(apiBase);

	let healthData: HealthInfo | null = null;
	let loadingHealth = false;

	// Optional subsystems (ADR-004 S6). Admin-scoped like the Proxmox card below,
	// because the report names the addresses this instance is wired to.
	let selfcheck: SelfcheckReport | null = null;
	let selfcheckError = '';
	let selfcheckLoading = false;
	let selfcheckRequested = false;

	const SELFCHECK_LABELS: Record<string, string> = {
		proxmox: 'Proxmox',
		agent_hub: 'Agent hub',
		vault: 'Vault',
		embeddings: 'KB embeddings',
		events_webhook: 'Events webhook',
		mcp: 'MCP',
		artifacts_remote: 'Artifact backup',
	};

	async function loadSelfcheck() {
		selfcheckRequested = true;
		selfcheckLoading = true;
		selfcheckError = '';
		try {
			selfcheck = await api.getSelfcheck();
		} catch (e) {
			// Say the report could not be loaded rather than rendering an empty
			// list, which would read as "nothing is configured".
			selfcheckError = e instanceof Error ? e.message : String(e);
		} finally {
			selfcheckLoading = false;
		}
	}

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
	// Starts true: the card only renders for admins, and it must show "Loading…"
	// rather than a set of blank inputs until the real settings arrive.
	let pveLoading = true;
	let pveSaving = false;
	let pveTesting = false;
	let pveTestResult: { status: string; message: string } | null = null;
	let pveLoadError = '';
	let pveRequested = false;

	async function loadProxmoxSettings() {
		pveRequested = true;
		pveLoading = true;
		pveLoadError = '';
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
			// Never swallow this: a silent failure left the inputs blank, which
			// reads as "nothing configured" and turns the next Save into a
			// surprise 403 (or an overwrite of a config the user never saw).
			pveLoadError = e instanceof Error ? e.message : String(e);
		} finally {
			pveLoading = false;
		}
	}

	// The session arrives asynchronously (the layout resolves /auth/me), so load
	// the admin-only settings as soon as we know the user is an admin.
	$: if (isAdminUser && !pveRequested) loadProxmoxSettings();
	$: if (isAdminUser && !selfcheckRequested) loadSelfcheck();

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
			// The backend can report a failure as HTTP 200 with status !== 'ok'.
			// Surface the message and KEEP the entered values so the user can fix
			// and retry — do not wipe the token fields or claim success.
			if (result.status && result.status !== 'ok') {
				pveStatus = result.status;
				pveTestResult = { status: 'error', message: result.message || result.status };
				notify('Failed to save Proxmox settings: ' + (result.message || result.status), 'err');
				return;
			}
			pveTokenConfigured = result.token_configured ?? !!pveToken;
			pveWriteTokenConfigured = result.write_token_configured ?? !!pveWriteToken;
			pveWriteTokenIsSeparate = !!(pveWriteToken && pveWriteToken !== pveToken);
			pveStatus = 'ok';
			pveToken = '';
			pveWriteToken = '';
			notify('Proxmox settings saved and reloaded');
			await checkHealth();
			// The reload rebinds the live Proxmox client, so the self-check above is
			// now stale — re-run it rather than leaving a contradicting report.
			await loadSelfcheck();
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
		apiBase = readStoredApiBase() ?? '';
		cookieSession = hasCookieSession();
		checkHealth();
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
		const previousBase = resolveApiBase(readStoredApiBase(), envApiBase());
		if (!writeStoredApiBase(apiBase) && apiBase.trim()) {
			// Never claim a setting was saved when storage refused it.
			notify('Could not store the API base URL — browser storage is unavailable.', 'err');
			return;
		}
		// api.ts/events.ts resolve the base per call, so requests move as soon as
		// it is stored — but a stream opened against the OLD origin has to be
		// reopened by hand.
		if (resolveApiBase(readStoredApiBase(), envApiBase()) !== previousBase) {
			stopEventStream();
			startEventStream();
		}
		if (token) {
			try {
				await api.login(token);
				cookieSession = true;
				// refreshSession (not a bare api.me) so the sidebar session panel and
				// the capability gating pick the new session up immediately.
				const info = await refreshSession();
				currentScope = info?.scope || '';
				currentLabel = info?.token_label || '';
				notify('Settings saved — session cookie set');
				const params = new URLSearchParams(window.location.search);
				const dest = safeReturnTo(params.get('returnTo'), base, '');
				if (dest) goto(dest);
			} catch {
				cookieSession = false;
				// The cookie could not be set (typically a cross-origin API base).
				// The bearer token stays in memory and the layout now accepts it as a
				// credential, so this session survives navigation — but confirm the
				// token actually works before claiming it did.
				const info = await refreshSession();
				if (info) {
					currentScope = info.scope || '';
					currentLabel = info.token_label || '';
					notify('Settings saved — no cookie; using the in-memory token (cleared on page reload)');
				} else {
					currentScope = '';
					currentLabel = '';
					notify('Settings saved, but the token was rejected — check it and try again', 'err');
				}
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
			// Test exactly what a saved value would resolve to (override → env →
			// same origin), so a green tick here means the app will reach the same
			// place after Save.
			const target = resolveApiBase(apiBase, envApiBase());
			const res = await fetch(`${target}/health`, { credentials: 'include' });
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
	<h1 class="page-title">Settings</h1>

	<div class="card space-y-4">
		<h2 class="section-title">API Connection</h2>

		<div class="space-y-1">
			<label class="field-label" for="api-base">API Base URL</label>
			<input
				id="api-base"
				class="input w-full {urlValidation.error ? 'border-danger' : urlValidation.warning ? 'border-warn' : ''}"
				placeholder="http://localhost:8000 (leave blank = same origin)"
				bind:value={apiBase}
			/>
			{#if urlValidation.error}
				<p class="text-xs text-danger">{urlValidation.error}</p>
			{:else if urlValidation.warning}
				<p class="text-xs text-warn">{urlValidation.warning}</p>
			{:else}
				<p class="prose-note text-xs">Used when the web UI is served from a different origin than the API.</p>
			{/if}
		</div>

		<div class="space-y-1">
			<label class="field-label" for="token">API Token</label>
			<input
				id="token"
				type="password"
				class="input w-full font-mono"
				placeholder="Bearer token from hp token create"
				bind:value={tokenVal}
			/>
			<p class="prose-note text-xs {cookieSession ? 'text-ok' : ''}">
				{#if cookieSession}
					Session active — token stored in HttpOnly cookie, cleared on browser close.
					{#if currentScope}
						<span class="text-muted">Scope: <span class="{currentScope === 'full' || currentScope === '*' || currentScope === 'admin' ? 'text-ok' : 'text-warn'}">{currentScope}</span></span>
						{#if currentScope !== 'full' && currentScope !== '*' && currentScope !== 'admin'}
							<br /><span class="text-warn">Write actions (save settings, manage tokens) require an admin/full-scope token.</span>
						{/if}
					{/if}
				{:else}
					No active session. Paste token and save to log in. Create tokens with <code class="text-muted">hp token create</code>.
				{/if}
			</p>
			{#if cookieSession && (currentScope === 'full' || currentScope === '*' || currentScope === 'admin')}
				<a href="/ui/tokens" class="text-xs text-accent hover:underline">Manage API tokens →</a>
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
				<span class="text-xs {health.startsWith('✓') ? 'text-ok' : 'text-danger'}">{health}</span>
			{/if}
		</div>
	</div>

	<div class="card space-y-3">
		<h2 class="section-title">System Health</h2>
		{#if loadingHealth}
			<p class="text-xs text-muted">Loading…</p>
		{:else if healthData}
			<dl class="text-xs space-y-1">
				{#each Object.entries(healthData.checks ?? healthData) as [key, val]}
					{#if typeof val === 'string'}
						<div class="flex gap-4">
							<dt class="text-muted w-28 capitalize">{key.replace(/_/g, ' ')}</dt>
							<dd class:text-ok={val === 'ok'}
							    class:text-warn={val === 'not_configured'}
							    class:text-danger={val === 'error' || val === 'unreachable' || val === 'locked'}
							    class:text-ink={!['ok','not_configured','error','unreachable','locked'].includes(val)}
							>{val}</dd>
						</div>
					{/if}
				{/each}
			</dl>
			{#if healthData.checks?.proxmox === 'not_configured'}
				<div class="mt-3 p-3 bg-canvas border border-border rounded text-xs text-muted">
					<p class="font-semibold text-ink mb-1">Proxmox not configured</p>
					<p>Configure Proxmox in the <strong>Proxmox Connection</strong> section below, or set <code class="text-ink">HP_PROXMOX_HOST</code> and <code class="text-ink">PVE_API_TOKEN</code> in your environment.</p>
				</div>
			{:else if healthData.checks?.proxmox === 'unreachable'}
				<div class="mt-3 p-3 bg-canvas border border-danger-border rounded text-xs text-muted">
					<p class="font-semibold text-danger mb-1">Proxmox unreachable</p>
					<p>The configured Proxmox host is not responding. Check the host address and network connectivity, or reconfigure in the <strong>Proxmox Connection</strong> section below.</p>
				</div>
			{/if}
		{:else}
			<p class="prose-note text-xs">Could not load health status.</p>
		{/if}
	</div>

	{#if isAdminUser}
	<div class="card space-y-3">
		<div class="flex items-center justify-between gap-3">
			<h2 class="section-title">Optional subsystems</h2>
			<button class="btn btn-ghost text-xs" disabled={selfcheckLoading} on:click={loadSelfcheck}>
				{selfcheckLoading ? 'Checking…' : '↻ Re-check'}
			</button>
		</div>
		{#if selfcheckLoading && !selfcheck}
			<p class="text-xs text-muted">Checking…</p>
		{:else if selfcheckError}
			<p class="text-xs text-danger">Could not load the self-check: {selfcheckError}</p>
		{:else if selfcheck}
			<ul class="text-xs space-y-2">
				{#each selfcheck.subsystems as sub (sub.name)}
					<li class="space-y-0.5">
						<div class="flex gap-2 items-baseline">
							<span class="text-ink font-semibold">{SELFCHECK_LABELS[sub.name] ?? sub.name.replace(/_/g, ' ')}</span>
							<span
								class:text-ok={sub.state === 'ok'}
								class:text-muted={sub.state === 'off'}
								class:text-danger={sub.state === 'unreachable'}
								class:text-warn={sub.state === 'unknown'}
							>{sub.state === 'unreachable' ? 'configured, unreachable' : sub.state}</span>
							{#if sub.target && sub.state !== 'off'}
								<span class="text-muted font-mono">{sub.target}</span>
							{/if}
						</div>
						<p class="prose-note">{sub.consequence}</p>
					</li>
				{/each}
			</ul>
			<p class="prose-note text-xs">
				<strong class="text-ink">off</strong> is a choice and needs nothing;
				<strong class="text-danger">configured, unreachable</strong> is a fault to fix.
				Probes are bounded at {selfcheck.timeout_seconds}s.
			</p>
		{/if}
	</div>

	<div class="card space-y-4">
		<h2 class="section-title">Proxmox Connection</h2>

		{#if pveLoading}
			<p class="text-xs text-muted">Loading…</p>
		{:else if pveLoadError}
			<div class="p-3 bg-canvas border border-danger-border rounded space-y-2">
				<p class="text-xs text-danger">Could not load the Proxmox settings: {pveLoadError}</p>
				<p class="prose-note text-xs">
					The fields below are not showing the current configuration — saving now would
					overwrite it.
				</p>
				<button class="btn btn-ghost text-xs" on:click={loadProxmoxSettings}>↻ Retry</button>
			</div>
		{:else}
			<div class="space-y-1">
				<label class="field-label" for="pve-host">Host</label>
				<input id="pve-host" class="input w-full" placeholder="pve.example.com" bind:value={pveHost} />
			</div>

			<div class="space-y-1">
				<label class="field-label" for="pve-port">Port</label>
				<input id="pve-port" type="number" class="input w-full" placeholder="8006" bind:value={pvePort} />
			</div>

			<div class="space-y-1">
				<label class="field-label" for="pve-token">Read API Token</label>
				<input id="pve-token" type="password" class="input w-full font-mono" placeholder={pveTokenConfigured ? '•••••••• (configured)' : 'user@pve!tokenid=uuid'} bind:value={pveToken} />
				<p class="prose-note text-xs">
					{#if pveTokenConfigured}
						Token stored in vault (source: <span class="text-muted">{pveTokenSource}</span>). Leave blank to keep existing.
					{:else}
						Enter a PVE API token with read access. Create one in the PVE UI under <em>Datacenter → Permissions → API Tokens</em>.
					{/if}
				</p>
			</div>

			<div class="space-y-1">
				<label class="field-label" for="pve-write-token">Write API Token <span class="text-muted">(optional)</span></label>
				<input id="pve-write-token" type="password" class="input w-full font-mono" placeholder={pveWriteTokenIsSeparate ? '•••••••• (configured)' : 'Same as read token'} bind:value={pveWriteToken} />
				<p class="prose-note text-xs">
					{#if pveWriteTokenIsSeparate}
						Write token stored in vault (source: <span class="text-muted">{pveWriteTokenSource}</span>). Used for VM/LXC create, delete, snapshot, config changes.
					{:else if pveWriteTokenConfigured}
						Write operations use the read token (no separate write token). Leave blank to keep.
					{:else}
						Separate token with write permissions (PVEVMAdmin, etc.). If unset, the read token is used for everything.
					{/if}
				</p>
			</div>

			<label class="field-label flex items-center gap-2 text-ink">
				<input type="checkbox" class="rounded border-border-strong" bind:checked={pveVerifySsl} />
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
					<span class="text-xs {pveTestResult.status === 'ok' ? 'text-ok' : 'text-danger'}">
						{pveTestResult.message}
					</span>
				{/if}
			</div>

			{#if pveStatus}
				<p class="prose-note text-xs">
					Connection status:
					<span class={pveStatus === 'ok' ? 'text-ok' : pveStatus === 'not_configured' ? 'text-warn' : 'text-danger'}>
						{pveStatus.replace(/_/g, ' ')}
					</span>
				</p>
			{/if}
		{/if}
	</div>
	{/if}

	<div class="card space-y-2">
		<h2 class="section-title">About</h2>
		<dl class="text-xs space-y-1">
			<div class="flex gap-4">
				<dt class="text-muted w-24">Version</dt>
				<dd class="text-ink">HomePilot v2</dd>
			</div>
			<div class="flex gap-4">
				<dt class="text-muted w-24">Chat</dt>
				<dd class="text-ink">Use opencode or Claude Code — no chat UI here</dd>
			</div>
		</dl>
	</div>
</div>
