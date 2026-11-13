import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '../test-mocks';
import AlertsPanel from './AlertsPanel.svelte';
import Overview from '../../routes/+page.svelte';
import AlertsPage from '../../routes/monitoring/alerts/+page.svelte';
import { api, sessionStore, type AlertEvent, type ChannelSummary } from '$lib/api';

/**
 * The Alerts list (#660): "did anything fire last night, and was anyone told?"
 *
 * The journey asserted is the morning one: a host went dark at night and came
 * back. The Overview must say so after the fact (the latch alone kept nothing),
 * say whether the phone got it, and say in words where alerts go - loudest when
 * the answer is "nowhere".
 *
 * Teeth (each verified by reverting the behaviour):
 *   - render only the firing list and the resolved episode case fails;
 *   - drop the destination line and the "no channel" case fails;
 *   - treat a rejected history read as empty and the unread case fails;
 *   - remove the Alerts number from the Overview and the Overview case fails;
 *     put the full list back on the Overview and it fails too (UI slice 3:
 *     the list lives on Monitoring > Alerts, the Overview keeps one number).
 */
const listFiringAlerts = api.listFiringAlerts as ReturnType<typeof vi.fn>;
const listAlertEvents = api.listAlertEvents as ReturnType<typeof vi.fn>;

const PHONE_OK: ChannelSummary = {
	id: 1,
	kind: 'ntfy',
	name: 'phone',
	enabled: true,
	state: 'ok',
	last_ok_at: '2026-11-07T06:00:00Z',
	last_error_at: null,
};

const DARK_RESOLVED: AlertEvent = {
	id: 2,
	rule_id: 'r1',
	rule_name: 'Host went dark',
	rule_kind: 'nodata',
	metric: '*',
	hostname: 'web-01',
	event: 'resolved',
	value: 12,
	since: '2026-11-07T02:14:00Z',
	duration_seconds: 420,
	reason: null,
	delivered_to: ['phone'],
	at: '2026-11-07T02:21:00Z',
};
const DARK_FIRING: AlertEvent = { ...DARK_RESOLVED, id: 1, event: 'firing', duration_seconds: null };

function history(items: AlertEvent[], channels: ChannelSummary[]) {
	return { items, total: items.length, since: '', hours: 24, hostname: null, channels };
}

