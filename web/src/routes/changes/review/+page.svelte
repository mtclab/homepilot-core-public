<script lang="ts">
	// The review queue as a QUEUE (#549 F4).
	//
	// Everything an operator needs to decide is on the card: what kind of change
	// it is, which host it lands on, what it is for, whether a plan can be shown
	// for it, and the approval code to relay over MCP (#544) — which until now
	// lived only on the artifact detail page, so working the queue meant opening
	// every artifact in turn just to read a code.
	//
	// Applied history stays a TABLE below it, paged: those rows genuinely are
	// comparable, and comparing them is the only thing anyone does with them
	// (facelift-v2 principle 4).
	import { onMount } from 'svelte';
	import {
		api,
		sessionStore,
		type Artifact,
		type ArtifactDetail,
		type ArtifactPlan,
	} from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { pruneSelection } from '$lib/selection';
	import { base } from '$app/paths';
	import ApprovalCodePanel from '$lib/components/ApprovalCodePanel.svelte';

	let items: Artifact[] = [];
	let history: Artifact[] = [];
	let loading = true;
	let loadError = '';
	// Approve/Reject 403 without write scope. Default-deny while loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	let working: Record<string, boolean> = {};
	let expanded: string | null = null;
	let details: Record<string, ArtifactDetail> = {};
	let detailErrors: Record<string, string> = {};
	/** Ids whose detail fetch has been issued — an empty error is not a marker. */
	let detailRequested: Record<string, boolean> = {};
	let rejectReasons: Record<string, string> = {};
	let plans: Record<string, ArtifactPlan> = {};
	let planErrors: Record<string, string> = {};
	let planLoading: Record<string, boolean> = {};

	// The queue is paged, not scrolled: an unbounded list of cards is the shape
	// facelift-v2 principle 1 forbids, and it also bounds how many artifact
	// details the page fetches for its approval codes (one per visible card).
	const QUEUE_PAGE_SIZE = 10;
	const HISTORY_PAGE_SIZE = 10;
	let queuePage = 0;
	let historyPage = 0;

	$: queuePages = Math.max(1, Math.ceil(items.length / QUEUE_PAGE_SIZE));
	$: visible = items.slice(queuePage * QUEUE_PAGE_SIZE, (queuePage + 1) * QUEUE_PAGE_SIZE);
	$: historyPages = Math.max(1, Math.ceil(history.length / HISTORY_PAGE_SIZE));
	$: historyRows = history.slice(
		historyPage * HISTORY_PAGE_SIZE,
		(historyPage + 1) * HISTORY_PAGE_SIZE,
	);
	// The approval code lives on the artifact DETAIL, so a card has to fetch it.
	// Only the visible ones, and only once each.
	$: loadDetails(visible);

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
			// Two queries, not one filtered client-side: the queue is capped at
			// what a person can work through, and the history is a different
			// question with its own paging.
			const [queueRes, historyRes] = await Promise.all([
				api.listArtifacts({ status: 'proposed', limit: 200 }),
				api.listArtifacts({ status: 'applied', limit: 200 }),
			]);
			items = queueRes.items;
			history = historyRes.items;
			if (queuePage >= Math.ceil(items.length / QUEUE_PAGE_SIZE)) queuePage = 0;
			if (historyPage >= Math.ceil(history.length / HISTORY_PAGE_SIZE)) historyPage = 0;
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

	/** Fetch the detail (approval code + body) behind each visible card, once. */
	function loadDetails(cards: Artifact[]) {
		for (const a of cards) {
			if (detailRequested[a.id]) continue;
			// Marked before the await so a re-render mid-flight cannot re-issue it.
			detailRequested[a.id] = true;
			void (async () => {
				try {
					const res = await api.getArtifact(a.id);
					if (res) details = { ...details, [a.id]: res };
				} catch (e) {
					detailErrors = {
						...detailErrors,
						[a.id]: e instanceof Error ? e.message : String(e),
					};
				}
			})();
		}
	}

	async function clearApprovalLock(id: string) {
		working = { ...working, [id]: true };
		try {
			await api.resetApprovalCode(id);
			notify('Approval lock cleared');
			const res = await api.getArtifact(id);
			if (res) details = { ...details, [id]: res };
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = { ...working, [id]: false };
		}
	}

	function toggleExpand(id: string) {
		if (expanded === id) {
			expanded = null;
			return;
		}
		expanded = id;
		loadPlan(id);
	}

	// What applying this would do to the HOST (#445 A1). Approval used to show
	// only the artifact text, so the decision was made blind. Loaded on demand
	// rather than for every card: the endpoint is read-only but it talks to the
	// host, and planning ten hosts because a page rendered is not free.
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

	// Selection for bulk actions. Pruned on every reload AND on every page turn,
	// so "3 selected -> Approve" can never act on cards the operator can no
	// longer see - the same rule the inventory table follows.
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
		const allVisible = visible.length > 0 && visible.every((a) => selected.has(a.id));
		selected = allVisible ? new Set() : new Set(visible.map((a) => a.id));
	}

	function setQueuePage(n: number) {
		queuePage = n;
		selected = pruneSelection(
			selected,
			items.slice(n * QUEUE_PAGE_SIZE, (n + 1) * QUEUE_PAGE_SIZE),
		);
		bulkConfirm = null;
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
			failed.length ? 'err' : 'ok',
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

	function fmtDate(s: string): string {
		return s ? new Date(s).toLocaleDateString() : '—';
	}

	/** One line for what a plan can say about this artifact, before it is opened. */
	function planAvailability(id: string): string {
		if (planLoading[id]) return 'checking the host…';
		if (planErrors[id]) return 'no plan available';
		const plan = plans[id];
		if (!plan) return 'plan not fetched yet';
		return plan.summary;
	}

	onMount(load);
</script>

<div class="section-stack">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Review Queue</h1>
		<div class="flex items-center gap-3">
			{#if !canWrite && !loading && !loadError}
				<span class="text-muted text-xs"
					>Read-only session — approving and rejecting need a write-scope token.</span
				>
			{/if}
			<span class="text-muted text-xs">{items.length} proposed</span>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Refresh</button>
		</div>
	</div>

	{#if canWrite && visible.length > 0}
		<!-- Bulk approve/reject (#435). Inventory has had checkboxes and bulk
		     actions for a while; the review queue - the page where a backlog
		     actually piles up - had none, and every decision was one mouse round
		     trip. The confirm is inline and two-step, the same as every other
		     destructive action here: a native confirm() in one place and nothing
		     in another was the inconsistency the issue names.
		     Select-all covers the CARDS ON SCREEN, never the pages behind them. -->
		<div class="flex items-center gap-3 flex-wrap">
			<label class="flex items-center gap-2 text-xs text-muted">
				<input
					type="checkbox"
					checked={selected.size > 0 && visible.every((a) => selected.has(a.id))}
					on:change={toggleAll}
				/>
				Select all
			</label>
			{#if selected.size > 0}
				<span class="text-xs text-ink">{selected.size} selected</span>
				{#if bulkConfirm}
					<span class="text-xs text-warn">
						{bulkConfirm === 'approve' ? 'Approve' : 'Reject'}
						{selected.size} artifact{selected.size === 1 ? '' : 's'}?
					</span>
					<button
						class="btn text-xs {bulkConfirm === 'approve' ? 'btn-success' : 'btn-danger'}"
						disabled={bulkRunning}
						on:click={() => runBulk(bulkConfirm === 'approve' ? 'approve' : 'reject')}
						>{bulkRunning ? 'Working…' : 'Confirm'}</button
					>
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
		<div class="section-stack">
			{#each visible as a (a.id)}
				<article class="card section-stack" data-testid="review-card">
					<!-- What this is: kind, target host, intent. -->
					<div class="flex items-start gap-3">
						{#if canWrite}
							<label class="shrink-0 pt-1">
								<span class="sr-only">Select {a.id}</span>
								<input
									type="checkbox"
									checked={selected.has(a.id)}
									on:change={() => toggle(a.id)}
								/>
							</label>
						{/if}
						<div class="flex-1 min-w-0 space-y-1">
							<h2 class="text-sm text-ink-strong font-medium">{a.intent}</h2>
							<div class="flex gap-2 flex-wrap items-center text-xs text-muted">
								<span class="badge {statusClass(a.status)}">{a.status}</span>
								<span>{a.kind}</span>
								<span>·</span>
								<span class="font-mono text-ink">{targetStr(a)}</span>
								{#if a.tags?.length}
									<span>·</span>
									{#each a.tags as tag}
										<span>{tag}</span>
									{/each}
								{/if}
								<span>·</span>
								<a
									href="{base}/changes/{a.id}"
									class="font-mono text-accent hover:text-accent-strong"
									title="Open artifact detail">{a.id.slice(-8)}</a
								>
							</div>
						</div>
						{#if canWrite}
							<div class="flex gap-2 shrink-0">
								<button
									class="btn btn-success text-xs"
									disabled={working[a.id]}
									on:click={() => approve(a.id)}>✓ Approve</button
								>
								<button
									class="btn btn-danger text-xs"
									disabled={working[a.id]}
									on:click={() => reject(a.id)}>✗ Reject</button
								>
							</div>
						{/if}
					</div>

					<!-- What it would DO. The plan is the decision; the artifact text is
					     the reference, so both sit behind one disclosure. -->
					<div class="space-y-2">
						<button
							class="text-xs text-accent hover:text-accent-strong"
							aria-expanded={expanded === a.id}
							on:click={() => toggleExpand(a.id)}
						>
							{expanded === a.id ? '▾' : '▸'} What this changes
						</button>
						<span class="text-xs text-muted" data-testid="plan-availability"
							>{planAvailability(a.id)}</span
						>
					</div>

					{#if expanded === a.id}
						<div class="space-y-2">
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
										on:click={() => {
											planErrors = { ...planErrors, [a.id]: '' };
											loadPlan(a.id);
										}}>Retry</button
									>
								</div>
							{:else if plans[a.id]}
								<div class="space-y-1">
									<p class="text-xs text-ink-strong font-medium">{plans[a.id].summary}</p>
									<table class="data-table text-xs">
										<tbody>
											{#each plans[a.id].items as item}
												<tr class="border-t border-line/60">
													<td class="col-secondary w-16">{item.kind}</td>
													<td class="col-primary font-mono">{item.name}</td>
													<td class="col-secondary">{item.observed}</td>
													<td class="col-secondary">
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
											<p class="text-xs text-warn font-medium">Policies for {plans[a.id].host}</p>
											{#each plans[a.id].policies ?? [] as policy}
												<p class="prose-note text-xs">
													<span class="text-ink">{policy.title}</span> — {policy.content}
												</p>
											{/each}
										</div>
									{/if}
								</div>
							{/if}
							{#if details[a.id]?.body}
								<pre
									class="code-block text-xs overflow-x-auto whitespace-pre-wrap max-h-64">{details[
										a.id
									].body}</pre>
							{:else if detailErrors[a.id]}
								<p class="text-xs text-muted">Could not load the artifact body: {detailErrors[a.id]}</p>
							{:else}
								<p class="text-muted text-xs">Loading…</p>
							{/if}
						</div>
					{/if}

					<!-- The code a human relays to the assistant to approve over MCP. -->
					<ApprovalCodePanel
						code={details[a.id]?.approval_code}
						locked={details[a.id]?.approval_locked ?? false}
						{canWrite}
						busy={working[a.id]}
						onClear={() => clearApprovalLock(a.id)}
					/>

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
								on:click={() => reject(a.id)}>Reject with reason</button
							>
						</div>
					{/if}
				</article>
			{/each}
		</div>

		{#if queuePages > 1}
			<div class="flex items-center gap-2 text-xs">
				<button
					class="btn btn-ghost text-xs"
					disabled={queuePage === 0}
					on:click={() => setQueuePage(queuePage - 1)}>← Newer</button
				>
				<span class="text-muted">Page {queuePage + 1} of {queuePages}</span>
				<button
					class="btn btn-ghost text-xs"
					disabled={queuePage >= queuePages - 1}
					on:click={() => setQueuePage(queuePage + 1)}>Older →</button
				>
			</div>
		{/if}
	{/if}

	{#if !loading && !loadError && history.length > 0}
		<!-- Applied history: rows that ARE comparable, so a table is the right
		     tool (principle 4). One primary column carrying the intent, the rest
		     muted supporting evidence. -->
		<section class="section-stack">
			<h2 class="section-title">Applied history</h2>
			<div class="card overflow-x-auto">
				<table class="data-table text-xs">
					<thead>
						<tr>
							<th class="col-primary">Intent</th>
							<th class="col-secondary">Kind</th>
							<th class="col-secondary">Target</th>
							<th class="col-secondary">Applied</th>
						</tr>
					</thead>
					<tbody>
						{#each historyRows as a (a.id)}
							<tr class="border-b border-divider hover:bg-raised transition-colors">
								<td class="col-primary">
									<span class="col-primary-inner">
										<a
											href="{base}/changes/{a.id}"
											class="text-accent hover:text-accent-strong"
											title={a.id}>{a.intent}</a
										>
										<span class="badge {statusClass(a.status)}">{a.status}</span>
									</span>
								</td>
								<td class="col-secondary">{a.kind}</td>
								<td class="col-secondary font-mono">{targetStr(a)}</td>
								<td class="col-secondary">{fmtDate(a.created_at)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
			{#if historyPages > 1}
				<div class="flex items-center gap-2 text-xs">
					<button
						class="btn btn-ghost text-xs"
						disabled={historyPage === 0}
						on:click={() => (historyPage -= 1)}>← Newer</button
					>
					<span class="text-muted">Page {historyPage + 1} of {historyPages}</span>
					<button
						class="btn btn-ghost text-xs"
						disabled={historyPage >= historyPages - 1}
						on:click={() => (historyPage += 1)}>Older →</button
					>
				</div>
			{/if}
		</section>
	{/if}
</div>
