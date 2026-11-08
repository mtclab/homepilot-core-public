import { render, screen, waitFor, cleanup, within } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
// test-mocks MUST come before the components, or they resolve the real $lib/api.
import { goto, setPage } from '../lib/test-mocks';
import { api, hasCookieSession, refreshSession } from '$lib/api';
import { NAV_GROUPS } from '$lib/nav';

import Layout from './+layout.svelte';
import OverviewPage from './+page.svelte';
import HostsPage from './hosts/+page.svelte';
import HostsLayout from './hosts/+layout.svelte';
import HostsAgentsPage from './hosts/agents/+page.svelte';
import MonitoringIndex from './monitoring/+page.svelte';
import MonitoringLayout from './monitoring/+layout.svelte';
import MonitoringAlertsPage from './monitoring/alerts/+page.svelte';
import MonitoringRulesPage from './monitoring/rules/+page.svelte';
import GuestsPage from './guests/+page.svelte';
import ChangesPage from './changes/+page.svelte';
import AssistantsPage from './assistants/+page.svelte';
import RecordsIndex from './records/+page.svelte';
import RecordsTasksPage from './records/tasks/+page.svelte';
import SettingsPage from './settings/+page.svelte';

/**
 * THE NAV GATE (UI slice 1): every entry in the sidebar reaches a rendered
 * page, the groups are the four jobs, Guests shows only where guests exist,
 * and the counts come from the summary or not at all.
 *
 * "Reaches" is the user's outcome, not the link's existence: the entry's
 * route exists, and following it - through any index redirect - renders a
 * page whose title the operator can read.
 *
 * Teeth: add a nav entry with no route and the reach table fails; point an
 * index redirect somewhere without a page and its case fails; drop the gate
 * and the fresh-install case shows Guests; paint a zero (or a failed read as
 * zero) and the count cases fail.
 */

const getDashboard = api.getDashboard as ReturnType<typeof vi.fn>;
const getGuests = api.getGuests as ReturnType<typeof vi.fn>;

type C = unknown;
/** Each nav href: the page it renders, after following an index redirect. */
const REACH: Record<string, { page: C; layout?: C; redirectsTo?: string; final?: C; title: string }> = {
	'/': { page: OverviewPage, title: 'Overview' },
	'/hosts': { page: HostsPage, layout: HostsLayout, title: 'Hosts' },
	'/monitoring': {
		page: MonitoringIndex,
		redirectsTo: '/ui/monitoring/alerts',
		final: MonitoringAlertsPage,
		layout: MonitoringLayout,
		title: 'Monitoring',
	},
	'/guests': { page: GuestsPage, title: 'Guests' },
	'/changes': { page: ChangesPage, title: 'Artifacts' },
	'/assistants': { page: AssistantsPage, title: 'Assistants' },
	'/records': {
		page: RecordsIndex,
		redirectsTo: '/ui/records/tasks',
		final: RecordsTasksPage,
		title: 'Tasks',
	},
	'/settings': { page: SettingsPage, title: 'Settings' },
};

const ALL_HREFS = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.href));

afterEach(() => cleanup());

describe('every nav entry reaches a rendered page', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('the reach table covers exactly the nav', () => {
		expect(Object.keys(REACH).sort()).toEqual([...ALL_HREFS].sort());
	});

	for (const href of ALL_HREFS) {
		it(`${href} has a route and renders its page`, async () => {
			const r = REACH[href];
			expect(r, `no reach entry for ${href}`).toBeTruthy();
			const dir = resolve(process.cwd(), 'src/routes' + (href === '/' ? '' : href));
			expect(existsSync(resolve(dir, '+page.svelte')), `${href}: no +page.svelte`).toBe(true);
			setPage('/ui' + (href === '/' ? '/' : href));

			let page = r.page;
			if (r.redirectsTo) {
				render(r.page as never);
				await waitFor(() => expect(goto).toHaveBeenCalled());
				expect(String((goto as ReturnType<typeof vi.fn>).mock.calls[0][0])).toBe(r.redirectsTo);
				cleanup();
				setPage(r.redirectsTo);
				page = r.final;
			}
			if (r.layout) render(r.layout as never);
			const { container } = render(page as never);
			expect(await screen.findByRole('heading', { level: 1, name: r.title })).toBeInTheDocument();
			expect((container.textContent ?? '').trim().length).toBeGreaterThan(0);
		});
	}
});

