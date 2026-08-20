<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { page } from '$app/stores';
	import { api, sessionStore, type AgentInstallEligibility, type HostDoc, type Task } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';

	let doc: HostDoc | null = null;
	let loading = true;
	let loadError = '';
	// Zero-touch agent install (ADR-004 S4), per host id: whether HomePilot can
	// enrol this guest over qemu-guest-agent, and the task once one is running.
	let enroll: Record<string, AgentInstallEligibility> = {};
	let enrollTask: Record<string, Task> = {};
	let installing: Record<string, boolean> = {};
	let destroyed = false;
	onDestroy(() => (destroyed = true));
	// The doc view can list several LIKE-matched hosts. Edit state is scoped to a
	// specific host id so an edit/action never lands on the wrong row.
	let editRoleHostId: string | null = null;
	let editIpHostId: string | null = null;
	let editRole = '';
	let editIp = '';

	$: id = $page.params.id ?? '';
	// Adopt / Ignore / Re-enrich / inline edits are write-scoped server-side.
	// Default-deny while the session is still loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);

	async function load() {
		loading = true;
		loadError = '';
		try {
			doc = await api.getHostDoc(id);
			await loadEligibility();
		} catch (e) {
			loadError = e instanceof Error ? e.message : String(e);
			notify(loadError, 'err');
		} finally {
			loading = false;
		}
	}

	// Best-effort: the answer is admin-only, so a read-only session simply gets
	// no install section rather than an error banner over a page that otherwise
	// loaded fine.
	async function loadEligibility() {
		const hosts = doc?.hosts ?? [];
		const answers = await Promise.all(
			hosts.map((h) => api.agentInstallEligibility(h.id).catch(() => null))
		);
		const next: Record<string, AgentInstallEligibility> = {};
		hosts.forEach((h, i) => {
			const answer = answers[i];
			if (answer) next[h.id] = answer;
		});
		enroll = next;
	}

	async function installAgent(hostId: string) {
		installing = { ...installing, [hostId]: true };
		try {
			const { task_id } = await api.installAgent(hostId);
			notify('Installing the agent — progress in Tasks', 'ok');
			await pollInstall(hostId, task_id);
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			installing = { ...installing, [hostId]: false };
		}
	}

	// The task is the progress view. It ends in succeeded only once the agent is
	// actually connected to the hub, so this line is the enrolment, not a
	// "request accepted" acknowledgement.
	async function pollInstall(hostId: string, taskId: string) {
		for (;;) {
			const task = await api.getTask(taskId);
			enrollTask = { ...enrollTask, [hostId]: task };
			if (task.status !== 'pending' && task.status !== 'running') break;
			await new Promise((r) => setTimeout(r, 3000));
			if (destroyed) return;
		}
		const finished = enrollTask[hostId];
		notify(
			finished.status === 'succeeded' ? 'Agent enrolled' : `Agent install failed: ${finished.error ?? ''}`,
			finished.status === 'succeeded' ? 'ok' : 'err'
		);
		await loadEligibility();
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
		<a href="{base}/inventory" class="text-muted hover:text-ink text-xs">← Inventory</a>
		<h1 class="page-title">{doc?.hosts?.[0]?.hostname ?? id}</h1>
		{#if doc?.hosts?.[0]?.source === 'discovered'}
			<span class="text-warn text-xs">Discovered</span>
		{:else if doc?.hosts?.[0]?.source === 'imported'}
			<span class="text-ok text-xs">Imported</span>
		{:else if doc?.hosts?.[0]?.source === 'hp_created'}
			<span class="text-accent text-xs">HP-Created</span>
		{/if}
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load this host.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Retry</button>
		</div>
	{:else if !doc}
		<p class="text-danger text-sm">Not found.</p>
	{:else}
		{#each doc.hosts as h}
			<div class="card space-y-2">
				<div class="flex items-center justify-between">
					<h2 class="section-title">{h.hostname}</h2>
					{#if canWrite}
						<div class="flex gap-2">
							{#if h.source === 'discovered' && h.import_state !== 'ignored'}
								<button class="btn btn-sm text-xs" on:click={() => adopt(h.id)}>Adopt</button>
								<button class="btn btn-sm text-xs" on:click={() => ignore(h.id)}>Ignore</button>
							{:else if h.import_state === 'ignored'}
								<button class="btn btn-sm text-xs" on:click={() => unignore(h.id)}>Unignore</button>
							{/if}
							{#if enroll[h.id]?.eligible}
								<button
									class="btn btn-sm text-xs"
									on:click={() => installAgent(h.id)}
									disabled={installing[h.id] || enroll[h.id]?.in_flight}
									title={enroll[h.id]?.message}
								>
									{installing[h.id] || enroll[h.id]?.in_flight ? 'Installing…' : 'Install agent'}
								</button>
							{/if}
							<button class="btn btn-sm btn-ghost text-xs" on:click={() => reenrich(h.id)}>Re-enrich</button>
						</div>
					{/if}
				</div>
				{#if enrollTask[h.id]}
					<p class="prose-note text-xs">
						Agent install: <span class="text-ink">{enrollTask[h.id].status}</span>
						{#if enrollTask[h.id].error}<span class="text-danger">— {enrollTask[h.id].error}</span>{/if}
						· <a class="text-accent hover:text-accent-strong" href="{base}/tasks">Tasks ↗</a>
					</p>
				{:else if enroll[h.id] && !enroll[h.id].eligible}
					<p class="prose-note text-xs">
						No agent install from here: {enroll[h.id].message}
					</p>
				{/if}
				<dl class="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-xs">
					<dt class="text-muted">IP</dt>
					<dd class="text-ink font-mono">
						{#if editIpHostId === h.id}
							<input type="text" class="input text-xs w-40" bind:value={editIp} />
							<button class="text-accent text-xs" on:click={() => saveIp(h.id)}>Save</button>
							<button class="text-muted text-xs" on:click={() => (editIpHostId = null)}>Cancel</button>
						{:else}
							{(h.ip_address || '—') + (h.ip_source ? ` (${h.ip_source})` : '')}
							{#if canWrite}
								<button class="text-muted text-xs ml-1" on:click={() => startEditIp(h)}>Edit</button>
							{/if}
						{/if}
					</dd>
					<dt class="text-muted">Role</dt>
					<dd class="text-ink">
						{#if editRoleHostId === h.id}
							<input type="text" class="input text-xs w-40" bind:value={editRole} />
							<button class="text-accent text-xs" on:click={() => saveRole(h.id)}>Save</button>
							<button class="text-muted text-xs" on:click={() => (editRoleHostId = null)}>Cancel</button>
						{:else}
							{h.role ?? '—'}
							{#if h.role_source === 'inferred'}<span class="text-warn" title="Inferred">?</span>{/if}
							{#if canWrite}
								<button class="text-muted text-xs ml-1" on:click={() => startEditRole(h)}>Edit</button>
							{/if}
						{/if}
					</dd>
					<dt class="text-muted">Status</dt> <dd class="text-ink">{h.status ?? '—'}</dd>
					<dt class="text-muted">PVE Status</dt> <dd class="text-ink">{h.pve_status ?? '—'}</dd>
					<dt class="text-muted">Managed</dt> <dd class="text-ink">{h.managed ? 'yes' : 'no'}</dd>
					<dt class="text-muted">Node</dt> <dd class="text-ink">{h.node ?? '—'}</dd>
					<dt class="text-muted">Import State</dt> <dd class="text-ink">{h.import_state ?? '—'}</dd>
					<dt class="text-muted">Description</dt> <dd class="text-ink">{h.description ?? '—'}</dd>
				</dl>
			</div>
		{/each}

		{#if doc.services.length}
			<div class="card space-y-2">
				<h2 class="section-title">Services ({doc.services.length})</h2>
				<table class="data-table text-xs">
					<thead>
						<tr>
							<th class="text-left pb-1 pr-4">Name</th>
							<th class="num pb-1 pr-4">Port</th>
							<th class="text-left pb-1">Status</th>
						</tr>
					</thead>
					<tbody>
						{#each doc.services as s}
							<tr class="border-b border-divider">
								<td class="py-1 pr-4 text-ink">{s.name}</td>
								<td class="num py-1 pr-4 text-muted font-mono">{s.port ?? '—'}</td>
								<td class="py-1 text-muted">{s.status ?? '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if doc.kb_entries.length}
			<div class="card space-y-2">
				<h2 class="section-title">KB ({doc.kb_entries.length})</h2>
				<div class="space-y-2">
					{#each doc.kb_entries as e}
						<div class="bg-canvas rounded p-3 text-xs space-y-1">
							{#if e.title}
								<p class="text-ink font-medium">{e.title}</p>
							{/if}
							<p class="prose-body line-clamp-3">{e.content}</p>
							<p class="prose-note">{e.kind} · {e.source}</p>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		{#if doc.artifact_history.length}
			<div class="card space-y-2">
				<h2 class="section-title">Artifact History ({doc.artifact_history.length})</h2>
				<table class="data-table text-xs">
					<thead>
						<tr>
							<th class="text-left pb-1 pr-4">Intent</th>
							<th class="text-left pb-1 pr-4">Kind</th>
							<th class="text-left pb-1 pr-4">Status</th>
							<th class="text-left pb-1">Date</th>
						</tr>
					</thead>
					<tbody>
						{#each doc.artifact_history as a}
							<tr class="border-b border-divider">
								<td class="py-1 pr-4 text-ink max-w-xs truncate">{a.intent}</td>
								<td class="py-1 pr-4 text-muted">{a.kind}</td>
								<td class="py-1 pr-4">
									<span class="badge {statusClass(a.status)}">{a.status}</span>
								</td>
								<td class="py-1 text-muted">{a.created_at}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}
</div>
