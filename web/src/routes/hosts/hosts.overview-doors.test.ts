import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '../../lib/test-mocks';
import Inventory from './+page.svelte';
import { api } from '$lib/api';

/**
 * The Overview's doors into Hosts (3.12): "Review" on the gone-machines line
 * opens `?gone=1`, the strip's "N stopped" opens `?pve_status=stopped`. Each must
 * reach the SERVER's filter (the list is paginated) and say on screen what the
 * list is showing, with the way out.
 *
 * Teeth: drop the URL read in onMount and both tests fail on the call.
 */
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;

describe('Hosts, opened from the Overview', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		listInventory.mockResolvedValue({ items: [], total: 0 });
	});
	afterEach(() => window.history.replaceState({}, '', '/'));

	it('lists only the machines Proxmox no longer reports for ?gone=1, and clears', async () => {
		window.history.replaceState({}, '', '/ui/hosts?gone=1');
		render(Inventory);
		await waitFor(() => expect(listInventory).toHaveBeenCalledWith(expect.objectContaining({ absent: true })));
		expect(screen.getByText('Showing the machines Proxmox no longer reports')).toBeInTheDocument();
		await fireEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0]);
		await waitFor(() => expect(listInventory.mock.calls.at(-1)?.[0].absent).toBeUndefined());
	});

	it('lists the stopped machines for ?pve_status=stopped', async () => {
		window.history.replaceState({}, '', '/ui/hosts?pve_status=stopped');
		render(Inventory);
		await waitFor(() =>
			expect(listInventory).toHaveBeenCalledWith(expect.objectContaining({ pve_status: 'stopped' })),
		);
		expect(screen.getByText('Showing stopped machines')).toBeInTheDocument();
	});
});
