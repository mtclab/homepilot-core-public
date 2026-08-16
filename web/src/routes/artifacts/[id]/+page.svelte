<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import { api, type ArtifactDetail, type Task } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import { onArtifactEvent } from '$lib/events';

	let detail: ArtifactDetail | null = null;
	let loading = true;
	let working = false;
	let activeTask: Task | null = null;
	let pollHandle: ReturnType<typeof setInterval> | null = null;
	let confirmAction: { fn: () => Promise<void>; label: string } | null = null;

	$: id = $page.params.id ?? '';
	$: status = detail?.frontmatter.status ?? '';
	$: hasActiveTask = detail?.active_task !== null && detail?.active_task !== undefined;

	const STATUS_CLASSES: Record<string, string> = {
		proposed: 'badge-proposed',
		approved: 'badge-approved',
		applied: 'badge-applied',
		rejected: 'badge-rejected',
		revoked: 'badge-revoked',
		failed: 'badge-failed',
		superseded: 'badge-superseded',
	};
	function statusClass(s: string): string { return STATUS_CLASSES[s] ?? 'badge-proposed'; }

	function fmtDate(s: string | undefined): string {
		return s ? new Date(s).toLocaleString() : '—';
	}

	function targetStr(): string {
		const t = detail?.frontmatter.target ?? {};
		return t.host ?? t.service ?? t.node ?? '—';
	}

	function getActions(): { label: string; cls: string; fn: () => Promise<void>; destructive?: boolean }[] {
		if (hasActiveTask || working) return [];
		switch (status) {
			case 'proposed':
				return [
					{ label: 'Approve', cls: 'btn-success', fn: doApprove },
					{ label: 'Reject', cls: 'btn-danger', fn: doReject, destructive: true },
				];
			case 'approved':
				return [{ label: 'Apply', cls: 'btn-success', fn: doApply }];
			case 'failed':
				return [
					{ label: 'Re-approve', cls: 'btn-success', fn: doApprove },
					{ label: 'Revoke', cls: 'btn-danger', fn: doRevoke, destructive: true },
				];
			case 'applied':
				const actions: { label: string; cls: string; fn: () => Promise<void>; destructive?: boolean }[] = [
					{ label: 'Revoke', cls: 'btn-danger', fn: doRevoke, destructive: true },
				];
				if (detail?.frontmatter?.replay_safe !== false) {
					actions.push({ label: 'Replay', cls: 'btn-success', fn: doApply });
				}
				return actions;
			default:
				return [];
		}
	}

	// `initial` shows the loading skeleton; a live SSE-driven refresh stays silent
	// so the panel doesn't flicker when an event lands.
	async function load(initial = true) {
		if (initial) loading = true;
		try {
			detail = await api.getArtifact(id);
			if (detail?.active_task) {
				startPolling(detail.active_task.id);
			}
		} catch (e) {
			if (initial) notify(String(e), 'err');
		} finally {
			loading = false;
		}
	}

	async function doApprove() {
		working = true;
		try {
			await api.approveArtifact(id);
			notify('Approved');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = false;
		}
	}

	async function doReject() {
		working = true;
		try {
			await api.rejectArtifact(id);
			notify('Rejected');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = false;
		}
	}

	async function doApply() {
		working = true;
		try {
			const res = await api.applyArtifact(id);
			notify('Apply queued');
			startPolling(res.task_id);
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = false;
		}
	}

	async function doRevoke() {
		working = true;
		try {
			const res = await api.revokeArtifact(id);
			notify('Revoke queued');
			startPolling(res.task_id);
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = false;
		}
	}

	function startPolling(taskId: string) {
		stopPolling();
		pollHandle = setInterval(async () => {
			try {
				const task = await api.getTask(taskId);
				activeTask = task;
				if (task.status === 'succeeded' || task.status === 'failed') {
					stopPolling();
					activeTask = null;
					if (task.status === 'failed') {
						notify('Task failed: ' + (task.error ?? 'unknown'), 'err');
					} else {
						notify('Task completed');
					}
					await load();
				}
			} catch {
				stopPolling();
			}
		}, 2000);
	}

	function stopPolling() {
		if (pollHandle) {
			clearInterval(pollHandle);
			pollHandle = null;
		}
	}

	function handleAction(action: { fn: () => Promise<void>; label: string; destructive?: boolean }) {
		if (action.destructive) {
			confirmAction = { fn: action.fn, label: action.label };
		} else {
			action.fn();
		}
	}

	function confirmDestructive() {
		if (confirmAction) {
			const fn = confirmAction.fn;
			confirmAction = null;
			fn();
		}
	}

	onMount(load);
	// Refresh this artifact when an event for IT arrives (drift, or a status
	// change made elsewhere). Other artifacts' events are ignored. The active-task
	// poller above still drives fine-grained apply/revoke progress.
	const unsub = onArtifactEvent((e) => {
		if (e.data?.id === id) load(false);
	});
	onDestroy(() => {
		stopPolling();
		unsub();
	});
