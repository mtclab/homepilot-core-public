import { describe, it, expect } from 'vitest';
import { taskStatusClass, isCancellable, isTerminalStatus, shortTaskId } from './taskStatus';
import type { TaskStatus } from './api';

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

describe('isTerminalStatus', () => {
	// The poller on the artifact detail page stops on this predicate. Missing a
	// terminal state here = a 2s poll loop that never ends, with the "in
	// progress" banner stuck and every action button disabled until reload.
	it('treats cancelled as terminal (the 2.7.0 state the old check omitted)', () => {
		expect(isTerminalStatus('cancelled')).toBe(true);
	});

	it('treats succeeded and failed as terminal', () => {
		expect(isTerminalStatus('succeeded')).toBe(true);
		expect(isTerminalStatus('failed')).toBe(true);
	});

	it('is false only while the task is in flight', () => {
		expect(isTerminalStatus('pending')).toBe(false);
		expect(isTerminalStatus('running')).toBe(false);
	});

	it('is terminal by DEFAULT for any status it has never seen', () => {
		// Forbids the whole class: a new backend state must stop the poller, not
		// spin it forever.
		expect(isTerminalStatus('expired')).toBe(true);
		expect(isTerminalStatus('timed_out')).toBe(true);
		expect(isTerminalStatus('')).toBe(true);
	});

	it('covers every declared TaskStatus', () => {
		const all: TaskStatus[] = ['pending', 'running', 'succeeded', 'failed', 'cancelled'];
		for (const s of all) {
			expect(isTerminalStatus(s)).toBe(!isCancellable(s));
		}
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
