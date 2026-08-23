<script lang="ts">
	import { onMount } from 'svelte';
	import { api, sessionStore, type Artifact, type ArtifactPlan } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { pruneSelection } from '$lib/selection';
	import { base } from '$app/paths';

	let items: Artifact[] = [];
	let loading = true;
	let loadError = '';
	// Approve/Reject 403 without write scope. Default-deny while loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	let working: Record<string, boolean> = {};
	let expanded: string | null = null;
	let bodies: Record<string, string> = {};
	let rejectReasons: Record<string, string> = {};
	let plans: Record<string, ArtifactPlan> = {};
	let planErrors: Record<string, string> = {};
	let planLoading: Record<string, boolean> = {};

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

	async function load() {
		loading = true;
		loadError = '';
		try {
			const res = await api.listArtifacts({ status: 'proposed', limit: 200 });
			items = res.items;
			selected = pruneSelection(selected, items);
		} catch (e) {
			// A toast alone left the last-good (or empty) list on screen with no way
			// back — the queue looked empty when it had simply failed to load.
			const msg = e instanceof Error ? e.message : String(e);
			if (items.length === 0) loadError = msg;
			else notify(msg, 'err');
		} finally {
			loading = false;
		}
	}

	async function expand(id: string) {
		if (expanded === id) { expanded = null; return; }
		expanded = id;
		if (!bodies[id]) {
			try {
				const res = await api.getArtifact(id);
				bodies[id] = res.body;
			} catch {
				bodies[id] = '(failed to load body)';
			}
		}
		loadPlan(id);
	}

	// What applying this would do to the HOST (#445 A1). Approval used to show
	// only the artifact text, so the decision was made blind. Safe to run on
	// expand: the endpoint is read-only by construction.
	async function loadPlan(id: string) {
		if (plans[id] || planErrors[id] || planLoading[id]) return;
		planLoading = { ...planLoading, [id]: true };
		try {
			plans = { ...plans, [id]: await api.planArtifact(id) };
		} catch (e) {
			// Shown inline, not as a toast: "we could not tell you what this will
			// do" has to stay on screen next to the Approve button.
			planErrors = { ...planErrors, [id]: e instanceof Error ? e.message : String(e) };
		} finally {
			planLoading = { ...planLoading, [id]: false };
		}
	}

	// Selection for bulk actions. Pruned on every reload so "3 selected -> Approve"
	// can never act on rows the operator can no longer see - the same rule the
	// inventory table follows.
	let selected: Set<string> = new Set();
	let bulkConfirm: 'approve' | 'reject' | null = null;
	let bulkRunning = false;

	function toggle(id: string) {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	function toggleAll() {
		selected = selected.size === items.length ? new Set() : new Set(items.map((a) => a.id));
	}

	/** Approve or reject everything selected, reporting per-artifact outcomes. */
	async function runBulk(action: 'approve' | 'reject') {
		bulkRunning = true;
		const ids = items.filter((a) => selected.has(a.id)).map((a) => a.id);
		let done = 0;
		const failed: string[] = [];
		for (const id of ids) {
			try {
				if (action === 'approve') await api.approveArtifact(id);
				else await api.rejectArtifact(id, 'web', (rejectReasons[id] ?? '').trim() || undefined);
				done += 1;
				items = items.filter((a) => a.id !== id);
			} catch {
				// One refusal must not abandon the rest of the batch, and the
				// operator has to be told WHICH ones did not go through.
				failed.push(id.slice(-8));
			}
		}
		selected = new Set();
		bulkConfirm = null;
		bulkRunning = false;
		notify(
			failed.length
				? `${action === 'approve' ? 'Approved' : 'Rejected'} ${done}; failed: ${failed.join(', ')}`
				: `${action === 'approve' ? 'Approved' : 'Rejected'} ${done}`,
			failed.length ? 'err' : 'ok'
		);
	}

	async function approve(id: string) {
		working = { ...working, [id]: true };
		try {
			await api.approveArtifact(id);
			notify(`Approved ${id.slice(-8)}`);
			items = items.filter((a) => a.id !== id);
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = { ...working, [id]: false };
		}
	}

	async function reject(id: string) {
		const reason = (rejectReasons[id] ?? '').trim() || undefined;
		working = { ...working, [id]: true };
		try {
			await api.rejectArtifact(id, 'web', reason);
			notify(`Rejected ${id.slice(-8)}`);
			items = items.filter((a) => a.id !== id);
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = { ...working, [id]: false };
			delete rejectReasons[id];
		}
	}

	function targetStr(a: Artifact): string {
		const t = a.target ?? {};
		return t.host ?? t.service ?? t.node ?? '—';
	}

	onMount(load);
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Review Queue</h1>
		<div class="flex items-center gap-3">
			{#if !canWrite && !loading && !loadError}
				<span class="text-muted text-xs">Read-only session — approving and rejecting need a write-scope token.</span>
			{/if}
			<span class="text-muted text-xs">{items.length} proposed</span>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Refresh</button>
		</div>
	</div>

	{#if canWrite && items.length > 0}
		<!-- Bulk approve/reject (#435). Inventory has had checkboxes and bulk
		     actions for a while; the review queue - the page where a backlog
		     actually piles up - had none, and every decision was one mouse round
		     trip. The confirm is inline and two-step, the same as every other
		     destructive action here: a native confirm() in one place and nothing
		     in another was the inconsistency the issue names. -->
		<div class="flex items-center gap-3 flex-wrap">
			<label class="flex items-center gap-2 text-xs text-muted">
				<input
					type="checkbox"
					checked={selected.size > 0 && selected.size === items.length}
					on:change={toggleAll}
				/>
				Select all
			</label>
			{#if selected.size > 0}
				<span class="text-xs text-ink">{selected.size} selected</span>
				{#if bulkConfirm}
					<span class="text-xs text-warn">
						{bulkConfirm === 'approve' ? 'Approve' : 'Reject'} {selected.size} artifact{selected.size ===
						1
							? ''
							: 's'}?
					</span>
					<button
						class="btn text-xs {bulkConfirm === 'approve' ? 'btn-success' : 'btn-danger'}"
						disabled={bulkRunning}
						on:click={() => runBulk(bulkConfirm === 'approve' ? 'approve' : 'reject')}
					>{bulkRunning ? 'Working…' : 'Confirm'}</button>
					<button class="btn btn-ghost text-xs" on:click={() => (bulkConfirm = null)}>Cancel</button>
				{:else}
					<button class="btn btn-success text-xs" on:click={() => (bulkConfirm = 'approve')}
						>✓ Approve selected</button
					>
					<button class="btn btn-danger text-xs" on:click={() => (bulkConfirm = 'reject')}
						>✗ Reject selected</button
					>
				{/if}
			{/if}
		</div>
	{/if}

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load the review queue.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Retry</button>
		</div>
	{:else if items.length === 0}
		<div class="card text-center py-10">
			<p class="prose-note text-sm">Queue empty — nothing to review.</p>
		</div>
	{:else}
		<div class="space-y-3">
			{#each items as a}
				<div class="card space-y-3">
					<!-- Header row -->
					<div class="flex items-start gap-3">
						<button
							class="text-muted hover:text-ink-strong text-xs mt-0.5 w-4 shrink-0"
							on:click={() => expand(a.id)}
						>
							{expanded === a.id ? '▼' : '▶'}
						</button>
						<div class="flex-1 min-w-0">
							<a
								href="{base}/changes/{a.id}"
								class="text-xs text-accent hover:text-accent-strong font-mono"
								title="Open artifact detail"
							>{a.id}</a>
							<p class="text-sm text-ink-strong font-medium mt-0.5 truncate">{a.intent}</p>
							<div class="flex gap-2 mt-1 flex-wrap text-xs text-muted">
								<span>{a.kind}</span>
								<span>·</span>
								<span>{targetStr(a)}</span>
								{#if a.tags?.length}
									<span>·</span>
									{#each a.tags as tag}
										<span class="text-muted">{tag}</span>
									{/each}
								{/if}
							</div>
						</div>
						<!-- Actions -->
						{#if canWrite}
							<label class="shrink-0 self-start pt-1">
								<span class="sr-only">Select {a.id}</span>
								<input
									type="checkbox"
									checked={selected.has(a.id)}
									on:change={() => toggle(a.id)}
								/>
							</label>
							<div class="flex gap-2 shrink-0">
								<button
									class="btn btn-success text-xs"
									disabled={working[a.id]}
									on:click={() => approve(a.id)}
								>✓ Approve</button>
								<button
									class="btn btn-danger text-xs"
									disabled={working[a.id]}
									on:click={() => reject(a.id)}
								>✗ Reject</button>
							</div>
						{/if}
					</div>

					<!-- Expanded body -->
					{#if expanded === a.id}
						<div class="ml-7 space-y-2">
							<!-- The plan comes FIRST: what happens to the host is the
							     decision, the artifact text is the reference. -->
							{#if planLoading[a.id]}
								<p class="text-xs text-muted">Checking {targetStr(a)} …</p>
							{:else if planErrors[a.id]}
								<div class="rounded border border-warn/40 bg-warn/5 p-2 space-y-1">
									<p class="text-xs text-ink-strong font-medium">
										Cannot show what this would change
									</p>
									<p class="text-xs text-muted">{planErrors[a.id]}</p>
									<button
										class="text-xs text-accent hover:text-accent-strong"
										on:click={() => { planErrors = { ...planErrors, [a.id]: '' }; loadPlan(a.id); }}
									>Retry</button>
								</div>
							{:else if plans[a.id]}
								<div class="space-y-1">
									<p class="text-xs text-ink-strong font-medium">{plans[a.id].summary}</p>
									<table class="w-full text-xs">
										<tbody>
											{#each plans[a.id].items as item}
												<tr class="border-t border-line/60">
													<td class="py-1 pr-2 text-muted w-16">{item.kind}</td>
													<td class="py-1 pr-2 font-mono text-ink-strong">{item.name}</td>
													<td class="py-1 pr-2 text-muted">{item.observed}</td>
													<td class="py-1 pr-2 text-muted">
														{#if item.changes}→ {item.desired}{:else}unchanged{/if}
													</td>
												</tr>
											{/each}
										</tbody>
									</table>
									{#if plans[a.id].policies?.length}
										<!-- The rules the operator wrote about this host, beside
										     what is about to happen to it (#429). Approving is
										     meant to be an informed decision. -->
										<div class="mt-2 border-l-2 border-warn-border pl-3 space-y-1">
											<p class="text-xs text-warn font-medium">
												Policies for {plans[a.id].host}
											</p>
											{#each plans[a.id].policies ?? [] as policy}
												<p class="prose-note text-xs">
													<span class="text-ink">{policy.title}</span> — {policy.content}
												</p>
											{/each}
										</div>
									{/if}
								</div>
							{/if}
							{#if bodies[a.id]}
								<pre class="code-block text-xs overflow-x-auto whitespace-pre-wrap max-h-64">{bodies[a.id]}</pre>
							{:else}
								<p class="text-muted text-xs">Loading…</p>
							{/if}
							{#if canWrite}
								<div class="flex gap-2 items-center">
									<input
										class="input text-xs flex-1"
										placeholder="Rejection reason (optional)"
										bind:value={rejectReasons[a.id]}
									/>
									<button
										class="btn btn-danger text-xs"
										disabled={working[a.id]}
										on:click={() => reject(a.id)}
									>Reject with reason</button>
								</div>
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>
