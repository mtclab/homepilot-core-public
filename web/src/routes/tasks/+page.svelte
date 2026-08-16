<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { api, type Task } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import { taskStatusClass, isCancellable, shortTaskId } from '$lib/taskStatus';
	import { onArtifactEvent } from '$lib/events';

	let tasks: Task[] = [];
	let total = 0;
	let loading = true;
	let loadError = '';
	let cancellingId = '';
	const LIMIT = 200;

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
			!window.confirm(`Cancel ${task.action} for ${task.artifact_id}?`)) return;
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
	onMount(() => {
		load(true);
		// SSE is the primary trigger: apply/revoke queue and completion fire
		// artifact lifecycle events, so refresh the list on each. The interval is
		// only a slow safety net (covers the pending→running interim, which emits
		// no event, and the case where the stream is down).
		unsub = onArtifactEvent(() => load(false));
		poll = setInterval(() => load(false), 15000);
	});
	onDestroy(() => {
		if (poll) clearInterval(poll);
		unsub();
	});
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-lg font-bold text-slate-100">Tasks</h1>
			<p class="text-xs text-slate-500">
				Every apply / revoke across all artifacts, newest first.
				{#if activeCount}<span class="text-amber-400">{activeCount} in flight.</span>{/if}
			</p>
		</div>
		<button class="btn btn-ghost text-xs" on:click={() => load(true)}>↻ Refresh</button>
	</div>

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-red-400">Could not load tasks.</p>
			<p class="text-xs text-slate-500">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={() => load(true)}>↻ Retry</button>
		</div>
	{:else if tasks.length === 0}
		<div class="card p-6 text-center space-y-1">
			<p class="text-slate-400 text-sm">No tasks yet.</p>
			<p class="text-xs text-slate-500">
				Applying or revoking an artifact starts a task — it will appear here.
			</p>
		</div>
	{:else}
		<div class="card overflow-x-auto">
			<table class="w-full text-xs">
				<thead>
					<tr class="text-slate-400 border-b border-slate-700">
						<th class="text-left pb-2 pr-4">Task</th>
						<th class="text-left pb-2 pr-4">Artifact</th>
						<th class="text-left pb-2 pr-4">Action</th>
						<th class="text-left pb-2 pr-4">Status</th>
						<th class="text-left pb-2 pr-4">Started</th>
						<th class="text-left pb-2 pr-4">Finished</th>
						<th class="text-left pb-2 pr-4">Detail</th>
						<th class="text-left pb-2"></th>
					</tr>
				</thead>
				<tbody>
					{#each tasks as t (t.id)}
						<tr class="border-b border-slate-700/40 align-top">
							<td class="py-2 pr-4 font-mono text-slate-400" title={t.id}>{shortTaskId(t.id)}</td>
							<td class="py-2 pr-4">
								<a
									href="{base}/artifacts/{t.artifact_id}"
									class="text-sky-400 hover:text-sky-300 font-mono"
								>{t.artifact_id}</a>
							</td>
							<td class="py-2 pr-4 text-slate-300">{t.action}</td>
							<td class="py-2 pr-4">
								<span class="badge {taskStatusClass(t.status)}">
									{t.status}
									{#if t.status === 'running'}<span class="animate-pulse">…</span>{/if}
								</span>
							</td>
							<td class="py-2 pr-4 text-slate-500 whitespace-nowrap">{fmtTs(t.created_at)}</td>
							<td class="py-2 pr-4 text-slate-500 whitespace-nowrap">{fmtTs(t.finished_at)}</td>
							<td class="py-2 pr-4 max-w-xs">
								{#if t.status === 'failed' && t.error}
									<span class="text-red-400 break-words">{t.error}</span>
								{:else}
									<span class="text-slate-600">—</span>
								{/if}
							</td>
							<td class="py-2">
								{#if isCancellable(t.status)}
									<button
										class="btn btn-danger text-xs px-2 py-0.5"
										disabled={cancellingId === t.id}
										on:click={() => cancel(t)}
									>{cancellingId === t.id ? 'Cancelling…' : 'Cancel'}</button>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		{#if total > tasks.length}
			<p class="text-xs text-slate-500">Showing newest {tasks.length} of {total}.</p>
		{/if}
	{/if}
</div>
