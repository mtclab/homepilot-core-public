<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type AgentInfo } from '$lib/api';
	import { notify } from '$lib/stores';

	let agents: AgentInfo[] = [];
	let loading = true;
	let isAdmin = true;

	let showBootstrap = false;
	let bootstrapData: { bootstrap_token: string; hub_host: string; hub_port: number } | null = null;
	let generating = false;

	let showHubToken = false;
	let hubData: { auth_token: string; hub_host: string; hub_port: number } | null = null;
	let loadingHubToken = false;

	let selectedAgent: AgentInfo | null = null;

	async function load() {
		loading = true;
		try {
			agents = await api.listAgents();
			isAdmin = true;
		} catch (e: any) {
			if (e?.message?.startsWith('403')) {
				isAdmin = false;
				notify('Admin scope required to view agents', 'err');
			} else {
				notify(String(e), 'err');
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

	onMount(load);
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="text-lg font-bold text-slate-100">Agents</h1>
		<div class="flex gap-2 items-center">
			<span class="text-slate-500 text-xs">{agents.length} connected</span>
			{#if isAdmin}
				<button class="btn btn-primary text-xs" on:click={() => (showBootstrap = !showBootstrap)}>
					{showBootstrap ? 'Cancel' : 'Enroll Agent'}
				</button>
				<button class="btn btn-ghost text-xs" on:click={() => (showHubToken = !showHubToken)}>
					{showHubToken ? 'Hide' : 'Hub Token'}
				</button>
			{/if}
		</div>
	</div>

	{#if showBootstrap}
		<div class="card p-4 space-y-4">
			<h2 class="text-sm font-semibold text-slate-300">Enroll New Agent</h2>
			<p class="text-xs text-slate-400">
				Bootstrap tokens are single-use and expire — fine for a one-off connect. For a
				permanent install that survives agent reboots, use the shared Hub Auth Token below.
			</p>

			{#if !bootstrapData}
				<button class="btn btn-primary text-xs" on:click={generateBootstrap} disabled={generating}>
					{generating ? 'Generating…' : 'Generate Bootstrap Token'}
				</button>
			{:else}
				<div class="space-y-3">
					<div class="bg-slate-900 border border-emerald-700 rounded p-3">
						<p class="text-xs text-emerald-400 font-mono mb-1">Hub endpoint:</p>
						<code class="text-xs text-slate-200 select-all">{fmtHost(bootstrapData.hub_host, bootstrapData.hub_port)}</code>
					</div>
					<div class="bg-slate-900 border border-emerald-700 rounded p-3">
						<p class="text-xs text-emerald-400 font-mono mb-1">Bootstrap token (copy now — shown once):</p>
						<code class="text-xs text-slate-200 break-all select-all">{bootstrapData.bootstrap_token}</code>
					</div>
					<div class="bg-slate-900 border border-slate-600 rounded p-3">
						<p class="text-xs text-slate-400 font-mono mb-1">One-liner install:</p>
						<code class="text-xs text-slate-200 break-all select-all">
							curl -fsSL https://github.com/mtclab/homepilot-core-public/releases/latest/download/install-agent.sh | bash -s -- --hub {fmtHost(bootstrapData.hub_host, bootstrapData.hub_port)} --token {bootstrapData.bootstrap_token}
						</code>
					</div>
					<button class="btn btn-ghost text-xs" on:click={generateBootstrap} disabled={generating}>
						Regenerate
					</button>
				</div>
			{/if}
		</div>
	{/if}

	{#if showHubToken}
		<div class="card p-4 space-y-3">
			<h2 class="text-sm font-semibold text-slate-300">Hub Auth Token</h2>
			<p class="text-xs text-slate-400">The shared token agents use to connect to the hub.</p>

			{#if !hubData}
				<button class="btn btn-primary text-xs" on:click={getHubToken} disabled={loadingHubToken}>
					{loadingHubToken ? 'Loading…' : 'Show Hub Token'}
				</button>
			{:else}
				<div class="bg-slate-900 border border-slate-600 rounded p-3">
					<p class="text-xs text-slate-400 mb-1">Hub: <code class="text-slate-200">{fmtHost(hubData.hub_host, hubData.hub_port)}</code></p>
					<code class="text-xs text-slate-200 select-all">{hubData.auth_token}</code>
				</div>
				<div class="bg-slate-900 border border-slate-600 rounded p-3">
					<p class="text-xs text-slate-400 font-mono mb-1">One-liner install (survives reboots):</p>
					<code class="text-xs text-slate-200 break-all select-all">
						curl -fsSL https://github.com/mtclab/homepilot-core-public/releases/latest/download/install-agent.sh | bash -s -- --hub {fmtHost(hubData.hub_host, hubData.hub_port)} --token {hubData.auth_token}
					</code>
				</div>
			{/if}
		</div>
	{/if}

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if !isAdmin}
		<div class="card p-6 text-center">
			<p class="text-slate-400">You need admin scope to view connected agents.</p>
		</div>
	{:else if agents.length === 0}
		<div class="card p-6 text-center">
			<p class="text-slate-400">No agents connected.</p>
			<p class="text-xs text-slate-500 mt-2">Click "Enroll Agent" to generate a bootstrap token and install the agent on a host.</p>
		</div>
	{:else}
		<div class="card overflow-x-auto">
			<table class="w-full text-xs">
				<thead>
					<tr class="text-slate-400 border-b border-slate-700">
						<th class="text-left pb-2 pr-4">Hostname</th>
						<th class="text-left pb-2 pr-4">Agent ID</th>
						<th class="text-left pb-2 pr-4">State</th>
						<th class="text-left pb-2 pr-4">Connected</th>
						<th class="text-left pb-2 pr-4">Last Heartbeat</th>
						<th class="text-left pb-2">Actions</th>
					</tr>
				</thead>
				<tbody>
					{#each agents as a}
						<tr class="border-b border-slate-700/50 hover:bg-slate-800/30">
							<td class="py-2 pr-4 text-slate-200 font-mono">{a.hostname}</td>
							<td class="py-2 pr-4 text-slate-400 font-mono text-[11px]">{a.agent_id.slice(0, 8)}…</td>
							<td class="py-2 pr-4">
								<span class="inline-flex items-center gap-1">
									<span class="w-1.5 h-1.5 rounded-full {a.state === 'connected' ? 'bg-emerald-400' : 'bg-yellow-400'}"></span>
									<span class="{a.state === 'connected' ? 'text-emerald-400' : 'text-yellow-400'}">{a.state}</span>
								</span>
							</td>
							<td class="py-2 pr-4 text-slate-500">{fmtDate(a.connected_at)}</td>
							<td class="py-2 pr-4 text-slate-500">{fmtDate(a.last_heartbeat)}</td>
							<td class="py-2">
								<button class="btn btn-ghost text-xs" on:click={() => (selectedAgent = selectedAgent?.agent_id === a.agent_id ? null : a)}>
									Details
								</button>
							</td>
						</tr>
						{#if selectedAgent?.agent_id === a.agent_id}
							<tr>
								<td colspan="6" class="px-4 py-3 bg-slate-800/40">
									<dl class="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-1 text-xs">
										<dt class="text-slate-500">Agent ID</dt>
										<dd class="text-slate-300 font-mono select-all">{a.agent_id}</dd>
										<dt class="text-slate-500">Hostname</dt>
										<dd class="text-slate-300">{a.hostname}</dd>
										<dt class="text-slate-500">State</dt>
										<dd class="text-slate-300">{a.state}</dd>
										<dt class="text-slate-500">Connected</dt>
										<dd class="text-slate-300">{fmtDate(a.connected_at)}</dd>
										<dt class="text-slate-500">Last Heartbeat</dt>
										<dd class="text-slate-300">{fmtDate(a.last_heartbeat)}</dd>
										{#if a.system_info}
											{#each Object.entries(a.system_info) as [k, v]}
												<dt class="text-slate-500">{k}</dt>
												<dd class="text-slate-300">{String(v)}</dd>
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