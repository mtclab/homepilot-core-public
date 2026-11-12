import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../lib/test-mocks';
import Overview from './+page.svelte';
import { api, sessionStore } from '$lib/api';

/**
 * Overview zone 1 — "needs attention" (#549 F2).
 *
 * The journey these assert is the operator's morning one: something is wrong in
 * the estate, and the first screen both SAYS so and hands over the door to the
 * surface where it is fixed. Asserting that the page loaded, or that a count
 * appeared somewhere, would pass on a zone whose links go nowhere — which is
 * the whole failure mode the zone exists to prevent.
 *
 * Teeth (each verified by reverting the behaviour):
 *   - drop the drift line and "renders every kind of trouble" fails on the
 *     drifted artifact;
 *   - link a failed task at `/records/tasks` instead of its change and the
 *     href assertion fails;
 *   - render the zone unconditionally and "a calm estate gets one line" fails
 *     on the heading;
 *   - show `in_spec_pct` when nothing has been checked and the P6 test fails.
 */
const getDashboard = api.getDashboard as ReturnType<typeof vi.fn>;
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;
const listFiringAlerts = api.listFiringAlerts as ReturnType<typeof vi.fn>;
const listTasks = api.listTasks as ReturnType<typeof vi.fn>;
const listAudit = api.listAudit as ReturnType<typeof vi.fn>;

/** A walked install: the getting-started block is gone, so the zones are alone. */
function summary(over: Record<string, unknown> = {}) {
	return {
		onboarding: { steps: [], complete: true },
		inventory: {
			total: 2,
			managed: 2,
			uncovered: 0,
			coverage_pct: 100,
			by_status: { online: 2 },
			by_role: { web: 2 },
			by_type: {},
		},
		drift: { total: 3, drifted: 0, in_spec: 3, unknown: 0, checked: 3, in_spec_pct: 100 },
		artifacts: { applied: 3 },
		tasks: {},
		agents: { known: 2, connected: 2 },
		metrics: { firing_alerts: 0, retention_days: 7, rules_enabled: 1, rules_watching_nothing: 0 },
		...over,
	};
}

const HEALTHY_HOST = {
	id: 'h1',
	hostname: 'web-01',
	status: 'online',
	agent_id: 'ag-1',
	agent_connected: true,
};
const AGENT_ABSENT_HOST = {
	id: 'h2',
	hostname: 'db-01',
	status: 'online',
	agent_id: 'ag-2',
	agent_connected: false,
};

const FAILED_TASK = {
	id: 'task-abcdef-1',
	artifact_id: 'nginx-conf',
	action: 'apply',
	status: 'failed',
	result_json: null,
	created_at: '2026-08-24T10:00:00Z',
	finished_at: '2026-08-24T10:00:09Z',
	error: 'exit 1: nginx -t refused',
};

function seed(opts: {
	summary?: Record<string, unknown>;
	hosts?: unknown[];
	alerts?: unknown[];
	tasks?: unknown[];
	audit?: unknown[];
}) {
	getDashboard.mockResolvedValue(summary(opts.summary));
	listInventory.mockResolvedValue({ items: opts.hosts ?? [HEALTHY_HOST], total: 1 });
	listFiringAlerts.mockResolvedValue({ items: opts.alerts ?? [], total: 0 });
	listTasks.mockResolvedValue({ items: opts.tasks ?? [], total: 0 });
	listAudit.mockResolvedValue({ items: opts.audit ?? [], total: 0 });
}

/** The zone itself, so a match cannot come from some other part of the page. */
async function attentionZone(): Promise<HTMLElement> {
	const heading = await screen.findByRole('heading', { name: /needs attention/i });
	const section = heading.closest('section');
	expect(section).not.toBeNull();
	return section as HTMLElement;
}