describe('AlertsPanel', () => {
	beforeEach(() => vi.clearAllMocks());

	it('lists a host that went dark and came back, with how long and who was told', async () => {
		listFiringAlerts.mockResolvedValue({ items: [], total: 0 });
		listAlertEvents.mockResolvedValue(history([DARK_RESOLVED, DARK_FIRING], [PHONE_OK]));

		render(AlertsPanel, { props: { hostHref: (h: string) => `/ui/hosts/id-${h}` } });

		const resolved = await screen.findByText('went dark for 7 min');
		const row = resolved.closest('li') as HTMLElement;
		expect(within(row).getByRole('link', { name: 'web-01' })).toHaveAttribute('href', '/ui/hosts/id-web-01');
		expect(within(row).getByText('sent to phone')).toBeInTheDocument();
		// The firing half of the same episode is history, not "firing now".
		expect(screen.getByText('Firing now (0)')).toBeInTheDocument();
		expect(screen.getByText(/Alerts go to phone \(ntfy, last delivery ok\)/)).toBeInTheDocument();
	});

	it('shows what is firing now', async () => {
		listFiringAlerts.mockResolvedValue({
			items: [
				{
					rule_id: 'r1',
					hostname: 'web-01',
					firing_since: '2026-11-07T02:14:00Z',
					last_value: 360,
					last_eval: '2026-11-07T02:20:00Z',
					name: 'Host went dark',
					metric: '*',
					comparison: 'gt',
					threshold: 0,
					for_seconds: 300,
					kind: 'nodata',
				},
			],
			total: 1,
		});
		listAlertEvents.mockResolvedValue(history([DARK_FIRING], [PHONE_OK]));
		render(AlertsPanel);
		expect(await screen.findByText('went dark - Host went dark')).toBeInTheDocument();
		expect(screen.getByText('Firing now (1)')).toBeInTheDocument();
	});

	it('says plainly when no channel exists and when an alert reached nobody', async () => {
		listFiringAlerts.mockResolvedValue({ items: [], total: 0 });
		listAlertEvents.mockResolvedValue(history([{ ...DARK_RESOLVED, delivered_to: [] }], []));
		render(AlertsPanel);
		expect(await screen.findByText(/no notification channel is set up/)).toBeInTheDocument();
		expect(screen.getByText('nobody was notified')).toBeInTheDocument();
	});

	it('names a failing channel as failing', async () => {
		listFiringAlerts.mockResolvedValue({ items: [], total: 0 });
		listAlertEvents.mockResolvedValue(history([], [{ ...PHONE_OK, state: 'failing' }]));
		render(AlertsPanel);
		const line = await screen.findByText(/last delivery FAILED/);
		expect(line.className).toContain('text-danger');
	});

	it('never reports "nothing fired" when the history could not be read', async () => {
		listFiringAlerts.mockResolvedValue({ items: [], total: 0 });
		listAlertEvents.mockRejectedValue(new Error('503'));
		render(AlertsPanel);
		expect(await screen.findByRole('alert')).toHaveTextContent('Could not read the alert history');
		expect(screen.queryByText(/Nothing fired and resolved/)).toBeNull();
	});

	it('asks for the longer window when 7 d is pressed', async () => {
		listFiringAlerts.mockResolvedValue({ items: [], total: 0 });
		listAlertEvents.mockResolvedValue(history([], [PHONE_OK]));
		render(AlertsPanel, { props: { hostname: 'web-01' } });
		await screen.findByText(/Nothing fired and resolved in the last 24 h/);
		expect(listAlertEvents).toHaveBeenLastCalledWith(24, 'web-01');
		(screen.getByRole('button', { name: '7 d' }) as HTMLButtonElement).click();
		await screen.findByText(/Nothing fired and resolved in the last 7 d/);
		expect(listAlertEvents).toHaveBeenLastCalledWith(168, 'web-01');
	});
});

// 3.12: the strip became the Estate panel's Alerts number - how many fire, how
// many resolved, where an alert goes in a few words - with the same door.
describe('Overview carries the Alerts number', () => {
	beforeEach(() => vi.clearAllMocks());

	it('counts the resolved dark-host episode in two lines and doors to Monitoring > Alerts', async () => {
		(api.getDashboard as ReturnType<typeof vi.fn>).mockResolvedValue({
			onboarding: { steps: [], complete: true },
			inventory: { total: 1, managed: 1, uncovered: 0, coverage_pct: 100, by_status: {}, by_role: {}, by_type: {} },
			drift: { total: 0, drifted: 0, in_spec: 0, unknown: 0, checked: 0, in_spec_pct: 0 },
			artifacts: {},
			tasks: {},
			agents: { known: 1, connected: 1 },
			metrics: { firing_alerts: 0, retention_days: 7, rules_enabled: 3, rules_watching_nothing: 0 },
		});
		(api.listInventory as ReturnType<typeof vi.fn>).mockResolvedValue({
			items: [{ id: 'h1', hostname: 'web-01', status: 'online', agent_id: 'a1', agent_connected: true }],
			total: 1,
		});
		listFiringAlerts.mockResolvedValue({ items: [], total: 0 });
		(api.listTasks as ReturnType<typeof vi.fn>).mockResolvedValue({ items: [], total: 0 });
		(api.listAudit as ReturnType<typeof vi.fn>).mockResolvedValue({ items: [], total: 0 });
		listAlertEvents.mockResolvedValue(history([DARK_RESOLVED], [PHONE_OK]));

		render(Overview);
		await screen.findByRole('heading', { name: 'Estate' });
		const alerts = () => document.querySelector('[data-readout="Alerts"]') as HTMLElement;
		await waitFor(() => expect(alerts().textContent).toContain('1 resolved in 24 h'));
		expect(alerts().textContent).toMatch(/^\s*Alerts\s*0/);
		expect(alerts().textContent).toContain('phone ok');
		expect(alerts()).toHaveAttribute('href', '/ui/monitoring/alerts');
		// The list is the Alerts page's; the Overview never repeats it.
		expect(screen.queryByText('went dark for 7 min')).not.toBeInTheDocument();
		expect(document.querySelector('[data-alerts-panel]')).toBeNull();
	});

	it('says it could not read, never "0 firing", when the reads failed', async () => {
		(api.getDashboard as ReturnType<typeof vi.fn>).mockResolvedValue({
			onboarding: { steps: [], complete: true },
			inventory: { total: 0, managed: 0, uncovered: 0, coverage_pct: 0, by_status: {}, by_role: {}, by_type: {} },
			drift: { total: 0, drifted: 0, in_spec: 0, unknown: 0, checked: 0, in_spec_pct: 0 },
			artifacts: {},
			tasks: {},
			agents: { known: 0, connected: 0 },
			metrics: { firing_alerts: 0, retention_days: 7, rules_enabled: 3, rules_watching_nothing: 0 },
		});
		(api.listInventory as ReturnType<typeof vi.fn>).mockResolvedValue({ items: [], total: 0 });
		(api.listTasks as ReturnType<typeof vi.fn>).mockResolvedValue({ items: [], total: 0 });
		(api.listAudit as ReturnType<typeof vi.fn>).mockResolvedValue({ items: [], total: 0 });
		listFiringAlerts.mockRejectedValue(new Error('503'));
		listAlertEvents.mockRejectedValue(new Error('503'));

		render(Overview);
		await screen.findByRole('heading', { name: 'Estate' });
		const alerts = () => document.querySelector('[data-readout="Alerts"]') as HTMLElement;
		await waitFor(() => expect(alerts().textContent).toContain('could not read what is firing'));
		expect(alerts().textContent).toMatch(/^\s*Alerts\s*\?/);
		expect(alerts().textContent).not.toMatch(/Alerts\s*0/);
	});
});

