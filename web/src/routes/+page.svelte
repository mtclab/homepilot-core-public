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
		type DashboardSummary,
		type EnrolmentRequests,
		type AgentUpgrade,
		type AttentionRun,
		type AttentionSeverity,
		type AttentionSnooze,
		type ServerAttention,
	} from '$lib/api';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import { shortTaskId } from '$lib/taskStatus';
	import { canWrite as capCanWrite, isAdmin as capIsAdmin } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { destination, resolvedEpisodes } from '$lib/alertHistory';
	import {
		dayMonth,
		forgetAll,
		isRunning,
		leftOutSentence,
		plural,
		putOffSentence,
		snoozeHolds,
		upgradeInTurn,
		type UpgradeState,
	} from '$lib/attention';
	import { describeJournal, sentence } from '$lib/journalWords';
	import Readout from '$lib/components/Readout.svelte';
	import Lamp from '$lib/components/Lamp.svelte';
	import Icon from '$lib/components/Icon.svelte';

	// Overview is the operator's morning glance (#549 F2; UI slice 4; made quiet
	// in 3.12 after the owner counted 14 attention lines on prod, 3 of them
	// decisions). Three zones, no tabs:
	//
	//   1. Needs you - a line only for what the operator can act on, as a
	//      sentence with its action(s) on the line; every line can be put off
	//      for a week. What was left out is counted in the footer, with the way
	//      back to it. "Nothing needs you." and what was checked when calm.
	//   2. Estate - four numbers (machines, watched, alerts, changes) and the
	//      RUNNING machines as chips; gone machines never appear.
	//   3. What happened - people and assistants in words; the system's own
	//      work (reconcilers) behind a toggle with its count.
	//
	// The rules that need the server's knowledge - which failed runs a later run
	// superseded, the machines Proxmox no longer reports, agents behind, the
	// snoozes - come from the summary's `attention` (dashboard/attention.py), so
	// this page and an assistant's get_dashboard_summary cannot disagree. The
	// lines built here come from the same reads the pages they link to make, and
	// every count is over that page's set - the drift count over ACTIVE
	// artifacts, as the drift page does.

	let d: DashboardSummary | null = null;
	// What waits in the Changes Inbox: proposals AND approved changes not yet
	// run (#805). Counting proposals alone made an approved change vanish from
	// the Estate readout, the "lost change" the owner hit (3.10.0).
	$: waitingProposed = d?.changes?.proposed ?? d?.artifacts?.proposed ?? 0;
	$: waitingReady = d?.changes?.ready ?? 0;
	let error = '';
	let loading = true;

	// The machine strip: one chip per RUNNING machine - state, agent, firing
	// alerts - so glancing is Overview and digging is the host page.
	let fleet: Host[] = [];
	let firing: FiringAlert[] = [];
	// The Alerts number's two reads, `null` when they did not come back - it
	// then says "could not read", never "0 firing".
	let firingRead: FiringAlert[] | null = null;
	let alertHistory: AlertHistory | null = null;
	// When the page last finished reading, for "last read 12 s ago" and the
	// calm line's "checked N sources".
	let readAt = 0;
	let now = Date.now();
	// "What happened": people and assistants, and - behind the toggle - the
	// system's own work today, with how much of it there was.
	let journal: AuditEntry[] = [];
	let systemToday: AuditEntry[] = [];
	let systemTotal = 0;
	let showSystem = false;
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

	// How much of the fleet the page read (#704). The per-host lines (an agent
	// offline) and the strip come from the first INVENTORY_PAGE machines; past
	// that the Estate panel says how many it looked at. Not an attention line:
	// it is the page's bookkeeping, not something the operator can act on (3.12).
	const INVENTORY_PAGE = 500;
	// The failed and stuck runs are the server's (summary.attention), over the
	// whole task table - this page no longer reads a slice of tasks.
	const JOURNAL_PAGE = 30;
	const SYSTEM_PAGE = 30;
	let fleetTotal = 0;
	let unread: Unread[] = [];
	/** The attention sources that answered on the last read. */
	let checked: string[] = [];
	/** Every source the attention list is built from (summary first). */
	const ATTENTION_SOURCES = 6;
	// Where every subsystem line (and the self-check's own unread line) goes.
	const SUBSYSTEMS_HREF = `${base}/settings?tab=subsystems`;

	/** Local midnight, ISO: "today" for the system-activity count. */
	function startOfToday(): string {
		const t = new Date();
		t.setHours(0, 0, 0, 0);
		return t.toISOString().replace(/\.\d{3}Z$/, 'Z');
	}

	async function load(force = false) {
		loading = true;
		try {
			d = await api.getDashboard();
			const selfcheckFresh = !force && selfcheck && Date.now() - selfcheckAt < SELFCHECK_TTL_MS;
			const [inv, alerts, auditRes, systemRes, checkRes, joinRes, historyRes] = await Promise.allSettled([
				// Templates are provisioning inputs, not machines of the fleet (B1).
				api.listInventory({ limit: INVENTORY_PAGE, templates: 'exclude' }),
				api.listFiringAlerts(),
				api.listAudit({ actor: 'people', limit: JOURNAL_PAGE }),
				api.listAudit({ actor: 'system', since: startOfToday(), limit: SYSTEM_PAGE }),
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
			fleetTotal = inv.status === 'fulfilled' ? (inv.value.total ?? fleet.length) : 0;
			firing = alerts.status === 'fulfilled' ? alerts.value.items : [];
			// A degraded side call must not blank the zone it feeds AND must not
			// leave stale rows claiming to be current: empty is the honest answer.
			journal = auditRes.status === 'fulfilled' ? (auditRes.value.items ?? []) : [];
			systemToday = systemRes.status === 'fulfilled' ? (systemRes.value.items ?? []) : [];
			systemTotal = systemRes.status === 'fulfilled' ? (systemRes.value.total ?? systemToday.length) : 0;
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
					text: 'Could not read the journal, so What happened is incomplete',
					href: `${base}/records/journal`,
				});
			// Which sources the attention list was built from, by name - the calm
			// line says how many it looked at (review 01, 3.5), and an unread one
			// is already its own line above.
			checked = [
				'the dashboard summary',
				...(inv.status === 'fulfilled' ? ['the host inventory'] : []),
				...(alerts.status === 'fulfilled' ? ['firing alerts'] : []),
				...(checkRes.status === 'fulfilled' ? ['the self-check'] : []),
				...(joinRes.status === 'fulfilled' ? ['enrolment requests'] : []),
				...(auditRes.status === 'fulfilled' ? ['the journal'] : []),
			];
			readAt = Date.now();
			now = readAt;
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
		severity: AttentionSeverity;
		/** The sentence the operator reads - and the door to where it is fixed. */
		text: string;
		/** One small line under it: what the sentence is about, by name. */
		sub?: string;
		href: string;
		meta: string;
		/** When the condition began (ISO), where the source says; never guessed. */
		since?: string | null;
		/** What this line lets the operator do without leaving the page. */
		actions?: AttentionAction[];
		/** Doors that are actions in their own right ("Review"). */
		links?: { text: string; href: string }[];
		/** The fix is outside the console (release notes): open it in a new tab. */
		external?: boolean;
		/** What this line's one subject asks of the operator (operator actions). */
		details?: string[];
		/** A line that cannot be put off: a read that failed is not a choice. */
		fixed?: boolean;
	}

	/**
	 * The actions an attention line can complete without leaving the page.
	 * The mutating ones ask first in place; the page re-reads afterwards so a
	 * line goes (or stays) by what the server now says.
	 */
	type AttentionAction =
		| { kind: 'open-window' }
		| { kind: 'recheck-drift' }
		| { kind: 'cancel-task'; taskId: string; what: string }
		| { kind: 'forget-all'; hosts: { id: string; hostname: string }[] }
		| { kind: 'upgrade-agents'; agents: AgentUpgrade[] }
		| { kind: 'reload' };

	// A host's agent dark this long is no reconnect blip.
	const AGENT_CRITICAL_MS = 60 * 60 * 1000;
	// Per KIND, so one bad night of alerts cannot bury the drift line.
	const PER_KIND = 4;

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

	/** "20:18" today, "8 Nov" before: when, for the What happened list. */
	function clock(iso: string): string {
		const t = new Date(iso);
		if (Number.isNaN(t.getTime())) return '';
		const today = new Date();
		return t.toDateString() === today.toDateString()
			? t.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
			: dayMonth(iso);
	}

	/** "web-01, db-01, cache and 4 more": names on a grouped line. */
	function names(list: string[], shown = 6): string {
		if (list.length <= shown) return list.join(', ');
		return `${list.slice(0, shown).join(', ')} and ${list.length - shown} more`;
	}

	// --- The server's lines (summary.attention), in words ------------------

	function runTarget(r: AttentionRun): string {
		return r.artifact_id ?? r.hostname ?? shortTaskId(r.task_id);
	}

	/** Where a run is fixed: its change, or the task list when it has none. */
	function runHref(r: AttentionRun): string {
		return r.artifact_id ? `${base}/changes/${r.artifact_id}` : `${base}/records/tasks`;
	}

	function failedLine(r: AttentionRun): Attention {
		const at = r.finished_at ?? r.created_at;
		return {
			key: r.key,
			severity: r.severity,
			text: `${r.action} ${runTarget(r)} failed${r.error ? `: ${r.error}` : ''}`,
			href: runHref(r),
			meta: ago(at),
			since: at,
		};
	}

	function stuckLine(r: AttentionRun): Attention {
		const what = `${r.action} ${runTarget(r)}`;
		return {
			key: r.key,
			severity: r.severity,
			text: `${what} is still ${r.status}`,
			href: runHref(r),
			meta: ago(r.created_at),
			since: r.created_at,
			actions: [{ kind: 'cancel-task', taskId: r.task_id, what }],
		};
	}

	function goneLine(g: NonNullable<ServerAttention['gone']>): Attention {
		const n = g.hosts.length;
		return {
			key: g.key,
			severity: g.severity,
			text: `${plural(n, 'machine no longer exists', 'machines no longer exist')} in Proxmox`,
			sub: `${names(g.hosts.map((h) => h.hostname))} · their history stays in Records`,
			// Review is the door; the sentence is not a second link to it.
			href: '',
			meta: '',
			links: [{ text: 'Review', href: `${base}/hosts?gone=1` }],
			actions: [{ kind: 'forget-all', hosts: g.hosts.map((h) => ({ id: h.id, hostname: h.hostname })) }],
		};
	}

	function agentsLine(a: NonNullable<ServerAttention['agents_behind']>): Attention {
		const n = a.agents.length;
		const silent = a.agents.some((x) => x.behind !== true);
		const versions = [...new Set(a.agents.map((x) => x.agent_version ?? 'no version'))];
		return {
			key: a.key,
			severity: a.severity,
			text:
				`${plural(n, 'agent is', 'agents are')} older than HomePilot ${a.control_version}` +
				(silent ? ' or do not say their version' : ''),
			sub: `${names(a.agents.map((x) => x.hostname))} · on ${versions.join(', ')}`,
			href: `${base}/hosts/agents`,
			meta: '',
			actions: [{ kind: 'upgrade-agents', agents: a.agents }],
		};
	}

	/** A server line that is put off, in the same words it would be said in. */
	function serverLine(kind: string, line: Record<string, unknown>): Attention | null {
		if (kind === 'failed_run') return failedLine(line as unknown as AttentionRun);
		if (kind === 'stuck_run') return stuckLine(line as unknown as AttentionRun);
		if (kind === 'gone') return goneLine(line as unknown as NonNullable<ServerAttention['gone']>);
		if (kind === 'agents_behind')
			return agentsLine(line as unknown as NonNullable<ServerAttention['agents_behind']>);
		return null;
	}

	function hostHref(hostname: string): string {
		const h = fleet.find((x) => x.hostname === hostname);
		return h ? `${base}/hosts/${h.id}` : `${base}/hosts`;
	}

	/** `items` capped at PER_KIND, with an honest "N more" door on the tail. */
	function capped(items: Attention[], moreText: string, moreHref: string): Attention[] {
		if (items.length <= PER_KIND) return items;
		const hidden = items.length - PER_KIND;
		return [
			...items.slice(0, PER_KIND),
			{
				key: `${moreHref}:more`,
				severity: items[0].severity,
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
				text: u.text,
				href: u.href,
				meta: '',
				fixed: true,
				actions: [{ kind: 'reload' }],
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
						text: c.summary,
						// The fix, under the sentence: the line stays one sentence long.
						sub: c.remedy,
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
				text: `${plural(waiting, 'host is', 'hosts are')} waiting to join - open an enrolment window on Hosts > Agents to let ${waiting === 1 ? 'it' : 'them'} in`,
				href: `${base}/hosts/agents`,
				meta: lasting(firstKnock),
				since: firstKnock,
				actions: [{ kind: 'open-window' }],
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
				text: `The host marked as HomePilot's own (${me.hostname}) is gone from Proxmox - confirm which host it runs on now`,
				href: `${base}/hosts`,
				meta: '',
			});
		} else if (me && !me.has_agent) {
			out.push({
				key: 'self:no_agent',
				severity: 'warning',
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
				text: `${plural(summary.drift.drifted, 'artifact disagrees', 'artifacts disagree')} with the host`,
				href: `${base}/changes/drift`,
				meta: '',
				actions: [{ kind: 'recheck-drift' }],
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
					text:
						`${plural(summary.drift.unknown, 'artifact', 'artifacts')} unverified` +
						(parts.length ? `: ${parts.join(', ')}` : ''),
					href: `${base}/changes/drift`,
					meta: '',
					// A failed check or a missing Proxmox link may have cleared.
					actions: n('check_failed') || n('no_proxmox') || n('other') ? [{ kind: 'recheck-drift' }] : undefined,
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
			// "Agents are OLDER than this control plane" is the grouped agents line
			// below, with its Upgrade action - not a second line in other words.
			if (sub.name === 'agent_versions' && sub.state === 'unreachable' && summary.attention) continue;
			out.push({
				key: `selfcheck:${sub.name}`,
				// An agent behind the hub is serious but not an outage; the others
				// unreachable mean a whole function of the product is gone.
				severity:
					sub.state === 'unreachable' && sub.name !== 'agent_versions' ? 'critical' : 'warning',
				text: sub.consequence,
				href: SUBSYSTEMS_HREF,
				meta: '',
			});
		}

		// Failed runs: the server's (dashboard/attention.py), over the whole task
		// table. A failure a later run of the same change superseded is not
		// here, nor one older than a day: both are counted in the footer and
		// stay in Records. A failed apply is fixed on its change page.
		const server = summary.attention;
		out.push(...capped((server?.failed_runs ?? []).map(failedLine), 'failed runs', `${base}/records/tasks`));

		// The state an empty alert list cannot distinguish itself from. `0 firing`
		// on an install with no rules is not good news, it is the absence of news,
		// and it sat on the first screen reading green (#642, #648 tranche 5).
		if (summary.metrics.rules_enabled === 0) {
			out.push({
				key: 'alerts:none',
				severity: 'warning',
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
					text: `${plural(thresholdInert, 'alert rule matches', 'alert rules match')} no host - check the metric name and the host filter`,
					href: `${base}/monitoring/rules`,
					meta: '',
				});
			if (darkInert > 0)
				out.push({
					key: 'alerts:inert:nodata',
					severity: 'warning',
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
					text: `${h.hostname}: agent enrolled but not connected`,
					href: `${base}/hosts/${h.id}`,
					meta: lasting(h.agent_offline_since),
					since: h.agent_offline_since,
				})),
				'hosts with an absent agent',
				`${base}/hosts`,
			),
		);

		// Machines Proxmox stopped reporting: ONE line, however many (3.12 - prod
		// had seven, each with its own Forget). The server applies the Hosts
		// page's membership rule, and has already forgotten the ones HomePilot
		// destroyed itself through an applied change.
		if (server?.gone) out.push(goneLine(server.gone));

		// Stuck: still pending or running long after it should have finished
		// (the server's; critical after an hour). A task that never left
		// `pending` is stuck too - it is the one that never even started (#704).
		out.push(...capped((server?.stuck_runs ?? []).map(stuckLine), 'long-running tasks', `${base}/records/tasks`));

		// Agents older than this HomePilot: one line, one action that upgrades
		// them in turn (the self-check's "behind" line is this one).
		if (server?.agents_behind) out.push(agentsLine(server.agents_behind));

		// Waiting on a human: not a fault, but nothing moves until it is done.
		const proposed = summary.changes?.proposed ?? summary.artifacts?.proposed ?? 0;
		if (proposed > 0) {
			out.push({
				key: 'review',
				severity: 'notice',
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
				text: `${plural(expired, 'approved change has', 'approved changes have')} an approval too old to run on - review ${expired === 1 ? 'it' : 'them'} again in the Inbox`,
				href: `${base}/changes/inbox`,
				meta: '',
			});
		}
		if (ready > 0) {
			out.push({
				key: 'ready',
				severity: 'notice',
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

	// Every line action follows one shape (plan 5.3): the mutating ones turn
	// their button into [Confirm verb] [Cancel] in place, the working one says
	// what it is doing, and the page re-reads afterwards so the line goes (or
	// stays) by what the server now says - never by what the click hoped.
	let confirming: string | null = null;
	let working: string | null = null;
	/** What an action left to say on its line: who could not be forgotten, where an upgrade stopped. */
	let lineNote: Record<string, string> = {};
	/** "Upgrade N agents", host by host, while it runs and after it stops. */
	let upgradeProgress: Record<string, { hostname: string; state: UpgradeState; note?: string }> = {};

	async function runAction(key: string, act: AttentionAction) {
		working = key;
		lineNote = { ...lineNote, [key]: '' };
		try {
			if (act.kind === 'forget-all') {
				// The Hosts page's forget, per machine the line names; every one is
				// tried, and the ones that could not be forgotten are named.
				const res = await forgetAll(act.hosts, (id) => api.forgetHost(id));
				if (res.failed.length) {
					lineNote = {
						...lineNote,
						[key]:
							`Forgot ${res.forgotten.length} of ${act.hosts.length}. Could not forget ` +
							res.failed.map((f) => `${f.hostname} (${f.error})`).join(', '),
					};
					notify(`Could not forget ${plural(res.failed.length, 'machine', 'machines')}`, 'err');
				} else {
					notify(`Forgot ${plural(res.forgotten.length, 'machine', 'machines')}`, 'ok');
				}
			} else if (act.kind === 'upgrade-agents') {
				upgradeProgress = Object.fromEntries(
					act.agents.map((a) => [a.agent_id, { hostname: a.hostname, state: 'waiting' as UpgradeState }]),
				);
				const res = await upgradeInTurn(
					act.agents,
					{ install: (hostId) => api.installAgent(hostId, true), getTask: (id) => api.getTask(id) },
					(agentId, state, note) => {
						upgradeProgress = { ...upgradeProgress, [agentId]: { ...upgradeProgress[agentId], state, note } };
					},
				);
				if (res.stopped) {
					lineNote = {
						...lineNote,
						[key]: `Stopped at ${res.stopped.hostname}: ${res.stopped.error}. ${plural(res.done.length, 'agent was', 'agents were')} upgraded before it; the rest were not tried.`,
					};
					notify(`Upgrade stopped at ${res.stopped.hostname}`, 'err');
				} else {
					upgradeProgress = {};
					notify(`Upgraded ${plural(res.done.length, 'agent', 'agents')}`, 'ok');
				}
			} else if (act.kind === 'open-window') {
				await api.openEnrolmentWindow(15);
				notify('Enrolment window open for 15 min - waiting hosts join on their next retry', 'ok');
			} else if (act.kind === 'recheck-drift') {
				const res = await api.getDriftStatus({ refresh: true });
				const drifted = (res.items ?? []).filter((c) => (c.state ? c.state === 'drifted' : c.drifted)).length;
				notify(`Drift re-checked: ${plural(drifted, 'artifact disagrees', 'artifacts disagree')} with the host`, 'ok');
			} else if (act.kind === 'cancel-task') {
				await api.cancelTask(act.taskId);
				notify(`Cancelled ${act.what}`, 'ok');
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
		danger?: boolean;
	} {
		switch (act.kind) {
			case 'forget-all':
				return {
					verb: `Forget all ${act.hosts.length}`,
					busy: 'Forgetting...',
					confirm: `Confirm forget ${act.hosts.length}`,
					title: `Remove ${names(act.hosts.map((h) => h.hostname))} from inventory, with their services and notes`,
					allowed: canWrite,
					danger: true,
				};
			case 'upgrade-agents': {
				const n = act.agents.filter((a) => a.host_id).length;
				return {
					verb: `Upgrade ${plural(n, 'agent', 'agents')}`,
					busy: 'Upgrading...',
					confirm: `Confirm upgrade of ${n}`,
					title: 'Reinstall the current agent on each host in turn; stops at the first one that fails',
					allowed: admin && n > 0,
				};
			}
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
					title: `Cancel ${act.what}`,
					allowed: canWrite,
				};
			case 'reload':
				return { verb: 'Read again', busy: 'Reading...', confirm: null, title: 'Read every source again', allowed: true };
		}
	}

	// "Not now, 7 days" (3.12): a server-side snooze, so every browser and an
	// assistant see the same quiet list. Recorded with the line's severity: if
	// it becomes more serious, it shows again at once.
	async function snoozeLine(item: Attention) {
		working = `snooze:${item.key}`;
		try {
			const s = await api.snoozeAttention(item.key, item.severity);
			notify(`Put off until ${dayMonth(s.until)}`, 'ok');
			await load(true);
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			working = null;
		}
	}

	async function bringBack(key: string) {
		working = `unsnooze:${key}`;
		try {
			await api.unsnoozeAttention(key);
			await load(true);
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			working = null;
		}
	}

	function lampOf(sev: AttentionSeverity): 'crit' | 'warn' | 'accent' {
		return sev === 'critical' ? 'crit' : sev === 'warning' ? 'warn' : 'accent';
	}

	// --- What happened ------------------------------------------------------
	const FEED_LIMIT = 8;
	let showLeftOut = false;

	$: doneCount = d ? d.onboarding.steps.filter((s) => s.done).length : 0;
	$: nextStep = d ? d.onboarding.steps.find((s) => !s.done) : undefined;
	$: allLines = buildAttention(d, fleet, firing, unread, selfcheck, joining);
	$: snoozes = new Map<string, AttentionSnooze>((d?.attention?.snoozes ?? []).map((s) => [s.key, s]));
	// A line a snooze holds leaves the list for the footer; a line that became
	// more serious than when it was put off does not (snoozeHolds).
	$: attention = allLines.filter((l) => l.fixed || !snoozeHolds(snoozes.get(l.key), l.severity, now));
	$: putOff = [
		...(d?.attention?.put_off ?? [])
			.map((p) => ({ line: serverLine(p.kind, p.line), snooze: p.snooze }))
			.filter((p): p is { line: Attention; snooze: AttentionSnooze } => p.line !== null),
		...allLines
			.filter((l) => !l.fixed && snoozeHolds(snoozes.get(l.key), l.severity, now))
			.map((l) => ({ line: l, snooze: snoozes.get(l.key) as AttentionSnooze })),
	];
	$: leftOut = leftOutSentence(d?.attention?.left_out);
	$: putOffText = putOffSentence(putOff.map((p) => p.snooze));
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	$: admin = capIsAdmin($sessionStore?.capabilities);
	$: readAgo = readAt ? secondsAgo(readAt, now) : '';
	// The Estate panel: the server's counts (gone never among them), the strip
	// of RUNNING machines only from the inventory read.
	$: machines = d?.inventory.machines;
	$: running = fleet.filter(isRunning);
	$: stoppedCount = machines?.stopped ?? fleet.filter((h) => !h.absent_since && h.pve_status === 'stopped').length;
	$: firingCount = firingRead === null ? null : firingRead.length;
	$: resolvedToday = alertHistory ? resolvedEpisodes(alertHistory.items ?? []).length : null;
	$: dest = alertHistory ? destination(alertHistory.channels ?? []) : null;
	// Where an alert goes, in a few words for the number's small line; the
	// full sentence is Monitoring > Alerts'.
	$: channelsShort = alertHistory
		? (() => {
				const live = (alertHistory.channels ?? []).filter((c) => c.enabled);
				if (!live.length) return 'no channel set up';
				const word = { ok: 'ok', failing: 'FAILED', untested: 'untested', disabled: 'off' } as const;
				return live.map((c) => `${c.name} ${word[c.state]}`).join(', ');
			})()
		: '';
	$: waiting = waitingProposed + waitingReady;
	// What happened: the journal's people and assistants, in words.
	$: journalUnread = unread.some((u) => u.key === 'unread:journal');
	// A sign-in is not something that happened to the estate: it stays in
	// Records and out of this list (every screenshot run filled it).
	$: happenings = describeJournal(
		journal.filter((e) => e.action !== 'login'),
		$sessionStore,
		base,
	).slice(0, FEED_LIMIT);
	$: systemHappenings = describeJournal(systemToday, $sessionStore, base);
	$: calmFacts = [
		`${plural(checked.length, 'source', 'sources')} checked${readAgo ? ` ${readAgo}` : ''}`,
		d && d.agents.known > 0 ? `${d.agents.connected} of ${d.agents.known} agents connected` : '',
		firingCount === 0 ? 'no alert firing' : '',
		d?.last_applied_at ? `last change applied ${ago(d.last_applied_at)}` : '',
	].filter(Boolean);
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

		<!-- Zone 1: Needs you. A line only for what the operator can act on, as a
		     sentence with its action(s) on the line; the footer says what was
		     left out and how to get it back. Calm is one serif sentence that says
		     what it looked at, so a calm screen is a checked one. -->
		<section class="panel overview-attention" aria-labelledby="needs-you">
			<div class="panel-head">
				<h2 class="panel-title" id="needs-you">Needs you</h2>
				<span class="panel-meta" data-attention-meta
					>{attention.length
						? `${plural(attention.length, 'thing', 'things')} · checked ${checked.length} of ${ATTENTION_SOURCES} sources`
						: `checked ${checked.length} of ${ATTENTION_SOURCES} sources`}</span
				>
			</div>
			{#if attention.length}
				<ul class="attn-rows">
					{#each attention as item (item.key)}
						<li class="attn-row" data-attention={item.key} data-severity={item.severity}>
							<Lamp state={lampOf(item.severity)} class="attn-lamp" />
							<div class="attn-main">
								<!-- The sentence, what it is about and since when: one door to
								     where it is fixed (a release's notes open in a new tab). -->
								<svelte:element
									this={item.href ? 'a' : 'div'}
									class="attn-say"
									href={item.href || undefined}
									target={item.href && item.external ? '_blank' : undefined}
									rel={item.href && item.external ? 'noopener noreferrer' : undefined}
								>
									<span class="sr-only">{item.severity}:</span>
									<span class="attn-text">{item.text}</span>
									{#if item.sub || item.meta}
										<small class="attn-sub"
											>{item.sub ?? ''}{item.sub && item.meta ? ' · ' : ''}{#if item.meta}{#if item.since}<time
														datetime={item.since}
														title={new Date(item.since).toLocaleString()}>{item.meta}</time
													>{:else}{item.meta}{/if}{/if}</small
										>
									{/if}
									{#if item.details?.length}
										<ul class="attn-details">
											{#each item.details as detail}<li>{detail}</li>{/each}
										</ul>
									{/if}
								</svelte:element>
								{#if item.actions?.some((a) => a.kind === 'upgrade-agents') && Object.keys(upgradeProgress).length}
									<ul class="attn-progress" data-upgrade-progress>
										{#each Object.entries(upgradeProgress) as [agentId, p] (agentId)}
											<li data-upgrade-state={p.state}>
												<Lamp
													state={p.state === 'done' ? 'ok' : p.state === 'failed' ? 'crit' : p.state === 'upgrading' ? 'accent' : 'off'}
												/>
												<span class="font-mono">{p.hostname}</span>
												<span>{p.state === 'upgrading' ? 'upgrading...' : p.state === 'done' ? 'upgraded' : p.state === 'failed' ? 'failed' : p.state === 'skipped' ? `skipped: ${p.note ?? ''}` : 'waiting'}</span>
											</li>
										{/each}
									</ul>
								{/if}
								{#if lineNote[item.key]}
									<p class="attn-note" role="status">{lineNote[item.key]}</p>
								{/if}
							</div>
							<div class="attn-acts">
								{#each item.links ?? [] as l}
									<a class="btn btn-ghost btn-sm text-xs" href={l.href}>{l.text}</a>
								{/each}
								{#each item.actions ?? [] as act}
									{@const spec = actionSpec(act)}
									{#if spec.allowed}
										{#if confirming === item.key && spec.confirm}
											<button
												class="btn {spec.danger ? 'btn-danger' : 'btn-primary'} btn-sm text-xs"
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
												on:click={() => (spec.confirm ? (confirming = item.key) : runAction(item.key, act))}
												>{working === item.key ? spec.busy : spec.verb}</button
											>
										{/if}
									{/if}
								{/each}
								{#if admin && !item.fixed}
									<button
										class="btn btn-ghost btn-sm text-xs"
										title="Hide this line for 7 days, or until it becomes more serious"
										disabled={working === `snooze:${item.key}`}
										on:click={() => snoozeLine(item)}>Not now, 7 days</button
									>
								{/if}
							</div>
						</li>
					{/each}
				</ul>
			{:else}
				<p class="overview-calm" data-calm>
					<span class="calm-say">Nothing needs you.</span>
					<small class="attn-sub" title={checked.join(', ')}>{calmFacts.join(' · ')}</small>
				</p>
			{/if}
			{#if leftOut || putOff.length}
				<div class="attn-foot" data-attention-footer>
					<span>{leftOut}{leftOut && putOffText ? ' ' : ''}{putOffText ? `${putOffText}.` : ''}</span>
					<button class="link-btn" aria-expanded={showLeftOut} on:click={() => (showLeftOut = !showLeftOut)}
						>{showLeftOut ? 'Hide what was left out' : 'Show what was left out'}</button
					>
				</div>
				{#if showLeftOut}
					<ul class="attn-left" data-left-out>
						{#if leftOut}
							<li><a class="text-accent" href="{base}/records/tasks">The failed runs, in Records</a></li>
						{/if}
						{#each putOff as p (p.line.key)}
							<li data-put-off={p.line.key}>
								<span class="attn-left-text">{p.line.text}</span>
								<small class="attn-sub">put off until {dayMonth(p.snooze.until)} by {p.snooze.by}</small>
								{#if admin}
									<button
										class="btn btn-ghost btn-sm text-xs"
										disabled={working === `unsnooze:${p.line.key}`}
										on:click={() => bringBack(p.line.key)}>Bring back</button
									>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
		</section>

		<div class="overview-cols">
			<!-- Zone 2: the estate. Four numbers, then the RUNNING machines as
			     chips - a deleted machine is never one of them. -->
			<section class="panel" aria-labelledby="estate">
				<div class="panel-head">
					<h2 class="panel-title" id="estate">Estate</h2>
					<a class="text-accent text-xs" href="{base}/hosts">All hosts <Icon name="arrow-right" /></a>
				</div>
				<div class="readouts" style="--readouts: 4">
					<Readout
						label="Machines"
						value={machines ? machines.running + machines.stopped + machines.other : running.length}
						sub={machines
							? `${machines.running} running · ${machines.stopped} stopped${machines.other ? ` · ${machines.other} unknown` : ''}`
							: `${running.length} running`}
						href="{base}/hosts"
					/>
					{#if unread.some((u) => u.key === 'unread:hosts') && !machines}
						<Readout label="Watched" value="?" sub="could not read the host inventory" tone="warn" href="{base}/hosts/agents" />
					{:else}
						<!-- A stopped or friend's guest without an agent is a fact, not
						     news: only "nothing watched at all" is painted. -->
						<Readout
							label="Watched"
							value={machines?.watched ?? running.filter((h) => h.agent_connected === true).length}
							unit="/{machines?.running ?? running.length}"
							sub="running machines with an agent"
							tone={(machines?.running ?? running.length) > 0 && (machines?.watched ?? 0) === 0 ? 'crit' : 'neutral'}
							href="{base}/hosts/agents"
						/>
					{/if}
					<Readout
						label="Alerts"
						value={firingCount ?? '?'}
						sub={firingCount === null
							? 'could not read what is firing'
							: [
									resolvedToday !== null ? `${resolvedToday} resolved in 24 h` : '',
									d.metrics.rules_enabled === 0 ? 'no rule enabled' : '',
									channelsShort,
								]
									.filter(Boolean)
									.join(' · ')}
						tone={firingCount === null ? 'warn' : firingCount > 0 || dest?.tone === 'danger' ? 'crit' : 'neutral'}
						href="{base}/monitoring/alerts"
					/>
					<Readout
						label="Changes"
						value={waiting}
						sub={waiting ? `${waitingProposed} to review · ${waitingReady} ready to run` : 'waiting for you'}
						href="{base}/changes/inbox"
					/>
				</div>

				{#if running.length || stoppedCount}
					<div class="overview-chips" data-host-chips aria-label="Running machines">
						{#each running as h (h.id)}
							<a
								href="{base}/hosts/{h.id}"
								class="overview-chip"
								title="{h.hostname}: running{h.agent_id ? (h.agent_connected ? ', agent connected' : ', agent not connected') : ', no agent'}"
							>
								<Lamp state={h.agent_connected ? 'ok' : 'off'} />
								<span class="font-mono">{h.hostname}</span>
								{#if h.agent_id && !h.agent_connected}
									<span class="text-muted">no agent</span>
								{/if}
								{#if alertCount(h.hostname)}
									<span class="badge badge-critical">{alertCount(h.hostname)}</span>
								{/if}
							</a>
						{/each}
						{#if stoppedCount}
							<a class="text-accent text-xs self-center" href="{base}/hosts?pve_status=stopped" data-stopped-link
								>{stoppedCount} stopped</a
							>
						{/if}
					</div>
				{/if}
				{#if fleetTotal > fleet.length}
					<p class="prose-note text-xs px-s-3 pb-s-2">Chips for the first {fleet.length} of {fleetTotal} machines; the rest are on Hosts.</p>
				{/if}
			</section>

			<!-- Zone 3: what happened - people and assistants, in words; the
			     system's own work behind a toggle. -->
			<section class="panel" aria-labelledby="what-happened">
				<div class="panel-head">
					<h2 class="panel-title" id="what-happened">What happened</h2>
					<a class="text-accent text-xs" href="{base}/records">Records <Icon name="arrow-right" /></a>
				</div>
				{#if happenings.length}
					<ul class="moves" data-happened>
						{#each happenings as h (h.key)}
							<li class="move">
								<time datetime={h.at} title={new Date(h.at).toLocaleString()}>{clock(h.at)}</time>
								<a href={h.href} class="move-say" title={sentence(h)}
									>{#each h.parts as part}{#if part.strong}<b>{part.text}</b>{:else}{part.text}{/if}{/each}</a
								>
							</li>
						{/each}
					</ul>
				{:else if journalUnread}
					<p class="prose-note text-xs px-s-3 py-s-2">
						This list is empty because the journal could not be read, not because nothing happened.
					</p>
				{:else}
					<p class="prose-note text-xs px-s-3 py-s-2">Nobody has done anything yet.</p>
				{/if}
				{#if showSystem}
					<ul class="moves moves-system" data-system-activity>
						{#each systemHappenings as h (h.key)}
							<li class="move">
								<time datetime={h.at}>{clock(h.at)}</time>
								<a href={h.href} class="move-say"
									>{#each h.parts as part}{#if part.strong}<b>{part.text}</b>{:else}{part.text}{/if}{/each}</a
								>
							</li>
						{/each}
						{#if systemTotal > systemToday.length}
							<li class="move"><span></span><a class="text-accent text-xs" href="{base}/records/journal">{systemTotal - systemToday.length} more in Records</a></li>
						{/if}
					</ul>
				{/if}
				<div class="attn-foot">
					<span>People and assistants only.</span>
					<button class="link-btn" aria-expanded={showSystem} on:click={() => (showSystem = !showSystem)}
						>{showSystem ? 'Hide system activity' : `Show system activity (${systemTotal} today)`}</button
					>
				</div>
			</section>
		</div>
	{/if}
</div>

<style>
	/* Needs you: one row per line - lamp, the sentence in the reading serif
	   with one small line under it, the actions at the right end. On a phone
	   the actions wrap under the sentence (owner decision 6; UI plan 5.2). */
	.attn-rows {
		display: flex;
		flex-direction: column;
	}
	.attn-row {
		display: grid;
		grid-template-columns: 0.5625rem minmax(0, 1fr) auto;
		gap: var(--space-1) 0.625rem;
		align-items: baseline;
		padding: 0.5625rem 0.75rem;
		border-top: 1px solid var(--color-divider);
	}
	.attn-row:first-child {
		border-top: 0;
	}
	.attn-row :global(.attn-lamp) {
		align-self: center;
	}
	.attn-main {
		min-width: 0;
	}
	.attn-say {
		display: block;
		min-width: 0;
		color: inherit;
	}
	.attn-text {
		font-family: var(--font-serif);
		font-size: 0.9375rem;
		line-height: 1.4;
		color: var(--color-text-strong);
		overflow-wrap: anywhere;
	}
	a.attn-say:hover .attn-text {
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.attn-sub {
		display: block;
		margin-top: 0.125rem;
		font-size: 0.75rem;
		line-height: 1.45;
		color: var(--color-muted);
		overflow-wrap: anywhere;
	}
	.attn-details,
	.attn-progress {
		margin-top: var(--space-1);
		font-size: 0.75rem;
		color: var(--color-muted);
	}
	.attn-details {
		padding-left: var(--space-4);
		list-style: disc;
		font-family: var(--font-serif);
	}
	.attn-progress li {
		display: flex;
		align-items: center;
		gap: var(--space-2);
	}
	.attn-note {
		margin-top: var(--space-1);
		font-size: 0.75rem;
		color: var(--color-danger);
		overflow-wrap: anywhere;
	}
	.attn-acts {
		display: flex;
		flex-wrap: wrap;
		justify-content: flex-end;
		gap: 0.375rem;
	}
	.attn-foot {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		gap: var(--space-1) var(--space-2);
		padding: 0.4375rem 0.75rem;
		border-top: 1px solid var(--color-divider);
		font-size: 0.75rem;
		color: var(--color-muted);
	}
	.link-btn {
		color: var(--color-accent);
		font-size: 0.75rem;
		background: none;
		border: 0;
		padding: 0;
		cursor: pointer;
	}
	.link-btn:hover {
		text-decoration: underline;
	}
	.attn-left {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		padding: var(--space-2) 0.75rem var(--space-3);
		border-top: 1px solid var(--color-divider);
		font-size: 0.8125rem;
	}
	.attn-left li {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0 var(--space-2);
		align-items: baseline;
	}
	.attn-left-text {
		font-family: var(--font-serif);
		color: var(--color-text);
	}
	.attn-left .attn-sub {
		grid-column: 1;
	}
	.attn-left li > button {
		grid-column: 2;
		grid-row: 1 / span 2;
	}
	/* The calm state is a sentence, set in the reading serif (owner decision
	   6: attention sentences and calm states in Source Serif 4). */
	.overview-calm {
		padding: 1.125rem 0.75rem;
	}
	.calm-say {
		font-family: var(--font-serif);
		font-size: 1rem;
		color: var(--color-text);
	}
	/* Estate and What happened side by side on a desk, stacked below lg. */
	.overview-cols {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: var(--space-section);
		align-items: start;
	}
	@media (min-width: 1024px) {
		.overview-cols {
			grid-template-columns: minmax(0, 1.7fr) minmax(0, 1fr);
		}
	}
	.moves {
		display: flex;
		flex-direction: column;
	}
	.moves-system {
		border-top: 1px dashed var(--color-divider);
	}
	.move {
		display: grid;
		grid-template-columns: 3.25rem minmax(0, 1fr);
		gap: 0.625rem;
		padding: 0.375rem 0.75rem;
		border-top: 1px solid var(--color-divider);
	}
	.move:first-child {
		border-top: 0;
	}
	.move time {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--color-muted);
	}
	.move-say {
		font-family: var(--font-serif);
		font-size: 0.875rem;
		line-height: 1.4;
		color: var(--color-text);
		overflow-wrap: anywhere;
	}
	.move-say b {
		color: var(--color-text-strong);
		font-weight: 600;
	}
	a.move-say:hover {
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	@media (max-width: 767px) {
		.attn-row {
			grid-template-columns: 0.5625rem minmax(0, 1fr);
		}
		.attn-acts {
			grid-column: 2;
			justify-content: flex-start;
		}
	}
	.overview-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 0.375rem;
		padding: var(--space-2) 0.625rem;
		border-top: 1px solid var(--color-divider);
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