describe('Overview: needs attention', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser: the test still holds */
		}
	});

	it('renders every kind of trouble as a line that links to its fix', async () => {
		seed({
			summary: {
				drift: { total: 3, drifted: 2, in_spec: 1, unknown: 0, checked: 3, in_spec_pct: 33 },
			},
			hosts: [HEALTHY_HOST, AGENT_ABSENT_HOST],
			tasks: [FAILED_TASK],
		});

		render(Overview);
		const zone = within(await attentionZone());

		// 1. The drifting artifact — the door is the drift page.
		const drift = zone.getByRole('link', { name: /artifacts disagree with the host/i });
		expect(drift).toHaveAttribute('href', '/ui/changes/drift');
		expect(drift.textContent).toContain('2 artifacts disagree');

		// 2. The failed task — the door is the CHANGE it failed to apply, which is
		//    where the operator can read the plan and try again.
		const failed = zone.getByRole('link', { name: /apply nginx-conf failed/i });
		expect(failed).toHaveAttribute('href', '/ui/changes/nginx-conf');
		expect(failed.textContent).toContain('nginx -t refused');

		// 3. The host whose agent is enrolled but gone — the door is that host.
		const agent = zone.getByRole('link', { name: /db-01: agent enrolled but not connected/i });
		expect(agent).toHaveAttribute('href', '/ui/hosts/h2');

		// And the calm line is NOT also on screen claiming all is well.
		expect(screen.queryByText(/nothing needs you/i)).not.toBeInTheDocument();
	});

	it("says when HomePilot's own host has no agent, and leaves templates off the fleet strip", async () => {
		// Lane B1: the operator confirmed which VM HomePilot runs on; it has no
		// agent, so the machine everything depends on is the one HomePilot can
		// neither measure nor maintain.
		seed({
			summary: {
				inventory: {
					...summary().inventory,
					self_guest: {
						host_id: 'h-self',
						hostname: 'hp-core',
						proxmox_id: 107,
						has_agent: false,
						absent: false,
					},
				},
			},
		});

		render(Overview);
		const zone = within(await attentionZone());

		const line = zone.getByRole('link', { name: /own host hp-core has no agent/i });
		expect(line).toHaveAttribute('href', '/ui/hosts/h-self');
		expect(listInventory.mock.calls[0][0].templates).toBe('exclude');
	});

	it('links a firing alert to the host that is firing it', async () => {
		seed({
			hosts: [HEALTHY_HOST],
			alerts: [
				{
					rule_id: 'r1',
					hostname: 'web-01',
					name: 'load high',
					firing_since: '2026-08-24T09:00:00Z',
					last_value: 9,
					last_eval: '2026-08-24T09:10:00Z',
					metric: 'load1',
					comparison: 'gt',
					threshold: 4,
					for_seconds: 300,
				},
			],
		});

		render(Overview);
		const zone = within(await attentionZone());

		expect(zone.getByRole('link', { name: /web-01: load high/i })).toHaveAttribute(
			'href',
			'/ui/hosts/h1',
		);
	});

	it('sends changes waiting on a human to the review queue', async () => {
		seed({ summary: { artifacts: { proposed: 3, applied: 1 } } });

		render(Overview);
		const zone = within(await attentionZone());

		expect(zone.getByRole('link', { name: /3 changes awaiting review/i })).toHaveAttribute(
			'href',
			'/ui/changes/inbox',
		);
	});

	it('says an approved change is still waiting to be run (owner feedback 2026-11-08)', async () => {
		// Teeth: drop the `ready` line and an approved-but-unapplied change is
		// on no screen the owner opens first - the "lost change" report.
		seed({
			summary: {
				artifacts: { approved: 2 },
				changes: { proposed: 0, ready: 2, running: 0 },
			},
		});

		render(Overview);
		const zone = within(await attentionZone());

		expect(
			zone.getByRole('link', { name: /2 approved changes ready to run/i }),
		).toHaveAttribute('href', '/ui/changes/inbox');
	});

	it('says an expired approval apart from the ready ones (#813)', async () => {
		// Teeth: count expired approvals as ready and the owner reads "3 ready
		// to run" over a change that will only go back to review.
		seed({
			summary: {
				artifacts: { approved: 3 },
				changes: { proposed: 0, ready: 3, running: 0, expired: 1 },
			},
		});

		render(Overview);
		const zone = within(await attentionZone());

		expect(
			zone.getByRole('link', { name: /1 approved change has an approval too old to run on/i }),
		).toHaveAttribute('href', '/ui/changes/inbox');
		expect(zone.getByRole('link', { name: /2 approved changes ready to run/i })).toBeInTheDocument();
	});

	it('calls a long-running task stuck, not busy', async () => {
		const started = new Date(Date.now() - 40 * 60 * 1000).toISOString();
		seed({
			tasks: [
				{
					...FAILED_TASK,
					id: 'task-stuck-1',
					artifact_id: null,
					status: 'running',
					error: null,
					created_at: started,
					finished_at: null,
				},
			],
		});

		render(Overview);
		const zone = within(await attentionZone());

		// No artifact to fix, so the door is the task list itself.
		expect(zone.getByRole('link', { name: /apply task is still running/i })).toHaveAttribute(
			'href',
			'/ui/records/tasks',
		);
	});

	// #648 tranche 5: `firing_alerts: 0` on an install with no rules is not good
	// news, it is the absence of news - and it sat on the first screen reading
	// green. A calm estate that is watching nothing must say so.
	it('says nothing is watched when no alert rule is enabled', async () => {
		seed({
			hosts: [HEALTHY_HOST],
			summary: {
				metrics: {
					firing_alerts: 0,
					retention_days: 7,
					rules_enabled: 0,
					rules_watching_nothing: 0,
				},
			},
		});

		render(Overview);
		const zone = within(await attentionZone());

		expect(zone.getByText(/no alert rule is enabled/i)).toBeInTheDocument();
	});

	it('says so when a rule is enabled but matches no host', async () => {
		seed({
			hosts: [HEALTHY_HOST],
			summary: {
				metrics: {
					firing_alerts: 0,
					retention_days: 7,
					rules_enabled: 2,
					rules_watching_nothing: 1,
				},
			},
		});

		render(Overview);
		const zone = within(await attentionZone());

		expect(zone.getByText(/matches no host/i)).toBeInTheDocument();
	});

	it('a calm estate gets one line and no attention chrome at all', async () => {
		seed({ hosts: [HEALTHY_HOST] });

		render(Overview);

		expect(await screen.findByText(/nothing needs you/i)).toBeInTheDocument();
		expect(screen.queryByRole('heading', { name: /needs attention/i })).not.toBeInTheDocument();
		// No zero-stat noise standing in for the zone either.
		expect(screen.queryByText(/0 failed/i)).not.toBeInTheDocument();
	});
});

describe('Overview: fleet at a glance', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser */
		}
	});

	it('never reports a percentage for drift nothing has checked (P6)', async () => {
		seed({
			summary: {
				drift: { total: 0, drifted: 0, in_spec: 0, unknown: 0, checked: 0, in_spec_pct: 100 },
			},
		});

		render(Overview);

		const sub = await screen.findByText(/nothing checked yet/i);
		// Scoped to the In-spec card on purpose: coverage may legitimately be
		// 100% on the same screen, and a page-wide "no 100% anywhere" assertion
		// would pass for the wrong reason the day coverage is 90%.
		const card = sub.closest('a') as HTMLElement;
		expect(within(card).getByText(/in spec/i)).toBeInTheDocument();
		expect(within(card).getByText('—')).toBeInTheDocument();
		expect(card.textContent).not.toMatch(/\d+%/);
	});

	it('does not carry the hosts-by-role breakdown any more (it moved to Hosts)', async () => {
		seed({});

		render(Overview);

		await waitFor(() => expect(getDashboard).toHaveBeenCalled());
		expect(screen.queryByText(/hosts by role/i)).not.toBeInTheDocument();
	});

	// UI slice 4 (review 01 step 13): the readout row replaced the two donuts.
	// Teeth: put either donut back and this fails on its title or its <svg>.
	it('shows the estate as one readout row, with no donut left', async () => {
		seed({
			hosts: [HEALTHY_HOST, AGENT_ABSENT_HOST],
			summary: {
				inventory: { ...summary().inventory, total: 2, managed: 2, templates: 3 },
				agents: { known: 2, connected: 1 },
				artifacts: { proposed: 2, applied: 5 },
				changes: { proposed: 2, ready: 1, running: 0 },
			},
		});

		render(Overview);

		const estate = (await screen.findByRole('heading', { name: 'Estate' })).closest('section') as HTMLElement;
		const readout = (label: string) => estate.querySelector(`[data-readout="${label}"]`) as HTMLElement;
		expect(readout('Hosts').textContent).toMatch(/2\s*\+3 tpl/);
		// Watched = hosts read whose agent is connected now: 1 of the 2.
		expect(readout('Watched by agent').textContent).toMatch(/1\s*\/2/);
		expect(readout('Watched by agent')).toHaveAttribute('href', '/ui/monitoring/alerts');
		expect(readout('Agents').textContent).toMatch(/1\s*\/2/);
		expect(readout('In spec').textContent).toMatch(/3\s*\/3/);
		// 3.10.0: the Inbox's count (proposed + approved, not yet run), and its
		// door - not the retired review queue, which counted proposals only.
		expect(readout('Changes').textContent).toMatch(/3\s*waiting/);
		expect(readout('Changes').textContent).toContain('2 proposed · 1 ready to run');
		expect(readout('Changes')).toHaveAttribute('href', '/ui/changes/inbox');
		expect(screen.queryByText(/hosts by status/i)).not.toBeInTheDocument();
		expect(screen.queryByRole('heading', { name: /^artifacts$/i })).not.toBeInTheDocument();
		expect(document.querySelector('main svg circle, svg circle')).toBeNull();
	});

	it('keeps the host chip strip as the door to each host', async () => {
		seed({ hosts: [HEALTHY_HOST, AGENT_ABSENT_HOST] });

		render(Overview);

		expect(await screen.findByRole('link', { name: /web-01/ })).toHaveAttribute(
			'href',
			'/ui/hosts/h1',
		);
	});
});