/**
 * Monitoring > Alerts as a page (UI slice 3, plan 3.5): the firing alert links
 * to its rule, every known delivery is a row, "Send test" proves the path to
 * the phone, and the page first says how much of the estate is watched at all.
 *
 * Teeth (each verified by reverting the behaviour):
 *   - drop the rule link and the firing case fails on the missing link;
 *   - list only resolved deliveries (or none) and the deliveries case fails;
 *   - send the test to a disabled channel too, or not at all, and the Send
 *     test case fails on the calls;
 *   - count every host as watched and the Watching case fails on 1/3.
 */

const FIRING_DISK = {
	rule_id: 'r-disk',
	hostname: 'web-01',
	firing_since: '2026-11-07T02:14:00Z',
	last_value: 97,
	last_eval: '2026-11-07T02:20:00Z',
	name: 'Disk over 90 %',
	metric: 'disk.used_pct',
	comparison: 'gt' as const,
	threshold: 90,
	for_seconds: 600,
	kind: 'threshold' as const,
};
const DISK_FIRED: AlertEvent = {
	...DARK_RESOLVED,
	id: 7,
	rule_id: 'r-disk',
	rule_name: 'Disk over 90 %',
	rule_kind: 'threshold',
	metric: 'disk.used_pct',
	event: 'firing',
	duration_seconds: null,
	delivered_to: ['phone'],
	at: '2026-11-07T02:14:05Z',
};

