<script lang="ts">
	// "How it is built" (#818): the host description the agent reports - what
	// runs, how it is exposed, what it depends on - grouped for reading, with
	// when it was collected, what changed since the last one, and the parts the
	// agent's user could not see. "Collect now" asks the agent (read-only on
	// the host); "Export to knowledge base" records it as the host's as-built
	// note, superseding the previous one. Self-contained like HostNetwork.
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		ApiError,
		collectHostDescription,
		exportHostAsBuilt,
		getHostDescription,
		type AsBuiltExport,
		type HostDescriptionView,
	} from '$lib/api';
	import { exactTime, timeAgo } from '$lib/relativeTime';

	export let hostId: string;
	export let canWrite = false;

	let view: HostDescriptionView | null = null;
	let unread = '';
	let busy = '';
	let failure = '';
	let exported: AsBuiltExport | null = null;

	function message(e: unknown): string {
		return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e);
	}

	async function load() {
		try {
			view = await getHostDescription(hostId);
			unread = '';
		} catch (e) {
			// Never "no description" when the answer is "could not ask".
			unread = message(e);
		}
	}

	async function collect() {
		busy = 'collect';
		failure = '';
		try {
			view = await collectHostDescription(hostId);
		} catch (e) {
			failure = message(e);
			await load();
		} finally {
			busy = '';
		}
	}

	async function exportNote() {
		busy = 'export';
		failure = '';
		try {
			exported = await exportHostAsBuilt(hostId);
			await load();
		} catch (e) {
			failure = message(e);
		} finally {
			busy = '';
		}
	}

	onMount(() => void load());

	type Node = any;
	$: d = (view?.description ?? null) as Node;
	$: services = (d?.units?.units ?? []).filter((u: Node) => u.type !== 'timer');
	$: timers = (d?.units?.units ?? []).filter((u: Node) => u.type === 'timer');
	$: hidden = d
		? ([
				['Listening sockets', d.listening],
				['systemd units', d.units],
				['Docker', d.docker],
				['nginx', d.nginx],
				['Certificates', d.certificates],
				['ufw', d.firewall?.ufw],
				['nftables', d.firewall?.nftables],
				['Updates', d.updates],
				['Time sync', d.time],
			] as [string, Node][]).filter(([, s]) => s && ['not_visible', 'partial', 'error'].includes(s.status))
		: [];

	function absent(s: Node, what: string): string {
		if (!s) return '';
		if (s.status === 'not_installed') return `No ${what} on this host.`;
		if (s.status === 'not_visible' || s.status === 'error') return s.reason ?? s.status;
		return '';
	}

	function target(loc: Node): string {
		if (loc.proxy_pass) return `proxy to ${loc.proxy_pass}`;
		if (loc.pass) return loc.pass;
		if (loc.alias || loc.root) return `files from ${loc.alias || loc.root}`;
		if (loc.return) return `return ${loc.return}`;
		return 'inherits';
	}

	function ports(c: Node): string {
		return (c.ports ?? [])
			.map((p: Node) => (p.host_port ? `${p.host_port}->${p.container_port}/${p.proto}` : `${p.container_port}/${p.proto}`))
			.join(', ');
	}
</script>

