import { get } from 'svelte/store';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { toast, notify, dismissToast } from '$lib/stores';

describe('toast store', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		dismissToast();
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

	// One slot, one timer. The first toast's dismiss timer must be cleared when
	// the second replaces it — otherwise it fires mid-life of the second and the
	// user loses the message that actually mattered (typically the error that
	// followed a success toast).
	it('a replacing toast gets the FULL dismiss window, not the leftover of the first', () => {
		notify('First');
		vi.advanceTimersByTime(3000);
		notify('Second', 'err');

		// The first toast's original 3500ms deadline passes: the second must survive.
		vi.advanceTimersByTime(500);
		expect(get(toast)).toEqual({ msg: 'Second', kind: 'err' });

		// And it dismisses 3500ms after IT was raised.
		vi.advanceTimersByTime(2999);
		expect(get(toast)).toEqual({ msg: 'Second', kind: 'err' });
		vi.advanceTimersByTime(1);
		expect(get(toast)).toBeNull();
	});

	it('a stale timer can never clear a newer toast, however many are raised', () => {
		notify('one');
		vi.advanceTimersByTime(1000);
		notify('two');
		vi.advanceTimersByTime(1000);
		notify('three');
		vi.advanceTimersByTime(3499);
		expect(get(toast)).toEqual({ msg: 'three', kind: 'ok' });
		vi.advanceTimersByTime(1);
		expect(get(toast)).toBeNull();
	});

	it('dismissToast clears the toast and its pending timer', () => {
		notify('gone');
		dismissToast();
		expect(get(toast)).toBeNull();
		notify('kept');
		// The dismissed toast's timer must not come back to clear this one.
		vi.advanceTimersByTime(3400);
		expect(get(toast)).toEqual({ msg: 'kept', kind: 'ok' });
	});
});