describe('AlertsPanel as the Alerts page', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		sessionStore.set({ authenticated: true, token_label: 'test', capabilities: ['read', 'write', 'admin'] } as never);
	});

	it("links each firing alert to its rule, and lists who was told about every transition", async () => {
		listFiringAlerts.mockResolvedValue({ items: [FIRING_DISK], total: 1 });
		listAlertEvents.mockResolvedValue(
			history(
				[
					DISK_FIRED,
					{ ...DARK_RESOLVED, delivered_to: [] },
					// Still being delivered: not a failure, not listed yet.
					{ ...DARK_FIRING, id: 9, delivered_to: null, at: '2026-11-07T03:00:00Z' },
				],
				[PHONE_OK],
			),
		);
		render(AlertsPanel, { props: { page: true, hostHref: (h: string) => `/ui/hosts/id-${h}` } });

		const firing = (await screen.findByText(/Disk over 90 %: disk.used_pct 97/)).closest('li') as HTMLElement;
		expect(within(firing).getByRole('link', { name: 'rule' })).toHaveAttribute('href', '/ui/monitoring/rules#rule-r-disk');

		const list = document.querySelector('[data-alerts-deliveries]') as HTMLElement;
		const rows = list.querySelectorAll('li');
		expect(rows).toHaveLength(2);
		expect(rows[0].textContent).toContain('went dark for 7 min, resolved');
		expect(rows[0].textContent).toContain('nobody was notified');
		expect(rows[1].textContent).toContain('Disk over 90 % fired');
		expect(rows[1].textContent).toContain('sent to phone');
		expect(screen.getByText('Deliveries in the last 24 h (2)')).toBeInTheDocument();
	});

	it('the host page keeps the plain list: no rule links, no deliveries, no Send test', async () => {
		listFiringAlerts.mockResolvedValue({ items: [FIRING_DISK], total: 1 });
		listAlertEvents.mockResolvedValue(history([DISK_FIRED], [PHONE_OK]));
		render(AlertsPanel, { props: { hostname: 'web-01' } });
		await screen.findByText(/Disk over 90 %: disk.used_pct 97/);
		expect(screen.queryByRole('link', { name: 'rule' })).toBeNull();
		expect(document.querySelector('[data-alerts-deliveries]')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Send test' })).toBeNull();
	});

	it('Send test reaches every enabled channel and says what each answered', async () => {
		const testChannel = api.testNotificationChannel as ReturnType<typeof vi.fn>;
		testChannel.mockImplementation(async (id: number) =>
			id === 1
				? { id, endpoint: 'https://ntfy.example.com', delivered: true, error: null }
				: { id, endpoint: 'https://push.example.com', delivered: false, error: '403 forbidden' },
		);
		listFiringAlerts.mockResolvedValue({ items: [], total: 0 });
		listAlertEvents.mockResolvedValue(
			history(
				[],
				[
					PHONE_OK,
					{ ...PHONE_OK, id: 2, name: 'tablet', state: 'untested' },
					{ ...PHONE_OK, id: 3, name: 'old', enabled: false, state: 'disabled' },
				],
			),
		);
		render(AlertsPanel, { props: { page: true } });

		await fireEvent.click(await screen.findByRole('button', { name: 'Send test' }));
		const result = await screen.findByText(/^Test message - /);
		expect(testChannel.mock.calls.map((c) => c[0])).toEqual([1, 2]);
		expect(result.textContent).toContain('phone: accepted');
		expect(result.textContent).toContain('tablet: NOT accepted (403 forbidden)');
		expect(result.className).toContain('text-danger');
		// The channel state changed with the test: the history is read again.
		await waitFor(() => expect(listAlertEvents.mock.calls.length).toBeGreaterThanOrEqual(2));
	});

	it('says why there is no Send test for a non-admin session, and doors to channels when there are none', async () => {
		listFiringAlerts.mockResolvedValue({ items: [], total: 0 });
		listAlertEvents.mockResolvedValue(history([], [PHONE_OK]));
		sessionStore.set({ authenticated: true, token_label: 'ro', capabilities: ['read'] } as never);
		const { unmount } = render(AlertsPanel, { props: { page: true } });
		expect(await screen.findByText('Sending a test needs an admin session.')).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Send test' })).toBeNull();
		unmount();

		sessionStore.set({ authenticated: true, token_label: 'test', capabilities: ['read', 'write', 'admin'] } as never);
		listAlertEvents.mockResolvedValue(history([], []));
		render(AlertsPanel, { props: { page: true } });
		expect(await screen.findByRole('link', { name: 'Add a channel' })).toHaveAttribute(
			'href',
			'/ui/monitoring/rules#notification-channels',
		);
	});
});

