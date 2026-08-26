<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import {
		api,
		type AuditEntry,
		type FiringAlert,
		type Host,
		type Task,
		type DashboardSummary,
	} from '$lib/api';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import { taskStatusClass, shortTaskId } from '$lib/taskStatus';
	import Donut from '$lib/components/Donut.svelte';
	import StatCard from '$lib/components/StatCard.svelte';
	import AttentionItem from '$lib/components/AttentionItem.svelte';

	// Overview is the operator's morning glance (#549 F2). Three zones, in
	// priority order, no tabs:
	//
	//   1. Needs attention — what wants an operator NOW, each line a door to the
	//      surface where it is fixed. Absent entirely when nothing is wrong.
	//   2. Fleet at a glance — the host chips, coverage, the honest counts.
	//   3. Recent movement — tasks and journal as ONE short feed.
	//
	// Everything below is composed from the four calls this page already makes
	// plus the task/journal lists Records already serves: nothing new was added
	// server-side to make the attention zone possible, so the zone can never
	// disagree with the pages it links to.

	let d: DashboardSummary | null = null;
	let error = '';
	let loading = true;

	// One status color language across the whole UI — the status tokens, never a
	// loose hex.
	const STATUS_COLORS: Record<string, string> = {
		online: 'var(--color-ok)',
		offline: 'var(--color-danger)',
		unknown: 'var(--color-muted)',
		running: 'var(--color-ok)',
		stopped: 'var(--color-danger)'
	};
	// Categorical (non-status) series use the neutral chart ramp so a green slice
	// never reads as "healthy".
	const ROLE_COLORS = [
		'var(--chart-1)',
		'var(--chart-2)',
		'var(--chart-3)',
		'var(--chart-4)',
		'var(--chart-5)',
		'var(--chart-6)'
	];

	function toSegments(m: Record<string, number>, colors?: Record<string, string>) {
		return Object.entries(m || {})
			.filter(([, v]) => v > 0)
			.map(([label, value], i) => ({
				label,
				value,
				color: (colors && colors[label]) || ROLE_COLORS[i % ROLE_COLORS.length]
			}));
	}

	// The fleet-health strip (#514 P3/S5): one chip per host - state, agent,
	// firing alerts - so glancing is Overview and digging is the host page.
	let fleet: Host[] = [];
	let firing: FiringAlert[] = [];
	// Zone 1 + zone 3 both read these; one fetch each, not one per zone.
	let tasks: Task[] = [];
	let journal: AuditEntry[] = [];

	async function load() {
		loading = true;
		try {
			d = await api.getDashboard();
			const [inv, alerts, taskRes, auditRes] = await Promise.allSettled([
				api.listInventory({ limit: 100 }),
				api.listFiringAlerts(),
				api.listTasks(undefined, 50, 0),
				api.listAudit({ limit: 20 }),
			]);
			fleet = inv.status === 'fulfilled' ? inv.value.items : [];
			firing = alerts.status === 'fulfilled' ? alerts.value.items : [];
			// A degraded side call must not blank the zone it feeds AND must not
			// leave stale rows claiming to be current: empty is the honest answer.
			tasks = taskRes.status === 'fulfilled' ? (taskRes.value.items ?? []) : [];
			journal = auditRes.status === 'fulfilled' ? (auditRes.value.items ?? []) : [];
		} catch (e) {
			error = String(e);
		} finally {
			loading = false;
		}
	}

	function alertCount(hostname: string): number {
		return firing.filter((f) => f.hostname === hostname).length;
	}
	onMount(() => {
		try {
			onboardingHidden = localStorage.getItem(HIDE_KEY) === '1';
		} catch {
			onboardingHidden = false;
		}
		return load();
	});
	// The dashboard summary rolls up artifact + drift counts, so refresh it live
	// on any lifecycle/drift event — coalesced, so a burst is one summary call.
	const refresh = debounce(() => load(), 400);
	const unsub = onArtifactEvent(refresh);
	onDestroy(() => {
		unsub();
		refresh.cancel();
	});

	// The checklist hides itself once the path is walked; this is the escape for
	// an operator who does not want it in the meantime. Per-browser on purpose -
	// it is a display preference, not estate state, and it must never be able to
	// make the dashboard claim setup is further along than it is.
	let onboardingHidden = false;
	const HIDE_KEY = 'hp.onboarding.hidden';

	function hideOnboarding() {
		onboardingHidden = true;
		try {
			localStorage.setItem(HIDE_KEY, '1');
		} catch {
			// A browser with storage blocked still gets the hide for this session.
		}
	}

	// --- Zone 1: needs attention ------------------------------------------
	//
	// One item is one line and one door. Counts that cannot name a single
	// culprit (drift, the review queue) stay ROLLED UP rather than pretending to
	// be per-row: the page they link to is where the rows live.

	interface Attention {
		key: string;
		severity: 'critical' | 'warning' | 'notice';
		label: string;
		text: string;
		href: string;
		meta: string;
	}

	// A running task that has outlived this is stuck, not busy. Deliberately
	// generous: an apply over a slow link is not an incident.
	const STUCK_MS = 15 * 60 * 1000;
	// Per KIND, so one bad night of failures cannot bury the drift line.
	const PER_KIND = 4;

	function plural(n: number, one: string, many: string): string {
		return `${n} ${n === 1 ? one : many}`;
	}

	function ago(iso: string | null | undefined): string {
		if (!iso) return '';
		const t = Date.parse(iso);
		if (Number.isNaN(t)) return '';
		const mins = Math.round((Date.now() - t) / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hours = Math.round(mins / 60);
		if (hours < 48) return `${hours}h ago`;
		return `${Math.round(hours / 24)}d ago`;
	}

	/** Where a task is fixed: its change, or the task list when it has none. */
	function taskHref(t: Task): string {
		return t.artifact_id ? `${base}/changes/${t.artifact_id}` : `${base}/records/tasks`;
	}

	function taskTarget(t: Task): string {
		return t.artifact_id ?? shortTaskId(t.id);
	}

	function hostHref(hostname: string): string {
		const h = fleet.find((x) => x.hostname === hostname);
		return h ? `${base}/hosts/${h.id}` : `${base}/hosts`;
	}

	/** `items` capped at PER_KIND, with an honest "+N more" door on the tail. */
	function capped(items: Attention[], moreText: string, moreHref: string): Attention[] {
		if (items.length <= PER_KIND) return items;
		const hidden = items.length - PER_KIND;
		return [
			...items.slice(0, PER_KIND),
			{
				key: `${moreHref}:more`,
				severity: items[0].severity,
				label: `+${hidden}`,
				text: `${hidden} more ${moreText}`,
				href: moreHref,
				meta: '',
			},
		];
	}

	function buildAttention(
		summary: DashboardSummary | null,
		hosts: Host[],
		alerts: FiringAlert[],
		taskList: Task[],
	): Attention[] {
		const out: Attention[] = [];
		if (!summary) return out;

		// Drift: the summary counts it, the drift page names it.
		if (summary.drift.drifted > 0) {
			out.push({
				key: 'drift',
				severity: 'critical',
				label: 'drifted',
				text: `${plural(summary.drift.drifted, 'artifact disagrees', 'artifacts disagree')} with the host`,
				href: `${base}/changes/drift`,
				meta: '',
			});
		}

		// Failed tasks: a failed apply is fixed on its change page.
		const failed = taskList.filter((t) => t.status === 'failed');
		out.push(
			...capped(
				failed.map((t) => ({
					key: `task:${t.id}`,
					severity: 'critical' as const,
					label: 'failed',
					text: `${t.action} ${taskTarget(t)} failed${t.error ? `: ${t.error}` : ''}`,
					href: taskHref(t),
					meta: ago(t.finished_at ?? t.created_at),
				})),
				'failed tasks',
				`${base}/records/tasks`,
			),
		);

		// Firing alerts, one line per alert - the host page is where the chart is.
		out.push(
			...capped(
				alerts.map((a) => ({
					key: `alert:${a.rule_id}:${a.hostname}`,
					severity: 'critical' as const,
					label: 'alert',
					text: `${a.hostname}: ${a.name}`,
					href: hostHref(a.hostname),
					meta: ago(a.firing_since),
				})),
				'firing alerts',
				`${base}/hosts`,
			),
		);

		// A host whose agent is enrolled but absent cannot be changed at all.
		const disconnected = hosts.filter((h) => h.agent_id && !h.agent_connected);
		out.push(
			...capped(
				disconnected.map((h) => ({
					key: `agent:${h.id}`,
					severity: 'warning' as const,
					label: 'agent offline',
					text: `${h.hostname}: agent enrolled but not connected`,
					href: `${base}/hosts/${h.id}`,
					meta: '',
				})),
				'hosts with an absent agent',
				`${base}/hosts`,
			),
		);

		// Stuck: still running long after it should have finished.
		const stuck = taskList.filter(
			(t) => t.status === 'running' && Date.now() - Date.parse(t.created_at) > STUCK_MS,
		);
		out.push(
			...capped(
				stuck.map((t) => ({
					key: `stuck:${t.id}`,
					severity: 'warning' as const,
					label: 'stuck',
					text: `${t.action} ${taskTarget(t)} is still running`,
					href: taskHref(t),
					meta: ago(t.created_at),
				})),
				'long-running tasks',
				`${base}/records/tasks`,
			),
		);

		// Waiting on a human: not a fault, but nothing moves until it is done.
		const proposed = summary.artifacts?.proposed ?? 0;
		if (proposed > 0) {
			out.push({
				key: 'review',
				severity: 'notice',
				label: 'review',
				text: `${plural(proposed, 'change', 'changes')} awaiting review`,
				href: `${base}/changes/review`,
				meta: '',
			});
		}
		return out;
	}

	// --- Zone 3: recent movement ------------------------------------------
	//
	// Tasks and journal are two halves of the same story (what was asked, what
	// ran), so they are ONE feed here and two pages in Records.

	interface Movement {
		key: string;
		ts: number;
		when: string;
		kind: string;
		chip: string;
		text: string;
		href: string;
	}

	const FEED_LIMIT = 10;

	const JOURNAL_CHIPS: Record<string, string> = {
		propose: 'badge-proposed',
		approve: 'badge-approved',
		apply: 'badge-applied',
		revoke: 'badge-revoked',
		reject: 'badge-rejected',
		replay: 'badge-superseded',
		drift_check: 'badge-notice',
		host_added: 'badge-approved',
		host_forgotten: 'badge-revoked',
	};

	function buildMovement(taskList: Task[], entries: AuditEntry[]): Movement[] {
		const items: Movement[] = [];
		for (const t of taskList) {
			const when = t.finished_at ?? t.created_at;
			items.push({
				key: `task:${t.id}`,
				ts: Date.parse(when) || 0,
				when: ago(when),
				kind: t.action,
				// The chip's COLOUR carries the outcome, its text the action, so a
				// failed apply cannot read like a successful one.
				chip: taskStatusClass(t.status),
				text: `${taskTarget(t)} · ${t.status}`,
				href: taskHref(t),
			});
		}
		for (const e of entries) {
			const target = [e.artifact_id, e.target_host].filter(Boolean).join(' · ');
			items.push({
				key: `audit:${e.id}`,
				ts: Date.parse(e.timestamp) || 0,
				when: ago(e.timestamp),
				kind: e.action,
				chip: JOURNAL_CHIPS[e.action] ?? 'badge-proposed',
				text: `${target || '—'} · ${e.source}`,
				href: e.artifact_id ? `${base}/changes/${e.artifact_id}` : `${base}/records/journal`,
			});
		}
		return items.sort((a, b) => b.ts - a.ts).slice(0, FEED_LIMIT);
	}

	$: doneCount = d ? d.onboarding.steps.filter((s) => s.done).length : 0;
	$: nextStep = d ? d.onboarding.steps.find((s) => !s.done) : undefined;
	$: statusSegments = d ? toSegments(d.inventory.by_status, STATUS_COLORS) : [];
	$: artifactSegments = d ? toSegments(d.artifacts) : [];
	$: attention = buildAttention(d, fleet, firing, tasks);
	$: movement = buildMovement(tasks, journal);
</script>

<div class="page-stack">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Overview</h1>
		<button class="btn btn-ghost text-xs" on:click={load} disabled={loading}>↻ Refresh</button>
	</div>

	{#if loading && !d}
		<p class="text-muted text-sm">Loading…</p>
	{:else if error && !d}
		<div class="card text-sm text-muted">Could not load the dashboard: {error}</div>
	{:else if d}
		{#if d.onboarding && !d.onboarding.complete && !onboardingHidden}
			<!-- A fresh install showed 0% coverage, three empty donuts and no
			     indication of what to do next (#445 A7). Every step's state is
			     read from the estate, so this cannot claim something happened
			     that did not. -->
			<section class="card section-stack" aria-labelledby="getting-started">
				<div class="flex items-start justify-between gap-3">
					<div>
						<h2 class="section-title" id="getting-started">Getting started</h2>
						<p class="prose-note text-xs">
							{doneCount} of {d.onboarding.steps.length} done. This disappears on its own once
							a change has been applied to a managed host.
						</p>
					</div>
					<button class="btn btn-ghost text-xs" on:click={hideOnboarding}>Hide</button>
				</div>
				<ol class="space-y-2">
					{#each d.onboarding.steps as step, i}
						<li class="flex items-start gap-3">
							<span
								class="mt-0.5 shrink-0 {step.done ? 'text-ok' : 'text-muted'}"
								aria-hidden="true">{step.done ? '✓' : `${i + 1}.`}</span
							>
							<span class="space-y-0.5">
								<span class="block text-sm {step.done ? 'text-muted line-through' : 'text-ink'}">
									{step.title}
									<span class="sr-only">{step.done ? '(done)' : '(not done yet)'}</span>
								</span>
								{#if !step.done}
									<span class="block prose-note text-xs">{step.detail}</span>
									{#if step.key === nextStep?.key}
										<a class="btn btn-primary text-xs mt-1 inline-block" href="{base}{step.href}"
											>Do this next →</a
										>
									{/if}
								{/if}
							</span>
						</li>
					{/each}
				</ol>
			</section>
		{/if}

		<!-- Zone 1. A calm estate gets ONE line, not an empty card with a heading
		     and a zero in it: the absence of a section is itself the signal. -->
		{#if attention.length}
			<section class="card section-stack" aria-labelledby="needs-attention">
				<div class="flex items-center justify-between">
					<h2 class="section-title" id="needs-attention">Needs attention</h2>
					<span class="text-xs text-muted num-inline">{attention.length}</span>
				</div>
				<div class="flex flex-col">
					{#each attention as item (item.key)}
						<AttentionItem
							severity={item.severity}
							label={item.label}
							text={item.text}
							href={item.href}
							meta={item.meta}
						/>
					{/each}
				</div>
			</section>
		{:else}
			<p class="prose-note prose-measure text-sm">Nothing needs you.</p>
		{/if}

		<!-- Zone 2: fleet at a glance. -->
		<section class="section-stack" aria-labelledby="fleet-at-a-glance">
			<h2 class="section-title" id="fleet-at-a-glance">Fleet at a glance</h2>

			<div class="grid grid-cols-1 md:grid-cols-3 gap-s-3">
				<StatCard
					label="Coverage"
					value="{d.inventory.coverage_pct}%"
					sub="{d.inventory.managed}/{d.inventory.total} hosts managed{d.inventory.uncovered
						? ` · ${d.inventory.uncovered} pending adoption`
						: ''}"
					accent={d.inventory.coverage_pct >= 80 ? 'ok' : 'warn'}
					href="{base}/hosts"
				/>
				<!-- P6: nothing checked is NOT 100%. The dash is the honest answer
				     until a check has actually run. -->
				<StatCard
					label="In spec"
					value={d.drift.checked > 0 ? `${d.drift.in_spec_pct}%` : '—'}
					sub={d.drift.checked === 0
						? 'nothing checked yet'
						: d.drift.unknown
							? `${d.drift.drifted} drifting / ${d.drift.checked} checked · ${d.drift.unknown} not established`
							: `${d.drift.drifted} drifting / ${d.drift.checked} checked`}
					accent={d.drift.checked === 0
						? 'neutral'
						: d.drift.drifted > 0
							? 'danger'
							: d.drift.unknown > 0
								? 'warn'
								: 'ok'}
					href="{base}/changes/drift"
				/>
				<StatCard
					label="Agents"
					value="{d.agents.connected}/{d.agents.known}"
					sub="connected / known"
					accent={d.agents.known > 0 && d.agents.connected === d.agents.known
						? 'ok'
						: d.agents.connected === 0
							? 'danger'
							: 'warn'}
					href="{base}/agents"
				/>
			</div>

			{#if fleet.length}
				<div class="card section-stack">
					<div class="flex items-center justify-between">
						<div class="section-title">Hosts</div>
						<a class="text-accent text-xs" href="{base}/hosts">All hosts →</a>
					</div>
					<div class="flex flex-wrap gap-1.5">
						{#each fleet as h (h.id)}
							<a
								href="{base}/hosts/{h.id}"
								class="inline-flex items-center gap-1.5 px-2 py-1 rounded border border-line text-xs hover:border-accent"
								title="{h.hostname}: {h.status ?? 'unknown'}{h.agent_id ? (h.agent_connected ? ', agent connected' : ', agent not connected') : ''}"
							>
								<span
									class="w-1.5 h-1.5 rounded-full {h.status === 'online'
										? 'bg-ok'
										: h.status === 'offline'
											? 'bg-danger'
											: 'bg-border-strong'}"
								></span>
								<span class="text-ink font-mono">{h.hostname}</span>
								{#if h.agent_id && !h.agent_connected}
									<span class="text-muted" title="Agent enrolled but not connected">∅</span>
								{/if}
								{#if alertCount(h.hostname)}
									<span class="badge badge-failed">{alertCount(h.hostname)}</span>
								{/if}
							</a>
						{/each}
					</div>
				</div>
			{/if}

			<div class="grid grid-cols-1 md:grid-cols-2 gap-s-3">
				<div class="card">
					<h3 class="section-title mb-3">Hosts by status</h3>
					{#if statusSegments.length}
						<Donut segments={statusSegments} centerLabel={String(d.inventory.total)} centerSub="hosts" />
					{:else}
						<p class="prose-note text-xs">No hosts yet.</p>
					{/if}
				</div>
				<div class="card">
					<h3 class="section-title mb-3">Artifacts</h3>
					{#if artifactSegments.length}
						<Donut
							segments={artifactSegments}
							centerLabel={String(artifactSegments.reduce((s, x) => s + x.value, 0))}
							centerSub="total"
						/>
					{:else}
						<p class="prose-note text-xs">No artifacts yet.</p>
					{/if}
				</div>
			</div>

			<p class="prose-note text-xs">
				Each host's page carries its charts. Metrics kept for {d.metrics.retention_days} days.
			</p>
		</section>

		<!-- Zone 3: recent movement. One feed, capped, with a door to the full
		     record rather than an unbounded scroll here. -->
		<section class="card section-stack" aria-labelledby="recent-movement">
			<div class="flex items-center justify-between">
				<h2 class="section-title" id="recent-movement">Recent movement</h2>
				<a class="text-accent text-xs" href="{base}/records">Records →</a>
			</div>
			{#if movement.length}
				<ul class="flex flex-col">
					{#each movement as m (m.key)}
						<li>
							<a
								href={m.href}
								class="flex items-baseline gap-s-2 py-s-1 px-s-2 rounded hover:bg-raised transition-colors"
							>
								<span class="text-xs text-muted num-inline shrink-0 w-16">{m.when}</span>
								<span class="badge {m.chip} shrink-0">{m.kind}</span>
								<span class="prose-body text-sm min-w-0 flex-1 truncate" title={m.text}>{m.text}</span>
							</a>
						</li>
					{/each}
				</ul>
			{:else}
				<p class="prose-note text-xs">Nothing has happened yet.</p>
			{/if}
		</section>
	{/if}
</div>
