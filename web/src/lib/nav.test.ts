import { describe, it, expect } from 'vitest';
import { safeReturnTo } from './nav';

const BASE = '/ui';
const FALLBACK = '/ui/artifacts';

describe('safeReturnTo', () => {
	it('returns fallback for empty / missing input', () => {
		expect(safeReturnTo(null, BASE, FALLBACK)).toBe(FALLBACK);
		expect(safeReturnTo(undefined, BASE, FALLBACK)).toBe(FALLBACK);
		expect(safeReturnTo('', BASE, FALLBACK)).toBe(FALLBACK);
	});

	it('allows an in-app path under the base', () => {
		expect(safeReturnTo('/ui/inventory', BASE, FALLBACK)).toBe('/ui/inventory');
		expect(safeReturnTo('/ui/inventory?x=1', BASE, FALLBACK)).toBe('/ui/inventory?x=1');
	});

	it('allows the base path itself', () => {
		expect(safeReturnTo('/ui', BASE, FALLBACK)).toBe('/ui');
	});

	it('decodes percent-encoded input', () => {
		expect(safeReturnTo('%2Fui%2Fsettings', BASE, FALLBACK)).toBe('/ui/settings');
	});

	it('rejects off-origin absolute URLs', () => {
		expect(safeReturnTo('https://evil.com', BASE, FALLBACK)).toBe(FALLBACK);
		expect(safeReturnTo('http://evil.com/ui', BASE, FALLBACK)).toBe(FALLBACK);
	});

	it('rejects protocol-relative and backslash-tricked URLs', () => {
		expect(safeReturnTo('//evil.com', BASE, FALLBACK)).toBe(FALLBACK);
		expect(safeReturnTo('/\\evil.com', BASE, FALLBACK)).toBe(FALLBACK);
		expect(safeReturnTo('%2F%2Fevil.com', BASE, FALLBACK)).toBe(FALLBACK);
	});

	it('rejects a path outside the base (e.g. /ui-evil)', () => {
		expect(safeReturnTo('/ui-evil/x', BASE, FALLBACK)).toBe(FALLBACK);
		expect(safeReturnTo('/other', BASE, FALLBACK)).toBe(FALLBACK);
	});

	it('rejects malformed percent-encoding', () => {
		expect(safeReturnTo('%E0%A4%A', BASE, FALLBACK)).toBe(FALLBACK);
	});
});