describe('Overview: recent movement', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser */
		}
	});

	it('merges tasks and journal into one feed with a door to Records', async () => {
		seed({
			tasks: [
				{
					...FAILED_TASK,
					id: 'task-ok-1',
					artifact_id: 'nginx-conf',
					status: 'succeeded',
					error: null,
				},
			],
			audit: [
				{
					id: 7,
					timestamp: '2026-08-24T11:00:00Z',
					user_id: 'olli',
					source: 'ui',
					action: 'propose',
					artifact_id: 'pg-tune',
					target_host: 'db-01',
					target_service: null,
					command: null,
					exit_code: null,
					snapshot_id: null,
					duration_ms: null,
					details_json: null,
				},
			],
		});

		render(Overview);

		const heading = await screen.findByRole('heading', { name: /recent movement/i });
		const feed = within(heading.closest('section') as HTMLElement);
		// The task half and the journal half are BOTH in the one feed.
		expect(feed.getByText(/nginx-conf · succeeded/)).toBeInTheDocument();
		expect(feed.getByText(/pg-tune · db-01 · ui/)).toBeInTheDocument();
		expect(feed.getByRole('link', { name: /records/i })).toHaveAttribute('href', '/ui/records');
	});

	it('caps the feed rather than growing an unbounded list', async () => {
		const many = Array.from({ length: 30 }, (_, i) => ({
			...FAILED_TASK,
			id: `task-${i}`,
			artifact_id: `art-${i}`,
			status: 'succeeded',
			error: null,
			created_at: new Date(Date.now() - i * 60000).toISOString(),
			finished_at: new Date(Date.now() - i * 60000).toISOString(),
		}));
		seed({ tasks: many });

		render(Overview);

		const heading = await screen.findByRole('heading', { name: /recent movement/i });
		const rows = (heading.closest('section') as HTMLElement).querySelectorAll('ul > li');
		expect(rows.length).toBe(10);
	});
});

/**
 * Overview when a read did not come back (#648 tranche 7).
 *
 * Zone 1 is composed from four side calls made with `Promise.allSettled`. A
 * rejection there used to become an empty array and nothing else, so a
 * backend that could not answer produced the same screen as an estate with
 * nothing wrong: "Nothing needs you." The KB page names this class in its own
 * source - a failure rendered as a successful empty result is the worst way to
 * be wrong, because the operator concludes all is well and stops looking.
 *
 * Teeth (each verified by reverting the fix): drop the `unread` push in
 * `load()` and every test below fails on the missing line AND on the calm
 * sentence appearing next to a failed read.
 */
describe('Overview: a read that did not come back', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser: the test still holds */
		}
	});

	it('never says nothing needs you when it could not ask', async () => {
		seed({});
		listTasks.mockRejectedValue(new Error('502 Bad Gateway'));

		render(Overview);

		const zone = within(await attentionZone());
		expect(zone.getByText(/could not read recent tasks/i)).toBeInTheDocument();
		expect(screen.queryByText(/Nothing needs you/i)).not.toBeInTheDocument();
	});

	it('names each unread source separately and doors it to the page that owns it', async () => {
		seed({});
		listInventory.mockRejectedValue(new Error('down'));
		listFiringAlerts.mockRejectedValue(new Error('down'));
		listAudit.mockRejectedValue(new Error('down'));

		const zone = (render(Overview), within(await attentionZone()));
		expect(zone.getByText(/could not read the host inventory/i).closest('a')).toHaveAttribute(
			'href',
			'/ui/hosts',
		);
		// Firing alerts are Monitoring's since UI slice 1, so that is the door.
		expect(zone.getByText(/could not read firing alerts/i).closest('a')).toHaveAttribute(
			'href',
			'/ui/monitoring/alerts',
		);
		expect(zone.getByText(/could not read the journal/i).closest('a')).toHaveAttribute(
			'href',
			'/ui/records/journal',
		);
	});

	it('does not call an unreadable feed "nothing has happened yet"', async () => {
		seed({});
		listTasks.mockRejectedValue(new Error('down'));
		listAudit.mockRejectedValue(new Error('down'));

		render(Overview);

		const heading = await screen.findByRole('heading', { name: /recent movement/i });
		const feed = within(heading.closest('section') as HTMLElement);
		expect(feed.getByText(/could not be read, not because nothing happened/i)).toBeInTheDocument();
		expect(feed.queryByText(/Nothing has happened yet/i)).not.toBeInTheDocument();
	});

	it('still reaches the calm screen when every read actually answered', async () => {
		// The other half: the fix must not make "all clear" unreachable, or the
		// zone becomes noise an operator learns to ignore.
		seed({});

		render(Overview);

		await waitFor(() => expect(screen.getByText(/Nothing needs you/i)).toBeInTheDocument());
		expect(screen.queryByText(/could not read/i)).not.toBeInTheDocument();
	});
});

/**
 * Claims the Overview made about sets it had only partly read (#704).
 *
 * "agent offline" was computed from the first 100 hosts and "failed"/"stuck"
 * from the 50 most recent tasks, with nothing on the page saying so; "stuck"
 * looked at `running` only, so a task that never left `pending` was invisible;
 * and a journal action with no chip was coloured as a pending review.
 *
 * Teeth: drop the `partial:*` pushes in `load()` and the first test fails;
 * drop `pending` from the stuck filter and the second fails; put the
 * `badge-proposed` fallback back and the third fails.
 */
