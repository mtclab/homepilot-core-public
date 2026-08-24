import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../test-mocks';
import EnrollAgent from './EnrollAgent.svelte';
import { api } from '$lib/api';

/**
 * The enrolment window in the enrol panel (#537).
 *
 * The failure this forbids: an operator copies the shared-token one-liner, runs
 * it on a new box, and the agent is refused by a rule the UI never mentioned.
 * The panel has to say - BEFORE the copy - whether that host can join, and let
 * the operator open the window from the same place.
 */
const getEnrolmentWindow = api.getEnrolmentWindow as ReturnType<typeof vi.fn>;
const openEnrolmentWindow = api.openEnrolmentWindow as ReturnType<typeof vi.fn>;
const closeEnrolmentWindow = api.closeEnrolmentWindow as ReturnType<typeof vi.fn>;

const CLOSED = { open: false, expires_at: null, seconds_remaining: 0, fleet_empty: false };
const OPEN = {
	open: true,
	expires_at: '2026-08-24T12:00:00Z',
	seconds_remaining: 900,
	fleet_empty: false,
};

describe('EnrollAgent: the enrolment window', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		(api.getHubToken as ReturnType<typeof vi.fn>).mockResolvedValue({
			auth_token: 'shared',
			hub_host: 'hub.example',
			hub_port: 8443,
		});
	});

	it('warns that a new host will be refused while the window is shut', async () => {
		getEnrolmentWindow.mockResolvedValue(CLOSED);

		render(EnrollAgent, { props: { show: 'hub' } });

		const state = await screen.findByTestId('enrolment-window-state');
		expect(state.textContent).toMatch(/closed/i);
		expect(state.textContent).toMatch(/refused/i);
	});

	it('opens the window from the panel and shows it as open', async () => {
		getEnrolmentWindow.mockResolvedValue(CLOSED);
		openEnrolmentWindow.mockResolvedValue(OPEN);

		render(EnrollAgent, { props: { show: 'hub' } });
		await screen.findByTestId('enrolment-window-state');

		await fireEvent.click(screen.getByRole('button', { name: 'Open window' }));

		expect(openEnrolmentWindow).toHaveBeenCalledWith(15);
		await waitFor(() =>
			expect(screen.getByTestId('enrolment-window-state').textContent).toMatch(/open until/i)
		);
		// And it can be shut again without leaving the panel.
		closeEnrolmentWindow.mockResolvedValue(CLOSED);
		await fireEvent.click(screen.getByRole('button', { name: 'Close now' }));
		expect(closeEnrolmentWindow).toHaveBeenCalled();
	});

	it('says the first host needs no window on an install with no agents', async () => {
		getEnrolmentWindow.mockResolvedValue({ ...CLOSED, fleet_empty: true });

		render(EnrollAgent, { props: { show: 'hub' } });

		const state = await screen.findByTestId('enrolment-window-state');
		expect(state.textContent).toMatch(/No agents enrolled yet/i);
		expect(state.textContent).not.toMatch(/refused/i);
	});
});
