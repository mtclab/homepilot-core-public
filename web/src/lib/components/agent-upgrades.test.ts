import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or it resolves the real $lib/api.
import '../test-mocks';
import AgentUpgrades from './AgentUpgrades.svelte';
import { api } from '$lib/api';

/**
 * Upgrade agent (Hosts > Agents): a live agent older than the control plane, or
 * reporting no version, is listed with the action that brings it current
 * through the install path - never a revoke, never the enrolment window.
 *
 * Teeth: drop the button, or send the install without `upgrade`, and the first
 * case fails.
 */
const getAgentUpgrades = api.getAgentUpgrades as ReturnType<typeof vi.fn>;
const installAgent = api.installAgent as ReturnType<typeof vi.fn>;
const getTask = api.getTask as ReturnType<typeof vi.fn>;
const revokeAgent = api.revokeAgent as ReturnType<typeof vi.fn>;
const openEnrolmentWindow = api.openEnrolmentWindow as ReturnType<typeof vi.fn>;

const OLD = {
	agent_id: 'agent-old',
	hostname: 'control-host',
	agent_version: null,
	control_version: '3.10.0',
	behind: null,
	host_id: 'host-7',
};

describe('AgentUpgrades', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		getAgentUpgrades.mockReset();
	});

	it('upgrades an unversioned agent through the install path', async () => {
		getAgentUpgrades.mockResolvedValue([OLD, { ...OLD, agent_id: 'agent-lxc', hostname: 'a-container', host_id: null, agent_version: '3.6.0', behind: true }]);
		installAgent.mockResolvedValue({ task_id: 't-9', status: 'pending', host_id: 'host-7' });
		getTask.mockResolvedValue({ id: 't-9', status: 'succeeded', error: null });

		render(AgentUpgrades, { props: { canWrite: true } });

		const rows = await screen.findAllByTestId('agent-upgrade');
		expect(rows[0].textContent).toMatch(/no version reported/);
		expect(rows[1].textContent).toMatch(/3\.6\.0/);
		const buttons = screen.getAllByRole('button', { name: 'Upgrade agent' });
		expect(buttons).toHaveLength(1);

		await fireEvent.click(buttons[0]);
		await waitFor(() => expect(installAgent).toHaveBeenCalledWith('host-7', true));
		await waitFor(() => expect(getTask).toHaveBeenCalledWith('t-9'));
		expect(revokeAgent).not.toHaveBeenCalled();
		expect(openEnrolmentWindow).not.toHaveBeenCalled();
	});

	it('offers a reader no button', async () => {
		getAgentUpgrades.mockResolvedValueOnce([OLD]);
		render(AgentUpgrades, { props: { canWrite: false } });
		await screen.findByTestId('agent-upgrade');
		expect(screen.queryByRole('button', { name: 'Upgrade agent' })).toBeNull();
	});

	it('renders nothing when there is nothing to upgrade', async () => {
		getAgentUpgrades.mockResolvedValueOnce([]);
		render(AgentUpgrades, { props: { canWrite: true } });
		await waitFor(() => expect(getAgentUpgrades).toHaveBeenCalled());
		expect(screen.queryByTestId('agent-upgrades')).toBeNull();
	});
});
