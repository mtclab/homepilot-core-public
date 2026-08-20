import { writable } from 'svelte/store';

export const toast = writable<{ msg: string; kind: 'ok' | 'err' } | null>(null);

const TOAST_MS = 3500;

// One slot, one timer. The dismiss timer MUST be cleared when a new toast
// replaces the old one — otherwise the first toast's timer fires mid-life of
// the second and the user loses the message they were actually meant to read.
let dismissTimer: ReturnType<typeof setTimeout> | null = null;

export function notify(msg: string, kind: 'ok' | 'err' = 'ok') {
	if (dismissTimer) clearTimeout(dismissTimer);
	toast.set({ msg, kind });
	dismissTimer = setTimeout(() => {
		dismissTimer = null;
		toast.set(null);
	}, TOAST_MS);
}

// Clears the current toast and its pending timer (used by tests and teardown).
export function dismissToast() {
	if (dismissTimer) clearTimeout(dismissTimer);
	dismissTimer = null;
	toast.set(null);
}