describe('Overview: says how much it read', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser */
		}
	});

	it('marks the host and task lines partial when the set is bigger than the page read', async () => {
		seed({ hosts: [HEALTHY_HOST], tasks: [] });
		listInventory.mockResolvedValue({ items: [HEALTHY_HOST], total: 240 });
		listTasks.mockResolvedValue({ items: [], total: 900 });

		render(Overview);
		const zone = within(await attentionZone());

		expect(zone.getByText(/checked here for 1 of 240 hosts/i).closest('a')).toHaveAttribute(
			'href',
			'/ui/hosts',
		);
		expect(zone.getByText(/0 most recent of 900/i).closest('a')).toHaveAttribute(
			'href',
			'/ui/records/tasks',
		);
		expect(screen.queryByText(/Nothing needs you/i)).not.toBeInTheDocument();
	});

	it('calls a task that never left pending stuck', async () => {
		const queued = new Date(Date.now() - 40 * 60 * 1000).toISOString();
		seed({
			tasks: [
				{
					...FAILED_TASK,
					id: 'task-pending-1',
					status: 'pending',
					error: null,
					created_at: queued,
					finished_at: null,
				},
			],
		});

		render(Overview);
		const zone = within(await attentionZone());

		expect(zone.getByText(/nginx-conf is still pending/i)).toBeInTheDocument();
	});

	it('gives a journal action it has no chip for a neutral chip, not the review colour', async () => {
		seed({
			audit: [
				{
					id: 9,
					timestamp: '2026-08-24T11:00:00Z',
					user_id: 'operator',
					source: 'api',
					action: 'token_minted',
					artifact_id: null,
					target_host: null,
					target_service: null,
					command: null,
					exit_code: null,
					snapshot_id: null,
					duration_ms: null,
					details_json: null,
				},
			],
		});

		render(Overview);
		const heading = await screen.findByRole('heading', { name: /recent movement/i });
		const chip = within(heading.closest('section') as HTMLElement).getByText('token_minted');

		expect(chip).toHaveClass('badge-neutral');
		expect(chip).not.toHaveClass('badge-proposed');
	});
});

/**
 * Attention honesty (review 01, steps 1-6): the zone says what is true, ranks it
 * by consequence and age, and stops repeating what was already settled.
 *
 * Teeth (each verified by reverting the behaviour in +page.svelte, see PR):
 *   - drop the `fleetDark` line and "every agent gone is ONE critical line" fails;
 *     keep the per-host agent lines under it and the same test fails on them;
 *   - drop the selfcheck loop and both self-check tests fail;
 *   - drop `settled()` from the failed filter and the superseded tests fail;
 *     make failures critical regardless of age and the 24 h test fails;
 *   - drop the `gone` block and the gone-host tests fail;
 *   - drop the `drift:unknown` block and the by-reason test fails.
 */
const getSelfcheck = api.getSelfcheck as ReturnType<typeof vi.fn>;
const forgetHost = api.forgetHost as ReturnType<typeof vi.fn>;

function minutesAgo(n: number): string {
	return new Date(Date.now() - n * 60 * 1000).toISOString();
}

/** The severity chip of the attention line whose text matches. */
function chipOf(zone: ReturnType<typeof within>, text: RegExp): HTMLElement {
	const line = zone.getByText(text).closest('a, div') as HTMLElement;
	return line.querySelector('.badge') as HTMLElement;
}

