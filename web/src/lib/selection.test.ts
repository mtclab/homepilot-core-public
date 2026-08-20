import { describe, it, expect } from 'vitest';
import { pruneSelection } from './selection';

const rows = (...ids: string[]) => ids.map((id) => ({ id }));

describe('pruneSelection', () => {
	it('keeps only ids that are still on screen', () => {
		const kept = pruneSelection(new Set(['a', 'b', 'c']), rows('a', 'c'));
		expect([...kept].sort()).toEqual(['a', 'c']);
	});

	it('drops everything when the filter hides every selected host', () => {
		// The bug: select 3 hosts, change the filter, hit Adopt — the invisible
		// hosts were still adopted and the "N selected" count lied.
		expect(pruneSelection(new Set(['a', 'b']), rows('x', 'y')).size).toBe(0);
	});

	it('never invents a selection for rows the user did not pick', () => {
		const kept = pruneSelection(new Set(['a']), rows('a', 'b', 'c'));
		expect([...kept]).toEqual(['a']);
	});

	it('handles an empty selection and an empty table', () => {
		expect(pruneSelection(new Set(), rows('a')).size).toBe(0);
		expect(pruneSelection(new Set(['a']), []).size).toBe(0);
	});

	it('returns a NEW set (Svelte reassignment, no mutation of the old one)', () => {
		const original = new Set(['a', 'b']);
		const kept = pruneSelection(original, rows('a'));
		expect(kept).not.toBe(original);
		expect(original.size).toBe(2);
	});
});
