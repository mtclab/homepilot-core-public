<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { api, type HostDoc } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';

	let doc: HostDoc | null = null;
	let loading = true;
	let loadError = '';
	// The doc view can list several LIKE-matched hosts. Edit state is scoped to a
	// specific host id so an edit/action never lands on the wrong row.
	let editRoleHostId: string | null = null;
	let editIpHostId: string | null = null;
	let editRole = '';
	let editIp = '';

	$: id = $page.params.id ?? '';

	async function load() {
		loading = true;
		loadError = '';
		try {
			doc = await api.getHostDoc(id);
		} catch (e) {
			loadError = e instanceof Error ? e.message : String(e);
			notify(loadError, 'err');
		} finally {
			loading = false;
		}
	}

	function startEditRole(h: { id: string; role?: string }) {
		editRole = h.role || '';
		editRoleHostId = h.id;
	}

	function startEditIp(h: { id: string; ip_address?: string }) {
		editIp = h.ip_address || '';
		editIpHostId = h.id;
	}

	async function saveRole(hostId: string) {
		try {
			await api.updateHost(hostId, { role: editRole });
			notify('Role saved', 'ok');
			editRoleHostId = null;
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function saveIp(hostId: string) {
		try {
			await api.updateHost(hostId, { ip_address: editIp });
			notify('IP saved', 'ok');
			editIpHostId = null;
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function adopt(hostId: string) {
		try {
			await api.adoptHost(hostId);
			notify('Adopted', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function ignore(hostId: string) {
		try {
			await api.ignoreHost(hostId);
			notify('Ignored', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function unignore(hostId: string) {
		try {
			await api.updateHost(hostId, { import_state: 'pending' });
			notify('Unignored', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function reenrich(hostId: string) {
		try {
			const res = await api.enrichInventory([hostId]);
			notify(`Enriched ${res.enriched}, failed ${res.failed}`, 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	const STATUS_CLASSES: Record<string, string> = {
		proposed: 'badge-proposed',
		approved: 'badge-approved',
		applied: 'badge-applied',
		rejected: 'badge-rejected',
		revoked: 'badge-revoked',
		failed: 'badge-failed',
		superseded: 'badge-superseded',
	};
	function statusClass(s: string): string {
		return STATUS_CLASSES[s] ?? 'badge-proposed';
	}

	onMount(load);
</script>

<div class="space-y-5">
	<div class="flex items-center gap-3">
		<a href="{base}/inventory" class="text-slate-500 hover:text-slate-300 text-xs">← Inventory</a>
		<h1 class="text-lg font-bold text-slate-100">{doc?.hosts?.[0]?.hostname ?? id}</h1>
		{#if doc?.hosts?.[0]?.source === 'discovered'}
			<span class="text-amber-400 text-xs">Discovered</span>
		{:else if doc?.hosts?.[0]?.source === 'imported'}
			<span class="text-emerald-400 text-xs">Imported</span>
		{:else if doc?.hosts?.[0]?.source === 'hp_created'}
			<span class="text-sky-400 text-xs">HP-Created</span>
		{/if}
	</div>

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-red-400">Could not load this host.</p>
			<p class="text-xs text-slate-500">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Retry</button>
		</div>
	{:else if !doc}
		<p class="text-red-400 text-sm">Not found.</p>
	{:else}
		{#each doc.hosts as h}
			<div class="card space-y-2">
				<div class="flex items-center justify-between">
					<h2 class="text-sm font-semibold text-slate-200">{h.hostname}</h2>
					<div class="flex gap-2">
						{#if h.source === 'discovered' && h.import_state !== 'ignored'}
							<button class="btn btn-sm text-xs" on:click={() => adopt(h.id)}>Adopt</button>
							<button class="btn btn-sm text-xs" on:click={() => ignore(h.id)}>Ignore</button>
						{:else if h.import_state === 'ignored'}
							<button class="btn btn-sm text-xs" on:click={() => unignore(h.id)}>Unignore</button>
						{/if}
						<button class="btn btn-sm btn-ghost text-xs" on:click={() => reenrich(h.id)}>Re-enrich</button>
					</div>
				</div>
				<dl class="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-xs">
					<dt class="text-slate-500">IP</dt>
					<dd class="text-slate-300 font-mono">
						{#if editIpHostId === h.id}
							<input type="text" class="input text-xs w-40" bind:value={editIp} />
							<button class="text-sky-400 text-xs" on:click={() => saveIp(h.id)}>Save</button>
							<button class="text-slate-400 text-xs" on:click={() => (editIpHostId = null)}>Cancel</button>
						{:else}
							{(h.ip_address || '—') + (h.ip_source ? ` (${h.ip_source})` : '')}
							<button class="text-slate-400 text-xs ml-1" on:click={() => startEditIp(h)}>Edit</button>
						{/if}
					</dd>
					<dt class="text-slate-500">Role</dt>
					<dd class="text-slate-300">
						{#if editRoleHostId === h.id}
							<input type="text" class="input text-xs w-40" bind:value={editRole} />
							<button class="text-sky-400 text-xs" on:click={() => saveRole(h.id)}>Save</button>
							<button class="text-slate-400 text-xs" on:click={() => (editRoleHostId = null)}>Cancel</button>
						{:else}
							{h.role ?? '—'}
							{#if h.role_source === 'inferred'}<span class="text-amber-400" title="Inferred">?</span>{/if}
							<button class="text-slate-400 text-xs ml-1" on:click={() => startEditRole(h)}>Edit</button>
						{/if}
					</dd>
					<dt class="text-slate-500">Status</dt> <dd class="text-slate-300">{h.status ?? '—'}</dd>
					<dt class="text-slate-500">PVE Status</dt> <dd class="text-slate-300">{h.pve_status ?? '—'}</dd>
					<dt class="text-slate-500">Managed</dt> <dd class="text-slate-300">{h.managed ? 'yes' : 'no'}</dd>
					<dt class="text-slate-500">Node</dt> <dd class="text-slate-300">{h.node ?? '—'}</dd>
					<dt class="text-slate-500">Import State</dt> <dd class="text-slate-300">{h.import_state ?? '—'}</dd>
					<dt class="text-slate-500">Description</dt> <dd class="text-slate-300">{h.description ?? '—'}</dd>
				</dl>
			</div>
		{/each}

		{#if doc.services.length}
			<div class="card space-y-2">
				<h2 class="text-sm font-semibold text-slate-200">Services ({doc.services.length})</h2>
				<table class="w-full text-xs">
					<thead>
						<tr class="text-slate-400 border-b border-slate-700">
							<th class="text-left pb-1 pr-4">Name</th>
							<th class="text-left pb-1 pr-4">Port</th>
							<th class="text-left pb-1">Status</th>
						</tr>
					</thead>
					<tbody>
						{#each doc.services as s}
							<tr class="border-b border-slate-700/40">
								<td class="py-1 pr-4 text-slate-200">{s.name}</td>
								<td class="py-1 pr-4 text-slate-400 font-mono">{s.port ?? '—'}</td>
								<td class="py-1 text-slate-400">{s.status ?? '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if doc.kb_entries.length}
			<div class="card space-y-2">
				<h2 class="text-sm font-semibold text-slate-200">KB ({doc.kb_entries.length})</h2>
				<div class="space-y-2">
					{#each doc.kb_entries as e}
						<div class="bg-slate-900 rounded p-3 text-xs space-y-1">
							{#if e.title}
								<p class="text-slate-300 font-medium">{e.title}</p>
							{/if}
							<p class="text-slate-400 line-clamp-3">{e.content}</p>
							<p class="text-slate-600">{e.kind} · {e.source}</p>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		{#if doc.artifact_history.length}
			<div class="card space-y-2">
				<h2 class="text-sm font-semibold text-slate-200">Artifact History ({doc.artifact_history.length})</h2>
				<table class="w-full text-xs">
					<thead>
						<tr class="text-slate-400 border-b border-slate-700">
							<th class="text-left pb-1 pr-4">Intent</th>
							<th class="text-left pb-1 pr-4">Kind</th>
							<th class="text-left pb-1 pr-4">Status</th>
							<th class="text-left pb-1">Date</th>
						</tr>
					</thead>
					<tbody>
						{#each doc.artifact_history as a}
							<tr class="border-b border-slate-700/40">
								<td class="py-1 pr-4 text-slate-200 max-w-xs truncate">{a.intent}</td>
								<td class="py-1 pr-4 text-slate-400">{a.kind}</td>
								<td class="py-1 pr-4">
									<span class="badge {statusClass(a.status)}">{a.status}</span>
								</td>
								<td class="py-1 text-slate-500">{a.created_at}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}
</div>
