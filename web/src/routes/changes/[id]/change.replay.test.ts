import { render, screen, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../../lib/test-mocks';
import Detail from './+page.svelte';
import { api } from '$lib/api';

/**
 * The Replay button replays (#695).
 *
 * It was wired to `doApply`, i.e. `POST /artifacts/{id}/apply`, which the
 * backend refuses for anything not APPROVED - and Replay is only offered on an
 * APPLIED artifact, so it failed with a 409 every time it was pressed. The
 * assertion is on the call the page makes, which is what decides whether the
 * operator gets a replay at all.
 *
 * Teeth: wire Replay back to doApply and `replayArtifact` is never called while
 * `applyArtifact` is.
 */
const getArtifact = api.getArtifact as ReturnType<typeof vi.fn>;
const applyArtifact = api.applyArtifact as ReturnType<typeof vi.fn>;
const replayArtifact = api.replayArtifact as ReturnType<typeof vi.fn>;
const getTask = api.getTask as ReturnType<typeof vi.fn>;

function applied(extra: Record<string, unknown> = {}) {
	return {
		frontmatter: {
			id: 'a-1',
			kind: 'host-provision',
			status: 'applied',
			intent: 'Install nginx',
			target: { host: 'web01' },
			created_at: '2026-08-30T09:59:00Z',
			...extra,
		},
		body: 'the plan',
		active_task: null,
	};
}

describe('Change detail: Replay', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/changes/a-1', { id: 'a-1' });
		getArtifact.mockResolvedValue(applied());
		getTask.mockResolvedValue({ id: 't-9', status: 'running' });
		replayArtifact.mockResolvedValue({
			task_id: 't-9',
			artifact_id: 'a-1',
			status: 'pending',
			action: 'replay',
		});
	});

	it('calls the replay endpoint, never apply', async () => {
		render(Detail);
		const button = await screen.findByRole('button', { name: 'Replay' });

		await fireEvent.click(button);

		await vi.waitFor(() => expect(replayArtifact).toHaveBeenCalledWith('a-1'));
		expect(applyArtifact).not.toHaveBeenCalled();
	});

	it('is not offered when the artifact says it is not replay-safe', async () => {
		getArtifact.mockResolvedValue(applied({ replay_safe: false }));
		render(Detail);
		await screen.findByRole('button', { name: 'Revoke' });
		expect(screen.queryByRole('button', { name: 'Replay' })).toBeNull();
	});
});
