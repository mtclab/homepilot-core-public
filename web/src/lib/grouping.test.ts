import { describe, it, expect } from 'vitest';
import { dayKey, dayLabel, groupByDay, partition, groupByKind } from './grouping';

// The grouping RULES behind Records (#549 F5). Kept as pure functions so the
// question "which day does this row belong to, and where does that group sit"
// is answered once and asserted without mounting three routes.

const NOW = new Date(2026, 7, 25, 14, 30); // 25 Aug 2026, local

describe('dayKey', () => {
	it('is the LOCAL calendar day, not the UTC one', () => {
		// A timestamp is grouped by the day the operator lived, so the key must
		// come from the local date parts. Building it from `toISOString()` would
		// put an evening event in tomorrow's group west of UTC (and a morning one
		// in yesterday's east of it).
		const d = new Date(2026, 7, 25, 23, 45);
		expect(dayKey(d.toISOString())).toBe('2026-08-25');
	});

	it('pads month and day so keys sort as text', () => {
		expect(dayKey(new Date(2026, 0, 3, 12).toISOString())).toBe('2026-01-03');
	});

	it('returns no key for a missing or unparsable timestamp', () => {
		expect(dayKey(null)).toBe('');
		expect(dayKey('')).toBe('');
		expect(dayKey('not a date')).toBe('');
	});
});

describe('dayLabel', () => {
	it('names today and yesterday relative to now', () => {
		expect(dayLabel('2026-08-25', NOW)).toBe('Today');
		expect(dayLabel('2026-08-24', NOW)).toBe('Yesterday');
	});

	it('crosses a month boundary backwards', () => {
		expect(dayLabel('2026-07-31', new Date(2026, 7, 1, 9))).toBe('Yesterday');
	});

	it('spells older days out rather than leaving a bare key', () => {
		const label = dayLabel('2026-08-20', NOW);
		expect(label).not.toBe('2026-08-20');
		expect(label).toMatch(/20/);
	});

	it('labels rows with no timestamp instead of hiding them', () => {
		expect(dayLabel('', NOW)).toBe('Undated');
	});
});

describe('groupByDay', () => {
	const rows = [
		{ id: 'a', at: new Date(2026, 7, 25, 9).toISOString() },
		{ id: 'b', at: new Date(2026, 7, 24, 18).toISOString() },
		{ id: 'c', at: new Date(2026, 7, 25, 8).toISOString() },
		{ id: 'd', at: new Date(2026, 7, 23, 10).toISOString() },
	];

	it('orders groups newest day first', () => {
		const groups = groupByDay(rows, (r) => r.at, NOW);
		expect(groups.map((g) => g.key)).toEqual(['2026-08-25', '2026-08-24', '2026-08-23']);
		expect(groups.map((g) => g.label).slice(0, 2)).toEqual(['Today', 'Yesterday']);
	});

	it('keeps the server order inside a day', () => {
		const groups = groupByDay(rows, (r) => r.at, NOW);
		expect(groups[0].items.map((r) => r.id)).toEqual(['a', 'c']);
	});

	it('shows undated rows in a trailing group rather than dropping them', () => {
		const groups = groupByDay([...rows, { id: 'x', at: '' }], (r) => r.at, NOW);
		expect(groups[groups.length - 1].key).toBe('');
		expect(groups[groups.length - 1].items.map((r) => r.id)).toEqual(['x']);
		// Nothing is lost: every input row is in exactly one group.
		expect(groups.flatMap((g) => g.items).length).toBe(5);
	});

	it('has no groups at all for no rows', () => {
		expect(groupByDay([], (r: { at: string }) => r.at, NOW)).toEqual([]);
	});
});

describe('partition', () => {
	it('splits on the predicate, both halves keeping their order', () => {
		const [yes, no] = partition([1, 2, 3, 4, 5], (n) => n % 2 === 1);
		expect(yes).toEqual([1, 3, 5]);
		expect(no).toEqual([2, 4]);
	});
});

describe('groupByKind', () => {
	const entries = [
		{ id: 1, kind: 'doc' },
		{ id: 2, kind: 'note' },
		{ id: 3, kind: 'runbook' },
		{ id: 4, kind: 'note' },
		{ id: 5, kind: 'policy' },
	];

	it('groups on the kind the DATA carries, including kinds it has never seen', () => {
		const groups = groupByKind(entries, (e) => e.kind, ['note', 'policy', 'doc', 'fact']);
		// `runbook` is not in the preferred list; it must still get a group, or a
		// whole class of entries silently disappears from the page.
		expect(groups.map((g) => g.kind)).toEqual(['note', 'policy', 'doc', 'runbook']);
		expect(groups.flatMap((g) => g.items).length).toBe(entries.length);
	});

	it('never invents an empty group for a preferred kind the data lacks', () => {
		const groups = groupByKind([{ id: 1, kind: 'note' }], (e) => e.kind, ['note', 'policy']);
		expect(groups.map((g) => g.kind)).toEqual(['note']);
	});

	it('files a blank kind under one honest bucket', () => {
		const groups = groupByKind([{ id: 1, kind: '' }], (e) => e.kind, []);
		expect(groups.map((g) => g.kind)).toEqual(['other']);
	});
});
