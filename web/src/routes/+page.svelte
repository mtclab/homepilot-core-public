<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import {
		api,
		sessionStore,
		type AuditEntry,
		type FiringAlert,
		type Host,
		type SelfcheckReport,
		type Task,
		type DashboardSummary,
		type EnrolmentRequests,
	} from '$lib/api';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import { taskStatusClass, shortTaskId } from '$lib/taskStatus';
	import { attentionReason } from '$lib/hostGroups';
	import { subsystemLabel } from '$lib/selfcheck';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import Donut from '$lib/components/Donut.svelte';
	import StatCard from '$lib/components/StatCard.svelte';
	import AttentionItem from '$lib/components/AttentionItem.svelte';
	import AlertsPanel from '$lib/components/AlertsPanel.svelte';

	// Overview is the operator's morning glance (#549 F2). Three zones, in
	// priority order, no tabs:
	//
	//   1. Needs attention — what wants an operator NOW, each line a door to the
	//      surface where it is fixed. Absent entirely when nothing is wrong.
	//   2. Fleet at a glance — the host chips, coverage, the honest counts.
	//   3. Recent movement — tasks and journal as ONE short feed.
	//
	// Everything below is composed from the summary plus the same reads the
	// pages it links to make (inventory, firing alerts, tasks, journal, the
	// self-check), and every count it quotes is computed over the same set as
	// that page - the drift count over ACTIVE artifacts, as the drift page does.
	// That is a rule to keep, not a property that holds by itself: a revoked
	// artifact's frozen verdict once made this zone say 2 where the page said 1.

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
	// The self-check (#549 F6) lived only under Settings -> Subsystems, so a
	// refused Proxmox token, a dead reconciler loop or an agent fleet behind the
	// hub was three clicks from the screen an operator actually opens. It runs
	// live probes, so it is re-read at most once a minute on event-driven
	// refreshes; the Refresh button always re-reads it.
	let selfcheck: SelfcheckReport | null = null;
	let selfcheckAt = 0;
	const SELFCHECK_TTL_MS = 60 * 1000;
	// Hosts knocking at the hub (lane A3): the same read the Hosts page's
	// "Waiting to join" panel makes, so the count here is the panel's count.
	let joining: EnrolmentRequests | null = null;

	// A side call that FAILED is not a side call that returned nothing. Emptying
	// the arrays and saying nothing turned an unanswered question into "Nothing
	// needs you." - a conclusion this page had not established (#642, #648
	// tranche 7), and exactly the mistake the KB page names at #663: a failure
	// rendered as a successful empty result is the worst way to be wrong,
	// because the operator concludes all is well and stops looking.
	interface Unread {
		key: string;
		text: string;
		href: string;
		/** 'unread' (the read failed) or 'partial' (it read only part of the set). */
		label?: string;
	}

	// How much of each set the page actually read (#704). The host strip reads
	// the first INVENTORY_PAGE hosts and the task lines the TASK_PAGE most
	// recent tasks; past that, "no agent offline" or "no failed task" is not
	// something this page has established, so it says how much it looked at.
	const INVENTORY_PAGE = 100;
	const TASK_PAGE = 50;
	let unread: Unread[] = [];
	// Where every subsystem line (and the self-check's own unread line) goes.
	const SUBSYSTEMS_HREF = `${base}/settings?tab=subsystems`;

	async function load(force = false) {
		loading = true;
		try {
			d = await api.getDashboard();
			const selfcheckFresh = !force && selfcheck && Date.now() - selfcheckAt < SELFCHECK_TTL_MS;
			const [inv, alerts, taskRes, auditRes, checkRes, joinRes] = await Promise.allSettled([
				api.listInventory({ limit: INVENTORY_PAGE }),
				api.listFiringAlerts(),
				api.listTasks(undefined, TASK_PAGE, 0),
				api.listAudit({ limit: 20 }),
				selfcheckFresh ? Promise.resolve(selfcheck as SelfcheckReport) : api.getSelfcheck(),
				api.getEnrolmentRequests(),
			]);
			joining = joinRes.status === 'fulfilled' ? joinRes.value : null;
			if (checkRes.status === 'fulfilled') {
				if (!selfcheckFresh) selfcheckAt = Date.now();
				selfcheck = checkRes.value;
			} else {
				selfcheck = null;
				selfcheckAt = 0;
			}
			fleet = inv.status === 'fulfilled' ? inv.value.items : [];
			firing = alerts.status === 'fulfilled' ? alerts.value.items : [];
			// A degraded side call must not blank the zone it feeds AND must not
			// leave stale rows claiming to be current: empty is the honest answer.
			tasks = taskRes.status === 'fulfilled' ? (taskRes.value.items ?? []) : [];
			journal = auditRes.status === 'fulfilled' ? (auditRes.value.items ?? []) : [];
			// ...and empty is only honest while the page SAYS the read failed.
			unread = [];
			if (inv.status === 'rejected')
				unread.push({
					key: 'unread:hosts',
					text: 'Could not read the host inventory, so no host or alert can be reported here',
					href: `${base}/hosts`,
				});
			if (alerts.status === 'rejected')
				unread.push({
					key: 'unread:alerts',
					text: 'Could not read firing alerts, so this page cannot say whether anything is alerting',
					href: `${base}/hosts`,
				});
			if (taskRes.status === 'rejected')
				unread.push({
					key: 'unread:tasks',
					text: 'Could not read recent tasks, so no failed or stuck task can be reported here',
					href: `${base}/records/tasks`,
				});
			if (checkRes.status === 'rejected')
				unread.push({
					key: 'unread:selfcheck',
					text: 'Could not read the self-check, so a subsystem that is down cannot be reported here',
					href: SUBSYSTEMS_HREF,
				});
			if (joinRes.status === 'rejected')
				unread.push({
					key: 'unread:joining',
					text: 'Could not read enrolment requests, so a host waiting to join cannot be reported here',
					href: `${base}/hosts`,
				});
			if (auditRes.status === 'rejected')
				unread.push({
					key: 'unread:journal',
					text: 'Could not read the journal, so recent movement is incomplete',
					href: `${base}/records/journal`,
				});
			if (inv.status === 'fulfilled' && (inv.value.total ?? 0) > fleet.length)
				unread.push({
					key: 'partial:hosts',
					label: 'partial',
					text: `Agent state is checked here for ${fleet.length} of ${inv.value.total} hosts; the rest are on the hosts page`,
					href: `${base}/hosts`,
				});
			if (taskRes.status === 'fulfilled' && (taskRes.value.total ?? 0) > tasks.length)
				unread.push({
					key: 'partial:tasks',
					label: 'partial',
					text: `Failed and stuck tasks are read from the ${tasks.length} most recent of ${taskRes.value.total}; older ones are in Records`,
					href: `${base}/records/tasks`,
				});
		} catch (e) {
			error = String(e);
		} finally {
			loading = false;
		}
	}

	// The Alerts list links a hostname to its page when inventory knows it.
	function alertHostHref(hostname: string): string {
		const h = fleet.find((x) => x.hostname === hostname);
		return h ? `${base}/hosts/${h.id}` : '';
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
		return load(true);
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
		/** A gone host the operator may remove from inventory from this line. */
		forget?: Host;
	}

	// A running task that has outlived this is stuck, not busy. Deliberately
	// generous: an apply over a slow link is not an incident.
	const STUCK_MS = 15 * 60 * 1000;
	// A failure is urgent while it is news. Past a day it is still unresolved -
	// so it stays - but it no longer outranks what broke this morning.
	const FAILED_CRITICAL_MS = 24 * 60 * 60 * 1000;
	// A host's agent dark this long is no reconnect blip.
	const AGENT_CRITICAL_MS = 60 * 60 * 1000;
	// Per KIND, so one bad night of failures cannot bury the drift line.
	const PER_KIND = 4;

	function plural(n: number, one: string, many: string): string {
		return `${n} ${n === 1 ? one : many}`;
	}

	function sinceMs(iso: string | null | undefined): number | null {
		if (!iso) return null;
		const t = Date.parse(iso);
		return Number.isNaN(t) ? null : Date.now() - t;
	}

	/** "for 3d": how long a condition has held, from when it started. */
	function lasting(iso: string | null | undefined): string {
		const ms = sinceMs(iso);
		if (ms === null) return '';
		const mins = Math.floor(ms / 60000);
		if (mins < 1) return 'for <1m';
		if (mins < 60) return `for ${mins}m`;
		const hours = Math.floor(mins / 60);
		if (hours < 48) return `for ${hours}h`;
		return `for ${Math.floor(hours / 24)}d`;
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

	/** What an artifactless task (an agent install) recorded about its host. */
	function taskHost(t: Task): { host_id?: string; hostname?: string } {
		if (!t.result_json) return {};
		try {
			const parsed: unknown = JSON.parse(t.result_json);
			return parsed && typeof parsed === 'object' ? (parsed as Record<string, string>) : {};
		} catch {
			return {};
		}
	}

	function taskTarget(t: Task): string {
		return t.artifact_id ?? taskHost(t).hostname ?? shortTaskId(t.id);
	}

	/** The thing a task acted on, or null when it cannot be told. */
	function taskSubject(t: Task): string | null {
		if (t.artifact_id) return `artifact:${t.artifact_id}`;
		const hostId = taskHost(t).host_id;
		return hostId ? `host:${hostId}` : null;
	}

	// Which later successes settle a failure. A failed apply is settled by the
	// apply or replay that then worked, or by revoking the change outright -
	// either way there is nothing left to fix. Any other action only by itself.
	const SETTLED_BY: Record<string, string[]> = {
		apply: ['apply', 'replay', 'revoke'],
		replay: ['apply', 'replay', 'revoke'],
	};

	/**
	 * A failure something later made moot: a later task on the same subject
	 * succeeded, or - for an agent install - the host's agent is connected now,
	 * which is the very thing the install was for. A failure whose subject
	 * cannot be told (an install recorded before tasks named their host) is
	 * never settled by guesswork; it only ages.
	 */
	function settled(t: Task, taskList: Task[], hosts: Host[]): boolean {
		const subject = taskSubject(t);
		if (!subject) return false;
		const actions = SETTLED_BY[t.action] ?? [t.action];
		const failedAt = Date.parse(t.created_at);
		const later = taskList.some(
			(o) =>
				o.id !== t.id &&
				o.status === 'succeeded' &&
				actions.includes(o.action) &&
				taskSubject(o) === subject &&
				Date.parse(o.created_at) > failedAt,
		);
		if (later) return true;
		if (t.action === 'install_agent') {
			const hostId = taskHost(t).host_id;
			return hosts.some((h) => h.id === hostId && h.agent_connected === true);
		}
		return false;
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
		unreadList: Unread[],
		checks: SelfcheckReport | null,
		join: EnrolmentRequests | null,
	): Attention[] {
		const out: Attention[] = [];

		// First, and before anything the page DOES know: what it could not ask.
		// A blind spot outranks a review queue, because everything below it is
		// only as complete as the reads that fed it.
		for (const u of unreadList) {
			out.push({
				key: u.key,
				severity: 'warning',
				label: u.label ?? 'unread',
				text: u.text,
				href: u.href,
				meta: '',
			});
		}
		if (!summary) return out;

		// The whole fleet dark is ONE fact with one consequence, not N quiet
		// "agent offline" lines plus a red card: prod sat a week like that and
		// said nothing louder. The server decides when it has lasted long enough
		// to say (a restart or a reconnect storm is not an incident); its cause
		// absorbs the per-host agent lines and the rules it leaves blind.
		const fleetDark = summary.agents.offline_alarm === true;
		if (fleetDark) {
			const enrolled = summary.agents.enrolled ?? summary.agents.known;
			const blind = summary.metrics.rules_enabled > 0 ? summary.metrics.rules_watching_nothing : 0;
			out.push({
				key: 'fleet:no_agent',
				severity: 'critical',
				label: 'no agent',
				text:
					`No agent is connected (${enrolled} enrolled), so nothing can be changed or measured on any host` +
					(blind > 0
						? `, and ${plural(blind, 'alert rule watches', 'alert rules watch')} nothing`
						: ''),
				href: `${base}/hosts`,
				meta: summary.agents.offline_since ? lasting(summary.agents.offline_since) : 'never connected',
			});
		}

		// Hosts whose agent holds a genuine token and is refused only because no
		// enrolment window is open (lane A3). Counted by hostname - the panel
		// keeps one row per hostname + address - and only while the window is
		// shut: with it open they get in on their next retry, nothing to do.
		const waiting = join?.window.open
			? 0
			: new Set((join?.attempts ?? []).filter((a) => a.wants_to_join).map((a) => a.hostname)).size;
		if (waiting > 0) {
			out.push({
				key: 'agents:waiting',
				severity: 'warning',
				label: 'wants to join',
				text: `${plural(waiting, 'host is', 'hosts are')} waiting to join - open an enrolment window on Hosts to let ${waiting === 1 ? 'it' : 'them'} in`,
				href: `${base}/hosts`,
				meta: '',
			});
		}

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

		// Unverified drift, by WHY - the answers are different jobs. A set that is
		// unverifiable by design (describe-only, a kind with no check) is the
		// artifact's nature, not a fault: it gets no line of its own, and is only
		// named alongside reasons that are work.
		const why = summary.drift.unknown_by_reason;
		if (summary.drift.unknown > 0) {
			const parts: string[] = [];
			const n = (k: keyof NonNullable<typeof why>) => why?.[k] ?? 0;
			if (n('no_agent')) parts.push(`${n('no_agent')} no agent on the host`);
			if (n('check_failed')) parts.push(`${n('check_failed')} check failed`);
			if (n('no_proxmox')) parts.push(`${n('no_proxmox')} no Proxmox connection`);
			if (n('other')) parts.push(`${n('other')} other reason`);
			const actionable = parts.length > 0 || !why;
			if (n('by_design')) parts.push(`${n('by_design')} describe-only (fine)`);
			if (actionable) {
				out.push({
					key: 'drift:unknown',
					severity: 'warning',
					label: 'unverified',
					text:
						`${plural(summary.drift.unknown, 'artifact', 'artifacts')} unverified` +
						(parts.length ? `: ${parts.join(', ')}` : ''),
					href: `${base}/changes/drift`,
					meta: '',
				});
			}
		}

		// Subsystems the self-check could not reach or could not verify, with its
		// own consequence sentence, verbatim - Settings words it per state, and a
		// paraphrase here would be a second, drifting copy. `off` is a choice and
		// stays on the Settings tab.
		for (const sub of checks?.subsystems ?? []) {
			if (sub.state !== 'unreachable' && sub.state !== 'unknown') continue;
			// "No agent is connected, so no version can be read" is the fleet line
			// again in other words - or, with none enrolled yet, the getting-started
			// step. Either way it is not a second piece of news.
			if (sub.name === 'agent_versions' && sub.state === 'unknown' && summary.agents.connected === 0)
				continue;
			out.push({
				key: `selfcheck:${sub.name}`,
				// An agent behind the hub is serious but not an outage; the others
				// unreachable mean a whole function of the product is gone.
				severity:
					sub.state === 'unreachable' && sub.name !== 'agent_versions' ? 'critical' : 'warning',
				label: subsystemLabel(sub),
				text: sub.consequence,
				href: SUBSYSTEMS_HREF,
				meta: '',
			});
		}

		// Failed tasks: a failed apply is fixed on its change page. One that a
		// later run settled is not attention any more, and one older than a day
		// is no longer critical - it was critical "forever" before, so ten-week
		// old retried failures sat above everything else on the screen.
		const failed = taskList.filter((t) => t.status === 'failed' && !settled(t, taskList, hosts));
		out.push(
			...capped(
				failed.map((t) => ({
					key: `task:${t.id}`,
					severity: ((sinceMs(t.finished_at ?? t.created_at) ?? 0) > FAILED_CRITICAL_MS
						? 'warning'
						: 'critical') as Attention['severity'],
					label: 'failed',
					text: `${t.action} ${taskTarget(t)} failed${t.error ? `: ${t.error}` : ''}`,
					href: taskHref(t),
					meta: ago(t.finished_at ?? t.created_at),
				})),
				'failed tasks',
				`${base}/records/tasks`,
			),
		);

		// The state an empty alert list cannot distinguish itself from. `0 firing`
		// on an install with no rules is not good news, it is the absence of news,
		// and it sat on the first screen reading green (#642, #648 tranche 5).
		if (summary.metrics.rules_enabled === 0) {
			out.push({
				key: 'alerts:none',
				severity: 'warning',
				label: 'not watched',
				text: 'No alert rule is enabled, so nothing raises an alert however bad it gets',
				href: `${base}/settings#monitoring`,
				meta: '',
			});
		} else if (summary.metrics.rules_watching_nothing > 0 && !fleetDark) {
			// A "went dark" (nodata) rule watches enrolled hosts, not metric
			// samples: when it matches none, no host its filter names has an
			// agent, and "check the metric name" would send the operator to a
			// field the rule does not have.
			const darkInert = summary.metrics.nodata_rules_watching_nothing ?? 0;
			const thresholdInert = summary.metrics.rules_watching_nothing - darkInert;
			if (thresholdInert > 0)
				out.push({
					key: 'alerts:inert',
					severity: 'warning',
					label: 'not watched',
					text: `${plural(thresholdInert, 'alert rule matches', 'alert rules match')} no host - check the metric name and the host filter`,
					href: `${base}/settings#monitoring`,
					meta: '',
				});
			if (darkInert > 0)
				out.push({
					key: 'alerts:inert:nodata',
					severity: 'warning',
					label: 'not watched',
					text: `${plural(darkInert, '"went dark" rule watches', '"went dark" rules watch')} no host - no host its filter matches has an agent enrolled`,
					href: `${base}/hosts`,
					meta: '',
				});
		}

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

		// A host whose agent is enrolled but absent cannot be changed at all. How
		// long matters: a reconnect blip and a dead week are different news. A
		// gone host says "gone" below instead - one reason per host, as on Hosts.
		const disconnected = fleetDark
			? []
			: hosts.filter((h) => h.agent_id && !h.agent_connected && !h.absent_since);
		out.push(
			...capped(
				disconnected.map((h) => ({
					key: `agent:${h.id}`,
					severity: ((sinceMs(h.agent_offline_since) ?? 0) > AGENT_CRITICAL_MS
						? 'critical'
						: 'warning') as Attention['severity'],
					label: 'agent offline',
					text: `${h.hostname}: agent enrolled but not connected`,
					href: `${base}/hosts/${h.id}`,
					meta: lasting(h.agent_offline_since),
				})),
				'hosts with an absent agent',
				`${base}/hosts`,
			),
		);

		// Hosts Proxmox stopped reporting. The Hosts page grouped them as
		// attention while this page said nothing and its donut counted them
		// online. Same membership rule as Hosts: an un-adopted guest is not news.
		const gone = hosts.filter((h) => attentionReason(h) === 'gone');
		out.push(
			...capped(
				gone.map((h) => ({
					key: `gone:${h.id}`,
					severity: 'warning' as const,
					label: 'gone',
					text: `${h.hostname}: Proxmox no longer reports this host`,
					href: `${base}/hosts/${h.id}`,
					meta: lasting(h.absent_since),
					forget: h,
				})),
				'hosts Proxmox no longer reports',
				`${base}/hosts`,
			),
		);

		// Stuck: still pending or running long after it should have finished. A
		// task that never left `pending` is stuck too - it is the one that never
		// even started (#704: this used to look at `running` only).
		const stuck = taskList.filter(
			(t) =>
				(t.status === 'running' || t.status === 'pending') &&
				Date.now() - Date.parse(t.created_at) > STUCK_MS,
		);
		out.push(
			...capped(
				stuck.map((t) => ({
					key: `stuck:${t.id}`,
					severity: 'warning' as const,
					label: 'stuck',
					text: `${t.action} ${taskTarget(t)} is still ${t.status}`,
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

	// Forget, from the line that says a host is gone: the same irreversible
	// DELETE the Hosts page offers (services and the observation note go with
	// the row), so the same two-step confirm. The server refuses a host
	// Proxmox still reports, so a stale line cannot delete a live machine.
	let forgetConfirm: string | null = null;
	let forgetting: string | null = null;

	async function forgetGone(h: Host) {
		forgetting = h.id;
		try {
			await api.forgetHost(h.id);
			notify(`Removed ${h.hostname} from inventory`);
			forgetConfirm = null;
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			forgetting = null;
		}
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

	/** The chip for a journal action; an action with none says nothing (#704). */
	function journalChip(action: string): string {
		return JOURNAL_CHIPS[action] ?? 'badge-neutral';
	}

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
				chip: journalChip(e.action),
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
	$: attention = buildAttention(d, fleet, firing, tasks, unread, selfcheck, joining);
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	// Zone 3 is fed by the two reads that can go unanswered, so its empty state
	// has the same two meanings and must not collapse them into "nothing yet".
	$: movementUnread = unread.some((u) => u.key === 'unread:tasks' || u.key === 'unread:journal');
	$: movement = buildMovement(tasks, journal);
</script>

<div class="page-stack">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Overview</h1>
		<button class="btn btn-ghost text-xs" on:click={() => load(true)} disabled={loading}>↻ Refresh</button>
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
						{#if item.forget && canWrite}
							{@const h = item.forget}
							<div class="flex items-baseline gap-s-2" data-testid="attention-gone">
								<div class="min-w-0 flex-1">
									<AttentionItem
										severity={item.severity}
										label={item.label}
										text={item.text}
										href={item.href}
										meta={item.meta}
									/>
								</div>
								{#if forgetConfirm === h.id}
									<button
										class="btn btn-danger btn-sm text-xs shrink-0"
										disabled={forgetting === h.id}
										on:click={() => forgetGone(h)}
										>{forgetting === h.id ? 'Removing…' : 'Confirm forget'}</button
									>
									<button
										class="btn btn-ghost btn-sm text-xs shrink-0"
										on:click={() => (forgetConfirm = null)}>Cancel</button
									>
								{:else}
									<button
										class="btn btn-ghost btn-sm text-xs shrink-0"
										title="Remove {h.hostname} from inventory, with its services and notes"
										on:click={() => (forgetConfirm = h.id)}>Forget</button
									>
								{/if}
							</div>
						{:else}
							<AttentionItem
								severity={item.severity}
								label={item.label}
								text={item.text}
								href={item.href}
								meta={item.meta}
							/>
						{/if}
					{/each}
				</div>
			</section>
		{:else}
			<p class="prose-note prose-measure text-sm">Nothing needs you.</p>
		{/if}

		<!-- Alerts (#660): firing now, resolved recently, and where they are sent. -->
		<AlertsPanel hostHref={alertHostHref} />

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
							? `${d.drift.drifted} drifting / ${d.drift.checked} checked · ${d.drift.unknown} not established${d.drift.unknown_by_reason?.by_design ? ` (${d.drift.unknown_by_reason.by_design} describe-only)` : ''}`
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
				{d.metrics.rules_enabled}
				{d.metrics.rules_enabled === 1 ? 'alert rule' : 'alert rules'} enabled.
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
			{:else if movementUnread}
				<p class="prose-note text-xs">
					This feed is empty because the record could not be read, not because nothing
					happened.
				</p>
			{:else}
				<p class="prose-note text-xs">Nothing has happened yet.</p>
			{/if}
		</section>
	{/if}
</div>
