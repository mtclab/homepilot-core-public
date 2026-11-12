<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import {
		api,
		sessionStore,
		type AlertHistory,
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
	import { canWrite as capCanWrite, isAdmin as capIsAdmin } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import AttentionItem from '$lib/components/AttentionItem.svelte';
	import AlertStrip from '$lib/components/AlertStrip.svelte';
	import Readout from '$lib/components/Readout.svelte';
	import Lamp from '$lib/components/Lamp.svelte';
	import Icon from '$lib/components/Icon.svelte';

	// Overview is the operator's morning glance (#549 F2; relaid out in UI
	// slice 4 / review 01 step 13). Four zones, in priority order, no tabs:
	//
	//   1. Needs attention — what wants an operator NOW, each line a door to the
	//      surface where it is fixed and, where one exists, the action that
	//      fixes it without leaving the page. One serif line when calm.
	//   2. Alerts — two lines: how many fire, where an alert goes (the list is
	//      Monitoring > Alerts; the Overview never repeats it).
	//   3. Estate — one readout row (hosts, watched, agents, in spec, changes)
	//      and the host chips. The capacity ledger joins this row when its
	//      backend reports (review 02/8, 03/7); until then it is absent, not
	//      zero - a number nobody measured is not shown.
	//   4. Recent movement — tasks and journal as ONE short feed.
	//
	// Everything below is composed from the summary plus the same reads the
	// pages it links to make (inventory, firing alerts, tasks, journal, the
	// self-check), and every count it quotes is computed over the same set as
	// that page - the drift count over ACTIVE artifacts, as the drift page does.
	// That is a rule to keep, not a property that holds by itself: a revoked
	// artifact's frozen verdict once made this zone say 2 where the page said 1.

	let d: DashboardSummary | null = null;
	// What waits in the Changes Inbox: proposals AND approved changes not yet
	// run (#805). Counting proposals alone made an approved change vanish from
	// the Estate readout, the "lost change" the owner hit (3.10.0).
	$: waitingProposed = d?.changes?.proposed ?? d?.artifacts?.proposed ?? 0;
	$: waitingReady = d?.changes?.ready ?? 0;
	let error = '';
	let loading = true;

	// The fleet-health strip (#514 P3/S5): one chip per host - state, agent,
	// firing alerts - so glancing is Overview and digging is the host page.
	let fleet: Host[] = [];
	let firing: FiringAlert[] = [];
	// The alert strip's two reads, `null` when they did not come back - the
	// strip then says "could not read", never "0 firing".
	let firingRead: FiringAlert[] | null = null;
	let alertHistory: AlertHistory | null = null;
	// When the page last finished reading, for "last read 12 s ago" and the
	// calm line's "checked N sources".
	let readAt = 0;
	let now = Date.now();
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
	/** The attention sources that answered on the last read. */
	let checked: string[] = [];
	/** Every source the attention list is built from (summary first). */
	const ATTENTION_SOURCES = 6;
	// Where every subsystem line (and the self-check's own unread line) goes.
	const SUBSYSTEMS_HREF = `${base}/settings?tab=subsystems`;

	async function load(force = false) {
		loading = true;
		try {
			d = await api.getDashboard();
			const selfcheckFresh = !force && selfcheck && Date.now() - selfcheckAt < SELFCHECK_TTL_MS;
			const [inv, alerts, taskRes, auditRes, checkRes, joinRes, historyRes] = await Promise.allSettled([
				// Templates are provisioning inputs, not machines of the fleet (B1).
				api.listInventory({ limit: INVENTORY_PAGE, templates: 'exclude' }),
				api.listFiringAlerts(),
				api.listTasks(undefined, TASK_PAGE, 0),
				api.listAudit({ limit: 20 }),
				selfcheckFresh ? Promise.resolve(selfcheck as SelfcheckReport) : api.getSelfcheck(),
				api.getEnrolmentRequests(),
				api.listAlertEvents(24),
			]);
			alertHistory = historyRes.status === 'fulfilled' ? historyRes.value : null;
			firingRead = alerts.status === 'fulfilled' ? alerts.value.items : null;
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
					href: `${base}/monitoring/alerts`,
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
					href: `${base}/hosts/agents`,
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
			// Which sources the attention list was built from, by name - the calm
			// line says how many it looked at (review 01, 3.5), and an unread one
			// is already its own line above.
			checked = [
				'the dashboard summary',
				...(inv.status === 'fulfilled' ? ['the host inventory'] : []),
				...(alerts.status === 'fulfilled' ? ['firing alerts'] : []),
				...(taskRes.status === 'fulfilled' ? ['recent tasks'] : []),
				...(checkRes.status === 'fulfilled' ? ['the self-check'] : []),
				...(joinRes.status === 'fulfilled' ? ['enrolment requests'] : []),
			];
			readAt = Date.now();
			now = readAt;
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

	function alertCount(hostname: string): number {
		return firing.filter((f) => f.hostname === hostname).length;
	}
	onMount(() => {
		try {
			onboardingHidden = localStorage.getItem(HIDE_KEY) === '1';
		} catch {
			onboardingHidden = false;
		}
		// "last read 12 s ago" ages on screen; the reads themselves stay
		// event-driven (no polling here).
		const ticker = setInterval(() => (now = Date.now()), 5000);
		void load(true);
		return () => clearInterval(ticker);
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
		/** When the condition began (ISO), where the source says; never guessed. */
		since?: string | null;
		/** The one-click fix this line offers, where one exists today. */
		action?: AttentionAction;
		/** The fix is outside the console (release notes): open it in a new tab. */
		external?: boolean;
		/** What this line's one subject asks of the operator (operator actions). */
		details?: string[];
	}

	/**
	 * The actions an attention line can complete without leaving the page
	 * (UI slice 4): the ones the API offers today. Forget and Cancel are
	 * mutating and ask first in place; opening the enrolment window is the
	 * same 15 minutes Hosts > Agents opens; Re-check only re-reads.
	 */
	type AttentionAction =
		| { kind: 'forget'; host: Host }
		| { kind: 'open-window' }
		| { kind: 'recheck-drift' }
		| { kind: 'cancel-task'; task: Task };

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

	/** "12 s ago" / "3 min ago" for the page's own last read. */
	function secondsAgo(at: number, nowMs: number): string {
		const s = Math.max(0, Math.round((nowMs - at) / 1000));
		if (s < 5) return 'just now';
		if (s < 90) return `${s} s ago`;
		return `${Math.round(s / 60)} min ago`;
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
				since: summary.agents.offline_since,
			});
		}

		// Two machines on one address (lane B2): the one that starts second takes
		// it over on the LAN, and nothing else on this screen would say so until
		// something stopped answering. One line per address, naming every holder;
		// the server words it, so the host's Network tab and this line agree.
		const clashes = summary.addresses?.conflicts ?? [];
		out.push(
			...capped(
				clashes.map((c) => {
					const first = c.host_ids[0];
					return {
						key: `address:${c.address}`,
						severity: 'critical' as const,
						label: 'address clash',
						text: `${c.summary}. ${c.remedy}`,
						href: first ? `${base}/hosts/${first}?tab=network` : `${base}/hosts`,
						meta: '',
					};
				}),
				'address clashes',
				`${base}/hosts`,
			),
		);

		// Hosts whose agent holds a genuine token and is refused only because no
		// enrolment window is open (lane A3). Counted by hostname - the panel
		// keeps one row per hostname + address - and only while the window is
		// shut: with it open they get in on their next retry, nothing to do.
		const knocking = join?.window.open ? [] : (join?.attempts ?? []).filter((a) => a.wants_to_join);
		const waiting = new Set(knocking.map((a) => a.hostname)).size;
		if (waiting > 0) {
			// Waiting since the first knock of any of them.
			const firstKnock = knocking.map((a) => a.first_seen).sort()[0] ?? null;
			out.push({
				key: 'agents:waiting',
				severity: 'warning',
				label: 'wants to join',
				text: `${plural(waiting, 'host is', 'hosts are')} waiting to join - open an enrolment window on Hosts > Agents to let ${waiting === 1 ? 'it' : 'them'} in`,
				href: `${base}/hosts/agents`,
				meta: lasting(firstKnock),
				since: firstKnock,
				action: { kind: 'open-window' },
			});
		}

		// HomePilot's own host (lane B1), once an operator confirmed which it is.
		// With no agent there, the machine everything else depends on is the
		// one HomePilot can neither measure nor maintain.
		const me = summary.inventory.self_guest;
		if (me?.absent) {
			out.push({
				key: 'self:gone',
				severity: 'warning',
				label: 'own host',
				text: `The host marked as HomePilot's own (${me.hostname}) is gone from Proxmox - confirm which host it runs on now`,
				href: `${base}/hosts`,
				meta: '',
			});
		} else if (me && !me.has_agent) {
			out.push({
				key: 'self:no_agent',
				severity: 'warning',
				label: 'own host',
				text: `HomePilot's own host ${me.hostname} has no agent, so it can be neither measured nor maintained from here`,
				href: `${base}/hosts/${me.host_id}`,
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
				action: { kind: 'recheck-drift' },
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
					// A failed check or a missing Proxmox link may have cleared.
					action: n('check_failed') || n('no_proxmox') || n('other') ? { kind: 'recheck-drift' } : undefined,
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
					since: t.finished_at ?? t.created_at,
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
				href: `${base}/monitoring/rules`,
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
					href: `${base}/monitoring/rules`,
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
					since: a.firing_since,
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
					since: h.agent_offline_since,
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
					since: h.absent_since,
					action: { kind: 'forget' as const, host: h },
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
					since: t.created_at,
					action: { kind: 'cancel-task' as const, task: t },
				})),
				'long-running tasks',
				`${base}/records/tasks`,
			),
		);

		// Waiting on a human: not a fault, but nothing moves until it is done.
		const proposed = summary.changes?.proposed ?? summary.artifacts?.proposed ?? 0;
		if (proposed > 0) {
			out.push({
				key: 'review',
				severity: 'notice',
				label: 'review',
				text: `${plural(proposed, 'change', 'changes')} awaiting review`,
				href: `${base}/changes/inbox`,
				meta: '',
			});
		}
		// Approved and not yet run (owner feedback 2026-11-08): it is still the
		// owner's move, so it is said here too - an approved change that left
		// every list read as a lost one.
		// An approval older than approval_max_age_days will not run (#813): the
		// cluster it was reviewed against may be gone - a VMID may hold a
		// different machine by now. Said apart from the ready ones, as a warning.
		const expired = summary.changes?.expired ?? 0;
		const ready = Math.max(0, (summary.changes?.ready ?? 0) - expired);
		if (expired > 0) {
			out.push({
				key: 'ready:expired',
				severity: 'warning',
				label: 'expired',
				text: `${plural(expired, 'approved change has', 'approved changes have')} an approval too old to run on - review ${expired === 1 ? 'it' : 'them'} again in the Inbox`,
				href: `${base}/changes/inbox`,
				meta: '',
			});
		}
		if (ready > 0) {
			out.push({
				key: 'ready',
				severity: 'notice',
				label: 'ready',
				text: `${plural(ready, 'approved change', 'approved changes')} ready to run`,
				href: `${base}/changes/inbox`,
				meta: '',
			});
		}

		// A newer HomePilot (review A+): ONE line while it is true - the server
		// compares against the version running now, so it is gone after the
		// upgrade - with the release's notes as the door and its operator actions
		// under it. News, not a fault: a notice, last. Nothing upgrades itself.
		const upd = summary.updates;
		if (upd?.update_available && upd.latest) {
			out.push({
				key: 'update:available',
				severity: 'notice',
				label: 'update',
				text: `HomePilot ${upd.latest.version} is available - you run ${upd.running_version}`,
				href: upd.latest.url,
				external: true,
				meta: ago(upd.latest.published_at),
				since: upd.latest.published_at,
				details: upd.latest.operator_actions ?? [],
			});
		}
		return out;
	}

	// Forget, from the line that says a host is gone: the same irreversible
	// DELETE the Hosts page offers (services and the observation note go with
	// the row), so the same two-step confirm. The server refuses a host
	// Proxmox still reports, so a stale line cannot delete a live machine.
	//
	// Every line action follows one shape (plan 5.3): the mutating ones turn
	// their button into [Confirm verb] [Cancel] in place, the working one says
	// what it is doing, and the page re-reads afterwards so the line goes (or
	// stays) by what the server now says - never by what the click hoped.
	let confirming: string | null = null;
	let working: string | null = null;

	async function runAction(key: string, act: AttentionAction) {
		working = key;
		try {
			if (act.kind === 'forget') {
				await api.forgetHost(act.host.id);
				notify(`Removed ${act.host.hostname} from inventory`);
			} else if (act.kind === 'open-window') {
				await api.openEnrolmentWindow(15);
				notify('Enrolment window open for 15 min - waiting hosts join on their next retry', 'ok');
			} else if (act.kind === 'recheck-drift') {
				const res = await api.getDriftStatus({ refresh: true });
				const drifted = (res.items ?? []).filter((c) => (c.state ? c.state === 'drifted' : c.drifted)).length;
				notify(`Drift re-checked: ${plural(drifted, 'artifact disagrees', 'artifacts disagree')} with the host`, 'ok');
			} else if (act.kind === 'cancel-task') {
				await api.cancelTask(act.task.id);
				notify(`Cancelled ${act.task.action} ${taskTarget(act.task)}`, 'ok');
			}
			confirming = null;
			await load(true);
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			working = null;
		}
	}

	/** The verb on the button, the confirm wording, and who may press it. */
	function actionSpec(act: AttentionAction): {
		verb: string;
		busy: string;
		confirm: string | null;
		title: string;
		allowed: boolean;
	} {
		switch (act.kind) {
			case 'forget':
				return {
					verb: 'Forget',
					busy: 'Removing...',
					confirm: 'Confirm forget',
					title: `Remove ${act.host.hostname} from inventory, with its services and notes`,
					allowed: canWrite,
				};
			case 'open-window':
				return {
					verb: 'Open window',
					busy: 'Opening...',
					confirm: 'Open for 15 min',
					title: 'Let hosts holding the shared token join for the next 15 minutes',
					allowed: admin,
				};
			case 'recheck-drift':
				return {
					verb: 'Re-check',
					busy: 'Checking...',
					confirm: null,
					title: 'Check every applied artifact against its host now',
					allowed: true,
				};
			case 'cancel-task':
				return {
					verb: 'Cancel task',
					busy: 'Cancelling...',
					confirm: 'Confirm cancel',
					title: `Cancel ${act.task.action} ${taskTarget(act.task)}`,
					allowed: canWrite,
				};
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
		withdraw: 'badge-revoked',
		replay: 'badge-superseded',
		drift_check: 'badge-notice',
		host_added: 'badge-approved',
		host_forgotten: 'badge-revoked',
		address_conflict: 'badge-critical',
		address_conflict_resolved: 'badge-approved',
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
	$: attention = buildAttention(d, fleet, firing, tasks, unread, selfcheck, joining);
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	$: admin = capIsAdmin($sessionStore?.capabilities);
	$: readAgo = readAt ? secondsAgo(readAt, now) : '';
	// The estate's watched share: hosts read whose agent is connected now.
	$: watched = fleet.filter((h) => h.agent_connected === true).length;
	// Zone 3 is fed by the two reads that can go unanswered, so its empty state
	// has the same two meanings and must not collapse them into "nothing yet".
	$: movementUnread = unread.some((u) => u.key === 'unread:tasks' || u.key === 'unread:journal');
	$: movement = buildMovement(tasks, journal);
</script>

<div class="page-stack overview">
	<div class="flex items-baseline justify-between gap-s-3 flex-wrap">
		<h1 class="page-title">Overview</h1>
		<div class="flex items-center gap-s-3">
			{#if readAgo}
				<span class="text-xs text-muted num-inline" data-last-read>last read {readAgo}</span>
			{/if}
			<button class="btn btn-ghost text-xs" on:click={() => load(true)} disabled={loading}><Icon name="refresh" />Refresh</button>
		</div>
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
								aria-hidden="true">{#if step.done}<Icon name="check" />{:else}{i + 1}.{/if}</span
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
											>Do this next <Icon name="arrow-right" /></a
										>
									{/if}
								{/if}
							</span>
						</li>
					{/each}
				</ol>
			</section>
		{/if}

		<div class="overview-top">
			<!-- Zone 1. A calm estate gets ONE serif line, not an empty card with a
			     heading and a zero in it - but the line says what it looked at, so
			     a calm screen is a checked one, not a quiet one. -->
			{#if attention.length}
				<section class="panel overview-attention" aria-labelledby="needs-attention">
					<div class="panel-head">
						<h2 class="panel-title flex items-center gap-s-2" id="needs-attention">
							<Lamp
								state={attention.some((a) => a.severity === 'critical')
									? 'crit'
									: attention.some((a) => a.severity === 'warning')
										? 'warn'
										: 'off'}
							/>Needs attention
						</h2>
						<span class="panel-meta" data-attention-meta
							>{attention.length} open · checked {checked.length} of {ATTENTION_SOURCES} sources</span
						>
					</div>
					<div class="flex flex-col py-s-1">
						{#each attention as item (item.key)}
							<AttentionItem
								id={item.key}
								severity={item.severity}
								label={item.label}
								text={item.text}
								href={item.href}
								meta={item.meta}
								since={item.since ?? ''}
								external={item.external ?? false}
								details={item.details ?? []}
							>
								<svelte:fragment slot="actions">
									{#if item.action}
										{@const spec = actionSpec(item.action)}
										{@const act = item.action}
										{#if spec.allowed}
											{#if confirming === item.key && spec.confirm}
												<button
													class="btn {act.kind === 'forget' ? 'btn-danger' : 'btn-primary'} btn-sm text-xs"
													disabled={working === item.key}
													on:click={() => runAction(item.key, act)}
													>{working === item.key ? spec.busy : spec.confirm}</button
												>
												<button class="btn btn-ghost btn-sm text-xs" on:click={() => (confirming = null)}
													>Cancel</button
												>
											{:else}
												<button
													class="btn btn-ghost btn-sm text-xs"
													title={spec.title}
													disabled={working === item.key}
													on:click={() =>
														spec.confirm ? (confirming = item.key) : runAction(item.key, act)}
													>{working === item.key ? spec.busy : spec.verb}</button
												>
											{/if}
										{/if}
									{/if}
								</svelte:fragment>
							</AttentionItem>
						{/each}
					</div>
				</section>
			{:else}
				<p class="overview-calm prose-body" data-calm>
					<Lamp state="ok" />
					<span
						>Nothing needs you - checked <span title={checked.join(', ')}
							>{checked.length} sources</span
						>{readAgo ? ` ${readAgo}` : ''}.</span
					>
				</p>
			{/if}

			<!-- Zone 2: would I know? Two lines and the door to Monitoring. -->
			<AlertStrip firing={firingRead} history={alertHistory} rulesEnabled={d.metrics.rules_enabled} />
		</div>

		<!-- Zone 3: the estate. One readout row where the donuts were (review 01
		     P3-1: they carried little decision value and one was wrong), then the
		     host chips. -->
		<section class="panel" aria-labelledby="estate">
			<div class="panel-head">
				<h2 class="panel-title" id="estate">Estate</h2>
				<a class="text-accent text-xs" href="{base}/hosts">All hosts <Icon name="arrow-right" /></a>
			</div>
			<div class="readouts" style="--readouts: 5">
				<Readout
					label="Hosts"
					value={d.inventory.total}
					unit={d.inventory.templates ? `+${d.inventory.templates} tpl` : ''}
					sub="{d.inventory.managed} managed{d.inventory.uncovered
						? ` · ${d.inventory.uncovered} pending adoption`
						: ''}"
					tone={d.inventory.uncovered ? 'warn' : 'neutral'}
					href="{base}/hosts"
				/>
				{#if unread.some((u) => u.key === 'unread:hosts')}
					<Readout label="Watched by agent" value="?" sub="could not read the host inventory" tone="warn" href="{base}/monitoring/alerts" />
				{:else}
					<!-- A stopped or friend's guest without an agent is a fact, not news:
					     only "nothing watched at all" is painted (the attention list
					     carries the absent agents that ARE news). -->
					<Readout
						label="Watched by agent"
						value={watched}
						unit="/{fleet.length}"
						sub={unread.some((u) => u.key === 'partial:hosts')
							? `of the first ${fleet.length} hosts read`
							: 'hosts whose metrics can alert'}
						tone={fleet.length > 0 && watched === 0 ? 'crit' : 'neutral'}
						href="{base}/monitoring/alerts"
					/>
				{/if}
				<Readout
					label="Agents"
					value={d.agents.connected}
					unit="/{d.agents.known}"
					sub="connected / known"
					tone={d.agents.known > 0 && d.agents.connected === d.agents.known
						? 'ok'
						: d.agents.connected === 0 && d.agents.known > 0
							? 'crit'
							: d.agents.known === 0
								? 'neutral'
								: 'warn'}
					href="{base}/hosts/agents"
				/>
				<!-- P6: nothing checked is NOT 100%. The dash is the honest answer
				     until a check has actually run. -->
				<Readout
					label="In spec"
					value={d.drift.checked > 0 ? d.drift.in_spec : '—'}
					unit={d.drift.checked > 0 ? `/${d.drift.checked}` : ''}
					sub={d.drift.checked === 0
						? 'nothing checked yet'
						: d.drift.unknown
							? `${d.drift.drifted} drifting · ${d.drift.unknown} not established${d.drift.unknown_by_reason?.by_design ? ` (${d.drift.unknown_by_reason.by_design} describe-only)` : ''}`
							: `${d.drift.drifted} drifting`}
					tone={d.drift.checked === 0
						? 'neutral'
						: d.drift.drifted > 0
							? 'crit'
							: d.drift.unknown > 0
								? 'warn'
								: 'ok'}
					href="{base}/changes/drift"
				/>
				<Readout
					label="Changes"
					value={waitingProposed + waitingReady}
					unit="waiting"
					sub="{waitingProposed} proposed · {waitingReady} ready to run"
					href="{base}/changes/inbox"
				/>
			</div>

			{#if fleet.length}
				<!-- The host chips: the door to each machine. One line that scrolls
				     sideways on a phone, so the estate never pushes the feed off the
				     first screen. -->
				<div class="overview-chips" data-host-chips>
					{#each fleet as h (h.id)}
						<a
							href="{base}/hosts/{h.id}"
							class="overview-chip"
							title="{h.hostname}: {h.status ?? 'unknown'}{h.agent_id ? (h.agent_connected ? ', agent connected' : ', agent not connected') : ', no agent'}"
						>
							<Lamp
								state={h.status === 'online' || h.status === 'running'
									? 'ok'
									: h.status === 'offline' || h.status === 'stopped'
										? 'crit'
										: 'off'}
							/>
							<span class="font-mono">{h.hostname}</span>
							{#if h.agent_id && !h.agent_connected}
								<span class="text-muted">no agent</span>
							{/if}
							{#if alertCount(h.hostname)}
								<span class="badge badge-critical">{alertCount(h.hostname)}</span>
							{/if}
						</a>
					{/each}
				</div>
			{/if}
		</section>

		<!-- Zone 3: recent movement. One feed, capped, with a door to the full
		     record rather than an unbounded scroll here. -->
		<section class="panel" aria-labelledby="recent-movement">
			<div class="panel-head">
				<h2 class="panel-title" id="recent-movement">Recent movement</h2>
				<a class="text-accent text-xs" href="{base}/records">Records <Icon name="arrow-right" /></a>
			</div>
			<div class="py-s-1">
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
				<p class="prose-note text-xs px-s-3">Nothing has happened yet.</p>
			{/if}
			</div>
		</section>
	{/if}
</div>

<style>
	/* Attention and the alert strip side by side on a desk (attention takes
	   two thirds), stacked in that order below lg. */
	.overview-top {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: var(--space-section);
		align-items: start;
	}
	@media (min-width: 1024px) {
		.overview-top {
			grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
		}
	}
	/* The calm state is a sentence, set in the reading serif (owner decision
	   6: attention sentences and calm states in Source Serif 4). */
	.overview-calm {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		font-size: 0.9375rem;
		padding: var(--space-2) 0;
	}
	.overview-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 0.375rem;
		padding: var(--space-2) 0.625rem;
		border-top: 1px solid var(--color-divider);
	}
	@media (max-width: 767px) {
		.overview-chips {
			flex-wrap: nowrap;
			overflow-x: auto;
		}
	}
	.overview-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		flex-shrink: 0;
		padding: 0.1875rem 0.5rem;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-sm);
		background: var(--color-surface-raised);
		color: var(--color-text-strong);
		font-size: 0.75rem;
		transition: border-color 120ms ease;
	}
	.overview-chip:hover {
		border-color: var(--color-accent);
	}
</style>