describe('Monitoring > Alerts page', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		sessionStore.set({ authenticated: true, token_label: 'test', capabilities: ['read', 'write', 'admin'] } as never);
		listFiringAlerts.mockResolvedValue({ items: [], total: 0 });
		listAlertEvents.mockResolvedValue(history([], [PHONE_OK]));
	});

	function dashboard(metrics: Record<string, number>) {
		return {
			onboarding: { steps: [], complete: true },
			inventory: { total: 3, managed: 3, uncovered: 0, coverage_pct: 100, by_status: {}, by_role: {}, by_type: {} },
			drift: { total: 0, drifted: 0, in_spec: 0, unknown: 0, checked: 0, in_spec_pct: 0 },
			artifacts: {},
			tasks: {},
			agents: { known: 2, connected: 1 },
			metrics: { firing_alerts: 0, retention_days: 7, ...metrics },
		};
	}

	it('says how much of the estate is watched before it lists anything', async () => {
		(api.listInventory as ReturnType<typeof vi.fn>).mockResolvedValue({
			items: [
				{ id: 'h1', hostname: 'web-01', status: 'online', agent_id: 'a1', agent_connected: true },
				{ id: 'h2', hostname: 'db-01', status: 'online', agent_id: 'a2', agent_connected: false },
				{ id: 'h3', hostname: 'dns', status: 'online' },
			],
			total: 3,
		});
		(api.getDashboard as ReturnType<typeof vi.fn>).mockResolvedValue(
			dashboard({ rules_enabled: 4, rules_watching_nothing: 1 }),
		);
		render(AlertsPage);

		const coverage = (await screen.findByLabelText('What is watched')) as HTMLElement;
		await waitFor(() => expect(coverage.querySelector('[data-readout="Watching"]')).not.toBeNull());
		const r = (label: string) => coverage.querySelector(`[data-readout="${label}"]`) as HTMLElement;
		expect(r('Watching').textContent).toMatch(/1\s*\/3 hosts/);
		expect(r('Not watched').textContent).toMatch(/2\s*hosts/);
		expect(r('Not watched')).toHaveAttribute('href', '/ui/hosts/agents');
		expect(r('Rules').textContent).toMatch(/4\s*on/);
		expect(r('Rules').textContent).toContain('1 matches no host');
		// And the page's list is the full panel, with Send test.
		expect(await screen.findByRole('button', { name: 'Send test' })).toBeInTheDocument();
	});

	// Found on the real build: the page handed the panel a function closing
	// over the inventory, the panel rendered before the inventory answered and
	// never re-rendered, so every host name stayed plain text.
	it('links host names once the inventory answers after the alerts did', async () => {
		let releaseInventory: (v: unknown) => void = () => {};
		(api.listInventory as ReturnType<typeof vi.fn>).mockImplementation(
			() => new Promise((r) => (releaseInventory = r)),
		);
		(api.getDashboard as ReturnType<typeof vi.fn>).mockResolvedValue(dashboard({ rules_enabled: 1, rules_watching_nothing: 0 }));
		listFiringAlerts.mockResolvedValue({ items: [FIRING_DISK], total: 1 });
		listAlertEvents.mockResolvedValue(history([DISK_FIRED], [PHONE_OK]));
		render(AlertsPage);
		const row = (await screen.findByText(/Disk over 90 %: disk.used_pct 97/)).closest('li') as HTMLElement;
		expect(within(row).queryByRole('link', { name: 'web-01' })).toBeNull();

		releaseInventory({ items: [{ id: 'h1', hostname: 'web-01', status: 'online' }], total: 1 });
		await waitFor(() =>
			expect(within(row).getByRole('link', { name: 'web-01' })).toHaveAttribute('href', '/ui/hosts/h1'),
		);
		const deliveries = document.querySelector('[data-alerts-deliveries]') as HTMLElement;
		expect(within(deliveries).getByRole('link', { name: 'web-01' })).toHaveAttribute('href', '/ui/hosts/h1');
	});

	it('never turns an unreadable inventory into "0 watched"', async () => {
		(api.listInventory as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('503'));
		(api.getDashboard as ReturnType<typeof vi.fn>).mockResolvedValue(dashboard({ rules_enabled: 1, rules_watching_nothing: 0 }));
		render(AlertsPage);
		const coverage = (await screen.findByLabelText('What is watched')) as HTMLElement;
		await waitFor(() => expect(coverage.querySelector('[data-readout="Watching"]')).not.toBeNull());
		expect(coverage.querySelector('[data-readout="Watching"]')?.textContent).toContain('could not read the host inventory');
		expect(coverage.textContent).not.toMatch(/\b0\s*\/0/);
	});
});
