import { render, screen, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../lib/test-mocks';
import Overview from './+page.svelte';
import { api } from '$lib/api';

/**
 * The first-run path (#445 A7).
 *
 * A fresh install landed on 0% coverage, three empty donuts and no indication of
 * what to do next. The checklist is driven entirely by the summary the dashboard
 * already loads, so it cannot claim a step happened that did not.
 *
 * Teeth: render the section unconditionally and "hides itself" fails; drop the
 * "Do this next" link and the operator has a list with nothing to click.
 */
const getDashboard = api.getDashboard as ReturnType<typeof vi.fn>;

function summary(done: string[], complete = false) {
	const steps = [
		{ key: 'inventory', title: 'Get a host into inventory', detail: 'Sync…', href: '/inventory' },
		{ key: 'adopt', title: 'Adopt a host to manage', detail: 'Adopting…', href: '/inventory' },
		{ key: 'agent', title: 'Install the agent on it', detail: 'The agent…', href: '/agents' },
		{
			key: 'artifact',
			title: 'Approve and apply your first change',
			detail: 'Propose…',
			href: '/artifacts',
		},
	].map((s) => ({ ...s, done: done.includes(s.key) }));
	return {
		onboarding: { steps, complete },
		inventory: {
			total: 0,
			managed: 0,
			uncovered: 0,
			coverage_pct: 0,
			by_status: {},
			by_role: {},
			by_type: {},
		},
		drift: { total: 0, drifted: 0, in_spec_pct: 100 },
		artifacts: {},
		tasks: {},
		agents: { known: 0, connected: 0 },
		metrics: { firing_alerts: 0, retention_days: 7 },
	};
}

describe('Dashboard: getting started', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser: the test still holds */
		}
	});

	it('tells a fresh install what to do first', async () => {
		getDashboard.mockResolvedValue(summary([]));

		render(Overview);

		expect(await screen.findByText(/getting started/i)).toBeInTheDocument();
		expect(screen.getByText(/get a host into inventory/i)).toBeInTheDocument();
	});

	it('points at exactly one next action', async () => {
		getDashboard.mockResolvedValue(summary(['inventory']));

		render(Overview);

		const next = await screen.findAllByRole('link', { name: /do this next/i });
		expect(next).toHaveLength(1);
		expect(next[0]).toHaveAttribute('href', expect.stringContaining('/inventory'));
	});

	it('reports progress from the estate, not from a stored flag', async () => {
		getDashboard.mockResolvedValue(summary(['inventory', 'adopt']));

		render(Overview);

		expect(await screen.findByText(/2 of 4 done/i)).toBeInTheDocument();
	});

	it('hides itself once the path has been walked', async () => {
		getDashboard.mockResolvedValue(
			summary(['inventory', 'adopt', 'agent', 'artifact'], true)
		);

		render(Overview);

		await waitFor(() => expect(getDashboard).toHaveBeenCalled());
		expect(screen.queryByText(/getting started/i)).not.toBeInTheDocument();
	});
});
