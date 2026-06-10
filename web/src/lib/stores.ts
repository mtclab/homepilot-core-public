import { writable } from 'svelte/store';

export const toast = writable<{ msg: string; kind: 'ok' | 'err' } | null>(null);

export function notify(msg: string, kind: 'ok' | 'err' = 'ok') {
	toast.set({ msg, kind });
	setTimeout(() => toast.set(null), 3500);
}
