import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import Hosts from './+page.svelte';
import { api } from '$lib/api';

/**
 * Fleet operations on the Hosts page (#514 S3, re-homed by S4 when the Agents
 * tab died). The gate scenario is unchanged - it is the prod cleanup that
 * hurt: a fleet of agents that stopped connecting, cleared one at a time with
 * button-swapping confirms and a reload per action.
 *
 * THE JOURNEY: three hosts, two carrying dead agents → one click selects the
 * disconnected pair → ONE dialog names both → confirm → the agent state flips
 * in place. listInventory is never re-fetched, and the hosts stay in the list
 * (the MACHINES exist; only the agents died).
 */
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;
const forgetAgent = api.forgetAgent as ReturnType<typeof vi.fn>;

function host(id: string, hostname: string, agentConnected: boolean | null) {
	return {
		id,
		hostname,
		role: 'guest',
		status: agentConnected ? 'online' : 'offline',
		source: 'agent',
		import_state: 'adopted',
		absent_since: null,
		agent_id: agentConnected === null ? null : `agent-${id}`,
		agent_connected: agentConnected ?? undefined,
		agent_version: agentConnected === null ? null : 'v2.9.0',
	};
}

const FLEET = [
	host('h-live', 'alive-box', true),
	host('h-dead-1', 'dead-box-1', false),
	host('h-dead-2', 'dead-box-2', false),
];

describe('Hosts fleet operations (#514 S3/S4)', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		listInventory.mockResolvedValue({ items: FLEET.map((h) => ({ ...h })), total: 3 });
		forgetAgent.mockResolvedValue({ forgotten: true });
	});

	it('clears the disconnected agents in ONE confirm, hosts stay, no reload', async () => {
		render(Hosts);
		await waitFor(() => expect(screen.getByText('dead-box-1')).toBeTruthy());
		const listCallsAfterLoad = listInventory.mock.calls.length;

		await fireEvent.click(
			screen.getByRole('button', { name: 'Select disconnected agents' })
		);
		await fireEvent.click(await screen.findByRole('button', { name: /Forget 2 agents/ }));

		const dialog = await screen.findByRole('dialog');
		expect(dialog.textContent).toContain('dead-box-1');
		expect(dialog.textContent).toContain('dead-box-2');

		await fireEvent.click(screen.getByRole('button', { name: 'Forget 2' }));

		// The MACHINES stay in inventory - only the agent link died.
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
		expect(screen.getByText('dead-box-1')).toBeTruthy();
		expect(screen.getByText('dead-box-2')).toBeTruthy();
		expect(forgetAgent).toHaveBeenCalledTimes(2);
		// And the table was never re-fetched: no reload-per-action jank.
		expect(listInventory.mock.calls.length).toBe(listCallsAfterLoad);
	});

	it('a failed forget keeps that agent chip and names the machine', async () => {
		forgetAgent
			.mockResolvedValueOnce({ forgotten: true })
			.mockRejectedValueOnce(new Error('agent is mid-reconnect'));
		render(Hosts);
		await waitFor(() => expect(screen.getByText('dead-box-1')).toBeTruthy());

		await fireEvent.click(
			screen.getByRole('button', { name: 'Select disconnected agents' })
		);
		await fireEvent.click(await screen.findByRole('button', { name: /Forget 2 agents/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Forget 2' }));

		// Both hosts remain either way; the failure is reported, not silent.
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
		expect(screen.getByText('dead-box-1')).toBeTruthy();
		expect(screen.getByText('dead-box-2')).toBeTruthy();
	});

	it('splits a mixed selection: dead agents forgettable, live ones revokable', async () => {
		render(Hosts);
		await waitFor(() => expect(screen.getByText('alive-box')).toBeTruthy());

		// Select all three rows through the header checkbox.
		const checkboxes = screen.getAllByRole('checkbox');
		await fireEvent.click(checkboxes[0]);

		expect(await screen.findByRole('button', { name: /Forget 2 agents/ })).toBeTruthy();
		expect(screen.getByRole('button', { name: /Revoke 1 agent/ })).toBeTruthy();
	});
});
