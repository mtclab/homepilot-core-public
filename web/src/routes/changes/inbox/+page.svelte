<script lang="ts">
	// Changes > Inbox (UI_PLAN 3.6; owner feedback 2026-11-08). Was the review
	// queue (#549 F4).
	//
	// One place a change waits in, from proposal until it has run. The owner
	// approved a change, it left the queue, nothing said it still had to be
	// applied, and the empty queue read as "lost". So the Inbox holds proposed
	// AND approved-not-yet-run changes: an approved one stays here as "Ready to
	// run" with Apply on its card, a running one says "Running: <intent>, started
	// 18:28" with the task's live state, and a finished one moves to "Finished
	// today" with its outcome in words. The queue never looks empty while work is
	// pending.
	//
	// Everything an operator needs to decide is on the card: the change in plain
	// words (the raw spec behind a disclosure), the target, whether a plan can
	// be shown, and the approval code to relay over MCP (#544).
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		api,
		sessionStore,
		type Artifact,
		type ArtifactDetail,
		type ArtifactPlan,
	} from '$lib/api';
	import { canWrite as capCanWrite, isAdmin } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { pruneSelection } from '$lib/selection';
	import { base } from '$app/paths';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import { inboxState, recentlyFinished } from '$lib/changes';
	import ApprovalCodePanel from '$lib/components/ApprovalCodePanel.svelte';
	import ChangeOutcome from '$lib/components/ChangeOutcome.svelte';
	import ChangeSummary from '$lib/components/ChangeSummary.svelte';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import Icon from '$lib/components/Icon.svelte';

	/** Proposed changes, waiting for a decision. */
	let items: Artifact[] = [];
	/** Approved changes not yet run - ready to run, or running now. */
	let ready: Artifact[] = [];
	/** Applied, failed or withdrawn in the last day: the outcome stays in view. */
	let finished: Artifact[] = [];
	let loading = true;
	let loadError = '';
	// Approve/Reject 403 without write scope. Default-deny while loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	// Approving needs an operator (admin) session (#681): a write token only
	// approves with a relayed code, which is for the assistant, not this screen.
	$: canApprove = isAdmin($sessionStore?.capabilities);
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
	const FINISHED_MAX = 10;
	let queuePage = 0;

	$: queuePages = Math.max(1, Math.ceil(items.length / QUEUE_PAGE_SIZE));
	$: visible = items.slice(queuePage * QUEUE_PAGE_SIZE, (queuePage + 1) * QUEUE_PAGE_SIZE);
	$: recent = finished.slice(0, FINISHED_MAX);
	// The approval code, the summary and the run state live on the artifact
	// DETAIL, so a card has to fetch it. Only the cards on screen, once each
	// (a running card is re-read by the poll below).
	$: loadDetails([...ready, ...visible, ...recent]);
	// Keyboard walk (UI_PLAN 3.6): ready cards first, then the proposals on screen.
	$: cards = [...ready, ...visible];
	$: runningIds = ready.filter((a) => details[a.id]?.active_task).map((a) => a.id);
	$: syncPoll(runningIds);
	$: waitingCount = items.length + ready.length;

	async function load(initial = true) {
		if (initial) loading = true;
		loadError = '';
		try {
			// Separate questions, not one list filtered client-side: the queue is
			// capped at what a person can work through, and "what ran today" is
			// its own question.
			const [queueRes, readyRes, appliedRes, failedRes, withdrawnRes] = await Promise.all([
				api.listArtifacts({ status: 'proposed', limit: 200 }),
				api.listArtifacts({ status: 'approved', limit: 200 }),
				api.listArtifacts({ status: 'applied', limit: 200 }),
				api.listArtifacts({ status: 'failed', limit: 200 }),
				api.listArtifacts({ status: 'withdrawn', limit: 200 }),
			]);
			items = queueRes.items;
			ready = readyRes.items;
			finished = recentlyFinished([
				...appliedRes.items,
				...failedRes.items,
				...withdrawnRes.items,
			]);
			// A card whose state moved is read again, not served from the cache.
			for (const a of [...items, ...ready, ...finished]) {
				const known = details[a.id];
				if (known && known.frontmatter.status !== a.status) delete detailRequested[a.id];
			}
			loadDetails([...ready, ...visible, ...finished.slice(0, FINISHED_MAX)]);
			if (queuePage >= Math.ceil(items.length / QUEUE_PAGE_SIZE)) queuePage = 0;
			selected = pruneSelection(selected, items);
		} catch (e) {
			// A toast alone left the last-good (or empty) list on screen with no way
			// back — the queue looked empty when it had simply failed to load.
			const msg = e instanceof Error ? e.message : String(e);
			if (items.length === 0 && ready.length === 0) loadError = msg;
			else notify(msg, 'err');
		} finally {
			loading = false;
		}
	}

	/** Re-read one card's detail now (after an action, or while it runs). */
	async function refreshDetail(id: string) {
		try {
			const res = await api.getArtifact(id);
			if (res) details = { ...details, [id]: res };
		} catch (e) {
			detailErrors = { ...detailErrors, [id]: e instanceof Error ? e.message : String(e) };
		}
	}

	// Live task state while something runs (UI_PLAN 5.4: honest, from the
	// server). Every 2 s the running cards are re-read; when one stops running
	// the lists are re-read, so it moves to "Finished today" with its outcome.
	let pollHandle: ReturnType<typeof setInterval> | null = null;
	function syncPoll(ids: string[]) {
		if (ids.length === 0) {
			if (pollHandle) clearInterval(pollHandle);
			pollHandle = null;
			return;
		}
		if (pollHandle) return;
		pollHandle = setInterval(async () => {
			const before = runningIds;
			await Promise.all(before.map(refreshDetail));
			if (before.some((id) => !details[id]?.active_task)) await load(false);
		}, 2000);
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
				else await api.rejectArtifact(id, (rejectReasons[id] ?? '').trim() || undefined);
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
		// Approved ones move to "Ready to run" - they stay on this page.
		await load(false);
		notify(
			failed.length
				? `${action === 'approve' ? 'Approved' : 'Rejected'} ${done}; failed: ${failed.join(', ')}`
				: `${action === 'approve' ? 'Approved' : 'Rejected'} ${done}`,
			failed.length ? 'err' : 'ok',
		);
	}

	/** Approve only: the change stays in the Inbox as "Ready to run". */
	async function approve(id: string) {
		working = { ...working, [id]: true };
		try {
			await api.approveArtifact(id);
			notify('Approved - ready to run below');
			await load(false);
			await refreshDetail(id);
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = { ...working, [id]: false };
		}
	}

	/** The owner's one action: approve, and start the run (UI_PLAN 3.6). */
	async function approveAndApply(id: string) {
		working = { ...working, [id]: true };
		try {
			await api.approveAndApplyArtifact(id);
			notify('Approved and started');
		} catch (e) {
			// Approved but not started still lands in "Ready to run": the reload
			// below shows it there, never lost.
			notify(String(e), 'err');
		} finally {
			await load(false);
			await refreshDetail(id);
			working = { ...working, [id]: false };
		}
	}

	async function applyNow(id: string) {
		working = { ...working, [id]: true };
		try {
			await api.applyArtifact(id);
			notify('Started');
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			await refreshDetail(id);
			working = { ...working, [id]: false };
		}
	}

	// Withdraw is the inline-confirm tier (UI_PLAN 5.3): it only stops a change.
	let withdrawArmed: string | null = null;
	async function withdraw(id: string) {
		withdrawArmed = null;
		working = { ...working, [id]: true };
		try {
			await api.withdrawArtifact(id);
			notify('Withdrawn - it will not run');
			await load(false);
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = { ...working, [id]: false };
		}
	}

	// Running a destroy or a forced stop asks for the target's name typed back
	// (UI_PLAN 3.10); any other change runs on one press. From the keyboard every
	// approve-and-apply asks first, since a key is easier to hit by accident.
	let confirming: { id: string; run: 'approve-and-apply' | 'apply'; typed: boolean } | null =
		null;
	function requestRun(id: string, run: 'approve-and-apply' | 'apply', fromKey = false) {
		const danger = details[id]?.summary?.danger;
		if (danger || fromKey) {
			confirming = { id, run, typed: !!danger };
			return;
		}
		void (run === 'apply' ? applyNow(id) : approveAndApply(id));
	}
	function confirmRun() {
		if (!confirming) return;
		const { id, run } = confirming;
		confirming = null;
		void (run === 'apply' ? applyNow(id) : approveAndApply(id));
	}
	function confirmTitle(c: { id: string; run: string }): string {
		const s = details[c.id]?.summary;
		const verb = s?.danger === 'destroy' ? 'Destroy' : s?.danger === 'stop' ? 'Force-stop' : 'Run';
		return `${verb} ${s?.target ?? c.id}`;
	}
	function confirmBody(c: { id: string; run: string }): string {
		const s = details[c.id]?.summary;
		const steps = s?.steps?.length ? s.steps.join('. ') + '.' : '';
		const cannot =
			s?.danger === 'destroy'
				? ' This cannot be undone: the guest and its disks are deleted.'
				: s?.danger === 'stop'
					? ' Anything unsaved in the guest is lost.'
					: '';
		const approving = c.run === 'approve-and-apply' ? 'Approves the change and runs it now. ' : '';
		return `${approving}${steps}${cannot}`;
	}

	async function reject(id: string) {
		const reason = (rejectReasons[id] ?? '').trim() || undefined;
		working = { ...working, [id]: true };
		try {
			await api.rejectArtifact(id, reason);
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


	/** One line for what a plan can say about this artifact, before it is opened. */
	function planAvailability(id: string): string {
		if (planLoading[id]) return 'checking the host…';
		if (planErrors[id]) return 'no plan available';
		const plan = plans[id];
		if (!plan) return 'plan not fetched yet';
		return plan.summary;
	}

	// Keyboard (UI_PLAN 3.6): j/k walk the cards, `a` approve-and-apply (asks
	// first), `r` reject, Enter opens. Never while typing in a field or while a
	// dialog is open.
	let focusIdx = -1;
	function cardEl(i: number): HTMLElement | null {
		const id = cards[i]?.id;
		return id ? document.querySelector<HTMLElement>(`[data-card-id="${id}"]`) : null;
	}
	function onKeydown(e: KeyboardEvent) {
		if (confirming || e.ctrlKey || e.metaKey || e.altKey) return;
		const t = e.target as HTMLElement | null;
		if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
		if (cards.length === 0) return;
		if (e.key === 'j' || e.key === 'k') {
			e.preventDefault();
			focusIdx =
				e.key === 'j' ? Math.min(cards.length - 1, focusIdx + 1) : Math.max(0, focusIdx - 1);
			cardEl(focusIdx)?.focus();
			return;
		}
		const a = cards[focusIdx];
		if (!a || document.activeElement !== cardEl(focusIdx)) return;
		if (e.key === 'Enter') {
			e.preventDefault();
			void goto(`${base}/changes/${a.id}`);
		} else if (e.key === 'a' && a.status === 'proposed' && canApprove) {
			if (details[a.id]?.summary?.two_step) return;
			e.preventDefault();
			requestRun(a.id, 'approve-and-apply', true);
		} else if (e.key === 'r' && a.status === 'proposed' && canWrite) {
			e.preventDefault();
			void reject(a.id);
		}
	}

	onMount(() => load());
	// Another tab, the assistant or the CLI moved a change: re-read, once per burst.
	const refresh = debounce(() => load(false), 400);
	const unsub = onArtifactEvent(() => refresh());
	onDestroy(() => {
		unsub();
		refresh.cancel();
		if (pollHandle) clearInterval(pollHandle);
	});
</script>

<svelte:window on:keydown={onKeydown} />

<div class="section-stack">
	<div class="flex items-center justify-between gap-3 flex-wrap">
		<h1 class="page-title">Inbox</h1>
		<div class="flex items-center gap-3 flex-wrap">
			<button class="btn btn-ghost text-xs" on:click={() => load()}
				><Icon name="refresh" />Refresh</button
			>
			<span class="text-muted text-xs" data-testid="inbox-counts"
				>{items.length} to review · {ready.length} ready to run</span
			>
		</div>
	</div>
	<!-- The approval rule, said once (review 06 3.2). -->
	<p class="prose-note text-xs" data-testid="approval-rule">
		An admin session approves directly. Anyone else needs the code.
		{#if !canWrite && !loading && !loadError}
			This session is read-only: it can look, not decide.
		{/if}
	</p>

	{#if !loading && !loadError && ready.length > 0}
		<!-- Approved, not yet run: the owner's move. Never off-screen (owner
		     feedback 2026-11-08: an approved change that left the queue read as
		     a lost one). -->
		<section class="section-stack" aria-labelledby="ready-title">
			<h2 class="section-title" id="ready-title">Ready to run</h2>
			{#each ready as a (a.id)}
				{@const d = details[a.id]}
				{@const state = inboxState(a, d?.active_task)}
				<article
					class="card section-stack"
					data-testid="ready-card"
					data-card-id={a.id}
					tabindex="-1"
				>
					<div class="flex items-start gap-3 flex-wrap" data-card-head>
						<div class="flex-[1_1_14rem] min-w-0 space-y-1" data-card-what>
							<h3 class="text-sm text-ink-strong font-medium">
								<a href="{base}/changes/{a.id}" class="hover:text-accent">{a.intent}</a>
							</h3>
							<p class="text-xs text-muted">
								{a.kind} · <span class="font-mono text-ink">{targetStr(a)}</span>
								{#if a.approved_by?.user}· approved by {a.approved_by.user}{/if}
							</p>
						</div>
						{#if canWrite && state === 'ready'}
							<div class="flex gap-2 shrink-0 flex-wrap">
								<button
									class="btn btn-primary text-xs"
									disabled={working[a.id] || !d}
									on:click={() => requestRun(a.id, 'apply')}>Apply</button
								>
								{#if withdrawArmed === a.id}
									<button
										class="btn btn-danger text-xs"
										disabled={working[a.id]}
										on:click={() => withdraw(a.id)}>Confirm withdraw</button
									>
									<button class="btn btn-ghost text-xs" on:click={() => (withdrawArmed = null)}
										>Cancel</button
									>
								{:else}
									<button
										class="btn btn-ghost text-xs"
										disabled={working[a.id]}
										on:click={() => (withdrawArmed = a.id)}>Withdraw</button
									>
								{/if}
							</div>
						{/if}
					</div>
					{#if d}
						<ChangeOutcome fm={d.frontmatter} activeTask={d.active_task} lastTask={d.last_task} />
						<ChangeSummary summary={d.summary} body={d.body} />
					{:else if detailErrors[a.id]}
						<p class="text-xs text-muted">Could not load this change: {detailErrors[a.id]}</p>
					{:else}
						<p class="text-muted text-xs">Loading…</p>
					{/if}
				</article>
			{/each}
		</section>
	{/if}

	{#if !loading && !loadError && items.length > 0}
		<h2 class="section-title">To review</h2>
	{/if}

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
					{#if canApprove}
						<button class="btn btn-success text-xs" on:click={() => (bulkConfirm = 'approve')}
							><Icon name="check" />Approve selected</button
						>
					{/if}
					<button class="btn btn-danger text-xs" on:click={() => (bulkConfirm = 'reject')}
						><Icon name="close" />Reject selected</button
					>
				{/if}
			{/if}
		</div>
	{/if}

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load the Inbox.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={() => load()}
				><Icon name="refresh" />Retry</button
			>
		</div>
	{:else if waitingCount === 0}
		<!-- Empty only when nothing is proposed AND nothing approved waits to run
		     (UI_PLAN 3.6). -->
		<div class="card text-center py-10" data-testid="inbox-empty">
			<p class="prose-note text-sm">Inbox empty. Nothing is waiting for you.</p>
		</div>
	{:else if items.length > 0}
		<div class="section-stack">
			{#each visible as a (a.id)}
				{@const d = details[a.id]}
				<article
					class="card section-stack"
					data-testid="review-card"
					data-card-id={a.id}
					tabindex="-1"
				>
					<!-- What this is: kind, target host, intent. The actions wrap under
					     it on a phone: with a zero flex-basis the title shrank to one word
					     a line under three buttons that ran off the card (3.10.0 walk). -->
					<div class="flex items-start gap-3 flex-wrap" data-card-head>
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
						<div class="flex-[1_1_14rem] min-w-0 space-y-1" data-card-what>
							<h3 class="text-sm text-ink-strong font-medium">{a.intent}</h3>
							<div class="flex gap-2 flex-wrap items-center text-xs text-muted">
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
							<!-- One primary: Approve and apply (owner decision 5). A
							     composite or replay_safe:false change keeps two steps,
							     so its primary is Approve. -->
							<div class="flex gap-2 flex-wrap min-w-0" data-card-actions>
								{#if canApprove && d && !d.summary?.two_step}
									<button
										class="btn btn-primary text-xs"
										disabled={working[a.id]}
										on:click={() => requestRun(a.id, 'approve-and-apply')}
										><Icon name="check" />Approve and apply</button
									>
									<button
										class="btn btn-ghost text-xs"
										disabled={working[a.id]}
										on:click={() => approve(a.id)}>Approve only</button
									>
								{:else if canApprove}
									<button
										class="btn btn-success text-xs"
										disabled={working[a.id]}
										on:click={() => approve(a.id)}><Icon name="check" />Approve</button
									>
								{/if}
								<button
									class="btn btn-danger text-xs"
									disabled={working[a.id]}
									on:click={() => reject(a.id)}><Icon name="close" />Reject</button
								>
							</div>
						{/if}
					</div>

					{#if d}
						<ChangeSummary summary={d.summary} />
					{/if}

					<!-- What it would DO. The plan is the decision; the artifact text is
					     the reference, so both sit behind one disclosure. -->
					<div class="space-y-2">
						<button
							class="text-xs text-accent hover:text-accent-strong"
							aria-expanded={expanded === a.id}
							on:click={() => toggleExpand(a.id)}
						>
							<Icon name={expanded === a.id ? 'chevron-down' : 'chevron-right'} /> What this changes
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
														{#if item.changes}<Icon name="arrow-right" label="becomes" /> {item.desired}{:else}unchanged{/if}
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
					on:click={() => setQueuePage(queuePage - 1)}><Icon name="arrow-left" />Newer</button
				>
				<span class="text-muted">Page {queuePage + 1} of {queuePages}</span>
				<button
					class="btn btn-ghost text-xs"
					disabled={queuePage >= queuePages - 1}
					on:click={() => setQueuePage(queuePage + 1)}>Older<Icon name="arrow-right" /></button
				>
			</div>
		{/if}
	{/if}

	{#if !loading && !loadError && recent.length > 0}
		<!-- What ran in the last day, with its outcome in words: a change that
		     just finished is not gone, it is here saying how it ended. The whole
		     record is Changes > All. -->
		<section class="section-stack" aria-labelledby="finished-title">
			<h2 class="section-title" id="finished-title">Finished today</h2>
			{#each recent as a (a.id)}
				{@const d = details[a.id]}
				<article class="card space-y-2" data-testid="finished-card">
					<h3 class="text-sm text-ink-strong font-medium">
						<a href="{base}/changes/{a.id}" class="hover:text-accent">{a.intent}</a>
					</h3>
					<p class="text-xs text-muted">
						{a.kind} · <span class="font-mono text-ink">{targetStr(a)}</span>
					</p>
					<ChangeOutcome fm={d?.frontmatter ?? a} activeTask={null} lastTask={d?.last_task} />
				</article>
			{/each}
			<a class="text-xs text-accent hover:text-accent-strong" href="{base}/changes"
				>Every change, in Changes > All</a
			>
		</section>
	{/if}
</div>

{#if confirming}
	{@const c = confirming}
	<ConfirmDialog
		title={confirmTitle(c)}
		consequence={confirmBody(c)}
		confirmName={c.typed ? (details[c.id]?.summary?.confirm_name ?? null) : null}
		confirmLabel={c.run === 'approve-and-apply' ? 'Approve and apply' : 'Apply'}
		on:confirm={confirmRun}
		on:cancel={() => (confirming = null)}
	/>
{/if}
