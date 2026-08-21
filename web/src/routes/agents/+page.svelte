<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { page } from '$app/stores';
	import {
		api,
		ApiError,
		type AgentInfo,
		type AlertComparison,
		type AlertRule,
		type FiringAlert,
		type MetricSeries
	} from '$lib/api';
	import { notify } from '$lib/stores';
	import { isValidHubHost } from '$lib/hostValidation';
	import Sparkline from '$lib/components/Sparkline.svelte';
	import { metricLabel } from '$lib/sparkline';

	let agents: AgentInfo[] = [];
	let loading = true;
	// Default-deny (same as tokens/+page.svelte): starting true flashed the
	// admin-only Enroll/Hub-token buttons at every non-admin before the first
	// 403 came back.
	let isAdmin = false;
	let loadError = '';

	let showBootstrap = false;
	let bootstrapData: {
		bootstrap_token: string;
		hub_host: string;
		hub_port: number;
		hub_tls?: boolean;
		hub_cert_sha256?: string;
	} | null = null;
	let generating = false;

	let showHubToken = false;
	let hubData: {
		auth_token: string;
		hub_host: string;
		hub_port: number;
		hub_tls?: boolean;
		hub_cert_sha256?: string;
	} | null = null;
	let loadingHubToken = false;

	let selectedAgent: AgentInfo | null = null;

	// ── Native metrics (ADR-004 S5) ──────────────────────────────────────────
	// Sparklines the operator sees at a glance in the row, and the full recent
	// panel when a host is opened. The row line is the load average because it
	// is the one metric whose SHAPE says something on its own.
	const ROW_METRIC = 'load.1m';
	// Metrics drawn in the per-host panel, in reading order. Totals are shown as
	// numbers beside the free-space lines rather than as their own charts: a
	// constant is not a series.
	const PANEL_METRICS = ['load.1m', 'load.5m', 'memory.free_gb', 'disk.free_gb'];
	// One colour per metric from the categorical ramp - a metric carries no
	// status meaning, so the status colours stay out of it.
	const METRIC_COLORS: Record<string, string> = {
		'load.1m': 'var(--chart-1)',
		'load.5m': 'var(--chart-5)',
		'memory.free_gb': 'var(--chart-2)',
		'disk.free_gb': 'var(--chart-3)'
	};
	// Metrics a rule can be written against: everything the agent reports whose
	// value moves. A total (disk.total_gb) is a constant, not an alert.
	const RULE_METRICS = ['load.1m', 'load.5m', 'load.15m', 'memory.free_gb', 'disk.free_gb'];
	const WINDOW_OPTIONS = [
		{ label: '1h', hours: 1 },
		{ label: '6h', hours: 6 },
		{ label: '24h', hours: 24 },
		{ label: '7d', hours: 168 }
	];

	let rowSeries: Record<string, MetricSeries | null> = {};
	let panelSeries: MetricSeries[] = [];
	let panelHost = '';
	let panelHours = 1;
	let panelLoading = false;
	let panelTruncated = false;
	let firing: FiringAlert[] = [];

	async function loadRowSparklines() {
		// One hour is enough to show shape in a table row, and it keeps the
		// request small: a 7-day row sparkline would be 10k points per host.
		const results = await Promise.all(
			agents.map(async (a) => {
				try {
					return [a.hostname, await api.getHostSeries(a.hostname, ROW_METRIC, 1)] as const;
				} catch {
					return [a.hostname, null] as const;
				}
			})
		);
		rowSeries = Object.fromEntries(results);
	}

	async function loadPanel(hostname: string, hours = panelHours) {
		panelHost = hostname;
		panelHours = hours;
		panelLoading = true;
		try {
			const series = await Promise.all(
				PANEL_METRICS.map((m) => api.getHostSeries(hostname, m, hours).catch(() => null))
			);
			panelSeries = series.filter((s): s is MetricSeries => s !== null);
			panelTruncated = panelSeries.some((s) => s.truncated);
		} finally {
			panelLoading = false;
		}
	}

	function toggleDetails(a: AgentInfo) {
		if (selectedAgent?.agent_id === a.agent_id) {
			selectedAgent = null;
			panelSeries = [];
			return;
		}
		selectedAgent = a;
		void loadPanel(a.hostname, panelHours);
	}

	let rules: AlertRule[] = [];
	let showRules = false;
	let savingRule = false;
	// A rule is (host filter, metric, comparison, threshold, for_seconds). The
	// duration is entered in MINUTES because that is how an operator thinks about
	// "don't page me for a blip"; the API takes seconds.
	let draft = {
		name: '',
		metric: 'load.1m',
		comparison: 'gt' as AlertComparison,
		threshold: 4,
		for_minutes: 5,
		host_filter: '*'
	};
	const COMPARISON_LABELS: Record<AlertComparison, string> = {
		gt: 'above',
		gte: 'at or above',
		lt: 'below',
		lte: 'at or below'
	};

	async function loadRules() {
		try {
			rules = (await api.listAlertRules()).items;
		} catch (e) {
			notify('Could not load alert rules: ' + String(e), 'err');
		}
	}

	async function createRule() {
		if (!draft.name.trim()) {
			notify('Give the rule a name', 'err');
			return;
		}
		savingRule = true;
		try {
			await api.createAlertRule({
				name: draft.name.trim(),
				metric: draft.metric,
				comparison: draft.comparison,
				threshold: Number(draft.threshold),
				for_seconds: Math.round(Number(draft.for_minutes) * 60),
				host_filter: draft.host_filter.trim() || '*'
			});
			draft = { ...draft, name: '' };
			await loadRules();
			notify('Alert rule created', 'ok');
		} catch (e) {
			notify('Could not create the rule: ' + String(e), 'err');
		} finally {
			savingRule = false;
		}
	}

	async function toggleRule(rule: AlertRule) {
		try {
			await api.setAlertRuleEnabled(rule.id, !rule.enabled);
			await loadRules();
		} catch (e) {
			notify('Could not update the rule: ' + String(e), 'err');
		}
	}

	async function removeRule(rule: AlertRule) {
		try {
			await api.deleteAlertRule(rule.id);
			await loadRules();
			firing = firing.filter((f) => f.rule_id !== rule.id);
		} catch (e) {
			notify('Could not delete the rule: ' + String(e), 'err');
		}
	}

	function alertsFor(hostname: string): FiringAlert[] {
		return firing.filter((f) => f.hostname === hostname);
	}

	async function load() {
		loading = true;
		loadError = '';
		try {
			agents = await api.listAgents();
			isAdmin = true;
		} catch (e) {
			// Only a real 403 means "needs admin". Anything else (e.g. a 500) is a
			// genuine error and must not be reported as a permission problem — the
			// old `message.startsWith('403')` check could never match the humanized
			// message, so every failure looked like a permission denial.
			if (e instanceof ApiError && e.status === 403) {
				isAdmin = false;
				notify('Admin scope required to view agents', 'err');
			} else {
				loadError = e instanceof Error ? e.message : String(e);
				notify(loadError, 'err');
			}
		} finally {
			loading = false;
		}
	}

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

	function fmtDate(s: string | null): string {
		if (!s) return '—';
		return new Date(s).toLocaleString();
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

	// Render scalars as-is and objects as compact JSON (the agent's state and
	// some system_info entries like disk/load/memory are nested objects).
	function fmtVal(v: unknown): string {
		if (v === null || v === undefined) return '—';
		if (typeof v === 'object') {
			const s = JSON.stringify(v);
			return s === '{}' ? '—' : s;
		}
		return String(v);
	}

	// The list overlays live connections on the persisted registry: persisted
	// entries carry connected:false (known host, reconnecting/offline). For live
	// ones freshness comes from heartbeat age (~3 missed heartbeats = stale).
	type AgentStatus = 'connected' | 'stale' | 'disconnected';
	function agentStatus(a: { connected?: boolean; stale_seconds?: number }): AgentStatus {
		if (a.connected === false) return 'disconnected';
		return (a.stale_seconds ?? 0) < 90 ? 'connected' : 'stale';
	}
	const statusDot: Record<AgentStatus, string> = {
		connected: 'bg-ok',
		stale: 'bg-warn',
		disconnected: 'bg-muted'
	};
	const statusText: Record<AgentStatus, string> = {
		connected: 'text-ok',
		stale: 'text-warn',
		disconnected: 'text-muted'
	};

	onMount(async () => {
		await load();
		if (!isAdmin) return;
		try {
			firing = (await api.listFiringAlerts()).items;
		} catch {
			firing = [];
		}
		await loadRules();
		await loadRowSparklines();
		// Deep link from the inventory table: open that host's panel directly.
		const wanted = $page.url.searchParams.get('host');
		const target = wanted ? agents.find((a) => a.hostname === wanted) : undefined;
		if (target) toggleDetails(target);
	});
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Agents</h1>
		<div class="flex gap-2 items-center">
			<span class="text-muted text-xs">
				{agents.filter((a) => agentStatus(a) === 'connected').length} connected / {agents.length} known
			</span>
			{#if isAdmin}
				<button class="btn btn-primary text-xs" on:click={() => (showBootstrap = !showBootstrap)}>
					{showBootstrap ? 'Cancel' : 'Enroll Agent'}
				</button>
				<button class="btn btn-ghost text-xs" on:click={() => (showHubToken = !showHubToken)}>
					{showHubToken ? 'Hide' : 'Hub Token'}
				</button>
				<button class="btn btn-ghost text-xs" on:click={() => (showRules = !showRules)}>
					{showRules ? 'Hide' : 'Alert Rules'}
				</button>
			{/if}
		</div>
	</div>

	{#if showBootstrap}
		<div class="card p-4 space-y-4">
			<h2 class="section-title">Enroll New Agent</h2>
			<p class="prose-note text-xs">
				Bootstrap tokens are single-use and expire — fine for a one-off connect. For a
				permanent install that survives agent reboots, use the shared Hub Auth Token below.
			</p>
			<p class="prose-note text-xs">
				A running Proxmox guest that answers on qemu-guest-agent needs none of this: open it
				under <a class="text-accent hover:text-accent-strong" href="{base}/inventory">Inventory</a>
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

	{#if showHubToken}
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
				{:else}
					<div class="bg-canvas border border-danger-border rounded p-3">
						<p class="text-xs text-danger font-semibold mb-1">Install one-liner unavailable</p>
						<p class="prose-note text-xs">The hub host reported by the server (<code class="text-ink break-all">{hubData.hub_host}</code>) is not a valid hostname or IP, so a root <code class="text-ink">curl … | bash</code> cannot be safely generated. Fix <code class="text-ink">HP_HUB_HOST</code> on the server.</p>
					</div>
				{/if}
			{/if}
		</div>
	{/if}

	{#if showRules}
		<div class="card p-4 space-y-4">
			<h2 class="section-title">Alert rules</h2>
			<p class="prose-note text-xs">
				A rule fires only when its condition has held for the whole duration, so a
				single spike never raises one. Firing and recovery both go out as events.
			</p>

			<div class="flex flex-wrap items-end gap-2">
				<label class="flex flex-col gap-1">
					<span class="field-label">Name</span>
					<input class="input w-44" bind:value={draft.name} placeholder="Load too high" />
				</label>
				<label class="flex flex-col gap-1">
					<span class="field-label">Metric</span>
					<select class="input" bind:value={draft.metric}>
						{#each RULE_METRICS as m}
							<option value={m}>{metricLabel(m)}</option>
						{/each}
					</select>
				</label>
				<label class="flex flex-col gap-1">
					<span class="field-label">Is</span>
					<select class="input" bind:value={draft.comparison}>
						{#each Object.entries(COMPARISON_LABELS) as [value, label]}
							<option {value}>{label}</option>
						{/each}
					</select>
				</label>
				<label class="flex flex-col gap-1">
					<span class="field-label">Threshold</span>
					<input class="input num w-20" type="number" step="0.1" bind:value={draft.threshold} />
				</label>
				<label class="flex flex-col gap-1">
					<span class="field-label">For (minutes)</span>
					<input class="input num w-20" type="number" min="0" step="1" bind:value={draft.for_minutes} />
				</label>
				<label class="flex flex-col gap-1">
					<span class="field-label">Host (* = all)</span>
					<input class="input w-32" bind:value={draft.host_filter} />
				</label>
				<button class="btn btn-primary text-xs" on:click={createRule} disabled={savingRule}>
					{savingRule ? 'Saving…' : 'Add rule'}
				</button>
			</div>

			{#if rules.length === 0}
				<p class="prose-note text-xs">No alert rules yet.</p>
			{:else}
				<table class="data-table text-xs">
					<thead>
						<tr>
							<th class="text-left pb-2 pr-4">Rule</th>
							<th class="text-left pb-2 pr-4">Condition</th>
							<th class="text-left pb-2 pr-4">Hosts</th>
							<th class="text-left pb-2 pr-4">State</th>
							<th class="text-left pb-2">Actions</th>
						</tr>
					</thead>
					<tbody>
						{#each rules as r}
							<tr class="border-b border-divider">
								<td class="py-2 pr-4 text-ink">{r.name}</td>
								<td class="py-2 pr-4 text-muted">
									{metricLabel(r.metric)} {COMPARISON_LABELS[r.comparison]}
									<span class="num text-ink">{r.threshold}</span>
									for {Math.round(r.for_seconds / 60)} min
								</td>
								<td class="py-2 pr-4 text-muted font-mono">{r.host_filter}</td>
								<td class="py-2 pr-4">
									{#if r.enabled}
										<span class="text-ok">enabled</span>
									{:else}
										<span class="text-muted">silenced</span>
									{/if}
								</td>
								<td class="py-2 space-x-1">
									<button class="btn btn-ghost btn-xs" on:click={() => toggleRule(r)}>
										{r.enabled ? 'Silence' : 'Enable'}
									</button>
									<button class="btn btn-ghost btn-xs" on:click={() => removeRule(r)}>Delete</button>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</div>
	{/if}

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load agents.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Retry</button>
		</div>
	{:else if !isAdmin}
		<div class="card p-6 text-center">
			<p class="prose-note">You need admin scope to view connected agents.</p>
		</div>
	{:else if agents.length === 0}
		<div class="card p-6 text-center">
			<p class="prose-note">No agents connected.</p>
			<p class="prose-note text-xs mt-2">Click "Enroll Agent" to generate a bootstrap token and install the agent on a host.</p>
		</div>
	{:else}
		<div class="card overflow-x-auto">
			<table class="data-table text-xs">
<thead>
					<tr>
						<th class="text-left pb-2 pr-4">Hostname</th>
						<th class="text-left pb-2 pr-4">Agent ID</th>
						<th class="text-left pb-2 pr-4">State</th>
						<th class="text-left pb-2 pr-4">Load (1h)</th>
						<th class="text-left pb-2 pr-4">Connected</th>
						<th class="text-left pb-2 pr-4">Last Heartbeat</th>
						<th class="text-left pb-2">Actions</th>
					</tr>
				</thead>
				<tbody>
					{#each agents as a}
						<tr class="border-b border-divider hover:bg-raised">
							<td class="py-2 pr-4 text-ink font-mono">{a.hostname}</td>
							<td class="py-2 pr-4 text-muted font-mono text-[11px]">{a.agent_id.slice(0, 8)}…</td>
							<td class="py-2 pr-4">
								<span class="inline-flex items-center gap-1">
									<span class="w-1.5 h-1.5 rounded-full {statusDot[agentStatus(a)]}"></span>
									<span class={statusText[agentStatus(a)]}>{agentStatus(a)}</span>
								</span>
								{#each alertsFor(a.hostname) as f}
									<span
										class="badge badge-failed ml-1"
										title="{f.name}: {f.metric} {f.comparison} {f.threshold} held for {f.for_seconds}s"
									>{f.name}</span>
								{/each}
							</td>
							<td class="py-2 pr-4">
								{#if rowSeries[a.hostname]}
									<Sparkline
										points={rowSeries[a.hostname]?.points ?? []}
										metric={ROW_METRIC}
										showLabel={false}
										width={110}
										height={22}
									/>
								{:else}
									<span class="text-muted">—</span>
								{/if}
							</td>
							<td class="py-2 pr-4 text-muted">{fmtDate(a.connected_at)}</td>
							<td class="py-2 pr-4 text-muted">{fmtDate(a.last_heartbeat)}</td>
							<td class="py-2">
								<button class="btn btn-ghost text-xs" on:click={() => toggleDetails(a)}>
									{selectedAgent?.agent_id === a.agent_id ? 'Hide' : 'Details'}
								</button>
							</td>
						</tr>
						{#if selectedAgent?.agent_id === a.agent_id}
							<tr>
								<td colspan="7" class="px-4 py-3 bg-raised space-y-4">
									<div class="flex items-center justify-between">
										<h3 class="section-title">Recent metrics</h3>
										<div class="flex gap-1">
											{#each WINDOW_OPTIONS as w}
												<button
													class="btn btn-xs {panelHours === w.hours ? 'btn-primary' : 'btn-ghost'}"
													on:click={() => loadPanel(a.hostname, w.hours)}>{w.label}</button
												>
											{/each}
										</div>
									</div>
									{#if panelLoading && panelHost === a.hostname}
										<p class="text-muted text-xs">Loading metrics…</p>
									{:else if panelSeries.length === 0}
										<p class="prose-note text-xs">
											No metrics stored for this host yet. Agents report every 60 seconds by default.
										</p>
									{:else}
										<div class="space-y-2">
											{#each panelSeries as s}
												<Sparkline
													points={s.points}
													metric={s.metric}
													color={METRIC_COLORS[s.metric] ?? 'var(--chart-6)'}
												/>
											{/each}
										</div>
										{#if panelTruncated}
											<p class="prose-note text-xs">
												Showing the most recent points only — this window holds more than the API returns.
											</p>
										{/if}
									{/if}
									<dl class="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-1 text-xs">
										<dt class="text-muted">Agent ID</dt>
										<dd class="text-ink font-mono select-all">{a.agent_id}</dd>
										<dt class="text-muted">Hostname</dt>
										<dd class="text-ink">{a.hostname}</dd>
										<dt class="text-muted">Connected</dt>
										<dd class="text-ink">{fmtDate(a.connected_at)}</dd>
										<dt class="text-muted">Last Heartbeat</dt>
										<dd class="text-ink">{fmtDate(a.last_heartbeat)}</dd>
										{#each Object.entries(a.state ?? {}) as [k, v]}
											<dt class="text-muted">{metricLabel(k)}</dt>
											<dd class="text-ink num">{fmtVal(v)}</dd>
										{/each}
										{#if a.system_info}
											{#each Object.entries(a.system_info) as [k, v]}
												<dt class="text-muted">{k}</dt>
												<dd class="text-ink">{fmtVal(v)}</dd>
											{/each}
										{/if}
									</dl>
								</td>
							</tr>
						{/if}
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>