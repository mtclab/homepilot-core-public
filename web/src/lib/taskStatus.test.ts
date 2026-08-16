import { describe, it, expect } from 'vitest';
import { taskStatusClass, isCancellable, shortTaskId } from './taskStatus';

describe('taskStatusClass', () => {
	it('maps every task status to a badge class', () => {
		expect(taskStatusClass('pending')).toBe('badge-proposed');
		expect(taskStatusClass('running')).toBe('badge-approved');
		expect(taskStatusClass('succeeded')).toBe('badge-applied');
		expect(taskStatusClass('failed')).toBe('badge-failed');
		expect(taskStatusClass('cancelled')).toBe('badge-revoked');
	});

	it('falls back to a neutral badge for an unknown status', () => {
		expect(taskStatusClass('bogus')).toBe('badge-proposed');
	});
});

describe('isCancellable', () => {
	it('is true only for in-flight states', () => {
		expect(isCancellable('pending')).toBe(true);
		expect(isCancellable('running')).toBe(true);
	});

	it('is false for terminal states', () => {
		expect(isCancellable('succeeded')).toBe(false);
		expect(isCancellable('failed')).toBe(false);
		expect(isCancellable('cancelled')).toBe(false);
	});
});

describe('shortTaskId', () => {
	it('returns the first uuid segment', () => {
		expect(shortTaskId('abcd1234-ef56-7890-ab12-cd34ef567890')).toBe('abcd1234');
	});

	it('handles empty input', () => {
		expect(shortTaskId('')).toBe('');
	});
});