describe('Overview: attention honesty', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		getSelfcheck.mockResolvedValue({ timeout_seconds: 2, counts: {}, subsystems: [] });
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser */
		}
	});

	it('says every agent gone as ONE critical line with how long, not N quiet ones', async () => {
		// Prod, a week: two enrolled agents, none connected, the seeded rules
		// blind because nothing reports, and the self-check unable to read any
		// agent's version for the same reason.
		getSelfcheck.mockResolvedValue({
			timeout_seconds: 2,
			counts: {},
			subsystems: [
				{
					name: 'agent_versions',
					configured: true,
					state: 'unknown',
					target: '',
					consequence: 'No agent is connected, so nothing can be said about what versions the fleet is running.',
				},
			],
		});
		seed({
			summary: {
				agents: {
					known: 2,
					connected: 0,
					enrolled: 2,
					offline_since: minutesAgo(3 * 24 * 60 + 5),
					offline_alarm: true,
				},
				metrics: {
					firing_alerts: 0,
					retention_days: 7,
					rules_enabled: 2,
					rules_watching_nothing: 2,
				},
			},
			hosts: [
				{ ...AGENT_ABSENT_HOST, agent_offline_since: minutesAgo(3 * 24 * 60) },
				{ ...AGENT_ABSENT_HOST, id: 'h3', hostname: 'app-01', agent_id: 'ag-3' },
			],
		});

		render(Overview);
		const zone = within(await attentionZone());

		const fleet = zone.getByRole('link', { name: /no agent is connected \(2 enrolled\)/i });
		expect(fleet).toHaveAttribute('href', '/ui/hosts');
		expect(fleet.textContent).toContain('2 alert rules watch nothing');
		expect(fleet.textContent).toContain('for 3d');
		expect(fleet.querySelector('.badge')).toHaveClass('badge-critical');
		// The cause absorbed its effects: no per-host agent lines, no separate
		// "rules match no host" line saying the same thing again.
		expect(zone.queryByText(/agent enrolled but not connected/i)).not.toBeInTheDocument();
		expect(zone.queryByText(/matches no host|match no host/i)).not.toBeInTheDocument();
		expect(zone.queryByText(/what versions the fleet is running/i)).not.toBeInTheDocument();
	});

	it('gives each absent agent its duration, and calls an hour-long one critical', async () => {
		seed({
			hosts: [
				{ ...AGENT_ABSENT_HOST, agent_offline_since: minutesAgo(125) },
				{
					...AGENT_ABSENT_HOST,
					id: 'h3',
					hostname: 'app-01',
					agent_id: 'ag-3',
					agent_offline_since: minutesAgo(7),
				},
			],
		});

		render(Overview);
		const zone = within(await attentionZone());

		const long = zone.getByRole('link', { name: /db-01: agent enrolled/i });
		expect(long.textContent).toContain('for 2h');
		expect(long.querySelector('.badge')).toHaveClass('badge-critical');
		const blip = zone.getByRole('link', { name: /app-01: agent enrolled/i });
		expect(blip.textContent).toContain('for 7m');
		expect(blip.querySelector('.badge')).toHaveClass('badge-warning');
	});

	it('puts a subsystem the self-check cannot reach on the Overview, in its own words', async () => {
		const skew =
			'1 of 1 connected agents are OLDER than this control plane (3.6.25): web-01. Nothing upgrades an enrolled agent.';
		getSelfcheck.mockResolvedValue({
			timeout_seconds: 2,
			counts: {},
			subsystems: [
				{ name: 'agent_versions', configured: true, state: 'unreachable', target: '', consequence: skew },
				{
					name: 'proxmox',
					configured: true,
					state: 'unreachable',
					target: 'https://pve.example.com:8006',
					consequence: 'Proxmox refused the token, so inventory is frozen.',
				},
				{
					name: 'reconcilers',
					configured: true,
					state: 'unknown',
					target: '',
					consequence: 'Could not check the reconcilers within 2s.',
				},
				{ name: 'vault', configured: true, state: 'ok', target: '', consequence: 'Vault is unlocked.' },
				{
					name: 'events_webhook',
					configured: false,
					state: 'off',
					target: '',
					consequence: 'Alerts reach the log and an open browser and nowhere else.',
				},
			],
		});
		seed({});

		render(Overview);
		const zone = within(await attentionZone());

		const versions = zone.getByText(skew).closest('a') as HTMLElement;
		expect(versions).toHaveAttribute('href', '/ui/settings?tab=subsystems');
		expect(versions.querySelector('.badge')).toHaveClass('badge-warning');
		expect(chipOf(zone, /Proxmox refused the token/)).toHaveClass('badge-critical');
		expect(chipOf(zone, /Could not check the reconcilers/)).toHaveClass('badge-warning');
		// ok and off are not attention.
		expect(zone.queryByText(/Vault is unlocked/)).not.toBeInTheDocument();
		expect(zone.queryByText(/nowhere else/)).not.toBeInTheDocument();
	});

	it('says so when the self-check itself could not be read', async () => {
		seed({});
		getSelfcheck.mockRejectedValue(new Error('502'));

		render(Overview);
		const zone = within(await attentionZone());

		expect(zone.getByText(/could not read the self-check/i).closest('a')).toHaveAttribute(
			'href',
			'/ui/settings?tab=subsystems',
		);
		expect(screen.queryByText(/Nothing needs you/i)).not.toBeInTheDocument();
	});

	it('drops a failure a later run settled, and an install the agent made moot', async () => {
		seed({
			hosts: [HEALTHY_HOST],
			tasks: [
				// Newest first, as the API returns them.
				{
					...FAILED_TASK,
					id: 'task-replay-ok',
					action: 'replay',
					status: 'succeeded',
					error: null,
					created_at: minutesAgo(60 * 24 * 9),
					finished_at: minutesAgo(60 * 24 * 9),
				},
				{
					...FAILED_TASK,
					id: 'task-apply-failed',
					created_at: minutesAgo(60 * 24 * 10),
					finished_at: minutesAgo(60 * 24 * 10),
				},
				{
					...FAILED_TASK,
					id: 'task-install-failed',
					artifact_id: null,
					action: 'install_agent',
					error: 'install: the installer exited 7',
					result_json: JSON.stringify({ host_id: 'h1', hostname: 'web-01' }),
					created_at: minutesAgo(60 * 24 * 10),
					finished_at: minutesAgo(60 * 24 * 10),
				},
			],
		});

		render(Overview);

		expect(await screen.findByText(/Nothing needs you/i)).toBeInTheDocument();
		expect(screen.queryByText(/apply nginx-conf failed/i)).not.toBeInTheDocument();
		expect(screen.queryByText(/installer exited 7/i)).not.toBeInTheDocument();
	});

	it('keeps an unsettled failure, critical for a day and a warning after', async () => {
		seed({
			tasks: [
				{
					...FAILED_TASK,
					id: 'task-fresh',
					artifact_id: 'pg-tune',
					created_at: minutesAgo(90),
					finished_at: minutesAgo(89),
				},
				{
					...FAILED_TASK,
					id: 'task-old',
					created_at: minutesAgo(60 * 24 * 3),
					finished_at: minutesAgo(60 * 24 * 3),
				},
				// A failure on ANOTHER artifact does not settle it, nor does an
				// install whose host cannot be told (recorded before tasks named it).
				{
					...FAILED_TASK,
					id: 'task-legacy-install',
					artifact_id: null,
					action: 'install_agent',
					error: 'install: the installer exited 7',
					result_json: null,
					created_at: minutesAgo(60 * 24 * 70),
					finished_at: minutesAgo(60 * 24 * 70),
				},
			],
		});

		render(Overview);
		const zone = within(await attentionZone());

		expect(chipOf(zone, /apply pg-tune failed/)).toHaveClass('badge-critical');
		expect(chipOf(zone, /apply nginx-conf failed/)).toHaveClass('badge-warning');
		expect(chipOf(zone, /installer exited 7/)).toHaveClass('badge-warning');
	});

	it('names a host Proxmox stopped reporting, with how long and a Forget', async () => {
		const GONE = {
			id: 'h9',
			hostname: 'old-build-vm',
			status: 'unknown',
			source: 'hp_created',
			import_state: 'adopted',
			absent_since: minutesAgo(60 * 24 * 70),
			agent_id: 'ag-9',
			agent_connected: false,
			agent_offline_since: minutesAgo(60 * 24 * 70),
		};
		seed({
			hosts: [
				HEALTHY_HOST,
				GONE,
				// Never adopted: not news, as on the Hosts page.
				{ ...GONE, id: 'h10', hostname: 'never-adopted-vm', import_state: 'pending' },
			],
		});

		render(Overview);
		const zone = within(await attentionZone());

		const line = zone.getByRole('link', { name: /old-build-vm: proxmox no longer reports/i });
		expect(line).toHaveAttribute('href', '/ui/hosts/h9');
		expect(line.textContent).toContain('for 70d');
		expect(line.querySelector('.badge')).toHaveClass('badge-warning');
		// One reason per host: gone, not also "agent offline".
		expect(zone.queryByText(/old-build-vm: agent enrolled/i)).not.toBeInTheDocument();
		expect(zone.queryByText(/never-adopted-vm/i)).not.toBeInTheDocument();

		// Forget is the irreversible one, so it asks first.
		const row = within(line.closest('[data-attention="gone:h9"]') as HTMLElement);
		await fireEvent.click(row.getByRole('button', { name: /^forget$/i }));
		expect(forgetHost).not.toHaveBeenCalled();
		await fireEvent.click(row.getByRole('button', { name: /confirm forget/i }));
		await waitFor(() => expect(forgetHost).toHaveBeenCalledWith('h9'));
	});

	it('says why drift is unverified, and stays calm when it is only by design', async () => {
		// Prod-shaped: 16 of 17 unknown because no agent could read the hosts.
		seed({
			summary: {
				drift: {
					total: 17,
					drifted: 0,
					in_spec: 1,
					unknown: 16,
					checked: 1,
					in_spec_pct: 100,
					unknown_by_reason: { no_agent: 14, check_failed: 0, no_proxmox: 0, by_design: 2, other: 0 },
				},
			},
		});

		const first = render(Overview);
		const zone = within(await attentionZone());
		const line = zone.getByRole('link', { name: /16 artifacts unverified/i });
		expect(line.textContent).toContain('14 no agent on the host');
		expect(line.textContent).toContain('2 describe-only (fine)');
		expect(line).toHaveAttribute('href', '/ui/changes/drift');
		first.unmount();

		// Describe-only artifacts are their nature, not a fault: no line at all.
		seed({
			summary: {
				drift: {
					total: 3,
					drifted: 0,
					in_spec: 1,
					unknown: 2,
					checked: 1,
					in_spec_pct: 100,
					unknown_by_reason: { no_agent: 0, check_failed: 0, no_proxmox: 0, by_design: 2, other: 0 },
				},
			},
		});
		render(Overview);
		expect(await screen.findByText(/Nothing needs you/i)).toBeInTheDocument();
		expect(screen.getByText(/2 not established \(2 describe-only\)/)).toBeInTheDocument();
	});
});

