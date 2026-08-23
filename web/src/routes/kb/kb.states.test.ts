import { render, screen, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import KB from './+page.svelte';
import { api } from '$lib/api';

/**
 * A failure must never be shown as an empty result (#445 B4).
 *
 * The page caught a failed search, cleared `items` and raised a toast. The
 * toast vanishes after a few seconds and the page is then rendering
 * "No knowledge base entries yet" - a failure presented as a successful empty
 * result. That is the worst way to be wrong here: the operator concludes the KB
 * is empty and stops looking, rather than retrying or fixing the backend.
 *
 * Teeth: restore `notify(String(e))` in place of setting `loadError` and both
 * assertions below fail - the empty-state copy comes back and the reason
 * disappears.
 */
const listKB = api.listKB as ReturnType<typeof vi.fn>;

describe('KB page states', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('shows the reason and a retry when the search fails', async () => {
		listKB.mockRejectedValue(new Error('Service unavailable'));

		render(KB);

		expect(await screen.findByText(/could not be searched/i)).toBeInTheDocument();
		expect(screen.getByText(/service unavailable/i)).toBeInTheDocument();
		expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
	});

	it('never reports a failed search as an empty knowledge base', async () => {
		listKB.mockRejectedValue(new Error('Service unavailable'));

		render(KB);

		await screen.findByText(/could not be searched/i);
		expect(screen.queryByText(/no knowledge base entries yet/i)).not.toBeInTheDocument();
	});

	it('still shows the empty state when the search genuinely returns nothing', async () => {
		listKB.mockResolvedValue({ items: [], total: 0 });

		render(KB);

		await waitFor(() =>
			expect(screen.getByText(/no knowledge base entries yet/i)).toBeInTheDocument()
		);
		expect(screen.queryByText(/could not be searched/i)).not.toBeInTheDocument();
	});
});
