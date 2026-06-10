import { get } from 'svelte/store';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { toast, notify } from '$lib/stores';

describe('toast store', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		toast.set(null);
	});

	afterEach(() => {
		vi.useRealTimers();
	});

	it('starts as null', () => {
		expect(get(toast)).toBeNull();
	});

	it('notify sets a success toast', () => {
		notify('Connected');
		expect(get(toast)).toEqual({ msg: 'Connected', kind: 'ok' });
	});

	it('notify sets an error toast', () => {
		notify('Failed', 'err');
		expect(get(toast)).toEqual({ msg: 'Failed', kind: 'err' });
	});

	it('toast auto-clears after timeout', () => {
		notify('Hello');
		expect(get(toast)).toEqual({ msg: 'Hello', kind: 'ok' });

		vi.advanceTimersByTime(3500);
		expect(get(toast)).toBeNull();
	});

	it('notify replaces previous toast', () => {
		notify('First');
		notify('Second');
		expect(get(toast)).toEqual({ msg: 'Second', kind: 'ok' });
	});
});