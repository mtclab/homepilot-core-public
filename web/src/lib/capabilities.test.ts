import { describe, it, expect } from 'vitest';
import { canWrite, isAdmin, canRead } from './capabilities';

describe('canWrite', () => {
	it('is true for a write or admin capability', () => {
		expect(canWrite(['read', 'write'])).toBe(true);
		expect(canWrite(['read', 'write', 'admin'])).toBe(true);
		expect(canWrite(['admin'])).toBe(true);
	});

	it('is false for read-only or empty capabilities', () => {
		expect(canWrite(['read'])).toBe(false);
		expect(canWrite([])).toBe(false);
		expect(canWrite(null)).toBe(false);
		expect(canWrite(undefined)).toBe(false);
	});
});

describe('isAdmin', () => {
	it('is true only when admin is present', () => {
		expect(isAdmin(['read', 'write', 'admin'])).toBe(true);
		expect(isAdmin(['admin'])).toBe(true);
	});

	it('is false without admin', () => {
		expect(isAdmin(['read', 'write'])).toBe(false);
		expect(isAdmin(['read'])).toBe(false);
		expect(isAdmin(null)).toBe(false);
	});
});

describe('canRead', () => {
	it('is true for any real capability', () => {
		expect(canRead(['read'])).toBe(true);
		expect(canRead(['write'])).toBe(true);
		expect(canRead(['admin'])).toBe(true);
	});

	it('is false for empty/missing capabilities', () => {
		expect(canRead([])).toBe(false);
		expect(canRead(undefined)).toBe(false);
	});
});