/**
 * Release 3.7.0: the three Phase A lanes, read together on the Overview.
 *
 * Teeth (each verified by reverting the behaviour):
 *   - word every inert rule as a threshold rule again and the "went dark" test
 *     finds no line of its own;
 *   - drop the waiting-to-join block and its test finds no line;
 *   - count rows instead of hostnames and it reads "2 hosts".
 */
describe('Overview: the Phase A lanes together', () => {
	const getEnrolmentRequests = api.getEnrolmentRequests as ReturnType<typeof vi.fn>;
	const WINDOW_SHUT = { open: false, expires_at: null, seconds_remaining: 0, fleet_empty: false };
	const NOBODY = { wants_to_join: 0, refused: 0, pending_tokens: 0, expired_unused_tokens: 0 };

	function knocking(window: typeof WINDOW_SHUT = WINDOW_SHUT) {
		const attempt = {
			hostname: 'new-01',
			peer: '192.0.2.10',
			reason_code: 'window_closed',
			reason: 'not accepting new hosts right now',
			agent_id: null,
			first_seen: minutesAgo(30),
			last_seen: minutesAgo(1),
			attempts: 12,
			wants_to_join: true,
		};
		return {
			// One host knocking from two addresses is one host waiting, and a
			// wrong-token host is refused, not waiting.
			attempts: [
				attempt,
				{ ...attempt, peer: '192.0.2.11' },
				{ ...attempt, hostname: 'guess-01', reason_code: 'bad_token', wants_to_join: false },
			],
			pending_tokens: [],
			window,
			summary: { wants_to_join: 2, refused: 1, pending_tokens: 0, expired_unused_tokens: 0 },
		};
	}

	beforeEach(() => {
		vi.clearAllMocks();
		getSelfcheck.mockResolvedValue({ timeout_seconds: 2, counts: {}, subsystems: [] });
		getEnrolmentRequests.mockResolvedValue({
			attempts: [],
			pending_tokens: [],
			window: WINDOW_SHUT,
			summary: NOBODY,
		});
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser */
		}
	});

	it('words a "went dark" rule that matches no host by its own cause', async () => {
		seed({
			summary: {
				metrics: {
					firing_alerts: 0,
					retention_days: 7,
					rules_enabled: 3,
					rules_watching_nothing: 2,
					nodata_rules_watching_nothing: 1,
				},
			},
		});

		render(Overview);
		const zone = within(await attentionZone());

		const dark = zone.getByRole('link', { name: /"went dark" rule watches no host/i });
		expect(dark.textContent).toContain('has an agent enrolled');
		expect(dark.textContent).not.toMatch(/metric name/i);
		expect(dark).toHaveAttribute('href', '/ui/hosts');
		// The threshold rule keeps its own advice, counted without the dark one.
		const typo = zone.getByRole('link', { name: /1 alert rule matches no host/i });
		expect(typo.textContent).toContain('check the metric name');
	});

	it('says how many hosts are waiting to join, and links to Hosts > Agents', async () => {
		getEnrolmentRequests.mockResolvedValue(knocking());
		seed({});

		render(Overview);
		const zone = within(await attentionZone());

		const line = zone.getByRole('link', { name: /waiting to join/i });
		expect(line.textContent).toContain('1 host is waiting to join');
		// The join panel lives on Hosts > Agents since UI slice 1.
		expect(line).toHaveAttribute('href', '/ui/hosts/agents');
		expect(line.querySelector('.badge')).toHaveClass('badge-warning');
	});

	it('stays calm about knocking hosts while the window is open', async () => {
		getEnrolmentRequests.mockResolvedValue(
			knocking({ open: true, expires_at: null, seconds_remaining: 600, fleet_empty: false }),
		);
		seed({});

		render(Overview);
		expect(await screen.findByText(/Nothing needs you/i)).toBeInTheDocument();
	});

	it('says so when the enrolment requests could not be read', async () => {
		getEnrolmentRequests.mockRejectedValue(new Error('503'));
		seed({});

		render(Overview);
		const zone = within(await attentionZone());
		expect(zone.getByText(/a host waiting to join cannot be reported here/i)).toBeInTheDocument();
	});

	// Lane B2. Teeth: drop the address-clash block from buildAttention and this
	// fails on the missing line; render it as a warning and it fails on the chip.
	it('says two guests on one address as ONE critical line naming both, with the fix', async () => {
		const clash = {
			address: '192.0.2.10',
			kind: 'configured',
			severity: 'critical',
			summary:
				'192.0.2.10 is configured on 2 machines: web-01 (VM 101, running) and db-01 (VM 102, stopped)',
			remedy:
				'Whichever starts second takes the address over on the LAN. Give one of them another address (its cloud-init ipconfig, or the container\'s net ip=) and restart it.',
			host_ids: ['h1', 'h2'],
			holders: [],
		};
		seed({ summary: { addresses: { conflicts: [clash] } } });

		render(Overview);
		const zone = within(await attentionZone());
		const lines = zone.getAllByRole('link', { name: /192\.0\.2\.10 is configured on 2 machines/i });
		expect(lines).toHaveLength(1);
		const line = lines[0];
		expect(line.textContent).toContain('web-01 (VM 101, running)');
		expect(line.textContent).toContain('db-01 (VM 102, stopped)');
		expect(line.textContent).toContain('takes the address over');
		expect(line.querySelector('.badge')).toHaveClass('badge-critical');
		expect(line.querySelector('.badge')?.textContent).toBe('address clash');
		// The door is the first holder's Network tab, where every holder is listed.
		expect(line).toHaveAttribute('href', '/ui/hosts/h1?tab=network');
	});

	it('says nothing about addresses on a backend that does not report them', async () => {
		seed({});
		render(Overview);
		await screen.findByText(/nothing needs you/i);
		expect(screen.queryByText(/address clash/i)).not.toBeInTheDocument();
	});
});

/**
 * Review A+: a newer HomePilot is ONE notice line, linked to its release notes,
 * with the operator actions under it - and nothing at all when the server says
 * no update is available (after an upgrade, or with the check switched off).
 *
 * Teeth (each verified by reverting the behaviour):
 *   - drop the update block from buildAttention and the first test finds no line;
 *   - drop `external` and the link opens in this tab (target assertion);
 *   - read `latest` alone instead of `update_available` and the "no update"
 *     test finds a line for the release this install already runs.
 */
