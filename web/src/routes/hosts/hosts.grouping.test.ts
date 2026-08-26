import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import Hosts from './+page.svelte';
import { api } from '$lib/api';

/**
 * The fleet page leads with what needs an operator (#549 F3, principle 1).
 *
 * THE JOURNEY: a mixed estate - a few machines in trouble, a lot that are fine,
 * a handful discovered and undecided. The page must open with the trouble, roll
 * the healthy ones up out of the way once there are enough of them to bury the
 * rest, and never render a heading for a population that is empty.
 *
 * Teeth: put the groups back in server order and the priority test fails; drop
 * the collapse and the "healthy rolled up" test fails (its rows are back on
 * screen); let the collapse apply to the attention group and the last test
 * fails, because a fault would be hidden by default.
 */
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;

function host(over: { id: string } & Record<string, unknown>) {
	return {
		hostname: over.id,
		role: 'guest',
		status: 'online',
		source: 'discovered',
		import_state: 'adopted',
		absent_since: null,
		...over,
	};
}

/** 1 offline + 1 gone, 12 healthy, 2 awaiting a decision. */
const MIXED = [
	host({ id: 'healthy-1' }),
	host({ id: 'broken-1', status: 'offline' }),
	host({ id: 'undecided-1', import_state: 'pending' }),
	...Array.from({ length: 11 }, (_, i) => host({ id: `healthy-${i + 2}` })),
	host({ id: 'gone-1', absent_since: '2026-08-01T09:00:00Z' }),
	host({ id: 'undecided-2', import_state: 'pending' }),
];

function groupOrder(container: HTMLElement): string[] {
	return Array.from(container.querySelectorAll('tbody[data-group]')).map(
		(el) => el.getAttribute('data-group') ?? '',
	);
}

describe('Hosts grouping (#549 F3)', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		try {
			globalThis.localStorage?.clear();
		} catch {
			/* storage may be unavailable; the page must not care */
		}
		listInventory.mockResolvedValue({ items: MIXED.map((h) => ({ ...h })), total: MIXED.length });
	});

	it('renders the groups in priority order, with counts', async () => {
		const { container } = render(Hosts);
		await waitFor(() => expect(screen.getByText('broken-1')).toBeTruthy());

		expect(groupOrder(container)).toEqual(['attention', 'managed', 'discovered']);
		// The counts are the populations, not the page size.
		expect(screen.getByText(/Needs attention \(2\)/)).toBeTruthy();
		expect(screen.getByRole('button', { name: /Managed \(12\)/ })).toBeTruthy();
		expect(screen.getByText(/Discovered \(2\)/)).toBeTruthy();
	});

	it('rolls the healthy machines up on a big fleet, and opens them on demand', async () => {
		render(Hosts);
		await waitFor(() => expect(screen.getByText('broken-1')).toBeTruthy());

		// THE GOAL: the two machines that want an operator are on screen and the
		// twelve that do not are out of the way - not merely "a flag was set".
		expect(screen.getByText('gone-1')).toBeTruthy();
		expect(screen.queryByText('healthy-1')).toBeNull();

		const toggle = screen.getByRole('button', { name: /Managed \(12\)/ });
		expect(toggle).toHaveAttribute('aria-expanded', 'false');
		await fireEvent.click(toggle);

		await waitFor(() => expect(screen.getByText('healthy-1')).toBeTruthy());
		expect(screen.getByRole('button', { name: /Managed \(12\)/ })).toHaveAttribute(
			'aria-expanded',
			'true',
		);
	});

	it('leaves a small fleet fully open', async () => {
		listInventory.mockResolvedValue({
			items: [host({ id: 'healthy-1' }), host({ id: 'broken-1', status: 'offline' })],
			total: 2,
		});
		render(Hosts);
		await waitFor(() => expect(screen.getByText('broken-1')).toBeTruthy());
		expect(screen.getByText('healthy-1')).toBeTruthy();
	});

	it('never collapses - or hides - the machines in trouble', async () => {
		render(Hosts);
		await waitFor(() => expect(screen.getByText('broken-1')).toBeTruthy());
		// The attention heading is not a toggle at all: there is no affordance
		// that could put a fault behind a disclosure triangle.
		expect(screen.queryByRole('button', { name: /Needs attention/ })).toBeNull();
		expect(screen.getByText('gone-1')).toBeTruthy();
	});

	it('renders no heading for a population that is empty', async () => {
		listInventory.mockResolvedValue({ items: [host({ id: 'healthy-1' })], total: 1 });
		const { container } = render(Hosts);
		await waitFor(() => expect(screen.getByText('healthy-1')).toBeTruthy());

		expect(groupOrder(container)).toEqual(['managed']);
		expect(screen.queryByText(/Needs attention/)).toBeNull();
		expect(screen.queryByText(/Discovered \(/)).toBeNull();
	});

	it('breaks the fleet down by role FLEET-wide, not page-wide', async () => {
		// The list is paged, so counting the rows on screen would describe page
		// one and call it the estate. The breakdown comes from the summary.
		(api.getDashboard as ReturnType<typeof vi.fn>).mockResolvedValue({
			inventory: { total: 40, by_role: { guest: 31, node: 9, worker: 0 } },
		});
		render(Hosts);
		await waitFor(() => expect(screen.getByText('Hosts by role')).toBeTruthy());

		const guest = await screen.findByRole('button', { name: /guest 31/ });
		expect(screen.getByRole('button', { name: /node 9/ })).toBeTruthy();
		// A role nothing is in is not a row of zero, it is absent.
		expect(screen.queryByRole('button', { name: /worker/ })).toBeNull();

		// And it is a door: the breakdown filters the table it sits above.
		await fireEvent.click(guest);
		await waitFor(() =>
			expect(listInventory).toHaveBeenCalledWith(expect.objectContaining({ role: 'guest' })),
		);
	});
});