describe('the moved panels render on their new pages', () => {
	beforeEach(() => vi.clearAllMocks());

	it('Monitoring > Rules has the rules, the channels and the webhooks', async () => {
		setPage('/ui/monitoring/rules');
		render(MonitoringRulesPage);
		for (const name of ['Alert rules', 'Notification channels', 'Webhooks']) {
			expect(screen.getByRole('heading', { name })).toBeInTheDocument();
		}
		await waitFor(() => expect(api.listAlertRules).toHaveBeenCalled());
		await waitFor(() => expect(api.listNotificationChannels).toHaveBeenCalled());
		await waitFor(() => expect(api.listWebhooks).toHaveBeenCalled());
	});

	it('Monitoring > Alerts reads what fired', async () => {
		setPage('/ui/monitoring/alerts');
		render(MonitoringAlertsPage);
		await waitFor(() => expect(api.listAlertEvents).toHaveBeenCalled());
	});

	it('Hosts > Agents has the join requests and the enrol controls', async () => {
		setPage('/ui/hosts/agents');
		render(HostsAgentsPage);
		await waitFor(() => expect(api.getEnrolmentRequests).toHaveBeenCalled());
		expect(screen.getByRole('button', { name: 'Enroll agent' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Hub token' })).toBeInTheDocument();
	});

	it('Guests reads the guest overview', async () => {
		setPage('/ui/guests');
		render(GuestsPage);
		await waitFor(() => expect(getGuests).toHaveBeenCalled());
	});

	it('Assistants is the token list and says what is not built', async () => {
		setPage('/ui/assistants');
		render(AssistantsPage);
		await waitFor(() => expect(api.listTokens).toHaveBeenCalled());
		expect(screen.getByTestId('assistants-not-built').textContent).toMatch(/not built yet/);
	});
});

describe('the sidebar', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/');
		(hasCookieSession as ReturnType<typeof vi.fn>).mockReturnValue(true);
		(refreshSession as ReturnType<typeof vi.fn>).mockResolvedValue({
			authenticated: true,
			token_label: 'test',
			capabilities: ['read', 'write', 'admin'],
		});
		getDashboard.mockResolvedValue(null);
		getGuests.mockResolvedValue({ guests: [], invites: [] });
	});
	afterEach(() => {
		(hasCookieSession as ReturnType<typeof vi.fn>).mockReturnValue(false);
		(refreshSession as ReturnType<typeof vi.fn>).mockResolvedValue(null);
	});

	function nav() {
		return within(screen.getByRole('navigation', { name: 'Main' }));
	}

	it('groups the nouns as four jobs, in order', async () => {
		render(Layout);
		await waitFor(() => expect(getDashboard).toHaveBeenCalled());
		const links = nav()
			.getAllByRole('link')
			.filter((a) => a.hasAttribute('data-nav'))
			.map((a) => a.getAttribute('href'));
		expect(links).toEqual([
			'/ui/',
			'/ui/hosts',
			'/ui/monitoring',
			'/ui/changes',
			'/ui/assistants',
			'/ui/records',
			'/ui/settings',
		]);
		const text = screen.getByRole('navigation', { name: 'Main' }).textContent ?? '';
		for (const g of ['Estate', 'Change', 'Record']) expect(text).toContain(g);
	});

	it('hides Guests on an install with no guest and no invite', async () => {
		render(Layout);
		await waitFor(() => expect(getGuests).toHaveBeenCalled());
		expect(nav().queryByRole('link', { name: /^Guests/ })).toBeNull();
	});

	it('shows Guests once an invite exists, between Monitoring and Changes', async () => {
		getGuests.mockResolvedValue({
			guests: [],
			invites: [{ id: 'i1', prefix: 'p', cn: 'friend', state: 'pending' }],
		});
		render(Layout);
		const link = await nav().findByRole('link', { name: /^Guests/ });
		expect(link).toHaveAttribute('href', '/ui/guests');
		const order = nav()
			.getAllByRole('link')
			.filter((a) => a.hasAttribute('data-nav'))
			.map((a) => a.getAttribute('data-nav'));
		expect(order.indexOf('/guests')).toBe(order.indexOf('/monitoring') + 1);
	});

	it('never asks a non-admin session for the guest overview, and hides Guests', async () => {
		(refreshSession as ReturnType<typeof vi.fn>).mockResolvedValue({
			authenticated: true,
			token_label: 'ro',
			capabilities: ['read'],
		});
		render(Layout);
		await waitFor(() => expect(getDashboard).toHaveBeenCalled());
		expect(getGuests).not.toHaveBeenCalled();
		expect(nav().queryByRole('link', { name: /^Guests/ })).toBeNull();
	});

	it('counts firing alerts on Monitoring and proposals on Changes, from the summary', async () => {
		getDashboard.mockResolvedValue({
			metrics: { firing_alerts: 2 },
			artifacts: { proposed: 3, applied: 9 },
		});
		render(Layout);
		const mon = await nav().findByRole('link', { name: /^Monitoring\W+2 firing$/ });
		expect(mon.querySelector('[data-count="firing"]')?.className).toContain('nav-count-warn');
		expect(nav().getByRole('link', { name: /^Changes\W+3 proposed$/ })).toBeInTheDocument();
	});

	it('shows no count at all when nothing fires and nothing is proposed', async () => {
		getDashboard.mockResolvedValue({ metrics: { firing_alerts: 0 }, artifacts: { proposed: 0 } });
		render(Layout);
		await waitFor(() => expect(getDashboard).toHaveBeenCalled());
		expect(nav().getByRole('link', { name: 'Monitoring' })).toBeInTheDocument();
		expect(document.querySelectorAll('[data-count]')).toHaveLength(0);
	});

	it('an unreadable summary shows no count rather than a zero', async () => {
		getDashboard.mockRejectedValue(new Error('503'));
		render(Layout);
		await waitFor(() => expect(getDashboard).toHaveBeenCalled());
		expect(nav().getByRole('link', { name: 'Changes' })).toBeInTheDocument();
		expect(document.querySelectorAll('[data-count]')).toHaveLength(0);
	});

	it('marks the page the operator is on, including views under a section', async () => {
		setPage('/ui/monitoring/rules');
		render(Layout);
		await waitFor(() => expect(getDashboard).toHaveBeenCalled());
		const current = nav()
			.getAllByRole('link')
			.filter((a) => a.getAttribute('aria-current') === 'page');
		expect(current.map((a) => a.getAttribute('data-nav'))).toEqual(['/monitoring']);
	});

	it('carries the theme control and the maker’s mark in its foot', async () => {
		render(Layout);
		await waitFor(() => expect(getDashboard).toHaveBeenCalled());
		const side = screen.getByRole('navigation', { name: 'Main' });
		expect(within(side).getByRole('group', { name: 'Theme' })).toBeInTheDocument();
		const mark = within(side).getByRole('link', { name: /Built by MTC/ });
		expect(mark).toHaveAttribute('href', 'https://mtclab.net');
	});
});
