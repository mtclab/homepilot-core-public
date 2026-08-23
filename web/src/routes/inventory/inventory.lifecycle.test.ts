import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import Inventory from './+page.svelte';
import { api } from '$lib/api';

/**
 * Inventory lifecycle in the UI (#445 A5).
 *
 * A homelab that is not entirely Proxmox guests was unrepresentable, a destroyed
 * guest looked exactly like a powered-off one, and no host could be removed.
 *
 * Teeth: drop the "gone" badge and the first test fails; drop the source/absent
 * condition on Forget and the last one fails (it would be offered on a live
 * Proxmox guest, where the API refuses it anyway).
 */
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;

const LIVE_GUEST = {
	id: 'h-live',
	hostname: 'web01',
	role: 'guest',
	status: 'online',
	source: 'discovered',
	import_state: 'adopted',
	absent_since: null,
};

const DESTROYED_GUEST = {
	id: 'h-gone',
	hostname: 'old-vm',
	role: 'guest',
	status: 'offline',
	source: 'discovered',
	import_state: 'adopted',
	absent_since: '2026-08-01T09:00:00Z',
};

const MANUAL_HOST = {
	id: 'h-manual',
	hostname: 'nas01',
	role: 'service',
	status: 'unknown',
	source: 'manual',
	import_state: 'adopted',
	absent_since: null,
};

describe('Inventory lifecycle', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		listInventory.mockResolvedValue({
			items: [LIVE_GUEST, DESTROYED_GUEST, MANUAL_HOST],
			total: 3,
		});
	});

	it('only offers roles the backend actually writes', async () => {
		// The filter used to offer hypervisor / vm / container / service - none of
		// which anything writes - while omitting `node` and `guest` (#424).
		render(Inventory);
		await screen.findByText('web01');

		const roleFilter = screen.getAllByRole('combobox')[0];
		const offered = Array.from(roleFilter.querySelectorAll('option')).map((o) => o.value);

		expect(offered).toContain('node');
		expect(offered).toContain('guest');
		expect(offered).not.toContain('hypervisor');
		expect(offered).not.toContain('vm');
	});

	it('marks a host the hypervisor no longer reports as gone', async () => {
		render(Inventory);

		expect(await screen.findByText('gone')).toBeInTheDocument();
	});

	it('does not mark a live guest as gone', async () => {
		listInventory.mockResolvedValue({ items: [LIVE_GUEST], total: 1 });

		render(Inventory);

		await screen.findByText('web01');
		expect(screen.queryByText('gone')).not.toBeInTheDocument();
	});

	it('can add a host Proxmox has never heard of', async () => {
		const addHost = api.addHost as ReturnType<typeof vi.fn>;
		addHost.mockResolvedValue(MANUAL_HOST);
		render(Inventory);
		await waitFor(() => expect(listInventory).toHaveBeenCalled());

		await fireEvent.click(screen.getByRole('button', { name: /add host/i }));
		await fireEvent.input(screen.getByPlaceholderText('nas01'), {
			target: { value: 'nas01' },
		});
		await fireEvent.input(screen.getByPlaceholderText('10.0.0.4'), {
			target: { value: '10.0.0.4' },
		});
		await fireEvent.click(screen.getByRole('button', { name: /^add host$/i }));

		await waitFor(() =>
			expect(addHost).toHaveBeenCalledWith(
				expect.objectContaining({ hostname: 'nas01', ip_address: '10.0.0.4' })
			)
		);
	});

	it('offers Forget only where it can succeed', async () => {
		render(Inventory);
		await screen.findByText('web01');

		// The manual host and the destroyed guest: two. Never the live guest -
		// the API refuses that, because the next sync would bring it back.
		const forgetButtons = screen.getAllByRole('button', { name: /^forget$/i });
		expect(forgetButtons).toHaveLength(2);
	});
});