describe('Overview: updates available', () => {
	const RELEASE = {
		version: '3.8.0',
		tag: 'v3.8.0',
		name: 'v3.8.0',
		url: 'https://github.com/mtclab/homepilot-core-public/releases/tag/v3.8.0',
		published_at: '2026-12-01T10:00:00Z',
		operator_actions: ['Upgrade: one migration runs.', 'Set HP_EXAMPLE first.'],
	};

	function updates(over: Record<string, unknown> = {}) {
		return {
			enabled: true,
			state: 'ok',
			running_version: '3.7.0',
			update_available: true,
			latest: RELEASE,
			checked_at: '2026-12-02T10:00:00Z',
			last_ok_at: '2026-12-02T10:00:00Z',
			last_error: null,
			consecutive_failures: 0,
			rate_limited_until: null,
			notified_version: '3.8.0',
			notified_at: '2026-12-02T10:00:00Z',
			interval_seconds: 86400,
			releases_url: 'https://github.com/mtclab/homepilot-core-public/releases',
			...over,
		};
	}

	beforeEach(() => {
		vi.clearAllMocks();
		getSelfcheck.mockResolvedValue({ timeout_seconds: 2, counts: {}, subsystems: [] });
		(api.getEnrolmentRequests as ReturnType<typeof vi.fn>).mockResolvedValue({
			attempts: [],
			pending_tokens: [],
			window: { open: false, expires_at: null, seconds_remaining: 0, fleet_empty: false },
			summary: { wants_to_join: 0, refused: 0, pending_tokens: 0, expired_unused_tokens: 0 },
		});
	});

	it('is one notice line linking to the release notes, with the operator actions', async () => {
		seed({ summary: { updates: updates() } });

		render(Overview);
		const zone = within(await attentionZone());

		const lines = zone.getAllByRole('link', { name: /is available/i });
		expect(lines).toHaveLength(1);
		const line = lines[0];
		expect(line.textContent).toContain('HomePilot 3.8.0 is available - you run 3.7.0');
		expect(line).toHaveAttribute('href', RELEASE.url);
		expect(line).toHaveAttribute('target', '_blank');
		expect(line).toHaveAttribute('rel', 'noopener noreferrer');
		expect(line.querySelector('.badge')).toHaveClass('badge-notice');
		const actions = within(line).getAllByRole('listitem').map((li) => li.textContent);
		expect(actions).toEqual(RELEASE.operator_actions);
	});

	it('says nothing when no update is available, even with a release known', async () => {
		seed({
			summary: {
				updates: updates({
					running_version: '3.8.0',
					update_available: false,
				}),
			},
		});

		render(Overview);
		expect(await screen.findByText(/Nothing needs you/i)).toBeInTheDocument();
		expect(screen.queryByText(/is available/i)).toBeNull();
	});

	it('release 3.8.0: the update, an address clash and the own-host line render together, each in its own shape', async () => {
		// The three 3.8.0 lanes all add Overview lines through one AttentionItem;
		// A+ gave it `external` and `details`. Only the release line may open a
		// new tab or carry a list: the clash (B2) and own-host (B1) lines are
		// doors inside the console.
		const clash = {
			address: '192.0.2.10',
			kind: 'configured',
			severity: 'critical',
			summary: '192.0.2.10 is configured on 2 machines: web-01 (VM 101, running) and db-01 (VM 102, stopped)',
			remedy: 'Give one of them another address and restart it.',
			host_ids: ['h1', 'h2'],
			holders: [],
		};
		seed({
			summary: {
				updates: updates(),
				addresses: { conflicts: [clash] },
				inventory: {
					...summary().inventory,
					self_guest: { host_id: 'h-self', hostname: 'hp-core', proxmox_id: 107, has_agent: false, absent: false },
				},
			},
		});

		render(Overview);
		const zone = within(await attentionZone());

		const update = zone.getAllByRole('link', { name: /is available/i });
		const clashes = zone.getAllByRole('link', { name: /192\.0\.2\.10 is configured on 2 machines/i });
		const own = zone.getAllByRole('link', { name: /own host hp-core has no agent/i });
		expect([update.length, clashes.length, own.length]).toEqual([1, 1, 1]);

		expect(update[0]).toHaveAttribute('target', '_blank');
		expect(within(update[0]).getAllByRole('listitem').map((li) => li.textContent)).toEqual(
			RELEASE.operator_actions,
		);
		for (const [line, href, chip] of [
			[clashes[0], '/ui/hosts/h1?tab=network', 'badge-critical'],
			[own[0], '/ui/hosts/h-self', 'badge-warning'],
		] as const) {
			expect(line).toHaveAttribute('href', href);
			expect(line).not.toHaveAttribute('target');
			expect(line).not.toHaveAttribute('rel');
			expect(line.querySelector('ul')).toBeNull();
			expect(line.querySelector('.badge')).toHaveClass(chip);
		}
	});

	it('says nothing while the check is off', async () => {
		seed({
			summary: {
				updates: updates({ enabled: false, state: 'off', update_available: false }),
			},
		});

		render(Overview);
		expect(await screen.findByText(/Nothing needs you/i)).toBeInTheDocument();
	});
});

/**
 * The relaid-out attention zone (UI slice 4, review 01 step 13): every line
 * has a door, says since when where the source knows, and carries the action
 * that fixes it where the API offers one today - and the action completes on
 * this page, re-reading so the line goes by what the server now says.
 *
 * Teeth (each verified by reverting the behaviour):
 *   - drop the actions slot and the three action cases fail on the buttons;
 *   - skip the confirm step and "asks first" fails (the API is called early);
 *   - skip the re-read after an action and each case fails on the second load;
 *   - drop `since` from a line and the <time> case fails;
 *   - say "Nothing needs you." without the sources and the calm case fails.
 */
