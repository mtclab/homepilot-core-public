import { render, screen, waitFor, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage, goto } from '../../lib/test-mocks';
import HostPage from './[id]/+page.svelte';
import { api, hostMetricsUrl } from '$lib/api';
import { HOST_TABS, hostTabFromHash } from '$lib/hostTabs';
import { activeQueryTab } from '$lib/tabs';

/**
 * The host page's sectors are ADDRESSES (#549 F3, principle 2).
 *
 * A tab an operator cannot send to a colleague is a tab that lost the argument
 * with the URL bar, and a link written before the tabs existed must not become
 * a dead end - it lands on the tab that swallowed its section.
 *
 * Teeth: make the tabs local component state instead of `?tab=` and the
 * addressability tests fail; drop the legacy-fragment resolution and the
 * deep-link test fails; make an unknown tab render nothing and the stale
 * bookmark test fails on an empty page rather than on Overview.
 */
const getHost = api.getHost as ReturnType<typeof vi.fn>;

const HOST = {
	id: 'h-1',
	hostname: 'web01',
	status: 'online',
	role: 'guest',
	source: 'agent',
	agent_id: 'ag-1',
	services: [],
	agent: { agent_id: 'ag-1', connected: true, version: 'v3.0.0' },
};

function at(query = ''): void {
	setPage(`/ui/hosts/h-1${query}`, { id: 'h-1' });
}

/** The tab the rendered page says is current. */
function currentTab(): string {
	const selected = screen
		.getAllByRole('tab')
		.filter((t) => t.getAttribute('aria-selected') === 'true');
	expect(selected).toHaveLength(1);
	return selected[0].textContent ?? '';
}

describe('Host page tab addressing', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		at();
		getHost.mockResolvedValue(HOST);
		(api.getHostDoc as ReturnType<typeof vi.fn>).mockResolvedValue({
			target: 'web01',
			hosts: [HOST],
			services: [],
			kb_entries: [],
			artifact_history: [],
		});
	});

	it('offers the five sectors, in order, each as its own URL', async () => {
		render(HostPage);
		await waitFor(() => expect(screen.getAllByRole('tab').length).toBe(5));

		expect(screen.getAllByRole('tab').map((t) => [t.textContent, t.getAttribute('href')])).toEqual(
			[
				['Overview', '/ui/hosts/h-1?tab=overview'],
				['Metrics', '/ui/hosts/h-1?tab=metrics'],
				['Changes', '/ui/hosts/h-1?tab=changes'],
				['Activity', '/ui/hosts/h-1?tab=activity'],
				['Agent', '/ui/hosts/h-1?tab=agent'],
			],
		);
	});

	it('is a real tab bar, keyboard-reachable, with a labelled panel', async () => {
		at('?tab=agent');
		render(HostPage);
		await waitFor(() => expect(screen.getByRole('tablist', { name: 'Host views' })).toBeTruthy());

		const panel = screen.getByRole('tabpanel');
		expect(panel).toHaveAttribute('id', 'host-panel');
		expect(panel).toHaveAttribute('aria-labelledby', 'tab-agent');
		// Roving tabindex: exactly one tab is in the tab order, and it is the
		// current one - the row can be reached and walked with a keyboard.
		const inOrder = screen.getAllByRole('tab').filter((t) => t.getAttribute('tabindex') === '0');
		expect(inOrder.map((t) => t.textContent)).toEqual(['Agent']);
	});

	for (const t of HOST_TABS) {
		it(`?tab=${t.id} lands on ${t.label}`, async () => {
			at(`?tab=${t.id}`);
			render(HostPage);
			await waitFor(() => expect(screen.getAllByRole('tab').length).toBe(5));
			expect(currentTab()).toBe(t.label);
		});
	}

	it('an absent or unrecognised tab lands on Overview, never on nothing', async () => {
		render(HostPage);
		await waitFor(() => expect(screen.getAllByRole('tab').length).toBe(5));
		expect(currentTab()).toBe('Overview');

		cleanup();
		// A bookmark from before a sector was renamed. It must still land on a
		// real sector rather than rendering an empty panel.
		at('?tab=retired-section');
		render(HostPage);
		await waitFor(() => expect(screen.getAllByRole('tab').length).toBe(5));
		expect(currentTab()).toBe('Overview');
		expect(screen.getByText('Needs attention')).toBeTruthy();
	});

	it('keeps any other query the URL is carrying when switching sector', async () => {
		at('?from=overview');
		render(HostPage);
		await waitFor(() => expect(screen.getAllByRole('tab').length).toBe(5));
		expect(screen.getByRole('tab', { name: 'Metrics' })).toHaveAttribute(
			'href',
			'/ui/hosts/h-1?from=overview&tab=metrics',
		);
	});

	it('a pre-tabs fragment link lands on the tab that swallowed its section', async () => {
		at('#agent');
		render(HostPage);
		// The GOAL: the URL is rewritten to the canonical address, so the operator
		// who follows an old link can copy a new one.
		await waitFor(() =>
			expect(goto).toHaveBeenCalledWith(
				'/ui/hosts/h-1?tab=agent',
				expect.objectContaining({ replaceState: true }),
			),
		);
	});

	it('leaves a fragment it does not recognise alone', async () => {
		at('#some-users-own-anchor');
		render(HostPage);
		await waitFor(() => expect(screen.getAllByRole('tab').length).toBe(5));
		// No invented sector, no history churn - Overview answers, as it does for
		// any address that names no tab.
		expect(goto).not.toHaveBeenCalled();
		expect(currentTab()).toBe('Overview');
	});

	it('does not override an explicit ?tab= with a stale fragment', async () => {
		at('?tab=changes#agent');
		render(HostPage);
		await waitFor(() => expect(screen.getAllByRole('tab').length).toBe(5));
		expect(goto).not.toHaveBeenCalled();
		expect(currentTab()).toBe('Changes');
	});

	it('the fleet page\'s "Metrics" row action lands ON the metrics tab', () => {
		// The one link in the console that PROMISES a sector. Landing on Overview
		// would be a broken promise the operator has to fix by hand every time.
		const href = hostMetricsUrl('/ui', 'web01', 'h-1');
		expect(href).toBe('/ui/hosts/h-1?tab=metrics');
		expect(activeQueryTab(HOST_TABS, new URL('http://localhost' + href))).toBe('metrics');
	});
});

describe('legacy fragment mapping', () => {
	it('maps every section heading the page used to have', () => {
		expect(hostTabFromHash('#metrics')).toBe('metrics');
		expect(hostTabFromHash('#changes')).toBe('changes');
		expect(hostTabFromHash('#activity')).toBe('activity');
		expect(hostTabFromHash('#agent')).toBe('agent');
		// Facts, Services and Notes were headings of their own; they live on
		// Overview now, so their links land there rather than nowhere.
		expect(hostTabFromHash('#facts')).toBe('overview');
		expect(hostTabFromHash('#services')).toBe('overview');
		expect(hostTabFromHash('#notes')).toBe('overview');
	});

	it('resolves to a tab that actually exists', () => {
		const ids = new Set(HOST_TABS.map((t) => t.id));
		for (const frag of ['#metrics', '#changes', '#activity', '#agent', '#facts']) {
			expect(ids.has(hostTabFromHash(frag))).toBe(true);
		}
	});

	it('claims nothing about a fragment it never had', () => {
		expect(hostTabFromHash('#tokens')).toBe('');
		expect(hostTabFromHash('')).toBe('');
	});
});
