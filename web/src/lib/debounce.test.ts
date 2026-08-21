import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { debounce } from './debounce';

beforeEach(() => {
	vi.useFakeTimers();
});

afterEach(() => {
	vi.useRealTimers();
});

describe('debounce', () => {
	it('does not run before the wait elapses', () => {
		const fn = vi.fn();
		const d = debounce(fn, 400);
		d();
		vi.advanceTimersByTime(399);
		expect(fn).not.toHaveBeenCalled();
		vi.advanceTimersByTime(1);
		expect(fn).toHaveBeenCalledTimes(1);
	});

	it('coalesces a burst of events into ONE call', () => {
		const fn = vi.fn();
		const d = debounce(fn, 400);
		// What an artifact bulk apply looks like on the SSE bus.
		for (let i = 0; i < 12; i++) {
			d();
			vi.advanceTimersByTime(20);
		}
		expect(fn).not.toHaveBeenCalled();
		vi.advanceTimersByTime(400);
		expect(fn).toHaveBeenCalledTimes(1);
	});

	it('fires again for a burst that arrives after the quiet period', () => {
		const fn = vi.fn();
		const d = debounce(fn, 400);
		d();
		vi.advanceTimersByTime(400);
		d();
		vi.advanceTimersByTime(400);
		expect(fn).toHaveBeenCalledTimes(2);
	});

	it('runs on the trailing edge with the newest arguments', () => {
		const fn = vi.fn();
		const d = debounce(fn, 400);
		d('first');
		d('second');
		vi.advanceTimersByTime(400);
		expect(fn).toHaveBeenCalledTimes(1);
		expect(fn).toHaveBeenCalledWith('second');
	});

	it('cancel() stops a pending call — nothing fires after teardown', () => {
		const fn = vi.fn();
		const d = debounce(fn, 400);
		d();
		expect(d.pending).toBe(true);
		d.cancel();
		expect(d.pending).toBe(false);
		vi.advanceTimersByTime(5000);
		expect(fn).not.toHaveBeenCalled();
	});

	it('flush() runs a pending call immediately, and is a no-op when idle', () => {
		const fn = vi.fn();
		const d = debounce(fn, 400);
		d.flush();
		expect(fn).not.toHaveBeenCalled();
		d();
		d.flush();
		expect(fn).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(5000);
		expect(fn).toHaveBeenCalledTimes(1);
	});
});