describe('Overview: lines that act (UI slice 4)', () => {
	const getEnrolmentRequests = api.getEnrolmentRequests as ReturnType<typeof vi.fn>;
	const openEnrolmentWindow = api.openEnrolmentWindow as ReturnType<typeof vi.fn>;
	const getDriftStatus = api.getDriftStatus as ReturnType<typeof vi.fn>;
	const cancelTask = api.cancelTask as ReturnType<typeof vi.fn>;
	const ADMIN = { authenticated: true, token_label: 'test', capabilities: ['read', 'write', 'admin'] };

	const WAITING = {
		attempts: [
			{
				hostname: 'new-01',
				peer: '192.0.2.10',
				reason_code: 'window_closed',
				reason: 'not accepting new hosts right now',
				agent_id: null,
				first_seen: minutesAgo(90),
				last_seen: minutesAgo(1),
				attempts: 12,
				wants_to_join: true,
			},
		],
		pending_tokens: [],
		window: { open: false, expires_at: null, seconds_remaining: 0, fleet_empty: false },
		summary: { wants_to_join: 1, refused: 0, pending_tokens: 0, expired_unused_tokens: 0 },
	};

	beforeEach(() => {
		vi.clearAllMocks();
		sessionStore.set(ADMIN as never);
		getSelfcheck.mockResolvedValue({ timeout_seconds: 2, counts: {}, subsystems: [] });
		getEnrolmentRequests.mockResolvedValue({ ...WAITING, attempts: [] });
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser */
		}
	});

	function line(key: string): HTMLElement {
		const el = document.querySelector(`[data-attention="${key}"]`);
		expect(el, `no attention line ${key}`).not.toBeNull();
		return el as HTMLElement;
	}

	it('gives every line a door, and the line a since where the source knows one', async () => {
		const stuck = {
			...FAILED_TASK,
			id: 'task-stuck-9',
			status: 'running',
			error: null,
			created_at: minutesAgo(40),
			finished_at: null,
		};
		getEnrolmentRequests.mockResolvedValue(WAITING);
		seed({
			summary: {
				drift: { total: 3, drifted: 1, in_spec: 2, unknown: 0, checked: 3, in_spec_pct: 66 },
				artifacts: { proposed: 1 },
			},
			hosts: [HEALTHY_HOST, { ...AGENT_ABSENT_HOST, agent_offline_since: minutesAgo(30) }],
			tasks: [FAILED_TASK, stuck],
		});

		render(Overview);
		await attentionZone();

		const lines = Array.from(document.querySelectorAll('[data-attention]'));
		expect(lines.length).toBeGreaterThanOrEqual(6);
		for (const l of lines) {
			const door = l.querySelector('a[href]');
			expect(door, `${l.getAttribute('data-attention')} has no door`).not.toBeNull();
		}
		// The waiting host: how long, as a real time with the moment on hover.
		const waitingTime = line('agents:waiting').querySelector('time') as HTMLElement;
		expect(waitingTime.textContent).toBe('for 1h');
		expect(waitingTime.getAttribute('datetime')).toBe(WAITING.attempts[0].first_seen);
		expect(waitingTime.getAttribute('title')).toBeTruthy();
		expect(line('agent:h2').querySelector('time')?.getAttribute('datetime')).toBeTruthy();
		expect(line('task:task-abcdef-1').querySelector('time')?.getAttribute('datetime')).toBe(
			FAILED_TASK.finished_at,
		);
		// The head says how much it looked at.
		expect(screen.getByText(/open · checked 6 of 6 sources/)).toBeInTheDocument();
	});

	it('opens the enrolment window from the line, asking first, and re-reads', async () => {
		getEnrolmentRequests.mockResolvedValue(WAITING);
		openEnrolmentWindow.mockResolvedValue({ open: true, expires_at: null, seconds_remaining: 900, fleet_empty: false });
		seed({});

		render(Overview);
		await attentionZone();
		const row = within(line('agents:waiting'));

		await fireEvent.click(row.getByRole('button', { name: 'Open window' }));
		expect(openEnrolmentWindow).not.toHaveBeenCalled();
		const calls = getDashboard.mock.calls.length;
		// With the window open the hosts get in on their next retry: no line.
		getEnrolmentRequests.mockResolvedValue({ ...WAITING, window: { ...WAITING.window, open: true } });
		await fireEvent.click(row.getByRole('button', { name: 'Open for 15 min' }));
		await waitFor(() => expect(openEnrolmentWindow).toHaveBeenCalledWith(15));
		await waitFor(() => expect(getDashboard.mock.calls.length).toBeGreaterThan(calls));
		expect(await screen.findByText(/Nothing needs you/)).toBeInTheDocument();
	});

	it('offers Open window only to a session that may open it', async () => {
		sessionStore.set({ authenticated: true, token_label: 'w', capabilities: ['read', 'write'] } as never);
		getEnrolmentRequests.mockResolvedValue(WAITING);
		seed({});

		render(Overview);
		await attentionZone();
		expect(within(line('agents:waiting')).queryByRole('button')).toBeNull();
		// The door still goes where an admin can.
		expect(line('agents:waiting').querySelector('a')).toHaveAttribute('href', '/ui/hosts/agents');
	});

	it('re-checks drift from the drift line without leaving the page', async () => {
		seed({ summary: { drift: { total: 3, drifted: 2, in_spec: 1, unknown: 0, checked: 3, in_spec_pct: 33 } } });
		getDriftStatus.mockResolvedValue({ items: [], total: 0 });

		render(Overview);
		await attentionZone();
		const calls = getDashboard.mock.calls.length;
		getDashboard.mockResolvedValue(summary());
		await fireEvent.click(within(line('drift')).getByRole('button', { name: 'Re-check' }));
		await waitFor(() => expect(getDriftStatus).toHaveBeenCalledWith({ refresh: true }));
		await waitFor(() => expect(getDashboard.mock.calls.length).toBeGreaterThan(calls));
		expect(await screen.findByText(/Nothing needs you/)).toBeInTheDocument();
	});

	it('cancels a stuck task from its line, asking first', async () => {
		const stuck = {
			...FAILED_TASK,
			id: 'task-stuck-2',
			status: 'pending',
			error: null,
			created_at: minutesAgo(50),
			finished_at: null,
		};
		seed({ tasks: [stuck] });
		cancelTask.mockResolvedValue({ ...stuck, status: 'cancelled' });

		render(Overview);
		await attentionZone();
		const row = within(line('stuck:task-stuck-2'));
		await fireEvent.click(row.getByRole('button', { name: 'Cancel task' }));
		expect(cancelTask).not.toHaveBeenCalled();
		listTasks.mockResolvedValue({ items: [{ ...stuck, status: 'cancelled' }], total: 1 });
		await fireEvent.click(row.getByRole('button', { name: 'Confirm cancel' }));
		await waitFor(() => expect(cancelTask).toHaveBeenCalledWith('task-stuck-2'));
		expect(await screen.findByText(/Nothing needs you/)).toBeInTheDocument();
	});

	it('says what a calm screen checked, and when', async () => {
		seed({ hosts: [HEALTHY_HOST] });

		render(Overview);

		const calm = await screen.findByText(/Nothing needs you/);
		const sentence = calm.closest('[data-calm]') as HTMLElement;
		expect(sentence.textContent).toMatch(/Nothing needs you - checked 6 sources just now\./);
		expect(within(sentence).getByTitle(/the host inventory, firing alerts, recent tasks/)).toBeInTheDocument();
		expect(screen.getByText(/last read just now/)).toBeInTheDocument();
	});
});
