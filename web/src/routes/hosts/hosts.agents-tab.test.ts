import { render, screen, waitFor, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or it resolves the real $lib/api.
import { setPage } from '../../lib/test-mocks';
import { api } from '$lib/api';
import HostsPage from './+page.svelte';

/**
 * UI slice 1 (owner decision 2): the agent chores moved to Hosts > Agents.
 * The fleet list no longer carries the join panel or the enrol controls on
 * every visit; it keeps ONE line naming who is waiting, with the door.
 *
 * Teeth: put JoinRequests / EnrollAgent back on the fleet page and the first
 * case fails; drop the strip and the waiting case fails; render nothing on a
 * failed read and the unread case fails.
 */
const getEnrolmentRequests = api.getEnrolmentRequests as ReturnType<typeof vi.fn>;

function requests(wants: number, refused = 0) {
	return {
		attempts: [],
		pending_tokens: [],
		window: { open: false, expires_at: null, seconds_remaining: 0, fleet_empty: false },
		summary: { wants_to_join: wants, refused, pending_tokens: 0, expired_unused_tokens: 0 },
	};
}

describe('Hosts fleet list after the Agents move', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/hosts');
	});

	it('carries neither the join panel nor the enrol controls', async () => {
		getEnrolmentRequests.mockResolvedValue(requests(0));
		render(HostsPage);
		await waitFor(() => expect(getEnrolmentRequests).toHaveBeenCalled());
		expect(screen.queryByTestId('join-requests')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Enroll agent' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Hub token' })).toBeNull();
		// Nobody waiting: no strip either.
		expect(screen.queryByTestId('joining-strip')).toBeNull();
	});

	it('says how many hosts are waiting to join, with the door to Agents', async () => {
		getEnrolmentRequests.mockResolvedValue(requests(2, 1));
		render(HostsPage);
		const strip = await screen.findByTestId('joining-strip');
		expect(strip.textContent).toMatch(/2 hosts waiting to join, 1 refused/);
		expect(screen.getByRole('link', { name: 'Open Agents' })).toHaveAttribute('href', '/ui/hosts/agents');
	});

	it('says so when it could not read who is waiting', async () => {
		getEnrolmentRequests.mockRejectedValue(new Error('503'));
		render(HostsPage);
		const strip = await screen.findByTestId('joining-strip');
		expect(strip.textContent).toMatch(/could not read who is waiting/i);
	});
});
