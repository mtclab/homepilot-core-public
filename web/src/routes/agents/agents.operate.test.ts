import { render, screen, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import Agents from './+page.svelte';
import { api } from '$lib/api';

/**
 * The fleet list has to answer two operator questions (#430).
 *
 * 1. "Which hosts still run the broken binary?" - after the 2.6.0 regression the
 *    only way to answer it was to SSH each box, because no agent reported a
 *    version at all.
 * 2. "Why is this host dark?" - a revoked agent, a banned peer, a duplicate
 *    identity and a powered-off machine were all the same grey dot.
 *
 * Teeth: drop the Version column, or the `last_error` line, and these fail.
 */
const listAgents = api.listAgents as ReturnType<typeof vi.fn>;

const FLEET = [
	{
		agent_id: 'aaaaaaaa-1111-2222-3333-444444444444',
		hostname: 'web01',
		system_info: { os: 'Linux', arch: 'amd64', agent_version: 'v2.8.0' },
		state: {},
		connected_at: '2026-08-21T10:00:00Z',
		last_heartbeat: '2026-08-21T10:05:00Z',
		stale_seconds: 5,
		connected: true,
		last_error: null,
	},
	{
		agent_id: 'bbbbbbbb-1111-2222-3333-444444444444',
		hostname: 'db01',
		system_info: { os: 'Linux', arch: 'arm64' },
		state: {},
		connected_at: '2026-08-01T09:00:00Z',
		last_heartbeat: '2026-08-01T09:00:00Z',
		connected: false,
		disconnected_at: '2026-08-01T09:30:00Z',
		last_error: 'credential revoked by an operator; re-enrol this host to restore it',
		last_error_at: '2026-08-01T09:30:00Z',
	},
];

describe('Agents page: the fleet can explain itself', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		listAgents.mockResolvedValue(FLEET);
	});

	it('shows each host the version of the binary it runs', async () => {
		render(Agents);

		expect(await screen.findByText('v2.8.0')).toBeInTheDocument();
	});

	it('says "unknown" for an agent that predates the version stamp', async () => {
		render(Agents);

		// Never invent a value: an unstamped binary is exactly the fleet an
		// upgrade hunt is looking for.
		expect(await screen.findByText('unknown')).toBeInTheDocument();
	});

	it('shows why a disconnected agent is gone', async () => {
		render(Agents);

		expect(await screen.findByText(/credential revoked by an operator/i)).toBeInTheDocument();
	});

	it('does not show a reason against a connected agent', async () => {
		listAgents.mockResolvedValue([{ ...FLEET[0], last_error: 'stale reason from long ago' }]);

		render(Agents);

		await screen.findByText('web01');
		expect(screen.queryByText(/stale reason from long ago/i)).not.toBeInTheDocument();
	});

	it('offers Revoke, which closes the live channel as well as the credential', async () => {
		const revokeAgent = api.revokeAgent as ReturnType<typeof vi.fn>;
		render(Agents);

		const revokeButtons = await screen.findAllByRole('button', { name: /^revoke$/i });
		revokeButtons[0].click();
		const confirm = await screen.findByRole('button', { name: /confirm revoke/i });
		confirm.click();

		await waitFor(() => expect(revokeAgent).toHaveBeenCalledWith(FLEET[0].agent_id));
	});
});
