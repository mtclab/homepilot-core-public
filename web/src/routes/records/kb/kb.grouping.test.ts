import { render, screen, waitFor, within, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../../lib/test-mocks';
import KB from './+page.svelte';
import { api } from '$lib/api';

/**
 * The knowledge base is grouped BY KIND, search stays the primary action, and
 * each entry is one line until it is opened (#549 F5).
 *
 * The kinds come off the payload rather than a hardcoded list: the KB's kind
 * column is open, and a page that only knows four kinds silently drops a fifth.
 * Teeth: seed a `runbook` entry - a kind no constant in the page mentions - and
 * assert it gets its own group and its entry. Replace `groupByKind(items, …)`
 * with a loop over a fixed ['note','policy','doc','fact'] and that entry
 * vanishes from the page while every other assertion here still passes.
 */

const listKB = api.listKB as ReturnType<typeof vi.fn>;
const searchKB = api.searchKB as ReturnType<typeof vi.fn>;

interface SeedEntry {
	id: number;
	source: string;
	kind: string;
	target?: string;
	title?: string;
	content: string;
}

const SEED: SeedEntry[] = [
	{ id: 1, source: 'ui', kind: 'doc', target: 'nginx', title: 'nginx layout', content: 'Sites live in /etc/nginx/sites-enabled.' },
	{ id: 2, source: 'cli', kind: 'note', target: 'web-01', title: 'disk was full', content: 'Cleared /var/log/journal in June.' },
	{ id: 3, source: 'mcp', kind: 'policy', title: 'no prod on Fridays', content: 'Applies are frozen after Thursday 17:00.' },
	{ id: 4, source: 'ui', kind: 'note', target: 'db-02', title: 'replica lag', content: 'Lag spikes during the nightly dump.' },
	// A kind no constant in the page knows about.
	{ id: 5, source: 'ingest', kind: 'runbook', target: 'haproxy', title: 'failover drill', content: 'Drain the backend, then flip the VIP.' },
];

function headings(): string[] {
	return screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent?.trim() ?? '');
}

describe('KB — grouped by kind, one line per entry', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		listKB.mockResolvedValue({ items: SEED, total: SEED.length });
		searchKB.mockResolvedValue({ results: [], total: 0 });
	});

	it('groups the entries by the kinds the DATA carries', async () => {
		render(KB);
		await screen.findByRole('heading', { level: 2, name: /^note/ });

		const order = headings();
		// Known kinds in reading order, then anything else alphabetically.
		expect(order).toEqual([
			expect.stringMatching(/^note\s*\(2\)$/),
			expect.stringMatching(/^policy\s*\(1\)$/),
			expect.stringMatching(/^doc\s*\(1\)$/),
			expect.stringMatching(/^runbook\s*\(1\)$/),
		]);

		// The unknown kind is not merely counted - its entry is reachable.
		const runbooks = screen
			.getByRole('heading', { level: 2, name: /^runbook/ })
			.closest('section') as HTMLElement;
		expect(within(runbooks).getByText('failover drill')).toBeInTheDocument();

		const notes = screen
			.getByRole('heading', { level: 2, name: /^note/ })
			.closest('section') as HTMLElement;
		expect(within(notes).getByText('disk was full')).toBeInTheDocument();
		expect(within(notes).queryByText('nginx layout')).not.toBeInTheDocument();
	});

	it('keeps the body behind a disclosure, one entry at a time', async () => {
		render(KB);
		await screen.findByRole('heading', { level: 2, name: /^note/ });

		expect(screen.queryByText(/Cleared \/var\/log\/journal/)).not.toBeInTheDocument();

		const summary = screen.getByRole('button', { name: /disk was full/ });
		expect(summary).toHaveAttribute('aria-expanded', 'false');
		await fireEvent.click(summary);

		await waitFor(() =>
			expect(screen.getByText(/Cleared \/var\/log\/journal/)).toBeInTheDocument()
		);

		// Opening another closes the first: the point of the disclosure is that
		// the page stays a list of lines.
		await fireEvent.click(screen.getByRole('button', { name: /replica lag/ }));
		await waitFor(() =>
			expect(screen.queryByText(/Cleared \/var\/log\/journal/)).not.toBeInTheDocument()
		);
		expect(screen.getAllByRole('button', { expanded: true })).toHaveLength(1);
	});

	it('puts search above every form, and searches on the server', async () => {
		render(KB);
		await screen.findByRole('heading', { level: 2, name: /^note/ });

		const search = screen.getByRole('searchbox', { name: /search the knowledge base/i });
		// "Above the fold" concretely: the search box precedes the create form
		// and the entry list in document order, whatever else is open.
		await fireEvent.click(screen.getByRole('button', { name: /new note/i }));
		const form = await screen.findByPlaceholderText(/enter knowledge base note content/i);
		expect(search.compareDocumentPosition(form) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

		searchKB.mockResolvedValue({ results: [SEED[2]], total: 1 });
		await fireEvent.input(search, { target: { value: 'fridays' } });
		await fireEvent.keyDown(search, { key: 'Enter' });

		await waitFor(() => expect(searchKB).toHaveBeenCalledWith('fridays', undefined, 50));
		expect(await screen.findByRole('heading', { level: 2, name: /^policy/ })).toBeInTheDocument();
		expect(headings()).toHaveLength(1);
	});

	it('never reports a failed search as an empty knowledge base (#489)', async () => {
		listKB.mockRejectedValue(new Error('Service unavailable'));
		render(KB);

		expect(await screen.findByText(/could not be searched/i)).toBeInTheDocument();
		expect(screen.getByText(/service unavailable/i)).toBeInTheDocument();
		expect(screen.queryByText(/no knowledge base entries yet/i)).not.toBeInTheDocument();
		// A failure renders NO groups: an empty grouped list would read as a
		// successful empty result all over again.
		expect(screen.queryByRole('heading', { level: 2 })).not.toBeInTheDocument();
	});
});
