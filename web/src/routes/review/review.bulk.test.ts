import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import Review from './+page.svelte';
import { api } from '$lib/api';

/**
 * Bulk approve/reject on the queue where a backlog actually piles up (#435).
 *
 * Inventory has had checkboxes and bulk actions for a while; the review queue had
 * none, so every decision was a mouse round trip. Approve also used a native
 * `confirm()` here while the same action elsewhere had none - the inconsistency
 * the issue names.
 *
 * Teeth: drop the per-row checkbox and single selection is impossible; drop the
 * failure collection in runBulk and a refused artifact vanishes silently.
 */
const listArtifacts = api.listArtifacts as ReturnType<typeof vi.fn>;
const approveArtifact = api.approveArtifact as ReturnType<typeof vi.fn>;

const QUEUE = [
	{
		id: '2026-08-21-first-aaaaaa',
		kind: 'host-provision',
		status: 'proposed',
		intent: 'Install nginx',
		target: { host: 'web01' },
		created_at: '2026-08-21T10:00:00Z',
	},
	{
		id: '2026-08-21-second-bbbbbb',
		kind: 'host-provision',
		status: 'proposed',
		intent: 'Install redis',
		target: { host: 'db01' },
		created_at: '2026-08-21T10:05:00Z',
	},
];

describe('Review queue bulk actions', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		listArtifacts.mockResolvedValue({ items: QUEUE, total: QUEUE.length });
		approveArtifact.mockResolvedValue({});
	});

	it('approves everything selected in one action', async () => {
		render(Review);
		await screen.findByText('Install nginx');

		await fireEvent.click(screen.getByRole('checkbox', { name: /select all/i }));
		await fireEvent.click(screen.getByRole('button', { name: /approve selected/i }));
		await fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }));

		await waitFor(() => expect(approveArtifact).toHaveBeenCalledTimes(2));
		expect(approveArtifact).toHaveBeenCalledWith(QUEUE[0].id);
		expect(approveArtifact).toHaveBeenCalledWith(QUEUE[1].id);
	});

	it('can select a single artifact from its own row', async () => {
		render(Review);
		await screen.findByText('Install nginx');

		await fireEvent.click(screen.getByRole('checkbox', { name: `Select ${QUEUE[1].id}` }));
		await fireEvent.click(screen.getByRole('button', { name: /approve selected/i }));
		await fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }));

		await waitFor(() => expect(approveArtifact).toHaveBeenCalledTimes(1));
		expect(approveArtifact).toHaveBeenCalledWith(QUEUE[1].id);
	});

	it('asks before acting on a batch', async () => {
		render(Review);
		await screen.findByText('Install nginx');

		await fireEvent.click(screen.getByRole('checkbox', { name: /select all/i }));
		await fireEvent.click(screen.getByRole('button', { name: /approve selected/i }));

		// Nothing has happened yet: the confirm step is the whole point.
		expect(approveArtifact).not.toHaveBeenCalled();
		expect(screen.getByRole('button', { name: /^cancel$/i })).toBeInTheDocument();
	});

	it('finishes the batch when one artifact is refused, and says which', async () => {
		const { notify } = await import('$lib/stores');
		approveArtifact
			.mockRejectedValueOnce(new Error('409 already applied'))
			.mockResolvedValueOnce({});
		render(Review);
		await screen.findByText('Install nginx');

		await fireEvent.click(screen.getByRole('checkbox', { name: /select all/i }));
		await fireEvent.click(screen.getByRole('button', { name: /approve selected/i }));
		await fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }));

		await waitFor(() => expect(approveArtifact).toHaveBeenCalledTimes(2));
		const messages = (notify as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
		expect(messages.some((m) => m.includes('failed'))).toBe(true);
	});

	it('offers no bulk bar to a read-only session', async () => {
		const { sessionStore } = await import('$lib/api');
		(sessionStore as unknown as { set: (v: unknown) => void }).set({
			authenticated: true,
			token_label: 'ro',
			capabilities: ['read'],
		});

		render(Review);
		await screen.findByText('Install nginx');

		expect(screen.queryByRole('button', { name: /approve selected/i })).not.toBeInTheDocument();
	});
});
