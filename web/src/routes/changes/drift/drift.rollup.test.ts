import { render, screen, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../../lib/test-mocks';
import Drift from './+page.svelte';
import { api } from '$lib/api';

/**
 * Drift is attention-first (#549 F4), and honest about what was checked (#425).
 *
 * The page used to enumerate every active artifact: forty green rows around the
 * two that actually disagree with reality. The rule now is that ONLY
 * disagreement is enumerated, and everything healthy is one line.
 *
 * TEETH, and they are the point of this file:
 *   - the in-spec artifacts' NAMES must not appear in the DOM at all. A
 *     "collapsed" section that still renders forty hidden rows would pass a
 *     count assertion and fail this one.
 *   - an ERRORED check must not be counted as in spec, and must be enumerated
 *     on its own — reverting `checkState` to trust `drifted: false` fails here.
 *   - with nothing checked, no percentage may appear anywhere on the page.
 */
const listArtifacts = api.listArtifacts as ReturnType<typeof vi.fn>;
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;
const getDriftStatus = api.getDriftStatus as ReturnType<typeof vi.fn>;

const NOW = '2026-08-25T12:00:00Z';

function artifact(id: string, intent: string, host: string) {
	return {
		id,
		kind: 'host-provision',
		status: 'applied',
		intent,
		target: { host },
		created_at: '2026-08-01T10:00:00Z',
	};
}

const DRIFTING = [
	artifact('drift-1', 'Install nginx', 'web01'),
	artifact('drift-2', 'Install redis', 'db01'),
];
// 39 healthy ones, each with a name that would be unmistakable in the DOM.
const IN_SPEC = Array.from({ length: 39 }, (_, i) =>
	artifact(`ok-${i}`, `HEALTHY-ARTIFACT-${i}`, `host-${i}`),
);
const ERRORED = artifact('err-1', 'Check the API gateway', 'gw01');

const ALL = [...DRIFTING, ...IN_SPEC, ERRORED];

const CHECKS = [
	{
		artifact_id: 'drift-1',
		drifted: true,
		state: 'drifted' as const,
		checked_at: NOW,
		details_json: JSON.stringify({ drifted_items: ['package:nginx', 'service:nginx'] }),
	},
	{
		artifact_id: 'drift-2',
		drifted: true,
		state: 'drifted' as const,
		checked_at: NOW,
		details_json: JSON.stringify({ drifted_items: ['service:redis'] }),
	},
	...IN_SPEC.map((a) => ({
		artifact_id: a.id,
		drifted: false,
		state: 'in_spec' as const,
		checked_at: NOW,
		details_json: null,
	})),
	{
		artifact_id: 'err-1',
		// The shape #425 is about: the executor could not look, and the stored
		// row still says `drifted: false`.
		drifted: false,
		state: 'unknown' as const,
		checked_at: NOW,
		details_json: JSON.stringify({ reason: 'no_host' }),
	},
];

describe('Drift rollup', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		listInventory.mockResolvedValue({ items: [], total: 0 });
	});
	afterEach(() => cleanup());

	it('enumerates only what disagrees and collapses the healthy ones to one line', async () => {
		listArtifacts.mockResolvedValue({ items: ALL, total: ALL.length });
		getDriftStatus.mockResolvedValue({ items: CHECKS, total: CHECKS.length });

		render(Drift);
		await screen.findByTestId('drift-attention');

		// Exactly three items are enumerated: the two that drifted and the one
		// whose check established nothing. Never the 39 that agree.
		const items = screen.getAllByTestId('drift-item');
		expect(items).toHaveLength(3);
		expect(screen.getByText('Install nginx')).toBeInTheDocument();
		expect(screen.getByText('Install redis')).toBeInTheDocument();

		// ONE line for the healthy ones, naming the count and nothing else.
		const summary = screen.getByTestId('drift-in-spec-summary');
		expect(summary.textContent).toContain('39 in spec');
		expect(summary.textContent).toContain('last checked');

		// TEETH: not one of the healthy artifacts' names reaches the DOM.
		const html = document.body.innerHTML;
		for (const a of IN_SPEC) {
			expect(html).not.toContain(a.intent);
		}
	});

	it('keeps an errored check out of "in spec" and gives it its own section', async () => {
		listArtifacts.mockResolvedValue({ items: ALL, total: ALL.length });
		getDriftStatus.mockResolvedValue({ items: CHECKS, total: CHECKS.length });

		render(Drift);
		const unresolved = await screen.findByTestId('drift-unresolved');

		// The errored one is named, with the reason the check gave.
		expect(unresolved.textContent).toContain('Check the API gateway');
		expect(unresolved.textContent).toContain('no host');
		// And it is NOT in the 39.
		expect(screen.getByTestId('drift-in-spec-summary').textContent).toContain('39 in spec');
	});

	it('summarises what a drifted artifact actually disagrees about', async () => {
		listArtifacts.mockResolvedValue({ items: ALL, total: ALL.length });
		getDriftStatus.mockResolvedValue({ items: CHECKS, total: CHECKS.length });

		render(Drift);
		await screen.findByTestId('drift-attention');

		const summaries = screen.getAllByTestId('drift-item-summary').map((el) => el.textContent ?? '');
		expect(summaries.some((t) => t.includes('package:nginx') && t.includes('service:nginx'))).toBe(
			true,
		);
	});

	it('quotes no percentage when nothing has been checked', async () => {
		listArtifacts.mockResolvedValue({ items: ALL, total: ALL.length });
		getDriftStatus.mockResolvedValue({ items: [], total: 0 });

		render(Drift);
		const coverage = await screen.findByTestId('drift-coverage');

		expect(coverage.textContent).toContain('Nothing checked yet');
		// TEETH: a coverage figure computed over zero checks is the "100% healthy"
		// lie #425 exists to stop. There must be no percent sign on the page.
		expect(document.body.textContent).not.toContain('%');
		expect(screen.queryByTestId('drift-in-spec-summary')).toBeNull();
		expect(screen.getByTestId('drift-unchecked-summary').textContent).toContain(
			`${ALL.length} not checked yet`,
		);
	});
});
