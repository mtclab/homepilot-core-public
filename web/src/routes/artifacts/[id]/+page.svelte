<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import { api, sessionStore, type ArtifactDetail, type Task } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import { isTerminalStatus } from '$lib/taskStatus';

	let detail: ArtifactDetail | null = null;
	let loading = true;
	let working = false;
	let activeTask: Task | null = null;
	let pollHandle: ReturnType<typeof setInterval> | null = null;
	let confirmAction: { fn: () => Promise<void>; label: string } | null = null;
	let confirmEl: HTMLDivElement | null = null;

	$: id = $page.params.id ?? '';
	// Approve/Reject/Apply/Revoke all 403 without write scope — don't offer them.
	// Default-deny while the session is still loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);
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
		if (!canWrite || hasActiveTask || working) return [];
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
				// Anything that is not pending/running is finished — an allow-list
				// here missed `cancelled` and polled a dead task forever, with the
				// banner up and every action button disabled until a page reload.
				if (isTerminalStatus(task.status)) {
					stopPolling();
					activeTask = null;
					if (task.status === 'failed') {
						notify('Task failed: ' + (task.error ?? 'unknown'), 'err');
					} else if (task.status === 'cancelled') {
						notify('Task cancelled', 'err');
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

	// The dialog guards a destructive path (Reject / Revoke), so it must behave
	// like a dialog: Escape cancels, and Tab cannot wander onto the page behind
	// it (where a stray Enter would hit an action button).
	function focusFirst(node: HTMLDivElement) {
		const first = node.querySelector<HTMLElement>('button');
		first?.focus();
		return {};
	}

	function modalKeydown(e: KeyboardEvent) {
		if (!confirmAction || !confirmEl) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			confirmAction = null;
			return;
		}
		if (e.key !== 'Tab') return;
		const focusable = Array.from(confirmEl.querySelectorAll<HTMLElement>('button'));
		if (focusable.length === 0) return;
		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		const active = document.activeElement as HTMLElement | null;
		if (e.shiftKey && (active === first || !confirmEl.contains(active))) {
			e.preventDefault();
			last.focus();
		} else if (!e.shiftKey && (active === last || !confirmEl.contains(active))) {
			e.preventDefault();
			first.focus();
		}
	}

	onMount(load);
	// Refresh this artifact when an event for IT arrives (drift, or a status
	// change made elsewhere). Other artifacts' events are ignored, and a burst is
	// coalesced into ONE refetch. The active-task poller above still drives
	// fine-grained apply/revoke progress.
	const refresh = debounce(() => load(false), 400);
	const unsub = onArtifactEvent((e) => {
		if (e.data?.id === id) refresh();
	});
	onDestroy(() => {
		stopPolling();
		unsub();
		refresh.cancel();
	});
</script>

<div class="space-y-5">
	<div class="flex items-center gap-3">
		<a href="{base}/artifacts" class="text-muted hover:text-ink text-xs">← Artifacts</a>
		<h1 class="page-title">
			{detail?.frontmatter.intent ?? id}
		</h1>
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if !detail}
		<p class="text-danger text-sm">Not found.</p>
	{:else}
		<div class="card space-y-3">
			<div class="flex items-center gap-2 text-xs flex-wrap">
				<span class="badge {statusClass(status)}">{status}</span>
				<span class="badge bg-raised text-ink border border-border-strong">{detail.frontmatter.kind}</span>
				{#if detail.frontmatter.tags?.length}
					{#each detail.frontmatter.tags as tag}
						<span class="badge bg-raised text-muted border border-border-strong">{tag}</span>
					{/each}
				{/if}
			</div>

			<dl class="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-xs">
				<dt class="text-muted">ID</dt>
				<dd class="text-ink font-mono truncate">{detail.frontmatter.id}</dd>
				<dt class="text-muted">Intent</dt>
				<dd class="text-ink">{detail.frontmatter.intent}</dd>
				<dt class="text-muted">Kind</dt>
				<dd class="text-ink">{detail.frontmatter.kind}</dd>
				<dt class="text-muted">Target</dt>
				<dd class="text-ink">{targetStr()}</dd>
				<dt class="text-muted">Status</dt>
				<dd class="text-ink"><span class="badge {statusClass(status)}">{status}</span></dd>
				<dt class="text-muted">Created</dt>
				<dd class="text-ink">{fmtDate(detail.frontmatter.created_at)}</dd>
				<dt class="text-muted">Created By</dt>
				<dd class="text-ink">{detail.frontmatter.created_by ?? '—'}</dd>
				{#if detail.frontmatter.version}
					<dt class="text-muted">Version</dt>
					<dd class="text-ink">{detail.frontmatter.version}</dd>
				{/if}
			</dl>
		</div>

		{#if hasActiveTask || activeTask}
			<div class="card flex items-center gap-2 text-xs text-warn">
				<svg class="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
					<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
					<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
				</svg>
				<span>Task {detail?.active_task?.action ?? activeTask?.action ?? ''} in progress ({detail?.active_task?.status ?? activeTask?.status ?? ''})…</span>
			</div>
		{/if}

		{#if canWrite && getActions().length > 0}
			<div class="flex gap-2 flex-wrap">
				{#each getActions() as action}
					<button
						class="btn {action.cls} text-xs"
						disabled={working || hasActiveTask}
						on:click={() => handleAction(action)}
					>{action.label}</button>
				{/each}
			</div>
		{:else if !canWrite}
			<p class="prose-note text-xs">
				Read-only session — approving, applying and revoking need a write-scope token.
			</p>
		{/if}

		{#if detail.body}
			<div class="card space-y-2">
				<h2 class="section-title">Body</h2>
				<pre class="code-block text-xs overflow-x-auto whitespace-pre-wrap">{detail.body}</pre>
			</div>
		{/if}
	{/if}
</div>

<!-- Top level by necessity (a <svelte:window> may not sit inside a block); the
     handler no-ops while no dialog is open. -->
<svelte:window on:keydown={modalKeydown} />

{#if confirmAction}
	<div class="fixed inset-0 bg-black/60 z-20 flex items-center justify-center" on:click|self={() => (confirmAction = null)} role="presentation">
		<div
			class="bg-surface border border-border rounded-lg p-5 max-w-sm space-y-4"
			bind:this={confirmEl}
			use:focusFirst
			role="dialog"
			aria-modal="true"
			aria-labelledby="confirm-title"
		>
			<p id="confirm-title" class="text-sm text-ink">Confirm {confirmAction.label.toLowerCase()}?</p>
			<div class="flex gap-2 justify-end">
				<button class="btn btn-ghost text-xs" on:click={() => (confirmAction = null)}>Cancel</button>
				<button class="btn btn-danger text-xs" on:click={confirmDestructive}>{confirmAction.label}</button>
			</div>
		</div>
	</div>
{/if}