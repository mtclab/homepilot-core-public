<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { api, type HostDoc } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';

	let doc: HostDoc | null = null;
	let loading = true;
	let editingRole = false;
	let editingIp = false;
	let editRole = '';
	let editIp = '';

	$: id = $page.params.id ?? '';

	async function load() {
		loading = true;
		try {
			doc = await api.getHostDoc(id);
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			loading = false;
		}
	}

	async function saveRole() {
		try {
			await api.updateHost(id, { role: editRole });
			notify('Role saved', 'ok');
			editingRole = false;
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function saveIp() {
		try {
			await api.updateHost(id, { ip_address: editIp });
			notify('IP saved', 'ok');
			editingIp = false;
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function adopt() {
		try {
			await api.adoptHost(id);
			notify('Adopted', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function ignore() {
		try {
			await api.ignoreHost(id);
			notify('Ignored', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function unignore() {
		try {
			await api.updateHost(id, { import_state: 'pending' });
			notify('Unignored', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function reenrich() {
		try {
			const res = await api.enrichInventory([id]);
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
	{:else if !doc}
		<p class="text-red-400 text-sm">Not found.</p>
	{:else}
		{#each doc.hosts as h}
			<div class="card space-y-2">
				<div class="flex items-center justify-between">
					<h2 class="text-sm font-semibold text-slate-200">{h.hostname}</h2>
					<div class="flex gap-2">
						{#if h.source === 'discovered' && h.import_state !== 'ignored'}
							<button class="btn btn-sm text-xs" on:click={adopt}>Adopt</button>
							<button class="btn btn-sm text-xs" on:click={ignore}>Ignore</button>
						{:else if h.import_state === 'ignored'}
							<button class="btn btn-sm text-xs" on:click={unignore}>Unignore</button>
						{/if}
						<button class="btn btn-sm btn-ghost text-xs" on:click={reenrich}>Re-enrich</button>
					</div>
				</div>
				<dl class="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-xs">
					<dt class="text-slate-500">IP</dt>
					<dd class="text-slate-300 font-mono">
						{#if editingIp}
							<input type="text" class="input text-xs w-40" bind:value={editIp} />
							<button class="text-sky-400 text-xs" on:click={saveIp}>Save</button>
							<button class="text-slate-400 text-xs" on:click={() => (editingIp = false)}>Cancel</button>
						{:else}
							{(h.ip_address || '—') + (h.ip_source ? ` (${h.ip_source})` : '')}
							<button class="text-slate-400 text-xs ml-1" on:click={() => { editIp = h.ip_address || ''; editingIp = true; }}>Edit</button>
						{/if}
					</dd>
					<dt class="text-slate-500">Role</dt>
					<dd class="text-slate-300">
						{#if editingRole}
							<input type="text" class="input text-xs w-40" bind:value={editRole} />
							<button class="text-sky-400 text-xs" on:click={saveRole}>Save</button>
							<button class="text-slate-400 text-xs" on:click={() => (editingRole = false)}>Cancel</button>
						{:else}
							{h.role ?? '—'}
							{#if h.role_source === 'inferred'}<span class="text-amber-400" title="Inferred">?</span>{/if}
							<button class="text-slate-400 text-xs ml-1" on:click={() => { editRole = h.role || ''; editingRole = true; }}>Edit</button>
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
