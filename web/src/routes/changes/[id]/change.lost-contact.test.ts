import { render, screen, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../../lib/test-mocks';
import Detail from './+page.svelte';
import { api } from '$lib/api';

/**
 * Losing contact while a task is running (#648 tranche 7).
 *
 * The page polls the task it queued. A poll that threw used to call
 * `stopPolling()` and say nothing at all - so the spinner kept claiming the
 * task was "in progress", every action stayed disabled, and NOTHING was left
 * running that could ever correct either. The page reported a live task it was
 * no longer watching. That is the same dead end the `cancelled` allow-list
 * caused (its fix is commented in the source), reached by a different road.
 *
 * Two things have to hold at once, so both are gated:
 *   - one blip is not a lost task: keep polling, because the next poll answers;
 *   - a persistent failure must SAY the outcome is unknown, not keep spinning.
 *
 * Teeth: restore the bare `catch { stopPolling(); }` and the lost-contact test
 * fails on the missing sentence and on the surviving spinner; drop the retry
 * budget and the blip test fails when the second poll never happens.
 */
const getArtifact = api.getArtifact as ReturnType<typeof vi.fn>;
const getTask = api.getTask as ReturnType<typeof vi.fn>;

const RUNNING_TASK = {
	id: 'task-1',
	artifact_id: 'a-1',
	action: 'apply',
	status: 'running',
	result_json: null,
	created_at: '2026-08-30T10:00:00Z',
	finished_at: null,
	error: null,
};

function withActiveTask() {
	return {
		frontmatter: {
			id: 'a-1',
			kind: 'host-provision',
			status: 'approved',
			intent: 'Install nginx',
			target: { host: 'web01' },
			created_at: '2026-08-30T09:59:00Z',
		},
		body: 'the plan',
		active_task: RUNNING_TASK,
	};
}

describe('Change detail: losing contact with a running task', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vi.useFakeTimers();
		getArtifact.mockResolvedValue(withActiveTask());
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	/** Drive the 2s poll timer `n` times, letting each promise chain settle. */
	async function poll(n: number): Promise<void> {
		for (let i = 0; i < n; i += 1) {
			await vi.advanceTimersByTimeAsync(2000);
		}
	}

	it('keeps polling through a single blip rather than abandoning the task', async () => {
		getTask
			.mockRejectedValueOnce(new Error('network'))
			.mockResolvedValue({ ...RUNNING_TASK, status: 'running' });

		render(Detail);
		await vi.waitFor(() => expect(getArtifact).toHaveBeenCalled());

		await poll(2);

		// The second poll happened: the blip did not stop the timer.
		expect(getTask.mock.calls.length).toBeGreaterThanOrEqual(2);
		expect(screen.queryByText(/lost contact/i)).toBeNull();
	});

	it('says the outcome is unknown rather than spinning forever', async () => {
		getTask.mockRejectedValue(new Error('network'));
		// The recovery read fails too: the backend is genuinely gone.
		getArtifact.mockResolvedValueOnce(withActiveTask()).mockRejectedValue(new Error('network'));

		render(Detail);
		await vi.waitFor(() => expect(getArtifact).toHaveBeenCalled());

		await poll(4);

		await waitFor(() => expect(screen.getByText(/lost contact/i)).toBeInTheDocument());
		// And it stops claiming the task is still in progress.
		expect(screen.queryByText(/in progress/i)).toBeNull();
		// The operator is not stranded: there is a way to ask again.
		expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
	});
});
