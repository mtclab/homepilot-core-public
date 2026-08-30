import { render, screen, waitFor, within } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../lib/test-mocks';
import Overview from './+page.svelte';
import { api } from '$lib/api';

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
			'/ui/changes/review',
		);
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
		expect(screen.getByText(/hosts by status/i)).toBeInTheDocument();
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
