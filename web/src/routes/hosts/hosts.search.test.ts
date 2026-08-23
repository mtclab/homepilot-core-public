import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import Inventory from './+page.svelte';
import { api } from '$lib/api';

/**
 * The search has to reach the SERVER (#445 A4).
 *
 * The inventory list is paginated. A search implemented as a browser-side filter
 * over `items` would only ever look at the page already fetched, and would
 * confidently report "no hosts match" for a host that exists on page three.
 * That is worse than having no search at all.
 *
 * Teeth: drop `q` from the listInventory call and the first test fails; drop the
 * debounce and the second fails (one request per keystroke).
 */
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;

describe('Inventory search', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		listInventory.mockResolvedValue({ items: [], total: 0 });
	});

	it('sends the query to the server rather than filtering the current page', async () => {
		render(Inventory);
		await waitFor(() => expect(listInventory).toHaveBeenCalled());

		const box = screen.getByPlaceholderText(/search name, address/i);
		await fireEvent.input(box, { target: { value: 'mail-relay' } });

		await waitFor(
			() =>
				expect(listInventory).toHaveBeenCalledWith(
					expect.objectContaining({ q: 'mail-relay' })
				),
			{ timeout: 2000 }
		);
	});

	it('keeps the dropdown filters when searching', async () => {
		render(Inventory);
		await waitFor(() => expect(listInventory).toHaveBeenCalled());

		// The first select is the role filter.
		const role = screen.getAllByRole('combobox')[0];
		// A role the backend actually writes; the dropdown no longer offers the
		// four that nothing ever matched (#424).
		await fireEvent.change(role, { target: { value: 'node' } });
		const box = screen.getByPlaceholderText(/search name, address/i);
		await fireEvent.input(box, { target: { value: 'mail' } });

		await waitFor(
			() =>
				expect(listInventory).toHaveBeenCalledWith(
					expect.objectContaining({ q: 'mail', role: 'node' })
				),
			{ timeout: 2000 }
		);
	});

	it('costs one request per pause in typing, not one per keystroke', async () => {
		render(Inventory);
		await waitFor(() => expect(listInventory).toHaveBeenCalled());
		listInventory.mockClear();

		const box = screen.getByPlaceholderText(/search name, address/i);
		for (const value of ['m', 'ma', 'mai', 'mail']) {
			await fireEvent.input(box, { target: { value } });
		}

		await waitFor(() => expect(listInventory).toHaveBeenCalledTimes(1), { timeout: 2000 });
	});
});
