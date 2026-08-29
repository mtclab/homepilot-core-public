<script lang="ts">
	// Settings (#549 F6): seven stacked cards became seven `?tab=` sectors.
	//
	// Nothing in them was redesigned except the self-check, which was a bullet
	// list summarising an admin diagnostic and is now the Subsystems tab: one
	// status card per subsystem, the report's own consequence sentence rendered
	// verbatim, and the address a failing one could not reach.
	//
	// #553 C2 added the editing: the non-secret settings behind each subsystem
	// are now edited on its card, each one showing where its value comes from,
	// because the server's env > db > default precedence is only honest if the
	// operator can see which of the three is in force.
	import AlertRules from '$lib/components/AlertRules.svelte';
	import SettingFields from '$lib/components/SettingFields.svelte';
	import GuestNetworkCard from '$lib/components/GuestNetworkCard.svelte';
	import GuestsPanel from '$lib/components/GuestsPanel.svelte';
	import TokensPanel from '$lib/components/TokensPanel.svelte';
	import TabBar from '$lib/components/TabBar.svelte';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import { queryTabs, activeQueryTab } from '$lib/tabs';
	import { SETTINGS_TABS, settingsTabFromHash } from '$lib/settingsTabs';
	import {
		subsystemLabel,
		subsystemStateClass,
		subsystemStateText,
		subsystemTarget,
	} from '$lib/selfcheck';
	import { EXTRA_GROUPS, SUBSYSTEM_SETTINGS, settingsFor, unplacedSettings } from '$lib/settingFields';
	import { api, setToken, getToken, hasCookieSession, refreshSession, sessionStore, type HealthInfo, type SelfcheckReport, type SettingOverride } from '$lib/api';
	import { isAdmin as capIsAdmin } from '$lib/capabilities';
	import { envApiBase, readStoredApiBase, resolveApiBase, writeStoredApiBase } from '$lib/apiBase';
	import { startEventStream, stopEventStream } from '$lib/events';
	import { notify } from '$lib/stores';
	import { validateApiBase } from '$lib/urlValidation';
	import { safeReturnTo } from '$lib/nav';

	let tokenVal = '';
	let apiBase = '';
	// Admin-only endpoints back the Proxmox tab; a read-only session gets a 403
	// on load and another on save. Show it only to admins. Default-deny while
	// the session is still loading.
	$: isAdminUser = capIsAdmin($sessionStore?.capabilities);
	let health: string | null = null;
	let checking = false;
	let cookieSession = false;
	let currentScope = '';
	let currentLabel = '';
	// The NORMALIZED capability list /auth/me hands us. This panel used to test
	// the raw scope string against ('full' | '*' | 'admin'), which is wrong in
	// both directions since the #579 rename: a superuser token minted as `all`
	// matched none of them (so the most privileged token in the product was
	// told it lacked privilege and had its "Manage API tokens" link hidden),
	// and `full` is the LEGACY superuser alias, not the write tier it reads as.
	// $lib/capabilities says it in as many words: never re-derive from `scope`.
	let currentCaps: string[] = [];
	$: sessionIsAdmin = capIsAdmin(currentCaps) || isAdminUser;

	$: urlValidation = validateApiBase(apiBase);

	// ── Sectors (#549 F6). `?tab=` state on this one route: Settings has no id
	// in its path, so the sector is the only thing the query has to carry.
	$: tabs = queryTabs(SETTINGS_TABS, $page.url.pathname, 'tab', $page.url.searchParams);
	$: activeTab = activeQueryTab(SETTINGS_TABS, $page.url);

	let healthData: HealthInfo | null = null;
	let loadingHealth = false;

	// Optional subsystems (ADR-004 S6). Admin-scoped like the Proxmox tab,
	// because the report names the addresses this instance is wired to.
	let selfcheck: SelfcheckReport | null = null;
	let selfcheckError = '';
	let selfcheckLoading = false;
	let selfcheckRequested = false;
	// The report carries no timestamp of its own, and a status card with no age
	// on it invites an operator to trust a probe that ran an hour ago. This is
	// when THIS page received it.
	let selfcheckAt: Date | null = null;

	// The editable half of the tab (#553 C2). Loaded with the report and
	// re-loaded after every save, so a card's status and the value driving it are
	// never two different vintages.
	let overrides: SettingOverride[] = [];
	let overridesError = '';

	async function loadOverrides() {
		try {
			overrides = (await api.listSettingOverrides()).settings;
			overridesError = '';
		} catch (e) {
			// Said out loud rather than leaving the cards silently read-only,
			// which would read as "nothing here is configurable".
			overridesError = e instanceof Error ? e.message : String(e);
		}
	}

	async function loadSelfcheck() {
		selfcheckRequested = true;
		selfcheckLoading = true;
		selfcheckError = '';
		void loadOverrides();
		try {
			selfcheck = await api.getSelfcheck();
			selfcheckAt = new Date();
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
	// Starts true: the tab only renders for admins, and it must show "Loading…"
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

	/**
	 * Links written before the tabs exist name a card by fragment
	 * (`/ui/settings#tokens`). They keep working: the fragment resolves to the
	 * tab that swallowed that card and the URL is rewritten to the canonical
	 * `?tab=` form. An explicit `?tab=` always wins - it is the newer,
	 * deliberate address.
	 */
	onMount(() => {
		const url = $page.url;
		if (url.searchParams.get('tab')) return;
		const fromHash = settingsTabFromHash(url.hash);
		// Connection is what an address-less URL already resolves to; redirecting
		// there would only churn history.
		if (!fromHash || fromHash === 'connection') return;
		const next = new URL(url);
		next.hash = '';
		next.searchParams.set('tab', fromHash);
		void goto(next.pathname + next.search, { replaceState: true, noScroll: true });
	});

	onMount(() => {
		tokenVal = getToken();
		apiBase = readStoredApiBase() ?? '';
		cookieSession = hasCookieSession();
		checkHealth();
		if (cookieSession || tokenVal) {
			api.me().then((info) => {
				currentScope = info.scope || '';
				currentCaps = info.capabilities ?? [];
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
				currentCaps = info?.capabilities ?? [];
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
					currentCaps = info.capabilities ?? [];
					currentLabel = info.token_label || '';
					notify('Settings saved — no cookie; using the in-memory token (cleared on page reload)');
				} else {
					currentScope = '';
					currentCaps = [];
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
		currentCaps = [];
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

<div class="page-stack">
	<h1 class="page-title">Settings</h1>

	<!-- ── Sectors (#549 F6) ──────────────────────────────────────────────
	     The same cards, addressed by `?tab=` so an operator who came to mint a
	     token does not scroll past the API base, the health dump and the whole
	     Proxmox form to reach it - and can send that address to someone else. -->
	<TabBar {tabs} activeId={activeTab} label="Settings sections" panelId="settings-panel" />

	<div
		id="settings-panel"
		role="tabpanel"
		aria-labelledby={activeTab ? `tab-${activeTab}` : undefined}
		class="section-stack"
	>
		{#if activeTab === 'connection'}
			<div class="section-stack max-w-lg">
				<div class="card space-y-4">
					<h2 class="section-title">API Connection</h2>
					<p class="prose-note prose-measure text-xs">
						Where THIS browser sends its API calls, and the token it signs in
						with. Both are stored in this browser only - they change nothing on
						the server and nothing for anyone else using it.
					</p>

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
									<span class="text-muted">Scope: <span class="{sessionIsAdmin ? 'text-ok' : 'text-warn'}">{currentScope}</span></span>
									{#if !sessionIsAdmin}
										<br /><span class="text-warn">Saving Proxmox settings and managing tokens need an <code>admin</code>-scope token; this one cannot do either.</span>
									{/if}
								{/if}
							{:else}
								No active session. Paste token and save to log in. Create tokens with <code class="text-muted">hp token create</code>.
							{/if}
						</p>
						{#if cookieSession && sessionIsAdmin}
							<a href="{base}/settings?tab=tokens" class="text-xs text-accent hover:underline">Manage API tokens →</a>
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
					<p class="prose-note prose-measure text-xs">
						What the server answered on <span class="font-mono">/health</span> when
						this page loaded: the state of each part it depends on. The optional
						subsystems have a deeper report of their own on the Subsystems tab.
					</p>
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
								<p>Configure Proxmox on the <a class="text-accent hover:underline" href="{base}/settings?tab=proxmox">Proxmox tab</a>, or set <code class="text-ink">HP_PROXMOX_HOST</code> and <code class="text-ink">PVE_API_TOKEN</code> in your environment.</p>
							</div>
						{:else if healthData.checks?.proxmox === 'unreachable'}
							<div class="mt-3 p-3 bg-canvas border border-danger-border rounded text-xs text-muted">
								<p class="font-semibold text-danger mb-1">Proxmox unreachable</p>
								<p>The configured Proxmox host is not responding. Check the host address and network connectivity, or reconfigure on the <a class="text-accent hover:underline" href="{base}/settings?tab=proxmox">Proxmox tab</a>.</p>
							</div>
						{/if}
					{:else}
						<p class="prose-note text-xs">Could not load health status.</p>
					{/if}
				</div>
			</div>
		{:else if activeTab === 'proxmox'}
			{#if isAdminUser}
				<div class="card space-y-4 max-w-lg">
					<h2 class="section-title">Proxmox Connection</h2>
					<p class="prose-note prose-measure text-xs">
						The cluster this instance manages, and the API tokens it reaches it
						with. Tokens go into the vault and are never shown again - leave a
						token field blank to keep the one already stored. Test asks the
						cluster without saving; Save &amp; Reload rebinds the live client
						immediately.
					</p>

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
			{:else}
				<p class="prose-note prose-measure text-xs">
					The Proxmox connection names the cluster this instance talks to and the
					vault entries holding its tokens, so it is admin-only. Sign in with an
					admin token to see or change it.
				</p>
			{/if}
		{:else if activeTab === 'subsystems'}
			{#if isAdminUser}
				<div class="section-stack max-w-2xl">
					<div class="flex items-baseline justify-between gap-3 flex-wrap">
						<p class="prose-note prose-measure text-xs">
							The optional parts of this instance, as the server's own self-check
							reports them. <strong class="text-ink">off</strong> is a choice and
							needs nothing; <strong class="text-danger">configured, unreachable</strong>
							is a fault to fix.
						</p>
						<div class="flex items-center gap-3 shrink-0">
							<span class="text-xs text-muted">
								{selfcheckAt ? `Checked ${selfcheckAt.toLocaleTimeString()}` : 'Not checked yet'}
							</span>
							<button class="btn btn-ghost btn-sm" disabled={selfcheckLoading} on:click={loadSelfcheck}>
								{selfcheckLoading ? 'Checking…' : '↻ Re-check'}
							</button>
						</div>
					</div>

					{#if selfcheckLoading && !selfcheck}
						<p class="text-xs text-muted">Checking…</p>
					{:else if selfcheckError}
						<p class="text-xs text-danger">Could not load the self-check: {selfcheckError}</p>
					{:else if selfcheck}
						{#each selfcheck.subsystems as sub (sub.name)}
							<div class="card space-y-2">
								<div class="flex items-baseline gap-2 flex-wrap">
									<h2 class="section-title">{subsystemLabel(sub)}</h2>
									<span class="badge {subsystemStateClass(sub.state)}">{subsystemStateText(sub.state)}</span>
								</div>
								<!-- Verbatim: the report writes one sentence per STATE, and it is
								     the only place that knows what this subsystem's absence or
								     failure actually costs. Paraphrasing it here would be the
								     grey mystery chip in longer words. -->
								<p class="prose-note prose-measure text-xs">{sub.consequence}</p>
								{#if subsystemTarget(sub)}
									<p class="text-xs text-muted">
										{sub.state === 'unreachable' ? 'Could not reach' : 'Target'}
										<span class="font-mono text-ink">{subsystemTarget(sub)}</span>
									</p>
								{/if}
								<SettingFields
									settings={settingsFor(overrides, SUBSYSTEM_SETTINGS[sub.name] || [])}
									canWrite={isAdminUser}
									onSaved={loadSelfcheck}
								/>
							</div>
						{/each}
						{#if selfcheck.subsystems.length === 0}
							<p class="prose-note text-xs">This build reports no optional subsystems.</p>
						{/if}

						<!-- Configuration with no probe behind it: nothing to reach, so the
						     self-check says nothing about it, and it would otherwise be the
						     one thing an operator cannot find here. -->
						<!-- The guest network: settings, survey and plan on ONE card,
						     because a subnet setting means nothing without what the
						     cluster currently has. -->
						<GuestNetworkCard
							overrides={overrides}
							canWrite={isAdminUser}
							onSaved={loadOverrides}
						/>

						{#each EXTRA_GROUPS as group (group.id)}
							{#if settingsFor(overrides, group.keys).length}
								<div class="card space-y-2">
									<h2 class="section-title">{group.title}</h2>
									<p class="prose-note prose-measure text-xs">{group.note}</p>
									<SettingFields
										settings={settingsFor(overrides, group.keys)}
										canWrite={isAdminUser}
										onSaved={loadSelfcheck}
									/>
								</div>
							{/if}
						{/each}
						{#if unplacedSettings(overrides, selfcheck.subsystems.map((s) => s.name)).length}
							<div class="card space-y-2">
								<h2 class="section-title">Other settings</h2>
								<SettingFields
									settings={unplacedSettings(overrides, selfcheck.subsystems.map((s) => s.name))}
									canWrite={isAdminUser}
									onSaved={loadSelfcheck}
								/>
							</div>
						{/if}
						{#if overridesError}
							<p class="text-xs text-danger">Could not load the editable settings: {overridesError}</p>
						{/if}
						<p class="prose-note text-xs">Probes are bounded at {selfcheck.timeout_seconds}s.</p>
					{/if}
				</div>
			{:else}
				<p class="prose-note prose-measure text-xs">
					The self-check names the addresses this instance is wired to, so it is
					admin-only. Sign in with an admin token to see it.
				</p>
			{/if}
		{:else if activeTab === 'guests'}
			<div class="card space-y-3 max-w-3xl">
				<h2 class="section-title">Guests</h2>
				<GuestsPanel />
			</div>
		{:else if activeTab === 'monitoring'}
			<div class="card space-y-3 max-w-3xl">
				<h2 class="section-title">Monitoring</h2>
				<AlertRules />
			</div>
		{:else if activeTab === 'tokens'}
			<div class="card space-y-3">
				<TokensPanel />
			</div>
		{:else if activeTab === 'about'}
			<div class="card space-y-2 max-w-lg">
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
		{/if}
	</div>
</div>
