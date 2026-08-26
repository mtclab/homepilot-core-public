import { describe, it, expect } from 'vitest';
import { buildDriftRollup, checkState, driftSummary } from './drift';
import { timeAgo } from './relativeTime';
import type { Artifact, DriftCheck, Host } from './api';

/**
 * The drift rollup rules (#549 F4 + #425 honesty), asserted without a DOM.
 *
 * Teeth: make `checkState` trust `drifted: false` again and the errored check
 * lands in `inSpec`; return 0 instead of null from `coveragePct` and the
 * nothing-checked case starts quoting a percentage.
 */
function artifact(id: string, status = 'applied', host?: string): Artifact {
	return {
		id,
		status,
		kind: 'host-provision',
		intent: `intent ${id}`,
		created_at: '2026-08-01T00:00:00Z',
		...(host ? { target: { host } } : {}),
	};
}

function check(id: string, state: DriftCheck['state'], details?: unknown): DriftCheck {
	return {
		artifact_id: id,
		drifted: state === 'drifted',
		state,
		checked_at: '2026-08-25T12:00:00Z',
		details_json: details === undefined ? null : JSON.stringify(details),
	};
}

const HOSTS: Host[] = [{ id: 'h1', hostname: 'web01' }];

describe('checkState', () => {
	it('separates "I looked and it matches" from "I could not look"', () => {
		expect(checkState(check('a', 'in_spec'))).toBe('in-spec');
		expect(checkState(check('a', 'unknown'))).toBe('unknown');
		expect(checkState(check('a', 'drifted'))).toBe('drifted');
		expect(checkState(null)).toBe('unchecked');
	});
});

describe('driftSummary', () => {
	it('names what disagrees, capped, with the remainder counted', () => {
		const summary = driftSummary(
			'drifted',
			check('a', 'drifted', { drifted_items: ['p:a', 'p:b', 'p:c', 'p:d'] }),
		);
		expect(summary).toBe('4 differences: p:a, p:b, p:c +1 more');
	});

	it('turns an unknown check into the reason it gave', () => {
		expect(driftSummary('unknown', check('a', 'unknown', { reason: 'no_host' }))).toBe('no host');
	});

	it('never claims a diff it does not have', () => {
		expect(driftSummary('drifted', check('a', 'drifted'))).toBe('drift detected');
		expect(driftSummary('in-spec', check('a', 'in_spec'))).toBe('');
	});
});

describe('buildDriftRollup', () => {
	it('enumerates disagreement and counts the rest', () => {
		const artifacts = [
			artifact('d1', 'applied', 'web01'),
			artifact('ok1', 'applied', 'web01'),
			artifact('ok2', 'approved', 'web01'),
			artifact('e1', 'applied', 'web01'),
			artifact('never', 'applied', 'web01'),
			// Not active: rejected artifacts have no reality to disagree with.
			artifact('rej', 'rejected', 'web01'),
		];
		const rollup = buildDriftRollup(artifacts, HOSTS, [
			check('d1', 'drifted', { drifted_items: ['x'] }),
			check('ok1', 'in_spec'),
			check('ok2', 'in_spec'),
			check('e1', 'unknown', { reason: 'no_spec' }),
		]);

		expect(rollup.drifted.map((i) => i.artifact.id)).toEqual(['d1']);
		expect(rollup.unresolved.map((i) => i.artifact.id)).toEqual(['e1']);
		expect(rollup.inSpec.count).toBe(2);
		expect(rollup.uncheckedCount).toBe(1);
		expect(rollup.total).toBe(5);
		expect(rollup.checkedCount).toBe(4);
		expect(rollup.coveragePct).toBe(80);
	});

	it('has no percentage to quote when nothing was checked', () => {
		const rollup = buildDriftRollup([artifact('a', 'applied', 'web01')], HOSTS, []);
		expect(rollup.coveragePct).toBeNull();
		expect(rollup.checkedCount).toBe(0);
	});

	it('counts orphans without enumerating them, and never calls a global one orphaned', () => {
		const rollup = buildDriftRollup(
			[artifact('a', 'applied', 'ghost01'), artifact('b', 'applied')],
			HOSTS,
			[],
		);
		expect(rollup.orphanCount).toBe(1);
	});

	it('reports hosts no active artifact targets', () => {
		const hosts: Host[] = [...HOSTS, { id: 'h2', hostname: 'db01' }];
		const rollup = buildDriftRollup([artifact('a', 'applied', 'web01')], hosts, []);
		expect(rollup.uncoveredHosts.map((h) => h.hostname)).toEqual(['db01']);
	});
});

describe('timeAgo', () => {
	const now = new Date('2026-08-25T12:00:00Z');
	it('reads as elapsed time, not as a timestamp', () => {
		expect(timeAgo('2026-08-25T11:48:00Z', now)).toBe('12m ago');
		expect(timeAgo('2026-08-25T09:00:00Z', now)).toBe('3h ago');
		expect(timeAgo('2026-08-23T12:00:00Z', now)).toBe('2d ago');
	});
	it('does not read as the future when the clocks disagree', () => {
		expect(timeAgo('2026-08-25T12:00:30Z', now)).toBe('just now');
	});
	it('says never rather than inventing a date', () => {
		expect(timeAgo(null, now)).toBe('never');
		expect(timeAgo('not a date', now)).toBe('never');
	});
});
