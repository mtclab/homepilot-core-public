import { render, screen, within } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '../test-mocks';
import AlertsPanel from './AlertsPanel.svelte';
import Overview from '../../routes/+page.svelte';
import { api, type AlertEvent, type ChannelSummary } from '$lib/api';

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
 *   - remove <AlertsPanel> from the Overview and the Overview case fails.
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

describe('Overview carries the Alerts list', () => {
	beforeEach(() => vi.clearAllMocks());

	it('shows a resolved dark-host episode on the first screen', async () => {
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
		const heading = await screen.findByRole('heading', { name: 'Alerts' });
		const panel = within(heading.closest('section') as HTMLElement);
		expect(await panel.findByText('went dark for 7 min')).toBeInTheDocument();
		expect(panel.getByRole('link', { name: 'web-01' })).toHaveAttribute('href', '/ui/hosts/h1');
	});
});
