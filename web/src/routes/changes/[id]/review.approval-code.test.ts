import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../../lib/test-mocks';
import Detail from './+page.svelte';
import { api } from '$lib/api';

/**
 * The human-relay approval surface (#385 follow-up): the review screen shows the
 * per-artifact approval code near the Approve control so a human can read it and
 * relay it to the assistant, which cannot see it over MCP. A valid code proves a
 * human approved.
 *
 * Teeth: drop the {#if ... detail.approval_code} panel and the code no longer
 * reaches the operator, so this fails; drop the Clear-lock button and a locked
 * artifact can never be unlocked from the UI.
 */
const getArtifact = api.getArtifact as ReturnType<typeof vi.fn>;
const resetApprovalCode = api.resetApprovalCode as ReturnType<typeof vi.fn>;

function proposed(extra: Record<string, unknown> = {}) {
	return {
		frontmatter: {
			id: '2026-08-25-install-nginx-abcabc',
			kind: 'host-provision',
			status: 'proposed',
			intent: 'Install nginx',
			target: { host: 'web01' },
			created_at: '2026-08-25T10:00:00Z',
		},
		body: 'the plan',
		active_task: null,
		...extra,
	};
}

describe('Review screen approval code', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('shows the approval code near the Approve control while proposed', async () => {
		getArtifact.mockResolvedValue(proposed({ approval_code: 'ABCDE-FGHJK', approval_locked: false }));
		render(Detail);
		const value = await screen.findByTestId('approval-code-value');
		expect(value.textContent).toContain('ABCDE-FGHJK');
		// The Approve control is present on the same screen.
		expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy();
		// Not locked -> no clear-lock control.
		expect(screen.queryByTestId('approval-locked')).toBeNull();
	});

	it('offers a clear-lock control when locked and calls the reset API', async () => {
		getArtifact.mockResolvedValue(proposed({ approval_code: 'ABCDE-FGHJK', approval_locked: true }));
		resetApprovalCode.mockResolvedValue({ id: 'x', locked: false });
		render(Detail);
		await screen.findByTestId('approval-locked');
		const btn = screen.getByRole('button', { name: 'Clear approval lock' });
		await fireEvent.click(btn);
		await waitFor(() => expect(resetApprovalCode).toHaveBeenCalledTimes(1));
	});

	it('shows no approval code once the artifact is approved', async () => {
		getArtifact.mockResolvedValue({
			frontmatter: { ...proposed().frontmatter, status: 'approved' },
			body: 'the plan',
			active_task: null,
			approval_code: null,
		});
		render(Detail);
		await screen.findByText('the plan'); // body renders once loaded
		expect(screen.queryByTestId('approval-code')).toBeNull();
	});
});
