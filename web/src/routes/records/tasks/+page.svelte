<script lang="ts">
	// Tasks — grouped chronology, attention first (#549 F5).
	//
	// This was one unbounded newest-first table of eight equally-weighted
	// columns. Two things were wrong with it: a running or failed task -- the
	// only rows that need an operator NOW -- sank out of sight as soon as newer
	// tasks landed on top of it, and every row painted its id, artifact, action
	// and status at the same weight, so there was nothing to scan by.
	//
	// So: unfinished and failed tasks are PINNED into one attention group
	// regardless of when they ran, everything else is grouped by the day it
	// happened, and each row leads with the one thing it is about (action +
	// target + state chip, the F1 col-primary pattern) with times and detail as
	// supporting columns.
	import { onMount, onDestroy } from 'svelte';
	import { api, sessionStore, type Task } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import { taskStatusClass, isCancellable, shortTaskId } from '$lib/taskStatus';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import { groupByDay, partition } from '$lib/grouping';

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

	/**
	 * What the task acted ON, for the primary column. An artifactless task
	 * (provision / install_agent) names the guest it created instead, which
	 * only exists inside result_json.
	 */
	function targetLabel(t: Task): string {
		if (t.artifact_id) return t.artifact_id;
		if (!t.result_json) return '';
		try {
			const r = JSON.parse(t.result_json) as {
				vmid?: number;
				name?: string;
				hostname?: string;
			};
			return r.name ?? r.hostname ?? (r.vmid === undefined ? '' : `vmid ${r.vmid}`);
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

	/**
	 * A task an operator may still have to do something about: still in flight
	 * (it may be stuck) or failed (it needs diagnosing). Defined as a predicate
	 * over the status rather than a list of ids so a new in-flight state is
	 * pinned by default instead of quietly sinking into the chronology.
	 */
	function needsAttention(t: Task): boolean {
		return isCancellable(t.status) || t.status === 'failed';
	}

	// The pinned group ignores the day entirely: a failure from Tuesday is still
	// a failure today. Everything else keeps its place in the chronology. Both
	// kinds of group render through one loop so a fix to a row is one edit.
	$: [pinned, settled] = partition(tasks, needsAttention);
	$: groups = [
		...(pinned.length
			? [{ key: 'attention', label: 'Needs attention', items: pinned }]
			: []),
		...groupByDay(settled, (t) => t.created_at).map((g) => ({
			key: g.key || 'undated',
			label: g.label,
			items: g.items,
		})),
	];
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

<div class="page-stack">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="page-title">Tasks</h1>
			<p class="prose-note prose-measure text-xs">
				Every apply / revoke / provision / agent install, grouped by the day it ran.
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
		{#each groups as group (group.key)}
			<section class="section-stack" aria-labelledby="task-group-{group.key}">
				<h2 class="section-title" id="task-group-{group.key}">
					{group.label} <span class="text-muted font-normal num-inline">({group.items.length})</span>
				</h2>
				<div class="card overflow-x-auto">
					<table class="data-table text-xs">
						<thead>
							<tr>
								<th class="col-primary">Task</th>
								<th class="col-secondary">Started</th>
								<th class="col-secondary">Finished</th>
								<th class="col-secondary">Detail</th>
								<th><span class="sr-only">Row actions</span></th>
							</tr>
						</thead>
						<tbody>
							{#each group.items as t (t.id)}
								<tr class="border-b border-divider">
									<td class="col-primary">
										<span class="col-primary-inner">
											<span>{t.action}</span>
											{#if t.artifact_id}
												<a
													href="{base}/changes/{t.artifact_id}"
													class="text-accent hover:text-accent-strong font-mono"
												>{t.artifact_id}</a>
											{:else if targetLabel(t)}
												<span class="font-mono text-muted">{targetLabel(t)}</span>
											{/if}
											<span class="badge {taskStatusClass(t.status)}">
												{t.status}
												{#if t.status === 'running'}<span class="animate-pulse">…</span>{/if}
											</span>
										</span>
										<span class="block font-mono text-muted" title={t.id}>{shortTaskId(t.id)}</span>
									</td>
									<td class="col-secondary whitespace-nowrap">{fmtTs(t.created_at)}</td>
									<td class="col-secondary whitespace-nowrap">{fmtTs(t.finished_at)}</td>
									<td class="col-secondary max-w-xs">
										{#if t.status === 'failed' && t.error}
											<span class="text-danger break-words">{t.error}</span>
										{:else if resultSummary(t)}
											<span class="break-words">{resultSummary(t)}</span>
										{:else}
											—
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
										<td colspan="5">
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
			</section>
		{/each}

		{#if total > tasks.length}
			<p class="prose-note text-xs">Showing newest {tasks.length} of {total}.</p>
		{/if}
	{/if}
</div>
