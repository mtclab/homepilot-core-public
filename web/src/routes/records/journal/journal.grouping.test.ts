import { render, screen, waitFor, within, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../../lib/test-mocks';
import Journal from './+page.svelte';
import { api } from '$lib/api';

/**
 * The journal is a grouped chronology of ONE-LINE entries that open on demand
 * (#549 F5).
 *
 * Fifty rows x seven columns of detail is the "long lists of things" the
 * facelift exists to remove: the details column alone carried a serialized
 * blob per row. The goal asserted here is the operator's: scan the day, then
 * open the one entry that matters.
 *
 * Teeth for the disclosure: render the detail unconditionally (drop the
 * `{#if openId === e.id}` guard) and "keeps the detail out of the DOM until
 * the entry is opened" fails - the command, exit code and details text are
 * found before any click.
 */

const listAudit = api.listAudit as ReturnType<typeof vi.fn>;

const NOW = new Date(2026, 7, 25, 14, 0); // 25 Aug 2026, local
const at = (day: number, hour: number, min = 0) =>
	new Date(2026, 7, day, hour, min).toISOString();

interface SeedEntry {
	id: number;
	timestamp: string;
	user_id: string;
	source: string;
	action: string;
	artifact_id: string | null;
	target_host: string | null;
	target_service: string | null;
	command: string | null;
	exit_code: number | null;
	snapshot_id: string | null;
	duration_ms: number | null;
	details_json: string | null;
}

function entry(over: Partial<SeedEntry> & { id: number; timestamp: string }): SeedEntry {
	return {
		user_id: 'olli',
		source: 'ui',
		action: 'apply',
		artifact_id: null,
		target_host: null,
		target_service: null,
		command: null,
		exit_code: null,
		snapshot_id: null,
		duration_ms: null,
		details_json: null,
		...over,
	};
}

const SEED: SeedEntry[] = [
	entry({
		id: 1,
		timestamp: at(25, 11, 5),
		action: 'apply',
		artifact_id: 'artifact-aaaa1111',
		target_host: 'web-01',
		command: 'systemctl restart nginx',
		exit_code: 0,
		duration_ms: 1240,
		details_json: JSON.stringify({ snapshot: 'snap-9', bytes: 4096 }),
	}),
	entry({ id: 2, timestamp: at(25, 9, 30), action: 'approve', target_host: 'web-01' }),
	entry({ id: 3, timestamp: at(24, 17, 0), action: 'revoke', target_host: 'db-02' }),
	entry({ id: 4, timestamp: at(21, 8, 0), action: 'host_added', target_host: 'cache-03' }),
];

function headings(): string[] {
	return screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent?.trim() ?? '');
}

describe('Journal — day groups and progressive disclosure', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vi.useFakeTimers({ shouldAdvanceTime: true });
		vi.setSystemTime(NOW);
		listAudit.mockResolvedValue({ items: SEED, total: SEED.length });
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it('groups the page by day, newest first', async () => {
		render(Journal);
		await screen.findByRole('heading', { level: 2, name: /^Today/ });

		const order = headings();
		expect(order[0]).toMatch(/^Today \(2\)$/);
		expect(order[1]).toMatch(/^Yesterday \(1\)$/);
		expect(order).toHaveLength(3);
		// The oldest group is a spelled-out date, not a bare key.
		expect(order[2]).not.toMatch(/^2026-08-21/);

		const today = screen
			.getByRole('heading', { level: 2, name: /^Today/ })
			.closest('section') as HTMLElement;
		expect(within(today).getByText('approve')).toBeInTheDocument();
		expect(within(today).queryByText('revoke')).not.toBeInTheDocument();
	});

	it('keeps the detail out of the DOM until the entry is opened', async () => {
		render(Journal);
		await screen.findByRole('heading', { level: 2, name: /^Today/ });

		// Collapsed: nothing of the detail exists to be read, or copied, or found
		// by ctrl-F. Not merely hidden with CSS - absent.
		expect(screen.queryByText(/systemctl restart nginx/)).not.toBeInTheDocument();
		expect(screen.queryByText(/snapshot=snap-9/)).not.toBeInTheDocument();
		expect(screen.queryByText('Exit code')).not.toBeInTheDocument();

		const summary = screen.getAllByRole('button', { expanded: false })[0];
		// The one line an operator scans: time, action, source, who/what.
		expect(summary).toHaveTextContent('apply');
		expect(summary).toHaveTextContent('web-01');

		await fireEvent.click(summary);

		await waitFor(() =>
			expect(screen.getByText(/systemctl restart nginx/)).toBeInTheDocument()
		);
		expect(screen.getByText('Exit code')).toBeInTheDocument();
		expect(screen.getByText(/snapshot=snap-9/)).toBeInTheDocument();
		expect(screen.getByText('1240 ms')).toBeInTheDocument();
		expect(summary).toHaveAttribute('aria-expanded', 'true');
	});

	it('closes the open entry again, taking its detail back out of the DOM', async () => {
		render(Journal);
		await screen.findByRole('heading', { level: 2, name: /^Today/ });

		const summary = screen.getAllByRole('button', { expanded: false })[0];
		await fireEvent.click(summary);
		await screen.findByText(/systemctl restart nginx/);

		await fireEvent.click(summary);
		await waitFor(() =>
			expect(screen.queryByText(/systemctl restart nginx/)).not.toBeInTheDocument()
		);
	});

	it('opens one entry at a time', async () => {
		render(Journal);
		await screen.findByRole('heading', { level: 2, name: /^Today/ });

		const summaries = screen.getAllByRole('button', { expanded: false });
		await fireEvent.click(summaries[0]);
		await screen.findByText(/systemctl restart nginx/);

		await fireEvent.click(summaries[1]);
		await waitFor(() =>
			expect(screen.queryByText(/systemctl restart nginx/)).not.toBeInTheDocument()
		);
		expect(screen.getAllByRole('button', { expanded: true })).toHaveLength(1);
	});

	it('keeps the filters, and reports an empty filtered result as filtered', async () => {
		render(Journal);
		await screen.findByRole('heading', { level: 2, name: /^Today/ });

		listAudit.mockResolvedValue({ items: [], total: 0 });
		await fireEvent.input(screen.getByPlaceholderText(/search artifact/i), {
			target: { value: 'nothing-matches-this' },
		});
		vi.advanceTimersByTime(400);

		expect(await screen.findByText(/no journal entries match the current filters/i))
			.toBeInTheDocument();
		// Both the filter bar and the empty card offer the way out.
		expect(screen.getAllByRole('button', { name: /clear filters/i }).length).toBeGreaterThan(0);
		// The search runs on the SERVER (#445 A4), not over the loaded page.
		expect(listAudit).toHaveBeenLastCalledWith(
			expect.objectContaining({ q: 'nothing-matches-this', offset: 0 }),
		);
	});

	it('still surfaces a load failure with its reason', async () => {
		listAudit.mockRejectedValue(new Error('gateway timeout'));
		render(Journal);

		expect(await screen.findByText(/could not load the journal/i)).toBeInTheDocument();
		expect(screen.getByText(/gateway timeout/i)).toBeInTheDocument();
		expect(screen.queryByText(/no journal entries yet/i)).not.toBeInTheDocument();
	});
});
