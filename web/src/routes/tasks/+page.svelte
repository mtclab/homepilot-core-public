<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { api, sessionStore, type Task } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import { taskStatusClass, isCancellable, shortTaskId } from '$lib/taskStatus';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';

	let tasks: Task[] = [];
	let total = 0;
	let loading = true;
	let loadError = '';
	let cancellingId = '';
	const LIMIT = 200;
	// Cancelling a task is write-scoped server-side. Default-deny while loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);

	// Silent refresh (poll / manual Refresh) must not flip the whole view back to
	// the loading skeleton — only the very first load does.
	async function load(initial = false) {
		if (initial) loading = true;
		loadError = '';
		try {
			const res = await api.listTasks(undefined, LIMIT, 0);
			tasks = res.items;
			total = res.total;
		} catch (e) {
			// On a poll failure keep the last-good rows; only surface the error card
			// when we have nothing to show.
			const msg = e instanceof Error ? e.message : String(e);
			if (initial || tasks.length === 0) loadError = msg;
			else notify(msg, 'err');
		} finally {
			loading = false;
		}
	}

	async function cancel(task: Task) {
		if (typeof window !== 'undefined' &&
			!window.confirm(`Cancel ${task.action} for ${task.artifact_id ?? shortTaskId(task.id)}?`)) return;
		cancellingId = task.id;
		try {
			const updated = await api.cancelTask(task.id);
			notify(
				updated.status === 'cancelled'
					? 'Task cancelled'
					: `Task already ${updated.status}`,
				'ok',
			);
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			cancellingId = '';
		}
	}

	// Which row has its execution log open. One at a time: these are long, and
	// the point is reading one carefully, not scanning many.
	let openLog: string | null = null;

	/** The execution output for a task, or '' when it kept none (#445 A3). */
	function executionLog(t: Task): string {
		if (!t.result_json) return '';
		try {
			const r = JSON.parse(t.result_json) as { execution_log?: string };
			return r.execution_log ?? '';
		} catch {
			return '';
		}
	}

	// A provision or install_agent task has no artifact to link to; its identity
	// is the guest it acted on, which only exists in result_json.
	function resultSummary(t: Task): string {
		if (!t.result_json) return '';
		try {
			const r = JSON.parse(t.result_json) as {
				vmid?: number;
				name?: string;
				hostname?: string;
				ip?: string | null;
				agent_id?: string;
			};
			const host = r.name ?? r.hostname;
			if (r.vmid === undefined && !host) return '';
			return [
				host,
				r.vmid === undefined ? '' : `vmid ${r.vmid}`,
				r.ip ?? '',
				r.agent_id ? `agent ${r.agent_id.slice(0, 12)}…` : '',
			]
				.filter(Boolean)
				.join(' · ');
		} catch {
			return '';
		}
	}

	function fmtTs(s: string | null): string {
		if (!s) return '—';
		try {
			return new Date(s).toLocaleString();
		} catch {
			return s;
		}
	}

	$: activeCount = tasks.filter((t) => isCancellable(t.status)).length;

	let poll: ReturnType<typeof setInterval> | undefined;
	let unsub: () => void = () => {};
	// One refetch per burst of events, not one per event.
	const refresh = debounce(() => load(false), 400);
	onMount(() => {
		load(true);
		// SSE is the primary trigger: apply/revoke queue and completion fire
		// artifact lifecycle events, so refresh the list on each. The interval is
		// only a slow safety net (covers the pending→running interim, which emits
		// no event, and the case where the stream is down).
		unsub = onArtifactEvent(refresh);
		poll = setInterval(() => load(false), 15000);
	});
	onDestroy(() => {
		if (poll) clearInterval(poll);
		unsub();
		refresh.cancel();
	});
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="page-title">Tasks</h1>
			<p class="prose-note text-xs">
				Every apply / revoke / provision / agent install, newest first.
				{#if activeCount}<span class="text-warn">{activeCount} in flight.</span>{/if}
			</p>
		</div>
		<button class="btn btn-ghost text-xs" on:click={() => load(true)}>↻ Refresh</button>
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load tasks.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={() => load(true)}>↻ Retry</button>
		</div>
	{:else if tasks.length === 0}
		<div class="card p-6 text-center space-y-1">
			<p class="prose-note text-sm">No tasks yet.</p>
			<p class="prose-note text-xs">
				Applying or revoking an artifact starts a task — it will appear here.
			</p>
		</div>
	{:else}
		<div class="card overflow-x-auto">
			<table class="data-table text-xs">
				<thead>
					<tr>
						<th class="text-left">Task</th>
						<th class="text-left">Artifact</th>
						<th class="text-left">Action</th>
						<th class="text-left">Status</th>
						<th class="text-left">Started</th>
						<th class="text-left">Finished</th>
						<th class="text-left">Detail</th>
						<th class="text-left"></th>
					</tr>
				</thead>
				<tbody>
					{#each tasks as t (t.id)}
						<tr class="border-b border-divider align-top">
							<td class="font-mono text-muted" title={t.id}>{shortTaskId(t.id)}</td>
							<td>
								{#if t.artifact_id}
									<a
										href="{base}/artifacts/{t.artifact_id}"
										class="text-accent hover:text-accent-strong font-mono"
									>{t.artifact_id}</a>
								{:else}
									<span class="text-muted">—</span>
								{/if}
							</td>
							<td class="text-ink">{t.action}</td>
							<td>
								<span class="badge {taskStatusClass(t.status)}">
									{t.status}
									{#if t.status === 'running'}<span class="animate-pulse">…</span>{/if}
								</span>
							</td>
							<td class="text-muted whitespace-nowrap">{fmtTs(t.created_at)}</td>
							<td class="text-muted whitespace-nowrap">{fmtTs(t.finished_at)}</td>
							<td class="max-w-xs">
								{#if t.status === 'failed' && t.error}
									<span class="text-danger break-words">{t.error}</span>
								{:else if resultSummary(t)}
									<span class="text-ink break-words">{resultSummary(t)}</span>
								{:else}
									<span class="text-muted">—</span>
								{/if}
							</td>
							<td class="whitespace-nowrap">
								{#if executionLog(t)}
									<button
										class="text-xs text-accent hover:text-accent-strong mr-2"
										aria-expanded={openLog === t.id}
										on:click={() => (openLog = openLog === t.id ? null : t.id)}
									>{openLog === t.id ? 'Hide log' : 'Log'}</button>
								{/if}
								{#if canWrite && isCancellable(t.status)}
									<button
										class="btn btn-danger text-xs px-2 py-0.5"
										disabled={cancellingId === t.id}
										on:click={() => cancel(t)}
									>{cancellingId === t.id ? 'Cancelling…' : 'Cancel'}</button>
								{/if}
							</td>
						</tr>
						{#if openLog === t.id}
							<!-- What actually happened on the host. The executor has always
							     produced this; until #445 A3 the runner discarded it, so a
							     failed apply left only a one-line error to diagnose from. -->
							<tr class="border-b border-divider">
								<td colspan="8">
									<pre
										class="code-block text-xs overflow-x-auto whitespace-pre-wrap max-h-80"
									>{executionLog(t)}</pre>
								</td>
							</tr>
						{/if}
					{/each}
				</tbody>
			</table>
		</div>
		{#if total > tasks.length}
			<p class="prose-note text-xs">Showing newest {tasks.length} of {total}.</p>
		{/if}
	{/if}
</div>
