<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import { api, sessionStore, type ArtifactDetail, type Task } from '$lib/api';
	import { canWrite as capCanWrite, isAdmin } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import ApprovalCodePanel from '$lib/components/ApprovalCodePanel.svelte';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import { isTerminalStatus } from '$lib/taskStatus';
	import Icon from '$lib/components/Icon.svelte';
	import ChangeOutcome from '$lib/components/ChangeOutcome.svelte';
	import ChangeSummary from '$lib/components/ChangeSummary.svelte';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import { liveConfirm, runningLine, type LiveConfirm } from '$lib/changes';

	let detail: ArtifactDetail | null = null;
	let loading = true;
	let working = false;
	let activeTask: Task | null = null;
	let pollHandle: ReturnType<typeof setInterval> | null = null;
	// A poll that throws used to stop the timer and say nothing, leaving the
	// spinner claiming "in progress" and every action disabled with nothing left
	// running to ever correct it - the same dead end the `cancelled` allow-list
	// caused below, reached by a different road (#648 tranche 7). One blip is not
	// a lost task, so tolerate a couple; past that, SAY the outcome is unknown.
	let pollFailures = 0;
	let pollLost = false;
	const POLL_MAX_FAILURES = 3;
	type Action = {
		label: string;
		cls: string;
		fn: () => Promise<void>;
		/** Asks first (UI_PLAN 5.3); `typed` asks for the target's name back. */
		destructive?: boolean;
		typed?: boolean;
		consequence?: string;
	};
	let confirmAction: Action | null = null;
	// For a typed confirm: the guest as read live when the dialog opened (#813).
	let confirmLive: LiveConfirm | null = null;

	$: id = $page.params.id ?? '';
	// Approve/Reject/Apply/Revoke all 403 without write scope — don't offer them.
	// Default-deny while the session is still loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	// Approving needs an operator (admin) session (#681).
	$: canApprove = isAdmin($sessionStore?.capabilities);
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
		withdrawn: 'badge-revoked',
	};
	function statusClass(s: string): string { return STATUS_CLASSES[s] ?? 'badge-proposed'; }

	function fmtDate(s: string | undefined): string {
		return s ? new Date(s).toLocaleString() : '—';
	}

	function targetStr(): string {
		const t = detail?.frontmatter.target ?? {};
		return t.host ?? t.service ?? t.node ?? '—';
	}

	// A destroy or a forced stop asks for the target's name typed back before
	// it runs (UI_PLAN 3.10); the words come from the server's summary.
	function runGuard(): Pick<Action, 'destructive' | 'typed' | 'consequence'> {
		const danger = detail?.summary?.danger;
		if (!danger) return {};
		return {
			destructive: true,
			typed: true,
			consequence:
				(detail?.summary?.steps.join('. ') ?? '') +
				(danger === 'destroy'
					? '. This cannot be undone: the guest and its disks are deleted.'
					: '. Anything unsaved in the guest is lost.'),
		};
	}

	function getActions(): Action[] {
		if (!canWrite || hasActiveTask || working) return [];
		const summary = detail?.summary;
		switch (status) {
			case 'proposed':
				return [
					// One action from proposal to applied (owner decision 5); a
					// composite or replay_safe:false change keeps two steps. An
					// older backend sends no summary: plain Approve, as before.
					...(canApprove && summary && !summary.two_step
						? [
								{
									label: 'Approve and apply',
									cls: 'btn-primary',
									fn: doApproveAndApply,
									...runGuard(),
								},
								{ label: 'Approve only', cls: 'btn-ghost', fn: doApprove },
							]
						: canApprove
							? [{ label: 'Approve', cls: 'btn-success', fn: doApprove }]
							: []),
					{ label: 'Reject', cls: 'btn-danger', fn: doReject, destructive: true },
				];
			case 'approved':
				// Ready to run: Apply, or take the approval back (#797).
				return [
					{ label: 'Apply', cls: 'btn-primary', fn: doApply, ...runGuard() },
					{
						label: 'Withdraw',
						cls: 'btn-ghost',
						fn: doWithdraw,
						destructive: true,
						consequence: 'It will never run. Nothing on any host is touched.',
					},
				];
			case 'failed':
				return [
					...(canApprove ? [{ label: 'Re-approve', cls: 'btn-success', fn: doApprove }] : []),
					{ label: 'Revoke', cls: 'btn-danger', fn: doRevoke, destructive: true },
				];
			case 'applied':
				const actions: Action[] = [
					{ label: 'Revoke', cls: 'btn-danger', fn: doRevoke, destructive: true },
				];
				if (detail?.frontmatter?.replay_safe !== false) {
					actions.push({ label: 'Replay', cls: 'btn-success', fn: doReplay });
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
			// A read that got through re-establishes the truth the lost poll left
			// unknown, so the banner stops saying it is lost.
			pollLost = false;
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

	async function doResetApprovalCode() {
		working = true;
		try {
			await api.resetApprovalCode(id);
			notify('Approval lock cleared');
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

	async function doWithdraw() {
		working = true;
		try {
			await api.withdrawArtifact(id);
			notify('Withdrawn - it will not run');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = false;
		}
	}

	async function doApproveAndApply() {
		working = true;
		try {
			const res = await api.approveAndApplyArtifact(id);
			notify('Approved and started');
			startPolling(res.task_id);
			await load();
		} catch (e) {
			// Approved but not started is still Ready to run: re-read so the page
			// says so instead of the old proposed state.
			notify(String(e), 'err');
			await load(false);
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

	async function doReplay() {
		working = true;
		try {
			const res = await api.replayArtifact(id);
			notify('Replay queued');
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
		pollFailures = 0;
		pollLost = false;
		pollHandle = setInterval(async () => {
			try {
				const task = await api.getTask(taskId);
				pollFailures = 0;
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
					} else if (task.status === 'unknown') {
						// Not a failure and not a success (#774): PVE is still
						// working. Opening the task again settles it.
						notify('Outcome unknown: ' + (task.error ?? 'the PVE task is still running'), 'err');
					} else {
						notify('Task completed');
					}
					await load();
				}
			} catch {
				pollFailures += 1;
				if (pollFailures >= POLL_MAX_FAILURES) {
					stopPolling();
					activeTask = null;
					pollLost = true;
					notify('Lost contact while the task was running — its outcome is unknown here', 'err');
					// One more read, in case the backend is back and the task is done.
					await load(false);
				}
			}
		}, 2000);
	}

	function stopPolling() {
		if (pollHandle) {
			clearInterval(pollHandle);
			pollHandle = null;
		}
	}

	async function handleAction(action: Action) {
		if (action.typed) {
			// The name typed back is the guest's name AS READ NOW, never its
			// VMID; a machine that is not the recorded one is not offered (#813).
			confirmLive = liveConfirm(await api.getArtifactTarget(id).catch(() => null));
			confirmAction = action;
		} else if (action.destructive) {
			confirmLive = null;
			confirmAction = action;
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
	// change made elsewhere). Other artifacts' events are ignored, and a burst is
	// coalesced into ONE refetch. The active-task poller above still drives
	// fine-grained apply/revoke progress.
	const refresh = debounce(() => load(false), 400);
	const unsub = onArtifactEvent((e) => {
		if (e.type === 'resync' || e.data?.id === id) refresh();
	});
	onDestroy(() => {
		stopPolling();
		unsub();
		refresh.cancel();
	});
</script>

<div class="space-y-5">
	<div class="flex items-center gap-3">
		<a href="{base}/changes" class="text-muted hover:text-ink text-xs"><Icon name="arrow-left" />Changes</a>
		<h1 class="page-title">
			{detail?.frontmatter.intent ?? id}
		</h1>
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if !detail}
		<p class="text-danger text-sm">Not found.</p>
	{:else}
		<!-- The change in plain words, first (owner feedback 2026-11-08): what
		     it does, to what, where it stands and how its last run ended. The raw
		     spec is further down, behind a disclosure. -->
		<div class="card space-y-3" data-testid="change-top">
			<ChangeOutcome
				fm={detail.frontmatter}
				activeTask={detail.active_task}
				lastTask={detail.last_task}
			/>
			{#if status === 'proposed' && detail.frontmatter.returned_for_review?.reason}
				<p class="text-xs text-warn" data-testid="returned-for-review">
					Back for review: {detail.frontmatter.returned_for_review.reason}.
				</p>
			{/if}
			<ChangeSummary summary={detail.summary} />
		</div>

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

		{#if pollLost}
			<div class="card flex items-center gap-2 text-xs text-danger">
				<span
					>Lost contact while the task was running — this page cannot say how it ended.</span
				>
				<button class="btn btn-ghost text-xs" on:click={() => load()} disabled={loading}
					>Retry</button
				>
			</div>
		{:else if hasActiveTask || activeTask}
			<div class="card flex items-center gap-2 text-xs text-warn">
				<svg class="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
					<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
					<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
				</svg>
				<span data-testid="running-banner"
					>{runningLine(
						detail.frontmatter.intent,
						detail.active_task?.created_at ?? activeTask?.created_at,
					)} · {detail.active_task?.action ?? activeTask?.action ?? ''}
					{detail.active_task?.status ?? activeTask?.status ?? ''}</span
				>
			</div>
		{/if}

		{#if status === 'proposed'}
			<ApprovalCodePanel
				code={detail.approval_code}
				locked={detail.approval_locked ?? false}
				{canWrite}
				busy={working}
				onClear={doResetApprovalCode}
			/>
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
				<h2 class="section-title">Spec and execution log</h2>
				<ChangeSummary summary={undefined} body={detail.body} />
			</div>
		{/if}
	{/if}
</div>

{#if confirmAction}
	{@const action = confirmAction}
	<ConfirmDialog
		title="{action.label} {detail?.summary?.target ?? targetStr()}"
		consequence={action.consequence ?? ''}
		confirmName={action.typed ? (confirmLive?.name ?? null) : null}
		blocked={action.typed ? (confirmLive?.blocked ?? null) : null}
		confirmLabel={action.label}
		on:confirm={confirmDestructive}
		on:cancel={() => (confirmAction = null)}
	>
		{#if action.typed && confirmLive?.name}
			<p class="text-xs text-muted" data-testid="confirm-live-state">
				At VMID {confirmLive.vmid} now: <span class="font-mono text-ink">{confirmLive.name}</span>, {confirmLive.state}
			</p>
		{/if}
	</ConfirmDialog>
{/if}