</script>

<div class="space-y-5">
	<div class="flex items-center gap-3">
		<a href="{base}/artifacts" class="text-slate-500 hover:text-slate-300 text-xs">← Artifacts</a>
		<h1 class="text-lg font-bold text-slate-100">
			{detail?.frontmatter.intent ?? id}
		</h1>
	</div>

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if !detail}
		<p class="text-red-400 text-sm">Not found.</p>
	{:else}
		<div class="card space-y-3">
			<div class="flex items-center gap-2 text-xs flex-wrap">
				<span class="badge {statusClass(status)}">{status}</span>
				<span class="badge bg-slate-700 text-slate-300 border border-slate-600">{detail.frontmatter.kind}</span>
				{#if detail.frontmatter.tags?.length}
					{#each detail.frontmatter.tags as tag}
						<span class="badge bg-slate-700 text-slate-400 border border-slate-600">{tag}</span>
					{/each}
				{/if}
			</div>

			<dl class="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-xs">
				<dt class="text-slate-500">ID</dt>
				<dd class="text-slate-300 font-mono truncate">{detail.frontmatter.id}</dd>
				<dt class="text-slate-500">Intent</dt>
				<dd class="text-slate-300">{detail.frontmatter.intent}</dd>
				<dt class="text-slate-500">Kind</dt>
				<dd class="text-slate-300">{detail.frontmatter.kind}</dd>
				<dt class="text-slate-500">Target</dt>
				<dd class="text-slate-300">{targetStr()}</dd>
				<dt class="text-slate-500">Status</dt>
				<dd class="text-slate-300"><span class="badge {statusClass(status)}">{status}</span></dd>
				<dt class="text-slate-500">Created</dt>
				<dd class="text-slate-300">{fmtDate(detail.frontmatter.created_at)}</dd>
				<dt class="text-slate-500">Created By</dt>
				<dd class="text-slate-300">{detail.frontmatter.created_by ?? '—'}</dd>
				{#if detail.frontmatter.version}
					<dt class="text-slate-500">Version</dt>
					<dd class="text-slate-300">{detail.frontmatter.version}</dd>
				{/if}
			</dl>
		</div>

		{#if hasActiveTask || activeTask}
			<div class="card flex items-center gap-2 text-xs text-amber-400">
				<svg class="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
					<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
					<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
				</svg>
				<span>Task {detail?.active_task?.action ?? activeTask?.action ?? ''} in progress ({detail?.active_task?.status ?? activeTask?.status ?? ''})…</span>
			</div>
		{/if}

		{#if getActions().length > 0}
			<div class="flex gap-2 flex-wrap">
				{#each getActions() as action}
					<button
						class="btn {action.cls} text-xs"
						disabled={working || hasActiveTask}
						on:click={() => handleAction(action)}
					>{action.label}</button>
				{/each}
			</div>
		{/if}

		{#if detail.body}
			<div class="card space-y-2">
				<h2 class="text-sm font-semibold text-slate-200">Body</h2>
				<pre class="bg-slate-900 border border-slate-700 rounded p-3 text-xs text-slate-300 overflow-x-auto whitespace-pre-wrap">{detail.body}</pre>
			</div>
		{/if}
	{/if}
</div>

{#if confirmAction}
	<div class="fixed inset-0 bg-black/60 z-20 flex items-center justify-center" on:click|self={() => (confirmAction = null)} role="presentation">
		<div class="bg-slate-800 border border-slate-700 rounded-lg p-5 max-w-sm space-y-4">
			<p class="text-sm text-slate-200">Confirm {confirmAction.label.toLowerCase()}?</p>
			<div class="flex gap-2 justify-end">
				<button class="btn btn-ghost text-xs" on:click={() => (confirmAction = null)}>Cancel</button>
				<button class="btn btn-danger text-xs" on:click={confirmDestructive}>{confirmAction.label}</button>
			</div>
		</div>
	</div>
{/if}