<div class="space-y-4" data-host-built>
	<section class="card p-4 space-y-2" aria-labelledby="built-head">
		<div class="flex flex-wrap items-baseline justify-between gap-2">
			<h2 class="section-title" id="built-head">How it is built</h2>
			<div class="flex gap-2">
				<button type="button" class="btn btn-ghost text-xs" on:click={collect} disabled={busy !== ''}>
					{busy === 'collect' ? 'Asking the agent...' : 'Collect now'}
				</button>
				{#if canWrite && d}
					<button type="button" class="btn btn-ghost text-xs" on:click={exportNote} disabled={busy !== ''}>
						{busy === 'export' ? 'Recording...' : 'Export to knowledge base'}
					</button>
				{/if}
			</div>
		</div>
		{#if unread}
			<p class="text-danger text-sm" role="alert">Could not read the description: {unread}</p>
		{:else if !view}
			<p class="text-muted text-sm">Loading...</p>
		{:else}
			{#if d}
				<p class="text-sm text-muted">
					Collected <span title={exactTime(view.collected_at)}>{timeAgo(view.collected_at)}</span>
					by agent {d.agent?.version ?? ''} ({d.agent?.privileged ? 'privileged' : `unprivileged, as ${d.agent?.run_as ?? 'its own user'}`}).
					{d.host?.os ?? ''}{d.host?.kernel ? `, kernel ${d.host.kernel}` : ''}.
				</p>
			{:else if !view.agent.connected}
				<p class="text-sm text-muted">No description yet: this host's agent is not connected.</p>
			{:else if !view.agent.can_describe}
				<p class="text-sm text-muted">
					No description yet: the agent ({view.agent.version ?? 'unknown version'}) is too old to describe its host.
					Upgrade it from the Agent tab.
				</p>
			{:else}
				<p class="text-sm text-muted">No description yet. Collect one now, or wait for the daily round.</p>
			{/if}
			{#if failure}
				<p class="text-danger text-sm" role="alert">{failure}</p>
			{:else if view.last_error}
				<p class="text-danger text-sm">Last attempt failed {timeAgo(view.last_error_at)}: {view.last_error}</p>
			{/if}
			{#if exported}
				<p class="text-ok text-sm" role="status">
					{exported.unchanged ? 'Unchanged since the last export:' : 'Recorded in the'}
					<a href="{base}/kb">knowledge base</a> as {exported.note_id}{exported.superseded ? `, superseding ${exported.superseded}` : ''}.
				</p>
			{:else if view.as_built}
				<p class="text-sm text-muted">As-built note {view.as_built.note_id}, exported {timeAgo(view.as_built.exported_at)}.</p>
			{/if}
		{/if}
	</section>

	{#if view && d}
		{#if view.previous_collected_at}
			<section class="card p-4 space-y-1" aria-labelledby="built-changes">
				<h2 class="section-title" id="built-changes">Changed since {timeAgo(view.previous_collected_at)}</h2>
				{#if view.changes.length === 0}
					<p class="text-sm text-muted">Nothing.</p>
				{/if}
				<ul class="text-sm space-y-0.5">
					{#each view.changes as c}
						{#each c.added as item}<li><span class="text-ok">+</span> {c.part}: {item}</li>{/each}
						{#each c.removed as item}<li><span class="text-danger">-</span> {c.part}: {item}</li>{/each}
					{/each}
				</ul>
			</section>
		{/if}

		<section class="card p-4 space-y-2" aria-labelledby="built-runs">
			<h2 class="section-title" id="built-runs">What runs</h2>
			{#if absent(d.units, 'systemd')}<p class="text-sm text-muted">{absent(d.units, 'systemd')}</p>{/if}
			{#if services.length}
				<table class="w-full text-sm">
					<thead><tr class="text-muted text-left"><th>Service</th><th>State</th><th>Enabled</th><th>Program</th></tr></thead>
					<tbody>
						{#each services as u}
							<tr>
								<td>{u.name}{#if u.custom} <span class="text-muted">(custom)</span>{/if}</td>
								<td>{[u.active, u.sub].filter(Boolean).join('/') || '-'}</td>
								<td>{u.enabled ?? '-'}</td>
								<td class="font-mono text-xs">{(u.programs ?? []).join(', ')}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
			{#if timers.length}
				<p class="text-sm"><span class="text-muted">Timers:</span> {timers.map((t: Node) => t.name).join(', ')}</p>
			{/if}
			{#if absent(d.docker, 'Docker')}<p class="text-sm text-muted">{absent(d.docker, 'Docker')}</p>{/if}
			{#each d.docker?.compose_projects ?? [] as p}
				<p class="text-sm">
					Compose project <strong>{p.name}</strong> in <span class="font-mono text-xs">{p.working_dir}</span>:
					{p.services.join(', ')}
				</p>
			{/each}
			{#if (d.docker?.containers ?? []).length}
				<table class="w-full text-sm">
					<thead><tr class="text-muted text-left"><th>Container</th><th>Image</th><th>State</th><th>Ports</th><th>Restarts</th></tr></thead>
					<tbody>
						{#each d.docker.containers as c}
							<tr>
								<td>{c.name}</td>
								<td class="font-mono text-xs">{c.image}</td>
								<td>{c.state}</td>
								<td>{ports(c) || '-'}</td>
								<td>{c.restart_count} ({c.restart_policy || 'no policy'})</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>

		<section class="card p-4 space-y-2" aria-labelledby="built-exposed">
			<h2 class="section-title" id="built-exposed">How it is exposed</h2>
			{#if (d.listening?.sockets ?? []).length}
				<p class="text-sm">
					<span class="text-muted">Listening:</span>
					{d.listening.sockets
						.map((s: Node) => `${s.proto} ${s.address}:${s.port}${s.process ? ` (${s.process})` : ''}`)
						.join(', ')}
				</p>
			{/if}
			{#if absent(d.nginx, 'nginx')}<p class="text-sm text-muted">{absent(d.nginx, 'nginx')}</p>{/if}
			{#each d.nginx?.servers ?? [] as s}
				<div class="text-sm">
					<strong>{(s.server_names ?? []).join(', ') || '(default)'}</strong>
					<span class="text-muted">listen {(s.listen ?? []).join(', ')}</span>
					{#if s.tls?.verify_client}<span class="text-muted">; client certificates {s.tls.verify_client}</span>{/if}
					<ul class="ml-4">
						{#each s.locations ?? [] as loc}<li><span class="font-mono text-xs">{loc.match}</span> -> {target(loc)}</li>{/each}
						{#if s.proxy_pass}<li>proxy to {s.proxy_pass}</li>{/if}
						{#if s.return}<li>return {s.return}</li>{/if}
					</ul>
				</div>
			{/each}
			<p class="text-sm">
				<span class="text-muted">Firewall:</span>
				{#if d.firewall?.ufw?.status === 'not_installed'}no ufw{:else}ufw {d.firewall?.ufw?.enabled ? 'enabled' : d.firewall?.ufw?.enabled === false ? 'disabled' : 'state unknown'}{#if (d.firewall?.ufw?.rules ?? []).length}, {d.firewall.ufw.rules.length} rule(s){/if}{/if};
				{#if d.firewall?.nftables?.status === 'ok'}nftables {(d.firewall.nftables.tables ?? []).map((t: Node) => `${t.family} ${t.name}`).join(', ') || 'empty'}{:else}nftables {d.firewall?.nftables?.status?.replace('_', ' ') ?? 'unknown'}{/if}
			</p>
		</section>

		<section class="card p-4 space-y-2" aria-labelledby="built-depends">
			<h2 class="section-title" id="built-depends">What it depends on</h2>
			<ul class="text-sm space-y-0.5">
				{#each d.certificates?.certificates ?? [] as c}
					<li>
						Certificate <span class="font-mono text-xs">{c.path}</span>
						{#if c.status === 'ok'}
							({(c.names ?? []).join(', ') || c.subject}) expires {exactTime(c.not_after)}
							<span class={c.days_left < 14 ? 'text-danger' : 'text-muted'}>({c.days_left} days)</span>
						{:else}<span class="text-muted">- {c.reason ?? c.status}</span>{/if}
					</li>
				{/each}
				{#each d.certificates?.letsencrypt ?? [] as r}
					<li>certbot renews {r.domains.join(', ')} via {r.authenticator ?? 'unknown'}</li>
				{/each}
				{#if d.updates?.status === 'ok'}
					<li>{d.updates.pending} pending update(s), {d.updates.security} security{d.updates.reboot_required ? '; a reboot is required' : ''}</li>
				{/if}
				{#if d.time?.status === 'ok'}
					<li>Time {d.time.synchronized ? 'synchronised' : 'NOT synchronised'}, timezone {d.time.timezone ?? 'unknown'}</li>
				{/if}
			</ul>
		</section>

		{#if hidden.length || (d.truncated ?? []).length}
			<section class="card p-4 space-y-1" aria-labelledby="built-hidden">
				<h2 class="section-title" id="built-hidden">Not visible to the agent</h2>
				<ul class="text-sm text-muted space-y-0.5">
					{#each hidden as [name, s]}<li>{name}: {s.reason ?? s.status}</li>{/each}
					{#if (d.truncated ?? []).length}<li>Trimmed to fit: {d.truncated.join('; ')}</li>{/if}
				</ul>
			</section>
		{/if}
	{/if}
</div>
