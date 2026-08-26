import { render, screen, waitFor, within, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../../lib/test-mocks';
import Tasks from './+page.svelte';
import { api } from '$lib/api';

/**
 * Records → Tasks is a grouped chronology with the unfinished work pinned
 * (#549 F5).
 *
 * The goal these assertions are about is not "the page rendered rows". It is
 * the operator's journey: a task that failed three days ago must still be the
 * FIRST thing on the page, and yesterday's work must be visibly yesterday's.
 * The old page could only answer that by scrolling.
 *
 * Teeth:
 *   * drop the `partition(...)` pin and render one flat `groupByDay(tasks)` —
 *     "pins an old failure above today's finished work" fails, because the
 *     failed task lands in its own 22 Aug group at the BOTTOM.
 *   * drop the day grouping and render one table — the group-heading
 *     assertions fail: there are no headings to find.
 */

const listTasks = api.listTasks as ReturnType<typeof vi.fn>;

const NOW = new Date(2026, 7, 25, 14, 0); // 25 Aug 2026, local
const at = (day: number, hour: number) => new Date(2026, 7, day, hour).toISOString();

interface SeedTask {
	id: string;
	artifact_id: string | null;
	action: string;
	status: string;
	created_at: string;
	finished_at: string | null;
	error: string | null;
	result_json: string | null;
}

function task(over: Partial<SeedTask> & { id: string; created_at: string }): SeedTask {
	return {
		artifact_id: null,
		action: 'apply',
		status: 'succeeded',
		finished_at: over.created_at,
		error: null,
		result_json: null,
		...over,
	};
}

// Three days of tasks, with the two that need an operator deliberately OLD:
// a run still going since the 23rd and a failure from the 22nd.
const SEED: SeedTask[] = [
	task({ id: 'today-1', artifact_id: 'art-today', created_at: at(25, 11) }),
	task({ id: 'today-2', artifact_id: 'art-today-2', created_at: at(25, 9) }),
	task({ id: 'yday-1', artifact_id: 'art-yday', created_at: at(24, 16) }),
	task({
		id: 'running-old',
		artifact_id: 'art-running',
		status: 'running',
		created_at: at(23, 8),
		finished_at: null,
	}),
	task({
		id: 'failed-old',
		artifact_id: 'art-failed',
		status: 'failed',
		created_at: at(22, 8),
		error: 'ssh: connection refused',
		result_json: JSON.stringify({ execution_log: 'CONNECTING\nssh: connection refused' }),
	}),
];

/** The page's group headings, in the order an operator reads them. */
function headings(): string[] {
	return screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent?.trim() ?? '');
}

describe('Tasks — grouped chronology with the unfinished pinned', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vi.useFakeTimers({ shouldAdvanceTime: true });
		vi.setSystemTime(NOW);
		listTasks.mockResolvedValue({ items: SEED, total: SEED.length });
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it('leads with the tasks that need an operator, whatever day they ran', async () => {
		render(Tasks);
		await screen.findByRole('heading', { level: 2, name: /needs attention/i });

		const order = headings();
		expect(order[0]).toMatch(/Needs attention/);
		expect(order[0]).toMatch(/\(2\)/); // the running one AND the failed one

		const attention = screen
			.getByRole('heading', { level: 2, name: /needs attention/i })
			.closest('section') as HTMLElement;
		expect(within(attention).getByText('art-running')).toBeInTheDocument();
		expect(within(attention).getByText('art-failed')).toBeInTheDocument();
		// …and they are NOT also enumerated down in their own day groups.
		expect(screen.getAllByText('art-failed')).toHaveLength(1);
	});

	it('groups everything else by the day it ran, newest day first', async () => {
		render(Tasks);
		await screen.findByRole('heading', { level: 2, name: /needs attention/i });

		const order = headings();
		expect(order.slice(1, 3)).toEqual([
			expect.stringMatching(/^Today \(2\)$/),
			expect.stringMatching(/^Yesterday \(1\)$/),
		]);
		// The 23rd and the 22nd held only the pinned tasks, so no day group is
		// left over for them: a group with nothing in it is noise.
		expect(order).toHaveLength(3);

		const today = screen
			.getByRole('heading', { level: 2, name: /^Today/ })
			.closest('section') as HTMLElement;
		expect(within(today).getByText('art-today')).toBeInTheDocument();
		expect(within(today).queryByText('art-yday')).not.toBeInTheDocument();
	});

	it('keeps the per-row execution log behind its toggle (#487)', async () => {
		render(Tasks);
		// Anchored on the row itself, not on the grouping: this gate is about the
		// log toggle and must fail for that reason alone.
		await screen.findByText('art-failed');

		expect(screen.queryByText(/CONNECTING/)).not.toBeInTheDocument();
		const toggle = screen.getByRole('button', { name: /^Log$/ });
		expect(toggle).toHaveAttribute('aria-expanded', 'false');

		await fireEvent.click(toggle);
		await waitFor(() => expect(screen.getByText(/CONNECTING/)).toBeInTheDocument());
		expect(screen.getByRole('button', { name: /hide log/i })).toHaveAttribute(
			'aria-expanded',
			'true',
		);

		await fireEvent.click(screen.getByRole('button', { name: /hide log/i }));
		await waitFor(() => expect(screen.queryByText(/CONNECTING/)).not.toBeInTheDocument());
	});

	it('still offers cancel on an in-flight task, and only on that one', async () => {
		render(Tasks);
		await screen.findByText('art-running');

		// One cancel button: the running task. The failed one is terminal, and
		// so is everything in the day groups.
		const cancels = screen.getAllByRole('button', { name: /^Cancel$/ });
		expect(cancels).toHaveLength(1);

		const attention = screen
			.getByRole('heading', { level: 2, name: /needs attention/i })
			.closest('section') as HTMLElement;
		expect(attention.contains(cancels[0])).toBe(true);
	});

	it('keeps the truncation notice when the list is capped', async () => {
		listTasks.mockResolvedValue({ items: SEED, total: 812 });
		render(Tasks);
		expect(await screen.findByText(/Showing newest 5 of 812/)).toBeInTheDocument();
	});

	it('says so plainly when there are no tasks at all', async () => {
		listTasks.mockResolvedValue({ items: [], total: 0 });
		render(Tasks);
		expect(await screen.findByText(/no tasks yet/i)).toBeInTheDocument();
		expect(screen.queryByRole('heading', { level: 2 })).not.toBeInTheDocument();
	});
});
